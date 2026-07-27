"""
customer_commerce_answerer.py
─────────────────────────────
Deterministic customer commerce replies from ``CustomerCommerceProfile``.

Phase 1: order history count + latest order summary only.
No payment totals, product lists, addresses, or tracking claims.
"""
from __future__ import annotations

from typing import List, Optional

from core.customer_commerce_ledger import CustomerCommerceProfile
from core.local_order_resolver import LocalOrderSnapshot
from core.order_status_label import order_status_label_ar

TOPIC_ORDER_HISTORY_COUNT = "order_history_count"
TOPIC_LATEST_ORDER_SUMMARY = "latest_order_summary"
TOPIC_ORDER_REFERENCE_LIST = "order_reference_list"

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


def render_order_reference_list_reply(
    profile: CustomerCommerceProfile,
    snapshots: List[LocalOrderSnapshot],
    *,
    limit: int = 5,
) -> str:
    if int(profile.order_counts.total_orders or 0) <= 0:
        return _NO_ORDERS_REPLY

    capped = max(1, min(int(limit or 5), 5))
    entries: List[str] = []
    for snap in snapshots[:capped]:
        ref = str(snap.display_reference or "").strip()
        if not ref:
            continue
        status = _status_label(profile, snap)
        entries.append(f"{ref} ({status})")

    if not entries:
        return "عندك طلبات مسجلة لكن ما عندي أرقام مرجعية واضحة لها حالياً."
    joined = "، ".join(entries)
    return f"أرقام طلباتك المسجلة عندنا: {joined}."


def render_customer_commerce_reply(
    topic: str,
    profile: CustomerCommerceProfile,
    *,
    snapshots: Optional[List[LocalOrderSnapshot]] = None,
    limit: int = 5,
) -> str:
    key = str(topic or "").strip().lower()
    if key == TOPIC_LATEST_ORDER_SUMMARY:
        return render_latest_order_summary_reply(profile)
    if key == TOPIC_ORDER_REFERENCE_LIST:
        return render_order_reference_list_reply(
            profile,
            list(snapshots or []),
            limit=limit,
        )
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
    limit: int = 5,
) -> str:
    from core.customer_commerce_ledger import (  # noqa: PLC0415
        list_recent_order_snapshots,
        resolve_customer_commerce_profile,
    )

    profile = resolve_customer_commerce_profile(
        db,
        tenant_id=int(tenant_id),
        conversation_id=conversation_id,
        customer_id=customer_id,
        phone=phone,
        include_abandoned=include_abandoned,
        include_cancelled=include_cancelled,
    )
    key = str(topic or "").strip().lower()
    if key == TOPIC_ORDER_REFERENCE_LIST:
        snapshots = list_recent_order_snapshots(
            db,
            tenant_id=int(tenant_id),
            conversation_id=conversation_id,
            customer_id=customer_id,
            phone=phone,
            include_abandoned=include_abandoned,
            include_cancelled=include_cancelled,
            limit=limit,
        )
        return render_order_reference_list_reply(profile, snapshots, limit=limit)
    return render_customer_commerce_reply(topic, profile)


__all__ = [
    "TOPIC_LATEST_ORDER_SUMMARY",
    "TOPIC_ORDER_HISTORY_COUNT",
    "TOPIC_ORDER_REFERENCE_LIST",
    "render_customer_commerce_reply",
    "render_latest_order_summary_reply",
    "render_order_history_count_reply",
    "render_order_reference_list_reply",
    "resolve_customer_commerce_reply",
]
