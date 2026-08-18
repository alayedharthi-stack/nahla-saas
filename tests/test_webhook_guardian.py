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
    _subscribed_fields_for,
    _subscription_covers_required_fields,
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


def test_embedded_coexistence_subscribes_expanded_fields():
    fields = _subscribed_fields_for("embedded", {"connection_mode": "coexistence"})
    assert "history" in fields
    assert "smb_app_state_sync" in fields
    assert "smb_message_echoes" in fields
    assert "account_update" in fields
    assert _subscribed_fields_for("embedded", {}) == [
        "messages",
        "messaging_postbacks",
        "message_echoes",
    ]


def test_inspect_fails_expired_coexistence_smb_deadline():
    import asyncio

    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=25)).isoformat()
    conn = _conn(
        status="configuring",
        sending_enabled=False,
        webhook_verified=True,
        extra_metadata={
            "connection_mode": "coexistence",
            "smb_sync_deadline_at": past,
            "smb_sync": {},
        },
    )
    db = SimpleNamespace(commit=lambda: None)
    health = asyncio.run(_inspect_connection(db, conn, now, now - timedelta(minutes=15)))
    assert health == "critical"
    assert conn.status == "failed"
    assert conn.sending_enabled is False


def test_inspect_retries_missing_smb_app_data_until_connected():
    import asyncio

    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=12)).isoformat()
    conn = _conn(
        status="configuring",
        sending_enabled=False,
        webhook_verified=True,
        last_webhook_received_at=now,
        extra_metadata={
            "connection_mode": "coexistence",
            "smb_sync_deadline_at": future,
            "smb_sync": {},
        },
    )
    db = SimpleNamespace(commit=lambda: None)
    results = {
        "smb_app_state_sync": {"accepted": True, "request_id": "a"},
        "history": {"accepted": True, "request_id": "b"},
    }
    with patch(
        "services.whatsapp_platform.wa_connection_secrets.read_access_token",
        return_value="tok",
    ), patch(
        "services.meta_coexistence.initiate_smb_app_data",
        return_value=results,
    ) as mock_sync:
        health = asyncio.run(_inspect_connection(db, conn, now, now - timedelta(minutes=15)))
    assert mock_sync.called
    assert conn.status == "connected"
    assert conn.sending_enabled is True
    assert health == "active"


def test_guardian_requires_all_coexistence_webhook_fields_when_listed():
    required = _subscribed_fields_for("embedded", {"connection_mode": "coexistence"})
    complete = [{
        "id": "app",
        "subscribed_fields": required,
    }]
    missing_history = [{
        "id": "app",
        "subscribed_fields": [f for f in required if f != "history"],
    }]
    omitted = [{"id": "app"}]
    ours_missing_history = [{
        "id": "ours",
        "subscribed_fields": [f for f in required if f != "history"],
    }]
    other_complete = [{
        "id": "other-app",
        "subscribed_fields": required,
    }]
    assert _subscription_covers_required_fields(complete, required) is True
    assert _subscription_covers_required_fields(missing_history, required) is False
    assert _subscription_covers_required_fields(omitted, required) is True
    assert _subscription_covers_required_fields(ours_missing_history, required) is False
    # Mixing a foreign complete app must not be used by Guardian; it filters first.
    assert _subscription_covers_required_fields(ours_missing_history, required) is False
    assert _subscription_covers_required_fields(other_complete, required) is True
