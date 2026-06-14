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
        for t in ("موقع", "فرع", "معرض", "محل", "عنوان", "مقر", "location", "maps")
    )
    return bool(has_where and has_place)


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
    "is_arrival_or_visit_signal",
    "is_contact_pronoun_followup",
    "is_location_query",
    "should_defer_staff_contact_policy",
    "staff_policy_applies_to_named_request",
]
