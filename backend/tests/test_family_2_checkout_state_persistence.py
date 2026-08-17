"""Family 2 — checkout state & field persistence.

Asserts ownership, hydration, and structured facts — not assistant wording.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any, Tuple
from unittest.mock import MagicMock, patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.customer_identity_resolver import STATUS_CUSTOMER_ENTERED  # noqa: E402
from core.order_context_builder import build_order_context  # noqa: E402
from core.order_context_prefill import (  # noqa: E402
    apply_saved_address_to_checkout_contract,
    checkout_location_evidence_known,
)
from core.order_flow import persist_checkout_location_patch  # noqa: E402
from core.wa_address_ingestion import resolve_address_state_patch  # noqa: E402
from models import Base, Customer, CustomerAddress, Tenant  # noqa: E402
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    _address_collect_goal_is_stale,
    build_commerce_turn_contract,
)
from modules.ai.brain.execution.orders import (  # noqa: E402
    _grounded_address_slot,
    _merge_message_details,
    _sync_single_product_line_item,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_CITY = "الرياض"
GENERIC_DISTRICT = "العليا"
GENERIC_SHORT = "RRRD1234"
GENERIC_MAPS = "https://maps.google.com/?q=24.7136,46.6753"
SHIRT_SKU = "sku-white-sneaker"
SHIRT_TITLE = "حذاء رياضي أبيض"

_CHECKOUT_PARAPHRASES = (
    "أبغى أكمل الطلب",
    "كمّل الطلب",
    "عندكم مسجلة",
    "العنوان محفوظ",
)

_CITY_SLOTS = {"city"}
_DELIVERY_SLOTS = {
    "delivery_address",
    "address",
    "address_line",
    "short_address_code",
    "google_maps_url",
    "address_location",
}
_NAME_SLOTS = {
    "name",
    "full_name",
    "customer_name",
    "customer_first_name",
    "customer_last_name",
}
_ADDRESS_COLLECT_GOALS = {
    "collect_missing_city",
    "collect_city_for_whatsapp_order",
    "collect_city_only",
    "collect_missing_address",
    "collect_delivery_address_for_whatsapp_order",
}


def _facts(**kwargs) -> CommerceFacts:
    payload = dict(
        store_name=GENERIC_MERCHANT,
        has_products=True,
        product_count=4,
        in_stock_count=4,
        orderable=True,
        snapshot_fresh=True,
    )
    payload.update(kwargs)
    return CommerceFacts(**payload)


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db, *, name: str = "T") -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _official_customer(db, tenant_id: int, *, phone: str, name: str) -> Customer:
    digits = phone.lstrip("+")
    customer = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=digits,
        name=name,
        extra_metadata={
            "customer_name_source": "manual_admin",
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
            "customer_name_confidence": 0.95,
        },
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _catalog_prep(**kwargs) -> OrderPreparationState:
    payload = dict(
        catalog_line_items_authoritative=True,
        line_items=[{
            "product_id": SHIRT_SKU,
            "product_retailer_id": SHIRT_SKU,
            "product_name": SHIRT_TITLE,
            "title": SHIRT_TITLE,
            "quantity": 1,
            "source": "whatsapp_native_catalog_order",
            "from_native_catalog_order": True,
        }],
        catalog_checkout_total=180.0,
    )
    payload.update(kwargs)
    return OrderPreparationState(**payload)


def _complete_previous_address(**overrides) -> SimpleNamespace:
    data = dict(
        city=GENERIC_CITY,
        district=GENERIC_DISTRICT,
        address_line="شارع التحلية",
        maps_url=GENERIC_MAPS,
        short_address=GENERIC_SHORT,
        latitude=24.7136,
        longitude=46.6753,
        source="customer_addresses",
        accepted_delivery_address=True,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _order_context(*, previous=None, shipping_edit=False, customer_id=1):
    return SimpleNamespace(
        known_previous_address=previous,
        prefill=SimpleNamespace(shipping_edit_requested=shipping_edit),
        customer_id=customer_id,
    )


def _checkout_ctx(*, message: str, prep: OrderPreparationState, tenant_id: int = 1, phone: str = "966511111111"):
    state = MerchantConversationState(stage="ordering", order_prep=prep)
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=message,
        intent=Intent(name="start_order", confidence=0.9, raw_message=message),
        state=state,
        facts=_facts(),
        history=[],
        profile={"name": GENERIC_CUSTOMER, "customer_name": GENERIC_CUSTOMER},
    )


def _build_contract(ctx, order_context):
    with patch(
        "modules.ai.brain.commerce.commerce_turn_contract._load_order_context_for_contract",
        return_value=order_context,
    ), patch(
        "modules.ai.brain.commerce.catalog_order_checkout.is_current_catalog_order_submitted",
        return_value=False,
    ), patch(
        "modules.ai.brain.commerce.catalog_order_checkout.try_active_catalog_checkout_continue_decision",
        return_value=None,
    ):
        return build_commerce_turn_contract(ctx, db=None)


class TestCheckout01CompleteSavedAddressNotRecollected:
    def test_complete_saved_address_not_in_missing_across_paraphrases(self) -> None:
        previous = _complete_previous_address()
        oc = _order_context(previous=previous)
        for message in _CHECKOUT_PARAPHRASES:
            contract = _build_contract(_checkout_ctx(message=message, prep=_catalog_prep()), oc)
            missing = set(contract.missing_fields)
            assert not (_CITY_SLOTS & missing), message
            assert not (_DELIVERY_SLOTS & missing), message
            assert contract.next_goal not in _ADDRESS_COLLECT_GOALS, message
            assert contract.known_facts.get("saved_address_available") is True
            assert contract.known_facts.get("saved_address_complete") is True
            assert contract.known_facts.get("saved_city") == GENERIC_CITY
            assert contract.known_facts.get("saved_district") == GENERIC_DISTRICT
            after_goal = contract.known_facts.get("next_goal_after_hydration") or contract.next_goal
            assert after_goal not in _ADDRESS_COLLECT_GOALS, message

    def test_collect_city_goal_is_stale_after_complete_saved_address(self) -> None:
        missing, facts = apply_saved_address_to_checkout_contract(
            missing_fields=["city", "delivery_address", "short_address_code"],
            known_facts={},
            order_context=_order_context(previous=_complete_previous_address()),
            order_prep={},
        )
        assert "city" not in missing
        assert not (_DELIVERY_SLOTS & set(missing))
        assert facts.get("saved_address_complete") is True
        assert _address_collect_goal_is_stale("collect_missing_city", missing) is True
        assert _address_collect_goal_is_stale("collect_missing_address", missing) is True

    def test_generic_perfume_merchant_same_contract(self) -> None:
        previous = _complete_previous_address(city="جدة", district="الشاطئ", short_address="JEDA1234")
        oc = _order_context(previous=previous)
        ctx = _checkout_ctx(message="نكمل الطلب", prep=_catalog_prep(), tenant_id=77, phone="966533333333")
        ctx.facts = _facts(store_name="متجر عطور تجريبي")
        ctx.profile = {"name": "نورة عبدالله", "customer_name": "نورة عبدالله"}
        contract = _build_contract(ctx, oc)
        assert "city" not in contract.missing_fields
        assert contract.known_facts.get("saved_city") == "جدة"


class TestCheckout02PartialSavedAddress:
    def test_only_missing_delivery_evidence_remains(self) -> None:
        previous = SimpleNamespace(
            city=GENERIC_CITY,
            district=GENERIC_DISTRICT,
            address_line="",
            maps_url="",
            short_address="",
            latitude=None,
            longitude=None,
            source="customer_addresses",
            accepted_delivery_address=False,
        )
        missing, facts = apply_saved_address_to_checkout_contract(
            missing_fields=["city", "delivery_address", "short_address_code"],
            known_facts={},
            order_context=_order_context(previous=previous),
            order_prep={},
        )
        assert "city" not in missing
        assert "delivery_address" in missing
        assert facts.get("saved_address_available") is True
        assert facts.get("saved_address_complete") is not True
        assert facts.get("saved_city") == GENERIC_CITY


class TestCheckout03CurrentTurnCorrectionWins:
    def test_current_message_district_overwrites_checkout_district(self) -> None:
        prep = OrderPreparationState(district="العليا", city=GENERIC_CITY)
        _merge_message_details(
            prep,
            {"district": "الورود"},
            "الحي الورود",
        )
        assert prep.district == "الورود"
        assert prep.city == GENERIC_CITY


class TestCheckout04StaleHistoryDoesNotOverwrite:
    def test_old_prose_district_not_applied_when_absent_from_current_message(self) -> None:
        prep = OrderPreparationState(district="الحي", city=GENERIC_CITY)
        _merge_message_details(
            prep,
            {"district": "الحب", "city": "جدة"},
            "أبغى أكمل الطلب",
        )
        assert prep.district == "الحي"
        assert prep.city == GENERIC_CITY

    def test_grounded_helper_rejects_history_only_slot(self) -> None:
        assert _grounded_address_slot(
            "district",
            {"district": "الحب"},
            {},
            "أبغى أكمل الطلب",
        ) == ""
        assert _grounded_address_slot(
            "district",
            {"district": "الحي"},
            {},
            "الحي",
        ) == "الحي"


class TestCheckout05LocationPersistedNotReasked:
    def test_persisted_maps_url_removes_delivery_slots(self) -> None:
        missing, facts = apply_saved_address_to_checkout_contract(
            missing_fields=["city", "delivery_address", "google_maps_url"],
            known_facts={},
            order_context=_order_context(previous=None),
            order_prep={"city": GENERIC_CITY, "google_maps_url": GENERIC_MAPS},
        )
        assert "delivery_address" not in missing
        assert "google_maps_url" not in missing
        assert facts.get("location_link_persisted") is True
        assert facts.get("saved_address_complete") is not True
        assert facts.get("saved_address_available") is not True
        assert checkout_location_evidence_known({"google_maps_url": GENERIC_MAPS}) is True

    def test_pending_maps_evidence_counts_as_known(self) -> None:
        assert checkout_location_evidence_known({
            "pending_google_maps_url": GENERIC_MAPS,
        }) is True


class TestCheckout06LocationPersistFailure:
    def test_empty_patch_is_not_saved(self) -> None:
        assert persist_checkout_location_patch(
            MagicMock(),
            tenant_id=1,
            phone="966511111111",
            state_patch={},
        ) is False

    def test_apply_state_patch_false_is_not_claimed_saved(self) -> None:
        import core.order_flow as order_flow

        with patch.object(order_flow, "apply_state_patch", return_value=False):
            ok = order_flow.persist_checkout_location_patch(
                MagicMock(),
                tenant_id=1,
                phone="966511111111",
                state_patch={"google_maps_url": GENERIC_MAPS},
            )
        assert ok is False

    def test_invalid_city_only_text_does_not_produce_saved_patch(self) -> None:
        assert resolve_address_state_patch(
            inbound_normalized_type="text",
            inbound_text="الرياض",
        ) is None


class TestCheckout07CatalogSelectionPersists:
    def test_authoritative_catalog_item_not_replaced_by_whatsapp_sync(self) -> None:
        prep = _catalog_prep()
        original = list(prep.line_items)
        ctx = _checkout_ctx(message="الرياض", prep=prep)
        _sync_single_product_line_item(
            prep,
            {"external_id": "other-sku", "title": "قميص قطني أزرق", "from_catalog": False},
            ctx,
        )
        assert prep.line_items == original
        assert prep.line_items[0]["product_id"] == SHIRT_SKU


class TestCheckout08AddressCollectionKeepsProduct:
    def test_city_merge_does_not_drop_catalog_line_items(self) -> None:
        prep = _catalog_prep()
        _merge_message_details(prep, {"city": GENERIC_CITY}, GENERIC_CITY)
        assert prep.city == GENERIC_CITY
        assert prep.line_items[0]["product_id"] == SHIRT_SKU
        assert prep.catalog_line_items_authoritative is True


class TestCheckout09ProductSelectionKeepsAddress:
    def test_catalog_sync_skip_does_not_clear_hydrated_city(self) -> None:
        prep = _catalog_prep(city=GENERIC_CITY, google_maps_url=GENERIC_MAPS)
        ctx = _checkout_ctx(message="حذاء رياضي أبيض", prep=prep)
        _sync_single_product_line_item(
            prep,
            {"external_id": "other-sku", "title": "عطر ورد 100ml"},
            ctx,
        )
        assert prep.city == GENERIC_CITY
        assert prep.google_maps_url == GENERIC_MAPS
        assert prep.line_items[0]["product_id"] == SHIRT_SKU


class TestCheckout10Family1IdentityPreserved:
    def test_known_name_and_saved_address_coexist(self) -> None:
        previous = _complete_previous_address()
        contract = _build_contract(
            _checkout_ctx(message="أبغى أكمل الطلب", prep=_catalog_prep()),
            _order_context(previous=previous),
        )
        assert contract.known_facts.get("customer_name_known") is True
        assert contract.known_facts.get("customer_name") == GENERIC_CUSTOMER
        assert not (_NAME_SLOTS & set(contract.missing_fields))
        assert contract.known_facts.get("saved_address_complete") is True
        assert "city" not in contract.missing_fields


class TestCheckout11CrossTenantIsolation:
    def test_same_phone_addresses_do_not_leak(self) -> None:
        db, _ = _make_db()
        tenant_a = _seed_tenant(db, name="متجر أ")
        tenant_b = _seed_tenant(db, name="متجر ب")
        phone = "+966522222222"
        cust_a = _official_customer(db, tenant_a.id, phone=phone, name="نورة عبدالله")
        cust_b = _official_customer(db, tenant_b.id, phone=phone, name="أحمد سالم")
        db.add(CustomerAddress(
            tenant_id=tenant_a.id,
            customer_id=cust_a.id,
            city="جدة",
            district="الشاطئ",
            saudi_national_address="JEDA1234",
            google_maps_link="https://maps.google.com/?q=21.5,39.1",
        ))
        db.add(CustomerAddress(
            tenant_id=tenant_b.id,
            customer_id=cust_b.id,
            city="الرياض",
            district="العليا",
            saudi_national_address="RRRD1234",
            google_maps_link=GENERIC_MAPS,
        ))
        db.commit()

        ctx_a = build_order_context(
            db,
            tenant_id=tenant_a.id,
            customer=cust_a,
            phone=phone,
            brain_state={"order_prep": _catalog_prep().to_dict()},
        )
        ctx_b = build_order_context(
            db,
            tenant_id=tenant_b.id,
            customer=cust_b,
            phone=phone,
            brain_state={"order_prep": _catalog_prep().to_dict()},
        )
        assert ctx_a.known_previous_address is not None
        assert ctx_b.known_previous_address is not None
        assert ctx_a.known_previous_address.city == "جدة"
        assert ctx_b.known_previous_address.city == "الرياض"
        assert ctx_a.customer_id != ctx_b.customer_id


class TestCheckout12NoPhraseRuntime:
    def test_hydration_does_not_add_typo_or_phrase_maps(self) -> None:
        src = open(
            os.path.join(_BACKEND, "core", "order_context_prefill.py"),
            encoding="utf-8",
        ).read()
        apply_src = src.split("def apply_saved_address_to_checkout_contract", 1)[1]
        apply_src = apply_src.split("def ", 1)[0] if "\ndef " in apply_src else apply_src
        assert "الحب" not in apply_src
        assert "عندكم مسجلة" not in apply_src
        assert "أعتمد عنوانك" not in apply_src
        merge_src = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "execution", "orders.py"),
            encoding="utf-8",
        ).read()
        helper = merge_src.split("def _grounded_address_slot", 1)[1].split("def ", 1)[0]
        assert "الحب" not in helper
        assert "الحي" not in helper


class TestCheckout13NoCustomerPromptChange:
    def test_family_2_does_not_touch_persona_or_prompt_builder(self) -> None:
        persona = open(
            os.path.join(_BACKEND, "modules", "ai", "prompts", "nahla_persona.py"),
            encoding="utf-8",
        ).read()
        builder = open(
            os.path.join(_BACKEND, "modules", "ai", "prompts", "builder.py"),
            encoding="utf-8",
        ).read()
        assert "apply_saved_address_to_checkout_contract" not in persona
        assert "apply_saved_address_to_checkout_contract" not in builder
        contract_src = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "commerce", "commerce_turn_contract.py"),
            encoding="utf-8",
        ).read()
        assert "openai" not in contract_src.lower().split("apply_saved_address_to_checkout_contract")[-1][:800]


class TestAddressCollectGoalStaleHelper:
    def test_city_goal_stale_when_city_slot_gone(self) -> None:
        assert _address_collect_goal_is_stale("collect_missing_city", ["delivery_address"]) is True
        assert _address_collect_goal_is_stale("collect_missing_city", ["city"]) is False
        assert _address_collect_goal_is_stale("confirm_known_address", []) is False


class TestShippingEditDoesNotHydrate:
    def test_explicit_edit_keeps_city_missing(self) -> None:
        previous = _complete_previous_address()
        missing, facts = apply_saved_address_to_checkout_contract(
            missing_fields=["city", "delivery_address"],
            known_facts={},
            order_context=_order_context(previous=previous, shipping_edit=True),
            order_prep={},
        )
        assert "city" in missing
        assert facts.get("customer_corrections_applied") is True
