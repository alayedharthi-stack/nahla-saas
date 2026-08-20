"""
tests/test_salla_store_identity.py
──────────────────────────────────
Platform-wide regression tests for Salla store identity normalization,
alias integration lookup, token-login routing, webhooks, and OAuth guards.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, event, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (REPO_ROOT, BACKEND_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Integration, Tenant, User  # noqa: E402


if not getattr(Base.metadata, "_salla_identity_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = JSON()

    Base.metadata._salla_identity_jsonb_shim = True  # type: ignore[attr-defined]


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


# ── Partner test-store constants (documented incident) ───────────────────────

PARTNER_CANONICAL_STORE = "22825873"
PARTNER_ALT_MERCHANT    = "1979048767"
PARTNER_TENANT          = 1
PARTNER_EMAIL           = "cgcaqkpx5wgewsyv@email.partners"

# ── Generic commerce store (platform-wide, not honey-specific) ─────────────

GENERIC_CANONICAL_STORE = "55112233"
GENERIC_ALT_MERCHANT    = "88776655"
GENERIC_TENANT          = 5
GENERIC_STORE_NAME      = "متجر تجريبي عام"


def _seed_canonical_integration(
    db,
    *,
    tenant_id: int,
    canonical_store_id: str,
    alt_merchant_id: str = "",
    store_name: str = "test store",
    owner_email: str = "",
) -> Integration:
    tenant = Tenant(id=tenant_id, name=f"Tenant {tenant_id}")
    db.merge(tenant)
    cfg = {
        "store_id": canonical_store_id,
        "store_name": store_name,
        "salla_owner_email": owner_email,
    }
    if alt_merchant_id:
        cfg["salla_merchant_id_alt"] = alt_merchant_id
        cfg["merchant_id"] = alt_merchant_id
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


class TestExtractStoreIdFromIntrospect:
    def test_prefers_store_id_over_merchant_id(self):
        from services.salla_store_identity import extract_store_id_from_introspect

        store_id, merchant_id = extract_store_id_from_introspect({
            "store": {"id": PARTNER_CANONICAL_STORE, "name": "Nahlah Ai honey"},
            "merchant": {"id": PARTNER_ALT_MERCHANT, "email": PARTNER_EMAIL},
        })
        assert store_id == PARTNER_CANONICAL_STORE
        assert merchant_id == PARTNER_ALT_MERCHANT

    def test_generic_store_same_preference(self):
        from services.salla_store_identity import extract_store_id_from_introspect

        store_id, merchant_id = extract_store_id_from_introspect({
            "store": {"id": GENERIC_CANONICAL_STORE, "name": GENERIC_STORE_NAME},
            "merchant": {"id": GENERIC_ALT_MERCHANT},
        })
        assert store_id == GENERIC_CANONICAL_STORE
        assert merchant_id == GENERIC_ALT_MERCHANT


class TestResolveSallaStoreIdentity:
    def test_store_info_fallback_when_only_merchant_id_in_introspect(self):
        from services.salla_store_identity import resolve_salla_store_identity

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {"id": PARTNER_CANONICAL_STORE, "name": "Nahlah Ai honey"},
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        async def _run():
            return await resolve_salla_store_identity(
                {"merchant": {"id": PARTNER_ALT_MERCHANT}},
                "access-token",
                client=mock_client,
            )

        identity = asyncio.run(_run())
        assert identity.store_id == PARTNER_CANONICAL_STORE
        assert identity.merchant_account_id == PARTNER_ALT_MERCHANT
        assert identity.resolved_via == "store_info"


class TestAliasIntegrationLookup:
    def test_alias_routes_to_canonical_tenant(self, db):
        from services.salla_store_identity import (
            find_salla_integration_by_identity,
            resolve_tenant_for_salla_store,
            SallaStoreIdentity,
        )

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
            alt_merchant_id=PARTNER_ALT_MERCHANT,
            store_name="Nahlah Ai honey",
            owner_email=PARTNER_EMAIL,
        )

        row, via = find_salla_integration_by_identity(
            db, PARTNER_ALT_MERCHANT, allow_alias_match=True,
        )
        assert row is not None
        assert row.tenant_id == PARTNER_TENANT
        assert via == "config.salla_merchant_id_alt"

        tenant_id, _, _ = resolve_tenant_for_salla_store(
            db,
            SallaStoreIdentity(store_id=PARTNER_ALT_MERCHANT),
            allow_alias_match=True,
        )
        assert tenant_id == PARTNER_TENANT

    def test_generic_store_alias_lookup(self, db):
        from services.salla_store_identity import find_salla_integration_by_identity

        _seed_canonical_integration(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_CANONICAL_STORE,
            alt_merchant_id=GENERIC_ALT_MERCHANT,
            store_name=GENERIC_STORE_NAME,
        )

        row, via = find_salla_integration_by_identity(
            db, GENERIC_ALT_MERCHANT, allow_alias_match=True,
        )
        assert row.tenant_id == GENERIC_TENANT
        assert via in ("config.salla_merchant_id_alt", "config.merchant_id")


class TestMerchantProvisioningAlias:
    def test_provisioning_via_alt_id_does_not_create_new_tenant(self, db):
        from core.merchant_provisioning import get_or_create_merchant_user

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
            alt_merchant_id=PARTNER_ALT_MERCHANT,
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
        db.commit()

        before = db.query(Tenant).count()
        result = get_or_create_merchant_user(
            db,
            provider="salla",
            external_store_id=PARTNER_ALT_MERCHANT,
            owner_email=PARTNER_EMAIL,
            store_name="Nahlah Ai honey",
        )
        db.commit()
        after = db.query(Tenant).count()

        assert result.tenant_id == PARTNER_TENANT
        assert result.linked_existing is True
        assert after == before

    def test_dual_login_ids_same_tenant_no_duplicate_integrations(self, db):
        from core.merchant_provisioning import get_or_create_merchant_user

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
            alt_merchant_id=PARTNER_ALT_MERCHANT,
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
        db.commit()

        r1 = get_or_create_merchant_user(
            db,
            provider="salla",
            external_store_id=PARTNER_CANONICAL_STORE,
            owner_email=PARTNER_EMAIL,
            store_name="Nahlah Ai honey",
        )
        r2 = get_or_create_merchant_user(
            db,
            provider="salla",
            external_store_id=PARTNER_ALT_MERCHANT,
            owner_email=PARTNER_EMAIL,
            store_name="Nahlah Ai honey",
        )
        db.commit()

        assert r1.tenant_id == PARTNER_TENANT
        assert r2.tenant_id == PARTNER_TENANT
        assert db.query(Integration).filter(Integration.provider == "salla").count() == 1


class TestWebhookTenantResolution:
    def test_merchant_id_and_store_id_variants_same_tenant(self, db):
        from routers.webhooks import _resolve_tenant_from_store

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
            alt_merchant_id=PARTNER_ALT_MERCHANT,
        )

        assert _resolve_tenant_from_store(db, PARTNER_CANONICAL_STORE) == PARTNER_TENANT
        assert _resolve_tenant_from_store(db, PARTNER_ALT_MERCHANT) == PARTNER_TENANT

    def test_normalize_webhook_payload_prefers_store_id(self):
        from services.salla_store_identity import normalize_salla_ids_from_event_data

        identity = normalize_salla_ids_from_event_data({
            "merchant_id": PARTNER_ALT_MERCHANT,
            "store_id": PARTNER_CANONICAL_STORE,
            "store": {"id": PARTNER_CANONICAL_STORE, "name": "Nahlah Ai honey"},
        })
        assert identity.store_id == PARTNER_CANONICAL_STORE


class TestOAuthMismatchGuard:
    def test_blocks_claim_when_session_tenant_differs_from_store_owner(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
        )

        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=43,
            store_id=PARTNER_CANONICAL_STORE,
        )
        assert ok is False
        assert owner_tid == PARTNER_TENANT
        assert reason == "store_owned_by_other_tenant"

    def test_allows_when_session_tenant_matches_owner(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
        )

        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=PARTNER_TENANT,
            store_id=PARTNER_CANONICAL_STORE,
        )
        assert ok is True
        assert owner_tid == PARTNER_TENANT
        assert reason == ""


class TestTokenLoginRoute:
    def test_token_login_resolves_partner_store_to_tenant_one(self, db):
        from routers.salla_oauth import salla_token_login

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
            alt_merchant_id=PARTNER_ALT_MERCHANT,
            store_name="Nahlah Ai honey",
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
        db.commit()

        introspect_body = {
            "success": True,
            "data": {
                "access_token": "admin-access",
                "merchant": {
                    "id": PARTNER_ALT_MERCHANT,
                    "email": PARTNER_EMAIL,
                    "name": "Nahlah Ai honey",
                },
                "store": {
                    "id": PARTNER_CANONICAL_STORE,
                    "name": "Nahlah Ai honey",
                },
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
        assert result["tenant_id"] == PARTNER_TENANT
        assert result["store_id"] == PARTNER_CANONICAL_STORE
        assert db.query(Integration).filter(Integration.provider == "salla").count() == 1

    def test_token_login_alt_then_canonical_no_duplicate(self, db):
        from routers.salla_oauth import salla_token_login

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
            alt_merchant_id=PARTNER_ALT_MERCHANT,
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
        db.commit()

        async def _login_with(store_id: str, merchant_id: str):
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test"})
            request.headers = {}
            request.client = None

            introspect_body = {
                "success": True,
                "data": {
                    "access_token": "admin-access",
                    "merchant": {"id": merchant_id, "email": PARTNER_EMAIL},
                    "store": {"id": store_id, "name": "Nahlah Ai honey"},
                },
            }
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

        r1 = asyncio.run(_login_with(PARTNER_CANONICAL_STORE, PARTNER_ALT_MERCHANT))
        r2 = asyncio.run(_login_with(PARTNER_CANONICAL_STORE, PARTNER_ALT_MERCHANT))
        assert r1["tenant_id"] == PARTNER_TENANT
        assert r2["tenant_id"] == PARTNER_TENANT
        assert db.query(Integration).filter(Integration.provider == "salla").count() == 1

    def test_generic_store_token_login_alias_routes_correctly(self, db):
        from routers.salla_oauth import salla_token_login

        generic_email = "ahmad.salem@example.com"
        _seed_canonical_integration(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_CANONICAL_STORE,
            alt_merchant_id=GENERIC_ALT_MERCHANT,
            store_name=GENERIC_STORE_NAME,
            owner_email=generic_email,
        )
        db.add(User(
            username="ahmad",
            email=generic_email,
            password_hash="x",
            role="merchant",
            tenant_id=GENERIC_TENANT,
            is_active=True,
        ))
        db.commit()

        async def _run():
            request = MagicMock()
            request.json = AsyncMock(return_value={"token": "v4.public.test"})
            request.headers = {}
            request.client = None

            introspect_body = {
                "success": True,
                "data": {
                    "access_token": "tok",
                    "merchant": {"id": GENERIC_ALT_MERCHANT, "email": generic_email},
                    "store": {"id": GENERIC_CANONICAL_STORE, "name": GENERIC_STORE_NAME},
                },
            }
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
        assert result["tenant_id"] == GENERIC_TENANT
        assert result["store_id"] == GENERIC_CANONICAL_STORE


class TestSyncOAuthCallbackMismatch:
    def test_sync_oauth_callback_rejects_wrong_session_tenant(self, db):
        from routers.salla_oauth import salla_api_oauth_callback

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
        )

        async def _run():
            request = MagicMock()
            request.query_params = {}
            request.cookies = {}
            request.headers = {}
            request.client = None

            token_resp = MagicMock()
            token_resp.status_code = 200
            token_resp.json.return_value = {
                "access_token": "acc",
                "refresh_token": "ref",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
            store_resp = MagicMock()
            store_resp.status_code = 200
            store_resp.json.return_value = {
                "data": {"id": PARTNER_CANONICAL_STORE, "name": "Nahlah Ai honey"},
            }

            with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_ID", "cid"):
                with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_SECRET", "sec"):
                    with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                        mock_client = AsyncMock()
                        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                        mock_client.__aexit__ = AsyncMock(return_value=None)
                        mock_client.post = AsyncMock(return_value=token_resp)
                        mock_client.get = AsyncMock(return_value=store_resp)
                        mock_client_cls.return_value = mock_client

                        return await salla_api_oauth_callback(
                            request,
                            code="auth-code",
                            state="t43_abc_apisync",
                            error=None,
                            db=db,
                        )

        response = asyncio.run(_run())
        assert response.status_code == 302
        assert "store_owned_by_other_tenant" in response.headers["location"]


class TestStoreInfoTypedIdentity:
    def test_data_id_is_canonical_store_not_nested_merchant(self):
        from services.salla_store_identity import store_identity_from_store_info

        identity = store_identity_from_store_info({
            "id": GENERIC_CANONICAL_STORE,
            "name": GENERIC_STORE_NAME,
            "merchant": {"id": GENERIC_ALT_MERCHANT},
            "owner": {"id": "1689171978"},
        })
        assert identity.canonical_store_id == GENERIC_CANONICAL_STORE
        assert identity.merchant_account_id == GENERIC_ALT_MERCHANT
        assert identity.authorizing_user_id == "1689171978"
        assert identity.identity_source == "canonical_store_id"
        assert identity.resolved_via == "store_info"

    def test_merchant_only_payload_is_not_canonical(self):
        from services.salla_store_identity import store_identity_from_store_info

        identity = store_identity_from_store_info({
            "merchant": {"id": GENERIC_ALT_MERCHANT, "name": "Account"},
        })
        assert identity.canonical_store_id == ""
        assert identity.merchant_account_id == GENERIC_ALT_MERCHANT
        assert identity.identity_source == "merchant_account_only"


class TestCanonicalOAuthOwnership:
    """AD-SALLA-ID-1: merchant/legacy aliases must not prove store ownership."""

    def test_canonical_cross_tenant_conflict_blocks(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        _seed_canonical_integration(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_CANONICAL_STORE,
        )
        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=9,
            store_id=GENERIC_CANONICAL_STORE,
        )
        assert ok is False
        assert owner_tid == GENERIC_TENANT
        assert reason == "store_owned_by_other_tenant"

    def test_legacy_merchant_alias_is_not_ownership(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        row = _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
            alt_merchant_id=PARTNER_ALT_MERCHANT,
        )
        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=35,
            store_id=PARTNER_ALT_MERCHANT,
        )
        assert ok is True
        assert owner_tid is None
        assert reason == ""
        db.refresh(row)
        assert row.external_store_id == PARTNER_CANONICAL_STORE
        assert (row.config or {}).get("salla_merchant_id_alt") == PARTNER_ALT_MERCHANT

    def test_generic_legacy_alias_is_not_ownership(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        _seed_canonical_integration(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_CANONICAL_STORE,
            alt_merchant_id=GENERIC_ALT_MERCHANT,
            store_name=GENERIC_STORE_NAME,
        )
        from services.salla_store_identity import SallaStoreIdentity

        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=9,
            identity=SallaStoreIdentity(
                store_id=GENERIC_ALT_MERCHANT,
                resolved_via="store_info",
            ),
        )
        assert ok is True
        assert owner_tid is None
        assert reason == ""

    def test_merchant_id_config_is_not_ownership(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        db.merge(Tenant(id=GENERIC_TENANT, name="Generic"))
        db.add(Integration(
            tenant_id=GENERIC_TENANT,
            provider="salla",
            external_store_id=GENERIC_CANONICAL_STORE,
            config={
                "store_id": GENERIC_CANONICAL_STORE,
                "merchant_id": GENERIC_ALT_MERCHANT,
            },
            enabled=True,
        ))
        db.commit()

        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=9,
            store_id=GENERIC_ALT_MERCHANT,
        )
        assert ok is True
        assert owner_tid is None
        assert reason == ""

    def test_merchant_account_only_cannot_persist(self, db):
        from services.salla_store_identity import (
            SallaStoreIdentity,
            assert_oauth_tenant_matches_store_owner,
        )

        _seed_canonical_integration(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_CANONICAL_STORE,
            alt_merchant_id=GENERIC_ALT_MERCHANT,
        )
        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=9,
            identity=SallaStoreIdentity(
                store_id="",
                merchant_account_id=GENERIC_ALT_MERCHANT,
                resolved_via="merchant_account_only",
            ),
        )
        assert ok is False
        assert owner_tid is None
        assert reason == "merchant_identity_not_canonical"

    def test_same_tenant_canonical_reconnect_allowed(self, db):
        from services.salla_store_identity import assert_oauth_tenant_matches_store_owner

        _seed_canonical_integration(
            db,
            tenant_id=GENERIC_TENANT,
            canonical_store_id=GENERIC_CANONICAL_STORE,
        )
        ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
            db,
            session_tenant_id=GENERIC_TENANT,
            store_id=GENERIC_CANONICAL_STORE,
        )
        assert ok is True
        assert owner_tid == GENERIC_TENANT
        assert reason == ""

    def test_multiple_canonical_owners_fail_closed(self, db):
        from services.salla_store_identity import (
            SallaStoreIdentity,
            SallaStoreIdentityConflictError,
            assert_oauth_tenant_matches_store_owner,
            resolve_canonical_store_owner,
        )
        from unittest.mock import patch

        row_a = Integration(
            id=201,
            tenant_id=GENERIC_TENANT,
            provider="salla",
            external_store_id=GENERIC_CANONICAL_STORE,
            config={"store_id": GENERIC_CANONICAL_STORE},
            enabled=True,
        )
        row_b = Integration(
            id=202,
            tenant_id=9,
            provider="salla",
            external_store_id=GENERIC_CANONICAL_STORE,
            config={"store_id": GENERIC_CANONICAL_STORE},
            enabled=True,
        )

        class _CanonicalQuery:
            def filter(self, *_args, **_kwargs):
                return self

            def all(self):
                return [row_a, row_b]

        original_query = db.query

        def _query_wrapper(model):
            from models import Integration as IntegrationModel
            if model is IntegrationModel:
                return _CanonicalQuery()
            return original_query(model)

        with patch.object(db, "query", side_effect=_query_wrapper):
            with pytest.raises(SallaStoreIdentityConflictError):
                resolve_canonical_store_owner(
                    db, SallaStoreIdentity(store_id=GENERIC_CANONICAL_STORE),
                )
            ok, owner_tid, reason = assert_oauth_tenant_matches_store_owner(
                db,
                session_tenant_id=35,
                store_id=GENERIC_CANONICAL_STORE,
            )
        assert ok is False
        assert reason == "store_identity_conflict"
        assert owner_tid is None

    def test_legacy_config_store_id_bridge_when_external_empty(self, db):
        from services.salla_store_identity import resolve_canonical_store_owner

        db.merge(Tenant(id=GENERIC_TENANT, name="Generic"))
        db.add(Integration(
            tenant_id=GENERIC_TENANT,
            provider="salla",
            external_store_id=None,
            config={"store_id": GENERIC_CANONICAL_STORE},
            enabled=True,
        ))
        db.commit()
        owner_tid, row, via = resolve_canonical_store_owner(
            db, GENERIC_CANONICAL_STORE,
        )
        assert owner_tid == GENERIC_TENANT
        assert via == "config.store_id"
        assert row is not None

    def test_claim_store_ignores_merchant_alias(self, db):
        from services.salla_guard import claim_store_for_tenant

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
            alt_merchant_id=PARTNER_ALT_MERCHANT,
        )
        db.merge(Tenant(id=35, name="Session tenant"))
        claimed = claim_store_for_tenant(
            db,
            store_id=PARTNER_ALT_MERCHANT,
            tenant_id=35,
            new_config={"store_id": PARTNER_ALT_MERCHANT, "api_key": "tok"},
        )
        assert claimed.tenant_id == 35
        assert claimed.external_store_id == PARTNER_ALT_MERCHANT
        owner = (
            db.query(Integration)
            .filter_by(tenant_id=PARTNER_TENANT, provider="salla")
            .one()
        )
        assert owner.external_store_id == PARTNER_CANONICAL_STORE
        assert (owner.config or {}).get("salla_merchant_id_alt") == PARTNER_ALT_MERCHANT


class TestSyncOAuthCallbackAliasNotOwnership:
    def test_sync_callback_does_not_block_on_legacy_merchant_alias(self, db):
        from routers.salla_oauth import salla_api_oauth_callback

        _seed_canonical_integration(
            db,
            tenant_id=PARTNER_TENANT,
            canonical_store_id=PARTNER_CANONICAL_STORE,
            alt_merchant_id=PARTNER_ALT_MERCHANT,
        )
        db.merge(Tenant(id=35, name="Apparel test tenant"))
        db.commit()

        async def _run():
            request = MagicMock()
            request.query_params = {}
            request.cookies = {}
            request.headers = {}
            request.client = None

            token_resp = MagicMock()
            token_resp.status_code = 200
            token_resp.json.return_value = {
                "access_token": "acc",
                "refresh_token": "ref",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
            store_resp = MagicMock()
            store_resp.status_code = 200
            store_resp.json.return_value = {
                "data": {"id": PARTNER_ALT_MERCHANT, "name": GENERIC_STORE_NAME},
            }

            with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_ID", "cid"):
                with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_SECRET", "sec"):
                    with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                        mock_client = AsyncMock()
                        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                        mock_client.__aexit__ = AsyncMock(return_value=None)
                        mock_client.post = AsyncMock(return_value=token_resp)
                        mock_client.get = AsyncMock(return_value=store_resp)
                        mock_client_cls.return_value = mock_client

                        return await salla_api_oauth_callback(
                            request,
                            code="auth-code",
                            state="t35_abc_apisync",
                            error=None,
                            db=db,
                        )

        response = asyncio.run(_run())
        assert response.status_code == 302
        location = response.headers["location"]
        assert "store_owned_by_other_tenant" not in location
        assert "salla_oauth=success" in location
        owner = (
            db.query(Integration)
            .filter_by(tenant_id=PARTNER_TENANT, provider="salla")
            .one()
        )
        assert owner.external_store_id == PARTNER_CANONICAL_STORE
        assert (owner.config or {}).get("salla_merchant_id_alt") == PARTNER_ALT_MERCHANT

    def test_sync_callback_merchant_only_store_info_fails_closed(self, db):
        from routers.salla_oauth import salla_api_oauth_callback

        db.merge(Tenant(id=35, name="Apparel test tenant"))
        db.commit()

        async def _run():
            request = MagicMock()
            request.query_params = {}
            request.cookies = {}
            request.headers = {}
            request.client = None

            token_resp = MagicMock()
            token_resp.status_code = 200
            token_resp.json.return_value = {
                "access_token": "acc",
                "refresh_token": "ref",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
            store_resp = MagicMock()
            store_resp.status_code = 200
            store_resp.json.return_value = {
                "data": {"merchant": {"id": GENERIC_ALT_MERCHANT}},
            }

            with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_ID", "cid"):
                with patch("routers.salla_oauth.SALLA_OAUTH_CLIENT_SECRET", "sec"):
                    with patch("routers.salla_oauth.httpx.AsyncClient") as mock_client_cls:
                        mock_client = AsyncMock()
                        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                        mock_client.__aexit__ = AsyncMock(return_value=None)
                        mock_client.post = AsyncMock(return_value=token_resp)
                        mock_client.get = AsyncMock(return_value=store_resp)
                        mock_client_cls.return_value = mock_client

                        return await salla_api_oauth_callback(
                            request,
                            code="auth-code",
                            state="t35_abc_apisync",
                            error=None,
                            db=db,
                        )

        response = asyncio.run(_run())
        assert response.status_code == 302
        assert "merchant_identity_not_canonical" in response.headers["location"]
