"""Retain a single cleanup task until an owned process confirms shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


def begin_close(
    existing: asyncio.Task[None] | None,
    close: Callable[[], Awaitable[None]],
    *,
    name: str,
) -> asyncio.Task[None]:
    retry = existing is None or existing.cancelled()
    if existing is not None and existing.done() and not existing.cancelled():
        retry = existing.exception() is not None
    if not retry:
        assert existing is not None
        return existing

    async def finish() -> None:
        await close()

    return asyncio.create_task(finish(), name=name)


async def await_close(task: asyncio.Task[None], *, timeout: float) -> None:
    # The owner stores the task before awaiting, so timeout/cancellation never
    # loses the cleanup operation or permits an overlapping replacement.
    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
