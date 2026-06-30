"""
payment_evidence_turn_route.py
──────────────────────────────
Current-turn payment receipt / transfer evidence ownership.

When inbound media is classified as bank transfer receipt evidence, catalog
browse/delivery and general image ack must not win over payment handling.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.brain.payment_evidence_turn")

TOPIC_PAYMENT_RECEIPT_RECEIVED = "payment_receipt_received"
TOPIC_PAYMENT_EVIDENCE_PENDING = "payment_evidence_pending_review"

_PAYMENT_EVIDENCE_STATUSES = frozenset({
    "confirmed",
    "needs_confirmation",
    "pre_transfer_review",
})

_PAYMENT_MEDIA_KINDS = frozenset({
    "payment_receipt",
    "payment_pre_review",
    "payment_pending_evidence",
})

_FORBIDDEN_CLAIMS = (
    "payment_verified_without_merchant",
    "catalog_push_during_receipt",
    "storefront_push_during_receipt",
    "shipping_promise_from_receipt",
    "invented_payment_confirmation",
)


def _inbound_metadata(ctx: Any) -> Dict[str, Any]:
    profile = getattr(ctx, "profile", None) or {}
    if not isinstance(profile, dict):
        return {}
    raw = profile.get("inbound_metadata") or {}
    try:
        from core.payment_media_metadata import flatten_inbound_payment_metadata  # noqa: PLC0415

        return flatten_inbound_payment_metadata(raw if isinstance(raw, dict) else {})
    except Exception:  # noqa: BLE001  # noqa: silent-ok — flatten is best-effort
        return dict(raw) if isinstance(raw, dict) else {}


def _normalized_inbound_type(ctx: Any, md: Dict[str, Any]) -> str:
    profile = getattr(ctx, "profile", None) or {}
    return str(
        md.get("normalized_type")
        or md.get("source_type")
        or (profile.get("inbound_normalized_type") if isinstance(profile, dict) else "")
        or ""
    ).strip().lower()


def _receipt_data_signals(md: Dict[str, Any]) -> bool:
    receipt_data = md.get("receipt_data")
    if isinstance(receipt_data, dict):
        if any(receipt_data.get(k) for k in (
            "amount",
            "beneficiary_name",
            "beneficiary_iban",
            "reference_number",
            "bank_name",
            "transfer_date",
        )):
            return True
    hints = md.get("payment_evidence_hints")
    if isinstance(hints, dict):
        if any(hints.get(k) for k in ("amount", "bank_name", "beneficiary_name", "reference_number")):
            return True
    return False


def inbound_metadata_has_payment_evidence(
    md: Optional[Dict[str, Any]],
    *,
    normalized_type: str = "",
) -> bool:
    """True when inbound metadata alone carries payment receipt / transfer evidence."""
    meta = dict(md or {})
    if not meta:
        return False

    norm_type = str(
        normalized_type
        or meta.get("normalized_type")
        or meta.get("source_type")
        or ""
    ).strip().lower()

    try:
        from core.payment_receipt_attachment_gate import has_inbound_attachment  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        has_inbound_attachment = lambda _t, _m: bool(_m.get("has_attached_media"))  # type: ignore[misc, assignment]

    if not has_inbound_attachment(norm_type, meta) and not meta.get("has_attached_media"):
        return False

    try:
        from modules.ai.brain.commerce.conversational_priority import (  # noqa: PLC0415
            is_receipt_inbound,
        )

        if is_receipt_inbound(meta, normalized_type=norm_type):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    pe = str(meta.get("payment_evidence_status") or "").strip().lower()
    if pe in _PAYMENT_EVIDENCE_STATUSES:
        return True

    kind = str(meta.get("pdf_kind") or meta.get("image_kind") or "").strip().lower()
    if kind in _PAYMENT_MEDIA_KINDS:
        return True

    sem = str(
        meta.get("media_semantic_category") or meta.get("semantic_category") or ""
    ).strip().lower()
    if sem in {"payment_receipt", "invoice"}:
        return True

    if meta.get("payment_receipt_detected") or meta.get("payment_receipt_short_circuit"):
        return True

    return _receipt_data_signals(meta)


def current_turn_has_payment_evidence(ctx: Any) -> bool:
    """True when current inbound carries payment receipt / transfer evidence."""
    md = _inbound_metadata(ctx)
    return inbound_metadata_has_payment_evidence(
        md,
        normalized_type=_normalized_inbound_type(ctx, md),
    )


def block_catalog_for_payment_evidence(ctx: Any) -> None:
    """Same-turn catalog block is enforced via ``current_turn_has_payment_evidence``.

    Payment receipt turns must not persist catalog blocks into later turns — the
    customer may explicitly request catalog delivery on the next message.
    """
    _ = ctx


def _summary_from_ctx(ctx: Any, md: Dict[str, Any]) -> Dict[str, Any]:
    state = getattr(ctx, "state", None)
    op = getattr(state, "order_prep", None) if state is not None else None
    prep: Dict[str, Any] = {}
    if op is not None:
        if hasattr(op, "to_dict"):
            try:
                prep = dict(op.to_dict() or {})
            except Exception:  # noqa: BLE001  # noqa: silent-ok
                prep = {}
        elif isinstance(op, dict):
            prep = dict(op)

    focus = dict(getattr(state, "current_product_focus", None) or {}) if state else {}
    summary: Dict[str, Any] = {
        "selected_product": str(
            prep.get("product_name")
            or prep.get("selected_product")
            or focus.get("title")
            or ""
        ),
        "selected_product_id": str(prep.get("product_id") or focus.get("id") or ""),
        "price": prep.get("catalog_checkout_total") or prep.get("price") or focus.get("price"),
        "currency": str(prep.get("currency") or focus.get("currency") or "SAR"),
        "city": str(prep.get("city") or ""),
        "short_address_code": str(prep.get("short_address_code") or ""),
        "google_maps_url": str(prep.get("google_maps_url") or ""),
        "customer_first_name": str(prep.get("customer_first_name") or ""),
        "customer_last_name": str(prep.get("customer_last_name") or ""),
        "missing_fields": list(prep.get("missing_fields") or []),
        "awaiting_payment_receipt": bool(prep.get("awaiting_payment_receipt")),
        "payment_receipt_received": bool(prep.get("payment_receipt_received")),
        "order_status": str(prep.get("order_status") or getattr(state, "stage", "") or ""),
        "payment_method": str(prep.get("payment_method") or ""),
    }

    try:
        from core.receipt_order_grounding import (  # noqa: PLC0415
            apply_receipt_grounding_to_summary,
            evaluate_receipt_order_grounding_from_state,
        )

        evidence = evaluate_receipt_order_grounding_from_state(state)
        summary = apply_receipt_grounding_to_summary(summary, evidence)
        parsed = md.get("receipt_data")
        if isinstance(parsed, dict) and parsed.get("amount") is not None:
            ev_dict = dict(summary.get("receipt_order_evidence") or {})
            ev_dict.setdefault("receipt_amount", parsed.get("amount"))
            summary["receipt_order_evidence"] = ev_dict
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok
        logger.debug("[PAYMENT_EVIDENCE_TURN] grounding skipped err=%s", exc)

    return summary


def _collect_allowed_facts(md: Dict[str, Any]) -> Dict[str, Any]:
    allowed: Dict[str, Any] = {}
    receipt_data = md.get("receipt_data")
    if isinstance(receipt_data, dict):
        for key in (
            "amount",
            "beneficiary_name",
            "beneficiary_iban",
            "bank_name",
            "reference_number",
            "transfer_date",
        ):
            if receipt_data.get(key) not in (None, ""):
                allowed[key] = receipt_data[key]
    hints = md.get("payment_evidence_hints")
    if isinstance(hints, dict):
        for key in ("amount", "bank_name", "beneficiary_name", "reference_number"):
            if hints.get(key) not in (None, "") and key not in allowed:
                allowed[key] = hints[key]
    pe = str(md.get("payment_evidence_status") or "").strip().lower()
    if pe:
        allowed["payment_evidence_status"] = pe
    kind = str(md.get("pdf_kind") or md.get("image_kind") or "").strip().lower()
    if kind:
        allowed["media_payment_kind"] = kind
    return allowed


def _compose_response_goal(
    *,
    route_kind: str,
    summary: Dict[str, Any],
    pending_review: bool,
) -> str:
    parts = [
        "PAYMENT RECEIPT compose principles: acknowledge receipt/evidence received; "
        "natural concise Saudi Arabic; no rigid templates; "
        "never confirm payment verified unless merchant/policy evidence exists; "
        "never push catalog/storefront/browse; never offer staff contact unprompted.",
        f"route_kind={route_kind}",
    ]
    if pending_review:
        parts.append(
            "pre-transfer or needs-review evidence — ask customer to send final receipt "
            "after transfer completes; do not mutate payment confirmed state."
        )
    elif route_kind == "needs_order_linking":
        parts.append(
            "receipt received but no confirmed order evidence — ask which order this "
            "transfer belongs to; do not mention stale browse/catalog products."
        )
    elif route_kind == "attach_receipt_to_active_order":
        parts.append(
            "receipt linked to active order evidence — acknowledge receipt and pending "
            "merchant review; include grounded order facts only when allowed_facts permit."
        )
    if summary.get("receipt_amount_mismatch"):
        parts.append("amount mismatch — needs merchant review before confirmation.")
    parts.append(
        "forbidden: payment verified claim, catalog push, shipping promise, invented facts."
    )
    return " | ".join(parts)


def try_payment_evidence_turn_decision(ctx: Any) -> Optional[Any]:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    if not current_turn_has_payment_evidence(ctx):
        return None

    md = _inbound_metadata(ctx)
    summary = _summary_from_ctx(ctx, md)
    allowed_facts = _collect_allowed_facts(md)

    try:
        from core.payment_evidence import PAYMENT_EVIDENCE_CONFIRMED, PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW  # noqa: PLC0415
        from core.payment_receipt_attachment_gate import (  # noqa: PLC0415
            blocks_receipt_received_ack,
            build_receipt_received_state_patch,
        )
        from core.receipt_order_grounding import compose_grounded_receipt_ack  # noqa: PLC0415
        from core.reply_instruction import build_payment_receipt_instruction  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok
        logger.debug("[PAYMENT_EVIDENCE_TURN] imports failed err=%s", exc)
        return None

    pe = str(md.get("payment_evidence_status") or "").strip().lower()
    pending_review = blocks_receipt_received_ack(md, summary=summary) or (
        pe == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW
    )

    has_active_order = bool(summary.get("can_mention_receipt_product")) or bool(
        summary.get("awaiting_payment_receipt"),
    )

    if pending_review:
        route_kind = "pending_merchant_review"
        topic = TOPIC_PAYMENT_EVIDENCE_PENDING
        state_patch: Dict[str, Any] = {}
        legacy = (
            "Payment evidence received but transfer completion is not confirmed yet. "
            "Ask the customer to send the final receipt after the transfer completes."
        )
    elif has_active_order:
        route_kind = "attach_receipt_to_active_order"
        topic = TOPIC_PAYMENT_RECEIPT_RECEIVED
        state_patch = (
            build_receipt_received_state_patch(inbound_metadata=md, source="brain_payment_evidence_turn")
            if pe == PAYMENT_EVIDENCE_CONFIRMED and not blocks_receipt_received_ack(md, summary=summary)
            else {}
        )
        legacy = compose_grounded_receipt_ack(summary)
    else:
        route_kind = "needs_order_linking"
        topic = TOPIC_PAYMENT_RECEIPT_RECEIVED
        state_patch = {}
        legacy = compose_grounded_receipt_ack(summary)

    instruction = build_payment_receipt_instruction(
        legacy_copy=legacy,
        summary=summary,
        inbound_text=str(getattr(ctx, "message", "") or ""),
    )

    logger.info(
        "[PAYMENT_EVIDENCE_TURN] tenant=%s route=%s topic=%s pending=%s active_order=%s",
        getattr(ctx, "tenant_id", None),
        route_kind,
        topic,
        pending_review,
        has_active_order,
    )

    args: Dict[str, Any] = {
        "topic": topic,
        "payment_receipt_route_kind": route_kind,
        "allowed_facts": allowed_facts,
        "forbidden_claims": list(_FORBIDDEN_CLAIMS),
        "block_catalog_escalation": True,
        "block_commerce_escalation": True,
        "block_storefront": True,
        "block_general_image_ack": True,
        "block_staff_contact": True,
        "payment_receipt_turn": True,
        "response_goal": _compose_response_goal(
            route_kind=route_kind,
            summary=summary,
            pending_review=pending_review,
        ),
        "reply_instruction": instruction.to_dict(),
    }
    if state_patch:
        args["state_patch"] = dict(state_patch)

    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason=f"payment_evidence_turn — {route_kind}",
        confidence=0.97,
    )


__all__ = [
    "TOPIC_PAYMENT_EVIDENCE_PENDING",
    "TOPIC_PAYMENT_RECEIPT_RECEIVED",
    "block_catalog_for_payment_evidence",
    "current_turn_has_payment_evidence",
    "inbound_metadata_has_payment_evidence",
    "try_payment_evidence_turn_decision",
]
