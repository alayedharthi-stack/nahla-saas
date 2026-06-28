"""
staff_contact_product_label_guard.py
────────────────────────────────────
Platform-wide guard: staff / contact / showroom / escalation phrases must
never become product labels or availability rewrites.

Architectural rule:
  staff/showroom/contact phrase → must NOT → "متوفر {label} بعدة خيارات"
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.staff_contact_product_label_guard")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_STAFF_ROLE_WORDS = frozenset({
    "موظف", "موظفه", "موظفة", "الموظف", "الموظفه", "الموظفة",
    "بائع", "البائع", "بائع المعرض", "بائع_المعرض",
    "مندوب", "المندوب", "استقبال", "الاستقبال",
    "خدمة العملاء", "خدمه العملاء", "خدمة_العملاء",
    "seller", "reception", "showroom", "staff", "employee",
    "المعرض", "معرض", "الفرع", "فرع",
})

# Person/location ask — «وين هو …», «أين …», not product availability.
_STAFF_LOCATION_QUERY_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:وين|فين|اين|أين|where)\s+(?:هو|هي|هم|هن|هذا|هذي|ال)?"
    r"|(?:^|\s)(?:وينه|وينها|فينه|فينها)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Person identity ask — «من أمين؟», «مين البائع؟», not a product label.
_STAFF_IDENTITY_QUERY_RE = re.compile(
    r"^(?:من|مين|من\s+هو|مين\s+هو|وش\s+هو|ايش\s+هو)\s+"
    r"(?!انت|أنت|انتِ|أنتِ|you\b)"
    r"\S.{0,40}[\?؟]?$",
    re.UNICODE | re.IGNORECASE,
)

# Pronoun / follow-up contact — «ارسل رقمه», «رقمه», «أبي أكلمه».
_STAFF_PRONOUN_CONTACT_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي)\s*(?:لي\s+)?(?:رقم(?:ه|ها|هم)?|جوال(?:ه|ها)?|هاتف(?:ه|ها)?|تواصل(?:ه|ها)?|بيانات(?:ه|ها)?)"
    r"|(?:^|\s)(?:رقم(?:ه|ها|هم)|جوال(?:ه|ها)|هاتف(?:ه|ها))\s*(?:لاهنت|لو\s+سمحت)?"
    r"|(?:^|\s)(?:ابي|ابغى|أبي|أبغى|بدي|اريد|أريد)\s*(?:اكلم|أكلم|اتصل|أتصل|اتواصل|أتواصل|كلم|كلمه)"
    r"|(?:^|\s)(?:اكلم|أكلم|اتصل|أتصل|اتواصل|أتواصل|كلم|كلمه)(?:ه|ها|هم)?"
    r"|(?:^|\s)(?:وين|فين|اين|أين)\s*رقم(?:ه|ها|هم)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SHOWROOM_NO_RESPONSE_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ما\s*فيه|م(?:ا)?\s*فيه|مافيه|ما\s*في)\s*(?:احد|أحد|حد)?"
    r"|(?:^|\s)(?:ما\s*لقيت|مالقيت)\s*(?:احد|أحد|حد|أحد)"
    r"|(?:^|\s)(?:ما\s*يرد|مايرد|م(?:ا)?\s*يجاوب|م(?:ا)?\s*يردون)"
    r"|(?:^|\s)(?:محد|ما\s*حد)\s*(?:رد|يجاوب|موجود)"
    r"|(?:^|\s)(?:ما\s*أحد\s*موجود|ما\s*احد\s*موجود|م(?:ا)?\s*فيه\s*احد|م(?:ا)?\s*فيه\s*أحد)"
    r"|(?:^|\s)(?:المعرض\s*مقفل|مقفل\s*المعرض)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ESCALATION_SIGNAL_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ص(?:ع|ّ)د|ص(?:ع|ّ)دي|التصعيد|escalat)"
    r"|(?:^|\s)(?:المستوى\s*(?:ال)?(?:اول|أول|1|ثاني|ثان|2|ثالث|3))"
    r"|(?:^|\s)(?:حولني|حولوني)\s*(?:ل|الى|إلى)?\s*(?:موظف|شخص|بشر|الادارة|الإدارة|مدير)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_EXPLICIT_PRODUCT_AVAILABILITY_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:هل|هَل)\s+\S+\s+(?:متوفر|متاح|available|in\s+stock|عندكم|عندك)"
    r"|(?:^|\s)(?:هل|هَل)\s+(?:عند(?:كم|ك)?|متوفر|available)\s+\S+"
    r"|(?:^|\s)(?:عند(?:كم|ك)?|لديكم|do\s+you\s+have)\s+\S+"
    r"|(?:^|\s)(?:ابي|ابغى|أبي|أبغى|بدي|اريد|أريد|want)\s+(?:عسل|منتج|product|\d+|\S+\s+(?:حجم|كilo|كيلو|كجم))"
    r"|(?:^|\s)(?:كم\s+سعر|بكم|سعر\s+)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CATALOG_BROWSE_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:وش|ايش|إيش|what)\s*(?:ال)?(?:انواع|أنواع|types|options|choices|خيارات|منتجات|products|كتالوج|catalog)"
    r"|(?:^|\s)(?:ال)?(?:انواع|أنواع|خيارات|منتجات)\s*(?:المتوف(?:ر|رة|ره)|available|موجود(?:ه|ة)?)"
    r"|(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي|show|send)\s*(?:لي\s+)?(?:ال)?(?:كتالوج|catalog|خيارات|options|منتجات|products)"
    r"|(?:^|\s)(?:top\s+products|best\s+sellers|الاكثر\s+مبيعا|اكثر\s+مبيعا)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class StaffContactGuardContext:
    """Optional signals — guard works without these when message is clear."""

    merchant_op_policy_hint: Any = None
    conversation_history: Tuple[str, ...] = ()
    configured_staff_names: Tuple[str, ...] = ()
    configured_staff_roles: Tuple[str, ...] = ()
    intent_name: str = ""
    staff_route_detected: bool = False
    arrival_thread_active: bool = False


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
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def has_explicit_product_commerce_intent(message: str) -> bool:
    """True when message clearly asks about products/catalog — never block these."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if _EXPLICIT_PRODUCT_AVAILABILITY_RE.search(norm):
        return True
    if _CATALOG_BROWSE_RE.search(norm):
        return True
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_commerce_or_product_flow_message,
        )

        if is_commerce_or_product_flow_message(raw):
            return True
    except Exception:
        logger.exception("[STAFF_CONTACT_PRODUCT_LABEL_GUARD] commerce_flow_check_failed")
    try:
        from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )

        if global_availability_browse_requested(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional browse policy import
        pass
    return False


def _history_has_arrival_thread(history: Sequence[str]) -> bool:
    if not history:
        return False
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_arrival_or_visit_signal,
            has_explicit_contact_intent,
        )
        from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
            classify_employee_not_responding,
        )
    except Exception:
        return False
    for turn in list(history)[-6:]:
        body = str(turn or "").strip()
        if not body:
            continue
        if is_arrival_or_visit_signal(body):
            return True
        if has_explicit_contact_intent(body):
            return True
        if classify_employee_not_responding(body) is not None:
            return True
        if _SHOWROOM_NO_RESPONSE_RE.search(_norm(body)):
            return True
    return False


