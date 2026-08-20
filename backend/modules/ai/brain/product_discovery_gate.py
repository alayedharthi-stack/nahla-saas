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
from typing import Any, Dict, Optional

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

_LOGISTICS_CONTEXT_RE = re.compile(
    r"(?:"
    r"\b(?:smsa|aramex|spl|dhl|naqel|fedex|courier|tracking|pin|delivery\s*code)\b|"
    r"سمسا|ارامكس|أرامكس|ساعي|مندوب|موصل|توصيل|شحن|شحنه|شحنة|"
    r"رقم\s*(?:التتبع|الشحنه|الشحنة)|كود\s*(?:التوصيل|الاستلام)|رمز\s*(?:التوصيل|الاستلام)|"
    r"بوليصه|بوليصة"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_CONTACT_CONTEXT_RE = re.compile(
    r"(?:"
    r"ارسل\s*(?:لي\s*)?(?:ال)?(?:ارقام|أرقام|رقم)|"
    r"(?:ابي|أبي|ابغى|أبغى)\s*(?:رقم(?:كم|ك)?|احد\s*يكلمني|أحد\s*يكلمني)|"
    r"رقمكم|رقمك|اتصال|مكالمة|كلمني|تواصل(?:وا)?\s*معي"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_SOCIAL_CONTEXT_RE = re.compile(
    r"(?:"
    r"جزاك(?:م)?\s*الله|الله\s*يعطيك|بارك\s*الله|"
    r"صلى\s*الله\s*عليه\s*وسلم|اللهم|دعاء|"
    r"شكرا|شكرًا|مشكور|يعطيك\s*العافيه|يعطيك\s*العافية"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Religious «آمين» only — standalone amen, not a substring inside staff names/phrases.
_RELIGIOUS_AMEN_RE = re.compile(
    r"(?:"
    r"^(?:اللهم\s+)?(?:آ?\s*م\s*ي\s*ن)\s*(?:يا\s*رب)?(?:[!.؟?\s]|$)"
    r"|^(?:يا\s*رب)\s"
    r"|جزاك(?:\s+الله)?(?:\s+خير)?[^\n]{0,40}(?:آ?\s*م\s*ي\s*ن)\s*(?:[!.؟?]|$)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

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
    """Strict allowlist — ``top_products`` may run only on explicit browse.

    Single source of truth: ``global_availability_browse_requested`` in
    ``product_breadth_policy`` (no duplicated phrase lists here).
    """
    try:
        from .commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )

        return bool(global_availability_browse_requested(message))
    except Exception:  # noqa: BLE001
        return False


def product_browse_negative_context_reason(message: str) -> str:
    """Non-product operational/social contexts that must not fall into browse."""
    raw = str(message or "").strip()
    if not raw:
        return ""
    if _LOGISTICS_CONTEXT_RE.search(raw):
        return "logistics_context"
    try:
        from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
            has_explicit_product_commerce_intent,
            is_staff_or_contact_context,
            staff_contact_context_reason,
        )

        if not has_explicit_product_commerce_intent(raw):
            if is_staff_or_contact_context(raw):
                return staff_contact_context_reason(raw) or "staff_contact_context"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional staff guard import
        pass
    if _CONTACT_CONTEXT_RE.search(raw):
        return "contact_context"
    if _RELIGIOUS_AMEN_RE.search(raw):
        return "social_context"
    if _SOCIAL_CONTEXT_RE.search(raw):
        return "social_context"
    return ""


_INQUIRY_PRODUCT_QUERY_RE = re.compile(
    r"(?:"
    r"استفسار\s*عن|استفسر\s*عن|"
    r"عندي\s+سؤال\s*عن|عندي\s+استفسار\s*عن|"
    r"اريد\s+معرف(?:ة|ه)|أريد\s+معرف(?:ة|ه)|"
    r"(?:ابغ|ابي|أبغ|أبي|اريد|أريد).{0,24}(?:اعرف|أعرف|استفسر|استفسار)(?:\s*عن)?"
    r")\s+(.{2,40})",
    re.UNICODE | re.IGNORECASE,
)

_INQUIRY_PHRASING_RE = re.compile(
    r"(?:"
    r"استفسار\s*عن|استفسر\s*عن|"
    r"عندي\s+سؤال\s*عن|عندي\s+استفسار\s*عن|"
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

_TYPES_SUBJECT_STOPWORDS = frozenset({
    "الي", "اللي", "ال", "عندكم", "عندك", "لديكم", "لديك", "متوفر", "متوفرة",
    "available", "what", "which",
})

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
INQUIRY_CLASS_OPEN = "open_category_inquiry"
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
            "عندي سؤال عن",
            "عندي استفسار عن",
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
    if _normalize_ar(core) in _TYPES_SUBJECT_STOPWORDS:
        return False
    if _SKU_SPECIFICITY_RE.search(core):
        return False
    tokens = [t for t in core.split() if t]
    if len(tokens) == 1 and _normalize_ar(tokens[0]) in _TYPES_SUBJECT_STOPWORDS:
        return False
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
    candidate = candidate.strip(" ؟?!.")
    norm_candidate = _normalize_ar(candidate)
    if not norm_candidate or norm_candidate in _TYPES_SUBJECT_STOPWORDS:
        return ""
    if len(norm_candidate.split()) == 1 and norm_candidate in _TYPES_SUBJECT_STOPWORDS:
        return ""
    return candidate


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


def is_open_category_inquiry_turn(message: str, query: str = "") -> bool:
    """True for open category inquiry — not explicit browse or availability."""
    msg = (message or "").strip()
    if not msg:
        return False
    q = (query or "").strip() or extract_inquiry_product_query(msg)
    if not q or not is_generic_category_noun(q):
        return False
    if not has_inquiry_phrasing(msg) and not extract_inquiry_product_query(msg):
        return False
    if _SKU_SPECIFICITY_RE.search(msg):
        return False
    if has_types_overview_ask(msg, q):
        return False
    if has_explicit_broad_browse_request(msg):
        return False
    try:
        from .commerce.product_breadth_policy import (  # noqa: PLC0415
            explicit_hard_browse_requested,
            global_availability_browse_requested,
        )

        if explicit_hard_browse_requested(msg) or global_availability_browse_requested(msg):
            return False
    except Exception:  # noqa: BLE001
        pass
    try:
        from .commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
            CommerceTurnKind,
            classify_commerce_turn_kind,
            has_price_inquiry_signal,
        )

        if has_price_inquiry_signal(msg):
            return False
        kind = classify_commerce_turn_kind(msg)
        if kind in (
            CommerceTurnKind.AVAILABILITY,
            CommerceTurnKind.VISUAL_BROWSE,
            CommerceTurnKind.PRICE_INQUIRY,
        ):
            return False
        # ORDER/BROWSE false positives on «أريد معرفة» / «أبغى استفسار» — inquiry
        # phrasing and explicit browse guards above are the source of truth.
    except Exception:  # noqa: BLE001
        pass
    return True


def _stamp_open_inquiry_category(ctx: BrainContext, category: str) -> None:
    """Persist category anchor for follow-up turns without forcing catalog browse."""
    state = getattr(ctx, "state", None)
    cat = _strip_category_noun(category or "")
    if state is None or not cat:
        return
    try:
        state.last_browse_query = cat
        from .commerce.commerce_conversation_guard import (  # noqa: PLC0415
            apply_commerce_session,
            load_commerce_session,
        )

        session = load_commerce_session(state)
        session.active_category = cat
        apply_commerce_session(state, session)
    except Exception:  # noqa: BLE001
        pass


def _build_open_category_inquiry_decision(
    ctx: BrainContext,
    *,
    query: str,
) -> Decision:
    category_hint = _strip_category_noun(query)
    _stamp_open_inquiry_category(ctx, category_hint or query)
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "open_category_inquiry",
            "category_scope": category_hint or query,
            "inquiry_category": category_hint or query,
            "block_availability_rewrite": True,
            "response_goal": "open_category_inquiry",
        },
        reason="open category inquiry — conversational LLM with category anchor",
        confidence=0.88,
    )


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
        if is_open_category_inquiry_turn(msg, q):
            return INQUIRY_CLASS_OPEN, "llm"
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
    """Route broad category inquiry to catalog search or open conversational LLM."""
    _referent_reply = try_referent_scoped_product_reply_decision(ctx)
    if _referent_reply is not None:
        return _referent_reply
    if not inquiry_class:
        inquiry_class, route = classify_product_inquiry_route(ctx, query=query)
    if inquiry_class == INQUIRY_CLASS_OPEN and route == "llm":
        return _build_open_category_inquiry_decision(ctx, query=query)
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


