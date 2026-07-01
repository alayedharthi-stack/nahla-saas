"""Regression tests — saved customer address must not be denied or re-asked."""
from __future__ import annotations

import sys
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


@pytest.fixture(autouse=True)
def _enable_order_flow_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "true")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "false")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)


def _seed_known_customer_world(
    *,
    customer_name: str = "هيثم الحارثي",
    city: str = "الطائف",
    short_code: str = "TAIF1234",
):
    db, _ = make_scenario_db()
    tenant = seed_tenant(db)
    customer = seed_customer(
        db,
        tenant.id,
        phone=DEFAULT_PHONE_E164,
        name=customer_name,
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
        city=city,
        saudi_national_address=short_code,
    )
    product = seed_product(
        db,
        tenant.id,
        title="250جرام عسل صيفي",
        external_id="honey-summer-250",
        price="139",
        meta_retailer_id="honey-summer-250",
    )
    convo = seed_conversation(db, tenant.id, customer.id)
    return db, tenant, customer, product, convo


def _catalog_meta(*, retailer_id: str = "honey-summer-250") -> dict:
    return {
        "source_type": "catalog_order",
        "product_items": [{
            "product_retailer_id": retailer_id,
            "quantity": 1,
            "item_price": 139,
            "currency": "SAR",
        }],
    }


class TestKnownCustomerAddressRegression:
    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    def test_catalog_order_uses_existing_customer_address_before_asking_city(
        self,
        mock_items,
    ) -> None:
        db, tenant, _customer, product, _convo = _seed_known_customer_world()
        mock_items.return_value = SimpleNamespace(
            line_items=[{
                "product_id": str(product.id),
                "product_name": product.title,
                "quantity": 1,
                "catalog_price": 139.0,
                "price_source": "whatsapp_catalog",
            }],
            unmatched_count=0,
        )
        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="",
            inbound_metadata=_catalog_meta(retailer_id=product.meta_retailer_id),
        )
        assert result.handled, result.reason
        assert _ASK_CITY not in result.reply
        assert _DENY_SAVED_ADDRESS not in result.reply
        assert "الطائف" in result.reply
        assert "TAIF1234" in result.reply
        assert "هل نعتمد نفس العنوان" in result.reply

    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    def test_catalog_order_does_not_deny_saved_address_when_customer_says_previous_address(
        self,
        mock_items,
    ) -> None:
        db, tenant, _customer, product, convo = _seed_known_customer_world()
        active_prep = {
            "order_flow_v2_active": True,
            "line_items": [{
                "product_id": str(product.id),
                "product_name": product.title,
                "quantity": 1,
                "catalog_price": 139.0,
            }],
            "order_flow_v2_trusted_price": True,
            "order_flow_v2_catalog_total": 139,
            "customer_first_name": "هيثم",
            "customer_last_name": "الحارثي",
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
        assert result.handled, result.reason
        assert _DENY_SAVED_ADDRESS not in result.reply
        assert _ASK_CITY not in result.reply
        patch = result.state_patch
        assert patch.get("city") == "الطائف" or patch.get("customer_confirmed_previous_address")
        assert patch.get("short_address_code") == "TAIF1234" or patch.get("google_maps_url")

    def test_order_flow_uses_known_customer_city_and_short_code(self) -> None:
        db, tenant, customer, _product, convo = _seed_known_customer_world()
        prep = {
            "line_items": [{"product_name": "منتج", "quantity": 1, "catalog_price": 100}],
            "order_flow_v2_trusted_price": True,
            "catalog_line_items_authoritative": True,
            "customer_first_name": "هيثم",
            "customer_last_name": "الحارثي",
        }
        ctx = load_checkout_reply_context(
            db,
            tenant_id=tenant.id,
            conversation=convo,
            customer_phone=DEFAULT_PHONE,
            order_prep=prep,
            brain_state={"order_prep": prep},
        )
        assert ctx.field_modes.get("city") == MODE_CONFIRM or ctx.known_previous.get("city")
        reply = build_next_field_reply(
            order_prep=prep,
            brain_state={"order_prep": prep},
            missing_fields=["city"],
            field_modes=ctx.field_modes or {"city": MODE_CONFIRM},
            known_previous=ctx.known_previous
            or {"city": "الطائف", "short_address": "TAIF1234"},
        )
        assert _ASK_CITY not in reply
        assert "الطائف" in reply
        assert "TAIF1234" in reply
        assert "هل نعتمد نفس العنوان" in reply

    def test_greeting_uses_known_customer_first_name_once(self) -> None:
        reply = build_greeting_with_pending_hint(
            has_pending=True,
            first_name="هيثم",
        )
        assert "يا هيثم" in reply
        assert reply.count("هيثم") == 1

        plain = build_greeting_with_pending_hint(has_pending=False, first_name="")
        assert "يا " not in plain
