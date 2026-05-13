"""tests/test_wave_scheduler.py
─────────────────────────────────
Phase 4 — Wave/Batch sending architecture.

These tests lock down the planning + persistence layer
(``services/wave_scheduler.py``). The dispatcher integration is
covered separately by smoke runs through ``dispatch_campaign``
in ``test_campaign_dispatcher_*``.

What we lock down
─────────────────
1.  **Small-campaign carve-out.** Anything under
    :data:`WAVE_THRESHOLD_RECIPIENTS` is forced to
    ``strategy='immediate'`` — even if the merchant asks for
    adaptive. The wave UI must never appear for a coffee shop.

2.  **Quality-tier → batch_size + delay** mapping is monotonic
    in the right direction: a worse tier produces SMALLER batches
    and LONGER inter-wave delays.

3.  **Audience splits cleanly** across waves: the planner's wave
    sizes sum to the audience size; the last wave inherits the
    remainder; total wave count = ``ceil(audience / batch_size)``.

4.  **Meta RED rating** demotes the effective tier by one band
    even if Nahla's own tier says healthy.

5.  **plan_waves edge cases** — zero audience, audience smaller
    than batch_size, audience that's an exact multiple of
    batch_size, audience with leftover.

6.  **Persistence layer** — ``materialise_waves`` writes one row
    per wave with the right denormalised columns;
    ``assign_send_logs_to_waves`` distributes rows in id-order
    respecting each wave's planned slot.

7.  **Due-wave picking** — only ``status='pending'`` waves whose
    ``scheduled_at <= now()`` are returned, regardless of
    campaign or tenant.

Regression guards we explicitly do NOT skip
───────────────────────────────────────────
* The 'immediate downgrade' guard. A small campaign that the
  merchant accidentally clicks ``adaptive`` for must still end
  up as ``immediate`` with zero waves. Without this the
  scheduler would create a "wave 1 of 1" row that never fires
  because the dispatcher already ran inline.
"""
from __future__ import annotations

import math
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
    CampaignWave,
    Tenant,
)
from services import wave_scheduler as ws  # noqa: E402


# ──────────────────────────────────────────────────────────────────
# SQLite shim — same pattern as test_quality_score.py
# ──────────────────────────────────────────────────────────────────


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


