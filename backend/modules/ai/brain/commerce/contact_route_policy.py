"""
Contact route policy — separate location, arrival, and staff escalation.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

logger = logging.getLogger("nahla.brain.contact_route_policy")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_PRONOUN_CONTACT_RE = re.compile(
    r"(?:"
    r"وين\s*رقم(?:ه|ها|هم)?"
    r"|كم\s*رقم(?:ه|ها|هم)?"
    r"|ايش\s*رقم(?:ه|ها|هم)?"
    r"|وش\s*رقم(?:ه|ها|هم)?"
    r"|رقم(?:ه|ها|هم)\s*وين"
    r"|رقمه\s*وين"
    r"|what\s*(?:is|'s)\s*(?:his|her|their)\s*number"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Stricter than classify_store_arrival — pre-brain arrival delivery only.
_EXPLICIT_ARRIVAL_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:انا|أنا)\s*(?:جاي|جا(?:ي|يك)(?:كم|ك|ين)?|في\s*الطريق)"
    r"|(?:^|\s)(?:انا|أنا)\s*وصل(?:ت|نا|وا)?\s*(?:ل(?:ل)?(?:معرض|فرع|محل|باب)|(?:ل\s*)?(?:المعرض|الفرع|المحل|الباب))"
    r"|(?:^|\s)وصل(?:ت|نا|وا)?\s*(?:ل(?:ل)?(?:معرض|فرع|محل|باب)|(?:ل\s*)?(?:المعرض|الفرع|المحل|الباب))"
    r"|(?:^|\s)عند\s*الب(?:و)?اب(?:ة)?"
    r"|(?:^|\s)عند\s*الباب"
    r"|(?:^|\s)(?:انا|أنا)\s*(?:عند|بر(?:ا|ه))\s*(?:المعرض|الفرع|الباب|البواب|الحوش)"
    r"|(?:^|\s)جاي\s*(?:أستلم|استلم|اخذ|آخذ|باستلم|استلام)"
    r"|عندكم\s*(?:في)?\s*المعرض"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Customer deferral to the bot/agent — NOT a staff-contact request.
# e.g. «أروح أصلي وأتواصل معاك», «أكلمك بعدين», «أرجع لك».
_CUSTOMER_DEFER_TO_AGENT_RE = re.compile(
    r"(?:"
    r"(?:اتواصل|تواصل|اكلم|أكلم|اتكلم|أتكلم|كلم)\s*مع(?:ك|اك|كم|كن|كِ|ال)?(?:ك|اك|كم)?"
    r"|(?:ارجع|أرجع|برجع|بارجع|ارجعلك|أرجعلك)\s*(?:لك|ليك|لكم|لي)?"
    r"|(?:اكلم|أكلم|اتكلم|أتكلم)(?:ك|كم|كن)(?:\s|$|[،,.!?]|(?:بعدين|لاحق|later))"
    r"|(?:بعدين|later)\s*(?:ارسل|أرسل|ارسلك|أرسلك)"
    r"|(?:^|\s)(?:أروح|اروح|بروح|رايح|رايحة)\s*(?:أصلي|اصلي|اسوي|اسوي|أصل|اص)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_EXPLICIT_CONTACT_INTENT_RE = re.compile(
    r"(?:"
    r"رقم|جوال|هاتف|تليفون|موبايل|"
    r"كلم|اتصل|اتواصل|تواصل|وصلني|وصلوني|"
    r"ما\s*يرد|مايرد|"
    r"ارسل\s*رقم|أرسل\s*رقم|ارسلي\s*رقم|أرسلي\s*رقم|"
    r"خدمة\s*العملاء|خدمه\s*العملاء|"
    r"وين\s*رقم|كم\s*رقم|"
    r"اكلم\s*موظف|أكلم\s*موظف|"
    r"حولني\s*(?:ل|الى|إلى)?\s*(?:موظف|شخص|بشر)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_COMMERCE_PRODUCT_FLOW_RE = re.compile(
    r"(?:"
    r"^(?:ابي|ابغى|أبي|أبغى|بدي|ابغا)\s+\d+"
    r"|\d+\s*(?:حجم|كilo|كيلو|كجم|kg|ك\s*g|غرام|جرام|كرتون|علبة|حبة|صنف)"
    r"|(?:حجم|كilo|كيلو|كجم|كرتون|علبة|حبة)\s*\d+"
    r"|(?:هل|هَل)\s+\S+\s+(?:متوفر|available)"
    r"|(?:تفاصيل|سعر|ثمن|كم\s*(?:سعر|ثمن|بسعر))"
    r"|(?:وش|ايش|ما)\s+(?:عندكم|انواع|أنواع|types)"
    r"|(?:أضف|اضف|add)\s*(?:ل)?(?:ال)?سلة"
    r"|(?:اخترت|اختر|ال(?:اول|أول|اولى|ثاني|ثانية|رقم))\s*\d*"
    r"|(?:ابي|ابغى|أبي|أبغى)\s+(?:ال)?(?:اول|أول|اولى|ثاني|ثانية|ثالث|رقم\s*\d+)"
    r"|(?:ابي|ابغى|أبي|أبغى)\s+\S+\s+(?:حجم|كilo|كيلو|كجم|كرتون)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_SHORT_COMMERCE_AFFIRMATIVE_RE = re.compile(
    r"^(?:"
    r"اي\s*والله|أي\s*والله|"
    r"نعم|ايه|أيه|"
    r"تمام|اوكي|ok|"
    r"أبشر|ابشر|"
    r"ماشي|"
    r"بالتأكيد|"
    r"اي|أي|"
    r"اه|أه|"
    r"ايوه|أيوه"
    r")(?:\s*[!.🌷👍]*\s*)$",
    re.IGNORECASE | re.UNICODE,
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    return _WS_RE.sub(" ", t).strip()


def is_location_query(message: str) -> bool:
    """True when the customer asks for store/branch physical location."""
    raw = (message or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.brain.commerce.link_intent import (  # noqa: PLC0415
            LinkIntentType,
            resolve_link_intent,
        )

        resolved = resolve_link_intent(raw)
        if resolved == LinkIntentType.PHYSICAL_LOCATION:
            return True
        if resolved in (
            LinkIntentType.WEBSITE_URL,
            LinkIntentType.PRODUCT_URL,
            LinkIntentType.PAYMENT_LINK,
        ):
            return False
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[CONTACT_ROUTE_POLICY] location_query_check_failed err=%s",
            exc,
        )
    try:
        from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
            looks_like_physical_location_request,
        )

        if looks_like_physical_location_request(raw):
            return True
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[CONTACT_ROUTE_POLICY] location_query_check_failed err=%s",
            exc,
        )
    norm = _norm(raw)
    if not norm:
        return False
    has_where = any(t in norm for t in ("وين", "اين", "أين", "where"))
    has_place = any(
        t in norm
        for t in ("فرع", "معرض", "محل", "عنوان", "مقر", "location", "maps")
    )
    # Bare «الموقع» alone is ambiguous — require an explicit where-clause
    # plus a physical-place noun, never «موقع» by itself.
    return bool(has_where and has_place)


def is_explicit_arrival_intent(message: str) -> bool:
    """True only for explicit in-person arrival / visit signals."""
    norm = _norm(message or "")
    if not norm:
        return False
    return bool(_EXPLICIT_ARRIVAL_RE.search(norm))


def is_customer_defer_or_return_later(message: str) -> bool:
    """True when the customer defers the conversation — not asking for staff."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if re.search(r"^(?:كيف|how)\s", norm, flags=re.UNICODE | re.IGNORECASE):
        if re.search(r"(?:تواصل|اتواصل|اكلم|أكلم|كلم)", norm, flags=re.UNICODE | re.IGNORECASE):
            return False
    return bool(_CUSTOMER_DEFER_TO_AGENT_RE.search(norm))


