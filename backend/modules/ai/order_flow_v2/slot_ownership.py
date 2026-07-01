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
_ARABIC_TEXT_RE = re.compile(r"^[\u0600-\u06FF\s]+$", re.UNICODE)


def _city_and_hint_from_text(text: str) -> Tuple[str, str]:
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    try:
        from modules.ai.brain.intent.ordering_extractor import _detect_city  # noqa: PLC0415

        city = str(_detect_city(raw) or "").strip()
    except Exception:  # noqa: BLE001
        city = ""
    if not city:
        return "", ""

    hint = raw
    if city and city in hint:
        hint = hint.replace(city, "", 1).strip()
    elif city.startswith("مكة") and hint.startswith("مكة"):
        hint = hint[len("مكة"):].strip()
    elif city.startswith("مكه") and hint.startswith("مكه"):
        hint = hint[len("مكه"):].strip()
    elif city.startswith("المدينة") and hint.startswith("المدينة"):
        hint = hint[len("المدينة"):].strip()

    return city, hint


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
        name_patch, name_reason = _consume_customer_name_patch(text, prep, missing)
        if name_patch:
            return name_patch, name_reason

    if expected == "city" and _ARABIC_TEXT_RE.match(text):
        city, address_hint = _city_and_hint_from_text(text)
        if city:
            patch["city"] = city
            if address_hint and not prep.get("address_line"):
                patch["address_line"] = address_hint
            patch["order_flow_v2_last_field"] = "delivery_address"
            patch.update(build_contract(
                decision="update_slot",
                field="city",
                reason="city_owned_turn",
                facts={"address_hint": bool(address_hint)},
            ).to_patch())
            return patch, "city_owned"
        patch.update(stamp_last_field_patch(missing))
        patch.update(build_contract(
            decision="ask_missing_field",
            field="city",
            reason="city_uncertain_before_checkout",
        ).to_patch())
        return patch, "city_uncertain"

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

    if "delivery_address" in missing:
        try:
            from core.wa_address_ingestion import is_address_like_delivery_text  # noqa: PLC0415

            if is_address_like_delivery_text(text):
                from core.wa_address_ingestion import build_delivery_address_patch  # noqa: PLC0415

                patch.update(build_delivery_address_patch(text))
                patch["order_flow_v2_last_field"] = "payment_method"
                patch.update(build_contract(
                    decision="update_slot",
                    field="delivery_address",
                    reason="address_like_text_owned_turn",
                ).to_patch())
                return patch, "address_owned"
        except Exception:  # noqa: BLE001  # noqa: silent-ok — address-like probe must not block slot ownership
            pass

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


def _consume_customer_name_patch(
    text: str,
    prep: Dict[str, Any],
    missing: List[str],
) -> Tuple[Dict[str, Any], str]:
    """Own a name-like turn when customer_name is the next missing slot."""
    patch: Dict[str, Any] = {}
    if "customer_name" not in missing or not _ARABIC_SHORT_TEXT_RE.match(text):
        return patch, ""
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
