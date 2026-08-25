"""Regression tests for Salla embedded OAuth-verified identity bindings."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND = os.path.join(_REPO, "backend")
for p in (_REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import (  # noqa: E402
    Base,
    Integration,
    SallaEmbeddedIdentityBinding,
    Tenant,
    User,
)

if not getattr(Base.metadata, "_salla_embedded_binding_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._salla_embedded_binding_jsonb_shim = True  # type: ignore[attr-defined]

PARTNER_STORE = "22825873"
PARTNER_ALT = "1979048767"
PARTNER_TENANT = 1
PARTNER_EMAIL = "cgcaqkpx5wgewsyv@email.partners"

GENERIC_STORE = "55112233"
GENERIC_ALT = "88776655"
GENERIC_TENANT = 5
GENERIC_EMAIL = "ahmad.salem@example.com"
GENERIC_STORE_NAME = "متجر تجريبي عام"

APP_ID = "test-embedded-app-id"
STATE_SUFFIX = "_apisync"


class FakeRedis:
    """Minimal Redis stand-in for reconciliation challenge tests."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        del ex
        if nx and key in self._data:
            return False
        self._data[key] = value
        return True

    def get(self, key: str):
        return self._data.get(key)

    def getdel(self, key: str):
        return self._data.pop(key, None)


def _postgres_reachable() -> bool:
    from tests.legacy_migration_drift_postgres_fixtures import _candidate_database_urls

    for url in _candidate_database_urls():
        try:
            engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return True
        except Exception:
            continue
    return False


@pytest.fixture()
def fake_redis():
    store = FakeRedis()
    with patch("core.redis_client.get_redis", return_value=store):
        with patch("services.salla_reconciliation_challenge.get_redis", return_value=store):
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


def _seed_store(
    db,
    *,
    tenant_id: int,
    canonical_store_id: str,
    alt_merchant_id: str = "",
    owner_email: str = "",
    store_name: str = "",
) -> Integration:
    db.merge(Tenant(id=tenant_id, name=f"Tenant {tenant_id}"))
    cfg = {"store_id": canonical_store_id}
    if alt_merchant_id:
        cfg["salla_merchant_id_alt"] = alt_merchant_id
        cfg["merchant_id"] = alt_merchant_id
    if owner_email:
        cfg["salla_owner_email"] = owner_email
    if store_name:
        cfg["store_name"] = store_name
    row = Integration(
        tenant_id=tenant_id,
        provider="salla",
        external_store_id=canonical_store_id,
        config=cfg,
        enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_merchant(db, *, tenant_id: int, email: str) -> User:
    user = User(
        username=email.split("@")[0],
        email=email,
        password_hash="x",
        role="merchant",
        tenant_id=tenant_id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_challenge(*, app_id: str = APP_ID, merchant_account_id: str):
    from services.salla_reconciliation_challenge import ReconciliationChallenge

    now = datetime.now(timezone.utc)
    return ReconciliationChallenge(
        nonce="test-nonce",
        provider="salla",
        app_id=app_id,
        merchant_account_id=merchant_account_id,
        created_at=now.isoformat(),
        expires_at=(now.replace(year=now.year + 1)).isoformat(),
    )


def _seed_active_binding(
    db,
    *,
    integration: Integration,
    merchant_account_id: str,
    app_id: str = APP_ID,
) -> SallaEmbeddedIdentityBinding:
    from services.salla_embedded_identity_binding import upsert_binding_from_oauth_reconcile

    challenge = _make_challenge(app_id=app_id, merchant_account_id=merchant_account_id)
    binding = upsert_binding_from_oauth_reconcile(
        db,
        challenge=challenge,
        canonical_store_id=integration.external_store_id,
        integration_id=integration.id,
        tenant_id=integration.tenant_id,
    )
    db.commit()
    return binding


def _merchant_only_introspect(merchant_id: str) -> dict:
    return {
        "success": True,
        "data": {
            "merchant_id": merchant_id,
            "user_id": "embedded-user",
            "exp": 9999999999,
        },
    }


def _mock_token_login_client(introspect_body: dict):
    mock_introspect_resp = MagicMock()
    mock_introspect_resp.status_code = 200
    mock_introspect_resp.json.return_value = introspect_body

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_introspect_resp)
    mock_client.get = AsyncMock()
    return mock_client


def _mock_oauth_client(store_id: str, merchant_id: str, store_name: str = "Store"):
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
            "merchant": {"id": merchant_id},
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
    state: str,
    store_id: str,
    merchant_id: str,
    store_name: str = "Store",
):
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
                        mock_client_cls.return_value = _mock_oauth_client(
                            store_id, merchant_id, store_name,
                        )
                        with patch("asyncio.ensure_future", return_value=MagicMock()):
                            return await salla_api_oauth_callback(
                                request,
                                code="oauth-code",
                                state=state,
                                db=db,
                            )


