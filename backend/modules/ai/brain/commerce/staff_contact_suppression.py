"""
staff_contact_suppression.py
────────────────────────────
P0 — Staff contact suppression and pickup confirmation gates.

``suppress_staff_contact`` persists on ``commerce_session`` when the
customer rejects staff/showroom routing. vCard delivery requires explicit
customer contact intent or confirmed pickup contact preference.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Mapping, Optional

logger = logging.getLogger("nahla.brain.staff_contact_suppression")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

KEY_SUPPRESS_STAFF = "suppress_staff_contact"
KEY_PICKUP_PENDING = "pickup_contact_pending"
KEY_PICKUP_CONFIRMED = "pickup_contact_confirmed"

_PICKUP_CONTACT_CONFIRM_RE = re.compile(
    r"(?:"
    r"بيانات\s*التواصل|"
    r"(?:ارسل|أرسل)(?:ي|وا|لي|ل)?\s*(?:رقم|جوال|بيانات)?|"
    r"(?:اب(?:ي|غ(?:ى|a)?)|أ(?:بي|ب(?:غ(?:ى|a)?)?))\s*(?:رقم|جوال|بيانات)|"
    r"(?:اكلم|أكلم|اتصل|أتصل|تواصل)\s*(?:مع)?\s*(?:أ?مين|امين|البائع|الموظف)?|"
    r"^(?:نعم|اي|أي|تمام|اوكي|ok|yes)$"
    r")",
    re.UNICODE | re.IGNORECASE,
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


def _session_dict(state: Any) -> dict:
    raw = getattr(state, "commerce_session", None) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _write_session(state: Any, session: Mapping[str, Any]) -> None:
    try:
        state.commerce_session = dict(session)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — duck-typed state patch is best-effort
        pass


def is_staff_contact_suppressed(state: Any = None, commerce_session: Any = None) -> bool:
    cs = commerce_session if commerce_session is not None else _session_dict(state)
    if isinstance(cs, dict):
        return bool(cs.get(KEY_SUPPRESS_STAFF))
    return False


def set_staff_contact_suppressed(state: Any, *, suppressed: bool = True) -> None:
    cs = _session_dict(state)
    if suppressed:
        cs[KEY_SUPPRESS_STAFF] = True
    else:
        cs.pop(KEY_SUPPRESS_STAFF, None)
    _write_session(state, cs)


def clear_staff_suppression_if_explicit_request(message: str, state: Any) -> None:
    if has_explicit_staff_contact_request(message):
        set_staff_contact_suppressed(state, suppressed=False)


def has_explicit_staff_contact_request(message: str) -> bool:
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            has_explicit_contact_intent,
        )

        if has_explicit_contact_intent(message or ""):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional contact policy import must not block gate
        pass
    norm = _norm(message or "")
    if not norm:
        return False
    return bool(
        re.search(
            r"(?:"
            r"رقم\s*(?:أ?مين|امين|البائع|الموظف)|"
            r"(?:أ?ب(?:ي|غ(?:ى|a)?)|ار(?:سل|سل)|أ(?:رسل|رس(?:ل)?))\s*(?:رقم\s*)?(?:أ?مين|امين|البائع)"
            r")",
            norm,
            flags=re.UNICODE,
        )
    )


def is_pickup_contact_pending(state: Any = None, commerce_session: Any = None) -> bool:
    cs = commerce_session if commerce_session is not None else _session_dict(state)
    if isinstance(cs, dict):
        return bool(cs.get(KEY_PICKUP_PENDING))
    return False


def is_pickup_contact_confirmed(state: Any = None, commerce_session: Any = None) -> bool:
    cs = commerce_session if commerce_session is not None else _session_dict(state)
    if isinstance(cs, dict):
        return bool(cs.get(KEY_PICKUP_CONFIRMED))
    return False


def mark_pickup_contact_pending(state: Any) -> None:
    cs = _session_dict(state)
    cs[KEY_PICKUP_PENDING] = True
    cs.pop(KEY_PICKUP_CONFIRMED, None)
    _write_session(state, cs)


def mark_pickup_contact_confirmed(state: Any) -> None:
    cs = _session_dict(state)
    cs[KEY_PICKUP_PENDING] = False
    cs[KEY_PICKUP_CONFIRMED] = True
    cs.pop(KEY_SUPPRESS_STAFF, None)
    _write_session(state, cs)


def customer_confirmed_pickup_contact(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    return bool(_PICKUP_CONTACT_CONFIRM_RE.search(norm))


def needs_pickup_contact_confirmation(message: str) -> bool:
    try:
        from modules.ai.brain.commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
            has_explicit_showroom_pickup_intent,
        )
        from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
            classify_store_arrival,
        )
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_explicit_arrival_intent,
        )

        raw = (message or "").strip()
        if not raw:
            return False
        if classify_store_arrival(raw) is not None:
            return False
        if not has_explicit_showroom_pickup_intent(raw):
            return False
        if is_explicit_arrival_intent(raw):
            return False
        if has_explicit_staff_contact_request(raw):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def apply_staff_contact_session_flags(
    state: Any,
    message: str,
    decision: Any = None,
) -> None:
    from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
        is_staff_route_rejection_message,
    )

    msg = (message or "").strip()
    if is_staff_route_rejection_message(msg):
        set_staff_contact_suppressed(state, suppressed=True)
        return

    clear_staff_suppression_if_explicit_request(msg, state)

    if needs_pickup_contact_confirmation(msg):
        mark_pickup_contact_pending(state)
        return

    if is_pickup_contact_pending(state) and customer_confirmed_pickup_contact(msg):
        mark_pickup_contact_confirmed(state)


def staff_vcard_delivery_blocked(
    *,
    customer_msg: str,
    commerce_session: Any = None,
    state: Any = None,
    customer_intent: bool = False,
) -> tuple[bool, str]:
    """Return (blocked, reason). Blocked vCard must not attach."""
    cs = commerce_session if commerce_session is not None else _session_dict(state)

    if is_staff_contact_suppressed(state=None, commerce_session=cs):
        return True, "staff_contact_suppressed"

    try:
        from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: PLC0415
            classify_complaint_refund,
        )

        if classify_complaint_refund(customer_msg or ""):
            return True, "complaint_refund_active"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — complaint probe must not block staff gate
        pass

    if needs_pickup_contact_confirmation(customer_msg or ""):
        return True, "pickup_confirm_pending"

    if is_pickup_contact_pending(state=None, commerce_session=cs):
        if not customer_confirmed_pickup_contact(customer_msg or ""):
            return True, "pickup_confirm_pending"

    return False, ""


def customer_allows_staff_vcard(
    *,
    customer_msg: str,
    commerce_session: Any = None,
    state: Any = None,
    employee_not_responding: Any = None,
    location_branch_failure: Any = None,
    customer_intent: bool = False,
) -> tuple[bool, str]:
    """Return (allowed, skip_reason) for vCard attachment."""
    blocked, reason = staff_vcard_delivery_blocked(
        customer_msg=customer_msg,
        commerce_session=commerce_session,
        state=state,
        customer_intent=customer_intent,
    )
    if blocked:
        return False, reason
    return True, "customer_intent_evidence"


__all__ = [
    "KEY_PICKUP_CONFIRMED",
    "KEY_PICKUP_PENDING",
    "KEY_SUPPRESS_STAFF",
    "apply_staff_contact_session_flags",
    "clear_staff_suppression_if_explicit_request",
    "customer_allows_staff_vcard",
    "customer_confirmed_pickup_contact",
    "has_explicit_staff_contact_request",
    "is_pickup_contact_confirmed",
    "is_pickup_contact_pending",
    "is_staff_contact_suppressed",
    "mark_pickup_contact_confirmed",
    "mark_pickup_contact_pending",
    "needs_pickup_contact_confirmation",
    "set_staff_contact_suppressed",
    "staff_vcard_delivery_blocked",
]
