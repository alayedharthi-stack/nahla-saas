"""OrderFlowV2 missing fields — ordered checkout slot collection."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from core.wa_order_lifecycle import compute_wa_missing_fields

from .state import has_payment_method, line_items_from_state
from .slot_ownership import has_address_evidence

logger = logging.getLogger("nahla.order_flow_v2.missing_fields")

_V2_FIELD_ORDER: Tuple[str, ...] = (
    "customer_name",
    "city",
    "delivery_address",
    "payment_method",
)


def compute_v2_missing_fields(
    order_prep: Dict[str, Any],
    *,
    brain_state: Optional[Dict[str, Any]] = None,
    whatsapp_phone: Optional[str] = None,
    db: Any = None,
    tenant_id: Optional[int] = None,
    conversation: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Ordered missing fields for V2 checkout. Phone is never listed."""
    if db is not None and tenant_id is not None:
        try:
            from core.order_missing_fields_engine import (  # noqa: PLC0415
                missing_fields_engine_enabled,
                resolve_flow_missing_fields,
            )

            if missing_fields_engine_enabled():
                engine_missing, engine_result = resolve_flow_missing_fields(
                    order_prep,
                    brain_state=brain_state,
                    whatsapp_phone=whatsapp_phone,
                    db=db,
                    tenant_id=tenant_id,
                    conversation=conversation,
                    inbound_metadata=inbound_metadata,
                )
                if engine_result is not None:
                    return engine_missing
        except Exception:  # noqa: BLE001
            logger.exception("[ORDER_FLOW_V2] missing_fields_engine resolve failed")

    bs = dict(brain_state or {})
    items = line_items_from_state(order_prep, bs)
    base = compute_wa_missing_fields(
        order_prep,
        brain_state=bs,
        whatsapp_phone=whatsapp_phone,
        line_items=items or None,
    )
    missing: List[str] = []
    if "product" in base:
        missing.append("product")
    if "customer_first_name" in base or "customer_last_name" in base:
        missing.append("customer_name")
    if "city" in base:
        missing.append("city")
    if "delivery_address" in base or not has_address_evidence(order_prep):
        missing.append("delivery_address")
    if not has_payment_method(order_prep):
        missing.append("payment_method")
    return missing


def next_missing_field(missing: List[str]) -> Optional[str]:
    for field in _V2_FIELD_ORDER:
        if field in missing:
            return field
    if "product" in missing:
        return "product"
    return None
