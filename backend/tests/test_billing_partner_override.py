"""
Tests for tenant-scoped Salla partner testing billing override (tenant 1 only).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
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
    BillingPlan,
    BillingSubscription,
    Tenant,
    TenantSettings,
)

if not getattr(Base.metadata, "_billing_override_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._billing_override_jsonb_shim = True  # type: ignore[attr-defined]

TENANT_PARTNER = 1
TENANT_OTHER = 2


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


def _seed_tenant(db, tenant_id: int) -> Tenant:
    tenant = Tenant(
        id=tenant_id,
        name=f"Tenant {tenant_id}",
        subscription_status="trial_expired",
        trial_started_at=datetime.now(timezone.utc) - timedelta(days=30),
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.merge(tenant)
    settings = TenantSettings(tenant_id=tenant_id, extra_metadata={})
    db.merge(settings)
    db.commit()
    return tenant


def _seed_expired_subscription(db, tenant_id: int) -> None:
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
    sub = BillingSubscription(
        id=tenant_id,
        tenant_id=tenant_id,
        plan_id=1,
        status="active",
        started_at=datetime.now(timezone.utc) - timedelta(days=60),
        ends_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db.merge(sub)
    db.commit()


def _enable_override(db, tenant_id: int, *, enabled: bool = True) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).one()
    meta = dict(settings.extra_metadata or {})
    billing = dict(meta.get("billing") or {})
    billing["partner_testing_override"] = {
        "enabled": enabled,
        "reason": "salla_partner_testing",
        "plan_slug": "scale",
        "expires_at": expires,
        "granted_at": datetime.now(timezone.utc).isoformat(),
        "granted_by": "test",
    }
    meta["billing"] = billing
    settings.extra_metadata = meta
    db.commit()


class TestPartnerTestingOverride:
    def test_tenant_one_expired_subscription_with_override_allowed(self, db):
        from core.billing import has_billing_access
        from core.plan_entitlements import get_entitlements

        _seed_tenant(db, TENANT_PARTNER)
        _seed_expired_subscription(db, TENANT_PARTNER)
        _enable_override(db, TENANT_PARTNER)

        assert has_billing_access(db, TENANT_PARTNER) is True

        ent = get_entitlements(db, TENANT_PARTNER)
        assert ent.is_active is True
        assert ent.is_blocked is False
        assert ent.plan_slug == "scale"
        assert ent.features.store_brain_advanced is True

    def test_tenant_two_expired_subscription_without_override_blocked(self, db):
        from core.billing import has_billing_access
        from core.plan_entitlements import get_entitlements

        _seed_tenant(db, TENANT_OTHER)
        _seed_expired_subscription(db, TENANT_OTHER)

        assert has_billing_access(db, TENANT_OTHER) is False

        ent = get_entitlements(db, TENANT_OTHER)
        assert ent.plan_slug in ("none", "failed")
        assert ent.is_active is False

    def test_tenant_one_override_disabled_blocked_normally(self, db):
        from core.billing import has_billing_access

        _seed_tenant(db, TENANT_PARTNER)
        _seed_expired_subscription(db, TENANT_PARTNER)
        _enable_override(db, TENANT_PARTNER, enabled=False)

        assert has_billing_access(db, TENANT_PARTNER) is False

    def test_override_on_tenant_two_metadata_ignored(self, db):
        from core.billing import has_billing_access

        _seed_tenant(db, TENANT_OTHER)
        _seed_expired_subscription(db, TENANT_OTHER)
        _enable_override(db, TENANT_OTHER)

        assert has_billing_access(db, TENANT_OTHER) is False

    def test_expired_override_does_not_grant_access(self, db):
        from core.billing import has_billing_access
        from core.billing_override import is_partner_testing_override_active

        _seed_tenant(db, TENANT_PARTNER)
        settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == TENANT_PARTNER).one()
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        settings.extra_metadata = {
            "billing": {
                "partner_testing_override": {
                    "enabled": True,
                    "reason": "salla_partner_testing",
                    "plan_slug": "scale",
                    "expires_at": past,
                }
            }
        }
        db.commit()

        assert is_partner_testing_override_active(db, TENANT_PARTNER) is False
        assert has_billing_access(db, TENANT_PARTNER) is False

    def test_status_payload_includes_test_banner_fields(self, db):
        from core.trial_lifecycle import build_billing_status_payload

        tenant = _seed_tenant(db, TENANT_PARTNER)
        _enable_override(db, TENANT_PARTNER)

        payload = build_billing_status_payload(
            db,
            TENANT_PARTNER,
            tenant,
            active_sub=None,
            conversations_used=0,
            usage_data={
                "conversations_limit": 5000,
                "usage_pct": 0,
                "exceeded": False,
            },
            integration_fee_sar=59,
        )

        assert payload["partner_testing_override_active"] is True
        assert payload["partner_testing_override_headline_ar"] == "وضع اختبار سلة مفعل لهذا المتجر"
        assert payload["ai_auto_replies_allowed"] is True

    def test_conversation_quota_bypassed_under_override(self, db):
        from core.wa_usage import check_limit

        _seed_tenant(db, TENANT_PARTNER)
        _enable_override(db, TENANT_PARTNER)

        result = check_limit(db, TENANT_PARTNER, category="service")
        assert result.allowed is True
        assert result.reason == "partner_testing_override"
