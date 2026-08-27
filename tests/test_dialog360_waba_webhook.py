"""
tests/test_dialog360_waba_webhook.py
────────────────────────────────────
Locks down the WABA-level webhook plumbing that fixes the
"Channel Webhook ✓ / Waba Webhook N/A" coexistence drop:

  1. `dialog360_set_waba_webhook` posts to /waba_webhook on the
     360dialog Channel API base with the expected payload shape
     (url, headers, override_all=True).
  2. `dialog360_get_waba_webhook` returns the parsed body, surfacing
     `numbers_on_this_waba` so callers can spot phone_number_id drift
     between the local connection row and what 360dialog has on file.
  3. `admin_coexistence_auto_configure` writes BOTH the channel webhook
     AND the WABA webhook on every call — the merchant should never
     have to remember the WABA scope manually again.
  4. `admin_coexistence_waba_webhook_read` reports phone-id drift when
     the local `WhatsAppConnection.phone_number_id` is not in the WABA's
     `numbers_on_this_waba`.

The 360dialog HTTP layer is replaced with an in-process fake so the
tests run offline and deterministically.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)


from models import Base, Tenant, WhatsAppConnection  # noqa: E402
from services.whatsapp_platform import service as wa_service  # noqa: E402


# ── In-memory DB helpers ───────────────────────────────────────────────────


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    _saved: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig_type in _saved:
        col.type = orig_type
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_conn(
    db,
    *,
    tenant_id: int = 1,
    api_key: str = "d360_secret_key",
    phone_id: str = "1061057720431678",
    waba_id: str = "1749448639704788",
) -> WhatsAppConnection:
    t = Tenant(id=tenant_id, name=f"T{tenant_id}", is_active=True)
    db.add(t)
    db.flush()
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        provider="dialog360",
        connection_type="coexistence",
        status="connected",
        access_token=api_key,
        token_type="dialog360_api_key",
        phone_number_id=phone_id,
        whatsapp_business_account_id=waba_id,
        webhook_verified=True,
        sending_enabled=True,
        extra_metadata={
            "coexistence_internal_secret": "secret-abc",
            "coexistence": {"webhook": {}},
        },
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


# ── Fake httpx client ──────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> Any:
        return self._body


class _FakeClient:
    """Records every call to .get/.post and returns a scripted response."""

    calls: List[Dict[str, Any]] = []
    responses: Dict[Tuple[str, str], _FakeResp] = {}

    def __init__(self, *_a, **_kw) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url: str, headers=None, params=None):  # noqa: D401
        self.calls.append({"method": "GET", "url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        return self.responses.get(("GET", url)) or _FakeResp(200, {})

    async def post(self, url: str, headers=None, json=None):  # noqa: D401
        self.calls.append({"method": "POST", "url": url, "headers": dict(headers or {}), "json": json})
        return self.responses.get(("POST", url)) or _FakeResp(200, {})

    async def request(self, method, url, headers=None, json=None):  # noqa: D401
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "json": json})
        return self.responses.get((method.upper(), url)) or _FakeResp(200, {})


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeClient.calls = []
    _FakeClient.responses = {}
    yield


@pytest.fixture
def patch_httpx(monkeypatch):
    """Swap httpx.AsyncClient inside `service.py` for our fake and pin the
    canonical Coexistence webhook URL so assertions don't depend on the
    BACKEND_URL env var the developer's shell may have set."""
    monkeypatch.setattr(wa_service.httpx, "AsyncClient", _FakeClient)
    from routers import whatsapp_connect  # noqa: PLC0415
    monkeypatch.setattr(
        whatsapp_connect,
        "_coexistence_webhook_url",
        lambda: "https://api.nahlah.ai/webhook/whatsapp/360dialog",
    )
    return _FakeClient


# ── Helper-level tests ─────────────────────────────────────────────────────


