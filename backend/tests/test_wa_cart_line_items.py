"""Tests for PR-3 multi-item WhatsApp draft cart helpers."""
from __future__ import annotations

from core.wa_cart_line_items import (
    apply_cart_delta,
    build_line_items_from_order_prep,
    format_cart_summary_ar,
    line_item_merge_key,
    merge_line_items,
    normalize_line_item,
)
from core.wa_order_lifecycle import (
    STATUS_DRAFT,
    STATUS_PENDING_CUSTOMER_INFO,
    STATUS_PENDING_PAYMENT,
    STATUS_PAYMENT_SUBMITTED,
    compute_wa_missing_fields,
    resolve_wa_order_status,
)


def _item(name: str, variant: str = "", qty: int = 1, **extra):
    payload = {
        "product_name": name,
        "variant": variant,
        "quantity": qty,
    }
    payload.update(extra)
    return payload


def test_single_product_creates_one_line_item() -> None:
    items, title, _ = build_line_items_from_order_prep(
        order_prep={"product_name": "عسل طلح", "variant": "1kg", "quantity": 1},
        brain_state={},
    )
    assert len(items) == 1
    assert items[0]["product_name"] == "عسل طلح"
    assert items[0]["variant"] == "1kg"
    assert title == "عسل طلح"


def test_two_different_products_create_two_line_items() -> None:
    items, _, _ = build_line_items_from_order_prep(
        order_prep={
            "line_items": [
                _item("عسل طلح", "1kg"),
                _item("عسل سمر", "500g"),
            ],
        },
        brain_state={},
    )
    assert len(items) == 2
    names = {i["product_name"] for i in items}
    assert names == {"عسل طلح", "عسل سمر"}


def test_same_product_same_variant_merges_quantity() -> None:
    merged = merge_line_items([
        _item("عسل طلح", "1kg", 1),
        _item("عسل طلح", "1kg", 1),
    ])
    assert len(merged) == 1
    assert merged[0]["quantity"] == 2


def test_same_product_different_variants_stay_separate() -> None:
    merged = merge_line_items([
        _item("عسل طلح", "1kg", 1),
        _item("عسل طلح", "500g", 1),
    ])
    assert len(merged) == 2
    variants = {i["variant"] for i in merged}
    assert variants == {"1kg", "500g"}


def test_update_quantity_delta() -> None:
    cart = merge_line_items([_item("عسل طلح", "1kg", 1)])
    updated, events = apply_cart_delta(
        cart,
        {
            "op": "update_quantity",
            "match": {"product_name_contains": "طلح"},
            "quantity": 2,
        },
    )
    assert updated[0]["quantity"] == 2
    assert events[0]["event"] == "line_item_quantity_updated"
    assert events[0]["old_quantity"] == 1
    assert events[0]["new_quantity"] == 2


def test_update_variant_delta() -> None:
    cart = merge_line_items([_item("عسل طلح", "1kg", 1)])
    updated, events = apply_cart_delta(
        cart,
        {
            "op": "update_variant",
            "match": {"product_name_contains": "طلح", "variant": "1kg"},
            "variant": "500g",
        },
    )
    assert updated[0]["variant"] == "500g"
    assert events[0]["event"] == "line_item_variant_updated"


def test_ambiguous_delta_does_not_corrupt_cart() -> None:
    cart = merge_line_items([
        _item("عسل طلح", "1kg", 1),
        _item("عسل سمر", "500g", 1),
    ])
    updated, events = apply_cart_delta(
        cart,
        {"op": "update_quantity", "match": {}, "quantity": 5},
    )
    assert updated == cart
    assert events == []


def test_remove_product_delta() -> None:
    cart = merge_line_items([
        _item("عسل طلح", "1kg", 1),
        _item("عسل سمر", "500g", 1),
    ])
    updated, events = apply_cart_delta(
        cart,
        {"op": "remove", "match": {"product_name_contains": "سمر"}},
    )
    assert len(updated) == 1
    assert "سمر" not in updated[0]["product_name"]
    assert events[0]["event"] == "line_item_removed"


def test_remove_missing_product_is_safe() -> None:
    cart = merge_line_items([_item("عسل طلح", "1kg", 1)])
    updated, events = apply_cart_delta(
        cart,
        {"op": "remove", "match": {"product_name_contains": "سدر"}},
    )
    assert updated == cart
    assert events == []


