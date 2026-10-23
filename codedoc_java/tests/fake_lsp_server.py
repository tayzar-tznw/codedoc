#!/usr/bin/env python3
"""Scripted fake LSP server (stdio) for lsp_client unit tests.

Behaviors are driven by the request method name the client sends:
  ping            -> immediate result {"pong": <params>}
  slow            -> result {"pong": "slow"} after 0.15s
  split_pong      -> result {"pong": "split"} with the frame written in 3 chunks
  echo_via_config -> server issues workspace/configuration to the client,
                     then answers with {"config": <client response result>}
  do_caps_progress-> server issues client/registerCapability and
                     window/workDoneProgress/create, then emits $/progress
                     begin+end notifications, then answers
                     {"acks": [<reg resp>, <prog resp>]}
  weird_probe     -> server issues bogus/serverRequest, answers
                     {"reply": {"result":..., "error": {code,...}|None}}
  emit_status_sequence
                  -> emits the requested notification sequence, then answers
                     {"emitted": <count>}
  boom            -> error response {code: -32099, message: "boom failed"}
  sleepy          -> never answered
  die             -> process exits immediately without responding
"""

import json
import os
import sys
import threading
import time

_out_lock = threading.Lock()
_next_id = 1000
_pending_server: dict[int, dict] = {}
_pending_cond = threading.Condition()


def write_msg(msg: dict, chunks: int = 1):
    body = json.dumps(msg).encode("utf-8")
    frame = b"Content-Length: %d\r\n\r\n" % len(body) + body
    with _out_lock:
        if chunks <= 1:
            sys.stdout.buffer.write(frame)
            sys.stdout.buffer.flush()
        else:
            step = max(1, len(frame) // chunks)
            for i in range(0, len(frame), step):
                sys.stdout.buffer.write(frame[i:i + step])
                sys.stdout.buffer.flush()
                time.sleep(0.02)


def read_msg():
    headers = b""
    while not headers.endswith(b"\r\n\r\n"):
        b = sys.stdin.buffer.read(1)
        if not b:
            return None
        headers += b
    length = 0
    for line in headers.decode().split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1])
    body = sys.stdin.buffer.read(length)
    return json.loads(body)


def server_request(method: str, params) -> dict:
    """Send a server->client request and block until the client responds."""
    global _next_id
    with _pending_cond:
        rid = _next_id
        _next_id += 1
    write_msg({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    with _pending_cond:
        while rid not in _pending_server:
            _pending_cond.wait(timeout=5)
        return _pending_server.pop(rid)


def handle(msg: dict):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params")

    if method == "ping":
        write_msg({"jsonrpc": "2.0", "id": mid, "result": {"pong": params}})
    elif method == "slow":
        time.sleep(0.15)
        write_msg({"jsonrpc": "2.0", "id": mid, "result": {"pong": "slow"}})
    elif method == "split_pong":
        write_msg({"jsonrpc": "2.0", "id": mid, "result": {"pong": "split"}}, chunks=3)
    elif method == "echo_via_config":
        resp = server_request("workspace/configuration", {"items": [{"section": "csharp"}]})
        write_msg({"jsonrpc": "2.0", "id": mid, "result": {"config": resp.get("result")}})
    elif method == "do_caps_progress":
        reg = server_request("client/registerCapability", {"registrations": []})
        prog = server_request("window/workDoneProgress/create", {"token": "load-1"})
        write_msg({"jsonrpc": "2.0", "method": "$/progress",
                   "params": {"token": "load-1", "value": {"kind": "begin", "title": "loading"}}})
        write_msg({"jsonrpc": "2.0", "method": "$/progress",
                   "params": {"token": "load-1", "value": {"kind": "end"}}})
        write_msg({"jsonrpc": "2.0", "id": mid,
                   "result": {"acks": [reg.get("result"), prog.get("result")]}})
    elif method == "weird_probe":
        resp = server_request("bogus/serverRequest", {"x": 1})
        write_msg({"jsonrpc": "2.0", "id": mid,
                   "result": {"reply": {"result": resp.get("result"), "error": resp.get("error")}}})
    elif method == "emit_custom_note":
        write_msg({"jsonrpc": "2.0", "method": "workspace/projectInitializationComplete",
                   "params": None})
        write_msg({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}})
    elif method == "emit_status_sequence":
        notifications = (
            params.get("notifications", [])
            if isinstance(params, dict)
            else (params or [])
        )
        for notification in notifications:
            delay_ms = notification.get("delay_ms", 0)
            if delay_ms:
                time.sleep(float(delay_ms) / 1000)
            write_msg({
                "jsonrpc": "2.0",
                "method": notification["method"],
                "params": notification.get("params"),
            })
        write_msg({"jsonrpc": "2.0", "id": mid,
                   "result": {"emitted": len(notifications)}})
    elif method == "ask_folders":
        resp = server_request("workspace/workspaceFolders", None)
        write_msg({"jsonrpc": "2.0", "id": mid, "result": {"folders": resp.get("result")}})
    elif method == "ask_unregister":
        resp = server_request("client/unregisterCapability", {"unregisterations": []})
        write_msg({"jsonrpc": "2.0", "id": mid,
                   "result": {"reply": {"result": resp.get("result"), "error": resp.get("error")}}})
    elif method == "boom":
        write_msg({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32099, "message": "boom failed"}})
    elif method == "sleepy":
        pass  # never answer
    elif method == "die":
        os._exit(1)
    elif method == "shutdown":
        write_msg({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        os._exit(0)
    # notifications and anything else: ignore


def main():
    while True:
        msg = read_msg()
        if msg is None:
            return
        if "method" not in msg and "id" in msg:
            # response from client to one of our server->client requests
            with _pending_cond:
                _pending_server[msg["id"]] = msg
                _pending_cond.notify_all()
            continue
        threading.Thread(target=handle, args=(msg,), daemon=True).start()


if __name__ == "__main__":
    main()
