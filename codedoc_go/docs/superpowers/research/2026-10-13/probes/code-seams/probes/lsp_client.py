"""Asyncio LSP client over stdio (JSON-RPC 2.0 with Content-Length framing).

Forked from codedoc_java's lsp_client.py for pyright-langserver (spec §5.1).
Changes from the Java client:
  - file URIs are percent-encoded from the path's bytes (spaces, '#', '%',
    non-ASCII, undecodable bytes) and decoded back exactly; a raw '#' made
    pyright drop the rest of the path (probe D3);
  - workspace/configuration is answered per item through
    config_answer(section, scopeUri);
  - every other server->client request gets a null result: a -32601 reply
    to workspace/diagnostic/refresh made pyright exit (probe B2);
  - a notification waiter is registered before history is checked and is
    resolved by the read loop when a matching message arrives, so a match
    followed by many other notifications is never missed;
  - a timed-out request is cancelled with $/cancelRequest;
  - a write the server does not read within the timeout breaks the session
    (a half-written frame leaves the channel unusable);
  - a dying server fails every in-flight request and notification waiter.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote_to_bytes, urlparse


class LspError(Exception):
    """Error response from the server for one of our requests."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class LspTimeout(Exception):
    """A request or a wait exceeded its deadline (the server is still running)."""


class LspConnectionError(Exception):
    """The server process died or the stdio channel broke."""


_STREAM_LIMIT = 4 * 1024 * 1024
_NOTIFICATION_HISTORY_LIMIT = 10_000


def path_to_uri(path: str) -> str:
    """file:// URI of an absolute path: its bytes (os.fsencode) percent-encoded except letters, digits, '_.-~' and '/'."""
    return "file://" + quote(os.fsencode(os.path.abspath(path)), safe="/")


