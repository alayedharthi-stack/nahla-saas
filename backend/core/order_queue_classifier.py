"""
core/order_queue_classifier.py
──────────────────────────────
Single source of truth for "what queue does this Order belong to?".

Why this module exists
──────────────────────
Salla webhooks can deliver ``Order.status`` in three different shapes:

  1. A clean English slug             — ``"pending_payment"``, ``"under_review"``
  2. A localised Arabic display name  — ``"في انتظار الدفع"``, ``"بإنتظار المراجعة"``
  3. A mixed-case variant             — ``"Pending"``, ``"PAYMENT_PENDING"``

Historically the operational-queue route and the
``automation_emitters`` sweepers each carried their own ``frozenset`` of
canonical English slugs. That worked when the slug was always present
in the dict payload, but Salla also exposes a flat string form for
custom store statuses where only the Arabic ``name`` lands in
``Order.status``. The row then silently fell out of *both* the queue
tab AND the unpaid-reminder sweeper — so the merchant saw a paid-link
order vanish from "بانتظار الدفع" and never got the WhatsApp nudge.

This module centralises the matching so every consumer agrees on the
same answer. New aliases (e.g. Zid / Shopify localisations) only need
to be added in one place.

Conventions
───────────
* ``normalize_status`` lowercases + strips. Returns ``""`` for ``None``.
* English-slug membership is checked against the *normalised* form so
  the matcher is case-insensitive.
* Arabic aliases are matched against the *raw* (unmodified) value so we
  don't accidentally fold Arabic diacritics.
* ``classify_order_queue`` returns one of:
      "pending_payment", "pending_confirmation", ""
  The empty string means "this order does not belong to a payment/
  confirmation queue".

The Arabic alias lists deliberately cover Salla's two common spellings
of "waiting" — both ``بانتظار`` (no hamza below) and ``بإنتظار`` (with
hamza below) appear in the wild depending on which Salla template
generated the status.
"""
from __future__ import annotations

from typing import Any, FrozenSet


# ── English / slug forms ────────────────────────────────────────────────────
# Anything in this set means "we still owe a payment notification for this
# order". The sweeper turns these into ORDER_PAYMENT_PENDING events.
PENDING_PAYMENT_SLUGS: FrozenSet[str] = frozenset({
    "pending",
    "pending_payment",
    "payment_pending",
    "awaiting_payment",
    "draft",
    "new",
})

# Anything in this set means "the order is created but waiting for the
# customer (typically COD) to confirm before shipping". The sweeper turns
# these into ORDER_COD_PENDING events.
PENDING_CONFIRMATION_SLUGS: FrozenSet[str] = frozenset({
    "pending_confirmation",
    "awaiting_confirmation",
    "under_review",
    "in_review",
})


# ── Arabic display-name aliases ─────────────────────────────────────────────
# Salla sometimes drops the slug and leaves only the localised name in
# Order.status. We accept the variants the live store_adapter has been
# observed emitting plus a few common merchant-customised wordings.
PENDING_PAYMENT_ARABIC: FrozenSet[str] = frozenset({
    "في انتظار الدفع",
    "بانتظار الدفع",
    "بإنتظار الدفع",
    "قيد الانتظار",        # Salla's localised "pending"
    "بانتظار السداد",
    "بإنتظار السداد",
})

PENDING_CONFIRMATION_ARABIC: FrozenSet[str] = frozenset({
    "بانتظار التأكيد",
    "بإنتظار التأكيد",
    "قيد المراجعة",
    "بانتظار المراجعة",
    "بإنتظار المراجعة",
    "بانتظار تأكيد العميل",
})


# ── Public helpers ──────────────────────────────────────────────────────────

def normalize_status(status: Any) -> str:
    """Return a lowercased, whitespace-stripped status. Empty string for None."""
    if status is None:
        return ""
    return str(status).strip().lower()


def _raw(status: Any) -> str:
    """Return the trimmed raw value (preserves Arabic) — used for Arabic match."""
    if status is None:
        return ""
    return str(status).strip()


def is_pending_payment_status(status: Any) -> bool:
    """True iff this order status maps to the 'بانتظار الدفع' queue."""
    norm = normalize_status(status)
    if not norm:
        return False
    if norm in PENDING_PAYMENT_SLUGS:
        return True
    return _raw(status) in PENDING_PAYMENT_ARABIC


def is_pending_confirmation_status(status: Any) -> bool:
    """True iff this order status maps to the 'بانتظار التأكيد' queue."""
    norm = normalize_status(status)
    if not norm:
        return False
    if norm in PENDING_CONFIRMATION_SLUGS:
        return True
    return _raw(status) in PENDING_CONFIRMATION_ARABIC


def classify_order_queue(status: Any) -> str:
    """
    Return the operational queue this status belongs to:

        "pending_confirmation"  — COD / under-review orders awaiting customer ack
        "pending_payment"       — online-payment orders still unpaid
        ""                      — neither (completed / shipped / cancelled / …)

    Pending-confirmation wins if a status appears in both buckets (defensive
    — they're disjoint today but a future status mapping might overlap and
    a duplicate reminder is worse than a missing one).
    """
    if is_pending_confirmation_status(status):
        return "pending_confirmation"
    if is_pending_payment_status(status):
        return "pending_payment"
    return ""


__all__ = [
    "PENDING_PAYMENT_SLUGS",
    "PENDING_CONFIRMATION_SLUGS",
    "PENDING_PAYMENT_ARABIC",
    "PENDING_CONFIRMATION_ARABIC",
    "normalize_status",
    "is_pending_payment_status",
    "is_pending_confirmation_status",
    "classify_order_queue",
]
