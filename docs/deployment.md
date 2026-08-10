# Deployment Guide

This guide covers deploying hamroh to a VPS (Contabo, Hetzner,
DigitalOcean, etc.) using Docker, and setting up a continuous deployment
workflow.

## Prerequisites

- A VPS with SSH access
- A GitHub repo with your hamroh code
- A Telegram bot token (from @BotFather)
- A ChatGPT account whose plan includes Codex
- A browser for the one-time ChatGPT login, with access to the host's local
  OAuth callback on port `1455`

## Initial server setup (one-time)

```bash
# SSH into your server and forward the browser OAuth callback to your computer
ssh -L 1455:localhost:1455 root@your-server-ip

# Install Docker
curl -fsSL https://get.docker.com | sh

# Clone your private repo (SSH auth — add server's public key to GitHub first)
#   On server: ssh-keygen -t ed25519 (if no key exists)
#   Copy ~/.ssh/id_ed25519.pub → GitHub Settings → SSH keys
git clone git@github.com:your-user/hamroh.git ~/hamroh
cd ~/hamroh

# Configure
cp .env.example .env
chmod 600 .env
vim .env   # set AGENT_NAME, TELEGRAM_BOT_TOKEN, and HAMROH_OWNER_ID
cp prompts/project.md.example prompts/project.md
vim prompts/project.md   # customize persona, integrations, and team info

# Build, then authenticate this bot's private Codex home once.
# The command prints an authorization URL; open it on your computer.
make auth

# Start
make up

# Verify it's running
docker compose ps
docker compose logs -f   # should see "hamroh is live"
```

DM your bot on Telegram to confirm it replies.

`make auth` runs a short-lived `codex-auth` container, prints a ChatGPT browser
authorization URL, and waits for the OAuth redirect on `localhost:1455`. The
SSH port forwarding above routes that callback from your computer to the
server. For a local desktop deployment, no SSH tunnel is needed: open the URL
in a browser on the same computer.

The auth container deliberately receives only `AGENT_NAME` and `CODEX_HOME`,
so the login helper cannot see the Telegram token or external MCP credentials.
The resulting
ChatGPT OAuth cache is stored in the named `codex-home` Docker volume and
mounted at `/var/lib/codex` as `CODEX_HOME` in the agent. Do not mount a
developer's general `~/.codex` directory: it mixes the bot's identity and
conversation state with an interactive Codex setup. Check the isolated login
at any time with `make auth-status`.

Device-code login is not the default. If a remote or headless environment
cannot forward the localhost callback, request that fallback explicitly:

```bash
docker compose run --rm codex-auth python -m hamroh.codex_login --device-code
```

### Enabling capabilities

The bot ships with a tight default surface — Telegram messaging,
memory tools, reminders, live search, and the path-restricted browser. Shell,
code-editing, subagents, and any external MCPs are **all off by
default**.

Toggles live in `plugins.json` at the repo root. Copy the shipped
template once on first setup:

```bash
cp plugins.json.example plugins.json
```

Then edit:

```jsonc
{
  "tool_groups": { "bash": true, "code": true, "subagents": false },
  "mcps":  [ /* sample Jira / GitLab / GitHub entries — keep, edit, or delete */ ],
  "skills_disabled": [],
  "builtin_tools_disabled": []
}
```

`plugins.json` is gitignored, so different deployments can carry
different toggles without fighting over the file. External MCPs are
declared in `plugins.json` but their credentials live in `.env`,
referenced as `${VAR}`. An MCP whose `${VAR}` references aren't
satisfied is silently skipped at boot. The shipped example carries
sample Jira / GitLab / GitHub entries to copy from — they're not
first-class, just convenient starting points.

For the per-tool list, the schema, "How to add a new MCP", and how
to disable individual built-in tools (e.g. `telegram_create_poll`,
`render_latex`) or skills, see [tools.md](tools.md). Restart the
container after editing either file: `docker compose up -d
--force-recreate`.

## Update workflow

### Manual (SSH)

Every time you push changes to GitHub:

```bash
ssh root@your-server-ip 'cd ~/hamroh && ./scripts/commit-and-push.sh && git pull && make up'
```

Or step by step:

```bash
ssh root@your-server-ip
cd ~/hamroh
./scripts/commit-and-push.sh   # commit the bot's memories so pull isn't blocked
git pull
make up
docker compose logs -f   # verify it started correctly
```

`commit-and-push.sh` commits and pushes anything the bot wrote to
`memories/` since the last deploy. Without it, an uncommitted memory
file that also changed upstream makes `git pull` abort.

### Automatic (GitHub Actions)

Create `.github/workflows/deploy.yml` in your repo:

```yaml
name: Deploy
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.SERVER_IP }}
          username: root
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            cd ~/hamroh
            ./scripts/commit-and-push.sh
            git pull
            make up
```

Then add these secrets to your GitHub repo (Settings → Secrets and
variables → Actions):

