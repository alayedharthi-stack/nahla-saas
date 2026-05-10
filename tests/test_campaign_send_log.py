"""tests/test_campaign_send_log.py
─────────────────────────────────
Idempotency / anti-spam protection for manual marketing campaigns.

What we cover here (pure unit tests, no provider HTTP, no real Meta):

  * Snapshot inserts one row per recipient with ``status='queued'`` and
    is a no-op on re-run (``UNIQUE(tenant_id, campaign_id, phone)``).
  * Snapshot marks unreachable / opted-out recipients with the
    appropriate ``skipped_*`` status — never reaching the provider.
  * The frequency-cap pass flips ``queued`` rows to
    ``skipped_duplicate`` when the same phone has a prior ``sent`` row
    on a *different* campaign within the cap window.
  * The frequency cap is tenant-isolated: two tenants holding the same
    phone do NOT cross-block each other.
  * A sent row OUTSIDE the cap window does not block.
  * The status-counter helper agrees with the underlying rows and
    feeds the campaign report endpoint.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    Base,
    Campaign,
    CampaignSendLog,
    Customer,
    Tenant,
    WhatsAppTemplate,
)
from services.campaign_dispatcher import (  # noqa: E402
    LOG_QUEUED,
    LOG_SENT,
    LOG_SKIPPED_DUPLICATE,
    LOG_SKIPPED_UNREACHABLE,
    LOG_SKIPPED_UNSUBSCRIBED,
    REASON_FREQ_CAP,
    _apply_frequency_cap,
    _count_log_statuses,
    _snapshot_recipients,
)


# ── SQLite in-memory DB (mirrors test_send_governor.py shim) ────────────


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
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


# ── Seed helpers ────────────────────────────────────────────────────────


def _seed_tenant(db, name="T") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_customer(
    db,
    tenant_id: int,
    phone: str,
    *,
    name: str = "Cust",
    extra_metadata: dict | None = None,
) -> Customer:
    c = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=phone,
        name=name,
        extra_metadata=extra_metadata or {},
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_template(db, tenant_id: int, name: str = "tpl_promo") -> WhatsAppTemplate:
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name=name,
        language="ar",
        category="MARKETING",
        status="APPROVED",
        components=[{"type": "BODY", "text": "Hi {{1}}"}],
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


def _seed_campaign(
    db,
    tenant_id: int,
    template: WhatsAppTemplate,
    *,
    name: str = "C",
) -> Campaign:
    c = Campaign(
        tenant_id=tenant_id,
        name=name,
        campaign_type="broadcast",
        template_id=str(template.id),
        template_name=template.name,
        template_language="ar",
        template_category="MARKETING",
        audience_type="all",
        status="scheduled",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ── Snapshot ────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_snapshot_inserts_one_row_per_recipient(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp = _seed_campaign(db, t.id, tpl)

        c1 = _seed_customer(db, t.id, "+966500000001")
        c2 = _seed_customer(db, t.id, "+966500000002")

        result = _snapshot_recipients(db, t.id, camp.id, [c1, c2], tpl)
        db.commit()

        assert result["new"] == 2
        rows = db.query(CampaignSendLog).filter_by(campaign_id=camp.id).all()
        assert len(rows) == 2
        assert {r.status for r in rows} == {LOG_QUEUED}
        assert {r.customer_phone_e164 for r in rows} == {"+966500000001", "+966500000002"}

    def test_snapshot_is_idempotent_on_rerun(self):
        """Calling ``_snapshot_recipients`` twice for the same campaign
        must not produce duplicate rows — this is the core anti-spam
        guarantee against re-runs."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp = _seed_campaign(db, t.id, tpl)

        c1 = _seed_customer(db, t.id, "+966500000001")
        c2 = _seed_customer(db, t.id, "+966500000002")

        _snapshot_recipients(db, t.id, camp.id, [c1, c2], tpl)
        db.commit()

        # Simulate the row already being marked sent — the second
        # snapshot must NOT touch it.
        sent_row = (
            db.query(CampaignSendLog)
              .filter_by(campaign_id=camp.id, customer_phone_e164="+966500000001")
              .first()
        )
        sent_row.status = LOG_SENT
        sent_row.sent_at = datetime.now(timezone.utc)
        db.commit()

        result = _snapshot_recipients(db, t.id, camp.id, [c1, c2], tpl)
        db.commit()

        assert result["new"] == 0  # nothing new on re-run
        rows = db.query(CampaignSendLog).filter_by(campaign_id=camp.id).all()
        assert len(rows) == 2
        # The already-sent row is preserved untouched.
        sent_row_after = (
            db.query(CampaignSendLog)
              .filter_by(campaign_id=camp.id, customer_phone_e164="+966500000001")
              .first()
        )
        assert sent_row_after.status == LOG_SENT

    def test_snapshot_marks_unsubscribed(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp = _seed_campaign(db, t.id, tpl)

        opted_out = _seed_customer(
            db, t.id, "+966500000099",
            extra_metadata={"is_unsubscribed": True},
        )

        _snapshot_recipients(db, t.id, camp.id, [opted_out], tpl)
        db.commit()

        row = db.query(CampaignSendLog).filter_by(campaign_id=camp.id).one()
        assert row.status == LOG_SKIPPED_UNSUBSCRIBED
        assert row.skip_reason == "unsubscribed"

    def test_snapshot_marks_no_phone(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp = _seed_campaign(db, t.id, tpl)

        # Customer with no normalized_phone — the snapshot should
        # record an unreachable row, not raise.
        c = Customer(
            tenant_id=t.id, phone="", normalized_phone="", name="No Phone",
        )
        db.add(c); db.commit(); db.refresh(c)

        _snapshot_recipients(db, t.id, camp.id, [c], tpl)
        db.commit()

        row = db.query(CampaignSendLog).filter_by(campaign_id=camp.id).one()
        assert row.status == LOG_SKIPPED_UNREACHABLE


# ── Frequency cap ───────────────────────────────────────────────────────


class TestFrequencyCap:
    def test_cap_skips_recipient_with_recent_sent_row(self, monkeypatch):
        """Phone X received campaign A 1 day ago. Snapshotting campaign B
        for the same phone should flip its queued row to
        ``skipped_duplicate`` once the cap pass runs."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp_a = _seed_campaign(db, t.id, tpl, name="A")
        camp_b = _seed_campaign(db, t.id, tpl, name="B")

        cust = _seed_customer(db, t.id, "+966500000010")

        # Pre-existing sent row from campaign A, 1 day ago.
        db.add(CampaignSendLog(
            tenant_id=t.id,
            campaign_id=camp_a.id,
            customer_id=cust.id,
            customer_phone_e164=cust.normalized_phone,
            template_name=tpl.name,
            template_language=tpl.language,
            status=LOG_SENT,
            sent_at=datetime.now(timezone.utc) - timedelta(days=1),
            provider_message_id="wamid.A1",
        ))
        db.commit()

        # Snapshot campaign B for the same customer.
        _snapshot_recipients(db, t.id, camp_b.id, [cust], tpl)
        db.commit()

        skipped = _apply_frequency_cap(db, t.id, camp_b.id)
        db.commit()

        assert skipped == 1
        row = db.query(CampaignSendLog).filter_by(campaign_id=camp_b.id).one()
        assert row.status == LOG_SKIPPED_DUPLICATE
        assert row.skip_reason and REASON_FREQ_CAP in row.skip_reason

    def test_cap_ignores_prior_failed_row_same_phone(self, monkeypatch):
        """Failed attempts must NOT count toward frequency protection —
        only provably successful WhatsApp sends do."""
        from services import campaign_dispatcher as disp
        monkeypatch.setattr(disp, "MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS", 14)

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp_a = _seed_campaign(db, t.id, tpl, name="A")
        camp_b = _seed_campaign(db, t.id, tpl, name="B")

        cust = _seed_customer(db, t.id, "+966500000050")

        db.add(CampaignSendLog(
            tenant_id=t.id,
            campaign_id=camp_a.id,
            customer_id=cust.id,
            customer_phone_e164=cust.normalized_phone,
            template_name=tpl.name,
            template_language=tpl.language,
            status="failed",
            sent_at=datetime.now(timezone.utc) - timedelta(days=1),
            error_message="Meta boom",
        ))
        db.commit()

        _snapshot_recipients(db, t.id, camp_b.id, [cust], tpl)
        db.commit()

        skipped = _apply_frequency_cap(db, t.id, camp_b.id)
        db.commit()

        assert skipped == 0
        row_b = db.query(CampaignSendLog).filter_by(campaign_id=camp_b.id).one()
        assert row_b.status == LOG_QUEUED

    def test_cap_ignores_sent_without_delivery_proof(self, monkeypatch):
        """``status=sent`` without ``provider_message_id`` AND without
        ``sent_at`` never counts — nothing proves Meta accepted it."""
        from services import campaign_dispatcher as disp
        monkeypatch.setattr(disp, "MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS", 14)

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp_a = _seed_campaign(db, t.id, tpl, name="A")
        camp_b = _seed_campaign(db, t.id, tpl, name="B")

        cust = _seed_customer(db, t.id, "+966500000060")

        db.add(CampaignSendLog(
            tenant_id=t.id,
            campaign_id=camp_a.id,
            customer_id=cust.id,
            customer_phone_e164=cust.normalized_phone,
            template_name=tpl.name,
            template_language=tpl.language,
            status=LOG_SENT,
            sent_at=None,
            provider_message_id=None,
        ))
        db.commit()

        _snapshot_recipients(db, t.id, camp_b.id, [cust], tpl)
        db.commit()

        skipped = _apply_frequency_cap(db, t.id, camp_b.id)
        db.commit()

        assert skipped == 0
        row_b = db.query(CampaignSendLog).filter_by(campaign_id=camp_b.id).one()
        assert row_b.status == LOG_QUEUED

    def test_bypass_disables_frequency_cap(self, monkeypatch):
        """When ``bypass=True``, recent successful sends must NOT dedupe."""
        from services import campaign_dispatcher as disp
        monkeypatch.setattr(disp, "MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS", 14)

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp_a = _seed_campaign(db, t.id, tpl, name="A")
        camp_b = _seed_campaign(db, t.id, tpl, name="B")

        cust = _seed_customer(db, t.id, "+966500000070")

        db.add(CampaignSendLog(
            tenant_id=t.id,
            campaign_id=camp_a.id,
            customer_id=cust.id,
            customer_phone_e164=cust.normalized_phone,
            template_name=tpl.name,
            template_language=tpl.language,
            status=LOG_SENT,
            sent_at=datetime.now(timezone.utc) - timedelta(days=1),
            provider_message_id="wamid.BYPASS_TEST",
        ))
        db.commit()

        _snapshot_recipients(db, t.id, camp_b.id, [cust], tpl)
        db.commit()

        skipped = _apply_frequency_cap(db, t.id, camp_b.id, bypass=True)
        db.commit()

        assert skipped == 0
        row_b = db.query(CampaignSendLog).filter_by(campaign_id=camp_b.id).one()
        assert row_b.status == LOG_QUEUED

    def test_cap_counts_legacy_wamid_without_sent_at_if_recent(self, monkeypatch):
        """``sent_at`` may be missing on legacy rows; a live ``wamid`` +
        fresh ``updated_at`` still proves Meta acceptance → burns cap."""
        from services import campaign_dispatcher as disp
        monkeypatch.setattr(disp, "MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS", 14)

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp_a = _seed_campaign(db, t.id, tpl, name="A")
        camp_b = _seed_campaign(db, t.id, tpl, name="B")

        cust = _seed_customer(db, t.id, "+966500000080")
        recent = datetime.now(timezone.utc) - timedelta(hours=3)

        db.add(CampaignSendLog(
            tenant_id=t.id,
            campaign_id=camp_a.id,
            customer_id=cust.id,
            customer_phone_e164=cust.normalized_phone,
            template_name=tpl.name,
            template_language=tpl.language,
            status=LOG_SENT,
            sent_at=None,
            provider_message_id="wamid.LEGACY_NO_SENT_AT",
            updated_at=recent,
            created_at=recent,
        ))
        db.commit()

        _snapshot_recipients(db, t.id, camp_b.id, [cust], tpl)
        db.commit()

        skipped = _apply_frequency_cap(db, t.id, camp_b.id)
        db.commit()

        assert skipped == 1

    def test_cap_does_not_skip_when_prior_send_is_outside_window(self, monkeypatch):
        """A 30-day-old sent row should NOT block a new campaign when
        the cap is 14 days."""
        # Cap default is 14d at module import; force it to 14 explicitly
        # in case env overrides it.
        from services import campaign_dispatcher as disp
        monkeypatch.setattr(disp, "MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS", 14)

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp_a = _seed_campaign(db, t.id, tpl, name="A")
        camp_b = _seed_campaign(db, t.id, tpl, name="B")

        cust = _seed_customer(db, t.id, "+966500000020")

        db.add(CampaignSendLog(
            tenant_id=t.id,
            campaign_id=camp_a.id,
            customer_id=cust.id,
            customer_phone_e164=cust.normalized_phone,
            template_name=tpl.name,
            template_language=tpl.language,
            status=LOG_SENT,
            sent_at=datetime.now(timezone.utc) - timedelta(days=30),
            provider_message_id="wamid.A2",
        ))
        db.commit()

        _snapshot_recipients(db, t.id, camp_b.id, [cust], tpl)
        db.commit()
        skipped = _apply_frequency_cap(db, t.id, camp_b.id)
        db.commit()

        assert skipped == 0
        row = db.query(CampaignSendLog).filter_by(campaign_id=camp_b.id).one()
        assert row.status == LOG_QUEUED

    def test_cap_is_tenant_isolated(self):
        """Two tenants, same phone, recent sent row only on tenant A.
        Tenant B's campaign for that phone must NOT see the dedupe."""
        db, _ = _make_db()
        t_a = _seed_tenant(db, "A")
        t_b = _seed_tenant(db, "B")
        tpl_a = _seed_template(db, t_a.id)
        tpl_b = _seed_template(db, t_b.id)
        camp_a = _seed_campaign(db, t_a.id, tpl_a, name="A1")
        camp_b = _seed_campaign(db, t_b.id, tpl_b, name="B1")

        # Same phone shared across stores (legitimate).
        cust_a = _seed_customer(db, t_a.id, "+966500000030")
        cust_b = _seed_customer(db, t_b.id, "+966500000030")

        db.add(CampaignSendLog(
            tenant_id=t_a.id,
            campaign_id=camp_a.id,
            customer_id=cust_a.id,
            customer_phone_e164="+966500000030",
            template_name=tpl_a.name,
            template_language="ar",
            status=LOG_SENT,
            sent_at=datetime.now(timezone.utc) - timedelta(hours=1),
            provider_message_id="wamid.A3",
        ))
        db.commit()

        _snapshot_recipients(db, t_b.id, camp_b.id, [cust_b], tpl_b)
        db.commit()
        skipped = _apply_frequency_cap(db, t_b.id, camp_b.id)
        db.commit()

        assert skipped == 0  # tenant A's send must not block tenant B
        row_b = db.query(CampaignSendLog).filter_by(campaign_id=camp_b.id).one()
        assert row_b.status == LOG_QUEUED

    def test_cap_disabled_when_zero(self, monkeypatch):
        """Setting the cap to 0 disables protection — admin escape
        hatch (should never be 0 in production)."""
        from services import campaign_dispatcher as disp
        monkeypatch.setattr(disp, "MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS", 0)

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp_a = _seed_campaign(db, t.id, tpl, name="A")
        camp_b = _seed_campaign(db, t.id, tpl, name="B")
        cust = _seed_customer(db, t.id, "+966500000040")

        db.add(CampaignSendLog(
            tenant_id=t.id, campaign_id=camp_a.id, customer_id=cust.id,
            customer_phone_e164=cust.normalized_phone,
            template_name=tpl.name, template_language="ar",
            status=LOG_SENT, sent_at=datetime.now(timezone.utc),
            provider_message_id="wamid.A4",
        ))
        db.commit()

        _snapshot_recipients(db, t.id, camp_b.id, [cust], tpl)
        db.commit()
        skipped = _apply_frequency_cap(db, t.id, camp_b.id)

        assert skipped == 0


# ── Counter helper ─────────────────────────────────────────────────────


class TestCountLogStatuses:
    def test_counts_match_rows(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp = _seed_campaign(db, t.id, tpl)

        for i, status in enumerate([
            LOG_QUEUED, LOG_QUEUED, LOG_SENT, LOG_SKIPPED_DUPLICATE,
        ]):
            db.add(CampaignSendLog(
                tenant_id=t.id, campaign_id=camp.id,
                customer_phone_e164=f"+96650000{i:04d}",
                template_name=tpl.name, template_language="ar",
                status=status,
            ))
        db.commit()

        counts = _count_log_statuses(db, camp.id)
        assert counts.get(LOG_QUEUED) == 2
        assert counts.get(LOG_SENT) == 1
        assert counts.get(LOG_SKIPPED_DUPLICATE) == 1


# ── Unique constraint contract ─────────────────────────────────────────


class TestUniqueConstraint:
    def test_double_insert_violates_unique_index(self):
        """The DB-level unique constraint on (tenant, campaign, phone)
        is the second line of defence behind the snapshot dedupe."""
        from sqlalchemy.exc import IntegrityError

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp = _seed_campaign(db, t.id, tpl)

        db.add(CampaignSendLog(
            tenant_id=t.id, campaign_id=camp.id,
            customer_phone_e164="+966500000050",
            template_name=tpl.name, template_language="ar",
            status=LOG_QUEUED,
        ))
        db.commit()

        db.add(CampaignSendLog(
            tenant_id=t.id, campaign_id=camp.id,
            customer_phone_e164="+966500000050",  # same phone, same campaign
            template_name=tpl.name, template_language="ar",
            status=LOG_QUEUED,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
