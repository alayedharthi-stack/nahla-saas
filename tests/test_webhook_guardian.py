from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


from core.webhook_guardian import (  # noqa: E402
    SubscriptionAttemptResult,
    _classify_connection_health,
    _inspect_connection,
    _subscription_targets,
)


def _conn(**overrides):
    base = {
        "tenant_id": 1,
        "phone_number_id": "1030006246869922",
        "whatsapp_business_account_id": "878936815163659",
        "status": "connected",
        "webhook_verified": True,
        "sending_enabled": True,
        "last_webhook_received_at": datetime.now(timezone.utc) - timedelta(minutes=30),
        "connection_type": "embedded",
        "access_token": "token-123",
        "updated_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_embedded_connections_prefer_waba_subscription():
    targets = _subscription_targets("embedded", "phone123", "waba456")
    assert targets == [("waba", "waba456")]


def test_direct_connections_keep_phone_then_waba_fallback():
    targets = _subscription_targets("direct", "phone123", "waba456")
    assert targets == [("phone", "phone123"), ("waba", "waba456")]


def test_idle_classification_when_only_silence_exists():
    now = datetime.now(timezone.utc)
    conn = _conn(last_webhook_received_at=now - timedelta(minutes=40))
    idle_cutoff = now - timedelta(minutes=15)
    assert _classify_connection_health(conn, now, idle_cutoff) == "idle"


def test_critical_classification_requires_webhook_failure():
    now = datetime.now(timezone.utc)
    conn = _conn(webhook_verified=False, last_webhook_received_at=now - timedelta(minutes=2))
    idle_cutoff = now - timedelta(minutes=15)
    assert _classify_connection_health(conn, now, idle_cutoff) == "critical"


async def _run_inspect_idle():
    now = datetime.now(timezone.utc)
    conn = _conn(last_webhook_received_at=now - timedelta(minutes=25))
    db = SimpleNamespace()
    health = await _inspect_connection(db, conn, now, now - timedelta(minutes=15))
    return health


def test_inspect_connection_returns_idle_without_resubscribe():
    with patch("core.webhook_guardian._resubscribe", new=AsyncMock()) as mock_resubscribe:
        import asyncio

        health = asyncio.run(_run_inspect_idle())
        assert health == "idle"
        mock_resubscribe.assert_not_called()


async def _run_inspect_critical():
    now = datetime.now(timezone.utc)
    conn = _conn(webhook_verified=False)
    db = SimpleNamespace(commit=lambda: None)
    with patch("core.webhook_guardian._guardian_log"), patch("core.webhook_guardian._audit"):
        return await _inspect_connection(
            db,
            conn,
            now,
            now - timedelta(minutes=15),
        )


def test_inspect_connection_recovers_after_successful_resubscribe():
    result = SubscriptionAttemptResult(
        success=True,
        subscribe_target="waba",
        connection_type="embedded",
        token_source="merchant_oauth",
        waba_id="878936815163659",
        attempted_fallback=False,
        fallback_succeeded=False,
    )
    with patch("core.webhook_guardian._resubscribe", new=AsyncMock(return_value=result)):
        import asyncio

        health = asyncio.run(_run_inspect_critical())
        assert health == "active"
