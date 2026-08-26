"""API response body canary tests for connect routes."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from routers import whatsapp_connect as wc  # noqa: E402

TOKEN = "API-PAYLOAD-TOKEN-877"
PHONE = "API-PAYLOAD-PHONE-877"
WABA = "API-PAYLOAD-WABA-877"
GRAPH_BODY = "RAW-META-BODY-CANARY-877"
TENANT = 990877


def _conn():
    return SimpleNamespace(
        tenant_id=TENANT,
        phone_number_id=PHONE,
        whatsapp_business_account_id=WABA,
        status="pending",
        access_token=TOKEN,
        connection_type="direct",
        provider="meta",
        phone_number="+966500009999",
        business_display_name="Test",
        extra_metadata={},
        last_webhook_received_at=None,
    )


def test_refresh_status_from_meta_no_raw_meta_response():
    request = SimpleNamespace(state=SimpleNamespace(tenant_id=TENANT))
    db = SimpleNamespace(query=lambda *a, **k: SimpleNamespace(filter_by=lambda **kw: SimpleNamespace(first=lambda: _conn())))
    class _Resp:
        status_code = 400
        @staticmethod
        def json():
            return {"error": {"message": GRAPH_BODY, "code": 190}}
    with patch.object(wc, "resolve_tenant_id", return_value=TENANT), \
         patch.object(wc, "get_token_for_operation", new=AsyncMock(return_value=SimpleNamespace(token=TOKEN))), \
         patch.object(wc.httpx, "AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get = AsyncMock(return_value=_Resp())
        client_cls.return_value = client
        payload = asyncio.run(wc.refresh_status_from_meta(request, db))
    rendered = json.dumps(payload)
    assert "meta_response" not in rendered
    assert GRAPH_BODY not in rendered