def has_explicit_contact_intent(message: str) -> bool:
    """True when the customer clearly asks for staff contact / phone."""
    raw = (message or "").strip()
    if not raw:
        return False
    if is_customer_defer_or_return_later(raw):
        return False
    norm = _norm(raw)
    if is_contact_pronoun_followup(raw):
        return True
    try:
        from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
            classify_employee_not_responding,
        )

        if classify_employee_not_responding(raw) is not None:
            return True
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[CONTACT_ROUTE_POLICY] not_responding_check_failed err=%s",
            exc,
        )
    return bool(_EXPLICIT_CONTACT_INTENT_RE.search(norm))


def is_commerce_or_product_flow_message(message: str) -> bool:
    """True when the message belongs to product/cart/quantity/availability flow."""
    raw = (message or "").strip()
    if not raw:
        return False
    if has_explicit_contact_intent(raw):
        return False
    if is_explicit_arrival_intent(raw):
        return False
    if is_location_query(raw):
        return False
    norm = _norm(raw)
    if not norm:
        return False
    if _COMMERCE_PRODUCT_FLOW_RE.search(norm):
        return True
    try:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            _PAYMENT_OR_NON_STAFF_RE,
        )

        if _PAYMENT_OR_NON_STAFF_RE.search(norm):
            return True
    except Exception:  # noqa: silent-ok - optional import guard for route policy
        pass
    return False


