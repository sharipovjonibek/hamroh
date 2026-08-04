# hamroh documentation

Deep-dive technical documentation. The README is the high-level intro;
this is the manual. Read this when you're modifying internals,
debugging, or auditing.

## Highlights

The parts of hamroh:

- Custom MCP tools: Telegram messaging (text/reply/edit/delete/reactions/polls), memory, chat history, web search/fetch
- Vision + media: read inbound photos/docs/PDFs, render HTML and LaTeX to PNG, send photos back
- Browser automation: drive a real headless Chromium for JS-heavy and stateful pages (live network, stateful session)
- Structured turn control: every response ends with an explicit action
- Memory: persistent notes organized by person, group, or topic
- Reminders (one-shot + cron-recurring)
- Agent skills
- plugins.json to extend with external MCPs
- access.json for group and DM access with different access policy.
- Project rules: the owner can append deployment-specific instructions from a DM; every edit takes a timestamped backup first.
- Error handling
  - Tool, MCP, and model-turn failures are bounded instead of retrying forever.
  - The official Codex SDK/app-server is supervised: transport failures reconnect with exponential backoff and the persistent thread resumes.
  - If the model writes a reply without sending it, the engine sends a corrective turn so the reply is actually delivered.
  - If the API rejects a turn outright, the bot says so and respawns a fresh session automatically.
  - A circuit breaker aborts a turn after repeated tool errors instead of letting it spin for minutes.


## Table of contents

