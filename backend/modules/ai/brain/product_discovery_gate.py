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
        from .commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )

        if global_availability_browse_requested(message):
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


_INQUIRY_PRODUCT_QUERY_RE = re.compile(
    r"(?:"
    r"استفسار\s*عن|استفسر\s*عن|"
    r"اريد\s+معرف(?:ة|ه)|أريد\s+معرف(?:ة|ه)|"
    r"(?:ابغ|ابي|أبغ|أبي|اريد|أريد).{0,24}(?:اعرف|أعرف|استفسر|استفسار)(?:\s*عن)?"
    r")\s+(.{2,40})",
    re.UNICODE | re.IGNORECASE,
)

_INQUIRY_PHRASING_RE = re.compile(
    r"(?:"
    r"استفسار\s*عن|استفسر\s*عن|"
    r"اريد\s+معرف(?:ة|ه)|أريد\s+معرف(?:ة|ه)|"
    r"(?:ابغ|ابي|أبغ|أبي|اريد|أريد).{0,24}(?:اعرف|أعرف|استفسر|استفسار)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_TYPES_OVERVIEW_RE = re.compile(
    r"(?:^|\s)(?:انواع|أنواع|types?)\s+(?:ال|about|the\s+)?(.{2,40})",
    re.UNICODE | re.IGNORECASE,
)

# Trailing storefront fluff after the family noun («… السمر عندكم؟»).
_TYPES_SUBJECT_TAIL_RE = re.compile(
    r"(?:\s+(?:عندكم|عندك|لديكم|لديك|متوفرة|متوفر|available))+\s*$",
    re.UNICODE | re.IGNORECASE,
)

