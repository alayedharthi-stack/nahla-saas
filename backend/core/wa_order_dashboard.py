"""
core/wa_order_dashboard.py
────────────────────────────
Merchant-dashboard helpers for Nahla WhatsApp order lifecycle visibility.

Operational only — derives labels, filters, and action chips from persisted
order state (status, extra_metadata, customer_info). No LLM.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.merchant_payment_confirmation import ADDRESS_MISSING_MERCHANT_NOTICE
from core.order_payment_policy import (
    ORDER_STATUS_PAYMENT_SUBMITTED,
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_METHOD_LABELS_AR,
    infer_payment_method,
    is_payment_explicitly_confirmed,
    is_provider_payment_confirmed,
)
from core.wa_order_lifecycle import (
    STATUS_ABANDONED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DRAFT,
    STATUS_PAID,
    STATUS_PENDING_CUSTOMER_INFO,
    STATUS_PENDING_PAYMENT,
    STATUS_PAYMENT_SUBMITTED,
    has_accepted_delivery_address,
)

# ── Arabic lifecycle labels (merchant-facing) ───────────────────────────────
WA_STATUS_LABELS_AR: Dict[str, str] = {
    STATUS_DRAFT:                 "مسودة طلب",
    STATUS_PENDING_CUSTOMER_INFO:   "ناقص بيانات",
    STATUS_PENDING_PAYMENT:         "بانتظار الدفع",
    STATUS_PAYMENT_SUBMITTED:       "دفع مرسل — يحتاج تحقق",
    STATUS_PAID:                    "مدفوع",
    STATUS_CANCELLED:               "ملغي",
    STATUS_COMPLETED:               "مكتمل",
    STATUS_ABANDONED:               "متروك",
    "cod_pending":                  "دفع عند الاستلام",
    "processing":                   "قيد التجهيز",
}

ADDRESS_STATUS_LABELS_AR: Dict[str, str] = {
    "accepted":   "موقع مُستلم",
    "missing":    "ناقص موقع",
    "required":   "ناقص موقع",
    "unknown":    "—",
}

PAYMENT_STATUS_LABELS_AR: Dict[str, str] = {
    "pending":              "بانتظار الدفع",
    "pending_verification": "دفع مرسل — يحتاج تحقق",
    "submitted":            "دفع مرسل — يحتاج تحقق",
    "paid":                 "مدفوع",
    "cod_pending":          "دفع عند الاستلام",
    "failed":               "فشل الدفع",
    "refunded":             "مُسترد",
}

PAYMENT_VERIFICATION_LABELS_AR: Dict[str, str] = {
    "pending":                    "بانتظار التحقق",
    "pending_merchant_review":    "بانتظار اعتماد التاجر",
    "confirmed":                  "مؤكد",
    "provider_confirmed":         "مؤكد (مزود الدفع)",
}

BANK_TRANSFER_VERIFY_CHIP = "دفع مرسل — يحتاج تحقق"
BANK_TRANSFER_VERIFY_BANNER = (
    "⚠️ العميل أرسل إثبات دفع. تحقق من وصول التحويل البنكي "
    "قبل تجهيز الطلب أو شحنه."
)
MISSING_LOCATION_CHIP = "يحتاج موقع العميل"
MISSING_LOCATION_DETAIL = (
    "العميل لم يرسل رابط Google Maps أو رمز العنوان الوطني المختصر."
)
AWAITING_PAYMENT_CHIP = "بانتظار الدفع"

# Dashboard filter keys (query param ``lifecycle_filter``)
LIFECYCLE_FILTER_ALL = "all"
LIFECYCLE_FILTER_NEEDS_ACTION = "needs_action"
LIFECYCLE_FILTER_MISSING_LOCATION = "missing_location"
LIFECYCLE_FILTER_PENDING_PAYMENT = "pending_payment"
LIFECYCLE_FILTER_PAYMENT_SUBMITTED = "payment_submitted"
LIFECYCLE_FILTER_PAID = "paid"
LIFECYCLE_FILTER_ABANDONED = "abandoned"
LIFECYCLE_FILTER_COMPLETED = "completed"
LIFECYCLE_FILTER_CANCELLED = "cancelled"

VALID_LIFECYCLE_FILTERS = frozenset({
    LIFECYCLE_FILTER_ALL,
    LIFECYCLE_FILTER_NEEDS_ACTION,
    LIFECYCLE_FILTER_MISSING_LOCATION,
    LIFECYCLE_FILTER_PENDING_PAYMENT,
    LIFECYCLE_FILTER_PAYMENT_SUBMITTED,
    LIFECYCLE_FILTER_PAID,
    LIFECYCLE_FILTER_ABANDONED,
    LIFECYCLE_FILTER_COMPLETED,
    LIFECYCLE_FILTER_CANCELLED,
})


def _order_meta(order: Any) -> Dict[str, Any]:
    meta = getattr(order, "extra_metadata", None) or {}
    return meta if isinstance(meta, dict) else {}


def _customer_info(order: Any) -> Dict[str, Any]:
    info = getattr(order, "customer_info", None) or {}
    return info if isinstance(info, dict) else {}


def _parsed_status(order: Any) -> str:
    return str(getattr(order, "status", "") or "").strip().lower()


def _address_prep(order: Any) -> Dict[str, Any]:
    meta = _order_meta(order)
    customer = _customer_info(order)
    return {
        "short_address_code": str(
            meta.get("short_address_code") or customer.get("short_address_code") or ""
        ).strip(),
        "google_maps_url": str(
            meta.get("google_maps_url")
            or meta.get("delivery_address_url")
            or customer.get("google_maps_url")
            or customer.get("delivery_address_url")
            or ""
        ).strip(),
        "delivery_address_url": str(
            meta.get("delivery_address_url") or customer.get("delivery_address_url") or ""
        ).strip(),
        "latitude": meta.get("latitude") or meta.get("delivery_location_lat") or customer.get("latitude"),
        "longitude": meta.get("longitude") or meta.get("delivery_location_lng") or customer.get("longitude"),
        "delivery_location_lat": meta.get("delivery_location_lat") or customer.get("delivery_location_lat"),
        "delivery_location_lng": meta.get("delivery_location_lng") or customer.get("delivery_location_lng"),
        "delivery_address_status": str(
            meta.get("delivery_address_status") or customer.get("delivery_address_status") or ""
        ).strip(),
    }


def is_missing_delivery_address(order: Any) -> bool:
    meta = _order_meta(order)
    missing = meta.get("missing_fields") or []
    if isinstance(missing, list) and "delivery_address" in missing:
        return True
    return not has_accepted_delivery_address(_address_prep(order))


def resolve_address_status_label_ar(order: Any) -> str:
    if has_accepted_delivery_address(_address_prep(order)):
        return ADDRESS_STATUS_LABELS_AR["accepted"]
    if is_missing_delivery_address(order):
        return ADDRESS_STATUS_LABELS_AR["missing"]
    return ADDRESS_STATUS_LABELS_AR["unknown"]


def resolve_wa_status_label_ar(raw_status: str, *, order: Optional[Any] = None) -> str:
    norm = str(raw_status or "").strip().lower()
    if norm == STATUS_PENDING_CUSTOMER_INFO and order is not None and is_missing_delivery_address(order):
        return "ناقص موقع"
    return WA_STATUS_LABELS_AR.get(norm, norm or "—")


def resolve_payment_status_label_ar(meta: Dict[str, Any]) -> Optional[str]:
    raw = str(meta.get("payment_status") or "").strip().lower()
    if not raw:
        return None
    return PAYMENT_STATUS_LABELS_AR.get(raw, raw)


def resolve_payment_verification_label_ar(meta: Dict[str, Any]) -> Optional[str]:
    raw = str(meta.get("payment_verification_status") or "").strip().lower()
    if not raw:
        if meta.get("payment_confirmed"):
            return PAYMENT_VERIFICATION_LABELS_AR["confirmed"]
        return None
    return PAYMENT_VERIFICATION_LABELS_AR.get(raw, raw)


def build_action_chips(order: Any) -> List[Dict[str, str]]:
    """Operational chips for list/detail — distinct from legacy needs_action extras."""
    chips: List[Dict[str, str]] = []
    status = _parsed_status(order)
    meta = _order_meta(order)
    payment_method = infer_payment_method(None, meta)
    confirmed = is_payment_explicitly_confirmed(None, meta) or is_provider_payment_confirmed(meta)

    if status == STATUS_PENDING_CUSTOMER_INFO and is_missing_delivery_address(order):
        chips.append({
            "key":   "missing_location",
            "label": MISSING_LOCATION_CHIP,
            "level": "amber",
            "detail": MISSING_LOCATION_DETAIL,
        })

    if status == STATUS_PENDING_PAYMENT and not confirmed:
        chips.append({
            "key":   "awaiting_payment",
            "label": AWAITING_PAYMENT_CHIP,
            "level": "amber",
        })

    if (
        status == STATUS_PAYMENT_SUBMITTED
        and payment_method == PAYMENT_METHOD_BANK_TRANSFER
        and not confirmed
    ):
        chips.append({
            "key":   "payment_submitted_verify",
            "label": BANK_TRANSFER_VERIFY_CHIP,
            "level": "red",
            "detail": BANK_TRANSFER_VERIFY_BANNER,
        })

    if confirmed and is_missing_delivery_address(order):
        chips.append({
            "key":   "paid_missing_address",
            "label": "الدفع مؤكد — العنوان ناقص",
            "level": "amber",
            "detail": ADDRESS_MISSING_MERCHANT_NOTICE,
        })

    return chips


def compute_wa_needs_action(order: Any) -> Tuple[bool, List[Dict[str, str]]]:
    """
    Return ``(needs_action_flag, action_items)`` for WhatsApp lifecycle orders.
    """
    items = build_action_chips(order)
    return bool(items), items


def resolve_lifecycle_filter_key(order: Any) -> str:
    """Primary bucket for DB / API lifecycle filtering."""
    status = _parsed_status(order)
    if status == STATUS_ABANDONED or bool(getattr(order, "is_abandoned", False)):
        return LIFECYCLE_FILTER_ABANDONED
    if status == STATUS_CANCELLED:
        return LIFECYCLE_FILTER_CANCELLED
    if status == STATUS_COMPLETED:
        return LIFECYCLE_FILTER_COMPLETED
    if status == STATUS_PAID:
        return LIFECYCLE_FILTER_PAID
    if status == STATUS_PAYMENT_SUBMITTED:
        return LIFECYCLE_FILTER_PAYMENT_SUBMITTED
    if status == STATUS_PENDING_PAYMENT:
        return LIFECYCLE_FILTER_PENDING_PAYMENT
    if status == STATUS_PENDING_CUSTOMER_INFO and is_missing_delivery_address(order):
        return LIFECYCLE_FILTER_MISSING_LOCATION
    if status == STATUS_DRAFT:
        return LIFECYCLE_FILTER_PENDING_PAYMENT  # drafts surface under needs_action
    return LIFECYCLE_FILTER_ALL


def order_matches_lifecycle_filter(order: Any, lifecycle_filter: str) -> bool:
    """Deterministic filter match for list endpoint."""
    filt = (lifecycle_filter or LIFECYCLE_FILTER_ALL).strip().lower()
    if filt == LIFECYCLE_FILTER_ALL or filt not in VALID_LIFECYCLE_FILTERS:
        return True

    status = _parsed_status(order)
    meta = _order_meta(order)

    if filt == LIFECYCLE_FILTER_NEEDS_ACTION:
        flag, _ = compute_wa_needs_action(order)
        return flag

    if filt == LIFECYCLE_FILTER_MISSING_LOCATION:
        return status == STATUS_PENDING_CUSTOMER_INFO and is_missing_delivery_address(order)

    if filt == LIFECYCLE_FILTER_PENDING_PAYMENT:
        return status == STATUS_PENDING_PAYMENT

    if filt == LIFECYCLE_FILTER_PAYMENT_SUBMITTED:
        return status == STATUS_PAYMENT_SUBMITTED

    if filt == LIFECYCLE_FILTER_PAID:
        return status == STATUS_PAID or bool(meta.get("payment_confirmed"))

    if filt == LIFECYCLE_FILTER_ABANDONED:
        return status == STATUS_ABANDONED or bool(getattr(order, "is_abandoned", False))

    if filt == LIFECYCLE_FILTER_COMPLETED:
        return status == STATUS_COMPLETED

    if filt == LIFECYCLE_FILTER_CANCELLED:
        return status in (STATUS_CANCELLED, "canceled")

    return True


def _google_maps_link_from_coords(lat: Any, lng: Any) -> Optional[str]:
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return None
    return f"https://www.google.com/maps?q={lat_f},{lng_f}"


def build_delivery_location_display(order: Any) -> Optional[Dict[str, Any]]:
    """
    Structured address/location block for order detail.
    """
    meta = _order_meta(order)
    customer = _customer_info(order)
    prep = _address_prep(order)

    maps_url = prep.get("google_maps_url") or prep.get("delivery_address_url") or ""
    lat = prep.get("latitude") or prep.get("delivery_location_lat")
    lng = prep.get("longitude") or prep.get("delivery_location_lng")
    short_code = prep.get("short_address_code") or ""
    location_name = str(
        meta.get("location_name") or customer.get("location_name") or ""
    ).strip()
    address_text = str(
        customer.get("address")
        or meta.get("delivery_address_text")
        or customer.get("delivery_address_text")
        or ""
    ).strip()
    source = str(meta.get("delivery_address_source") or "").strip().lower()

    if lat is not None and lng is not None and not maps_url:
        open_url = _google_maps_link_from_coords(lat, lng)
        return {
            "type":            "whatsapp_location",
            "type_label_ar":   "موقع واتساب",
            "latitude":        lat,
            "longitude":       lng,
            "location_name":   location_name or None,
            "address_text":    address_text or None,
            "open_url":        open_url,
            "short_address_code": short_code or None,
        }

    if maps_url:
        lower = maps_url.lower()
        if "apple" in lower or "maps.apple.com" in lower:
            type_key = "apple_maps"
            type_label = "رابط Apple Maps"
        else:
            type_key = "maps_url"
            type_label = "رابط خرائط"
        return {
            "type":            type_key,
            "type_label_ar":   type_label,
            "url":             maps_url,
            "open_url":        maps_url,
            "location_name":   location_name or None,
            "address_text":    address_text or None,
            "short_address_code": short_code or None,
        }

    if short_code:
        return {
            "type":              "short_national_address",
            "type_label_ar":     "رمز العنوان الوطني",
            "short_address_code": short_code,
            "address_text":      address_text or None,
        }

    if source == "whatsapp_location" and lat is not None and lng is not None:
        return build_delivery_location_display(order)

    return None


def build_list_summary(
    *,
    customer_name: str,
    items_text: str,
    amount_text: str,
    city_line: str,
    payment_label: Optional[str],
    address_label: str,
) -> str:
    parts = [p for p in (
        customer_name,
        items_text,
        amount_text,
        city_line,
        payment_label,
        address_label,
    ) if p and p != "—"]
    return " — ".join(parts)


def build_city_line(order: Any) -> str:
    customer = _customer_info(order)
    meta = _order_meta(order)
    parts = [
        str(customer.get("city") or meta.get("city") or "").strip(),
        str(customer.get("district") or meta.get("district") or "").strip(),
        str(customer.get("neighborhood") or meta.get("neighborhood") or "").strip(),
    ]
    line = " — ".join(p for p in parts if p)
    return line or "—"


__all__ = [
    "ADDRESS_STATUS_LABELS_AR",
    "BANK_TRANSFER_VERIFY_BANNER",
    "LIFECYCLE_FILTER_ABANDONED",
    "LIFECYCLE_FILTER_ALL",
    "LIFECYCLE_FILTER_CANCELLED",
    "LIFECYCLE_FILTER_COMPLETED",
    "LIFECYCLE_FILTER_MISSING_LOCATION",
    "LIFECYCLE_FILTER_NEEDS_ACTION",
    "LIFECYCLE_FILTER_PAID",
    "LIFECYCLE_FILTER_PAYMENT_SUBMITTED",
    "LIFECYCLE_FILTER_PENDING_PAYMENT",
    "PAYMENT_STATUS_LABELS_AR",
    "PAYMENT_VERIFICATION_LABELS_AR",
    "VALID_LIFECYCLE_FILTERS",
    "WA_STATUS_LABELS_AR",
    "build_action_chips",
    "build_city_line",
    "build_delivery_location_display",
    "build_list_summary",
    "compute_wa_needs_action",
    "is_missing_delivery_address",
    "order_matches_lifecycle_filter",
    "resolve_address_status_label_ar",
    "resolve_lifecycle_filter_key",
    "resolve_payment_status_label_ar",
    "resolve_payment_verification_label_ar",
    "resolve_wa_status_label_ar",
]
