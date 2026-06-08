"""
core/payment_relevance_gate.py
──────────────────────────────
Platform-wide payment relevance gate.

Invariant
─────────
Unrelated multimodal inbound must NEVER resurrect stale payment
workflows or dispatch payment artifacts (barcode, IBAN, transfer
instructions, receipt reminders) without current-turn relevance.

All outbound payment paths must consult this module BEFORE:
  * dedup fallback substitution
  * replay / short-continuation payment flow
  * payment barcode routing
  * workflow resurrection (``awaiting_payment_receipt`` resume)
  * outbound override layers (artifact guard, payment-context rewrite)

This is architectural state-resurrection containment — not an
image-classifier or tenant-catalog tweak.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.payment_relevance_gate")

DISPATCH_WORKFLOW_RESUME = "workflow_resume"
DISPATCH_OUTBOUND_ARTIFACT = "outbound_artifact"
DISPATCH_EVIDENCE_PROMPT = "evidence_prompt"

_MEDIA_FRAME_RE = re.compile(
    r"^\[(?:"
    r"وصف\s*الصورة(?:\s*المرسلة)?|"
    r"وصف\s*الفيديو|"
    r"تصنيف\s*الصورة[^\]]*"
    r")\]\s*",
    re.UNICODE | re.IGNORECASE,
)
_CLASSIFICATION_TAG_RE = re.compile(
    r"^\[تصنيف\s*الصورة:[^\]]+\]\s*",
    re.UNICODE | re.IGNORECASE,
)

_NON_PAYMENT_MEDIA_SEMANTIC = frozenset({
    "religious_social_forward",
    "social_image",
    "unrelated_media",
    "unknown_media",
    "product_image",
    "map_location",
    "map_screenshot",
})

_SOCIAL_NON_COMMERCE = frozenset({
    "religious_media",
    "eid_greeting",
    "dua",
    "condolence",
    "social_image",
    "religious_social_forward",
})

_MEDIA_INBOUND_TYPES = frozenset({"image", "document", "pdf", "video"})

_PAYMENT_FUNNEL_SHORT_ACK = frozenset({
    "تم", "تمام", "اوكي", "ok", "ماشي", "موافق", "جاهز", "جاهزة", "جاهزه",
})

_RECEIPT_REMINDER_MARKERS = (
    "بانتظار إيصال التحويل",
    "بانتظار ايصال التحويل",
    "أرسله هنا (صورة أو PDF)",
    "ارسله هنا (صورة او pdf)",
)


@dataclass(frozen=True)
class PaymentRelevanceVerdict:
    allowed: bool
    reason: str = ""
    route: str = ""
    dispatch_kind: str = ""
    payment_semantics: bool = False
    media_relevant: bool = False
    workflow_fresh: bool = True
    topic_shift: bool = False
    visual_batch: bool = False
    social_context: bool = False
    payment_intent_strength: float = 0.0

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "route": self.route,
            "dispatch_kind": self.dispatch_kind,
            "payment_semantics": self.payment_semantics,
            "media_relevant": self.media_relevant,
            "workflow_fresh": self.workflow_fresh,
            "topic_shift": self.topic_shift,
            "visual_batch": self.visual_batch,
            "social_context": self.social_context,
            "payment_intent_strength": self.payment_intent_strength,
        }


@dataclass(frozen=True)
class PaymentRelevanceLogContext:
    """Optional correlation fields for the unified gate log line."""

    tenant_id: Any = None
    phone_tail: str = ""
    message: str = ""
    inbound_metadata: Optional[Dict[str, Any]] = None
    normalized_type: Optional[str] = None
    payment_intent_strength: Optional[float] = None
    dedup: bool = False
    fallback_source: str = ""
    artifact: Optional[bool] = None
    final_action: str = ""


def strip_media_framing(text: str) -> str:
    """Remove normalizer vision / classification prefixes for semantics."""
    s = (text or "").strip()
    while s:
        nxt = _MEDIA_FRAME_RE.sub("", s).strip()
        nxt = _CLASSIFICATION_TAG_RE.sub("", nxt).strip()
        if nxt == s:
            break
        s = nxt
    return s


def outbound_text_is_payment_artifact(reply_text: str) -> bool:
    """True when outbound copy is a payment artifact / receipt reminder."""
    blob = (reply_text or "").strip()
    if not blob:
        return False
    norm = blob.lower()
    if any(m.lower() in norm for m in _RECEIPT_REMINDER_MARKERS):
        return True
    try:
        from modules.ai.brain.decision.payment_barcode_routing import (  # noqa: PLC0415
            _PHONE_ONLY_REPLY_MARKERS,
            _norm as _pbr_norm,
        )
        pn = _pbr_norm(blob)
        if any(_pbr_norm(m) in pn for m in _PHONE_ONLY_REPLY_MARKERS):
            return True
    except Exception:  # noqa: BLE001
        pass
    payment_markers = (
        "iban", "ايبان", "آيبان", "رقم الحساب", "رقم حساب",
        "باركود", "الباركود", "qr", "كيو ار",
        "بعد التحويل أرسل", "بعد التحويل ارسل",
        "إيصال التحويل", "ايصال التحويل",
        "transfer receipt", "send the receipt",
    )
    return any(m in norm for m in payment_markers)


def _state_summary_from_state(state: Any) -> Dict[str, Any]:
    if state is None:
        return {}
    if isinstance(state, dict):
        op = state.get("order_prep") or state.get("order_preparation") or {}
        focus = state.get("current_product_focus") or {}
        title = ""
        if isinstance(focus, dict):
            title = str(focus.get("title") or "").strip()
        return {
            "awaiting_payment_receipt": bool(op.get("awaiting_payment_receipt")),
            "payment_receipt_received": bool(op.get("payment_receipt_received")),
            "selected_product": title or op.get("selected_product") or "",
            "order_status": str(op.get("order_status") or ""),
            "product_id": str(op.get("product_id") or ""),
        }
    op = getattr(state, "order_prep", None)
    focus = getattr(state, "current_product_focus", None) or {}
    title = ""
    if isinstance(focus, dict):
        title = str(focus.get("title") or "").strip()
    return {
        "awaiting_payment_receipt": bool(getattr(op, "awaiting_payment_receipt", False)),
        "payment_receipt_received": bool(getattr(op, "payment_receipt_received", False)),
        "selected_product": title or str(getattr(op, "selected_product", "") or ""),
        "order_status": str(getattr(op, "order_status", "") or ""),
        "product_id": str(getattr(op, "product_id", "") or ""),
    }


def _has_payment_semantics(message: str) -> bool:
    try:
        from modules.ai.brain.state.state_relevance import (  # noqa: PLC0415
            has_payment_semantics,
        )
        return has_payment_semantics(message)
    except Exception:  # noqa: BLE001
        return False


def _payment_intent_strength(
    message: str,
    *,
    state: Any = None,
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
) -> float:
    try:
        from modules.ai.brain.commerce.conversational_priority import (  # noqa: PLC0415
            detect_payment_intent_strength,
        )
        return detect_payment_intent_strength(
            message,
            state=state,
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
        ).strength
    except Exception:  # noqa: BLE001
        return 0.0


def _is_social_context(
    message: str,
    inbound_metadata: Optional[dict],
) -> bool:
    meta = inbound_metadata or {}
    nc = str(meta.get("non_commerce_category") or "").strip().lower()
    if nc in _SOCIAL_NON_COMMERCE:
        return True
    sem = str(
        meta.get("media_semantic_category")
        or meta.get("semantic_category")
        or ""
    ).strip().lower()
    if sem in {"religious_social_forward", "social_image"}:
        return True
    try:
        from modules.ai.brain.intent.social_classifier import classify_social  # noqa: PLC0415
        if classify_social(strip_media_framing(message)):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _media_semantic_category(inbound_metadata: Optional[dict]) -> str:
    meta = inbound_metadata or {}
    return str(
        meta.get("media_semantic_category")
        or meta.get("semantic_category")
        or ""
    ).strip().lower()


def _media_confidence(inbound_metadata: Optional[dict]) -> str:
    meta = inbound_metadata or {}
    return str(meta.get("media_semantic_confidence") or "").strip().lower()


def _is_multimodal_inbound(normalized_type: Optional[str]) -> bool:
    return str(normalized_type or "").strip().lower() in _MEDIA_INBOUND_TYPES


def _is_payment_funnel_short_ack(message: str) -> bool:
    """Colloquial readiness / ack while already in awaiting-receipt funnel."""
    n = _norm_short_ack(strip_media_framing(message))
    if not n:
        return False
    if n in _PAYMENT_FUNNEL_SHORT_ACK:
        return True
    return len(n.split()) <= 2 and n in _PAYMENT_FUNNEL_SHORT_ACK


def _norm_short_ack(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _recent_visual_batch_count(history: Optional[Sequence[Any]]) -> int:
    """Count recent customer turns that look like bare media descriptions."""
    if not history:
        return 0
    count = 0
    for item in list(history)[-6:]:
        body = ""
        direction = ""
        if isinstance(item, dict):
            body = str(item.get("content") or item.get("body") or item.get("text") or "")
            direction = str(item.get("direction") or item.get("role") or "")
        else:
            body = str(getattr(item, "content", "") or getattr(item, "body", "") or "")
            direction = str(getattr(item, "direction", "") or getattr(item, "role", "") or "")
        if direction and direction not in {"inbound", "user", "customer"}:
            continue
        stripped = strip_media_framing(body)
        meta = {}
        if isinstance(item, dict):
            meta = dict(item.get("extra_metadata") or item.get("metadata") or {})
        ntype = str(meta.get("normalized_type") or meta.get("source_type") or "").lower()
        if ntype in _MEDIA_INBOUND_TYPES or stripped != (body or "").strip():
            if not _has_payment_semantics(stripped) and not _has_payment_semantics(body):
                count += 1
    return count


def is_visual_batch_context(
    *,
    message: str = "",
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    history: Optional[Sequence[Any]] = None,
) -> bool:
    """Multimodal inbound without current-turn payment relevance."""
    meta = inbound_metadata or {}
    stripped = strip_media_framing(message)
    sem = _media_semantic_category(meta)
    pe = str(meta.get("payment_evidence_status") or "").strip().lower()
    kind = str(meta.get("pdf_kind") or meta.get("image_kind") or "")

    if pe in {"needs_confirmation", "pre_transfer_review"} and kind in {
        "payment_pre_review",
        "payment_pending_evidence",
    }:
        return False

    if sem in _NON_PAYMENT_MEDIA_SEMANTIC:
        if pe not in {"confirmed", "needs_confirmation", "pre_transfer_review"}:
            return True
        if not _has_payment_semantics(stripped) and not _has_payment_semantics(message):
            return True

    if _is_multimodal_inbound(normalized_type):
        if not _has_payment_semantics(stripped):
            caption = str(meta.get("caption") or "").strip()
            if not caption or not _has_payment_semantics(caption):
                if sem in _NON_PAYMENT_MEDIA_SEMANTIC or not sem:
                    if _media_confidence(meta) in {"low", ""} or sem in {
                        "unknown_media", "unrelated_media", "product_image",
                    }:
                        return True

    if _recent_visual_batch_count(history) >= 2 and _is_multimodal_inbound(normalized_type):
        if not _has_payment_semantics(stripped):
            return True

    return False


def _workflow_is_fresh(state_summary: Optional[dict], *, dispatch_kind: str) -> bool:
    s = state_summary or {}
    if dispatch_kind == DISPATCH_WORKFLOW_RESUME:
        if s.get("payment_receipt_received"):
            return False
        status = str(s.get("order_status") or "").lower()
        if status in {"under_review", "processing", "complete", "shipped", "delivered"}:
            return False
    return True


def _topic_shift_detected(
    message: str,
    *,
    history: Optional[Sequence[Any]] = None,
) -> bool:
    try:
        from modules.ai.brain.commerce.fallback_guard import (  # noqa: PLC0415
            detect_hard_topic_shift,
        )
        hist: List[Dict[str, Any]] = []
        for item in history or []:
            if isinstance(item, dict):
                hist.append(item)
            else:
                hist.append({
                    "content": getattr(item, "content", "") or getattr(item, "body", ""),
                    "direction": getattr(item, "direction", "") or getattr(item, "role", ""),
                })
        return bool(detect_hard_topic_shift(message, history=hist).detected)
    except Exception:  # noqa: BLE001
        return False


def _state_relevance_blocks_payment(
    message: str,
    state_summary: Optional[dict],
) -> bool:
    s = state_summary or {}
    if not s.get("awaiting_payment_receipt"):
        return False
    try:
        from modules.ai.brain.state.state_relevance import (  # noqa: PLC0415
            should_block_workflow_resume,
            validate_state_relevance_from_summary,
        )
        verdict = validate_state_relevance_from_summary(message=message, summary=s)
        return should_block_workflow_resume("payment_flow", verdict)
    except Exception:  # noqa: BLE001
        return False


def _is_receipt_inbound(
    inbound_metadata: Optional[dict],
    normalized_type: Optional[str],
) -> bool:
    try:
        from modules.ai.brain.commerce.conversational_priority import (  # noqa: PLC0415
            is_receipt_inbound,
        )
        return is_receipt_inbound(inbound_metadata, normalized_type=normalized_type)
    except Exception:  # noqa: BLE001
        return False


def _phone_tail(phone: Optional[str]) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits[-4:] if len(digits) >= 4 else ""


def _preview_text(message: str, *, limit: int = 80) -> str:
    s = strip_media_framing(message or "").replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _resolved_artifact_flag(
    verdict: PaymentRelevanceVerdict,
    context: Optional[PaymentRelevanceLogContext],
) -> bool:
    if not verdict.allowed:
        return False
    if context is not None and context.artifact is not None:
        return bool(context.artifact)
    return verdict.dispatch_kind in {
        DISPATCH_OUTBOUND_ARTIFACT,
        DISPATCH_WORKFLOW_RESUME,
        DISPATCH_EVIDENCE_PROMPT,
    }


def format_payment_relevance_gate_line(
    verdict: PaymentRelevanceVerdict,
    context: Optional[PaymentRelevanceLogContext] = None,
) -> str:
    """Single grep-friendly correlation line — observability only."""
    ctx = context or PaymentRelevanceLogContext()
    meta = dict(ctx.inbound_metadata or {})
    media_category = _media_semantic_category(meta) or "-"
    pe_status = str(meta.get("payment_evidence_status") or "-").strip() or "-"
    receipt_confidence = _media_confidence(meta) or "-"
    tenant = ctx.tenant_id if ctx.tenant_id is not None else "-"
    phone = ctx.phone_tail or "-"
    intent_strength = (
        ctx.payment_intent_strength
        if ctx.payment_intent_strength is not None
        else verdict.payment_intent_strength
    )
    preview = _preview_text(ctx.message)
    artifact = _resolved_artifact_flag(verdict, ctx)
    fallback = ctx.fallback_source or verdict.route or "-"
    final_action = ctx.final_action or "-"
    return (
        f"[PAYMENT_RELEVANCE_GATE] tenant={tenant} phone=*{phone} "
        f"kind={verdict.dispatch_kind or '-'} "
        f"allow={'true' if verdict.allowed else 'false'} "
        f"reason={verdict.reason or '-'} "
        f"media={media_category} "
        f"payment_evidence_status={pe_status} "
        f"receipt_confidence={receipt_confidence} "
        f"payment_semantics={'true' if verdict.payment_semantics else 'false'} "
        f"payment_intent_strength={intent_strength:.2f} "
        f"hard_topic_shift={'true' if verdict.topic_shift else 'false'} "
        f"visual_batch={'true' if verdict.visual_batch else 'false'} "
        f"dedup={'true' if ctx.dedup else 'false'} "
        f"fallback={fallback} "
        f"artifact={'true' if artifact else 'false'} "
        f"final_action={final_action} "
        f"preview={preview!r}"
    )


def evaluate_payment_relevance(
    *,
    message: str = "",
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    state_summary: Optional[dict] = None,
    state: Any = None,
    history: Optional[Sequence[Any]] = None,
    dispatch_kind: str = DISPATCH_WORKFLOW_RESUME,
    tenant_id: Any = None,
    route: str = "",
    log_context: Optional[PaymentRelevanceLogContext] = None,
) -> PaymentRelevanceVerdict:
    """Central gate — returns ``allowed=True`` only when all checks pass."""
    summary = state_summary if state_summary is not None else _state_summary_from_state(state)

    intent_message = message
    if dispatch_kind == DISPATCH_OUTBOUND_ARTIFACT:
        try:
            from modules.ai.brain.commerce.customer_origin_intent import (  # noqa: PLC0415
                extract_customer_origin_text,
            )
            intent_message, _ = extract_customer_origin_text(
                message,
                inbound_metadata=inbound_metadata,
                normalized_type=normalized_type,
            )
        except Exception:  # noqa: BLE001
            intent_message = message

    stripped = strip_media_framing(
        intent_message if dispatch_kind == DISPATCH_OUTBOUND_ARTIFACT else message
    )
    sem = _media_semantic_category(inbound_metadata)
    pe = str((inbound_metadata or {}).get("payment_evidence_status") or "").strip().lower()

    _sem_msg = intent_message if dispatch_kind == DISPATCH_OUTBOUND_ARTIFACT else message
    intent_strength = _payment_intent_strength(
        stripped or _sem_msg,
        state=state,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
    )
    payment_sem = (
        _has_payment_semantics(stripped)
        or _has_payment_semantics(_sem_msg)
        or intent_strength >= 0.65
    )
    social = _is_social_context(message, inbound_metadata)
    visual_batch = is_visual_batch_context(
        message=message,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
        history=history,
    )
    topic_shift = _topic_shift_detected(
        strip_media_framing(message) or message,
        history=history,
    )
    workflow_fresh = _workflow_is_fresh(summary, dispatch_kind=dispatch_kind)

    media_relevant = True
    if sem in _NON_PAYMENT_MEDIA_SEMANTIC and pe not in {
        "confirmed", "needs_confirmation", "pre_transfer_review",
    }:
        media_relevant = False
    if visual_batch and pe not in {
        "confirmed",
        "needs_confirmation",
        "pre_transfer_review",
    }:
        media_relevant = False

    def _emit_log(verdict: PaymentRelevanceVerdict) -> None:
        ctx = log_context or PaymentRelevanceLogContext()
        merged = PaymentRelevanceLogContext(
            tenant_id=ctx.tenant_id if ctx.tenant_id is not None else tenant_id,
            phone_tail=ctx.phone_tail,
            message=ctx.message or message,
            inbound_metadata=ctx.inbound_metadata or inbound_metadata,
            normalized_type=ctx.normalized_type or normalized_type,
            payment_intent_strength=(
                ctx.payment_intent_strength
                if ctx.payment_intent_strength is not None
                else intent_strength
            ),
            dedup=ctx.dedup,
            fallback_source=ctx.fallback_source,
            artifact=ctx.artifact,
            final_action=ctx.final_action,
        )
        log_payment_relevance_gate(verdict, merged)

    def _deny(reason: str) -> PaymentRelevanceVerdict:
        verdict = PaymentRelevanceVerdict(
            allowed=False,
            reason=reason,
            route=route,
            dispatch_kind=dispatch_kind,
            payment_semantics=payment_sem,
            media_relevant=media_relevant,
            workflow_fresh=workflow_fresh,
            topic_shift=topic_shift,
            visual_batch=visual_batch,
            social_context=social,
            payment_intent_strength=intent_strength,
        )
        _emit_log(verdict)
        return verdict

    if social:
        return _deny("social_context")

    short_payment_ack = (
        _is_payment_funnel_short_ack(stripped or message)
        and bool(summary.get("awaiting_payment_receipt"))
        and not visual_batch
        and not _is_multimodal_inbound(normalized_type)
    )

    if topic_shift and not payment_sem and not short_payment_ack:
        return _deny("hard_topic_shift")

    if not workflow_fresh and dispatch_kind == DISPATCH_WORKFLOW_RESUME:
        return _deny("stale_workflow")

    if (
        not short_payment_ack
        and _state_relevance_blocks_payment(stripped or message, summary)
    ):
        return _deny("state_relevance_not_payment")

    if dispatch_kind == DISPATCH_OUTBOUND_ARTIFACT:
        if _is_receipt_inbound(inbound_metadata, normalized_type):
            return _deny("receipt_inbound_no_outbound")
        if visual_batch and not payment_sem:
            return _deny("visual_batch_no_explicit_payment_ask")
        if not payment_sem:
            return _deny("no_current_turn_payment_semantics")

    elif dispatch_kind == DISPATCH_EVIDENCE_PROMPT:
        if (
            visual_batch
            and sem in _NON_PAYMENT_MEDIA_SEMANTIC
            and pe not in {
                "confirmed",
                "needs_confirmation",
                "pre_transfer_review",
            }
        ):
            return _deny("unrelated_multimodal_evidence")
        if not media_relevant and not payment_sem:
            return _deny("media_not_payment_relevant")
        awaiting = bool(summary.get("awaiting_payment_receipt"))
        has_order = bool(summary.get("selected_product") or summary.get("product_id"))
        if not awaiting and not has_order and not payment_sem:
            return _deny("no_active_payment_context")

    elif dispatch_kind == DISPATCH_WORKFLOW_RESUME:
        if visual_batch:
            return _deny("unrelated_multimodal_inbound")
        if _is_multimodal_inbound(normalized_type):
            if not payment_sem and not media_relevant:
                return _deny("multimodal_without_payment_semantics")
        elif not payment_sem and not bool(summary.get("awaiting_payment_receipt")):
            return _deny("no_active_payment_workflow")

    verdict = PaymentRelevanceVerdict(
        allowed=True,
        reason="ok",
        route=route,
        dispatch_kind=dispatch_kind,
        payment_semantics=payment_sem,
        media_relevant=media_relevant,
        workflow_fresh=workflow_fresh,
        topic_shift=topic_shift,
        visual_batch=visual_batch,
        social_context=social,
        payment_intent_strength=intent_strength,
    )
    _emit_log(verdict)
    return verdict


def validate_payment_workflow_resume(
    *,
    message: str = "",
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    state_summary: Optional[dict] = None,
    state: Any = None,
    history: Optional[Sequence[Any]] = None,
    tenant_id: Any = None,
    route: str = "",
    log_context: Optional[PaymentRelevanceLogContext] = None,
) -> PaymentRelevanceVerdict:
    return evaluate_payment_relevance(
        message=message,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
        state_summary=state_summary,
        state=state,
        history=history,
        dispatch_kind=DISPATCH_WORKFLOW_RESUME,
        tenant_id=tenant_id,
        route=route,
        log_context=log_context,
    )


def validate_payment_outbound_artifact(
    *,
    message: str = "",
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    state_summary: Optional[dict] = None,
    state: Any = None,
    history: Optional[Sequence[Any]] = None,
    tenant_id: Any = None,
    route: str = "",
    log_context: Optional[PaymentRelevanceLogContext] = None,
) -> PaymentRelevanceVerdict:
    return evaluate_payment_relevance(
        message=message,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
        state_summary=state_summary,
        state=state,
        history=history,
        dispatch_kind=DISPATCH_OUTBOUND_ARTIFACT,
        tenant_id=tenant_id,
        route=route,
        log_context=log_context,
    )


def validate_payment_evidence_prompt(
    *,
    message: str = "",
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    state_summary: Optional[dict] = None,
    state: Any = None,
    history: Optional[Sequence[Any]] = None,
    tenant_id: Any = None,
    route: str = "",
    log_context: Optional[PaymentRelevanceLogContext] = None,
) -> PaymentRelevanceVerdict:
    return evaluate_payment_relevance(
        message=message,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
        state_summary=state_summary,
        state=state,
        history=history,
        dispatch_kind=DISPATCH_EVIDENCE_PROMPT,
        tenant_id=tenant_id,
        route=route,
        log_context=log_context,
    )


def log_payment_relevance_gate(
    verdict: PaymentRelevanceVerdict,
    context: Optional[PaymentRelevanceLogContext] = None,
) -> None:
    """Emit one unified correlation line — never raises, never routes."""
    try:
        logger.info(format_payment_relevance_gate_line(verdict, context))
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "DISPATCH_EVIDENCE_PROMPT",
    "DISPATCH_OUTBOUND_ARTIFACT",
    "DISPATCH_WORKFLOW_RESUME",
    "PaymentRelevanceLogContext",
    "PaymentRelevanceVerdict",
    "evaluate_payment_relevance",
    "format_payment_relevance_gate_line",
    "is_visual_batch_context",
    "log_payment_relevance_gate",
    "outbound_text_is_payment_artifact",
    "strip_media_framing",
    "validate_payment_evidence_prompt",
    "validate_payment_outbound_artifact",
    "validate_payment_workflow_resume",
]
