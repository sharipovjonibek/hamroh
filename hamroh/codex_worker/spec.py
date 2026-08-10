"""Configuration and prompt assembly for the Codex SDK worker.

The worker talks to Codex through the official ``openai-codex`` Python SDK.
This module deliberately contains no process-launching code; it only validates
the files and immutable settings that are handed to the SDK.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping


SandboxName = Literal["read-only", "workspace-write"]


@dataclass(frozen=True)
class CodexSpawnSpec:
    """Everything needed to create or resume one persistent Codex thread.

    Security-sensitive defaults are intentionally narrow: filesystem access is
    read-only and approval requests are denied by :class:`CodexWorker`. A
    deployment that needs the code-writing tools may explicitly select
    ``workspace-write``; unrestricted host access is not representable here.

    ``codex_config`` is passed to Codex's typed ``thread/start`` or
    ``thread/resume`` request. It is the integration point for the local
    Hamroh MCP server and optional external MCP plugins without reading or
    mutating a developer's general Codex configuration.
    """

    system_prompt_path: Path
    json_schema_path: Path
    cwd: Path
    model: str | None = None
    effort: str = "high"
    session_id: str | None = None
    codex_bin: str | None = None
    codex_home: Path | None = None
    project_prompt_path: Path | None = None
    agent_name: str = "Assistant"
    sandbox: SandboxName = "read-only"
    codex_config: Mapping[str, Any] = field(default_factory=dict)
    skills_index: str = ""
    memory_index: str = ""
    hamroh_tool_names: tuple[str, ...] = ()
    enable_subagents: bool = False
    subagents_prompt_path: Path | None = None


def load_output_schema(spec: CodexSpawnSpec) -> dict[str, Any]:
    """Load and minimally validate the per-turn structured-output schema."""

    if not spec.json_schema_path.is_file():
        raise FileNotFoundError(spec.json_schema_path)
    payload = json.loads(spec.json_schema_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Codex output schema must be a JSON object")
    return payload


def _render_tools_index(spec: CodexSpawnSpec) -> str:
    if not spec.hamroh_tool_names:
        return ""
    tools = "\n".join(
        f"- `mcp__hamroh__{name}`" for name in sorted(spec.hamroh_tool_names)
    )
    return (
        "# Your Telegram tools\n\n"
        "Call each tool by its exact name below. These tools are the only way "
        "to deliver a Telegram response; ordinary assistant text is not shown "
        "to the user.\n\n"
        f"{tools}\n"
    )


def compose_developer_instructions(spec: CodexSpawnSpec) -> str:
    """Build the developer instructions supplied when the thread is opened."""

    if not spec.system_prompt_path.is_file():
        raise FileNotFoundError(spec.system_prompt_path)
    if not spec.cwd.is_dir():
        raise FileNotFoundError(spec.cwd)

    prompt = spec.system_prompt_path.read_text(encoding="utf-8")
    if spec.project_prompt_path is not None and spec.project_prompt_path.exists():
        prompt += "\n\n" + spec.project_prompt_path.read_text(encoding="utf-8")

    prompt += (
        "\n\n# Configured identity\n\n"
        f"Your name is {json.dumps(spec.agent_name, ensure_ascii=False)}. "
        "This value comes from the AGENT_NAME deployment setting and is "
        "authoritative. Use it whenever you identify yourself, even if a "
        "project prompt or persisted instruction contains an older name."
    )

    model_name = spec.model or "Codex recommended subscription default"
    prompt += (
        "\n\n# Runtime\n\n"
        "You are running through the official OpenAI Codex SDK with:\n"
        f"- model: `{model_name}`\n"
        f"- reasoning effort: `{spec.effort}`\n"
        f"- sandbox: `{spec.sandbox}`\n\n"
        "If asked, report these public runtime values accurately."
    )
    if spec.skills_index:
        prompt += "\n\n" + spec.skills_index
    if spec.memory_index:
        prompt += "\n\n" + spec.memory_index
    tools_index = _render_tools_index(spec)
    if tools_index:
        prompt += "\n\n" + tools_index

    if spec.enable_subagents:
        path = spec.subagents_prompt_path
        if path is None or not path.is_file():
            raise FileNotFoundError(
                f"enable_subagents=True but subagents_prompt_path is missing: {path!r}"
            )
        prompt += "\n\n" + path.read_text(encoding="utf-8")
    return prompt