class TestReconciliationChallengeService:
    def test_create_resolve_and_consume_nonce(self, fake_redis):
        from services.salla_reconciliation_challenge import (
            consume_reconciliation_challenge,
            create_reconciliation_challenge,
            resolve_reconciliation_nonce,
        )

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=PARTNER_ALT,
        )
        assert nonce

        resolved = resolve_reconciliation_nonce(nonce)
        assert resolved is not None
        assert resolved.app_id == APP_ID
        assert resolved.merchant_account_id == PARTNER_ALT

        consumed = consume_reconciliation_challenge(nonce)
        assert consumed is not None
        assert resolve_reconciliation_nonce(nonce) is None

    def test_bind_oauth_state_and_resolve_by_state(self, fake_redis):
        from services.salla_reconciliation_challenge import (
            bind_oauth_state_to_reconciliation_challenge,
            create_reconciliation_challenge,
            resolve_reconciliation_challenge_for_oauth_state,
        )

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=GENERIC_ALT,
        )
        oauth_state = f"embedded_generic_test{STATE_SUFFIX}"
        bind_oauth_state_to_reconciliation_challenge(oauth_state, nonce=nonce)

        challenge = resolve_reconciliation_challenge_for_oauth_state(oauth_state)
        assert challenge is not None
        assert challenge.merchant_account_id == GENERIC_ALT

    def test_create_persists_nonce_payload_in_fake_redis(self, fake_redis):
        from services.salla_reconciliation_challenge import create_reconciliation_challenge

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=PARTNER_ALT,
        )
        assert nonce
        assert len(fake_redis._data) == 1
        stored = next(iter(fake_redis._data.values()))
        assert PARTNER_ALT in stored


class TestTokenLoginIdentityBinding:
    def test_no_binding_returns_403_with_reconcile_nonce_in_oauth_path_partner(self, db, fake_redis):
        from routers.salla_oauth import salla_token_login

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test", "app_id": APP_ID})
            request.headers = {}
            request.client = None

            with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                mock_client_cls.return_value = _mock_token_login_client(
                    _merchant_only_introspect(PARTNER_ALT),
                )
                with pytest.raises(HTTPException) as exc_info:
                    await salla_token_login(request, db)
                return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        detail = exc.detail
        assert isinstance(detail, dict)
        assert detail["detail"] == "merchant_identity_not_canonical"
        oauth_path = detail["oauth_start_path"]
        assert oauth_path.startswith("/api/salla/oauth/start?embedded_reconcile=1")
        assert "reconcile_nonce=" in oauth_path

    def test_no_binding_returns_403_with_reconcile_nonce_generic(self, db, fake_redis):
        from routers.salla_oauth import salla_token_login

        _seed_store(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_STORE,
            alt_merchant_id=GENERIC_ALT,
            owner_email=GENERIC_EMAIL,
            store_name=GENERIC_STORE_NAME,
        )

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test", "app_id": APP_ID})
            request.headers = {}
            request.client = None

            with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                mock_client_cls.return_value = _mock_token_login_client(
                    _merchant_only_introspect(GENERIC_ALT),
                )
                with pytest.raises(HTTPException) as exc_info:
                    await salla_token_login(request, db)
                return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert "reconcile_nonce=" in exc.detail["oauth_start_path"]

    def test_valid_binding_issues_jwt_with_canonical_store_partner(self, db, fake_redis):
        from routers.salla_oauth import salla_token_login

        integration = _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )
        _seed_merchant(db, tenant_id=PARTNER_TENANT, email=PARTNER_EMAIL)
        _seed_active_binding(db, integration=integration, merchant_account_id=PARTNER_ALT)

        captured: dict = {}

        def _capture_token(**kwargs):
            captured.update(kwargs)
            return "jwt-token"

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test", "app_id": APP_ID})
            request.headers = {}
            request.client = None

            with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                mock_client_cls.return_value = _mock_token_login_client(
                    _merchant_only_introspect(PARTNER_ALT),
                )
                with patch("routers.salla_oauth.create_token", side_effect=_capture_token):
                    with patch("routers.salla_oauth.audit"):
                        with patch("core.salla_onboarding_email.queue_salla_onboarding_email"):
                            return await salla_token_login(request, db)

        result = asyncio.run(_run())
        assert result["tenant_id"] == PARTNER_TENANT
        assert result["store_id"] == PARTNER_STORE
        assert captured["tenant_id"] == PARTNER_TENANT
        assert captured.get("extra_claims", {}).get("store_id") == PARTNER_STORE

    def test_valid_binding_issues_jwt_with_canonical_store_generic(self, db, fake_redis):
        from routers.salla_oauth import salla_token_login

        integration = _seed_store(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_STORE,
            alt_merchant_id=GENERIC_ALT,
            owner_email=GENERIC_EMAIL,
            store_name=GENERIC_STORE_NAME,
        )
        _seed_merchant(db, tenant_id=GENERIC_TENANT, email=GENERIC_EMAIL)
        _seed_active_binding(db, integration=integration, merchant_account_id=GENERIC_ALT)

        captured: dict = {}

        def _capture_token(**kwargs):
            captured.update(kwargs)
            return "jwt-token"

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test", "app_id": APP_ID})
            request.headers = {}
            request.client = None

            with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                mock_client_cls.return_value = _mock_token_login_client(
                    _merchant_only_introspect(GENERIC_ALT),
                )
                with patch("routers.salla_oauth.create_token", side_effect=_capture_token):
                    with patch("routers.salla_oauth.audit"):
                        with patch("core.salla_onboarding_email.queue_salla_onboarding_email"):
                            return await salla_token_login(request, db)

        result = asyncio.run(_run())
        assert result["store_id"] == GENERIC_STORE
        assert captured.get("extra_claims", {}).get("store_id") == GENERIC_STORE


