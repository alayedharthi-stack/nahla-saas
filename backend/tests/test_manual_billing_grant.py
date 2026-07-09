"""
Tests for tenant-scoped manual gift billing grants (metadata-only).
"""
from __future__ import annotations

import os
import sys
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
    Integration,
    Tenant,
    TenantSettings,
)

if not getattr(Base.metadata, "_manual_gift_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._manual_gift_jsonb_shim = True  # type: ignore[attr-defined]

TENANT_GIFT = 42
TENANT_OTHER = 7


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


def _seed_starter_plan(db) -> None:
    db.merge(
        BillingPlan(
            id=1,
            tenant_id=None,
            slug="starter",
            name="Starter",
            currency="SAR",
            price_sar=899,
            billing_cycle="monthly",
            features=[],
            limits={},
        )
    )
    db.merge(
        BillingPlan(
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
    )
    db.commit()


def _seed_active_growth_subscription(db, tenant_id: int) -> None:
    _seed_starter_plan(db)
    sub = BillingSubscription(
        id=tenant_id,
        tenant_id=tenant_id,
        plan_id=2,
        status="active",
        started_at=datetime.now(timezone.utc) - timedelta(days=5),
        ends_at=datetime.now(timezone.utc) + timedelta(days=25),
    )
    db.merge(sub)
    db.commit()


def _seed_salla_integration(db, tenant_id: int, *, billing_status: str, plan_slug: str) -> Integration:
    integration = Integration(
        id=tenant_id,
        tenant_id=tenant_id,
        provider="salla",
        external_store_id=f"store-{tenant_id}",
        config={
            "billing_status": billing_status,
            "salla_plan_slug": plan_slug,
        },
    )
    db.merge(integration)
    db.commit()
    return integration


def _enable_gift(
    db,
    tenant_id: int,
    *,
    enabled: bool = True,
    ends_delta_days: int = 30,
    plan_slug: str = "starter",
    revoked_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    ends = (now + timedelta(days=ends_delta_days)).replace(microsecond=0).isoformat()
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).one()
    meta = dict(settings.extra_metadata or {})
    billing = dict(meta.get("billing") or {})
    billing["manual_gift_grant"] = {
        "enabled": enabled,
        "grant_type": "gift",
        "plan_slug": plan_slug,
        "starts_at": now.replace(microsecond=0).isoformat(),
        "ends_at": ends,
        "reason": "test gift",
        "granted_by": "test",
        "granted_at": now.replace(microsecond=0).isoformat(),
        "revoked_at": revoked_at,
        "revoked_by": None,
    }
    meta["billing"] = billing
    settings.extra_metadata = meta
    db.commit()


def _billing_row_counts(db) -> dict:
    return {
        "subscriptions": db.query(func.count(BillingSubscription.id)).scalar(),
        "payments": db.query(func.count(BillingPayment.id)).scalar(),
        "invoices": db.query(func.count(BillingInvoice.id)).scalar(),
    }


class TestManualGiftGrant:
    def test_gift_activates_access_without_trial_or_subscription(self, db):
        from core.billing import has_billing_access
        from core.plan_entitlements import get_entitlements

        _seed_tenant(db, TENANT_GIFT)
        _enable_gift(db, TENANT_GIFT)

        assert has_billing_access(db, TENANT_GIFT) is True

        ent = get_entitlements(db, TENANT_GIFT)
        assert ent.plan_slug == "starter"
        assert ent.billing_status == "gift"
        assert ent.is_active is True
        assert ent.features.autopilot_order_confirmation is True
        assert ent.features.autopilot_full is False

    def test_paid_growth_wins_over_gift_entitlements(self, db):
        from core.plan_entitlements import get_entitlements

        _seed_tenant(db, TENANT_GIFT)
        _seed_active_growth_subscription(db, TENANT_GIFT)
        _enable_gift(db, TENANT_GIFT)

        ent = get_entitlements(db, TENANT_GIFT)
        assert ent.plan_slug == "growth"
        assert ent.billing_status == "active"
        assert ent.features.autopilot_full is True

    def test_expired_gift_inactive(self, db):
        from core.billing import has_billing_access
        from core.manual_billing_grant import is_manual_gift_grant_active

        _seed_tenant(db, TENANT_GIFT)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == TENANT_GIFT).one()
        settings.extra_metadata = {
            "billing": {
                "manual_gift_grant": {
                    "enabled": True,
                    "grant_type": "gift",
                    "plan_slug": "starter",
                    "starts_at": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),
                    "ends_at": past,
                    "reason": "expired",
                    "granted_by": "test",
                    "granted_at": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),
                    "revoked_at": None,
                    "revoked_by": None,
                }
            }
        }
        db.commit()

        assert is_manual_gift_grant_active(db, TENANT_GIFT) is False
        assert has_billing_access(db, TENANT_GIFT) is False

    def test_revoked_gift_inactive(self, db):
        from copy import deepcopy

        from sqlalchemy.orm.attributes import flag_modified

        from core.billing import has_billing_access
        from core.manual_billing_grant import is_manual_gift_grant_active

        _seed_tenant(db, TENANT_GIFT)
        _enable_gift(db, TENANT_GIFT)
        settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == TENANT_GIFT).one()
        meta = deepcopy(settings.extra_metadata)
        meta["billing"]["manual_gift_grant"]["enabled"] = False
        meta["billing"]["manual_gift_grant"]["revoked_at"] = datetime.now(timezone.utc).isoformat()
        meta["billing"]["manual_gift_grant"]["revoked_by"] = "ops@nahla"
        settings.extra_metadata = meta
        flag_modified(settings, "extra_metadata")
        db.commit()

        assert is_manual_gift_grant_active(db, TENANT_GIFT) is False
        assert has_billing_access(db, TENANT_GIFT) is False

    def test_apply_grant_does_not_create_billing_rows(self, db):
        from core.manual_billing_grant import apply_manual_gift_grant

        _seed_tenant(db, TENANT_GIFT)
        before = _billing_row_counts(db)

        apply_manual_gift_grant(
            db,
            TENANT_GIFT,
            days=30,
            plan_slug="starter",
            reason="community gift",
            granted_by="ops@nahla",
        )

        after = _billing_row_counts(db)
        assert after == before == {"subscriptions": 0, "payments": 0, "invoices": 0}

    def test_generic_tenant_not_limited_to_tenant_one(self, db):
        from core.billing import has_billing_access

        _seed_tenant(db, TENANT_OTHER)
        _enable_gift(db, TENANT_OTHER)

        assert has_billing_access(db, TENANT_OTHER) is True

    def test_salla_config_not_modified_by_grant(self, db):
        from core.manual_billing_grant import apply_manual_gift_grant

        _seed_tenant(db, TENANT_GIFT)
        integration = _seed_salla_integration(
            db,
            TENANT_GIFT,
            billing_status="cancelled",
            plan_slug="growth",
        )
        original_config = dict(integration.config)

        apply_manual_gift_grant(
            db,
            TENANT_GIFT,
            days=30,
            plan_slug="starter",
            reason="gift",
            granted_by="ops@nahla",
        )

        refreshed = (
            db.query(Integration)
            .filter(Integration.tenant_id == TENANT_GIFT, Integration.provider == "salla")
            .one()
        )
        assert refreshed.config == original_config

    def test_unknown_plan_slug_rejected(self, db):
        from core.manual_billing_grant import ManualGiftGrantError, apply_manual_gift_grant

        _seed_tenant(db, TENANT_GIFT)

        with pytest.raises(ManualGiftGrantError) as exc:
            apply_manual_gift_grant(
                db,
                TENANT_GIFT,
                days=30,
                plan_slug="growth",
                reason="gift",
                granted_by="ops@nahla",
            )
        assert exc.value.code == "invalid_plan_slug"

    def test_only_one_active_gift_without_force(self, db):
        from core.manual_billing_grant import ManualGiftGrantError, apply_manual_gift_grant

        _seed_tenant(db, TENANT_GIFT)
        apply_manual_gift_grant(
            db,
            TENANT_GIFT,
            days=30,
            plan_slug="starter",
            reason="first gift",
            granted_by="ops@nahla",
        )

        with pytest.raises(ManualGiftGrantError) as exc:
            apply_manual_gift_grant(
                db,
                TENANT_GIFT,
                days=30,
                plan_slug="starter",
                reason="second gift",
                granted_by="ops@nahla",
            )
        assert exc.value.code == "active_gift_exists"

    def test_grant_rejected_when_active_paid_subscription(self, db):
        from core.manual_billing_grant import ManualGiftGrantError, apply_manual_gift_grant

        _seed_tenant(db, TENANT_GIFT)
        _seed_active_growth_subscription(db, TENANT_GIFT)

        with pytest.raises(ManualGiftGrantError) as exc:
            apply_manual_gift_grant(
                db,
                TENANT_GIFT,
                days=30,
                plan_slug="starter",
                reason="gift",
                granted_by="ops@nahla",
            )
        assert exc.value.code == "active_paid_subscription"

    def test_status_payload_includes_gift_fields(self, db):
        from core.trial_lifecycle import build_billing_status_payload

        tenant = _seed_tenant(db, TENANT_GIFT)
        _enable_gift(db, TENANT_GIFT)

        payload = build_billing_status_payload(
            db,
            TENANT_GIFT,
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

        assert payload["manual_gift_grant_active"] is True
        assert payload["manual_gift_grant_plan_slug"] == "starter"
        assert payload["manual_gift_grant_billing_status"] == "gift"

    def test_revoke_via_module(self, db):
        from core.billing import has_billing_access
        from core.manual_billing_grant import apply_manual_gift_grant, revoke_manual_gift_grant

        _seed_tenant(db, TENANT_GIFT)
        apply_manual_gift_grant(
            db,
            TENANT_GIFT,
            days=30,
            plan_slug="starter",
            reason="gift",
            granted_by="ops@nahla",
        )
        assert has_billing_access(db, TENANT_GIFT) is True

        revoke_manual_gift_grant(db, TENANT_GIFT, granted_by="ops@nahla")
        assert has_billing_access(db, TENANT_GIFT) is False
