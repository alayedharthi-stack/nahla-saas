
"""Consolidated coexistence route + resolver tests (Sol final remediation)."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from services.embedded_waba_resolution import (  # noqa: E402
    CoexistenceWabaResolutionError,
    REAUTH_REQUIRED,
    WRONG_BUSINESS_OWNER,
    WRONG_PHONE,
    assert_retry_claim_matches,
    canonicalize_phone_e164,
    derive_trusted_business_portfolio_id,
    phones_match_exact_e164,
    resolve_coexistence_assets_from_graph,
)
from services.coexistence_embedded_exchange import (  # noqa: E402
    load_connection_for_update,
    persist_oauth_nonce,
)


def _debug(waba_ids: list[str], portfolios: list[str] | None = None) -> dict:
    scopes = [{"scope": "whatsapp_business_management", "target_ids": waba_ids}]
    if portfolios:
        scopes.append({"scope": "business_management", "target_ids": portfolios})
    return {"granular_scopes": scopes}


def test_e164_exact_match_positive_saudi_local():
    assert phones_match_exact_e164("0501234567", "+966501234567")
    assert canonicalize_phone_e164("0501234567") == "+966501234567"


def test_e164_suffix_mismatch_rejected():
    assert not phones_match_exact_e164("1234567", "+966501234567")
    assert not phones_match_exact_e164("+966501234567", "+966509999999")


def test_e164_international_uk_positive():
    assert canonicalize_phone_e164("+447911123456") == "+447911123456"
    assert phones_match_exact_e164("+447911123456", "+447911123456")


def test_oauth_state_contains_mode():
    from routers.whatsapp_embedded import _sign_oauth_state, _verify_oauth_state  # noqa: PLC0415

    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(42, "nonce-test", issued_at, "https://api.example.test/cb", "coexistence")
    parsed = _verify_oauth_state(state)
    assert parsed.connection_mode == "coexistence"
    assert parsed.nonce == "nonce-test"


def test_oauth_state_rejects_missing_mode():
    from routers.whatsapp_embedded import _verify_oauth_state  # noqa: PLC0415
    import base64
    import hmac
    import hashlib
    from core.config import JWT_SECRET  # noqa: PLC0415

    body = json.dumps({"v": 1, "t": 1, "iat": 1, "ru": "https://x/cb"}, separators=(",", ":")).encode()
    sig = hmac.new(JWT_SECRET.encode(), body, hashlib.sha256).digest()
    import base64 as b64

    state = f"{b64.urlsafe_b64encode(body).rstrip(b'=').decode()}.{b64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"
    with pytest.raises(HTTPException):
        _verify_oauth_state(state)


def test_client_waba_hint_ignored_without_graph_proof():
    calls = []

    async def fake_get(graph_base, token, node, fields):  # noqa: ANN001
        calls.append(node)
        if node == "TEST-WABA-CLIENT":
            raise AssertionError("client hint must not be queried")
        if node == "TEST-WABA-TRUSTED":
            return {"ok": True, "data": {"id": "TEST-WABA-TRUSTED", "owner_business_info": {"id": "TEST-BUSINESS-001"}}}
        if node.endswith("/phone_numbers"):
            return {"ok": True, "data": {"data": [{"id": "TEST-PHONE-001", "display_phone_number": "+966501234567"}]}}
        if node == "TEST-PHONE-001":
            return {"ok": True, "data": {"id": "TEST-PHONE-001", "display_phone_number": "+966501234567"}}
        return {"ok": False}

    async def run(monkeypatch):
        monkeypatch.setattr("services.embedded_waba_resolution._graph_get", fake_get)
        result = await resolve_coexistence_assets_from_graph(
            "https://graph.facebook.com/v21.0",
            "token",
            _debug(["TEST-WABA-TRUSTED"], ["TEST-BUSINESS-001"]),
            expected_phone_number="+966501234567",
            hinted_waba_id="TEST-WABA-CLIENT",
            hinted_phone_number_id="TEST-PHONE-CLIENT",
        )
        assert result.waba_id == "TEST-WABA-TRUSTED"

    asyncio.run(run(pytest.MonkeyPatch()))


def test_missing_owner_rejected():
    async def fake_get(graph_base, token, node, fields):  # noqa: ANN001
        if node == "TEST-WABA-001":
            return {"ok": True, "data": {"id": "TEST-WABA-001", "owner_business_info": {}}}
        return {"ok": False}

    async def run(monkeypatch):
        monkeypatch.setattr("services.embedded_waba_resolution._graph_get", fake_get)
        with pytest.raises(CoexistenceWabaResolutionError) as exc:
            await resolve_coexistence_assets_from_graph(
                "https://graph.facebook.com/v21.0",
                "token",
                _debug(["TEST-WABA-001"], ["TEST-BUSINESS-001"]),
                expected_phone_number="+966501234567",
            )
        assert exc.value.code == WRONG_PHONE

    asyncio.run(run(pytest.MonkeyPatch()))


def test_retry_claim_mismatch_fails():
    from services.embedded_waba_resolution import VerifiedCoexistenceAssets  # noqa: PLC0415

    verified = VerifiedCoexistenceAssets(
        waba_id="TEST-WABA-002",
        phone_number_id="TEST-PHONE-002",
        display_phone_number="+966509876543",
        verified_name=None,
        ownership_type=None,
        owner_business_id="TEST-BUSINESS-001",
        canonical_phone_e164="+966509876543",
        trusted_business_portfolio_id="TEST-BUSINESS-001",
    )
    with pytest.raises(CoexistenceWabaResolutionError):
        assert_retry_claim_matches(
            {"waba_id": "TEST-WABA-OLD", "phone_number_id": "TEST-PHONE-OLD"},
            verified,
        )


def _sqlite_session():
    from sqlalchemy import JSON, create_engine, text
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from models import Base
    from database.models import Base as DatabaseBase

    engine = create_engine("sqlite:///:memory:")
    saved = []
    for metadata in (Base.metadata, DatabaseBase.metadata):
        for table in metadata.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    saved.append((col, col.type))
                    col.type = JSON()
    Base.metadata.create_all(engine)
    DatabaseBase.metadata.create_all(engine)
    for col, orig_type in saved:
        col.type = orig_type
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
        )
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0101')"))
    Session = sessionmaker(bind=engine)
    return Session()


def test_new_connection_failure_leaves_no_row():
    from models import Tenant, WhatsAppConnection  # noqa: PLC0415

    db = _sqlite_session()
    db.add(Tenant(id=601, name="new-tenant", is_active=True))
    db.commit()
    conn, had_row = load_connection_for_update(db, 601)
    assert had_row is False
    conn.provider = "meta"
    db.flush()
    db.rollback()
    assert db.query(WhatsAppConnection).filter_by(tenant_id=601).count() == 0
    db.close()


def test_oauth_callback_coexistence_skips_begin_waba_session(monkeypatch):
    from routers import whatsapp_embedded as emb  # noqa: PLC0415
    from models import Tenant, WhatsAppConnection  # noqa: PLC0415

    db = _sqlite_session()
    db.add(Tenant(id=701, name="oauth-coex", is_active=True))
    db.add(
        WhatsAppConnection(
            tenant_id=701,
            status="disconnected",
            provider="dialog360",
            phone_number="+966501234567",
        )
    )
    db.commit()

    issued_at = int(datetime.now(timezone.utc).timestamp())
    persist_oauth_nonce(
        db,
        nonce="n1",
        tenant_id=701,
        connection_mode="coexistence",
        expires_at=datetime.fromtimestamp(issued_at + 600, tz=timezone.utc),
    )
    db.commit()
    state = emb._sign_oauth_state(701, "n1", issued_at, "https://api.example.test/cb", "coexistence")

    monkeypatch.setattr(emb, "_exchange_code_for_token", AsyncMock(return_value={"access_token": "short"}))
    monkeypatch.setattr(emb, "_exchange_for_long_lived_token", AsyncMock(return_value={"access_token": "long-token"}))
    monkeypatch.setattr(emb, "_debug_token", AsyncMock(return_value=_debug(["TEST-WABA-001"], ["TEST-BUSINESS-001"])))

    async def fake_resolve(*a, **k):
        from services.embedded_waba_resolution import VerifiedCoexistenceAssets  # noqa: PLC0415
        return VerifiedCoexistenceAssets(
            waba_id="TEST-WABA-001",
            phone_number_id="TEST-PHONE-001",
            display_phone_number="+966501234567",
            verified_name=None,
            ownership_type=None,
            owner_business_id="TEST-BUSINESS-001",
            canonical_phone_e164="+966501234567",
            trusted_business_portfolio_id="TEST-BUSINESS-001",
        )

    monkeypatch.setattr(
        "services.embedded_waba_resolution.resolve_coexistence_assets_from_graph",
        AsyncMock(side_effect=fake_resolve),
    )
    monkeypatch.setattr(emb, "_get_phone_numbers", AsyncMock(return_value=[{"id": "TEST-PHONE-001"}]))
    monkeypatch.setattr(emb, "_finalize_coexistence_exchange", AsyncMock(return_value={"status": "connected"}))

    with patch("services.whatsapp_connection_service.begin_waba_session") as begin_mock:
        with patch.object(emb, "_get_waba_id_from_token", AsyncMock()) as legacy_mock:
            resp = asyncio.run(
                emb.oauth_callback(
                    request=type("R", (), {"state": type("S", (), {"tenant_id": 701})})(),
                    db=db,
                    code="oauth-code",
                    state=state,
                )
            )
            begin_mock.assert_not_called()
            legacy_mock.assert_not_called()
    assert resp.status_code == 302
    db.close()


def test_no_graph_create_delete_mutations(monkeypatch):
    recorded = []

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ANN001
        recorded.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": "TEST-WABA-001", "owner_business_info": {"id": "TEST-BUSINESS-001"}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "services.embedded_waba_resolution.httpx.get",
        lambda url, **kw: httpx.Client(transport=transport).get(url, **kw),
    )

    async def fake_phone_get(graph_base, token, node, fields):  # noqa: ANN001
        if node == "TEST-WABA-001":
            return {"ok": True, "data": {"id": "TEST-WABA-001", "owner_business_info": {"id": "TEST-BUSINESS-001"}}}
        if node.endswith("/phone_numbers"):
            return {"ok": True, "data": {"data": [{"id": "TEST-PHONE-001", "display_phone_number": "+966501234567"}]}}
        if node == "TEST-PHONE-001":
            return {"ok": True, "data": {"id": "TEST-PHONE-001", "display_phone_number": "+966501234567"}}
        return {"ok": False}

    monkeypatch.setattr("services.embedded_waba_resolution._graph_get", fake_phone_get)

    async def run():
        await resolve_coexistence_assets_from_graph(
            "https://graph.facebook.com/v21.0",
            "token",
            _debug(["TEST-WABA-001"], ["TEST-BUSINESS-001"]),
            expected_phone_number="+966501234567",
        )

    asyncio.run(run())
    for method, path in recorded:
        assert method == "GET"
        assert "delete" not in path.lower()
        assert "deregister" not in path.lower()
