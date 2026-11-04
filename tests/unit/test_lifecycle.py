from __future__ import annotations

import asyncio

import pytest

from hamroh.helpers.lifecycle import await_close, begin_close


def test_cleanup_timeout_retains_one_uncancelled_operation():
    async def run():
        released = asyncio.Event()
        calls = []

        async def close():
            calls.append(True)
            await released.wait()

        task = begin_close(None, close, name="test-close")
        with pytest.raises(asyncio.TimeoutError):
            await await_close(task, timeout=0.001)
        assert not task.cancelled()
        assert begin_close(task, close, name="test-close") is task
        released.set()
        await await_close(task, timeout=1)
        assert len(calls) == 1

    asyncio.run(run())


def test_cancelled_waiter_does_not_cancel_cleanup():
    async def run():
        started = asyncio.Event()
        released = asyncio.Event()

        async def close():
            started.set()
            await released.wait()

        task = begin_close(None, close, name="test-close")
        waiter = asyncio.create_task(await_close(task, timeout=1))
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not task.cancelled()
        released.set()
        await await_close(task, timeout=1)

    asyncio.run(run())


def test_completed_cleanup_is_not_repeated_after_a_timed_out_waiter():
    async def run():
        calls = []

        async def close():
            calls.append(True)

        task = begin_close(None, close, name="test-close")
        await task
        assert begin_close(task, close, name="test-close") is task
        await await_close(task, timeout=1)
        assert calls == [True]

    asyncio.run(run())


def test_failed_cleanup_can_retry_before_any_replacement_is_created():
    async def run():
        calls = []

        async def close():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("cleanup failed")

        task = begin_close(None, close, name="test-close")
        with pytest.raises(RuntimeError):
            await await_close(task, timeout=1)
        retried = begin_close(task, close, name="test-close")
        assert retried is not task
        await await_close(retried, timeout=1)
        assert len(calls) == 2

    asyncio.run(run())
