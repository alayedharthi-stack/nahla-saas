"""
intent_priority/analyzer.py
───────────────────────────
Customer Intent Priority Layer — extract and rank conversational elements.

Platform-wide, tenant-agnostic. Uses closed element *categories* with
extensible pattern sets — never merchant-specific phrase hardcoding.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from ..types import (
    INTENT_ASK_LOCATION,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_GREETING,
    INTENT_SOCIAL,
    INTENT_START_ORDER,
    INTENT_TALK_HUMAN,
    Intent,
    MerchantConversationState,
)
from .types import (
    ELEMENT_BLESSING,
    ELEMENT_COURTESY,
    ELEMENT_GREETING,
    ELEMENT_IMAGE_ATTACHMENT,
    ELEMENT_LOCATION_REQUEST,
    ELEMENT_ORDER_REQUEST,
    ELEMENT_PAYMENT_INQUIRY,
    ELEMENT_PRICE_INQUIRY,
    ELEMENT_PRODUCT_AVAILABILITY,
    ELEMENT_PRODUCT_REFERENCE,
    ELEMENT_QUANTITY_UNIT,
    ELEMENT_SHIPPING_INQUIRY,
    ELEMENT_STAFF_CONTACT,
    DetectedElement,
    GOAL_GENERAL,
    GOAL_GREETING_ONLY,
    GOAL_LOCATION_REQUEST,
    GOAL_ORDER_REQUEST,
    GOAL_PAYMENT_INQUIRY,
    GOAL_PRICE_INQUIRY,
    GOAL_PRODUCT_AVAILABILITY,
    GOAL_SHIPPING_INQUIRY,
    GOAL_SOCIAL_ONLY,
    GOAL_STAFF_CONTACT,
    IntentPriorityVerdict,
    _COMMERCIAL_ELEMENT_TYPES,
    _ELEMENT_PRIORITY_WEIGHT,
    _GOAL_FROM_ELEMENT,
    _SOCIAL_ELEMENT_TYPES,
)

_DIA = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_WS = re.compile(r"\s+")

# ── Category pattern tables (extensible, not phrase-specific routing) ──────────
_COURTESY_PATTERNS: Tuple[Tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"ما\s*شاء\s*الله"), ELEMENT_COURTESY, 0.92),
    (re.compile(r"تبارك\s*الله"), ELEMENT_COURTESY, 0.91),
    (re.compile(r"ما\s*قصرت"), ELEMENT_COURTESY, 0.90),
    (re.compile(r"كفو"), ELEMENT_COURTESY, 0.89),
    (re.compile(r"جزاك\s*الله"), ELEMENT_BLESSING, 0.93),
    (re.compile(r"الله\s*يبارك"), ELEMENT_BLESSING, 0.92),
    (re.compile(r"الله\s*يعافيك"), ELEMENT_BLESSING, 0.91),
    (re.compile(r"يعطيك\s*العافيه"), ELEMENT_BLESSING, 0.90),
    (re.compile(r"بيض\s*الله\s*وجهك"), ELEMENT_BLESSING, 0.93),
)

_GREETING_PATTERNS: Tuple[Tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"^السلام\s*عليكم"), ELEMENT_GREETING, 0.95),
    (re.compile(r"^مرحب"), ELEMENT_GREETING, 0.93),
    (re.compile(r"^اهلا"), ELEMENT_GREETING, 0.92),
    (re.compile(r"^هلا(?:\s|$)"), ELEMENT_GREETING, 0.91),
    (re.compile(r"^صباح\s*الخير"), ELEMENT_GREETING, 0.93),
    (re.compile(r"^مساء\s*الخير"), ELEMENT_GREETING, 0.93),
    (re.compile(r"^حياكم(?:\s*الله)?"), ELEMENT_GREETING, 0.92),
    (re.compile(r"^حيا\s*الله"), ELEMENT_GREETING, 0.92),
)

# ``بكم`` inside welcome phrases («مرحبا بكم») is NOT a price ask.
_WELCOME_BKM_PHRASE_RE: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^مرحب\S*\s+بكم(?:\s|$)"),
    re.compile(r"^اهل\S*\s+بكم(?:\s|$)"),
    re.compile(r"^هلا\s+بكم(?:\s|$)"),
    re.compile(r"^نورت\S*\s+بكم(?:\s|$)"),
)

_WELCOME_BEFORE_BKM_TOKENS = frozenset({
    "مرحبا", "مرحب", "مرحبتين", "اهلا", "أهلا", "هلا", "اهلين",
    "نورتونا", "نورتكم", "نورتنا",
})

_BKM_PRICE_AFTER_DEMONSTRATIVE = frozenset({
    "هذا", "هذي", "هذه", "هال", "كذا",
})

_COMMERCIAL_PATTERNS: Tuple[Tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"(?:كم\s*)?(?:ال)?سعر|كم\s*ثمن|قد\s*ايش|how\s*much"), ELEMENT_PRICE_INQUIRY, 0.94),
    (re.compile(r"كم\s*(?:ال)?(?:كيلو|كيلوغرام|kg|جرام|حجم|مقاس|لتر)"), ELEMENT_QUANTITY_UNIT, 0.93),
    (re.compile(r"(?:كيلو|كيلوغرام|kg|جرام|نصف\s*كيلو|ربع\s*كيلو)"), ELEMENT_QUANTITY_UNIT, 0.88),
    (re.compile(r"(?:عندكم|عندك|لديكم|متوفر|موجود)\s+\S"), ELEMENT_PRODUCT_AVAILABILITY, 0.95),
    (re.compile(r"(?:وين|فين|اين)\s*(?:موقع|فرع|محل|مكان)"), ELEMENT_LOCATION_REQUEST, 0.96),
    (re.compile(r"موقعكم|موقعك|العنوان|الفرع|خريطة|google\s*maps"), ELEMENT_LOCATION_REQUEST, 0.94),
    (re.compile(r"(?:كم|بكم)\s*(?:ال)?(?:شحن|توصيل|التوصيل)"), ELEMENT_SHIPPING_INQUIRY, 0.96),
    (re.compile(r"(?:تكلفه|تكلفة|سعر)\s*(?:ال)?(?:شحن|توصيل)"), ELEMENT_SHIPPING_INQUIRY, 0.94),
    (re.compile(r"(?:ارسل|أرسل|اعطني|أعطني|ابي|أبي)\s+(?:لي\s+)?(?:رقم|جوال|واتس)"), ELEMENT_STAFF_CONTACT, 0.95),
    (re.compile(r"رقم\s+\S{2,20}"), ELEMENT_STAFF_CONTACT, 0.90),
    (re.compile(r"(?:اتصل|تواصل)\s+(?:ب|مع|على)"), ELEMENT_STAFF_CONTACT, 0.88),
    (re.compile(r"(?:اطلب|أطلب|اشتري|أشتري|خذ\s*لي|احجز)"), ELEMENT_ORDER_REQUEST, 0.90),
    (re.compile(r"(?:حساب|iban|تحويل|طريقه\s*الدفع|طريقة\s*الدفع)"), ELEMENT_PAYMENT_INQUIRY, 0.91),
)

_IMAGE_TYPE_TOKENS = frozenset({
    "image",
    "photo",
    "picture",
    "sticker",
    "product_photo",
    "customer_photo",
})


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _DIA.sub("", s)
    s = (
        s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    )
    s = re.sub(r"[؟?!.,؛:]", " ", s)
    return _WS.sub(" ", s.lower()).strip()


def _has_image_attachment(profile: Dict[str, Any]) -> bool:
    meta = dict((profile or {}).get("inbound_metadata") or {})
    for key in ("normalized_type", "message_type", "media_type", "type"):
        val = str(meta.get(key) or "").strip().lower()
        if val in _IMAGE_TYPE_TOKENS:
            return True
    for key in ("has_image", "has_media", "is_image"):
        if meta.get(key):
            return True
    image_kind = str(meta.get("image_kind") or "").strip()
    if image_kind and image_kind not in {"text", ""}:
        return True
    return False


def _has_product_focus(state: Optional[MerchantConversationState]) -> bool:
    if state is None:
        return False
    focus = dict(getattr(state, "current_product_focus", None) or {})
    return bool(str(focus.get("title") or focus.get("id") or "").strip())


def _tokenize(norm: str) -> List[str]:
    return [t for t in _WS.split(norm or "") if t]


def _is_welcome_bkm_phrase(norm: str) -> bool:
    """True when ``بكم`` is part of a welcome idiom, not a price question."""
    if not norm:
        return False
    return any(p.search(norm) for p in _WELCOME_BKM_PHRASE_RE)


def _detect_bkm_price_inquiry(norm: str) -> Optional[DetectedElement]:
    """
    Detect ``بكم`` only as a standalone price token — never inside welcome openers.
    """
    if not norm or _is_welcome_bkm_phrase(norm):
        return None

    tokens = _tokenize(norm)
    for i, tok in enumerate(tokens):
        if tok != "بكم":
            continue
        if i > 0 and tokens[i - 1] in _WELCOME_BEFORE_BKM_TOKENS:
            continue
        if i > 0 and tokens[i - 1] in _BKM_PRICE_AFTER_DEMONSTRATIVE:
            return DetectedElement(
                element_type=ELEMENT_PRICE_INQUIRY,
                confidence=0.94,
                span_hint="بكم",
            )
        if i == 0 or i + 1 < len(tokens):
            return DetectedElement(
                element_type=ELEMENT_PRICE_INQUIRY,
                confidence=0.94,
                span_hint="بكم",
            )
    return None


def _resolved_product_query(intent: Intent) -> str:
    slots = dict(getattr(intent, "slots", None) or {})
    return str(
        slots.get("product_query")
        or slots.get("product_name")
        or ""
    ).strip()


def _detect_elements(norm: str, raw: str) -> List[DetectedElement]:
    found: List[DetectedElement] = []
    seen_types: set[str] = set()

    def _add(etype: str, conf: float, hint: str) -> None:
        if etype in seen_types:
            return
        seen_types.add(etype)
        found.append(DetectedElement(element_type=etype, confidence=conf, span_hint=hint))

    for pattern, etype, conf in _COMMERCIAL_PATTERNS:
        m = pattern.search(norm)
        if m:
            _add(etype, conf, (m.group(0) or "")[:40])

    _bkm_price = _detect_bkm_price_inquiry(norm)
    if _bkm_price is not None:
        _add(
            _bkm_price.element_type,
            _bkm_price.confidence,
            _bkm_price.span_hint,
        )

    for pattern, etype, conf in _COURTESY_PATTERNS:
        m = pattern.search(norm)
        if m:
            _add(etype, conf, (m.group(0) or "")[:40])

    for pattern, etype, conf in _GREETING_PATTERNS:
        m = pattern.search(norm)
        if m:
            _add(etype, conf, (m.group(0) or "")[:40])

    # Product token after availability opener — secondary product reference.
    if ELEMENT_PRODUCT_AVAILABILITY in seen_types:
        avail_m = re.search(
            r"(?:عندكم|عندك|لديكم|متوفر|موجود)\s+(\S{2,30})",
            norm,
        )
        if avail_m:
            _add(
                ELEMENT_PRODUCT_REFERENCE,
                0.85,
                (avail_m.group(1) or "")[:30],
            )

    return found


def _rank_elements(elements: List[DetectedElement]) -> List[str]:
    ranked = sorted(
        elements,
        key=lambda e: (
            _ELEMENT_PRIORITY_WEIGHT.get(e.element_type, 0),
            e.confidence,
        ),
        reverse=True,
    )
    return [e.element_type for e in ranked]


def _goal_from_intent(intent: Intent) -> Optional[str]:
    mapping = {
        INTENT_ASK_PRICE: GOAL_PRICE_INQUIRY,
        INTENT_ASK_PRODUCT: GOAL_PRODUCT_AVAILABILITY,
        INTENT_ASK_LOCATION: GOAL_LOCATION_REQUEST,
        INTENT_ASK_SHIPPING: GOAL_SHIPPING_INQUIRY,
        INTENT_ASK_OWNER_CONTACT: GOAL_STAFF_CONTACT,
        INTENT_TALK_HUMAN: GOAL_STAFF_CONTACT,
        INTENT_ASK_PAYMENT_INFO: GOAL_PAYMENT_INQUIRY,
        INTENT_START_ORDER: GOAL_ORDER_REQUEST,
        INTENT_SOCIAL: GOAL_SOCIAL_ONLY,
        INTENT_GREETING: GOAL_GREETING_ONLY,
    }
    return mapping.get(str(getattr(intent, "name", "") or ""))


def _resolve_primary_goal(
    elements: List[DetectedElement],
    intent: Intent,
    *,
    norm: str = "",
) -> str:
    try:
        from ..intent.education_context_classifier import is_education_non_commerce_context  # noqa: PLC0415

        if is_education_non_commerce_context(norm):
            element_types = {e.element_type for e in elements}
            if ELEMENT_GREETING in element_types:
                return GOAL_GREETING_ONLY
            return GOAL_SOCIAL_ONLY
    except Exception:  # noqa: silent-ok — education gate must not break priority
        pass

    commercial = [
        e for e in elements
        if e.element_type in _COMMERCIAL_ELEMENT_TYPES
    ]
    if commercial:
        best = max(
            commercial,
            key=lambda e: (
                _ELEMENT_PRIORITY_WEIGHT.get(e.element_type, 0),
                e.confidence,
            ),
        )
        return _GOAL_FROM_ELEMENT.get(best.element_type, GOAL_GENERAL)

    element_types = {e.element_type for e in elements}

    # Welcome «بكم» must not inherit a stale rules-layer ask_price intent.
    if _is_welcome_bkm_phrase(norm):
        if ELEMENT_GREETING in element_types:
            return GOAL_GREETING_ONLY
        return GOAL_SOCIAL_ONLY

    intent_goal = _goal_from_intent(intent)
    if intent_goal and intent_goal not in {GOAL_GENERAL, GOAL_SOCIAL_ONLY, GOAL_GREETING_ONLY}:
        return intent_goal

    social = [e for e in elements if e.element_type in _SOCIAL_ELEMENT_TYPES]
    if social and not commercial:
        if any(e.element_type == ELEMENT_GREETING for e in social):
            return GOAL_GREETING_ONLY
        return GOAL_SOCIAL_ONLY

    return intent_goal or GOAL_GENERAL


def _resolve_clarification(
    *,
    primary_goal: str,
    elements: List[DetectedElement],
    has_image: bool,
    has_product_focus: bool,
    product_query: str,
) -> Tuple[bool, str, str]:
    element_types = {e.element_type for e in elements}
    has_unit = ELEMENT_QUANTITY_UNIT in element_types

    if primary_goal == GOAL_PRICE_INQUIRY:
        if has_product_focus or product_query:
            return False, "", "answer_price_for_known_product"
        if has_image:
            return (
                True,
                "image_product_uncertain",
                "clarify_product_for_price_with_image",
            )
        return (
            True,
            "missing_product_for_price",
            "clarify_product_for_price_quote",
        )

    if primary_goal == GOAL_PRODUCT_AVAILABILITY and not product_query:
        # Availability with named product in span — no clarification.
        if ELEMENT_PRODUCT_REFERENCE in element_types:
            return False, "", "confirm_product_availability"
        return False, "", "confirm_product_availability"

    if primary_goal in {
        GOAL_SHIPPING_INQUIRY,
        GOAL_LOCATION_REQUEST,
        GOAL_STAFF_CONTACT,
        GOAL_ORDER_REQUEST,
        GOAL_PAYMENT_INQUIRY,
    }:
        return False, "", f"answer_{primary_goal}"

    if primary_goal in {GOAL_SOCIAL_ONLY, GOAL_GREETING_ONLY}:
        return False, "", "brief_social_ack"

    if has_unit and not has_product_focus and not product_query:
        return (
            True,
            "missing_product_for_unit_price",
            "clarify_product_for_unit_price",
        )

    return False, "", "advance_conversation"


def _build_recommended_focus(
    *,
    primary_goal: str,
    requires_clarification: bool,
    clarification_reason: str,
    focus_token: str,
    has_secondary_social: bool,
) -> str:
    if requires_clarification:
        if clarification_reason == "image_product_uncertain":
            return (
                "image_price_clarify — العميل أرفق صورة وسأل عن السعر/الكمية. "
                "لا تخمّني المنتج من الصورة. اسألي عن نوع المنتج المقصود "
                "حتى تعطي سعر الكيلو/الوحدة الصحيح — سؤال واحد مباشر مرتبط "
                "بالسعر، ليس سؤالاً عاماً عن الصفة أو النوع بلا سياق."
            )
        if clarification_reason in {
            "missing_product_for_price",
            "missing_product_for_unit_price",
        }:
            return (
                "product_price_clarify — العميل يسأل عن السعر/الوحدة بدون "
                "تحديد منتج واضح. اسألي عن المنتج المقصود حتى تعطي السعر "
                "الصحيح — ليس «أي نوع أو صفة تهمك؟»."
            )

    focus_map = {
        GOAL_PRICE_INQUIRY: (
            "price_inquiry — أجيبي على سؤال السعر/الوحدة أولاً. "
            "مجاملة قصيرة مسموحة (كلمتان كحد أقصى) ثم انتقلي للسعر."
        ),
        GOAL_PRODUCT_AVAILABILITY: (
            "product_availability — أجيبي على توفر المنتج المطلوب أولاً."
        ),
        GOAL_LOCATION_REQUEST: (
            "location_request — أجيبي على موقع/الفرع المطلوب."
        ),
        GOAL_SHIPPING_INQUIRY: (
            "shipping_inquiry — أجيبي على تكلفة/سياسة الشحن مباشرة."
        ),
        GOAL_STAFF_CONTACT: (
            "staff_contact — وفّري وسيلة التواصل المطلوبة دون استيضاح منتج."
        ),
        GOAL_ORDER_REQUEST: "order_request — ساعدي العميل في إتمام الطلب.",
        GOAL_PAYMENT_INQUIRY: "payment_inquiry — أجيبي على سؤال الدفع/التحويل.",
        GOAL_SOCIAL_ONLY: "social_only — مجاملة قصيرة فقط.",
        GOAL_GREETING_ONLY: "greeting_only — تحية قصيرة ثم سؤال خدمة واحد.",
    }
    base = focus_map.get(primary_goal, focus_token or "advance_conversation")
    if has_secondary_social and primary_goal not in {
        GOAL_SOCIAL_ONLY,
        GOAL_GREETING_ONLY,
    }:
        base += (
            " ممنوع جعل عبارة المجاملة/التحية موضوع الرد أو إعادة صياغتها "
            "كعنوان («بخصوص ما شاء الله…»). لا تكرري عبارات العميل الاجتماعية."
        )
    return base


def compute_customer_intent_priority(
    *,
    message: str,
    intent: Intent,
    state: Optional[MerchantConversationState] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> IntentPriorityVerdict:
    """
    Extract, rank, and resolve customer intent priority for one turn.

    Pure function — never raises, never touches DB/LLM.
    """
    raw = (message or "").strip()
    norm = _norm(raw)
    elements = _detect_elements(norm, raw)

    try:
        from ..intent.education_context_classifier import is_education_non_commerce_context  # noqa: PLC0415

        if is_education_non_commerce_context(raw):
            elements = [
                e for e in elements
                if e.element_type not in _COMMERCIAL_ELEMENT_TYPES
            ]
    except Exception:  # noqa: silent-ok — education gate must not break priority
        pass

    has_image = _has_image_attachment(profile or {})
    if has_image:
        elements.append(
            DetectedElement(
                element_type=ELEMENT_IMAGE_ATTACHMENT,
                confidence=0.99,
                span_hint="inbound_image",
            )
        )

    ranking = _rank_elements(elements)
    primary = _resolve_primary_goal(elements, intent, norm=norm)

    secondary = [
        e.element_type
        for e in elements
        if e.element_type in _SOCIAL_ELEMENT_TYPES
        and _GOAL_FROM_ELEMENT.get(e.element_type) != primary
    ]
    # De-dupe while preserving order.
    seen_sec: set[str] = set()
    secondary_clean: List[str] = []
    for s in secondary:
        if s not in seen_sec:
            seen_sec.add(s)
            secondary_clean.append(s)

    has_focus = _has_product_focus(state)
    product_query = _resolved_product_query(intent)

    requires, clar_reason, focus_token = _resolve_clarification(
        primary_goal=primary,
        elements=elements,
        has_image=has_image,
        has_product_focus=has_focus,
        product_query=product_query,
    )

    recommended = _build_recommended_focus(
        primary_goal=primary,
        requires_clarification=requires,
        clarification_reason=clar_reason,
        focus_token=focus_token,
        has_secondary_social=bool(secondary_clean),
    )

    return IntentPriorityVerdict(
        detected_elements=elements,
        primary_customer_goal=primary,
        secondary_elements=secondary_clean,
        priority_ranking=ranking,
        requires_clarification=requires,
        clarification_reason=clar_reason,
        recommended_focus=recommended,
    )


def enrich_intent_with_priority(
    intent: Intent,
    verdict: IntentPriorityVerdict,
) -> Intent:
    """
    Stamp priority annotations onto intent slots for downstream layers.

    Does not change intent.name — only enriches slots used by compose
    and clarification paths.
    """
    slots = dict(getattr(intent, "slots", None) or {})
    slots["primary_customer_goal"] = verdict.primary_customer_goal
    slots["intent_priority_ranking"] = list(verdict.priority_ranking)
    slots["recommended_focus"] = verdict.recommended_focus
    if verdict.secondary_elements:
        slots["secondary_social_elements"] = list(verdict.secondary_elements)
        slots["embedded_greeting"] = (
            ELEMENT_GREETING in verdict.secondary_elements
            or ELEMENT_COURTESY in verdict.secondary_elements
            or ELEMENT_BLESSING in verdict.secondary_elements
        )
    if verdict.requires_clarification:
        slots["requires_goal_bound_clarification"] = True
        slots["clarification_reason"] = verdict.clarification_reason
    intent.slots = slots
    return intent


__all__ = [
    "compute_customer_intent_priority",
    "enrich_intent_with_priority",
]