class TestDialog360WabaWebhookHelpers:
    def test_set_waba_webhook_posts_expected_payload(self, patch_httpx):
        """POST /waba_webhook with url + headers + override_all=True."""
        patch_httpx.responses[("POST", f"{wa_service.D360_BASE}/waba_webhook")] = _FakeResp(
            200, {"message": "Webhook config will be set"},
        )

        result = asyncio.run(
            wa_service.dialog360_set_waba_webhook(
                api_key="d360_secret_key",
                url="https://api.nahlah.ai/webhook/whatsapp/360dialog",
                headers={"X-Nahla-Coexistence-Secret": "shh"},
                override_all=True,
            )
        )

        assert "error" not in result
        assert any(
            c["method"] == "POST"
            and c["url"].endswith("/waba_webhook")
            and c["json"] == {
                "url": "https://api.nahlah.ai/webhook/whatsapp/360dialog",
                "override_all": True,
                "headers": {"X-Nahla-Coexistence-Secret": "shh"},
            }
            and c["headers"].get("D360-API-KEY") == "d360_secret_key"
            and c["headers"].get("Content-Type") == "application/json"
            for c in patch_httpx.calls
        ), patch_httpx.calls

    def test_set_waba_webhook_surfaces_http_errors(self, patch_httpx):
        patch_httpx.responses[("POST", f"{wa_service.D360_BASE}/waba_webhook")] = _FakeResp(
            401, {"error": "Invalid API key"},
        )
        result = asyncio.run(
            wa_service.dialog360_set_waba_webhook(
                api_key="bad_key",
                url="https://api.nahlah.ai/webhook/whatsapp/360dialog",
            )
        )
        assert result["success"] is False
        assert result["http_status"] == 401
        assert result.get("error_type") == "remote_error"

    def test_get_waba_webhook_returns_full_config(self, patch_httpx):
        patch_httpx.responses[("GET", f"{wa_service.D360_BASE}/waba_webhook")] = _FakeResp(
            200,
            {
                "url":                  "https://api.nahlah.ai/webhook/whatsapp/360dialog",
                "headers":              {"X-Nahla-Coexistence-Secret": "shh"},
                "waba_id":              1749448639704788,
                "numbers_on_this_waba": ["1061057720431678", "100543193146977"],
            },
        )
        out = asyncio.run(wa_service.dialog360_get_waba_webhook(api_key="d360_secret_key"))
        assert out.get("remote_url_present") is True
        assert out.get("numbers_count") == 2
        assert out.get("has_waba_id") is True
        assert "url" not in out
        assert "numbers_on_this_waba" not in out
        assert "headers" not in out


# ── Admin endpoint tests ───────────────────────────────────────────────────


class TestAdminCoexistenceAutoConfigureSetsBothScopes:
    """Auto-configure must push BOTH the channel webhook AND the WABA webhook
    on every call. Setting only the channel scope re-introduces the bug where
    a phone_number_id rotation silently kills inbound."""

    def test_auto_configure_calls_channel_and_waba(self, patch_httpx, monkeypatch):
        from routers import whatsapp_connect

        db, _ = _make_db()
        _seed_conn(db, tenant_id=1)

        patch_httpx.responses[("POST", f"{wa_service.D360_BASE}/v1/configs/webhook")] = _FakeResp(
            200, {"url": "https://api.nahlah.ai/webhook/whatsapp/360dialog"},
        )
        patch_httpx.responses[("POST", f"{wa_service.D360_BASE}/waba_webhook")] = _FakeResp(
            200, {"message": "Webhook config will be set"},
        )

        body = whatsapp_connect._TenantOnly(tenant_id=1)
        out = asyncio.run(
            whatsapp_connect.admin_coexistence_auto_configure(
                body=body,
                db=db,
                _admin={"sub": "admin@nahla"},
            )
        )

        assert out["ok"] is True
        assert out["channel_ok"] is True
        assert out["waba_ok"] is True

        posted_urls = [c["url"] for c in patch_httpx.calls if c["method"] == "POST"]
        assert any(u.endswith("/v1/configs/webhook") for u in posted_urls)
        assert any(u.endswith("/waba_webhook") for u in posted_urls)

        # Both bodies must carry the same secret header so the receiving
        # router can authenticate both scopes.
        post_calls = [c for c in patch_httpx.calls if c["method"] == "POST"]
        for c in post_calls:
            body_json = c.get("json") or {}
            assert body_json.get("headers", {}).get("X-Nahla-Coexistence-Secret"), c

        # The WABA call MUST request override_all=True; anything less leaves
        # stale per-channel webhooks in place.
        waba_call = next(c for c in post_calls if c["url"].endswith("/waba_webhook"))
        assert waba_call["json"].get("override_all") is True

    def test_auto_configure_marks_verified_when_only_waba_succeeds(self, patch_httpx, monkeypatch):
        """A merchant whose channel API key was rotated may temporarily fail
        the per-channel write, but if the WABA scope accepts our URL the
        pipe is still alive (WABA acts as a fallback)."""
        from routers import whatsapp_connect

        db, _ = _make_db()
        conn = _seed_conn(db, tenant_id=2)

        patch_httpx.responses[("POST", f"{wa_service.D360_BASE}/v1/configs/webhook")] = _FakeResp(
            500, {"error": "channel transient"},
        )
        patch_httpx.responses[("POST", f"{wa_service.D360_BASE}/waba_webhook")] = _FakeResp(
            200, {"message": "Webhook config will be set"},
        )

        body = whatsapp_connect._TenantOnly(tenant_id=2)
        out = asyncio.run(
            whatsapp_connect.admin_coexistence_auto_configure(
                body=body, db=db, _admin={"sub": "admin@nahla"},
            )
        )

        assert out["channel_ok"] is False
        assert out["waba_ok"]    is True
        db.refresh(conn)
        assert conn.webhook_verified is True