class TestBindingUpsertIdempotentAndRebind:
    def test_upsert_idempotent_refreshes_same_binding(self, db):
        from services.salla_embedded_identity_binding import (
            STATUS_ACTIVE,
            upsert_binding_from_oauth_reconcile,
        )

        integration = _seed_store(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_STORE,
            alt_merchant_id=GENERIC_ALT,
            owner_email=GENERIC_EMAIL,
        )
        challenge = _make_challenge(merchant_account_id=GENERIC_ALT)

        first = upsert_binding_from_oauth_reconcile(
            db,
            challenge=challenge,
            canonical_store_id=GENERIC_STORE,
            integration_id=integration.id,
            tenant_id=GENERIC_TENANT,
        )
        second = upsert_binding_from_oauth_reconcile(
            db,
            challenge=challenge,
            canonical_store_id=GENERIC_STORE,
            integration_id=integration.id,
            tenant_id=GENERIC_TENANT,
        )
        db.commit()

        assert first.id == second.id
        assert second.status == STATUS_ACTIVE
        active_count = (
            db.query(SallaEmbeddedIdentityBinding)
            .filter_by(
                provider="salla",
                app_id=APP_ID,
                merchant_account_id=GENERIC_ALT,
                status=STATUS_ACTIVE,
            )
            .count()
        )
        assert active_count == 1

    def test_rebind_revokes_previous_active_binding(self, db):
        if db.get_bind().dialect.name == "sqlite":
            pytest.skip("rebind upsert requires PostgreSQL partial unique active index")

        from services.salla_embedded_identity_binding import (
            STATUS_ACTIVE,
            STATUS_REVOKED,
            upsert_binding_from_oauth_reconcile,
        )

        first_integration = _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )
        second_store = "66223344"
        second_integration = Integration(
            tenant_id=PARTNER_TENANT,
            provider="salla",
            external_store_id=second_store,
            config={"store_id": second_store},
            enabled=True,
        )
        db.add(second_integration)
        db.commit()
        db.refresh(second_integration)

        challenge = _make_challenge(merchant_account_id=PARTNER_ALT)
        original = upsert_binding_from_oauth_reconcile(
            db,
            challenge=challenge,
            canonical_store_id=PARTNER_STORE,
            integration_id=first_integration.id,
            tenant_id=PARTNER_TENANT,
        )
        rebound = upsert_binding_from_oauth_reconcile(
            db,
            challenge=challenge,
            canonical_store_id=second_store,
            integration_id=second_integration.id,
            tenant_id=PARTNER_TENANT,
        )
        db.commit()
        db.refresh(original)

        assert original.status == STATUS_REVOKED
        assert original.revoked_reason == "oauth_rebound"
        assert rebound.status == STATUS_ACTIVE
        assert rebound.canonical_store_id == second_store


