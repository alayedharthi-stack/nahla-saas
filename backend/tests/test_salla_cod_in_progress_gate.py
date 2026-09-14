"""Regression coverage for Salla storefront COD orders awaiting confirmation."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _path in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402
from core.salla_order_fidelity import extract_salla_payment_facts  # noqa: E402
from database.models import AutomationEvent, Base, Order, Tenant  # noqa: E402
from services.store_sync import StoreSyncService  # noqa: E402
from services.store_sync import _normalise_order  # noqa: E402
from services.salla_orders_poller import _emit_for_order  # noqa: E402
from store_adapters.salla_adapter import SallaAdapter  # noqa: E402
from store_integration.models import NormalizedOrder  # noqa: E402
from store_adapters.salla_lifecycle import (  # noqa: E402
    normalize_salla_lifecycle_business_intent,
    salla_cod_requires_customer_confirmation,
)


def _live_created(order: dict) -> dict:
    return {
        **order,
        "lifecycle_observation": "live_webhook",
        "lifecycle_source_event": "order.created",
    }


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb_for_sqlite(target, connection, **kw):
    for table in target.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()


def _run(coro):
    return asyncio.run(coro)


def test_real_salla_shape_keeps_cod_method_separate_from_waiting_state():
    raw = {
        "id": 605882301,
        "reference_id": "ORD-GENERIC-101",
        "status": {"slug": "in_progress", "name": "قيد التنفيذ"},
        "payment": {"method": "waiting"},
        "payment_method": {"slug": "cash_on_delivery", "name": "الدفع عند الاستلام"},
        "amounts": {"total": {"amount": 174, "currency": "SAR"}},
    }

    facts = extract_salla_payment_facts(raw)
    normalized = _normalise_order(raw)

    assert facts == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }
    assert normalized["payment_method"] == "cod"
    assert normalized["payment_status"] == "waiting"
    assert normalized["is_cod"] is True


def test_waiting_method_uses_cod_payment_action_label_as_selected_method():
    raw = {
        "id": 605882302,
        "reference_id": "ORD-GENERIC-102",
        "status": {"slug": "in_progress"},
        "payment_method": "waiting",
        "payment_actions": {
            "refund_action": {"payment_method_label": "الدفع عند الاستلام"},
            "remaining_action": {"payment_method_label": "الدفع عند الاستلام"},
        },
        "accepted_payment_methods": ["bank", "cod"],
        "amounts": {"total": {"amount": 174, "currency": "SAR"}},
    }

    assert extract_salla_payment_facts(raw) == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }


def test_waiting_method_uses_singleton_cod_acceptance_and_requires_context_for_mixed_list():
    singleton = {
        "payment_method": "waiting",
        "accepted_payment_methods": ["cash_on_delivery"],
    }
    mixed = {
        "payment_method": "waiting",
        "accepted_payment_methods": ["bank", "cash_on_delivery"],
    }
    storefront_cod = {
        "status": {"slug": "in_progress"},
        "payment_method": "waiting",
        "accepted_payment_methods": ["bank", "cash_on_delivery"],
    }

    assert extract_salla_payment_facts(singleton) == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }
    assert extract_salla_payment_facts(mixed) == {
        "payment_method": "waiting",
        "payment_status": "waiting",
        "is_cod": False,
    }
    assert extract_salla_payment_facts(storefront_cod) == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }


def test_nested_payment_shape_and_list_actions_preserve_cod_evidence():
    raw = {
        "status": {"slug": "in_progress"},
        "payment": {
            "method": "waiting",
            "accepted_methods": ["bank", "cod"],
            "actions": [
                {"payment_method_label": "الدفع عند الاستلام"},
            ],
        },
    }

    assert extract_salla_payment_facts(raw) == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }


def test_mixed_store_methods_do_not_imply_cod_outside_unpaid_in_progress_state():
    raw = {
        "status": {"slug": "under_review"},
        "payment_method": "waiting",
        "accepted_payment_methods": ["bank", "cod"],
    }

    assert extract_salla_payment_facts(raw) == {
        "payment_method": "waiting",
        "payment_status": "waiting",
        "is_cod": False,
    }


def test_salla_waiting_bank_shape_does_not_misread_payment_action_state_label():
    raw = {
        "payment_method": "waiting",
        "accepted_payment_methods": ["bank"],
        "payment_actions": {
            "refund_action": {"payment_method_label": "بإنتظار الدفع"},
            "remaining_action": {"payment_method_label": "بإنتظار الدفع"},
        },
    }

    assert extract_salla_payment_facts(raw) == {
        "payment_method": "bank",
        "payment_status": "waiting",
        "is_cod": False,
    }


def test_positive_salla_cod_fee_is_strong_cod_evidence():
    raw = {
        "payment_method": "waiting",
        "accepted_payment_methods": ["bank", "cod"],
        "amounts": {"cash_on_delivery": {"amount": 5, "currency": "SAR"}},
    }

    assert extract_salla_payment_facts(raw) == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }


def test_salla_adapter_preserves_payment_facts_for_periodic_sync():
    raw = {
        "id": 605882303,
        "reference_id": "ORD-GENERIC-103",
        "status": {"slug": "in_progress"},
        "payment_method": "waiting",
        "accepted_payment_methods": ["cod"],
        "customer": {"name": "Customer", "mobile": "966500000000"},
        "amounts": {"total": {"amount": 174, "currency": "SAR"}},
    }

    adapter = object.__new__(SallaAdapter)
    adapter_order = adapter._normalize_order(raw, None)
    normalized = _normalise_order(adapter_order)

    assert adapter_order.payment_method == "cod"
    assert adapter_order.payment_status == "waiting"
    assert adapter_order.is_cod is True
    assert normalized["payment_method"] == "cod"
    assert normalized["payment_status"] == "waiting"
    assert normalized["is_cod"] is True


def test_live_in_progress_cod_prompts_customer_and_withholds_final_confirmation():
    normalized = _live_created({
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    })

    assert salla_cod_requires_customer_confirmation("in_progress", normalized) is True
    assert normalize_salla_lifecycle_business_intent(
        None, "in_progress", normalized
    ) == BusinessIntent.COD_CONFIRMATION


def test_authoritative_created_webhook_owns_cod_after_poller_inserted_same_state():
    normalized = _live_created({
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    })

    assert normalize_salla_lifecycle_business_intent(
        "in_progress", "in_progress", normalized
    ) == BusinessIntent.COD_CONFIRMATION


def test_lifecycle_owned_poll_import_does_not_emit_legacy_event_without_customer():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        tenant = Tenant(name="Generic Store", is_active=True)
        db.add(tenant)
        db.commit()
        adapter = MagicMock()
        adapter.platform = "salla"
        adapter.get_orders = AsyncMock(return_value=[NormalizedOrder(
            id="7001",
            reference_id="REF-7001",
            status="in_progress",
            total=114,
            currency="SAR",
            payment_method="cod",
            payment_status="waiting",
            is_cod=True,
            customer_name="Customer",
            customer_phone="966500000000",
            source="salla",
        )])
        service = StoreSyncService(db, tenant.id, adapter=adapter)

        with patch(
            "services.store_sync._lifecycle_dispatch_owns_tenant",
            return_value=True,
        ), patch(
            "services.store_sync._handle_external_lifecycle_transition_best_effort",
            new_callable=AsyncMock,
        ):
            assert _run(service.sync_orders(triggered_by="salla_orders_poller")) == 1

        order = db.query(Order).filter_by(tenant_id=tenant.id, external_id="7001").one()
        assert db.query(AutomationEvent).filter_by(tenant_id=tenant.id).count() == 0
        assert order.extra_metadata.get("notifications_emitted") is not True
        assert (
            order.extra_metadata["legacy_notifications_suppressed_by"]
            == "lifecycle_dispatch_owner"
        )
    finally:
        db.close()
        engine.dispose()


def test_safety_poller_defers_to_lifecycle_webhook_owner(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        tenant = Tenant(name="Generic Store", is_active=True)
        db.add(tenant)
        db.flush()
        order = Order(
            tenant_id=tenant.id,
            external_id="7002",
            external_order_number="REF-7002",
            status="in_progress",
            total="114",
            customer_info={"mobile": "966500000000"},
            is_abandoned=False,
            extra_metadata={"payment_method": "cod", "is_cod": True},
        )
        db.add(order)
        db.commit()
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv(
            "COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST",
            str(tenant.id),
        )

        assert _emit_for_order(db, tenant.id, order) is False
        assert db.query(AutomationEvent).filter_by(tenant_id=tenant.id).count() == 0
        assert order.extra_metadata.get("notifications_emitted") is not True
        assert (
            order.extra_metadata["legacy_notifications_suppressed_by"]
            == "lifecycle_dispatch_owner"
        )
    finally:
        db.close()
        engine.dispose()


def test_safety_poller_does_not_duplicate_webhook_cod_prompt():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        tenant = Tenant(name="Generic Store", is_active=True)
        db.add(tenant)
        db.flush()
        order = Order(
            tenant_id=tenant.id,
            external_id="7003",
            external_order_number="REF-7003",
            status="in_progress",
            total="174",
            customer_info={"mobile": "966500000000"},
            is_abandoned=False,
            extra_metadata={
                "payment_method": "cod",
                "is_cod": True,
                "cod_webhook_triggered": True,
            },
        )
        db.add(order)
        db.commit()

        assert _emit_for_order(db, tenant.id, order) is False
        assert db.query(AutomationEvent).filter_by(tenant_id=tenant.id).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_generic_non_cod_in_progress_order_keeps_existing_confirmation_behavior():
    normalized = _live_created({
        "payment_method": "credit_card",
        "payment_status": "paid",
        "is_cod": False,
    })

    assert salla_cod_requires_customer_confirmation("in_progress", normalized) is False
    assert normalize_salla_lifecycle_business_intent(
        None, "in_progress", normalized
    ) == BusinessIntent.ORDER_CONFIRMED


def test_cod_under_review_is_no_longer_awaiting_customer_confirmation():
    normalized = _live_created({
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    })

    assert salla_cod_requires_customer_confirmation("under_review", normalized) is False
    # The button handler owns the immediate final confirmation. A later Salla
    # status webhook must not duplicate that already-sent message.
    assert normalize_salla_lifecycle_business_intent(
        "in_progress", "under_review", normalized
    ) is None
