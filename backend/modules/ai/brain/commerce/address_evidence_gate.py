"""
Address evidence gate — SA checkout requires shippable address evidence.

Operational rule: order confirmation / payment must not proceed without at
least one of:
  * Google Maps URL (or lat/lng resolved from maps)
  * Saudi national short address code
  * Structured national address (street + district + postal)
  * Validated free-form address (city + district/street + number/description)
"""
from __future__ import annotations

import re
import unicodedata

from modules.ai.brain.types import OrderPreparationState

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_DISTRICT_RE = re.compile(
    r"(?:حي|حى|حي\s|منطقة|حارة|حاره|محافظة\s+\S+)",
    re.IGNORECASE | re.UNICODE,
)
_STREET_RE = re.compile(
    r"(?:شارع|طريق|سكة|زقاق|طريق\s+\S+)",
    re.IGNORECASE | re.UNICODE,
)
_NUMBER_RE = re.compile(r"\d{1,5}")


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


def _has_structured_address(prep: OrderPreparationState) -> bool:
    return bool(prep.street and prep.district and prep.postal_code)


def is_valid_shippable_freeform_sa_address(prep: OrderPreparationState) -> bool:
    """True when a descriptive address_line is specific enough to ship."""
    city = (prep.city or "").strip()
    line = (prep.address_line or "").strip()
    if not city or not line:
        return False

    norm = _norm(line)
    has_district = bool(_DISTRICT_RE.search(norm))
    has_street = bool(_STREET_RE.search(norm))
    has_number = bool(
        (prep.building_number or "").strip()
        or _NUMBER_RE.search(norm)
    )

    if prep.district and prep.street and (prep.building_number or has_number):
        return True
    return (has_district or has_street) and has_number


def has_sa_address_evidence(prep: OrderPreparationState) -> bool:
    """Operational address evidence for Saudi checkout."""
    if prep.short_address_code or prep.google_maps_url:
        return True
    if prep.latitude is not None and prep.longitude is not None:
        return True
    if _has_structured_address(prep):
        return True
    return is_valid_shippable_freeform_sa_address(prep)


MSG_SA_ADDRESS_EVIDENCE_REQUIRED = (
    "أرسل رابط الموقع من Google Maps أو الرمز المختصر للعنوان الوطني "
    "عشان نكمل الطلب بدقة."
)


__all__ = [
    "MSG_SA_ADDRESS_EVIDENCE_REQUIRED",
    "has_sa_address_evidence",
    "is_valid_shippable_freeform_sa_address",
]