# Same product-deixis class as catalog_product_answer._PRODUCT_DEIXIS_RE.
# Linguistic reference to the established product — not a phrase/SKU map.
_REFERENT_SCOPE_DEICTIC_RE = re.compile(
    r"(?:(?:^|\s)(?:هذا|هذه|هذي|ذلك|تلك)\b)"
    r"|(?:هل\s+هو\b)|(?:هل\s+هي\b)"
    r"|(?:وهل\s+هو\b)|(?:وهل\s+هي\b)"
    r"|(?:^|\s)(?:this|that|these|those)\b",
    re.UNICODE | re.IGNORECASE,
)
_REFERENT_SCOPE_STOPWORDS = frozenset({
    "من", "اي", "أي", "في", "عن", "على", "هل", "ما", "وش", "ايش",
    "هذا", "هذه", "هذي", "ذلك", "تلك", "عندكم", "عندك", "ال",
    "انواع", "أنواع", "نوع", "types", "type", "the", "of", "what", "which",
})
_REFERENT_SCOPE_UNIT_TOKENS = frozenset({
    "كيلو", "كيلوغرام", "كجم", "جرام", "kg", "gram", "g",
    "لتر", "ml", "حبه", "حبة", "piece", "pack",
})


def _referent_scope_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in _normalize_ar(text or "").split():
        tok = raw.strip(" ؟?!.،,")
        tok = re.sub(r"^ال", "", tok)
        if len(tok) < 3:
            continue
        if tok in _REFERENT_SCOPE_STOPWORDS or tok in _REFERENT_SCOPE_UNIT_TOKENS:
            continue
        if tok.isdigit():
            continue
        tokens.add(tok)
    return tokens


