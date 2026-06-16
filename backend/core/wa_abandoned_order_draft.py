"""
core/wa_abandoned_order_draft.py
────────────────────────────────
PR-5 — Safe WhatsApp draft-order reminder eligibility.

Operational only: determines which Nahla-native WhatsApp orders may receive
an abandoned-draft reminder and of which kind. Sending always flows through
``automation_emitters.scan_abandoned_order_drafts`` → automation engine →
``send_governor``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.wa_order_lifecycle import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DRAFT,
    STATUS_PAID,
    STATUS_PAYMENT_SUBMITTED,
    STATUS_PENDING_CUSTOMER_INFO,
    STATUS_PENDING_PAYMENT,
    STATUS_PROCESSING,
    has_accepted_delivery_address,
)

REMINDER_COMPLETE_ORDER = "complete_order"
REMINDER_ADDRESS = "address"
REMINDER_PAYMENT = "payment"

_NO_REMINDER_STATUSES = frozenset({
    STATUS_PAYMENT_SUBMITTED,
    STATUS_PAID,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PROCESSING,
    "cancelled",
    "canceled",
    "complete",
    "abandoned",
})

_METADATA_REMINDERS_KEY = "wa_abandoned_draft_reminders"


@dataclass(frozen=True)
class WaDraftReminderPlan:
    reminder_kind: str
    order_status: str


def is_nahla_wa_order(order: Any) -> bool:
    """True for orders created by the Nahla WhatsApp bridge — not Salla/Zid carts."""
    meta = dict(getattr(order, "extra_metadata", None) or {})
    if meta.get("created_via") == "nahla_order_bridge":
        return True
    if str(meta.get("origin") or "").strip().lower() == "whatsapp_ai":
        return True
    ext = str(getattr(order, "external_id", "") or "")
    return ext.startswith("nahla-wa-")


def _order_line_items(order: Any) -> List[Dict[str, Any]]:
    raw = getattr(order, "line_items", None)
    if isinstance(raw, list) and raw:
        return raw
    meta = dict(getattr(order, "extra_metadata", None) or {})
    for key in ("line_items", "cart_items", "items"):
        items = meta.get(key)
        if isinstance(items, list) and items:
            return items
    return []


def _order_prep_from_metadata(order: Any) -> Dict[str, Any]:
    meta = dict(getattr(order, "extra_metadata", None) or {})
    prep = meta.get("order_prep")
    if isinstance(prep, dict):
        return prep
    return meta


def has_wa_order_line_items(order: Any) -> bool:
    return bool(_order_line_items(order))


def _delivery_address_accepted(order: Any) -> bool:
    prep = _order_prep_from_metadata(order)
    if has_accepted_delivery_address(prep):
        return True
    meta = dict(getattr(order, "extra_metadata", None) or {})
    customer = dict(getattr(order, "customer_info", None) or {})
    merged = {**customer, **meta}
    return has_accepted_delivery_address(merged)


def resolve_wa_abandoned_draft_reminder(
    order: Any,
) -> Optional[WaDraftReminderPlan]:
    """
    Return a reminder plan for this order, or ``None`` when no customer reminder
    should be sent.
    """
    if not is_nahla_wa_order(order):
        return None
    if bool(getattr(order, "is_abandoned", False)):
        return None

    status = str(getattr(order, "status", "") or "").strip().lower()
    if status in _NO_REMINDER_STATUSES:
        return None

    if status == STATUS_DRAFT:
        if not has_wa_order_line_items(order):
            return None
        return WaDraftReminderPlan(
            reminder_kind=REMINDER_COMPLETE_ORDER,
            order_status=status,
        )

    if status == STATUS_PENDING_CUSTOMER_INFO:
        if _delivery_address_accepted(order):
            return None
        return WaDraftReminderPlan(
            reminder_kind=REMINDER_ADDRESS,
            order_status=status,
        )

    if status == STATUS_PENDING_PAYMENT:
        if not _delivery_address_accepted(order):
            return None
        return WaDraftReminderPlan(
            reminder_kind=REMINDER_PAYMENT,
            order_status=status,
        )

    return None


def reminder_already_sent(
    order: Any,
    *,
    reminder_kind: str,
) -> bool:
    meta = dict(getattr(order, "extra_metadata", None) or {})
    progress: List[Dict[str, Any]] = list(meta.get(_METADATA_REMINDERS_KEY) or [])
    return any(
        str(entry.get("reminder_kind") or "") == reminder_kind
        for entry in progress
    )


def stamp_reminder_emitted(
    order: Any,
    *,
    reminder_kind: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return updated ``extra_metadata`` with reminder progress appended."""
    meta = dict(getattr(order, "extra_metadata", None) or {})
    progress: List[Dict[str, Any]] = list(meta.get(_METADATA_REMINDERS_KEY) or [])
    ts = (now or datetime.now(timezone.utc)).isoformat()
    progress.append({
        "reminder_kind": reminder_kind,
        "emitted_at":    ts,
    })
    meta[_METADATA_REMINDERS_KEY] = progress[-10:]
    return meta


__all__ = [
    "REMINDER_ADDRESS",
    "REMINDER_COMPLETE_ORDER",
    "REMINDER_PAYMENT",
    "WaDraftReminderPlan",
    "has_wa_order_line_items",
    "is_nahla_wa_order",
    "reminder_already_sent",
    "resolve_wa_abandoned_draft_reminder",
    "stamp_reminder_emitted",
]
