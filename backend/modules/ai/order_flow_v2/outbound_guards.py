"""Post-process deterministic OrderFlowV2 outbound replies before WhatsApp send."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def apply_order_flow_v2_outbound_guards(
    reply: str,
    *,
    db: Any,
    tenant_id: int,
    conversation_id: Optional[int] = None,
    order_prep: Optional[Dict[str, Any]] = None,
    known_facts: Optional[Dict[str, Any]] = None,
    missing_fields: Optional[List[str]] = None,
    conversation: Any = None,
    turn_ref: str = "",
    provenance_sink: Optional[Dict[str, Any]] = None,
) -> str:
    text = str(reply or "")
    if not text.strip():
        return text

    try:
        from modules.ai.brain.postprocess.payment_credential_guard import (  # noqa: PLC0415
            apply_payment_credential_guard,
        )

        pcg = apply_payment_credential_guard(
            text,
            db=db,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        if pcg.replaced:
            text = pcg.reply
    except Exception:  # noqa: BLE001  # noqa: silent-ok — outbound guard belt must not block V2 send
        pass

    try:
        from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
            sanitize_forbidden_catalog_name_question,
        )

        text = sanitize_forbidden_catalog_name_question(
            text,
            known_facts=known_facts,
            missing_fields=missing_fields,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — outbound guard belt must not block V2 send
        pass

    try:
        from modules.ai.brain.postprocess.saudi_dialect_guard import apply_saudi_dialect_guard  # noqa: PLC0415

        sdg = apply_saudi_dialect_guard(
            text,
            locale="ar",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        if sdg.replaced:
            text = sdg.reply
    except Exception:  # noqa: BLE001  # noqa: silent-ok — outbound guard belt must not block V2 send
        pass

    try:
        from modules.ai.brain.postprocess.order_creation_claim_guard import (  # noqa: PLC0415
            apply_order_creation_claim_guard,
        )

        occg = apply_order_creation_claim_guard(
            text,
            db=db,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            order_prep=order_prep,
            brain_state=None,
        )
        if occg.replaced:
            text = occg.reply
    except Exception:  # noqa: BLE001  # noqa: silent-ok — outbound guard belt must not block V2 send
        pass

    # The address truth guard belongs HERE, not only in the Brain
    # pipeline. This branch answers the customer and returns before the
    # shared post-compose boundary ever runs, so a reply produced here
    # claiming "تم حفظ عنوانك" met no address guard at all. It is the
    # same Claim Rule the shipment and payment slices enforce: the claim
    # stands only while committed evidence carries it.
    try:
        text = _apply_address_save_claim_guard(
            text,
            db=db,
            tenant_id=tenant_id,
            conversation=conversation,
            conversation_id=conversation_id,
            turn_ref=turn_ref,
            provenance_sink=provenance_sink,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — outbound guard belt must not block V2 send
        pass

    _ = order_prep  # used by creation-claim guard above
    return text


def _apply_address_save_claim_guard(
    text: str,
    *,
    db: Any,
    tenant_id: int,
    conversation: Any,
    conversation_id: Optional[int],
    turn_ref: str,
    provenance_sink: Optional[Dict[str, Any]],
) -> str:
    """Remove a save/adoption claim this turn's evidence cannot carry.

    The evidence is the one the WRITER published for this exact turn, read
    back and verified against committed state — never assembled here, or
    the boundary would be vouching for its own claim.

    When removal leaves nothing to say, the approved generic emergency
    fallback speaks instead, and the provenance records that the delivered
    text is the platform's rather than the composer's.
    """
    from modules.ai.brain.postprocess.customer_address_save_claim_guard import (  # noqa: PLC0415
        apply_address_claim_failed_compose_fallback,
        apply_customer_address_save_claim_guard,
        stamp_address_claim_fallback_provenance,
    )
    from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
        read_turn_address_operation,
    )

    attempt = read_turn_address_operation(conversation, turn_ref=turn_ref)
    evidence = None
    try:
        from core.customer_address_persistence_evidence import (  # noqa: PLC0415
            resolve_customer_address_persistence_evidence,
        )

        evidence = resolve_customer_address_persistence_evidence(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(getattr(conversation, "customer_id", 0) or 0) or None,
            attempt=attempt,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — no evidence is the safe reading, and the guard treats None as "carries nothing"
        evidence = None

    guard = apply_customer_address_save_claim_guard(
        reply=text,
        evidence=evidence,
        tenant_id=int(tenant_id),
        conversation_id=conversation_id,
        # No composer is reachable on this pre-Brain path, so a second
        # composition cannot be attempted here. Saying otherwise in the
        # metadata would be the untruth this guard exists to remove.
        allow_recompose=False,
    )
    if isinstance(provenance_sink, dict):
        provenance_sink["address_save_claim_guard_action"] = guard.action
        provenance_sink["address_save_claim_evidence_scope"] = guard.evidence_scope
    if not guard.replaced:
        return text
    if str(guard.reply or "").strip():
        if isinstance(provenance_sink, dict):
            provenance_sink["final_text_transformed"] = True
            reasons = list(provenance_sink.get("final_transform_reasons") or [])
            reasons.append("customer_address_save_claim_guard")
            provenance_sink["final_transform_reasons"] = reasons
        return str(guard.reply)
    # Removal emptied the reply: the turn still has to speak.
    fallback = apply_address_claim_failed_compose_fallback(provenance_sink)
    if isinstance(provenance_sink, dict):
        stamp_address_claim_fallback_provenance(provenance_sink)
    return fallback


__all__ = ["apply_order_flow_v2_outbound_guards"]
