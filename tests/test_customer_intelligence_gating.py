"""
tests/test_customer_intelligence_gating.py
──────────────────────────────────────────
Targeted tests for:
  - Deterministic customer status + RFM classification
  - Lead recompute on first contact
  - 24h WhatsApp service-window gating for freeform replies
  - Approved-template campaign gating
  - Placeholder integrity in template edits

All router modules are imported LAZILY (inside each test) to avoid the
`observability` dual-import conflict that occurs during full-suite collection
when `backend/` is imported both as a package from repo root AND as a direct
path from pytest.ini's pythonpath.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _path in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from models import (  # noqa: E402
    Base,
    Campaign,
    CustomerProfile,
    Tenant,
    WaConversationWindow,
    WhatsAppConnection,
    WhatsAppTemplate,
)
from services.customer_intelligence import (  # noqa: E402
    CustomerIntelligenceService,
    CustomerMetrics,
    compute_customer_status,
    compute_rfm_scores,
    compute_rfm_segment,
)
from core.wa_usage import has_open_service_window  # noqa: E402


# ── SQLite compatibility: replace JSONB columns with JSON ────────────────────

@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _make_request(tenant_id: int, path: str):
    from types import SimpleNamespace
    return SimpleNamespace(
        state=SimpleNamespace(tenant_id=tenant_id),
        url=SimpleNamespace(path=path),
    )


def _seed_tenant(db, name: str = "Tenant") -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _seed_connected_whatsapp(db, tenant_id: int, *, phone_number_id: str = "PID_1") -> WhatsAppConnection:
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        status="connected",
        phone_number_id=phone_number_id,
        phone_number="+966500000000",
        sending_enabled=True,
        webhook_verified=True,
        connection_type="embedded",
        provider="meta",
    )
    db.add(conn)
    db.commit()
    return conn


def _seed_template(
    db,
    tenant_id: int,
    *,
    name: str,
    status: str,
    body_text: str = "مرحبا {{1}}",
) -> WhatsAppTemplate:
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name=name,
        language="ar",
        category="MARKETING",
        status=status,
        components=[{"type": "BODY", "text": body_text}],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


# ═════════════════════════════════════════════════════════════════════════════
# § 1  Deterministic customer status classification
# ═════════════════════════════════════════════════════════════════════════════

def test_zero_orders_is_lead():
    """Customer with no orders → lead."""
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    metrics = CustomerMetrics(
        total_orders=0,
        total_spend_sar=0.0,
        average_order_value_sar=0.0,
        max_single_order_sar=0.0,
        first_seen_at=now - timedelta(days=10),
        last_seen_at=now,
        first_order_at=None,
        last_order_at=None,
        days_since_first_order=None,
        days_since_last_order=None,
    )
    assert compute_customer_status(metrics, now) == "lead"
    scores = compute_rfm_scores(metrics, now)
    assert scores.code == "000"
    assert compute_rfm_segment(scores, "lead") == "lead"


def test_first_time_recent_buyer_is_new():
    """Single order within 30 days → new."""
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    metrics = CustomerMetrics(
        total_orders=1,
        total_spend_sar=180.0,
        average_order_value_sar=180.0,
        max_single_order_sar=180.0,
        first_seen_at=now - timedelta(days=5),
        last_seen_at=now - timedelta(days=1),
        first_order_at=now - timedelta(days=5),
        last_order_at=now - timedelta(days=1),
        days_since_first_order=5,
        days_since_last_order=1,
    )
    status = compute_customer_status(metrics, now)
    assert status == "new"


def test_repeat_recent_buyer_is_active():
    """2+ orders, last within 60 days, first > 30 days ago → active."""
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    metrics = CustomerMetrics(
        total_orders=3,
        total_spend_sar=750.0,
        average_order_value_sar=250.0,
        max_single_order_sar=300.0,
        first_seen_at=now - timedelta(days=90),
        last_seen_at=now - timedelta(days=10),
        first_order_at=now - timedelta(days=80),
        last_order_at=now - timedelta(days=10),
        days_since_first_order=80,
        days_since_last_order=10,
    )
    status = compute_customer_status(metrics, now)
    assert status == "active"


def test_high_value_customer_is_vip():
    """≥ 5 orders AND ≥ 2000 SAR → vip."""
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    metrics = CustomerMetrics(
        total_orders=6,
        total_spend_sar=3500.0,
        average_order_value_sar=583.33,
        max_single_order_sar=900.0,
        first_seen_at=now - timedelta(days=140),
        last_seen_at=now - timedelta(days=5),
        first_order_at=now - timedelta(days=120),
        last_order_at=now - timedelta(days=5),
        days_since_first_order=120,
        days_since_last_order=5,
    )
    status = compute_customer_status(metrics, now)
    scores = compute_rfm_scores(metrics, now)
    segment = compute_rfm_segment(scores, status)
    assert status == "vip"
    assert scores.code == "544"
    assert segment == "champions"


def test_previously_active_becomes_at_risk():
    """Last order 61-90 days ago → at_risk."""
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    metrics = CustomerMetrics(
        total_orders=4,
        total_spend_sar=900.0,
        average_order_value_sar=225.0,
        max_single_order_sar=400.0,
        first_seen_at=now - timedelta(days=200),
        last_seen_at=now - timedelta(days=75),
        first_order_at=now - timedelta(days=180),
        last_order_at=now - timedelta(days=75),
        days_since_first_order=180,
        days_since_last_order=75,
    )
    status = compute_customer_status(metrics, now)
    assert status == "at_risk"


def test_long_inactive_is_inactive():
    """Last order > 90 days ago → inactive."""
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    metrics = CustomerMetrics(
        total_orders=2,
        total_spend_sar=250.0,
        average_order_value_sar=125.0,
        max_single_order_sar=150.0,
        first_seen_at=now - timedelta(days=220),
        last_seen_at=now - timedelta(days=120),
        first_order_at=now - timedelta(days=200),
        last_order_at=now - timedelta(days=120),
        days_since_first_order=200,
        days_since_last_order=120,
    )
    status = compute_customer_status(metrics, now)
    scores = compute_rfm_scores(metrics, now)
    segment = compute_rfm_segment(scores, status)
    assert status == "inactive"
    assert scores.code == "121"
    assert segment == "lost_customers"


def test_rfm_scoring_is_deterministic():
    """Same input always produces same RFM code."""
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    metrics = CustomerMetrics(
        total_orders=3,
        total_spend_sar=1200.0,
        average_order_value_sar=400.0,
        max_single_order_sar=500.0,
        first_seen_at=now - timedelta(days=120),
        last_seen_at=now - timedelta(days=20),
        first_order_at=now - timedelta(days=100),
        last_order_at=now - timedelta(days=20),
        days_since_first_order=100,
        days_since_last_order=20,
    )
    scores_a = compute_rfm_scores(metrics, now)
    scores_b = compute_rfm_scores(metrics, now)
    assert scores_a.code == scores_b.code
    assert scores_a.total == scores_b.total
    assert scores_a.recency == scores_b.recency


# ═════════════════════════════════════════════════════════════════════════════
# § 2  Lead recompute on first contact
# ═════════════════════════════════════════════════════════════════════════════

def test_upsert_lead_customer_creates_recomputed_profile():
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db, "Lead Tenant")
        service = CustomerIntelligenceService(db, tenant.id)

        customer = service.upsert_lead_customer(
            phone="0555000001",
            name="Lead Customer",
            source="whatsapp_inbound",
            commit=True,
        )

        profile = db.query(CustomerProfile).filter(
            CustomerProfile.tenant_id == tenant.id,
            CustomerProfile.customer_id == customer.id,
        ).first()

        assert customer is not None
        assert profile is not None
        assert profile.customer_status == "lead"
        assert profile.rfm_segment == "lead"
        assert profile.total_orders == 0
        assert profile.last_recomputed_reason == "whatsapp_inbound"
    finally:
        db.close()
        engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# § 3  24h WhatsApp service-window gating
# ═════════════════════════════════════════════════════════════════════════════

def test_has_open_service_window_requires_service_category():
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db, "Window Tenant")
        phone = "+966555000001"
        recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)

        db.add(WaConversationWindow(tenant_id=tenant.id, customer_phone=phone, window_start=recent, category="marketing"))
        db.commit()

        # marketing window → NOT a service window
        assert has_open_service_window(db, tenant.id, phone) is False

        db.query(WaConversationWindow).filter(
            WaConversationWindow.tenant_id == tenant.id,
            WaConversationWindow.customer_phone == phone,
        ).update({"category": "service"})
        db.commit()

        # service window within 24h → allowed
        assert has_open_service_window(db, tenant.id, phone) is True
    finally:
        db.close()
        engine.dispose()


def test_reply_blocks_freeform_outside_service_window():
    """POST /conversations/reply must raise 409 when no open service window."""
    import routers.conversations as conv_router  # lazy import

    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db, "Reply Guard Tenant")
        _seed_connected_whatsapp(db, tenant.id, phone_number_id="PID_BLOCK")

        # Marketing window only — freeform NOT allowed
        db.add(WaConversationWindow(
            tenant_id=tenant.id,
            customer_phone="+966555000002",
            window_start=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
            category="marketing",
        ))
        db.commit()

        fake_module = ModuleType("routers.whatsapp_webhook")
        fake_module._send_whatsapp_message = AsyncMock()
        body = conv_router.ReplyIn(customer_phone="0555000002", message="رسالة حرة")
        request = _make_request(tenant.id, "/conversations/reply")

        with patch.dict(sys.modules, {"routers.whatsapp_webhook": fake_module}):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(conv_router.reply_to_conversation(body, request, db))

        assert exc.value.status_code == 409
        assert "24 ساعة" in exc.value.detail
        fake_module._send_whatsapp_message.assert_not_awaited()
    finally:
        db.close()
        engine.dispose()


def test_reply_allows_freeform_inside_service_window():
    """POST /conversations/reply succeeds inside an open service window."""
    import routers.conversations as conv_router  # lazy import

    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db, "Reply Allow Tenant")
        _seed_connected_whatsapp(db, tenant.id, phone_number_id="PID_ALLOW")

        # Service window — freeform allowed
        db.add(WaConversationWindow(
            tenant_id=tenant.id,
            customer_phone="+966555000003",
            window_start=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
            category="service",
        ))
        db.commit()

        fake_module = ModuleType("routers.whatsapp_webhook")
        fake_module._send_whatsapp_message = AsyncMock()
        body = conv_router.ReplyIn(customer_phone="0555000003", message="رد يدوي")
        request = _make_request(tenant.id, "/conversations/reply")

        with patch.dict(sys.modules, {"routers.whatsapp_webhook": fake_module}):
            result = asyncio.run(conv_router.reply_to_conversation(body, request, db))

        assert result == {"sent": True}
        fake_module._send_whatsapp_message.assert_awaited_once()
        # Customer profile was created automatically
        assert db.query(CustomerProfile).filter(CustomerProfile.tenant_id == tenant.id).count() == 1
    finally:
        db.close()
        engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# § 4  Approved-template campaign gating
# ═════════════════════════════════════════════════════════════════════════════

def test_create_campaign_rejects_unapproved_template():
    """Campaigns must not be created with DRAFT/PENDING templates."""
    import routers.campaigns as camp_router  # lazy import

    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db, "Campaign Guard Tenant")
        draft_tpl = _seed_template(db, tenant.id, name="draft_offer", status="DRAFT")
        request = _make_request(tenant.id, "/campaigns")
        body = camp_router.CreateCampaignIn(
            name="Draft Campaign",
            campaign_type="promotion",
            template_id=str(draft_tpl.id),
            template_name="frontend name",
            template_body="frontend body",
            audience_type="all",
            audience_count=10,
        )

        with pytest.raises(HTTPException) as exc:
            asyncio.run(camp_router.create_campaign(body, request, db))

        assert exc.value.status_code == 422
        assert "قالب معتمد" in exc.value.detail
    finally:
        db.close()
        engine.dispose()


def test_create_campaign_uses_database_template_metadata():
    """Campaign creation must pull template metadata from DB, not from the frontend payload."""
    import routers.campaigns as camp_router  # lazy import

    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db, "Campaign Tenant")
        approved_tpl = _seed_template(
            db, tenant.id,
            name="approved_offer",
            status="APPROVED",
            body_text="العرض الخاص للعميل {{1}}",
        )
        request = _make_request(tenant.id, "/campaigns")
        body = camp_router.CreateCampaignIn(
            name="Approved Campaign",
            campaign_type="promotion",
            template_id=str(approved_tpl.id),
            template_name="wrong client name",   # must be ignored
            template_body="wrong client body",   # must be ignored
            template_language="en",              # must be ignored
            template_category="UTILITY",         # must be ignored
            audience_type="vip",
            audience_count=12,
        )

        result = asyncio.run(camp_router.create_campaign(body, request, db))
        campaign = db.query(Campaign).filter(Campaign.tenant_id == tenant.id).first()

        assert result["template_name"] == "approved_offer"
        assert result["template_language"] == "ar"
        assert "العرض الخاص" in (result["template_body"] or "")
        assert campaign.template_name == "approved_offer"
        assert campaign.template_language == "ar"
    finally:
        db.close()
        engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# § 5  Placeholder integrity in template edits
# ═════════════════════════════════════════════════════════════════════════════

def test_placeholder_helpers_support_named_and_numeric_placeholders():
    import routers.templates as tpl_router  # lazy import

    old = [{"type": "BODY", "text": "مرحبا {{1}}، الطلب {{order_id}} جاهز"}]
    same = [{"type": "BODY", "text": "أهلًا {{1}}، رقم الطلب {{order_id}} جاهز الآن"}]
    renamed = [{"type": "BODY", "text": "أهلًا {{customer_name}}، الطلب {{order_id}} جاهز"}]

    placeholders = tpl_router._extract_template_placeholders(old)
    assert placeholders == ["{{1}}", "{{order_id}}"]

    # Same placeholders, different surrounding text → allowed
    tpl_router._validate_placeholder_integrity(old_components=old, new_components=same)

    # Renamed placeholder → must be rejected
    with pytest.raises(HTTPException) as exc:
        tpl_router._validate_placeholder_integrity(old_components=old, new_components=renamed)
    assert exc.value.status_code == 422


def test_dashboard_customer_counts_match_classification():
    """
    Verify that status_counts() totals returned by CustomerIntelligenceService
    match the per-customer classification results from compute_customer_status().
    """
    from models import Customer, Order

    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db, "Counts Tenant")
        now = datetime.now(timezone.utc)

        # 1 lead (no orders)
        lead = Customer(tenant_id=tenant.id, name="Lead", phone="+966500000010")
        db.add(lead)

        # 1 new customer (single order 5 days ago)
        new_cust = Customer(tenant_id=tenant.id, name="New", phone="+966500000011")
        db.add(new_cust)
        db.flush()
        db.add(Order(
            tenant_id=tenant.id, external_id="o-new", status="completed",
            total="200", customer_info={"name": "New", "mobile": "+966500000011"},
            line_items=[],
            extra_metadata={"created_at": (now - timedelta(days=5)).isoformat()},
        ))

        # 1 vip customer
        vip = Customer(tenant_id=tenant.id, name="VIP", phone="+966500000012")
        db.add(vip)
        db.flush()
        for i in range(6):
            db.add(Order(
                tenant_id=tenant.id, external_id=f"o-vip-{i}", status="completed",
                total="600",
                customer_info={"name": "VIP", "mobile": "+966500000012"},
                line_items=[],
                extra_metadata={"created_at": (now - timedelta(days=i + 1)).isoformat()},
            ))
        db.commit()

        svc = CustomerIntelligenceService(db, tenant.id)
        svc.rebuild_profiles_for_tenant("test_count_verify", commit=True)

        counts = svc.status_counts()

        assert counts["lead"] == 1
        assert counts["new"] == 1
        assert counts["vip"] == 1
        assert counts.get("active", 0) == 0
        assert counts.get("at_risk", 0) == 0
        assert counts.get("inactive", 0) == 0
    finally:
        db.close()
        engine.dispose()
