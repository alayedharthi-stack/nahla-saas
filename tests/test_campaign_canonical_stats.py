"""
tests/test_campaign_canonical_stats.py
──────────────────────────────────────
Regression coverage for the canonical campaign-stats aggregator —
the helper that drives the top-of-page summary cards, the per-row
table cells, and the campaign detail panel.

The merchant reported a screenshot mismatch: the detail panel showed
``Meta accepted=5905, Delivered=4397, Read=1543`` while the summary
card on top read ``إجمالي المرسل ≈ 2876`` and ``معدل القراءة 54%``.
That's a classic "drift in ``Campaign.sent_count``" symptom — the
column is a best-effort incremental counter the dispatcher writes,
but it doesn't survive wave-mode restarts or partial dispatches.
The canonical source of truth is ``CampaignSendLog`` rows + the
nullable ``delivered_at`` / ``read_at`` / ``failed_at`` timestamps.

These tests pin the contract so it doesn't regress again:

  * Aggregator buckets exactly match the merchant screenshot
    semantics (Meta accepted / Delivered / Read / Not delivered yet /
    Failed after accept / Queued / Failed).
  * Aggregator survives a stale ``Campaign.sent_count`` (the bug
    that produced the 2876 number in the screenshot).
  * ``/campaigns`` list and ``/campaigns/{id}/report`` agree on
    every number — no more dashboard saying 54% while detail says 35%.
  * Rates are emitted with explicit, named denominators
    (delivery_rate, read_rate_of_accepted, read_rate_of_delivered)
    so the UI can label them transparently.
  * Sums across multiple campaigns (what the dashboard cards show)
    match summing the per-campaign canonical numbers.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

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


def _seed_tenant(db, tenant_id: int = 33):
    from models import Tenant
    if not db.query(Tenant).filter(Tenant.id == tenant_id).first():
        db.add(Tenant(id=tenant_id, name=f"t-{tenant_id}"))
        db.commit()


def _seed_campaign(
    db,
    *,
    tenant_id: int = 33,
    campaign_id: int = 77,
    sent_count_column: int = 0,
    delivered_count_column: int = 0,
    read_count_column: int = 0,
):
    """Create a Campaign row with optionally stale legacy counters.

    The merchant's bug is precisely that ``sent_count_column`` (=2876)
    is wildly different from the actual ``CampaignSendLog`` rows
    (=5905). Tests pass arbitrary values to ``*_column`` arguments to
    confirm the canonical aggregator ignores them.
    """
    from models import Campaign, WhatsAppTemplate
    _seed_tenant(db, tenant_id)
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id, name=f"tpl{campaign_id}", language="ar",
        category="MARKETING", status="APPROVED",
        components=[{"type": "BODY", "text": "hi"}],
    )
    db.add(tpl)
    db.commit()
    c = Campaign(
        id=campaign_id, tenant_id=tenant_id, name=f"C-{campaign_id}",
        campaign_type="custom", status="completed",
        audience_type="all", template_id=tpl.id,
        sent_count=sent_count_column,
        delivered_count=delivered_count_column,
        read_count=read_count_column,
    )
    db.add(c)
    db.commit()
    return c


def _bulk_send_logs(
    db,
    *,
    tenant_id: int,
    campaign_id: int,
    status: str,
    count: int,
    with_delivered_at: int = 0,
    with_read_at: int = 0,
    with_failed_at: int = 0,
    base_phone: int = 966500000000,
):
    """Insert ``count`` CampaignSendLog rows in the given status with
    a configurable number that also carry delivered_at / read_at /
    failed_at timestamps (subsets of ``count``).

    ``with_*`` arguments must be ≤ ``count``. Each timestamp is
    applied to the *first* N rows in insertion order, which matches
    the real-world pattern (older rows are more likely to have all
    webhook echoes back).
    """
    from models import CampaignSendLog
    assert 0 <= with_delivered_at <= count
    assert 0 <= with_read_at <= count
    assert 0 <= with_failed_at <= count
    now = datetime(2026, 5, 11, 12, 0, 0)
    rows = []
    for i in range(count):
        rows.append(CampaignSendLog(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            customer_phone_e164=f"+{base_phone + campaign_id * 100000 + i}",
            template_name=f"tpl{campaign_id}",
            template_language="ar",
            status=status,
            provider_message_id=f"wamid.{campaign_id}.{i}" if status == "sent" else None,
            sent_at=now if status == "sent" else None,
            delivered_at=now if i < with_delivered_at else None,
            read_at=now if i < with_read_at else None,
            failed_at=now if i < with_failed_at else None,
        ))
    db.add_all(rows)
    db.commit()


# ──────────────────────────────────────────────────────────────────────
# Aggregator unit tests
# ──────────────────────────────────────────────────────────────────────


class TestCampaignCanonicalStats:
    def test_empty_campaign_returns_empty_dict(self):
        """A campaign with zero send-log rows must NOT appear in the
        aggregator output. Callers fall back to legacy columns / show
        zeros when their campaign id is missing from the result."""
        from routers.campaigns import _campaign_canonical_stats

        db, _ = _make_db()
        _seed_campaign(db, campaign_id=1)
        result = _campaign_canonical_stats(db, [1])
        assert result == {}

    def test_merchant_screenshot_buckets_exact(self):
        """Mirror the EXACT numbers from the merchant's screenshot:

          Meta accepted (status='sent') = 5905
          Delivered (delivered_at NOT NULL) = 4397
          Read (read_at NOT NULL) = 1543
          Failed after accept (failed_at NOT NULL) = 1139
          Not delivered yet (derived) = 5905 - 4397 - 1139 = 369
          Failed (status='failed' pre-accept) = 8
          Queued (status='queued' or 'sending') = 2107

        Total recipients = 5905 + 8 + 2107 = 8020.

        Note: the screenshot lists 1508 as "لم تصل بعد" — but the
        screenshot likely uses ``sent - delivered`` (not subtracting
        failed_after_accept). The canonical aggregator uses the
        merchant-friendly definition (subtract both delivered and
        post-accept failures), which is more precise and prevents
        double-counting. The number renders as a sub-bucket so the
        merchant always sees the full breakdown.
        """
        from routers.campaigns import _campaign_canonical_stats

        db, _ = _make_db()
        _seed_campaign(db, campaign_id=77, sent_count_column=2876)
        _bulk_send_logs(
            db, tenant_id=33, campaign_id=77, status="sent",
            count=5905, with_delivered_at=4397, with_read_at=1543,
            with_failed_at=1139,
        )
        _bulk_send_logs(
            db, tenant_id=33, campaign_id=77, status="failed",
            count=8, base_phone=977000000000,
        )
        _bulk_send_logs(
            db, tenant_id=33, campaign_id=77, status="queued",
            count=2107, base_phone=988000000000,
        )

        result = _campaign_canonical_stats(db, [77])
        stats = result[77]
        assert stats["meta_accepted"]       == 5905
        assert stats["delivered"]           == 4397
        assert stats["read"]                == 1543
        assert stats["failed_after_accept"] == 1139
        assert stats["not_delivered_yet"]   == 5905 - 4397 - 1139
        assert stats["failed"]              == 8
        assert stats["queued"]              == 2107
        assert stats["total_recipients"]    == 5905 + 8 + 2107

    def test_aggregator_ignores_stale_campaign_columns(self):
        """The whole point of the canonical aggregator: even when
        Campaign.sent_count is wildly wrong (drifted to 2876 due to
        a wave-mode restart) the merchant sees the real CampaignSendLog
        count (5905). This is the regression that produced the
        merchant's 54% bogus read rate."""
        from routers.campaigns import _campaign_canonical_stats

        db, _ = _make_db()
        # Sprinkle wildly wrong legacy values — the aggregator must
        # not even look at them.
        _seed_campaign(
            db, campaign_id=42,
            sent_count_column=2876,           # stale: real is 5905
            delivered_count_column=999,       # stale: real is 4397
            read_count_column=99999,          # stale: real is 1543
        )
        _bulk_send_logs(
            db, tenant_id=33, campaign_id=42, status="sent",
            count=5905, with_delivered_at=4397, with_read_at=1543,
        )
        stats = _campaign_canonical_stats(db, [42])[42]
        assert stats["meta_accepted"] == 5905
        assert stats["delivered"]     == 4397
        assert stats["read"]          == 1543

    def test_aggregator_handles_skipped_buckets(self):
        """``skipped_duplicate``, ``skipped_invalid``, etc. all roll
        up into a single ``skipped`` bucket. The detail report
        endpoint exposes the individual sub-buckets — the aggregator
        only needs the rollup for the summary cards."""
        from routers.campaigns import _campaign_canonical_stats

        db, _ = _make_db()
        _seed_campaign(db, campaign_id=11)
        _bulk_send_logs(
            db, tenant_id=33, campaign_id=11, status="skipped_duplicate",
            count=12, base_phone=910000000000,
        )
        _bulk_send_logs(
            db, tenant_id=33, campaign_id=11, status="skipped_unsubscribed",
            count=5, base_phone=920000000000,
        )
        _bulk_send_logs(
            db, tenant_id=33, campaign_id=11, status="skipped_invalid",
            count=3, base_phone=930000000000,
        )
        stats = _campaign_canonical_stats(db, [11])[11]
        assert stats["skipped"] == 12 + 5 + 3
        assert stats["queued"]  == 0
        assert stats["failed"]  == 0
        assert stats["meta_accepted"] == 0

    def test_aggregator_batches_multiple_campaigns_in_one_query(self):
        """The list endpoint calls the aggregator once with ALL
        campaign ids. The result must be a {id → stats} map with one
        entry per campaign that has rows."""
        from routers.campaigns import _campaign_canonical_stats

        db, _ = _make_db()
        for cid, sent_n in [(101, 10), (102, 20), (103, 5)]:
            _seed_campaign(db, campaign_id=cid)
            _bulk_send_logs(
                db, tenant_id=33, campaign_id=cid, status="sent",
                count=sent_n, with_delivered_at=sent_n // 2,
                with_read_at=sent_n // 4,
                base_phone=900000000000 + cid * 100,
            )
        result = _campaign_canonical_stats(db, [101, 102, 103])
        assert result[101]["meta_accepted"] == 10
        assert result[102]["meta_accepted"] == 20
        assert result[103]["meta_accepted"] == 5
        assert result[102]["delivered"]     == 10
        assert result[103]["read"]          == 1

    def test_aggregator_with_no_ids_returns_empty(self):
        from routers.campaigns import _campaign_canonical_stats

        db, _ = _make_db()
        assert _campaign_canonical_stats(db, []) == {}

    def test_not_delivered_yet_clamps_at_zero(self):
        """Defensive: webhook double-counting could in theory produce
        delivered > meta_accepted; the derived bucket must never go
        negative. (The aggregator computes the count via separate
        non-null counts, so this case shouldn't arise in practice,
        but the clamp protects the UI.)"""
        from routers.campaigns import _campaign_canonical_stats

        db, _ = _make_db()
        _seed_campaign(db, campaign_id=200)
        _bulk_send_logs(
            db, tenant_id=33, campaign_id=200, status="sent",
            count=3, with_delivered_at=3, with_failed_at=2,
        )
        # delivered=3, failed_after_accept=2 → not_delivered_yet =
        # max(0, 3 - 3 - 2) = max(0, -2) = 0.
        stats = _campaign_canonical_stats(db, [200])[200]
        assert stats["not_delivered_yet"] == 0