_SKU_SPECIFICITY_RE = re.compile(
    r"(?:"
    r"\d|"
    r"كilo|كيلo|كيلو|كيلوغرام|kg|gram|جرام|كجم|"
    r"لتر|\bml\b|"
    r"سدر|طلح|ضهيان|"
    r"رجالي|رجال|نسائي|نساء|"
    r"\bsmall\b|\bmedium\b|\blarge\b|\bxl\b|\bxxl\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

INQUIRY_CLASS_BROAD = "broad_category_inquiry"
INQUIRY_CLASS_BROWSE = "category_browse"
INQUIRY_CLASS_SPECIFIC = "specific_product_search"


def extract_inquiry_product_query(message: str) -> str:
    """Extract product/category from inquiry phrasing, e.g. 'استفسار عن العسل'."""
    raw = (message or "").strip()
    if not raw:
        return ""
    m = _INQUIRY_PRODUCT_QUERY_RE.search(raw)
    if not m:
        return ""
    candidate = (m.group(1) or "").strip(" ؟?!.")
    candidate = re.sub(r"^(?:ال|about|the)\s+", "", candidate, flags=re.UNICODE | re.IGNORECASE)
    return candidate.strip(" ؟?!.")


def has_inquiry_phrasing(message: str) -> bool:
    """True when the customer uses discovery inquiry wording (not SKU lookup)."""
    raw = (message or "").strip()
    if not raw:
        return False
    if _INQUIRY_PHRASING_RE.search(raw):
        return True
    norm = _normalize_ar(raw)
    return any(
        _normalize_ar(p) in norm
        for p in (
            "استفسار عن",
            "استفسر عن",
            "ابغى اعرف عن",
            "أبغى أعرف عن",
            "ابي اعرف عن",
            "أبي أعرف عن",
            "اريد معرفه",
            "أريد معرفة",
        )
    )


def _strip_category_noun(query: str) -> str:
    q = (query or "").strip(" ؟?!.")
    q = re.sub(r"^(?:ال|about|the)\s+", "", q, flags=re.UNICODE | re.IGNORECASE)
    q = re.sub(r"^(?:انواع|أنواع|types?)\s+", "", q, flags=re.UNICODE | re.IGNORECASE)
    return q.strip(" ؟?!.")


def is_generic_category_noun(query: str) -> bool:
    """True for a single category noun or «أنواع X» — not a named SKU."""
    core = _strip_category_noun(query)
    if not core or len(core) < 2:
        return False
    if _SKU_SPECIFICITY_RE.search(core):
        return False
    tokens = [t for t in core.split() if t]
    return len(tokens) <= 1


def has_types_overview_ask(message: str, query: str = "") -> bool:
    """True when the customer asks for types/overview of a category."""
    raw = (message or "").strip()
    q = (query or "").strip()
    norm_msg = _normalize_ar(raw)
    norm_q = _normalize_ar(q)
    if norm_q.startswith("انواع ") or norm_q.startswith("أنواع "):
        return True
    if "انواع " in norm_msg or "أنواع " in norm_msg:
        return True
    return bool(_TYPES_OVERVIEW_RE.search(raw))


def extract_types_overview_query(message: str) -> str:
    """Extract the family/category noun from a types/options ask."""
    raw = (message or "").strip()
    if not raw:
        return ""
    m = _TYPES_OVERVIEW_RE.search(raw)
    if not m:
        return ""
    candidate = (m.group(1) or "").strip(" ؟?!.")
    candidate = _TYPES_SUBJECT_TAIL_RE.sub("", candidate).strip(" ؟?!.")
    candidate = re.sub(
        r"^(?:ال|about|the)\s+",
        "",
        candidate,
        flags=re.UNICODE | re.IGNORECASE,
    )
    return candidate.strip(" ؟?!.")


def log_inquiry_class(
    *,
    tenant_id: Any,
    inquiry_class: str,
    route: str,
    query: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[INQUIRY_CLASS] tenant=%s class=%s route=%s query=%r preview=%r",
            tenant_id,
            inquiry_class,
            route,
            (query or "")[:60],
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def classify_product_inquiry_route(
    ctx: BrainContext,
    *,
    query: str,
) -> tuple[str, str]:
    """Classify discovery turn and recommend route (telemetry + routing).

    Returns ``(inquiry_class, route)`` where *route* is one of:
    ``category_discovery``, ``clarify``, ``search``.
    """
    msg = ctx.message or ""
    q = (query or "").strip()
    slots = getattr(ctx.intent, "slots", None) or {}
    slot_q = str(slots.get("product_query") or slots.get("product_name") or "").strip()

    types_subject = extract_types_overview_query(msg) or _strip_category_noun(q) or q
    if has_types_overview_ask(msg, q) and types_subject and is_generic_category_noun(types_subject):
        if not _SKU_SPECIFICITY_RE.search(msg):
            return INQUIRY_CLASS_BROAD, "search"

    if _has_prior_browse_context(ctx):
        return INQUIRY_CLASS_SPECIFIC, "search"

    try:
        from .commerce.product_breadth_policy import explicit_soft_browse_requested  # noqa: PLC0415

        soft_browse = explicit_soft_browse_requested(msg)
    except Exception:  # noqa: BLE001
        soft_browse = False

    if not q:
        if soft_browse or has_explicit_broad_browse_request(msg):
            return INQUIRY_CLASS_BROWSE, "clarify"
        return INQUIRY_CLASS_BROWSE, "clarify"

    try:
        from .commerce.price_turn_classifier import normalize_price_subject  # noqa: PLC0415

        price_subject = normalize_price_subject(ctx) or _extract_price_subject(msg)
    except Exception:  # noqa: BLE001
        price_subject = _extract_price_subject(msg)
    if price_subject and price_subject.strip() == q and not is_generic_category_noun(q):
        return INQUIRY_CLASS_SPECIFIC, "search"

    inquiry_turn = has_inquiry_phrasing(msg) or has_types_overview_ask(msg, q)
    generic = is_generic_category_noun(q)

    if inquiry_turn and generic and not _SKU_SPECIFICITY_RE.search(msg):
        return INQUIRY_CLASS_BROAD, "search"

    if soft_browse and generic:
        return INQUIRY_CLASS_BROWSE, "clarify"

    if slot_q and slot_q == q and not inquiry_turn and not generic:
        return INQUIRY_CLASS_SPECIFIC, "search"

    return INQUIRY_CLASS_SPECIFIC, "search"


def try_broad_category_inquiry_decision(
    ctx: BrainContext,
    *,
    query: str,
    inquiry_class: str = "",
    route: str = "",
) -> Optional[Decision]:
    """Route broad category inquiry to catalog-grounded browse search."""
    if not inquiry_class:
        inquiry_class, route = classify_product_inquiry_route(ctx, query=query)
    if inquiry_class != INQUIRY_CLASS_BROAD or route != "search":
        return None

    category_hint = _strip_category_noun(query)
    return Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={
            "query": category_hint or query,
            "source": "category_browse",
        },
        reason="broad category inquiry — catalog-grounded browse search",
        confidence=0.88,
    )


def try_types_overview_decision(ctx: BrainContext) -> Optional[Decision]:
    """
    Route explicit types/options asks to category discovery.

    Beats stale browse/availability follow-up so «وش أنواع X عندكم؟»
    lists variants instead of jumping to prices/sizes only.
    """
    msg = ctx.message or ""
    if not has_types_overview_ask(msg):
        return None
    if not getattr(ctx.facts, "has_products", False):
        return None
    subject = extract_types_overview_query(msg)
    if not subject or not is_generic_category_noun(subject):
        return None
    if _SKU_SPECIFICITY_RE.search(msg):
        return None
    try:
        from .order_context_gate import should_block_product_discovery  # noqa: PLC0415

        if should_block_product_discovery(ctx, msg):
            return None
    except Exception:  # noqa: BLE001
        pass
    inquiry_class, route = classify_product_inquiry_route(ctx, query=subject)
    log_inquiry_class(
        tenant_id=getattr(ctx, "tenant_id", None),
        inquiry_class=inquiry_class,
        route=route,
        query=subject,
        preview=msg[:80],
    )
    return try_broad_category_inquiry_decision(
        ctx,
        query=subject,
        inquiry_class=inquiry_class,
        route=route,
    )


def has_explicit_product_inquiry(message: str) -> bool:
    """True when the customer is explicitly asking about a product/category."""
    msg = (message or "").strip()
    if not msg:
        return False
    if extract_inquiry_product_query(msg):
        return True
    norm = _normalize_ar(msg)
    if not norm:
        return False
    _INQUIRY_MARKERS = (
        "استفسار عن",
        "استفسر عن",
        "ابغى اعرف عن",
        "أبغى أعرف عن",
        "ابي اعرف عن",
        "أبي أعرف عن",
        "عندكم عسل",
        "عندك عسل",
        "عندكم منتج",
    )
    if any(_normalize_ar(p) in norm for p in _INQUIRY_MARKERS):
        return True
    try:
        from .commerce.fallback_guard import detect_hard_topic_shift  # noqa: PLC0415

        return detect_hard_topic_shift(msg)
    except Exception:  # noqa: BLE001
        return False


# Optional sellable-unit token after a trailing price ask (كيلو، لتر، …).
_PRICE_UNIT_SUFFIX = (
    r"(?:ال)?(?:كilo|كيلo|كيلو|كيلوغرام|كيلограм|kg|gram|جرام|كجم|g|"
    r"لتر|ml|piece|pack|حبه|حبة)"
)

# Trailing price-ask tokens (بكم، كم سعره، …) — not category-specific.
_PRICE_ASK_SUFFIX = (
    r"(?:بكم|كم\s*سعر|سعر|قد\s*ايش|how\s*much|"
    r"كم\s*سعره|كم\s*سعرها|كم\s*ثمنه|كم\s*ثمنها|"
    r"كم\s*تمنه|كم\s*تمنها)"
)

_PRICE_SUFFIX_RE = re.compile(
    r"^(.{2,80}?)\s+"
    rf"{_PRICE_ASK_SUFFIX}\s*"
    rf"(?:per\s+)?(?:{_PRICE_UNIT_SUFFIX})?"
    r"\s*$",
    re.UNICODE | re.IGNORECASE,
)


def _subject_has_product_substance(candidate: str) -> bool:
    """True when ``candidate`` is more than bare price/unit tokens."""
    norm = _normalize_ar(candidate or "")
    norm = re.sub(r"^ال", "", norm)
    if not norm:
        return False
    tokens = [t for t in norm.split() if t]
    if not tokens:
        return False
    non_unit = [t for t in tokens if t not in _UNIT_ONLY_TOKENS]
    return any(len(t) >= 2 for t in non_unit)


def _extract_price_subject(message: str) -> str:
    """Recover a product name from price-style messages.

    Supported shapes (platform-wide, any catalog wording):
      * ``<product> بكم`` / ``<product> كم سعره``
      * ``<product> بكم <unit>``  (e.g. per-kilo / per-litre asks)
      * ``بكم <product>`` / ``كم سعر <product>``
    """
    raw = (message or "").strip()
    if not raw:
        return ""
    norm = _normalize_ar(raw)
    for prefix in ("كم سعر", "بكم", "سعر", "قد ايش", "how much"):
        pn = _normalize_ar(prefix)
        if norm.startswith(pn):
            rest = raw[len(prefix):].strip(" ؟?!.")
            if rest and _subject_has_product_substance(rest):
                return rest
            return ""
    m = _PRICE_SUFFIX_RE.match(raw.strip(" ؟?!."))
    if m:
        candidate = (m.group(1) or "").strip(" ؟?!.")
        if candidate and _subject_has_product_substance(candidate):
            return candidate
    return ""


def _resolved_product_query(ctx: BrainContext, extracted: str = "") -> str:
    intent = ctx.intent
    slots = getattr(intent, "slots", None) or {}
    slot_query = (
        str(slots.get("product_query") or "").strip()
        or str(slots.get("product_name") or "").strip()
    )
    if slot_query:
        return slot_query
    if str(extracted or "").strip():
        return str(extracted or "").strip()
    try:
        from .commerce.price_turn_classifier import (  # noqa: PLC0415
            normalize_price_subject,
        )

        return normalize_price_subject(ctx)
    except Exception:  # noqa: BLE001 — never break routing
        return _extract_price_subject(ctx.message or "")


def _is_unit_only_price_message(message: str) -> bool:
    """True when the message is a bare price/unit ask with no product subject."""
    if _extract_price_subject(message or ""):
        return False
    norm = _normalize_ar(message or "")
    norm = re.sub(r"^ال", "", norm)
    if not norm:
        return False
    if _PRICE_ONLY_RE.search(norm):
        return True
    tokens = set(norm.split())
    if tokens and tokens <= _UNIT_ONLY_TOKENS:
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
    try:
        from .commerce.price_turn_classifier import (  # noqa: PLC0415
            PriceTurnKind,
            classify_price_turn,
        )

        kind = classify_price_turn(ctx)
        if kind == PriceTurnKind.PRODUCT_PRICE_ASK:
            return False
    except Exception:  # noqa: BLE001
        if _extract_price_subject(msg):
            return False
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

    try:
        from .intent.rules import is_pure_greeting_without_commerce  # noqa: PLC0415

        if is_pure_greeting_without_commerce(msg):
            return "pure_greeting"
    except Exception:  # noqa: BLE001
        pass

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
        _profile = getattr(ctx, "profile", None) or {}
        _in_meta = (
            _profile.get("inbound_metadata")
            if isinstance(_profile, dict) else None
        )
        if resolve_commerce_block(
            msg,
            inbound_metadata=_in_meta if isinstance(_in_meta, dict) else None,
            intent_name=getattr(intent, "name", None),
            intent_confidence=getattr(intent, "confidence", None),
        ):
            return "non_commerce"
    except Exception:  # noqa: BLE001
        pass

    if is_price_without_product_context(ctx):
        return "price_without_product_context"

    if has_explicit_product_inquiry(msg):
        return None

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

    _price_kind = None
    _focus_first_kinds: set = set()
    try:
        from .commerce.price_turn_classifier import (  # noqa: PLC0415
            PriceTurnKind,
            classify_price_turn,
            log_price_turn_classification,
        )

        _price_kind = classify_price_turn(ctx)
        _focus_first_kinds = {
            PriceTurnKind.PRONOUN_REFERENCE,
            PriceTurnKind.PRICE_COMMENT,
            PriceTurnKind.UNIT_PRICE_REFERENCE,
        }
    except Exception:  # noqa: BLE001
        pass

    if focus and (
        (_price_kind in _focus_first_kinds)
        or not product_query
        or _is_unit_only_price_message(msg)
    ):
        try:
            from .commerce.variant_pricing import try_variant_pricing_decision  # noqa: PLC0415

            _variant_dec = try_variant_pricing_decision(ctx)
            if _variant_dec is not None:
                if _price_kind is not None:
                    log_price_turn_classification(
                        ctx, kind=_price_kind, normalized=product_query,
                    )
                return _variant_dec
        except Exception as _vp_exc:  # noqa: BLE001
            logger.debug(
                "[VARIANT_PRICING] decision hook failed tenant=%s err=%s",
                getattr(ctx, "tenant_id", None), _vp_exc,
            )
        if _price_kind is not None:
            log_price_turn_classification(
                ctx, kind=_price_kind, normalized=product_query,
            )
        return Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "price", "product": dict(focus)},
            reason="price question with active product focus",
            confidence=0.88,
        )

    if (
        product_query
        and _price_kind == PriceTurnKind.PRODUCT_PRICE_ASK
        and not _is_unit_only_price_message(product_query)
    ):
        if _price_kind is not None:
            log_price_turn_classification(
                ctx, kind=_price_kind, normalized=product_query,
            )
        return None

    if is_price_without_product_context(ctx, extracted_product_query=extracted_product_query):
        log_product_discovery_blocked(
            tenant_id=getattr(ctx, "tenant_id", None),
            reason="price_without_product_context",
            preview=msg[:80],
        )
        try:
            from .clarification.router import (  # noqa: PLC0415
                try_contextual_price_clarification,
            )

            _ctx_price = try_contextual_price_clarification(
                ctx, trigger="price_without_product_context",
            )
            if _ctx_price is not None:
                return _ctx_price
        except Exception:  # noqa: BLE001  # noqa: silent-ok — optional price clarify import
            pass

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
    state = ctx.state
    try:
        from .commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )
        from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: PLC0415

        if global_availability_browse_requested(msg) or has_explicit_broad_browse_request(msg):
            return Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "", "source": "global_browse_recovery"},
                reason=f"global availability browse — recover from blocked top_products ({reason})",
                confidence=0.91,
            )
        from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: PLC0415
            browse_alternatives_requested,
        )

        if browse_alternatives_requested(msg) and _has_prior_browse_context(ctx):
            return Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={
                    "query": str(getattr(state, "last_browse_query", "") or ""),
                    "source": "show_more",
                },
                reason=f"browse alternatives — recover from blocked top_products ({reason})",
                confidence=0.90,
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional browse recovery imports
        pass
    try:
        from .intent.rules import is_pure_greeting_without_commerce  # noqa: PLC0415

        if is_pure_greeting_without_commerce(msg):
            from .persona_expression import (  # noqa: PLC0415
                PERSONA_KIND_GREETING,
                PERSONA_TOPIC_SOCIAL,
            )

            log_product_discovery_blocked(
                tenant_id=getattr(ctx, "tenant_id", None),
                reason="pure_greeting",
                preview=msg[:80],
                source="top_products",
            )
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": PERSONA_TOPIC_SOCIAL,
                    "persona_kind": PERSONA_KIND_GREETING,
                    "block_commerce_escalation": True,
                },
                reason=(
                    "pure greeting — persona_social (product discovery "
                    "clarify suppressed)"
                ),
                confidence=0.85,
            )
    except Exception:  # noqa: BLE001
        pass
    tenant_id = getattr(ctx, "tenant_id", None)
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
        from .commerce.conversational_priority import (  # noqa: PLC0415
            is_single_offer_short_acceptance,
            try_priority_before_suppression,
            try_short_continuation_decision,
        )

        if is_single_offer_short_acceptance(ctx):
            _single_dec = try_short_continuation_decision(
                ctx, route="single_offer_guard",
            )
            if _single_dec is not None:
                return _single_dec

        _priority_dec = try_priority_before_suppression(
            ctx, history=history, route="clarify_fallback",
        )
        if _priority_dec is not None:
            return _priority_dec
    except Exception:  # noqa: BLE001
        pass

    try:
        from .commerce.fallback_guard import (  # noqa: PLC0415
            detect_hard_topic_shift,
            detect_semantic_dead_end,
            invalidate_suppression_memory,
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

        if detect_hard_topic_shift(
            msg,
            history=history,
            state=state,
        ):
            invalidate_suppression_memory(
                state,
                reason="hard_topic_shift",
                tenant_id=tenant_id,
                preview=msg,
                history=history,
            )

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
                    if should_block_fallback_repeat(
                        state, _pay_q, message=msg, history=history,
                    ):
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
            except Exception:
                logger.exception(
                    "[SOLUTION_SEEKING] telemetry log failed tenant=%s",
                    tenant_id,
                )
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

        try:
            from .clarification.router import (  # noqa: PLC0415
                try_contextual_clarification_fallback,
            )

            _ctx_clar = try_contextual_clarification_fallback(
                ctx,
                trigger=str(reason or "discovery_blocked"),
                reason_prefix=f"blocked top_products ({reason})",
            )
            if _ctx_clar is not None:
                return _ctx_clar
        except Exception:
            logger.exception(
                "[PRODUCT_DISCOVERY] contextual_clarification_fallback failed",
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
        if should_block_fallback_repeat(
            state, _question, message=msg, history=history,
        ):
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
        from .clarification.resolved_product_guard import (  # noqa: PLC0415
            apply_resolved_product_clarify_guard,
            has_resolved_product_subject,
        )
        if has_resolved_product_subject(ctx):
            _question = apply_resolved_product_clarify_guard(
                ctx,
                _question,
                source="clarify_instead_of_top_products",
            )
    except Exception:  # noqa: BLE001
        pass

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
    "classify_product_inquiry_route",
    "clarify_instead_of_top_products",
    "extract_inquiry_product_query",
    "extract_types_overview_query",
    "has_explicit_broad_browse_request",
    "has_explicit_product_inquiry",
    "has_inquiry_phrasing",
    "has_types_overview_ask",
    "INQUIRY_CLASS_BROAD",
    "INQUIRY_CLASS_BROWSE",
    "INQUIRY_CLASS_SPECIFIC",
    "is_generic_category_noun",
    "is_need_based_product_advice",
    "is_solution_seeking_commerce",
    "is_price_without_product_context",
    "log_inquiry_class",
    "log_product_discovery_blocked",
    "product_discovery_block_reason",
    "should_block_generic_product_discovery",
    "should_suppress_recommendation_escalation",
    "try_broad_category_inquiry_decision",
    "try_price_query_decision",
    "try_types_overview_decision",
]
