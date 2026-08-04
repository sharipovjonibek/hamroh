"""Official-SDK-backed Codex worker with the engine's existing async contract."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
from openai_codex.api import AsyncThread, AsyncTurnHandle
from openai_codex.errors import TransportClosedError
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ErrorNotification,
    ItemCompletedNotification,
    ItemStartedNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    ThreadItem,
    TurnCompletedNotification,
    TurnStatus,
)
from openai_codex.models import JsonObject, Notification
from openai_codex.types import ReasoningEffort
from codex_cli_bin import bundled_codex_path, bundled_path_dir  # type: ignore[import-untyped]

from ..cc_worker.events import CrashLoop, TurnResult
from ..cc_worker.worker import WorkerHooks
from ..config import Config
from ..helpers.transcript import (
    log_cc_result,
    log_cc_text,
    log_cc_tool_result,
    log_cc_tool_use,
    log_cc_user,
)
from ..models import ControlAction
from ..tools.base import Heartbeat
from .spec import CodexSpawnSpec, compose_developer_instructions, load_output_schema


log = logging.getLogger("hamroh.codex_worker")

# Bare MCP tool names as Codex reports them on ``McpToolCallThreadItem.tool``.
# The server name is checked separately, preventing an external MCP from
# spoofing a user-visible Hamroh action just by reusing one of these names.
USER_VISIBLE_HAMROH_TOOLS: frozenset[str] = frozenset(
    {
        "telegram_send_message",
        "telegram_reply_to_message",
        "telegram_send_photo",
        "telegram_send_memory_document",
        "telegram_create_poll",
        "telegram_add_reaction",
        "telegram_edit_message",
        "telegram_delete_message",
        "telegram_stop_poll",
    }
)

_STALE_THREAD_MARKERS: tuple[str, ...] = (
    "thread not found",
    "no thread found",
    "rollout not found",
    "no rollout found",
    "unknown thread",
)
_RPC_TIMEOUT_SECONDS = 60.0
_CONTROL_RPC_TIMEOUT_SECONDS = 10.0


def _sandbox(value: str) -> Sandbox:
    if value == "read-only":
        return Sandbox.read_only
    if value == "workspace-write":
        return Sandbox.workspace_write
    raise ValueError(
        "Codex sandbox must be 'read-only' or 'workspace-write'; "
        "full host access is intentionally unsupported"
    )


def _effort(value: str) -> ReasoningEffort:
    try:
        return ReasoningEffort(value.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ReasoningEffort)
        raise ValueError(
            f"unsupported Codex reasoning effort {value!r}: {allowed}"
        ) from exc


def _unwrap_item(item: ThreadItem | Any) -> Any:
    return item.root if hasattr(item, "root") else item


def _is_stale_thread_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _STALE_THREAD_MARKERS)


def _patch_sdk_early_completion_race(runtime: AsyncCodex) -> None:
    """Retain terminal notifications emitted before turn registration.

    ``openai-codex==0.1.0b3`` buffers early item notifications but discards
    that buffer when an early ``turn/completed`` arrives. A fast response can
    therefore leave ``AsyncTurnHandle.stream()`` waiting forever. The project
    intentionally pins that SDK build, so patch its per-client router until an
    upstream release containing the fix is adopted. Unknown/fake clients are
    left untouched.
    """

    try:
        router = runtime._client._sync._router  # type: ignore[attr-defined]
    except AttributeError:
        return
    if getattr(router, "_hamroh_early_completion_fix", False):
        return

    original = router.route_notification

    def route_notification(notification: Notification) -> None:
        if notification.method == "turn/completed":
            turn_id = router._notification_turn_id(notification)
            if turn_id is not None:
                with router._lock:
                    turn_queue = router._turn_notifications.get(turn_id)
                    if turn_queue is None:
                        router._pending_turn_notifications.setdefault(
                            turn_id, deque()
                        ).append(notification)
                        return
                turn_queue.put(notification)
                return
        original(notification)

    setattr(router, "route_notification", route_notification)
    setattr(router, "_hamroh_early_completion_fix", True)


def _install_fail_closed_approval_handler(runtime: AsyncCodex) -> None:
    """Defensively reject any approval request that reaches the SDK client."""

    try:
        client = runtime._client._sync  # type: ignore[attr-defined]
    except AttributeError:
        return

    def reject(method: str, _params: JsonObject | None) -> JsonObject:
        log.error("Codex unexpectedly requested approval via %s; rejecting", method)
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"decision": "decline"}
        if method in {"execCommandApproval", "applyPatchApproval"}:
            return {"decision": "denied"}
        # Unknown approval/elicitation shapes fail schema validation rather
        # than receiving an accidentally permissive response.
        return {}

    client._approval_handler = reject


class CodexWorker:
    """Run one persistent Codex thread and expose the legacy worker surface.

    One SDK app-server process lives for the worker lifetime. Each ``send``
    starts a typed streaming turn; ``inject`` steers the active turn. Results
    use the shared :class:`hamroh.cc_worker.TurnResult`, so the engine does not
    need provider-specific control flow.
    """

    def __init__(
        self,
        spec: CodexSpawnSpec,
        config: Config,
        hooks: WorkerHooks = WorkerHooks(),
    ) -> None:
        self.spec = spec
        self.heartbeat = hooks.heartbeat or Heartbeat()

        self._on_crash = hooks.on_crash
        self._on_giveup = hooks.on_giveup
        self._on_stale_session = hooks.on_stale_session
        self._session_id_path = config.session_id_path
        self._session_id = spec.session_id
        self._codex_home = spec.codex_home or getattr(config, "codex_home", None)

        self._liveness_timeout = config.liveness_timeout_seconds
        self._liveness_poll = config.liveness_poll_seconds
        self._crash_backoff_base = config.crash_backoff_base
        self._crash_backoff_cap = config.crash_backoff_cap
        self._crash_limit = config.crash_limit
        self._crash_window_seconds = config.crash_window_seconds

        self._client: AsyncCodex | None = None
        self._thread: AsyncThread | None = None
        self._active_turn: AsyncTurnHandle | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._liveness_task: asyncio.Task[None] | None = None
        self._result_queue: asyncio.Queue[TurnResult | Exception] = asyncio.Queue()
        self._current_result: TurnResult | None = None
        self._seen_item_ids: set[str] = set()
        self._started_item_ids: set[str] = set()
        self._runtime_error: BaseException | None = None
        self._runtime_failed = asyncio.Event()
        self._stop_requested = asyncio.Event()
        self._connect_lock = asyncio.Lock()
        self._crash_times: list[float] = []
        self._generation = 0
        self._last_event_at = time.monotonic()
        self._running = False
        self._developer_instructions = ""
        self._output_schema: dict[str, Any] = {}

    @property
    def session_id(self) -> str | None:
        """The persistent Codex thread ID (legacy name kept for Engine)."""

        return self._session_id

    @property
    def is_running(self) -> bool:
        return self._running and self._client is not None and self._thread is not None

    async def start(self) -> None:
        """Start the SDK runtime and create or resume the persistent thread."""

        async with self._connect_lock:
            if self.is_running:
                return
            self._stop_requested.clear()
            self._developer_instructions = compose_developer_instructions(self.spec)
            self._output_schema = load_output_schema(self.spec)
            await self._connect()

    async def _connect(self) -> None:
        if self._codex_home is not None:
            self._prepare_private_codex_home(self._codex_home)

        launch_args = self._clean_runtime_argv()
        runtime = AsyncCodex(
            CodexConfig(
                # The SDK normally merges ``os.environ`` into the app-server
                # process. Hamroh's parent environment contains Telegram and
                # plugin credentials, none of which the model runtime needs.
                # Launch through ``env -i`` so only the explicit, non-secret
                # runtime variables below survive the exec boundary.
                launch_args_override=launch_args,
                cwd=str(self.spec.cwd),
                client_name="shahnoza",
                client_title="Shahnoza Telegram Agent",
            )
        )
        _patch_sdk_early_completion_race(runtime)
        _install_fail_closed_approval_handler(runtime)
        try:
            await asyncio.wait_for(
                runtime.__aenter__(), timeout=_RPC_TIMEOUT_SECONDS
            )
            thread = await self._open_thread(runtime)
        except Exception:
            with suppress(Exception):
                await runtime.close()
            raise

        self._client = runtime
        self._thread = thread
        self._running = True
        self._runtime_error = None
        self._runtime_failed.clear()
        log.info(
            "Codex runtime ready (thread=%s model=%s effort=%s sandbox=%s)",
            thread.id,
            self.spec.model or "default",
            self.spec.effort,
            self.spec.sandbox,
        )

    def _clean_runtime_argv(self) -> tuple[str, ...]:
        """Build the app-server argv with a minimal, non-secret environment."""

        codex_bin = (
            Path(self.spec.codex_bin).expanduser()
            if self.spec.codex_bin
            else bundled_codex_path()
        )
        if not codex_bin.is_file():
            raise FileNotFoundError(f"Codex binary not found: {codex_bin}")

        path_entries: list[str] = []
        sdk_bin_dir = bundled_path_dir()
        if sdk_bin_dir is not None:
            path_entries.append(str(sdk_bin_dir))
        parent_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        path_entries.extend(part for part in parent_path.split(os.pathsep) if part)

        clean_env = {
            "PATH": os.pathsep.join(dict.fromkeys(path_entries)),
            "HOME": str(self._codex_home or self.spec.cwd),
            "CODEX_HOME": str(self._codex_home or self.spec.cwd / ".codex"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PYTHONUNBUFFERED": "1",
        }
        assignments = tuple(f"{key}={value}" for key, value in clean_env.items())
        return (
            "/usr/bin/env",
            "-i",
            *assignments,
            str(codex_bin),
            "app-server",
            "--listen",
            "stdio://",
        )

    def _thread_options(self) -> dict[str, Any]:
        return {
            "approval_mode": ApprovalMode.deny_all,
            "config": copy.deepcopy(dict(self.spec.codex_config)) or None,
            "cwd": str(self.spec.cwd),
            "developer_instructions": self._developer_instructions,
            "model": self.spec.model or None,
            "sandbox": _sandbox(self.spec.sandbox),
        }

    async def _open_thread(self, runtime: AsyncCodex) -> AsyncThread:
        options = self._thread_options()
        if self._session_id:
            stale_id = self._session_id
            try:
                thread = await asyncio.wait_for(
                    runtime.thread_resume(stale_id, **options),
                    timeout=_RPC_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                if not _is_stale_thread_error(exc):
                    raise
                log.warning(
                    "Codex thread %s is stale; starting a fresh thread", stale_id
                )
                if self._on_stale_session is not None:
                    with suppress(Exception):
                        await self._on_stale_session(stale_id)
                self._session_id = None
                self._session_id_path.unlink(missing_ok=True)
                thread = await asyncio.wait_for(
                    runtime.thread_start(ephemeral=False, **options),
                    timeout=_RPC_TIMEOUT_SECONDS,
                )
        else:
            thread = await asyncio.wait_for(
                runtime.thread_start(ephemeral=False, **options),
                timeout=_RPC_TIMEOUT_SECONDS,
            )

        # Persist before start() returns, rather than waiting for shutdown. A
        # power loss after the first user turn can therefore still resume it.
        self._session_id = thread.id
        self._persist_session_id(thread.id)
        return thread

    @staticmethod
    def _prepare_private_codex_home(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    def _persist_session_id(self, session_id: str) -> None:
        path = self._session_id_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(session_id + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)

    async def supervise(self) -> None:
        """Start crash-recovery and liveness tasks, matching ``CcWorker``."""

        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = asyncio.create_task(
                self._supervise_loop(), name="codex-supervisor"
            )
        if self._liveness_task is None or self._liveness_task.done():
            self._liveness_task = asyncio.create_task(
                self._liveness_loop(), name="codex-liveness"
            )

    async def stop(self) -> None:
        self._stop_requested.set()
        self._runtime_failed.set()
        self._generation += 1

        active = self._active_turn
        if active is not None:
            with suppress(Exception):
                await asyncio.wait_for(
                    active.interrupt(), timeout=_CONTROL_RPC_TIMEOUT_SECONDS
                )

        for task in (self._turn_task, self._liveness_task, self._supervisor_task):
            if (
                task is not None
                and not task.done()
                and task is not asyncio.current_task()
            ):
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        self._turn_task = None
        self._liveness_task = None
        self._supervisor_task = None
        self._active_turn = None
        self._current_result = None
        await self._close_runtime()

    async def _close_runtime(self) -> None:
        runtime = self._client
        self._client = None
        self._thread = None
        self._running = False
        if runtime is not None:
            with suppress(Exception):
                await asyncio.wait_for(
                    runtime.close(), timeout=_CONTROL_RPC_TIMEOUT_SECONDS
                )

    async def reset_session(self) -> None:
        """Interrupt the current turn and replace its thread with a fresh one."""

        async with self._connect_lock:
            if self._client is None:
                raise RuntimeError("Codex worker not started")
            self._running = False
            self._generation += 1
            active = self._active_turn
            if active is not None:
                with suppress(Exception):
                    await asyncio.wait_for(
                        active.interrupt(), timeout=_CONTROL_RPC_TIMEOUT_SECONDS
                    )
            task = self._turn_task
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except asyncio.TimeoutError:
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task

            if self._current_result is not None:
                self._current_result.aborted_reason = "session-reset"
                self._result_queue.put_nowait(self._current_result)
            self._current_result = None
            self._active_turn = None
            self._turn_task = None
            self._session_id = None
            self._session_id_path.unlink(missing_ok=True)
            self._thread = None
            self._running = False
            try:
                thread = await asyncio.wait_for(
                    self._client.thread_start(
                        ephemeral=False, **self._thread_options()
                    ),
                    timeout=_RPC_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                self._runtime_error = exc
                self._runtime_failed.set()
                raise RuntimeError("Codex runtime failed during session reset") from exc
            self._thread = thread
            self._running = True
            self._session_id = thread.id
            self._persist_session_id(thread.id)
            log.warning("Codex session reset; new thread=%s", thread.id)

    async def send(self, text: str) -> None:
        """Start one streamed Codex turn and return without waiting for it."""

        async with self._connect_lock:
            if not self.is_running or self._thread is None:
                raise RuntimeError("Codex worker not started")
            if self._turn_task is not None and not self._turn_task.done():
                raise RuntimeError("Codex worker already has a turn in progress")

            self._generation += 1
            generation = self._generation
            self._current_result = TurnResult()
            self._seen_item_ids.clear()
            self._started_item_ids.clear()
            self._last_event_at = time.monotonic()
            log_cc_user(text)
            self._turn_task = asyncio.create_task(
                self._run_turn(text, generation), name="codex-turn"
            )

    async def inject(self, text: str) -> bool:
        """Steer an active turn; return false when the engine must requeue.

        Codex can finish between the engine observing ``is_processing`` and
        this call. Returning ``False`` keeps ownership with the engine, which
        starts a fresh turn and only then marks the Telegram row consumed.
        """

        active = self._active_turn
        if active is None:
            return False
        try:
            await asyncio.wait_for(
                active.steer(text), timeout=_CONTROL_RPC_TIMEOUT_SECONDS
            )
        except Exception:
            log.info("Codex steer raced turn completion; engine will requeue")
            return False
        return True

    async def wait_for_result(self) -> TurnResult:
        result = await self._result_queue.get()
        if isinstance(result, Exception):
            raise result
        return result

    async def _run_turn(self, text: str, generation: int) -> None:
        assert self._thread is not None
        handle: AsyncTurnHandle | None = None
        try:
            handle = await asyncio.wait_for(
                self._thread.turn(
                    text,
                    approval_mode=ApprovalMode.deny_all,
                    effort=_effort(self.spec.effort),
                    model=self.spec.model or None,
                    output_schema=copy.deepcopy(self._output_schema),
                    sandbox=_sandbox(self.spec.sandbox),
                ),
                timeout=_RPC_TIMEOUT_SECONDS,
            )
            if handle is None:
                raise RuntimeError("Codex returned no turn handle")
            if generation != self._generation:
                with suppress(Exception):
                    await handle.interrupt()
                return
            self._active_turn = handle

            completed = False
            async for event in handle.stream():
                if generation != self._generation:
                    return
                self._last_event_at = time.monotonic()
                completed = self._handle_notification(event, handle.id)
                if completed:
                    break
            if not completed and generation == self._generation:
                self._finish_api_error("Codex turn ended without a completion event")
        except asyncio.CancelledError:
            raise
        except TransportClosedError as exc:
            if generation == self._generation:
                self._finish_worker_error(exc)
                self._runtime_error = exc
                self._running = False
                self._runtime_failed.set()
        except Exception as exc:
            if generation == self._generation:
                log.exception("Codex turn failed")
                if handle is None:
                    # turn/start never returned a handle, so the model may not
                    # have accepted the message. Preserve reminder at-least-
                    # once semantics and reconnect the runtime.
                    self._finish_worker_error(exc)
                    self._runtime_error = exc
                    self._running = False
                    self._runtime_failed.set()
                else:
                    self._finish_api_error(str(exc) or type(exc).__name__)
        finally:
            if generation == self._generation:
                self._active_turn = None

    def _handle_notification(self, event: Notification, turn_id: str) -> bool:
        payload = event.payload
        if isinstance(payload, ItemStartedNotification) and payload.turn_id == turn_id:
            self._on_item_started(payload.item)
            return False
        if (
            isinstance(payload, ItemCompletedNotification)
            and payload.turn_id == turn_id
        ):
            self._on_item_completed(payload.item)
            return False
        if isinstance(payload, ErrorNotification) and payload.turn_id == turn_id:
            if not payload.will_retry and self._current_result is not None:
                self._current_result.api_error = payload.error.message
            level = log.warning if payload.will_retry else log.error
            level(
                "Codex turn error (will_retry=%s): %s",
                payload.will_retry,
                payload.error.message,
            )
            return False
        if (
            isinstance(payload, TurnCompletedNotification)
            and payload.turn.id == turn_id
        ):
            for item in payload.turn.items:
                self._on_item_completed(item)
            self._finish_turn(payload)
            return True
        return False

    def _on_item_started(self, wrapped: ThreadItem) -> None:
        item = _unwrap_item(wrapped)
        if not isinstance(item, McpToolCallThreadItem):
            return
        self._started_item_ids.add(item.id)
        tool_name = f"mcp__{item.server}__{item.tool}"
        args = (
            item.arguments
            if isinstance(item.arguments, dict)
            else {"input": item.arguments}
        )
        log_cc_tool_use(tool_name=tool_name, tool_use_id=item.id, args=args)

    def _on_item_completed(self, wrapped: ThreadItem) -> None:
        item = _unwrap_item(wrapped)
        item_id = getattr(item, "id", None)
        if isinstance(item_id, str):
            if item_id in self._seen_item_ids:
                return
            self._seen_item_ids.add(item_id)

        if isinstance(item, AgentMessageThreadItem):
            self._on_agent_message(item)
        elif isinstance(item, McpToolCallThreadItem):
            self._on_mcp_tool_completed(item)

    def _on_agent_message(self, item: AgentMessageThreadItem) -> None:
        if self._current_result is None or not item.text:
            return
        if item.phase == MessagePhase.commentary:
            # Commentary is runtime narration, not a user-facing answer. It
            # must never fall into Hamroh's dropped-text Telegram delivery.
            log_cc_text("(commentary) " + item.text)
            return
        control = self._parse_control(item.text)
        if control is not None:
            self._current_result.control = control
            return
        # Unexpected free-form final output is invisible to Telegram and
        # therefore feeds the existing dropped-text recovery.
        self._current_result.text_blocks.append(item.text)
        log_cc_text(item.text)

    @staticmethod
    def _parse_control(text: str) -> ControlAction | None:
        try:
            payload = json.loads(text)
            return ControlAction.model_validate(payload)
        except Exception:
            return None

    def _on_mcp_tool_completed(self, item: McpToolCallThreadItem) -> None:
        if self._current_result is None:
            return
        tool_name = f"mcp__{item.server}__{item.tool}"
        # Some runtime versions omit the item-started lifecycle notification.
        if item.id not in self._started_item_ids:
            args = (
                item.arguments
                if isinstance(item.arguments, dict)
                else {"input": item.arguments}
            )
            log_cc_tool_use(tool_name=tool_name, tool_use_id=item.id, args=args)
        failed = item.status == McpToolCallStatus.failed or item.error is not None
        if item.error is not None:
            rendered = item.error.message
        elif item.result is not None:
            rendered = json.dumps(item.result.model_dump(mode="json"), default=str)
        else:
            rendered = ""
        log_cc_tool_result(item.id, rendered, failed)
        if item.server == "hamroh" and item.tool in USER_VISIBLE_HAMROH_TOOLS:
            # A failed call did not reach the user and must not suppress the
            # dropped-text recovery path.
            if not failed:
                self._current_result.user_visible_action = True

    def _finish_turn(self, completed: TurnCompletedNotification) -> None:
        result = self._current_result
        if result is None:
            return
        turn = completed.turn
        if turn.status == TurnStatus.failed:
            result.api_error = (
                turn.error.message if turn.error is not None else "Codex turn failed"
            )
        elif turn.status == TurnStatus.interrupted and result.aborted_reason is None:
            result.aborted_reason = "interrupted"
        result.dropped_text = (
            bool(result.text_blocks) and not result.user_visible_action
        )
        control = result.control
        log_cc_result(
            action=control.action if control else None,
            reason=control.reason if control else None,
        )
        # Clear the in-flight marker before waking Engine. A heartbeat/sleep
        # continuation may call send() immediately after queue.get() returns.
        self._active_turn = None
        self._turn_task = None
        self._result_queue.put_nowait(result)
        self._current_result = None

    def _finish_api_error(self, message: str) -> None:
        result = self._current_result or TurnResult()
        result.api_error = message
        result.dropped_text = (
            bool(result.text_blocks) and not result.user_visible_action
        )
        self._active_turn = None
        self._turn_task = None
        self._result_queue.put_nowait(result)
        self._current_result = None

    def _finish_worker_error(self, exc: BaseException) -> None:
        """Unblock Engine via its crash path, never its API-reset path."""

        self._current_result = None
        message = str(exc) or "Codex runtime connection closed"
        self._result_queue.put_nowait(RuntimeError(message))

    async def _liveness_loop(self) -> None:
        while not self._stop_requested.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_requested.wait(), timeout=self._liveness_poll
                )
                return
            except asyncio.TimeoutError:
                pass
            if self._current_result is None:
                continue
            silence = time.monotonic() - max(
                self._last_event_at, self.heartbeat.last_activity
            )
            if silence <= self._liveness_timeout:
                continue
            if self._active_turn is None:
                log.error(
                    "Codex turn/start wedged: no handle after %.0fs; reconnecting",
                    silence,
                )
                self._generation += 1
                partial = self._current_result
                self._current_result = None
                turn_task = self._turn_task
                if turn_task is not None and not turn_task.done():
                    turn_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await turn_task
                partial.aborted_reason = "liveness-wedge"
                self._result_queue.put_nowait(partial)
                self._turn_task = None
                self._running = False
                self._runtime_error = RuntimeError("Codex turn/start timed out")
                self._runtime_failed.set()
                continue
            log.error("Codex turn wedged: no activity for %.0fs; interrupting", silence)
            self._generation += 1
            active = self._active_turn
            self._active_turn = None
            partial = self._current_result
            self._current_result = None
            interrupt_failed = False
            try:
                await asyncio.wait_for(active.interrupt(), timeout=5.0)
            except Exception:
                interrupt_failed = True
                log.warning("Codex interrupt timed out; runtime will be reconnected")
            turn_task = self._turn_task
            if turn_task is not None and not turn_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(turn_task), timeout=5.0)
                except asyncio.TimeoutError:
                    turn_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await turn_task
            partial.aborted_reason = "liveness-wedge"
            partial.dropped_text = (
                bool(partial.text_blocks) and not partial.user_visible_action
            )
            self._result_queue.put_nowait(partial)
            self._turn_task = None
            if interrupt_failed:
                self._running = False
                self._runtime_error = RuntimeError("Codex interrupt timed out")
                self._runtime_failed.set()

    async def _supervise_loop(self) -> None:
        while not self._stop_requested.is_set():
            await self._runtime_failed.wait()
            if self._stop_requested.is_set():
                return
            self._runtime_failed.clear()
            now = time.monotonic()
            self._crash_times = [
                stamp
                for stamp in self._crash_times
                if now - stamp < self._crash_window_seconds
            ]
            self._crash_times.append(now)
            count = len(self._crash_times)
            if count >= self._crash_limit:
                if self._on_giveup is not None:
                    with suppress(Exception):
                        await self._on_giveup(count)
                raise CrashLoop(
                    f"Codex runtime crashed {count} times in "
                    f"{self._crash_window_seconds:.0f}s"
                )
            backoff = min(
                self._crash_backoff_cap,
                self._crash_backoff_base * (2 ** (count - 1)),
            )
            if self._on_crash is not None:
                with suppress(Exception):
                    await self._on_crash(count, backoff)
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                pass
            async with self._connect_lock:
                await self._close_runtime()
                try:
                    await self._connect()
                except Exception as exc:
                    self._runtime_error = exc
                    self._runtime_failed.set()


__all__ = [
    "CodexWorker",
    "USER_VISIBLE_HAMROH_TOOLS",
    "_install_fail_closed_approval_handler",
    "_patch_sdk_early_completion_race",
]