# ──────────────────────────────────────────────────────────────────────
# Rate annotation
# ──────────────────────────────────────────────────────────────────────


class TestStatsWithRates:
    def test_rates_match_merchant_screenshot_math(self):
        """1543 / 5905 = 0.2613… (read of accepted).
        1543 / 4397 = 0.3509… (read of delivered).
        4397 / 5905 = 0.7446… (delivery)."""
        from routers.campaigns import _stats_with_rates

        annotated = _stats_with_rates({
            "meta_accepted": 5905,
            "delivered":     4397,
            "read":          1543,
        })
        assert annotated["delivery_rate"]          == pytest.approx(0.7446, abs=0.0001)
        assert annotated["read_rate_of_accepted"]  == pytest.approx(0.2613, abs=0.0001)
        assert annotated["read_rate_of_delivered"] == pytest.approx(0.3509, abs=0.0001)

    def test_rates_are_none_when_denominator_is_zero(self):
        """Frontend renders ``—`` instead of ``0%`` when there's no
        data yet. The aggregator must NEVER divide-by-zero or emit a
        misleading 0% for a campaign that hasn't started."""
        from routers.campaigns import _stats_with_rates
        annotated = _stats_with_rates({
            "meta_accepted": 0, "delivered": 0, "read": 0,
        })
        assert annotated["delivery_rate"]          is None
        assert annotated["read_rate_of_accepted"]  is None
        assert annotated["read_rate_of_delivered"] is None

    def test_read_rate_of_delivered_handles_zero_delivered_with_accepted(self):
        """During the gap between Meta-accept and the first delivery
        webhook the campaign has accepted>0 but delivered=0. The
        delivery rate is 0 (we know none have delivered yet) but
        read_rate_of_delivered must be None (no denominator)."""
        from routers.campaigns import _stats_with_rates
        annotated = _stats_with_rates({
            "meta_accepted": 100, "delivered": 0, "read": 0,
        })
        assert annotated["delivery_rate"]          == 0.0
        assert annotated["read_rate_of_accepted"]  == 0.0
        assert annotated["read_rate_of_delivered"] is None


