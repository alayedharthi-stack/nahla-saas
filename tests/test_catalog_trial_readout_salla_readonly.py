"""The trial readout's Salla re-read never refreshes, saves or invalidates a token.

``--include-salla`` is documented as GET only and runs inside the production
container. The adapter's request helper would refresh an access token that is
near expiry (and save the new pair), refresh on a 401, or mark the
integration for re-authorisation. The readout must do none of that: it reads
with the stored token as is and refuses, with a clear code, when the read
could only proceed by changing stored credentials.

Uses a real ``SallaAdapter`` instance whose mutating methods fail the test if
they are ever called. No network: the HTTP GET is served by the test.
Generic merchant data (متجر تجريبي عام).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import services.catalog_trial_readout as readout
from store_adapters.salla_adapter import SallaAdapter


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def _adapter(*, api_key="salla-read-token", expires_at=None, refresh_token="salla-refresh-token"):
    adapter = SallaAdapter.__new__(SallaAdapter)
    adapter.api_key = api_key
    adapter._refresh_token = refresh_token
    adapter._expires_at = expires_at
    adapter._tenant_id = 41
    adapter.platform = "salla"

    def _forbidden(name):
        def _fail(*_a, **_k):
            raise AssertionError(f"{name} must never run from the read-only readout")
        return _fail

    for name in ("_get", "_post", "_ensure_token_fresh", "_refresh_access_token",
                 "_persist_refreshed_tokens", "_mark_needs_reauth", "get_raw_variants"):
        setattr(adapter, name, _forbidden(name))
    return adapter


def _serve(monkeypatch, status=200):
    calls = []

    async def _http_get(url, headers, params=None):
        calls.append((url, headers.get("Authorization")))
        if status != 200:
            return _Resp(status, {"error": "unauthorized"})
        if url.endswith("/variants"):
            return _Resp(200, {"data": [{"id": 1, "quantity": 2, "price": {"amount": 99.0, "currency": "SAR"}}]})
        return _Resp(200, {"data": {"id": 910300, "name": "قميص قطني أزرق", "quantity": 2}})

    monkeypatch.setattr(readout, "_salla_http_get", _http_get)
    return calls


def test_a_token_near_expiry_is_used_as_is_and_never_refreshed(monkeypatch):
    calls = _serve(monkeypatch)
    soon = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()   # inside the adapter's 24h refresh window
    adapter = _adapter(expires_at=soon)
    data, variants = readout._salla_product_read(adapter, "910300")
    assert data["id"] == 910300 and len(variants) == 1
    assert [url.rsplit("/admin/v2", 1)[1] for url, _ in calls] == ["/products/910300", "/products/910300/variants"]
    assert all(auth == "Bearer salla-read-token" for _, auth in calls)
    assert adapter.api_key == "salla-read-token" and adapter._refresh_token == "salla-refresh-token"


def test_an_expired_token_is_refused_without_any_request(monkeypatch):
    calls = _serve(monkeypatch)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with pytest.raises(readout.SallaReadRefused) as exc:
        readout._salla_product_read(_adapter(expires_at=past), "910300")
    assert exc.value.code == readout.SALLA_READ_REFUSED_TOKEN_EXPIRED
    assert calls == []


def test_a_rejected_token_is_refused_without_refresh_or_reauth_mark(monkeypatch):
    calls = _serve(monkeypatch, status=401)
    with pytest.raises(readout.SallaReadRefused) as exc:
        readout._salla_product_read(_adapter(), "910300")
    assert exc.value.code == readout.SALLA_READ_REFUSED_TOKEN_REJECTED
    assert len(calls) == 1


def test_a_missing_token_is_refused(monkeypatch):
    calls = _serve(monkeypatch)
    with pytest.raises(readout.SallaReadRefused) as exc:
        readout._salla_product_read(_adapter(api_key=""), "910300")
    assert exc.value.code == readout.SALLA_READ_REFUSED_TOKEN_MISSING
    assert calls == []


def test_the_readout_reports_the_refusal_code_per_product(monkeypatch):
    _serve(monkeypatch)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    out = readout._salla_check(None, 41, [{"product_id": 7, "external_id": "910300"}], adapter=_adapter(expires_at=past))
    assert out["checked"] == [{"product_id": 7, "external_id": "910300",
                               "error": readout.SALLA_READ_REFUSED_TOKEN_EXPIRED}]
    assert out["reads"] == []
