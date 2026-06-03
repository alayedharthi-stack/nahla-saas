"""
Contact escalation — Phase 1 (observability + memory).

Detects follow-up frustration when a previously suggested staff contact
did not respond. Persists ``staff_contacts_sent[]`` on brain_state and
emits structured ``[CONTACT_ESCALATION]`` telemetry.

Phase 2 (escalation chain from KB/config) is intentionally NOT here.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.contact_escalation")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Follow-up: staff/contact did not respond (not a fresh handoff ask).
_EMPLOYEE_NOT_RESPONDING_RE = re.compile(
    r"(?:"
    r"ما\s*(?:رد|يرد|جاوب|يجاوب|يفتح|ردّ)(?:\s*(?:علي|عليّ|عليا|لي))?"
    r"|"
    r"م(?:ا|احد)\s*(?:رد|يرد|جاوب|يجاوب)(?:\s*(?:علي|عليّ|لي))?"
    r"|"
    r"(?:اتصل(?:ت)?|كلم(?:ت)?|راسل(?:ت)?|تواصل(?:ت)?|رنت(?:ي)?|رن(?:يت)?)"
    r"(?:\s*(?:عليه|عليهم|بيه|فيه|معه|معاه|معاه))?"
    r".{0,40}ما\s*(?:رد|يرد|جاوب|يجاوب|يفتح)"
    r"|"
    r"ما\s*(?:يفتح|يفتح\s*علي|يفتح\s*عليّ|يرد\s*على\s*الاتصال)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Do not steal order / delivery / complaint / fresh-support intents.
_COMPETING_INTENT_RE = re.compile(
    r"(?:"
    # track_order
    r"(?:طلبي|طلبيتي|شحنتي|الشحنة|الطلبية|تتبع\s*(?:ال)?طلب|رقم\s*(?:ال)?تتبع|"
    r"رابط\s*(?:ال)?(?:تتبع|شحن)|track(?:ing)?|order\s*status|where\s*is\s*my\s*order)"
    r"|"
    # delivery / shipping policy (not personal shipment status alone)
    r"(?:كم\s*(?:ال)?(?:شحن|توصيل)|رسوم\s*(?:ال)?(?:شحن|توصيل)|"
    r"(?:طريقة|طرق)\s*(?:ال)?(?:شحن|توصيل|توصيلكم)|سياسة\s*(?:ال)?(?:شحن|توصيل)|"
    r"هل\s*(?:تشحنون|توصلون)|مناطق\s*(?:ال)?(?:توصيل|شحن))"
    r"|"
    # complaint axis
    r"(?:شكو(?:ى|ي)|مشكل(?:ة|ه)|تأخير|ما\s*وصل(?:ت)?\s*(?:ال)?(?:شحن|طلب|طلبي|طلبيتي|شحنتي)|"
    r"complaint|unacceptable|disappointed|frustrated|خدمة\s*سي(?:ئة|ئه))"
    r"|"
    # fresh support / handoff without a not-responding follow-up
    r"(?:^(?:كلموني|كلميني|حولني|حوّلني|حولوني|ابي\s+(?:اكلم|اتكلم|اتواصل)|"
    r"أبي\s+(?:أكلم|أتكلم|أتواصل))\b)"
    r")",
    re.IGNORECASE | re.UNICODE,
)


@dataclass(frozen=True)
class EmployeeNotRespondingVerdict:
    matched: bool
    confidence: float = 0.935
    pattern: str = ""


@dataclass(frozen=True)
class LocationBranchFailureVerdict:
    """Branch/location visit failed — not staff-not-responding."""

    trigger: str  # branch_closed | location_failed
    context: str  # post_location | post_branch_ask | standalone
    pattern: str = ""
    confidence: float = 0.92


_BRANCH_CLOSED_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:مب?قفل|مقفل)(?:ين|ه)?"
    r"|(?:ال)?(?:فرع|فروع)\s*(?:مب?قفل|مقفل)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_LOCATION_FAILED_RE = re.compile(
    r"(?:"
    r"ما\s*(?:فتح|يفتح)"
    r"|ما\s*لقيت(?:هم|ه|ها|كم)?"
    r"|(?:ال)?موقع\s*غلط"
    r"|(?:ال)?(?:فرع|فروع)\s*غلط"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_BRANCH_LIST_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ال)?فروع(?:\s|[؟?!.,]|$)"
    r"|(?:ابغ|ابي|أبغ|أبي|اريد|أريد)\s*(?:لي\s+)?(?:ال)?فروع"
    r"|\bbranches\b"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_BRANCH_LOCATION_NOUNS = frozenset({
    "فروع", "فرع", "فروعكم", "موقع", "الموقع", "لوكيشن", "اللوكيشن",
    "branches", "branch", "location", "address", "خرايط", "خريطة", "الخريطة",
})


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    return _WS_RE.sub(" ", t).strip()


def is_branch_location_order_tail(text: str) -> bool:
    """True when an order-prefix capture is a branch/location noun, not a SKU."""
    raw = (text or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    norm = re.sub(r"^ال", "", norm).strip()
    if norm in _BRANCH_LOCATION_NOUNS:
        return True
    return bool(_BRANCH_LIST_REQUEST_RE.search(raw))


def is_branch_list_request(message: str) -> bool:
    """True for branch-list asks such as «أبغى الفروع»."""
    raw = (message or "").strip()
    if not raw:
        return False
    if _BRANCH_LIST_REQUEST_RE.search(raw):
        return True
    try:
        from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
            looks_like_physical_location_request,
        )

        return looks_like_physical_location_request(raw)
    except Exception:  # noqa: BLE001
        return False


def _turn_text(turn: Any) -> str:
    if isinstance(turn, dict):
        return str(
            turn.get("body")
            or turn.get("content")
            or turn.get("text")
            or ""
        ).strip()
    return str(turn or "").strip()


def _history_has_location_context(history: Optional[Sequence[Any]]) -> bool:
    if not history:
        return False
    try:
        from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
            looks_like_physical_location_request,
        )
    except Exception:  # noqa: BLE001
        looks_like_physical_location_request = None  # type: ignore[assignment]

    for turn in list(history)[-6:]:
        body = _turn_text(turn)
        if not body:
            continue
        if looks_like_physical_location_request and looks_like_physical_location_request(body):
            return True
        if _BRANCH_LIST_REQUEST_RE.search(body):
            return True
    return False


def _history_has_branch_ask(history: Optional[Sequence[Any]]) -> bool:
    if not history:
        return False
    for turn in list(history)[-6:]:
        body = _turn_text(turn)
        if body and _BRANCH_LIST_REQUEST_RE.search(body):
            return True
    return False


def _resolve_location_failure_context(
    message: str,
    *,
    history: Optional[Sequence[Any]] = None,
) -> str:
    norm = _norm(message)
    if _history_has_branch_ask(history):
        return "post_branch_ask"
    if _history_has_location_context(history):
        return "post_location"
    if re.search(r"(?:فرع|فروع|موقع)", norm):
        return "standalone"
    return "none"


@dataclass(frozen=True)
class StoreArrivalVerdict:
    """Customer is arriving, on the way, at the door, or branch access failed."""

    trigger: str  # store_arrival | branch_closed | location_failed
    context: str  # standalone | post_location | post_branch_ask
    pattern: str = ""
    confidence: float = 0.91


# On-the-way / at-door / arrived (physical visit — not shipment).
_STORE_ARRIVAL_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:انا|أنا)\s*(?:جاي|جا(?:ي|يك)(?:كم|ك|ين)?|في\s*الطريق)"
    r"|(?:^|\s)وصل(?:ت|نا|وا)?(?:\s|[،,.]|$)"
    r"|(?:^|\s)عند\s*الب(?:و)?اب(?:ة)?"
    r"|(?:^|\s)عند\s*الباب"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Shipment / order delivery — NOT in-store arrival.
_SHIPPING_STATUS_ARRIVAL_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ال)?(?:شحن(?:ة|ه)?|طلب(?:ي|يتي)?|الطلب(?:ية)?|order|shipment|delivery|parcel|package)"
    r"|"
    r"وصل(?:ت|نا)?\s*(?:ال)?(?:شحن(?:ة|ه)?|طلب(?:ي|يتي)?|الطلب(?:ية)?)"
    r"|"
    r"(?:ال)?(?:شحن(?:ة|ه)?|طلب(?:ي|يتي)?|الطلب(?:ية)?)\s*وصل(?:ت|نا)?"
    r"|"
    r"هل\s*وصل(?:ت)?\s*(?:ال)?(?:شحن(?:ة|ه)?|طلب)"
    r"|"
    r"ما\s*وصل(?:ت)?\s*(?:ال)?(?:شحن(?:ة|ه)?|طلب(?:ي|يتي)?|الطلب(?:ية)?)"
    r")",
    re.IGNORECASE | re.UNICODE,
)


def classify_store_arrival(
    message: str,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Optional[StoreArrivalVerdict]:
    """Detect in-person arrival / on-the-way / branch-access signals.

    Excludes shipment-status phrasing such as «وصلت الشحنة».
    Branch-closed / location-failed follow-ups reuse
    :func:`classify_location_branch_failure` context rules.
    """
    norm = _norm(message)
    if not norm:
        return None
    if _SHIPPING_STATUS_ARRIVAL_RE.search(norm):
        return None
    if _COMPETING_INTENT_RE.search(norm):
        return None

    branch = classify_location_branch_failure(message, history=history)
    if branch is not None:
        return StoreArrivalVerdict(
            trigger=branch.trigger,
            context=branch.context,
            pattern=branch.pattern,
            confidence=branch.confidence,
        )

    m = _STORE_ARRIVAL_RE.search(norm)
    if not m:
        return None

    context = _resolve_location_failure_context(message, history=history)
    if context == "none":
        context = "standalone"

    return StoreArrivalVerdict(
        trigger="store_arrival",
        context=context,
        pattern=(m.group(0) or "")[:48],
    )


def classify_location_branch_failure(
    message: str,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Optional[LocationBranchFailureVerdict]:
    """Detect branch-closed / location-failed follow-ups (telemetry only)."""
    norm = _norm(message)
    if not norm:
        return None
    if _COMPETING_INTENT_RE.search(norm):
        return None

    trigger = ""
    pattern = ""
    m_closed = _BRANCH_CLOSED_RE.search(norm)
    m_failed = _LOCATION_FAILED_RE.search(norm)
    if m_closed:
        trigger = "branch_closed"
        pattern = (m_closed.group(0) or "")[:48]
    elif m_failed:
        trigger = "location_failed"
        pattern = (m_failed.group(0) or "")[:48]
    else:
        return None

    context = _resolve_location_failure_context(message, history=history)
    if context == "none":
        return None

    return LocationBranchFailureVerdict(
        trigger=trigger,
        context=context,
        pattern=pattern,
    )


def log_location_branch_failure(
    *,
    tenant_id: Any = None,
    conversation_id: Any = None,
    trigger: str = "",
    context: str = "",
    matched: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[LOCATION_BRANCH_FAILURE] tenant=%s conversation_id=%s "
            "trigger=%s context=%s matched=%r preview=%r",
            tenant_id if tenant_id is not None else "-",
            conversation_id if conversation_id is not None else "-",
            trigger or "-",
            context or "-",
            (matched or "")[:48],
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def classify_employee_not_responding(message: str) -> Optional[EmployeeNotRespondingVerdict]:
    """Return a verdict when the message is a staff-not-responding follow-up."""
    norm = _norm(message)
    if not norm:
        return None
    if _COMPETING_INTENT_RE.search(norm):
        return None
    m = _EMPLOYEE_NOT_RESPONDING_RE.search(norm)
    if not m:
        return None
    return EmployeeNotRespondingVerdict(matched=True, pattern=m.group(0)[:48])


def _normalize_phone_key(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("966") and len(digits) >= 12:
        return digits[-9:]
    if len(digits) >= 9:
        return digits[-9:]
    return digits


def _normalize_name_key(name: str) -> str:
    return _norm(name)


def parse_staff_contacts_sent(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        phone = str(item.get("phone") or "").strip()
        if not name and not phone:
            continue
        try:
            turn = int(item.get("turn") or 0)
        except (TypeError, ValueError):
            turn = 0
        out.append({"name": name, "phone": phone, "turn": turn})
    return out


def contact_already_sent(
    contacts_sent: Sequence[Dict[str, Any]],
    *,
    name: str = "",
    phone: str = "",
) -> bool:
    name_key = _normalize_name_key(name)
    phone_key = _normalize_phone_key(phone)
    for entry in contacts_sent or []:
        en = _normalize_name_key(str(entry.get("name") or ""))
        ep = _normalize_phone_key(str(entry.get("phone") or ""))
        if phone_key and ep and phone_key == ep:
            return True
        if name_key and en and name_key == en:
            return True
    return False


def append_staff_contact_sent(
    contacts_sent: List[Dict[str, Any]],
    *,
    name: str,
    phone: str,
    turn: int,
) -> List[Dict[str, Any]]:
    """Return a new list with the contact appended (immutable-style)."""
    updated = list(contacts_sent or [])
    updated.append({
        "name": (name or "").strip(),
        "phone": (phone or "").strip(),
        "turn": int(turn or 0),
    })
    return updated


def log_contact_escalation(
    *,
    tenant_id: Any = None,
    trigger: str = "",
    context: str = "",
    name_source: str = "",
    already_sent: bool = False,
    selected_contact: str = "",
    contacts_sent_count: int = 0,
    conversation_id: Any = None,
    policy_allowed: Optional[bool] = None,
) -> None:
    """Emit one grep-friendly ``[CONTACT_ESCALATION]`` line."""
    try:
        policy_suffix = ""
        if policy_allowed is not None:
            policy_suffix = (
                f" policy_allowed={'true' if policy_allowed else 'false'}"
            )
        logger.info(
            "[CONTACT_ESCALATION] tenant=%s conversation_id=%s "
            "trigger=%s context=%s name_source=%s already_sent=%s "
            "selected_contact=%r contacts_sent_count=%d%s",
            tenant_id if tenant_id is not None else "-",
            conversation_id if conversation_id is not None else "-",
            trigger or "-",
            context or "-",
            name_source or "-",
            "true" if already_sent else "false",
            selected_contact or "",
            int(contacts_sent_count or 0),
            policy_suffix,
        )
    except Exception:  # noqa: BLE001
        pass


def persist_staff_contact_sent(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    name: str,
    contact_phone: str,
    turn: int,
) -> bool:
    """Append one entry to ``brain_state.staff_contacts_sent`` (best-effort)."""
    return persist_staff_contacts_sent_batch(
        db,
        tenant_id=tenant_id,
        phone=phone,
        entries=[{"name": name, "phone": contact_phone, "turn": turn}],
    )


def persist_staff_contacts_sent_batch(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    entries: Sequence[Dict[str, Any]],
) -> bool:
    """Append multiple staff-contact entries in one DB commit."""
    if not db or not tenant_id or not phone or not entries:
        return False
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        if conv is None:
            return False
        bs = dict(bs or {})
        existing = parse_staff_contacts_sent(bs.get("staff_contacts_sent"))
        updated = list(existing)
        for entry in entries:
            updated = append_staff_contact_sent(
                updated,
                name=str(entry.get("name") or ""),
                phone=str(entry.get("phone") or ""),
                turn=int(entry.get("turn") or 0),
            )
        bs["staff_contacts_sent"] = updated
        meta = dict(getattr(conv, "extra_metadata", None) or {})
        meta["brain_state"] = bs
        conv.extra_metadata = meta
        flag_modified(conv, "extra_metadata")
        db.add(conv)
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CONTACT_ESCALATION] persist batch failed tenant=%s err=%s",
            tenant_id, exc,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


__all__ = [
    "EmployeeNotRespondingVerdict",
    "LocationBranchFailureVerdict",
    "append_staff_contact_sent",
    "classify_employee_not_responding",
    "classify_location_branch_failure",
    "classify_store_arrival",
    "contact_already_sent",
    "is_branch_list_request",
    "is_branch_location_order_tail",
    "log_contact_escalation",
    "log_location_branch_failure",
    "parse_staff_contacts_sent",
    "persist_staff_contact_sent",
    "persist_staff_contacts_sent_batch",
    "StoreArrivalVerdict",
]
