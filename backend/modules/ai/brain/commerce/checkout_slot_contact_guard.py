"""
checkout_slot_contact_guard.py
──────────────────────────────
Defer pre-brain contact/showroom routing when the customer is answering
an active checkout slot (city / address), not requesting staff or pickup.

Platform-wide: persisted ``order_prep.missing_fields`` + ordering slots —
no tenant hardcoding.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.checkout_slot_contact_guard")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_ADDRESS_MISSING = frozenset({
    "address",
    "address_location",
    "address_line",
    "short_address_code",
    "google_maps_url",
    "city",
    "district",
    "street",
    "postal_code",
})

_SHOWROOM_PICKUP_RE = re.compile(
    r"(?:"
    r"المعرض|الفرع|استلام\s*من|أ?ستلم\s*من|استلم\s*من|"
    r"أ?ج(?:ي|يك)(?:كم|ك)?\s*(?:المعرض|الفرع)?|"
    r"موقع\s*المعرض|فرع\s+|وين\s*(?:المعرض|الفرع|موقع)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    return _WS_RE.sub(" ", t).strip()


def has_explicit_showroom_pickup_intent(message: str) -> bool:
    """True when customer explicitly asks about showroom pickup / branch visit."""
    raw = (message or "").strip()
    if not raw:
        return False
    if _SHOWROOM_PICKUP_RE.search(raw):
        return True
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_explicit_arrival_intent,
            is_location_query,
        )

        if is_explicit_arrival_intent(raw):
            return True
        if is_location_query(raw):
            return True
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — optional arrival probe must not block city guard
        logger.debug("[CHECKOUT_SLOT_GUARD] arrival_intent_probe_failed err=%s", exc)
    return False


def is_bare_city_token_message(message: str) -> bool:
    """True when the inbound is essentially a Saudi city name (checkout answer)."""
    raw = (message or "").strip()
    if not raw or len(raw) > 48:
        return False
    if has_explicit_showroom_pickup_intent(raw):
        return False
    try:
        from modules.ai.brain.intent.ordering_extractor import (  # noqa: PLC0415
            _detect_city,
        )

        city = _detect_city(raw)
        if not city:
            return False
        remainder = _norm(raw)
        for token in ("المدينه", "المدينة", "city", "توصيل", "delivery"):
            remainder = remainder.replace(_norm(token), " ")
        remainder = _WS_RE.sub(" ", remainder).strip()
        city_norm = _norm(city)
        if remainder in {"", city_norm, _norm(raw)}:
            return True
        if len(remainder.split()) <= 2 and city_norm in remainder:
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _order_prep_awaiting_address(op: Any) -> bool:
    if op is None:
        return False
    if isinstance(op, dict):
        missing = {str(x).strip().lower() for x in (op.get("missing_fields") or []) if x}
        has_product = bool(str(op.get("product_id") or op.get("product_name") or "").strip())
        has_city = bool(str(op.get("city") or "").strip())
        status = str(op.get("order_status") or "").strip().lower()
    else:
        missing = {
            str(x).strip().lower()
            for x in (getattr(op, "missing_fields", None) or [])
            if x
        }
        has_product = bool(
            str(getattr(op, "product_id", "") or getattr(op, "product_name", "") or "").strip()
        )
        has_city = bool(str(getattr(op, "city", "") or "").strip())
        status = str(getattr(op, "order_status", "") or "").strip().lower()

    if missing & _ADDRESS_MISSING:
        return True
    if has_product and not has_city:
        return True
    if status in {
        "awaiting_address",
        "awaiting_product",
        "awaiting_payment",
        "awaiting_payment_receipt",
    }:
        return True
    return False


def message_fulfills_checkout_slot(message: str, *, order_prep: Any) -> bool:
    """True when inbound text satisfies an awaited checkout slot."""
    if not _order_prep_awaiting_address(order_prep):
        return False
    if isinstance(order_prep, dict):
        missing = {str(x).strip().lower() for x in (order_prep.get("missing_fields") or []) if x}
    else:
        missing = {
            str(x).strip().lower()
            for x in (getattr(order_prep, "missing_fields", None) or [])
            if x
        }

    try:
        from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots  # noqa: PLC0415

        slots = extract_ordering_slots(message or "")
    except Exception:  # noqa: BLE001
        slots = {}

    if "city" in missing and slots.get("city"):
        return True
    if missing & {"address", "short_address_code", "google_maps_url"}:
        if slots.get("short_address_code") or slots.get("google_maps_url"):
            return True
    if is_bare_city_token_message(message or ""):
        return True
    return False


def should_defer_contact_routing_for_checkout_slot(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
) -> bool:
    """Return True when contact/showroom pre-brain paths must yield to Brain."""
    if not (message or "").strip():
        return False
    if has_explicit_showroom_pickup_intent(message):
        return False
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415

        _, brain_state = _load_brain_state(
            db,
            tenant_id=int(tenant_id or 0),
            phone=str(customer_phone or ""),
        )
        op = (brain_state or {}).get("order_prep") or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CHECKOUT_SLOT_GUARD] brain_state load skipped tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return is_bare_city_token_message(message)

    if not _order_prep_awaiting_address(op):
        return False

    if message_fulfills_checkout_slot(message, order_prep=op):
        logger.info(
            "[CHECKOUT_SLOT_GUARD] defer=contact_routing tenant=%s "
            "reason=slot_fulfillment preview=%r missing=%s",
            tenant_id,
            (message or "")[:80],
            list((op.get("missing_fields") if isinstance(op, dict) else []) or [])[:6],
        )
        return True

    if is_bare_city_token_message(message):
        logger.info(
            "[CHECKOUT_SLOT_GUARD] defer=contact_routing tenant=%s "
            "reason=bare_city_token preview=%r",
            tenant_id,
            (message or "")[:80],
        )
        return True
    return False


__all__ = [
    "has_explicit_showroom_pickup_intent",
    "is_bare_city_token_message",
    "message_fulfills_checkout_slot",
    "should_defer_contact_routing_for_checkout_slot",
]
