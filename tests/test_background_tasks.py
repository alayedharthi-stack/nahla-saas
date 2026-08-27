"""Tests for asyncio background task registry and shutdown."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.background_tasks import (
    get_background_task,
    register_background_task,
    shutdown_background_tasks,
)


def _start_poller_once(name: str, factory):
    """Mirror main.py duplicate-guard for the coupons poller."""
    existing = get_background_task(name)
    if existing is not None and not existing.done():
        return None
    task = asyncio.create_task(factory())
    register_background_task(name, task)
    return task


def test_register_duplicate_poller_prevented():
    async def _run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def _fake_poller():
            started.set()
            await release.wait()

        first = _start_poller_once("salla_coupons_poller", _fake_poller)
        assert first is not None
        await started.wait()
        second = _start_poller_once("salla_coupons_poller", _fake_poller)
        assert second is None
        assert get_background_task("salla_coupons_poller") is first

        release.set()
        await shutdown_background_tasks(timeout_seconds=2.0)
        assert first.done()

    asyncio.run(_run())


def test_shutdown_awaits_and_cancels_registered_tasks():
    async def _run():
        cancelled = asyncio.Event()

        async def _slow_worker():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(_slow_worker())
        register_background_task("test_worker", task)
        await shutdown_background_tasks(timeout_seconds=2.0)

        assert get_background_task("test_worker") is None

    asyncio.run(_run())
