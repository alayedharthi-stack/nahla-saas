"""
tests/test_campaign_delivery_tracking.py
────────────────────────────────────────
Coverage for Priority 3 — per-recipient delivery tracking on
campaigns:

  * The status webhook (`_handle_message_status`) updates the right
    `CampaignSendLog` row by `provider_message_id`, populating
    `delivered_at` / `read_at` / `failed_at` exactly once each
    (idempotency).
  * `read` implies `delivered` — backfills `delivered_at` even when
    Meta coalesces and only sends the read event.
  * `failed_at` is set for post-accept failures; the error_code /
    error_message are written from the webhook's `errors[]` block.
  * `delivery_summary` in `/campaigns/{id}/debug` correctly counts
    accepted_by_provider / delivered / read / failed_after_accept /
    unknown_delivery / missing_provider_message_id.
  * `sample_sent` carries `delivery_stage` per row and flags rows
    missing a `provider_message_id` as `has_provider_message_id=false`.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_db():
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from models import Base

    engine = create_engine("sqlite:///:memory:")
    _saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_campaign_with_send_log(
    db,
    *,
    tenant_id=33,
    campaign_id=77,
    wamid="wamid.ABC",
    phone="+966500000111",
    status="sent",
    provider_message_id=None,
):
    from models import (
        Campaign, CampaignSendLog, Customer, Tenant, WhatsAppTemplate,
    )
    t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        t = Tenant(id=tenant_id, name=f"t-{tenant_id}"); db.add(t); db.commit()
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id, name=f"tpl{campaign_id}", language="ar",
        category="MARKETING", status="APPROVED",
        components=[{"type": "BODY", "text": "hi"}],
    )
    db.add(tpl); db.commit()
    cust = Customer(
        tenant_id=tenant_id, name="X", phone=phone, normalized_phone=phone,
    )
    db.add(cust); db.commit()
    c = Campaign(
        id=campaign_id, tenant_id=tenant_id, name="C",
        campaign_type="custom", status="completed",
        audience_type="all", template_id=tpl.id,
    )
    db.add(c); db.commit()
    log = CampaignSendLog(
        tenant_id=tenant_id, campaign_id=campaign_id, customer_id=cust.id,
        customer_phone_e164=phone, template_name=tpl.name, template_language="ar",
        status=status,
        provider_message_id=provider_message_id if provider_message_id is not None else wamid,
        sent_at=datetime(2026, 5, 11, 10, 0, 0),
    )
    db.add(log); db.commit()
    return c, log


# ──────────────────────────────────────────────────────────────────────


def _patch_get_db(wh, db):
    """Replace the handler's `get_db` with a noop-close generator
    that yields our test session. The real generator yields and then
    closes — but the handler also calls `db.close()` explicitly,
    which detaches every ORM instance the test was holding. We use
    a fresh session per call so the test session stays alive."""
    from sqlalchemy.orm import sessionmaker
    engine = db.get_bind()
    Session = sessionmaker(bind=engine)

    def _gen():
        s = Session()
        try:
            yield s
        finally:
            s.close()
    wh.get_db = _gen  # type: ignore


def _run_status(wh, status_dict):
    asyncio.run(wh._handle_message_status(status_dict))


class TestStatusWebhookAttribution:
    def test_delivered_sets_delivered_at_on_send_log(self):
        from routers import whatsapp_webhook as wh
        from models import CampaignSendLog

        db, _ = _make_db()
        _, log = _seed_campaign_with_send_log(db, wamid="wamid.AAA")
        log_id = log.id  # capture before the handler closes its session
        orig = wh.get_db
        _patch_get_db(wh, db)
        try:
            _run_status(wh, {"id": "wamid.AAA", "status": "delivered"})
        finally:
            wh.get_db = orig

        # Re-fetch via the test session so our ORM instance is fresh.
        db.expire_all()
        fresh = db.query(CampaignSendLog).filter(CampaignSendLog.id == log_id).first()
        assert fresh.delivered_at is not None
        assert fresh.read_at is None
        assert fresh.failed_at is None

    def test_read_backfills_delivered_when_meta_coalesces(self):
        """Meta sometimes skips the `delivered` event and jumps
        straight to `read` (especially for archived phones). The
        handler must still mark the row as delivered — reading
        implies delivery."""
        from routers import whatsapp_webhook as wh
        from models import CampaignSendLog

        db, _ = _make_db()
        _, log = _seed_campaign_with_send_log(db, wamid="wamid.READ")
        log_id = log.id
        orig = wh.get_db
        _patch_get_db(wh, db)
        try:
            _run_status(wh, {"id": "wamid.READ", "status": "read"})
        finally:
            wh.get_db = orig

        db.expire_all()
        fresh = db.query(CampaignSendLog).filter(CampaignSendLog.id == log_id).first()
        assert fresh.read_at is not None
        assert fresh.delivered_at is not None

    def test_failed_sets_failed_at_and_extracts_error(self):
        from routers import whatsapp_webhook as wh
        from models import CampaignSendLog

        db, _ = _make_db()
        _, log = _seed_campaign_with_send_log(db, wamid="wamid.FAIL")
        log_id = log.id
        orig = wh.get_db
        _patch_get_db(wh, db)
        try:
            _run_status(wh, {
                "id":     "wamid.FAIL",
                "status": "failed",
                "errors": [{
                    "code":    131026,
                    "title":   "Message Undeliverable",
                    "message": "Phone number is not on WhatsApp.",
                }],
            })
        finally:
            wh.get_db = orig

        db.expire_all()
        fresh = db.query(CampaignSendLog).filter(CampaignSendLog.id == log_id).first()
        assert fresh.failed_at is not None
        assert fresh.error_code == "131026"
        assert "Message Undeliverable" in (fresh.error_message or "")

    def test_idempotent_double_delivered_event(self):
        from routers import whatsapp_webhook as wh
        from models import CampaignSendLog

        db, _ = _make_db()
        _, log = _seed_campaign_with_send_log(db, wamid="wamid.IDEM")
        log_id = log.id
        orig = wh.get_db
        _patch_get_db(wh, db)
        try:
            _run_status(wh, {"id": "wamid.IDEM", "status": "delivered"})
            db.expire_all()
            first_ts = db.query(CampaignSendLog).filter(
                CampaignSendLog.id == log_id,
            ).first().delivered_at
            assert first_ts is not None
            _run_status(wh, {"id": "wamid.IDEM", "status": "delivered"})
        finally:
            wh.get_db = orig

        db.expire_all()
        fresh = db.query(CampaignSendLog).filter(CampaignSendLog.id == log_id).first()
        assert fresh.delivered_at == first_ts

    def test_sent_event_is_a_noop(self):
        from routers import whatsapp_webhook as wh
        from models import CampaignSendLog

        db, _ = _make_db()
        _, log = _seed_campaign_with_send_log(db, wamid="wamid.SENT")
        log_id = log.id
        orig = wh.get_db
        _patch_get_db(wh, db)
        try:
            _run_status(wh, {"id": "wamid.SENT", "status": "sent"})
        finally:
            wh.get_db = orig

        db.expire_all()
        fresh = db.query(CampaignSendLog).filter(CampaignSendLog.id == log_id).first()
        assert fresh.delivered_at is None
        assert fresh.read_at is None
        assert fresh.failed_at is None

    def test_unknown_wamid_is_silently_ignored(self):
        """Status events for non-campaign sends (e.g. cart-recovery
        or one-off /conversations/reply messages) must not raise
        even though there's no matching send-log row."""
        from routers import whatsapp_webhook as wh
        db, _ = _make_db()
        orig = wh.get_db
        _patch_get_db(wh, db)
        try:
            _run_status(wh, {
                "id": "wamid.NEVER_HEARD_OF", "status": "delivered",
            })
        finally:
            wh.get_db = orig


