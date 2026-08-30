"""
Tests for admin manual gift grant API helpers and route contracts.
"""
from __future__ import annotations

import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import (  # noqa: E402
    Base,
    BillingInvoice,
    BillingPayment,
    BillingPlan,
    BillingSubscription,
    Tenant,
    TenantSettings,
)

if not getattr(Base.metadata, "_admin_gift_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._admin_gift_jsonb_shim = True  # type: ignore[attr-defined]

TENANT_ID = 55


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


def _seed_tenant(db, tenant_id: int = TENANT_ID) -> None:
    tenant = Tenant(
        id=tenant_id,
        name="متجر تجريبي عام",
        subscription_status="trial_expired",
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.merge(tenant)
    db.merge(TenantSettings(tenant_id=tenant_id, extra_metadata={}))
    db.commit()


def _metadata_snapshot(db, tenant_id: int) -> dict:
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).one()
    return deepcopy(settings.extra_metadata or {})


def _billing_row_counts(db, tenant_id: int) -> dict:
    return {
        "subscriptions": db.query(func.count(BillingSubscription.id)).filter(
            BillingSubscription.tenant_id == tenant_id,
        ).scalar(),
        "payments": db.query(func.count(BillingPayment.id)).filter(
            BillingPayment.tenant_id == tenant_id,
        ).scalar(),
        "invoices": db.query(func.count(BillingInvoice.id)).filter(
            BillingInvoice.tenant_id == tenant_id,
        ).scalar(),
    }


def _route_dep_names(path: str, method: str) -> set[str]:
    from routers.admin import router  # noqa: PLC0415

    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            names: set[str] = set()
            for dep in getattr(route.dependant, "dependencies", []) or []:
                call = getattr(dep, "call", None)
                if call is not None:
                    names.add(getattr(call, "__name__", repr(call)))
            return names
    return set()


class TestAdminManualGiftGrant:
    def test_build_admin_context_snapshot(self, db):
        from core.manual_billing_grant import build_admin_manual_gift_context

        _seed_tenant(db)
        ctx = build_admin_manual_gift_context(db, TENANT_ID)

        assert ctx["tenant_id"] == TENANT_ID
        assert ctx["store_name"] == "متجر تجريبي عام"
        assert ctx["can_grant"] is True
        assert ctx["entitlements"]["plan_slug"] == "none"
        assert ctx["gift"]["active"] is False

    def test_preview_does_not_write_metadata(self, db):
        from core.manual_billing_grant import apply_manual_gift_grant

        _seed_tenant(db)
        before = _metadata_snapshot(db, TENANT_ID)

        apply_manual_gift_grant(
            db,
            TENANT_ID,
            days=30,
            reason="preview test",
            granted_by="admin@test",
            dry_run=True,
        )

        after = _metadata_snapshot(db, TENANT_ID)
        assert after == before

    def test_apply_creates_gift_and_starter_entitlements(self, db):
        from core.manual_billing_grant import apply_manual_gift_grant, is_manual_gift_grant_active
        from core.plan_entitlements import get_entitlements

        _seed_tenant(db)
        before = _billing_row_counts(db, TENANT_ID)

        apply_manual_gift_grant(
            db,
            TENANT_ID,
            days=30,
            reason="admin dashboard gift",
            granted_by="admin@test",
        )

        assert is_manual_gift_grant_active(db, TENANT_ID) is True
        ent = get_entitlements(db, TENANT_ID)
        assert ent.plan_slug == "starter"
        assert ent.billing_status == "gift"
        assert _billing_row_counts(db, TENANT_ID) == before

    def test_apply_rejects_active_paid_subscription(self, db):
        from core.manual_billing_grant import ManualGiftGrantError, apply_manual_gift_grant

        _seed_tenant(db)
        plan = BillingPlan(
            id=1,
            tenant_id=None,
            slug="growth",
            name="Growth",
            currency="SAR",
            price_sar=1699,
            billing_cycle="monthly",
            features=[],
            limits={},
        )
        db.merge(plan)
        db.merge(
            BillingSubscription(
                id=1,
                tenant_id=TENANT_ID,
                plan_id=1,
                status="active",
                started_at=datetime.now(timezone.utc) - timedelta(days=5),
                ends_at=datetime.now(timezone.utc) + timedelta(days=25),
            )
        )
        db.commit()

        with pytest.raises(ManualGiftGrantError) as exc:
            apply_manual_gift_grant(
                db,
                TENANT_ID,
                days=30,
                reason="gift",
                granted_by="admin@test",
            )
        assert exc.value.code == "active_paid_subscription"

    def test_revoke_disables_active_gift(self, db):
        from core.manual_billing_grant import (
            apply_manual_gift_grant,
            is_manual_gift_grant_active,
            revoke_manual_gift_grant,
        )

        _seed_tenant(db)
        apply_manual_gift_grant(
            db,
            TENANT_ID,
            days=30,
            reason="gift",
            granted_by="admin@test",
        )
        assert is_manual_gift_grant_active(db, TENANT_ID) is True

        revoke_manual_gift_grant(db, TENANT_ID, granted_by="admin@test")
        assert is_manual_gift_grant_active(db, TENANT_ID) is False

    def test_apply_permanent_grant_via_admin_helper(self, db):
        from core.manual_billing_grant import apply_manual_gift_grant, is_manual_gift_grant_active
        from core.plan_entitlements import get_entitlements

        _seed_tenant(db)
        result = apply_manual_gift_grant(
            db,
            TENANT_ID,
            permanent=True,
            reason="admin permanent starter",
            granted_by="admin@test",
        )
        assert result["permanent"] is True
        assert result["ends_at"] is None
        assert is_manual_gift_grant_active(db, TENANT_ID) is True
        ent = get_entitlements(db, TENANT_ID)
        assert ent.plan_slug == "starter"
        assert ent.billing_status == "gift"

    def test_get_route_requires_admin(self):
        deps = _route_dep_names("/admin/tenants/{tenant_id}/manual-gift-grant", "GET")
        assert "require_admin" in deps

    def test_post_routes_require_admin_and_block_impersonation(self):
        for path, method in (
            ("/admin/tenants/{tenant_id}/manual-gift-grant/preview", "POST"),
            ("/admin/tenants/{tenant_id}/manual-gift-grant", "POST"),
            ("/admin/tenants/{tenant_id}/manual-gift-grant/revoke", "POST"),
        ):
            deps = _route_dep_names(path, method)
            assert "require_admin" in deps, path
            assert "require_not_support_impersonation" in deps, path

    def test_paid_subscription_blocks_can_grant_in_context(self, db):
        from core.manual_billing_grant import build_admin_manual_gift_context

        _seed_tenant(db)
        plan = BillingPlan(
            id=2,
            tenant_id=None,
            slug="growth",
            name="Growth",
            currency="SAR",
            price_sar=1699,
            billing_cycle="monthly",
            features=[],
            limits={},
        )
        db.merge(plan)
        db.merge(
            BillingSubscription(
                id=2,
                tenant_id=TENANT_ID,
                plan_id=2,
                status="active",
                started_at=datetime.now(timezone.utc) - timedelta(days=5),
                ends_at=datetime.now(timezone.utc) + timedelta(days=25),
            )
        )
        db.commit()

        ctx = build_admin_manual_gift_context(db, TENANT_ID)
        assert ctx["can_grant"] is False
        assert ctx["grant_blocked_reason"] == "active_paid_subscription"
        assert "اشتراك مدفوع نشط" in (ctx["grant_blocked_message_ar"] or "")
