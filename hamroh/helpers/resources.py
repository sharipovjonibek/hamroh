"""Small Linux /proc and cgroup snapshots without psutil or command-line secrets."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import statistics
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("hamroh.resources")


def _kind(name: str, command: bytes) -> str:
    # Classify the executable first. A Python "-c" diagnostic can itself
    # contain the strings "codex" and "chromium" in its source argument.
    if "python" in name.lower():
        return "python"
    if "chrom" in name.lower():
        return "chromium"
    if "codex" in name.lower():
        return "codex"
    if name.lower() in {"node", "npm", "npx"}:
        if b"playwright" in command.lower():
            return "playwright"
        return "mcp_node"
    return "other"


def read_process(pid: int, proc: Path = Path("/proc")) -> dict | None:
    """Read current RSS and CPU counters, never returning raw argv or env."""
    try:
        directory = proc / str(pid)
        raw = (directory / "stat").read_text()
        start, end = raw.index("("), raw.rindex(")")
        name = raw[start + 1:end]
        fields = raw[end + 2:].split()
        status = {}
        for line in (directory / "status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"VmRSS", "RssAnon", "VmData", "Threads"}:
                status[key] = int(value.split()[0])
        with (directory / "cmdline").open("rb") as command_file:
            command = command_file.read(4096)
        return {
            "pid": pid, "ppid": int(fields[1]), "state": fields[0],
            "kind": _kind(name, command),
            "rss_bytes": status.get("VmRSS", 0) * 1024,
            "anon_bytes": status.get("RssAnon", 0) * 1024,
            "data_bytes": status.get("VmData", 0) * 1024,
            "threads": status.get("Threads"),
            "cpu_ticks": int(fields[11]) + int(fields[12]),
            "start_ticks": int(fields[19]),
        }
    except (OSError, ValueError, IndexError):
        # Children can exit between enumeration and reading these files.
        return None


def read_processes(proc: Path = Path("/proc")) -> dict[int, dict]:
    try:
        return {
            int(p.name): row for p in proc.iterdir() if p.name.isdecimal()
            if (row := read_process(int(p.name), proc)) is not None
        }
    except OSError:
        return {}


def process_tree(
    pid: int, proc: Path = Path("/proc"), *, processes: dict[int, dict] | None = None,
) -> list[dict]:
    if processes is None:
        processes = read_processes(proc)
    selected = {pid}
    while True:
        children = {p["pid"] for p in processes.values() if p["ppid"] in selected}
        if children <= selected:
            break
        selected |= children
    return [processes[p] for p in sorted(selected) if p in processes]


def cgroup_memory(root: Path = Path("/sys/fs/cgroup")) -> dict:
    """Report cgroup charge separately: summed RSS double-counts shared pages."""
    result = {}
    for key, filename in (
        ("current_bytes", "memory.current"),
        ("peak_bytes", "memory.peak"),
        ("limit_bytes", "memory.max"),
    ):
        try:
            value = (root / filename).read_text().strip()
            result[key] = None if value == "max" else int(value)
        except (OSError, ValueError):
            pass
    try:
        values = dict(line.split() for line in (root / "memory.stat").read_text().splitlines())
        for key in ("anon", "file", "kernel", "sock"):
            if key in values:
                result[key + "_bytes"] = int(values[key])
    except (OSError, ValueError):
        pass
    return result


class ResourceSampler:
    """Keep only the previous live process counters for interval CPU usage."""

    def __init__(self, pid: int | None = None, *, proc: Path = Path("/proc")):
        self.pid = os.getpid() if pid is None else pid
        self.proc = proc
        self._previous: dict[tuple[int, int], int] = {}
        self._sampled_at: float | None = None

    def snapshot(self, event: str, *, state: dict | None = None) -> dict:
        now = time.monotonic()
        processes = read_processes(self.proc)
        rows = process_tree(self.pid, self.proc, processes=processes)
        elapsed = now - self._sampled_at if self._sampled_at is not None else 0
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        current = {}
        for row in rows:
            identity = (row["pid"], row["start_ticks"])
            ticks = row["cpu_ticks"]
            before = self._previous.get(identity)
            row["cpu_percent"] = (
                round(100 * max(0, ticks - before) / ticks_per_second / elapsed, 2)
                if elapsed > 0 and before is not None else None
            )
            current[identity] = ticks
        self._previous, self._sampled_at = current, now
        root = next((p for p in rows if p["pid"] == self.pid), None)
        heap = None
        if self.pid == os.getpid() and tracemalloc.is_tracing():
            heap = tracemalloc.get_traced_memory()[0]
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event, "pid": self.pid,
            "supported": root is not None, "rss_bytes": root["rss_bytes"] if root else None,
            "tree_rss_bytes": sum(p["rss_bytes"] for p in rows) if rows else None,
            "child_count": max(0, len(rows) - 1),
            "zombie_count": sum(p["state"] == "Z" for p in rows),
            "orphan_candidates": [
                p for p in processes.values() if p["ppid"] == 1 and p["pid"] != self.pid
                and p["kind"] in {"codex", "chromium", "playwright", "mcp_node"}
                and p["pid"] not in {row["pid"] for row in rows}
            ],
            "python_heap_bytes": heap,
            "python_allocated_blocks": (
                sys.getallocatedblocks() if self.pid == os.getpid()
                and hasattr(sys, "getallocatedblocks") else None
            ),
            "processes": rows, "cgroup": cgroup_memory(),
            "runtime": state or {},
        }


def summarize(path: Path) -> dict:
    """Summarize actual JSONL samples, keeping boot/turn/idle phases distinct."""
    phases: dict[str, dict[str, list]] = {}
    with path.open() as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            sample = json.loads(line)
            values = phases.setdefault(sample["event"], {})
            for name in ("rss_bytes", "tree_rss_bytes", "child_count", "python_heap_bytes"):
                if isinstance(sample.get(name), (int, float)):
                    values.setdefault(name, []).append(sample[name])
            if isinstance(sample.get("cgroup", {}).get("current_bytes"), int):
                values.setdefault("cgroup_bytes", []).append(sample["cgroup"]["current_bytes"])
    return {
        phase: {
            field: {"samples": len(values), "first": values[0], "last": values[-1],
                    "min": min(values), "max": max(values),
                    "median": statistics.median(values), "delta": values[-1] - values[0]}
            for field, values in fields.items()
        } for phase, fields in phases.items()
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="default: bot PID from its instance lock")
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--label", default="manual")
    parser.add_argument("--summary", type=Path, help="summarize previously captured JSONL")
    args = parser.parse_args(argv)
    if args.summary is not None:
        print(json.dumps(summarize(args.summary), indent=2))
        return
    if args.count < 1 or not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("count and interval must be positive")
    pid = args.pid
    if pid is None:
        root = Path(os.environ.get("HAMROH_DATA_DIR", "/data/hamroh"))
        try:
            pid = int((root / ".lock").read_text().strip())
        except (OSError, ValueError):
            parser.error("cannot read bot PID; supply --pid or set HAMROH_DATA_DIR")
    if pid <= 0:
        parser.error("PID must be positive")
    sampler = ResourceSampler(pid)
    for number in range(args.count):
        if number:
            time.sleep(args.interval)
        sample = sampler.snapshot(args.label)
        print(json.dumps(sample, separators=(",", ":")), flush=True)
        if not sample["supported"]:
            raise SystemExit("target process missing, inaccessible, or /proc unavailable")


class ResourceMonitor:
    def __init__(self, state: Callable[[], dict], *, interval: float = 300):
        self.sampler = ResourceSampler()
        self.state = state
        self.interval = interval
        self._task: asyncio.Task | None = None

    def record(self, event: str) -> None:
        if not log.isEnabledFor(logging.INFO):
            return
        try:
            sample = self.sampler.snapshot(event, state=self.state())
            log.info("resource_snapshot %s", json.dumps(sample, separators=(",", ":")))
        except Exception:
            # Diagnostics must never prevent Telegram delivery or recovery.
            log.warning("resource snapshot unavailable", exc_info=True)

    def start(self) -> None:
        if self.interval > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="hamroh-resources")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self.record("periodic")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


if __name__ == "__main__":
    main()
