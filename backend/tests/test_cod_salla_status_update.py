"""Provider boundary for customer-confirmed Salla COD orders."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from store_adapters.salla_adapter import SallaAdapter  # noqa: E402
from store_integration.order_service import update_order_status  # noqa: E402


def test_salla_status_update_uses_predefined_slug_and_disables_provider_sms():
    adapter = SallaAdapter(api_key="test", store_id="store-1", tenant_id=9)
    adapter._post = AsyncMock(return_value={"success": True})

    result = asyncio.run(adapter.update_order_status("salla-77", "under_review"))

    assert result is True
    adapter._post.assert_awaited_once_with(
        "/orders/salla-77/status",
        {"slug": "under_review", "send_status_sms": False},
    )


def test_order_service_returns_false_when_provider_update_fails():
    adapter = SallaAdapter(api_key="test", store_id="store-1", tenant_id=9)
    adapter.update_order_status = AsyncMock(side_effect=RuntimeError("provider down"))
    with patch("store_integration.order_service.get_adapter", return_value=adapter):
        result = asyncio.run(update_order_status(9, "salla-78", "under_review"))
    assert result is False
