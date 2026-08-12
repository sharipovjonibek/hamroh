# The big picture

Hamroh is a Telegram agent framework. Telegram is the user interface, the
official OpenAI Codex Python SDK is the default model runtime, and Hamroh's
local MCP server is the action layer. The most useful way to learn the code is
to follow one Telegram message through those pieces.

```text
Telegram ──▶ dispatcher ──▶ engine ──▶ codex_worker ──▶ Codex SDK/app-server
                               │                              │
                               │                        calls MCP tools
                               ▼                              ▼
                            SQLite       tools/ ◀─ mcp_server (localhost)
                                             │
                                             └──▶ Telegram, memory,
                                                  reminders, browser, …
```

The framework does more than relay chat. It persists messages, enforces access
and rate limits, batches bursts, supervises the AI runtime, exposes tightly
scoped tools, and requires every model turn to return a structured control
action. `HAMROH_PROVIDER=claude` retains the former `cc_worker` as a legacy
compatibility path; Codex is the default and the architecture below follows
that path.

## Startup and authentication

- `hamroh/__main__.py` is the readable entrypoint. It starts the database,
  local MCP server, selected worker, engine, reminder scheduler, and Telegram
  dispatcher.
- `hamroh/startup.py` wires those objects and translates `plugins.json` into
  provider-specific runtime configuration.
- `hamroh/config.py` resolves environment variables once and gives the rest of
  the application a typed `Config` object.
- `hamroh/codex_login.py` performs the one-time ChatGPT browser OAuth login
  used by `make auth`, with a localhost callback on port `1455`.

In Docker, authentication and generated-image outputs belong to a named volume
mounted at `CODEX_HOME=/var/lib/codex`. The short-lived `codex-auth` service
receives the non-secret `AGENT_NAME` setting through Compose interpolation,
but does not load the agent's Telegram/plugin secrets; it only writes the
ChatGPT OAuth cache. The running agent mounts the same volume. This is
intentionally separate from the operator's general `~/.codex` directory.

The Python dependency pins `openai-codex`, whose SDK starts its matching Codex
app-server. `codex_worker` launches that process through `env -i` with only a
minimal `PATH`, locale, `HOME`, and `CODEX_HOME`. Telegram tokens and external
MCP credentials therefore are not inherited as ambient environment. A plugin's
explicit credentials are sent only in that MCP server's config.

## One message through the system

1. `hamroh/telegram_io/dispatcher.py` receives an update, applies
   `access.json` and rate-limit policy, stores the inbound message in SQLite,
   and calls `Engine.submit()`.
2. `hamroh/engine/engine.py` debounces nearby messages, renders the batch as
   XML, and calls the worker's common async contract: `send()`, `inject()`,
   `wait_for_result()`, and `reset_session()`.
3. `hamroh/codex_worker/worker.py` starts or resumes one persistent Codex
   thread. A normal message starts a streamed turn; a message arriving during
   that turn uses Codex steering. If steering races a completed turn, the
   worker returns `False` so the engine can safely requeue it.
4. Codex calls Hamroh actions through MCP. `hamroh/mcp_server.py` serves every
   enabled `BaseTool` on a random loopback HTTP port. The Codex thread config
   marks this server required, gives it the exact enabled-tool list, and
   approves those operator-selected MCP tools without an interactive prompt.
5. The worker converts SDK item notifications (assistant messages, MCP tool
   starts/results, errors, and turn completion) into the existing
   `TurnResult`. The engine then processes `stop`, `skip`, `sleep`, or
   `heartbeat` and marks the source messages consumed.

The persistent Codex thread ID is written atomically to `data/session_id`
before worker startup returns. A reboot can resume the same conversation. If
Codex reports that the thread is stale, Hamroh deletes that ID, starts a new
thread, and notifies through the existing recovery hook.

## Runtime policy and MCP configuration

`plugins.json` is provider-neutral input. `startup._build_codex_config()` turns
it into an isolated config attached to `thread/start` or `thread/resume`:

- The local `hamroh` MCP is `required=true`; its `enabled_tools` are exactly
  the built-ins left after `builtin_tools_disabled` filtering.
