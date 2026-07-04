"""
core/salla_order_fidelity.py
────────────────────────────
Parse and preserve Salla order timestamps, money fields, line items,
and customer/address evidence for merchant-facing fidelity.

Salla is source of truth for imported orders — never substitute catalog prices.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

SALLA_DEFAULT_TZ = "Asia/Riyadh"


def _riyadh_tz() -> timezone:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(SALLA_DEFAULT_TZ)  # type: ignore[return-value]
        except Exception:
            pass
    return timezone(timedelta(hours=3))

_AMOUNT_CONTAINER_KEYS = frozenset({
    "total", "sub_total", "subtotal", "tax", "discount", "discounts",
    "coupon_discount", "shipping", "fees", "cash_on_delivery", "amount",
})


def extract_salla_money_amount(value: Any) -> Optional[str]:
    """Extract a numeric amount string from Salla money scalars or dicts."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        if any(k in value for k in _AMOUNT_CONTAINER_KEYS):
            for key in ("total", "grand_total", "amount", "value"):
                if key in value:
                    nested = extract_salla_money_amount(value.get(key))
                    if nested:
                        return nested
            return None
        amt = value.get("amount")
        if amt is None:
            amt = value.get("value")
        if amt is not None:
            return str(amt).strip() or None
        return None
    text = (
        str(value)
        .replace("ر.س", "")
        .replace("SAR", "")
        .replace(",", "")
        .strip()
    )
    if not text:
        return None
    try:
        float(text.split()[0])
    except (TypeError, ValueError):
        return None
    return text.split()[0]


def extract_salla_grand_total(raw: Dict[str, Any]) -> str:
    """Best-effort Salla grand total — prefers ``amounts.total``."""
    amounts = raw.get("amounts") if isinstance(raw.get("amounts"), dict) else {}
    for candidate in (
        amounts.get("total"),
        raw.get("total"),
        amounts.get("sub_total"),
        amounts.get("subtotal"),
        raw.get("sub_total"),
        raw.get("amount"),
        raw.get("price"),
    ):
        amt = extract_salla_money_amount(candidate)
        if amt:
            try:
                if float(amt) > 0:
                    return amt
            except (TypeError, ValueError):
                continue
    return extract_salla_money_amount(raw.get("total")) or "0"


def extract_salla_amounts_breakdown(raw: Dict[str, Any]) -> Dict[str, str]:
    """Normalised monetary breakdown for ``extra_metadata.salla_amounts``."""
    amounts = raw.get("amounts") if isinstance(raw.get("amounts"), dict) else {}
    if not amounts:
        return {}

    field_map = (
        ("subtotal", ("sub_total", "subtotal")),
        ("discounts", ("discount", "discounts")),
        ("coupon_discount", ("coupon_discount", "coupon")),
        ("shipping", ("shipping", "shipping_cost")),
        ("tax", ("tax", "vat")),
        ("fees", ("fees", "cash_on_delivery")),
        ("total", ("total",)),
    )
    out: Dict[str, str] = {}
    for out_key, src_keys in field_map:
        for src in src_keys:
            if src not in amounts:
                continue
            amt = extract_salla_money_amount(amounts.get(src))
            if amt is not None:
                out[out_key] = amt
                break

    total_block = amounts.get("total")
    if isinstance(total_block, dict) and total_block.get("currency"):
        out["currency"] = str(total_block["currency"])
    elif raw.get("currency"):
        out["currency"] = str(raw["currency"])
    else:
        out.setdefault("currency", "SAR")
    return out


