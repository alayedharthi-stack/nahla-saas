"""Repair A v2 — opaque launch handoff + merchant identity security tests."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.parse
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
ADMIN_EMAIL = "admin@example.com"
PARTNER_EMAIL = "cgcaqkpx5wgewsyv@email.partners"
EMBEDDED_STATE = "embedded_testhandoff_apisync"
NORMAL_STATE = f"t{PARTNER_TENANT}_normal_apisync"


class FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        del ex
        if nx and key in self._data:
            return False
        self._data[key] = value
        return True

    def getdel(self, key: str):
        return self._data.pop(key, None)


@pytest.fixture()
def fake_redis():
    store = FakeRedis()
    with patch("core.launch_handoff.get_redis", return_value=store):
        yield store


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


def _seed_partner_store(db, *, owner_email: str = PARTNER_EMAIL) -> User:
    db.merge(Tenant(id=PARTNER_TENANT, name="Tenant 1"))
    user = User(
        username="merchant",
        email=owner_email,
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
            config={"store_id": PARTNER_STORE, "salla_owner_email": owner_email},
            enabled=True,
        )
    )
    db.commit()
    db.refresh(user)
    return user


def _seed_admin_and_merchant(db) -> tuple[User, User]:
    db.merge(Tenant(id=PARTNER_TENANT, name="Tenant 1"))
    admin = User(
        username="admin",
        email=ADMIN_EMAIL,
        password_hash="x",
        role="admin",
        tenant_id=PARTNER_TENANT,
        is_active=True,
    )
    merchant = User(
        username="merchant",
        email=PARTNER_EMAIL,
        password_hash="x",
        role="merchant",
        tenant_id=PARTNER_TENANT,
        is_active=True,
    )
    db.add(admin)
    db.add(merchant)
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
    db.refresh(admin)
    db.refresh(merchant)
    return admin, merchant


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


async def _run_api_oauth_callback(db, *, state: str = EMBEDDED_STATE, store_id: str = PARTNER_STORE):
    from routers.salla_oauth import salla_api_oauth_callback

    request = MagicMock()
    request.headers = {}
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    request.cookies = {"nahla_oauth_state": state}

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
                                state=state,
                                db=db,
                            )


def _extract_opaque_token(location: str) -> str:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    token = qs.get("token", [""])[0]
    assert token
    assert token.count(".") != 2, "signed JWT must not appear in URL"
    return token


class TestEmbeddedReconcileCompletionHandoff:
    def test_embedded_success_redirects_to_opaque_launch_handoff(self, db, fake_redis):
        _seed_partner_store(db)
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
        _extract_opaque_token(location)

        integration = db.query(Integration).filter_by(tenant_id=PARTNER_TENANT, provider="salla").one()
        assert integration.external_store_id == PARTNER_STORE
        assert integration.config.get("refresh_token") == "salla-refresh-token"

    def test_merchant_selected_when_admin_also_exists(self, db, fake_redis):
        admin, merchant = _seed_admin_and_merchant(db)
        assert admin.id < merchant.id
        response = asyncio.run(_run_api_oauth_callback(db))
        token = _extract_opaque_token(response.headers["location"])
        from core.launch_handoff import consume_launch_handoff

        record = consume_launch_handoff(token)
        assert record is not None
        assert record.user_id == merchant.id
        assert record.role == "merchant"

    def test_only_admin_creates_merchant_not_admin_handoff(self, db, fake_redis):
        db.merge(Tenant(id=PARTNER_TENANT, name="Tenant 1"))
        admin = User(
            username="admin",
            email=ADMIN_EMAIL,
            password_hash="x",
            role="admin",
            tenant_id=PARTNER_TENANT,
            is_active=True,
        )
        db.add(admin)
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
        response = asyncio.run(_run_api_oauth_callback(db))
        token = _extract_opaque_token(response.headers["location"])
        from core.launch_handoff import consume_launch_handoff

        record = consume_launch_handoff(token)
        assert record is not None
        assert record.role == "merchant"
        assert record.user_id != admin.id
        resolved = db.query(User).filter(User.id == record.user_id).one()
        assert resolved.role == "merchant"

    def test_normal_oauth_flow_unchanged(self, db, fake_redis):
        _seed_partner_store(db)
        response = asyncio.run(_run_api_oauth_callback(db, state=NORMAL_STATE))
        location = response.headers["location"]
        assert "/app/salla/launch" not in location
        assert "salla_oauth=success" in location

    def test_opaque_token_consumes_once_and_replay_fails(self, db, fake_redis):
        from routers.salla_oauth import resolve_launch

        merchant = _seed_partner_store(db)
        callback = asyncio.run(_run_api_oauth_callback(db))
        launch_token = _extract_opaque_token(callback.headers["location"])

        async def _resolve_once():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": launch_token})
            return await resolve_launch(request, db)

        first = asyncio.run(_resolve_once())
        assert first["tenant_id"] == PARTNER_TENANT
        assert first["store_id"] == PARTNER_STORE
        assert first["role"] == "merchant"
        assert first["next_path"] == "/app/entry?salla_oauth=success"
        assert first["access_token"]

        async def _resolve_replay():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": launch_token})
            with pytest.raises(HTTPException) as exc:
                await resolve_launch(request, db)
            return exc.value

        replay = asyncio.run(_resolve_replay())
        assert replay.status_code == 401

    def test_second_consumer_with_shared_store_fails(self, db, fake_redis):
        from core.launch_handoff import issue_launch_handoff, consume_launch_handoff

        merchant = _seed_partner_store(db)
        handle = issue_launch_handoff(
            tenant_id=PARTNER_TENANT,
            store_id=PARTNER_STORE,
            user_id=merchant.id,
            email=merchant.email,
            next_path="/app/entry?salla_oauth=success",
            role="merchant",
        )
        assert consume_launch_handoff(handle) is not None
        assert consume_launch_handoff(handle) is None

    def test_redis_unavailable_issue_and_resolve_fail_closed(self, db):
        from core.launch_handoff import issue_launch_handoff, consume_launch_handoff, LaunchHandoffUnavailable
        from routers.salla_oauth import resolve_launch

        merchant = _seed_partner_store(db)
        with patch("core.launch_handoff.get_redis", return_value=None):
            with pytest.raises(LaunchHandoffUnavailable):
                issue_launch_handoff(
                    tenant_id=PARTNER_TENANT,
                    store_id=PARTNER_STORE,
                    user_id=merchant.id,
                    email=merchant.email,
                    next_path="/overview",
                    role="merchant",
                )
            assert consume_launch_handoff("opaque-handle") is None

        async def _resolve():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "opaque-handle"})
            with pytest.raises(HTTPException) as exc:
                await resolve_launch(request, db)
            return exc.value

        exc = asyncio.run(_resolve())
        assert exc.status_code == 401

    def test_signed_jwt_launch_credential_rejected(self, db, fake_redis):
        from routers.salla_oauth import resolve_launch
        from core.config import JWT_SECRET, JWT_ALGORITHM
        import jose.jwt as jwt

        _seed_partner_store(db)
        signed = jwt.encode(
            {
                "sub": PARTNER_EMAIL,
                "role": "merchant",
                "tenant_id": PARTNER_TENANT,
                "store_id": PARTNER_STORE,
                "launch_token": True,
                "jti": "legacy",
                "exp": 9999999999,
            },
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": signed})
            with pytest.raises(HTTPException) as exc:
                await resolve_launch(request, db)
            return exc.value

        exc = asyncio.run(_run())
        assert exc.status_code == 401

    def test_wrong_store_binding_fails_closed(self, db, fake_redis):
        from routers.salla_oauth import resolve_launch
        from core.launch_handoff import issue_launch_handoff

        _seed_partner_store(db)
        merchant = db.query(User).filter(User.email == PARTNER_EMAIL).one()
        bad = issue_launch_handoff(
            tenant_id=STALE_TENANT,
            store_id=PARTNER_STORE,
            user_id=merchant.id,
            email=merchant.email,
            next_path="/app/entry?salla_oauth=success",
            role="merchant",
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

    def test_resolve_returns_canonical_tenant_not_stale_browser_state(self, db, fake_redis):
        from routers.salla_oauth import resolve_launch

        _seed_partner_store(db)
        callback = asyncio.run(_run_api_oauth_callback(db))
        launch_token = _extract_opaque_token(callback.headers["location"])

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": launch_token})
            return await resolve_launch(request, db)

        session = asyncio.run(_run())
        assert session["tenant_id"] == PARTNER_TENANT
        assert session["tenant_id"] != STALE_TENANT


class TestLaunchNextPathSafety:
    def test_sanitize_internal_next_path_rejects_external_urls(self):
        from routers.salla_oauth import _sanitize_internal_next_path, _OAUTH_EMBEDDED_RECONCILE_NEXT

        assert _sanitize_internal_next_path("/app/entry?salla_oauth=success") == "/app/entry?salla_oauth=success"
        assert _sanitize_internal_next_path("https://evil.example/x", default=_OAUTH_EMBEDDED_RECONCILE_NEXT) == _OAUTH_EMBEDDED_RECONCILE_NEXT

    def test_build_launch_handoff_url_never_includes_secrets(self):
        from routers.salla_oauth import _build_launch_handoff_url

        url = _build_launch_handoff_url("opaque-handle-abc", "/app/entry?salla_oauth=success")
        assert "salla-access-token" not in url
        assert "salla-refresh-token" not in url
        assert url.count(".") == 0 or "opaque-handle-abc" in url
