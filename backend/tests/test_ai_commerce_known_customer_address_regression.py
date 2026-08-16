"""
Regression tests — saved customer address must not be denied or re-asked.

Policy: platform-wide, merchant-agnostic commerce data only. See AGENTS.md
「Generic Commerce Regression Tests」. Do not bind these tests to honey,
sidr, Al Ayed, or production-store product names.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.customer_identity_resolver import SOURCE_MERCHANT, STATUS_CUSTOMER_ENTERED  # noqa: E402
from core.order_context_prefill import MODE_CONFIRM  # noqa: E402
from modules.ai.order_flow_v2.checkout_context import (  # noqa: E402
    load_checkout_reply_context,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.replies import (  # noqa: E402
    build_greeting_with_pending_hint,
    build_next_field_reply,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE,
    DEFAULT_PHONE_E164,
    attach_brain_state,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_customer_address,
    seed_product,
    seed_tenant,
)

_DENY_SAVED_ADDRESS = "ما عندي العنوان السابق"
_ASK_CITY = "وش المدينة؟"
_HONEY_MARKERS = ("عسل", "سدر", "آل عايد", "العايد", "sidr", "honey")


@dataclass(frozen=True)
class CommerceScenario:
    tenant_name: str
    customer_name: str
    customer_first_name: str
    customer_last_name: str
    city: str
    short_code: str
    product_title: str
    product_external_id: str
    product_price: str
    catalog_price: float


GENERIC_SHOES = CommerceScenario(
    tenant_name="متجر تجريبي عام",
    customer_name="أحمد سالم",
    customer_first_name="أحمد",
    customer_last_name="سالم",
    city="الرياض",
    short_code="RRRD1234",
    product_title="حذاء رياضي أبيض مقاس 42",
    product_external_id="shoe-runner-white-42",
    product_price="199",
    catalog_price=199.0,
)

GENERIC_CLOTHING = CommerceScenario(
    tenant_name="متجر ملابس تجريبي",
    customer_name="نورة عبدالله",
    customer_first_name="نورة",
    customer_last_name="عبدالله",
    city="جدة",
    short_code="JEDA5678",
    product_title="قميص قطني أزرق L",
    product_external_id="shirt-cotton-blue-l",
    product_price="89",
    catalog_price=89.0,
)


@pytest.fixture(autouse=True)
def _enable_order_flow_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "true")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "false")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)


def _seed_known_customer_world(scenario: CommerceScenario = GENERIC_SHOES):
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name=scenario.tenant_name)
    customer = seed_customer(
        db,
        tenant.id,
        phone=DEFAULT_PHONE_E164,
        name=scenario.customer_name,
        extra_metadata={
            "customer_name_source": SOURCE_MERCHANT,
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
            "customer_name_confidence": 0.95,
        },
    )
    seed_customer_address(
        db,
        tenant.id,
        customer.id,
        city=scenario.city,
        saudi_national_address=scenario.short_code,
    )
    product = seed_product(
        db,
        tenant.id,
        title=scenario.product_title,
        external_id=scenario.product_external_id,
        price=scenario.product_price,
        meta_retailer_id=scenario.product_external_id,
    )
    convo = seed_conversation(db, tenant.id, customer.id)
    return db, tenant, customer, product, convo, scenario


def _catalog_meta(*, retailer_id: str, price: float) -> dict:
    return {
        "source_type": "catalog_order",
        "product_items": [{
            "product_retailer_id": retailer_id,
            "quantity": 1,
            "item_price": price,
            "currency": "SAR",
        }],
    }


def _assert_address_confirm_reply(
    reply: str,
    *,
    scenario: CommerceScenario,
    forbidden_markers: tuple[str, ...] = _HONEY_MARKERS,
) -> None:
    assert _ASK_CITY not in reply
    assert _DENY_SAVED_ADDRESS not in reply
    assert scenario.city in reply
    assert scenario.short_code in reply
    lowered = reply.lower()
    for marker in forbidden_markers:
        assert marker.lower() not in lowered


class TestKnownCustomerAddressRegression:
    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    def test_catalog_order_uses_existing_customer_address_before_asking_city(
        self,
        mock_items,
    ) -> None:
        db, tenant, _customer, product, _convo, scenario = _seed_known_customer_world(
            GENERIC_SHOES,
        )
        mock_items.return_value = SimpleNamespace(
            line_items=[{
                "product_id": str(product.id),
                "product_name": product.title,
                "quantity": 1,
                "catalog_price": scenario.catalog_price,
                "price_source": "whatsapp_catalog",
            }],
            unmatched_count=0,
        )
        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="",
            inbound_metadata=_catalog_meta(
                retailer_id=product.meta_retailer_id,
                price=scenario.catalog_price,
            ),
        )
        assert result.handled, result.reason
        _assert_address_confirm_reply(result.reply, scenario=scenario)
        assert scenario.product_title.split()[0] in result.reply or str(
            int(scenario.catalog_price),
        ) in result.reply

    def test_catalog_order_does_not_deny_saved_address_when_customer_says_previous_address(
        self,
    ) -> None:
        db, tenant, _customer, product, convo, scenario = _seed_known_customer_world(
            GENERIC_CLOTHING,
        )
        active_prep = {
            "order_flow_v2_active": True,
            "line_items": [{
                "product_id": str(product.id),
                "product_name": product.title,
                "quantity": 1,
                "catalog_price": scenario.catalog_price,
            }],
            "order_flow_v2_trusted_price": True,
            "order_flow_v2_catalog_total": scenario.catalog_price,
            "customer_first_name": scenario.customer_first_name,
            "customer_last_name": scenario.customer_last_name,
        }
        attach_brain_state(convo, active_prep)
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="عنواني السابق عندكم",
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"

    def test_order_flow_uses_known_customer_city_and_short_code(self) -> None:
        db, tenant, _customer, _product, convo, scenario = _seed_known_customer_world(
            GENERIC_SHOES,
        )
        prep = {
            "line_items": [{
                "product_name": scenario.product_title,
                "quantity": 1,
                "catalog_price": scenario.catalog_price,
            }],
            "order_flow_v2_trusted_price": True,
            "catalog_line_items_authoritative": True,
            "customer_first_name": scenario.customer_first_name,
            "customer_last_name": scenario.customer_last_name,
        }
        ctx = load_checkout_reply_context(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            customer_phone=DEFAULT_PHONE,
            order_prep=prep,
            brain_state={"order_prep": prep},
        )
        assert ctx.known_previous.get("city") == scenario.city
        assert ctx.known_previous.get("short_address") == scenario.short_code
        assert ctx.field_modes.get("city") == MODE_CONFIRM
        reply = build_next_field_reply(
            order_prep=prep,
            brain_state={"order_prep": prep},
            missing_fields=["city"],
            field_modes=ctx.field_modes,
            known_previous=ctx.known_previous,
        )
        _assert_address_confirm_reply(reply, scenario=scenario)

    def test_greeting_uses_known_customer_first_name_once(self) -> None:
        reply = build_greeting_with_pending_hint(
            has_pending=True,
            first_name=GENERIC_SHOES.customer_first_name,
        )
        assert f"يا {GENERIC_SHOES.customer_first_name}" in reply
        assert reply.count(GENERIC_SHOES.customer_first_name) == 1

        plain = build_greeting_with_pending_hint(has_pending=False, first_name="")
        assert "يا " not in plain

    @pytest.mark.parametrize(
        "scenario",
        [GENERIC_SHOES, GENERIC_CLOTHING],
        ids=["generic_shoes", "generic_clothing"],
    )
    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    def test_known_customer_address_grounding_is_generic_across_product_categories(
        self,
        mock_items,
        scenario: CommerceScenario,
    ) -> None:
        """Saved-address confirm must follow CustomerAddress state, not store category."""
        db, tenant, _customer, product, _convo, scenario = _seed_known_customer_world(
            scenario,
        )
        mock_items.return_value = SimpleNamespace(
            line_items=[{
                "product_id": str(product.id),
                "product_name": product.title,
                "quantity": 1,
                "catalog_price": scenario.catalog_price,
                "price_source": "whatsapp_catalog",
            }],
            unmatched_count=0,
        )
        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="",
            inbound_metadata=_catalog_meta(
                retailer_id=product.meta_retailer_id,
                price=scenario.catalog_price,
            ),
        )
        assert result.handled, scenario.tenant_name
        _assert_address_confirm_reply(
            result.reply,
            scenario=scenario,
            forbidden_markers=_HONEY_MARKERS,
        )
        assert tenant.name == scenario.tenant_name
        for marker in _HONEY_MARKERS:
            assert marker.lower() not in scenario.product_title.lower()

    def test_previous_address_phrase_does_not_invent_address_without_customer_address_row(
        self,
    ) -> None:
        """Phrase is only a trigger; without CustomerAddress row no city is applied."""
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name=GENERIC_SHOES.tenant_name)
        customer = seed_customer(
            db,
            tenant.id,
            phone=DEFAULT_PHONE_E164,
            name=GENERIC_SHOES.customer_name,
            extra_metadata={
                "customer_name_source": SOURCE_MERCHANT,
                "customer_name_status": STATUS_CUSTOMER_ENTERED,
                "customer_name_confidence": 0.95,
            },
        )
        product = seed_product(
            db,
            tenant.id,
            title=GENERIC_SHOES.product_title,
            external_id=GENERIC_SHOES.product_external_id,
            price=GENERIC_SHOES.product_price,
            meta_retailer_id=GENERIC_SHOES.product_external_id,
        )
        convo = seed_conversation(db, tenant.id, customer.id)
        active_prep = {
            "order_flow_v2_active": True,
            "line_items": [{
                "product_id": str(product.id),
                "product_name": product.title,
                "quantity": 1,
                "catalog_price": GENERIC_SHOES.catalog_price,
            }],
            "order_flow_v2_trusted_price": True,
            "customer_first_name": GENERIC_SHOES.customer_first_name,
            "customer_last_name": GENERIC_SHOES.customer_last_name,
        }
        attach_brain_state(convo, active_prep)
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="عنواني السابق عندكم",
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"
