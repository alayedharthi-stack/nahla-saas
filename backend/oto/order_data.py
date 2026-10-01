"""Map verified Nahlah orders to OTO without guessing missing carrier data."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def oto_order_id(tenant_id: int, order_id: int) -> str:
    return f"nahlah-{tenant_id}-{order_id}"


def parse_oto_order_id(value: str) -> tuple[int, int] | None:
    bits = str(value).split("-")
    if len(bits) != 3 or bits[0] != "nahlah" or not all(x.isdecimal() for x in bits[1:]):
        return None
    tenant_id, order_id = int(bits[1]), int(bits[2])
    return (tenant_id, order_id) if tenant_id > 0 and order_id > 0 else None


def _amount(value: Any) -> float:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError("oto_amount_missing") from None
    if not number.is_finite() or number < 0:
        raise ValueError("oto_amount_invalid")
    return float(number)


def build_order_payload(order: Any, *, tenant_id: int, pickup_code: str,
                        weight_kg: float, width_cm: float, length_cm: float,
                        height_cm: float) -> dict[str, Any]:
    from core.order_shipping_policy import order_has_accepted_address

    if not order_has_accepted_address(order):
        raise ValueError("oto_address_not_confirmed")
    customer = order.customer_info if isinstance(order.customer_info, dict) else {}
    meta = order.extra_metadata if isinstance(order.extra_metadata, dict) else {}
    address = (customer.get("address") or customer.get("address_text") or
               meta.get("address_line") or meta.get("delivery_address_text") or
               customer.get("short_address_code") or meta.get("short_address_code"))
    city = customer.get("city") or meta.get("city") or meta.get("delivery_city")
    name = order.customer_name or customer.get("name")
    mobile = customer.get("mobile") or customer.get("phone")
    if not all(str(x or "").strip() for x in (address, city, name, mobile)):
        raise ValueError("oto_recipient_details_incomplete")
    if str(customer.get("country") or "SA").upper() != "SA":
        raise ValueError("oto_country_unsupported")
    if not pickup_code:
        raise ValueError("oto_pickup_missing")
    dimensions = (weight_kg, width_cm, length_cm, height_cm)
    if any(not isinstance(x, (float, int)) or x <= 0 for x in dimensions):
        raise ValueError("oto_package_details_invalid")
    amount = _amount(meta.get("amount_value"))
    payment_method = "cod" if meta.get("payment_method") == "cash_on_delivery" else "paid"
    items = []
    for index, row in enumerate(order.line_items or [], 1):
        if not isinstance(row, dict):
            raise ValueError("oto_items_invalid")
        item_name = row.get("catalog_product_name") or row.get("name") or row.get("product_name")
        unit_price = row.get("unit_price") if row.get("unit_price") is not None else row.get("price")
        quantity = row.get("quantity")
        if not item_name or not quantity:
            raise ValueError("oto_items_incomplete")
        items.append({
            "name": str(item_name),
            "price": _amount(unit_price),
            "quantity": _amount(quantity),
            "sku": str(row.get("sku") or f"nahlah-{order.id}-{index}"),
        })
    if not items:
        raise ValueError("oto_items_missing")
    recipient = {
        "name": str(name), "mobile": str(mobile), "address": str(address),
        "city": str(city), "country": "SA",
    }
    for key in ("district", "postcode", "email", "short_address_code"):
        value = customer.get(key) or meta.get(key)
        if value:
            recipient["shortAddressCode" if key == "short_address_code" else key] = str(value)
    for source, dest in (("latitude", "lat"), ("longitude", "lon")):
        value = customer.get(source) or meta.get(source)
        if value is not None:
            recipient[dest] = str(value)
    return {
        "orderId": oto_order_id(tenant_id, order.id),
        "pickupLocationCode": pickup_code,
        "createShipment": False,
        "payment_method": payment_method,
        "amount": amount,
        "amount_due": amount if payment_method == "cod" else 0,
        "currency": "SAR",
        "packageCount": 1,
        "packageWeight": weight_kg,
        "boxWidth": width_cm,
        "boxLength": length_cm,
        "boxHeight": height_cm,
        "customer": recipient,
        "items": items,
    }
