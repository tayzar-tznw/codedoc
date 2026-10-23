"""Unit tests for graph_generator.lsp_client against a scripted fake stdio server."""

import asyncio
import os
import sys

import pytest

from graph_generator.lsp_client import (
    LspClient,
    LspConnectionError,
    LspError,
    LspTimeout,
)

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_lsp_server.py")]
STATUS_METHOD = "language/status"


def run(coro):
    return asyncio.run(coro)


async def _started(config_sections=None):
    client = LspClient(FAKE, cwd=os.path.dirname(__file__), config_sections=config_sections)
    await client.start()
    return client


def test_request_response_correlation_out_of_order():
    async def body():
        client = await _started()
        try:
            slow_task = asyncio.ensure_future(client.request("slow", None))
            fast = await client.request("ping", {"n": 1})
            slow = await slow_task
            assert fast == {"pong": {"n": 1}}
            assert slow == {"pong": "slow"}
        finally:
            await client.stop()
    run(body())


def test_framing_survives_split_frames():
    async def body():
        client = await _started()
        try:
            result = await client.request("split_pong", None)
            assert result == {"pong": "split"}
        finally:
            await client.stop()
    run(body())


def test_answers_workspace_configuration_with_sections():
    async def body():
        client = await _started(config_sections={"csharp": {"useMetadataUris": True}})
        try:
            result = await client.request("echo_via_config", None)
            assert result == {"config": [{"useMetadataUris": True}]}
        finally:
            await client.stop()
    run(body())


def test_answers_register_capability_and_progress_create_and_signals_end():
    async def body():
        client = await _started()
        try:
            assert client.progress_ends == 0
            result = await client.request("do_caps_progress", None)
            # both server->client requests must be answered with null results
            assert result == {"acks": [None, None]}
            await client.wait_progress_end(timeout=2.0)
            assert client.progress_ends == 1
        finally:
            await client.stop()
    run(body())


def test_unknown_server_request_gets_method_not_found():
    async def body():
        client = await _started()
        try:
            result = await client.request("weird_probe", None)
            assert result["reply"]["result"] is None
            assert result["reply"]["error"]["code"] == -32601
        finally:
            await client.stop()
    run(body())


def test_error_response_raises_lsp_error():
    async def body():
        client = await _started()
        try:
            with pytest.raises(LspError) as exc:
                await client.request("boom", None)
            assert exc.value.code == -32099
            assert "boom failed" in str(exc.value)
        finally:
            await client.stop()
    run(body())


def test_request_timeout_raises_and_cleans_pending():
    async def body():
        client = await _started()
        try:
            with pytest.raises(LspTimeout):
                await client.request("sleepy", None, timeout=0.2)
            assert client.pending_count == 0
            # client still usable afterwards
            assert await client.request("ping", 1) == {"pong": 1}
        finally:
            await client.stop()
    run(body())


def test_server_crash_fails_pending_requests():
    async def body():
        client = await _started()
        try:
            with pytest.raises(LspConnectionError):
                await client.request("die", None, timeout=5.0)
            assert not client.is_alive
        finally:
            await client.stop()
    run(body())


def test_stop_terminates_process_and_is_idempotent():
    async def body():
        client = await _started()
        await client.request("ping", None)
        await client.stop()
        assert not client.is_alive
        await client.stop()  # second stop must not raise
    run(body())


def test_notify_does_not_expect_response():
    async def body():
        client = await _started()
        try:
            await client.notify("textDocument/didOpen", {"textDocument": {"uri": "file:///x"}})
            # server ignores notifications; a follow-up request still works
            assert await client.request("ping", "after-notify") == {"pong": "after-notify"}
        finally:
            await client.stop()
    run(body())


def test_wait_progress_end_times_out_when_no_progress():
    async def body():
        client = await _started()
        try:
            with pytest.raises(LspTimeout):
                await client.wait_progress_end(timeout=0.2)
        finally:
            await client.stop()
    run(body())


def test_request_and_notify_on_stopped_client_raise_connection_error():
    async def body():
        client = await _started()
        await client.stop()
        with pytest.raises(LspConnectionError):
            await client.request("ping", None)
        with pytest.raises(LspConnectionError):
            await client.notify("x", None)
    run(body())


def test_watched_notification_sets_event_and_counts():
    async def body():
        client = LspClient(FAKE, cwd=os.path.dirname(__file__),
                           watch_notifications={"workspace/projectInitializationComplete"})
        await client.start()
        try:
            assert client.notification_counts.get("workspace/projectInitializationComplete", 0) == 0
            result = await client.request("emit_custom_note", None)
            assert result == {"ok": True}
            await client.wait_notification("workspace/projectInitializationComplete", timeout=2.0)
            assert client.notification_counts["workspace/projectInitializationComplete"] == 1
        finally:
            await client.stop()
    run(body())


