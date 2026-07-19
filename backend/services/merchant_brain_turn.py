"""Stateful boundary for the production merchant Brain turn.

This function passes the live SQLAlchemy session through to Brain and its
post-compose guards. Those transitive calls may write to the database or call
providers. It is therefore *not* a read-only evaluation API.

Do not use this boundary for internal evaluation unless the caller separately
enforces both a PostgreSQL read-only transaction and a process-level
provider/network firewall.
"""

from __future__ import annotations

import logging
import re as _re_signal
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEDUP_OVERLAP_THRESHOLD = 0.60
_DEDUP_HARD_OVERLAP_THRESHOLD = 0.85
_DEDUP_MIN_TOKENS = 6
_DEDUP_LOOKBACK_OUTBOUND = 2

_REPLY_SIGNAL_URL_RE = _re_signal.compile(r"https?://\S+", _re_signal.IGNORECASE)
_REPLY_SIGNAL_PHONE_RE = _re_signal.compile(
    r"(?:\+?966|00966|0)?5\d{8}|\+\d{7,15}",
)
_REPLY_SIGNAL_MARKERS = ("[MEDIA:", "[MEDIA_KEY:", "[PRODUCT:", "[CALL:")


def require_explicit_tenant_id(tenant_id: int | None) -> int:
    if tenant_id is None:
        raise ValueError("tenant_id is required")
    return int(tenant_id)


@dataclass
class LiveMerchantBrainPreconditions:
    brain_active: bool = True
    skip_ai: bool = False
    skip_reason: Optional[str] = None
    human_priority: bool = False
    billing_allowed: bool = True
    conversation_quota_allowed: bool = True
    outbound_lock_available: bool = True
    store_ai_allowed: bool = True
    ai_disabled: bool = False
    ai_disabled_reason: str = ""


@dataclass
class LiveMerchantBrainTurnInput:
    customer_phone: str
    text: str
    inbound_metadata: Optional[Dict[str, Any]] = None
    wa_msg_id: Optional[str] = None
    conversation_id: Optional[int] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    preconditions: LiveMerchantBrainPreconditions = field(
        default_factory=LiveMerchantBrainPreconditions
    )
    profile: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TextProvenance:
    compose_source: str = ""
    response_mode: str = ""
    chosen_path: str = ""
    llm_candidate_present: bool = False
    final_text_transformed: bool = False
    final_transform_reasons: List[str] = field(default_factory=list)
    reply_source: str = ""
    fallback_source: str = ""
    fallback_reason: str = ""
    fallback_action_type: str = ""


@dataclass
class LiveMerchantBrainTurnResult:
    status: str
    reply_text: str = ""
    brain_buttons: List[Any] = field(default_factory=list)
    brain_handoff: bool = False
    brain_result: Optional[Dict[str, Any]] = None
    provenance: TextProvenance = field(default_factory=TextProvenance)
    merchant_brain_enabled_fallback: bool = False
    billing_denied: bool = False
    brain_active: bool = True
    relational_moment: str = ""
    brain_nc_block: bool = False
    brain_nc_category: str = ""
    br_action: str = ""
    br_dec_action: str = ""
    br_dec_args: Dict[str, Any] = field(default_factory=dict)
    outbound_abort_suppressor: str = ""
    outbound_customer_id: Optional[int] = None
    brain_reply_candidate: str = ""
    brain_persona_compose_event: Optional[Dict[str, Any]] = None
    outbound_text_tracker: Any = None
    native_catalog_entry: Dict[str, Any] = field(default_factory=dict)
    brain_exception: Optional[BaseException] = None
    early_return: bool = False
    brain_silent: bool = False


def _dedup_tokenise(text: str) -> set:
    if not text:
        return set()
    import re as _re

    stripped = _re.sub(r"[\u064B-\u0652\u0670\u0640]", "", str(text))
    cleaned = _re.sub(r"[^\w\u0600-\u06FF]+", " ", stripped, flags=_re.UNICODE)
    return {token for token in cleaned.lower().split() if len(token) >= 2}


