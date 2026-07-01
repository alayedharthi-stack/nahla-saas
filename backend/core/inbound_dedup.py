"""
core/inbound_dedup.py
─────────────────────
Strict in-memory deduplication of inbound WhatsApp webhooks **before** they
enter the per-conversation lock or any DB write.

Why
===
Both Meta and 360dialog retry inbound webhooks aggressively. The same
``msg_id`` can arrive 2–4 times within a few seconds (HTTP timeout, ack lag,
upstream load-balancer retry). The existing ``IdempotencyGuard`` in
``Conversation.extra_metadata['recent_msg_ids']`` correctly detects these
duplicates — but only **after** the request has already:

  1. Acquired the per-conversation ``conversation_lock``  (queueing)
  2. Loaded ``StateManager.load(...)`` from the DB        (full row read)
  3. Stamped ``last_webhook_received_at`` on the WhatsAppConnection

So duplicate retries still produce log noise like::

    [ORDER FLOW] acquiring conversation lock | … waiters_ahead=1
    [ORDER FLOW] acquiring conversation lock | … waiters_ahead=2

…and a brief moment of contention on the same row even though both retries
will be dropped a few lines later. Under load this also adds DB round-trips
for traffic that will be discarded.

This module sits **in front** of the lock with a tiny TTL cache keyed by
``(phone_number_id, msg_id)`` (with phone_number_id as the namespace, so
two tenants whose channels happen to be named the same can never collide).
First arrival → ``False`` (allow through). Any retry within ``ttl_seconds``
→ ``True`` (drop instantly with a single log line, no lock, no DB).

Notes
-----
* Pure-Python dict guarded by a ``threading.Lock``. No infra cost.
* Cache is per-process. With multiple uvicorn workers a duplicate could
  still slip through to the slower DB-backed guard — that's fine, the
  DB guard remains the source of truth. This layer is an optimisation.
* Eviction is opportunistic: every insert triggers a sweep of expired
  entries. Worst case the cache holds a few thousand entries during a
  burst; each entry is ~80 bytes so memory is bounded.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger("nahla.inbound_dedup")

# (phone_number_id, msg_id) → expires_at_monotonic
_CACHE: Dict[Tuple[str, str], float] = {}
_LOCK = threading.Lock()

# Default window: 10 minutes is comfortably longer than any retry window
# Meta or 360dialog uses, and far shorter than the 24 h conversation
# window so the cache cannot drift.
_DEFAULT_TTL_SECONDS = 600.0
# Sweep at most this many expired entries per insert so the worst-case
# cost stays O(1).
_SWEEP_BUDGET = 64


def _normalize(value: Optional[str]) -> str:
    return (value or "").strip()


def log_inbound_dedup_event(
    *,
    phone_number_id: Optional[str],
    provider_msg_id: Optional[str],
    result: str,
    source: str = "memory",
) -> None:
    """Structured, log-safe dedup diagnosis — no message body."""
    try:
        logger.info(
            "[INBOUND_DEDUP] phone_number_id=%s provider_msg_id=%s result=%s source=%s",
            (phone_number_id or "-")[:24],
            (provider_msg_id or "-")[:64],
            result,
            source,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — dedup log must not block inbound processing
        pass


def is_duplicate_inbound(
    *,
    phone_number_id: Optional[str],
    msg_id: Optional[str],
    ttl_seconds: float = _DEFAULT_TTL_SECONDS,
) -> bool:
    """
    Atomic check-and-mark for an inbound message id.

    Returns ``True`` when ``(phone_number_id, msg_id)`` was already seen
    within the last ``ttl_seconds`` — caller should drop the webhook.
    Returns ``False`` for a first arrival (and records it for future
    duplicate detection).

    Edge cases:
      * Empty / missing ids → never deduplicated (return False) so we
        cannot accidentally swallow a real message that happens to lack
        an id field. The downstream DB guard will still protect us.
    """
    pid = _normalize(phone_number_id)
    mid = _normalize(msg_id)
    if not pid or not mid:
        log_inbound_dedup_event(
            phone_number_id=pid,
            provider_msg_id=mid,
            result="miss_missing_ids",
            source="memory",
        )
        return False

    key = (pid, mid)
    now = time.monotonic()
    expires = now + max(1.0, ttl_seconds)

    with _LOCK:
        existing = _CACHE.get(key)
        if existing is not None and existing > now:
            log_inbound_dedup_event(
                phone_number_id=pid,
                provider_msg_id=mid,
                result="hit",
                source="memory",
            )
            return True

        # Not a duplicate (or expired) — record this arrival.
        _CACHE[key] = expires

        # Opportunistic eviction so the cache cannot grow unboundedly.
        if len(_CACHE) > 1024:
            stale = []
            for k, exp in _CACHE.items():
                if exp <= now:
                    stale.append(k)
                    if len(stale) >= _SWEEP_BUDGET:
                        break
            for k in stale:
                _CACHE.pop(k, None)

    log_inbound_dedup_event(
        phone_number_id=pid,
        provider_msg_id=mid,
        result="miss",
        source="memory",
    )
    return False


def cache_size() -> int:
    """Diagnostic: how many entries are currently held."""
    with _LOCK:
        return len(_CACHE)


def reset_cache() -> None:
    """Test helper: clear all entries (never call in production paths)."""
    with _LOCK:
        _CACHE.clear()
