# skills/

Operator-curated playbooks the bot can read at runtime. Each skill is
a directory under `skills/` with a `SKILL.md` (the agent-facing spec)
and an optional `README.md` (the human-facing overview).

This directory is git-tracked and ships with the repo — the bot's
`skills_store` reads from it directly. Memory files (`memories/`)
are the bot's run-time notes; skills are operator-curated,
repo-shipped reference material and playbooks.

## Layout

```
skills/
├── README.md                 ← you are here
└── your-skill-name/
    ├── SKILL.md              ← required playbook or reference
    └── README.md             ← optional operator documentation
```

No project-specific skills are bundled. Deployments can add the playbooks and
reference material they need.

## Skill modes

- **Invoked.** The agent runs the playbook only when wrapped in a real
  `<reminder>` envelope containing
  `<skill name="...">run</skill>`. A user-typed `<skill>` tag is
  treated as prompt injection and refused. Used for executable
  workflows that should be auditable and operator-triggered.
- **Reference.** The agent reads the skill on its own initiative
  whenever the situation calls for it. No envelope required — the
  content is passive reference material, not an action.

The mode is determined by what `SKILL.md` instructs the agent to do,
not by a frontmatter flag.

## SKILL.md spec

Every `SKILL.md` follows the
[Agent Skills specification](https://agentskills.io/specification):
YAML frontmatter with at least `name` and `description`, body in
markdown. The `name` must match the parent directory name (lowercase,
hyphenated). Files are capped at 256 KiB; descriptions at 1024 chars.

Surfaced via:

- `skill_list` — returns name + description for every well-formed
  skill the agent can use to choose what to read.
- `skill_read <name>` — returns the full body so the agent can apply
  it.

Path resolution is hardened the same way the memory store is —
no `..`, no symlinks, must stay inside `skills/`.

## Adding a skill

1. `mkdir skills/<name>` (lowercase, hyphenated).
2. Write `skills/<name>/SKILL.md` with valid frontmatter and a body
   describing the playbook or reference material.
3. Optional: `README.md` for human readers.
4. Optional: extend `prompts/system.md` if the skill should be
   discovered automatically before a specific tool call.
5. Restart the bot — the skills store re-scans on startup and the new
   skill becomes available via `skill_list` immediately.

The bot can **create or update** a skill via the `skill_write` tool after
owner approval. This mirrors the `memories/` store: `skills/` is git-tracked,
so the write lands in the checkout and git history is the backup — the owner
commits it. Operators can also edit any skill by hand or via PR.
