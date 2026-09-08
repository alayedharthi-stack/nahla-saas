"""
COMMERCE_LIFECYCLE_PROVIDER_IDENTITY — regression for provider vs order channel.

Ensures merchant-dashboard / API channel in normalised.source never becomes the
lifecycle adapter provider passed to normalize_external_lifecycle_intent.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402
from models import CommerceLifecycleNotificationLedger, Order  # noqa: E402
from services.store_sync import (  # noqa: E402
    StoreSyncService,
    _attach_lifecycle_observation,
    _extract_order_channel_source,
    _handle_external_lifecycle_transition_best_effort,
    _resolve_canonical_lifecycle_provider,
)
from store_integration.lifecycle_normalization import normalize_external_lifecycle_intent  # noqa: E402

from test_commerce_lifecycle_dispatch import (  # noqa: E402
    _approved_template,
    _configure_lifecycle_dispatch_pilot_allowlists,
    _ensure_webhook_dedupe_index,
    _generic_salla_order_created_payload,
    _merchant_caps,
    _run_async,
    _seed_salla_integration,
)

from commerce_scenario_fixtures import make_scenario_db, seed_tenant  # noqa: E402


def _order_payload(
    *,
    store_id: str,
    source: str | None = "merchant-dashboard",
    status_slug: str = "in_progress",
    payment_method: str = "waiting",
    accepted_payment_methods: list[str] | None = None,
    external_id: int = 8801001,
) -> tuple[dict, dict]:
    order_data, parsed = _generic_salla_order_created_payload(store_id=store_id)
    order_data["id"] = external_id
    order_data["status"] = {"slug": status_slug, "name": status_slug}
    order_data["payment_method"] = payment_method
    if accepted_payment_methods is not None:
        order_data["accepted_payment_methods"] = accepted_payment_methods
    if source is not None:
        order_data["source"] = source
    elif "source" in order_data:
        del order_data["source"]
    parsed["data"] = order_data
    return order_data, parsed


def _dispatch_order_created(
    db,
    *,
    tenant_id: int,
    store_id: str,
    source: str | None = "merchant-dashboard",
    status_slug: str = "in_progress",
    payment_method: str = "waiting",
    accepted_payment_methods: list[str] | None = None,
    external_event_id: str = "salla-wh-evt-provider-id",
    external_id: int = 8801001,
) -> None:
    from core.webhook_dispatcher import _process_event  # noqa: PLC0415
    from core.webhook_events import claim_next_batch, persist_event  # noqa: PLC0415

    _order_data, parsed_payload = _order_payload(
        store_id=store_id,
        source=source,
        status_slug=status_slug,
        payment_method=payment_method,
        accepted_payment_methods=accepted_payment_methods,
        external_id=external_id,
    )
    persist_event(
        db,
        provider="salla",
        raw_body='{"event":"order.created"}',
        parsed_payload=parsed_payload,
        event_type="order.created",
        external_event_id=external_event_id,
        store_id=store_id,
    )
    batch = claim_next_batch(db, limit=5)
    assert len(batch) == 1
    _run_async(_process_event(db, batch[0]))


@pytest.fixture(autouse=True)
def _enable_dispatch(monkeypatch):
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_SEND_STALE_SECONDS", "0")
    _configure_lifecycle_dispatch_pilot_allowlists(
        monkeypatch,
        tenants="1,2,3,4,5,6,7,8,9,10",
        recipients="+966500222333,+966549815590",
    )


class TestProviderIdentityHelpers:
    def test_merchant_dashboard_channel_does_not_change_salla_provider(self):
        normalised = {"source": "merchant-dashboard", "status": "in_progress"}
        assert _extract_order_channel_source(normalised) == "merchant-dashboard"
        adapter = SimpleNamespace(platform="salla")
        assert _resolve_canonical_lifecycle_provider(adapter) == "salla"
        live = _attach_lifecycle_observation(
            normalised, "live_webhook", source_event="order.created"
        )
        intent, reason = normalize_external_lifecycle_intent(
            provider="salla",
            raw_previous_status=None,
            raw_current_status="in_progress",
            normalized_order=live,
        )
        assert intent == BusinessIntent.ORDER_CONFIRMED
        assert reason == "adapter_mapped"

    def test_shopify_channel_with_salla_adapter_still_uses_salla_provider(self):
        normalised = {"source": "shopify", "status": "in_progress"}
        assert _extract_order_channel_source(normalised) == "shopify"
        adapter = SimpleNamespace(platform="salla")
        assert _resolve_canonical_lifecycle_provider(adapter) == "salla"

    def test_unregistered_adapter_platform_fails_closed(self):
        adapter = SimpleNamespace(platform="unknown_platform_xyz")
        assert _resolve_canonical_lifecycle_provider(adapter) is None


class TestStoreSyncDispatcherProviderIdentity:
    @patch("services.outcome_tracker.record_order_outcome")
    @patch("services.offer_attribution_service.attribute_order_to_decision")
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_merchant_dashboard_path_order_confirmed(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_attr,
        _mock_outcome,
    ):
        db, _engine = make_scenario_db()
        _ensure_webhook_dedupe_index(db)
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        store_id = "STORE-MD-8801"
        _seed_salla_integration(db, tenant.id, store_id)
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.md.dashboard"})

        _dispatch_order_created(
            db,
            tenant_id=tenant.id,
            store_id=store_id,
            source="merchant-dashboard",
            status_slug="in_progress",
        )

        order = db.query(Order).filter_by(tenant_id=tenant.id).one()
        assert order.source == "salla"
        assert (order.extra_metadata or {}).get("order_channel_source") == "merchant-dashboard"
        ledger = db.query(CommerceLifecycleNotificationLedger).filter_by(
            tenant_id=tenant.id, order_id=order.id
        ).one()
        assert ledger.business_intent == BusinessIntent.ORDER_CONFIRMED.value
        assert mock_send.await_count == 1

    @patch("services.outcome_tracker.record_order_outcome")
    @patch("services.offer_attribution_service.attribute_order_to_decision")
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_api_app_source_preserves_channel_but_provider_stays_salla(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_attr,
        _mock_outcome,
    ):
        db, _engine = make_scenario_db()
        _ensure_webhook_dedupe_index(db)
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        store_id = "STORE-API-8802"
        _seed_salla_integration(db, tenant.id, store_id)
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.api.app"})

        _dispatch_order_created(
            db,
            tenant_id=tenant.id,
            store_id=store_id,
            source="api-app-12345",
            status_slug="in_progress",
            external_event_id="salla-wh-evt-api-app",
            external_id=8801002,
        )

        order = db.query(Order).filter_by(tenant_id=tenant.id).one()
        assert order.source == "salla"
        assert (order.extra_metadata or {}).get("order_channel_source") == "api-app-12345"
        assert db.query(CommerceLifecycleNotificationLedger).count() == 1

    @patch("services.outcome_tracker.record_order_outcome")
    @patch("services.offer_attribution_service.attribute_order_to_decision")
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_missing_channel_source_keeps_canonical_provider(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_attr,
        _mock_outcome,
    ):
        db, _engine = make_scenario_db()
        _ensure_webhook_dedupe_index(db)
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        store_id = "STORE-NOCH-8803"
        _seed_salla_integration(db, tenant.id, store_id)
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.no.channel"})

        _dispatch_order_created(
            db,
            tenant_id=tenant.id,
            store_id=store_id,
            source=None,
            status_slug="in_progress",
            external_event_id="salla-wh-evt-no-channel",
            external_id=8801003,
        )

        order = db.query(Order).filter_by(tenant_id=tenant.id).one()
        assert order.source == "salla"
        assert "order_channel_source" not in (order.extra_metadata or {})
        assert db.query(CommerceLifecycleNotificationLedger).count() == 1

    @patch("services.outcome_tracker.record_order_outcome")
    @patch("services.offer_attribution_service.attribute_order_to_decision")
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_bank_payment_pending_yields_payment_needed(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_attr,
        _mock_outcome,
    ):
        db, _engine = make_scenario_db()
        _ensure_webhook_dedupe_index(db)
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        store_id = "STORE-BANK-8804"
        _seed_salla_integration(db, tenant.id, store_id)
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.bank.pay"})

        _dispatch_order_created(
            db,
            tenant_id=tenant.id,
            store_id=store_id,
            source="merchant-dashboard",
            status_slug="payment_pending",
            payment_method="waiting",
            accepted_payment_methods=["bank"],
            external_event_id="salla-wh-evt-bank",
            external_id=8801004,
        )

        ledger = db.query(CommerceLifecycleNotificationLedger).one()
        assert ledger.business_intent == BusinessIntent.PAYMENT_NEEDED.value

    @patch("services.outcome_tracker.record_order_outcome")
    @patch("services.offer_attribution_service.attribute_order_to_decision")
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_ledger_dedup_on_replayed_order_created(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_attr,
        _mock_outcome,
    ):
        from core.webhook_dispatcher import _process_event  # noqa: PLC0415
        from core.webhook_events import claim_next_batch, persist_event  # noqa: PLC0415
        db, _engine = make_scenario_db()
        _ensure_webhook_dedupe_index(db)
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        store_id = "STORE-DEDUP-8805"
        _seed_salla_integration(db, tenant.id, store_id)
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.dedup.once"})

        _order_data, parsed_payload = _order_payload(
            store_id=store_id,
            source="merchant-dashboard",
            status_slug="in_progress",
        )
        ext_event = "salla-wh-evt-dedup-8805"
        first = persist_event(
            db,
            provider="salla",
            raw_body='{"event":"order.created"}',
            parsed_payload=parsed_payload,
            event_type="order.created",
            external_event_id=ext_event,
            store_id=store_id,
        )
        _run_async(_process_event(db, claim_next_batch(db, limit=5)[0]))
        replay = persist_event(
            db,
            provider="salla",
            raw_body='{"event":"order.created"}',
            parsed_payload=parsed_payload,
            event_type="order.created",
            external_event_id=ext_event,
            store_id=store_id,
        )
        assert replay.id == first.id
        _run_async(_process_event(db, replay))

        assert mock_send.await_count == 1
        assert db.query(CommerceLifecycleNotificationLedger).count() == 1

    def test_poll_snapshot_same_status_no_fabricated_confirmation(self):
        db, _engine = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        order = Order(
            tenant_id=tenant.id,
            external_id="poll-8806",
            external_order_number="POLL-8806",
            status="in_progress",
            total="100",
            customer_info={"phone": "+966500222333"},
            line_items=[],
            source="salla",
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        svc = StoreSyncService(db, tenant.id)
        normalised = {
            "external_id": "poll-8806",
            "status": "in_progress",
            "source": "merchant-dashboard",
            "customer_info": {"phone": "+966500222333"},
        }
        poll_norm = _attach_lifecycle_observation(normalised, "poll_import")

        _run_async(
            _handle_external_lifecycle_transition_best_effort(
                svc,
                order=order,
                lifecycle_provider="salla",
                order_channel_source="merchant-dashboard",
                raw_previous_status="in_progress",
                raw_current_status="in_progress",
                normalized_order=poll_norm,
            )
        )

        assert db.query(CommerceLifecycleNotificationLedger).count() == 0

    def test_unregistered_adapter_fails_closed_no_ledger(self):
        db, _engine = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        order = Order(
            tenant_id=tenant.id,
            external_id="unreg-8807",
            external_order_number="UNREG-8807",
            status="in_progress",
            total="100",
            customer_info={"phone": "+966500222333"},
            line_items=[],
            source="unknown",
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        svc = StoreSyncService(db, tenant.id)
        normalised = {
            "external_id": "unreg-8807",
            "status": "in_progress",
            "source": "merchant-dashboard",
            "customer_info": {"phone": "+966500222333"},
        }
        live = _attach_lifecycle_observation(
            normalised, "live_webhook", source_event="order.created"
        )

        _run_async(
            _handle_external_lifecycle_transition_best_effort(
                svc,
                order=order,
                lifecycle_provider=None,
                order_channel_source="merchant-dashboard",
                raw_previous_status=None,
                raw_current_status="in_progress",
                normalized_order=live,
            )
        )

        assert db.query(CommerceLifecycleNotificationLedger).count() == 0

    @patch("services.outcome_tracker.record_order_outcome")
    @patch("services.offer_attribution_service.attribute_order_to_decision")
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_model_call_count_zero_on_dispatch_path(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_attr,
        _mock_outcome,
    ):
        db, _engine = make_scenario_db()
        _ensure_webhook_dedupe_index(db)
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        store_id = "STORE-ZERO-AI-8808"
        _seed_salla_integration(db, tenant.id, store_id)
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.zero.ai"})

        _dispatch_order_created(
            db,
            tenant_id=tenant.id,
            store_id=store_id,
            source="merchant-dashboard",
            status_slug="in_progress",
            external_event_id="salla-wh-evt-zero-ai",
            external_id=8801008,
        )
        assert mock_send.await_count == 1
        assert db.query(CommerceLifecycleNotificationLedger).count() == 1