def parse_salla_order_datetime(raw: Dict[str, Any]) -> Tuple[Optional[datetime], Dict[str, str]]:
    """
  Return UTC ``datetime`` plus metadata stamps:
  ``salla_created_at``, ``salla_date``, ``salla_timezone``.
    """
    stamps: Dict[str, str] = {}
    candidates: List[Any] = [
        raw.get("created_at"),
        raw.get("date"),
        raw.get("order_date"),
    ]
    for value in candidates:
        if not value:
            continue
        tz_name = SALLA_DEFAULT_TZ
        text = ""
        if isinstance(value, dict):
            text = str(value.get("date") or value.get("iso") or value.get("formatted") or "").strip()
            tz_name = str(value.get("timezone") or SALLA_DEFAULT_TZ).strip() or SALLA_DEFAULT_TZ
        else:
            text = str(value).strip()
        if not text:
            continue

        stamps["salla_created_at"] = text
        stamps["salla_timezone"] = tz_name
        if "T" in text or " " in text:
            stamps["salla_date"] = text.split("T", 1)[0].split(" ", 1)[0]
        else:
            stamps["salla_date"] = text[:10]

        parsed = _parse_datetime_to_utc(text, tz_name)
        if parsed is not None:
            stamps["created_at"] = parsed.isoformat()
            return parsed, stamps
    return None, stamps


def _parse_datetime_to_utc(text: str, tz_name: str) -> Optional[datetime]:
    has_offset = text.endswith("Z") or "+" in text[10:] or text.count("-") > 2
    variants = [text.replace("Z", "+00:00"), text.replace(" ", "T", 1)]
    if "." in text:
        variants.append(text.split(".", 1)[0].replace(" ", "T", 1))

    for variant in variants:
        try:
            dt = datetime.fromisoformat(variant)
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc)
            if has_offset:
                return dt.replace(tzinfo=timezone.utc)
            try:
                local_tz = ZoneInfo(tz_name) if ZoneInfo is not None else _riyadh_tz()
            except Exception:
                local_tz = _riyadh_tz()
            return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
        except Exception:
            continue
    return None


def normalize_salla_line_items(items: Any) -> List[Dict[str, Any]]:
    """Preserve purchased line prices; parse Salla ``price`` dicts."""
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for li in items:
        if not isinstance(li, dict):
            continue
        row = dict(li)
        name = (
            row.get("name")
            or row.get("product_name")
            or row.get("product_title")
            or row.get("title")
            or ""
        )
        if name:
            row["name"] = name
            row["product_name"] = name
            row.setdefault("title", name)

        qty_raw = row.get("quantity") or 1
        try:
            row["quantity"] = max(int(qty_raw), 1)
        except (TypeError, ValueError):
            row["quantity"] = 1

        unit_amt = extract_salla_money_amount(
            row.get("unit_price") or row.get("price")
        )
        if unit_amt is not None:
            try:
                row["unit_price"] = float(unit_amt)
                row["price"] = float(unit_amt)
            except (TypeError, ValueError):
                pass

        line_total_amt = extract_salla_money_amount(
            row.get("line_total") or row.get("total") or row.get("amount")
        )
        if line_total_amt is not None:
            try:
                row["line_total"] = float(line_total_amt)
            except (TypeError, ValueError):
                pass

        if row.get("product_id") is not None:
            row["product_id"] = str(row["product_id"])
        if row.get("variant_id") is not None:
            row["variant_id"] = str(row["variant_id"])
        out.append(row)
    return out


def enrich_salla_customer_info(raw: Dict[str, Any], customer_info: Dict[str, Any]) -> Dict[str, Any]:
    """Merge Salla customer, shipping, and payment blocks without dropping fields."""
    out = dict(customer_info or {})
    customer = raw.get("customer") if isinstance(raw.get("customer"), dict) else {}
    if customer:
        if customer.get("id") is not None:
            out["salla_customer_id"] = customer.get("id")
        for key in ("email", "first_name", "last_name"):
            if customer.get(key):
                out[key] = customer[key]
        name = str(customer.get("name") or "").strip()
        if name:
            out["name"] = name
        mobile = customer.get("mobile") or customer.get("phone")
        if mobile:
            out["mobile"] = mobile
            out["phone"] = mobile

    ship = raw.get("shipping") or raw.get("ship_to") or {}
    if isinstance(ship, dict):
        addr = ship.get("address") if isinstance(ship.get("address"), dict) else ship
        if isinstance(addr, dict):
            for key in (
                "country", "city", "district", "street", "postal_code",
                "building_number", "additional_number", "short_address",
                "short_address_code", "lat", "lng", "latitude", "longitude",
                "address", "description",
            ):
                if addr.get(key) not in (None, ""):
                    out[key] = addr[key]
        company = ship.get("company")
        if isinstance(company, dict):
            if company.get("name"):
                out["shipping_company"] = company.get("name")
            if company.get("id") is not None:
                out["shipping_company_id"] = company.get("id")
        elif company:
            out["shipping_company"] = str(company)
        ship_cost = extract_salla_money_amount(ship.get("cost") or ship.get("price"))
        if ship_cost:
            out["shipping_cost"] = ship_cost

    payment = raw.get("payment") if isinstance(raw.get("payment"), dict) else {}
    if payment:
        if payment.get("method"):
            out["payment_method"] = str(payment.get("method")).lower()
        if payment.get("status"):
            out["payment_status"] = payment.get("status")

    receiver = raw.get("receiver") if isinstance(raw.get("receiver"), dict) else {}
    if receiver and not out.get("name"):
        if receiver.get("name"):
            out["name"] = receiver.get("name")
        if receiver.get("phone"):
            out["phone"] = receiver.get("phone")

    return out