def test_empty_cart_after_remove_does_not_reach_pending_payment() -> None:
    cart = merge_line_items([_item("عسل سمر", "500g", 1)])
    updated, _ = apply_cart_delta(
        cart,
        {"op": "remove", "match": {"product_name_contains": "سمر"}},
    )
    assert updated == []
    status, missing, _ = resolve_wa_order_status({}, {}, line_items=updated)
    assert status is None
    assert "product" in compute_wa_missing_fields({}, line_items=updated)


def test_line_items_without_address_is_pending_customer_info() -> None:
    items = [_item("عسل طلح", "1kg", 1)]
    status, missing, _ = resolve_wa_order_status(
        {
            "customer_first_name": "سارة",
            "customer_last_name": "أحمد",
            "city": "الرياض",
        },
        {},
        line_items=items,
    )
    assert status == STATUS_PENDING_CUSTOMER_INFO
    assert "delivery_address" in missing
    assert "customer_phone" not in missing


def test_line_items_with_maps_can_reach_pending_payment() -> None:
    items = [_item("عسل طلح", "1kg", 1)]
    status, missing, _ = resolve_wa_order_status(
        {
            "customer_first_name": "سارة",
            "customer_last_name": "أحمد",
            "city": "الرياض",
            "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
        },
        {},
        line_items=items,
    )
    assert status == STATUS_PENDING_PAYMENT
    assert missing == []


def test_whatsapp_phone_never_in_missing_fields() -> None:
    missing = compute_wa_missing_fields(
        {"customer_first_name": "x", "customer_last_name": "y", "city": "z"},
        whatsapp_phone="966551234567",
        line_items=[_item("عسل طلح", "1kg", 1)],
    )
    assert "customer_phone" not in missing


def test_payment_submission_after_multi_item_order() -> None:
    items = [
        _item("عسل طلح", "1kg", 2),
        _item("عسل سمر", "500g", 1),
    ]
    status, _, _ = resolve_wa_order_status(
        {
            "payment_receipt_received": True,
            "customer_first_name": "سارة",
            "customer_last_name": "أحمد",
            "city": "الرياض",
            "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
        },
        {},
        line_items=items,
    )
    assert status == STATUS_PAYMENT_SUBMITTED


def test_merge_key_uses_product_id_and_variant() -> None:
    a = normalize_line_item(_item("A", "1kg", product_id="p1", variant_id="v1"))
    b = normalize_line_item(_item("B", "1kg", product_id="p1", variant_id="v1"))
    assert line_item_merge_key(a) == line_item_merge_key(b)


def test_existing_order_line_items_preserved_when_adding_focus() -> None:
    existing = [_item("عسل طلح", "1kg", 1, product_id="p-talh")]
    items, _, _ = build_line_items_from_order_prep(
        order_prep={"product_id": "p-samr", "product_name": "عسل سمر", "variant": "500g", "quantity": 1},
        brain_state={"current_product_focus": {"id": "p-samr", "title": "عسل سمر", "variant": "500g"}},
        existing_line_items=existing,
    )
    assert len(items) == 2
    names = {i["product_name"] for i in items}
    assert names == {"عسل طلح", "عسل سمر"}


def test_cart_deltas_applied_in_order() -> None:
    items, _, events = build_line_items_from_order_prep(
        order_prep={
            "line_items": [_item("عسل طلح", "1kg", 1)],
            "cart_deltas": [
                {"op": "add", "item": _item("عسل سمر", "500g", 1)},
                {"op": "update_quantity", "match": {"product_name_contains": "طلح"}, "quantity": 2},
            ],
        },
        brain_state={},
    )
    assert len(items) == 2
    talh = next(i for i in items if "طلح" in i["product_name"])
    assert talh["quantity"] == 2
    assert any(e["event"] == "line_item_added" for e in events)


def test_format_cart_summary_ar() -> None:
    summary = format_cart_summary_ar([
        _item("عسل طلح نجد", "1kg", 2),
        _item("عسل سمر الحجاز", "500g", 1),
    ])
    assert "طلبك الحالي:" in summary
    assert "طلح" in summary
    assert "الكمية 2" in summary


def test_empty_cart_existing_order_resolves_to_draft() -> None:
    status, missing, _ = resolve_wa_order_status(
        {"customer_first_name": "x", "customer_last_name": "y", "city": "z"},
        {},
        line_items=[],
    )
    assert status is None
    # Bridge promotes existing-order empty cart to draft explicitly.
    assert STATUS_DRAFT == "draft"
    assert "product" in compute_wa_missing_fields({}, line_items=[])
