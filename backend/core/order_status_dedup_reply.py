"""
core/order_status_dedup_reply.py
────────────────────────────────
Short, non-duplicate order-status replies when CHAT_DEDUP hard-tier would
otherwise silence track/order-number turns that already have local order evidence.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from modules.ai.brain.commerce.dedup_operational_delta import (
    is_local_order_status_inquiry,
)

_ORDER_REF_IN_TEXT_RE = re.compile(
    r"(?:"
    r"رقم\s*(?:ال)?طلب(?:يه)?\s*"
    r"|طلب\s*"
    r")(\d{5,})",
    re.UNICODE | re.IGNORECASE,
)
_TRAILING_DIGITS_RE = re.compile(r"(\d{6,})\s*$")

_DEDUP_STATUS_EXTRA_AR = {
    "payment_pending": "قيد إكمال الدفع",
    "pending_payment": "قيد إكمال الدفع",
}


def _extract_order_ref_from_inbound(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None
    match = _ORDER_REF_IN_TEXT_RE.search(raw)
    if match:
        return str(match.group(1) or "").strip() or None
    tail = _TRAILING_DIGITS_RE.search(raw)
    if tail:
        return str(tail.group(1) or "").strip() or None
    return None


def _status_label_ar(status: str) -> str:
    slug = str(status or "").strip().lower()
    if not slug:
        return "غير معروفة"
    if slug in _DEDUP_STATUS_EXTRA_AR:
        return _DEDUP_STATUS_EXTRA_AR[slug]
    try:
        from modules.ai.brain.execution.orders import _ORDER_STATUS_AR  # noqa: PLC0415

        mapped = _ORDER_STATUS_AR.get(slug)
        if mapped:
            return mapped
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional status map import
        pass
    return f"حالة طلبك الحالية: {status}"


def build_dedup_local_order_short_reply(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    conversation_id: Optional[int] = None,
    inbound_text: str,
    previous_outbound: str = "",
) -> str:
    """
    Build a concise order ref + status reply for hard-dedup silenced turns.

    Returns empty when the inbound is not an order-status inquiry or when no
    local order evidence exists — callers keep existing dedup silence behavior.
    """
    if not is_local_order_status_inquiry(inbound_text):
        return ""

    order_number = _extract_order_ref_from_inbound(inbound_text)
    try:
        from modules.ai.order_flow_v2.triggers import (  # noqa: PLC0415
            is_checkout_order_number_intent,
        )

        intent = (
            "order_number"
            if is_checkout_order_number_intent(inbound_text)
            else "track_order"
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional trigger import
        intent = "track_order"

    try:
        from core.local_order_resolver import resolve_customer_order_context  # noqa: PLC0415

        ctx = resolve_customer_order_context(
            db,
            tenant_id=int(tenant_id),
            phone=str(phone or "").strip(),
            conversation_id=conversation_id,
            intent=intent,
            order_number=order_number,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — resolver must not break dedup path
        return ""

    selected = getattr(ctx, "selected_order", None)
    if selected is None:
        return ""

    ref = str(getattr(selected, "display_reference", "") or "").strip()
    if not ref:
        return ""

    status = str(getattr(selected, "status", "") or "").strip()
    label = _status_label_ar(status)
    prev = (previous_outbound or "").strip()
    ref_in_prev = ref in prev

    try:
        from modules.ai.order_flow_v2.triggers import (  # noqa: PLC0415
            is_checkout_order_number_intent,
        )

        asks_order_number = is_checkout_order_number_intent(inbound_text)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional trigger import
        asks_order_number = False

    if asks_order_number:
        return f"رقم طلبك {ref}، وحالته الحالية {label}."
    if ref_in_prev:
        return f"نفس الطلب {ref} ما زال {label}."
    return f"طلبك هو {ref}، وحالته الحالية {label}."
