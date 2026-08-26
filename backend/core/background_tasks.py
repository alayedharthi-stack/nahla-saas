"""Registry for long-lived asyncio background tasks with graceful shutdown."""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

logger = logging.getLogger('nahla.background_tasks')

_REGISTRY: Dict[str, asyncio.Task] = {}


def register_background_task(name: str, task: asyncio.Task) -> None:
    """Register a named background task, replacing any completed predecessor."""
    existing = _REGISTRY.get(name)
    if existing is not None and not existing.done():
        logger.warning('[BG/registry] replacing live task name=%s', name)
    _REGISTRY[name] = task


def get_background_task(name: str) -> Optional[asyncio.Task]:
    return _REGISTRY.get(name)


async def shutdown_background_tasks(timeout_seconds: float = 10.0) -> None:
    """Cancel registered tasks and await completion with a bounded timeout."""
    live = [(name, task) for name, task in list(_REGISTRY.items()) if not task.done()]
    if not live:
        return

    for name, task in live:
        logger.info('[BG/registry] cancelling name=%s', name)
        task.cancel()

    tasks = [task for _, task in live]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        remaining = sum(1 for task in tasks if not task.done())
        logger.warning(
            '[BG/registry] shutdown timed out after %.1fs; remaining=%d',
            timeout_seconds,
            remaining,
        )

    for name, task in live:
        if task.done():
            _REGISTRY.pop(name, None)
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                logger.error('[BG/registry] task=%s exited with error_class=%s', name, type(exc).__name__)


__all__ = [
    'register_background_task',
    'get_background_task',
    'shutdown_background_tasks',
]