def uri_to_path(uri: str) -> str:
    """Path of a file:// URI: percent-decoded bytes through os.fsdecode; no Unicode normalization.

    Raises ValueError for other schemes.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"not a file URI: {uri!r}")
    return os.fsdecode(unquote_to_bytes(parsed.path))


class LspClient:
    """One LSP server subprocess with request/response correlation."""

    def __init__(
        self,
        cmd: list[str],
        cwd: str,
        config_answer: Callable[[str, str | None], Any] | None = None,
        default_timeout: float = 60.0,
        watch_notifications: set[str] | None = None,
        workspace_folders: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._cmd = list(cmd)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._config_answer = config_answer
        self._default_timeout = default_timeout
        self._workspace_folders = list(workspace_folders or [])
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._stopped = False
        self._closed = False
        self._watched = set(watch_notifications or ())
        self.notification_counts: dict[str, int] = {}
        self.notification_params: dict[str, list[Any]] = {m: [] for m in self._watched}
        self._waiters: list[tuple[str, Callable[[Any], bool], asyncio.Future]] = []

    # -- lifecycle ----------------------------------------------------

    async def start(self) -> None:
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

    async def stop(self) -> None:
        """Graceful shutdown; safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        graceful = False
        if self.is_alive and not self._closed:
            try:
                await self.request("shutdown", None, timeout=3.0)
                await self.notify("exit", None)
                graceful = True
            except Exception:
                pass
        if graceful and self._proc is not None and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
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
        self._closed = True
        self._fail_pending(LspConnectionError("client stopped"))

    # -- protocol I/O -------------------------------------------------

    async def request(self, method: str, params: Any, timeout: float | None = None) -> Any:
        if self._closed or not self.is_alive:
            raise LspConnectionError(f"server not running (request {method})")
        limit = self._default_timeout if timeout is None else timeout
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                             limit)
            return await asyncio.wait_for(fut, limit)
        except asyncio.TimeoutError:
            try:
                await self.notify("$/cancelRequest", {"id": rid})
            except LspConnectionError:
                pass
            raise LspTimeout(f"{method} timed out after {limit}s") from None
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Any) -> None:
        if self._closed or not self.is_alive:
            raise LspConnectionError(f"server not running (notify {method})")
        await self._send({"jsonrpc": "2.0", "method": method, "params": params},
                         self._default_timeout)

    def notification_count(self, method: str,
                           predicate: Callable[[Any], bool] | None = None) -> int:
        """Count watched notifications of ``method``, optionally only those matching ``predicate``."""
        if predicate is None:
            return self.notification_counts.get(method, 0)
        return sum(1 for params in self.notification_params.get(method, ())
                   if _matches(predicate, params))

    async def wait_notification(self, method: str,
                                predicate: Callable[[Any], bool] | None = None,
                                timeout: float = 60.0) -> Any:
        """Return the params of the first watched ``method`` notification matching ``predicate``.

        The waiter is registered before history is checked; the read loop
        resolves it when a matching notification arrives.
        """
        if method not in self._watched:
            raise LspTimeout(f"notification {method} is not watched")
        pred = predicate if predicate is not None else (lambda _params: True)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        waiter = (method, pred, fut)
        self._waiters.append(waiter)
        try:
            for params in self.notification_params[method]:
                if _matches(pred, params):
                    return params
            if self._closed:
                raise LspConnectionError(f"server closed before {method} arrived")
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise LspTimeout(f"no matching {method} notification within {timeout}s") from None
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    async def _send(self, msg: dict, timeout: float) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        body = json.dumps(msg, ensure_ascii=True).encode("ascii")
        frame = b"Content-Length: %d\r\n\r\n" % len(body) + body
        try:
            async with self._send_lock:
                self._proc.stdin.write(frame)
                await asyncio.wait_for(self._proc.stdin.drain(), timeout)
        except asyncio.TimeoutError:
            error = LspConnectionError(f"server did not read its input within {timeout}s")
            self._closed = True
            self._fail_pending(error)
            raise error from None
        except (BrokenPipeError, ConnectionResetError) as e:
            raise LspConnectionError(f"stdio write failed: {e}") from e

    async def _read_loop(self) -> None:
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
            self._closed = True
            self._fail_pending(LspConnectionError("server closed the stdio channel"))

    async def _handle(self, msg: dict) -> None:
        if "method" in msg and "id" in msg:
            await self._answer_server_request(msg)
        elif "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                if "error" in msg:
                    err = msg["error"] or {}
                    fut.set_exception(LspError(err.get("code", 0), err.get("message", ""),
                                               err.get("data")))
                else:
                    fut.set_result(msg.get("result"))
        else:
            method = msg.get("method")
            if method not in self._watched:
                return
            params = msg.get("params")
            history = self.notification_params[method]
            history.append(params)
            if len(history) > _NOTIFICATION_HISTORY_LIMIT:
                del history[0]
            self.notification_counts[method] = self.notification_counts.get(method, 0) + 1
            for wmethod, pred, fut in list(self._waiters):
                if wmethod == method and not fut.done() and _matches(pred, params):
                    fut.set_result(params)

    async def _answer_server_request(self, msg: dict) -> None:
        method = msg["method"]
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
        if method == "workspace/configuration":
            items = (msg.get("params") or {}).get("items") or []
            answer = self._config_answer
            reply["result"] = [
                answer(item.get("section") or "", item.get("scopeUri")) if answer else None
                for item in items]
        elif method == "workspace/workspaceFolders":
            reply["result"] = [{"uri": path_to_uri(f), "name": os.path.basename(f.rstrip("/"))}
                               for f in self._workspace_folders]
        else:
            reply["result"] = None
        try:
            await self._send(reply, self._default_timeout)
        except LspConnectionError:
            pass

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        for _method, _pred, fut in list(self._waiters):
            if not fut.done():
                fut.set_exception(exc)


def _matches(predicate: Callable[[Any], bool], params: Any) -> bool:
    try:
        return bool(predicate(params))
    except Exception:
        return False
