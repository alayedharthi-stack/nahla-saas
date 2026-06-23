"""
checkout_slot_contact_guard.py
──────────────────────────────
Defer pre-brain contact/showroom routing when the customer is answering
an active checkout slot — not requesting staff or pickup.

Delegates to :mod:`prebrain_order_flow_arbiter` for platform-wide slot
ownership. Legacy helpers remain for city/showroom probes.
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
    "delivery_address",
    "location",
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


def message_fulfills_checkout_slot(message: str, *, order_prep: Any) -> bool:
    """True when inbound text satisfies an awaited checkout slot."""
    from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: PLC0415
        message_fulfills_awaited_checkout_slot,
    )

    return message_fulfills_awaited_checkout_slot(
        message or "",
        order_prep=order_prep,
        customer_phone="",
    )


def should_defer_contact_routing_for_checkout_slot(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
) -> bool:
    """Return True when contact/showroom pre-brain paths must yield to Brain."""
    from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: PLC0415
        should_yield_prebrain_to_order_flow,
    )

    if should_yield_prebrain_to_order_flow(
        db,
        tenant_id=int(tenant_id or 0),
        customer_phone=customer_phone or "",
        message=message or "",
    ):
        return True

    # Fallback when brain state is unavailable: bare city token still yields.
    if not (message or "").strip():
        return False
    if has_explicit_showroom_pickup_intent(message):
        return False
    return is_bare_city_token_message(message)


__all__ = [
    "has_explicit_showroom_pickup_intent",
    "is_bare_city_token_message",
    "message_fulfills_checkout_slot",
    "should_defer_contact_routing_for_checkout_slot",
]
