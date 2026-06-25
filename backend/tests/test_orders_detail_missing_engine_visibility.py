"""Order detail API — missing_fields_engine visibility and legacy divergence."""
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

from core.customer_identity_resolver import SOURCE_MERCHANT, STATUS_CUSTOMER_ENTERED  # noqa: E402
from core.order_context_builder import build_order_context_for_order  # noqa: E402
from core.order_context_prefill import (  # noqa: E402
    MODE_CONFIRM,
    MODE_SKIP,
    build_order_context_api_payload,
)
from models import Base, Conversation, Customer, Order, Tenant  # noqa: E402
from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: E402
    build_checkout_slot_fallback_reply,
)
from modules.ai.order_flow_v2.missing_fields import compute_v2_missing_fields  # noqa: E402
from routers.orders import _serialise_order  # noqa: E402
from services.nahla_order_bridge import nahla_wa_external_id  # noqa: E402


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


def _verified_customer(db, tenant_id: int, *, first: str = "هيثم", last: str = "الحارثي") -> Customer:
    c = Customer(
        tenant_id=tenant_id,
        phone="+966500000060",
        normalized_phone="966500000060",
        name=f"{first} {last}",
        extra_metadata={
            "customer_name_source": SOURCE_MERCHANT,
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
            "customer_name_confidence": 0.95,
        },
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _detail_payload(db, order: Order, tenant_id: int) -> dict:
    return _serialise_order(
        order,
        customer_lookup={},
        now=datetime.now(timezone.utc),
        detailed=True,
        db=db,
        tenant_id=tenant_id,
    )


def test_order_detail_payload_contains_missing_fields_engine_for_whatsapp_order() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    convo = Conversation(tenant_id=tenant.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        total="100.00 ر.س",
        line_items=[{"name": "P", "quantity": 1, "price": 100.0}],
        extra_metadata={"lifecycle": "whatsapp_draft", "conversation_id": convo.id},
    )
    db.add(order)
    db.commit()

    payload = _detail_payload(db, order, tenant.id)
    engine = payload.get("missing_fields_engine")
    assert engine is not None
    assert engine.get("available") is True
    assert "readiness_state" in engine
    assert "field_states" in engine
    assert "divergence_flags" in engine


def _proposed_only_customer(db, tenant_id: int) -> Customer:
    c = Customer(
        tenant_id=tenant_id,
        phone="+966500000099",
        normalized_phone="966500000099",
        name="",
        extra_metadata={
            "proposed_name": "WA Display",
            "customer_name_status": "proposed",
            "customer_name_confidence": 0.4,
        },
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_order_detail_engine_name_skip_from_order_customer_info_when_complete() -> None:
    """Order 60 pattern: WA proposed profile + complete order.customer_info names."""
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _proposed_only_customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="pending_customer_info",
        total="100.00 ر.س",
        line_items=[{"name": "P", "quantity": 1, "price": 100.0}],
        customer_info={
            "phone": "+966500000099",
            "first_name": "هيثم",
            "last_name": "الحارثي",
        },
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "conversation_id": convo.id,
            "missing_fields": ["customer_first_name", "customer_last_name", "city"],
        },
    )
    db.add(order)
    db.commit()

    payload = _detail_payload(db, order, tenant.id)
    engine = payload["missing_fields_engine"]
    prefill = payload["order_context_prefill"]

    assert engine.get("available") is True
    assert engine["missing_modes"]["name"] == MODE_SKIP
    assert engine["field_states"]["name"]["mode"] == MODE_SKIP
    assert engine["field_states"]["name"]["reason"] == "persisted_order_customer_name"
    assert prefill["shadow_missing_modes"]["name"] == MODE_SKIP
    assert prefill["identity"]["can_use_for_shipping_label"] is True
    assert prefill["identity"]["name_source"] == "order_customer_info"


def test_order_detail_engine_name_confirm_for_proposed_only() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _proposed_only_customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        total="100.00 ر.س",
        line_items=[{"name": "P", "quantity": 1, "price": 100.0}],
        customer_info={"phone": "+966500000099"},
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "conversation_id": convo.id,
            "missing_fields": ["customer_first_name", "customer_last_name", "city"],
        },
    )
    db.add(order)
    db.commit()

    payload = _detail_payload(db, order, tenant.id)
    engine = payload["missing_fields_engine"]
    prefill = payload["order_context_prefill"]

    assert engine["missing_modes"]["name"] == MODE_CONFIRM
    assert engine["field_states"]["name"]["mode"] == MODE_CONFIRM
    assert prefill["shadow_missing_modes"]["name"] == MODE_CONFIRM
    assert prefill["identity"]["can_use_for_shipping_label"] is False


def test_order_context_for_order_can_use_shipping_label_when_first_last_present() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _proposed_only_customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        line_items=[{"name": "P", "quantity": 1, "price": 50.0}],
        customer_info={
            "phone": "+966500000099",
            "first_name": "Ali",
            "last_name": "Ahmed",
        },
        extra_metadata={"lifecycle": "whatsapp_draft", "conversation_id": convo.id},
    )
    db.add(order)
    db.commit()

    ctx = build_order_context_for_order(db, tenant_id=tenant.id, order=order)
    api = build_order_context_api_payload(ctx)

    assert ctx.identity.can_use_for_shipping_label is True
    assert api["identity"]["can_use_for_shipping_label"] is True
    assert ctx.missing_fields_result is not None
    assert ctx.missing_fields_result.missing_modes["name"] == MODE_SKIP


