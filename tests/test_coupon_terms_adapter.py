"""Synthetic tests for SallaAdapter.update_coupon_terms.

No network, no database, no real tokens.  Uses a fake AsyncClient modelled
after the pattern in test_coupon_active_sync.py.
"""
import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from store_adapters.salla_adapter import SallaAdapter


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _adapter():
    a = SallaAdapter(
        api_key="fake-test-only",
        store_id="999",
        tenant_id=1,
        integration_id=42,
    )
    a._ensure_token_fresh = AsyncMock()
    a._refresh_access_token = AsyncMock(return_value=False)
    a._mark_needs_reauth = MagicMock()
    return a


class _FakeClient:
    """Minimal fake httpx.AsyncClient that returns pre-loaded responses."""

    def __init__(self, responses, seen):
        self._responses = list(responses)
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def put(self, url, *, headers=None, content=None, **kwargs):
        self._seen.append({"url": url, "content": content, "headers": headers})
        return self._responses.pop(0)


def _ok(coupon_id="101"):
    req = httpx.Request("PUT", f"https://api.salla.dev/admin/v2/coupons/{coupon_id}")
    return httpx.Response(200, request=req)


def _resp(status, coupon_id="101"):
    req = httpx.Request("PUT", f"https://api.salla.dev/admin/v2/coupons/{coupon_id}")
    return httpx.Response(status, request=req)


# ---------------------------------------------------------------------------
# happy-path: body shape
# ---------------------------------------------------------------------------

def test_amount_only_sends_correct_keys(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient([_ok()], seen),
    )
    asyncio.run(adapter.update_coupon_terms("101", amount="19.99"))
    assert len(seen) == 1
    body = json.loads(seen[0]["content"], parse_float=Decimal)
    assert set(body.keys()) == {"amount"}
    assert body["amount"] == Decimal("19.99")
    assert "status" not in body
    assert "eligibility" not in body


def test_expiry_only_sends_correct_keys(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient([_ok()], seen),
    )
    asyncio.run(adapter.update_coupon_terms("101", expiry_date="2027-03-15"))
    body = json.loads(seen[0]["content"], parse_float=Decimal)
    assert set(body.keys()) == {"expiry_date"}
    assert body["expiry_date"] == "2027-03-15"
    assert "status" not in body


def test_both_fields_sends_exactly_two_keys(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient([_ok()], seen),
    )
    asyncio.run(adapter.update_coupon_terms("101", amount="5.00", expiry_date="2028-12-31"))
    body = json.loads(seen[0]["content"], parse_float=Decimal)
    assert set(body.keys()) == {"amount", "expiry_date"}
    assert "status" not in body
    assert "free_shipping" not in body
    assert "code" not in body
    assert "type" not in body


def test_high_precision_decimal_preserved(monkeypatch):
    """Decimal precision must survive serialisation without float() rounding."""
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient([_ok()], seen),
    )
    asyncio.run(adapter.update_coupon_terms("101", amount="123.456789012345"))
    body = json.loads(seen[0]["content"], parse_float=Decimal)
    assert body["amount"] == Decimal("123.456789012345")


def test_content_type_is_application_json(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient([_ok()], seen),
    )
    asyncio.run(adapter.update_coupon_terms("101", amount="10.00"))
    assert seen[0]["headers"].get("Content-Type") == "application/json"


# ---------------------------------------------------------------------------
# invalid input – must not call PUT at all
# ---------------------------------------------------------------------------

def test_no_fields_raises_before_put(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient([], seen),
    )
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101"))
    assert seen == []
    adapter._ensure_token_fresh.assert_not_called()


def test_negative_amount_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", amount="-5.00"))
    assert seen == []


def test_zero_amount_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", amount="0"))
    assert seen == []


def test_exponent_amount_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", amount="1e5"))
    assert seen == []


def test_hex_amount_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", amount="0xFF"))
    assert seen == []


def test_non_string_amount_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", amount=10.5))
    assert seen == []


def test_empty_string_amount_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", amount=""))
    assert seen == []


