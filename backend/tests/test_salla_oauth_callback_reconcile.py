"""
End-to-end regression tests for legacy ``salla_oauth_callback``.

Covers the Tenant 47 ghost-tenant pattern and identity-conflict fail-closed paths.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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

if not getattr(Base.metadata, "_salla_oauth_cb_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._salla_oauth_cb_jsonb_shim = True  # type: ignore[attr-defined]

PARTNER_STORE = "22825873"
PARTNER_TENANT = 1
WRONG_TENANT = 47
PARTNER_EMAIL = "cgcaqkpx5wgewsyv@email.partners"
NEW_STORE = "99001122"
NEW_STORE_EMAIL = "new-store-99001122@salla-merchant.nahlah.ai"


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


def _seed_legacy_integration(db, *, tenant_id: int, store_id: str) -> Integration:
    db.merge(Tenant(id=tenant_id, name=f"Tenant {tenant_id}"))
    db.add(User(
        username="merchant",
        email=PARTNER_EMAIL,
        password_hash="x",
        role="merchant",
        tenant_id=tenant_id,
        is_active=True,
    ))
    row = Integration(
        tenant_id=tenant_id,
        provider="salla",
        external_store_id=None,
        config={"store_id": store_id, "salla_owner_email": PARTNER_EMAIL},
        enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _mock_oauth_client(store_id: str, store_name: str = "Nahlah Ai honey"):
    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {
        "access_token": "test-access",
        "refresh_token": "test-refresh",
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


async def _run_oauth_callback(db, *, code: str = "oauth-code", state: str = ""):
    from routers.salla_oauth import salla_oauth_callback

    request = MagicMock()
    request.headers = {}
    request.client = MagicMock()
    request.client.host = "127.0.0.1"

    scheduled: list = []

    def _capture_future(coro):
        scheduled.append(coro)
        return MagicMock()

    with patch("routers.salla_oauth.SALLA_CLIENT_ID", "test-client-id"):
        with patch("routers.salla_oauth.SALLA_CLIENT_SECRET", "test-secret"):
            with patch("routers.salla_oauth.SALLA_REDIRECT_URI", "https://api.test/callback"):
                with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                    mock_client_cls.return_value = _mock_oauth_client(PARTNER_STORE)
                    with patch("asyncio.ensure_future", side_effect=_capture_future):
                        with patch("services.email_service.enqueue_email"):
                            response = await salla_oauth_callback(
                                request,
                                code=code,
                                state=state,
                                db=db,
                            )
    return response, scheduled


class TestLegacyOAuthCallbackReconcile:
    def test_existing_merchant_state_lost_reclaims_legacy_owner(self, db):
        _seed_legacy_integration(db, tenant_id=PARTNER_TENANT, store_id=PARTNER_STORE)
        tenants_before = db.query(Tenant).count()
        users_before = db.query(User).count()

        response, scheduled = asyncio.run(_run_oauth_callback(db, state=""))

        assert response.status_code == 200
        assert db.query(Tenant).count() == tenants_before
        assert db.query(User).count() == users_before

        integration = (
            db.query(Integration)
            .filter_by(tenant_id=PARTNER_TENANT, provider="salla")
            .order_by(Integration.id.asc())
            .first()
        )
        assert integration is not None
        assert integration.tenant_id == PARTNER_TENANT
        assert integration.external_store_id == PARTNER_STORE
        assert integration.config.get("api_key") == "test-access"
        assert integration.enabled is True

        assert len(scheduled) == 1
        assert scheduled[0].cr_code.co_name == "_initial_sync"

    def test_identity_conflict_blocks_callback_without_sync(self, db):
        _seed_legacy_integration(db, tenant_id=PARTNER_TENANT, store_id=PARTNER_STORE)
        db.merge(Tenant(id=WRONG_TENANT, name="Ghost"))
        db.add(Integration(
            tenant_id=WRONG_TENANT,
            provider="salla",
            external_store_id=None,
            config={"store_id": PARTNER_STORE},
            enabled=True,
        ))
        db.commit()

        response, scheduled = asyncio.run(_run_oauth_callback(db, state=""))

        assert response.status_code == 403
        assert scheduled == []
        owner_integration = (
            db.query(Integration)
            .filter_by(tenant_id=PARTNER_TENANT, provider="salla")
            .order_by(Integration.id.asc())
            .first()
        )
        assert owner_integration is not None
        assert owner_integration.config.get("api_key") is None

    def test_new_store_creates_once_and_repeat_callback_is_idempotent(self, db):
        from routers.salla_oauth import salla_oauth_callback

        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        scheduled: list = []

        def _capture_future(coro):
            scheduled.append(coro)
            return MagicMock()

        async def _run_once():
            with patch("routers.salla_oauth.SALLA_CLIENT_ID", "test-client-id"):
                with patch("routers.salla_oauth.SALLA_CLIENT_SECRET", "test-secret"):
                    with patch("routers.salla_oauth.SALLA_REDIRECT_URI", "https://api.test/callback"):
                        with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                            mock_client_cls.return_value = _mock_oauth_client(NEW_STORE, "متجر تجريبي عام")
                            with patch("asyncio.ensure_future", side_effect=_capture_future):
                                with patch("services.email_service.enqueue_email"):
                                    with patch(
                                        "routers.salla_oauth.create_token",
                                        return_value="jwt",
                                    ):
                                        return await salla_oauth_callback(
                                            request,
                                            code="oauth-code",
                                            state="salla_new_testinstall",
                                            db=db,
                                        )

        first = asyncio.run(_run_once())
        tenants_after_first = db.query(Tenant).count()
        users_after_first = db.query(User).count()
        integration_after_first = (
            db.query(Integration)
            .filter(Integration.external_store_id == NEW_STORE)
            .count()
        )

        assert first.status_code == 200
        assert tenants_after_first == 1
        assert users_after_first == 1
        assert integration_after_first == 1

        second = asyncio.run(_run_once())
        assert second.status_code == 200
        assert db.query(Tenant).count() == tenants_after_first
        assert db.query(User).count() == users_after_first
        assert (
            db.query(Integration)
            .filter(Integration.external_store_id == NEW_STORE)
            .count()
        ) == 1
