"""Shared post-compose truth-guard pipeline for Brain (primary) and webhook (last-line)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional

from core.config import POST_COMPOSE_SINGLE_OWNER

logger = logging.getLogger("nahla.brain.postprocess.post_compose_guard_pipeline")

GuardMode = Literal["primary", "last_line"]

_TRUTH_GUARD_NAMES = (
    "service_closer_guard",
    "payment_context_rewrite",
    "payment_reply_guard",
    "shipment_truth_guard",
    "staff_escalation_truth_guard",
)


@dataclass
class PostComposeGuardEvent:
    guard: str
    acted: bool
    modified: bool
    suppressed_send: bool
    reason: str = ""
    layer: str = ""


@dataclass
class PostComposeGuardResult:
    reply: str
    events: list[PostComposeGuardEvent] = field(default_factory=list)
    primary_applied: bool = False


def _log_guard_event(
    *,
    tenant_id: int,
    conversation_id: Optional[int],
    layer: str,
    guard: str,
    acted: bool,
    modified: bool,
    suppressed_send: bool,
    reason: str = "",
) -> PostComposeGuardEvent:
    logger.info(
        "[POST_COMPOSE_GUARD] tenant=%s conversation_id=%s layer=%s guard=%s "
        "acted=%s modified=%s suppressed_send=%s reason=%s",
        tenant_id,
        conversation_id,
        layer,
        guard,
        acted,
        modified,
        suppressed_send,
        reason or "",
    )
    return PostComposeGuardEvent(
        guard=guard,
        acted=acted,
        modified=modified,
        suppressed_send=suppressed_send,
        reason=reason,
        layer=layer,
    )


def _emit_skip_events(
    *,
    tenant_id: int,
    conversation_id: Optional[int],
    layer: str,
) -> list[PostComposeGuardEvent]:
    return [
        _log_guard_event(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            layer=layer,
            guard=guard_name,
            acted=False,
            modified=False,
            suppressed_send=False,
            reason="skipped_primary_owner",
        )
        for guard_name in _TRUTH_GUARD_NAMES
    ]


def _note_live_text_mutation(
    tracker: Optional[Dict[str, Any]],
    *,
    reason_token: str,
    before: str,
    after: str,
) -> None:
    if not isinstance(tracker, dict):
        return
    if (before or "").strip() == (after or "").strip():
        return
    token = str(reason_token or "").strip()
    if not token:
        return
    reasons = tracker.get("final_transform_reasons")
    if not isinstance(reasons, list):
        reasons = []
    if token not in reasons:
        reasons.append(token)
    tracker["final_transform_reasons"] = reasons


def _apply_staff_truth_guard_only(
    *,
    db,
    tenant_id: int,
    to: str,
    text: str,
    reply: str,
    convo: Any,
    inbound_metadata: Optional[Dict[str, Any]],
    br_action: str,
    brain_persona_compose_event: Optional[Dict[str, Any]],
    live_provenance_tracker: Optional[Dict[str, Any]],
    layer: str,
    conversation_id: Optional[int],
    events: list[PostComposeGuardEvent],
    on_staff_guard_complete: Optional[Callable[..., None]] = None,
) -> str:
    guard_name = "staff_escalation_truth_guard"
    before_guard = reply
    modified = False
    suppressed_send = False
    try:
        from modules.ai.brain.postprocess.staff_escalation_truth_guard import (
            apply_staff_escalation_truth_guard,
        )
        from core.order_flow import _load_brain_state

        metadata = dict(inbound_metadata or {}) if isinstance(inbound_metadata, dict) else {}
        chosen_path = str(metadata.get("deterministic_path") or br_action or "")
        _, brain_state = _load_brain_state(db, tenant_id=tenant_id, phone=to)
        result = apply_staff_escalation_truth_guard(
            reply=reply,
            inbound_text=text or "",
            inbound_metadata=metadata,
            conversation_flags={
                "needs_human": bool(getattr(convo, "needs_human", False)),
                "handoff_active": bool(getattr(convo, "handoff_active", False)),
                "is_human_handoff": bool(getattr(convo, "is_human_handoff", False)),
                "status": str(getattr(convo, "status", "") or ""),
            },
            chosen_path=chosen_path,
            brain_handoff=True,
            tenant_id=tenant_id,
            conversation_id=conversation_id or getattr(convo, "id", None),
            state=brain_state,
        )
        if (
            result.requires_grounded_compose
            and result.route_action == "track_order_need_identifiers"
            and isinstance(brain_persona_compose_event, dict)
            and brain_persona_compose_event.get("track_order_need_identifiers_compose_active")
            and brain_persona_compose_event.get("llm_candidate_present")
        ):
            from modules.ai.brain.compose import templates as tracking_templates
            from modules.ai.brain.compose.track_order_need_identifiers_compose import (
                record_fallback_metadata_on_data,
            )

            record_fallback_metadata_on_data(
                brain_persona_compose_event,
                reason="staff_escalation_truth_guard_false_claim",
                transformed_by_guard=True,
                llm_candidate_present=True,
            )
            fallback_reply = tracking_templates.track_order_need_identifiers_emergency_fallback()
            _note_live_text_mutation(
                live_provenance_tracker,
                reason_token=guard_name,
                before=reply,
                after=fallback_reply,
            )
            reply = fallback_reply
            modified = True
        elif result.replaced and not result.requires_grounded_compose:
            _note_live_text_mutation(
                live_provenance_tracker,
                reason_token=guard_name,
                before=reply,
                after=result.reply,
            )
            reply = result.reply
            modified = True
        if on_staff_guard_complete is not None:
            try:
                on_staff_guard_complete(
                    reply=reply,
                    brain_state=brain_state,
                    inbound_metadata=metadata,
                    chosen_path=chosen_path,
                    setg_result=result,
                )
            except Exception:  # noqa: silent-ok — webhook ack emit must not block send
                pass
    except Exception:  # noqa: silent-ok — staff truth guard is fail-open
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                layer=layer,
                guard=guard_name,
                acted=False,
                modified=False,
                suppressed_send=False,
                reason="guard_exception",
            )
        )
        return reply

    events.append(
        _log_guard_event(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            layer=layer,
            guard=guard_name,
            acted=True,
            modified=modified,
            suppressed_send=suppressed_send,
        )
    )
    if modified and (before_guard or "").strip() != (reply or "").strip():
        pass  # telemetry already captured via modified=True
    return reply


def run_post_compose_truth_guards(
    *,
    db,
    tenant_id: int,
    to: str,
    text: str,
    reply: str,
    convo: Any,
    inbound_metadata: Optional[Dict[str, Any]],
    brain_handoff: bool,
    brain_nc_block: bool,
    brain_nc_category: str,
    br_action: str,
    brain_persona_compose_event: Optional[Dict[str, Any]],
    mode: GuardMode,
    primary_already_applied: bool = False,
    persona_ownership: Any = None,
    live_provenance_tracker: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[int] = None,
    on_staff_guard_complete: Optional[Callable[..., None]] = None,
) -> PostComposeGuardResult:
    """Run post-compose truth guards in primary (Brain) or last-line (webhook) mode."""
    layer = mode
    conv_id = conversation_id if conversation_id is not None else getattr(convo, "id", None)
    events: list[PostComposeGuardEvent] = []

    if not reply:
        return PostComposeGuardResult(reply=reply, events=events, primary_applied=False)

    should_skip = (
        mode == "last_line"
        and primary_already_applied
        and POST_COMPOSE_SINGLE_OWNER
    )
    if should_skip:
        events.extend(
            _emit_skip_events(tenant_id=tenant_id, conversation_id=conv_id, layer=layer)
        )
        return PostComposeGuardResult(reply=reply, events=events, primary_applied=False)

    primary_applied = mode == "primary"

    if brain_handoff:
        reply = _apply_staff_truth_guard_only(
            db=db,
            tenant_id=tenant_id,
            to=to,
            text=text,
            reply=reply,
            convo=convo,
            inbound_metadata=inbound_metadata,
            br_action=br_action,
            brain_persona_compose_event=brain_persona_compose_event,
            live_provenance_tracker=live_provenance_tracker,
            layer=layer,
            conversation_id=conv_id,
            events=events,
            on_staff_guard_complete=on_staff_guard_complete,
        )
        return PostComposeGuardResult(
            reply=reply,
            events=events,
            primary_applied=primary_applied,
        )

    po_reply_before_guards = reply

    # ── service_closer_guard ─────────────────────────────────────────────
    guard_name = "service_closer_guard"
    try:
        from modules.ai.brain.postprocess.service_closer_guard import apply_service_closer_guard
        from modules.ai.brain.persona_ownership import PersonaBypassReason as POReason

        nc_meta = dict(inbound_metadata or {}) if isinstance(inbound_metadata, dict) else {}
        if brain_nc_category:
            nc_meta.setdefault("non_commerce_category", brain_nc_category)
        if brain_nc_block:
            nc_meta.setdefault("block_commerce_escalation", True)
        scg = apply_service_closer_guard(
            reply,
            inbound_text=text or "",
            inbound_metadata=nc_meta,
            non_commerce_block_mode=brain_nc_block,
            block_commerce_escalation=brain_nc_block,
            tenant_id=tenant_id,
        )
        modified = bool(scg.stripped)
        if modified:
            reply = scg.reply
            if persona_ownership is not None:
                persona_ownership.on_text_replaced(
                    layer=guard_name,
                    reason=POReason.FALLBACK_REPLY,
                    before=po_reply_before_guards,
                    after=reply,
                )
            _note_live_text_mutation(
                live_provenance_tracker,
                reason_token=guard_name,
                before=po_reply_before_guards,
                after=reply,
            )
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=True,
                modified=modified,
                suppressed_send=False,
            )
        )
    except Exception:  # noqa: silent-ok — service closer guard is fail-open
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=False,
                modified=False,
                suppressed_send=False,
                reason="guard_exception",
            )
        )

    # ── payment_context_rewrite ────────────────────────────────────────────
    guard_name = "payment_context_rewrite"
    try:
        from core.payment_intent import rewrite_generic_reply_for_payment_context
        from core.order_flow import _focus_summary, _load_brain_state

        _, bs = _load_brain_state(db, tenant_id=tenant_id, phone=to)
        summary = _focus_summary(bs)
        before_rewrite = reply
        rewritten = rewrite_generic_reply_for_payment_context(
            inbound_text=text or "",
            brain_reply=reply,
            state_summary=summary,
        )
        modified = bool(rewritten)
        if modified:
            reply = rewritten
            _note_live_text_mutation(
                live_provenance_tracker,
                reason_token=guard_name,
                before=before_rewrite,
                after=reply,
            )
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=True,
                modified=modified,
                suppressed_send=False,
            )
        )
    except Exception:  # noqa: silent-ok — payment-context rewrite is fail-open
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=False,
                modified=False,
                suppressed_send=False,
                reason="guard_exception",
            )
        )

    # ── payment_reply_guard ────────────────────────────────────────────────
    guard_name = "payment_reply_guard"
    try:
        from modules.ai.brain.postprocess.payment_reply_guard import apply_payment_reply_guard
        from core.order_flow import _focus_summary, _load_brain_state

        _, bs = _load_brain_state(db, tenant_id=tenant_id, phone=to)
        summary = _focus_summary(bs)
        prg_meta = dict(inbound_metadata or {}) if isinstance(inbound_metadata, dict) else {}
        for key in (
            "awaiting_payment_receipt",
            "payment_receipt_received",
            "selected_product",
            "order_status",
            "payment_method",
        ):
            if key in summary:
                prg_meta[key] = summary[key]
        before_guard = reply
        prg_result = apply_payment_reply_guard(
            reply=reply,
            inbound_text=text or "",
            inbound_metadata=prg_meta,
            payment_receipt_received=bool(summary.get("payment_receipt_received")),
            tenant_id=tenant_id,
            conversation_id=conv_id,
        )
        modified = bool(prg_result.replaced)
        if modified:
            reply = prg_result.reply
            _note_live_text_mutation(
                live_provenance_tracker,
                reason_token=guard_name,
                before=before_guard,
                after=reply,
            )
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=True,
                modified=modified,
                suppressed_send=False,
            )
        )
    except Exception:  # noqa: silent-ok — payment truth guard is fail-open
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=False,
                modified=False,
                suppressed_send=False,
                reason="guard_exception",
            )
        )

    # ── shipment_truth_guard ───────────────────────────────────────────────
    guard_name = "shipment_truth_guard"
    try:
        from core.active_order_context import load_commerce_bundle
        from modules.ai.brain.postprocess.shipment_truth_guard import apply_shipment_truth_guard
        from core.order_flow import _focus_summary, _load_brain_state

        _, bs = _load_brain_state(db, tenant_id=tenant_id, phone=to)
        summary = _focus_summary(bs)
        stg_meta = dict(inbound_metadata or {}) if isinstance(inbound_metadata, dict) else {}
        bundle = load_commerce_bundle(dict(getattr(convo, "extra_metadata", None) or {}))
        before_guard = reply
        stg_result = apply_shipment_truth_guard(
            reply=reply,
            commerce_bundle=bundle,
            inbound_metadata=stg_meta,
            payment_receipt_received=bool(summary.get("payment_receipt_received")),
            tenant_id=tenant_id,
            conversation_id=conv_id,
        )
        modified = bool(stg_result.replaced)
        if modified:
            reply = stg_result.reply
            _note_live_text_mutation(
                live_provenance_tracker,
                reason_token=guard_name,
                before=before_guard,
                after=reply,
            )
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=True,
                modified=modified,
                suppressed_send=False,
            )
        )
    except Exception:  # noqa: silent-ok — shipment truth guard is fail-open
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=False,
                modified=False,
                suppressed_send=False,
                reason="guard_exception",
            )
        )

    # ── staff_escalation_truth_guard ─────────────────────────────────────
    guard_name = "staff_escalation_truth_guard"
    suppressed_send = False
    try:
        from modules.ai.brain.postprocess.staff_escalation_truth_guard import (
            apply_staff_escalation_truth_guard,
        )
        from core.order_flow import _load_brain_state

        setg_meta = dict(inbound_metadata or {}) if isinstance(inbound_metadata, dict) else {}
        setg_path = str(setg_meta.get("deterministic_path") or br_action or "")
        _, setg_bs = _load_brain_state(db, tenant_id=tenant_id, phone=to)
        before_guard = reply
        setg_result = apply_staff_escalation_truth_guard(
            reply=reply,
            inbound_text=text or "",
            inbound_metadata=setg_meta,
            conversation_flags={
                "needs_human": bool(getattr(convo, "needs_human", False)),
                "handoff_active": bool(getattr(convo, "handoff_active", False)),
                "is_human_handoff": bool(getattr(convo, "is_human_handoff", False)),
                "status": str(getattr(convo, "status", "") or ""),
            },
            chosen_path=setg_path,
            brain_handoff=bool(brain_handoff),
            tenant_id=tenant_id,
            conversation_id=conv_id,
            state=setg_bs,
        )
        modified = False
        if (
            setg_result.requires_grounded_compose
            and setg_result.route_action == "track_order_need_identifiers"
            and isinstance(brain_persona_compose_event, dict)
            and brain_persona_compose_event.get("track_order_need_identifiers_compose_active")
            and brain_persona_compose_event.get("llm_candidate_present")
        ):
            from modules.ai.brain.compose import templates as tracking_templates
            from modules.ai.brain.compose.track_order_need_identifiers_compose import (
                record_fallback_metadata_on_data,
            )

            record_fallback_metadata_on_data(
                brain_persona_compose_event,
                reason="staff_escalation_truth_guard_false_claim",
                transformed_by_guard=True,
                llm_candidate_present=True,
            )
            fallback_reply = tracking_templates.track_order_need_identifiers_emergency_fallback()
            _note_live_text_mutation(
                live_provenance_tracker,
                reason_token=guard_name,
                before=reply,
                after=fallback_reply,
            )
            reply = fallback_reply
            modified = True
        elif setg_result.replaced and not setg_result.requires_grounded_compose:
            _note_live_text_mutation(
                live_provenance_tracker,
                reason_token=guard_name,
                before=reply,
                after=setg_result.reply,
            )
            reply = setg_result.reply
            modified = True
        if on_staff_guard_complete is not None:
            try:
                on_staff_guard_complete(
                    reply=reply,
                    brain_state=setg_bs,
                    inbound_metadata=setg_meta,
                    chosen_path=setg_path,
                    setg_result=setg_result,
                )
            except Exception:  # noqa: silent-ok — webhook ack emit must not block send
                pass
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=True,
                modified=modified,
                suppressed_send=suppressed_send,
            )
        )
    except Exception:  # noqa: silent-ok — staff truth guard is fail-open
        events.append(
            _log_guard_event(
                tenant_id=tenant_id,
                conversation_id=conv_id,
                layer=layer,
                guard=guard_name,
                acted=False,
                modified=False,
                suppressed_send=False,
                reason="guard_exception",
            )
        )

    return PostComposeGuardResult(
        reply=reply,
        events=events,
        primary_applied=primary_applied,
    )
