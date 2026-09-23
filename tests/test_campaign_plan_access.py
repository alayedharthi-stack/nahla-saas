"""Campaign launch uses Starter+ access, including its optional auto coupon.

Exercise the real billing/entitlement readers against persisted subscriptions.
Only request identity and background dispatch are stubbed; no message is sent.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core.plan_entitlements import EntitlementError, get_entitlements, require_feature
from models import Base, Campaign, Integration, Tenant, WhatsAppTemplate
from routers import campaigns


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    try:
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if isinstance(column.type, JSONB):
                    saved.append((column, column.type))
                    column.type = JSON()
        Base.metadata.create_all(engine)
    finally:
        for column, original in saved:
            column.type = original
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def _seed(db, plan="starter", status="active", name="Generic clothing store"):
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.flush()
    db.add(Integration(
        tenant_id=tenant.id, provider="salla",
        config={"billing_status": status, "salla_plan_slug": plan},
    ))
    template = WhatsAppTemplate(
        tenant_id=tenant.id, name="seasonal_offer", language="ar",
        category="MARKETING", status="APPROVED",
        components=[{"type": "BODY", "text": "Offer {{1}}"}],
    )
    db.add(template)
    db.commit()
    return tenant, template


def _body(template, **overrides):
    data = dict(
        name="Seasonal offer", campaign_type="broadcast",
        template_id=str(template.id), template_name=template.name,
        audience_type="all", audience_count=8856,
        schedule_type="immediate", send_strategy="immediate",
        auto_coupon=True, coupon_code="auto", discount_percent=5,
        template_variables={"1": "5%", "_exclude_segments": []},
        idempotency_key="campaign-plan-regression",
    )
    data.update(overrides)
    return campaigns.CreateCampaignIn(**data)


def _create(db, body):
    return asyncio.run(campaigns.create_campaign(
        body=body, request=MagicMock(), db=db,
    ))


@pytest.mark.parametrize("plan,status", [
    ("starter", "active"), ("starter", "trial"),
    ("growth", "active"), ("scale", "active"),
])
@pytest.mark.parametrize("auto_coupon", [False, True])
def test_paid_plans_create_and_dispatch_campaigns(db, plan, status, auto_coupon):
    tenant, template = _seed(db, plan, status)
    body = _body(template, auto_coupon=auto_coupon,
                 coupon_code="auto" if auto_coupon else "",
                 discount_percent=5 if auto_coupon else None)
    with patch.object(campaigns, "resolve_tenant_id", return_value=tenant.id), \
            patch.object(campaigns, "_spawn_dispatch_in_background") as dispatch:
        result = _create(db, body)
        dispatch.assert_called_once_with(result["id"])
        row = db.get(Campaign, result["id"])
        assert row.tenant_id == tenant.id
        assert row.status == "active"
        assert row.audience_type == "all"
        assert row.audience_count == 8856
        assert row.template_variables["1"] == "5%"
        if auto_coupon:
            assert row.template_variables["_auto_coupon"] == "true"
            assert row.template_variables["_discount_percent"] == "5"
            assert row.coupon_code == "auto"
        else:
            assert "_auto_coupon" not in row.template_variables
            assert row.coupon_code is None

        # Retrying Launch must return the same row and never send it twice.
        replay = _create(db, body)
        assert replay["id"] == result["id"]
        dispatch.assert_called_once_with(result["id"])
        assert db.query(Campaign).count() == 1


@pytest.mark.parametrize("schedule_type", ["scheduled", "delayed"])
def test_starter_can_schedule_auto_coupon_without_immediate_dispatch(db, schedule_type):
    tenant, template = _seed(db)
    body = _body(
        template, schedule_type=schedule_type, delay_minutes=30,
        schedule_time=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    )
    with patch.object(campaigns, "resolve_tenant_id", return_value=tenant.id), \
            patch.object(campaigns, "_spawn_dispatch_in_background") as dispatch:
        result = _create(db, body)
    row = db.get(Campaign, result["id"])
    assert row.schedule_type == schedule_type
    assert row.status == ("scheduled" if schedule_type == "scheduled" else "draft")
    assert result["auto_coupon"] is True
    dispatch.assert_not_called()


@pytest.mark.parametrize("status", ["none", "failed", "cancelled"])
def test_inactive_subscription_still_cannot_create_or_dispatch(db, status):
    tenant, template = _seed(db, status=status)
    with patch.object(campaigns, "resolve_tenant_id", return_value=tenant.id), \
            patch.object(campaigns, "_spawn_dispatch_in_background") as dispatch:
        with pytest.raises(HTTPException) as exc:
            _create(db, _body(template))
    assert exc.value.status_code == 402
    assert exc.value.detail["code"] == "billing_access_denied"
    assert db.query(Campaign).count() == 0
    dispatch.assert_not_called()


@pytest.mark.parametrize("invalid_template", ["unapproved", "other_tenant"])
def test_starter_still_requires_own_approved_template(db, invalid_template):
    tenant, template = _seed(db)
    if invalid_template == "unapproved":
        template.status = "PENDING"
        db.commit()
    else:
        _, template = _seed(db, name="Generic perfume store")
    with patch.object(campaigns, "resolve_tenant_id", return_value=tenant.id), \
            patch.object(campaigns, "_spawn_dispatch_in_background") as dispatch:
        with pytest.raises(HTTPException) as exc:
            _create(db, _body(template))
    assert exc.value.status_code == (422 if invalid_template == "unapproved" else 404)
    assert db.query(Campaign).count() == 0
    dispatch.assert_not_called()


def test_campaign_access_does_not_unlock_other_growth_features(db):
    tenant, _ = _seed(db)
    ent = get_entitlements(db, tenant.id)
    assert ent.plan_slug == "starter"
    for feature in ("advanced_coupon_types", "campaign_ai_optimization"):
        with pytest.raises(EntitlementError) as exc:
            require_feature(ent, feature)
        assert exc.value.error_code == "upgrade_required"
        assert exc.value.required_plan == "growth"
