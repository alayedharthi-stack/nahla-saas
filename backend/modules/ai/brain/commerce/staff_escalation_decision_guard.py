"""
staff_escalation_decision_guard.py
──────────────────────────────────
P0 — Final validation before staff contact / vCard delivery.

Staff escalation is a high-trust operational action. Keyword presence
alone (وصل، الطائف، أمين in reply) must never trigger vCard delivery.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.staff_escalation_decision_guard")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

EVIDENCE_EXPLICIT_STAFF = "explicit_staff_contact_intent"
EVIDENCE_PICKUP_CONFIRMED = "confirmed_pickup_contact"
EVIDENCE_STORE_ARRIVAL = "high_confidence_store_arrival"
EVIDENCE_KB_POLICY = "kb_policy_required"
EVIDENCE_EMPLOYEE_NOT_RESPONDING = "employee_not_responding"
EVIDENCE_LOCATION_FAILURE = "location_branch_failure"

_BLOCKED_DELIVERY_RECEIVED = "blocked:delivery_received_phrase"
_BLOCKED_THANKS_BLESSING = "blocked:thanks_or_blessing"
_BLOCKED_CITY_MENTION = "blocked:city_mention_only"
_BLOCKED_KEYWORD_ONLY = "blocked:keyword_only_no_intent"
_BLOCKED_NO_EVIDENCE = "blocked:no_operational_evidence"

# Delivery received / thanks — NOT store arrival.
_DELIVERY_RECEIVED_RE = re.compile(
    r"(?:"
    r"^(?:وصل|وصلت|وصلني|وصلنا)(?:\s|$|[،,.!?])"
    r"|(?:^|\s)وصل(?:\s|$)\s*الله"
    r"|(?:^|\s)وصل(?:\s|$).*?(?:بيض|بارك|الله|وجه|شكر|حلال|مال)"
    r"|(?:^|\s)وصل(?:ت|نا|ني)?\s*(?:ال)?(?:عسل|منتج|طلب|شحن(?:ة|ه)?|الطلب)"
    r"|(?:ال)?(?:عسل|منتج|طلب|شحن(?:ة|ه)?|الطلب(?:ية)?)\s*وصل(?:ت|نا|ني)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_THANKS_BLESSING_RE = re.compile(
    r"(?:"
    r"بيض\s*الله|"
    r"الله\s*يبارك|"
    r"بارك\s*(?:ال)?(?:لك|كم|في)|"
    r"(?:^|\s)ش(?:كر(?:ا|اً)|كر)(?:\s|$|[!.])|"
    r"جز(?:اك|اكم)\s*الله|"
    r"حياك\s*الله"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CITY_MENTION_ONLY_RE = re.compile(
    r"^(?:"
    r"(?:انا|أنا)\s*(?:في|من)\s+\S+"
    r"|(?:توصيل|شحن)\s*(?:ل|ال)?\S+"
    r"|\S+\s*(?:توصيل|شحن)"
    r")(?:\s|$|[!.])$",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class StaffContactValidation:
    allowed: bool
    reason: str
    evidence: str = ""


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


def is_delivery_received_phrase(message: str) -> bool:
    """True when «وصل» means shipment/product received — not store visit."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    try:
        from .contact_escalation import (  # noqa: PLC0415
            _DELIVERY_RECEIVED_CONTEXT_RE,
            _STORE_VISIT_DESTINATION_RE,
        )

        if _DELIVERY_RECEIVED_CONTEXT_RE.search(norm):
            if _STORE_VISIT_DESTINATION_RE.search(norm):
                return False
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — fallback to local patterns
        pass
    if _DELIVERY_RECEIVED_RE.search(norm):
        return True
    if _THANKS_BLESSING_RE.search(norm) and re.search(
        r"وصل", norm, flags=re.UNICODE,
    ):
        return True
    return False


