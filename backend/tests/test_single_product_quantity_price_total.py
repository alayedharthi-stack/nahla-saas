"""
Round-2 regressions — single-product quantity, unit price, total, and options.

Before/after proof (baseline → fixed):
| Defect                         | Before              | After               |
|--------------------------------|---------------------|---------------------|
| quantity extraction            | None                | 2 / 3 / 2 / 2 / 2 / 2 |
| personal-slot pollution        | customer_first_name | no personal slots   |
| mid-funnel quantity apply      | 1 → 1               | 1 → 2 (persists)    |
| unit price in order_prep       | missing             | 129 from catalog    |
| order total (qty=2 @ 129)      | 129                 | 258                 |
| options from local metadata    | adapter-only        | metadata['options'] |
| option value price             | base price          | option price        |
| non-existent option            | silently ignored    | unmatched_option_attempt |
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.wa_cart_line_items import build_line_items_from_order_prep, cart_total_amount  # noqa: E402
from modules.ai.brain.execution.orders import (  # noqa: E402
    DraftOrderHandler,
    _apply_quantity_from_message,
    _ensure_product_options_loaded,
    _merge_message_options,
    _order_prep_export_dict,
    _sync_single_product_line_item,
    _trusted_catalog_unit_price,
)
from modules.ai.brain.intent.ordering_extractor import (  # noqa: E402
    extract_ordering_quantity,
    extract_ordering_slots,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    Decision,
    MerchantConversationState,
    OrderPreparationState,
)
from models import Product  # noqa: E402
from services.nahla_order_bridge import _resolve_order_amount  # noqa: E402
from tests.commerce_scenario_fixtures import make_scenario_db, seed_product, seed_tenant  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"
SHIRT_TITLE = "قميص قطني أزرق"
SHIRT_EXTERNAL = "ext-shirt-blue"
SHIRT_UNIT = 129.0
OPTION_GROUP = {
    "id": 101,
    "name": "المقاس",
    "required": True,
    "type": "radio",
    "values": [
        {"id": 1001, "name": "S", "price": 129, "image_url": ""},
        {"id": 1002, "name": "L", "price": 129, "image_url": ""},
        {"id": 1003, "name": "M", "price": 149, "image_url": ""},
    ],
}

QUANTITY_PHRASES = [
    ("كميتين", 2),
    ("حبتين", 2),
    ("قطعتين", 2),
    ("ثلاث حبات", 3),
    ("2", 2),
    ("٢", 2),
]


def _shirt_product_info() -> Dict[str, Any]:
    return {
        "title": SHIRT_TITLE,
        "external_id": SHIRT_EXTERNAL,
        "id": 1,
        "price": SHIRT_UNIT,
        "can_checkout": True,
    }


def _seed_shirt_with_options(db, tenant_id: int) -> Product:
    return seed_product(
        db,
        tenant_id,
        title=SHIRT_TITLE,
        external_id=SHIRT_EXTERNAL,
        price=str(int(SHIRT_UNIT)),
        meta_retailer_id=SHIRT_EXTERNAL,
    )


def _set_product_options(db, product: Product) -> None:
    product.extra_metadata = {"options": [OPTION_GROUP]}
    db.add(product)
    db.commit()
    db.refresh(product)


def _ctx(
    db,
    tenant_id: int,
    *,
    message: str,
    prep: OrderPreparationState | None = None,
) -> BrainContext:
    state = MerchantConversationState(
        stage="ordering",
        greeted=True,
        order_prep=prep,
        current_product_focus=_shirt_product_info(),
    )
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=message,
        intent=SimpleNamespace(name="general", confidence=0.9, slots={}, raw_message=message),
        state=state,
        facts=SimpleNamespace(has_products=True, orderable=True, store_name=GENERIC_MERCHANT),
    )
    ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _decision(**extra) -> Decision:
    base = {
        "action": "propose_draft_order",
        "args": {"product": _shirt_product_info()},
        "reason": "test",
    }
    base["args"].update(extra.get("args") or {})
    return Decision(action=base["action"], args=base["args"], reason=base["reason"])


class TestQuantityExtractionBaseline:
    @pytest.mark.parametrize("phrase,expected", QUANTITY_PHRASES)
    def test_quantity_lands_in_slots_not_personal_fields(self, phrase: str, expected: int) -> None:
        slots = extract_ordering_slots(phrase)
        assert slots.get("quantity") == expected
        assert "customer_first_name" not in slots
        assert "customer_last_name" not in slots
        assert "customer_name" not in slots

    @pytest.mark.parametrize("phrase,_expected", QUANTITY_PHRASES)
    def test_extract_ordering_quantity_structural(self, phrase: str, _expected: int) -> None:
        assert extract_ordering_quantity(phrase) == _expected


class TestMidFunnelQuantityPersistence:
    def test_quantity_applies_before_needs_collection_and_persists(self) -> None:
        prep = OrderPreparationState(
            product_id=SHIRT_EXTERNAL,
            quantity=1,
            line_items=[{
                "product_id": SHIRT_EXTERNAL,
                "product_name": SHIRT_TITLE,
                "quantity": 1,
                "unit_price": SHIRT_UNIT,
            }],
        )
        assert _apply_quantity_from_message(prep, "حبتين") is True
        assert prep.quantity == 2
        assert prep.line_items[0]["quantity"] == 2
        assert prep.line_items[0]["product_id"] == SHIRT_EXTERNAL

    def test_product_unchanged_when_quantity_changes(self) -> None:
        prep = OrderPreparationState(
            product_id=SHIRT_EXTERNAL,
            quantity=1,
            line_items=[{"product_id": SHIRT_EXTERNAL, "product_name": SHIRT_TITLE, "quantity": 1}],
        )
        _apply_quantity_from_message(prep, "2")
        assert prep.product_id == SHIRT_EXTERNAL
        assert prep.line_items[0]["product_name"] == SHIRT_TITLE


class TestUnitPriceAndTotal:
    def test_unit_price_from_catalog_and_total_is_unit_times_qty(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        product = _seed_shirt_with_options(db, tenant.id)

        prep = OrderPreparationState(product_id=SHIRT_EXTERNAL, quantity=2)
        ctx = _ctx(db, tenant.id, message="2", prep=prep)
        _sync_single_product_line_item(prep, _shirt_product_info(), ctx)

        unit = _trusted_catalog_unit_price(prep, _shirt_product_info(), ctx)
        assert unit == SHIRT_UNIT
        assert prep.line_items[0]["unit_price"] == SHIRT_UNIT

        exported = _order_prep_export_dict(prep)
        assert exported["price"] == SHIRT_UNIT
        assert exported["total_price"] == 258.0

        items, _, _ = build_line_items_from_order_prep(
            order_prep=exported,
            brain_state={"current_product_focus": _shirt_product_info()},
        )
        assert cart_total_amount(items) == 258.0

        amt, needs_review, source = _resolve_order_amount(
            db=db,
            tenant_id=tenant.id,
            conversation_id=1,
            order_prep=exported,
            brain_state={"current_product_focus": _shirt_product_info()},
            receipt_metadata={},
            line_items=items,
            is_paid_path=False,
        )
        assert amt == 258.0
        assert needs_review is False
        assert source in {"line_items", "order_prep_total_price"}

    def test_worked_example_product_size_l_qty2_total_258(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        product = _seed_shirt_with_options(db, tenant.id)
        _set_product_options(db, product)

        prep = OrderPreparationState(product_id=SHIRT_EXTERNAL, quantity=2)
        ctx = _ctx(db, tenant.id, message="L", prep=prep)

        async def _load() -> None:
            with patch("store_integration.registry.get_adapter", return_value=None):
                await _ensure_product_options_loaded(prep, ctx, SHIRT_EXTERNAL)

        asyncio.run(_load())
        assert prep.product_options_loaded is True
        assert len(prep.product_options_meta) == 1

        _merge_message_options(prep, "L")
        assert prep.product_options.get("المقاس".lower(), {}).get("value_name") == "L"

        _sync_single_product_line_item(prep, _shirt_product_info(), ctx)
        exported = _order_prep_export_dict(prep)

        assert exported["quantity"] == 2
        assert exported["price"] == SHIRT_UNIT
        assert exported["total_price"] == 258.0
        assert prep.line_items[0]["variant"] == "L"


class TestOptionsFromLocalMetadata:
    def test_local_metadata_options_without_adapter(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        product = _seed_shirt_with_options(db, tenant.id)
        _set_product_options(db, product)

        prep = OrderPreparationState(product_id=SHIRT_EXTERNAL)
        ctx = _ctx(db, tenant.id, message="", prep=prep)

        async def _load() -> None:
            with patch(
                "store_integration.registry.get_adapter",
                return_value=None,
            ):
                await _ensure_product_options_loaded(prep, ctx, SHIRT_EXTERNAL)

        asyncio.run(_load())
        assert prep.product_options_loaded is True
        assert prep.product_options_meta[0]["name"] == "المقاس"
        assert len(prep.product_options_meta[0]["values"]) == 3

    def test_option_value_price_updates_unit_price(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        product = _seed_shirt_with_options(db, tenant.id)
        _set_product_options(db, product)

        prep = OrderPreparationState(product_id=SHIRT_EXTERNAL, quantity=1)
        ctx = _ctx(db, tenant.id, message="L", prep=prep)

        async def _load() -> None:
            with patch("store_integration.registry.get_adapter", return_value=None):
                await _ensure_product_options_loaded(prep, ctx, SHIRT_EXTERNAL)

        asyncio.run(_load())
        _merge_message_options(prep, "M")
        _sync_single_product_line_item(prep, _shirt_product_info(), ctx)

        assert _trusted_catalog_unit_price(prep, _shirt_product_info(), ctx) == 149.0
        assert _order_prep_export_dict(prep)["price"] == 149.0

    def test_nonexistent_option_not_silently_accepted(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        product = _seed_shirt_with_options(db, tenant.id)
        _set_product_options(db, product)

        prep = OrderPreparationState(product_id=SHIRT_EXTERNAL)
        ctx = _ctx(db, tenant.id, message="وردي", prep=prep)

        async def _run() -> ActionResult:
            with patch(
                "modules.ai.brain.execution.orders._missing_checkout_fields",
                return_value=[],
            ), patch(
                "modules.ai.brain.execution.orders._resolve_checkout_address",
                new=AsyncMock(),
            ), patch(
                "modules.ai.brain.execution.orders.spl_resolution_available",
                return_value=False,
            ), patch(
                "modules.ai.brain.execution.orders.predict_missing_options",
                new=AsyncMock(return_value=None),
            ), patch(
                "store_integration.registry.get_adapter",
                return_value=None,
            ):
                return await DraftOrderHandler().handle(_decision(), ctx)

        from modules.ai.brain.types import ActionResult  # noqa: PLC0415

        result = asyncio.run(_run())
        assert result.data.get("needs_options") is True
        assert result.data.get("unmatched_option_attempt") is True
        assert not prep.product_options


class TestDraftOrderHandlerNoDuplicate:
    def test_needs_collection_does_not_create_duplicate_order(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        _seed_shirt_with_options(db, tenant.id)

        prep = OrderPreparationState(product_id=SHIRT_EXTERNAL, quantity=1)
        ctx = _ctx(db, tenant.id, message="حبتين", prep=prep)

        create_calls: List[Tuple] = []

        async def _run():
            with patch(
                "modules.ai.brain.execution.orders._missing_checkout_fields",
                return_value=["customer_first_name"],
            ), patch(
                "modules.ai.brain.execution.orders._resolve_checkout_address",
                new=AsyncMock(),
            ), patch(
                "modules.ai.brain.execution.orders.spl_resolution_available",
                return_value=False,
            ), patch(
                "modules.ai.commerce.runtime.CommerceToolRuntime",
            ) as mock_runtime_cls:
                mock_runtime_cls.return_value.create_draft_order = AsyncMock(
                    side_effect=lambda *a, **k: create_calls.append((a, k)) or {"order_id": "x"}
                )
                with patch("store_integration.registry.get_adapter", return_value=None):
                    return await DraftOrderHandler().handle(_decision(), ctx)

        result = asyncio.run(_run())
        assert result.data.get("needs_collection") is True
        assert result.data["order_prep"]["quantity"] == 2
        assert create_calls == []
