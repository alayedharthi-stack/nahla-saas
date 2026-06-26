"""Phase 2.8 — known customer identity during native catalog checkout."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.customer_identity_resolver import (  # noqa: E402
    STATUS_VERIFIED,
    CustomerIdentitySnapshot,
)
from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: E402
    enrich_catalog_checkout_prep_and_missing,
    filter_missing_for_known_catalog_customer,
    reply_contains_forbidden_catalog_name_question,
    resolve_catalog_checkout_customer_identity,
    split_operational_full_name,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.order_flow_v2.missing_fields import compute_v2_missing_fields  # noqa: E402
from modules.ai.order_flow_v2.replies import build_catalog_order_start_reply  # noqa: E402

_LIVE_CATALOG_META = {
    "source_type": "catalog_order",
    "product_items": [
        {"product_retailer_id": "ctv068l2de", "quantity": 1, "item_price": 126},
        {"product_retailer_id": "lm70d804u8", "quantity": 1, "item_price": 193},
    ],
    "total_price": 319.0,
    "currency": "SAR",
}

_FORBIDDEN_NAME = ("وش اسمك", "وش اسمك الكامل", "ممكن تذكر اسمك", "اكتب اسمك")
_FORBIDDEN_PRODUCT = ("وش المنتج", "وش العدد", "وش الوزن")


def _customer(*, name: str = "محمد العمري") -> SimpleNamespace:
    return SimpleNamespace(
        id=99,
        name=name,
        normalized_phone="966542980511",
        extra_metadata={
            "customer_name_source": "salla_order",
            "customer_name_status": STATUS_VERIFIED,
            "customer_name_confidence": 1.0,
        },
    )


def _assert_no_forbidden_name(text: str) -> None:
    for marker in _FORBIDDEN_NAME:
        assert marker not in text, f"forbidden name prompt {marker!r} in {text!r}"


def _assert_no_forbidden_product(text: str) -> None:
    for marker in _FORBIDDEN_PRODUCT:
        assert marker not in text, f"forbidden product prompt {marker!r} in {text!r}"


class TestResolveKnownCustomerIdentity:
    def test_known_customer_name_from_customer_db(self) -> None:
        with patch(
            "core.customer_identity_resolver.read_customer_identity",
            return_value=CustomerIdentitySnapshot(
                customer_name="محمد العمري",
                customer_name_source="salla_order",
                customer_name_status=STATUS_VERIFIED,
                customer_name_confidence=1.0,
                customer_name_updated_at=None,
            ),
        ), patch(
            "core.customer_identity_resolver.can_use_name_for_operations",
            return_value=True,
        ):
            identity = resolve_catalog_checkout_customer_identity(
                customer=_customer(),
                tenant_id=33,
                phone="966542980511",
                order_prep={"line_items": [{}], "catalog_line_items_authoritative": True},
            )
        assert identity.customer_name_known is True
        assert identity.prep_patch["customer_first_name"] == "محمد"
        assert identity.prep_patch["customer_last_name"] == "العمري"
        assert identity.known_facts["customer_name"] == "محمد العمري"

    def test_known_name_from_order_prep(self) -> None:
        identity = resolve_catalog_checkout_customer_identity(
            order_prep={
                "customer_first_name": "سارة",
                "customer_last_name": "العتيبي",
                "catalog_line_items_authoritative": True,
            },
            phone="966542980511",
        )
        assert identity.customer_name_known is True
        assert "customer_first_name" not in filter_missing_for_known_catalog_customer(
            ["customer_first_name", "city"],
            known_facts=identity.known_facts,
            phone="966542980511",
        )

    def test_full_name_split_without_first_last(self) -> None:
        first, last = split_operational_full_name("فهد بن سعيد")
        assert first == "فهد"
        assert last == "بن سعيد"
        with patch(
            "core.customer_identity_resolver.read_customer_identity",
            return_value=CustomerIdentitySnapshot(
                customer_name="فهد بن سعيد",
                customer_name_source="salla_order",
                customer_name_status=STATUS_VERIFIED,
                customer_name_confidence=1.0,
                customer_name_updated_at=None,
            ),
        ), patch(
            "core.customer_identity_resolver.can_use_name_for_operations",
            return_value=True,
        ):
            identity = resolve_catalog_checkout_customer_identity(
                customer=_customer(name="فهد بن سعيد"),
                phone="966542980511",
                order_prep={},
            )
        assert identity.prep_patch["customer_first_name"] == "فهد"
        assert identity.prep_patch["customer_last_name"] == "بن سعيد"

    def test_unknown_customer_still_allows_name_prompt(self) -> None:
        identity = resolve_catalog_checkout_customer_identity(
            order_prep={"catalog_line_items_authoritative": True},
            phone="966542980511",
        )
        assert identity.customer_name_known is False
        missing = filter_missing_for_known_catalog_customer(
            ["customer_name", "city"],
            known_facts=identity.known_facts,
            phone="966542980511",
        )
        assert "customer_name" in missing

    def test_phone_known_never_in_missing(self) -> None:
        missing = filter_missing_for_known_catalog_customer(
            ["customer_phone", "phone", "city"],
            known_facts={"phone_known": True},
            phone="966542980511",
        )
        assert "customer_phone" not in missing
        assert "phone" not in missing
        assert "city" in missing


class TestComputeV2MissingFieldsWithKnownCustomer:
    def test_catalog_order_skips_name_when_customer_known(self) -> None:
        db = MagicMock()
        customer = _customer()
        db.query.return_value.filter.return_value.first.return_value = customer
        prep = {
            "line_items": [{"product_retailer_id": "x", "quantity": 1}],
            "catalog_line_items_authoritative": True,
            "order_flow_v2_trusted_price": True,
        }
        with patch(
            "core.customer_identity_resolver.read_customer_identity",
            return_value=CustomerIdentitySnapshot(
                customer_name="محمد العمري",
                customer_name_source="salla_order",
                customer_name_status=STATUS_VERIFIED,
                customer_name_confidence=1.0,
                customer_name_updated_at=None,
            ),
        ), patch(
            "core.customer_identity_resolver.can_use_name_for_operations",
            return_value=True,
        ), patch(
            "utils.phone_utils.normalize_to_e164",
            return_value="966542980511",
        ):
            missing = compute_v2_missing_fields(
                prep,
                whatsapp_phone="966542980511",
                db=db,
                tenant_id=33,
                inbound_metadata=_LIVE_CATALOG_META,
            )
        assert "customer_name" not in missing
        assert "customer_first_name" not in missing
        assert "customer_last_name" not in missing
        assert "city" in missing or "delivery_address" in missing


class TestLiveCatalogOrderReply:
    def test_live_scenario_known_customer_asks_city_not_name(self) -> None:
        prep = {
            "line_items": [
                {"title": "250 جرام عسل سمر الحجاز", "quantity": 1, "item_price": 126},
                {"title": "500 جرام عسل سمر الحجاز", "quantity": 1, "item_price": 193},
            ],
            "catalog_line_items_authoritative": True,
            "order_flow_v2_trusted_price": True,
            "order_total": 319,
            "customer_first_name": "محمد",
            "customer_last_name": "العمري",
        }
        missing = ["city"]
        reply = build_catalog_order_start_reply(
            order_prep=prep,
            brain_state={},
            missing_fields=missing,
        )
        _assert_no_forbidden_name(reply)
        _assert_no_forbidden_product(reply)
        assert "المدينة" in reply or "التوصيل" in reply or "عنوان" in reply


class TestCommerceTurnContractKnownName:
    def test_contract_sets_customer_name_known_for_catalog_order(self) -> None:
        prep = OrderPreparationState(
            catalog_line_items_authoritative=True,
            line_items=[{"product_retailer_id": "x", "quantity": 1}],
            catalog_checkout_total=319.0,
        )
        state = MerchantConversationState(order_prep=prep)
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966542980511",
            message="[طلب كتالوج من العميل]",
            intent=Intent(name="catalog_order", confidence=1.0, raw_message="x"),
            state=state,
            facts=SimpleNamespace(),
            history=[],
            profile={"inbound_metadata": _LIVE_CATALOG_META},
        )
        db = MagicMock()
        customer = _customer()
        db.query.return_value.filter.return_value.first.return_value = customer
        with patch(
            "modules.ai.brain.commerce.commerce_turn_contract._load_order_context_for_contract",
            return_value=None,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.is_current_catalog_order_submitted",
            return_value=True,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.try_catalog_order_continue_decision",
            return_value=None,
        ), patch(
            "core.customer_identity_resolver.read_customer_identity",
            return_value=CustomerIdentitySnapshot(
                customer_name="محمد العمري",
                customer_name_source="salla_order",
                customer_name_status=STATUS_VERIFIED,
                customer_name_confidence=1.0,
                customer_name_updated_at=None,
            ),
        ), patch(
            "core.customer_identity_resolver.can_use_name_for_operations",
            return_value=True,
        ), patch(
            "utils.phone_utils.normalize_to_e164",
            return_value="966542980511",
        ):
            contract = build_commerce_turn_contract(ctx, db=db)
        assert contract.known_facts.get("customer_name_known") is True
        assert "customer_name" not in contract.missing_fields
        assert "customer_first_name" not in contract.missing_fields