def _matches_configured_staff(text: str, ctx: StaffContactGuardContext) -> bool:
    norm = _norm(text)
    if not norm:
        return False
    names = tuple(ctx.configured_staff_names or ()) + tuple(ctx.configured_staff_roles or ())
    for name in names:
        candidate = _norm(name)
        if not candidate or len(candidate) < 2:
            continue
        if candidate in norm or norm in candidate:
            return True
        # Token overlap for multi-word names/roles.
        name_tokens = [t for t in candidate.split() if len(t) >= 2]
        if name_tokens and all(t in norm for t in name_tokens):
            return True
    return False


def _hint_indicates_staff_contact(ctx: StaffContactGuardContext) -> bool:
    hint = ctx.merchant_op_policy_hint
    if hint is None:
        return False
    purpose = str(getattr(hint, "response_purpose", None) or "").strip()
    if purpose in {
        "showroom_visit",
        "contact_request",
        "showroom_escalation",
        "staff_contact",
    }:
        return True
    forbidden = frozenset(getattr(hint, "forbidden_actions", ()) or ())
    if forbidden & frozenset({
        "browse_products",
        "catalog_promise",
        "ask_product",
    }):
        if purpose:
            return True
    contact_hint = getattr(hint, "contact_policy_hint", None)
    showroom_hint = getattr(hint, "showroom_policy_hint", None)
    return contact_hint is not None or showroom_hint is not None


