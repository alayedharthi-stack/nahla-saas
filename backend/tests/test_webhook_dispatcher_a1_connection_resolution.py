"""Webhook dispatcher: integration-first with legacy lifecycle fallback (A1-v3.7)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.webhook_dispatcher import _dispatch_salla  # noqa: E402
from services.salla_integration_resolver import (  # noqa: E402
    ResolvedSallaIntegration,
    UnresolvedSallaIntegration,
)


def _event(*, provider: str = "salla", store_id: str = "STORE-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=99,
        provider=provider,
        store_id=store_id,
        tenant_id=None,
        event_type="order.created",
        parsed_payload={
            "event": "order.created",
            "merchant": store_id,
            "data": {"id": "ORD-1", "status": "pending", "total": 10},
        },
    )


def test_unresolved_integration_legacy_tenant_routes_lifecycle_without_a1() -> None:
    db = MagicMock()
    event = _event()
    captured: dict = {}

    class _Svc:
        def __init__(self, _db, tenant_id, *, integration_connection_id=None, adapter=None):
            captured["tenant_id"] = tenant_id
            captured["integration_connection_id"] = integration_connection_id

        handle_order_webhook = AsyncMock()

    with (
        patch(
            "services.salla_integration_resolver.resolve_salla_integration_connection",
            return_value=UnresolvedSallaIntegration(reason="connection_not_found"),
        ),
        patch("routers.webhooks._resolve_tenant_from_store", return_value=42),
        patch("services.store_sync.StoreSyncService", _Svc),
        patch("core.webhook_dispatcher.log_event"),
        patch("services.order_customer_identity_logging.log_connection_resolution_status") as log_status,
    ):
        asyncio.run(_dispatch_salla(db, event))

    assert captured["tenant_id"] == 42
    assert captured["integration_connection_id"] is None
    _Svc.handle_order_webhook.assert_awaited_once()
    assert _Svc.handle_order_webhook.await_args.kwargs.get("integration_resolution") is None
    log_status.assert_called_once()
    assert log_status.call_args.kwargs["status"] == "unresolved"


def test_ambiguous_integration_uses_legacy_lifecycle_fail_closed_identity() -> None:
    db = MagicMock()
    event = _event()

    class _Svc:
        def __init__(self, _db, tenant_id, *, integration_connection_id=None, adapter=None):
            self.integration_connection_id = integration_connection_id

        handle_order_webhook = AsyncMock()

    with (
        patch(
            "services.salla_integration_resolver.resolve_salla_integration_connection",
            return_value=UnresolvedSallaIntegration(reason="ambiguous_tier_a"),
        ),
        patch("routers.webhooks._resolve_tenant_from_store", return_value=7),
        patch("services.store_sync.StoreSyncService", _Svc),
        patch("core.webhook_dispatcher.log_event"),
        patch("services.order_customer_identity_logging.log_connection_resolution_status") as log_status,
    ):
        asyncio.run(_dispatch_salla(db, event))

    assert _Svc.handle_order_webhook.await_args.kwargs.get("integration_resolution") is None
    assert log_status.call_args.kwargs["reason"] == "ambiguous_tier_a"


def test_legacy_resolver_failure_preserves_dlq_retry_error() -> None:
    db = MagicMock()
    event = _event()

    with (
        patch(
            "services.salla_integration_resolver.resolve_salla_integration_connection",
            return_value=UnresolvedSallaIntegration(reason="connection_not_found"),
        ),
        patch("routers.webhooks._resolve_tenant_from_store", return_value=None),
        patch("core.webhook_dispatcher.log_event"),
    ):
        with pytest.raises(RuntimeError, match="tenant_unresolved"):
            asyncio.run(_dispatch_salla(db, event))


def test_resolved_integration_uses_authoritative_a1_path() -> None:
    db = MagicMock()
    event = _event()
    resolution = ResolvedSallaIntegration(
        integration_id=5, tenant_id=42, matched_via="tier_a_external_store_id+channel",
    )
    captured: dict = {}

    class _Svc:
        def __init__(self, _db, tenant_id, *, integration_connection_id=None, adapter=None):
            captured["integration_connection_id"] = integration_connection_id

        handle_order_webhook = AsyncMock()

    with (
        patch(
            "services.salla_integration_resolver.resolve_salla_integration_connection",
            return_value=resolution,
        ),
        patch("services.store_sync.StoreSyncService", _Svc),
        patch("services.order_customer_identity_logging.log_connection_resolution_status") as log_status,
    ):
        asyncio.run(_dispatch_salla(db, event))

    assert captured["integration_connection_id"] == 5
    assert _Svc.handle_order_webhook.await_args.kwargs["integration_resolution"] is resolution
    assert log_status.call_args.kwargs["status"] == "resolved"