def max_outbound_overlap(new_reply: str, history: list) -> float:
    new_tokens = _dedup_tokenise(new_reply)
    if len(new_tokens) < _DEDUP_MIN_TOKENS:
        return 0.0
    best = 0.0
    checked = 0
    for turn in reversed(history or []):
        direction = str(turn.get("direction") or "").lower()
        if direction not in {"out", "outbound"}:
            continue
        prev_tokens = _dedup_tokenise(turn.get("body") or "")
        if not prev_tokens:
            continue
        overlap = len(new_tokens & prev_tokens) / float(len(new_tokens))
        best = max(best, overlap)
        checked += 1
        if checked >= _DEDUP_LOOKBACK_OUTBOUND:
            break
    return best


def reply_carries_new_signal(reply: str) -> bool:
    if not reply:
        return False
    if _REPLY_SIGNAL_URL_RE.search(reply):
        return True
    if _REPLY_SIGNAL_PHONE_RE.search(reply):
        return True
    return any(marker in reply for marker in _REPLY_SIGNAL_MARKERS)


def _dedup_operational_substitute(
    db,
    *,
    tenant_id: int,
    phone: str,
    history: list,
    inbound_text: str,
    inbound_metadata: dict | None,
    normalized_type: str | None,
) -> str:
    try:
        from core.order_flow import context_aware_dedup_fallback

        return context_aware_dedup_fallback(
            db,
            tenant_id=tenant_id,
            phone=phone,
            history=history,
            default_fallback="",
            inbound_text=inbound_text,
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CHAT_DEDUP] context-aware fallback failed: %s", exc)
        return ""


def _empty_reply_fallback() -> str:
    from core.fallback_policy import empty_reply_fallback

    return empty_reply_fallback()


