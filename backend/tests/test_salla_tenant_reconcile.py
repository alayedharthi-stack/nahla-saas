"""
Regression: Salla store owner reconciliation and order-sync tenant isolation.

Covers the Tenant 47 ghost-tenant pattern:
  • legacy integration rows matched only via config.store_id
  • OAuth callback must reconcile to the documented owner tenant
  • claim_store must not steal a store from a tenant that already has merchants
  • order upserts remain scoped to tenant_id + external_id
"""
from __future__ import annotations

import os
import sys

import pytest
from unittest.mock import patch
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND = os.path.join(_REPO, "backend")
for p in (_REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import Base, Integration, Order, Tenant, User  # noqa: E402

if not getattr(Base.metadata, "_salla_reconcile_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._salla_reconcile_jsonb_shim = True  # type: ignore[attr-defined]

PARTNER_STORE = "22825873"
PARTNER_TENANT = 1
WRONG_TENANT = 47
PARTNER_EMAIL = "cgcaqkpx5wgewsyv@email.partners"


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
    """Integration owned by tenant but external_store_id column empty (legacy)."""
    db.add(Tenant(id=tenant_id, name=f"Tenant {tenant_id}"))
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


class TestReconcileSallaOAuthTenant:
    def test_legacy_config_store_id_resolves_to_owner(self, db):
        from services.salla_store_identity import reconcile_salla_oauth_tenant_id

        _seed_legacy_integration(db, tenant_id=PARTNER_TENANT, store_id=PARTNER_STORE)

        tenant_id, reason = reconcile_salla_oauth_tenant_id(
            db,
            session_tenant_id=0,
            store_id=PARTNER_STORE,
        )
        assert tenant_id == PARTNER_TENANT
        assert reason == "store_owner_resolved"

    def test_wrong_session_reconciles_to_documented_owner(self, db):
        from services.salla_store_identity import reconcile_salla_oauth_tenant_id

        _seed_legacy_integration(db, tenant_id=PARTNER_TENANT, store_id=PARTNER_STORE)

        tenant_id, reason = reconcile_salla_oauth_tenant_id(
            db,
            session_tenant_id=WRONG_TENANT,
            store_id=PARTNER_STORE,
        )
        assert tenant_id == PARTNER_TENANT
        assert reason == "store_owner_resolved"

    def test_provisioning_does_not_create_new_tenant_when_legacy_integration_exists(self, db):
        from core.merchant_provisioning import get_or_create_merchant_user

        _seed_legacy_integration(db, tenant_id=PARTNER_TENANT, store_id=PARTNER_STORE)
        tenants_before = db.query(Tenant).count()

        result = get_or_create_merchant_user(
            db,
            provider="salla",
            external_store_id=PARTNER_STORE,
            owner_email=f"store-{PARTNER_STORE}@salla-merchant.nahlah.ai",
            store_name="متجر سلة",
            is_email_derived=True,
            issued_via="test_reconcile",
            allow_alias_match=True,
        )

        assert result.tenant_id == PARTNER_TENANT
        assert result.created_tenant is False
        assert db.query(Tenant).count() == tenants_before


class TestClaimStoreOwnershipGuard:
    def test_cannot_claim_store_from_tenant_with_merchant_users(self, db):
        from services.salla_guard import claim_store_for_tenant, SallaStoreOwnershipConflictError

        row = _seed_legacy_integration(db, tenant_id=PARTNER_TENANT, store_id=PARTNER_STORE)
        row.external_store_id = PARTNER_STORE
        db.commit()

        db.add(Tenant(id=WRONG_TENANT, name="متجر سلة-22825873"))
        db.commit()

        with pytest.raises(SallaStoreOwnershipConflictError) as exc_info:
            claim_store_for_tenant(
                db,
                store_id=PARTNER_STORE,
                tenant_id=WRONG_TENANT,
                new_config={"store_id": PARTNER_STORE, "api_key": "tok"},
            )

        assert exc_info.value.owner_tenant_id == PARTNER_TENANT
        assert exc_info.value.requested_tenant_id == WRONG_TENANT

    def test_same_tenant_reclaim_updates_integration(self, db):
        from services.salla_guard import claim_store_for_tenant

        row = _seed_legacy_integration(db, tenant_id=PARTNER_TENANT, store_id=PARTNER_STORE)
        row.external_store_id = PARTNER_STORE
        db.commit()
        integration_id = row.id

        updated = claim_store_for_tenant(
            db,
            store_id=PARTNER_STORE,
            tenant_id=PARTNER_TENANT,
            new_config={"store_id": PARTNER_STORE, "api_key": "fresh-token"},
        )

        assert updated.id == integration_id
        assert updated.tenant_id == PARTNER_TENANT
        assert updated.config.get("api_key") == "fresh-token"


class TestStoreIdentityConflict:
    def test_canonical_external_store_id_wins_over_legacy_alias_on_other_tenant(self, db):
        from services.salla_store_identity import find_salla_integration_by_identity

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

        canonical = db.query(Integration).filter_by(tenant_id=PARTNER_TENANT).one()
        canonical.external_store_id = PARTNER_STORE
        db.commit()

        found, matched_via = find_salla_integration_by_identity(
            db, PARTNER_STORE, allow_alias_match=True,
        )
        assert found is not None
        assert found.tenant_id == PARTNER_TENANT
        assert matched_via == "external_store_id"

    def test_duplicate_config_store_id_raises_conflict(self, db):
        from services.salla_store_identity import (
            find_salla_integration_by_identity,
            SallaStoreIdentityConflictError,
        )

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

        with pytest.raises(SallaStoreIdentityConflictError) as exc_info:
            find_salla_integration_by_identity(
                db, PARTNER_STORE, allow_alias_match=True,
            )

        assert set(exc_info.value.tenant_ids) == {PARTNER_TENANT, WRONG_TENANT}

    def test_duplicate_external_store_id_raises_conflict(self, db):
        from services.salla_store_identity import (
            find_salla_integration_by_identity,
            SallaStoreIdentityConflictError,
        )
        from models import Integration as IntegrationModel  # noqa: PLC0415

        row_a = IntegrationModel(
            id=101,
            tenant_id=PARTNER_TENANT,
            provider="salla",
            external_store_id=PARTNER_STORE,
            config={"store_id": PARTNER_STORE},
            enabled=True,
        )
        row_b = IntegrationModel(
            id=102,
            tenant_id=WRONG_TENANT,
            provider="salla",
            external_store_id=PARTNER_STORE,
            config={"store_id": PARTNER_STORE},
            enabled=True,
        )

        class _CanonicalQuery:
            def filter(self, *_args, **_kwargs):
                return self

            def all(self):
                return [row_a, row_b]

        original_query = db.query

        def _query_wrapper(model):
            if model is IntegrationModel:
                return _CanonicalQuery()
            return original_query(model)

        with patch.object(db, "query", side_effect=_query_wrapper):
            with pytest.raises(SallaStoreIdentityConflictError) as exc_info:
                find_salla_integration_by_identity(
                    db, PARTNER_STORE, allow_alias_match=True,
                )

        assert set(exc_info.value.tenant_ids) == {PARTNER_TENANT, WRONG_TENANT}
        assert exc_info.value.matched_via == "external_store_id"

    def test_reconcile_returns_conflict_without_picking_owner(self, db):
        from services.salla_store_identity import reconcile_salla_oauth_tenant_id

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

        tenant_id, reason = reconcile_salla_oauth_tenant_id(
            db,
            session_tenant_id=WRONG_TENANT,
            store_id=PARTNER_STORE,
        )
        assert tenant_id == WRONG_TENANT
        assert reason == "store_identity_conflict"


class TestOrderUpsertTenantIsolation:
    def test_same_external_id_on_two_tenants_stays_isolated(self, db):
        db.add(Tenant(id=PARTNER_TENANT, name="Tenant 1"))
        db.add(Tenant(id=WRONG_TENANT, name="Ghost"))
        db.add_all([
            Order(
                tenant_id=PARTNER_TENANT,
                external_id="salla-order-9001",
                status="pending",
                source="salla",
            ),
            Order(
                tenant_id=WRONG_TENANT,
                external_id="salla-order-9001",
                status="pending",
                source="salla",
            ),
        ])
        db.commit()

        owner_order = (
            db.query(Order)
            .filter_by(tenant_id=PARTNER_TENANT, external_id="salla-order-9001")
            .one()
        )
        owner_order.status = "paid"
        db.commit()

        ghost_order = (
            db.query(Order)
            .filter_by(tenant_id=WRONG_TENANT, external_id="salla-order-9001")
            .one()
        )
        assert ghost_order.status == "pending"
        assert db.query(Order).filter_by(tenant_id=PARTNER_TENANT).count() == 1
        assert db.query(Order).filter_by(tenant_id=WRONG_TENANT).count() == 1
