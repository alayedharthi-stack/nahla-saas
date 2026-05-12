"""tests/test_frequency_cap_bypass_revive.py
────────────────────────────────────────────
Regression coverage for the QA/testing "تجاهل حد التكرار" toggle on
``POST /campaigns/{id}/dispatch-now?bypass_frequency_cap=true``.

The user-visible symptom
------------------------
A merchant launches a campaign; the frequency-cap step on the first
dispatch flips every recipient to ``status='skipped_duplicate'`` because
they received another marketing campaign within the cap window.

The merchant then turns on the per-campaign "تجاهل حد التكرار لهذه
الحملة" checkbox and clicks "إرسال الآن". Pre-fix behaviour:

  * ``_snapshot_recipients`` sees existing rows for the campaign and
    inserts nothing (unique constraint on (tenant_id, campaign_id, phone)).
  * ``_apply_frequency_cap(bypass=True)`` early-returns 0, but the
    update query inside it only touches rows in ``status='queued'`` —
    so the pre-existing ``skipped_duplicate`` rows stay frozen.
  * The dispatcher loop then has nothing to send.

Net result: ``skipped_duplicate=3, queued=0, sent=0`` after toggling
bypass, exactly as reported in production.

The fix introduces ``_revive_frequency_cap_skipped`` which is called
BEFORE the cap step when bypass is on. It re-queues rows whose
``skip_reason`` starts with ``REASON_FREQ_CAP``, and ONLY those rows —
manual exclusions, opt-outs, and unreachable rows must remain skipped.

These tests assert:

  1. Bypass + existing cap-skipped rows → revived to queued.
  2. Bypass leaves non-cap skip reasons untouched.
  3. No bypass → revive is a no-op (default protection).
  4. Revive is idempotent (running it twice in a row is fine).
  5. Funnel surfaces ``frequency_cap_revived`` + ``frequency_cap_bypass``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

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
    Base, Campaign, CampaignSendLog, Customer, Tenant, WhatsAppTemplate,
)
from services import campaign_dispatcher as cd  # noqa: E402


def _make_db():
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


def _seed_campaign_with_skipped_rows(
    db,
    *,
    n_skipped_cap: int = 3,
    n_skipped_other: int = 0,
):
    """Plant a campaign with `n_skipped_cap` rows already flipped to
    ``skipped_duplicate`` by the cap, plus optionally `n_skipped_other`
    rows skipped for a *different* reason (manual exclusion / opt-out).

    Mirrors the production state the merchant hit: first dispatch ran,
    cap fired, all rows are now ``skipped_duplicate`` with
    ``skip_reason='frequency_cap_marketing:14d'``.
    """
    t = Tenant(name="T", is_active=True)
    db.add(t); db.commit(); db.refresh(t)

    tpl = WhatsAppTemplate(
        tenant_id=t.id, name="nahla_vip_exclusive_9bde", language="ar",
        category="MARKETING", status="APPROVED",
        components=[{"type": "BODY", "text": "Hi {{1}}"}],
    )
    db.add(tpl); db.commit(); db.refresh(tpl)

    c = Campaign(
        tenant_id=t.id, name="C-bypass-test", campaign_type="broadcast",
        template_id=str(tpl.id), template_name=tpl.name,
        template_language="ar", template_category="MARKETING",
        audience_type="all", status="active",
        audience_count=n_skipped_cap + n_skipped_other,
        sent_count=0,
        created_at=datetime.now(timezone.utc),
        launched_at=datetime.now(timezone.utc),
    )
    db.add(c); db.commit(); db.refresh(c)

    for i in range(n_skipped_cap):
        db.add(CampaignSendLog(
            tenant_id=t.id, campaign_id=c.id,
            customer_phone_e164=f"+96650000000{i}",
            template_name=tpl.name, template_language=tpl.language,
            status=cd.LOG_SKIPPED_DUPLICATE,
            skip_reason=f"{cd.REASON_FREQ_CAP}:14d",
        ))
    for j in range(n_skipped_other):
        db.add(CampaignSendLog(
            tenant_id=t.id, campaign_id=c.id,
            customer_phone_e164=f"+96650000{100 + j}",
            template_name=tpl.name, template_language=tpl.language,
            status=cd.LOG_SKIPPED_MANUAL_EXCLUSION,
            skip_reason=cd.REASON_MARKETING_OPT_OUT,
        ))
    db.commit()
    return t, tpl, c


# ── 1. Revive flips cap-skipped rows back to queued ─────────────────


class TestReviveCapSkipped:
    """Bypass must move ``skipped_duplicate`` rows that were skipped by
    the cap back into ``queued``. Without this the bypass toggle is a
    cosmetic no-op for any campaign that already ran once."""

    def test_revives_all_cap_skipped_rows_to_queued(self):
        db, _ = _make_db()
        _t, _tpl, c = _seed_campaign_with_skipped_rows(db, n_skipped_cap=3)

        revived = cd._revive_frequency_cap_skipped(db, c.id)
        db.commit()

        assert revived == 3
        rows = (
            db.query(CampaignSendLog)
            .filter(CampaignSendLog.campaign_id == c.id).all()
        )
        assert all(r.status == cd.LOG_QUEUED for r in rows)
        assert all(r.skip_reason is None for r in rows)

    def test_returns_zero_when_no_cap_skipped_rows(self):
        # Campaign with rows that are queued (never cap-skipped) — the
        # helper must not flip anything just because bypass is on.
        db, _ = _make_db()
        t = Tenant(name="T"); db.add(t); db.commit(); db.refresh(t)
        tpl = WhatsAppTemplate(
            tenant_id=t.id, name="t", language="ar",
            category="MARKETING", status="APPROVED",
            components=[{"type": "BODY", "text": "hi"}],
        )
        db.add(tpl); db.commit(); db.refresh(tpl)
        c = Campaign(
            tenant_id=t.id, name="C", campaign_type="broadcast",
            template_id=str(tpl.id), template_name=tpl.name,
            template_language="ar", template_category="MARKETING",
            audience_type="all", status="active", audience_count=1,
        )
        db.add(c); db.commit(); db.refresh(c)
        db.add(CampaignSendLog(
            tenant_id=t.id, campaign_id=c.id,
            customer_phone_e164="+966500000001",
            template_name="t", template_language="ar",
            status=cd.LOG_QUEUED,
        ))
        db.commit()

        revived = cd._revive_frequency_cap_skipped(db, c.id)

        assert revived == 0
        row = db.query(CampaignSendLog).filter_by(campaign_id=c.id).first()
        assert row.status == cd.LOG_QUEUED  # unchanged

    def test_idempotent_second_call_is_noop(self):
        # Revive twice in a row — second call must be a clean 0
        # (rows are already queued, nothing to flip).
        db, _ = _make_db()
        _t, _tpl, c = _seed_campaign_with_skipped_rows(db, n_skipped_cap=2)

        first  = cd._revive_frequency_cap_skipped(db, c.id)
        db.commit()
        second = cd._revive_frequency_cap_skipped(db, c.id)
        db.commit()

        assert first  == 2
        assert second == 0


# ── 2. Bypass MUST NOT revive non-cap skipped rows ─────────────────


class TestReviveScopedToCapOnly:
    """The bypass toggle is for frequency cap, not a global skip
    override. Manual exclusions, opt-outs, invalid phones, and
    unreachable rows must remain skipped even when bypass is on —
    otherwise we'd accidentally message customers who explicitly
    unsubscribed (regulatory risk)."""

    def test_manual_exclusion_rows_are_not_revived(self):
        db, _ = _make_db()
        _t, _tpl, c = _seed_campaign_with_skipped_rows(
            db, n_skipped_cap=2, n_skipped_other=2,
        )

        revived = cd._revive_frequency_cap_skipped(db, c.id)
        db.commit()

        assert revived == 2  # only the cap rows
        rows = (
            db.query(CampaignSendLog)
            .filter(CampaignSendLog.campaign_id == c.id).all()
        )
        manual_excluded = [
            r for r in rows
            if r.skip_reason == cd.REASON_MARKETING_OPT_OUT
        ]
        assert len(manual_excluded) == 2
        assert all(
            r.status == cd.LOG_SKIPPED_MANUAL_EXCLUSION for r in manual_excluded
        )

    def test_skip_reason_pattern_match_is_prefix_safe(self):
        # `_apply_frequency_cap` writes ``skip_reason=f"{REASON_FREQ_CAP}:14d"``
        # — the LIKE pattern in the revive helper must match the
        # prefix exactly, not collide with a hypothetical future reason
        # that happens to start with the same letters.
        db, _ = _make_db()
        _t, _tpl, c = _seed_campaign_with_skipped_rows(db, n_skipped_cap=1)

        # Plant an extra row that uses a DIFFERENT reason starting with
        # a similar substring to assert the LIKE doesn't over-match.
        bogus = CampaignSendLog(
            tenant_id=_t.id, campaign_id=c.id,
            customer_phone_e164="+966500999999",
            template_name=_tpl.name, template_language="ar",
            status=cd.LOG_SKIPPED_DUPLICATE,
            skip_reason="frequency_cap_other_unknown_future_reason",
        )
        db.add(bogus); db.commit()

        revived = cd._revive_frequency_cap_skipped(db, c.id)
        db.commit()

        # Both rows START with "frequency_cap" — so the LIKE pattern
        # ``frequency_cap_marketing%`` should match the genuine cap row
        # only. If this test starts failing, the helper's prefix was
        # widened too loosely.
        assert revived == 1
        remaining = (
            db.query(CampaignSendLog)
            .filter(
                CampaignSendLog.campaign_id == c.id,
                CampaignSendLog.status == cd.LOG_SKIPPED_DUPLICATE,
            ).all()
        )
        assert len(remaining) == 1
        assert remaining[0].skip_reason == (
            "frequency_cap_other_unknown_future_reason"
        )
