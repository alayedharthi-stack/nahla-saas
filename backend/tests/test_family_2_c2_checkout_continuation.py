"""Family 2 C2 — checkout continuation uses canonical missing-fields.

Asserts ownership and structured facts, not assistant wording.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any, Tuple
from unittest.mock import patch

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
from core.order_flow import persist_checkout_location_outcome  # noqa: E402
from core.reply_instruction import (  # noqa: E402
    CONSTRAINT_RESPECT_PLATFORM_NEXT_SLOT,
    build_order_slot_instruction,
)
from core.wa_order_lifecycle import (  # noqa: E402
    has_accepted_delivery_address,
    sync_funnel_status_after_accepted_delivery,
)
from models import Base, Customer, CustomerAddress, Tenant  # noqa: E402
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    attach_commerce_turn_contract,
    build_commerce_turn_contract,
    canonical_checkout_next_slot,
)
from modules.ai.brain.compose.operational_expression import (  # noqa: E402
    compose_operational_expression_goal,
)
from modules.ai.brain.pipeline import _compose_base_response_goal  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
    SuggestionSnapshot,
)
from modules.ai.order_flow_v2.missing_fields import next_missing_field  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_CITY = "الرياض"
GENERIC_DISTRICT = "العليا"
GENERIC_SHORT = "RRRD1234"
GENERIC_MAPS = "https://maps.google.com/?q=24.7136,46.6753"
SHIRT_SKU = "sku-white-sneaker"
SHIRT_TITLE = "حذاء رياضي أبيض"
PERFUME_SKU = "sku-rose-perfume"
PERFUME_TITLE = "عطر ورد 100ml"

_LOCATION_SLOTS = {
    "delivery_address",
    "address",
    "address_line",
    "short_address_code",
    "google_maps_url",
    "address_location",
    "city",
}
_ADDRESS_COLLECT_GOALS = {
    "collect_missing_city",
    "collect_city_for_whatsapp_order",
    "collect_city_only",
    "collect_missing_address",
    "collect_delivery_address_for_whatsapp_order",
    "collect_delivery_address_only",
    "collect_or_confirm_delivery_address",
    "confirm_known_address",
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


def _seed_tenant(db, *, name: str = GENERIC_MERCHANT) -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _official_customer(db, tenant_id: int, *, phone: str, name: str) -> Customer:
    customer = Customer(
        tenant_id=tenant_id,
        phone=phone,
        name=name,
        extra_metadata={
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
            "customer_name_source": "customer_entered",
        },
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _catalog_prep(**kwargs) -> OrderPreparationState:
    payload = dict(
        catalog_line_items_authoritative=True,
        customer_first_name="أحمد",
        customer_last_name="سالم",
        city=GENERIC_CITY,
        district=GENERIC_DISTRICT,
        google_maps_url=GENERIC_MAPS,
        latitude=24.7136,
        longitude=46.6753,
        short_address_code=GENERIC_SHORT,
        order_status="awaiting_address",
        line_items=[{
            "product_id": SHIRT_SKU,
            "product_retailer_id": SHIRT_SKU,
            "product_name": SHIRT_TITLE,
            "title": SHIRT_TITLE,
            "quantity": 1,
            "price": 180.0,
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


def _checkout_ctx(
    *,
    message: str,
    prep: OrderPreparationState,
    tenant_id: int = 1,
    phone: str = "966511111111",
):
    state = MerchantConversationState(
        stage="ordering",
        order_prep=prep,
        customer_goal="discover_products",
        last_intent="ask_product",
        last_action="propose_draft_order",
        pending_action="confirm_order_details",
    )
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=message,
        intent=Intent(name="ask_product", confidence=0.9, raw_message=message),
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


def _accepted_checkout_contract(message: str = "نكمل الطلب"):
    previous = _complete_previous_address()
    prep = _catalog_prep()
    ctx = _checkout_ctx(message=message, prep=prep)
    contract = _build_contract(ctx, _order_context(previous=previous))
    attach_commerce_turn_contract(ctx, contract)
    return contract, ctx


class TestC201ContinuationDoesNotRequestLocation:
    def test_known_maps_and_saved_address_continue_toward_canonical_next_slot(self) -> None:
        contract, _ctx = _accepted_checkout_contract("نكمل الطلب")
        missing = set(contract.missing_fields)
        nxt = contract.known_facts.get("next_missing_field")
        assert not (_LOCATION_SLOTS & missing)
        assert nxt not in _LOCATION_SLOTS
        assert nxt == next_missing_field(list(contract.missing_fields)) or nxt == "none"
        assert contract.next_goal not in _ADDRESS_COLLECT_GOALS
        assert contract.known_facts.get("checkout_location_evidence_known") is True
        assert contract.known_facts.get("saved_address_complete") is True
        assert contract.known_facts.get("location_link_persisted") is True
        assert has_accepted_delivery_address(_catalog_prep().to_dict()) is True


class TestC202MapsRemainsKnownOnLaterTurn:
    def test_independent_later_turn_keeps_maps_and_does_not_reopen_location(self) -> None:
        for message in ("نكمل الطلب", "أبغى أكمل الطلب", "كمّل الطلب"):
            contract, _ctx = _accepted_checkout_contract(message)
            assert contract.known_facts.get("saved_location_link") == GENERIC_MAPS
            assert contract.known_facts.get("checkout_location_evidence_known") is True
            assert "delivery_address" not in contract.missing_fields
            assert contract.known_facts.get("next_missing_field") not in _LOCATION_SLOTS


class TestC203NextMissingFieldProjectedThroughComposeBoundary:
    def test_order_slot_instruction_keeps_canonical_next_field(self) -> None:
        contract, ctx = _accepted_checkout_contract()
        missing, nxt = canonical_checkout_next_slot(ctx)
        assert nxt == (next_missing_field(list(contract.missing_fields)) or "none")
        instr = build_order_slot_instruction(
            slot=str(nxt),
            legacy_copy="",
            product={"title": SHIRT_TITLE},
            next_missing_field=str(nxt),
            missing_fields=list(missing),
        )
        assert instr.facts["next_missing_field"] == nxt
        assert CONSTRAINT_RESPECT_PLATFORM_NEXT_SLOT in instr.constraints
        goal = compose_operational_expression_goal(instr)
        assert f"next_missing_field={nxt}" in goal
        assert "Ask only for that field" in goal


class TestC204ProposeDraftOrderCannotOverrideCanonicalMissing:
    def test_response_goal_uses_contract_next_slot_not_stale_address(self) -> None:
        contract, _ctx = _accepted_checkout_contract()
        nxt = contract.known_facts.get("next_missing_field")
        goal = _compose_base_response_goal(
            Decision(action="propose_draft_order", args={}),
            SuggestionSnapshot(),
            checkout_facts={
                "next_missing_field": nxt,
                "next_goal": "collect_delivery_address_only",
                "checkout_location_evidence_known": True,
            },
        )
        assert f"next_missing_field={nxt}" in goal
        assert "collect_delivery_address_only" not in goal

    def test_confirm_known_address_survives_when_current_location_unknown(self) -> None:
        goal = _compose_base_response_goal(
            Decision(action="propose_draft_order", args={}),
            SuggestionSnapshot(),
            checkout_facts={
                "next_missing_field": "delivery_address",
                "next_goal": "confirm_known_address",
                "checkout_location_evidence_known": False,
            },
        )
        assert "Ask only for that field" not in goal
        assert "next_missing_field=delivery_address" not in goal


class TestC205StaleAwaitingAddressCannotCauseRecollection:
    def test_funnel_status_refresh_and_contract_ignore_stale_awaiting_address(self) -> None:
        prep = _catalog_prep(order_status="awaiting_address")
        refreshed = sync_funnel_status_after_accepted_delivery(prep.to_dict())
        assert refreshed == "awaiting_payment"
        assert refreshed != "awaiting_address"
        contract, _ctx = _accepted_checkout_contract()
        assert contract.next_goal not in _ADDRESS_COLLECT_GOALS
        assert contract.known_facts.get("next_missing_field") not in _LOCATION_SLOTS

    def test_location_persist_refreshes_stale_awaiting_address(self) -> None:
        captured: dict = {}

        def _fake_apply(*_args, **kwargs):
            captured.update(kwargs.get("state_patch") or {})
            return True

        with patch("core.order_flow.apply_state_patch", side_effect=_fake_apply):
            ok, reason = persist_checkout_location_outcome(
                db=object(),
                tenant_id=9,
                phone="966511111111",
                state_patch={
                    "google_maps_url": GENERIC_MAPS,
                    "order_status": "awaiting_address",
                },
            )
        assert ok is True
        assert reason == "persisted"
        assert captured.get("order_status") == "awaiting_payment"


class TestC206ProductRemainsPersisted:
    def test_selected_product_survives_continuation_contract(self) -> None:
        contract, _ctx = _accepted_checkout_contract()
        titles = contract.known_facts.get("product_titles") or []
        assert SHIRT_TITLE in titles or contract.known_facts.get("selected_product_id") == SHIRT_SKU
        assert "product" not in contract.missing_fields


class TestC207CityDistrictMapsRemainPersisted:
    def test_city_district_maps_stay_known(self) -> None:
        contract, _ctx = _accepted_checkout_contract()
        facts = contract.known_facts
        assert facts.get("checkout_city") == GENERIC_CITY
        assert facts.get("checkout_district") == GENERIC_DISTRICT or facts.get("saved_district") == GENERIC_DISTRICT
        assert facts.get("saved_location_link") == GENERIC_MAPS
        assert facts.get("location_link_persisted") is True


class TestC208GenericSecondTenant:
    def test_perfume_merchant_same_continuation_contract(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db, name="متجر عطور تجريبي")
        customer = _official_customer(
            db, tenant.id, phone="+966522222222", name="نورة عبدالله",
        )
        db.add(CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city="جدة",
            district="الشاطئ",
            saudi_national_address="JEDA1234",
            google_maps_link="https://maps.google.com/?q=21.5,39.1",
        ))
        db.commit()
        prep = _catalog_prep(
            city="جدة",
            district="الشاطئ",
            google_maps_url="https://maps.google.com/?q=21.5,39.1",
            short_address_code="JEDA1234",
            customer_first_name="نورة",
            customer_last_name="عبدالله",
            line_items=[{
                "product_id": PERFUME_SKU,
                "product_retailer_id": PERFUME_SKU,
                "product_name": PERFUME_TITLE,
                "title": PERFUME_TITLE,
                "quantity": 1,
                "source": "whatsapp_native_catalog_order",
                "from_native_catalog_order": True,
            }],
        )
        previous = _complete_previous_address(
            city="جدة",
            district="الشاطئ",
            maps_url="https://maps.google.com/?q=21.5,39.1",
            short_address="JEDA1234",
        )
        ctx = _checkout_ctx(
            message="نكمل الطلب",
            prep=prep,
            tenant_id=int(tenant.id),
            phone="966522222222",
        )
        contract = _build_contract(ctx, _order_context(previous=previous, customer_id=customer.id))
        assert contract.known_facts.get("checkout_location_evidence_known") is True
        assert contract.known_facts.get("next_missing_field") not in _LOCATION_SLOTS
        assert contract.next_goal not in _ADDRESS_COLLECT_GOALS


class TestC209NoPhraseRegexTenantPhoneRuntime:
    def test_new_owner_has_no_phrase_or_tenant_phone_special_cases(self) -> None:
        src = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "commerce", "commerce_turn_contract.py"),
            encoding="utf-8",
        ).read()
        block = src.split("canonical_v2_missing_fields_owner", 1)[1]
        block = block.split("return CommerceTurnContract", 1)[0]
        assert "966542980511" not in block
        assert "tenant_id == 33" not in block and "tenant_id==33" not in block
        assert "نكمل الطلب" not in block
        assert "خرائط قوقل" not in block
        owner = open(
            os.path.join(_BACKEND, "core", "wa_order_lifecycle.py"),
            encoding="utf-8",
        ).read()
        helper = owner.split("def sync_funnel_status_after_accepted_delivery", 1)[1]
        helper = helper.split("def ", 1)[0]
        assert "966" not in helper
        assert "tenant" not in helper.lower()


class TestC210NoPromptModelProviderChanges:
    def test_customer_ai_runtime_not_retargeted(self) -> None:
        changed = [
            os.path.join(_BACKEND, "modules", "ai", "brain", "commerce", "commerce_turn_contract.py"),
            os.path.join(_BACKEND, "core", "wa_order_lifecycle.py"),
            os.path.join(_BACKEND, "core", "order_flow.py"),
            os.path.join(_BACKEND, "core", "reply_instruction.py"),
            os.path.join(_BACKEND, "modules", "ai", "brain", "execution", "orders.py"),
        ]
        for path in changed:
            text = open(path, encoding="utf-8").read()
            assert "gpt-4o-mini" not in text
            assert "temperature" not in text.split("def sync_funnel_status_after_accepted_delivery", 1)[-1][:800]
            assert "openai" not in os.path.basename(path)
