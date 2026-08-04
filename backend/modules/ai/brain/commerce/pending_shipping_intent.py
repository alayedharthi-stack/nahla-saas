"""
pending_shipping_intent.py
──────────────────────────
Restore ask_shipping intent on city-only follow-ups after a pending
shipping-city inquiry marker survives state transition.
"""
from __future__ import annotations

from typing import Any

from ..types import INTENT_ASK_SHIPPING, INTENT_GENERAL, Intent


def restore_pending_shipping_city_intent(
    intent: Intent,
    *,
    db: Any,
    tenant_id: int,
    message: str,
    state: Any = None,
) -> Intent:
    """Promote general intent to ask_shipping when pending city inquiry resolves."""
    if str(getattr(intent, "name", "") or "") != INTENT_GENERAL:
        return intent

    try:
        from core.checkout_shipping_policy import (  # noqa: PLC0415
            build_shipping_knowledge_facts,
            get_pending_shipping_city,
        )
    except Exception:  # noqa: BLE001
        return intent

    pending = get_pending_shipping_city(state)
    if not pending or not pending.get("needs_city"):
        return intent

    if db is None:
        return intent

    brain_state: dict[str, Any] = {}
    order_prep: dict[str, Any] = {}
    try:
        if state is not None and hasattr(state, "to_dict"):
            brain_state = dict(state.to_dict() or {})
        prep_obj = getattr(state, "order_prep", None)
        if prep_obj is not None and hasattr(prep_obj, "to_dict"):
            order_prep = dict(prep_obj.to_dict() or {})
    except Exception:  # noqa: BLE001
        brain_state = {}
        order_prep = {}

    try:
        facts = build_shipping_knowledge_facts(
            db,
            tenant_id=int(tenant_id or 0),
            message=str(message or ""),
            brain_state=brain_state,
            order_prep=order_prep,
        )
    except Exception:  # noqa: BLE001
        return intent

    city = str((facts or {}).get("city") or "").strip()
    if not city or bool((facts or {}).get("need_city")):
        return intent

    slots = dict(getattr(intent, "slots", None) or {})
    slots["city"] = city

    return Intent(
        name=INTENT_ASK_SHIPPING,
        confidence=max(float(getattr(intent, "confidence", 0) or 0), 0.9),
        slots=slots,
        raw_message=str(getattr(intent, "raw_message", "") or ""),
        extraction_method="hybrid",
    )


__all__ = ["restore_pending_shipping_city_intent"]
