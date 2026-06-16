"""
core/wa_order_linking.py
────────────────────────
Resolve the Nahla-native WhatsApp order row to attach payment evidence to.

Lookup priority (PR-2):
  1. Active order for the same conversation (``nahla-wa-{tenant}-{conv}``).
  2. Same customer ``pending_payment`` WhatsApp order within tenant.
  3. Latest WhatsApp order in linkable statuses for that customer.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

from core.wa_order_lifecycle import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PAID,
    STATUS_PAYMENT_SUBMITTED,
    STATUS_PENDING_CUSTOMER_INFO,
    STATUS_PENDING_PAYMENT,
    STATUS_PROCESSING,
    STATUS_DRAFT,
)
from services.nahla_order_bridge import nahla_wa_external_id

logger = logging.getLogger("nahla.wa_order_linking")

WA_ORDER_SOURCES = frozenset({"whatsapp", "ai_sales_agent", "ai_sales", "ai", "nahla_order"})

LINKABLE_WA_ORDER_STATUSES = frozenset({
    STATUS_DRAFT,
    STATUS_PENDING_CUSTOMER_INFO,
    STATUS_PENDING_PAYMENT,
    STATUS_PAYMENT_SUBMITTED,
})

TERMINAL_WA_ORDER_STATUSES = frozenset({
    STATUS_PAID,
    STATUS_COMPLETED,
    STATUS_PROCESSING,
    STATUS_CANCELLED,
    "confirmed",
    "delivered",
    "shipped",
    "fulfilled",
    "cancelled",
    "canceled",
})


def _norm_status(status: Any) -> str:
    return str(status or "").strip().lower()


def is_linkable_wa_order_status(status: Any) -> bool:
    return _norm_status(status) in LINKABLE_WA_ORDER_STATUSES


def is_terminal_wa_order_status(status: Any) -> bool:
    return _norm_status(status) in TERMINAL_WA_ORDER_STATUSES


def _is_wa_order(order: Any) -> bool:
    src = str(getattr(order, "source", "") or "").strip().lower()
    if src in WA_ORDER_SOURCES:
        return True
    meta = getattr(order, "extra_metadata", None) or {}
    return (
        meta.get("created_via") == "nahla_order_bridge"
        or meta.get("origin") == "whatsapp_ai"
        or str(meta.get("lifecycle") or "").startswith("whatsapp")
    )


def _phone_matches(order: Any, phones: Sequence[str]) -> bool:
    candidates = {p for p in phones if p}
    if not candidates:
        return False
    info = getattr(order, "customer_info", None) or {}
    if isinstance(info, dict):
        for key in ("phone", "mobile", "shipping_phone"):
            val = str(info.get(key) or "").strip()
            if val and val in candidates:
                return True
    meta = getattr(order, "extra_metadata", None) or {}
    if isinstance(meta, dict):
        for key in ("customer_phone", "phone"):
            val = str(meta.get(key) or "").strip()
            if val and val in candidates:
                return True
    return False


def _conversation_id_from_order(order: Any) -> Optional[int]:
    meta = getattr(order, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return None
    raw = meta.get("conversation_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def find_linkable_wa_order(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any = None,
    customer: Any = None,
    phone_candidates: Optional[Sequence[str]] = None,
) -> Optional[Any]:
    """Return the best WhatsApp order to attach a payment submission to."""
    if db is None:
        return None
    try:
        from models import Order  # noqa: PLC0415

        tid = int(tenant_id)
        conv_id = getattr(conversation, "id", None) if conversation is not None else None
        cust = customer if customer is not None else getattr(conversation, "customer", None)

        if conv_id is not None:
            ext = nahla_wa_external_id(tid, int(conv_id))
            row = (
                db.query(Order)
                .filter_by(tenant_id=tid, external_id=ext)
                .first()
            )
            if row is not None and _is_wa_order(row) and is_linkable_wa_order_status(row.status):
                return row

        phones: List[str] = []
        if phone_candidates:
            phones.extend(str(p).strip() for p in phone_candidates if p)
        if cust is not None:
            for attr in ("phone", "mobile", "normalized_phone"):
                val = str(getattr(cust, attr, "") or "").strip()
                if val:
                    phones.append(val)

        base_q = db.query(Order).filter(Order.tenant_id == tid)
        rows = (
            base_q
            .filter(Order.source.in_(tuple(WA_ORDER_SOURCES)))
            .order_by(Order.id.desc())
            .limit(40)
            .all()
        )

        pending_payment: List[Any] = []
        linkable: List[Any] = []
        for row in rows:
            if not _is_wa_order(row):
                continue
            if is_terminal_wa_order_status(row.status):
                continue
            if phones and not _phone_matches(row, phones):
                continue
            if _norm_status(row.status) == STATUS_PENDING_PAYMENT:
                pending_payment.append(row)
            elif is_linkable_wa_order_status(row.status):
                linkable.append(row)

        if pending_payment:
            return pending_payment[0]
        if linkable:
            return linkable[0]
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[WA_ORDER_LINKING] lookup failed tenant=%s err=%s",
            tenant_id, exc,
        )
    return None


MSG_WA_PAYMENT_UNLINKED = (
    "وصلتني إفادة الدفع، لكن أحتاج أربطها بطلبك. "
    "فضلاً اكتب المنتج أو رقم الطلب إن وجد."
)


__all__ = [
    "LINKABLE_WA_ORDER_STATUSES",
    "MSG_WA_PAYMENT_UNLINKED",
    "TERMINAL_WA_ORDER_STATUSES",
    "find_linkable_wa_order",
    "is_linkable_wa_order_status",
    "is_terminal_wa_order_status",
]
