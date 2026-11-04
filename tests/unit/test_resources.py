"""Resource measurements use current counters and never reveal process arguments."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from hamroh.helpers import resources


def make_process(
    root: Path, pid: int, ppid: int, *, name="python", rss=123, ticks=10,
    born=100, state="S", command=b"python\0--secret=DO_NOT_LOG\0",
) -> Path:
    directory = root / str(pid)
    directory.mkdir(exist_ok=True)
    fields = ["0"] * 24
    fields[0], fields[1] = state, str(ppid)
    fields[11], fields[12], fields[19] = str(ticks), "0", str(born)
    (directory / "stat").write_text(f"{pid} ({name}) " + " ".join(fields))
    (directory / "status").write_text(
        f"VmRSS:\t{rss} kB\nRssAnon:\t50 kB\nVmData:\t200 kB\nThreads:\t3\n"
    )
    (directory / "cmdline").write_bytes(command)
    return directory


def test_process_tree_includes_descendants_and_excludes_unrelated(tmp_path):
    make_process(tmp_path, 10, 1, name="python (bot)")
    make_process(tmp_path, 20, 10, name="codex", rss=200, command=b"/bin/codex\0app-server\0")
    make_process(tmp_path, 30, 20, name="node", rss=40, command=b"node\0/playwright/cli.js\0")
    make_process(tmp_path, 40, 1, name="other")
    sample = resources.ResourceSampler(10, proc=tmp_path).snapshot("boot")
    assert [p["pid"] for p in sample["processes"]] == [10, 20, 30]
    assert sample["rss_bytes"] == 123 * 1024
    assert sample["tree_rss_bytes"] == (123 + 200 + 40) * 1024
    assert sample["child_count"] == 2
    assert [p["kind"] for p in sample["processes"]] == ["python", "codex", "playwright"]
    assert "DO_NOT_LOG" not in json.dumps(sample)
    assert "command" not in sample["processes"][0]


def test_cpu_uses_elapsed_time_and_pid_start_identity(tmp_path, monkeypatch):
    make_process(tmp_path, 10, 1, ticks=10)
    times = iter([1.0, 3.0, 4.0])
    monkeypatch.setattr(resources.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(resources.os, "sysconf", lambda _name: 100)
    sampler = resources.ResourceSampler(10, proc=tmp_path)
    assert sampler.snapshot("first")["processes"][0]["cpu_percent"] is None
    make_process(tmp_path, 10, 1, ticks=110)
    assert sampler.snapshot("second")["processes"][0]["cpu_percent"] == 50
    make_process(tmp_path, 10, 1, ticks=900, born=200)
    assert sampler.snapshot("reused")["processes"][0]["cpu_percent"] is None
    assert len(sampler._previous) == 1


def test_zombies_and_orphan_candidates_are_visible(tmp_path):
    make_process(tmp_path, 10, 1)
    make_process(tmp_path, 20, 10, state="Z", name="codex", rss=0)
    make_process(tmp_path, 30, 1, name="chrome", command=b"chrome\0")
    sample = resources.ResourceSampler(10, proc=tmp_path).snapshot("recovery")
    assert sample["zombie_count"] == 1
    assert [p["pid"] for p in sample["orphan_candidates"]] == [30]


def test_cgroup_charge_is_separate_from_process_rss(tmp_path):
    (tmp_path / "memory.current").write_text("1000000")
    (tmp_path / "memory.max").write_text("max")
    (tmp_path / "memory.stat").write_text("anon 500000\nfile 400000\nkernel 100000\n")
    assert resources.cgroup_memory(tmp_path) == {
        "current_bytes": 1000000, "limit_bytes": None,
        "anon_bytes": 500000, "file_bytes": 400000, "kernel_bytes": 100000,
    }


def test_vanishing_processes_and_unavailable_proc_are_handled(tmp_path):
    (tmp_path / "10").mkdir()
    assert resources.read_process(10, tmp_path) is None
    assert resources.process_tree(10, tmp_path / "missing") == []
    assert resources.ResourceSampler(10, proc=tmp_path).snapshot("gone")["supported"] is False


def test_python_diagnostic_source_is_not_misclassified_as_codex(tmp_path):
    make_process(tmp_path, 10, 1, command=b"python\0-c\0codex playwright chromium\0")
    assert resources.read_process(10, tmp_path)["kind"] == "python"


def test_summary_keeps_boot_and_turn_samples_separate(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"event": "boot", "rss_bytes": 10},
        {"event": "after_turn", "rss_bytes": 20},
        {"event": "after_turn", "rss_bytes": 25},
    ]))
    summary = resources.summarize(path)
    assert summary["boot"]["rss_bytes"]["median"] == 10
    assert summary["after_turn"]["rss_bytes"]["delta"] == 5
    assert summary["after_turn"]["rss_bytes"]["median"] == 22.5


def test_monitor_samples_periodically_and_stops_its_task(caplog):
    async def run():
        monitor = resources.ResourceMonitor(lambda: {"worker": {"state": "standby"}}, interval=0.001)
        seen = asyncio.Event()
        original = monitor.record

        def record(event):
            original(event)
            seen.set()

        monitor.record = record
        monitor.start()
        await asyncio.wait_for(seen.wait(), timeout=1)
        await monitor.stop()
        assert monitor._task is None

    with caplog.at_level(logging.INFO, logger="hamroh.resources"):
        asyncio.run(run())
    assert "resource_snapshot" in caplog.text
    assert '"state":"standby"' in caplog.text


def test_cli_reads_instance_pid_without_loading_config(tmp_path, monkeypatch, capsys):
    (tmp_path / ".lock").write_text("42")
    monkeypatch.setenv("HAMROH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(resources.ResourceSampler, "snapshot", lambda self, event: {
        "pid": self.pid, "event": event, "supported": True,
    })
    resources.main(["--label", "baseline"])
    assert json.loads(capsys.readouterr().out) == {
        "pid": 42, "event": "baseline", "supported": True,
    }


@pytest.mark.parametrize("args", [["--pid", "0"], ["--count", "0"], ["--interval", "0"]])
def test_cli_rejects_invalid_sampling_settings(args):
    with pytest.raises(SystemExit):
        resources.main(args)
