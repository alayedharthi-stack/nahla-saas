"""
customer_commerce_answerer.py
─────────────────────────────
Deterministic customer commerce replies from ``CustomerCommerceProfile``.

Phase 1: order history count + latest order summary only.
No payment totals, product lists, addresses, or tracking claims.
"""
from __future__ import annotations

from typing import Optional

from core.customer_commerce_ledger import CustomerCommerceProfile
from core.order_status_label import order_status_label_ar

TOPIC_ORDER_HISTORY_COUNT = "order_history_count"
TOPIC_LATEST_ORDER_SUMMARY = "latest_order_summary"

_NO_ORDERS_REPLY = "ما ظهر لي طلبات مسجلة على هذا الرقم."


def _status_label(profile: CustomerCommerceProfile, snap) -> str:
    if snap is None:
        return ""
    return order_status_label_ar(
        str(snap.status or ""),
        source=str(snap.source or "").strip() or None,
    )


def _latest_reference(profile: CustomerCommerceProfile) -> str:
    snap = profile.latest_order
    if snap is None:
        return ""
    return str(snap.display_reference or "").strip()


def render_order_history_count_reply(profile: CustomerCommerceProfile) -> str:
    total = int(profile.order_counts.total_orders or 0)
    if total <= 0:
        return _NO_ORDERS_REPLY

    parts = [f"عندك {total} طلبات مسجلة عندنا على هذا الرقم."]
    ref = _latest_reference(profile)
    if ref and profile.latest_order is not None:
        status = _status_label(profile, profile.latest_order)
        parts.append(f"آخر طلب رقم {ref} وحالته {status}.")
    return " ".join(parts)


def render_latest_order_summary_reply(profile: CustomerCommerceProfile) -> str:
    snap = profile.latest_order
    if snap is None or int(profile.order_counts.total_orders or 0) <= 0:
        return _NO_ORDERS_REPLY

    ref = str(snap.display_reference or "").strip()
    status = _status_label(profile, snap)
    if not ref:
        return f"آخر طلب مسجل عندنا حالته {status}."
    return f"آخر طلب مسجل عندنا رقم {ref} وحالته {status}."


def render_customer_commerce_reply(
    topic: str,
    profile: CustomerCommerceProfile,
) -> str:
    key = str(topic or "").strip().lower()
    if key == TOPIC_LATEST_ORDER_SUMMARY:
        return render_latest_order_summary_reply(profile)
    return render_order_history_count_reply(profile)


def resolve_customer_commerce_reply(
    db,
    *,
    topic: str,
    tenant_id: int,
    conversation_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    phone: Optional[str] = None,
    include_abandoned: bool = False,
    include_cancelled: bool = True,
) -> str:
    from core.customer_commerce_ledger import resolve_customer_commerce_profile  # noqa: PLC0415

    profile = resolve_customer_commerce_profile(
        db,
        tenant_id=int(tenant_id),
        conversation_id=conversation_id,
        customer_id=customer_id,
        phone=phone,
        include_abandoned=include_abandoned,
        include_cancelled=include_cancelled,
    )
    return render_customer_commerce_reply(topic, profile)


__all__ = [
    "TOPIC_LATEST_ORDER_SUMMARY",
    "TOPIC_ORDER_HISTORY_COUNT",
    "render_customer_commerce_reply",
    "render_latest_order_summary_reply",
    "render_order_history_count_reply",
    "resolve_customer_commerce_reply",
]