def is_short_commerce_affirmative(message: str) -> bool:
    """True for brief yes/OK replies that must not trigger contact delivery."""
    raw = (message or "").strip()
    if not raw:
        return False
    if has_explicit_contact_intent(raw):
        return False
    norm = _norm(raw)
    return bool(_SHORT_COMMERCE_AFFIRMATIVE_RE.match(norm))


def should_defer_contact_policies_for_commerce(message: str) -> bool:
    """Return True when contact/arrival policies must not short-circuit."""
    raw = (message or "").strip()
    if not raw:
        return False
    if has_explicit_contact_intent(raw):
        return False
    if is_explicit_arrival_intent(raw):
        return False
    return (
        is_commerce_or_product_flow_message(raw)
        or is_short_commerce_affirmative(raw)
    )


def is_arrival_or_visit_signal(message: str) -> bool:
    """True for in-person arrival / on-the-way / at-door signals."""
    raw = (message or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
            classify_store_arrival,
        )

        return classify_store_arrival(raw) is not None
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[CONTACT_ROUTE_POLICY] arrival_signal_check_failed err=%s",
            exc,
        )
        return False


def is_contact_pronoun_followup(message: str) -> bool:
    """True for «وين رقمه» / «كم رقمه» after a prior contact mention."""
    norm = _norm(message or "")
    if not norm:
        return False
    return bool(_PRONOUN_CONTACT_RE.search(norm))


def should_defer_staff_contact_policy(message: str) -> bool:
    """Return True when staff pre-brain policy must not short-circuit."""
    raw = (message or "").strip()
    if not raw:
        return True
    if is_customer_defer_or_return_later(raw):
        return True
    if should_defer_contact_policies_for_commerce(raw):
        return True
    if is_location_query(raw):
        return True
    if is_arrival_or_visit_signal(raw):
        return True
    if is_contact_pronoun_followup(raw):
        return True
    return False


def staff_policy_applies_to_named_request(
    message: str,
    *,
    registry_match: bool,
    explicit_contact_ask: bool,
) -> bool:
    """Named kind is a staff ask only when evidence or explicit ask exists."""
    norm = _norm(message or "")
    words = norm.split()
    if registry_match or explicit_contact_ask:
        return True
    # Single-token bare name ping only when it matched registry upstream.
    if len(words) == 1 and registry_match:
        return True
    return False


MSG_LOCATION_NOT_CONFIGURED = (
    "موقع المتجر غير مهيأ حالياً على الخريطة."
)


__all__ = [
    "MSG_LOCATION_NOT_CONFIGURED",
    "is_customer_defer_or_return_later",
    "has_explicit_contact_intent",
    "is_arrival_or_visit_signal",
    "is_commerce_or_product_flow_message",
    "is_contact_pronoun_followup",
    "is_explicit_arrival_intent",
    "is_location_query",
    "is_short_commerce_affirmative",
    "should_defer_contact_policies_for_commerce",
    "should_defer_staff_contact_policy",
    "staff_policy_applies_to_named_request",
]
