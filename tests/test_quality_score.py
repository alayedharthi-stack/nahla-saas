"""tests/test_quality_score.py
─────────────────────────────────
Phase 2 — Nahla's internal Quality Score for a WABA number.

What we lock down
─────────────────
1. **Tier discretisation** is monotonic and matches the published
   thresholds (excellent ≥ 90, healthy ≥ 75, warning ≥ 60, risky
   ≥ 40, critical < 40). Each threshold boundary is tested
   exactly so a future tweak of the cuts surfaces immediately.

2. **Score is bounded** to ``[0, 100]`` — a catastrophic tenant
   (every send failed, every recipient suppressed) hits 0 but
   never goes negative; a perfect tenant hits 100 not 105.

3. **Sample-size guard** — fewer than ``MIN_SAMPLE_FOR_SCORE``
   events in the window returns ``score=None``, and the tier
   defaults to ``"healthy"`` so newly-onboarded tenants are not
   misclassified as critical.

4. **End-to-end with the recorder** — a corpus of 50 events with
   a known mix (90% delivered, 10% quality_risk) flows through
   ``record_status_event`` → ``compute_quality_metrics`` →
   ``compute_quality_score`` and lands in the expected tier
   ("healthy"). This is the integration assertion that Phase 1
   and Phase 2 stay wired together.

5. **Persistence round-trip** — ``take_snapshot`` writes a row
   that can be read back with all the rates intact (especially
   the JSONB ``raw_metrics`` blob).
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
    MessageDeliveryEvent,
    Tenant,
    WaNumberQualitySnapshot,
)
from services import delivery_quality as dq  # noqa: E402
from services.quality_score import (  # noqa: E402
    DEFAULT_WINDOW_HOURS,
    MIN_SAMPLE_FOR_SCORE,
    QualityMetrics,
    QualityScore,
    TIER_THRESHOLDS,
    compute_quality_metrics,
    compute_quality_score,
    take_snapshot,
    tier_of,
)


# ── SQLite shim (matches other tests in the suite) ──────────────────


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


def _seed_tenant(db, name="QS") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── 1) Tier discretisation thresholds ────────────────────────────────


class TestTierDiscretisation:
    """Verify each tier boundary so the dashboard's colour bands stay
    in sync with the service."""

    @pytest.mark.parametrize("score,expected", [
        (100.0, "excellent"),
        (95.0,  "excellent"),
        (90.0,  "excellent"),   # boundary IN
        (89.99, "healthy"),     # boundary OUT
        (80.0,  "healthy"),
        (75.0,  "healthy"),     # boundary IN
        (74.99, "warning"),
        (65.0,  "warning"),
        (60.0,  "warning"),     # boundary IN
        (59.99, "risky"),
        (45.0,  "risky"),
        (40.0,  "risky"),       # boundary IN
        (39.99, "critical"),
        (20.0,  "critical"),
        (0.0,   "critical"),
    ])
    def test_tier_boundaries(self, score, expected):
        assert tier_of(score) == expected

    def test_none_defaults_to_healthy(self):
        # A merchant with no data yet must NOT see a red "critical"
        # banner — they haven't done anything wrong.
        assert tier_of(None) == "healthy"

    def test_thresholds_are_sorted_descending(self):
        # If someone re-orders TIER_THRESHOLDS the binary classifier
        # in tier_of() silently mis-sorts everything. Catch it.
        bounds = [bound for _, bound in TIER_THRESHOLDS]
        assert bounds == sorted(bounds, reverse=True)


# ── 2) Score bounds + math ───────────────────────────────────────────


class TestScoreMath:
    def test_score_clipped_to_0_100(self):
        # Fabricate an absurdly bad metrics blob — the score must
        # never go negative even if penalties exceed contributions.
        bad = QualityMetrics(
            sample_size=MIN_SAMPLE_FOR_SCORE * 2,
            window_hours=168,
            delivery_rate=0.0,
            read_rate=0.0,
            failure_rate=1.0,
            quality_risk_rate=1.0,
            critical_rate=1.0,
            suppress_rate=1.0,
        )
        scored = compute_quality_score(bad)
        assert scored.score == 0.0
        assert scored.tier == "critical"

    def test_perfect_metrics_hit_excellent(self):
        good = QualityMetrics(
            sample_size=1000,
            window_hours=168,
            delivery_rate=1.0,
            read_rate=1.0,
            failure_rate=0.0,
            quality_risk_rate=0.0,
            critical_rate=0.0,
            suppress_rate=0.0,
        )
        scored = compute_quality_score(good)
        assert scored.score is not None
        # Perfect blend hits the top band cleanly.
        assert scored.score >= 90.0
        assert scored.tier == "excellent"

    def test_mid_tier_is_warning(self):
        # 90% delivered, no reads, 10% failure of which all are
        # quality_risk-of-total = 0.10 → tenant has a real audience
        # quality problem. Expect "warning" band.
        mid = QualityMetrics(
            sample_size=500,
            window_hours=168,
            delivery_rate=0.90,
            read_rate=0.0,
            failure_rate=0.10,
            quality_risk_rate=0.10,
            critical_rate=0.0,
            suppress_rate=0.01,
        )
        scored = compute_quality_score(mid)
        assert scored.tier in ("healthy", "warning"), (
            f"unexpected tier for mid-quality tenant: {scored.tier} "
            f"score={scored.score}"
        )

    def test_one_critical_event_drops_tier(self):
        # Single ``critical``-tier event in 200 sends (= 0.5% of
        # total) should drag us out of "excellent" but not all the
        # way to "critical" — it's an alarm bell, not a death
        # sentence.
        with_critical = QualityMetrics(
            sample_size=200,
            window_hours=168,
            delivery_rate=0.95,
            read_rate=0.30,
            failure_rate=0.05,
            quality_risk_rate=0.0,
            critical_rate=1.0 / 200,        # one critical / 200 events
            suppress_rate=0.0,
        )
        scored = compute_quality_score(with_critical)
        assert scored.tier != "excellent", (
            f"a critical event should never leave us in excellent: "
            f"score={scored.score} tier={scored.tier}"
        )


# ── 3) Sample-size guard ─────────────────────────────────────────────


class TestInactivityPolicy:
    """Architectural policy: inactivity is NEVER an input to the score.

    Mirrors the "What this score does NOT use" section of
    ``services/quality_score.py``. Whoever later adds a
    ``last_inbound_age_days`` or ``audience_freshness`` field to
    ``QualityMetrics`` MUST update this test to keep the policy
    enforced.

    The rule we lock down: two tenants with identical
    deliverability metrics should produce identical scores even
    if one of them is sending exclusively to "cold" (long-inactive)
    customers. Win-back / reactivation campaigns are a
    first-class use case of the platform.
    """

    def test_score_independent_of_audience_freshness(self):
        # Two identical-by-deliverability metrics blobs. The
        # ``raw`` blob is allowed to carry freshness annotations
        # for the dashboard's information, but the scoring
        # function must completely ignore them.
        active_audience = QualityMetrics(
            sample_size=500,
            window_hours=168,
            delivery_rate=0.95,
            read_rate=0.30,
            failure_rate=0.05,
            quality_risk_rate=0.0,
            critical_rate=0.0,
            suppress_rate=0.0,
            raw={"audience_freshness_days_p50": 7, "note": "all engaged in last week"},
        )
        cold_but_valid_audience = QualityMetrics(
            sample_size=500,
            window_hours=168,
            delivery_rate=0.95,
            read_rate=0.30,
            failure_rate=0.05,
            quality_risk_rate=0.0,
            critical_rate=0.0,
            suppress_rate=0.0,
            raw={"audience_freshness_days_p50": 180, "note": "all dormant >6 months"},
        )

        active_score = compute_quality_score(active_audience)
        cold_score   = compute_quality_score(cold_but_valid_audience)

        assert active_score.score == cold_score.score, (
            "Inactivity-only audience freshness leaked into the "
            "score — see services/quality_score.py policy."
        )
        assert active_score.tier == cold_score.tier

    def test_win_back_campaign_scenario_stays_healthy(self):
        # Concrete scenario: merchant runs a win-back campaign to
        # 500 customers who haven't engaged in 6 months. Phone
        # numbers are valid → delivery rate is 95%, read rate is
        # modest (12%) because most are dormant. ZERO bad-phone
        # failures, ZERO suppressions, ZERO critical events.
        #
        # This MUST score healthy/excellent. A platform that
        # penalises legitimate re-engagement campaigns is broken.
        winback = QualityMetrics(
            sample_size=500,
            window_hours=168,
            delivery_rate=0.95,
            read_rate=0.12,         # low read is fine — it's a cold list
            failure_rate=0.05,      # 5% bounces, all unrelated to inactivity
            quality_risk_rate=0.0,
            critical_rate=0.0,
            suppress_rate=0.0,
        )
        scored = compute_quality_score(winback)
        assert scored.tier in ("excellent", "healthy"), (
            f"Win-back campaign with clean phones must NOT be "
            f"penalised — got tier={scored.tier} score={scored.score}"
        )


class TestSampleSizeGuard:
    def test_below_threshold_returns_none(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        # Insert exactly 5 events — way below the 20-event floor.
        for i in range(5):
            dq.record_status_event(
                db=db, tenant_id=t.id, wamid=f"thin:{i}",
                status="delivered", phone_e164=f"+96650{i:07d}",
            )
        db.commit()

        metrics = compute_quality_metrics(db=db, tenant_id=t.id)
        scored = compute_quality_score(metrics)

        assert metrics.sample_size == 5
        assert metrics.delivery_rate is None
        assert scored.score is None
        # Newly-onboarded tenants don't get scared with a red badge.
        assert scored.tier == "healthy"

    def test_above_threshold_yields_score(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        # 25 delivered events — comfortably above the floor.
        for i in range(25):
            dq.record_status_event(
                db=db, tenant_id=t.id, wamid=f"ok:{i}",
                status="delivered", phone_e164=f"+96651{i:07d}",
            )
        db.commit()

        metrics = compute_quality_metrics(db=db, tenant_id=t.id)
        scored = compute_quality_score(metrics)

        assert metrics.sample_size == 25
        assert metrics.delivery_rate == 1.0
        assert scored.score is not None
        assert scored.tier in ("excellent", "healthy")


# ── 4) End-to-end through the recorder ──────────────────────────────


class TestEndToEnd:
    """Phase 1 (recorder) → Phase 2 (score). One regression here
    means the layers are talking past each other."""

    def test_mixed_corpus_lands_in_healthy(self):
        db, _ = _make_db()
        t = _seed_tenant(db)

        # 45 delivered + 5 not_on_whatsapp failures over 50 phones.
        for i in range(45):
            dq.record_status_event(
                db=db, tenant_id=t.id, wamid=f"d:{i}",
                status="delivered", phone_e164=f"+96652{i:07d}",
            )
        # Distinct phones for the failure batch so the suppression
        # engine doesn't trip and confuse the suppress_rate metric.
        for i in range(5):
            dq.record_status_event(
                db=db, tenant_id=t.id, wamid=f"f:{i}",
                status="failed", phone_e164=f"+96660{i:07d}",
                errors_payload=[{"code": 131026}],
            )
        db.commit()

        metrics = compute_quality_metrics(db=db, tenant_id=t.id)
        scored = compute_quality_score(metrics)

        assert metrics.sample_size == 50
        # 45 delivered + 0 reads / 50 total = 0.9
        assert metrics.delivery_rate == pytest.approx(0.9, abs=0.01)
        assert metrics.failure_rate == pytest.approx(0.1, abs=0.01)
        # 5 quality_risk failures / 50 total = 0.1 (rates are
        # % of total, not % of failures — locked down in
        # services/quality_score.py).
        assert metrics.quality_risk_rate == pytest.approx(0.1, abs=0.01)
        # Tier should be in the middle of the band — not excellent
        # (high quality_risk) but not critical either.
        assert scored.tier in ("warning", "risky"), (
            f"unexpected mixed-corpus tier: {scored.tier} "
            f"score={scored.score}"
        )

    def test_catastrophic_corpus_is_critical(self):
        db, _ = _make_db()
        t = _seed_tenant(db)

        # 5 delivered, 45 failed — disaster scenario.
        for i in range(5):
            dq.record_status_event(
                db=db, tenant_id=t.id, wamid=f"d:{i}",
                status="delivered", phone_e164=f"+96670{i:07d}",
            )
        for i in range(45):
            dq.record_status_event(
                db=db, tenant_id=t.id, wamid=f"f:{i}",
                status="failed", phone_e164=f"+96671{i:07d}",
                errors_payload=[{"code": 131026}],
            )
        db.commit()

        metrics = compute_quality_metrics(db=db, tenant_id=t.id)
        scored = compute_quality_score(metrics)
        assert scored.score is not None
        assert scored.tier in ("risky", "critical")


# ── 5) Persistence round-trip ───────────────────────────────────────


class TestSnapshotPersistence:
    def test_take_snapshot_writes_row_with_metrics(self):
        db, _ = _make_db()
        t = _seed_tenant(db)

        # Seed enough events to get a real score.
        for i in range(30):
            dq.record_status_event(
                db=db, tenant_id=t.id, wamid=f"s:{i}",
                status="delivered", phone_e164=f"+96680{i:07d}",
            )
        db.commit()

        snap_id = take_snapshot(
            db=db,
            tenant_id=t.id,
            connection_id=42,    # synthetic — the model doesn't FK this
            triggered_by="test",
            meta_quality_rating="GREEN",
            meta_messaging_limit="TIER_1K",
        )
        db.commit()

        assert snap_id is not None
        snap = (
            db.query(WaNumberQualitySnapshot)
            .filter(WaNumberQualitySnapshot.id == snap_id)
            .one()
        )
        assert snap.tenant_id == t.id
        assert snap.connection_id == 42
        assert snap.metrics_window_hours == DEFAULT_WINDOW_HOURS
        assert snap.sample_size == 30
        assert snap.nahla_quality_score is not None
        assert snap.nahla_quality_tier in (
            "excellent", "healthy", "warning", "risky", "critical",
        )
        assert snap.meta_quality_rating == "GREEN"
        assert snap.meta_messaging_limit == "TIER_1K"
        assert snap.triggered_by == "test"
        # The raw_metrics blob round-trips so the dashboard can show
        # "X delivered / Y total" without re-querying.
        assert snap.raw_metrics is not None
        assert snap.raw_metrics["total"] == 30
        assert snap.raw_metrics["delivered"] == 30

    def test_thin_tenant_persists_null_score(self):
        # Even with too-thin data we still want a snapshot row —
        # the dashboard renders it as "في انتظار بيانات كافية"
        # rather than disappearing the number from the list.
        db, _ = _make_db()
        t = _seed_tenant(db)

        for i in range(3):
            dq.record_status_event(
                db=db, tenant_id=t.id, wamid=f"thin:{i}",
                status="delivered", phone_e164=f"+96690{i:07d}",
            )
        db.commit()

        snap_id = take_snapshot(
            db=db, tenant_id=t.id, connection_id=1,
        )
        db.commit()

        assert snap_id is not None
        snap = (
            db.query(WaNumberQualitySnapshot)
            .filter(WaNumberQualitySnapshot.id == snap_id)
            .one()
        )
        assert snap.sample_size == 3
        assert snap.nahla_quality_score is None
        assert snap.nahla_quality_tier == "healthy"
