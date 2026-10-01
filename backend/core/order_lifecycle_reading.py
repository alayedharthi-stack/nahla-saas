"""
core/order_lifecycle_reading.py
───────────────────────────────
Where an order stands, read the way its own store means its status.

One reading for every place that tells the agent about the customer's orders:
the history groups by it, the one-order lookup picks by it and labels by it, so
the two can never disagree about an order. The store's word is read through
that store's lifecycle adapter (``resolve_customer_relevant_state``, the
platform's provider-neutral projection): the same word can mean different
stages on different platforms — on Salla ``completed`` is the merchant's
«تنفيذ», fulfilled and not yet handed over, so it is still under way.

* ``finished`` — a terminal state (delivered, cancelled, refunded, …);
* ``ongoing`` — a state the order is still under way in;
* ``unknown`` — a status the platform cannot read: claimed neither way.

An abandoned cart, which store sync keeps as an order row, is not an order:
the platform's customer ledger counts a row as abandoned when it carries the
``is_abandoned`` flag or the ``abandoned`` status.
"""
from __future__ import annotations

import logging
from typing import Any

from core.order_status_label import (
    LIFECYCLE_STATE_LABELS_AR,
    ORDER_STATUS_LABELS_AR,
    order_status_label_ar,
)

logger = logging.getLogger("nahla.order_lifecycle_reading")

# Store states that mean an order is finished.
FINISHED_ORDER_STATES = frozenset({
    "cancelled", "canceled", "abandoned", "delivered", "completed", "complete",
    "refunded", "returned", "failed",
})
# The platform's lifecycle states an order is still under way in: the states a
# store's lifecycle adapter reads a status into
# (store_integration.lifecycle_normalization), before shipment is complete.
ONGOING_ORDER_STATES = frozenset({
    "payment_pending", "paid", "confirmed", "preparing", "ready", "shipped", "out_for_delivery",
})


def _status_slug(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _lifecycle_state(slug: str, source: Any) -> str:
    try:
        from store_integration.lifecycle_normalization import resolve_customer_relevant_state

        return _status_slug(resolve_customer_relevant_state(provider=str(source or ""), raw_status=slug))
    except Exception as exc:  # noqa: BLE001 - the plain reading stands in; the order read is not lost
        logger.warning("[ORDER_LIFECYCLE] lifecycle adapter unavailable error=%s", type(exc).__name__)
        return slug


def order_reading(status: Any, source: Any) -> tuple[str, str]:
    """Where an order stands and the label that says so.

    A finished-sounding word that the store's adapter reads as still under way
    (Salla ``completed``) is labelled by that state, since its plain label would
    claim what the order has not reached; the state's label claims no more than
    the state (``ready`` names neither shipment nor collection, so it holds for
    a pickup order too). Any other word keeps its own label: the adapter's
    reading places the order, but it may be coarser than the word.
    """
    slug = _status_slug(status)
    state = _lifecycle_state(slug, source)
    if state in FINISHED_ORDER_STATES:
        stage = "finished"
    elif state in ONGOING_ORDER_STATES or slug in ORDER_STATUS_LABELS_AR:
        stage = "ongoing"
    else:
        stage = "unknown"
    if slug in FINISHED_ORDER_STATES and stage != "finished":
        label = LIFECYCLE_STATE_LABELS_AR.get(state) or order_status_label_ar(state)
        return stage, label
    return stage, order_status_label_ar(str(status or "").strip())


def order_stage(status: Any, source: Any) -> str:
    return order_reading(status, source)[0]


def is_abandoned_cart(order: Any) -> bool:
    """The customer ledger's abandoned rule, for one row (``_ledger_abandoned_sql``)."""
    return (getattr(order, "is_abandoned", None) is True
            or str(getattr(order, "status", "") or "").lower() == "abandoned")


__all__ = [
    "FINISHED_ORDER_STATES",
    "ONGOING_ORDER_STATES",
    "is_abandoned_cart",
    "order_reading",
    "order_stage",
]