- [Highlights](#highlights)
- [What gets passed to Codex](#what-gets-passed-to-codex)
- [Full configuration](#full-configuration)
- [How it works (in detail)](#how-it-works-in-detail)
- [Known limitations](#known-limitations)
- [Adding a new tool](#adding-a-new-tool)
- [Access control](#access-control)
- [Memory](#memory)
- [Rendered visuals](#rendered-visuals)
- [Browser automation](#browser-automation)
- [Agent skills](#agent-skills)
- [Reminders](#reminders)
- [Run your own agent](#run-your-own-agent)
- [System prompt](#system-prompt)
- [External MCP integrations](#external-mcp-integrations)
- [Monitoring & observability](#monitoring--observability)
- [Security model](#security-model)
- [Manual end-to-end checklist](#manual-end-to-end-checklist)
- [Repo layout](#repo-layout)

## What gets passed to Codex

Hamroh uses the official `openai-codex` Python SDK. One SDK client owns a
long-lived Codex app-server and one persistent thread. For each turn the worker
sends the XML message batch, reasoning effort, model override (when set),
Codex sandbox, and Hamroh's JSON output schema. The base and project prompts,
skills/memory indexes, and exact `mcp__hamroh__<tool>` inventory are composed
once as developer instructions when the thread starts or resumes.

The local FastMCP server is attached through the thread's isolated Codex
config. It is `required`, and its `enabled_tools` are exactly the tools left
after `builtin_tools_disabled` filtering. External MCP prefixes or exact tool
names from `plugins.json` are translated to Codex server configuration; see
[tools.md](tools.md). Live web search is enabled. Shell execution, filesystem
writes, and multi-agent work remain off until their tool groups are enabled.
Every turn denies interactive approvals, and full host access is unsupported.

The SDK app-server starts through a minimal `env -i` environment. It receives
only runtime path/locale values plus the dedicated `CODEX_HOME`; it does not
inherit the Telegram token or external-MCP credentials from Hamroh's process.

The toggle source of truth is [`plugins.json`](../plugins.json) at
the repo root:

* `tool_groups` — maps `bash` and `code` to Codex shell features;
  `code` also selects the `workspace-write` sandbox. `subagents` maps to
  Codex's multi-agent feature.
* `mcps` — list of external MCP servers to spawn. Three transports
  supported: `stdio`, `http`, `sse`. `${VAR}` references pull
  credentials from `.env`. The shipped `plugins.json.example`
  carries sample Jira / GitLab / GitHub entries you can keep, edit,
  or delete — they're starting points, not first-class. Add a new
  entry to plug in any other MCP server.
* `builtin_tools_disabled` — names of hamroh built-in tools to
  hide (e.g. `telegram_create_poll`, `render_html`). Filtered at MCP
  registration time and omitted from Codex `enabled_tools`.
* `skills_disabled` — names of skill directories to hide.

The full per-tool list and schema reference live in [tools.md](tools.md); the
loader is `hamroh/plugins.py`, and `hamroh/startup.py` assembles the Codex
configuration. `hamroh/cc_worker/` remains only for
`HAMROH_PROVIDER=claude` compatibility.

## Full configuration

All settings come from environment variables (or a `.env` file). They are
read once when the bot starts, in `hamroh/config.py` (`Config.from_env`).
The rest of the code reads values from the `Config` object, never from
`os.environ` directly. To add a new setting, add a field to `Config`
instead of calling `os.environ.get` from somewhere else. Tests build a
`Config.for_test(tmp_path)` and set values on it, so they don't depend on
what's in your environment.

The one allowed exception is `hamroh/plugins.py` — it reads
`os.environ` directly to substitute `${VAR}` references in
`plugins.json` `mcps[].args`, `env`, `url`, and `headers` values.
That's how an external MCP's credentials reach the spawned server
(or the auth headers for an HTTP/SSE MCP) without being copied
into a `Config` field.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | from @BotFather |
| `HAMROH_OWNER_ID` | yes | — | your numeric Telegram user id |
| `HAMROH_PROVIDER` | no | `codex` | `codex` uses the official SDK and ChatGPT login; `claude` selects the legacy worker. |
| `HAMROH_MODEL` | no for Codex | — | Empty uses Codex's recommended subscription default. Required for the legacy Claude provider. |
| `HAMROH_EFFORT` | no | `high` | Codex reasoning effort: `none`, `minimal`, `low`, `medium`, `high`, or `xhigh` (model support varies). |
| `CODEX_BIN` | no | SDK-bundled | Optional full path to a compatible Codex binary; normally leave unset so the pinned SDK supplies its runtime. |
| `CODEX_HOME` | no | `<data>/codex` | Private Codex auth/session home. Compose sets `/var/lib/codex` on a named volume. Never point it at a shared operator `~/.codex`. |
| `CLAUDE_CODE_BIN` | legacy only | `claude` | CLI used only with `HAMROH_PROVIDER=claude`; not installed by the default image. |
| `HAMROH_DATA_DIR` | no | `./data` | SQLite, attachments, renders, logs, and persistent thread id. |
| `HAMROH_ACCESS_PATH` | no | repo-root `access.json` | override where `access.json` lives (mainly so the e2e harness can point at a temp file). |
| `HAMROH_DEBOUNCE_MS` | no | `0` | wait this long after a message before starting a model turn. Messages arriving during the wait are bundled. |
| `HAMROH_RATE_LIMIT_PER_MIN` | no | `20` | max DMs per minute from one user. The owner is not limited. Group chats are not limited. |
| `HAMROH_ATTACHMENT_MAX_BYTES` | no | `20000000` | largest inbound photo/document (20 MB) the bot will download and read; bigger files are refused with a marker. |
| `HAMROH_BROWSER_HEADLESS` | no | `true` | run the automation Chromium headless. Set `false` only for local debugging (visible window). |
| `HAMROH_LIVENESS_TIMEOUT_SECONDS` | no | `600` | if the runtime is mid-turn and silent (no output or tool activity) this long, interrupt/reconnect it. |
| `HAMROH_LIVENESS_POLL_SECONDS` | no | `30` | how often the watcher wakes up to check the timeout above. |
| `HAMROH_TOOL_ERROR_MAX_COUNT` | legacy Claude | `10` | failed-tool breaker threshold in `cc_worker`. |
| `HAMROH_TOOL_ERROR_WINDOW_SECONDS` | legacy Claude | `600` | time window for the legacy failed-tool breaker. |
| `HAMROH_CRASH_BACKOFF_BASE` | no | `2` | seconds before the first runtime reconnect; doubles after each crash up to the cap. |
| `HAMROH_CRASH_BACKOFF_CAP` | no | `64` | maximum wait between restarts. Once the wait reaches this, it stops growing. |
| `HAMROH_CRASH_LIMIT` | no | `10` | how many crashes within `CRASH_WINDOW_SECONDS` count as "too many". When reached, the bot tells the owner (crashes are the operator's to handle, so waiting chats stay silent), then exits — and something outside (systemd, docker) is expected to restart the whole bot. |
| `HAMROH_CRASH_WINDOW_SECONDS` | no | `600` | the time window used for `CRASH_LIMIT`. Only crashes from the last X seconds are counted. |
External-service credentials referenced by the default `plugins.json`
via `${VAR}`. Set these in `.env` to make the corresponding MCP
spawn; clear them to silently skip its MCP at boot.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GITLAB_URL` | no | — | GitLab URL — referenced by the `mcp-gitlab` plugin entry |
| `GITLAB_TOKEN` | no | — | GitLab personal access token — same |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | no | — | GitHub PAT — referenced by the `github` plugin entry. For Enterprise, add `GITHUB_HOST` to the entry's `env` block in `plugins.json` and set it here too. |

Who can DM the bot or use it in groups is set in `access.json` at the
repo root (sibling of `plugins.json`), not in environment variables.
See [Access control](#access-control).

## How it works (in detail)

Four application parts run together; Codex's SDK-managed app-server is the
child runtime:

```
Telegram listener  →  Engine (buffer + send/inject)  →  Codex worker  →  SDK/app-server
                                  │                                        │
                                  ▼                                        ▼
                               SQLite                             MCP server (HTTP, localhost:0)
```

1. **Telegram listener** (`hamroh/telegram_io/`). Uses
   python-telegram-bot v21 in polling mode. For each message it does two
   things: save it to SQLite, then hand it to the engine. Owner-only
   commands (`/kill`, `/health`, `/audit`, the access commands) skip the
   engine and run directly.
2. **Engine** (`hamroh/engine/`). Holds the pending message buffer,
   the debounce timer, the mid-turn processing flag, and the inject path.
   Bundles messages that arrive close together. If a new message comes in
   while the model is mid-reply, the engine sends it via `worker.inject()` so
   the running turn picks it up. If a turn ends with text but no
   `telegram_send_message` call (we call this "dropped text"), the engine sends a
   corrective `<error>...</error>` block to nudge the model into using the
   tool.
3. **Codex worker** (`hamroh/codex_worker/`). Starts/resumes a persistent SDK
   thread, streams typed item and completion notifications, translates them to
   the shared `TurnResult`, and steers mid-turn messages. It saves the thread
   ID atomically, interrupts wedges, and reconnects the app-server with
   exponential backoff after transport failure.
4. **MCP server** (`hamroh/mcp_server.py`). A FastMCP server on a
   random port on `127.0.0.1`. It finds every `BaseTool` subclass in
   `hamroh/tools/` and registers it. It writes a small JSON config
   registry whose URL and enabled tools are attached to the Codex thread.

## Known limitations

### One turn at a time

The engine handles **one model turn at a time**. While Codex is busy
with a long task (a code review, a big GitLab search, a complex Jira
query), the engine waits for it to finish. Messages from other chats
sit in the buffer and only go through after the current turn ends.

So a 3-minute code review for Chat A will delay replies to Chat B by up
to 3 minutes. For one user or a small group, this is fine. For busy
setups with many chats, run a separate hamroh for each chat group.

The system prompt tells the bot to send a quick "On it, reviewing
now..." reply via `telegram_send_message` before it starts a long task, so users
know the bot got their message even when the full reply takes time.

## Adding a new tool

Drop a single file in `hamroh/tools/`. No core code changes:

```python
# hamroh/tools/echo.py
from pydantic import BaseModel, Field
from hamroh.tools.base import BaseTool, ToolResult


class EchoArgs(BaseModel):
    text: str = Field(description="What to echo back.")


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo a string back to the caller."
    args_model = EchoArgs

    async def run(self, args: EchoArgs) -> ToolResult:
        return ToolResult(content=args.text)
```

Restart `python -m hamroh`. The tool is live.

## Access control

`access.json` at the repo root governs who can talk to the bot.
Hot-reloaded on every inbound message. Gitignored; template at
`access.json.example`. First run seeds `policy: "owner_only"` with
empty allowlists.

```json
{
  "policy": "owner_only",
  "allowed_users": [],
  "allowed_chats": [-1001234567890]
}
```

| Policy | DMs | Groups |
|---|---|---|
| `owner_only` (default) | Owner only | Blocked |
| `allowlist` | Owner + `allowed_users` | `allowed_chats` |
| `open` | Anyone | Any group |

The owner is always allowed in DMs. Blocked messages never reach the
`messages` or `users` tables and never trigger memory writes, tool
calls, or engine work. They are logged to a separate
`unauthorized_messages` table (chat_id, user_id, text, timestamp, …)
so the owner can review demand without polluting the main history.

In DMs only, the first blocked message from a new chat receives a
one-time canned reply: `"This is a private assistant. Please contact
the owner if you want an access."` Subsequent messages from the same
chat are silently dropped (still logged). Unauthorized groups stay
fully silent. Server-side logs continue to record every attempt.

### Owner commands

Owner-only — silently no-op for everyone else. `update.effective_user.id
== HAMROH_OWNER_ID` is the actual gate; `BotCommandScopeChat` just
hides the `/` menu from non-owners.

```
/access                      Show policy + allowlists
/allow user <id>             Add user to allowed_users
/allow group <chat_id>       Add chat to allowed_chats
/deny user <id>              Remove user
/deny group <chat_id>        Remove chat
/policy <owner_only|allowlist|open>
/pause                       Drop all inbound messages until /resume —
                             messages still arrive but are dropped (not
                             queued); in-memory only, resets on restart
/resume                      Re-enable message forwarding
/kill                        SIGTERM (graceful shutdown)
/reset_session               Replace the saved provider session/thread —
                             fresh context, chat history and memories kept
/health                      Pause status, last send, reminder state,
                             rate-limit notices, current turn duration,
                             queued messages
/audit                       Recent tool failures, backups, memory footprint
/logs [N]                    Tail the JSON log file (last 50 lines, or N)
/usage                       Show active Codex model/effort and commands for
                             login/rate-limit inspection. Legacy Claude relays
                             its CLI usage report.
```

Application logs are written two ways: human-readable text to the console
(captured by `docker logs`) and a structured JSON line per record to
`data/logs/hamroh.log` (rotated daily, 7 days kept). Each JSON record carries
`ts`, `level`, `component` (derived from the logger — `dispatcher`, `codex_worker`,
`tx`, `mcp`, `reminder`, …), `logger`, and `msg`. The root level is set by
`HAMROH_LOG_LEVEL` (default `INFO`); `/logs` tails this file from Telegram.

Edit `access.json` directly if you prefer — changes are hot-reloaded.

## Memory

`memories/*.md` is where the bot keeps its notes. It has six tools:

- `memory_list` — list the files
- `memory_search` — search the text inside files for keywords, best matches first
- `memory_read` — read a file (cuts off at 64 KiB)
- `memory_write` — create or overwrite a file (max 64 KiB)
- `memory_append` — add text to an existing file
- `telegram_send_memory_document` — send a memory file to a chat as a Telegram
  document (path-locked to `memories/`, optional caption + reply-to)

**Read before write.** To overwrite or append to a file that already
exists, the bot has to read it first in the same session. Brand-new files
are exempt. The list of "files I've read" is held in memory and clears
every time the bot restarts, so a fresh start has to re-read before
changing anything. This stops the bot from accidentally destroying notes
you wrote but it never read.

**No delete tool, on purpose.** If the bot wants to "forget" something,
it has to overwrite the file. Actually deleting a file is up to you:
`rm memories/<file>` on the host.

### One store: `memories/`

All memory lives in the single `memories/` folder at the repo root — one store,
no read-only tier. The bot reads, searches, writes, and appends here; you can
seed a file yourself and it shows up on the next `memory_list`.

The folder is **git-tracked**, so memories carry full history and survive a
lost volume, rebuild, or new machine. In Docker it's bind-mounted
(`./memories:/app/memories`), so runtime writes land in your checkout, ready to
`git commit`.

Every memory is named by its **full path** starting with `memories/` — a bare
`notes/ref.md` is rejected, so pass paths verbatim from `memory_list` /
`memory_search`. See [`memories/README.md`](../memories/README.md) for the
full how-to.

## Rendered visuals

Two tools turn structured data into a Telegram photo:

- `render_html(html, width?=800, height?=600, title?)` — runs the HTML
  through headless Chromium (Playwright) with **all outbound network
  blocked at the route layer**, takes a full-page PNG, saves it under
  `data/renders/<utc-stamp>-<slug>-<rand>.png`, returns the relative
  path. Inline any CSS/JS the page needs (Chart.js, D3, fonts) — the
  browser can't fetch.
- `telegram_send_photo(chat_id, path, caption?, reply_to_message_id?)` — sends
  a file from `data/renders/` as an inline Telegram photo. Path-locked
  to the renders root with the same hardening as `memory_read`.

When composing HTML, keep all CSS and JavaScript inline, choose a layout
that matches the data, and make labels readable at the requested image
size. The render tool does not impose a project-specific visual style.

Playwright + Chromium are pre-installed in the Docker image. For local
runs: `uv sync && uv run playwright install chromium`.

## Browser automation

For JS-rendered, multi-step, or form-driven pages,
the bot drives a real headless Chromium. Unlike `render_html` — which
runs network-blocked — the browser tools have **live network access**,
so this is the path for interacting with the open web. Private/internal
targets (localhost, RFC1918, link-local, `file://`) are still refused.

The key difference from the one-shot render path is **session state**:
`browser_navigate` opens one shared page, and every other `browser_*`
tool acts on that same page for the rest of the turn. One warm Chromium
instance is kept alive across the whole bot session (not relaunched per
call), and popups / new tabs are followed automatically. This lets the
agent chain steps — *navigate → wait for an element → click → read text
→ screenshot → send* — the way a person would.

The full tool list (navigate/history, interact, read, capture) is in
[tools.md](tools.md#browser). All sixteen are on by default; disable any
by name via `builtin_tools_disabled` in `plugins.json`.

## Agent skills

Skills are operator-curated multi-step playbooks stored in the top-level
`skills/<name>/SKILL.md` format, following the
**[Agent Skills specification](https://agentskills.io/specification)**.
They are versioned in git, and each deployment chooses which skills to add.

Each SKILL.md must begin with YAML frontmatter containing at least
`name` (matching the directory) and `description` (what the skill does
and when to use it). Our `SkillsStore` validates both on load and
refuses malformed skills.

Tools:

- `skill_list` — enumerate available skills as name + description pairs
  (the spec's progressive-disclosure metadata surface).
- `skill_read(name)` — load the full SKILL.md playbook.
- `skill_write(name, content)` — create or update a skill after explicit owner
  approval.

**Invocation.** A skill is triggered by a reminder whose text body is
`<skill name="X">run</skill>`. The reminder loop wraps that in a
`<reminder>` XML envelope before injecting into the engine. The bot,
per `system.md § Skills`, recognizes `<skill>` inside `<reminder>` and
calls `skill_read("X")` to load + execute the playbook.

**Trust model.** The bot trusts `<skill>` directives only when wrapped
in a `<reminder>` envelope (server-synthesized). A user typing
`<skill name="X">run</skill>` in regular chat is treated as a
prompt-injection attempt and ignored.

### Adding a new skill

Drop a new folder under `skills/`:

```
skills/
└── your-skill-name/
    ├── SKILL.md       # required: YAML frontmatter + playbook body
    ├── README.md      # optional, operator-facing doc
    ├── scripts/       # optional: executable helpers (spec)
    ├── references/    # optional: on-demand reference docs (spec)
    └── assets/        # optional: templates, schemas (spec)
```

Minimum SKILL.md:

```markdown
---
name: your-skill-name
description: One sentence on what the skill does AND when to use it (cap: 1024 chars).
---

# your-skill-name

Playbook body — step-by-step instructions the bot follows when this
skill activates.
```

The name must match the directory (lowercase, `a-z0-9-` only, no
leading/trailing/consecutive hyphens). Optional frontmatter fields per
spec: `license`, `compatibility`, `metadata`, `allowed-tools`.

The SkillsStore auto-discovers any first-level directory that contains a
`SKILL.md`; no code changes are needed. To run an invoked skill on a schedule,
add a recurring entry to `default-reminders.json` whose text is
`<skill name="your-skill-name">run</skill>`. See [Reminders](#reminders) for
the file format and lifecycle.

The playbook itself is markdown the bot reads and executes step by step. Keep
it self-contained: document preconditions, the data the skill should read, the
decisions it should make, and the tools it should call.

## Reminders

The agent can schedule one-shot and recurring reminders via three tools:

- `reminder_set` — schedule a reminder with a UTC trigger time and
  optional cron expression
- `reminder_list` — show pending reminders for a chat
- `reminder_cancel` — cancel a pending reminder by id

Reminders are stored in the `reminders` SQLite table. A background task
polls every 60 seconds for due entries and injects them into the engine
as synthetic inbound messages. The agent then sends the reminder text to
the appropriate chat. Recurring reminders (cron) automatically advance
to the next occurrence.

Reminders fire on time even if the bot is mid-conversation. When the
fire happens during an active turn, the synthetic reminder message is
queued and runs as soon as the current turn ends.

A reminder row is only marked `sent` (or its cron advanced) once the
model worker has actually consumed the turn. If the runtime crashes or wedges
mid-turn, the row stays `pending` and the next 60s loop tick re-fires
it — without this, a wedged subprocess would silently lose the
reminder.

All times are stored in UTC. The system prompt instructs the agent to
ask users for their timezone and convert to UTC before setting
reminders.

### Custom reminders (`default-reminders.json`)

Beyond reminders the agent sets at runtime, you can ship a fixed set of
**recurring** reminders with the bot in a git-tracked `default-reminders.json`
at the repo root (gitignored in this framework repo; copy
`default-reminders.json.example` to start, or keep it in your instance repo and
bind-mount it — see [Run your own agent](#run-your-own-agent)).

```json
{
  "reminders": [
    {
      "name": "morning-trends",
      "cron": "0 6 * * *",
      "chat": "owner",
      "text": "Post today's trends digest."
    }
  ]
}
```

Each object: `name` (required, unique — identifies the reminder across edits),
`cron` (required, 5-field, UTC), `text` (required), `chat` (optional: `"owner"`
default, or a numeric chat id), `enabled` (optional: `true` default, or `false`
to turn the reminder off without deleting its entry). JSON has no comments, so
keep notes out of the file itself.

`text` may be a plain string or a **list of strings joined with newlines** —
handy for long, multi-paragraph prompts, since JSON has no multi-line literals.
Both forms produce identical text (and the same seed key), so switching between
them never triggers a reseed:

```json
{
  "reminders": [
    {
      "name": "morning-brief",
      "cron": "0 6 * * *",
      "text": [
        "Good morning. Put together today's brief:",
        "",
        "1. Top 3 AI stories.",
        "2. Calendar conflicts this week."
      ]
    }
  ]
}
```

How it behaves:

- **Reconciled at every boot.** The startup hook diffs the file against
  the database: declared entries with no pending row are seeded, and
  committed rows no longer in the file are cancelled.
- **Edits apply on restart.** The seed key is content-addressed
  (`committed:<name>:<hash of cron+text+chat>`), so editing any field
  cancels the stale row and seeds a fresh one. Removing an entry cancels
  it, and so does setting `"enabled": false` — the entry stays in the file
  as an off switch, and flipping it back to `true` seeds the reminder again.
- **Source of truth is the file.** Because each row carries an
  `auto_seed_key`, the agent cannot cancel these from chat — change the file
  and restart instead.
- **Recurring only.** A one-shot would re-fire on every restart once
  sent, so only cron reminders are accepted here; for a one-off, ask the
  bot in chat (it uses `reminder_set`).
- A missing file means no custom reminders; a malformed file crashes boot
  loudly rather than silently dropping a reminder.

Implementation: parser in `hamroh/scheduler/reminders_config.py`, reconciler
`_reconcile_committed_reminders` in `hamroh/startup.py`.

## Run your own agent

Never fork. Keep this repo as the framework, pull it into your own agent
repo as a **git submodule**, and put your identity — persona, skills,
memories, config — in files that bind-mount over the image at runtime.

```
my-agent/                    # your private repo
├── framework/               # git submodule → github.com/sharipovjonibek/hamroh
├── Dockerfile         
├── docker-compose.yml       # runs framework/, mounts the files below
├── .env                     # bot token, owner id, plugin secrets — gitignore this
├── prompts/
│   ├── system.md            # seeded from framework/ — required
│   └── project.md           # bot name, language, personality
├── skills/                  # framework playbooks (seeded) + your own
├── memories/                # the bot's memory (git-tracked)
├── plugins.json             # tools + MCP capability surface
├── access.json              # DM / group policy
└── default-reminders.json   # custom recurring reminders
```

Set it up once:

```bash
git init my-agent && cd my-agent
git submodule add https://github.com/sharipovjonibek/hamroh framework
cp framework/.env.example .env             # fill TELEGRAM_BOT_TOKEN + HAMROH_OWNER_ID
chmod 600 .env
cp framework/prompts/project.md.example prompts/project.md
cp framework/prompts/system.md prompts/system.md         # required — re-copy after a framework bump
cp -R framework/skills/. skills/                         # seed the directory; add your own skills
cp framework/plugins.json.example plugins.json
cp framework/access.json.example access.json
cp framework/default-reminders.json.example default-reminders.json
```

`docker-compose.yml` — the submodule is the build context, everything else is a mount:

```yaml
services:
  hamroh:
    build: ./framework
    image: my-agent:local
    env_file: .env
    environment:
      CODEX_HOME: /var/lib/codex
    volumes:
      - ./data:/app/data
      - codex-home:/var/lib/codex
      - ./prompts:/app/prompts
      - ./skills:/app/skills
      - ./memories:/app/memories
      - ./plugins.json:/app/plugins.json
      - ./access.json:/app/access.json
      - ./default-reminders.json:/app/default-reminders.json:ro
    working_dir: /app

  codex-auth:
    image: my-agent:local
    profiles: ["auth"]
    network_mode: host       # expose the OAuth localhost callback on port 1455
    environment:
      CODEX_HOME: /var/lib/codex
    volumes:
      - codex-home:/var/lib/codex
    working_dir: /app
    command: ["python", "-m", "hamroh.codex_login"]

volumes:
  codex-home:
```

Run it:

```bash
docker compose build hamroh
docker compose run --rm codex-auth   # prints browser URL; waits on localhost:1455
docker compose up -d hamroh
docker compose logs -f hamroh
```

Notes:

- **Clone with the submodule.** A plain clone leaves `framework/` empty and
  the build fails — use `git clone --recurse-submodules`, or run
  `git submodule update --init` after cloning.
- **`prompts/` and `skills/` replace, they don't merge** — the mount hides the
  image's baked `system.md` and built-in skills, which is why you seed both
  above. Re-run those two `cp`s after a framework bump.
- **Update the framework:** `cd framework && git pull origin main && cd .. &&
  git add framework && git commit -m "bump framework"`.
- **Keep Codex private.** Mount the named `codex-home` volume, not the host's
  general `~/.codex`. The auth service does not need `env_file: .env`, and
  therefore cannot see Telegram or plugin secrets.
- **Browser OAuth is the default.** Open the printed authorization URL in a
  browser on this host. For a remote host, forward port `1455` over SSH first.
  If that callback cannot be forwarded, explicitly append
  `python -m hamroh.codex_login --device-code` to the `codex-auth` run command
  to use device code as a fallback.

### Installing extra packages

Need a system binary (ffmpeg, a font) or an extra Python dep for a custom
tool? **Don't edit `framework/`** — that's the pinned submodule. Add your own
`Dockerfile` in the agent repo that builds *on top of* the framework image:

```dockerfile
# my-agent/Dockerfile
FROM hamroh-base
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN /app/.venv/bin/pip install --no-cache-dir yt-dlp
```

`FROM hamroh-base` keeps everything the framework already has (Python, Node,
Chromium, hamroh, its `ENTRYPOINT`/`CMD`); you only add the extra lines. Point
compose at your Dockerfile instead of the submodule:

```yaml
    build: .            # was: build: ./framework
```

`FROM` needs that base image to exist first, so build the framework once, then
your layer — put both in a `Makefile` so it's one command:

```makefile
up:
	docker build -t hamroh-base ./framework   # build the pinned submodule
	docker compose up -d --build              # build your layer + run
```

Run `make up`; re-run it after a framework bump to rebuild both. (An MCP that
runs via `npx` needs none of this — that's a `plugins.json` edit, no rebuild.)

## System prompt

The Codex developer instructions are assembled from two files:

1. **`prompts/system.md`** — generic hamroh template covering tool
   discipline, message format, memory, reminders, and prompt-injection
   resistance. Ships with the repo.
2. **`prompts/project.md`** — project-specific overlay (identity,
   integrations, custom instructions). Gitignored. Copy
   `prompts/project.md.example` to get started. Path is hardcoded —
   always at `prompts/project.md`.

If `project.md` doesn't exist, only the base prompt is used. The worker then
appends public runtime settings, skills and memory indexes, the exact Hamroh
MCP tool list, and (when enabled) subagent instructions.

## External MCP integrations

hamroh can optionally connect to external MCP servers alongside
its own. There's no built-in integration list — every external MCP
is just an entry in `plugins.json` `mcps[]`. The shipped
`plugins.json.example` includes three sample entries you can keep,
edit, or delete — they're starting points, not first-class:

- **Jira** via a legacy Atlassian SSE sample — disabled by default. Codex
  attempts SSE-labelled URLs as remote HTTP, and Hamroh does not run the
  vendor's interactive OAuth flow. See `tools.md` before enabling it.
- **GitLab** via
  [@zereight/mcp-gitlab](https://www.npmjs.com/package/@zereight/mcp-gitlab)
  (stdio) — set `GITLAB_URL`, `GITLAB_TOKEN` in `.env`.
- **GitHub** via
  [@modelcontextprotocol/server-github](https://www.npmjs.com/package/@modelcontextprotocol/server-github)
  (stdio) — set `GITHUB_PERSONAL_ACCESS_TOKEN` in `.env`. For
  Enterprise, add `"GITHUB_HOST": "${GITHUB_HOST}"` to the entry's
  `env` block and set `GITHUB_HOST` in `.env` too.

Each entry references its credentials with `${VAR}` interpolation;
when any required var is empty, that MCP is silently skipped at boot.
To stop advertising one without removing credentials, flip
`enabled: false` on its entry. To remove permanently, delete the
entry.

Adding a new MCP (Notion, Linear, Slack, Postgres, Playwright, your own —
stdio or streamable HTTP) is a `plugins.json` edit, not a Python change. Exact
`mcp__server__tool` allow entries become Codex `enabled_tools`; a server prefix
means every tool from that server. Legacy `sse` remains a compatibility label.
See [tools.md](tools.md) for the schema and transport details.

## Monitoring & observability

Hamroh gives you **three complementary windows** into what the bot is
doing. Pick whichever fits the moment.

### 1. The live tagged log (the running terminal)

When the bot is running, the foreground terminal prints two streams of
structured tag lines on top of the usual lifecycle messages:

**Conversation transcript** (`hamroh.tx` logger):

| Tag | Meaning |
|---|---|
| `[RX]` | inbound message we forwarded to the engine |
| `[DROP]` | inbound message persisted but dropped (chat not allowed) |
| `[RX↺]` | inbound edited message |
| `[TX]` | outbound `telegram_send_message` / `telegram_reply_to_message` |
| `[EDIT]` / `[DEL]` / `[REACT]` | outbound edits, deletions, reactions |

**Model worker transcript** (`hamroh.cc` logger; the historical `CC` tag is
kept so existing log tooling still works):

| Tag | Meaning |
|---|---|
| `[CC.user]` | the XML batch sent to the configured model worker |
| `[CC.text]` | a text block the assistant emitted (rare; signals dropped-text) |
| `[CC.tool→]` | the assistant called a tool (with args + tool_use_id) |
| `[CC.tool✓]` / `[CC.tool✗]` | a tool returned (success / error) |
| `[CC.done]` | turn finished, parsed `action` + `reason` |

Sample (DM with one message):

```
21:34:12 INFO  hamroh.tx       [RX] DM Alice[12345] m42 | how fast are you
21:34:12 INFO  hamroh.engine   starting turn with 1 msgs
21:34:12 INFO  hamroh.cc       [CC.user] <msg id="42" chat="12345" ...>↵how fast are you↵</msg>
21:34:13 INFO  hamroh.cc       [CC.tool→] mcp__hamroh__send_message({"chat_id":12345,"text":"Honestly?…"}) id=toolu_01
21:34:14 INFO  hamroh.tx       [TX] DM Alice[12345] m43 | Honestly? Not blazing fast 😅 …
21:34:14 INFO  hamroh.cc       [CC.tool✓] id=toolu_01 | sent message_id=43
21:34:14 INFO  hamroh.cc       [CC.done]  action=stop reason=Answered the user's question
```

The `httpx`/`mcp` per-poll noise is silenced by default. To bring it
back for debugging, comment the relevant lines in
`hamroh/startup.py:_setup_logging()`.

### 2. Persistent Codex thread and rotating application log

`data/session_id` contains the current persistent Codex thread ID. It is
written atomically as soon as a thread starts or resumes; it is an opaque
recovery handle, not a transcript file. Authentication and Codex-owned state
live under the private `CODEX_HOME` (`/var/lib/codex` in Compose). Do not inspect
or share that directory as an observability interface because it contains
OAuth material.

Hamroh's structured rotating log lives under `data/logs/` and is the supported
record of SDK events translated by the worker. The legacy
`hamroh.scripts.trace` and `data/cc_logs/` raw subprocess captures apply only
when `HAMROH_PROVIDER=claude`; the Codex SDK path does not write Claude session
JSONL or a Claude stdout/stderr wire capture.

Useful checks:

```bash
make auth-status
docker compose logs -f hamroh
ls -l data/session_id data/logs/
```

### 3. SQLite — auditable, queryable history

Everything that touches Telegram or any MCP tool is in
`data/hamroh.db`. Useful one-liners:

```bash
# Last 10 messages in/out (from any chat)
sqlite3 data/hamroh.db \
  "SELECT direction, chat_id, user_id, substr(text,1,80) AS text
   FROM messages ORDER BY timestamp DESC LIMIT 10;"

# Every MCP tool call the bot has made (newest first)
sqlite3 data/hamroh.db \
  "SELECT created_at, tool_name, duration_ms, error
   FROM tool_calls ORDER BY id DESC LIMIT 20;"

# Per-user activity in a specific chat
sqlite3 data/hamroh.db \
  "SELECT username, first_name, message_count, last_message_date
   FROM users WHERE chat_id = 12345 ORDER BY message_count DESC;"

# Find every reply chain involving a specific user
sqlite3 data/hamroh.db \
  "SELECT message_id, reply_to_id, substr(text,1,100)
   FROM messages WHERE user_id = 12345 AND reply_to_id IS NOT NULL;"
```

`database_query` (the MCP tool) lets the agent run SELECTs against this same
database — sqlglot-validated, capped at 100 rows. `database_get_recent_messages`
returns the latest messages without writing SQL.

### Cheatsheet

| You want to know… | Look at |
|---|---|
| Who said what to who right now | the foreground terminal (`[RX]`/`[TX]` lines) |
| Which tools is it calling and why | the foreground terminal (`[CC.tool→]`/`[CC.done]` lines) |
| SDK/MCP lifecycle and model errors | `docker compose logs -f hamroh` and `data/logs/` |
| Whether ChatGPT authentication is present | `make auth-status` |
| Current resumable thread | `data/session_id` (identifier only) |
| Aggregate stats / cross-session queries | `sqlite3 data/hamroh.db` |

## Security model

The agent is a *front-facing public agent*. Anyone in an allowed chat
can talk to it, and they're not always trustworthy. The security model
is enforced by code, not by hope, and tested in
`tests/test_security_invariants.py`.

- **No shell, workspace writes, or subagents by default.** Codex starts with
  shell/unified execution and multi-agent features disabled in an isolated
  per-thread config. The sandbox is `read-only`; only `tool_groups.code`
  raises it to `workspace-write`, and full host access is never selected.
  Every turn uses `approval_mode=deny_all`, with a fail-closed handler for an
  unexpected approval request. See [tools.md](tools.md).
- **Private Codex identity and clean runtime environment.** Compose mounts a
  named volume at `CODEX_HOME=/var/lib/codex`; it never mounts the operator's
  general `~/.codex`. The auth helper does not load `.env`, and the worker
  launches the app-server through `env -i`, so Telegram and plugin secrets do
  not enter the model runtime's ambient environment. Explicit external-MCP
  credentials still travel in that server's scoped Codex configuration.
- **Web access.** Codex live web search is deliberately enabled so the agent
  can answer questions that need fresh information. This is a real trade-off.
  The system prompt instructs the agent to refuse private/internal
  URLs (localhost, RFC1918, link-local, `.local`), but a determined
  prompt-injection could still get it to fetch one. **Do not deploy
  the bot on a host with sensitive internal endpoints reachable from
  the same network.**
- **MCP namespace lockdown.** The local MCP server is registered as
  `hamroh`, marked required, and given the exact filtered `enabled_tools`.
  Every local call is logged as `mcp__hamroh__<x>`. External prefixes/exact
  names are validated before translation so a typo cannot silently widen a
  server's surface. User-visible delivery checks both server and tool name.
- **Memory writes with safety rails.** `memory_write` and
  `memory_append` exist, but are guarded by:
  - **Path traversal hardening** (no `..`, no absolute paths, no
    symlinks) — applies to writes the same way it applies to reads.
  - **64 KiB per-file size cap** — both writes and post-append totals.
  - **Read-before-write** — overwriting or appending to an *existing*
    file requires `memory_read` to have been called on it first in the
    same session. New files are exempt. The set of "read paths"
    resets on every restart so a fresh process must re-read before
    mutating.
  - **No deletion tool** — forgetting requires explicit overwriting.
- **No filesystem reads outside `memory.py`.** AST scan asserts no
  `open()` / `read_text()` / `read_bytes()` lives in any other tool
  module.
- **No subprocess calls in tools.** AST scan rejects `subprocess.*`,
  `os.system`, `os.popen`, `asyncio.create_subprocess_*` anywhere
  under `hamroh/tools/`. Provider runtime creation stays in the dedicated
  worker integration rather than becoming a model-callable tool.
- **Owner-only privileged commands.** `/kill`, `/health`, `/audit`,
  `/access`, `/allow`, `/deny`, `/policy` check
  `update.effective_user.id == HAMROH_OWNER_ID` before running and
  silently no-op for anyone else.
- **`database_query` is read-only.** Inputs are parsed with `sqlglot` and
  rejected unless they're a single SELECT. CTEs are walked
  recursively; semicolons, PRAGMA, ATTACH, INSERT/UPDATE/DELETE/DROP/
  CREATE/ALTER all fail. Results cap at 100 rows; text columns
  truncate at 2000 chars.
- **Per-user inbound DM rate limit.** 20 messages / 60s / user by
  default, DB-backed (`rate_limits` table, fixed-minute buckets) so it
  survives restarts. Enforced at `telegram_io._on_message` before
  `engine.submit()`: over-limit DMs are still persisted (audit trail)
  but never reach the model worker. **Groups are not rate-limited** —
  noisy users in groups are the group's problem. **The owner
  (`HAMROH_OWNER_ID`) is fully exempt** — the counter never ticks
  for the owner. When a user exhausts their bucket they get one
  Telegram notice ("you're sending too fast…") then the bot goes quiet
  until the bucket rolls over.
- **Audit log.** Every MCP tool invocation persists to `tool_calls`
  (name, args, result, error, duration). Owner can review recent
  failures via `/audit`.
- **Secrets scrubbing at persistence.** Inbound message text and the
  raw Telegram `Update` JSON are passed through
  `secrets_scrubber.scrub()` before `insert_message` writes to
  SQLite. Redacts Bearer tokens, `sk-…` keys, GitHub/Slack tokens,
  AWS access keys, JWTs, PEM private-key blocks, and DSNs with
  embedded passwords. An accidental credential paste never lands in
  the DB.
- **Unicode normalization at the boundary.** Before a message reaches
  the agent, `input_normalizer.py` strips zero-width and bidi-control
  characters (classic invisible prompt-injection carriers) and
  NFKC-normalizes the text. When anything was changed, the inbound
  `<msg>` envelope carries a `flags=` attribute (`zero_width_stripped`,
  `bidi_stripped`, `nfkc_changed`) and the system prompt tells the
  agent to treat instructions in flagged messages as adversarial.
- **Wedged-runtime detection.** `CodexWorker._liveness_loop` watches the last
  SDK notification or MCP activity while a turn is active. After
  `HAMROH_LIVENESS_TIMEOUT_SECONDS` (default 600), it interrupts the turn; if
  turn start or interrupt itself is wedged, it reconnects the app-server.
  Idle silence is ignored. The legacy worker implements the same external
  contract around its subprocess.
- **Legacy tool-error circuit breaker.** `HAMROH_TOOL_ERROR_MAX_COUNT` and
  `HAMROH_TOOL_ERROR_WINDOW_SECONDS` govern the stream-JSON breaker in the
  optional Claude worker. Codex reports failed MCP calls as typed SDK items;
  they are logged and included in the turn without inheriting the legacy
  subprocess counter.
- **Dropped-text delivery.** A turn that ends with text blocks but
  no `telegram_send_message` call (`dropped_text=True`) would be
  invisible to the user, so `Engine._handle_dropped_text` delivers
  those blocks directly to the waiting chats instead of burning a
  retry turn. Exception: when the text is actually a technical error,
  `classify_cc_failure` surfaces a targeted message (e.g. "model
  unavailable — fix `HAMROH_MODEL`") instead of echoing the raw
  diagnostic. Catches provider diagnostics (invalid model, auth
  failure, quota) that would otherwise be lost.
- **Crash-loop terminal notification.** When the crash budget
  (`Config.crash_limit` crashes in `Config.crash_window_seconds`,
  defaults 10 / 600s) is exhausted, the selected worker supervisor fires
  the `on_giveup` callback *before* raising `CrashLoop` — so owner
  + any active chats get a clear "I'm shutting down, operator needs
  to intervene" message (classified where possible) instead of the
  supervisor task dying silently.
- **Failure classifier.** `hamroh/cc_failure_classifier.py` retains the
  shared mapping from provider diagnostics / text blocks to
  user-facing messages. Used by the engine's post-turn stderr sweep,
  the dropped-text handler, the on_crash hook, and the on_giveup
  hook. Add a new failure mode = append one `CcFailurePattern`.
- **Instruction tools are owner-only (any chat).** Two tools —
  `instruction_read` and `instruction_append` — expose
  `prompts/project.md` (and only that file) to the bot. system.md is
  git-tracked, so it's intentionally not exposed; all owner-driven
  customisations accumulate in project.md, which is concatenated
  after system.md to form the full prompt. No code-level permission
  check exists — the owner-only rule is enforced by the system
  prompt. Code rails that DO enforce: the file path is hardcoded,
  the size cap (128 KiB), atomic write, and a timestamped backup
  before every append. Revert is `mv <backup> prompts/project.md &&
  docker compose restart hamroh`. Edits take effect on the next
  worker start/resume, not mid-turn, which gives the operator a natural
  review window.
- **Skills are operator-curated playbooks.** Markdown files under
  `skills/<name>/SKILL.md` that describe multi-step agent workflows.
  Exposed via `skill_list` / `skill_read`; `skill_write` requires explicit
  owner approval and enforces the same path and format rails. A skill is
  invoked when a `<reminder>` envelope contains `<skill
  name="X">run</skill>` — the system prompt teaches the bot to
  trust `<skill>` tags only inside that envelope, so a user typing
  one in chat does nothing.

If you weaken any of these, the security tests will fail loudly. They
are load-bearing — keep them.

## Manual end-to-end checklist

Once configured, you should be able to:

1. DM the bot, see the bot reply via `telegram_send_message`.
2. Drop `memories/user_preferences.md` containing "Alice prefers
   Russian", ask "what do you know about me?", watch it call
   `memory_list` → `memory_read` and reply in Russian.
3. Send 5 messages in 2 seconds, see them batched into one turn
   (debounce).
4. Send a 6th message *while* it's mid-turn, see it injected.
5. `sqlite3 data/hamroh.db 'SELECT direction, text FROM messages ORDER BY timestamp DESC LIMIT 10;'`
6. Drop `hamroh/tools/echo.py` (above), restart, and watch the bot
   gain the new tool with zero other code changes.
7. Restart the agent container, then confirm `data/session_id` still names the
   persistent Codex thread and the conversation continues.
8. Ask the bot to run a shell command — it should refuse because Codex shell
   features are disabled and the sandbox is read-only by default.
9. Run `uv run python -m pytest tests/test_security_invariants.py` and see the
   security invariants pass.

## When a session breaks

Sometimes the provider rejects a turn outright — for example a policy refusal
or context overflow.
Resuming that session would just replay the rejected content and fail
again, so the engine treats it as broken:

1. It tells the affected chats that the turn failed and that a fresh
   session was started (previous conversation context is gone).
2. It resets the worker to a fresh persistent Codex thread and replaces the ID
   in `data/session_id`, so a later restart cannot resume the broken thread.

The thread ID lives in `data/session_id` and is written atomically when the
thread starts, rather than only at shutdown. To force a fresh thread manually,
send `/reset_session`, or delete that file while the bot is stopped.

Recoverable failures — rate limit, auth, quota — do **not** trigger
this. The session is fine there; a reset would only lose context. The
engine just reports the error and keeps the session for the next turn.


## What `plugins.json` controls

One file, four blocks. Edit and restart to apply. 

Tool groups (shell / code / subagents, off by default), external MCPs, and toggles to hide built-in tools or skills. A missing file boots locked-down; a malformed one crashes boot loudly. 

- **`tool_groups`** — Codex shell, workspace-write, and multi-agent feature
  gates. All are off by default.
- **`mcps`** — external MCP servers (GitHub, Jira, Linear, Notion, your own). One array entry per server, `stdio` / `http` / `sse`, credentials pulled from `.env` via `${VAR}` references — no Python needed.
- **`builtin_tools_disabled`** — hamroh built-ins to hide from the agent (e.g. `telegram_create_poll`).
- **`skills_disabled`** — skill directories under `skills/` to hide.

A missing `plugins.json` boots locked-down (no integrations, no tool groups). A malformed file crashes boot loudly. Full schema, copy-paste examples, and per-MCP setup: [tools.md](tools.md).


```jsonc
{
  "tool_groups": {           // Codex runtime capabilities, all off by default
    "bash":      false,      // shell/unified execution, read-only sandbox
    "code":      false,      // shell/unified execution + workspace-write
    "subagents": false       // Codex multi-agent feature
  },
  "mcps": [                  // external MCP servers — stdio, http, or sse
    {                        //   stdio (local subprocess; auth via env)
      "name": "github",
      "type": "stdio",       //   optional; "stdio" is the default
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env":  { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}" },
      "allowed_tools": ["mcp__github"],
      "enabled": true
    },
    {                        //   http (remote server; auth via static headers)
      "name": "linear",
      "type": "http",
      "url": "https://mcp.linear.app/mcp",
      "headers": { "Authorization": "Bearer ${LINEAR_API_KEY}" },
      "allowed_tools": ["mcp__linear"],
      "enabled": true
    }
    // …Notion, Slack, Postgres, Playwright, your own; sse is a legacy label
  ],
  "builtin_tools_disabled": [ // hamroh built-ins to hide from the agent
    // e.g. "telegram_create_poll", "telegram_stop_poll", "render_html", "render_latex", "telegram_send_photo"
  ],
  "skills_disabled": [       // skill directories under skills/ to hide
    // e.g. "example-skill"
  ]
}
```

- **Tool groups.** Codex shell / workspace-write / multi-agent gates. All off
  by default. Flip to `true` and restart to apply.
- **External MCPs.** `stdio` uses a local command and explicit `env`; `http`
  uses a remote URL and static `headers`; `sse` is translated as remote HTTP
  with a warning. `${VAR}` references pull credentials from `.env`; an
  unresolved entry is skipped. Hamroh does not run interactive OAuth flows.
  Exact allow entries become Codex `enabled_tools`; a server prefix allows
  that server's full surface.
- **Built-in tool toggles.** Names of hamroh built-ins (e.g. `telegram_create_poll`, `render_latex`) you want hidden. Filtered at MCP registration — the agent literally can't see them. A typo crashes boot with the available list.
- **Skill toggles.** Directory names under `skills/` to hide. The skill stays on disk but isn't listed or readable, so it can't be invoked.

## Repo layout

```
hamroh/
├── pyproject.toml
├── README.md
├── docs/
│   ├── README.md               # index of what's in docs/
│   ├── documentation.md        # this file — full technical manual
│   ├── deployment.md           # VPS + CD setup walkthrough
├── Dockerfile
├── docker-compose.yml
├── plugins.json                # operator-edited capability config (gitignored)
├── plugins.json.example        # template for plugins.json
├── access.json                 # DM policy + allowed users/chats (gitignored, hot-reloaded)
├── access.json.example         # template for access.json
├── prompts/
│   ├── system.md               # generic hamroh system prompt (shipped)
│   ├── project.md              # project-specific overlay (gitignored)
│   └── project.md.example      # template for project.md
├── skills/                     # agent skills (playbooks, shipped)
│   ├── README.md               #   directory index + skill-mode notes
│   └── <name>/                 #   deployment-specific skill
│       ├── SKILL.md            #     required playbook or reference
│       └── README.md           #     optional operator documentation
├── memories/                   # the bot's memory (git-tracked, addressed as memories/...; bot reads + writes, bind-mounted in Docker)
│   └── README.md               #   how the memory store works
├── data/                       # gitignored
│   ├── hamroh.db            # SQLite (messages, users, tool_calls, ...)
│   ├── session_id              # persistent Codex thread id
│   ├── attachments/            # inbound photos/docs the dispatcher saves
│   ├── renders/                # outbound PNGs from render_html
│   ├── prompt_backups/         # auto-backups before instruction_append writes
│   ├── logs/                   # rotating structured application log
│   └── codex/                  # local CODEX_HOME (Compose uses a volume)
├── scripts/
│   ├── sync-memories.sh        # rsync helper for server ↔ local sync
│   └── prune-backups.sh        # archive stale prompt backups (keep newest 50)
├── hamroh/
│   ├── __main__.py             # entrypoint: reminder loop + async main
│   ├── startup.py              # boot wiring: stores, MCP, spec, callbacks, teardown
│   ├── access.py               # hot-reloadable access.json gate
│   ├── config.py
│   ├── plugins.py              # plugins.json loader + validation
│   ├── db/{database.py,messages.py,reminders.py,unauthorized.py,migrations/}
│   ├── telegram_io/
│   │   ├── dispatcher.py       # inbound pipeline: gate, rate limit, persist, forward
│   │   ├── commands.py         # owner-only commands (/kill /health /audit ...)
│   │   └── attachments.py      # inbound photo/document ingest
│   ├── engine/
│   │   ├── engine.py           # debouncer, queue, inject, control loop
│   │   ├── typing_indicator.py # "typing..." indicator state + refresh loop
│   │   └── format.py           # inbound batch → <msg> XML with reply chains
│   ├── codex_worker/
│   │   ├── worker.py           # official SDK/app-server lifecycle + events
│   │   └── spec.py             # instructions/schema + SDK spawn spec
│   ├── codex_login.py           # isolated ChatGPT browser-OAuth helper
│   ├── cc_worker/                # optional legacy Claude provider
│   │   ├── worker.py           # subprocess lifecycle + crash recovery + breaker
│   │   ├── event_handlers.py   # stream-json event dispatch
│   │   ├── raw_capture.py      # raw CC stdout/stderr capture files
│   │   ├── spec.py             # spawn spec + locked-down argv assembly
│   │   └── events.py           # TurnResult / CrashLoop dataclasses
│   ├── cc_worker/cc_schema.py   # shared ControlAction JSON schema
│   ├── cc_worker/cc_failure_classifier.py # provider errors → messages
│   ├── mcp_server.py           # FastMCP host + tool auto-discovery
│   ├── storage/
│   │   ├── path_safety.py      # shared traversal-hardened path resolver
│   │   ├── memory.py           # path-hardened markdown store
│   │   ├── attachments.py      # path-hardened read of data/attachments/
│   │   └── render.py           # writable PNG store under data/renders/
│   ├── instructions_store.py   # path-hardened read+append of project.md
│   ├── skills_store.py         # path-hardened read of skills/
│   ├── secrets_scrubber.py     # redacts tokens before persistence
│   ├── input_normalizer.py     # strips Unicode obfuscation at the boundary
│   ├── formatting.py           # markdown → Telegram HTML
│   ├── rate_limiter.py
│   ├── helpers/transcript.py   # [RX]/[TX]/[CC.*] log helpers
│   ├── models.py
│   ├── scripts/
│   │   ├── trace.py            # legacy Claude session renderer
│   │   └── validate_skills.py  # validate skills/ against the Agent Skills spec
│   └── tools/
│       ├── base.py             # BaseTool, ToolContext, Heartbeat
│       ├── now.py
│       ├── telegram_send_message.py
│       ├── telegram_reply_to_message.py
│       ├── telegram_edit_message.py
│       ├── telegram_delete_message.py
│       ├── telegram_add_reaction.py
│       ├── telegram_create_poll.py
│       ├── telegram_stop_poll.py
│       ├── telegram_read_attachment.py  # read a Telegram photo/doc by path under data/attachments/
│       ├── telegram_send_memory_document.py # send a memory file as a Telegram document
│       ├── render_html.py      # HTML → PNG via headless Chromium (network blocked)
│       ├── telegram_send_photo.py       # send a render as an inline Telegram photo
│       ├── memory.py           # list/read/write/append memory (read-before-write)
│       ├── instructions.py     # read/append project.md (owner-only by prompt policy)
│       ├── skills.py           # list/read agent skill playbooks under skills/
│       ├── telegram_create_poll.py      # send poll / quiz
│       ├── telegram_stop_poll.py
│       ├── database_query.py
│       ├── database_get_recent_messages.py
│       └── reminder.py         # set/list/cancel reminders
└── tests/
```
