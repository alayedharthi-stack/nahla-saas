"""Post-process deterministic OrderFlowV2 outbound replies before WhatsApp send."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.order_flow_v2.outbound_guards")


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
    #
    # This guard alone in this belt is FAIL-CLOSED. The others remove a
    # risk that is already in the text; skipping one leaves the text as
    # the composer wrote it. Skipping THIS one leaves an assertion that
    # the platform cannot support standing in front of the customer,
    # which is the one outcome the Claim Rule forbids outright. So a
    # failure anywhere under it — the operation reader, the evidence
    # query, the guard itself — establishes NO evidence rather than no
    # opinion, and the claim goes rather than the check.
    text = _apply_address_save_claim_guard(
        text,
        db=db,
        tenant_id=tenant_id,
        conversation=conversation,
        conversation_id=conversation_id,
        turn_ref=turn_ref,
        provenance_sink=provenance_sink,
    )

    _ = order_prep  # used by creation-claim guard above
    return text


# Set on the provenance sink when this boundary could not produce text it
# is able to stand behind. The send boundary refuses to deliver the turn
# rather than deliver an unverified save/adoption claim.
ADDRESS_CLAIM_SUPPRESS_KEY = "address_claim_send_suppressed"


def _record(sink: Optional[Dict[str, Any]], **fields: Any) -> None:
    if isinstance(sink, dict):
        sink.update(fields)


def _note_transform(sink: Optional[Dict[str, Any]], reason: str) -> None:
    if not isinstance(sink, dict):
        return
    sink["final_text_transformed"] = True
    reasons = [str(r) for r in (sink.get("final_transform_reasons") or []) if str(r).strip()]
    if reason not in reasons:
        reasons.append(reason)
    sink["final_transform_reasons"] = reasons


def _detect_address_save_claims(text: str) -> Tuple[Tuple[str, ...], bool]:
    """The claim kinds in this text, and whether detection actually ran."""
    try:
        from modules.ai.brain.postprocess.customer_address_save_claim_guard import (  # noqa: PLC0415
            detect_address_save_claim_kinds,
        )

        return tuple(detect_address_save_claim_kinds(text) or ()), True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — "cannot tell" is handled as "assume a claim" by the caller, which is the fail-closed direction
        return (), False


def _address_claim_evidence(
    db: Any, *, tenant_id: int, conversation: Any, turn_ref: str
) -> Any:
    """This turn's committed evidence, or None. Never raises.

    ``None`` means "nothing proven", never "probably fine": every failure
    below — unreadable persisted state, an unavailable database, an
    import error — lands here, and the caller treats it as support for
    nothing.
    """
    try:
        from core.customer_address_persistence_evidence import (  # noqa: PLC0415
            resolve_customer_address_persistence_evidence,
        )
        from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
            read_turn_address_operation,
        )

        attempt = read_turn_address_operation(conversation, turn_ref=turn_ref)
        return resolve_customer_address_persistence_evidence(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(getattr(conversation, "customer_id", 0) or 0) or None,
            attempt=attempt,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — no evidence is the safe reading; the caller removes the claim rather than trusting it
        logger.warning(
            "[ORDER_FLOW_V2] address claim evidence unavailable tenant=%s turn=%s",
            tenant_id,
            turn_ref,
            exc_info=True,
        )
        return None


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

    Never raises. The evidence is the one the WRITER published for this
    exact turn, read back and verified against committed state — never
    assembled here, or the boundary would be vouching for its own claim.

    A reply with no claim in it passes untouched even if everything below
    is unavailable: there is nothing to be wrong about. A reply WITH a
    claim needs that claim positively supported, so any failure removes
    it. If the removal leaves nothing to say, the turn is suppressed
    rather than answered — see ``ADDRESS_CLAIM_SUPPRESS_KEY``.
    """
    claims, detection_ran = _detect_address_save_claims(text)
    if detection_ran and not claims:
        _record(
            provenance_sink,
            address_save_claim_guard_action="allowed",
            address_save_claim_evidence_scope="not_required",
        )
        return text

    evidence = _address_claim_evidence(
        db, tenant_id=tenant_id, conversation=conversation, turn_ref=turn_ref
    )

    try:
        from modules.ai.brain.postprocess.customer_address_save_claim_guard import (  # noqa: PLC0415
            apply_customer_address_save_claim_guard,
        )

        guard = apply_customer_address_save_claim_guard(
            reply=text,
            evidence=evidence,
            tenant_id=int(tenant_id),
            conversation_id=conversation_id,
            # No composer is reachable on this pre-Brain path, so a
            # second composition cannot be attempted here. Saying
            # otherwise in the metadata would be the untruth this guard
            # exists to remove.
            allow_recompose=False,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — falls through to suppression below, which is stricter than passing the claim on
        guard = None

    if guard is None or not detection_ran:
        # The text may assert a save and nothing here can say whether it
        # is true. Sending it would be the platform guessing on its own
        # behalf.
        return _suppress_unverifiable_claim(
            provenance_sink,
            reason=("guard_unavailable" if guard is None else "claim_detection_unavailable"),
        )

    _record(
        provenance_sink,
        address_save_claim_guard_action=guard.action,
        address_save_claim_evidence_scope=guard.evidence_scope,
    )
    if not guard.replaced:
        return text
    if str(guard.reply or "").strip():
        _note_transform(provenance_sink, "customer_address_save_claim_guard")
        return str(guard.reply)

    # Removal emptied the reply. The approved generic emergency fallback
    # is NOT available here: its exception (EX-FALLBACK-GENERIC-001) is
    # scoped to a genuine LLM compose failure, and nothing on this path
    # composed anything. Borrowing its wording would mean recording a
    # model candidate and a recomposition that never happened. So the
    # turn is suppressed and the gap is measured instead of papered over.
    return _suppress_unverifiable_claim(
        provenance_sink, reason="removal_left_no_reply",
    )


def _suppress_unverifiable_claim(
    provenance_sink: Optional[Dict[str, Any]], *, reason: str
) -> str:
    """Refuse the turn rather than deliver an unsupported claim."""
    logger.error(
        "[ORDER_FLOW_V2] address save claim unverifiable, send suppressed reason=%s",
        reason,
    )
    _record(
        provenance_sink,
        **{
            ADDRESS_CLAIM_SUPPRESS_KEY: True,
            "address_save_claim_guard_action": "suppressed_unverifiable_address_save_claim",
            "address_save_claim_suppress_reason": reason,
            # Stated plainly because it is the whole point: nothing on
            # this path composed, so there is no model candidate and no
            # recomposition to report, and no approved fallback class to
            # borrow.
            "llm_candidate_present": False,
            "address_claim_compose_attempted": False,
        },
    )
    _note_transform(provenance_sink, "customer_address_save_claim_guard")
    return ""


__all__ = [
    "ADDRESS_CLAIM_SUPPRESS_KEY",
    "apply_order_flow_v2_outbound_guards",
]