def _looks_like_generic_category_browse(message: str) -> bool:
    msg = (message or "").strip()
    if not msg:
        return False
    if has_types_overview_ask(msg):
        return True
    try:
        from .commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            is_category_price_or_availability_message,
        )

        return bool(is_category_price_or_availability_message(msg, ""))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — browse-shape probe must not block routing
        return False


def _explicit_category_scope_broadening(message: str) -> bool:
    """Genuine catalog/category widening — existing owners only."""
    msg = (message or "").strip()
    if not msg:
        return False
    if has_explicit_broad_browse_request(msg):
        return True
    try:
        from .commerce.product_breadth_policy import (  # noqa: PLC0415
            explicit_broad_browse_requested,
            global_availability_browse_requested,
        )

        if explicit_broad_browse_requested(msg) or global_availability_browse_requested(msg):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — broadening probe must not block referent
        pass
    try:
        from .commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            is_generic_category_browse,
        )

        if is_generic_category_browse(msg):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — generic browse probe must not block referent
        pass
    try:
        from .postprocess.availability_guard_policy import (  # noqa: PLC0415
            browse_alternatives_requested,
        )

        if browse_alternatives_requested(msg):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — alternatives probe must not block referent
        pass
    return False


def _turn_scoped_to_canonical_referent(message: str, referent: Dict[str, Any]) -> bool:
    msg = (message or "").strip()
    if not msg or not isinstance(referent, dict):
        return False
    if _REFERENT_SCOPE_DEICTIC_RE.search(msg):
        return True
    title = str(referent.get("title") or referent.get("name") or "").strip()
    title_tokens = _referent_scope_tokens(title)
    if not title_tokens:
        return False
    generic_subject = ""
    try:
        from .commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            extract_browse_category_scope,
        )

        generic_subject = extract_browse_category_scope(msg, "") or ""
    except Exception:  # noqa: BLE001  # noqa: silent-ok — subject probe is optional for overlap
        generic_subject = ""
    if not generic_subject:
        generic_subject = extract_types_overview_query(msg) or ""
    generic_tokens = _referent_scope_tokens(generic_subject)
    distinctive = title_tokens - generic_tokens
    if not distinctive:
        return False
    return bool(distinctive & _referent_scope_tokens(msg))


def preserve_canonical_referent_over_category_browse(
    state: Any,
    message: str,
) -> bool:
    """True when a valid structured referent still owns this category-shaped turn.

    Generic category browse may proceed when the customer explicitly broadens
    scope, or when the turn is not bound to the current referent.
    """
    msg = (message or "").strip()
    if not msg:
        return False
    try:
        from .commerce.commerce_focus_owner import (  # noqa: PLC0415
            canonical_product_referent,
            has_structured_catalog_identity,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — missing focus owner must not block browse
        return False

    referent = canonical_product_referent(state)
    if not has_structured_catalog_identity(referent):
        return False
    if _explicit_category_scope_broadening(msg):
        return False
    if not _looks_like_generic_category_browse(msg):
        return False
    return _turn_scoped_to_canonical_referent(msg, referent or {})


def try_referent_scoped_product_reply_decision(ctx: BrainContext) -> Optional[Decision]:
    """Route a referent-scoped follow-up to Brain with the established product."""
    if not preserve_canonical_referent_over_category_browse(
        getattr(ctx, "state", None),
        getattr(ctx, "message", "") or "",
    ):
        return None
    try:
        from .commerce.catalog_reasoning_evidence import (  # noqa: PLC0415
            project_canonical_referent_catalog_facts,
        )
        from .commerce.commerce_focus_owner import (  # noqa: PLC0415
            canonical_product_referent,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — projection fallback uses live focus
        product = None
    else:
        product = project_canonical_referent_catalog_facts(
            state=getattr(ctx, "state", None),
            facts=getattr(ctx, "facts", None),
            merchant_context=getattr(ctx, "merchant_context", None),
        ) or canonical_product_referent(getattr(ctx, "state", None))
    if not isinstance(product, dict) or not product:
        return None
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "product",
            "product": dict(product),
            "block_catalog_browse": True,
        },
        reason="referent-scoped follow-up — preserve canonical catalog referent",
        confidence=0.9,
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
    _referent_reply = try_referent_scoped_product_reply_decision(ctx)
    if _referent_reply is not None:
        return _referent_reply
    try:
        from .commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )

        if global_availability_browse_requested(msg):
            return None
    except Exception:  # noqa: BLE001
        pass
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


