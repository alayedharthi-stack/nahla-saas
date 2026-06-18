"""
core/order_customer_display.py
────────────────────────────────
Unified operational display name for orders + customer sync from edits.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.customer_display import is_valid_customer_display_name, looks_like_phone_personalization_name


def compose_customer_full_name(first: str, last: str, *, fallback: str = "") -> str:
    first = str(first or "").strip()
    last = str(last or "").strip()
    if not is_valid_customer_display_name(first):
        first = ""
    if not is_valid_customer_display_name(last):
        last = ""
    if first and last:
        return f"{first} {last}"
    if first:
        return first
    if last:
        return last
    fb = str(fallback or "").strip()
    return fb if is_valid_customer_display_name(fb) else ""


def order_customer_prep(order: Any) -> Dict[str, str]:
    from core.wa_order_editor import _customer_info, _meta, _split_display_name  # noqa: PLC0415

    meta = _meta(order)
    info = _customer_info(order)
    first = str(meta.get("customer_first_name") or info.get("first_name") or "").strip()
    last = str(meta.get("customer_last_name") or info.get("last_name") or "").strip()
    if not first and not last:
        split_first, split_last = _split_display_name(str(getattr(order, "customer_name", "") or ""))
        if is_valid_customer_display_name(split_first):
            first = split_first
        if is_valid_customer_display_name(split_last):
            last = split_last
    return {
        "customer_first_name": first,
        "customer_last_name": last,
    }


def resolve_order_customer_display_name(
    order: Any,
    customer_lookup: Optional[Dict[str, str]] = None,
    *,
    normalise_phone_key=None,
) -> str:
    """Priority: merchant split names → valid stored names → lookup → phone."""
    from core.wa_order_editor import _customer_info, _meta  # noqa: PLC0415

    meta = _meta(order)
    info = _customer_info(order)
    prep = order_customer_prep(order)

    composed = compose_customer_full_name(
        prep["customer_first_name"],
        prep["customer_last_name"],
    )
    if composed and is_valid_customer_display_name(composed):
        return composed

    phone = str(
        info.get("phone") or info.get("mobile") or info.get("shipping_phone") or ""
    ).strip()

    for candidate in (
        meta.get("customer_name"),
        getattr(order, "customer_name", None),
        info.get("name"),
    ):
        text = str(candidate or "").strip()
        if is_valid_customer_display_name(text):
            return text

    if customer_lookup and phone:
        looked_up = customer_lookup.get(phone)
        if normalise_phone_key:
            looked_up = looked_up or customer_lookup.get(normalise_phone_key(phone))
        if looked_up and is_valid_customer_display_name(looked_up):
            return looked_up

    if phone and not looks_like_phone_personalization_name(phone):
        return phone
    if phone:
        return phone
    return "—"


def sync_order_customer_identity(
    db: Any,
    tenant_id: int,
    order: Any,
) -> None:
    """Upsert tenant-scoped Customer row from merchant order customer edit."""
    from core.customer_identity_resolver import SOURCE_MERCHANT, apply_customer_name  # noqa: PLC0415
    from core.wa_order_editor import _customer_info, _utcnow_iso  # noqa: PLC0415
    from services.customer_intelligence import CustomerIntelligenceService, normalize_phone  # noqa: PLC0415

    info = _customer_info(order)
    phone_raw = str(info.get("phone") or info.get("mobile") or "").strip()
    if not normalize_phone(phone_raw):
        return

    prep = order_customer_prep(order)
    full_name = compose_customer_full_name(
        prep["customer_first_name"],
        prep["customer_last_name"],
    )
    if not is_valid_customer_display_name(full_name):
        return

    svc = CustomerIntelligenceService(db, tenant_id)
    customer = svc.find_customer_by_phone(phone_raw)
    if customer is None:
        customer = svc.upsert_customer_identity(
            phone=phone_raw,
            name=full_name,
            source="merchant_correction",
        )
    if customer is None:
        return

    apply_customer_name(
        customer,
        full_name,
        source=SOURCE_MERCHANT,
        force_merchant=True,
    )
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    meta["manual_name_source"] = "order_edit"
    meta["manual_name_edited_at"] = _utcnow_iso()
    customer.extra_metadata = meta
    db.add(customer)
