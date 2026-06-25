"""Confirmed customer shipping snapshot + customer_addresses writer."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.customer_shipping_address_writer import (  # noqa: E402
    address_fingerprint,
    customer_address_to_snapshot,
    persist_customer_shipping_address_if_confirmed,
    sync_order_shipping_layers,
)
from core.order_context_builder import build_order_context_for_order  # noqa: E402
from core.order_context_prefill import MODE_CONFIRM, MODE_SKIP  # noqa: E402
from core.order_shipping_snapshot import (  # noqa: E402
    build_order_shipping_snapshot,
    shipping_fields_locked,
    shipping_snapshot_confirmed,
)
from models import Base, Conversation, Customer, CustomerAddress, Order, Tenant  # noqa: E402
from core.wa_order_editor import update_order_address  # noqa: E402
from services.nahla_order_bridge import nahla_wa_external_id, sync_nahla_wa_order  # noqa: E402


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


def _seed_tenant(db) -> Tenant:
    tenant = Tenant(name="T", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _customer(db, tenant_id: int) -> Customer:
    c = Customer(
        tenant_id=tenant_id,
        phone="+966500000001",
        normalized_phone="966500000001",
        name="Ali Ahmed",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _conversation(db, tenant_id: int, customer_id: int) -> Conversation:
    convo = Conversation(tenant_id=tenant_id, customer_id=customer_id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return convo


def test_order_bridge_copies_short_address_and_maps_to_customer_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "true")
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    convo = _conversation(db, tenant.id, customer.id)
    order_prep = {
        "line_items": [{"name": "P", "quantity": 1, "price": 100.0}],
        "total_price": 100.0,
        "city": "Riyadh",
        "short_address_code": "RRRD2929",
        "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
    }
    brain_state = {"stage": "ordering", "order_prep": order_prep}

    order = sync_nahla_wa_order(
        db,
        tenant_id=tenant.id,
        conversation=convo,
        brain_state=brain_state,
        order_prep=order_prep,
        trigger="test_shipping_sync",
        customer=customer,
    )
    db.commit()
    assert order is not None
    info = dict(order.customer_info or {})
    assert info.get("short_address_code") == "RRRD2929"
    assert info.get("google_maps_url") == "https://maps.google.com/?q=24.7,46.6"
    assert info.get("city") == "Riyadh"


def test_confirmed_short_address_persists_to_customer_addresses() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    snapshot = build_order_shipping_snapshot(
        order_prep={"city": "Riyadh", "short_address_code": "RRRD2929"},
    )
    persisted, row = persist_customer_shipping_address_if_confirmed(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        order_id=None,
        snapshot=snapshot,
        order_prep={"city": "Riyadh", "short_address_code": "RRRD2929"},
    )
    db.commit()
    assert persisted is True
    assert row is not None
    assert row.saudi_national_address == "RRRD2929"
    assert customer_address_to_snapshot(row)["short_address_code"] == "RRRD2929"


def test_confirmed_google_maps_persists_to_customer_addresses() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    url = "https://maps.google.com/?q=24.7,46.6"
    snapshot = build_order_shipping_snapshot(
        order_prep={"city": "Riyadh", "google_maps_url": url},
    )
    persisted, row = persist_customer_shipping_address_if_confirmed(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        order_id=None,
        snapshot=snapshot,
        order_prep={"city": "Riyadh", "google_maps_url": url},
    )
    db.commit()
    assert persisted is True
    assert row.google_maps_link == url


def test_whatsapp_location_pin_persists_to_customer_addresses() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    wa_loc = {"latitude": 24.7, "longitude": 46.6, "name": "Pin"}
    snapshot = build_order_shipping_snapshot(
        order_prep={
            "city": "Riyadh",
            "whatsapp_location": wa_loc,
            "latitude": "24.7",
            "longitude": "46.6",
        },
    )
    persisted, row = persist_customer_shipping_address_if_confirmed(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        order_id=None,
        snapshot=snapshot,
        order_prep={"city": "Riyadh", "latitude": "24.7", "longitude": "46.6"},
    )
    db.commit()
    assert persisted is True
    assert row.whatsapp_location == wa_loc
    assert row.lat == "24.7"
    assert row.lng == "46.6"


def test_known_previous_address_not_persisted_or_applied_without_confirmation() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    db.add(
        CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city="Jeddah",
            saudi_national_address="JEDD9988",
            google_maps_link="https://maps.google.com/?q=jeddah",
        )
    )
    db.commit()

    snapshot = build_order_shipping_snapshot(order_prep={"city": "Jeddah"})
    confirmed, _ = shipping_snapshot_confirmed(snapshot, order_prep={"city": "Jeddah"})
    assert confirmed is False

    persisted, _ = persist_customer_shipping_address_if_confirmed(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        order_id=None,
        snapshot=snapshot,
        order_prep={"city": "Jeddah"},
    )
    assert persisted is False

    convo = _conversation(db, tenant.id, customer.id)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        customer_info={"phone": "+966500000001"},
        extra_metadata={"lifecycle": "whatsapp_draft", "conversation_id": convo.id},
    )
    db.add(order)
    db.commit()
    ctx = build_order_context_for_order(db, tenant_id=tenant.id, order=order)
    assert ctx.known_previous_address is not None
    assert ctx.missing_fields_result is not None
    assert ctx.missing_fields_result.field_states["delivery_address"].mode == MODE_CONFIRM


def test_customer_confirmed_previous_address_promotes_and_persists() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    db.add(
        CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city="Jeddah",
            saudi_national_address="JEDD9988",
            google_maps_link="https://maps.google.com/?q=jeddah",
        )
    )
    db.commit()

    order_prep = {
        "city": "Jeddah",
        "short_address_code": "JEDD9988",
        "google_maps_url": "https://maps.google.com/?q=jeddah",
        "customer_confirmed_previous_address": True,
    }
    snapshot = build_order_shipping_snapshot(order_prep=order_prep)
    persisted, row = persist_customer_shipping_address_if_confirmed(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        order_id=1,
        snapshot=snapshot,
        order_prep=order_prep,
        extra_metadata={"customer_confirmed_previous_address": True},
    )
    db.commit()
    assert persisted is True
    assert row.saudi_national_address == "JEDD9988"


def test_merchant_shipping_lock_prevents_bridge_overwrite() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    convo = _conversation(db, tenant.id, customer.id)
    existing = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        customer_info={"city": "LockedCity", "phone": "+966500000001"},
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "conversation_id": convo.id,
            "merchant_shipping_locked": True,
        },
    )
    db.add(existing)
    db.commit()

    order_prep = {
        "line_items": [{"name": "P", "quantity": 1, "price": 50.0}],
        "city": "NewCity",
        "short_address_code": "NEWCODE1",
        "merchant_shipping_locked": True,
    }
    _, merged_info, _ = sync_order_shipping_layers(
        order_prep=order_prep,
        customer_info=dict(existing.customer_info or {}),
        extra_metadata=dict(existing.extra_metadata or {}),
    )
    assert merged_info.get("city") == "LockedCity"
    assert "NEWCODE1" not in (merged_info.get("short_address_code") or "")
    assert shipping_fields_locked(existing.extra_metadata, order_prep) is True


def test_customer_address_writer_idempotent() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    snapshot = build_order_shipping_snapshot(
        order_prep={"city": "Riyadh", "short_address_code": "RRRD2929"},
    )
    fp = address_fingerprint(customer_id=customer.id, snapshot=snapshot)
    assert fp

    persist_customer_shipping_address_if_confirmed(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        order_id=1,
        snapshot=snapshot,
        order_prep={"city": "Riyadh", "short_address_code": "RRRD2929"},
    )
    persist_customer_shipping_address_if_confirmed(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        order_id=2,
        snapshot=snapshot,
        order_prep={"city": "Riyadh", "short_address_code": "RRRD2929"},
    )
    db.commit()
    rows = (
        db.query(CustomerAddress)
        .filter_by(tenant_id=tenant.id, customer_id=customer.id)
        .all()
    )
    assert len(rows) == 1


def test_order_context_reads_persisted_customer_address_as_known_previous() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    convo = _conversation(db, tenant.id, customer.id)
    db.add(
        CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city="Riyadh",
            saudi_national_address="RRRD2929",
            google_maps_link="https://maps.google.com/?q=riyadh",
        )
    )
    db.commit()
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        customer_info={"phone": "+966500000001"},
        extra_metadata={"lifecycle": "whatsapp_draft", "conversation_id": convo.id},
    )
    db.add(order)
    db.commit()

    ctx = build_order_context_for_order(db, tenant_id=tenant.id, order=order)
    assert ctx.known_previous_address is not None
    assert ctx.known_previous_address.city == "Riyadh"


def test_missing_engine_previous_address_confirm_current_address_skip() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    convo = _conversation(db, tenant.id, customer.id)
    db.add(
        CustomerAddress(
            tenant_id=tenant.id,
            customer_id=customer.id,
            city="Jeddah",
            google_maps_link="https://maps.google.com/?q=jeddah",
        )
    )
    db.commit()

    order_only_previous = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        line_items=[{"name": "P", "quantity": 1, "price": 10.0}],
        customer_info={"phone": "+966500000001"},
        extra_metadata={"lifecycle": "whatsapp_draft", "conversation_id": convo.id},
    )
    db.add(order_only_previous)
    db.commit()
    ctx_prev = build_order_context_for_order(db, tenant_id=tenant.id, order=order_only_previous)
    assert ctx_prev.missing_fields_result.field_states["delivery_address"].mode == MODE_CONFIRM

    order_with_current = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id) + "-b",
        status="draft",
        source="whatsapp",
        line_items=[{"name": "P", "quantity": 1, "price": 10.0}],
        customer_info={
            "phone": "+966500000001",
            "city": "Riyadh",
            "short_address_code": "RRRD2929",
            "google_maps_url": "https://maps.google.com/?q=riyadh",
        },
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "conversation_id": convo.id,
            "short_address_code": "RRRD2929",
            "google_maps_url": "https://maps.google.com/?q=riyadh",
            "delivery_address_status": "accepted",
        },
    )
    db.add(order_with_current)
    db.commit()
    ctx_current = build_order_context_for_order(db, tenant_id=tenant.id, order=order_with_current)
    assert ctx_current.missing_fields_result.field_states["delivery_address"].mode == MODE_SKIP


def test_merchant_edit_persists_customer_address() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    order = Order(
        tenant_id=tenant.id,
        external_id="wa-merchant-edit",
        status="draft",
        source="whatsapp",
        customer_info={"phone": "+966500000001"},
        extra_metadata={"lifecycle": "whatsapp_draft"},
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    update_order_address(
        order,
        city="Riyadh",
        short_address_code="RRRD2929",
        google_maps_url="https://maps.google.com/?q=riyadh",
        db=db,
        tenant_id=tenant.id,
        customer=customer,
    )
    db.commit()
    rows = db.query(CustomerAddress).filter_by(tenant_id=tenant.id, customer_id=customer.id).all()
    assert len(rows) == 1
    assert rows[0].saudi_national_address == "RRRD2929"