def test_invalid_date_format_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", expiry_date="31-12-2027"))
    assert seen == []


def test_empty_string_date_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", expiry_date=""))
    assert seen == []


def test_invalid_coupon_id_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("abc", amount="10.00"))
    assert seen == []


# ---------------------------------------------------------------------------
# 422 propagates
# ---------------------------------------------------------------------------

def test_422_raises_http_status_error(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient([_resp(422)], seen),
    )
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.update_coupon_terms("101", amount="10.00"))


# ---------------------------------------------------------------------------
# 401 → one refresh retry with same body, then needs_reauth on final 401
# ---------------------------------------------------------------------------

def test_401_triggers_one_refresh_then_retries(monkeypatch):
    adapter = _adapter()
    adapter._refresh_token = "fake-refresh"
    adapter._refresh_access_token = AsyncMock(return_value=True)
    seen = []

    req = httpx.Request("PUT", "https://api.salla.dev/admin/v2/coupons/101")
    responses = [
        httpx.Response(401, request=req),
        httpx.Response(200, request=req),
    ]
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient(responses, seen),
    )
    asyncio.run(adapter.update_coupon_terms("101", amount="7.50"))

    assert len(seen) == 2
    # Both calls must carry the same body
    assert seen[0]["content"] == seen[1]["content"]
    body = json.loads(seen[0]["content"], parse_float=Decimal)
    assert body["amount"] == Decimal("7.50")


def test_final_401_marks_needs_reauth(monkeypatch):
    adapter = _adapter()
    adapter._refresh_token = "fake-refresh"
    adapter._refresh_access_token = AsyncMock(return_value=False)
    seen = []

    req = httpx.Request("PUT", "https://api.salla.dev/admin/v2/coupons/101")
    responses = [httpx.Response(401, request=req)]
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient(responses, seen),
    )
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.update_coupon_terms("101", amount="7.50"))

    adapter._mark_needs_reauth.assert_called_once()


# ---------------------------------------------------------------------------
# acceptance egress denial blocks outgoing calls
# ---------------------------------------------------------------------------

def test_egress_denial_blocks_put(monkeypatch):
    """deny_external_egress raising must prevent any PUT from being sent."""
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient([], seen),
    )

    class _EgressBlocked(Exception):
        pass

    import core.acceptance_execution_context as aec
    monkeypatch.setattr(aec, "deny_external_egress", lambda **kw: (_ for _ in ()).throw(_EgressBlocked()))

    with pytest.raises(_EgressBlocked):
        asyncio.run(adapter.update_coupon_terms("101", amount="10.00"))

    assert seen == []
    adapter._ensure_token_fresh.assert_not_called()


# ---------------------------------------------------------------------------
# impossible calendar dates – regex passes but fromisoformat must reject
# ---------------------------------------------------------------------------

def test_feb_30_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", expiry_date="2026-02-30"))
    assert seen == []
    adapter._ensure_token_fresh.assert_not_called()


def test_feb_29_non_leap_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", expiry_date="2025-02-29"))
    assert seen == []
    adapter._ensure_token_fresh.assert_not_called()


def test_month_13_rejected(monkeypatch):
    adapter = _adapter()
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient([], seen))
    with pytest.raises(ValueError):
        asyncio.run(adapter.update_coupon_terms("101", expiry_date="2026-13-01"))
    assert seen == []
    adapter._ensure_token_fresh.assert_not_called()


# ---------------------------------------------------------------------------
# initial PUT uses the token renewed by _ensure_token_fresh
# ---------------------------------------------------------------------------

def test_initial_put_uses_renewed_token(monkeypatch):
    """Token renewed inside _ensure_token_fresh must appear in the first PUT."""
    adapter = _adapter()
    seen = []

    async def _renew():
        adapter.api_key = "renewed-key"

    adapter._ensure_token_fresh = AsyncMock(side_effect=_renew)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: _FakeClient([_ok()], seen),
    )
    asyncio.run(adapter.update_coupon_terms("101", amount="10.00"))

    assert len(seen) == 1
    assert seen[0]["headers"].get("Authorization") == "Bearer renewed-key"
