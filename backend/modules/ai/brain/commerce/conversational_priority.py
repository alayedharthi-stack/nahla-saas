"""
Conversational priority gates — run BEFORE suppression / fallback replay.

Order (platform-wide, tenant-agnostic):
  1. Social / non-commerce (blocked when commerce_signal_strength is high)
  2. Short transactional continuation (continuation_mode + product focus)
  3. Payment outbound consent (semantic strength — receipt upload never consents)
  4. (Hard topic shift lives in fallback_guard — invoked by callers)
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.conversational_priority")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

SOCIAL_COMMERCE_BLOCK_THRESHOLD = 0.32
PAYMENT_CONSENT_STRENGTH_THRESHOLD = 0.65

# Gulf colloquial confirmations / send-it / deictic + label (tenant-agnostic).
_SHORT_TXN_RE = re.compile(
    r"^(?:"
    r"(?:ابغ|ابي|ابيك|أبغ|أبي|أبيك|ودي|حاب)(?:اه|ها|ه|ها)?|"
    r"(?:ابغ|ابي|ابيك|أبغ|أبي|أبيك).{0,16}(?:علام(?:ه|ة)?|مارك(?:ه|ة)?|براند|brand|تغليف(?:ه|ة)?)|"
    r"(?:هذا|هذي|هذه|نفسه|نفسها)(?:\s*(?:المطلوب|اللي\s*ابغ|اللي\s*ابي))?|"
    r"(?:ارسل|أرسل|ابعث|ابعت|خذ|خذه|خذها)(?:ه|ها|لي)?|"
    r"تمام|اوكي|ok|ماشي|موافق|"
    r"هذا\s*المطلوب|خذه|خذها"
    r")\s*$",
    re.IGNORECASE | re.UNICODE,
)

_GENERIC_CLARIFY_MARKERS = (
    "أي منتج تقصد",
    "أي منتج أو خدمة",
    "أي منتج تبغ",
    "تقصد حاجة أو مواصفة",
    "تقصد سعر كيلو أي منتج",
)

_COMMERCE_SIGNAL_PATTERNS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"(?<![ء-ي])بكم(?![ء-ي])"), 0.42),
    (re.compile(r"كم\s*(?:ال)?سعر|سعر|اسعار|أسعار"), 0.44),
    (re.compile(r"كيلو|جرام|حجم|مقاس|لتر"), 0.36),
    (re.compile(r"ورني|وريني|اعرض|أعرض|اريني|شوفني"), 0.46),
    (re.compile(r"(?:ابي|ابغى|أبي|أبغى|اريد|أريد)\s+\S{2,}"), 0.48),
    (re.compile(r"اطلب|أطلب|اشتري|أشتري"), 0.50),
    (re.compile(r"متوفر|موجود|متى\s*يتوفر"), 0.40),
    (re.compile(r"وين\s+(?:ال)?دفع|كيف\s+(?:ادفع|أدفع|احول)"), 0.45),
)

_PAYMENT_SEMANTIC_RE = re.compile(
    r"(?:"
    r"ارسل(?:وا)?\s*(?:لي\s+)?(?:ال)?حساب|"
    r"(?:ابي|ابغى|أبي|أبغى|ودي|حاب)\s+(?:احول|أحول|ادفع|أدفع)|"
    r"وين\s+(?:ال)?(?:دفع|تحويل)|"
    r"جاهز(?:ه|ة)?\s*(?:لل)?تحويل|"
    r"(?:حساب|رقم)\s*(?:ال)?بنك|"
    r"رقم\s*(?:ال)?حساب|"
    r"iban|"
    r"طريقة\s+(?:الدفع|التحويل)|"
    r"كيف\s+(?:ادفع|أدفع|احول|اسدد|الدفع|التحويل)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_CELEBRATION_PATTERNS = (
    re.compile(r"مبروك"),
    re.compile(r"تهانينا"),
    re.compile(r"تهنئ"),
    re.compile(r"congrat", re.I),
    re.compile(r"celebrat", re.I),
    re.compile(r"فرح(?:ه|ة)\s"),
    re.compile(r"بالتوفيق"),
    re.compile(r"بالسعاد(?:ه|ة)"),
    re.compile(r"كل\s*عام"),
    re.compile(r"عيد\s*مبار"),
)

_RECEIPT_KINDS = frozenset({
    "payment_receipt",
    "payment_pre_review",
    "payment_pending_evidence",
})

CONTINUATION_VARIANT_SELECTION = "variant_selection"
CONTINUATION_CHECKOUT = "checkout_confirmation"
CONTINUATION_PAYMENT_FLOW = "payment_flow"
CONTINUATION_DELIVERY = "delivery_resolution"
CONTINUATION_RECOMMENDATION = "recommendation"
CONTINUATION_PRODUCT = "product_assistance"


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )
    return _WS_RE.sub(" ", t).strip()


def inbound_type_label(
    *,
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    message: str = "",
) -> str:
    meta = inbound_metadata or {}
    for key in ("pdf_kind", "image_kind", "document_kind"):
        kind = str(meta.get(key) or "").strip().lower()
        if kind:
            return kind
    sem = str(meta.get("media_semantic_category") or meta.get("semantic_category") or "")
    if sem:
        return sem[:40]
    if normalized_type:
        return str(normalized_type)
    if (message or "").strip():
        return "text"
    return "empty"


def log_priority_correlation(
    *,
    tenant_id: Optional[int],
    route: str = "",
    inbound_type: str = "-",
    intent: str = "-",
    topic: str = "-",
    suppressed: str = "-",
    hard_shift: str = "-",
    continuation: str = "-",
    social: str = "-",
    payment_consent: str = "-",
    final_action: str = "-",
    media_sent: bool = False,
    reply_len: int = 0,
) -> None:
    """Flight-recorder line for production priority debugging."""
    logger.info(
        "[PRIORITY_CORRELATION] tenant=%s route=%s inbound_type=%s "
        "intent=%s topic=%s suppressed=%s hard_shift=%s continuation=%s "
        "social=%s payment_consent=%s final_action=%s media_sent=%s reply_len=%s",
        tenant_id if tenant_id is not None else "-",
        route or "-",
        inbound_type or "-",
        intent or "-",
        topic or "-",
        suppressed or "-",
        hard_shift or "-",
        continuation or "-",
        social or "-",
        payment_consent or "-",
        final_action or "-",
        str(bool(media_sent)).lower(),
        int(reply_len or 0),
    )


def log_short_continuation_detected(
    *,
    tenant_id: Optional[int],
    preview: str,
    focus_title: str = "",
    reason: str = "",
    continuation_mode: str = "",
) -> None:
    logger.info(
        "[SHORT_CONTINUATION_DETECTED] tenant=%s reason=%s mode=%s "
        "focus=%r preview=%r",
        tenant_id if tenant_id is not None else "-",
        reason or "-",
        continuation_mode or "-",
        (focus_title or "")[:60],
        (preview or "")[:80],
    )


def log_social_non_commerce_routed(
    *,
    tenant_id: Optional[int],
    category: str,
    source: str,
    preview: str,
    route: str = "",
) -> None:
    logger.info(
        "[SOCIAL_NON_COMMERCE_ROUTED] tenant=%s route=%s category=%s "
        "source=%s preview=%r",
        tenant_id if tenant_id is not None else "-",
        route or "-",
        category or "-",
        source or "-",
        (preview or "")[:80],
    )


def log_social_route_bypass(
    *,
    tenant_id: Optional[int],
    reason: str,
    commerce_strength: float,
    preview: str,
    route: str = "",
) -> None:
    logger.info(
        "[SOCIAL_ROUTE_BYPASS] tenant=%s route=%s reason=%s "
        "commerce_strength=%.2f preview=%r",
        tenant_id if tenant_id is not None else "-",
        route or "-",
        reason or "-",
        float(commerce_strength),
        (preview or "")[:80],
    )


def log_payment_outbound_attempt(
    *,
    tenant_id: Optional[int],
    request_kind: str,
    inbound_type: str = "",
    route: str = "",
) -> None:
    logger.info(
        "[PAYMENT_OUTBOUND_ATTEMPT] tenant=%s route=%s kind=%s inbound_type=%s",
        tenant_id if tenant_id is not None else "-",
        route or "-",
        request_kind or "-",
        inbound_type or "-",
    )


def log_payment_outbound_suppressed(
    *,
    tenant_id: Optional[int],
    reason: str,
    inbound_type: str = "",
    route: str = "",
) -> None:
    logger.info(
        "[PAYMENT_OUTBOUND_SUPPRESSED] tenant=%s route=%s reason=%s inbound_type=%s",
        tenant_id if tenant_id is not None else "-",
        route or "-",
        reason or "-",
        inbound_type or "-",
    )


def log_payment_consent_detected(
    *,
    tenant_id: Optional[int],
    source: str,
    strength: float,
    route: str = "",
) -> None:
    logger.info(
        "[PAYMENT_CONSENT_DETECTED] tenant=%s route=%s source=%s strength=%.2f",
        tenant_id if tenant_id is not None else "-",
        route or "-",
        source or "-",
        float(strength),
    )


def commerce_signal_strength(
    message: str,
    *,
    state: Any = None,
    intent_name: Optional[str] = None,
) -> float:
    """0..1 — high values mean social routing must not swallow commerce."""
    norm = _norm(message)
    if not norm:
        return 0.0

    strength = 0.0
    try:
        from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: PLC0415
            POSITIVE_COMMERCE_INTENTS,
            _has_strong_commerce,
        )
        if _has_strong_commerce(norm):
            return 1.0
        if intent_name in POSITIVE_COMMERCE_INTENTS:
            strength = max(strength, 0.62)
    except Exception:  # noqa: BLE001
        pass

    try:
        from modules.ai.brain.intent.social_classifier import (  # noqa: PLC0415
            _has_commercial_signal,
        )
        if _has_commercial_signal(message):
            strength = max(strength, 0.38)
    except Exception:  # noqa: BLE001
        pass

    for pat, weight in _COMMERCE_SIGNAL_PATTERNS:
        if pat.search(norm):
            strength = max(strength, weight)

    if state is not None:
        if getattr(state, "current_product_focus", None):
            strength = max(strength, 0.28)
        if list(getattr(state, "last_search_candidates", None) or []):
            strength = max(strength, 0.25)
        if str(getattr(state, "checkout_url", "") or "").strip():
            strength = max(strength, 0.35)
        stage = str(getattr(state, "stage", "") or "").lower()
        if stage in {"ordering", "checkout", "deciding"}:
            strength = max(strength, 0.40)

    return min(1.0, strength)


def infer_continuation_mode(
    state: Any,
    *,
    history: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """Active conversational mode — preserved before product title on short acks."""
    if state is None:
        return ""

    if bool(getattr(state, "awaiting_payment_receipt", False)):
        return CONTINUATION_PAYMENT_FLOW

    op = getattr(state, "order_prep", None)
    op_status = ""
    op_missing: List[str] = []
    if op is not None:
        op_status = str(getattr(op, "order_status", "") or "").lower()
        raw_missing = getattr(op, "missing_fields", None) or []
        if isinstance(raw_missing, list):
            op_missing = [str(x).lower() for x in raw_missing]

    if "payment" in op_status or "awaiting_payment" in op_status:
        return CONTINUATION_PAYMENT_FLOW

    if bool(getattr(state, "awaiting_variant_choice", False)):
        return CONTINUATION_VARIANT_SELECTION

    if bool(getattr(state, "awaiting_option_confirmation", False)):
        return CONTINUATION_CHECKOUT

    pending_opts = list(getattr(state, "options_pending", None) or [])
    if pending_opts:
        return CONTINUATION_VARIANT_SELECTION

    recent_topic = str(getattr(state, "recent_topic", "") or "").lower()
    if recent_topic in {"delivery_intent", "location_intent"}:
        return CONTINUATION_DELIVERY

    if any(f in op_missing for f in ("city", "address", "short_address", "location")):
        return CONTINUATION_DELIVERY

    stage = str(getattr(state, "stage", "") or "").lower()
    if stage in {"ordering", "checkout", "deciding"}:
        has_addr = False
        if op is not None:
            has_addr = bool(
                str(getattr(op, "city", "") or "").strip()
                or str(getattr(op, "short_address_code", "") or "").strip()
                or str(getattr(op, "google_maps_url", "") or "").strip()
            )
        if not has_addr:
            return CONTINUATION_DELIVERY
        return CONTINUATION_CHECKOUT

    cands = list(getattr(state, "last_search_candidates", None) or [])
    focus = getattr(state, "current_product_focus", None) or {}
    if len(cands) == 1 and isinstance(focus, dict) and focus.get("title"):
        return CONTINUATION_RECOMMENDATION

    if isinstance(focus, dict) and str(focus.get("title") or "").strip():
        return CONTINUATION_PRODUCT

    _ = history
    return ""


@dataclass(frozen=True)
class PaymentIntentVerdict:
    strength: float
    source: str = ""  # explicit | semantic | checkout_phase | blocked


def detect_payment_intent_strength(
    message: str,
    *,
    state: Any = None,
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
) -> PaymentIntentVerdict:
    if is_receipt_inbound(inbound_metadata, normalized_type=normalized_type):
        return PaymentIntentVerdict(0.0, "blocked")

    msg = (message or "").strip()
    norm = _norm(msg)

    try:
        from modules.ai.brain.decision.payment_barcode_routing import (  # noqa: PLC0415
            is_payment_barcode_image_request,
        )
        if is_payment_barcode_image_request(msg):
            return PaymentIntentVerdict(1.0, "explicit")
    except Exception:  # noqa: BLE001
        pass

    if _PAYMENT_SEMANTIC_RE.search(norm):
        return PaymentIntentVerdict(0.88, "semantic")

    try:
        from core.ai_libraries import is_payment_query  # noqa: PLC0415
        if is_payment_query(msg):
            return PaymentIntentVerdict(0.78, "semantic")
    except Exception:  # noqa: BLE001
        pass

    # Short readiness phrases during active payment funnel (not receipt upload).
    if state is not None and bool(getattr(state, "awaiting_payment_receipt", False)):
        if norm in {"تم", "تمام", "جاهز", "جاهزه", "جاهزة", "حولت", "تم التحويل"}:
            return PaymentIntentVerdict(0.0, "blocked")

    if state is not None:
        stage = str(getattr(state, "stage", "") or "").lower()
        checkout_url = str(getattr(state, "checkout_url", "") or "").strip()
        if stage == "checkout" and checkout_url and _PAYMENT_SEMANTIC_RE.search(norm):
            return PaymentIntentVerdict(0.72, "checkout_phase")

    return PaymentIntentVerdict(0.0, "")


def is_receipt_inbound(
    inbound_metadata: Optional[dict] = None,
    *,
    normalized_type: Optional[str] = None,
) -> bool:
    meta = inbound_metadata or {}
    for key in ("pdf_kind", "image_kind", "document_kind", "media_kind"):
        if str(meta.get(key) or "").strip().lower() in _RECEIPT_KINDS:
            return True
    sem = str(
        meta.get("media_semantic_category")
        or meta.get("semantic_category")
        or ""
    ).lower()
    if "payment_receipt" in sem:
        return True
    if meta.get("payment_receipt_detected") or meta.get("payment_receipt_short_circuit"):
        return True
    if normalized_type in {"document", "image"}:
        if str(meta.get("pdf_kind") or meta.get("image_kind") or "").lower() in _RECEIPT_KINDS:
            return True
        if "receipt" in sem and "payment" in sem:
            return True
    return False


def has_payment_outbound_consent(
    customer_msg: str,
    *,
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    tenant_id: Optional[int] = None,
    route: str = "",
    state: Any = None,
    conversation_id: Any = None,
) -> bool:
    from modules.ai.brain.commerce.customer_origin_intent import (  # noqa: PLC0415
        customer_origin_has_payment_request,
        emit_payment_intent_telemetry,
        split_inbound_text,
    )

    split = split_inbound_text(
        customer_msg,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
    )
    origin = split.customer_origin
    inbound_type = inbound_type_label(
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
        message=origin or customer_msg,
    )

    if not customer_origin_has_payment_request(
        origin,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
        state=state,
    ):
        _ocr_only = bool(split.evidence.strip()) and not origin.strip()
        emit_payment_intent_telemetry(
            tenant_id=tenant_id,
            route=route or "outbound_consent",
            split=split,
            allow_outbound=False,
            reason="ocr_only_payment_vocabulary" if _ocr_only else "no_customer_origin_payment_intent",
            conversation_id=conversation_id,
        )
        log_payment_outbound_suppressed(
            tenant_id=tenant_id,
            reason="no_customer_origin_payment_intent",
            inbound_type=inbound_type,
            route=route,
        )
        return False

    try:
        from core.payment_relevance_gate import (  # noqa: PLC0415
            PaymentRelevanceLogContext,
            validate_payment_outbound_artifact,
        )
        verdict = validate_payment_outbound_artifact(
            message=origin,
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
            state=state,
            tenant_id=tenant_id,
            route=route or "outbound_consent",
            log_context=PaymentRelevanceLogContext(
                tenant_id=tenant_id,
                message=origin,
                inbound_metadata=inbound_metadata,
                normalized_type=normalized_type,
                fallback_source=route or "outbound_consent",
                artifact=True,
                final_action="dispatch_payment_artifact",
            ),
        )
        if not verdict.allowed:
            emit_payment_intent_telemetry(
                tenant_id=tenant_id,
                route=route or "outbound_consent",
                split=split,
                allow_outbound=False,
                reason=verdict.reason or "payment_relevance_gate",
                conversation_id=conversation_id,
            )
            log_payment_outbound_suppressed(
                tenant_id=tenant_id,
                reason=verdict.reason or "payment_relevance_gate",
                inbound_type=inbound_type,
                route=route,
            )
            return False
        emit_payment_intent_telemetry(
            tenant_id=tenant_id,
            route=route or "outbound_consent",
            split=split,
            allow_outbound=True,
            reason="ok",
            conversation_id=conversation_id,
        )
        log_payment_consent_detected(
            tenant_id=tenant_id,
            source="payment_relevance_gate",
            strength=1.0 if verdict.payment_semantics else 0.7,
            route=route,
        )
        kind = "payment_info"
        try:
            from modules.ai.brain.decision.payment_barcode_routing import (  # noqa: PLC0415
                is_payment_barcode_image_request,
            )
            if is_payment_barcode_image_request(origin):
                kind = "barcode_image"
        except Exception:  # noqa: BLE001
            pass
        log_payment_outbound_attempt(
            tenant_id=tenant_id,
            request_kind=kind,
            inbound_type=inbound_type,
            route=route,
        )
        return True
    except Exception:  # noqa: BLE001
        pass

    verdict = detect_payment_intent_strength(
        origin,
        state=state,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
    )

    if verdict.source == "blocked" or is_receipt_inbound(
        inbound_metadata, normalized_type=normalized_type,
    ):
        log_payment_outbound_suppressed(
            tenant_id=tenant_id,
            reason="receipt_inbound_no_consent",
            inbound_type=inbound_type,
            route=route,
        )
        return False

    meta = inbound_metadata or {}
    msg = (origin or "").strip()
    if (meta.get("pdf_kind") or meta.get("image_kind")) and (
        not msg or len(_norm(msg).split()) <= 3
    ):
        if verdict.strength < PAYMENT_CONSENT_STRENGTH_THRESHOLD:
            log_payment_outbound_suppressed(
                tenant_id=tenant_id,
                reason="media_inbound_without_explicit_ask",
                inbound_type=inbound_type,
                route=route,
            )
            return False

    if verdict.strength >= PAYMENT_CONSENT_STRENGTH_THRESHOLD:
        log_payment_consent_detected(
            tenant_id=tenant_id,
            source=verdict.source or "semantic",
            strength=verdict.strength,
            route=route,
        )
        kind = "barcode_image" if verdict.source == "explicit" else "payment_info"
        log_payment_outbound_attempt(
            tenant_id=tenant_id,
            request_kind=kind,
            inbound_type=inbound_type,
            route=route,
        )
        return True

    return False


def _is_short_celebration_status(message: str) -> bool:
    norm = _norm(message)
    if not norm or len(norm.split()) > 30:
        return False
    if not any(p.search(norm) for p in _CELEBRATION_PATTERNS):
        return False
    if commerce_signal_strength(message) >= SOCIAL_COMMERCE_BLOCK_THRESHOLD:
        return False
    return True


@dataclass(frozen=True)
class ShortContinuationVerdict:
    matched: bool
    reason: str = ""
    focus_title: str = ""
    product: Optional[Dict[str, Any]] = None
    continuation_mode: str = ""


def _continuation_topic_for_mode(mode: str) -> tuple[str, str]:
    """Return (topic, response_goal) for LLM routing."""
    _map = {
        CONTINUATION_DELIVERY: ("ask_shipping", "continue_fulfillment"),
        CONTINUATION_PAYMENT_FLOW: ("awaiting_payment_receipt", "continue_payment_flow"),
        CONTINUATION_VARIANT_SELECTION: ("variant_selection", "continue_variant_selection"),
        CONTINUATION_CHECKOUT: ("checkout", "continue_checkout"),
        CONTINUATION_RECOMMENDATION: ("product_confirmation", "continue_single_offer"),
        CONTINUATION_PRODUCT: (
            "short_transactional_continuation",
            "continue_active_product_focus",
        ),
    }
    return _map.get(mode, ("short_transactional_continuation", "continue_active_product_focus"))


def detect_short_transactional_continuation(
    message: str,
    *,
    state: Any = None,
    history: Optional[Sequence[Dict[str, Any]]] = None,
) -> ShortContinuationVerdict:
    raw = (message or "").strip()
    norm = _norm(raw)
    if not norm:
        return ShortContinuationVerdict(matched=False)
    if len(norm.split()) > 8:
        return ShortContinuationVerdict(matched=False)
    if not _SHORT_TXN_RE.match(norm):
        return ShortContinuationVerdict(matched=False)

    mode = infer_continuation_mode(state, history=history)  # type: ignore[arg-type]

    if mode == CONTINUATION_PAYMENT_FLOW:
        try:
            from core.payment_relevance_gate import (  # noqa: PLC0415
                validate_payment_workflow_resume,
            )
            meta: Dict[str, Any] = {}
            ntype: Optional[str] = None
            if state is not None:
                profile = getattr(state, "profile", None) or {}
                if isinstance(profile, dict):
                    meta = dict(profile.get("inbound_metadata") or {})
            if meta:
                ntype = str(
                    meta.get("normalized_type") or meta.get("source_type") or ""
                ).lower() or None
            _prv = validate_payment_workflow_resume(
                message=raw,
                inbound_metadata=meta or None,
                normalized_type=ntype,
                state=state,
                history=list(history or []),
                route="short_continuation",
            )
            if not _prv.allowed:
                return ShortContinuationVerdict(
                    matched=False,
                    reason=f"payment_resume_blocked:{_prv.reason}",
                )
        except Exception:  # noqa: BLE001
            pass

    # Fulfillment / payment modes do not require product title.
    if mode in {
        CONTINUATION_DELIVERY,
        CONTINUATION_PAYMENT_FLOW,
        CONTINUATION_CHECKOUT,
        CONTINUATION_VARIANT_SELECTION,
    }:
        return ShortContinuationVerdict(
            matched=True,
            reason="short_continuation_preserve_mode",
            continuation_mode=mode,
        )

    product: Optional[Dict[str, Any]] = None
    title = ""
    if state is not None:
        focus = getattr(state, "current_product_focus", None) or {}
        if isinstance(focus, dict) and str(focus.get("title") or "").strip():
            product = dict(focus)
            title = str(focus.get("title") or "").strip()

    if not title:
        try:
            from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
                resolve_trusted_focus_for_deictic,
            )
            trusted = resolve_trusted_focus_for_deictic(state, raw)
            if trusted.trusted and str(trusted.title or "").strip():
                title = str(trusted.title).strip()
                product = {"title": title, "id": trusted.product_id}
        except Exception:  # noqa: BLE001
            pass

    if not title and mode not in {CONTINUATION_RECOMMENDATION}:
        return ShortContinuationVerdict(matched=False, reason="no_trusted_focus")

    if not mode:
        mode = CONTINUATION_PRODUCT if title else CONTINUATION_RECOMMENDATION

    return ShortContinuationVerdict(
        matched=True,
        reason="short_transactional_continuation",
        focus_title=title,
        product=product,
        continuation_mode=mode,
    )


def is_single_offer_short_acceptance(ctx: Any) -> bool:
    """One SKU offered + colloquial accept → must not generic-clarify."""
    state = getattr(ctx, "state", None)
    if state is None:
        return False
    cands = list(getattr(state, "last_search_candidates", None) or [])
    if len(cands) != 1:
        return False
    verdict = detect_short_transactional_continuation(
        getattr(ctx, "message", "") or "",
        state=state,
        history=list(getattr(ctx, "history", None) or []),
    )
    if not verdict.matched:
        return False
    focus = getattr(state, "current_product_focus", None) or {}
    return bool(isinstance(focus, dict) and str(focus.get("title") or "").strip())


def _ctx_inbound_metadata(ctx: Any) -> dict:
    profile = getattr(ctx, "profile", None) or {}
    if isinstance(profile, dict):
        meta = profile.get("inbound_metadata")
        if isinstance(meta, dict):
            return meta
    return {}


def _correlation_from_ctx(
    ctx: Any,
    *,
    route: str,
    final_action: str,
    social: str = "-",
    continuation: str = "-",
    payment_consent: str = "-",
    suppressed: str = "-",
    topic: str = "-",
) -> None:
    intent = getattr(ctx, "intent", None)
    log_priority_correlation(
        tenant_id=getattr(ctx, "tenant_id", None),
        route=route,
        inbound_type=inbound_type_label(
            inbound_metadata=_ctx_inbound_metadata(ctx),
            message=getattr(ctx, "message", "") or "",
        ),
        intent=str(getattr(intent, "name", "") or "-"),
        topic=topic,
        suppressed=suppressed,
        hard_shift="-",
        continuation=continuation,
        social=social,
        payment_consent=payment_consent,
        final_action=final_action,
        media_sent=False,
        reply_len=0,
    )


def try_social_non_commerce_decision(ctx: Any, *, route: str = "") -> Optional[Any]:
    from modules.ai.brain.decision.actions import ACTION_SOCIAL_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    msg = ctx.message or ""
    intent = getattr(ctx, "intent", None)
    intent_name = getattr(intent, "name", None)
    state = getattr(ctx, "state", None)
    meta = _ctx_inbound_metadata(ctx)

    strength = commerce_signal_strength(
        msg, state=state, intent_name=intent_name,
    )
    if strength >= SOCIAL_COMMERCE_BLOCK_THRESHOLD:
        log_social_route_bypass(
            tenant_id=getattr(ctx, "tenant_id", None),
            reason="commerce_signal_present",
            commerce_strength=strength,
            preview=msg,
            route=route,
        )
        _correlation_from_ctx(
            ctx,
            route=route,
            final_action="bypass_social",
            social="bypass",
            suppressed="-",
            topic="-",
        )
        return None

    try:
        from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: PLC0415
            resolve_commerce_block,
        )
        nc = resolve_commerce_block(
            msg,
            inbound_metadata=meta,
            intent_name=intent_name,
            intent_confidence=getattr(intent, "confidence", None),
        )
    except Exception:  # noqa: BLE001
        nc = None

    category = ""
    source = ""
    if nc is not None:
        category = nc.social_category
        source = nc.source
    elif _is_short_celebration_status(msg):
        category = "celebration"
        source = "celebration_status"
    else:
        return None

    tenant_id = getattr(ctx, "tenant_id", None)
    log_social_non_commerce_routed(
        tenant_id=tenant_id,
        category=category,
        source=source,
        preview=msg,
        route=route,
    )
    _correlation_from_ctx(
        ctx,
        route=route,
        final_action="social_reply",
        social=category,
        topic=category,
    )
    return Decision(
        action=ACTION_SOCIAL_REPLY,
        args={
            "social_category": category,
            "block_commerce_escalation": True,
        },
        reason=f"conversational priority — social/non-commerce ({category})",
        confidence=0.94,
    )


def try_short_continuation_decision(ctx: Any, *, route: str = "") -> Optional[Any]:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    history = list(getattr(ctx, "history", None) or [])
    state = getattr(ctx, "state", None)
    verdict = detect_short_transactional_continuation(
        ctx.message or "",
        state=state,
        history=history,
    )
    if not verdict.matched:
        return None

    tenant_id = getattr(ctx, "tenant_id", None)
    mode = verdict.continuation_mode or CONTINUATION_PRODUCT
    topic, response_goal = _continuation_topic_for_mode(mode)

    log_short_continuation_detected(
        tenant_id=tenant_id,
        preview=ctx.message or "",
        focus_title=verdict.focus_title,
        reason=verdict.reason,
        continuation_mode=mode,
    )
    _correlation_from_ctx(
        ctx,
        route=route,
        final_action="short_continuation_llm",
        continuation=mode,
        topic=topic,
    )

    args: Dict[str, Any] = {
        "topic": topic,
        "response_goal": response_goal,
        "continuation_mode": mode,
        "preserve_product_focus": mode
        not in {CONTINUATION_DELIVERY, CONTINUATION_PAYMENT_FLOW},
    }
    if verdict.product:
        args["product"] = verdict.product
    if mode == CONTINUATION_DELIVERY:
        args["topic_hint"] = "shipping"

    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason=f"short continuation — preserve mode={mode}",
        confidence=0.92,
    )


def _pending_offer_context(state: Any) -> bool:
    if state is None:
        return False
    return bool(
        str(getattr(state, "last_question_asked", "") or "").strip()
        or str(getattr(state, "pending_action", "") or "").strip()
        or getattr(state, "current_product_focus", None)
    )


def positive_commerce_signal(
    message: str,
    *,
    intent_name: Optional[str] = None,
    intent_confidence: Optional[float] = None,
    state: Any = None,
    inbound_metadata: Optional[dict] = None,
) -> bool:
    """True when existing detectors justify sales / commerce framing."""
    try:
        from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: PLC0415
            POSITIVE_COMMERCE_INTENTS,
            has_positive_commerce_intent,
        )
        if has_positive_commerce_intent(intent_name, intent_confidence):
            return True
        from modules.ai.brain.intent import rules as intent_rules  # noqa: PLC0415

        rule_hit = intent_rules.match(message)
        if rule_hit is not None and rule_hit.name in POSITIVE_COMMERCE_INTENTS:
            conf = float(getattr(rule_hit, "confidence", 0) or 0)
            if conf >= 0.70:
                return True
    except Exception:  # noqa: BLE001
        pass

    strength = commerce_signal_strength(
        message, state=state, intent_name=intent_name,
    )
    if strength >= SOCIAL_COMMERCE_BLOCK_THRESHOLD:
        return True

    if detect_short_transactional_continuation(message, state=state).matched:
        return True

    mode = infer_continuation_mode(state)
    if mode in {
        CONTINUATION_PAYMENT_FLOW,
        CONTINUATION_CHECKOUT,
        CONTINUATION_DELIVERY,
        CONTINUATION_VARIANT_SELECTION,
    }:
        return True

    if _pending_offer_context(state):
        return True

    pay = detect_payment_intent_strength(
        message,
        state=state,
        inbound_metadata=inbound_metadata,
    )
    if (
        pay.strength >= PAYMENT_CONSENT_STRENGTH_THRESHOLD
        and pay.source != "blocked"
    ):
        return True

    try:
        from modules.ai.brain.commerce.solution_seeking import (  # noqa: PLC0415
            classify_solution_seeking_commerce,
        )
        if classify_solution_seeking_commerce(message):
            return True
    except Exception:  # noqa: BLE001
        pass

    return False


def absence_of_positive_commerce_signal(
    message: str,
    *,
    intent_name: Optional[str] = None,
    intent_confidence: Optional[float] = None,
    state: Any = None,
    inbound_metadata: Optional[dict] = None,
    nc_match: Any = None,
) -> bool:
    """True when no authoritative source justifies activating sales framing."""
    name = str(intent_name or "").strip().lower()
    if not str(message or "").strip():
        return False

    if nc_match is not None:
        return False

    if name in {
        "social",
        "persona_interaction",
        "who_are_you",
        "platform_inquiry",
    }:
        return False

    if name not in {"general", "hesitation"}:
        return False

    if state is not None and not bool(getattr(state, "greeted", False)):
        return False

    return not positive_commerce_signal(
        message,
        intent_name=intent_name,
        intent_confidence=intent_confidence,
        state=state,
        inbound_metadata=inbound_metadata,
    )


def log_absence_commerce_gate(
    *,
    tenant_id: Optional[int],
    preview: str,
    route: str = "",
    commerce_strength: float = 0.0,
) -> None:
    logger.info(
        "[ABSENCE_COMMERCE_GATE] tenant=%s route=%s strength=%.2f preview=%r",
        tenant_id if tenant_id is not None else "-",
        route or "-",
        float(commerce_strength),
        (preview or "")[:80],
    )


def try_absence_non_sales_decision(ctx: Any, *, route: str = "") -> Optional[Any]:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.persona_expression import (  # noqa: PLC0415
        PERSONA_TOPIC_NON_SALES_AMBIGUOUS,
    )
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    msg = ctx.message or ""
    intent = getattr(ctx, "intent", None)
    intent_name = getattr(intent, "name", None)
    state = getattr(ctx, "state", None)
    meta = _ctx_inbound_metadata(ctx)

    nc = None
    try:
        from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: PLC0415
            resolve_commerce_block,
        )
        nc = resolve_commerce_block(
            msg,
            inbound_metadata=meta,
            intent_name=intent_name,
            intent_confidence=getattr(intent, "confidence", None),
        )
    except Exception:  # noqa: BLE001
        nc = None

    if not absence_of_positive_commerce_signal(
        msg,
        intent_name=intent_name,
        intent_confidence=getattr(intent, "confidence", None),
        state=state,
        inbound_metadata=meta,
        nc_match=nc,
    ):
        return None

    strength = commerce_signal_strength(
        msg, state=state, intent_name=intent_name,
    )
    tenant_id = getattr(ctx, "tenant_id", None)
    log_absence_commerce_gate(
        tenant_id=tenant_id,
        preview=msg,
        route=route,
        commerce_strength=strength,
    )
    _correlation_from_ctx(
        ctx,
        route=route,
        final_action="non_sales_ambiguous_llm",
        topic=PERSONA_TOPIC_NON_SALES_AMBIGUOUS,
    )
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": PERSONA_TOPIC_NON_SALES_AMBIGUOUS,
            "block_commerce_escalation": True,
        },
        reason="absence of positive commerce signal — non-sales generative frame",
        confidence=0.88,
    )


def try_priority_before_suppression(
    ctx: Any,
    *,
    history: Optional[Sequence[Dict[str, Any]]] = None,
    route: str = "",
) -> Optional[Any]:
    _ = history
    dec = try_social_non_commerce_decision(ctx, route=route)
    if dec is not None:
        return dec
    return try_short_continuation_decision(ctx, route=route)


__all__ = [
    "CONTINUATION_CHECKOUT",
    "CONTINUATION_DELIVERY",
    "CONTINUATION_PAYMENT_FLOW",
    "CONTINUATION_PRODUCT",
    "CONTINUATION_RECOMMENDATION",
    "CONTINUATION_VARIANT_SELECTION",
    "PaymentIntentVerdict",
    "ShortContinuationVerdict",
    "absence_of_positive_commerce_signal",
    "commerce_signal_strength",
    "detect_payment_intent_strength",
    "detect_short_transactional_continuation",
    "has_payment_outbound_consent",
    "infer_continuation_mode",
    "inbound_type_label",
    "is_receipt_inbound",
    "is_single_offer_short_acceptance",
    "log_absence_commerce_gate",
    "log_payment_consent_detected",
    "log_payment_outbound_attempt",
    "log_payment_outbound_suppressed",
    "log_priority_correlation",
    "log_short_continuation_detected",
    "log_social_non_commerce_routed",
    "log_social_route_bypass",
    "positive_commerce_signal",
    "try_absence_non_sales_decision",
    "try_priority_before_suppression",
    "try_short_continuation_decision",
    "try_social_non_commerce_decision",
]