def is_showroom_or_escalation_context(
    message: str,
    *,
    ctx: Optional[StaffContactGuardContext] = None,
) -> bool:
    """True for arrival, showroom visit, no-response, or escalation threads."""
    raw = (message or "").strip()
    if not raw:
        return False
    if has_explicit_product_commerce_intent(raw):
        return False
    norm = _norm(raw)
    guard_ctx = ctx or StaffContactGuardContext()
    if guard_ctx.arrival_thread_active or _history_has_arrival_thread(guard_ctx.conversation_history):
        if not has_explicit_product_commerce_intent(raw):
            if (
                _STAFF_LOCATION_QUERY_RE.search(norm)
                or _STAFF_PRONOUN_CONTACT_RE.search(norm)
                or _SHOWROOM_NO_RESPONSE_RE.search(norm)
                or _ESCALATION_SIGNAL_RE.search(norm)
            ):
                return True
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_arrival_or_visit_signal,
            is_explicit_arrival_intent,
        )

        if is_arrival_or_visit_signal(raw) or is_explicit_arrival_intent(raw):
            return True
    except Exception:
        logger.exception("[STAFF_CONTACT_PRODUCT_LABEL_GUARD] arrival_check_failed")
    if _SHOWROOM_NO_RESPONSE_RE.search(norm):
        return True
    if _ESCALATION_SIGNAL_RE.search(norm):
        return True
    return False


def is_staff_or_contact_context(
    message: str,
    *,
    ctx: Optional[StaffContactGuardContext] = None,
) -> bool:
    """
    True when inbound belongs to staff/contact/showroom/escalation — not catalog.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if has_explicit_product_commerce_intent(raw):
        return False
    try:
        from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: PLC0415
            _looks_like_role_or_courier_intro,
        )

        if _looks_like_role_or_courier_intro(raw):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional courier intro import
        pass
    guard_ctx = ctx or StaffContactGuardContext()
    norm = _norm(raw)

    if guard_ctx.staff_route_detected:
        return True
    if _hint_indicates_staff_contact(guard_ctx):
        return True
    if is_showroom_or_escalation_context(raw, ctx=guard_ctx):
        return True

    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            has_explicit_contact_intent,
            is_contact_pronoun_followup,
            is_location_query,
        )

        if has_explicit_contact_intent(raw) or is_contact_pronoun_followup(raw):
            return True
        if is_location_query(raw) and not has_explicit_product_commerce_intent(raw):
            return True
    except Exception:
        logger.exception("[STAFF_CONTACT_PRODUCT_LABEL_GUARD] contact_route_check_failed")

    if _STAFF_LOCATION_QUERY_RE.search(norm):
        return True
    if _STAFF_IDENTITY_QUERY_RE.search(norm):
        return True
    if _STAFF_PRONOUN_CONTACT_RE.search(norm):
        return True

    for role in _STAFF_ROLE_WORDS:
        role_norm = _norm(role)
        if role_norm and role_norm in norm:
            if not has_explicit_product_commerce_intent(raw):
                return True

    if _matches_configured_staff(raw, guard_ctx):
        return True

    intent = str(guard_ctx.intent_name or "").strip().lower()
    if intent in {"general_channel", "staff_contact", "staff_contact_evidence", "contact_request"}:
        return True

    return False


def is_staff_or_contact_label(
    text: str,
    *,
    ctx: Optional[StaffContactGuardContext] = None,
) -> bool:
    """True when text must never be used as a catalog product label."""
    raw = (text or "").strip()
    if not raw:
        return False
    if is_staff_or_contact_context(raw, ctx=ctx):
        return True
    guard_ctx = ctx or StaffContactGuardContext()
    if _matches_configured_staff(raw, guard_ctx):
        return True
    norm = _norm(raw)
    if _STAFF_LOCATION_QUERY_RE.search(norm) and len(norm.split()) <= 6:
        return True
    return False


def should_block_product_availability_rewrite(
    message: str,
    *,
    label: str = "",
    ctx: Optional[StaffContactGuardContext] = None,
    guard_action: str = "",
) -> bool:
    """
    Block «متوفر {label} بعدة خيارات» when staff/contact/showroom context applies.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if has_explicit_product_commerce_intent(raw):
        return False
    guard_ctx = ctx or StaffContactGuardContext()
    if is_staff_or_contact_context(raw, ctx=guard_ctx):
        return True
    label_raw = (label or raw).strip()
    if label_raw and is_staff_or_contact_label(label_raw, ctx=guard_ctx):
        return True
    if guard_action.startswith("rewrite") and is_showroom_or_escalation_context(raw, ctx=guard_ctx):
        return True
    return False


def staff_contact_context_reason(message: str) -> str:
    """Short reason token for logging / discovery gate."""
    if not is_staff_or_contact_context(message):
        return ""
    if is_showroom_or_escalation_context(message):
        return "showroom_escalation_context"
    return "staff_contact_context"


__all__ = [
    "StaffContactGuardContext",
    "has_explicit_product_commerce_intent",
    "is_showroom_or_escalation_context",
    "is_staff_or_contact_context",
    "is_staff_or_contact_label",
    "should_block_product_availability_rewrite",
    "staff_contact_context_reason",
]