def _build_persona_compose_event(brain_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    chosen_path = str(brain_result.get("chosen_path") or "").strip()
    persona_compose = brain_result.get("persona_compose")
    if (
        chosen_path == "fact_bound_persona_compose"
        and isinstance(persona_compose, dict)
        and persona_compose
    ):
        event = {
            "chosen_path": chosen_path,
            "persona_compose": dict(persona_compose),
        }
        for key in (
            "knowledge_source",
            "kb_section_ids",
            "question_kind",
            "catalog_product_id",
            "catalog_product_ids",
            "catalog_fact_products_len",
            "catalog_fact_product_ids",
            "catalog_fact_price_values",
            "catalog_fact_rebuild_source",
            "price_source",
            "availability_source",
            "checkout_pressure_allowed",
        ):
            if key in brain_result:
                event[key] = brain_result[key]
        if persona_compose.get("surface"):
            event["surface"] = persona_compose.get("surface")
        if persona_compose.get("source"):
            event["source"] = persona_compose.get("source")
        return event

    provenance_loaders = (
        (
            "trusted_coupon_offer_compose",
            "trusted_coupon_offer_compose_active",
            "modules.ai.brain.persona.trusted_coupon_offer_provenance",
        ),
        (
            "customer_conditional_coupon_compose",
            "customer_conditional_coupon_compose_active",
            "modules.ai.brain.persona.customer_conditional_coupon_provenance",
        ),
        (
            "customer_conditional_coupon_general_llm_fallthrough",
            "customer_conditional_coupon_general_llm_fallthrough",
            "modules.ai.brain.persona.customer_conditional_coupon_provenance",
        ),
        (
            ("general_offer_discovery_compose", "product_sale_offer_compose"),
            None,
            "modules.ai.brain.persona.product_sale_offer_provenance",
        ),
        (
            "track_order_need_order_number",
            "track_order_need_identifiers_compose_active",
            "modules.ai.brain.persona.track_order_need_identifiers_provenance",
        ),
    )
    for spec in provenance_loaders:
        if isinstance(spec[0], tuple):
            paths, flag, module_path = spec
            if chosen_path not in paths:
                continue
            if not (
                brain_result.get("general_offer_discovery_compose_active")
                or brain_result.get("product_sale_offer_compose_active")
            ):
                continue
        else:
            path, flag, module_path = spec
            if chosen_path != path or not brain_result.get(flag):
                continue
        try:
            import importlib

            mod = importlib.import_module(module_path)
            event = mod.extract_constitutional_metadata(brain_result)
            event["chosen_path"] = chosen_path
            return event
        except Exception:  # noqa: BLE001
            if isinstance(spec[0], tuple):
                return {
                    "chosen_path": chosen_path,
                    "general_offer_discovery_compose_active": bool(
                        brain_result.get("general_offer_discovery_compose_active")
                    ),
                    "product_sale_offer_compose_active": bool(
                        brain_result.get("product_sale_offer_compose_active")
                    ),
                }
            return {"chosen_path": chosen_path, spec[1]: True}
    return None


def _parse_brain_result(
    *,
    brain_result: Any,
    profile: Dict[str, Any],
    persona_ownership: Any,
) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "reply": "",
        "brain_buttons": [],
        "brain_handoff": False,
        "billing_denied": False,
        "relational_moment": "",
        "native_catalog_entry": {},
        "brain_reply_candidate": "",
        "outbound_customer_id": profile.get("id"),
        "brain_persona_compose_event": None,
        "outbound_text_tracker": None,
        "brain_nc_block": False,
        "brain_nc_category": "",
        "br_dec_action": "",
        "br_dec_args": {},
    }
    if not isinstance(brain_result, dict):
        state["reply"] = str(brain_result or "")
        return state

    billing_denied = (
        brain_result.get("skipped") and brain_result.get("reason") == "billing_access_denied"
    )
    state["billing_denied"] = billing_denied
    if billing_denied:
        return state

    reply = brain_result.get("reply", "") or ""
    state["reply"] = reply
    state["brain_reply_candidate"] = (reply or "").strip()
    state["brain_buttons"] = brain_result.get("buttons") or []
    state["native_catalog_entry"] = dict(brain_result.get("native_catalog_entry") or {})
    try:
        from core.native_catalog_fallback import defer_native_catalog_customer_reply

        reply = defer_native_catalog_customer_reply(
            reply,
            native_catalog_entry=state["native_catalog_entry"],
        )
        state["reply"] = reply
        state["brain_reply_candidate"] = (reply or "").strip()
    except Exception:  # noqa: silent-ok — native catalog defer is best-effort
        pass

    state["brain_handoff"] = bool(brain_result.get("handoff"))
    state["relational_moment"] = str(brain_result.get("relational_moment") or "").strip()
    if hasattr(persona_ownership, "merge_from_dict"):
        persona_ownership.merge_from_dict(brain_result.get("persona_ownership"))
    state["brain_persona_compose_event"] = _build_persona_compose_event(brain_result)
    state["brain_nc_block"] = bool(brain_result.get("non_commerce_block_mode"))
    state["brain_nc_category"] = str(brain_result.get("non_commerce_category") or "").strip()
    state["br_dec_action"] = str(brain_result.get("decision_action") or "")
    state["br_dec_args"] = dict(brain_result.get("decision_args") or {})
    try:
        from core.outbound_text_policy import OutboundTextTracker

        state["outbound_text_tracker"] = OutboundTextTracker.from_brain_result(brain_result)
    except Exception:  # noqa: BLE001
        state["outbound_text_tracker"] = None
    return state


