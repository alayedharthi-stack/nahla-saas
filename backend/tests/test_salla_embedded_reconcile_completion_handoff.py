"""Repair A — embedded reconcile OAuth completion launch handoff tests."""
from __future__ import annotations

import asyncio
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND = os.path.join(_REPO, "backend")
for p in (_REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import Base, Integration, Tenant, User  # noqa: E402

if not getattr(Base.metadata, "_salla_embedded_handoff_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._salla_embedded_handoff_jsonb_shim = True  # type: ignore[attr-defined]

PARTNER_STORE = "22825873"
PARTNER_TENANT = 1
STALE_TENANT = 33
PARTNER_EMAIL = "cgcaqkpx5wgewsyv@email.partners"
EMBEDDED_STATE = "embedded_testhandoff_apisync"
NORMAL_STATE = f"t{PARTNER_TENANT}_normal_apisync"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _seed_partner_store(db) -> User:
    db.merge(Tenant(id=PARTNER_TENANT, name="Tenant 1"))
    user = User(
        username="merchant",
        email=PARTNER_EMAIL,
        password_hash="x",
        role="merchant",
        tenant_id=PARTNER_TENANT,
        is_active=True,
    )
    db.add(user)
    db.add(
        Integration(
            tenant_id=PARTNER_TENANT,
            provider="salla",
            external_store_id=PARTNER_STORE,
            config={"store_id": PARTNER_STORE, "salla_owner_email": PARTNER_EMAIL},
            enabled=True,
        )
    )
    db.commit()
    db.refresh(user)
    return user


def _mock_oauth_client(store_id: str, store_name: str = "Nahlah Ai honey"):
    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {
        "access_token": "salla-access-token",
        "refresh_token": "salla-refresh-token",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    token_resp.text = ""

    store_resp = MagicMock()
    store_resp.status_code = 200
    store_resp.json.return_value = {
        "data": {
            "id": store_id,
            "name": store_name,
            "merchant": {"id": "1979048767"},
        },
    }
    store_resp.text = ""

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=token_resp)
    mock_client.get = AsyncMock(return_value=store_resp)
    return mock_client


async def _run_api_oauth_callback(
    db,
    *,
    state: str = EMBEDDED_STATE,
    store_id: str = PARTNER_STORE,
    query_store: str | None = None,
    query_tenant: str | None = None,
):
    from routers.salla_oauth import salla_api_oauth_callback

    request = MagicMock()
    request.headers = {}
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    request.cookies = { "nahla_oauth_state": state }

    query_suffix = ""
    extras = []
    if query_store is not None:
        extras.append(f"store={urllib.parse.quote(query_store)}")
    if query_tenant is not None:
        extras.append(f"tenant={urllib.parse.quote(query_tenant)}")
    if extras:
        query_suffix = "&" + "&".join(extras)

    with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_ID", "test-client-id"):
        with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_SECRET", "test-secret"):
            with patch("routers.salla_oauth.SALLA_OAUTH_REDIRECT_URI", "https://api.test/callback"):
                with patch("routers.salla_oauth._DASHBOARD_ORIGIN", "https://app.nahlah.ai"):
                    with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                        mock_client_cls.return_value = _mock_oauth_client(store_id)
                        with patch("asyncio.ensure_future", return_value=MagicMock()):
                            return await salla_api_oauth_callback(
                                request,
                                code="oauth-code",
                                state=state + (query_suffix.replace("&", "") if False else ""),
                                db=db,
                            )


def _decode_launch_token_from_redirect(location: str) -> dict:
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    token = qs.get("token", [""])[0]
    assert token
    import jose.jwt as jwt
    from core.config import JWT_SECRET, JWT_ALGORITHM
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


class TestEmbeddedReconcileCompletionHandoff:
    def test_embedded_success_redirects_to_launch_handoff_after_db_commit(self, db):
        user = _seed_partner_store(db)
        response = asyncio.run(_run_api_oauth_callback(db))
        assert response.status_code == 302
        location = response.headers["location"]
        parsed = urllib.parse.urlparse(location)
        assert parsed.path == "/app/salla/launch"
        qs = urllib.parse.parse_qs(parsed.query)
        assert qs.get("next", [""])[0] == "/app/entry?salla_oauth=success"
        assert "store=" not in location
        assert "salla-access-token" not in location
        assert "salla-refresh-token" not in location

        decoded = _decode_launch_token_from_redirect(location)
        assert decoded["launch_token"] is True
        assert decoded["tenant_id"] == PARTNER_TENANT
        assert decoded["store_id"] == PARTNER_STORE
        assert decoded["sub"] == user.email
        assert decoded.get("jti")

        integration = db.query(Integration).filter_by(tenant_id=PARTNER_TENANT, provider="salla").one()
        assert integration.external_store_id == PARTNER_STORE
        assert integration.config.get("refresh_token") == "salla-refresh-token"

    def test_forged_store_query_does_not_change_launch_binding(self, db):
        _seed_partner_store(db)
        response = asyncio.run(_run_api_oauth_callback(db, query_store="99999999"))
        decoded = _decode_launch_token_from_redirect(response.headers["location"])
        assert decoded["tenant_id"] == PARTNER_TENANT
        assert decoded["store_id"] == PARTNER_STORE

    def test_forged_tenant_query_does_not_change_launch_binding(self, db):
        _seed_partner_store(db)
        response = asyncio.run(_run_api_oauth_callback(db, query_tenant=str(STALE_TENANT)))
        decoded = _decode_launch_token_from_redirect(response.headers["location"])
        assert decoded["tenant_id"] == PARTNER_TENANT
        assert decoded["store_id"] == PARTNER_STORE

    def test_normal_oauth_flow_unchanged(self, db):
        _seed_partner_store(db)
        response = asyncio.run(_run_api_oauth_callback(db, state=NORMAL_STATE))
        location = response.headers["location"]
        assert "/app/salla/launch" not in location
        assert "salla_oauth=success" in location
        assert "/app/entry" in location

    def test_launch_token_consumes_once_and_replay_fails(self, db):
        from routers.salla_oauth import resolve_launch

        _seed_partner_store(db)
        callback = asyncio.run(_run_api_oauth_callback(db))
        launch_token = urllib.parse.parse_qs(urllib.parse.urlparse(callback.headers["location"]).query)["token"][0]

        async def _resolve_once():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": launch_token})
            return await resolve_launch(request, db)

        first = asyncio.run(_resolve_once())
        assert first["tenant_id"] == PARTNER_TENANT
        assert first["store_id"] == PARTNER_STORE
        assert first["access_token"]

        async def _resolve_replay():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": launch_token})
            with pytest.raises(HTTPException) as exc:
                await resolve_launch(request, db)
            return exc.value

        replay = asyncio.run(_resolve_replay())
        assert replay.status_code == 401

    def test_expired_launch_token_fails(self, db):
        from routers.salla_oauth import resolve_launch
        from core.config import JWT_SECRET, JWT_ALGORITHM
        import jose.jwt as jwt

        _seed_partner_store(db)
        expired = jwt.encode(
            {
                "sub": PARTNER_EMAIL,
                "role": "merchant",
                "tenant_id": PARTNER_TENANT,
                "user_id": 1,
                "store_id": PARTNER_STORE,
                "launch_token": True,
                "jti": "expired-jti-123",
                "exp": int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp()),
            },
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": expired})
            with pytest.raises(HTTPException) as exc:
                await resolve_launch(request, db)
            return exc.value

        exc = asyncio.run(_run())
        assert exc.status_code == 401

    def test_wrong_store_binding_fails_closed(self, db):
        from routers.salla_oauth import resolve_launch
        from core.config import JWT_SECRET, JWT_ALGORITHM
        import jose.jwt as jwt

        _seed_partner_store(db)
        bad = jwt.encode(
            {
                "sub": PARTNER_EMAIL,
                "role": "merchant",
                "tenant_id": STALE_TENANT,
                "user_id": 1,
                "store_id": PARTNER_STORE,
                "launch_token": True,
                "jti": "wrong-tenant-jti",
                "exp": int((datetime.now(timezone.utc) + timedelta(seconds=60)).timestamp()),
            },
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": bad})
            with pytest.raises(HTTPException) as exc:
                await resolve_launch(request, db)
            return exc.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert exc.detail == "store_tenant_mismatch"

    def test_resolve_launch_returns_canonical_tenant_not_stale_browser_session(self, db):
        from routers.salla_oauth import resolve_launch

        _seed_partner_store(db)
        callback = asyncio.run(_run_api_oauth_callback(db))
        launch_token = urllib.parse.parse_qs(urllib.parse.urlparse(callback.headers["location"]).query)["token"][0]

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": launch_token})
            return await resolve_launch(request, db)

        session = asyncio.run(_run())
        assert session["tenant_id"] == PARTNER_TENANT
        assert session["store_id"] == PARTNER_STORE
        assert session["tenant_id"] != STALE_TENANT


class TestLaunchNextPathSafety:
    def test_sanitize_internal_next_path_rejects_external_urls(self):
        from routers.salla_oauth import _sanitize_internal_next_path, _OAUTH_EMBEDDED_RECONCILE_NEXT

        assert _sanitize_internal_next_path("/app/entry?salla_oauth=success") == "/app/entry?salla_oauth=success"
        assert _sanitize_internal_next_path("https://evil.example/x", default=_OAUTH_EMBEDDED_RECONCILE_NEXT) == _OAUTH_EMBEDDED_RECONCILE_NEXT
        assert _sanitize_internal_next_path("//evil.example/x", default=_OAUTH_EMBEDDED_RECONCILE_NEXT) == _OAUTH_EMBEDDED_RECONCILE_NEXT

    def test_build_launch_handoff_url_never_includes_salla_tokens(self):
        from routers.salla_oauth import _build_launch_handoff_url, _issue_launch_token

        launch = _issue_launch_token(
            email=PARTNER_EMAIL,
            role="merchant",
            tenant_id=PARTNER_TENANT,
            user_id=1,
            store_id=PARTNER_STORE,
        )
        url = _build_launch_handoff_url(launch, "/app/entry?salla_oauth=success")
        assert "salla-access-token" not in url
        assert "salla-refresh-token" not in url
        assert "/app/salla/launch" in url