# ──────────────────────────────────────────────────────────────────────
# Wire-level: _campaign_to_dict prefers canonical over Campaign columns
# ──────────────────────────────────────────────────────────────────────


class TestCampaignToDictUsesCanonical:
    def test_dict_overrides_stale_sent_count_when_canonical_supplied(self):
        """The merchant's bug: top-of-page card shows 2876 because
        Campaign.sent_count drifted. With the canonical override,
        sent_count in the dict reflects CampaignSendLog reality."""
        from routers.campaigns import _campaign_to_dict

        db, _ = _make_db()
        c = _seed_campaign(
            db, campaign_id=300,
            sent_count_column=2876,        # stale
            delivered_count_column=2200,   # stale
            read_count_column=1500,        # stale
        )
        canonical = {
            "meta_accepted":       5905,
            "delivered":           4397,
            "read":                1543,
            "failed_after_accept": 1139,
            "not_delivered_yet":   369,
            "failed":              8,
            "queued":              2107,
            "skipped":             0,
            "total_recipients":    8020,
        }
        out = _campaign_to_dict(c, canonical_stats=canonical)
        assert out["sent_count"]      == 5905
        assert out["delivered_count"] == 4397
        assert out["read_count"]      == 1543
        assert out["stats"]["meta_accepted"] == 5905
        assert out["stats"]["delivery_rate"] == pytest.approx(0.7446, abs=0.0001)
        # The merchant's wrong "54%" came from 1543/2876 ≈ 0.5365.
        # The canonical read_rate_of_accepted is 1543/5905 = 0.2613
        # — far from 54%. The fix is regression-pinned here.
        assert out["stats"]["read_rate_of_accepted"] == pytest.approx(0.2613, abs=0.0001)
        # And `read / delivered` is the explicit "of the ones who
        # received it, X% read" rate.
        assert out["stats"]["read_rate_of_delivered"] == pytest.approx(0.3509, abs=0.0001)

    def test_dict_falls_back_to_columns_when_no_canonical(self):
        """Backwards-compat: callers that don't pass canonical_stats
        keep the legacy behaviour. Used by older code paths and as
        the bootstrap-time default before any send-log rows exist."""
        from routers.campaigns import _campaign_to_dict

        db, _ = _make_db()
        c = _seed_campaign(
            db, campaign_id=301,
            sent_count_column=100,
            delivered_count_column=80,
            read_count_column=40,
        )
        out = _campaign_to_dict(c, canonical_stats=None)
        assert out["sent_count"]      == 100
        assert out["delivered_count"] == 80
        assert out["read_count"]      == 40
        # Stats block is still emitted (from the column fallback) so
        # the frontend's typed code can rely on `stats` always being
        # present.
        assert out["stats"]["meta_accepted"] == 100

    def test_lifecycle_uses_canonical_sent_for_completed_check(self):
        """A "completed" campaign with stale ``sent_count=0`` but
        5905 actual sends in CampaignSendLog must surface as
        lifecycle=``sent``, not ``completed_empty``. The lifecycle
        verb drives the badge color — we don't want a successful
        campaign to look empty just because the counter drifted."""
        from routers.campaigns import _campaign_to_dict

        db, _ = _make_db()
        c = _seed_campaign(
            db, campaign_id=302,
            sent_count_column=0,   # very stale
        )
        canonical = {
            "meta_accepted":     5905,
            "delivered":         4397,
            "read":              1543,
            "failed_after_accept": 0,
            "not_delivered_yet": 1508,
            "failed":            0,
            "queued":            0,
            "skipped":           0,
            "total_recipients":  5905,
        }
        out = _campaign_to_dict(c, canonical_stats=canonical)
        assert out["lifecycle"] == "sent"
