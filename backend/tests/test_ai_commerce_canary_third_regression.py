"""
Third canary regression — checkout address, order-number truth, product keywords, dedup.

Platform-wide generic commerce only. See AGENTS.md 「Generic Commerce Regression Tests」.
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
from core.inbound_dedup import is_duplicate_inbound  # noqa: E402
from core.order_creation_evidence import resolve_track_order_fallback  # noqa: E402
from core.wa_draft_confirmation import compose_wa_order_flow_reply  # noqa: E402
from models import Conversation  # noqa: E402
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from services.nahla_order_bridge import nahla_wa_external_id  # noqa: E402
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE,
    DEFAULT_PHONE_E164,
    attach_brain_state,
    make_scenario_db,
    seed_customer,
    seed_customer_address,
    seed_order,
    seed_product,
    seed_tenant,
)

_SOCIAL_GREETING = "حياك الله"
_ASK_CITY = "وش المدينة؟"
_REGISTERED_ORDER_CLAIM = "سجلت لك الطلب"
_HONEST_PREP = "اختياراتك محفوظة"
_NO_ORDERS = "لم أجد أي طلبات مسجّلة"
_NO_NUMBER_YET = "لسه ما صدر رقم طلب"


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
    product_keyword: str


GENERIC_SHOES = CommerceScenario(
    tenant_name="متجر أحذية تجريبي",
    customer_name="أحمد سالم",
    customer_first_name="أحمد",
    customer_last_name="سالم",
    city="الرياض",
    short_code="RRRD1234",
    product_title="حذاء رياضي أبيض مقاس 42",
    product_external_id="shoe-runner-white-42",
    product_price="199",
    catalog_price=199.0,
    product_keyword="حذاء",
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
    product_keyword="قميص",
)

GENERIC_PERFUME = CommerceScenario(
    tenant_name="متجر عطور تجريبي",
    customer_name="سارة محمد",
    customer_first_name="سارة",
    customer_last_name="محمد",
    city="الدمام",
    short_code="DMMD4321",
    product_title="عطر floral 50ml",
    product_external_id="perfume-floral-50",
    product_price="149",
    catalog_price=149.0,
    product_keyword="عطر",
)


@pytest.fixture(autouse=True)
def _enable_order_flow_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "true")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "false")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)


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


def _seed_address_world(
    scenario: CommerceScenario,
    *,
    phone: str = DEFAULT_PHONE_E164,
    link_customer_on_conversation: bool = True,
):
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name=scenario.tenant_name)
    customer = seed_customer(
        db,
        tenant.id,
        phone=phone,
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
    if link_customer_on_conversation:
        from tests.commerce_scenario_fixtures import seed_conversation  # noqa: PLC0415

        convo = seed_conversation(db, tenant.id, customer.id)
    else:
        convo = Conversation(
            tenant_id=tenant.id,
            customer_id=None,
            status="open",
            extra_metadata={"customer_phone": phone.lstrip("+")},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)
    return db, tenant, customer, product, convo, scenario


def _active_catalog_prep(scenario: CommerceScenario, product) -> dict:
    return {
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


class TestAddressRuntimeGrounding:
    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    def test_real_catalog_order_path_rehydrates_customer_address_before_city_prompt(
        self,
        mock_items,
    ) -> None:
        db, tenant, _customer, product, _convo, scenario = _seed_address_world(
            GENERIC_SHOES,
            link_customer_on_conversation=False,
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
        assert _ASK_CITY not in result.reply
        assert scenario.city in result.reply
        assert scenario.short_code in result.reply

    def test_catalog_checkout_on_file_address_claim_uses_customer_address_in_runtime_path(
        self,
    ) -> None:
        db, tenant, _customer, product, convo, scenario = _seed_address_world(
            GENERIC_CLOTHING,
        )
        attach_brain_state(convo, _active_catalog_prep(scenario, product))
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="مسجله عندكم",
        )
        assert result.handled, result.reason
        assert _ASK_CITY not in result.reply
        assert scenario.city in result.reply or result.state_patch.get("city") == scenario.city

    @pytest.mark.parametrize(
        "lookup_phone",
        [DEFAULT_PHONE, DEFAULT_PHONE_E164, "966500000001", "+966500000001"],
        ids=["local", "e164_plus", "e164_no_plus", "e164_explicit"],
    )
    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    def test_catalog_checkout_phone_identity_matches_customer_address_variants(
        self,
        mock_items,
        lookup_phone: str,
    ) -> None:
        db, tenant, _customer, product, _convo, scenario = _seed_address_world(
            GENERIC_PERFUME,
            phone=DEFAULT_PHONE_E164,
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
            customer_phone=lookup_phone,
            message="",
            inbound_metadata=_catalog_meta(
                retailer_id=product.meta_retailer_id,
                price=scenario.catalog_price,
            ),
        )
        assert result.handled, result.reason
        assert _ASK_CITY not in result.reply
        assert scenario.city in result.reply


class TestDraftOrderNumberConsistency:
    def test_active_checkout_does_not_claim_registered_order_without_persisted_order(
        self,
    ) -> None:
        reply = compose_wa_order_flow_reply(
            order_prep={
                "line_items": [{"product_id": "1", "product_name": "حذاء", "quantity": 1}],
            },
            brain_state={"order_prep": {}},
            catalog_resolution=None,
        )
        assert reply
        assert _REGISTERED_ORDER_CLAIM not in reply
        assert _HONEST_PREP in reply

    def test_order_number_question_during_active_checkout_does_not_fall_to_no_orders_tracking(
        self,
    ) -> None:
        db, tenant, _customer, product, convo, scenario = _seed_address_world(GENERIC_SHOES)
        attach_brain_state(convo, _active_catalog_prep(scenario, product))
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="كم رقم الطلب ؟",
        )
        assert result.handled, result.reason
        assert _NO_ORDERS not in result.reply
        assert _NO_NUMBER_YET in result.reply

    def test_order_number_question_with_order_prep_explains_no_number_until_checkout_completed(
        self,
    ) -> None:
        class _Prep:
            @staticmethod
            def to_dict() -> dict:
                return {
                    "line_items": [{"product_name": "قميص", "quantity": 1}],
                    "order_flow_v2_trusted_price": True,
                }

        class _State:
            order_prep = _Prep()
            draft_order_id = ""
            current_product_focus = {}

        reply = resolve_track_order_fallback(state=_State(), history=[])
        assert reply is not None
        assert _NO_ORDERS not in reply
        assert _NO_NUMBER_YET in reply

    def test_order_number_question_with_persisted_draft_returns_draft_number_if_available(
        self,
    ) -> None:
        db, tenant, _customer, product, convo, scenario = _seed_address_world(
            GENERIC_CLOTHING,
        )
        draft_number = "NHL-SCENARIO-7788"
        seed_order(
            db,
            tenant.id,
            external_id=nahla_wa_external_id(tenant.id, convo.id),
            external_order_number=draft_number,
            status="draft",
            extra_metadata={"lifecycle": "whatsapp_draft"},
            line_items=[{"title": scenario.product_title, "quantity": 1}],
        )
        attach_brain_state(convo, _active_catalog_prep(scenario, product))
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="كم رقم الطلب؟",
        )
        assert result.handled, result.reason
        assert draft_number in result.reply
        assert _NO_ORDERS not in result.reply


class TestProductKeywordInOrderFlow:
    @pytest.mark.parametrize(
        "scenario",
        [GENERIC_SHOES, GENERIC_CLOTHING, GENERIC_PERFUME],
        ids=["shoes", "clothing", "perfume"],
    )
    def test_product_keyword_inside_quick_order_routes_to_catalog_search_not_social(
        self,
        scenario: CommerceScenario,
    ) -> None:
        db, tenant, _customer, _product, convo, _scenario = _seed_address_world(scenario)
        attach_brain_state(convo, {
            "checkout_channel": "whatsapp_quick_order",
            "awaiting_checkout_channel": False,
        })
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message=scenario.product_keyword,
        )
        assert result.handled, result.reason
        assert _SOCIAL_GREETING not in result.reply
        assert "منتج" in result.reply or "قائمة" in result.reply

    @pytest.mark.parametrize(
        ("scenario", "keyword"),
        [
            (GENERIC_SHOES, "حذاء"),
            (GENERIC_CLOTHING, "قميص"),
            (GENERIC_PERFUME, "عطر"),
        ],
        ids=["shoe_keyword", "shirt_keyword", "perfume_keyword"],
    )
    def test_generic_product_keyword_inside_order_flow_is_not_social_greeting(
        self,
        scenario: CommerceScenario,
        keyword: str,
    ) -> None:
        db, tenant, _customer, product, convo, scenario = _seed_address_world(scenario)
        attach_brain_state(convo, {
            "order_flow_v2_active": True,
            "checkout_channel": "whatsapp_catalog",
            "line_items": [{
                "product_id": str(product.id),
                "product_name": product.title,
                "quantity": 1,
            }],
        })
        db.add(convo)
        db.commit()

        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message=keyword,
        )
        assert result.handled, result.reason
        assert _SOCIAL_GREETING not in result.reply


class TestInboundOutboundDedupRegression:
    def test_duplicate_inbound_same_provider_message_id_is_dropped(self) -> None:
        msg_id = "wamid.scenario.duplicate.inbound"
        phone_number_id = "PH_SCENARIO_DEDUP"
        assert is_duplicate_inbound(phone_number_id=phone_number_id, msg_id=msg_id) is False
        assert is_duplicate_inbound(phone_number_id=phone_number_id, msg_id=msg_id) is True

    def test_outbound_semantic_dedup_flags_near_identical_replies(self) -> None:
        from routers.whatsapp_webhook import _is_repeat_reply  # noqa: PLC0415

        history = [
            {"direction": "outbound", "body": "أرسل صورة المنتج من الكتالوج لو تكرمت."},
        ]
        duplicate = "أرسل صورة المنتج من الكتالوج لو تكرمت."
        distinct = "كم السعر؟"

        assert _is_repeat_reply(duplicate, history) is True
        assert _is_repeat_reply(distinct, history) is False

    def test_intentional_later_repeat_is_not_blocked_by_inbound_dedup(self) -> None:
        msg_id_a = "wamid.scenario.visual.a"
        msg_id_b = "wamid.scenario.visual.b"
        phone_number_id = "PH_SCENARIO_VISUAL"
        assert is_duplicate_inbound(phone_number_id=phone_number_id, msg_id=msg_id_a) is False
        assert is_duplicate_inbound(phone_number_id=phone_number_id, msg_id=msg_id_b) is False