def _apply_brain_silent_and_welcome_guards(
    *,
    db,
    tenant_id: int,
    to: str,
    text: str,
    convo: Any,
    reply: str,
    brain_result: Optional[Dict[str, Any]],
    billing_denied: bool,
    trace: Any,
    persona_ownership: Any,
) -> tuple[str, bool]:
    from modules.ai.brain.persona_ownership import PersonaBypassReason as POReason
    from services import turn_trace as TS

    brain_silent = False
    if billing_denied:
        trace.fallback_source = TS.SOURCE_BILLING_DENIED
        trace.response_goal = "silent"
        trace.reply_source = TS.SOURCE_BILLING_DENIED
        return reply, brain_silent

    if (reply or "").strip():
        return reply, brain_silent

    skip_silent_ack = bool(
        isinstance(brain_result, dict) and brain_result.get("shipment_claim_scrubbed_empty")
    )
    if not skip_silent_ack and isinstance(brain_result, dict):
        kb_pc = brain_result.get("persona_compose") or {}
        if (
            str(brain_result.get("chosen_path") or "") == "fact_bound_persona_compose"
            and str(kb_pc.get("surface") or "") == "kb_product_answer"
        ):
            skip_silent_ack = True
    if not skip_silent_ack:
        try:
            from modules.ai.brain.commerce.product_visual import is_product_visual_request

            skip_silent_ack = bool(is_product_visual_request(text or ""))
        except Exception:  # noqa: BLE001
            skip_silent_ack = False

    if skip_silent_ack:
        return reply, brain_silent

    brain_silent = True
    trace.brain_silent = True
    trace.fallback_source = "brain_silent_ack"
    trace.response_goal = "ack"
    persona_ownership.mark_bypass(POReason.BRAIN_SILENT_ACK, owner="brain_silent_ack")
    try:
        from modules.ai.brain.postprocess.conversation_recovery import try_guard_recovery_reply
        from core.order_flow import _load_brain_state

        conv_hist, _bs = _load_brain_state(db, tenant_id=tenant_id, phone=to)
        hist = list(getattr(conv_hist, "messages", None) or [])
        recovery = try_guard_recovery_reply(
            inbound_text=text or "",
            state=(convo.extra_metadata or {}).get("brain_state"),
            history=hist,
            tenant_id=tenant_id,
            db=db,
        )
        if recovery.reply:
            return recovery.reply, brain_silent
    except Exception:  # noqa: silent-ok — recovery uses registered emergency fallback
        pass
    return _empty_reply_fallback(), brain_silent


