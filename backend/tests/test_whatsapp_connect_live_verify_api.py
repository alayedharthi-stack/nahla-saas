"""Route-level live-verify sanitization tests."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from routers import whatsapp_connect as wc  # noqa: E402

API_KEY = "D360-LIVE-VERIFY-CANARY-877"
PHONE = "D360-LIVE-PHONE-877"
WABA = "D360-LIVE-WABA-877"
TENANT = 990877


def _conn():
    return SimpleNamespace(
        tenant_id=TENANT,
        status="connected",
        phone_number_id=PHONE,
        whatsapp_business_account_id=WABA,
        access_token=API_KEY,
        connection_type="coexistence",
        provider="360dialog",
        extra_metadata={"provider_details": {"channel_id": "chan-877"}},
        last_webhook_received_at=None,
    )


def test_live_verify_sanitizes_provider_probe():
    request = SimpleNamespace(state=SimpleNamespace(tenant_id=TENANT))
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = _conn()
    raw_probe = {
        "composite_alive": True,
        "channel_auth_revoked": False,
        "summary": "ok",
        "steps": [{
            "step": "v1_configs",
            "url": "https://secret.example/877",
            "body_preview": API_KEY,
            "phone_number_id": PHONE,
        }],
    }
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), \
         patch.object(wc, "_live_verify_cache_get", return_value=None), \
         patch.object(wc, "_live_verify_cache_put", side_effect=lambda _t, payload: payload), \
         patch.object(wc, "_has_recent_webhook_traffic", return_value=False), \
         patch.object(wc, "get_token_for_operation", new=AsyncMock(return_value=SimpleNamespace(token=API_KEY))), \
         patch.object(wc, "dialog360_live_verify_probes", new=AsyncMock(return_value=raw_probe)):
        payload = asyncio.run(wc.live_verify_connection(request, db))
    probe = payload.get("provider_probe") or {}
    rendered = json.dumps(probe)
    assert API_KEY not in rendered
    assert PHONE not in rendered
    assert "body_preview" not in rendered
    assert "url" not in rendered
