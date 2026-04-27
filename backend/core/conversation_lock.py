"""
core/conversation_lock.py
─────────────────────────
Per-conversation processing lock.

Rationale
=========
A WhatsApp customer can fire several messages within a couple of seconds
("8", "TAPA7401", "1", "TAPA7401", …). Without a lock those inbound
turns are processed concurrently by the FastAPI event loop, and they
race on the same row in `Conversation.extra_metadata['brain_state']` /
`order_prep`. The last writer wins → state is shredded, the order flow
loops, the customer ends up being asked for their address before they
even picked a product, etc.

This module provides an in-process `asyncio.Lock` keyed by
``tenant_id + customer_phone``. Inbound webhooks for the same
conversation are forced to run one after the other; concurrent inbounds
for *different* conversations are completely unaffected.

Notes
-----
* The lock lives in-process. With multiple uvicorn workers you would
  need a Redis-backed lock (e.g. `redis.lock.Lock`) — but the current
  Nahla deployment runs a single web worker, so this is sufficient and
  has zero infra cost.
* The lock is *fair-ish*: asyncio.Lock grants ownership in FIFO order
  among tasks awaiting it on the same event loop.
* We log acquire / release / queued events so the timing of concurrent
  turns is visible in Railway.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Tuple

logger = logging.getLogger("nahla.conversation_lock")

_LOCKS: Dict[Tuple[int, str], asyncio.Lock] = {}
# Tracks how many tasks are currently waiting on each lock so we can
# emit a "queued inbound turn" log for the second / third / … message
# without having to peek into the lock's internals.
_WAITERS: Dict[Tuple[int, str], int] = {}
# Guards _LOCKS / _WAITERS itself against the (theoretical) race of two
# tasks on the same event loop creating the lock dict entry at the same
# time. asyncio.Lock() is cheap, so this is a tiny critical section.
_REGISTRY_LOCK = asyncio.Lock()


def _normalize_phone_for_key(phone: str) -> str:
    """Strip non-digits so '+966 5...', '966 5...', '00966...' etc.
    all map to the same lock entry. We intentionally keep this very
    cheap — we are NOT trying to reproduce the full E.164 normalizer,
    just to collapse trivial format differences for the SAME number.
    """
    return "".join(ch for ch in (phone or "") if ch.isdigit())


async def _get_lock(tenant_id: int, phone: str) -> Tuple[Tuple[int, str], asyncio.Lock]:
    key = (int(tenant_id), _normalize_phone_for_key(phone))
    lock = _LOCKS.get(key)
    if lock is not None:
        return key, lock
    async with _REGISTRY_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
            _WAITERS[key] = 0
        return key, lock


@asynccontextmanager
async def conversation_lock(
    tenant_id: int,
    phone: str,
    *,
    msg_id: str = "",
    text_snippet: str = "",
) -> AsyncIterator[None]:
    """Serialise inbound processing for a single (tenant, phone) pair.

    Usage::

        async with conversation_lock(tenant_id, phone, msg_id=msg_id, text_snippet=text):
            await _process_turn(...)

    Always logs:
      [ORDER FLOW] acquiring conversation lock | ...
      [ORDER FLOW] queued inbound turn         | ...   (only if other waiters)
      [ORDER FLOW] releasing conversation lock | ... wait_ms=… held_ms=…
    """
    key, lock = await _get_lock(tenant_id, phone)
    waiters_before = _WAITERS.get(key, 0)
    _WAITERS[key] = waiters_before + 1

    snippet = (text_snippet or "")[:60].replace("\n", " ")
    logger.info(
        "[ORDER FLOW] acquiring conversation lock | tenant=%s phone=%s "
        "msg_id=%s waiters_ahead=%d text=%r",
        tenant_id, phone, msg_id, waiters_before, snippet,
    )
    if waiters_before > 0:
        # Another inbound for the same customer is being processed right now —
        # this turn will wait its turn instead of racing with it.
        logger.info(
            "[ORDER FLOW] queued inbound turn | tenant=%s phone=%s "
            "msg_id=%s waiters_ahead=%d",
            tenant_id, phone, msg_id, waiters_before,
        )

    t_wait = time.monotonic()
    try:
        await lock.acquire()
        wait_ms = (time.monotonic() - t_wait) * 1000.0
        t_held = time.monotonic()
        try:
            yield
        finally:
            held_ms = (time.monotonic() - t_held) * 1000.0
            lock.release()
            logger.info(
                "[ORDER FLOW] releasing conversation lock | tenant=%s phone=%s "
                "msg_id=%s wait_ms=%.0f held_ms=%.0f",
                tenant_id, phone, msg_id, wait_ms, held_ms,
            )
    finally:
        _WAITERS[key] = max(0, _WAITERS.get(key, 1) - 1)
        # Trim the registry once a lock has no current owner and no
        # waiters, so the dict can't grow unboundedly across the lifetime
        # of the process.
        if _WAITERS[key] == 0 and not lock.locked():
            _LOCKS.pop(key, None)
            _WAITERS.pop(key, None)
