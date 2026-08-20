"""
Regression tests for Salla cross-tenant safety guard.

Ensures no embedded/session/launch path issues or accepts a JWT for tenant B
when the resolved Salla store_id belongs to tenant A.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import QueryParams

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND = os.path.join(_REPO, "backend")
for p in (_REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import Base, Integration, Tenant, User  # noqa: E402

if not getattr(Base.metadata, "_salla_tenant_guard_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._salla_tenant_guard_jsonb_shim = True  # type: ignore[attr-defined]

PARTNER_STORE = "22825873"
PARTNER_ALT = "1979048767"
PARTNER_TENANT = 1
WRONG_TENANT = 47
PARTNER_EMAIL = "cgcaqkpx5wgewsyv@email.partners"
DERIVED_EMAIL = "store-22825873@salla-merchant.nahlah.ai"

GENERIC_STORE = "55112233"
GENERIC_ALT = "88776655"
GENERIC_TENANT = 5
GENERIC_WRONG_TENANT = 9
GENERIC_EMAIL = "ahmad.salem@example.com"
UNKNOWN_STORE = "99001122"


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
) -> Integration:
    db.merge(Tenant(id=tenant_id, name=f"Tenant {tenant_id}"))
    cfg = {"store_id": canonical_store_id}
    if alt_merchant_id:
        cfg["salla_merchant_id_alt"] = alt_merchant_id
        cfg["merchant_id"] = alt_merchant_id
    if owner_email:
        cfg["salla_owner_email"] = owner_email
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


class TestSallaTenantGuardHelper:
    def test_mismatch_fails_closed(self, db):
        from services.salla_store_identity import verify_jwt_tenant_owns_salla_store

        _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)

        result = verify_jwt_tenant_owns_salla_store(
            db,
            jwt_tenant_id=WRONG_TENANT,
            store_id=PARTNER_STORE,
            context="test",
        )
        assert result.ok is False
        assert result.reason == "store_tenant_mismatch"
        assert result.owner_tenant_id == PARTNER_TENANT

    def test_matching_tenant_passes(self, db):
        from services.salla_store_identity import verify_jwt_tenant_owns_salla_store

        row = _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)

        result = verify_jwt_tenant_owns_salla_store(
            db,
            jwt_tenant_id=PARTNER_TENANT,
            store_id=PARTNER_STORE,
            context="test",
        )
        assert result.ok is True
        assert result.integration_id == row.id

    def test_generic_store_cross_tenant_fails(self, db):
        from services.salla_store_identity import verify_jwt_tenant_owns_salla_store

        _seed_store(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_STORE,
            alt_merchant_id=GENERIC_ALT,
            owner_email=GENERIC_EMAIL,
        )

        result = verify_jwt_tenant_owns_salla_store(
            db,
            jwt_tenant_id=GENERIC_WRONG_TENANT,
            store_id=GENERIC_STORE,
            context="test",
        )
        assert result.ok is False
        assert result.owner_tenant_id == GENERIC_TENANT


class TestTokenLoginTenantGuard:
    def test_token_login_never_issues_wrong_tenant_when_store_on_tenant_one(self, db):
        from routers.salla_oauth import salla_token_login

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )
        db.add(User(
            username="partner",
            email=PARTNER_EMAIL,
            password_hash="x",
            role="merchant",
            tenant_id=PARTNER_TENANT,
            is_active=True,
        ))
        db.add(User(
            username="derived",
            email=DERIVED_EMAIL,
            password_hash="x",
            role="merchant",
            tenant_id=WRONG_TENANT,
            is_active=True,
        ))
        db.commit()

        introspect_body = {
            "success": True,
            "data": {
                "access_token": "admin-access",
                "merchant": {
                    "id": PARTNER_ALT,
                    "email": PARTNER_EMAIL,
                    "name": "Nahlah Ai honey",
                },
                "store": {"id": PARTNER_STORE, "name": "Nahlah Ai honey"},
            },
        }

        captured: dict = {}

        def _capture_token(**kwargs):
            captured.update(kwargs)
            return "jwt"

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test", "app_id": "app"})
            request.headers = {}
            request.client = None

            mock_introspect_resp = MagicMock()
            mock_introspect_resp.status_code = 200
            mock_introspect_resp.json.return_value = introspect_body

            with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(return_value=mock_introspect_resp)
                mock_client.get = AsyncMock()
                mock_client_cls.return_value = mock_client

                with patch("routers.salla_oauth.create_token", side_effect=_capture_token):
                    with patch("routers.salla_oauth.audit"):
                        with patch(
                            "core.salla_onboarding_email.queue_salla_onboarding_email",
                        ):
                            return await salla_token_login(request, db)

        result = asyncio.run(_run())
        assert result["tenant_id"] == PARTNER_TENANT
        assert result["store_id"] == PARTNER_STORE
        assert captured["tenant_id"] == PARTNER_TENANT
        assert captured.get("extra_claims", {}).get("store_id") == PARTNER_STORE

    def test_token_login_new_store_does_not_issue_tenant_one(self, db):
        from routers.salla_oauth import salla_token_login

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )

        introspect_body = {
            "success": True,
            "data": {
                "access_token": "admin-access",
                "merchant": {
                    "id": "88770011",
                    "email": "new.test.store@example.com",
                    "name": "متجر تجريبي عام",
                },
                "store": {"id": UNKNOWN_STORE, "name": "متجر تجريبي عام"},
            },
        }

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test", "app_id": "app"})
            request.headers = {}
            request.client = None

            mock_introspect_resp = MagicMock()
            mock_introspect_resp.status_code = 200
            mock_introspect_resp.json.return_value = introspect_body

            with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(return_value=mock_introspect_resp)
                mock_client.get = AsyncMock()
                mock_client_cls.return_value = mock_client

                with patch("routers.salla_oauth.create_token", return_value="jwt"):
                    with patch("routers.salla_oauth.audit"):
                        with patch(
                            "core.salla_onboarding_email.queue_salla_onboarding_email",
                        ):
                            return await salla_token_login(request, db)

        result = asyncio.run(_run())
        assert result["tenant_id"] != PARTNER_TENANT
        assert result["store_id"] == UNKNOWN_STORE


class TestSessionTenantGuard:
    def test_session_jwt_wrong_tenant_returns_403(self, db):
        from routers.salla_oauth import salla_check_session

        _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)

        async def _run():
            request = MagicMock()
            request.query_params = QueryParams(f"store_id={PARTNER_STORE}")
            request.state = MagicMock()
            request.state.jwt_payload = {
                "tenant_id": WRONG_TENANT,
                "user_id": 16,
                "sub": DERIVED_EMAIL,
                "role": "merchant",
            }
            with pytest.raises(HTTPException) as exc_info:
                await salla_check_session(request, db)
            return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert exc.detail == "store_tenant_mismatch"

    def test_session_unregistered_store_returns_403(self, db):
        from routers.salla_oauth import salla_check_session

        _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)

        async def _run():
            request = MagicMock()
            request.query_params = QueryParams(f"store_id={UNKNOWN_STORE}")
            request.state = MagicMock()
            request.state.jwt_payload = {
                "tenant_id": PARTNER_TENANT,
                "user_id": 15,
                "sub": PARTNER_EMAIL,
                "role": "merchant",
            }
            with pytest.raises(HTTPException) as exc_info:
                await salla_check_session(request, db)
            return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert exc.detail == "store_not_registered"

    def test_session_matching_tenant_passes(self, db):
        from routers.salla_oauth import salla_check_session

        row = _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)
        db.merge(Tenant(id=PARTNER_TENANT, name="Partner"))
        db.commit()

        async def _run():
            request = MagicMock()
            request.query_params = QueryParams(f"store_id={PARTNER_STORE}")
            request.state = MagicMock()
            request.state.jwt_payload = {
                "tenant_id": PARTNER_TENANT,
                "user_id": 15,
                "sub": PARTNER_EMAIL,
                "role": "merchant",
            }

            with patch("routers.salla_oauth.create_token", return_value="fresh-jwt"):
                return await salla_check_session(request, db)

        result = asyncio.run(_run())
        assert result["connected"] is True
        assert result["tenant_id"] == PARTNER_TENANT
        assert result["store_id"] == PARTNER_STORE
        assert result["integration"]["id"] == row.id

    def test_session_missing_store_id_returns_403(self, db):
        from routers.salla_oauth import salla_check_session

        async def _run():
            request = MagicMock()
            request.query_params = QueryParams("")
            request.state = MagicMock()
            request.state.jwt_payload = {
                "tenant_id": PARTNER_TENANT,
                "user_id": 15,
                "sub": PARTNER_EMAIL,
                "role": "merchant",
            }
            with pytest.raises(HTTPException) as exc_info:
                await salla_check_session(request, db)
            return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert exc.detail == "store_id_required"


class TestOAuthWrongSessionTenant:
    def test_wrong_session_tenant_cannot_claim_partner_store(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)

        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=WRONG_TENANT,
            store_id=PARTNER_STORE,
        )
        assert ok is False
        assert owner_tid == PARTNER_TENANT
        assert reason == "store_owned_by_other_tenant"

    def test_generic_wrong_session_tenant_blocked(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        _seed_store(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_STORE,
            alt_merchant_id=GENERIC_ALT,
            owner_email=GENERIC_EMAIL,
        )

        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=GENERIC_WRONG_TENANT,
            store_id=GENERIC_STORE,
        )
        assert ok is False
        assert owner_tid == GENERIC_TENANT
        assert reason == "store_owned_by_other_tenant"

    def test_legacy_alias_does_not_prove_ownership(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
        )
        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=WRONG_TENANT,
            store_id=PARTNER_ALT,
        )
        assert ok is True
        assert owner_tid is None
        assert reason == ""

    def test_merchant_id_only_config_does_not_prove_ownership(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        db.merge(Tenant(id=GENERIC_TENANT, name="Generic"))
        db.add(Integration(
            tenant_id=GENERIC_TENANT,
            provider="salla",
            external_store_id=GENERIC_STORE,
            config={"store_id": GENERIC_STORE, "merchant_id": GENERIC_ALT},
            enabled=True,
        ))
        db.commit()
        ok, _, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=GENERIC_WRONG_TENANT,
            store_id=GENERIC_ALT,
        )
        assert ok is True
        assert reason == ""


class TestLaunchTenantGuard:
    def test_launch_dashboard_missing_store_id_returns_403(self, db):
        from routers.salla_oauth import launch_dashboard

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "fake-jwt"})
            request.headers = {}
            request.query_params = {}

            with patch("jose.jwt.decode", return_value={
                "tenant_id": PARTNER_TENANT,
                "sub": PARTNER_EMAIL,
                "role": "merchant",
                "user_id": 15,
            }):
                with pytest.raises(HTTPException) as exc_info:
                    await launch_dashboard(request, db)
                return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert exc.detail == "store_id_required"

    def test_launch_dashboard_mismatch_returns_403(self, db):
        from routers.salla_oauth import launch_dashboard

        _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "token": "fake-jwt",
                "store_id": PARTNER_STORE,
            })
            request.headers = {}
            request.query_params = {}

            with patch("jose.jwt.decode", return_value={
                "tenant_id": WRONG_TENANT,
                "sub": DERIVED_EMAIL,
                "role": "merchant",
                "user_id": 16,
            }):
                with pytest.raises(HTTPException) as exc_info:
                    await launch_dashboard(request, db)
                return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert exc.detail == "store_tenant_mismatch"

    def test_launch_dashboard_matching_tenant_passes(self, db):
        from routers.salla_oauth import launch_dashboard

        _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "token": "fake-jwt",
                "store_id": PARTNER_STORE,
            })
            request.headers = {}
            request.query_params = {"next": "/overview"}

            with patch("jose.jwt.decode", return_value={
                "tenant_id": PARTNER_TENANT,
                "sub": PARTNER_EMAIL,
                "role": "merchant",
                "user_id": 15,
                "store_id": PARTNER_STORE,
            }):
                with patch("jose.jwt.encode", return_value="launch-jwt"):
                    return await launch_dashboard(request, db)

        result = asyncio.run(_run())
        assert "launch_url" in result
        assert "launch-jwt" in result["launch_url"]

    def test_resolve_launch_mismatch_returns_403(self, db):
        from routers.salla_oauth import resolve_launch

        _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "launch-jwt"})

            with patch("jose.jwt.decode", return_value={
                "launch_token": True,
                "tenant_id": WRONG_TENANT,
                "sub": DERIVED_EMAIL,
                "role": "merchant",
                "store_id": PARTNER_STORE,
            }):
                with pytest.raises(HTTPException) as exc_info:
                    await resolve_launch(request, db)
                return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert exc.detail == "store_tenant_mismatch"


class TestMerchantOnlyAliasRouting:
    """P0: merchant_id without store.id must not open tenants via salla_merchant_id_alt."""

    def test_guard_rejects_alias_only_partner_alt(self, db):
        from services.salla_store_identity import verify_jwt_tenant_owns_salla_store

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )

        result = verify_jwt_tenant_owns_salla_store(
            db,
            jwt_tenant_id=PARTNER_TENANT,
            store_id=PARTNER_ALT,
            context="test",
        )
        assert result.ok is False
        assert result.reason == "merchant_identity_not_canonical"
        assert result.owner_tenant_id == PARTNER_TENANT

    def test_guard_passes_canonical_external_store_id(self, db):
        from services.salla_store_identity import verify_jwt_tenant_owns_salla_store

        _seed_store(db, tenant_id=PARTNER_TENANT, canonical_store_id=PARTNER_STORE)

        result = verify_jwt_tenant_owns_salla_store(
            db,
            jwt_tenant_id=PARTNER_TENANT,
            store_id=PARTNER_STORE,
            context="test",
        )
        assert result.ok is True

    def test_find_integration_alias_disabled_by_default(self, db):
        from services.salla_store_identity import find_salla_integration_by_identity

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
        )

        row, via = find_salla_integration_by_identity(db, PARTNER_ALT)
        assert row is None
        assert via == ""

        row2, via2 = find_salla_integration_by_identity(
            db, PARTNER_ALT, allow_alias_match=True,
        )
        assert row2 is not None
        assert via2 == "config.salla_merchant_id_alt"

    def test_token_login_merchant_only_alias_blocked(self, db):
        from routers.salla_oauth import salla_token_login

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )

        introspect_body = {
            "success": True,
            "data": {
                "merchant_id": PARTNER_ALT,
                "user_id": "embedded-user",
                "exp": 9999999999,
            },
        }

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test", "app_id": "app"})
            request.headers = {}
            request.client = None

            mock_introspect_resp = MagicMock()
            mock_introspect_resp.status_code = 200
            mock_introspect_resp.json.return_value = introspect_body

            with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(return_value=mock_introspect_resp)
                mock_client.get = AsyncMock()
                mock_client_cls.return_value = mock_client

                with pytest.raises(HTTPException) as exc_info:
                    await salla_token_login(request, db)
                return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert isinstance(exc.detail, dict)
        assert exc.detail["detail"] == "merchant_identity_not_canonical"
        assert exc.detail["code"] == "salla_store_link_required"
        assert exc.detail["next_action"] == "oauth_sync"
        assert exc.detail["oauth_start_path"] == "/api/salla/oauth/start?embedded_reconcile=1"
        assert exc.detail["has_canonical_store_id"] is False
        assert exc.detail["identity_source"] == "merchant_account_only"

    def test_token_login_merchant_id_matching_external_store_is_not_canonical(self, db):
        from routers.salla_oauth import salla_token_login

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
            owner_email=PARTNER_EMAIL,
        )
        db.add(User(
            username="partner",
            email=DERIVED_EMAIL,
            password_hash="x",
            role="merchant",
            tenant_id=PARTNER_TENANT,
            is_active=True,
        ))
        db.commit()

        introspect_body = {
            "success": True,
            "data": {
                "merchant_id": PARTNER_STORE,
                "user_id": "embedded-user",
                "exp": 9999999999,
            },
        }

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test", "app_id": "app"})
            request.headers = {}
            request.client = None

            mock_introspect_resp = MagicMock()
            mock_introspect_resp.status_code = 200
            mock_introspect_resp.json.return_value = introspect_body

            with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(return_value=mock_introspect_resp)
                mock_client.get = AsyncMock()
                mock_client_cls.return_value = mock_client

                with pytest.raises(HTTPException) as exc_info:
                    await salla_token_login(request, db)
                return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert isinstance(exc.detail, dict)
        assert exc.detail["detail"] == "merchant_identity_not_canonical"
        assert exc.detail["code"] == "salla_store_link_required"
        assert exc.detail["has_canonical_store_id"] is False

    def test_token_login_unknown_merchant_only_fails_closed(self, db):
        from routers.salla_oauth import salla_token_login

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
        )
        tenants_before = db.query(Tenant).count()

        introspect_body = {
            "success": True,
            "data": {
                "merchant_id": UNKNOWN_STORE,
                "user_id": "embedded-user",
                "exp": 9999999999,
            },
        }

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test", "app_id": "app"})
            request.headers = {}
            request.client = None

            mock_introspect_resp = MagicMock()
            mock_introspect_resp.status_code = 200
            mock_introspect_resp.json.return_value = introspect_body

            with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(return_value=mock_introspect_resp)
                mock_client.get = AsyncMock()
                mock_client_cls.return_value = mock_client

                with pytest.raises(HTTPException) as exc_info:
                    await salla_token_login(request, db)
                return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert isinstance(exc.detail, dict)
        assert exc.detail["detail"] == "merchant_identity_not_canonical"
        assert exc.detail["code"] == "salla_store_link_required"
        assert db.query(Tenant).count() == tenants_before

    def test_session_rejects_partner_alt_for_tenant_one_jwt(self, db):
        from routers.salla_oauth import salla_check_session

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
        )

        async def _run():
            request = MagicMock()
            request.query_params = QueryParams(f"store_id={PARTNER_ALT}")
            request.state = MagicMock()
            request.state.jwt_payload = {
                "tenant_id": PARTNER_TENANT,
                "user_id": 15,
                "sub": PARTNER_EMAIL,
                "role": "merchant",
                "store_id": PARTNER_ALT,
            }
            with pytest.raises(HTTPException) as exc_info:
                await salla_check_session(request, db)
            return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert exc.detail == "merchant_identity_not_canonical"

    def test_launch_dashboard_rejects_partner_alt(self, db):
        from routers.salla_oauth import launch_dashboard

        _seed_store(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_STORE,
            alt_merchant_id=PARTNER_ALT,
        )

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "token": "fake-jwt",
                "store_id": PARTNER_ALT,
            })
            request.headers = {}
            request.query_params = {"next": "/overview"}

            with patch("jose.jwt.decode", return_value={
                "tenant_id": PARTNER_TENANT,
                "sub": PARTNER_EMAIL,
                "role": "merchant",
                "user_id": 15,
                "store_id": PARTNER_ALT,
            }):
                with pytest.raises(HTTPException) as exc_info:
                    await launch_dashboard(request, db)
                return exc_info.value

        exc = asyncio.run(_run())
        assert exc.status_code == 403
        assert exc.detail == "merchant_identity_not_canonical"


class TestSallaStoreLinkOnboarding:
    def test_build_merchant_identity_not_canonical_detail(self):
        from services.salla_store_identity import (
            SALLA_EMBEDDED_OAUTH_START_PATH,
            SALLA_OAUTH_SYNC_NEXT_ACTION,
            SALLA_STORE_LINK_REQUIRED_CODE,
            build_merchant_identity_not_canonical_detail,
        )

        payload = build_merchant_identity_not_canonical_detail(
            merchant_account_id="1979048767",
        )
        assert payload["detail"] == "merchant_identity_not_canonical"
        assert payload["code"] == SALLA_STORE_LINK_REQUIRED_CODE
        assert payload["next_action"] == SALLA_OAUTH_SYNC_NEXT_ACTION
        assert payload["oauth_start_path"] == SALLA_EMBEDDED_OAUTH_START_PATH
        assert payload["merchant_account_id"] == "1979048767"
        assert payload["has_canonical_store_id"] is False

    def test_embedded_oauth_start_without_jwt_redirects(self):
        from routers.salla_oauth import salla_api_oauth_start

        request = MagicMock()

        with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_ID", "test-client-id"):
            with patch("routers.salla_oauth.SALLA_OAUTH_REDIRECT_URI", "https://api.example/oauth/callback"):
                response = asyncio.run(
                    salla_api_oauth_start(request, token=None, embedded_reconcile=True),
                )

        assert response.status_code == 302
        assert "accounts.salla.sa/oauth2/auth" in response.headers["location"]

    def test_embedded_oauth_start_still_requires_jwt_without_flag(self):
        from routers.salla_oauth import salla_api_oauth_start

        request = MagicMock()

        with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_ID", "test-client-id"):
            with patch("routers.salla_oauth.SALLA_OAUTH_REDIRECT_URI", "https://api.example/oauth/callback"):
                with pytest.raises(HTTPException) as exc_info:
                    asyncio.run(salla_api_oauth_start(request, token=None, embedded_reconcile=False))
        assert exc_info.value.status_code == 401