def has_explicit_product_browse_intent(
    ctx: BrainContext,
    *,
    message: Optional[str] = None,
    source: str = "",
) -> bool:
    """Positive allowlist for any browse/search/listing surface."""
    msg = message if message is not None else (ctx.message or "")
    if not str(msg or "").strip():
        return False

    if has_explicit_broad_browse_request(msg):
        return True
    if has_explicit_product_inquiry(msg):
        inquiry_q = extract_inquiry_product_query(msg)
        if inquiry_q and is_open_category_inquiry_turn(msg, inquiry_q):
            return False
        return True
    if has_types_overview_ask(msg):
        return True
    if _extract_price_subject(msg):
        return True

    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    if intent_name == INTENT_ASK_PRODUCT:
        try:
            from .commerce.commerce_inquiry_boundary import extract_inquiry_subject  # noqa: PLC0415
            from .commerce.contact_escalation import (  # noqa: PLC0415
                is_branch_list_request,
                is_branch_location_order_tail,
            )
            from .order_context_gate import is_order_fulfillment_product_query  # noqa: PLC0415

            inquiry_subject = extract_inquiry_subject(msg) or ""
            if (
                inquiry_subject
                and _subject_has_product_substance(inquiry_subject)
                and not is_order_fulfillment_product_query(inquiry_subject)
                and not is_branch_location_order_tail(inquiry_subject)
                and not is_branch_list_request(msg)
            ):
                return True
        except Exception:  # noqa: BLE001  # noqa: silent-ok — optional inquiry boundary probe
            pass

    try:
        from .commerce.start_order_verb_guard import (  # noqa: PLC0415
            extract_start_order_product_query,
            is_bare_start_order_phrase,
        )

        if extract_start_order_product_query(msg):
            return True
        if is_bare_start_order_phrase(msg):
            return False
    except Exception:  # noqa: BLE001
        logger.exception("[PRODUCT_DISCOVERY_GATE] explicit_product_intent_start_order_probe_failed")

    slots = dict(getattr(getattr(ctx, "intent", None), "slots", None) or {})
    if str(slots.get("product_query") or slots.get("product_name") or "").strip():
        return True

    src = str(source or "").strip().lower()
    if src in _CONTINUATION_SOURCES and _has_prior_browse_context(ctx):
        return True

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