def _apply_outbound_dedup(
    *,
    db,
    tenant_id: int,
    to: str,
    text: str,
    reply: str,
    history: list,
    inbound_metadata: Optional[Dict[str, Any]],
    brain_handoff: bool,
    brain_active: bool,
    relational_moment: str,
    convo: Any,
    persona_ownership: Any,
    brain_persona_compose_event: Optional[Dict[str, Any]],
) -> tuple[str, str]:
    from modules.ai.brain.persona_ownership import PersonaBypassReason as POReason

    outbound_abort_suppressor = ""
    if not reply or brain_handoff:
        return reply, outbound_abort_suppressor

    po_reply_before_dedup = reply
    overlap = max_outbound_overlap(reply, history)
    is_hard = overlap >= _DEDUP_HARD_OVERLAP_THRESHOLD
    carries_signal = reply_carries_new_signal(reply)
    try:
        from modules.ai.brain.commerce.product_visual import is_product_visual_request

        visual_inbound = is_product_visual_request(text or "")
    except Exception:  # noqa: BLE001
        visual_inbound = False

    if is_hard and not carries_signal and not visual_inbound:
        skip_substitution = False
        try:
            from modules.ai.brain.commerce.fallback_guard import detect_hard_topic_shift

            if detect_hard_topic_shift(text or "", history=history):
                skip_substitution = True
        except Exception:  # noqa: BLE001
            skip_substitution = False

        if not skip_substitution:
            meta_for_dedup = dict(inbound_metadata or {}) if isinstance(inbound_metadata, dict) else None
            norm_type = str(
                (inbound_metadata or {}).get("source_type")
                or (inbound_metadata or {}).get("normalized_type")
                or ""
            ) or None
            reply = _dedup_operational_substitute(
                db,
                tenant_id=tenant_id,
                phone=to,
                history=history,
                inbound_text=text,
                inbound_metadata=meta_for_dedup,
                normalized_type=norm_type,
            )
            if not (reply or "").strip():
                outbound_abort_suppressor = "chat_dedup_hard"
                try:
                    from core.order_status_dedup_reply import build_dedup_local_order_short_reply
                    from modules.ai.brain.commerce.dedup_operational_delta import (
                        last_outbound_body,
                        should_restore_brain_reply_after_dedup_silence,
                    )

                    prev_outbound = last_outbound_body(history)
                    order_status_alt = build_dedup_local_order_short_reply(
                        db,
                        tenant_id=tenant_id,
                        phone=to,
                        conversation_id=getattr(convo, "id", None),
                        inbound_text=text or "",
                        previous_outbound=prev_outbound,
                    )
                    if order_status_alt:
                        reply = order_status_alt
                        outbound_abort_suppressor = ""
                    elif should_restore_brain_reply_after_dedup_silence(
                        current_inbound=text or "",
                        candidate_reply=po_reply_before_dedup,
                        previous_outbound=prev_outbound,
                    ):
                        reply = po_reply_before_dedup
                        outbound_abort_suppressor = ""
                    else:
                        reply = ""
                except Exception:  # noqa: BLE001
                    reply = ""
            else:
                persona_ownership.on_text_replaced(
                    layer="dedup_substitution",
                    reason=POReason.DEDUP_REPLY,
                    before=po_reply_before_dedup,
                    after=reply,
                )
                if isinstance(brain_persona_compose_event, dict):
                    try:
                        from modules.ai.brain.persona.customer_conditional_coupon_provenance import (
                            note_customer_conditional_coupon_dedup_substitution,
                        )
                        from modules.ai.brain.persona.product_sale_offer_provenance import (
                            note_product_sale_offer_dedup_substitution,
                        )
                        from modules.ai.brain.persona.track_order_need_identifiers_provenance import (
                            note_track_order_need_identifiers_dedup_substitution,
                        )
                        from modules.ai.brain.persona.trusted_coupon_offer_provenance import (
                            note_trusted_coupon_offer_dedup_substitution,
                        )

                        for note_substitution in (
                            note_trusted_coupon_offer_dedup_substitution,
                            note_customer_conditional_coupon_dedup_substitution,
                            note_product_sale_offer_dedup_substitution,
                            note_track_order_need_identifiers_dedup_substitution,
                        ):
                            note_substitution(
                                brain_persona_compose_event,
                                before=po_reply_before_dedup,
                                after=reply,
                            )
                    except Exception:  # noqa: silent-ok — provenance cannot block LIVE delivery
                        pass
    return reply, outbound_abort_suppressor