def looks_like_salla_order(raw: Dict[str, Any]) -> bool:
    if str(raw.get("source") or "").lower() == "salla":
        return True
    if raw.get("reference_id") is not None:
        return True
    amounts = raw.get("amounts")
    return isinstance(amounts, dict) and bool(amounts)


def build_salla_order_metadata(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Metadata bag to merge into ``Order.extra_metadata``."""
    utc_dt, time_stamps = parse_salla_order_datetime(raw)
    amounts = extract_salla_amounts_breakdown(raw)
    meta: Dict[str, Any] = dict(time_stamps)
    if utc_dt is not None and "created_at" not in meta:
        meta["created_at"] = utc_dt.isoformat()
    if amounts:
        meta["salla_amounts"] = amounts
    payment = raw.get("payment") if isinstance(raw.get("payment"), dict) else {}
    if payment.get("method"):
        meta["payment_method"] = str(payment.get("method")).lower()
    ship = raw.get("shipping") if isinstance(raw.get("shipping"), dict) else {}
    if ship.get("company"):
        company = ship.get("company")
        meta["shipping_method"] = (
            company.get("name") if isinstance(company, dict) else str(company)
        )
    tracking = raw.get("tracking") or raw.get("shipment") or {}
    if isinstance(tracking, dict) and tracking.get("number"):
        meta["tracking_number"] = tracking.get("number")
    return meta


def apply_salla_order_normalisation(raw: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """Apply Salla fidelity fields onto a partially normalised order dict."""
    if not looks_like_salla_order(raw):
        return base

    merged = dict(base)
    merged["total"] = extract_salla_grand_total(raw)
    merged["line_items"] = normalize_salla_line_items(
        raw.get("items") or raw.get("line_items") or base.get("line_items") or []
    )
    merged["customer_info"] = enrich_salla_customer_info(
        raw, merged.get("customer_info") or {}
    )
    if merged["customer_info"].get("name"):
        merged["customer_name"] = str(merged["customer_info"]["name"]).strip()

    salla_meta = build_salla_order_metadata(raw)
    utc_dt, _ = parse_salla_order_datetime(raw)
    if utc_dt is not None:
        merged["created_at"] = utc_dt.isoformat()
    elif salla_meta.get("created_at"):
        merged["created_at"] = salla_meta["created_at"]

    payment_method = (
        salla_meta.get("payment_method")
        or merged.get("payment_method")
        or ""
    )
    if payment_method:
        merged["payment_method"] = payment_method

    merged["salla_metadata"] = salla_meta
    return merged


__all__ = [
    "SALLA_DEFAULT_TZ",
    "apply_salla_order_normalisation",
    "build_salla_order_metadata",
    "enrich_salla_customer_info",
    "extract_salla_amounts_breakdown",
    "extract_salla_grand_total",
    "extract_salla_money_amount",
    "looks_like_salla_order",
    "normalize_salla_line_items",
    "parse_salla_order_datetime",
]
