"""
services/unsubscribe.py
───────────────────────
Unsubscribe / opt-out management for Nahla.

When a customer sends an unsubscribe keyword ("إلغاء الاشتراك", "STOP", …)
they are immediately marked as opted-out in their extra_metadata and excluded
from:
  - All outbound campaigns
  - All autopilot automations (cart recovery, order notifications, …)
  - All AI-generated replies from the merchant brain

Re-subscription is automatic: the moment the customer sends **any** message
(even a single character), they are moved back to the regular customer pool
and the merchant receives a system notification in the conversation.

All state is stored in Customer.extra_metadata so no DB migration is needed:
    is_unsubscribed   bool   True while opted-out
    unsubscribed_at   str    ISO-8601 timestamp of the opt-out event
    resubscribed_at   str    ISO-8601 timestamp of the last re-subscription
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("nahla.unsubscribe")

# ── Keyword registry ──────────────────────────────────────────────────────────
# All matching is case-insensitive and ignores leading/trailing whitespace.
# We intentionally keep the list broad enough to catch common misspellings
# and both Arabic/English forms without requiring an exact word boundary,
# while keeping it narrow enough that casual messages are not misclassified.

_UNSUB_PATTERNS: tuple[str, ...] = (
    r"إلغاء\s*الاشتراك",
    r"إلغاء\s*الاشتراك",   # variant with alef wasla
    r"الغاء\s*الاشتراك",
    r"إيقاف\s*الرسائل?",
    r"ايقاف\s*الرسائل?",
    r"لا\s*أريد\s*الرسائل?",
    r"لا\s*اريد\s*الرسائل?",
    r"أوقف\s*الرسائل?",
    r"اوقف\s*الرسائل?",
    r"توقف\s*عن\s*الإرسال",
    r"توقف\s*عن\s*الارسال",
    r"لا\s*ترسل\s*لي",
    r"لا\s*تراسلني",
    r"أوقف\s*التواصل",
    r"اوقف\s*التواصل",
    r"^stop$",
    r"^unsubscribe$",
    r"^optout$",
    r"^opt.out$",
)

_COMPILED = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in _UNSUB_PATTERNS
]


# ── Public API ────────────────────────────────────────────────────────────────

def is_unsubscribe_request(text: str) -> bool:
    """Return True if *text* matches any unsubscribe keyword."""
    if not text:
        return False
    cleaned = text.strip()
    return any(rx.search(cleaned) for rx in _COMPILED)


def is_customer_unsubscribed(customer: Any) -> bool:
    """Check whether *customer* (ORM row) has opted out."""
    meta = getattr(customer, "extra_metadata", None) or {}
    return bool(meta.get("is_unsubscribed"))


def mark_unsubscribed(
    db: Any,
    customer: Any,
    *,
    commit: bool = True,
) -> None:
    """Flag *customer* as unsubscribed and persist."""
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    meta["is_unsubscribed"]  = True
    meta["unsubscribed_at"]  = datetime.now(timezone.utc).isoformat()
    meta.pop("resubscribed_at", None)

    customer.extra_metadata = meta
    _flag(customer)
    db.add(customer)
    if commit:
        db.commit()
    logger.info(
        "customer %s (phone=%s) marked as UNSUBSCRIBED",
        getattr(customer, "id", "?"),
        getattr(customer, "phone", "?"),
    )


def mark_resubscribed(
    db: Any,
    customer: Any,
    *,
    commit: bool = True,
) -> None:
    """Remove the unsubscribed flag from *customer* and persist."""
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    meta["is_unsubscribed"]  = False
    meta["resubscribed_at"]  = datetime.now(timezone.utc).isoformat()

    customer.extra_metadata = meta
    _flag(customer)
    db.add(customer)
    if commit:
        db.commit()
    logger.info(
        "customer %s (phone=%s) RE-SUBSCRIBED automatically",
        getattr(customer, "id", "?"),
        getattr(customer, "phone", "?"),
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _flag(customer: Any) -> None:
    """Nudge SQLAlchemy to emit an UPDATE for the JSONB column."""
    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        flag_modified(customer, "extra_metadata")
    except Exception:
        pass