def _seed_tenant(db, name="WV") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_campaign(db, tenant_id: int, audience: int) -> Campaign:
    c = Campaign(
        tenant_id=tenant_id,
        name="Test broadcast",
        campaign_type="broadcast",
        status="scheduled",
        audience_type="all",
        audience_count=audience,
        send_strategy="immediate",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_queued_send_logs(
    db, *, tenant_id: int, campaign_id: int, count: int,
) -> list[int]:
    """Create ``count`` queued send_log rows for a campaign.

    Returns the list of inserted ids in insertion order so the
    test can verify wave assignment order.
    """
    ids: list[int] = []
    for i in range(count):
        row = CampaignSendLog(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            customer_id=None,
            customer_phone_e164=f"+9665{i:08d}",
            status="queued",
        )
        db.add(row)
        db.flush()
        ids.append(int(row.id))
    db.commit()
    return ids


# ──────────────────────────────────────────────────────────────────
# 1) Small-campaign carve-out
# ──────────────────────────────────────────────────────────────────


class TestSmallCampaignCarveOut:
    """Audiences below the threshold must NEVER recommend waves."""

    @pytest.mark.parametrize("size", [0, 1, 50, 100, ws.WAVE_THRESHOLD_RECIPIENTS - 1])
    def test_adaptive_downgrades_to_immediate(self, size):
        spec = ws.compute_adaptive_strategy(audience_size=size)
        assert spec.strategy == ws.STRATEGY_IMMEDIATE, (
            f"audience={size} must stay immediate"
        )
        if size == 0:
            assert spec.total_waves == 0
            assert spec.waves == []
        else:
            assert spec.total_waves == 1
            assert len(spec.waves) == 1
            assert spec.waves[0].planned_recipients == size

    def test_threshold_exact_triggers_waves(self):
        # The boundary itself should produce waves (>=, not >).
        spec = ws.compute_adaptive_strategy(
            audience_size=ws.WAVE_THRESHOLD_RECIPIENTS,
            quality_tier="healthy",
        )
        assert spec.strategy == ws.STRATEGY_ADAPTIVE
        assert spec.total_waves >= 1


# ──────────────────────────────────────────────────────────────────
# 2) Tier → (batch_size, delay) monotonicity
# ──────────────────────────────────────────────────────────────────


class TestTierMapping:
    """A worse Nahla tier MUST produce smaller batches and longer
    delays. If someone tweaks the tier table accidentally the
    monotonicity breaks and we'd start sending faster to risky
    tenants — exactly the bug this layer exists to prevent."""

    TIERS_BEST_TO_WORST = ("excellent", "healthy", "warning", "risky", "critical")

    def test_batch_size_monotonic_decreasing(self):
        sizes = [ws.suggest_batch_size_for_tier(t) for t in self.TIERS_BEST_TO_WORST]
        for a, b in zip(sizes, sizes[1:]):
            assert a > b, f"batch_size monotonicity broken: {sizes}"

    def test_delay_monotonic_increasing(self):
        delays = [ws.suggest_delay_for_tier(t) for t in self.TIERS_BEST_TO_WORST]
        for a, b in zip(delays, delays[1:]):
            assert a < b, f"delay monotonicity broken: {delays}"

    def test_unknown_tier_falls_back_to_healthy(self):
        assert (
            ws.suggest_batch_size_for_tier("nonsense")
            == ws.suggest_batch_size_for_tier("healthy")
        )
        assert (
            ws.suggest_delay_for_tier(None)
            == ws.suggest_delay_for_tier("healthy")
        )

    def test_published_tier_examples_from_user_spec(self):
        # The user's request explicitly named these batch sizes:
        #   Excellent → 5000, Healthy → 2000, Warning → 500, Risky → 100.
        # Lock them so a tuning change without UX sign-off fails CI.
        assert ws.suggest_batch_size_for_tier("excellent") == 5000
        assert ws.suggest_batch_size_for_tier("healthy")   == 2000
        assert ws.suggest_batch_size_for_tier("warning")   == 500
        assert ws.suggest_batch_size_for_tier("risky")     == 100


# ──────────────────────────────────────────────────────────────────
# 3) plan_waves arithmetic
# ──────────────────────────────────────────────────────────────────


class TestPlanWavesArithmetic:
    """The pure ``plan_waves`` function — the heart of how an
    audience gets split. Asserted exhaustively so any off-by-one
    is caught at the boundary."""

    def test_zero_audience_returns_empty(self):
        assert ws.plan_waves(
            audience_size=0, batch_size=100, delay_between_batches_sec=60,
        ) == []

    def test_audience_smaller_than_batch_yields_one_wave(self):
        waves = ws.plan_waves(
            audience_size=80, batch_size=500, delay_between_batches_sec=3600,
        )
        assert len(waves) == 1
        assert waves[0].planned_recipients == 80
        assert waves[0].wave_index == 1

    def test_exact_multiple_yields_no_remainder(self):
        # 6000 / 2000 = 3 even waves.
        waves = ws.plan_waves(
            audience_size=6000, batch_size=2000, delay_between_batches_sec=1800,
        )
        assert len(waves) == 3
        assert [w.planned_recipients for w in waves] == [2000, 2000, 2000]

    def test_remainder_lands_in_last_wave(self):
        waves = ws.plan_waves(
            audience_size=5500, batch_size=2000, delay_between_batches_sec=1800,
        )
        assert len(waves) == 3
        sizes = [w.planned_recipients for w in waves]
        assert sizes == [2000, 2000, 1500]
        assert sum(sizes) == 5500

    def test_scheduled_times_are_evenly_spaced(self):
        moment = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
        waves = ws.plan_waves(
            audience_size=5000, batch_size=1000,
            delay_between_batches_sec=3600, now=moment,
        )
        assert len(waves) == 5
        for i, w in enumerate(waves):
            assert w.scheduled_at == moment + timedelta(hours=i)
            assert w.wave_index == i + 1

    def test_batch_size_zero_collapses_to_single_wave(self):
        # Defensive: invalid input must NOT raise — the API layer
        # already validates, but the planner has to be robust.
        waves = ws.plan_waves(
            audience_size=1000, batch_size=0, delay_between_batches_sec=60,
        )
        assert len(waves) == 1
        assert waves[0].planned_recipients == 1000


# ──────────────────────────────────────────────────────────────────
# 4) Meta RED rating demotes the effective tier
# ──────────────────────────────────────────────────────────────────


class TestMetaRatingOverride:
    def test_red_demotes_healthy_to_warning(self):
        # Audience above threshold so we actually run the adaptive
        # branch.
        spec = ws.compute_adaptive_strategy(
            audience_size=3000,
            quality_tier="healthy",
            meta_quality_rating="RED",
        )
        # Healthy → demoted to ``warning`` → batch_size 500.
        assert spec.batch_size == ws.suggest_batch_size_for_tier("warning")
        assert "Meta" in spec.rationale

    def test_green_keeps_nahla_tier(self):
        spec = ws.compute_adaptive_strategy(
            audience_size=3000,
            quality_tier="healthy",
            meta_quality_rating="GREEN",
        )
        assert spec.batch_size == ws.suggest_batch_size_for_tier("healthy")

    def test_red_on_risky_floors_at_critical(self):
        # Already-risky tenants with a Meta RED rating get the
        # most conservative plan we have — critical tier.
        spec = ws.compute_adaptive_strategy(
            audience_size=3000,
            quality_tier="risky",
            meta_quality_rating="RED",
        )
        assert spec.batch_size == ws.suggest_batch_size_for_tier("critical")


# ──────────────────────────────────────────────────────────────────
# 5) materialise_waves + assign_send_logs_to_waves
# ──────────────────────────────────────────────────────────────────


class TestPersistence:
    def test_materialise_creates_one_row_per_wave(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        camp = _seed_campaign(db, tenant.id, audience=5500)
        spec = ws.compute_adaptive_strategy(
            audience_size=5500, quality_tier="healthy",
        )
        rows = ws.materialise_waves(db=db, campaign=camp, spec=spec)
        db.commit()

        assert len(rows) == spec.total_waves
        assert all(r.id is not None for r in rows)
        assert all(r.tenant_id == tenant.id for r in rows)
        assert all(r.campaign_id == camp.id for r in rows)
        assert [r.wave_index for r in rows] == list(range(1, spec.total_waves + 1))
        assert all(r.total_waves == spec.total_waves for r in rows)
        assert all(r.status == ws.WAVE_PENDING for r in rows)
        assert sum(r.planned_recipients for r in rows) == 5500

    def test_assign_send_logs_distributes_in_id_order(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        camp = _seed_campaign(db, tenant.id, audience=10)

        # Deliberately pick batch=4 → waves: [4, 4, 2].
        spec = ws.WavePlanSpec(
            strategy=ws.STRATEGY_BATCHED,
            audience_size=10,
            batch_size=4,
            delay_between_batches_sec=60,
            total_waves=3,
            estimated_completion_at=None,
            rationale="test",
            waves=ws.plan_waves(
                audience_size=10, batch_size=4, delay_between_batches_sec=60,
            ),
        )
        # Force the small-campaign carve-out OFF for this test:
        # we constructed the spec by hand, not via
        # compute_adaptive_strategy, so it isn't downgraded.
        waves = ws.materialise_waves(db=db, campaign=camp, spec=spec)
        log_ids = _seed_queued_send_logs(
            db, tenant_id=tenant.id, campaign_id=camp.id, count=10,
        )
        n = ws.assign_send_logs_to_waves(
            db=db, campaign_id=camp.id, waves=waves,
        )
        db.commit()
        assert n == 10

        # First 4 ids should belong to wave 1, next 4 to wave 2,
        # last 2 to wave 3.
        rows = (
            db.query(CampaignSendLog)
            .filter(CampaignSendLog.campaign_id == camp.id)
            .order_by(CampaignSendLog.id.asc())
            .all()
        )
        slices = [
            rows[0:4],
            rows[4:8],
            rows[8:10],
        ]
        for wave, slc in zip(waves, slices):
            assert all(r.wave_id == wave.id for r in slc), (
                f"wave {wave.wave_index} got the wrong rows"
            )

    def test_assign_corrects_planned_when_actual_is_smaller(self):
        # Plan says wave 1 = 4 recipients but only 3 queued rows
        # exist (some got skipped between plan and snapshot). The
        # assigner should rewrite planned_recipients to the
        # actual count so the UI's "x/y sent" stays honest.
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        camp = _seed_campaign(db, tenant.id, audience=4)
        spec = ws.WavePlanSpec(
            strategy=ws.STRATEGY_BATCHED, audience_size=4,
            batch_size=4, delay_between_batches_sec=0, total_waves=1,
            estimated_completion_at=None, rationale="",
            waves=[ws.WaveEntry(
                wave_index=1, planned_recipients=4,
                scheduled_at=datetime.now(timezone.utc),
            )],
        )
        waves = ws.materialise_waves(db=db, campaign=camp, spec=spec)
        _seed_queued_send_logs(
            db, tenant_id=tenant.id, campaign_id=camp.id, count=3,
        )
        ws.assign_send_logs_to_waves(
            db=db, campaign_id=camp.id, waves=waves,
        )
        db.commit()
        assert waves[0].planned_recipients == 3


# ──────────────────────────────────────────────────────────────────
# 6) pick_due_waves
# ──────────────────────────────────────────────────────────────────


class TestDueWavePicking:
    """The scheduler relies on a simple SELECT — make sure its
    semantics are tight."""

    def _materialise_two_waves(self, db, tenant, *, when):
        """Helper: a 2-wave plan scheduled at ``when``."""
        camp = _seed_campaign(db, tenant.id, audience=4000)
        spec = ws.WavePlanSpec(
            strategy=ws.STRATEGY_BATCHED, audience_size=4000,
            batch_size=2000, delay_between_batches_sec=3600,
            total_waves=2, estimated_completion_at=None, rationale="",
            waves=[
                ws.WaveEntry(1, 2000, when),
                ws.WaveEntry(2, 2000, when + timedelta(hours=1)),
            ],
        )
        waves = ws.materialise_waves(db=db, campaign=camp, spec=spec)
        db.commit()
        return camp, waves

    def test_returns_only_due_pending(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        now = datetime.now(timezone.utc)

        _, waves = self._materialise_two_waves(db, tenant, when=now - timedelta(minutes=10))

        due = ws.pick_due_waves(db=db, now=now)
        # Wave 1 is due (now-10min), wave 2 is in the future (now+50min).
        assert len(due) == 1
        assert due[0].id == waves[0].id

    def test_excludes_non_pending(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        now = datetime.now(timezone.utc)
        _, waves = self._materialise_two_waves(
            db, tenant, when=now - timedelta(minutes=10),
        )
        waves[0].status = ws.WAVE_COMPLETED
        db.commit()

        due = ws.pick_due_waves(db=db, now=now)
        assert due == []

    def test_cross_tenant_visibility_is_global(self):
        # The scheduler is a SYSTEM job — it must see every tenant's
        # waves. Tenant isolation is at the API layer, not the
        # scheduler. This test pins that behaviour so a future
        # "let's add a tenant_id filter to pick_due_waves" PR is
        # caught immediately.
        db, _ = _make_db()
        t1 = _seed_tenant(db, name="A")
        t2 = _seed_tenant(db, name="B")
        now = datetime.now(timezone.utc)
        self._materialise_two_waves(db, t1, when=now - timedelta(minutes=10))
        self._materialise_two_waves(db, t2, when=now - timedelta(minutes=10))

        due = ws.pick_due_waves(db=db, now=now)
        # 1 due wave per tenant = 2 total.
        assert len(due) == 2
        assert {w.tenant_id for w in due} == {t1.id, t2.id}


# ──────────────────────────────────────────────────────────────────
# 7) mark_wave_dispatching + complete_wave
# ──────────────────────────────────────────────────────────────────


class TestWaveStateTransitions:
    def test_mark_dispatching_flips_status_and_started_at(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        camp = _seed_campaign(db, tenant.id, audience=1000)
        spec = ws.WavePlanSpec(
            strategy=ws.STRATEGY_BATCHED, audience_size=1000,
            batch_size=1000, delay_between_batches_sec=0,
            total_waves=1, estimated_completion_at=None, rationale="",
            waves=[ws.WaveEntry(1, 1000, datetime.now(timezone.utc))],
        )
        waves = ws.materialise_waves(db=db, campaign=camp, spec=spec)
        db.commit()
        ws.mark_wave_dispatching(db=db, wave=waves[0])
        db.commit()

        assert waves[0].status == ws.WAVE_DISPATCHING
        assert waves[0].started_at is not None

    def test_mark_dispatching_is_idempotent(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        camp = _seed_campaign(db, tenant.id, audience=1000)
        spec = ws.WavePlanSpec(
            strategy=ws.STRATEGY_BATCHED, audience_size=1000,
            batch_size=1000, delay_between_batches_sec=0,
            total_waves=1, estimated_completion_at=None, rationale="",
            waves=[ws.WaveEntry(1, 1000, datetime.now(timezone.utc))],
        )
        waves = ws.materialise_waves(db=db, campaign=camp, spec=spec)
        waves[0].status = ws.WAVE_COMPLETED  # already finished
        completed_at_before = waves[0].started_at
        db.commit()
        ws.mark_wave_dispatching(db=db, wave=waves[0])  # no-op
        assert waves[0].status == ws.WAVE_COMPLETED
        assert waves[0].started_at == completed_at_before

    def test_complete_wave_writes_counters(self):
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        camp = _seed_campaign(db, tenant.id, audience=1000)
        spec = ws.WavePlanSpec(
            strategy=ws.STRATEGY_BATCHED, audience_size=1000,
            batch_size=1000, delay_between_batches_sec=0,
            total_waves=1, estimated_completion_at=None, rationale="",
            waves=[ws.WaveEntry(1, 1000, datetime.now(timezone.utc))],
        )
        waves = ws.materialise_waves(db=db, campaign=camp, spec=spec)
        db.commit()
        ws.complete_wave(db=db, wave=waves[0], sent=982, failed=18, success=True)
        db.commit()

        assert waves[0].status == ws.WAVE_COMPLETED
        assert waves[0].sent_count == 982
        assert waves[0].failed_count == 18
        assert waves[0].completed_at is not None
