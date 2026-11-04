# Resource usage and runtime lifecycle (007-HCA)

The resource changes retain Codex SDK/CLI 0.163.0, the configured model,
the saved conversation ID, and the existing authentication directory.

## Findings from the deployed revision's code

| Area | Finding | Change or verification |
| --- | --- | --- |
| Chromium | Startup called BrowserManager.warm(), launching Chromium and its Node driver for every boot. They remained resident even without browser use. | Startup no longer warms Chromium. Renders reuse one browser, which closes after 60 seconds without contexts. |
| Interactive pages | A browser session intentionally retains a page across navigate/click/fill calls. Closed pages could leave their old context referenced. | Old contexts close before replacement. An open interactive session is preserved until browser_reset or shutdown, so it remains a possible baseline cost. |
| Codex idle | The app-server and configured MCP subprocesses lived for the worker's lifetime. | Start on the first turn, close after 120 idle seconds, resume the same stored thread on the next turn. |
| Continuous turns | Last-turn item IDs were retained until the next turn. SDK-side notifications/history could remain for the process's lifetime. No live SDK heap profile was available. | Clear item caches, explicitly close streams, and recycle the SDK client before the next turn after 32 completed turns. This bounds client lifetime without changing conversation history. |
| Recovery ownership | Client close errors/timeouts were suppressed after discarding the client reference. Cancellation during initialization bypassed the Exception cleanup handler. These paths could allow an unconfirmed old process to survive replacement. | Retain and await one shielded close task before replacement. Cancellation also cleans up the partially initialized client. No private SDK router patch is added. |
| Thread reset | Repeated resets opened new threads on the same app-server, retaining server-side thread state until process exit. | Explicit reset closes the old runtime before creating a fresh thread. Normal idle/recovery resumes the existing thread. |
| Playwright cleanup | Node-driver stop had no timeout, and failed launch/cleanup could leave ownership unclear. | Bounded, shielded driver shutdown retains ownership on failure and retries without launching a duplicate driver. |
| Local MCP | The local FastMCP server used stateful HTTP sessions. Tools use shared ToolContext and do not require client-session state. | Stateless HTTP with JSON responses avoids retained connection sessions across SDK wake-ups. A real HTTP regression test exercises repeated connections. |
| Optional MCPs | Repository seed plugins disable all external MCPs. Production /data/config/plugins.json can differ and always wins over seeds. | Keep plugin functionality. Inspect the actual process tree and enabled production plugin names before attributing Node memory to a particular plugin. |
| Python baseline | Startup discovers all tool classes and imports the SQL parser even before a database tool is used. | SQLGlot loads on the first database query. Python, Telegram, SQLite, and the in-process HTTP MCP server remain resident. No measured RSS saving is claimed yet. |
| Polling/logging | Telegram uses long polling; reminders poll every 60 seconds, liveness every 30 seconds. The pending-message retry timer exists only with queued work. Logs rotate daily with seven backups. | Keep these functional loops. Diagnostics add a 300-second timer, not a busy loop. |
| Container | One replica, no HTTP healthcheck, zero deployment overlap, and tini are already configured. Installed Chromium/Node files do not imply their processes are running. | Preserve the deployment configuration and inspect cgroup memory/file cache alongside process RSS. No service sleep or periodic whole-service restart is used. |

These are code findings and prevented failure paths, not proof that every
failure path occurred in production. There is no production trace establishing
an upstream 0.163.0 leak or a measured regression caused by commit 9a2d755.

## Controls

| Environment setting | Default | Meaning |
| --- | --- | --- |
| HAMROH_CODEX_LAZY_START | true | Start the app-server on the first message/reminder. |
| HAMROH_CODEX_IDLE_SECONDS | 120 | Close only between turns after this idle interval; 0 keeps it warm. |
| HAMROH_CODEX_MAX_TURNS | 32 | Recycle the client before the following turn; 0 disables this bound. |
| HAMROH_BROWSER_IDLE_SECONDS | 60 | Close the browser and driver only when there are no live contexts; 0 keeps them warm. |
| HAMROH_RESOURCE_INTERVAL_SECONDS | 300 | Periodic snapshots; 0 disables periodic sampling, while lifecycle events remain. |
| HAMROH_RESOURCE_TRACE_HEAP | false | Enable optional tracemalloc allocation tracing from startup. |

Cold starts add initialization/resume latency. Adjacent requests share the
same process; active turns, tool calls, and interactive browser contexts are
not evicted by idle timers. Telegram polling and reminder delivery stay live.
If a send races unavailable recovery, the engine retains the unconsumed batch
and callbacks until the worker is ready.

Idle shutdown does not log out, unlink auth.json, clear session_id, archive
threads, or remove any persistent files. ChatGPT's managed token renewal
continues when Codex is active. See the
[OpenAI authentication documentation](https://learn.chatgpt.com/docs/auth).
The [app-server thread lifecycle](https://learn.chatgpt.com/docs/app-server)
documents resuming persisted threads; this change uses the already-supported
SDK resume API rather than relying on newer unload APIs.

The local MCP transport choice follows the
[MCP v1 server documentation](https://py.sdk.modelcontextprotocol.io/v1/server/#streamable-http-transport).
External MCP server configuration is unchanged.

## Measurements

The logger emits resource_snapshot JSON for boot, before_turn, after_turn,
codex_connected, codex_reconnected, codex_idle_closed, browser_started,
browser_idle_closed, and periodic sampling. Snapshots include the worker state,
turns per connection, saved-session presence, cached item count, and browser
context count.

Each process entry contains PID, parent PID, process type, current RSS,
anonymous/data bytes, CPU counters, thread count, and state. Arguments and
environment variables are never emitted. Node processes are classified as
Playwright or MCP candidates; the latter classification does not prove which
plugin owns a process.

CPU percent requires two snapshots of the same PID and start time. Summed RSS
double-counts shared pages, so cgroup current/anonymous/file/kernel charge is
reported separately. Cgroup peak is a high-water mark, not current usage.
Orphan candidates are recognized process types reparented to PID 1 outside
the bot tree; they require inspection rather than automatic termination.

CLI snapshots can inspect the live bot without starting another Codex or
browser process:

~~~bash
python -m hamroh.scripts.resources --label idle --count 12 --interval 30
python -m hamroh.scripts.resources --summary measurements.jsonl
~~~

The CLI defaults to the PID in HAMROH_DATA_DIR/.lock, not its own PID.
The SSH sampler is a separate Python process: bot/tree RSS excludes it,
while cgroup/platform totals include it during sampling. Use the same probe
before and after, and inspect platform idle usage again after probes exit.
Python allocation bytes are available only in the bot's own lifecycle logs
when tracemalloc is enabled. An external CLI cannot read another Python
process's heap. Allocation tracing excludes native Rust/Chromium memory and
allocations made before tracing started.

## Validation recorded on 2026-11-04

28 standalone lifecycle/resource/config tests passed locally using cached pytest.
Python 3.11 syntax checks passed for all framework and unit-test files.
Lifecycle, browser, database, engine, and real HTTP MCP tests were added or
extended and are included in CI.

The complete dependency environment could not be installed: the sandbox
cannot download Python 3.11 or missing packages. Docker access is denied.
The checkout is not linked to Railway and its CLI cannot reach the service.
Consequently native SDK/browser tests, deployment acceptance, and actual
before/after RSS/CPU measurements remain pending. No synthetic process
measurements are presented as Railway results.
