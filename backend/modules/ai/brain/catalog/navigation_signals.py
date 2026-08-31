"""
catalog/navigation_signals.py
─────────────────────────────
Signal-based catalog browse intent — morphological frames, not phrase whitelists.

Operational routing only; reply wording stays with DiscoveryPresentationComposer.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..types import (
    BrainContext,
    INTENT_GREETING,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_START_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
)

logger = logging.getLogger("nahla.brain.catalog.navigation_signals")

_DIA = r"[\u064B-\u065F\u0640]"

# Inventory / assortment nouns plus merchant-sell frames (what the store sells).
_INVENTORY_SUBJECT_RE = re.compile(
    r"(?:"
    r"انواع|اقسام|أقسام|مجموعات|خيارات|منتجات|متوفر|متاح|"
    r"تبيعون|تبيعو|تبيع|"
    r"available|catalog|types|categories|sections|products|sell(?:s|ing)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_MERCHANT_SCOPE_RE = re.compile(
    r"(?:"
    r"عند(?:كم|ك|نا)|available|in\s+(?:store|shop)|your\s+(?:store|shop|products)"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_QUESTION_OPEN_RE = re.compile(
    r"(?:^|[\s؟?])"
    r"(?:وش|ايش|ما|what|which|show|list|display|ورني|اب(?:ي|غ)|ودي|حاب|بدي)",
    re.UNICODE | re.IGNORECASE,
)
_EXPLORE_VERB_RE = re.compile(
    r"(?:ورني|اعرض|عرض|browse|explore|list|show|details?)",
    re.UNICODE | re.IGNORECASE,
)
_ADVISORY_RE = re.compile(
    r"(?:"
    r"انسب|تنصحن|نصيح|recommend|best\s+for|which\s+(?:one|is\s+better)|"
    r"ايه(?:ما|م)\s+(?:احسن|أحسن|افضل|أفضل|انسب|أنسب)"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    r"(?:"
    r"الفرق|فرق\s+between|compare|comparison|أيهما|ايهما|better|"
    r"وش\s+(?:ال)?(?:فرق|اختلاف)"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_PHANTOM_SCOPE_STOPWORDS = frozenset({
    "الي", "اللي", "الموجود", "الموجوده", "الموجودة", "عندكم", "عندك", "عندنا",
    "the", "that", "this", "available", "here",
})

_BROWSE_INTENTS = frozenset({
    INTENT_GREETING,
    "greeting",
    INTENT_START_ORDER,
    "start_order",
    "general",
    "hesitation",
})

HIGH_BROWSE_THRESHOLD = 0.62
MEDIUM_BROWSE_THRESHOLD = 0.40


def _normalize_ar(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(_DIA, "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[؟?!.,؛:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass(frozen=True)
class CatalogNavigationSignals:
    catalog_browse_intent: bool = False
    specific_product_target: bool = False
    product_information_question: bool = False
    shipping_or_order_status: bool = False
    support_or_staff_contact: bool = False
    advisory_or_comparison: bool = False
    navigation_state: bool = False
    confidence: float = 0.0
    catalog_browse_score: float = 0.0
    hard_blocked: bool = False
    block_reason: str = ""
    exit_reason: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)


def is_phantom_category_scope(scope: str) -> bool:
    norm = _normalize_ar(scope or "")
    if not norm:
        return True
    tokens = norm.split()
    if len(tokens) == 1 and tokens[0] in _PHANTOM_SCOPE_STOPWORDS:
        return True
    if all(tok in _PHANTOM_SCOPE_STOPWORDS for tok in tokens):
        return True
    return False


_ORDER_CONTINUATION_RE = re.compile(
    r"(?:"
    r"اطلب(?:ه|ها|هم)|اشتري(?:ه|ها|هم)|"
    r"order\s+it|buy\s+it|"
    r"اب(?:ي|غ)(?:ه|ها)|"
    r"أطلب(?:ه|ها)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _ordering_continuation_blocks_navigator(ctx: BrainContext) -> bool:
    state = getattr(ctx, "state", None)
    if state is None:
        return False
    stage = str(getattr(state, "stage", "") or "")
    if stage not in {"ordering", "checkout", "deciding"}:
        return False
    if not getattr(state, "current_product_focus", None):
        return False
    msg = _normalize_ar(getattr(ctx, "message", "") or "")
    if not msg:
        return False
    if _ORDER_CONTINUATION_RE.search(msg):
        return True
    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    return intent_name == INTENT_START_ORDER and len(msg.split()) <= 4


def evaluate_catalog_navigation_signals(ctx: BrainContext) -> CatalogNavigationSignals:
    """Evaluate browse/advisory/blocker signals for one inbound turn."""
    msg = ctx.message or ""
    norm = _normalize_ar(msg)
    intent_name = str(getattr(ctx.intent, "name", "") or "")
    evidence: Dict[str, Any] = {}

    product_information = False
    try:
        from ..state.product_information_topic import (  # noqa: PLC0415
            detect_product_information_topic_shift,
            product_information_blocks_checkout,
        )

        product_information = bool(
            detect_product_information_topic_shift(msg)
            or product_information_blocks_checkout(ctx)
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product info probe
        product_information = False

    shipping_or_order_status = intent_name == INTENT_TRACK_ORDER
    try:
        from ..commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            is_explicit_order_tracking_request,
        )

        shipping_or_order_status = shipping_or_order_status or bool(
            is_explicit_order_tracking_request(
                msg,
                state=getattr(ctx, "state", None),
                history=getattr(ctx, "history", None),
                commerce_bundle=getattr(ctx, "commerce_bundle", None),
            )
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional tracking probe
        pass

    product_visual_request = intent_name == INTENT_PRODUCT_VISUAL_REQUEST

    support_or_staff_contact = intent_name == INTENT_TALK_HUMAN
    try:
        from ..commerce.contact_escalation import (  # noqa: PLC0415
            is_branch_list_request,
        )

        if is_branch_list_request(msg):
            support_or_staff_contact = True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional support probe
        pass

    specific_product_target = False
    product_query = ""
    try:
        from ..discovery.entry import extract_order_product_query  # noqa: PLC0415

        product_query = extract_order_product_query(ctx) or ""
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product query probe
        pass

    navigation_state = False
    try:
        from ..commerce.collection_navigation import (  # noqa: PLC0415
            get_presented_collections,
            has_active_collection_navigation_context,
            is_collection_navigation_message,
        )

        navigation_state = bool(
            has_active_collection_navigation_context(getattr(ctx, "state", None))
            and (
                get_presented_collections(getattr(ctx, "state", None))
                or is_collection_navigation_message(msg)
            )
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional navigation context probe
        pass

    advisory_or_comparison = bool(
        norm
        and (_ADVISORY_RE.search(norm) or _COMPARISON_RE.search(norm))
    )

    general_store_browse = bool(
        norm
        and _QUESTION_OPEN_RE.search(norm)
        and _MERCHANT_SCOPE_RE.search(norm)
    )
    inventory_frame = bool(
        norm
        and _INVENTORY_SUBJECT_RE.search(norm)
        and (_MERCHANT_SCOPE_RE.search(norm) or _QUESTION_OPEN_RE.search(norm))
    )
    explore_frame = bool(
        norm
        and _EXPLORE_VERB_RE.search(norm)
        and _INVENTORY_SUBJECT_RE.search(norm)
    )

    if product_query and not is_phantom_category_scope(product_query):
        if not (inventory_frame or general_store_browse):
            specific_product_target = True
            evidence["product_query"] = product_query

    order_without_target = False
    has_product_focus = bool(getattr(getattr(ctx, "state", None), "current_product_focus", None))
    try:
        from ..commerce.start_order_verb_guard import (  # noqa: PLC0415
            is_bare_start_order_phrase,
        )

        order_without_target = (
            not has_product_focus
            and (
                is_bare_start_order_phrase(msg)
                or (intent_name == INTENT_START_ORDER and not specific_product_target)
            )
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional start-order probe
        order_without_target = (
            not has_product_focus
            and intent_name == INTENT_START_ORDER
            and not specific_product_target
        )

    intent_browse = intent_name in _BROWSE_INTENTS and not specific_product_target

    score = 0.0
    if inventory_frame:
        score += 0.36
    if general_store_browse:
        score += 0.38
    if explore_frame:
        score += 0.22
    # Generic start_order / bare order-entry must not grant groups ownership.
    # COLLECTIONS_HEADER / COLLECTIONS_CLOSING stay reachable via explicit browse frames only.
    if intent_browse:
        score += 0.10
    if navigation_state:
        score += 0.20

    hard_blocked = False
    block_reason = ""
    if product_information:
        hard_blocked = True
        block_reason = "product_information_question"
    elif shipping_or_order_status:
        hard_blocked = True
        block_reason = "shipping_or_order_status"
    elif product_visual_request:
        hard_blocked = True
        block_reason = "product_visual_request"
    elif support_or_staff_contact:
        hard_blocked = True
        block_reason = "support_or_staff_contact"
    elif specific_product_target and not navigation_state:
        hard_blocked = True
        block_reason = "specific_product_target"

    exit_reason = ""
    if advisory_or_comparison and not hard_blocked:
        exit_reason = "advisory_or_comparison"

    catalog_browse_intent = (
        not hard_blocked
        and not advisory_or_comparison
        and (
            inventory_frame
            or general_store_browse
            or score >= HIGH_BROWSE_THRESHOLD
        )
    )

    scoped_catalog_subject = ""
    try:
        from ..product_discovery_gate import (  # noqa: PLC0415
            extract_inquiry_product_query,
            extract_types_overview_query,
            has_types_overview_ask,
        )

        if has_types_overview_ask(msg):
            scoped_catalog_subject = extract_types_overview_query(msg) or ""
        if not scoped_catalog_subject:
            scoped_catalog_subject = extract_inquiry_product_query(msg) or ""
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional scoped query probe
        pass
    if scoped_catalog_subject and not is_phantom_category_scope(scoped_catalog_subject):
        catalog_browse_intent = False
        if not hard_blocked:
            exit_reason = exit_reason or "scoped_catalog_query"

    if _ordering_continuation_blocks_navigator(ctx):
        catalog_browse_intent = False
        if not hard_blocked:
            exit_reason = exit_reason or "ordering_continuation"

    if has_product_focus and intent_name == INTENT_START_ORDER:
        catalog_browse_intent = False
        if not hard_blocked:
            exit_reason = exit_reason or "start_order_with_product_focus"

    confidence = min(0.99, max(0.0, score if score else (0.72 if catalog_browse_intent else 0.0)))

    evidence.update({
        "inventory_frame": inventory_frame,
        "general_store_browse": general_store_browse,
        "explore_frame": explore_frame,
        "order_without_target": order_without_target,
        "intent_browse": intent_browse,
        "navigation_state": navigation_state,
        "score": round(score, 3),
        "product_visual_request": product_visual_request,
    })

    return CatalogNavigationSignals(
        catalog_browse_intent=catalog_browse_intent,
        specific_product_target=specific_product_target,
        product_information_question=product_information,
        shipping_or_order_status=shipping_or_order_status,
        support_or_staff_contact=support_or_staff_contact,
        advisory_or_comparison=advisory_or_comparison,
        navigation_state=navigation_state,
        confidence=confidence,
        catalog_browse_score=score,
        hard_blocked=hard_blocked,
        block_reason=block_reason,
        exit_reason=exit_reason,
        evidence=evidence,
    )


def message_indicates_catalog_browse(message: str, *, intent_name: str = "") -> bool:
    """Lightweight browse-frame probe for modules without BrainContext."""
    norm = _normalize_ar(message or "")
    if not norm:
        return False
    inventory_frame = bool(
        _INVENTORY_SUBJECT_RE.search(norm)
        and (_MERCHANT_SCOPE_RE.search(norm) or _QUESTION_OPEN_RE.search(norm))
    )
    general_store_browse = bool(
        norm
        and _QUESTION_OPEN_RE.search(norm)
        and _MERCHANT_SCOPE_RE.search(norm)
    )
    explore_frame = bool(
        _EXPLORE_VERB_RE.search(norm) and _INVENTORY_SUBJECT_RE.search(norm)
    )
    score = 0.0
    if inventory_frame:
        score += 0.36
    if general_store_browse:
        score += 0.38
    if explore_frame:
        score += 0.22
    if str(intent_name or "").strip() in _BROWSE_INTENTS:
        score += 0.10
    return score >= HIGH_BROWSE_THRESHOLD or inventory_frame or general_store_browse


_MORE_FRAME_RE = re.compile(
    r"(?:"
    r"^(?:المزيد|اكثر|أكثر|more|next|continue|كمل|كمان|باقي|غيرهم|غيرها|غيره)"
    r"|(?:ودي|ابي|ابغ|أبي|أبغ|show|list)\s*(?:المزيد|more|next|باقي|others?)"
    r"|(?:products?|options?|choices?|items?)\s*(?:more|next)"
    r"|(?:ورني|اعرض)\s*(?:باقي|المزيد|more)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def is_group_products_more_request(message: str) -> bool:
    """Morphological 'show more in this group' — not a closed phrase whitelist."""
    norm = _normalize_ar(message or "")
    if not norm or len(norm.split()) > 6:
        return False
    return bool(_MORE_FRAME_RE.search(norm))


is_navigation_more_request = is_group_products_more_request


_START_OVER_RE = re.compile(
    r"(?:"
    r"^(?:البدا(?:ية|يه)|من\s*البدا(?:ية|يه)|back\s*to\s*start|start\s*over|الصف(?:حة|ه)\s*الاول(?:ى|ي))$"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def is_collections_start_over_request(message: str) -> bool:
    norm = _normalize_ar(message or "")
    if not norm:
        return False
    return bool(_START_OVER_RE.search(norm))


__all__ = [
    "CatalogNavigationSignals",
    "HIGH_BROWSE_THRESHOLD",
    "MEDIUM_BROWSE_THRESHOLD",
    "evaluate_catalog_navigation_signals",
    "is_collections_start_over_request",
    "is_group_products_more_request",
    "is_navigation_more_request",
    "is_phantom_category_scope",
    "message_indicates_catalog_browse",
]