def _apply_post_compose_truth_guards(
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
    persona_ownership: Any,
) -> str:
    from modules.ai.brain.persona_ownership import PersonaBypassReason as POReason

    if not reply:
        return reply
    if brain_handoff:
        return _apply_staff_truth_guard_only(
            db=db,
            tenant_id=tenant_id,
            to=to,
            text=text,
            reply=reply,
            convo=convo,
            inbound_metadata=inbound_metadata,
            br_action=br_action,
            brain_persona_compose_event=brain_persona_compose_event,
        )

    po_reply_before_guards = reply
    try:
        from modules.ai.brain.postprocess.service_closer_guard import apply_service_closer_guard

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
        if scg.stripped:
            reply = scg.reply
            persona_ownership.on_text_replaced(
                layer="service_closer_guard",
                reason=POReason.FALLBACK_REPLY,
                before=po_reply_before_guards,
                after=reply,
            )
    except Exception:  # noqa: silent-ok — service closer guard is fail-open
        pass

    try:
        from core.payment_intent import rewrite_generic_reply_for_payment_context
        from core.order_flow import _focus_summary, _load_brain_state

        _, bs = _load_brain_state(db, tenant_id=tenant_id, phone=to)
        summary = _focus_summary(bs)
        rewritten = rewrite_generic_reply_for_payment_context(
            inbound_text=text or "",
            brain_reply=reply,
            state_summary=summary,
        )
        if rewritten:
            reply = rewritten
    except Exception:  # noqa: silent-ok — payment-context rewrite is fail-open
        pass

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
        prg_result = apply_payment_reply_guard(
            reply=reply,
            inbound_text=text or "",
            inbound_metadata=prg_meta,
            payment_receipt_received=bool(summary.get("payment_receipt_received")),
            tenant_id=tenant_id,
            conversation_id=getattr(convo, "id", None),
        )
        if prg_result.replaced:
            reply = prg_result.reply
    except Exception:  # noqa: silent-ok — payment truth guard is fail-open
        pass

    try:
        from core.active_order_context import load_commerce_bundle
        from modules.ai.brain.postprocess.shipment_truth_guard import apply_shipment_truth_guard
        from core.order_flow import _focus_summary, _load_brain_state

        _, bs = _load_brain_state(db, tenant_id=tenant_id, phone=to)
        summary = _focus_summary(bs)
        stg_meta = dict(inbound_metadata or {}) if isinstance(inbound_metadata, dict) else {}
        bundle = load_commerce_bundle(dict(getattr(convo, "extra_metadata", None) or {}))
        stg_result = apply_shipment_truth_guard(
            reply=reply,
            commerce_bundle=bundle,
            inbound_metadata=stg_meta,
            payment_receipt_received=bool(summary.get("payment_receipt_received")),
            tenant_id=tenant_id,
            conversation_id=getattr(convo, "id", None),
        )
        if stg_result.replaced:
            reply = stg_result.reply
    except Exception:  # noqa: silent-ok — shipment truth guard is fail-open
        pass

    try:
        from modules.ai.brain.postprocess.staff_escalation_truth_guard import (
            apply_staff_escalation_truth_guard,
        )
        from core.order_flow import _load_brain_state

        setg_meta = dict(inbound_metadata or {}) if isinstance(inbound_metadata, dict) else {}
        setg_path = str(setg_meta.get("deterministic_path") or br_action or "")
        _, setg_bs = _load_brain_state(db, tenant_id=tenant_id, phone=to)
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
            conversation_id=getattr(convo, "id", None),
            state=setg_bs,
        )
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
            )
            reply = tracking_templates.track_order_need_identifiers_emergency_fallback()
        elif setg_result.replaced and not setg_result.requires_grounded_compose:
            reply = setg_result.reply
    except Exception:  # noqa: silent-ok — staff truth guard is fail-open
        pass

    return reply


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
) -> str:
    """Preserve the live staff-truth guard for Brain handoff candidates."""
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
            conversation_id=getattr(convo, "id", None),
            state=brain_state,
        )
        if (
            result.requires_grounded_compose
            and result.route_action == "track_order_need_identifiers"
            and isinstance(brain_persona_compose_event, dict)
            and brain_persona_compose_event.get(
                "track_order_need_identifiers_compose_active"
            )
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
            )
            return tracking_templates.track_order_need_identifiers_emergency_fallback()
        if result.replaced and not result.requires_grounded_compose:
            return result.reply
    except Exception:  # noqa: silent-ok — staff truth guard is fail-open
        pass
    return reply


def _persist_live_handoff(
    *,
    db,
    tenant_id: int,
    to: str,
    text: str,
    convo: Any,
    profile: Dict[str, Any],
) -> None:
    payload = {
        "tenant_id": tenant_id,
        "phone": to,
        "text": text,
        "customer_name": profile.get("name") or to,
        "reason": "customer_request",
    }
    from handoff.manager import create_handoff_session

    create_handoff_session(
        db,
        tenant_id,
        to,
        payload["customer_name"],
        text,
        reason="customer_request",
    )
    convo.status = "human"
    convo.is_human_handoff = True
    convo.needs_human = True
    convo.handoff_active = True
    db.flush()