def is_thanks_or_blessing_only(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if not _THANKS_BLESSING_RE.search(norm):
        return False
    try:
        from .contact_escalation import classify_store_arrival  # noqa: PLC0415
        from .staff_contact_suppression import has_explicit_staff_contact_request  # noqa: PLC0415

        if has_explicit_staff_contact_request(raw):
            return False
        if classify_store_arrival(raw) is not None:
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional probes must not block guard
        pass
    return True


def is_city_mention_only(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if not _CITY_MENTION_ONLY_RE.match(norm):
        return False
    try:
        from .contact_escalation import classify_store_arrival  # noqa: PLC0415

        if classify_store_arrival(raw) is not None:
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional arrival probe
        pass
    return True


def validate_staff_contact_action(
    *,
    customer_msg: str,
    commerce_session: Any = None,
    state: Any = None,
    history: Any = None,
    proposed_reason: str = "",
    store_arrival: Any = None,
    kb_policy_required: bool = False,
    policy_allowed: bool = False,
    employee_not_responding: Any = None,
    location_branch_failure: Any = None,
    explicit_contact_intent: bool = False,
) -> StaffContactValidation:
    """
    Final gate before staff message / vCard.

    Allowed only with operational evidence — never keyword alone.
    """
    raw = (customer_msg or "").strip()
    norm = _norm(raw)

    if not norm:
        return StaffContactValidation(False, "empty_message", _BLOCKED_NO_EVIDENCE)

    if is_delivery_received_phrase(raw):
        return StaffContactValidation(
            False,
            "delivery_received_not_store_arrival",
            _BLOCKED_DELIVERY_RECEIVED,
        )

    if is_thanks_or_blessing_only(raw):
        return StaffContactValidation(
            False,
            "thanks_or_blessing_not_staff_intent",
            _BLOCKED_THANKS_BLESSING,
        )

    if is_city_mention_only(raw):
        return StaffContactValidation(
            False,
            "city_mention_not_store_arrival",
            _BLOCKED_CITY_MENTION,
        )

    try:
        from .staff_contact_suppression import (  # noqa: PLC0415
            customer_confirmed_pickup_contact,
            has_explicit_staff_contact_request,
            is_pickup_contact_confirmed,
            is_pickup_contact_pending,
        )

        if has_explicit_staff_contact_request(raw) or explicit_contact_intent:
            return StaffContactValidation(
                True,
                proposed_reason or "explicit_staff_contact_request",
                EVIDENCE_EXPLICIT_STAFF,
            )

        cs = commerce_session
        if cs is None and state is not None:
            cs = getattr(state, "commerce_session", None)
        if is_pickup_contact_confirmed(state=None, commerce_session=cs):
            return StaffContactValidation(
                True,
                proposed_reason or "pickup_contact_confirmed",
                EVIDENCE_PICKUP_CONFIRMED,
            )
        if is_pickup_contact_pending(state=None, commerce_session=cs):
            if customer_confirmed_pickup_contact(raw):
                return StaffContactValidation(
                    True,
                    proposed_reason or "pickup_contact_confirmed_turn",
                    EVIDENCE_PICKUP_CONFIRMED,
                )
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — explicit intent probe must not block guard
        logger.debug("[STAFF_ESCALATION_GUARD] explicit_probe_failed err=%s", exc)

    if employee_not_responding is not None:
        return StaffContactValidation(
            True,
            proposed_reason or "employee_not_responding",
            EVIDENCE_EMPLOYEE_NOT_RESPONDING,
        )

    if location_branch_failure is not None:
        return StaffContactValidation(
            True,
            proposed_reason or "location_branch_failure",
            EVIDENCE_LOCATION_FAILURE,
        )

    if store_arrival is not None and policy_allowed:
        return StaffContactValidation(
            True,
            proposed_reason or "store_arrival_policy_allowed",
            EVIDENCE_STORE_ARRIVAL,
        )

    if kb_policy_required and policy_allowed:
        return StaffContactValidation(
            True,
            proposed_reason or "kb_policy_required",
            EVIDENCE_KB_POLICY,
        )

    return StaffContactValidation(
        False,
        proposed_reason or "no_operational_evidence",
        _BLOCKED_KEYWORD_ONLY,
    )


__all__ = [
    "EVIDENCE_EXPLICIT_STAFF",
    "EVIDENCE_KB_POLICY",
    "EVIDENCE_PICKUP_CONFIRMED",
    "EVIDENCE_STORE_ARRIVAL",
    "StaffContactValidation",
    "is_city_mention_only",
    "is_delivery_received_phrase",
    "is_thanks_or_blessing_only",
    "validate_staff_contact_action",
]