class TestAdminCoexistenceWabaWebhookRead:
    def test_read_reports_drift_when_local_phone_not_on_waba(self, patch_httpx):
        from routers import whatsapp_connect

        db, _ = _make_db()
        _seed_conn(db, tenant_id=3, phone_id="1061057720431678")

        # 360dialog reports the WABA only has the *historical* number on it.
        patch_httpx.responses[("GET", f"{wa_service.D360_BASE}/v1/configs/webhook")] = _FakeResp(
            200, {"url": "https://api.nahlah.ai/webhook/whatsapp/360dialog"},
        )
        patch_httpx.responses[("GET", f"{wa_service.D360_BASE}/waba_webhook")] = _FakeResp(
            200,
            {
                "url":                  "https://api.nahlah.ai/webhook/whatsapp/360dialog",
                "waba_id":              1749448639704788,
                "numbers_on_this_waba": ["100543193146977"],
            },
        )

        out = asyncio.run(
            whatsapp_connect.admin_coexistence_waba_webhook_read(
                tenant_id=3, db=db, _admin={"sub": "admin@nahla"},
            )
        )

        assert out["channel"]["matches"] is True
        assert out["waba"]["matches"]    is True
        assert out["waba"]["numbers_on_this_waba_count"] == 1
        assert out["waba"]["waba_id_remote_present"] is True
        assert "numbers_on_this_waba" not in out["waba"]
        assert out["phone_id_drift_with_360dialog"] is True

    def test_read_no_drift_when_local_phone_listed(self, patch_httpx):
        from routers import whatsapp_connect

        db, _ = _make_db()
        _seed_conn(db, tenant_id=4, phone_id="1061057720431678")

        patch_httpx.responses[("GET", f"{wa_service.D360_BASE}/v1/configs/webhook")] = _FakeResp(
            200, {"url": "https://api.nahlah.ai/webhook/whatsapp/360dialog"},
        )
        patch_httpx.responses[("GET", f"{wa_service.D360_BASE}/waba_webhook")] = _FakeResp(
            200,
            {
                "url":                  "https://api.nahlah.ai/webhook/whatsapp/360dialog",
                "waba_id":              1749448639704788,
                "numbers_on_this_waba": ["1061057720431678", "100543193146977"],
            },
        )

        out = asyncio.run(
            whatsapp_connect.admin_coexistence_waba_webhook_read(
                tenant_id=4, db=db, _admin={"sub": "admin@nahla"},
            )
        )

        assert out["phone_id_drift_with_360dialog"] is False