class TestOAuthStartBindsStateToChallenge:
    def test_embedded_reconcile_start_binds_state_to_challenge(self, fake_redis):
        from routers.salla_oauth import salla_api_oauth_start
        from services.salla_reconciliation_challenge import (
            create_reconciliation_challenge,
            resolve_reconciliation_challenge_for_oauth_state,
        )

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=PARTNER_ALT,
        )

        async def _run():
            request = MagicMock()
            with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_ID", "test-client-id"):
                with patch("routers.salla_oauth.SALLA_OAUTH_REDIRECT_URI", "https://api.test/callback"):
                    return await salla_api_oauth_start(
                        request,
                        embedded_reconcile=True,
                        reconcile_nonce=nonce,
                    )

        response = asyncio.run(_run())
        assert response.status_code == 302
        parsed = urllib.parse.urlparse(response.headers["location"])
        qs = urllib.parse.parse_qs(parsed.query)
        oauth_state = qs.get("state", [""])[0]
        assert oauth_state.startswith("embedded_")
        assert oauth_state.endswith(STATE_SUFFIX)

        challenge = resolve_reconciliation_challenge_for_oauth_state(oauth_state)
        assert challenge is not None
        assert challenge.merchant_account_id == PARTNER_ALT


class TestOAuthCallbackCreatesBinding:
    def test_callback_creates_binding_when_challenge_associated_partner(self, db, fake_redis):
        from services.salla_reconciliation_challenge import (
            bind_oauth_state_to_reconciliation_challenge,
            create_reconciliation_challenge,
        )

        integration = _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )
        _seed_merchant(db, tenant_id=PARTNER_TENANT, email=PARTNER_EMAIL)

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=PARTNER_ALT,
        )
        oauth_state = f"embedded_partner_binding{STATE_SUFFIX}"
        bind_oauth_state_to_reconciliation_challenge(oauth_state, nonce=nonce)

        response = asyncio.run(
            _run_api_oauth_callback(
                db,
                state=oauth_state,
                store_id=PARTNER_STORE,
                merchant_id=PARTNER_ALT,
            )
        )
        assert response.status_code == 302

        binding = (
            db.query(SallaEmbeddedIdentityBinding)
            .filter_by(
                provider="salla",
                app_id=APP_ID,
                merchant_account_id=PARTNER_ALT,
                status="active",
            )
            .one()
        )
        assert binding.canonical_store_id == PARTNER_STORE
        assert binding.integration_id == integration.id
        assert binding.tenant_id == PARTNER_TENANT

    def test_callback_creates_binding_generic_commerce_store(self, db, fake_redis):
        from services.salla_reconciliation_challenge import (
            bind_oauth_state_to_reconciliation_challenge,
            create_reconciliation_challenge,
        )

        integration = _seed_store(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_STORE,
            alt_merchant_id=GENERIC_ALT,
            owner_email=GENERIC_EMAIL,
            store_name=GENERIC_STORE_NAME,
        )
        _seed_merchant(db, tenant_id=GENERIC_TENANT, email=GENERIC_EMAIL)

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=GENERIC_ALT,
        )
        oauth_state = f"embedded_generic_binding{STATE_SUFFIX}"
        bind_oauth_state_to_reconciliation_challenge(oauth_state, nonce=nonce)

        response = asyncio.run(
            _run_api_oauth_callback(
                db,
                state=oauth_state,
                store_id=GENERIC_STORE,
                merchant_id=GENERIC_ALT,
                store_name=GENERIC_STORE_NAME,
            )
        )
        assert response.status_code == 302

        binding = (
            db.query(SallaEmbeddedIdentityBinding)
            .filter_by(merchant_account_id=GENERIC_ALT, status="active")
            .one()
        )
        assert binding.canonical_store_id == GENERIC_STORE
        assert binding.integration_id == integration.id



