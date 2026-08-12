"""Startup-policy tests for the Codex provider integration."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

import hamroh.startup as startup
from hamroh.config import Config, agent_name_slug
from hamroh.plugins import McpPluginSpec, Plugins
from hamroh.storage.generated_image_store import GeneratedImageStore


def _required_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-telegram-token")
    monkeypatch.setenv("HAMROH_OWNER_ID", "12345")
    monkeypatch.setenv("HAMROH_DATA_DIR", str(tmp_path / "data"))


def test_from_env_defaults_to_codex_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _required_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HAMROH_PROVIDER", raising=False)
    monkeypatch.delenv("HAMROH_MODEL", raising=False)
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)

    config = Config.from_env()

    assert config.provider == "codex"
    assert config.model == ""
    assert config.effort == "high"
    assert config.codex_bin is None
    assert config.codex_home == (tmp_path / "data" / "codex").resolve()
    assert (
        config.generated_images_dir
        == (tmp_path / "data" / "codex" / "generated_images").resolve()
    )
    assert config.agent_name == "Assistant"


def test_from_env_applies_explicit_codex_runtime_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HAMROH_PROVIDER", "CODEX")
    monkeypatch.setenv("HAMROH_MODEL", "gpt-test-codex")
    monkeypatch.setenv("HAMROH_EFFORT", "medium")
    monkeypatch.setenv("CODEX_BIN", "/opt/codex/bin/codex")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "private-codex-home"))
    monkeypatch.setenv("AGENT_NAME", "Dono Status")

    config = Config.from_env()

    assert config.provider == "codex"
    assert config.model == "gpt-test-codex"
    assert config.effort == "medium"
    assert config.codex_bin == "/opt/codex/bin/codex"
    assert config.codex_home == (tmp_path / "private-codex-home").resolve()
    assert (
        config.generated_images_dir
        == (tmp_path / "private-codex-home" / "generated_images").resolve()
    )
    assert config.agent_name == "Dono Status"
    assert agent_name_slug(config.agent_name) == "dono-status"


@pytest.mark.asyncio
async def test_startup_wires_generated_image_store_into_tool_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config.for_test(tmp_path)
    plugins = Plugins()
    stores = startup._build_stores(config, SimpleNamespace(), plugins)
    captured: dict[str, object] = {}

    class FakeMcpServer:
        def __init__(self, ctx, **_kwargs) -> None:
            captured["ctx"] = ctx
            self.url = "http://127.0.0.1:8765/mcp"

        async def start(self) -> None:
            return None

    monkeypatch.setattr(startup, "McpServer", FakeMcpServer)
    app = SimpleNamespace(db=SimpleNamespace(), config=config)

    ctx, _mcp = await startup._start_mcp_server(app, stores, plugins, {})

    assert isinstance(stores.generated_images, GeneratedImageStore)
    assert stores.generated_images.root == config.generated_images_dir
    assert ctx.generated_image_store is stores.generated_images
    assert captured["ctx"] is ctx


@pytest.mark.parametrize("value", ["   ", "line one\nline two", "x" * 81])
def test_from_env_rejects_unsafe_agent_names(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_NAME", value)

    with pytest.raises(RuntimeError, match="AGENT_NAME"):
        Config.from_env()


def test_from_env_requires_model_only_for_legacy_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HAMROH_PROVIDER", "claude")
    monkeypatch.delenv("HAMROH_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="HAMROH_MODEL is required"):
        Config.from_env()

    monkeypatch.setenv("HAMROH_MODEL", "claude-test-model")
    config = Config.from_env()

    assert config.provider == "claude"
    assert config.model == "claude-test-model"


def _fake_mcp() -> SimpleNamespace:
    return SimpleNamespace(
        url="http://127.0.0.1:8765/mcp",
        tools=[
            SimpleNamespace(name="telegram_send_message"),
            SimpleNamespace(name="memory_read"),
        ],
    )


def test_build_codex_config_locks_down_features_and_maps_mcp_allowlists() -> None:
    plugins = Plugins(
        mcps=(
            McpPluginSpec(
                name="github",
                type="stdio",
                allowed_tools=(
                    "mcp__github__search_issues",
                    "mcp__github__get_issue",
                    "mcp__github__search_issues",
                ),
                command="github-mcp",
                args=("stdio", "--read-only"),
                env={"GITHUB_TOKEN": "test-token"},
            ),
            McpPluginSpec(
                name="knowledge",
                type="http",
                allowed_tools=("mcp__knowledge",),
                url="https://mcp.example.test/api",
                headers={"Authorization": "Bearer test-token"},
            ),
        )
    )

    config = startup._build_codex_config(plugins, _fake_mcp())

    assert config["features"] == {
        "shell_tool": False,
        "unified_exec": False,
        "shell_snapshot": False,
        "multi_agent": False,
        "apps": False,
        "remote_plugin": False,
        "hooks": False,
        "goals": False,
        "memories": False,
        "personality": False,
    }
    assert config["web_search"] == "live"

    hamroh = config["mcp_servers"]["hamroh"]
    assert hamroh["url"] == "http://127.0.0.1:8765/mcp"
    assert hamroh["required"] is True
    assert hamroh["enabled"] is True
    assert hamroh["enabled_tools"] == ["memory_read", "telegram_send_message"]
    assert hamroh["default_tools_approval_mode"] == "approve"

    github = config["mcp_servers"]["github"]
    assert github["command"] == "github-mcp"
    assert github["args"] == ["stdio", "--read-only"]
    assert github["env"] == {"GITHUB_TOKEN": "test-token"}
    assert github["enabled_tools"] == ["get_issue", "search_issues"]
    assert github["required"] is False

    knowledge = config["mcp_servers"]["knowledge"]
    assert knowledge["url"] == "https://mcp.example.test/api"
    assert knowledge["http_headers"] == {"Authorization": "Bearer test-token"}
    assert "enabled_tools" not in knowledge


def test_build_codex_config_enables_only_requested_native_capabilities() -> None:
    plugins = Plugins(tool_groups={"bash": False, "code": True, "subagents": True})

    features = startup._build_codex_config(plugins, _fake_mcp())["features"]

    assert features["shell_tool"] is True
    assert features["unified_exec"] is True
    assert features["multi_agent"] is True
    assert features["apps"] is False
    assert features["remote_plugin"] is False


def test_build_codex_config_rejects_cross_server_tool_allowlist() -> None:
    plugins = Plugins(
        mcps=(
            McpPluginSpec(
                name="github",
                type="stdio",
                allowed_tools=("mcp__other__search",),
                command="github-mcp",
            ),
        )
    )

    with pytest.raises(ValueError, match="invalid allowed tool"):
        startup._build_codex_config(plugins, _fake_mcp())


def test_build_worker_spec_selects_configured_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config.for_test(tmp_path)
    codex_marker = object()
    claude_marker = object()
    calls: list[str] = []

    def fake_codex(*_args: object) -> object:
        calls.append("codex")
        return codex_marker

    def fake_claude(*_args: object) -> object:
        calls.append("claude")
        return claude_marker

    monkeypatch.setattr(startup, "_build_codex_spec", fake_codex)
    monkeypatch.setattr(startup, "_build_cc_spec", fake_claude)
    dependencies = (Plugins(), object(), object())

    codex_config = dataclasses.replace(config, provider="codex", model="")
    assert startup._build_worker_spec(codex_config, *dependencies) is codex_marker
    assert startup._build_worker_spec(config, *dependencies) is claude_marker
    assert calls == ["codex", "claude"]
