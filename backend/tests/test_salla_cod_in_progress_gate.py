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
from database.models import (  # noqa: E402
    AutomationEvent,
    Base,
    CommerceLifecycleNotificationLedger,
    Customer,
    Order,
    Tenant,
    WhatsAppTemplate,
)
from core.merchant_capabilities import MerchantCapabilities  # noqa: E402
from services.store_sync import StoreSyncService  # noqa: E402
from services.store_sync import _normalise_order  # noqa: E402
from services.salla_orders_poller import _emit_for_order  # noqa: E402
from store_adapters.salla_adapter import SallaAdapter  # noqa: E402
from store_integration.models import NormalizedOrder  # noqa: E402
from store_adapters.salla_lifecycle import (  # noqa: E402
    normalize_salla_lifecycle_business_intent,
    salla_cod_requires_customer_confirmation,
    salla_payment_fidelity_pending,
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


def _race_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    tenant = Tenant(name="Race Store", is_active=True)
    db.add(tenant)
    db.flush()
    customer = Customer(
        tenant_id=tenant.id,
        phone="+966500000001",
        normalized_phone="966500000001",
        name="Customer",
    )
    db.add(customer)
    db.commit()
    return db, engine, tenant, customer


def test_store_cod_template_context_binds_only_pending_order_and_customer():
    from services.cod_confirmation import resolve_verified_structured_cod_control

    db, engine, tenant, _customer = _race_db()
    try:
        order = Order(
            tenant_id=tenant.id,
            external_id="salla-shirt-101",
            external_order_number="ORDER-SHIRT-101",
            status="in_progress",
            total="174",
            customer_info={"phone": "+966500000001"},
            extra_metadata={
                "payment_method": "cod",
                "nahla_cod_confirmation_sent": True,
                "nahla_cod_confirmation_origin": "external_store",
                "nahla_cod_confirmation_wamid": "wamid.cod.shirt",
            },
        )
        db.add(order)
        db.commit()
        bound = resolve_verified_structured_cod_control(
            db, tenant_id=tenant.id, customer_phone="966500000001",
            button_payload="provider-default", button_text="تأكيد الطلب",
            context_wamid="wamid.cod.shirt",
        )
        assert bound == f"nahla_cod_confirm:{order.id}"
        assert resolve_verified_structured_cod_control(
            db, tenant_id=tenant.id, customer_phone="966500000999",
            button_payload="provider-default", button_text="تأكيد الطلب",
            context_wamid="wamid.cod.shirt",
        ) is None
        order.status = "under_review"
        db.commit()
        assert resolve_verified_structured_cod_control(
            db, tenant_id=tenant.id, customer_phone="966500000001",
            button_payload="provider-default", button_text="تأكيد الطلب",
            context_wamid="wamid.cod.shirt",
        ) is None
    finally:
        db.close()
        engine.dispose()


def _cod_created_payload(external_id: str, reference_id: str) -> dict:
    return {
        "id": external_id,
        "reference_id": reference_id,
        "status": {"slug": "in_progress"},
        "payment_method": "waiting",
        "accepted_payment_methods": ["cod"],
        "is_pending_payment": True,
        "customer": {"name": "Customer", "mobile": "+966500000001"},
        "amounts": {
            "total": {"amount": 174, "currency": "SAR"},
            "cash_on_delivery": {"amount": 0, "currency": "SAR"},
        },
        "items": [],
    }


def _stub_customer_intelligence(service, customer):
    service._customer_intelligence.upsert_customer_from_order = MagicMock(
        return_value=customer
    )
    service._customer_intelligence.recompute_profile_for_customer = MagicMock()


def test_webhook_first_then_poller_emits_one_cod_prompt_and_no_final_confirmation():
    db, engine, tenant, customer = _race_db()
    try:
        service = StoreSyncService(db, tenant.id)
        _stub_customer_intelligence(service, customer)
        payload = _cod_created_payload("race-webhook-first", "REF-WEBHOOK-FIRST")

        with patch(
            "services.store_sync._lifecycle_dispatch_owns_tenant",
            return_value=False,
        ), patch(
            "services.store_sync._handle_external_lifecycle_transition_best_effort",
            new_callable=AsyncMock,
        ):
            _run(service.handle_order_webhook(
                payload, webhook_event_type="order.created"
            ))
            # A duplicate webhook and the safety poller must both defer to the
            # persisted webhook ownership stamp.
            _run(service.handle_order_webhook(
                payload, webhook_event_type="order.created"
            ))

        order = db.query(Order).filter_by(
            tenant_id=tenant.id, external_id="race-webhook-first"
        ).one()
        assert _emit_for_order(db, tenant.id, order) is False
        event_types = [
            row.event_type
            for row in db.query(AutomationEvent).filter_by(tenant_id=tenant.id)
        ]
        assert event_types.count("order_cod_pending") == 1
        assert event_types.count("order_notifications") == 0
        cod_event = db.query(AutomationEvent).filter_by(
            tenant_id=tenant.id, event_type="order_cod_pending"
        ).one()
        assert cod_event.customer_id == customer.id
        assert cod_event.payload["message_type"] == "initial_confirmation"
        assert order.extra_metadata["cod_webhook_triggered"] is True
    finally:
        db.close()
        engine.dispose()


def test_poller_first_defers_ambiguous_payment_then_webhook_claims_cod_once():
    db, engine, tenant, customer = _race_db()
    try:
        adapter = MagicMock()
        adapter.platform = "salla"
        adapter.get_orders = AsyncMock(return_value=[NormalizedOrder(
            id="race-poller-first",
            reference_id="REF-POLLER-FIRST",
            status="in_progress",
            total=174,
            currency="SAR",
            payment_method="waiting",
            payment_status="waiting",
            is_cod=False,
            customer_name="Customer",
            customer_phone="+966500000001",
            source="salla",
        )])
        service = StoreSyncService(db, tenant.id, adapter=adapter)
        _stub_customer_intelligence(service, customer)

        with patch(
            "services.store_sync._lifecycle_dispatch_owns_tenant",
            return_value=False,
        ), patch(
            "services.store_sync._handle_external_lifecycle_transition_best_effort",
            new_callable=AsyncMock,
        ):
            assert _run(service.sync_orders(
                triggered_by="salla_orders_poller"
            )) == 1

            order = db.query(Order).filter_by(
                tenant_id=tenant.id, external_id="race-poller-first"
            ).one()
            assert order.extra_metadata[
                "notifications_withheld_payment_fidelity"
            ] is True
            assert order.extra_metadata.get("notifications_emitted") is not True
            assert db.query(AutomationEvent).count() == 0
            assert _emit_for_order(db, tenant.id, order) is False

            payload = _cod_created_payload(
                "race-poller-first", "REF-POLLER-FIRST"
            )
            _run(service.handle_order_webhook(
                payload, webhook_event_type="order.created"
            ))
            _run(service.handle_order_webhook(
                payload, webhook_event_type="order.created"
            ))

        db.refresh(order)
        event_types = [row.event_type for row in db.query(AutomationEvent).all()]
        assert event_types.count("order_cod_pending") == 1
        assert event_types.count("order_notifications") == 0
        cod_event = db.query(AutomationEvent).filter_by(
            event_type="order_cod_pending"
        ).one()
        assert cod_event.customer_id == customer.id
        assert cod_event.payload["message_type"] == "initial_confirmation"
        assert order.extra_metadata["cod_webhook_triggered"] is True
        assert order.extra_metadata.get(
            "notifications_withheld_payment_fidelity"
        ) is not True
        assert _emit_for_order(db, tenant.id, order) is False
    finally:
        db.close()
        engine.dispose()


def test_webhook_proven_cod_is_not_downgraded_by_waiting_poller_snapshot():
    db, engine, tenant, customer = _race_db()
    try:
        webhook_service = StoreSyncService(db, tenant.id)
        _stub_customer_intelligence(webhook_service, customer)
        with patch(
            "services.store_sync._lifecycle_dispatch_owns_tenant",
            return_value=False,
        ), patch(
            "services.store_sync._handle_external_lifecycle_transition_best_effort",
            new_callable=AsyncMock,
        ):
            _run(webhook_service.handle_order_webhook(
                _cod_created_payload("cod-then-waiting", "REF-COD-THEN-WAITING"),
                webhook_event_type="order.created",
            ))

        order = db.query(Order).filter_by(
            tenant_id=tenant.id, external_id="cod-then-waiting"
        ).one()
        assert order.extra_metadata["payment_method"] == "cod"

        adapter = MagicMock()
        adapter.platform = "salla"
        adapter.get_orders = AsyncMock(return_value=[NormalizedOrder(
            id="cod-then-waiting",
            reference_id="REF-COD-THEN-WAITING",
            status="in_progress",
            total=174,
            currency="SAR",
            payment_method="waiting",
            payment_status="waiting",
            is_cod=False,
            customer_name="Customer",
            customer_phone="+966500000001",
            source="salla",
        )])
        poller_service = StoreSyncService(db, tenant.id, adapter=adapter)
        with patch(
            "services.store_sync._handle_external_lifecycle_transition_best_effort",
            new_callable=AsyncMock,
        ):
            assert _run(poller_service.sync_orders(
                triggered_by="salla_orders_poller"
            )) == 1

        db.refresh(order)
        assert order.extra_metadata["payment_method"] == "cod"
        assert order.extra_metadata["is_cod"] is True
        assert order.extra_metadata["cod_webhook_triggered"] is True
        assert db.query(AutomationEvent).filter_by(
            event_type="order_cod_pending"
        ).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_waiting_non_cod_order_is_not_promoted_without_prior_cod_proof():
    from services.store_sync import _merge_order_extra_metadata

    merged = _merge_order_extra_metadata(
        {"payment_method": "credit_card", "is_cod": False},
        {"payment_method": "waiting", "payment_status": "waiting"},
    )
    assert (merged["payment_method"], merged["is_cod"]) == ("waiting", False)


def test_paid_non_cod_poller_order_keeps_single_final_confirmation_event():
    db, engine, tenant, customer = _race_db()
    try:
        adapter = MagicMock()
        adapter.platform = "salla"
        adapter.get_orders = AsyncMock(return_value=[NormalizedOrder(
            id="paid-card-order",
            reference_id="REF-PAID-CARD",
            status="in_progress",
            total=174,
            currency="SAR",
            payment_method="credit_card",
            payment_status="paid",
            is_cod=False,
            customer_name="Customer",
            customer_phone="+966500000001",
            source="salla",
        )])
        service = StoreSyncService(db, tenant.id, adapter=adapter)
        _stub_customer_intelligence(service, customer)

        with patch(
            "services.store_sync._lifecycle_dispatch_owns_tenant",
            return_value=False,
        ), patch(
            "services.store_sync._handle_external_lifecycle_transition_best_effort",
            new_callable=AsyncMock,
        ):
            assert _run(service.sync_orders(
                triggered_by="salla_orders_poller"
            )) == 1

        order = db.query(Order).filter_by(
            tenant_id=tenant.id, external_id="paid-card-order"
        ).one()
        assert _emit_for_order(db, tenant.id, order) is False
        events = db.query(AutomationEvent).all()
        assert [event.event_type for event in events] == ["order_notifications"]
        assert events[0].customer_id == customer.id
    finally:
        db.close()
        engine.dispose()


def test_unpaid_in_progress_waiting_method_requires_payment_fidelity():
    ambiguous = {
        "payment_method": "waiting",
        "payment_status": "waiting",
        "is_cod": False,
    }
    assert salla_payment_fidelity_pending("in_progress", ambiguous) is True
    assert salla_payment_fidelity_pending(
        "in_progress", {**ambiguous, "payment_method": "bank"}
    ) is False
    assert salla_payment_fidelity_pending(
        "in_progress", {**ambiguous, "is_cod": True}
    ) is False
    assert salla_payment_fidelity_pending(
        "in_progress", {"payment_method": "", "payment_status": "paid"}
    ) is False


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


def _lifecycle_test_capabilities() -> MerchantCapabilities:
    return MerchantCapabilities(
        has_external_store=True,
        supports_external_checkout=True,
        supports_external_coupons=False,
        supports_whatsapp_orders=True,
        supports_nahla_orders=False,
        supports_bank_transfer=False,
        supports_cod=True,
        has_whatsapp_catalog=False,
        has_external_tracking=True,
        has_nahla_tracking=False,
        has_payment_link=True,
    )


def test_cod_template_startup_gap_recovers_on_later_poll_once(monkeypatch):
    """A blocked live COD transition is re-evaluated by a later poll."""
    db, engine, tenant, _customer = _race_db()
    try:
        phone = "+966500000001"
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv(
            "COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", str(tenant.id)
        )
        monkeypatch.setenv(
            "COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", phone
        )

        order = Order(
            tenant_id=tenant.id,
            external_id="startup-gap-cod",
            external_order_number="REF-STARTUP-GAP",
            status="in_progress",
            total="174",
            customer_name="Customer",
            customer_info={"phone": phone},
            is_abandoned=False,
            extra_metadata={
                "payment_method": "cod",
                "payment_status": "waiting",
                "is_cod": True,
                "legacy_notifications_suppressed": True,
            },
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        approved = MagicMock()
        approved.id = 440
        approved.name = "nahla_cod_confirmation_r3_3275d1"
        approved.language = "ar"
        approved.revision = 3
        approved.components = []
        template_state = {"approved": False}

        def _resolve_template(*_args, **_kwargs):
            return approved if template_state["approved"] else None

        adapter = MagicMock()
        adapter.platform = "salla"
        adapter.update_order_status = AsyncMock()
        adapter.get_orders = AsyncMock(return_value=[NormalizedOrder(
            id="startup-gap-cod",
            reference_id="REF-STARTUP-GAP",
            status="in_progress",
            total=174,
            currency="SAR",
            payment_method="cod",
            payment_status="waiting",
            is_cod=True,
            customer_name="Customer",
            customer_phone=phone,
            source="salla",
        )])
        service = StoreSyncService(db, tenant.id, adapter=adapter)

        from core.commerce_lifecycle.dispatch import (  # noqa: PLC0415
            dispatch_external_lifecycle_notification,
        )

        provider_send = AsyncMock(
            return_value=("sent", {"wa_message_id": "wamid.startup-gap"})
        )
        brain = AsyncMock(side_effect=AssertionError("Brain must not run"))
        with patch(
            "core.merchant_capabilities.resolve_merchant_capabilities",
            return_value=_lifecycle_test_capabilities(),
        ), patch(
            "core.commerce_lifecycle.order_updates.evaluate_order_update_delivery",
            return_value=(True, None),
        ), patch(
            "core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send",
            side_effect=_resolve_template,
        ), patch(
            "core.automation_engine.send_lifecycle_whatsapp_template",
            provider_send,
        ), patch(
            "services.merchant_brain_turn.evaluate_live_merchant_brain_turn",
            brain,
        ):
            blocked = _run(dispatch_external_lifecycle_notification(
                db,
                tenant_id=tenant.id,
                order=order,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="in_progress",
                normalized_order=_live_created({
                    "external_id": order.external_id,
                    "external_order_number": order.external_order_number,
                    "status": "in_progress",
                    "total": order.total,
                    "customer_name": order.customer_name,
                    "customer_phone": phone,
                    "payment_method": "cod",
                    "payment_status": "waiting",
                    "is_cod": True,
                }),
                raw_payload=None,
            ))
            assert blocked.reason_code == "no_approved_template"
            assert provider_send.await_count == 0
            assert order.extra_metadata.get("nahla_cod_confirmation_sent") is not True
            assert db.query(AutomationEvent).count() == 0

            template_state["approved"] = True
            assert _run(service.sync_orders(
                triggered_by="salla_orders_poller"
            )) == 1
            assert provider_send.await_count == 1
            sent_payload = provider_send.await_args.args[4]
            assert sent_payload["order_id"] == str(order.id)
            assert sent_payload["order_internal_id"] == str(order.id)
            db.refresh(order)
            assert order.extra_metadata["nahla_cod_confirmation_sent"] is True
            assert order.extra_metadata["nahla_cod_confirmation_sent_at"]
            assert (
                order.extra_metadata["nahla_cod_confirmation_wamid"]
                == "wamid.startup-gap"
            )

            assert _run(service.sync_orders(
                triggered_by="salla_orders_poller"
            )) == 1

        assert provider_send.await_count == 1
        assert adapter.update_order_status.await_count == 0
        assert brain.await_count == 0
        assert db.query(AutomationEvent).count() == 0
        rows = db.query(CommerceLifecycleNotificationLedger).all()
        assert len(rows) == 1
        assert rows[0].business_intent == BusinessIntent.COD_CONFIRMATION.value
        assert rows[0].send_state == "sent"
        assert rows[0].provider_message_id == "wamid.startup-gap"
    finally:
        db.close()
        engine.dispose()


def test_cod_initial_recovery_ignores_non_cod_confirmed_and_sent(monkeypatch):
    from core.commerce_lifecycle.cod_initial_recovery import (  # noqa: PLC0415
        reconcile_missing_initial_cod_confirmation,
    )

    db, engine, tenant, _customer = _race_db()
    try:
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv(
            "COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", str(tenant.id)
        )
        cases = (
            Order(
                tenant_id=tenant.id,
                external_id="non-cod",
                status="in_progress",
                customer_info={"mobile": "+966500000001"},
                is_abandoned=False,
                extra_metadata={"payment_method": "credit_card", "is_cod": False},
            ),
            Order(
                tenant_id=tenant.id,
                external_id="confirmed-cod",
                status="under_review",
                customer_info={"mobile": "+966500000001"},
                is_abandoned=False,
                extra_metadata={"payment_method": "cod", "is_cod": True},
            ),
            Order(
                tenant_id=tenant.id,
                external_id="sent-cod",
                status="in_progress",
                customer_info={"mobile": "+966500000001"},
                is_abandoned=False,
                extra_metadata={
                    "payment_method": "cod",
                    "is_cod": True,
                    "nahla_cod_confirmation_sent": True,
                },
            ),
        )
        db.add_all(cases)
        db.commit()

        reasons = [
            _run(reconcile_missing_initial_cod_confirmation(
                db, tenant_id=tenant.id, order=row
            )).reason_code
            for row in cases
        ]
        assert reasons == ["cod_not_proven", "status_not_pending", "already_sent_stamp"]
        assert db.query(CommerceLifecycleNotificationLedger).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_cod_initial_recovery_repairs_approved_template_outside_strict_slot(
    monkeypatch,
):
    """Recovery re-binds the approved COD template before strict dispatch."""
    from core.commerce_lifecycle.cod_initial_recovery import (  # noqa: PLC0415
        reconcile_missing_initial_cod_confirmation,
    )

    db, engine, tenant, customer = _race_db()
    try:
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv(
            "COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", str(tenant.id)
        )
        order = Order(
            tenant_id=tenant.id,
            customer_id=customer.id,
            external_id="rebind-cod",
            external_order_number="REF-REBIND-COD",
            status="in_progress",
            customer_info={"mobile": customer.phone},
            is_abandoned=False,
            extra_metadata={
                "payment_method": "cod",
                "payment_status": "waiting",
                "is_cod": True,
                "cod_webhook_triggered": True,
            },
        )
        approved = WhatsAppTemplate(
            tenant_id=tenant.id,
            name="nahla_cod_confirmation_r3_3275d1",
            language="ar",
            category="UTILITY",
            status="APPROVED",
            components=[],
            service_key="cod_confirmation",
            step_number=1,
            revision=3,
            is_active=False,
            is_hidden=True,
        )
        reminder = WhatsAppTemplate(
            tenant_id=tenant.id,
            name="nahla_cod_reminder_before_shipping_e29c",
            language="ar",
            category="UTILITY",
            status="APPROVED",
            components=[],
            service_key="cod_confirmation",
            nahla_source_key="cod_reminder_before_shipping",
            step_number=None,
            revision=1,
            is_active=False,
            is_hidden=False,
        )
        db.add_all([order, approved, reminder])
        db.commit()

        dispatch_result = MagicMock(
            dispatched=True,
            duplicate=False,
            reason_code=None,
            outcome="sent",
            ledger_id=812,
        )
        dispatch = AsyncMock(return_value=dispatch_result)
        with patch(
            "core.commerce_lifecycle.dispatch.dispatch_external_lifecycle_notification",
            dispatch,
        ):
            result = _run(reconcile_missing_initial_cod_confirmation(
                db,
                tenant_id=tenant.id,
                order=order,
            ))

        assert result.attempted is True
        assert result.sent is True
        assert result.ledger_id == 812
        db.refresh(approved)
        assert approved.step_number is None
        assert approved.is_active is True
        assert approved.is_hidden is False
        db.refresh(reminder)
        assert reminder.is_active is False
        dispatch.assert_awaited_once()
    finally:
        db.close()
        engine.dispose()
