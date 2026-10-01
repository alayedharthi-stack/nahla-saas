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

The statuses the platform writes on its own orders are read as it defines
them; a status no store adapter and no platform module defines stays unknown.
"""
from __future__ import annotations

import logging
from typing import Any

from core import order_payment_policy as _payment_policy
from core import wa_order_lifecycle as _wa_lifecycle
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


# The statuses the platform writes on its own orders while they are still under
# way, taken from where it defines them: the WhatsApp order lifecycle, the
# payment/fulfilment policy, and the COD confirmation flow's waiting states
# (services/cod_confirmation: STATUS_PENDING_CUSTOMER, STATUS_PENDING_MERCHANT).
PLATFORM_ONGOING_STATUSES = frozenset({
    _wa_lifecycle.STATUS_DRAFT,
    _wa_lifecycle.STATUS_PENDING_CUSTOMER_INFO,
    _wa_lifecycle.STATUS_PENDING_PAYMENT,
    _wa_lifecycle.STATUS_PAYMENT_SUBMITTED,
    _wa_lifecycle.STATUS_PAID,
    _wa_lifecycle.STATUS_PROCESSING,
    _payment_policy.ORDER_STATUS_PAYMENT_SUBMITTED,
    _payment_policy.ORDER_STATUS_COD_PENDING,
    _payment_policy.ORDER_STATUS_READY_TO_PROCESS,
    _payment_policy.ORDER_STATUS_READY_TO_SHIP,
    _payment_policy.ORDER_STATUS_SHIPMENT_CREATED,
    _payment_policy.ORDER_STATUS_LABEL_GENERATED,
    "pending_confirmation",
    "under_review",
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
    elif (state in ONGOING_ORDER_STATES or slug in PLATFORM_ONGOING_STATUSES
          or slug in ORDER_STATUS_LABELS_AR):
        stage = "ongoing"
    else:
        stage = "unknown"
    if slug in FINISHED_ORDER_STATES and stage != "finished":
        label = LIFECYCLE_STATE_LABELS_AR.get(state) or order_status_label_ar(state)
        return stage, label
    return stage, order_status_label_ar(str(status or "").strip())


def order_stage(status: Any, source: Any) -> str:
    return order_reading(status, source)[0]


__all__ = [
    "FINISHED_ORDER_STATES",
    "ONGOING_ORDER_STATES",
    "PLATFORM_ONGOING_STATUSES",
    "order_reading",
    "order_stage",
]