def _build_provenance(
    *,
    brain_result: Optional[Dict[str, Any]],
    brain_reply_candidate: str,
    reply_text: str,
    brain_persona_compose_event: Optional[Dict[str, Any]],
    trace: Any,
) -> TextProvenance:
    provenance = TextProvenance(
        compose_source=str((brain_result or {}).get("compose_source") or ""),
        response_mode=str(getattr(trace, "response_mode", "") or ""),
        chosen_path=str((brain_result or {}).get("chosen_path") or getattr(trace, "chosen_path", "") or ""),
        llm_candidate_present=bool(brain_reply_candidate),
        final_text_transformed=bool(
            brain_reply_candidate and (reply_text or "").strip() != brain_reply_candidate
        ),
        reply_source=str(getattr(trace, "reply_source", "") or ""),
        fallback_source=str(getattr(trace, "fallback_source", "") or ""),
    )
    if isinstance(brain_persona_compose_event, dict):
        provenance.compose_source = str(
            brain_persona_compose_event.get("compose_source")
            or provenance.compose_source
            or "persona_llm"
        )
        provenance.chosen_path = str(
            brain_persona_compose_event.get("chosen_path") or provenance.chosen_path
        )
        provenance.llm_candidate_present = bool(
            brain_persona_compose_event.get("llm_candidate_present", provenance.llm_candidate_present)
        )
    if provenance.compose_source in ("", "llm") and provenance.llm_candidate_present:
        provenance.compose_source = "persona_llm"
    return provenance


