from __future__ import annotations

import pytest

from hamroh.config import Config


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("HAMROH_OWNER_ID", "1")
    monkeypatch.setenv("HAMROH_PROVIDER", "codex")
    monkeypatch.setenv("HAMROH_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("HAMROH_DATA_DIR", str(tmp_path))
    for name in (
        "HAMROH_CODEX_LAZY_START", "HAMROH_CODEX_IDLE_SECONDS",
        "HAMROH_CODEX_MAX_TURNS",
        "HAMROH_BROWSER_IDLE_SECONDS", "HAMROH_RESOURCE_INTERVAL_SECONDS",
        "HAMROH_RESOURCE_TRACE_HEAP",
    ):
        monkeypatch.delenv(name, raising=False)


def test_resource_defaults_keep_requested_model_and_lazy_runtime(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    cfg = Config.from_env()
    assert cfg.codex_lazy_start is True
    assert cfg.codex_idle_timeout_seconds == 120
    assert cfg.codex_max_turns == 32
    assert cfg.browser_idle_timeout_seconds == 60
    assert cfg.resource_sample_interval_seconds == 300
    assert cfg.resource_trace_heap is False
    assert cfg.model == "gpt-5.6-sol"


def test_idle_opt_out_does_not_disable_any_tools(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HAMROH_CODEX_LAZY_START", "false")
    monkeypatch.setenv("HAMROH_CODEX_IDLE_SECONDS", "0")
    monkeypatch.setenv("HAMROH_BROWSER_IDLE_SECONDS", "0")
    monkeypatch.setenv("HAMROH_RESOURCE_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("HAMROH_RESOURCE_TRACE_HEAP", "true")
    cfg = Config.from_env()
    assert not cfg.codex_lazy_start
    assert cfg.codex_idle_timeout_seconds == cfg.browser_idle_timeout_seconds == 0
    assert cfg.resource_sample_interval_seconds == 0
    assert cfg.resource_trace_heap


@pytest.mark.parametrize("name", [
    "HAMROH_CODEX_IDLE_SECONDS", "HAMROH_BROWSER_IDLE_SECONDS",
    "HAMROH_RESOURCE_INTERVAL_SECONDS",
])
@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_resource_timeouts_reject_invalid_values(monkeypatch, tmp_path, name, value):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        Config.from_env()


def test_negative_turn_retention_budget_is_rejected(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HAMROH_CODEX_MAX_TURNS", "-1")
    with pytest.raises(RuntimeError, match="HAMROH_CODEX_MAX_TURNS"):
        Config.from_env()