# ──────────────────────────────────────────────────────────────────────


def _call_debug(db, tenant_id, campaign_id):
    from routers import campaigns as camp_router
    original = camp_router.resolve_tenant_id
    camp_router.resolve_tenant_id = lambda request: tenant_id  # type: ignore
    req = MagicMock()
    try:
        return asyncio.run(camp_router.debug_campaign(
            campaign_id=campaign_id, request=req, db=db,
        ))
    finally:
        camp_router.resolve_tenant_id = original


class TestDeliverySummary:
    def test_all_accepted_no_downstream_webhooks(self):
        """4 rows in `sent` with wamids, none with delivered_at set
        → accepted_by_provider=4, unknown_delivery=4, the rest 0.
        Mirrors the exact scenario from the user's screenshot."""
        db, _ = _make_db()
        for i in range(4):
            _seed_campaign_with_send_log(
                db, tenant_id=40, campaign_id=40,
                wamid=f"wamid.A{i}", phone=f"+96650000022{i}",
            ) if i == 0 else _seed_extra_sent(db, campaign_id=40, wamid=f"wamid.A{i}", phone=f"+96650000022{i}")
        result = _call_debug(db, 40, 40)
        ds = result["delivery_summary"]
        assert ds["accepted_by_provider"]        == 4
        assert ds["delivered"]                   == 0
        assert ds["read"]                        == 0
        assert ds["failed_after_accept"]         == 0
        assert ds["unknown_delivery"]            == 4
        assert ds["missing_provider_message_id"] == 0

    def test_mixed_delivery_states(self):
        """1 read, 1 delivered, 1 failed_after_accept, 1 accepted-only
        — all classifications resolve cleanly."""
        from models import CampaignSendLog
        db, _ = _make_db()
        _seed_campaign_with_send_log(
            db, tenant_id=41, campaign_id=41,
            wamid="wamid.R", phone="+966500000301",
        )
        _seed_extra_sent(db, campaign_id=41, wamid="wamid.D", phone="+966500000302")
        _seed_extra_sent(db, campaign_id=41, wamid="wamid.F", phone="+966500000303")
        _seed_extra_sent(db, campaign_id=41, wamid="wamid.A", phone="+966500000304")
        # Manually stamp the delivery timestamps to match the scenario.
        rows = {r.provider_message_id: r for r in db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == 41).all()}
        rows["wamid.R"].read_at      = datetime(2026, 5, 11, 11, 0)
        rows["wamid.R"].delivered_at = datetime(2026, 5, 11, 10, 59)
        rows["wamid.D"].delivered_at = datetime(2026, 5, 11, 11, 0)
        rows["wamid.F"].failed_at    = datetime(2026, 5, 11, 11, 5)
        db.commit()

        result = _call_debug(db, 41, 41)
        ds = result["delivery_summary"]
        assert ds["accepted_by_provider"]        == 4
        assert ds["delivered"]                   == 2   # R + D
        assert ds["read"]                        == 1
        assert ds["failed_after_accept"]         == 1
        # unknown = accepted - max(delivered, read, failed) = 4 - 2 = 2
        # (The "wamid.A" row hasn't received any webhook yet, plus the
        #  failed_at row didn't get a delivered event by spec.)
        assert ds["unknown_delivery"] >= 1

    def test_corrupt_sent_row_without_wamid_flagged(self):
        """Status='sent' WITHOUT a provider_message_id is corrupt —
        the dispatcher should never write this, but if it ever
        happens we surface it in `missing_provider_message_id`
        instead of silently counting it as 'accepted by Meta'."""
        db, _ = _make_db()
        # First seed creates the campaign; pass provider_message_id="" to flag corruption.
        _seed_campaign_with_send_log(
            db, tenant_id=42, campaign_id=42,
            wamid="wamid.OK", phone="+966500000401",
        )
        _seed_extra_sent(db, campaign_id=42, wamid="", phone="+966500000402")
        result = _call_debug(db, 42, 42)
        ds = result["delivery_summary"]
        # One row has a wamid, one doesn't.
        assert ds["accepted_by_provider"]        == 1
        assert ds["missing_provider_message_id"] == 1

    def test_sample_sent_carries_delivery_stage(self):
        from models import CampaignSendLog
        db, _ = _make_db()
        _seed_campaign_with_send_log(
            db, tenant_id=43, campaign_id=43,
            wamid="wamid.X", phone="+966500000501",
        )
        rows = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == 43).all()
        rows[0].delivered_at = datetime(2026, 5, 11, 12, 0)
        db.commit()

        result = _call_debug(db, 43, 43)
        sample = result["sample_sent"]
        assert len(sample) == 1
        assert sample[0]["delivery_stage"] == "delivered"
        assert sample[0]["has_provider_message_id"] is True


def _seed_extra_sent(db, *, campaign_id, wamid, phone):
    """Add another already-sent log row to an existing campaign."""
    from models import Campaign, CampaignSendLog, Customer
    camp = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    cust = Customer(
        tenant_id=camp.tenant_id, name="X", phone=phone, normalized_phone=phone,
    )
    db.add(cust); db.commit()
    log = CampaignSendLog(
        tenant_id=camp.tenant_id, campaign_id=campaign_id, customer_id=cust.id,
        customer_phone_e164=phone, template_name="tpl",
        template_language="ar", status="sent",
        provider_message_id=wamid or None,
        sent_at=datetime(2026, 5, 11, 10, 0, 0),
    )
    db.add(log); db.commit()
    return log
