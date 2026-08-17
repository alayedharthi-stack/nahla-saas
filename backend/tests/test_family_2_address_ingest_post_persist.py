"""Family 2 post-deploy — address-ingest reply uses POST-PERSIST checkout state.

Asserts decision ownership and structured facts, not assistant wording.
"""
from __future__ import annotations

import os
import sys
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

from core.address_ingest_post_persist import (  # noqa: E402
    reproject_address_ingest_decision_after_persist,
)
from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_WHATSAPP_PROFILE,
    STATUS_CUSTOMER_ENTERED,
    STATUS_PROPOSED,
)
from core.order_flow import (  # noqa: E402
    maybe_handle_wa_address_inbound,
    persist_checkout_location_outcome,
)
from core.reply_instruction import (  # noqa: E402
    CONSTRAINT_RESPECT_PLATFORM_NEXT_SLOT,
    ReplyInstruction,
    build_address_instruction,
)
from models import Base, Conversation, Customer, CustomerAddress, Tenant  # noqa: E402
from modules.ai.brain.compose.operational_expression import (  # noqa: E402
    compose_operational_expression_goal,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_CITY = "الرياض"
GENERIC_DISTRICT = "العليا"
GENERIC_MAPS = "https://maps.google.com/?q=24.7136,46.6753"
SHIRT_SKU = "sku-white-sneaker"
SHIRT_TITLE = "حذاء رياضي أبيض"
PERFUME_SKU = "sku-rose-perfume"
PERFUME_TITLE = "عطر ورد 100ml"
_NAME_SLOTS = {
    "name",
    "full_name",
    "customer_name",
    "customer_first_name",
    "customer_last_name",
}
_PHONE_SLOTS = {"phone", "customer_phone", "customer_phone_number", "mobile"}


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


def _customer(
    db,
    tenant_id: int,
    *,
    phone: str,
    name: str,
    status: str = STATUS_CUSTOMER_ENTERED,
    source: str = "manual_admin",
) -> Customer:
    digits = phone.lstrip("+")
    customer = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=digits,
        name=name,
        extra_metadata={
            "customer_name_source": source,
            "customer_name_status": status,
            "customer_name_confidence": 0.95,
        },
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _catalog_prep(**kwargs) -> dict:
    first, last = GENERIC_CUSTOMER.split(" ", 1)
    payload = dict(
        catalog_line_items_authoritative=True,
        customer_first_name=first,
        customer_last_name=last,
        customer_phone="966511111111",
        city=GENERIC_CITY,
        district=GENERIC_DISTRICT,
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
        order_status="awaiting_address",
    )
    payload.update(kwargs)
    return payload


def _seed_checkout(
    db,
    *,
    tenant: Tenant,
    customer: Customer,
    prep: dict,
) -> Conversation:
    conv = Conversation(
        tenant_id=tenant.id,
        customer_id=customer.id,
        status="open",
        extra_metadata={"brain_state": {"order_prep": prep}},
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _ingest_maps_and_reproject(db, *, tenant_id: int, phone: str, maps: str = GENERIC_MAPS):
    decision = maybe_handle_wa_address_inbound(
        db=db,
        tenant_id=tenant_id,
        phone=phone,
        inbound_normalized_type="text",
        inbound_text=maps,
    )
    assert decision is not None
    ok, reason = persist_checkout_location_outcome(
        db,
        tenant_id=tenant_id,
        phone=phone,
        state_patch=decision.get("state_patch") or {},
    )
    assert ok is True, reason
    rebuilt = reproject_address_ingest_decision_after_persist(
        db,
        tenant_id=tenant_id,
        phone=phone,
        inbound_text=maps,
        address_type=str((decision.get("state_patch") or {}).get("delivery_address_type") or ""),
    )
    return decision, rebuilt


class TestPostPersistStateUsed:
    def test_pre_patch_decision_does_not_compose_reply(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _customer(db, tenant.id, phone="966511111111", name=GENERIC_CUSTOMER)
        _seed_checkout(db, tenant=tenant, customer=customer, prep=_catalog_prep())
        decision = maybe_handle_wa_address_inbound(
            db=db,
            tenant_id=tenant.id,
            phone="966511111111",
            inbound_normalized_type="text",
            inbound_text=GENERIC_MAPS,
        )
        assert decision is not None
        assert decision.get("compose_after_persist") is True
        assert not str(decision.get("reply_text") or "").strip()
        assert (decision.get("state_patch") or {}).get("google_maps_url") == GENERIC_MAPS

    def test_reproject_uses_persisted_maps_not_pre_patch_missing(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _customer(db, tenant.id, phone="966511111111", name=GENERIC_CUSTOMER)
        _seed_checkout(db, tenant=tenant, customer=customer, prep=_catalog_prep())
        with patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ):
            pre, rebuilt = _ingest_maps_and_reproject(
                db, tenant_id=tenant.id, phone="966511111111",
            )
        assert pre.get("compose_after_persist") is True
        assert rebuilt.get("post_persist_reprojected") is True
        facts = (rebuilt.get("reply_instruction") or {}).get("facts") or {}
        assert facts.get("checkout_maps_url") == GENERIC_MAPS
        assert facts.get("checkout_location_evidence_known") is True
        assert "delivery_address" not in list(rebuilt.get("missing_fields") or [])
        assert rebuilt.get("next_missing_field") != "delivery_address"


class TestWhatsAppPhoneNotMissing:
    def test_sender_phone_is_not_a_missing_slot(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _customer(db, tenant.id, phone="966511111111", name=GENERIC_CUSTOMER)
        _seed_checkout(db, tenant=tenant, customer=customer, prep=_catalog_prep())
        with patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ):
            _, rebuilt = _ingest_maps_and_reproject(
                db, tenant_id=tenant.id, phone="966511111111",
            )
        missing = set(rebuilt.get("missing_fields") or [])
        facts = (rebuilt.get("reply_instruction") or {}).get("facts") or {}
        assert not (missing & _PHONE_SLOTS)
        assert facts.get("phone_source") == "whatsapp"
        assert facts.get("whatsapp_sender_valid_order_contact") is True
        assert rebuilt.get("next_missing_field") not in _PHONE_SLOTS


class TestActiveCheckoutNamePreserved:
    def test_prep_name_does_not_become_missing_after_maps(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _customer(
            db,
            tenant.id,
            phone="966522222222",
            name="هيثم الحارثي",
            status=STATUS_PROPOSED,
            source=SOURCE_WHATSAPP_PROFILE,
        )
        prep = _catalog_prep(
            customer_first_name="هيثم",
            customer_last_name="الحارثي",
            customer_phone="966522222222",
        )
        _seed_checkout(db, tenant=tenant, customer=customer, prep=prep)
        with patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ):
            _, rebuilt = _ingest_maps_and_reproject(
                db, tenant_id=tenant.id, phone="966522222222",
            )
        missing = set(rebuilt.get("missing_fields") or [])
        facts = (rebuilt.get("reply_instruction") or {}).get("facts") or {}
        assert not (missing & _NAME_SLOTS)
        assert facts.get("active_checkout_name_known") is True
        assert rebuilt.get("next_missing_field") not in _NAME_SLOTS


class TestSingleNextMissingField:
    def test_only_city_remaining_is_the_platform_slot(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _customer(db, tenant.id, phone="966511111111", name=GENERIC_CUSTOMER)
        prep = _catalog_prep()
        prep.pop("city", None)
        _seed_checkout(db, tenant=tenant, customer=customer, prep=prep)
        with patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ):
            _, rebuilt = _ingest_maps_and_reproject(
                db, tenant_id=tenant.id, phone="966511111111",
            )
        missing = list(rebuilt.get("missing_fields") or [])
        assert "city" in missing
        assert rebuilt.get("next_missing_field") == "city"
        facts = (rebuilt.get("reply_instruction") or {}).get("facts") or {}
        assert facts.get("next_missing_field") == "city"
        assert facts.get("constrained_compose_decides_slot") is False


class TestNoInventedMissingField:
    def test_complete_checkout_has_no_next_slot(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _customer(db, tenant.id, phone="966511111111", name=GENERIC_CUSTOMER)
        prep = _catalog_prep(payment_method="bank_transfer")
        _seed_checkout(db, tenant=tenant, customer=customer, prep=prep)
        with patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ):
            _, rebuilt = _ingest_maps_and_reproject(
                db, tenant_id=tenant.id, phone="966511111111",
            )
        assert list(rebuilt.get("missing_fields") or []) == []
        assert rebuilt.get("next_missing_field") is None
        facts = (rebuilt.get("reply_instruction") or {}).get("facts") or {}
        assert facts.get("next_missing_field") == "none"
        goal = compose_operational_expression_goal(
            ReplyInstruction.from_dict(rebuilt["reply_instruction"])
        )
        assert "next_missing_field is none" in goal
        assert "continue collecting any remaining order fields" not in goal


class TestCatalogAndAddressSurvive:
    def test_catalog_city_district_maps_remain_after_ingest(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _customer(db, tenant.id, phone="966511111111", name=GENERIC_CUSTOMER)
        db.add(CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city=GENERIC_CITY,
            district=GENERIC_DISTRICT,
            saudi_national_address="RRRD1234",
            google_maps_link=GENERIC_MAPS,
        ))
        db.commit()
        _seed_checkout(db, tenant=tenant, customer=customer, prep=_catalog_prep())
        with patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ):
            _, rebuilt = _ingest_maps_and_reproject(
                db, tenant_id=tenant.id, phone="966511111111",
            )
        facts = (rebuilt.get("reply_instruction") or {}).get("facts") or {}
        assert facts.get("selected_product_id") == SHIRT_SKU
        assert facts.get("catalog_line_items_authoritative") is True
        assert facts.get("checkout_city") == GENERIC_CITY
        assert facts.get("checkout_district") == GENERIC_DISTRICT
        assert facts.get("checkout_maps_url") == GENERIC_MAPS
        assert facts.get("saved_address_available") is True
        assert facts.get("saved_address_complete") is True


class TestFamily1IdentityStillProjected:
    def test_official_name_and_whatsapp_phone_stay_known(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _customer(db, tenant.id, phone="966511111111", name=GENERIC_CUSTOMER)
        _seed_checkout(db, tenant=tenant, customer=customer, prep=_catalog_prep())
        with patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ):
            _, rebuilt = _ingest_maps_and_reproject(
                db, tenant_id=tenant.id, phone="966511111111",
            )
        facts = (rebuilt.get("reply_instruction") or {}).get("facts") or {}
        assert facts.get("customer_name_known") is True
        assert facts.get("phone_source") == "whatsapp"
        missing = set(rebuilt.get("missing_fields") or [])
        assert not (missing & _NAME_SLOTS)
        assert not (missing & _PHONE_SLOTS)


class TestCrossTenant:
    def test_second_tenant_perfume_checkout_same_contract(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db, name="متجر عطور تجريبي")
        phone = "966533333333"
        customer = _customer(db, tenant.id, phone=phone, name="نورة عبدالله")
        first, last = "نورة", "عبدالله"
        prep = _catalog_prep(
            customer_first_name=first,
            customer_last_name=last,
            customer_phone=phone,
            city="جدة",
            district="الشاطئ",
            line_items=[{
                "product_id": PERFUME_SKU,
                "product_retailer_id": PERFUME_SKU,
                "product_name": PERFUME_TITLE,
                "title": PERFUME_TITLE,
                "quantity": 1,
                "source": "whatsapp_native_catalog_order",
            }],
        )
        _seed_checkout(db, tenant=tenant, customer=customer, prep=prep)
        with patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ):
            _, rebuilt = _ingest_maps_and_reproject(
                db, tenant_id=tenant.id, phone=phone,
            )
        facts = (rebuilt.get("reply_instruction") or {}).get("facts") or {}
        assert facts.get("selected_product_id") == PERFUME_SKU
        assert facts.get("checkout_city") == "جدة"
        assert facts.get("active_checkout_name_known") is True
        assert facts.get("phone_source") == "whatsapp"
        assert rebuilt.get("next_missing_field") not in (_NAME_SLOTS | _PHONE_SLOTS)


class TestNoPhraseOrTenantLogic:
    def test_new_owner_has_no_phrase_regex_or_tenant_hooks(self) -> None:
        src = open(
            os.path.join(_BACKEND, "core", "address_ingest_post_persist.py"),
            encoding="utf-8",
        ).read()
        assert "tenant_id == 33" not in src
        assert "966542980511" not in src
        assert "966537970430" not in src
        assert "الاسم" not in src
        assert "الجوال" not in src
        assert "re.compile" not in src
        webhook = open(
            os.path.join(_BACKEND, "routers", "whatsapp_webhook.py"),
            encoding="utf-8",
        ).read()
        persist_idx = webhook.find("persist_checkout_location_outcome")
        reproject_idx = webhook.find("reproject_address_ingest_decision_after_persist")
        assert persist_idx > 0
        assert reproject_idx > persist_idx
        maybe_src = open(
            os.path.join(_BACKEND, "core", "order_flow.py"),
            encoding="utf-8",
        ).read()
        handle = maybe_src.split("def maybe_handle_wa_address_inbound", 1)[1]
        handle = handle.split("\ndef ", 1)[0]
        assert "compose_address_reply" not in handle
        assert "compose_after_persist" in handle

    def test_instruction_constraint_is_platform_slot_not_phrase_ban(self) -> None:
        instr = build_address_instruction(
            legacy_copy="تم",
            next_missing_field=None,
            missing_fields=[],
        )
        assert CONSTRAINT_RESPECT_PLATFORM_NEXT_SLOT in instr.constraints
        assert instr.facts["next_missing_field"] == "none"
        assert instr.facts["constrained_compose_decides_slot"] is False
        goal = compose_operational_expression_goal(instr)
        assert "next_missing_field is none" in goal

    def test_message_persist_reuses_state_manager(self) -> None:
        from core.address_ingest_post_persist import persist_address_ingest_turn_messages

        db, _ = _make_db()
        tenant = _seed_tenant(db)
        customer = _customer(db, tenant.id, phone="966511111111", name=GENERIC_CUSTOMER)
        _seed_checkout(db, tenant=tenant, customer=customer, prep=_catalog_prep())
        with patch("core.conversation_engine.StateManager.save_message") as save:
            persist_address_ingest_turn_messages(
                db,
                tenant_id=tenant.id,
                phone="966511111111",
                inbound_body=GENERIC_MAPS,
                outbound_body="ack",
            )
        assert save.call_count == 2
        directions = [c.kwargs["direction"] for c in save.call_args_list]
        assert "inbound" in directions
        assert "outbound" in directions
