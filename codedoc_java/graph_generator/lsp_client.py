"""Minimal asyncio LSP client over stdio (JSON-RPC 2.0 with Content-Length framing).

Purpose-built for driving the Eclipse JDT language server (jdtls) from the
pipeline (Phase 1.6 semantic resolution). Design constraints proven by
scripts/lsp_spike.py and scripts/jdtls_spike.py:
  - the client MUST answer three server->client requests during solution load
    (workspace/configuration, client/registerCapability,
    window/workDoneProgress/create) or the server stalls;
  - unknown server->client requests get a spec-correct MethodNotFound error;
  - $/progress "end" events signal solution-load milestones;
  - a dying server must fail all in-flight requests immediately.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from collections.abc import Callable
from typing import Any


class LspError(Exception):
    """Error response from the server for one of our requests."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class LspTimeout(Exception):
    """A request exceeded its deadline (the server is still running)."""


class LspConnectionError(Exception):
    """The server process died or the stdio channel broke."""


_STREAM_LIMIT = 4 * 1024 * 1024  # decompiled-source payloads can be large
_NOTIFICATION_HISTORY_LIMIT = 50


class LspClient:
    """One LSP server subprocess with request/response correlation."""

    def __init__(
        self,
        cmd: list[str],
        cwd: str,
        config_sections: dict[str, Any] | None = None,
        default_timeout: float = 60.0,
        watch_notifications: set[str] | None = None,
        workspace_folders: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self._cmd = cmd
        self._cwd = cwd
        # jdtls picks its JVM from JAVA_HOME — the launcher env must be
        # overridable per session (the pipeline pins JDK 21 there).
        self._env = dict(env) if env is not None else None
        self._config_sections = config_sections or {}
        self._default_timeout = default_timeout
        self._workspace_folders = list(workspace_folders or [])
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._send_lock: asyncio.Lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._stopped = False
        self.progress_ends = 0
        self._progress_event: asyncio.Event = asyncio.Event()
        self.notification_counts: dict[str, int] = {}
        self.notification_params: dict[str, deque[Any]] = {
            m: deque(maxlen=_NOTIFICATION_HISTORY_LIMIT)
            for m in (watch_notifications or set())
        }
        self._notification_events: dict[str, asyncio.Event] = {
            m: asyncio.Event() for m in (watch_notifications or set())}

    # -- lifecycle ----------------------------------------------------

    async def start(self):
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=_STREAM_LIMIT,
            env=self._env,
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def stop(self):
        """Graceful shutdown; safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        if self.is_alive:
            try:
                await self.request("shutdown", None, timeout=3.0)
                await self.notify("exit", None)
            except Exception:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._proc.kill()
                    await self._proc.wait()
                except ProcessLookupError:
                    pass
        self._fail_pending(LspConnectionError("client stopped"))

    # -- protocol I/O -------------------------------------------------

    async def request(self, method: str, params: Any, timeout: float | None = None) -> Any:
        if not self.is_alive:
            raise LspConnectionError(f"server not running (request {method})")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout or self._default_timeout)
        except asyncio.TimeoutError:
            raise LspTimeout(f"{method} timed out after {timeout or self._default_timeout}s")
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Any):
        if not self.is_alive:
            raise LspConnectionError(f"server not running (notify {method})")
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def wait_progress_end(self, timeout: float = 60.0):
        """Return once at least one $/progress end has been observed."""
        if self.progress_ends > 0:
            return
        try:
            await asyncio.wait_for(self._progress_event.wait(), timeout)
        except asyncio.TimeoutError:
            raise LspTimeout(f"no $/progress end within {timeout}s")

    def notification_count(
        self,
        method: str,
        predicate: Callable[[Any], bool] | None = None,
    ) -> int:
        """Count watched notifications, optionally matching retained params."""
        if predicate is None:
            return self.notification_counts.get(method, 0)
        return sum(
            1 for params in self.notification_params.get(method, ())
            if predicate(params)
        )

    async def wait_notification(
        self,
        method: str,
        predicate: Callable[[Any], bool] | None = None,
        timeout: float = 60.0,
    ):
        """Return once a watched notification matching ``predicate`` arrives."""
        # Preserve the old wait_notification(method, timeout) positional form.
        if predicate is not None and not callable(predicate):
            if isinstance(predicate, (int, float)) and not isinstance(predicate, bool):
                timeout = float(predicate)
                predicate = None
            else:
                raise TypeError("predicate must be callable or None")

        event = self._notification_events.get(method)
        if event is None:
            raise LspTimeout(f"notification {method} is not watched")

        if predicate is None:
            if self.notification_counts.get(method, 0) > 0:
                return
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                raise LspTimeout(f"no {method} notification within {timeout}s")
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self.notification_count(method, predicate) > 0:
                return

            # The event may still be set by a non-matching notification. Clear
            # it, then re-check history before awaiting so no notification can
            # be lost between the check and the wait.
            event.clear()
            if self.notification_count(method, predicate) > 0:
                return

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise LspTimeout(f"no matching {method} notification within {timeout}s")
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError:
                raise LspTimeout(f"no matching {method} notification within {timeout}s")

    async def _send(self, msg: dict):
        assert self._proc is not None and self._proc.stdin is not None
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        frame = b"Content-Length: %d\r\n\r\n" % len(body) + body
        try:
            async with self._send_lock:
                self._proc.stdin.write(frame)
                await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise LspConnectionError(f"stdio write failed: {e}")

    async def _read_loop(self):
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    header_blob = await stdout.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    break
                length = 0
                for line in header_blob.decode("ascii", "replace").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                if length <= 0:
                    continue
                try:
                    body = await stdout.readexactly(length)
                except asyncio.IncompleteReadError:
                    break
                try:
                    msg = json.loads(body.decode("utf-8"))
                except ValueError:
                    continue
                await self._handle(msg)
        except asyncio.CancelledError:
            raise
        finally:
            if self._proc is not None and self._proc.returncode is None:
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
            self._fail_pending(LspConnectionError("server closed the stdio channel"))

    async def _handle(self, msg: dict):
        if "method" in msg and "id" in msg:
            await self._answer_server_request(msg)
        elif "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                if "error" in msg:
                    err = msg["error"] or {}
                    fut.set_exception(LspError(err.get("code", 0), err.get("message", ""), err.get("data")))
                else:
                    fut.set_result(msg.get("result"))
        else:
            method = msg.get("method")
            if method == "$/progress":
                value = (msg.get("params") or {}).get("value") or {}
                if value.get("kind") == "end":
                    self.progress_ends += 1
                    self._progress_event.set()
            if method in self._notification_events:
                self.notification_params[method].append(msg.get("params"))
                self.notification_counts[method] = self.notification_counts.get(method, 0) + 1
                self._notification_events[method].set()

    async def _answer_server_request(self, msg: dict):
        method = msg["method"]
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
        if method == "workspace/configuration":
            items = (msg.get("params") or {}).get("items", [])
            reply["result"] = [self._config_sections.get(item.get("section", "")) for item in items]
        elif method in ("client/registerCapability", "client/unregisterCapability",
                        "window/workDoneProgress/create"):
            reply["result"] = None
        elif method == "workspace/workspaceFolders":
            reply["result"] = [{"uri": "file://" + f, "name": os.path.basename(f.rstrip("/"))}
                               for f in self._workspace_folders]
        else:
            reply["error"] = {"code": -32601, "message": f"unhandled server request: {method}"}
        try:
            await self._send(reply)
        except LspConnectionError:
            pass

    def _fail_pending(self, exc: Exception):
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