_CATEGORY_PRICE_PREFIX_RE = re.compile(
    r"^(?:اسعار|أسعار|سعر(?:ات)?|ثمن|تكلفة|"
    rf"{_PRICE_ASK_SUFFIX}|how\s*much|price(?:s)?)"
    r"\s+(?:ال)?(.+)$",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_SUFFIX_RE = re.compile(
    r"^(.{2,80}?)\s+"
    rf"{_PRICE_ASK_SUFFIX}\s*"
    rf"(?:per\s+)?(?:{_PRICE_UNIT_SUFFIX})?"
    r"\s*$",
    re.UNICODE | re.IGNORECASE,
)

_INLINE_PRICE_SUBJECT_RE = re.compile(
    r"(?:كم\s*(?:سعر|ثمن)|بكم(?:\s+ال)?|سعر\s*(?:ال)?|"
    r"(?:what\s+is\s+the\s+)?price\s+of\s+(?:the\s+)?|"
    r"how\s*much\s+(?:(?:is\s+)?(?:it\s+)?for\s+)?"
    r"(?:is\s+)?(?:the\s+)?)"
    r"(?P<subject>.+?)"
    r"(?="
    r"\s*[,،]?\s+(?:وهل|هل)\s+|"
    r"\s*[,،]?\s+and\s+(?:(?:is|are)\s+(?:it|this|the\s+product)?\s*)?"
    r"(?:available|availability|in\s+stock)\b|"
    r"[?؟!]|$"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SUBJECT_FIRST_PRICE_RE = re.compile(
    r"^(?P<subject>.{2,80}?)\s+"
    r"(?:كم\s+سعر(?:ه|ها|هم)?|بكم|price|how\s+much(?:\s+is\s+it)?)"
    r"(?=\s*(?:[,،]?\s*(?:وهل|هل|و?متوفر(?:ة|ه)?|و?موجود(?:ة|ه)?|and\b))|[?؟!]|$)",
    re.UNICODE | re.IGNORECASE,
)

_AVAILABILITY_FIRST_PRICE_RE = re.compile(
    r"^(?P<subject>.{2,80}?)\s+"
    r"(?:هل\s+)?(?:متوفر(?:ة|ه)?|موجود(?:ة|ه)?|available|in\s+stock)"
    r"(?:\s+عندكم)?\s*[?؟!]?\s*(?:(?:و|and)\s*)?"
    r"(?:كم\s+سعر(?:ه|ها|هم)?|بكم|how\s+much(?:\s+is\s+it)?)",
    re.UNICODE | re.IGNORECASE,
)

_SUBJECT_BEFORE_PRICE_PRONOUN_RE = re.compile(
    r"^(?:هل\s+)?عندكم\s+(?P<subject>.{2,80}?)\s+"
    r"(?:و\s*)?كم\s+سعر(?:ه|ها|هم)",
    re.UNICODE | re.IGNORECASE,
)

_STANDALONE_GREETING_SLOT_TOKENS = frozenset({
    "سلام", "سلام عليكم", "السلام", "السلام عليكم",
    "مرحبا", "مرحباً", "اهلا", "أهلا", "أهلاً", "هلا",
    "شكرا", "شكراً", "مشكور", "مشكوره",
    "hello", "hi", "hey", "thanks", "thank you",
})

_WEAK_PRONOUN_SLOT_TOKENS = frozenset({
    "ه", "ها", "هم", "هو", "هي", "هذا", "هذه", "هذي", "ذا",
    "it", "its", "this", "that",
    "هل", "وهل", "and", "availability", "and availability",
})

_WEAK_REFERENCE_PRICE_MESSAGE_RE = re.compile(
    r"^\s*(?:(?:شكرا|شكراً|مشكور(?:ه)?|thanks|thank\s+you)\s*[,،!]?\s*)?(?:"
    r"(?:كم\s+)?سعر(?:ه|ها|هم)\b|"
    r"(?:كم\s+)?سعر\s+(?:هذا|هذه|هذي|ذا)\b"
    r"(?=\s*(?:[?؟!.,،]|$|(?:وهل|هل|و?متوفر(?:ة|ه)?|و?موجود(?:ة|ه)?)\b))|"
    r"how\s+much\s+(?:is\s+)?(?:it|this|that)\b"
    r"(?=\s*(?:[?!.]|$|(?:and\s+)?(?:available|in\s+stock)\b))"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _message_commerce_probes(message: str) -> list[str]:
    """Message variants for deterministic commerce-subject extraction."""
    raw = (message or "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    probes: list[str] = []

    def _add(candidate: str) -> None:
        c = (candidate or "").strip()
        if c and c not in seen:
            seen.add(c)
            probes.append(c)

    _add(raw)
    try:
        from .intent.rules import _strip_greeting_residue  # noqa: PLC0415

        _add(_strip_greeting_residue(raw))
    except Exception:  # noqa: BLE001
        pass
    for line in raw.splitlines():
        _add(line)
        try:
            from .intent.rules import _strip_greeting_residue  # noqa: PLC0415

            _add(_strip_greeting_residue(line))
        except Exception:  # noqa: BLE001  # noqa: silent-ok — optional per-line greeting residue probe
            pass
    return probes


def _is_greeting_or_social_slot_token(token: str, message: str = "") -> bool:
    """True when an LLM slot token is greeting/social — not a catalog subject."""
    q = (token or "").strip()
    if not q:
        return True
    norm = _normalize_ar(q)
    if norm in _STANDALONE_GREETING_SLOT_TOKENS or norm in _WEAK_PRONOUN_SLOT_TOKENS:
        return True
    try:
        from .commerce.catalog_search_evidence import is_discourse_only_query  # noqa: PLC0415

        if is_discourse_only_query(q):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from .intent.rules import (  # noqa: PLC0415
            _GREETING_RESIDUE_LEAD_TOKENS,
            _strip_greeting_residue,
        )

        for greeting in _GREETING_RESIDUE_LEAD_TOKENS:
            gn = _normalize_ar(greeting)
            if norm == gn:
                return True
            if len(norm) <= 6 and gn.startswith(norm):
                return True
        if message:
            residue = (_strip_greeting_residue(message) or "").strip()
            if residue and len(residue) < len(message) - 2:
                greeting_only = message.replace(residue, "", 1).strip(" ,،؟?!.")
                if _normalize_ar(greeting_only) == norm:
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _deterministic_commerce_subject(ctx: BrainContext, extracted: str = "") -> str:
    """Extract catalog subject from message evidence — not weak LLM slots."""
    msg = ctx.message or ""
    ext = str(extracted or "").strip()
    if ext and _subject_has_product_substance(ext) and not _is_greeting_or_social_slot_token(ext, msg):
        return ext

    try:
        from .commerce.price_turn_classifier import normalize_price_subject  # noqa: PLC0415

        price_subject = normalize_price_subject(ctx)
        if price_subject and _subject_has_product_substance(price_subject):
            return price_subject
    except Exception:  # noqa: BLE001
        pass

    for probe in _message_commerce_probes(msg):
        subject = _extract_price_subject(probe)
        if subject and _subject_has_product_substance(subject):
            return subject
        inquiry = extract_inquiry_product_query(probe)
        if inquiry and _subject_has_product_substance(inquiry):
            return inquiry
        try:
            from .commerce.catalog_query_normalization import (  # noqa: PLC0415
                extract_english_order_product_query,
            )

            en = extract_english_order_product_query(probe)
            if en and _subject_has_product_substance(en):
                return en
        except Exception:  # noqa: BLE001  # noqa: silent-ok — optional English catalog query probe
            pass
    return ""


def _subject_has_product_substance(candidate: str) -> bool:
    """True when ``candidate`` is more than bare price/unit tokens."""
    norm = _normalize_ar(candidate or "")
    norm = re.sub(r"^ال", "", norm)
    if not norm:
        return False
    if norm in _WEAK_PRONOUN_SLOT_TOKENS:
        return False
    if _is_greeting_or_social_slot_token(candidate):
        return False
    tokens = [t for t in norm.split() if t]
    if not tokens:
        return False
    non_unit = [t for t in tokens if t not in _UNIT_ONLY_TOKENS]
    return any(len(t) >= 2 for t in non_unit)


def _clean_price_subject_candidate(candidate: str) -> str:
    """Trim availability tails and fluff from a price-subject candidate."""
    core = (candidate or "").strip(" ,،؟?!.")
    core = re.sub(
        r"^(?:(?:السلام\s+عليكم|سلام\s+عليكم|هلا|اهلا|أهلا|مرحبا)"
        r"[,،]?\s+|"
        r"(?:شكرا|شكراً|مشكور(?:ه)?)\s*[,،]?\s+|"
        r"(?:لو\s+سمحت|من\s+فضلك|ممكن|فضلا|فضلاً)\s+|"
        r"(?:hi|hello|hey)[,!]?\s+|"
        r"(?:هل\s+)?عندكم\s+|هل\s+|"
        r"(?:هذا|هذه|هذي|ذا)\s+|(?:is\s+)?(?:this|that)\s+|"
        r"(?:is\s+)?(?:of\s+|for\s+)?(?:the\s+)?)",
        "",
        core,
        flags=re.UNICODE | re.IGNORECASE,
    ).strip()
    core = re.sub(r"^ال(?=\S{2})", "", core, flags=re.UNICODE).strip()
    core = re.sub(
        r"\s+(?:"
        r"(?:وهل|هل)\s+.*|"
        r"(?:و?التوفر|و?متوفر(?:ة|ه)?|و?موجود(?:ة|ه)?)(?:\s+عندكم)?\s*|"
        r"and\s+(?:(?:is|are)\s+(?:it|this|the\s+product)?\s*)?"
        r"(?:available|availability|in\s+stock).*|"
        r"(?:is|are)\s+(?:it|this|the\s+product)?\s*"
        r"(?:available|in\s+stock).*)$",
        "",
        core,
        flags=re.UNICODE | re.IGNORECASE,
    ).strip(" ,،؟?!.")
    if (
        _normalize_ar(core) in _WEAK_PRONOUN_SLOT_TOKENS
        or _is_greeting_or_social_slot_token(core)
    ):
        return ""
    return core


def _extract_price_subject_from_probe(message: str) -> str:
    raw = (message or "").strip()
    if not raw:
        return ""
    norm = _normalize_ar(raw)
    prefix_match = _CATEGORY_PRICE_PREFIX_RE.match(norm)
    if prefix_match:
        candidate = _clean_price_subject_candidate(prefix_match.group(1) or "")
        if candidate and _subject_has_product_substance(candidate):
            return candidate
    for prefix in (
        "كم سعر",
        "بكم",
        "اسعار",
        "أسعار",
        "سعر",
        "ثمن",
        "قد ايش",
        "how much",
    ):
        pn = _normalize_ar(prefix)
        if norm.startswith(pn):
            rest = _clean_price_subject_candidate(raw[len(prefix):])
            if rest and _subject_has_product_substance(rest):
                return rest
            return ""
    availability_first = _AVAILABILITY_FIRST_PRICE_RE.search(raw)
    if availability_first:
        candidate = _clean_price_subject_candidate(
            availability_first.group("subject") or "",
        )
        if candidate and _subject_has_product_substance(candidate):
            return candidate
    subject_first = _SUBJECT_FIRST_PRICE_RE.search(raw)
    if subject_first:
        candidate = _clean_price_subject_candidate(
            subject_first.group("subject") or "",
        )
        if candidate and _subject_has_product_substance(candidate):
            return candidate
    before_pronoun = _SUBJECT_BEFORE_PRICE_PRONOUN_RE.search(raw)
    if before_pronoun:
        candidate = _clean_price_subject_candidate(
            before_pronoun.group("subject") or "",
        )
        if candidate and _subject_has_product_substance(candidate):
            return candidate
    m = _PRICE_SUFFIX_RE.match(raw.strip(" ,،؟?!."))
    if m:
        candidate = _clean_price_subject_candidate(m.group(1) or "")
        if candidate and _subject_has_product_substance(candidate):
            return candidate
    inline = _INLINE_PRICE_SUBJECT_RE.search(raw)
    if inline:
        candidate = _clean_price_subject_candidate(inline.group("subject") or "")
        if candidate and _subject_has_product_substance(candidate):
            return candidate
    return ""


def _extract_price_subject(message: str) -> str:
    """Recover a product name from price-style messages.

    Supported shapes (platform-wide, any catalog wording):
      * ``<product> بكم`` / ``<product> كم سعره``
      * ``<product> بكم <unit>``  (e.g. per-kilo / per-litre asks)
      * ``بكم <product>`` / ``كم سعر <product>``
      * greeting-prefixed turns after residue peel / inline price markers
    """
    for probe in _message_commerce_probes(message):
        subject = _extract_price_subject_from_probe(probe)
        if subject:
            return subject
    return ""


def extract_price_subject(message: str) -> str:
    """Public deterministic subject extractor for routing guards."""
    return _extract_price_subject(message)


def _is_weak_reference_price_message(message: str) -> bool:
    return bool(_WEAK_REFERENCE_PRICE_MESSAGE_RE.search(message or ""))


def _resolved_product_query(ctx: BrainContext, extracted: str = "") -> str:
    msg = ctx.message or ""
    if (
        getattr(ctx.state, "current_product_focus", None)
        and _is_weak_reference_price_message(msg)
    ):
        return ""
    try:
        from .commerce.price_turn_classifier import (  # noqa: PLC0415
            PriceTurnKind,
            classify_price_turn,
        )

        if getattr(ctx.state, "current_product_focus", None) and classify_price_turn(
            ctx
        ) in {
            PriceTurnKind.PRONOUN_REFERENCE,
            PriceTurnKind.PRICE_COMMENT,
            PriceTurnKind.UNIT_PRICE_REFERENCE,
        }:
            return ""
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional price-turn context probe
        pass
    deterministic = _deterministic_commerce_subject(ctx, extracted)
    if deterministic:
        return deterministic
    intent = ctx.intent
    slots = getattr(intent, "slots", None) or {}
    slot_query = (
        str(slots.get("product_query") or "").strip()
        or str(slots.get("product_name") or "").strip()
    )
    if slot_query and not _is_greeting_or_social_slot_token(slot_query, msg):
        return slot_query
    return ""


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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional solution-seeking classifier
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
    try:
        from .commerce.selection_context import (  # noqa: PLC0415
            has_active_selection_context,
            is_selection_followup_message,
        )

        if has_active_selection_context(ctx.state) and is_selection_followup_message(
            ctx.message or "",
        ):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional selection context probe
        pass
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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional price-turn classification probe
        if _extract_price_subject(msg):
            return False
    norm = _normalize_ar(msg)
    return bool(_PRICE_ONLY_RE.search(norm))


def _has_fulfillment_message_context(message: str) -> bool:
    try:
        from .order_context_gate import detect_fulfillment_update  # noqa: PLC0415

        if detect_fulfillment_update(message or "", {}):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional fulfillment update probe
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
    try:
        from .commerce.selection_context import (  # noqa: PLC0415
            get_presented_products,
            has_active_selection_context,
        )

        if has_active_selection_context(state) and get_presented_products(state):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional selection context probe
        pass
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

    try:
        from .turn.ownership import (  # noqa: PLC0415
            FALLBACK_PRODUCT_DISCOVERY,
            ownership_forbids_fallback,
        )

        owned = ownership_forbids_fallback(ctx, FALLBACK_PRODUCT_DISCOVERY)
        if owned:
            return owned
    except Exception:  # noqa: BLE001
        pass

    if intent_name == "product_visual_request":
        return None

    try:
        from .intent.rules import is_pure_greeting_without_commerce  # noqa: PLC0415

        if is_pure_greeting_without_commerce(msg):
            return "pure_greeting"
    except Exception:  # noqa: BLE001
        pass

    if intent_name == INTENT_NEED_BASED_PRODUCT_ADVICE or is_need_based_product_advice(ctx):
        return "health_advisory"

    negative_context = product_browse_negative_context_reason(msg)
    if negative_context and not has_explicit_product_browse_intent(ctx, message=msg, source=src):
        return negative_context

    try:
        from .commerce.catalog_order_checkout import is_active_catalog_checkout  # noqa: PLC0415

        if is_active_catalog_checkout(ctx):
            return "active_catalog_checkout"
    except Exception:  # noqa: BLE001
        logger.exception("[PRODUCT_DISCOVERY_GATE] active_catalog_checkout_probe_failed")

    try:
        from .turn.ownership import has_explicit_catalog_browse_intent  # noqa: PLC0415

        if has_explicit_catalog_browse_intent(ctx, message=msg, intent_name=intent_name):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional browse policy probe
        pass

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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional non-commerce classification probe
        pass

    if is_price_without_product_context(ctx):
        return "price_without_product_context"

    try:
        from .commerce.commerce_objective import (  # noqa: PLC0415
            COMMERCE_OBJECTIVE_DISCOVERY,
            get_commerce_objective,
        )
        from .commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            _is_valid_scope_token,
            extract_browse_category_scope,
        )

        if get_commerce_objective(ctx.state) == COMMERCE_OBJECTIVE_DISCOVERY:
            scope = extract_browse_category_scope(msg, "")
            if scope and _is_valid_scope_token(scope):
                words = [w for w in str(msg or "").split() if w.strip()]
                if len(words) <= 3:
                    return None
    except Exception:
        logger.exception("[PRODUCT_DISCOVERY_GATE] discovery_category_followup_probe_failed")

    if has_explicit_product_inquiry(msg):
        return None

    try:
        from .commerce.selection_context import (  # noqa: PLC0415
            has_active_selection_context,
            is_selection_followup_message,
        )

        if has_active_selection_context(ctx.state) and is_selection_followup_message(msg):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional selection context probe
        pass

    try:
        from .commerce.start_order_verb_guard import is_bare_start_order_phrase  # noqa: PLC0415

        if is_bare_start_order_phrase(msg):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional bare start-order probe
        pass

    if src in _TOP_PRODUCTS_SOURCES and not has_explicit_broad_browse_request(msg):
        return "weak_or_unknown_intent"

    if src in _CONTINUATION_SOURCES:
        if not _has_prior_browse_context(ctx):
            return "weak_or_unknown_intent"
        return None

    explicit_product_browse = has_explicit_product_browse_intent(ctx, message=msg, source=src)
    if not explicit_product_browse:
        return "missing_explicit_product_browse_intent"

    if explicit_product_browse:
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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional escalation suppression probe
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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional generic discovery block probe
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


def try_category_price_browse_decision(ctx: BrainContext) -> Optional[Decision]:
    """Route category-level price/availability asks to scoped catalog browse."""
    msg = ctx.message or ""
    if not msg:
        return None
    if preserve_canonical_referent_over_category_browse(getattr(ctx, "state", None), msg):
        return None
    intent_name = str(getattr(ctx.intent, "name", "") or "")
    if intent_name not in (INTENT_ASK_PRICE, INTENT_ASK_PRODUCT):
        return None

    try:
        from .commerce.catalog_order_checkout import is_current_catalog_order_submitted  # noqa: PLC0415

        if is_current_catalog_order_submitted(ctx):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog-order guard must not block price routing
        pass

    if not getattr(ctx.facts, "has_products", False):
        return None

    try:
        from .commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            active_category_from_state,
            extract_browse_category_scope,
            is_category_price_or_availability_message,
        )
        from .catalog.catalog_browse_scope_resolver import (  # noqa: PLC0415
            active_catalog_group_slug_from_state,
            resolve_catalog_category_scope,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[PRODUCT_DISCOVERY] category_price_browse_import_failed")
        return None

    scope_subject = extract_browse_category_scope(msg, "")
    price_subject = _extract_price_subject(msg)
    if scope_subject and _is_greeting_or_social_slot_token(scope_subject, msg):
        scope_subject = ""
    subject = scope_subject
    if price_subject and _subject_has_product_substance(price_subject):
        if not scope_subject or _is_greeting_or_social_slot_token(scope_subject, msg):
            subject = price_subject
    if not subject or not _subject_has_product_substance(subject):
        return None

    if not is_category_price_or_availability_message(msg, ""):
        return None

    if price_subject and not is_generic_category_noun(price_subject):
        return None

    db = getattr(ctx, "_db", None)
    tenant_id = getattr(ctx, "tenant_id", None)
    if db is None or tenant_id is None:
        if not is_generic_category_noun(subject):
            return None
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": subject,
                "source": "category_browse",
                "category_scope": subject,
            },
            reason="category price browse — subject token scope",
            confidence=0.88,
        )

    scope = resolve_catalog_category_scope(
        db,
        int(tenant_id),
        msg,
        subject,
        active_group_slug=active_catalog_group_slug_from_state(ctx.state),
        active_category=active_category_from_state(ctx.state),
    )
    if not scope.must_filter_by_category or scope.specific_product:
        return None

    args: Dict[str, Any] = {
        "query": scope.query_subject or scope.matched_category or subject,
        "source": "category_browse",
        "category_scope": scope.matched_category or subject,
        "use_catalog_prices_only": True,
    }
    if scope.catalog_group_id is not None:
        args["catalog_group_id"] = scope.catalog_group_id
    return Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args=args,
        reason="category price browse — merchant catalog category scope",
        confidence=0.9,
    )


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

    _category_browse = try_category_price_browse_decision(ctx)
    if _category_browse is not None:
        return _category_browse
    _referent_reply = try_referent_scoped_product_reply_decision(ctx)
    if _referent_reply is not None:
        return _referent_reply

    msg = ctx.message or ""
    focus = ctx.state.current_product_focus
    product_query = _resolved_product_query(ctx, extracted_product_query)

    try:
        from .commerce.selection_context import (  # noqa: PLC0415
            try_selection_context_decision,
        )

        _sel_dec = try_selection_context_decision(ctx)
        if _sel_dec is not None:
            return _sel_dec
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional selection context routing
        pass

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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional price-turn classifier import
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
        from .commerce.start_order_verb_guard import is_bare_start_order_phrase  # noqa: PLC0415
        from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: PLC0415

        if is_bare_start_order_phrase(msg):
            return Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "", "source": "top_products_start_order"},
                reason=f"bare start-order opener — recover from blocked top_products ({reason})",
                confidence=0.90,
            )
    except Exception:  # noqa: BLE001
        logger.exception("[PRODUCT_DISCOVERY_GATE] bare_start_order_recovery_probe_failed")
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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — pure greeting persona path optional
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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — conversational priority optional
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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — clarify guard must not block discovery gate
        pass

    try:
        from .commerce.solution_seeking import log_intelligent_need_clarification  # noqa: PLC0415

        log_intelligent_need_clarification(
            tenant_id=tenant_id,
            axis="general_attribute",
            reason=reason,
            preview=msg,
        )
    except Exception:
        logger.exception(
            "[PRODUCT_DISCOVERY_GATE] intelligent_need_clarification_log_failed",
        )

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
    "extract_price_subject",
    "extract_types_overview_query",
    "has_explicit_broad_browse_request",
    "has_explicit_product_inquiry",
    "has_inquiry_phrasing",
    "has_types_overview_ask",
    "is_open_category_inquiry_turn",
    "INQUIRY_CLASS_BROAD",
    "INQUIRY_CLASS_OPEN",
    "INQUIRY_CLASS_BROWSE",
    "INQUIRY_CLASS_SPECIFIC",
    "is_generic_category_noun",
    "is_need_based_product_advice",
    "is_solution_seeking_commerce",
    "is_price_without_product_context",
    "log_inquiry_class",
    "log_product_discovery_blocked",
    "preserve_canonical_referent_over_category_browse",
    "product_discovery_block_reason",
    "should_block_generic_product_discovery",
    "should_suppress_recommendation_escalation",
    "try_broad_category_inquiry_decision",
    "try_price_query_decision",
    "try_referent_scoped_product_reply_decision",
    "try_types_overview_decision",
]
