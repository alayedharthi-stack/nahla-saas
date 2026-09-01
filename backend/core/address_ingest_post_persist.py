"""Rebuild the address-ingest reply from POST-PERSIST checkout state.

The webhook persists the location patch first. This owner then reloads
canonical checkout facts and decides ``next_missing_field``. Constrained
compose may phrase that decision; it must not choose the missing slot.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.address_ingest_post_persist")

_NAME_SLOTS = frozenset({
    "name",
    "full_name",
    "customer_name",
    "customer_first_name",
    "customer_last_name",
})
_PHONE_SLOTS = frozenset({
    "phone",
    "customer_phone",
    "customer_phone_number",
    "mobile",
})

_NEXT_GOAL_BY_FIELD = {
    "customer_name": "collect_customer_name_only",
    "city": "collect_city_only",
    "delivery_address": "collect_delivery_address_only",
    "payment_method": "collect_payment_method",
    "product": "collect_product",
}


def _prep_str(prep: Dict[str, Any], key: str) -> str:
    return str(prep.get(key) or "").strip()


def active_checkout_name_known(order_prep: Optional[Dict[str, Any]] = None) -> bool:
    prep = dict(order_prep or {})
    first = _prep_str(prep, "customer_first_name")
    last = _prep_str(prep, "customer_last_name")
    return bool(first and last)


def next_goal_for_missing_field(next_missing_field: Optional[str]) -> str:
    if not next_missing_field:
        return "none"
    return _NEXT_GOAL_BY_FIELD.get(str(next_missing_field), "none")


def reproject_address_ingest_decision_after_persist(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    inbound_text: str = "",
    address_type: str = "",
) -> Dict[str, Any]:
    """Reload post-persist checkout state and build the address-ack decision.

    Reuses existing identity, saved-address, and missing-fields owners.
    Does not persist a second identity write and does not choose wording.
    """
    from core.order_context_prefill import (  # noqa: PLC0415
        apply_saved_address_to_checkout_contract,
        checkout_location_evidence_known,
    )
    from core.order_flow import _focus_summary, _load_brain_state  # noqa: PLC0415
    from core.reply_instruction import (  # noqa: PLC0415
        attach_instruction_to_decision,
        build_address_instruction,
    )
    from core.wa_checkout_reply import compose_address_reply  # noqa: PLC0415
    from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
        merge_prep_with_customer_identity,
        resolve_catalog_checkout_customer_identity,
    )
    from modules.ai.order_flow_v2.missing_fields import (  # noqa: PLC0415
        compute_v2_missing_fields,
        next_missing_field,
    )

    conv, bs = _load_brain_state(db, tenant_id=int(tenant_id), phone=phone or "")
    op = dict(bs.get("order_prep") or bs.get("order_preparation") or {})
    summary = _focus_summary(bs)
    line_items = list(op.get("line_items") or bs.get("cart_items") or [])
    checkout_name_known = active_checkout_name_known(op)

    identity = resolve_catalog_checkout_customer_identity(
        db=db,
        tenant_id=int(tenant_id),
        phone=str(phone or ""),
        order_prep=op,
    )
    op_projected = merge_prep_with_customer_identity(op, identity)

    missing = compute_v2_missing_fields(
        op_projected,
        brain_state=bs,
        whatsapp_phone=str(phone or ""),
        db=db,
        tenant_id=int(tenant_id),
        conversation=conv,
    )

    order_context = None
    try:
        from core.order_context_builder import (  # noqa: PLC0415
            build_order_context as build_canonical_order_context,
        )

        order_context = build_canonical_order_context(
            db,
            tenant_id=int(tenant_id),
            conversation=conv,
            phone=str(phone or ""),
            brain_state=bs,
            message=str(inbound_text or ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ADDRESS_INGEST_POST_PERSIST] order_context load failed tenant=%s err=%s",
            tenant_id,
            exc,
        )

    known_facts: Dict[str, Any] = dict(identity.known_facts or {})
    known_facts["phone_known"] = True
    known_facts["phone_source"] = "whatsapp"
    known_facts["whatsapp_sender_valid_order_contact"] = True
    known_facts["active_checkout_name_known"] = checkout_name_known
    known_facts["checkout_location_evidence_known"] = checkout_location_evidence_known(
        op_projected
    )
    if line_items:
        known_facts["catalog_line_item_present"] = True
        known_facts["catalog_line_items_authoritative"] = bool(
            op_projected.get("catalog_line_items_authoritative")
        )
        first_item = line_items[0] if isinstance(line_items[0], dict) else {}
        known_facts["selected_product_id"] = (
            first_item.get("product_id") or first_item.get("product_retailer_id")
        )
        known_facts["selected_product_title"] = (
            first_item.get("product_name")
            or first_item.get("title")
            or first_item.get("name")
        )

    missing, known_facts = apply_saved_address_to_checkout_contract(
        missing_fields=missing,
        known_facts=known_facts,
        order_context=order_context,
        order_prep=op_projected,
    )
    if checkout_name_known:
        missing = [m for m in missing if m not in _NAME_SLOTS]
        known_facts["active_checkout_name_known"] = True
    missing = [m for m in missing if m not in _PHONE_SLOTS]
    known_facts["checkout_missing_fields"] = list(missing)

    nxt = next_missing_field(missing)
    next_goal = next_goal_for_missing_field(nxt)
    known_facts["next_missing_field"] = nxt or "none"
    known_facts["next_goal"] = next_goal
    known_facts["address_ack_scope"] = "delivery_only"
    for _pay_key in list(known_facts.keys()):
        if (
            str(_pay_key).startswith("payment_")
            or str(_pay_key).startswith("awaiting_payment")
            or str(_pay_key) in {"order_status", "payment_receipt_received"}
        ):
            known_facts.pop(_pay_key, None)
    if _prep_str(op_projected, "city"):
        known_facts["checkout_city"] = _prep_str(op_projected, "city")
    if _prep_str(op_projected, "district"):
        known_facts["checkout_district"] = _prep_str(op_projected, "district")
    if _prep_str(op_projected, "google_maps_url") or _prep_str(
        op_projected, "delivery_address_url"
    ):
        known_facts["checkout_maps_url"] = _prep_str(
            op_projected, "google_maps_url"
        ) or _prep_str(op_projected, "delivery_address_url")

    from core.merchant_payment_methods import load_merchant_payment_methods  # noqa: PLC0415

    payment_methods = load_merchant_payment_methods(db, int(tenant_id))
    reply_text = compose_address_reply(
        order_prep=op_projected,
        brain_state=bs,
        line_items=line_items,
        payment_methods=payment_methods,
        missing_fields=missing,
    )
    instruction = build_address_instruction(
        legacy_copy=reply_text,
        summary=summary,
        address_type=str(address_type or ""),
        inbound_text=str(inbound_text or ""),
        checkout_facts=known_facts,
        missing_fields=missing,
        next_missing_field=nxt,
    )
    return attach_instruction_to_decision(
        {
            "reply_text": reply_text,
            "summary": summary,
            "state_patch": {},
            "deterministic_path": "address_ingest_ack",
            "post_persist_reprojected": True,
            "missing_fields": list(missing),
            "next_missing_field": nxt,
            "next_goal": next_goal,
            "known_facts": known_facts,
        },
        instruction,
    )


def persist_address_ingest_turn_messages(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    inbound_body: str,
    outbound_body: str,
    inbound_event_type: str = "whatsapp_message",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Reuse ``StateManager.save_message`` like the map-image short-circuit."""
    from core.conversation_engine import StateManager  # noqa: PLC0415
    from core.order_flow import _load_brain_state  # noqa: PLC0415

    conv, _bs = _load_brain_state(db, tenant_id=int(tenant_id), phone=phone or "")
    conv_id = getattr(conv, "id", None) if conv is not None else None
    meta = dict(extra_metadata or {})
    inbound = str(inbound_body or "").strip()
    outbound = str(outbound_body or "").strip()
    if inbound:
        try:
            StateManager.save_message(
                db,
                phone=phone,
                direction="inbound",
                body=inbound,
                event_type=str(inbound_event_type or "whatsapp_message"),
                conversation_id=conv_id,
                tenant_id=int(tenant_id),
                extra_metadata={**meta, "address_ingest_short_circuit": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ADDRESS_INGEST_POST_PERSIST] inbound save failed tenant=%s err=%s",
                tenant_id,
                exc,
            )
    if outbound:
        try:
            StateManager.save_message(
                db,
                phone=phone,
                direction="outbound",
                body=outbound,
                event_type="whatsapp_message",
                conversation_id=conv_id,
                tenant_id=int(tenant_id),
                extra_metadata={
                    **meta,
                    "is_ai": True,
                    "deterministic_path": "address_ingest_ack",
                    "post_persist_reprojected": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ADDRESS_INGEST_POST_PERSIST] outbound save failed tenant=%s err=%s",
                tenant_id,
                exc,
            )


__all__ = [
    "active_checkout_name_known",
    "next_goal_for_missing_field",
    "persist_address_ingest_turn_messages",
    "reproject_address_ingest_decision_after_persist",
]