def test_predicate_wait_skips_non_matching_status_notifications():
    async def body():
        client = LspClient(FAKE, cwd=os.path.dirname(__file__),
                           watch_notifications={STATUS_METHOD})
        await client.start()
        try:
            sequence = [
                {"method": STATUS_METHOD, "params": {"type": "Starting"}, "delay_ms": 10},
                {"method": STATUS_METHOD, "params": {"type": "Started"}, "delay_ms": 10},
                {"method": STATUS_METHOD, "params": {"type": "ServiceReady"}, "delay_ms": 10},
            ]
            emit_task = asyncio.create_task(
                client.request("emit_status_sequence", sequence))
            await client.wait_notification(
                STATUS_METHOD,
                predicate=lambda params: params.get("type") == "ServiceReady",
                timeout=2.0,
            )
            assert [params["type"] for params in client.notification_params[STATUS_METHOD]] == [
                "Starting", "Started", "ServiceReady",
            ]
            assert client.notification_count(
                STATUS_METHOD,
                lambda params: params.get("type") == "ServiceReady",
            ) == 1
            assert await emit_task == {"emitted": 3}
        finally:
            await client.stop()
    run(body())


def test_predicate_wait_matches_notification_recorded_before_wait():
    async def body():
        client = LspClient(FAKE, cwd=os.path.dirname(__file__),
                           watch_notifications={STATUS_METHOD})
        await client.start()
        try:
            sequence = [
                {"method": STATUS_METHOD, "params": {"type": "Starting"}, "delay_ms": 0},
                {"method": STATUS_METHOD, "params": {"type": "ServiceReady"}, "delay_ms": 0},
            ]
            assert await client.request("emit_status_sequence", sequence) == {"emitted": 2}
            await client.wait_notification(
                STATUS_METHOD,
                predicate=lambda params: params.get("type") == "ServiceReady",
                timeout=0.01,
            )
        finally:
            await client.stop()
    run(body())


def test_wait_notification_without_predicate_keeps_first_event_behavior():
    async def body():
        client = LspClient(FAKE, cwd=os.path.dirname(__file__),
                           watch_notifications={STATUS_METHOD})
        await client.start()
        try:
            sequence = [
                {"method": STATUS_METHOD, "params": {"type": "Starting"}, "delay_ms": 10},
                {"method": STATUS_METHOD, "params": {"type": "Started"}, "delay_ms": 250},
                {"method": STATUS_METHOD, "params": {"type": "ServiceReady"}, "delay_ms": 0},
            ]
            emit_task = asyncio.create_task(
                client.request("emit_status_sequence", sequence))
            await client.wait_notification(STATUS_METHOD, timeout=2.0)
            assert [params["type"] for params in client.notification_params[STATUS_METHOD]] == [
                "Starting",
            ]
            assert await emit_task == {"emitted": 3}
        finally:
            await client.stop()
    run(body())


def test_watched_notification_params_history_is_bounded():
    async def body():
        client = LspClient(FAKE, cwd=os.path.dirname(__file__),
                           watch_notifications={STATUS_METHOD})
        await client.start()
        try:
            sequence = [
                {
                    "method": STATUS_METHOD,
                    "params": {"type": f"State{i}"},
                    "delay_ms": 0,
                }
                for i in range(55)
            ]
            assert await client.request("emit_status_sequence", sequence) == {"emitted": 55}
            history = client.notification_params[STATUS_METHOD]
            assert len(history) == 50
            assert history[0] == {"type": "State5"}
            assert history[-1] == {"type": "State54"}
            assert client.notification_count(STATUS_METHOD) == 55
            assert client.notification_count(
                STATUS_METHOD, lambda params: params["type"] == "State0") == 0
            assert client.notification_count(
                STATUS_METHOD, lambda params: params["type"] == "State54") == 1
        finally:
            await client.stop()
    run(body())


def test_wait_notification_times_out_for_unseen_method():
    async def body():
        client = LspClient(FAKE, cwd=os.path.dirname(__file__),
                           watch_notifications={"never/happens"})
        await client.start()
        try:
            with pytest.raises(LspTimeout):
                await client.wait_notification("never/happens", timeout=0.2)
        finally:
            await client.stop()
    run(body())


def test_answers_workspace_folders_request():
    async def body():
        client = LspClient(FAKE, cwd=os.path.dirname(__file__),
                           workspace_folders=["/a/proj"])
        await client.start()
        try:
            result = await client.request("ask_folders", None)
            assert result == {"folders": [{"uri": "file:///a/proj", "name": "proj"}]}
        finally:
            await client.stop()
    run(body())


def test_answers_unregister_capability_with_null():
    async def body():
        client = await _started()
        try:
            result = await client.request("ask_unregister", None)
            assert result["reply"]["result"] is None
            assert result["reply"]["error"] is None
        finally:
            await client.stop()
    run(body())