class TestEmbeddedReconcileOAuthMerchantCorrelation:
    def test_matching_oauth_merchant_creates_binding(self, db, fake_redis):
        from services.salla_reconciliation_challenge import (
            bind_oauth_state_to_reconciliation_challenge,
            create_reconciliation_challenge,
        )

        integration = _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )
        _seed_merchant(db, tenant_id=PARTNER_TENANT, email=PARTNER_EMAIL)

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=PARTNER_ALT,
        )
        oauth_state = f"embedded_corr_match{STATE_SUFFIX}"
        bind_oauth_state_to_reconciliation_challenge(oauth_state, nonce=nonce)

        response = asyncio.run(
            _run_api_oauth_callback(
                db,
                state=oauth_state,
                store_id=PARTNER_STORE,
                merchant_id=PARTNER_ALT,
            )
        )
        assert response.status_code == 302
        binding = (
            db.query(SallaEmbeddedIdentityBinding)
            .filter_by(merchant_account_id=PARTNER_ALT, status="active")
            .one()
        )
        assert binding.canonical_store_id == PARTNER_STORE
        assert binding.integration_id == integration.id

    def test_oauth_merchant_mismatch_fails_closed_without_binding_or_handoff(self, db, fake_redis):
        from services.salla_reconciliation_challenge import (
            bind_oauth_state_to_reconciliation_challenge,
            create_reconciliation_challenge,
            resolve_reconciliation_challenge_for_oauth_state,
        )

        _seed_store(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_STORE,
            alt_merchant_id=GENERIC_ALT,
            owner_email=GENERIC_EMAIL,
        )
        _seed_merchant(db, tenant_id=GENERIC_TENANT, email=GENERIC_EMAIL)

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=GENERIC_ALT,
        )
        oauth_state = f"embedded_corr_mismatch{STATE_SUFFIX}"
        bind_oauth_state_to_reconciliation_challenge(oauth_state, nonce=nonce)

        with patch("routers.salla_oauth._issue_opaque_launch_handoff") as handoff_mock:
            response = asyncio.run(
                _run_api_oauth_callback(
                    db,
                    state=oauth_state,
                    store_id=GENERIC_STORE,
                    merchant_id="88770011",
                )
            )

        assert response.status_code == 302
        assert "reason=reconcile_identity_mismatch" in response.headers["location"]
        assert (
            db.query(SallaEmbeddedIdentityBinding)
            .filter_by(merchant_account_id=GENERIC_ALT, status="active")
            .count()
            == 0
        )
        assert resolve_reconciliation_challenge_for_oauth_state(oauth_state) is None
        handoff_mock.assert_not_called()

    def test_missing_oauth_merchant_account_id_fails_closed(self, db, fake_redis):
        from services.salla_reconciliation_challenge import (
            bind_oauth_state_to_reconciliation_challenge,
            create_reconciliation_challenge,
        )

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=PARTNER_ALT,
        )
        oauth_state = f"embedded_corr_missing{STATE_SUFFIX}"
        bind_oauth_state_to_reconciliation_challenge(oauth_state, nonce=nonce)

        with patch("routers.salla_oauth._issue_opaque_launch_handoff") as handoff_mock:
            response = asyncio.run(
                _run_api_oauth_callback(
                    db,
                    state=oauth_state,
                    store_id=PARTNER_STORE,
                    merchant_id="",
                )
            )

        assert response.status_code == 302
        assert "reason=reconcile_identity_mismatch" in response.headers["location"]
        assert db.query(SallaEmbeddedIdentityBinding).count() == 0
        handoff_mock.assert_not_called()

    def test_integration_alias_cannot_satisfy_oauth_merchant_correlation(self, db, fake_redis):
        from services.salla_embedded_identity_binding import (
            verify_embedded_reconcile_oauth_merchant_correlation,
        )
        from services.salla_reconciliation_challenge import (
            bind_oauth_state_to_reconciliation_challenge,
            create_reconciliation_challenge,
        )
        from services.salla_store_identity import SallaStoreIdentity

        integration = _seed_store(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_STORE,
            alt_merchant_id=GENERIC_ALT,
            owner_email=GENERIC_EMAIL,
        )
        assert integration.config.get("merchant_id") == GENERIC_ALT

        challenge = _make_challenge(app_id=APP_ID, merchant_account_id=GENERIC_ALT)
        oauth_identity = SallaStoreIdentity(
            store_id=GENERIC_STORE,
            merchant_account_id="99001122",
            resolved_via="store_info",
        )
        ok, reason = verify_embedded_reconcile_oauth_merchant_correlation(
            challenge=challenge,
            oauth_store_identity=oauth_identity,
        )
        assert ok is False
        assert reason == "reconcile_identity_mismatch"

        nonce = create_reconciliation_challenge(
            provider="salla",
            app_id=APP_ID,
            merchant_account_id=GENERIC_ALT,
        )
        oauth_state = f"embedded_corr_alias{STATE_SUFFIX}"
        bind_oauth_state_to_reconciliation_challenge(oauth_state, nonce=nonce)

        with patch("routers.salla_oauth._issue_opaque_launch_handoff") as handoff_mock:
            response = asyncio.run(
                _run_api_oauth_callback(
                    db,
                    state=oauth_state,
                    store_id=GENERIC_STORE,
                    merchant_id="99001122",
                )
            )

        assert response.status_code == 302
        assert "reason=reconcile_identity_mismatch" in response.headers["location"]
        assert db.query(SallaEmbeddedIdentityBinding).count() == 0
        handoff_mock.assert_not_called()


