"""Focused contract tests for the official Codex SDK worker."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ItemCompletedNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    ThreadItem,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
)
from openai_codex.models import Notification

from hamroh.cc_worker.cc_schema import schema_json
from hamroh.cc_worker.events import TurnResult
from hamroh.codex_worker import (
    CodexSpawnSpec,
    CodexWorker,
    WorkerHooks,
    compose_developer_instructions,
)
from hamroh.codex_worker.worker import _install_fail_closed_approval_handler
from hamroh.config import Config


def _spec(tmp_path: Path, *, session_id: str | None = None) -> CodexSpawnSpec:
    system = tmp_path / "system.md"
    system.write_text("Use Telegram tools.", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(schema_json(), encoding="utf-8")
    return CodexSpawnSpec(
        system_prompt_path=system,
        json_schema_path=schema,
        cwd=tmp_path,
        session_id=session_id,
        codex_home=tmp_path / "codex-home",
        codex_config={
            "mcp_servers": {
                "hamroh": {"url": "http://127.0.0.1:8765/mcp", "required": True}
            }
        },
        hamroh_tool_names=("telegram_send_message",),
    )


def test_configured_agent_name_is_authoritative(tmp_path: Path) -> None:
    spec = dataclasses.replace(_spec(tmp_path), agent_name="Dono Status")

    prompt = compose_developer_instructions(spec)

    assert "# Configured identity" in prompt
    assert 'Your name is "Dono Status"' in prompt
    assert "AGENT_NAME deployment setting" in prompt


class _FakeHandle:
    id = "turn-1"

    def __init__(self, events: list[Notification]) -> None:
        self.events = events
        self.steered: list[str] = []
        self.release = asyncio.Event()
        self.release.set()
        self.stream_started = asyncio.Event()
        self.interrupted = False

    async def steer(self, text: str) -> object:
        self.steered.append(text)
        return object()

    async def interrupt(self) -> object:
        self.interrupted = True
        self.release.set()
        return object()

    async def stream(self):
        self.stream_started.set()
        await self.release.wait()
        for event in self.events:
            yield event


class _FakeThread:
    def __init__(self, thread_id: str, handle: _FakeHandle) -> None:
        self.id = thread_id
        self.handle = handle
        self.turn_calls: list[tuple[str, dict[str, Any]]] = []

    async def turn(self, text: str, **kwargs: Any) -> _FakeHandle:
        self.turn_calls.append((text, kwargs))
        return self.handle


class _FakeCodex:
    instances: list["_FakeCodex"] = []
    thread: _FakeThread
    resume_error: Exception | None = None

    def __init__(self, config: object) -> None:
        self.config = config
        self.started: list[dict[str, Any]] = []
        self.resumed: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        type(self).instances.append(self)

    async def __aenter__(self) -> "_FakeCodex":
        return self

    async def close(self) -> None:
        self.closed = True

    async def thread_start(self, **kwargs: Any) -> _FakeThread:
        self.started.append(kwargs)
        return type(self).thread

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> _FakeThread:
        self.resumed.append((thread_id, kwargs))
        if type(self).resume_error is not None:
            raise type(self).resume_error
        return type(self).thread


def _events() -> list[Notification]:
    mcp = ThreadItem(
        root=McpToolCallThreadItem(
            arguments={"chat_id": 1, "text": "hello"},
            id="tool-1",
            server="hamroh",
            status=McpToolCallStatus.completed,
            tool="telegram_send_message",
            type="mcpToolCall",
        )
    )
    control = ThreadItem(
        root=AgentMessageThreadItem(
            id="message-1",
            phase=MessagePhase.final_answer,
            text=json.dumps(
                {
                    "action": "stop",
                    "reason": "Reply delivered",
                    "sleep_ms": None,
                }
            ),
            type="agentMessage",
        )
    )
    return [
        Notification(
            method="item/completed",
            payload=ItemCompletedNotification(
                completedAtMs=1,
                item=mcp,
                threadId="thread-new",
                turnId="turn-1",
            ),
        ),
        Notification(
            method="item/completed",
            payload=ItemCompletedNotification(
                completedAtMs=2,
                item=control,
                threadId="thread-new",
                turnId="turn-1",
            ),
        ),
        Notification(
            method="turn/completed",
            payload=TurnCompletedNotification(
                threadId="thread-new",
                turn=Turn(
                    id="turn-1",
                    items=[],
                    status=TurnStatus.completed,
                ),
            ),
        ),
    ]


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    import hamroh.codex_worker.worker as worker_module

    _FakeCodex.instances.clear()
    _FakeCodex.resume_error = None
    _FakeCodex.thread = _FakeThread("thread-new", _FakeHandle(_events()))
    monkeypatch.setattr(worker_module, "AsyncCodex", _FakeCodex)


async def test_start_persists_thread_immediately_with_private_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-reach-codex")
    cfg = Config.for_test(tmp_path)
    object.__setattr__(cfg, "agent_name", "Dono Status")
    spec = dataclasses.replace(_spec(tmp_path), agent_name=cfg.agent_name)
    worker = CodexWorker(spec, cfg)

    await worker.start()

    assert worker.session_id == "thread-new"
    assert cfg.session_id_path.read_text(encoding="utf-8").strip() == "thread-new"
    assert stat.S_IMODE(cfg.session_id_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "codex-home").stat().st_mode) == 0o700
    call = _FakeCodex.instances[0].started[0]
    assert call["approval_mode"].value == "deny_all"
    assert call["sandbox"].value == "read-only"
    assert call["config"]["mcp_servers"]["hamroh"]["required"] is True
    launch_argv = _FakeCodex.instances[0].config.launch_args_override
    assert _FakeCodex.instances[0].config.client_name == "dono-status"
    assert _FakeCodex.instances[0].config.client_title == "Dono Status Telegram Agent"
    assert launch_argv[:2] == ("/usr/bin/env", "-i")
    assert not any("must-not-reach-codex" in part for part in launch_argv)
    await worker.stop()


async def test_stream_maps_mcp_action_and_structured_control(tmp_path: Path) -> None:
    worker = CodexWorker(_spec(tmp_path), Config.for_test(tmp_path))
    await worker.start()

    await worker.send("incoming Telegram envelope")
    result = await asyncio.wait_for(worker.wait_for_result(), timeout=1)

    assert result.user_visible_action is True
    assert result.dropped_text is False
    assert result.control is not None
    assert result.control.action == "stop"
    assert result.text_blocks == []
    turn_call = _FakeCodex.thread.turn_calls[0]
    assert turn_call[0] == "incoming Telegram envelope"
    assert turn_call[1]["output_schema"]["type"] == "object"
    # Engine can start a heartbeat/sleep continuation as soon as it receives
    # the result; no one-loop scheduling race may leave the old turn "active".
    await worker.send("immediate continuation")
    await worker.stop()


async def test_inject_uses_native_turn_steer(tmp_path: Path) -> None:
    handle = _FakeCodex.thread.handle
    handle.release.clear()
    worker = CodexWorker(_spec(tmp_path), Config.for_test(tmp_path))
    await worker.start()

    await worker.send("first")
    # Entering the SDK stream proves the native steer handle is active.
    await asyncio.wait_for(handle.stream_started.wait(), timeout=1)
    accepted = await worker.inject("second")
    handle.release.set()
    await asyncio.wait_for(worker.wait_for_result(), timeout=1)

    assert handle.steered == ["second"]
    assert accepted is True
    await worker.stop()


async def test_inject_before_turn_handle_returns_false_for_engine_requeue(
    tmp_path: Path,
) -> None:
    worker = CodexWorker(_spec(tmp_path), Config.for_test(tmp_path))
    await worker.start()

    assert await worker.inject("not yet steerable") is False
    await worker.stop()


async def test_stale_thread_falls_back_and_calls_hook(tmp_path: Path) -> None:
    _FakeCodex.resume_error = RuntimeError("thread not found")
    stale: list[str] = []

    async def on_stale(thread_id: str) -> None:
        stale.append(thread_id)

    worker = CodexWorker(
        _spec(tmp_path, session_id="thread-old"),
        Config.for_test(tmp_path),
        WorkerHooks(on_stale_session=on_stale),
    )
    await worker.start()

    assert stale == ["thread-old"]
    assert _FakeCodex.instances[0].resumed[0][0] == "thread-old"
    assert worker.session_id == "thread-new"
    await worker.stop()


async def test_start_leaves_sdk_notification_router_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The former beta-SDK patch replaced this method and accessed router
    # fields that no longer exist in 0.163.0, killing the reader thread.
    def route_notification(_event: object) -> None:
        pass

    router = SimpleNamespace(route_notification=route_notification)
    client = SimpleNamespace(_router=router)
    monkeypatch.setattr(
        _FakeCodex, "_client", SimpleNamespace(_sync=client), raising=False
    )
    worker = CodexWorker(_spec(tmp_path), Config.for_test(tmp_path))
    await worker.start()

    assert router.route_notification is route_notification
    await worker.stop()


@pytest.mark.parametrize("broken_stream", [True, False])
async def test_sdk_stream_failure_reconnects_without_resetting_saved_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken_stream: bool
) -> None:
    async def stream():
        yield _events()[0]
        if broken_stream:
            raise AttributeError("SDK notification router failed")

    monkeypatch.setattr(_FakeCodex.thread.handle, "stream", stream)
    cfg = dataclasses.replace(Config.for_test(tmp_path), crash_backoff_base=0)
    worker = CodexWorker(_spec(tmp_path), cfg)
    await worker.start()
    original_client = _FakeCodex.instances[0]
    saved_id = cfg.session_id_path.read_text(encoding="utf-8")
    await worker.send("incoming Telegram envelope")
    message = "SDK notification router failed" if broken_stream else "completion event"
    with pytest.raises(RuntimeError, match=message):
        await asyncio.wait_for(worker.wait_for_result(), timeout=1)

    assert not worker.is_running
    assert worker._runtime_failed.is_set()
    assert cfg.session_id_path.read_text(encoding="utf-8") == saved_id

    # The supervisor replaces the dead SDK process and resumes the same
    # conversation instead of issuing thread/start on the broken client.
    await worker.supervise()

    async def wait_for_reconnect() -> None:
        while not worker.is_running:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_reconnect(), timeout=1)
    assert original_client.closed
    assert len(original_client.started) == 1
    assert _FakeCodex.instances[-1].resumed[0][0] == "thread-new"
    assert cfg.session_id_path.read_text(encoding="utf-8") == saved_id
    await worker.stop()


async def test_typed_api_failure_keeps_runtime_available_for_session_reset(
    tmp_path: Path,
) -> None:
    completed = _events()[-1]
    completed.payload.turn.status = TurnStatus.failed
    _FakeCodex.thread.handle.events = [completed]
    worker = CodexWorker(_spec(tmp_path), Config.for_test(tmp_path))
    await worker.start()
    await worker.send("request rejected by the model")

    result = await asyncio.wait_for(worker.wait_for_result(), timeout=1)

    assert result.api_error == "Codex turn failed"
    assert worker.is_running
    assert not worker._runtime_failed.is_set()
    await worker.reset_session()
    assert len(_FakeCodex.instances[0].started) == 2
    await worker.stop()


async def test_session_reset_timeout_marks_runtime_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hamroh.codex_worker.worker as worker_module

    worker = CodexWorker(_spec(tmp_path), Config.for_test(tmp_path))
    await worker.start()
    auth = tmp_path / "codex-home/auth.json"
    auth.write_text("existing auth cache", encoding="utf-8")

    async def wedged_start(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(worker._client, "thread_start", wedged_start)
    monkeypatch.setattr(worker_module, "_RPC_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(RuntimeError, match="runtime failed during session reset"):
        await worker.reset_session()

    assert not worker.is_running
    assert worker._runtime_failed.is_set()
    assert auth.read_text() == "existing auth cache"
    await worker.stop()


async def test_explicit_gpt_5_6_model_survives_turn_and_session_reset(
    tmp_path: Path,
) -> None:
    cfg = Config.for_test(tmp_path)
    spec = dataclasses.replace(_spec(tmp_path), model="gpt-5.6-sol")
    worker = CodexWorker(spec, cfg)
    await worker.start()
    await worker.send("incoming Telegram envelope")
    await asyncio.wait_for(worker.wait_for_result(), timeout=1)
    assert _FakeCodex.thread.turn_calls[0][1]["model"] == "gpt-5.6-sol"

    _FakeCodex.thread = _FakeThread("thread-reset", _FakeHandle(_events()))
    await worker.reset_session()

    assert all(
        call["model"] == "gpt-5.6-sol" for call in _FakeCodex.instances[0].started
    )
    assert worker.session_id == "thread-reset"
    assert cfg.session_id_path.read_text().strip() == "thread-reset"
    await worker.stop()


def test_unexpected_sdk_approvals_fail_closed() -> None:
    client = SimpleNamespace(_approval_handler=None)
    runtime = SimpleNamespace(_client=SimpleNamespace(_sync=client))
    _install_fail_closed_approval_handler(runtime)  # type: ignore[arg-type]

    assert client._approval_handler(
        "item/commandExecution/requestApproval", None
    ) == {"decision": "decline"}
    assert client._approval_handler("applyPatchApproval", None) == {
        "decision": "denied"
    }
    assert client._approval_handler("unknown/request", None) == {}


def test_commentary_is_logged_but_never_delivered_as_dropped_text(
    tmp_path: Path,
) -> None:
    worker = CodexWorker(_spec(tmp_path), Config.for_test(tmp_path))
    worker._current_result = TurnResult()
    worker._on_agent_message(
        AgentMessageThreadItem(
            id="commentary-1",
            phase=MessagePhase.commentary,
            text="internal progress narration",
            type="agentMessage",
        )
    )

    assert worker._current_result.text_blocks == []
