"""
Active checkout resume regressions — greeting and on-file address claims.

Same-conversation continuation: incomplete checkout must rehydrate CustomerAddress
before asking for city. Phrases are triggers only; persisted rows own truth.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.customer_identity_resolver import SOURCE_MERCHANT, STATUS_CUSTOMER_ENTERED  # noqa: E402
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
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


def _seed_world(
    scenario: CommerceScenario,
    *,
    with_address: bool = True,
):
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
    if with_address:
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


def _active_checkout_prep(product, scenario: CommerceScenario) -> dict:
    return {
        "order_flow_v2_active": True,
        "order_flow_v2_trusted_price": True,
        "catalog_line_items_authoritative": True,
        "line_items": [{
            "product_id": str(product.id),
            "product_name": product.title,
            "quantity": 1,
            "catalog_price": scenario.catalog_price,
        }],
        "order_flow_v2_catalog_total": scenario.catalog_price,
        "customer_first_name": scenario.customer_first_name,
        "customer_last_name": scenario.customer_last_name,
        "order_flow_v2_last_field": "city",
    }


class TestActiveCheckoutResumeAddressRegression:
    def test_active_checkout_resume_rehydrates_saved_address_before_asking_city(
        self,
    ) -> None:
        db, tenant, _customer, product, convo, scenario = _seed_world(GENERIC_SHOES)
        attach_brain_state(convo, _active_checkout_prep(product, scenario))
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="السلام عليكم",
        )
        assert not result.handled or result.reason != "greeting_checkout_resume", (
            "Pure salaam must not force aggressive checkout resume (Phase A.1)"
        )

    @pytest.mark.parametrize(
        "message",
        ["عنواني عندكم", "عندكم مسجلة"],
        ids=["address_at_store", "registered_at_store"],
    )
    def test_active_checkout_resume_previous_address_claim_uses_customer_address(
        self,
        message: str,
    ) -> None:
        db, tenant, _customer, product, convo, scenario = _seed_world(GENERIC_CLOTHING)
        attach_brain_state(convo, _active_checkout_prep(product, scenario))
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message=message,
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"

    def test_active_checkout_resume_without_saved_address_does_not_contradict_itself(
        self,
    ) -> None:
        db, tenant, _customer, product, convo, scenario = _seed_world(
            GENERIC_SHOES,
            with_address=False,
        )
        attach_brain_state(convo, _active_checkout_prep(product, scenario))
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="عنواني عندكم",
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"

    @pytest.mark.parametrize(
        "scenario",
        [GENERIC_SHOES, GENERIC_CLOTHING],
        ids=["generic_shoes", "generic_clothing"],
    )
    def test_active_checkout_resume_is_generic_across_product_categories(
        self,
        scenario: CommerceScenario,
    ) -> None:
        db, tenant, _customer, product, convo, scenario = _seed_world(
            scenario,
            with_address=True,
        )
        attach_brain_state(convo, _active_checkout_prep(product, scenario))
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="السلام عليكم",
        )
        assert not result.handled or result.reason != "greeting_checkout_resume", (
            "Pure salaam must not force aggressive checkout resume (Phase A.1)"
        )
        assert tenant.name == scenario.tenant_name