class TestBindingValidationNegative:
    def test_integration_external_store_mismatch_revokes_binding(self, db):
        from services.salla_embedded_identity_binding import (
            STATUS_REVOKED,
            validate_binding_for_reentry,
        )

        integration = _seed_store(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_STORE,
            alt_merchant_id=GENERIC_ALT,
            owner_email=GENERIC_EMAIL,
        )
        _seed_merchant(db, tenant_id=GENERIC_TENANT, email=GENERIC_EMAIL)
        binding = _seed_active_binding(
            db,
            integration=integration,
            merchant_account_id=GENERIC_ALT,
        )

        integration.external_store_id = "99001122"
        db.commit()

        result = validate_binding_for_reentry(db, binding, app_id=APP_ID)
        db.commit()
        db.refresh(binding)

        assert result.ok is False
        assert result.reason == "external_store_id_mismatch"
        assert binding.status == STATUS_REVOKED


@pytest.mark.skipif(not _postgres_reachable(), reason="PostgreSQL not available")
class TestMigration0100SallaEmbeddedIdentityBindings:
    _TABLE = "salla_embedded_identity_bindings"
    _INDEXES = (
        "ix_seib_lookup",
        "ix_seib_integration_id",
        "ix_seib_tenant_id",
        "ix_seib_canonical_store_id",
        "uq_seib_active_identity",
    )

    @pytest.fixture()
    def ephemeral_migration_engine_at_0099(self) -> Iterator[Engine]:
        from tests.legacy_migration_drift_postgres_fixtures import (
            connect_engine,
            create_ephemeral_database,
            drop_ephemeral_database,
            run_alembic,
        )

        admin_engine = connect_engine()
        db_name, _ = create_ephemeral_database(admin_engine)
        test_engine = create_engine(
            str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
            poolclass=NullPool,
            pool_pre_ping=True,
        )
        try:
            run_alembic(test_engine, "0099")
            yield test_engine
        finally:
            test_engine.dispose()
            drop_ephemeral_database(admin_engine, db_name)
            admin_engine.dispose()

    def test_upgrade_0099_to_0100_creates_binding_schema(
        self,
        ephemeral_migration_engine_at_0099: Engine,
    ) -> None:
        from tests.legacy_migration_drift_postgres_fixtures import assert_revision, run_alembic

        run_alembic(ephemeral_migration_engine_at_0099, "0100")
        assert_revision(ephemeral_migration_engine_at_0099, "0100")

        insp = inspect(ephemeral_migration_engine_at_0099)
        assert self._TABLE in insp.get_table_names()
        present_indexes = {idx.get("name") for idx in insp.get_indexes(self._TABLE)}
        for index_name in self._INDEXES:
            assert index_name in present_indexes

    def test_upgrade_0100_is_idempotent(self, ephemeral_migration_engine_at_0099: Engine) -> None:
        from tests.legacy_migration_drift_postgres_fixtures import assert_revision, run_alembic

        run_alembic(ephemeral_migration_engine_at_0099, "0100")
        run_alembic(ephemeral_migration_engine_at_0099, "0100")
        assert_revision(ephemeral_migration_engine_at_0099, "0100")
