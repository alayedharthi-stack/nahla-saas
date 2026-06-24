"""OrderFlowV2 slot ownership and progression guards."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from core.wa_order_lifecycle import has_accepted_delivery_address

from .contract import build_contract
_FIELD_ORDER = ("customer_name", "city", "delivery_address", "payment_method", "product")

_ARABIC_SHORT_TEXT_RE = re.compile(r"^[\u0600-\u06FF]{2,}(?:\s+[\u0600-\u06FF]{2,})?$")
_ADDRESS_REFUSAL_RE = re.compile(
    r"^(?:لا|لا\s*يوجد|ما\s*عندي|ماعندي|بدون|لاحقا|بعدها|بعدين)$",
    re.I | re.UNICODE,
)
_PAYMENT_ATTEMPT_RE = re.compile(
    r"(?:تحويل|بنك|الاهلي|الأهلي|راجحي|الراجحي|دفع|كاش|استلام|iban|bank|cod)",
    re.I | re.UNICODE,
)
_SHORT_CODE_RE = re.compile(r"\b[A-Z]{4}\d{4}\b", re.I)
_ADDRESS_CLUE_RE = re.compile(
    r"(?:حي|شارع|طريق|منزل|بيت|عمارة|شقة|قريب|بجوار|مكة|جدة|الرياض|المدينة|الدمام|الخبر)",
    re.I | re.UNICODE,
)


def has_address_evidence(order_prep: Dict[str, Any]) -> bool:
    if has_accepted_delivery_address(order_prep):
        return True
    line = str(order_prep.get("address_line") or "").strip()
    if not line:
        return False
    if _ADDRESS_REFUSAL_RE.match(line):
        return False
    if _SHORT_CODE_RE.search(line):
        return True
    return len(line) >= 12 and bool(_ADDRESS_CLUE_RE.search(line))


def address_refusal(message: str) -> bool:
    return bool(_ADDRESS_REFUSAL_RE.match(str(message or "").strip()))


def payment_attempt(message: str) -> bool:
    return bool(_PAYMENT_ATTEMPT_RE.search(str(message or "").strip()))


def stamp_last_field_patch(missing_fields: List[str]) -> Dict[str, Any]:
    field = _next_missing_field(list(missing_fields or [])) or ""
    if not field:
        return {}
    return {"order_flow_v2_last_field": field}


def apply_slot_ownership(
    *,
    message: str,
    order_prep: Dict[str, Any],
    missing_fields: List[str],
) -> Tuple[Dict[str, Any], str]:
    """Return state patch + reason for current-turn slot ownership."""
    text = str(message or "").strip()
    prep = dict(order_prep or {})
    missing = list(missing_fields or [])
    patch: Dict[str, Any] = {}
    expected = _next_missing_field(missing) or ""
    last_field = str(prep.get("order_flow_v2_last_field") or "").strip()

    if not text:
        return stamp_last_field_patch(missing), "empty"

    if (
        last_field in {"customer_name", "customer_last_name", "last_name"}
        and expected == "city"
        and _ARABIC_SHORT_TEXT_RE.match(text)
    ):
        patch["customer_last_name"] = text
        patch["order_flow_v2_last_field"] = "city"
        patch.update(build_contract(
            decision="update_slot",
            field="customer_last_name",
            reason="last_name_correction_before_city",
        ).to_patch())
        return patch, "last_name_correction"

    if expected == "customer_name" and _ARABIC_SHORT_TEXT_RE.match(text):
        if prep.get("customer_first_name") and not prep.get("customer_last_name"):
            patch["customer_last_name"] = text
            patch["order_flow_v2_last_field"] = "city"
            field = "customer_last_name"
        elif not prep.get("customer_first_name"):
            patch["customer_first_name"] = text.split()[0]
            if len(text.split()) > 1:
                patch["customer_last_name"] = " ".join(text.split()[1:])
                patch["order_flow_v2_last_field"] = "city"
            else:
                patch["order_flow_v2_last_field"] = "customer_name"
            field = "customer_first_name"
        else:
            patch["order_flow_v2_last_field"] = "customer_name"
            field = "customer_name"
        patch.update(build_contract(
            decision="update_slot",
            field=field,
            reason="customer_name_owned_turn",
        ).to_patch())
        return patch, "customer_name_owned"

    if expected == "delivery_address" and address_refusal(text):
        patch.update({
            "order_flow_v2_last_field": "delivery_address",
            "order_flow_v2_address_refused": True,
        })
        patch.update(build_contract(
            decision="ask_missing_field",
            field="delivery_address",
            reason="address_required_before_payment",
            facts={"address_refused": True},
        ).to_patch())
        return patch, "address_refusal"

    if expected == "delivery_address" and payment_attempt(text):
        patch.update(stamp_last_field_patch(missing))
        patch.update(build_contract(
            decision="ask_missing_field",
            field="delivery_address",
            reason="payment_blocked_until_address",
        ).to_patch())
        return patch, "payment_before_address"

    patch.update(stamp_last_field_patch(missing))
    return patch, "no_slot_override"


def higher_priority_missing_before_payment(missing_fields: List[str]) -> bool:
    missing = list(missing_fields or [])
    if "payment_method" not in missing:
        return False
    for field in missing:
        if field == "payment_method":
            return False
        if field in {"product", "customer_name", "city", "delivery_address"}:
            return True
    return False


def _next_missing_field(missing: List[str]) -> str:
    for field in _FIELD_ORDER:
        if field in missing:
            return field
    return ""
