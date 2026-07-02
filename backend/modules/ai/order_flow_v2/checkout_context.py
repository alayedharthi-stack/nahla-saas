"""Bridge OrderContext saved-address truth into OrderFlowV2 deterministic replies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.order_context_prefill import MODE_CONFIRM
from core.wa_order_lifecycle import has_accepted_delivery_address

from .missing_fields import compute_v2_missing_fields


@dataclass(frozen=True)
class CheckoutReplyContext:
    missing_fields: List[str]
    field_modes: Dict[str, str]
    known_previous: Dict[str, str]
    identity_first_name: str = ""


def _shipping_context_dict(previous: Any) -> Dict[str, str]:
    if previous is None:
        return {}
    return {
        "city": str(getattr(previous, "city", "") or "").strip(),
        "short_address": str(getattr(previous, "short_address", "") or "").strip(),
        "maps_url": str(getattr(previous, "maps_url", "") or "").strip(),
    }


def _identity_first_name(ctx: Any) -> str:
    identity = getattr(ctx, "identity", None)
    if identity is None:
        return ""
    first = str(getattr(identity, "first_name", "") or "").strip()
    if first:
        return first
    operational = str(getattr(identity, "operational_name", "") or "").strip()
    if operational:
        return operational.split()[0]
    return ""


def _engine_modes_to_v2(result: Any) -> Dict[str, str]:
    modes = dict(getattr(result, "missing_modes", None) or {})
    out: Dict[str, str] = {}
    if modes.get("name"):
        out["customer_name"] = str(modes["name"])
    if modes.get("city"):
        out["city"] = str(modes["city"])
    if modes.get("delivery_address"):
        out["delivery_address"] = str(modes["delivery_address"])
    return out


def _fallback_modes_from_known_previous(
    order_prep: Dict[str, Any],
    known_previous: Dict[str, str],
) -> Dict[str, str]:
    modes: Dict[str, str] = {}
    if known_previous.get("city") and not str(order_prep.get("city") or "").strip():
        modes["city"] = MODE_CONFIRM
    if (
        (known_previous.get("short_address") or known_previous.get("maps_url"))
        and not has_accepted_delivery_address(order_prep)
    ):
        modes["delivery_address"] = MODE_CONFIRM
    return modes


def load_checkout_reply_context(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> CheckoutReplyContext:
    """Load missing slots + confirm/ask modes from persisted customer/order context."""
    prep = dict(order_prep or {})
    bs = dict(brain_state or {})
    missing = compute_v2_missing_fields(
        prep,
        brain_state=bs,
        whatsapp_phone=customer_phone,
        db=db,
        tenant_id=tenant_id,
        conversation=conversation,
        inbound_metadata=inbound_metadata,
    )

    ctx = None
    known_previous: Dict[str, str] = {}
    field_modes: Dict[str, str] = {}
    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415
        from core.order_missing_fields_engine import resolve_flow_missing_fields  # noqa: PLC0415

        ctx = build_order_context(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            phone=str(customer_phone or ""),
            brain_state=bs,
            inbound_metadata=inbound_metadata,
            build_source="order_flow_v2_reply",
        )
        known_previous = _shipping_context_dict(getattr(ctx, "known_previous_address", None))
        _, engine_result = resolve_flow_missing_fields(
            prep,
            brain_state=bs,
            whatsapp_phone=customer_phone,
            db=db,
            tenant_id=tenant_id,
            conversation=conversation,
            inbound_metadata=inbound_metadata,
        )
        if engine_result is not None:
            missing = compute_v2_missing_fields(
                prep,
                brain_state=bs,
                whatsapp_phone=customer_phone,
                db=db,
                tenant_id=tenant_id,
                conversation=conversation,
                inbound_metadata=inbound_metadata,
            )
            field_modes = _engine_modes_to_v2(engine_result)
        elif known_previous:
            field_modes = _fallback_modes_from_known_previous(prep, known_previous)
    except Exception:  # noqa: BLE001
        if known_previous:
            field_modes = _fallback_modes_from_known_previous(prep, known_previous)

    first_name = _identity_first_name(ctx) if ctx is not None else ""
    return CheckoutReplyContext(
        missing_fields=list(missing),
        field_modes=field_modes,
        known_previous=known_previous,
        identity_first_name=first_name,
    )


def load_identity_first_name(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
) -> str:
    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415

        ctx = build_order_context(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            phone=str(customer_phone or ""),
            build_source="order_flow_v2_greeting",
        )
        return _identity_first_name(ctx)
    except Exception:  # noqa: BLE001
        return ""


def apply_previous_address_confirmation(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> Dict[str, Any]:
    """Promote saved customer address when the customer confirms previous/on-file address."""
    from core.order_context_builder import build_order_context  # noqa: PLC0415
    from core.order_context_prefill import detect_edit_intent_facts  # noqa: PLC0415
    from modules.ai.brain.commerce.commerce_turn_contract import is_address_on_file_claim  # noqa: PLC0415

    text = str(message or "").strip()
    if not text:
        return {}
    edit = detect_edit_intent_facts(text, order_prep)
    if not (edit.previous_address_confirmed or is_address_on_file_claim(text)):
        return {}

    ctx = build_order_context(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        phone=str(customer_phone or ""),
        brain_state=brain_state,
        inbound_metadata=inbound_metadata,
        message=text,
        build_source="order_flow_v2_address_claim",
    )
    if ctx.known_previous_address is None:
        return {}

    if edit.previous_address_confirmed:
        from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415

        if bool(getattr(ctx.shipping, "locked_by_merchant", False)):
            return {}
        if has_accepted_delivery_address(dict(order_prep or {})):
            return {}
        patch = _shipping_context_to_prep_patch(ctx.known_previous_address)
        patch["customer_confirmed_previous_address"] = True
        patch["shipping_source"] = "customer_confirmed_previous_address"
        return patch

    # On-file claim without explicit confirm phrase — reply layer confirms; do not auto-apply.
    return {}


def apply_delivery_continuation_address_patch(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Accept saved / evidenced address when customer asks delivery to their address."""
    from modules.ai.order_flow_v2.slot_ownership import promote_address_evidence_patch  # noqa: PLC0415

    prep = dict(order_prep or {})
    patch = promote_address_evidence_patch(prep)
    merged = {**prep, **patch}
    if has_accepted_delivery_address(merged):
        patch["customer_confirmed_previous_address"] = True
        patch["shipping_source"] = patch.get("shipping_source") or "delivery_continuation"
        return patch

    from core.order_context_builder import build_order_context  # noqa: PLC0415
    from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415

    ctx = build_order_context(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        phone=str(customer_phone or ""),
        brain_state=brain_state,
        inbound_metadata=inbound_metadata,
        message="",
        build_source="order_flow_v2_delivery_continuation",
    )
    if ctx.known_previous_address is None:
        return patch
    if bool(getattr(ctx.shipping, "locked_by_merchant", False)):
        return patch
    saved = _shipping_context_to_prep_patch(ctx.known_previous_address)
    saved["customer_confirmed_previous_address"] = True
    saved["shipping_source"] = "delivery_continuation_saved_address"
    patch.update(saved)
    return patch
