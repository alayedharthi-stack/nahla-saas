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
    prep = dict(order_prep or {})
    identity_facts: Dict[str, Any] = {}
    if _catalog_checkout_prep(prep, inbound_metadata) and db is not None and tenant_id and whatsapp_phone:
        try:
            from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
                filter_missing_for_known_catalog_customer,
                merge_prep_with_customer_identity,
                resolve_catalog_checkout_customer_identity,
            )

            identity = resolve_catalog_checkout_customer_identity(
                db=db,
                tenant_id=int(tenant_id),
                phone=str(whatsapp_phone),
                order_prep=prep,
            )
            prep = merge_prep_with_customer_identity(prep, identity)
            identity_facts = dict(identity.known_facts or {})
        except Exception:  # noqa: BLE001
            logger.exception(
                "[ORDER_FLOW_V2] catalog customer identity prefill failed tenant=%s",
                tenant_id,
            )

    if db is not None and tenant_id is not None:
        try:
            from core.order_missing_fields_engine import (  # noqa: PLC0415
                missing_fields_engine_enabled,
                resolve_flow_missing_fields,
            )

            if missing_fields_engine_enabled():
                engine_missing, engine_result = resolve_flow_missing_fields(
                    prep,
                    brain_state=brain_state,
                    whatsapp_phone=whatsapp_phone,
                    db=db,
                    tenant_id=tenant_id,
                    conversation=conversation,
                    inbound_metadata=inbound_metadata,
                )
                if engine_result is not None:
                    if identity_facts:
                        from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
                            filter_missing_for_known_catalog_customer,
                        )

                        return filter_missing_for_known_catalog_customer(
                            engine_missing,
                            known_facts=identity_facts,
                            phone=str(whatsapp_phone or ""),
                        )
                    return engine_missing
        except Exception:  # noqa: BLE001
            logger.exception("[ORDER_FLOW_V2] missing_fields_engine resolve failed")

    bs = dict(brain_state or {})
    items = line_items_from_state(prep, bs)
    base = compute_wa_missing_fields(
        prep,
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
    if not has_payment_method(prep):
        missing.append("payment_method")
    return filter_missing_for_known_catalog_customer(
        missing,
        known_facts=identity_facts,
        phone=str(whatsapp_phone or ""),
    ) if identity_facts else missing


def _catalog_checkout_prep(
    order_prep: Dict[str, Any],
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    prep = dict(order_prep or {})
    if prep.get("catalog_line_items_authoritative"):
        return True
    meta = dict(inbound_metadata or {})
    if str(meta.get("source_type") or "").strip().lower() == "catalog_order":
        items = meta.get("product_items") or []
        if isinstance(items, list) and items:
            return True
    if prep.get("order_flow_v2_trusted_price") and (prep.get("line_items") or prep.get("cart_items")):
        return True
    return False


def next_missing_field(missing: List[str]) -> Optional[str]:
    for field in _V2_FIELD_ORDER:
        if field in missing:
            return field
    if "product" in missing:
        return "product"
    return None