| Secret | Value |
|--------|-------|
| `SERVER_IP` | Your VPS IP address |
| `SSH_PRIVATE_KEY` | Contents of `~/.ssh/id_ed25519` (generate with `ssh-keygen -t ed25519` and add the public key to the server's `~/.ssh/authorized_keys`) |

Every push to `main` will automatically deploy to your server.

The workflow reuses the existing `codex-home` volume. Run `make auth` once on
the server before enabling automatic deployment, and run it again only if
`make auth-status` reports that the ChatGPT login is no longer valid. Browser
login is intentionally not automated or stored in GitHub Actions secrets.

**Note:** Since the repo is private, the server needs SSH access to
GitHub for `git pull` to work. Make sure the server's SSH key
(`~/.ssh/id_ed25519.pub`) is added as either:

- A **deploy key** on the repo (Settings → Deploy keys) — scoped to
  this repo only, recommended
- Or an **SSH key** on your GitHub account (Settings → SSH keys) —
  grants access to all your repos

## The `data/` directory

The `data/` directory is created automatically on first run. It contains
only ephemeral, server-local state — memory no longer lives here. The bot's
memory is the git-tracked `memories/` folder at the repo root (bind-mounted
into the container as `./memories:/app/memories`), so it travels with the
repo and needs no migration. `data/` contains:

- `data/hamroh.db` — SQLite database (messages, users, reminders,
  tool call logs) — starts fresh on new servers
- `data/session_id` — persistent Codex thread ID for conversation continuity
- `data/attachments/` — inbound photos/docs the dispatcher saved
- `data/renders/` — outbound PNGs from `render_html`
- `data/logs/` — rotating application logs

Codex authentication is not under `data/`: it lives in the private
`codex-home` Docker volume mounted at `CODEX_HOME=/var/lib/codex`.

Headless Chromium for `render_html` is pre-installed in the Docker
image (`playwright install --with-deps chromium`) — no per-host
provisioning step needed.

**First deployment:** nothing to do — the bot creates everything.

**Migrating from another server:** nothing in `data/` is worth copying. The
bot's memory lives in the git-tracked `memories/` folder, so a fresh
`git clone` (or `git pull`) brings every note with it. Don't copy `session_id`
or `hamroh.db` to a new server — the database rebuilds naturally from new
messages and a thread ID is meaningful only with its original Codex state.
Authenticate the destination's fresh `codex-home` volume with `make auth`.

## Syncing memories and config

Memories travel with the repo: the `memories/` folder is tracked in git, so
`git pull` / `git push` move it like any other code. Every deploy commits the
bot's latest notes automatically via `./scripts/commit-and-push.sh`; run it by
hand (or via cron) anytime you want the server's memories pushed sooner.

If you and the bot edit the same memory file, git merges by keeping both
sides' lines (`merge=union` in `.gitattributes`) — no conflict markers, no
manual resolution. Skim the merged file if you both touched the same lines.

For gitignored config that only lives on the server — such as `project.md` —
use the included sync script:

```bash
# Push updated project.md to the server
./scripts/sync-memories.sh push root@your-server-ip
```

After pushing `project.md`, restart for changes to take effect:

```bash
ssh root@your-server-ip 'cd ~/hamroh && docker compose restart'
```

## Common operations

```bash
# View live logs
ssh root@your-server-ip 'cd ~/hamroh && docker compose logs -f'

# Shell into the container
ssh root@your-server-ip 'cd ~/hamroh && docker compose exec hamroh bash'

# Restart without rebuilding
ssh root@your-server-ip 'cd ~/hamroh && docker compose restart'

# Stop the bot
ssh root@your-server-ip 'cd ~/hamroh && docker compose down'

# Check status
ssh root@your-server-ip 'cd ~/hamroh && docker compose ps'
```

## Troubleshooting

### Telegram conflict error

```
Conflict: terminated by other getUpdates request
```

Another instance is polling the same bot token. Make sure only one is
running — check both local (`pkill -f 'python -m hamroh'`) and
Docker (`docker compose down`).

### Codex runtime cannot start or repeatedly reconnects

Common causes:

- **Login missing or expired** — run `make auth-status`, then `make auth` if
  needed. Authentication must exist in the same `codex-home` volume the agent
  mounts.
- **Stale thread ID** — normally Hamroh detects this and creates a fresh
  thread. If startup cannot recover, stop the agent, delete `data/session_id`,
  and run `make up`.
- **Hamroh MCP unavailable** — the local `hamroh` MCP is marked `required` in
  Codex's per-thread config. Inspect `docker compose logs hamroh` for its URL
  and startup error. Optional external MCPs do not make the core server
  optional.
- **External stdio MCP command missing** — commands such as `npx` must exist
  in the image when that plugin is enabled.

### Codex/ChatGPT authentication failed

There is no OpenAI API key or OAuth token to paste into `.env`. Authenticate
the dedicated volume through the helper:

```bash
ssh -L 1455:localhost:1455 root@your-server-ip
cd ~/hamroh
make auth-status
make auth                   # prints a URL; finish sign-in in your browser
make up
```

Keep that SSH session open until the browser returns to `localhost:1455` and
`make auth` reports that login completed.

The official `openai-codex` SDK launches its pinned Codex app-server. The
worker gives that process a minimal environment, a private `CODEX_HOME`, and
no ambient Telegram/plugin secrets. External MCP credentials are passed only
inside the explicitly configured server entry. A ChatGPT subscription
authorizes Codex usage; it does not create general OpenAI API credits for
unrelated API clients.

### Legacy Claude provider

`HAMROH_PROVIDER=claude` keeps the former `cc_worker` path available for
compatibility. It is not the Docker default: the image no longer installs the
Claude CLI or mounts Claude credentials. A legacy deployment must provide its
own compatible `claude` binary/authentication, set `HAMROH_MODEL`, and accept
that the Claude-specific session/logging behavior documented in older
revisions differs from the Codex worker.