- An external `mcp__server` prefix means all tools from that server. Exact
  entries such as `mcp__server__search` become Codex `enabled_tools=["search"]`.
  A mismatched namespace is rejected instead of accidentally widening access.
- `stdio` MCPs receive their configured command, arguments, and explicit env.
  Remote HTTP MCPs receive their URL and static headers. Legacy `sse` entries
  are attempted as remote HTTP and produce a startup warning.
- Shell execution is enabled only when either the `bash` or `code` group is
  enabled. The Codex sandbox is `read-only` by default and becomes
  `workspace-write` only for the `code` group. Full host access is not a
  representable worker option.
- Subagents follow `tool_groups.subagents`. Codex apps, remote plugins, hooks,
  goals, runtime memories, and personality features are disabled because
  Hamroh supplies its own explicit equivalents.
- Every turn is `deny_all` for interactive approvals. Unexpected approval
  requests are also rejected by a fail-closed SDK handler.

Tool names on the wire are `mcp__<server>__<tool>`. The SDK reports server and
bare tool name separately, so the worker reconstructs that form for logs and
checks both fields before deciding that a successful Telegram action reached
the user. An external MCP cannot spoof delivery merely by reusing
`telegram_send_message` as its bare tool name.

## Structured output and dropped text

Telegram users see only successful Telegram tool calls. An ordinary final
assistant message exists inside the Codex thread but is not itself sent to a
chat. That separation creates a dangerous failure mode: the model can write a
perfect answer, end the turn, and leave the user waiting.

The worker therefore asks Codex for the JSON schema generated from
`ControlAction` and parses the final answer as one of:

- `stop`: the turn is complete, normally after a user-visible tool call;
- `skip`: deliberate silence is correct;
- `sleep`: resume work after a delay;
- `heartbeat`: continue a longer operation with progress handling.

Unexpected free-form final text is collected in `TurnResult.text_blocks`. If
there was no successful user-visible Hamroh tool call, the worker sets
`dropped_text=True`. The engine can recover stranded text for `stop`, while it
discards narration accompanying `skip` so deliberate group-chat silence does
not become spam. SDK commentary is logged as runtime narration and never
enters this recovery path.

The short `reason` attached to `stop`/`skip` helps operators diagnose claims
such as "replied" versus "silence is correct" without exposing private model
reasoning to Telegram.

## Supervision and recovery

`codex_worker` preserves the engine-facing behavior of the legacy worker:

- a liveness watcher interrupts a turn that produces no model or tool activity
  within the configured timeout;
- a closed SDK transport triggers a reconnect with exponential backoff;
- repeated crashes inside the rolling window raise `CrashLoop`, allowing
  Docker or systemd to restart the application;
- thread reset interrupts the active turn, removes the persisted ID, creates a
  fresh persistent thread, and saves the new ID atomically;
- shutdown interrupts active work and closes the SDK/app-server cleanly.

Dropped-text correction, reminders, and message at-least-once behavior remain
in the shared engine rather than being duplicated per provider. The legacy
Claude worker retains its provider-specific stream-JSON tool-error breaker.

## Important files and reading order

1. `README.md` and `hamroh/__main__.py` — product intent and boot sequence.
2. `hamroh/engine/engine.py` — batching and the control loop.
3. `hamroh/codex_worker/worker.py` and `spec.py` — SDK lifecycle, event
   translation, prompt/schema assembly, and secure launch.
4. `hamroh/startup.py` — plugin-to-Codex config and component wiring.
5. `hamroh/mcp_server.py` and one module under `hamroh/tools/telegram/` — how
   the model acts on the world.
6. `prompts/system.md` — behavioral rules applied as developer instructions.
7. `hamroh/cc_worker/` — only when maintaining the optional legacy Claude
   provider.

Supporting subsystems include `hamroh/db/` (SQLite), `hamroh/storage/`
(attachments and memory), `reminder_scheduler.py`, `plugins.py`,
`access.py`, and the tool modules under `hamroh/tools/`.
