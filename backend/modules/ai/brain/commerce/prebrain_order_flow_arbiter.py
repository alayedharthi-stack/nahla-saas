"""
prebrain_order_flow_arbiter.py
──────────────────────────────
Central ownership arbiter: active order flow owns slot answers.

Pre-brain contact/showroom/handoff policies must yield when checkout
signals show the customer is answering an awaited slot — not when weak
contact wording appears inside a checkout answer.

Platform-wide: evidence from persisted order_prep + stage + extractor —
no tenant hardcoding, no phrase whitelist.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional, Set

logger = logging.getLogger("nahla.brain.prebrain_order_flow_arbiter")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_CHECKOUT_STAGES = frozenset({"ordering", "checkout", "deciding"})

_IDENTITY_SLOTS = frozenset({
    "customer_first_name",
    "customer_last_name",
    "customer_name",
    "customer_phone",
    "customer_email",
})

_ADDRESS_SLOTS = frozenset({
    "address",
    "address_location",
    "address_line",
    "short_address_code",
    "google_maps_url",
    "delivery_address",
    "location",
    "city",
    "district",
    "street",
    "postal_code",
    "country",
})

_OTHER_CHECKOUT_SLOTS = frozenset({
    "payment_method",
    "quantity",
})

_ALL_CHECKOUT_SLOTS = _IDENTITY_SLOTS | _ADDRESS_SLOTS | _OTHER_CHECKOUT_SLOTS

_ACTIVE_ORDER_STATUSES = frozenset({
    "awaiting_address",
    "awaiting_product",
    "awaiting_payment",
    "awaiting_payment_receipt",
})

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.UNICODE,
)

_PAYMENT_CHOICE_RE = re.compile(
    r"(?:"
    r"تحويل|حوال(?:ه|ة)|mada|apple\s*pay|stc\s*pay|"
    r"tab(?:i|y)|tamara|visa|master|card|بطاق(?:ه|ة)|"
    r"كاش|cash|نقد"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_QUANTITY_ANSWER_RE = re.compile(
    r"(?:"
    r"^\s*\d+\s*$"
    r"|(?:^|\s)(?:واحد|واحده|اثن(?:ين|ين)|ثن(?:ين|ين)|ثلاث(?:ه|ة)?|"
    r"اربع(?:ه|ة)?|خمس(?:ه|ة)?|ست(?:ه|ة)?|سبع(?:ه|ة)?|"
    r"ثمان(?:يه|ية)?|تسع(?:ه|ة)?|عشر(?:ه|ة)?|نصف|ربع|كيلو)"
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


def _missing_fields_from_order_prep(order_prep: Any) -> Set[str]:
    if order_prep is None:
        return set()
    if isinstance(order_prep, dict):
        raw = order_prep.get("missing_fields") or []
    else:
        raw = getattr(order_prep, "missing_fields", None) or []
    return {str(x).strip().lower() for x in raw if x}


def _order_prep_has_product(order_prep: Any) -> bool:
    if order_prep is None:
        return False
    if isinstance(order_prep, dict):
        return bool(
            str(order_prep.get("product_id") or order_prep.get("product_name") or "").strip()
        )
    return bool(
        str(getattr(order_prep, "product_id", "") or getattr(order_prep, "product_name", "") or "").strip()
    )


def _order_prep_flag(order_prep: Any, key: str) -> bool:
    if order_prep is None:
        return False
    if isinstance(order_prep, dict):
        return bool(order_prep.get(key))
    return bool(getattr(order_prep, key, False))


def is_active_order_flow(*, stage: str = "", order_prep: Any = None) -> bool:
    """True when checkout/order collection is in progress."""
    if str(stage or "").strip().lower() in _CHECKOUT_STAGES:
        return True
    if order_prep is None:
        return False
    if _order_prep_flag(order_prep, "awaiting_option_confirmation"):
        return True
    if _order_prep_flag(order_prep, "awaiting_payment_receipt"):
        return True
    if isinstance(order_prep, dict):
        status = str(order_prep.get("order_status") or "").strip().lower()
        has_city = bool(str(order_prep.get("city") or "").strip())
    else:
        status = str(getattr(order_prep, "order_status", "") or "").strip().lower()
        has_city = bool(str(getattr(order_prep, "city", "") or "").strip())
    missing = _missing_fields_from_order_prep(order_prep)
    if status in _ACTIVE_ORDER_STATUSES:
        return True
    if _order_prep_has_product(order_prep) and missing:
        return True
    if _order_prep_has_product(order_prep) and not has_city:
        return True
    return False


def has_strong_prebrain_contact_intent(message: str) -> bool:
    """
    True when pre-brain contact/handoff policies may legitimately own the turn.

    Uses structural staff/handoff/complaint evidence — not bare phone tokens
    like «جوال» that often appear inside checkout slot answers.
    """
    raw = (message or "").strip()
    if not raw:
        return False

    norm = _norm(raw)

    try:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            _CS_REQUEST_RE,
            _GENERIC_STAFF_RE,
            _ROLE_STAFF_RE,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional staff regex import must not block arbiter
        _CS_REQUEST_RE = _GENERIC_STAFF_RE = _ROLE_STAFF_RE = None  # type: ignore[assignment]

    if _GENERIC_STAFF_RE and _GENERIC_STAFF_RE.search(norm):
        return True
    if _ROLE_STAFF_RE and _ROLE_STAFF_RE.search(norm):
        return True
    if _CS_REQUEST_RE and _CS_REQUEST_RE.search(norm):
        return True

    try:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            extract_staff_name_candidate,
            is_thanks_with_contact_phrase,
        )

        if extract_staff_name_candidate(raw):
            return True
        if is_thanks_with_contact_phrase(raw):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional entity guard probe must not block arbiter
        pass

    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_contact_pronoun_followup,
            is_explicit_arrival_intent,
        )

        if is_contact_pronoun_followup(raw):
            return True
        if is_explicit_arrival_intent(raw):
            return True
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — optional contact route probe must not block arbiter
        logger.debug("[PREBRAIN_ARBITER] contact_route_probe_failed err=%s", exc)

    try:
        from modules.ai.brain.commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
            has_explicit_showroom_pickup_intent,
        )

        if has_explicit_showroom_pickup_intent(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional showroom probe must not block arbiter
        pass

    try:
        from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
            classify_employee_not_responding,
        )

        if classify_employee_not_responding(raw) is not None:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional escalation probe must not block arbiter
        pass

    try:
        from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: PLC0415
            classify_complaint_refund,
        )

        if classify_complaint_refund(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional complaint probe must not block arbiter
        pass

    try:
        from modules.ai.brain.commerce.staff_contact_evidence import _CONTACT_ASK_RE  # noqa: PLC0415

        if _CONTACT_ASK_RE.search(norm) and re.search(
            r"(?:حولني|موظف|شخص|بشر|خدمة\s*العملاء|الدعم|الادارة|الإدارة|"
            r"customer\s*service|support)",
            norm,
            flags=re.UNICODE | re.IGNORECASE,
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional ask-regex probe must not block arbiter
        pass

    return False


def _extract_ordering_slots(message: str) -> dict[str, Any]:
    try:
        from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots  # noqa: PLC0415

        return extract_ordering_slots(message or "") or {}
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional ordering extractor must not block arbiter
        return {}


def is_weak_contact_reference_during_checkout(
    message: str,
    *,
    order_prep: Any,
    missing: Set[str],
    customer_phone: str = "",
) -> bool:
    """
    Weak store-channel wording inside a checkout identity answer.

    Uses contact wording + absence of strong ask intent — not a phrase list.
    """
    if not missing & _IDENTITY_SLOTS:
        return False
    if has_strong_prebrain_contact_intent(message):
        return False

    slots = _extract_ordering_slots(message)
    has_name_signal = bool(slots.get("customer_first_name") or slots.get("customer_name"))
    if has_name_signal:
        return True

    try:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            is_store_channel_phone_phrase,
        )
        from modules.ai.brain.commerce.staff_contact_evidence import _CONTACT_ASK_RE  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional weak-contact imports must not block arbiter
        return False

    raw = (message or "").strip()
    if not raw:
        return False

    norm = _norm(raw)
    if not is_store_channel_phone_phrase(raw):
        return False
    if _CONTACT_ASK_RE.search(norm):
        return False

    known_whatsapp = bool(str(customer_phone or "").strip())
    phone_slot_open = "customer_phone" in missing
    if known_whatsapp and not phone_slot_open:
        return True
    if phone_slot_open and not _CONTACT_ASK_RE.search(norm):
        return True
    return False


def message_fulfills_awaited_checkout_slot(
    message: str,
    *,
    order_prep: Any,
    customer_phone: str = "",
) -> bool:
    """True when inbound text satisfies an awaited checkout slot."""
    missing = _missing_fields_from_order_prep(order_prep)
    if not missing and not _order_prep_flag(order_prep, "awaiting_option_confirmation"):
        return False

    if _order_prep_flag(order_prep, "awaiting_option_confirmation"):
        if has_strong_prebrain_contact_intent(message):
            return False
        return True

    if _order_prep_flag(order_prep, "awaiting_payment_receipt"):
        return not has_strong_prebrain_contact_intent(message)

    slots = _extract_ordering_slots(message)

    if missing & _IDENTITY_SLOTS:
        if slots.get("customer_first_name") or slots.get("customer_name"):
            return True
        if "customer_phone" in missing and slots.get("customer_phone"):
            return True
        if "customer_email" in missing and _EMAIL_RE.search(message or ""):
            return True

    if "city" in missing and slots.get("city"):
        return True

    if missing & {
        "address",
        "short_address_code",
        "google_maps_url",
        "address_location",
        "delivery_address",
        "location",
    }:
        if slots.get("short_address_code") or slots.get("google_maps_url"):
            return True
        try:
            from core.wa_address_ingestion import (  # noqa: PLC0415
                is_accepted_maps_url,
                is_bare_short_address_code,
            )

            if is_bare_short_address_code(message or "") or is_accepted_maps_url(message or ""):
                return True
        except Exception:  # noqa: BLE001  # noqa: silent-ok — optional address probe must not block arbiter
            pass

    if "payment_method" in missing and _PAYMENT_CHOICE_RE.search(_norm(message or "")):
        return True

    if "quantity" in missing and _QUANTITY_ANSWER_RE.search(message or ""):
        return True

    try:
        from modules.ai.brain.commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
            is_bare_city_token_message,
        )

        if "city" in missing and is_bare_city_token_message(message or ""):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional city token probe must not block arbiter
        pass

    if is_weak_contact_reference_during_checkout(
        message,
        order_prep=order_prep,
        missing=missing,
        customer_phone=customer_phone,
    ):
        return True

    return False


def load_order_flow_context(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
) -> tuple[str, dict[str, Any]]:
    """Load ``(stage, order_prep_dict)`` from persisted brain state."""
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415

        _, brain_state = _load_brain_state(
            db,
            tenant_id=int(tenant_id or 0),
            phone=str(customer_phone or ""),
        )
        bs = brain_state or {}
        stage = str(bs.get("stage") or "").strip().lower()
        op = bs.get("order_prep") or {}
        if not isinstance(op, dict):
            op = {}
        return stage, op
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — brain state load failure falls back to bare city token
        logger.debug(
            "[PREBRAIN_ARBITER] brain_state load skipped tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return "", {}


def should_yield_prebrain_to_order_flow(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
) -> bool:
    """
    Return True when pre-brain shortcuts must defer to Brain / order flow.

    Order flow owns slot answers unless strong staff/contact intent is present.
    """
    raw = (message or "").strip()
    if not raw:
        return False

    if has_strong_prebrain_contact_intent(raw):
        return False

    stage, order_prep = load_order_flow_context(
        db,
        tenant_id=int(tenant_id or 0),
        customer_phone=customer_phone or "",
    )

    if not is_active_order_flow(stage=stage, order_prep=order_prep):
        return False

    if message_fulfills_awaited_checkout_slot(
        raw,
        order_prep=order_prep,
        customer_phone=customer_phone or "",
    ):
        missing = sorted(_missing_fields_from_order_prep(order_prep))[:8]
        logger.info(
            "[PREBRAIN_ARBITER] yield=true tenant=%s stage=%s "
            "reason=slot_answer preview=%r missing=%s",
            tenant_id,
            stage or "-",
            raw[:80],
            missing,
        )
        return True

    return False


__all__ = [
    "has_strong_prebrain_contact_intent",
    "is_active_order_flow",
    "load_order_flow_context",
    "message_fulfills_awaited_checkout_slot",
    "should_yield_prebrain_to_order_flow",
]
