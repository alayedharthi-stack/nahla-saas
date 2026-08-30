"""
Tests for compact billing display on admin tenant list summaries.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
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

if not getattr(Base.metadata, "_admin_billing_badge_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._admin_billing_badge_jsonb_shim = True  # type: ignore[attr-defined]

TENANT_GIFT = 52
TENANT_PAID = 53


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


def _seed_tenant(db, tenant_id: int, *, is_active: bool = True) -> Tenant:
    tenant = Tenant(
        id=tenant_id,
        name=f"Tenant {tenant_id}",
        subscription_status="trial_expired",
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        is_active=is_active,
    )
    db.merge(tenant)
    db.merge(TenantSettings(tenant_id=tenant_id, extra_metadata={}))
    db.commit()
    return tenant


def _enable_gift(db, tenant_id: int) -> None:
    now = datetime.now(timezone.utc)
    ends = (now + timedelta(days=30)).replace(microsecond=0).isoformat()
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).one()
    meta = dict(settings.extra_metadata or {})
    meta["billing"] = {
        "manual_gift_grant": {
            "enabled": True,
            "grant_type": "gift",
            "plan_slug": "starter",
            "starts_at": now.replace(microsecond=0).isoformat(),
            "ends_at": ends,
            "reason": "test",
            "granted_by": "ops",
            "granted_at": now.replace(microsecond=0).isoformat(),
            "revoked_at": None,
            "revoked_by": None,
        }
    }
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.commit()


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


class TestAdminTenantListBillingBadge:
    def test_gift_only_tenant_summary_shows_gift_badge(self, db):
        from routers.admin import _tenant_summary_payload

        tenant = _seed_tenant(db, TENANT_GIFT)
        _enable_gift(db, TENANT_GIFT)
        before = _billing_row_counts(db, TENANT_GIFT)

        summary = _tenant_summary_payload(db, tenant)
        display = summary["billing_display"]

        assert display["billing_access_kind"] == "gift"
        assert display["gift_active"] is True
        assert display["billing_plan_slug"] == "starter"
        assert "هدية" in display["billing_access_label_ar"]
        assert "Starter" in display["billing_access_label_ar"]
        assert "دائمة" not in display["billing_access_label_ar"]
        assert _billing_row_counts(db, TENANT_GIFT) == before

    def test_permanent_gift_summary_shows_no_expiry_badge(self, db):
        from core.manual_billing_grant import apply_manual_gift_grant
        from routers.admin import _tenant_summary_payload

        tenant = _seed_tenant(db, TENANT_GIFT)
        apply_manual_gift_grant(
            db,
            TENANT_GIFT,
            permanent=True,
            reason="permanent starter",
            granted_by="ops@nahla",
        )
        display = _tenant_summary_payload(db, tenant)["billing_display"]
        assert display["billing_access_kind"] == "gift"
        assert display["gift_active"] is True
        assert display["billing_ends_at"] is None
        assert display["gift_ends_at"] is None
        assert display["billing_access_label_ar"] == "هدية دائمة — Starter"

    def test_paid_subscription_wins_over_gift_metadata(self, db):
        from routers.admin import _tenant_summary_payload

        tenant = _seed_tenant(db, TENANT_PAID)
        _enable_gift(db, TENANT_PAID)
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
                tenant_id=TENANT_PAID,
                plan_id=1,
                status="active",
                started_at=datetime.now(timezone.utc) - timedelta(days=5),
                ends_at=datetime.now(timezone.utc) + timedelta(days=25),
            )
        )
        db.commit()

        display = _tenant_summary_payload(db, tenant)["billing_display"]
        assert display["billing_access_kind"] == "paid"
        assert "مدفوع نشط" in display["billing_access_label_ar"]
        assert display["gift_active"] is True

    def test_no_billing_shows_none(self, db):
        from routers.admin import _tenant_summary_payload

        tenant = _seed_tenant(db, 99)
        display = _tenant_summary_payload(db, tenant)["billing_display"]
        assert display["billing_access_kind"] == "none"
        assert display["billing_access_label_ar"] == "لا باقة"

    def test_inactive_store_shows_store_disabled(self, db):
        from routers.admin import _tenant_summary_payload

        tenant = _seed_tenant(db, 88, is_active=False)
        _enable_gift(db, 88)
        display = _tenant_summary_payload(db, tenant)["billing_display"]
        assert display["billing_access_kind"] == "store_disabled"
        assert display["billing_access_label_ar"] == "المتجر معطل"
