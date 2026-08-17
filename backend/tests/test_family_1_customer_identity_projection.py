"""Family 1 — customer facts & identity projection.

Asserts ownership and structured facts, not exact model wording.
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, Tuple
from unittest.mock import patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_WHATSAPP_PROFILE,
    STATUS_CUSTOMER_ENTERED,
    STATUS_PROPOSED,
)
from models import Base, Customer, Tenant  # noqa: E402
from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: E402
    merchant_customer_record_facts,
    resolve_catalog_checkout_customer_identity,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    _identity_collect_goal_is_stale,
    build_commerce_turn_contract,
)
from modules.ai.brain.compose.prompt_payload_slim import (  # noqa: E402
    strip_state_dict_for_prompt,
)
from modules.ai.brain.pipeline import _build_reply_state  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
    SuggestionSnapshot,
)
from modules.ai.gender.context import resolve_customer_gender_context  # noqa: E402
from modules.ai.gender.detector import GENDER_UNKNOWN, detect_gender  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
LIVE_REPRO_CUSTOMER = "هيثم الحارثي"

_GREETING_PARAPHRASES = (
    "السلام عليكم",
    "مرحبا",
    "هلا",
)

_NAME_SLOTS = {
    "name",
    "full_name",
    "customer_name",
    "customer_first_name",
    "customer_last_name",
}
_PHONE_SLOTS = {"phone", "customer_phone", "customer_phone_number", "mobile"}
_NAME_COLLECT_GOALS = {
    "collect_customer_name_for_whatsapp_order",
    "collect_customer_name_only",
    "confirm_customer_name_once",
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


def _catalog_prep() -> OrderPreparationState:
    return OrderPreparationState(
        catalog_line_items_authoritative=True,
        line_items=[{
            "product_retailer_id": "sku-blue-shirt",
            "quantity": 1,
            "from_native_catalog_order": True,
        }],
        catalog_checkout_total=180.0,
    )


def _first_turn_reply_state(*, message: str, profile: dict, phone: str = "966511111111"):
    state = MerchantConversationState()
    ctx = BrainContext(
        tenant_id=1,
        customer_phone=phone,
        message=message,
        intent=Intent(name="greeting", confidence=0.95, raw_message=message),
        state=state,
        facts=_facts(),
        profile=profile,
    )
    return _build_reply_state(
        ctx=ctx,
        previous_state=state,
        current_state=state,
        suggestion=SuggestionSnapshot(),
        decision=Decision(
            action="llm_reply",
            args={"topic": "persona_social", "block_commerce_escalation": True},
            reason="greeting",
        ),
        merchant_context={},
        db=None,
    )


class TestIdentity01FirstTurnBrainProjection:
    def test_greeting_paraphrases_project_authoritative_name(self) -> None:
        for greeting in _GREETING_PARAPHRASES:
            reply_state = _first_turn_reply_state(
                message=greeting,
                profile={"name": GENERIC_CUSTOMER, "customer_name": GENERIC_CUSTOMER},
            )
            facts = reply_state.known_facts or {}
            assert facts.get("customer_name_known") is True, greeting
            assert facts.get("customer_name") == GENERIC_CUSTOMER, greeting
            slim = strip_state_dict_for_prompt(
                asdict(reply_state),
                reply_state,
                kb_in_prompt_block=False,
            )
            slim_facts = slim.get("known_facts") or {}
            assert slim_facts.get("customer_name") == GENERIC_CUSTOMER, greeting

    def test_live_repro_name_is_available_without_requiring_utterance(self) -> None:
        reply_state = _first_turn_reply_state(
            message="السلام عليكم",
            profile={"name": LIVE_REPRO_CUSTOMER, "customer_name": LIVE_REPRO_CUSTOMER},
        )
        facts = reply_state.known_facts or {}
        assert facts.get("customer_name_known") is True
        assert facts.get("customer_name") == LIVE_REPRO_CUSTOMER


class TestIdentity02TenantScopedName:
    def test_same_phone_does_not_leak_across_tenants(self) -> None:
        db, _ = _make_db()
        tenant_a = _seed_tenant(db, name="متجر أ")
        tenant_b = _seed_tenant(db, name="متجر ب")
        phone = "+966522222222"
        _official_customer(db, tenant_a.id, phone=phone, name="نورة عبدالله")
        _official_customer(db, tenant_b.id, phone=phone, name="أحمد سالم")

        ident_a = resolve_catalog_checkout_customer_identity(
            db=db,
            tenant_id=tenant_a.id,
            phone="966522222222",
        )
        ident_b = resolve_catalog_checkout_customer_identity(
            db=db,
            tenant_id=tenant_b.id,
            phone="966522222222",
        )
        assert ident_a.known_facts.get("customer_name") == "نورة عبدالله"
        assert ident_b.known_facts.get("customer_name") == "أحمد سالم"
        assert ident_a.known_facts.get("customer_id") != ident_b.known_facts.get("customer_id")


class TestIdentity04KnownNameNotMissing:
    def test_active_checkout_refreshes_next_goal_after_known_name(self) -> None:
        prep = _catalog_prep()
        state = MerchantConversationState(stage="ordering", order_prep=prep)
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966511111111",
            message="أبغى أكمل الطلب",
            intent=Intent(name="start_order", confidence=0.9, raw_message="أبغى أكمل الطلب"),
            state=state,
            facts=_facts(),
            history=[],
            profile={"name": GENERIC_CUSTOMER, "customer_name": GENERIC_CUSTOMER},
        )
        with patch(
            "modules.ai.brain.commerce.commerce_turn_contract._load_order_context_for_contract",
            return_value=None,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.is_current_catalog_order_submitted",
            return_value=False,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.try_active_catalog_checkout_continue_decision",
            return_value=None,
        ):
            contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("customer_name_known") is True
        assert contract.known_facts.get("customer_name") == GENERIC_CUSTOMER
        assert not (_NAME_SLOTS & set(contract.missing_fields))
        assert contract.next_goal not in _NAME_COLLECT_GOALS
        assert "identity_next_goal_refreshed_after_known_facts" in (contract.reasons or [])
        assert contract.known_facts.get("phone_known") is True
        assert not (_PHONE_SLOTS & set(contract.missing_fields))

    def test_generic_perfume_merchant_same_contract(self) -> None:
        prep = _catalog_prep()
        state = MerchantConversationState(stage="ordering", order_prep=prep)
        ctx = BrainContext(
            tenant_id=77,
            customer_phone="966533333333",
            message="كمّل الطلب",
            intent=Intent(name="start_order", confidence=0.9, raw_message="كمّل الطلب"),
            state=state,
            facts=_facts(store_name="متجر عطور تجريبي"),
            history=[],
            profile={"name": "نورة عبدالله", "customer_name": "نورة عبدالله"},
        )
        with patch(
            "modules.ai.brain.commerce.commerce_turn_contract._load_order_context_for_contract",
            return_value=None,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.is_current_catalog_order_submitted",
            return_value=False,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.try_active_catalog_checkout_continue_decision",
            return_value=None,
        ):
            contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("customer_name") == "نورة عبدالله"
        assert contract.next_goal not in _NAME_COLLECT_GOALS


class TestIdentity05UnknownNameMayRemainMissing:
    def test_unknown_customer_name_stays_collectable(self) -> None:
        prep = _catalog_prep()
        state = MerchantConversationState(stage="ordering", order_prep=prep)
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966544444444",
            message="أبغى أكمل الطلب",
            intent=Intent(name="start_order", confidence=0.9, raw_message="أبغى أكمل الطلب"),
            state=state,
            facts=_facts(),
            history=[],
            profile={},
        )
        with patch(
            "modules.ai.brain.commerce.commerce_turn_contract._load_order_context_for_contract",
            return_value=None,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.is_current_catalog_order_submitted",
            return_value=False,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.try_active_catalog_checkout_continue_decision",
            return_value=None,
        ):
            contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("customer_name_known") is not True
        assert contract.next_goal in _NAME_COLLECT_GOALS or any(
            slot in set(contract.missing_fields) for slot in _NAME_SLOTS
        )

    def test_proposed_whatsapp_name_is_not_operational(self) -> None:
        customer = SimpleNamespace(
            id=12,
            name="",
            extra_metadata={
                "proposed_name": LIVE_REPRO_CUSTOMER,
                "customer_name_source": SOURCE_WHATSAPP_PROFILE,
                "customer_name_status": STATUS_PROPOSED,
            },
        )
        identity = resolve_catalog_checkout_customer_identity(
            customer=customer,
            phone="966537970430",
            order_prep={},
        )
        assert identity.customer_name_known is False
        assert not identity.known_facts.get("customer_name")


class TestIdentity06WhatsappSenderPhoneProvenance:
    def test_whatsapp_sender_is_authorized_order_contact_not_a_collect_slot(self) -> None:
        prep = _catalog_prep()
        state = MerchantConversationState(stage="ordering", order_prep=prep)
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966511111111",
            message="أبغى أكمل الطلب",
            intent=Intent(name="start_order", confidence=0.9, raw_message="أبغى أكمل الطلب"),
            state=state,
            facts=_facts(),
            profile={"name": GENERIC_CUSTOMER},
        )
        with patch(
            "modules.ai.brain.commerce.commerce_turn_contract._load_order_context_for_contract",
            return_value=None,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.is_current_catalog_order_submitted",
            return_value=False,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.try_active_catalog_checkout_continue_decision",
            return_value=None,
        ):
            contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("phone_known") is True
        assert contract.known_facts.get("phone_source") in {"whatsapp", "whatsapp_sender"}
        assert not (_PHONE_SLOTS & set(contract.missing_fields))

    def test_no_whatsapp_phone_does_not_invent_order_contact(self) -> None:
        identity = resolve_catalog_checkout_customer_identity(
            phone="",
            order_prep={},
        )
        assert identity.known_facts.get("phone_known") is not True


class TestIdentity07AddressingUnknown:
    def test_no_canonical_gender_on_merchant_record(self) -> None:
        identity = resolve_catalog_checkout_customer_identity(
            profile={"name": LIVE_REPRO_CUSTOMER},
            customer=SimpleNamespace(id=9, name=LIVE_REPRO_CUSTOMER, full_name=""),
        )
        facts = merchant_customer_record_facts(identity)
        record = facts.get("merchant_customer_record") or {}
        assert "gender" not in record
        assert "customer_gender" not in facts
        assert "addressing" not in facts

    def test_name_does_not_infer_platform_gender(self) -> None:
        hint = detect_gender("", LIVE_REPRO_CUSTOMER)
        assert hint.value == GENDER_UNKNOWN
        ctx = resolve_customer_gender_context(
            message="السلام عليكم",
            customer_name=LIVE_REPRO_CUSTOMER,
            state=None,
            profile={},
        )
        assert ctx.gender == GENDER_UNKNOWN
        assert ctx.source in {"unknown", "none"}


class TestIdentity08ExplicitAddressingIfPresent:
    def test_profile_gender_is_projected_when_explicit(self) -> None:
        ctx = resolve_customer_gender_context(
            message="السلام عليكم",
            customer_name=GENERIC_CUSTOMER,
            state=None,
            profile={"gender": "male", "gender_source": "profile"},
        )
        assert ctx.gender == "male"
        assert ctx.source == "profile"


class TestIdentity09NoPhraseRuntime:
    def test_family_1_patch_does_not_add_greeting_or_gender_phrases(self) -> None:
        src = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "commerce", "commerce_turn_contract.py"),
            encoding="utf-8",
        ).read()
        assert "السلام عليكم" not in src
        assert "أنا رجل" not in src
        assert "هيثم" not in src
        assert "أبشري" not in src


class TestIdentityCollectGoalStaleHelper:
    def test_name_goal_stale_when_name_slots_gone(self) -> None:
        assert _identity_collect_goal_is_stale(
            "collect_customer_name_for_whatsapp_order",
            ["city"],
        ) is True
        assert _identity_collect_goal_is_stale(
            "collect_customer_name_for_whatsapp_order",
            ["customer_first_name", "city"],
        ) is False
        assert _identity_collect_goal_is_stale(
            "continue_checkout_from_catalog_order",
            ["city"],
        ) is False
