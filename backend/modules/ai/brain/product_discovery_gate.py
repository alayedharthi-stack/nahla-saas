"""
brain/product_discovery_gate.py
───────────────────────────────
Positive-commerce gate for product discovery and recommendations.

Production regression (May 2026): weak / ambiguous turns and fulfillment
messages were falling through to ``top_products`` catalog order, surfacing
unrelated best sellers (e.g. bee-venom SKUs) during active checkout.

Invariant: product recommendations require POSITIVE commerce intent AND
contextual relevance. Generic ``top_products`` fallback runs ONLY when the
customer explicitly asks to browse (``وش عندكم؟``, ``وريني المنتجات``, …).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .decision.actions import (
    ACTION_CLARIFY,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from .intent.rules import INTENT_ASK_PRICE, INTENT_ASK_PRODUCT
from .types import BrainContext, Decision, INTENT_NEED_BASED_PRODUCT_ADVICE

logger = logging.getLogger("nahla.brain.product_discovery_gate")

_TOP_PRODUCTS_SOURCES = frozenset({
    "top_products",
    "top_products_numeric_fallback",
    "top_products_replay_fallback",
    "top_products_start_order",
})

_CONTINUATION_SOURCES = frozenset({
    "show_more",
    "replay",
})

_AMBIGUOUS_INTENTS = frozenset({
    "general",
    "greeting",
    "hesitation",
    "unknown",
})

_PRICE_ONLY_RE = re.compile(
    r"(?:كم\s*سعر|بكم|سعر\s*ال|قد\s*ايش|how\s*much)"
    r"[\s\u0020]*"
    r"(?:ال)?(?:كilo|كيلو|كيلوغرام|كيلограм|kg|gram|جرام|ج\s*ر\s*ا\s*م)?"
    r"\s*$",
    re.UNICODE | re.IGNORECASE,
)

_UNIT_ONLY_TOKENS = frozenset({
    "كilo", "كيلo", "كيلو", "كيلوغرام", "كيلограм", "kg", "gram", "جرام",
    "كجم", "g", "ك", "سعر", "بكم", "كم",
})


def log_product_discovery_blocked(
    *,
    tenant_id: Any,
    reason: str,
    preview: str = "",
    source: str = "",
) -> None:
    try:
        logger.info(
            "[PRODUCT_DISCOVERY_BLOCKED] tenant=%s reason=%s source=%s preview=%r",
            tenant_id,
            reason,
            source or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def _normalize_ar(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[؟?!.,؛:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def has_explicit_broad_browse_request(message: str) -> bool:
    """Strict allowlist — ``top_products`` may run only on explicit browse."""
    try:
        from .commerce.product_breadth_policy import explicit_broad_browse_requested  # noqa: PLC0415

        if explicit_broad_browse_requested(message):
            return True
    except Exception:  # noqa: BLE001
        pass

    norm = _normalize_ar(message or "")
    if not norm:
        return False

    _EXTRA_BROWSE = (
        "وريني المنتجات",
        "ورني المنتجات",
        "اعرض المنتجات",
        "أرسل الخيارات",
        "ارسل الخيارات",
        "ابي اشوف الانواع",
        "أبي أشوف الأنواع",
        "ابغى اشوف الانواع",
        "أبغى أشوف الأنواع",
        "الاكثر مبيعا",
        "اكثر مبيعا",
        "show products",
        "top products",
    )
    return any(_normalize_ar(p) in norm for p in _EXTRA_BROWSE)


_PRICE_SUFFIX_RE = re.compile(
    r"^(.{2,60}?)\s+(?:بكم|كم\s*سعر|سعر)\s*$",
    re.UNICODE | re.IGNORECASE,
)


def _extract_price_subject(message: str) -> str:
    """Recover a product name from price-style messages."""
    raw = (message or "").strip()
    if not raw:
        return ""
    norm = _normalize_ar(raw)
    for prefix in ("كم سعر", "بكم", "سعر", "قد ايش", "how much"):
        pn = _normalize_ar(prefix)
        if norm.startswith(pn):
            rest = raw[len(prefix):].strip(" ؟?!.")
            if rest and not _is_unit_only_price_message(rest):
                return rest
            return ""
    m = _PRICE_SUFFIX_RE.match(raw.strip(" ؟?!."))
    if m:
        candidate = (m.group(1) or "").strip(" ؟?!.")
        if candidate and not _is_unit_only_price_message(candidate):
            return candidate
    return ""


def _resolved_product_query(ctx: BrainContext, extracted: str = "") -> str:
    intent = ctx.intent
    slots = getattr(intent, "slots", None) or {}
    return (
        str(slots.get("product_query") or "").strip()
        or str(slots.get("product_name") or "").strip()
        or str(extracted or "").strip()
        or _extract_price_subject(ctx.message or "")
    )


def _is_unit_only_price_message(message: str) -> bool:
    norm = _normalize_ar(message or "")
    norm = re.sub(r"^ال", "", norm)
    if not norm:
        return False
    if _PRICE_ONLY_RE.search(norm):
        return True
    tokens = set(norm.split())
    if tokens and tokens <= _UNIT_ONLY_TOKENS:
        return True
    if "كيلo" in norm or "كيلو" in norm:
        productish = re.search(
            r"عسل|سدر|سمر|طلح|شمع|منتج|product|honey",
            norm,
            re.I,
        )
        if not productish and re.search(r"كم|سعر|بكم", norm):
            return True
    return False


def is_solution_seeking_commerce(ctx: BrainContext) -> bool:
    """True when turn is attribute/outcome-based commerce — not unknown SKU."""
    if str(getattr(ctx.intent, "name", "") or "") in {
        INTENT_NEED_BASED_PRODUCT_ADVICE,
        "need_based_product_advice",
        "solution_seeking_commerce",
    }:
        return True
    try:
        from .commerce.solution_seeking import classify_solution_seeking_commerce  # noqa: PLC0415

        return classify_solution_seeking_commerce(ctx.message or "") is not None
    except Exception:  # noqa: BLE001
        return False


def is_need_based_product_advice(ctx: BrainContext) -> bool:
    """Backward-compat alias for :func:`is_solution_seeking_commerce`."""
    return is_solution_seeking_commerce(ctx)


def is_price_without_product_context(
    ctx: BrainContext,
    *,
    extracted_product_query: str = "",
) -> bool:
    if is_need_based_product_advice(ctx):
        return False
    intent_name = str(getattr(ctx.intent, "name", "") or "")
    if intent_name not in (INTENT_ASK_PRICE, INTENT_ASK_PRODUCT):
        return False
    if ctx.state.current_product_focus:
        return False
    if _resolved_product_query(ctx, extracted_product_query):
        return False
    msg = ctx.message or ""
    if _is_unit_only_price_message(msg):
        return True
    norm = _normalize_ar(msg)
    return bool(_PRICE_ONLY_RE.search(norm))


def _has_fulfillment_message_context(message: str) -> bool:
    try:
        from .order_context_gate import detect_fulfillment_update  # noqa: PLC0415

        if detect_fulfillment_update(message or "", {}):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.address_resolution import extract_address_signals  # noqa: PLC0415

        sig = extract_address_signals(message or "") or {}
        if sig.get("google_maps_url") or sig.get("short_address_code"):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _has_prior_browse_context(ctx: BrainContext) -> bool:
    state = ctx.state
    if list(getattr(state, "last_search_candidates", None) or []):
        return True
    if list(getattr(state, "catalog_browse_pool", None) or []):
        return True
    if str(getattr(state, "last_browse_query", "") or "").strip():
        return True
    return False


def product_discovery_block_reason(
    ctx: BrainContext,
    *,
    message: Optional[str] = None,
    source: Optional[str] = None,
) -> Optional[str]:
    """Return block reason or ``None`` when discovery may proceed."""
    msg = message if message is not None else (ctx.message or "")
    src = str(source or "").strip().lower()
    intent_name = str(getattr(ctx.intent, "name", "") or "")
    if intent_name == "product_visual_request":
        return None

    if intent_name == INTENT_NEED_BASED_PRODUCT_ADVICE or is_need_based_product_advice(ctx):
        return None

    intent_conf = float(getattr(ctx.intent, "confidence", 0.0) or 0.0)

    try:
        from .order_context_gate import should_block_product_discovery  # noqa: PLC0415

        if should_block_product_discovery(ctx, msg):
            return "active_fulfillment"
    except Exception:  # noqa: BLE001
        pass

    if _has_fulfillment_message_context(msg):
        return "active_fulfillment"

    try:
        from .intent.non_commerce_classifier import resolve_commerce_block  # noqa: PLC0415

        intent = ctx.intent
        if resolve_commerce_block(
            msg,
            intent_name=getattr(intent, "name", None),
            intent_confidence=getattr(intent, "confidence", None),
        ):
            return "non_commerce"
    except Exception:  # noqa: BLE001
        pass

    if is_price_without_product_context(ctx):
        return "price_without_product_context"

    if src in _TOP_PRODUCTS_SOURCES and not has_explicit_broad_browse_request(msg):
        return "weak_or_unknown_intent"

    if src in _CONTINUATION_SOURCES:
        if not _has_prior_browse_context(ctx):
            return "weak_or_unknown_intent"
        return None

    if src in _TOP_PRODUCTS_SOURCES:
        return None

    norm = _normalize_ar(msg)
    if len(norm) <= 2 and not _resolved_product_query(ctx):
        return "weak_or_unknown_intent"

    if intent_name in _AMBIGUOUS_INTENTS and intent_conf < 0.72:
        if not _resolved_product_query(ctx) and not has_explicit_broad_browse_request(msg):
            return "weak_or_unknown_intent"

    if intent_name in _AMBIGUOUS_INTENTS and not _resolved_product_query(ctx):
        if not has_explicit_broad_browse_request(msg):
            if intent_name == "general" and norm:
                return "weak_or_unknown_intent"

    return None


def should_block_generic_product_discovery(
    ctx: BrainContext,
    *,
    message: Optional[str] = None,
    source: Optional[str] = None,
) -> bool:
    return product_discovery_block_reason(ctx, message=message, source=source) is not None


def allows_top_products_decision(
    ctx: BrainContext,
    *,
    source: str,
    message: Optional[str] = None,
) -> bool:
    reason = product_discovery_block_reason(ctx, message=message, source=source)
    if reason:
        log_product_discovery_blocked(
            tenant_id=getattr(ctx, "tenant_id", None),
            reason=reason,
            preview=(message or ctx.message or "")[:80],
            source=source,
        )
        return False
    return True


def allows_search_top_products_fallback(
    ctx: BrainContext,
    *,
    query: str,
    source: str = "",
    message: Optional[str] = None,
) -> bool:
    """Gate implicit top-products fallback after a failed catalog search."""
    msg = message if message is not None else (ctx.message or "")
    src = str(source or "").strip().lower()

    if not str(query or "").strip():
        return allows_top_products_decision(ctx, source=src or "top_products", message=msg)

    reason = product_discovery_block_reason(ctx, message=msg, source=src)
    if reason:
        log_product_discovery_blocked(
            tenant_id=getattr(ctx, "tenant_id", None),
            reason=reason,
            preview=msg[:80],
            source=src or "search_miss_fallback",
        )
        return False

    if not _resolved_product_query(ctx) and _is_unit_only_price_message(msg):
        log_product_discovery_blocked(
            tenant_id=getattr(ctx, "tenant_id", None),
            reason="price_without_product_context",
            preview=msg[:80],
            source=src or "search_miss_fallback",
        )
        return False

    return False


def should_suppress_recommendation_escalation(
    *,
    message: str = "",
    brain_state: Optional[dict] = None,
    commerce_bundle: Optional[dict] = None,
    intent_name: Optional[str] = None,
) -> bool:
    """Webhook helper — fulfillment lock OR weak discovery."""
    try:
        from .order_context_gate import should_suppress_product_escalation  # noqa: PLC0415

        if should_suppress_product_escalation(
            message=message,
            brain_state=brain_state,
            commerce_bundle=commerce_bundle,
            intent_name=intent_name,
        ):
            return True
    except Exception:  # noqa: BLE001
        pass

    try:
        from .types import (  # noqa: PLC0415
            BrainContext,
            CommerceFacts,
            Intent,
            MerchantConversationState,
        )

        state = MerchantConversationState.from_dict(dict(brain_state or {}))
        ctx = BrainContext(
            tenant_id=0,
            customer_phone="",
            message=message or "",
            intent=Intent(
                name=intent_name or "general",
                confidence=0.5,
                raw_message=message or "",
            ),
            state=state,
            facts=CommerceFacts(),
            commerce_bundle=commerce_bundle or {},
        )
        if should_block_generic_product_discovery(ctx, message=message):
            return True
    except Exception:  # noqa: BLE001
        pass

    norm = _normalize_ar(message or "")
    if len(norm) <= 2:
        return True
    if _is_unit_only_price_message(message or "") and not (
        (brain_state or {}).get("current_product_focus")
    ):
        return True
    if not has_explicit_broad_browse_request(message or ""):
        if intent_name in _AMBIGUOUS_INTENTS:
            return True
    return False


def try_price_query_decision(
    ctx: BrainContext,
    *,
    extracted_product_query: str = "",
) -> Optional[Decision]:
    """Route price asks without product context to clarify / focus — not search."""
    intent_name = str(getattr(ctx.intent, "name", "") or "")
    if intent_name == INTENT_NEED_BASED_PRODUCT_ADVICE or is_need_based_product_advice(ctx):
        return None
    if intent_name not in (INTENT_ASK_PRICE, INTENT_ASK_PRODUCT):
        return None

    msg = ctx.message or ""
    focus = ctx.state.current_product_focus
    product_query = _resolved_product_query(ctx, extracted_product_query)

    if focus and (not product_query or _is_unit_only_price_message(msg)):
        return Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "price", "product": dict(focus)},
            reason="price question with active product focus",
            confidence=0.88,
        )

    if product_query and not _is_unit_only_price_message(product_query):
        return None

    if is_price_without_product_context(ctx, extracted_product_query=extracted_product_query):
        log_product_discovery_blocked(
            tenant_id=getattr(ctx, "tenant_id", None),
            reason="price_without_product_context",
            preview=msg[:80],
        )
        return Decision(
            action=ACTION_CLARIFY,
            args={
                "question": (
                    "تقصد سعر كيلو أي منتج؟ اكتب اسم المنتج أو نوعه "
                    "وأعطيك السعر."
                ),
            },
            reason="price ask without resolved product — clarify instead of catalog",
            confidence=0.86,
        )

    return None


def clarify_instead_of_top_products(
    ctx: BrainContext,
    *,
    reason: str,
) -> Decision:
    log_product_discovery_blocked(
        tenant_id=getattr(ctx, "tenant_id", None),
        reason=reason,
        preview=(ctx.message or "")[:80],
        source="top_products",
    )
    msg = ctx.message or ""
    tenant_id = getattr(ctx, "tenant_id", None)
    state = ctx.state
    history = list(getattr(ctx, "history", None) or [])
    if not history and state is not None:
        for turn in list(getattr(state, "recent_messages", None) or [])[-8:]:
            role = str(turn.get("role") or turn.get("direction") or "").lower()
            if role not in {"user", "customer", "in", "inbound"}:
                continue
            body = str(turn.get("content") or turn.get("text") or turn.get("body") or "").strip()
            if body:
                history.append({"direction": "in", "body": body})

    try:
        from .commerce.fallback_guard import (  # noqa: PLC0415
            detect_semantic_dead_end,
            log_fallback_repeat_blocked,
            record_fallback_sent,
            resolve_active_topic,
            should_block_fallback_repeat,
            stamp_recent_topic,
        )
        from .commerce.solution_seeking import (  # noqa: PLC0415
            classify_solution_seeking_commerce,
            contextual_non_product_clarification,
            detect_solution_seeking_suppression,
            intelligent_need_clarification,
            log_intelligent_need_clarification,
            log_intelligent_need_clarification_suppressed,
            log_solution_seeking_suppressed,
            should_suppress_repeat_need_clarification,
        )

        _canonical = ""
        _interp = getattr(ctx, "semantic_interpretation", None)
        if _interp is not None:
            _canonical = str(getattr(_interp, "canonical_text", "") or "").strip()

        _dead_end_goal = detect_semantic_dead_end(
            msg,
            history=history,
            state=state,
            previous_goal=str(getattr(state, "customer_goal", "") or ""),
        )
        if _dead_end_goal:
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "show_all_variants_prices",
                    "customer_goal": _dead_end_goal,
                    "response_goal": "show_all_variants_prices",
                },
                reason="semantic dead-end — inferred all_variant_prices goal",
                confidence=0.88,
            )

        _suppressed = resolve_active_topic(msg, state, history)
        if not _suppressed:
            _suppressed = detect_solution_seeking_suppression(
                msg, skip_recent_topic=True,
            )
        if _canonical and _canonical != msg.strip():
            _post = detect_solution_seeking_suppression(
                _canonical, skip_recent_topic=True,
            )
            if _post:
                _suppressed = _post
        if _suppressed:
            stamp_recent_topic(state, _suppressed)
            log_solution_seeking_suppressed(
                tenant_id=tenant_id,
                reason=_suppressed,
                preview=msg,
            )
            if _suppressed == "delivery_intent":
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={"topic": "ask_shipping", "topic_hint": "shipping"},
                    reason="delivery question — LLM shipping reply, not product advisory",
                    confidence=0.90,
                )
            if _suppressed in {"order_intent"}:
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={"topic": "track_order", "topic_hint": "order_status"},
                    reason="order/tracking question — not product advisory",
                    confidence=0.90,
                )
            if _suppressed == "location_intent":
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={"topic": "fulfillment_location", "topic_hint": "location"},
                    reason="location/fulfillment message — not product advisory",
                    confidence=0.88,
                )
            if _suppressed == "support_intent":
                _support_q = contextual_non_product_clarification(msg)
                if _support_q:
                    return Decision(
                        action=ACTION_CLARIFY,
                        args={"question": _support_q},
                        reason="support clarify — short, not product advisory",
                        confidence=0.86,
                    )
                return Decision(
                    action=ACTION_HANDOFF,
                    args={"reason": "support_intent"},
                    reason="support question — handoff, not product advisory",
                    confidence=0.88,
                )
            if _suppressed == "payment_intent":
                _pay_q = contextual_non_product_clarification(msg)
                if _pay_q:
                    if should_block_fallback_repeat(state, _pay_q):
                        log_fallback_repeat_blocked(
                            tenant_id=tenant_id,
                            reason="payment_clarify_repeat",
                            preview=msg,
                        )
                        return Decision(
                            action=ACTION_LLM_REPLY,
                            args={"topic": "ask_payment_info", "topic_hint": "payment"},
                            reason="payment repeat blocked — direct payment topic",
                            confidence=0.88,
                        )
                    record_fallback_sent(state, _pay_q)
                    return Decision(
                        action=ACTION_CLARIFY,
                        args={"question": _pay_q},
                        reason="payment clarify — short, not product advisory",
                        confidence=0.86,
                    )
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args={"topic": "ask_payment_info", "topic_hint": "payment"},
                    reason="payment question — not product advisory",
                    confidence=0.88,
                )

        _ss = classify_solution_seeking_commerce(msg)
        if _ss is None and _canonical:
            _ss = classify_solution_seeking_commerce(_canonical)
        if _ss is not None:
            try:
                from .commerce.solution_seeking import log_solution_seeking_commerce  # noqa: PLC0415

                log_solution_seeking_commerce(
                    tenant_id=tenant_id,
                    axis=_ss.axis,
                    source=_ss.source,
                    route="clarify_fallback_llm",
                    preview=msg,
                )
            except Exception:  # noqa: BLE001
                pass
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "solution_seeking_commerce",
                    "need_category": _ss.axis,
                    "solution_axis": _ss.axis,
                },
                reason="solution-seeking commerce — advisory LLM, not SKU clarify",
                confidence=0.88,
            )

        _question = intelligent_need_clarification("general_attribute")
        if should_suppress_repeat_need_clarification(state, "general_attribute", _question):
            log_intelligent_need_clarification_suppressed(
                tenant_id=tenant_id,
                axis="general_attribute",
                reason="repeat_blocked",
                preview=msg,
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args={"topic": "solution_seeking_commerce", "solution_axis": "general_attribute"},
                reason="repeat need clarification blocked — advisory LLM",
                confidence=0.82,
            )
        if should_block_fallback_repeat(state, _question):
            log_fallback_repeat_blocked(
                tenant_id=tenant_id,
                reason="need_clarify_repeat",
                preview=msg,
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args={"topic": "solution_seeking_commerce", "solution_axis": "general_attribute"},
                reason="fallback repeat blocked — advisory LLM",
                confidence=0.82,
            )
        record_fallback_sent(state, _question)
    except Exception:  # noqa: BLE001
        _question = (
            "تقصد حاجة أو مواصفة معيّنة؟ وضّح الاستخدام أو الصفة المطلوبة "
            "وأرشّح لك الأنسب — بدون ما تحتاج تكتب اسم منتج."
        )

    try:
        from .commerce.solution_seeking import log_intelligent_need_clarification  # noqa: PLC0415

        log_intelligent_need_clarification(
            tenant_id=tenant_id,
            axis="general_attribute",
            reason=reason,
            preview=msg,
        )
    except Exception:  # noqa: BLE001
        pass

    return Decision(
        action=ACTION_CLARIFY,
        args={"question": _question},
        reason=f"blocked top_products ({reason}) — intelligent need clarification",
        confidence=0.80,
    )


__all__ = [
    "allows_search_top_products_fallback",
    "allows_top_products_decision",
    "clarify_instead_of_top_products",
    "has_explicit_broad_browse_request",
    "is_need_based_product_advice",
    "is_solution_seeking_commerce",
    "is_price_without_product_context",
    "log_product_discovery_blocked",
    "product_discovery_block_reason",
    "should_block_generic_product_discovery",
    "should_suppress_recommendation_escalation",
    "try_price_query_decision",
]