async def evaluate_live_merchant_brain_turn(
    *,
    db,
    tenant_id: int | None,
    phone_id: str,
    turn_input: LiveMerchantBrainTurnInput,
    convo: Any,
    trace: Any,
    persona_ownership: Any,
    brain_factory: Callable[[], Any],
    brain_active: bool = True,
    skip_reason: Optional[str] = None,
) -> LiveMerchantBrainTurnResult:
    """Run the live, stateful Brain/compose/guard segment.

    The supplied session is passed unchanged to transitive production code.
    This boundary does not block writes, provider calls, webhooks, automation,
    Salla operations, or financial actions.
    """
    explicit_tenant_id = require_explicit_tenant_id(tenant_id)
    pre = turn_input.preconditions

    if pre.ai_disabled or not pre.store_ai_allowed:
        return LiveMerchantBrainTurnResult(status="suppressed", early_return=True)
    if pre.skip_ai:
        return LiveMerchantBrainTurnResult(status="suppressed", early_return=True)
    if not pre.billing_allowed or not pre.conversation_quota_allowed:
        return LiveMerchantBrainTurnResult(
            status="billing_denied",
            billing_denied=True,
            early_return=True,
        )
    if not brain_active or not pre.brain_active:
        return LiveMerchantBrainTurnResult(status="legacy_path", brain_active=False)
    if not pre.outbound_lock_available:
        return LiveMerchantBrainTurnResult(status="outbound_locked", early_return=True)

    profile = dict(turn_input.profile or {})
    merchant_brain_enabled_fallback = False
    brain_result: Optional[Dict[str, Any]] = None

    try:
        brain = brain_factory()
        trace.brain_called = True
        human_priority_turn = skip_reason == "human_priority" or pre.human_priority
        brain_result = await brain.process(
            db=db,
            tenant_id=explicit_tenant_id,
            customer_phone=turn_input.customer_phone,
            message=turn_input.text,
            history=turn_input.history,
            profile=profile,
            customer_id=profile.get("id"),
            conversation_id=turn_input.conversation_id or getattr(convo, "id", None),
            human_priority=human_priority_turn,
        )
        parsed = _parse_brain_result(
            brain_result=brain_result,
            profile=profile,
            persona_ownership=persona_ownership,
        )
        reply = parsed["reply"]
        brain_handoff = parsed["brain_handoff"]
        billing_denied = parsed["billing_denied"]
        if not brain_handoff and (turn_input.text or "").strip():
            try:
                from modules.ai.brain.intent.rules import match as rules_match
                from modules.ai.brain.types import INTENT_TALK_HUMAN

                rule_intent = rules_match(turn_input.text or "")
                if (
                    rule_intent is not None
                    and getattr(rule_intent, "name", "") == INTENT_TALK_HUMAN
                    and float(getattr(rule_intent, "confidence", 0.0) or 0.0) >= 0.85
                ):
                    brain_handoff = True
            except Exception:  # noqa: silent-ok — handoff rule promotion is best-effort
                pass

        reply, brain_silent = _apply_brain_silent_and_welcome_guards(
            db=db,
            tenant_id=explicit_tenant_id,
            to=turn_input.customer_phone,
            text=turn_input.text,
            convo=convo,
            reply=reply,
            brain_result=brain_result if isinstance(brain_result, dict) else None,
            billing_denied=billing_denied,
            trace=trace,
            persona_ownership=persona_ownership,
        )

        if brain_handoff:
            _persist_live_handoff(
                db=db,
                tenant_id=explicit_tenant_id,
                to=turn_input.customer_phone,
                text=turn_input.text,
                convo=convo,
                profile=profile,
            )

        br_action = ""
        try:
            br_action = str(
                ((convo.extra_metadata or {}).get("brain_state") or {}).get("last_action") or ""
            )
        except Exception:  # noqa: BLE001
            br_action = ""

        reply, outbound_abort_suppressor = _apply_outbound_dedup(
            db=db,
            tenant_id=explicit_tenant_id,
            to=turn_input.customer_phone,
            text=turn_input.text,
            reply=reply,
            history=turn_input.history,
            inbound_metadata=turn_input.inbound_metadata,
            brain_handoff=brain_handoff,
            brain_active=True,
            relational_moment=parsed["relational_moment"],
            convo=convo,
            persona_ownership=persona_ownership,
            brain_persona_compose_event=parsed["brain_persona_compose_event"],
        )

        reply = _apply_post_compose_truth_guards(
            db=db,
            tenant_id=explicit_tenant_id,
            to=turn_input.customer_phone,
            text=turn_input.text,
            reply=reply,
            convo=convo,
            inbound_metadata=turn_input.inbound_metadata,
            brain_handoff=brain_handoff,
            brain_nc_block=parsed["brain_nc_block"],
            brain_nc_category=parsed["brain_nc_category"],
            br_action=br_action,
            brain_persona_compose_event=parsed["brain_persona_compose_event"],
            persona_ownership=persona_ownership,
        )

        if not billing_denied and not brain_silent and (reply or "").strip():
            from services import turn_trace as TS

            trace.reply_source = TS.SOURCE_BRAIN
            trace.response_goal = trace.response_goal or "answer"

        provenance = _build_provenance(
            brain_result=brain_result if isinstance(brain_result, dict) else None,
            brain_reply_candidate=parsed["brain_reply_candidate"],
            reply_text=reply,
            brain_persona_compose_event=parsed["brain_persona_compose_event"],
            trace=trace,
        )
        trace.handoff_triggered = bool(brain_handoff)

        return LiveMerchantBrainTurnResult(
            status="evaluated",
            reply_text=reply or "",
            brain_buttons=parsed["brain_buttons"],
            brain_handoff=brain_handoff,
            brain_result=brain_result if isinstance(brain_result, dict) else None,
            provenance=provenance,
            merchant_brain_enabled_fallback=merchant_brain_enabled_fallback,
            billing_denied=billing_denied,
            brain_active=True,
            relational_moment=parsed["relational_moment"],
            brain_nc_block=parsed["brain_nc_block"],
            brain_nc_category=parsed["brain_nc_category"],
            br_action=br_action,
            br_dec_action=parsed["br_dec_action"],
            br_dec_args=parsed["br_dec_args"],
            outbound_abort_suppressor=outbound_abort_suppressor,
            outbound_customer_id=parsed["outbound_customer_id"],
            brain_reply_candidate=parsed["brain_reply_candidate"],
            brain_persona_compose_event=parsed["brain_persona_compose_event"],
            outbound_text_tracker=parsed["outbound_text_tracker"],
            native_catalog_entry=parsed["native_catalog_entry"],
            brain_silent=brain_silent,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: silent-ok — rollback is best-effort after Brain failure
            pass
        return LiveMerchantBrainTurnResult(
            status="brain_exception",
            brain_exception=exc,
            brain_active=True,
        )
