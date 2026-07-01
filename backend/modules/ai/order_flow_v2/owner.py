"""OrderFlowV2 owner — single deterministic entry for checkout/shipping turns."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.order_flow import apply_state_patch, _load_brain_state
from core.wa_native_catalog_order import (
    build_line_items_from_payload,
    parse_native_catalog_order,
)

from .contract import build_contract
from .checkout_context import (
    CheckoutReplyContext,
    apply_previous_address_confirmation,
    load_checkout_reply_context,
    load_identity_first_name,
)
from .flags import is_order_flow_v2_enabled, is_order_flow_v2_shadow_enabled
from .ingest import apply_inbound_slots
from .missing_fields import compute_v2_missing_fields
from .payment import (
    apply_payment_method_selection,
    build_payment_instruction_reply,
    default_payment_method_patch,
)
from .replies import (
    build_address_on_file_collect_reply,
    build_catalog_order_extraction_fallback_reply,
    build_catalog_order_start_reply,
    build_checkout_order_number_reply,
    build_greeting_checkout_resume_reply,
    build_greeting_with_pending_hint,
    build_next_field_reply,
    build_order_flow_product_keyword_reply,
    build_product_image_request_reply,
    build_resume_ack,
)
from .state import (
    activate_checkout_patch,
    checkout_active_now,
    deactivate_checkout_patch,
    incomplete_checkout_with_items,
    in_flight_catalog_checkout,
    line_items_from_state,
    mark_pending_patch,
    pending_order_exists,
    prep_dict,
    should_resume_checkout_on_greeting,
)
from .slot_ownership import (
    apply_slot_ownership,
    higher_priority_missing_before_payment,
    payment_attempt,
    stamp_last_field_patch,
)
from .triggers import (
    is_catalog_order_inbound,
    is_checkout_escape_inquiry,
    is_checkout_order_number_intent,
    is_explicit_purchase_intent,
    is_greeting_message,
    is_product_image_request_in_order_flow,
    is_resume_order_command,
    is_short_product_keyword_in_order_flow,
    is_whatsapp_order_browse_context,
    should_not_start_checkout,
)

logger = logging.getLogger("nahla.order_flow_v2")

_PRODUCT_MISSING_SLOTS = frozenset({
    "product",
    "products",
    "product_id",
    "variant",
    "quantity",
    "qty",
})


def _filter_catalog_missing(missing_fields: List[str]) -> List[str]:
    """Native catalog orders already carry product/quantity in the payload."""
    return [
        field
        for field in list(missing_fields or [])
        if str(field).strip().lower() not in _PRODUCT_MISSING_SLOTS
    ]


@dataclass
class OrderFlowV2Result:
    handled: bool = False
    reply: str = ""
    skip_brain: bool = False
    shadow_only: bool = False
    reason: str = ""
    state_patch: Dict[str, Any] = field(default_factory=dict)


def _catalog_order_patch(
    db: Any,
    *,
    tenant_id: int,
    inbound_metadata: Dict[str, Any],
    message: str = "",
) -> Dict[str, Any]:
    meta = dict(inbound_metadata or {})
    if message:
        meta.setdefault("_catalog_order_message", message)
    payload = parse_native_catalog_order(
        dict(meta.get("order") or {}),
        metadata=meta,
    )
    resolution = build_line_items_from_payload(db, int(tenant_id), payload)
    patch: Dict[str, Any] = {
        "line_items": list(resolution.line_items),
        "order_flow_v2_trusted_price": True,
        "order_flow_v2_pending": True,
        "catalog_line_items_authoritative": bool(resolution.line_items),
    }
    skus = [
        item.product_retailer_id
        for item in payload.items
        if str(item.product_retailer_id or "").strip()
    ]
    if skus:
        patch["catalog_skus"] = skus
    if payload.text_line_count:
        patch["catalog_order_line_count"] = int(payload.text_line_count)
    if payload.total_quantity:
        patch["quantity"] = int(payload.total_quantity)
        patch["catalog_total_quantity"] = int(payload.total_quantity)
    if payload.total_price is not None:
        patch["order_flow_v2_catalog_total"] = float(payload.total_price)
        patch["order_total"] = float(payload.total_price)
        if payload.currency:
            patch["order_flow_v2_currency"] = payload.currency
    if payload.items:
        total = 0.0
        currency = ""
        for item in payload.items:
            if item.item_price is not None:
                total += float(item.item_price) * int(item.quantity or 1)
            if item.currency:
                currency = item.currency
        if total > 0:
            patch["order_flow_v2_catalog_total"] = total
            patch["order_total"] = total
            if currency:
                patch["order_flow_v2_currency"] = currency
    first = next((li for li in resolution.line_items if isinstance(li, dict)), None)
    if isinstance(first, dict) and first.get("product_id"):
        patch["product_id"] = str(first.get("product_id"))
    if isinstance(first, dict) and first.get("quantity") and not patch.get("quantity"):
        patch["quantity"] = int(first.get("quantity") or 1)
    if payload.customer_note:
        patch.setdefault("address_line", payload.customer_note)
    expected_lines = int(payload.text_line_count or 0)
    actual_lines = len(payload.items or [])
    if (
        not resolution.line_items
        or int(getattr(resolution, "unmatched_count", 0) or 0) > 0
        or (expected_lines > 0 and actual_lines < expected_lines)
    ):
        patch["catalog_order_extraction_incomplete"] = True
    return patch


def _sync_draft_order(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    brain_state: Dict[str, Any],
) -> None:
    try:
        from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: PLC0415

        sync_nahla_wa_order(
            db,
            tenant_id=int(tenant_id),
            customer_phone=phone,
            brain_state=brain_state,
            trigger="order_flow_v2",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ORDER_FLOW_V2] draft sync failed tenant=%s: %s", tenant_id, exc)


def _finalize_result(
    *,
    enabled: bool,
    shadow: bool,
    reply: str,
    reason: str,
    state_patch: Dict[str, Any],
    skip_brain: bool = True,
) -> OrderFlowV2Result:
    if shadow and not enabled:
        logger.info(
            "[ORDER_FLOW_V2/shadow] reason=%s reply_preview=%r patch_keys=%s",
            reason,
            (reply or "")[:120],
            sorted(state_patch.keys()),
        )
        return OrderFlowV2Result(handled=False, shadow_only=True, reason=reason, state_patch=state_patch)
    return OrderFlowV2Result(
        handled=True,
        reply=reply,
        skip_brain=skip_brain,
        reason=reason,
        state_patch=state_patch,
    )


def try_handle_order_flow_v2(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    inbound_normalized_type: str = "text",
) -> OrderFlowV2Result:
    """Deterministic checkout owner. Returns handled=False for inquiry/general AI."""
    enabled = is_order_flow_v2_enabled()
    shadow = is_order_flow_v2_shadow_enabled()
    if not enabled and not shadow:
        return OrderFlowV2Result(handled=False)

    meta = dict(inbound_metadata or {})
    text = str(message or "").strip()
    conversation, brain_state = _load_brain_state(db, tenant_id=tenant_id, phone=customer_phone)
    order_prep = prep_dict((brain_state or {}).get("order_prep") or {})
    bs = dict(brain_state or {})
    patch: Dict[str, Any] = {}

    def _missing(prep: Dict[str, Any]) -> List[str]:
        return compute_v2_missing_fields(
            prep,
            brain_state=bs,
            whatsapp_phone=customer_phone,
            db=db,
            tenant_id=tenant_id,
            conversation=conversation,
            inbound_metadata=meta,
        )

    def _reply_ctx(prep: Dict[str, Any]) -> CheckoutReplyContext:
        return load_checkout_reply_context(
            db,
            tenant_id=tenant_id,
            conversation=conversation,
            customer_phone=customer_phone,
            order_prep=prep,
            brain_state=bs,
            inbound_metadata=meta,
        )

    if is_catalog_order_inbound(meta, text):
        try:
            patch.update(_catalog_order_patch(
                db,
                tenant_id=tenant_id,
                inbound_metadata=meta,
                message=text,
            ))
            patch.update(activate_checkout_patch())
            merged_prep = {**order_prep, **patch}
            missing = _filter_catalog_missing(
                [
                    m for m in _missing(merged_prep)
                    if m not in {"product", "products", "product_id", "variant", "quantity", "qty"}
                ]
            )
            try:
                from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
                    enrich_catalog_checkout_prep_and_missing,
                )

                merged_prep, missing, _identity_facts = enrich_catalog_checkout_prep_and_missing(
                    merged_prep,
                    list(missing),
                    db=db,
                    tenant_id=int(tenant_id),
                    phone=str(customer_phone or ""),
                )
                patch.update(
                    {
                        k: merged_prep[k]
                        for k in ("customer_first_name", "customer_last_name", "customer_phone")
                        if k in merged_prep
                    }
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[ORDER_FLOW_V2] catalog customer identity enrich failed tenant=%s",
                    tenant_id,
                )
            if patch.get("catalog_order_extraction_incomplete"):
                patch.update(build_contract(
                    decision="catalog_order_extraction_incomplete",
                    field="",
                    reason="catalog_order_text_extraction_incomplete",
                ).to_patch())
                reply = build_catalog_order_extraction_fallback_reply(order_prep=merged_prep)
                return _finalize_result(
                    enabled=enabled,
                    shadow=shadow,
                    reply=reply,
                    reason="catalog_order_extraction_incomplete",
                    state_patch=patch,
                )
            patch.update(stamp_last_field_patch(missing))
            patch.update(build_contract(
                decision="ask_missing_field",
                field=(patch.get("order_flow_v2_last_field") or ""),
                reason="catalog_order_start",
            ).to_patch())
            reply_ctx = _reply_ctx(merged_prep)
            reply = build_catalog_order_start_reply(
                order_prep=merged_prep,
                brain_state=bs,
                missing_fields=missing,
                field_modes=reply_ctx.field_modes,
                known_previous=reply_ctx.known_previous,
            )
            return _finalize_result(
                enabled=enabled,
                shadow=shadow,
                reply=reply,
                reason="catalog_order_start",
                state_patch=patch,
            )
        except Exception:
            logger.exception(
                "[ORDER_FLOW_V2] catalog_order path failed tenant=%s phone=%s",
                tenant_id,
                customer_phone,
            )
            return OrderFlowV2Result(
                handled=False,
                reason="catalog_order_v2_error",
            )

    if (
        is_product_image_request_in_order_flow(text)
        and is_whatsapp_order_browse_context(order_prep, bs, meta)
        and not is_greeting_message(text)
    ):
        reply = build_product_image_request_reply(order_prep=order_prep, brain_state=bs)
        return _finalize_result(
            enabled=enabled,
            shadow=shadow,
            reply=reply,
            reason="order_flow_product_image_request",
            state_patch={},
            skip_brain=True,
        )

    if (
        is_short_product_keyword_in_order_flow(text)
        and is_whatsapp_order_browse_context(order_prep, bs, meta)
        and not is_greeting_message(text)
        and not is_catalog_order_inbound(meta, text)
    ):
        reply = build_order_flow_product_keyword_reply(order_prep=order_prep)
        return _finalize_result(
            enabled=enabled,
            shadow=shadow,
            reply=reply,
            reason="order_flow_product_keyword",
            state_patch={},
            skip_brain=True,
        )

    def _address_on_file_claim(text_value: str) -> bool:
        from modules.ai.brain.commerce.commerce_turn_contract import is_address_on_file_claim  # noqa: PLC0415

        return is_address_on_file_claim(text_value)

    def _handle_greeting_checkout_resume() -> OrderFlowV2Result:
        if not checkout_active_now(order_prep):
            patch.update(activate_checkout_patch())
        merged_prep = {**order_prep, **patch}
        missing = _missing(merged_prep)
        reply_ctx = _reply_ctx(merged_prep)
        first_name = load_identity_first_name(
            db,
            tenant_id=tenant_id,
            conversation=conversation,
            customer_phone=customer_phone,
        )
        patch.update(stamp_last_field_patch(missing))
        patch.update(build_contract(
            decision="ask_missing_field",
            field=(patch.get("order_flow_v2_last_field") or ""),
            reason="greeting_checkout_resume",
        ).to_patch())
        reply = build_greeting_checkout_resume_reply(
            order_prep=merged_prep,
            brain_state=bs,
            missing_fields=missing,
            field_modes=reply_ctx.field_modes,
            known_previous=reply_ctx.known_previous,
            first_name=first_name,
            address_on_file_claim=False,
        )
        return _finalize_result(
            enabled=enabled,
            shadow=shadow,
            reply=reply,
            reason="greeting_checkout_resume",
            state_patch=patch,
        )

    if is_greeting_message(text):
        if should_resume_checkout_on_greeting(order_prep, bs):
            return _handle_greeting_checkout_resume()
        if pending_order_exists(order_prep, bs):
            first_name = load_identity_first_name(
                db,
                tenant_id=tenant_id,
                conversation=conversation,
                customer_phone=customer_phone,
            )
            reply = build_greeting_with_pending_hint(
                has_pending=True,
                first_name=first_name,
            )
            return _finalize_result(
                enabled=enabled,
                shadow=shadow,
                reply=reply,
                reason="greeting_pending_hint",
                state_patch={},
                skip_brain=True,
            )
        return OrderFlowV2Result(handled=False, reason="greeting_no_pending")

    if is_checkout_escape_inquiry(text, meta):
        try:
            from core.wa_address_ingestion import is_address_like_delivery_text  # noqa: PLC0415
            from .state import incomplete_checkout_with_items  # noqa: PLC0415

            if not (
                is_address_like_delivery_text(text)
                and incomplete_checkout_with_items(order_prep, bs)
            ):
                return OrderFlowV2Result(handled=False, reason="inquiry_escape")
        except Exception:  # noqa: BLE001
            return OrderFlowV2Result(handled=False, reason="inquiry_escape")

    if is_resume_order_command(text) and pending_order_exists(order_prep, bs):
        patch.update(activate_checkout_patch())
        merged_prep = {**order_prep, **patch}
        missing = _missing(merged_prep)
        patch.update(stamp_last_field_patch(missing))
        patch.update(build_contract(
            decision="ask_missing_field",
            field=(patch.get("order_flow_v2_last_field") or ""),
            reason="resume_checkout",
        ).to_patch())
        reply_ctx = _reply_ctx(merged_prep)
        reply = build_resume_ack(
            order_prep=merged_prep,
            brain_state=bs,
            missing_fields=missing,
            field_modes=reply_ctx.field_modes,
            known_previous=reply_ctx.known_previous,
        )
        return _finalize_result(
            enabled=enabled,
            shadow=shadow,
            reply=reply,
            reason="resume_checkout",
            state_patch=patch,
        )

    if is_explicit_purchase_intent(text) and not checkout_active_now(order_prep):
        if should_not_start_checkout(text, meta):
            return OrderFlowV2Result(handled=False, reason="purchase_blocked")
        if not line_items_from_state(order_prep, bs) and not order_prep.get("product_id"):
            return OrderFlowV2Result(handled=False, reason="purchase_no_product")
        patch.update(activate_checkout_patch())
        merged_prep = {**order_prep, **patch}
        missing = _missing(merged_prep)
        patch.update(stamp_last_field_patch(missing))
        patch.update(build_contract(
            decision="ask_missing_field",
            field=(patch.get("order_flow_v2_last_field") or ""),
            reason="explicit_purchase_start",
        ).to_patch())
        reply_ctx = _reply_ctx(merged_prep)
        reply = build_next_field_reply(
            order_prep=merged_prep,
            brain_state=bs,
            missing_fields=missing,
            field_modes=reply_ctx.field_modes,
            known_previous=reply_ctx.known_previous,
        )
        return _finalize_result(
            enabled=enabled,
            shadow=shadow,
            reply=reply,
            reason="explicit_purchase_start",
            state_patch=patch,
        )

    if is_checkout_order_number_intent(text) and in_flight_catalog_checkout(order_prep, bs):
        reply = build_checkout_order_number_reply(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            order_prep=order_prep,
            brain_state=bs,
        )
        if reply:
            active_patch: Dict[str, Any] = {}
            if not checkout_active_now(order_prep):
                active_patch.update(activate_checkout_patch())
            return _finalize_result(
                enabled=enabled,
                shadow=shadow,
                reply=reply,
                reason="checkout_order_number",
                state_patch=active_patch,
                skip_brain=True,
            )

    if not in_flight_catalog_checkout(order_prep, bs):
        try:
            from core.wa_address_ingestion import is_address_like_delivery_text  # noqa: PLC0415
            from .state import incomplete_checkout_with_items  # noqa: PLC0415

            if is_address_like_delivery_text(text) and incomplete_checkout_with_items(order_prep, bs):
                patch.update(activate_checkout_patch())
            else:
                if pending_order_exists(order_prep, bs):
                    patch.update(mark_pending_patch())
                return OrderFlowV2Result(handled=False, reason="not_active")
        except Exception:  # noqa: BLE001
            if pending_order_exists(order_prep, bs):
                patch.update(mark_pending_patch())
            return OrderFlowV2Result(handled=False, reason="not_active")

    if not checkout_active_now(order_prep):
        patch.update(activate_checkout_patch())

    on_file_claim = _address_on_file_claim(text)

    addr_confirm_patch = apply_previous_address_confirmation(
        db,
        tenant_id=tenant_id,
        conversation=conversation,
        customer_phone=customer_phone,
        order_prep=order_prep,
        brain_state=bs,
        inbound_metadata=meta,
        message=text,
    )
    if addr_confirm_patch:
        patch.update(addr_confirm_patch)

    pre_missing = _missing({**order_prep, **patch})
    owner_patch, owner_reason = apply_slot_ownership(
        message=text,
        order_prep=order_prep,
        missing_fields=pre_missing,
    )
    if owner_patch:
        patch.update(owner_patch)

    if owner_reason not in {
        "last_name_correction",
        "customer_name_owned",
        "city_owned",
        "city_uncertain",
        "address_refusal",
        "payment_before_address",
        "address_owned",
    }:
        slot_patch = apply_inbound_slots(
            message=text,
            inbound_normalized_type=inbound_normalized_type,
            inbound_metadata=meta,
            order_prep=order_prep,
        )
        if slot_patch:
            patch.update(slot_patch)

    merged_prep = {**order_prep, **patch}
    missing = _missing(merged_prep)

    if (
        "payment_method" in missing
        and not merged_prep.get("payment_method")
        and not higher_priority_missing_before_payment(missing)
        and not on_file_claim
    ):
        pay_patch, chosen = apply_payment_method_selection(db, tenant_id=tenant_id, message=text)
        if not pay_patch and not payment_attempt(text):
            pay_patch, chosen = default_payment_method_patch(db, tenant_id=tenant_id)
        if pay_patch:
            patch.update(pay_patch)
            merged_prep = {**merged_prep, **pay_patch}
            missing = _missing(merged_prep)

    if missing:
        patch.update(stamp_last_field_patch(missing))
        if "order_flow_v2_contract" not in patch:
            patch.update(build_contract(
                decision="ask_missing_field",
                field=(patch.get("order_flow_v2_last_field") or ""),
                reason=owner_reason or "collect_next_field",
            ).to_patch())

    if merged_prep.get("payment_method") and not missing and not on_file_claim:
        reply = build_payment_instruction_reply(
            db,
            tenant_id=tenant_id,
            order_prep=merged_prep,
            brain_state=bs,
            payment_method=str(merged_prep.get("payment_method") or ""),
        )
        return _finalize_result(
            enabled=enabled,
            shadow=shadow,
            reply=reply,
            reason="payment_instructions",
            state_patch=patch,
        )

    reply_ctx = _reply_ctx(merged_prep)
    if on_file_claim:
        reply = build_address_on_file_collect_reply(
            order_prep=merged_prep,
            brain_state=bs,
            missing_fields=missing,
            field_modes=reply_ctx.field_modes,
            known_previous=reply_ctx.known_previous,
        )
    else:
        reply = build_next_field_reply(
            order_prep=merged_prep,
            brain_state=bs,
            missing_fields=missing,
            field_modes=reply_ctx.field_modes,
            known_previous=reply_ctx.known_previous,
        )
    return _finalize_result(
        enabled=enabled,
        shadow=shadow,
        reply=reply,
        reason="collect_next_field",
        state_patch=patch,
    )


def persist_order_flow_v2_result(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    result: OrderFlowV2Result,
) -> None:
    if not result.state_patch:
        return
    apply_state_patch(
        db,
        tenant_id=tenant_id,
        phone=customer_phone,
        state_patch=result.state_patch,
    )
    _, bs = _load_brain_state(db, tenant_id=tenant_id, phone=customer_phone)
    _sync_draft_order(db, tenant_id=tenant_id, phone=customer_phone, brain_state=bs)