def test_order_detail_engine_name_skip_when_customer_name_present() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    convo = Conversation(tenant_id=tenant.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        total="100.00 ر.س",
        line_items=[{"name": "P", "quantity": 1, "price": 100.0}],
        customer_info={
            "phone": "+966500000060",
            "first_name": "هيثم",
            "last_name": "الحارثي",
        },
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "conversation_id": convo.id,
            "customer_first_name": "هيثم",
            "customer_last_name": "الحارثي",
            "missing_fields": [
                "customer_first_name",
                "customer_last_name",
                "city",
                "delivery_address",
            ],
        },
    )
    db.add(order)
    db.commit()

    payload = _detail_payload(db, order, tenant.id)
    engine = payload["missing_fields_engine"]
    assert engine.get("available") is True
    assert engine["field_states"]["name"]["mode"] == MODE_SKIP
    assert engine["missing_modes"]["name"] == MODE_SKIP
    assert "customer_first_name" in (payload.get("confirm_blockers") or [])


def test_order_detail_engine_when_source_unset_but_nahla_wa_external_id() -> None:
    """Regression: WA bridge orders may lack source=whatsapp on the Order row."""
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    convo = Conversation(tenant_id=tenant.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="pending_customer_info",
        total="100.00 ر.س",
        line_items=[{"name": "P", "quantity": 1, "price": 100.0}],
        customer_info={
            "phone": "+966500000060",
            "first_name": "هيثم",
            "last_name": "الحارثي",
        },
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "conversation_id": convo.id,
            "missing_fields": ["customer_first_name", "customer_last_name", "city"],
        },
    )
    db.add(order)
    db.commit()

    payload = _detail_payload(db, order, tenant.id)
    assert payload.get("missing_fields_engine") is not None
    assert payload["missing_fields_engine"].get("available") is True
    assert "readiness_state" in payload["missing_fields_engine"]


def test_order_detail_engine_divergence_when_legacy_blockers_stale() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id)
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    order = Order(
        tenant_id=tenant.id,
        external_id=nahla_wa_external_id(tenant.id, convo.id),
        status="draft",
        source="whatsapp",
        total="100.00 ر.س",
        line_items=[{"name": "P", "quantity": 1, "price": 100.0}],
        customer_info={
            "phone": "+966500000060",
            "first_name": "هيثم",
            "last_name": "الحارثي",
        },
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "conversation_id": convo.id,
            "missing_fields": ["customer_first_name", "customer_last_name", "city"],
        },
    )
    db.add(order)
    db.commit()

    payload = _detail_payload(db, order, tenant.id)
    flags = payload["missing_fields_engine"]["divergence_flags"]
    assert flags.get("name_divergence") is True
    assert flags.get("confirm_blockers_stale") is True
    assert flags.get("confirm_blockers_name_stale") is True


def test_existing_order_without_engine_metadata_computes_engine_on_serialize() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    order = Order(
        tenant_id=tenant.id,
        external_id="wa-old-no-engine",
        status="draft",
        source="whatsapp",
        total="50.00 ر.س",
        line_items=[{"name": "Legacy", "quantity": 1, "price": 50.0}],
        customer_info={"phone": "+966511111111"},
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "missing_fields": ["city", "delivery_address"],
        },
    )
    db.add(order)
    db.commit()

    payload = _detail_payload(db, order, tenant.id)
    engine = payload.get("missing_fields_engine")
    assert engine is not None
    assert "city" in engine["missing_fields"]
    assert "missing_fields_engine" not in (order.extra_metadata or {})


def test_catalog_order_after_engine_enabled_next_missing_is_city_or_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORDER_MISSING_FIELDS_ENGINE_ENABLED", "true")

    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _verified_customer(db, tenant.id, first="Ali", last="Ahmed")
    convo = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="open")
    db.add(convo)
    db.commit()
    db.refresh(convo)

    order_prep = {
        "line_items": [{"name": "Catalog Item", "quantity": 2, "price": 75.0}],
        "total": 150.0,
        "customer_first_name": "Ali",
        "customer_last_name": "Ahmed",
        "missing_fields": ["product", "customer_first_name", "city"],
    }
    brain_state = {"stage": "ordering", "order_prep": order_prep}

    missing = compute_v2_missing_fields(
        order_prep,
        brain_state=brain_state,
        whatsapp_phone="+966500000060",
        db=db,
        tenant_id=tenant.id,
        conversation=convo,
    )
    assert "product" not in missing
    assert missing[0] in {"city", "delivery_address", "customer_name"}

    bs = brain_state
    op = order_prep

    class _State:
        brain_state = bs
        order_prep = op

    reply = build_checkout_slot_fallback_reply(state=_State(), inbound_text="")
    assert reply is not None
    assert "المنتج" not in reply
    assert "الكمية" not in reply
