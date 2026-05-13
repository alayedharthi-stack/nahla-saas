"""
backend/services/quality_score.py
─────────────────────────────────
Nahla's internal Quality Score for a WhatsApp Business number.

Why this exists
───────────────
``WhatsAppConnection.meta_quality_rating`` is Meta's own label —
``GREEN`` / ``YELLOW`` / ``RED`` — and it only updates when Meta
decides to publish a new value. By that point a merchant's number
has often already drifted. We want a **leading** indicator: a
number we can recompute every 30 minutes from our own delivery
events table that tells the merchant "your quality is sliding"
*before* Meta itself takes action.

The score is a simple weighted blend of:

* ``delivery_rate``  — the headline "did the message land?" rate.
* ``read_rate``      — bonus: customers who read the message.
* ``failure_rate``   — overall failed/total. Heavy penalty.
* ``quality_risk_rate`` — share of failures classified as
                          ``quality_tier='quality_risk'`` (the
                          ones Meta penalises us for repeating).
* ``critical_rate``  — share of WABA-level critical events
                       (``policy_violation`` / ``template_paused``
                       / ``account_locked``). Single occurrence
                       is enough to drag the score down.
* ``suppress_rate``  — share of *recipients* in the window that
                       the Suppression Engine had to auto-block.
                       Strongest leading signal.

What this score does NOT use (architectural policy)
───────────────────────────────────────────────────
This is a deliberate platform-level decision, not a bug.

**Inactivity is NOT a quality input.**

A customer who hasn't replied / read / purchased in 60, 90, 180+
days but whose phone is still a valid, active WhatsApp number
is a perfectly legitimate target for marketing — in fact,
re-engagement / win-back campaigns are one of the highest-value
use cases the platform exists to serve. Penalising the merchant's
Quality Score because they are about to (or just did) send a
re-engagement campaign would invert the platform's incentives.

Concretely, we do NOT feed the following into the score, even
though we could compute them from existing data:

* Time since last inbound from the customer.
* Time since last successful read/click.
* Time since last order/purchase.
* "Audience freshness" or "engagement age".

These signals belong elsewhere:

1. **Engagement / conversion analytics** (Phase 3 dashboards) —
   "your reactivation campaign read-through is 12% vs. typical
   18%; consider a stronger offer" is a marketing insight, not a
   quality signal.
2. **Audience Intent Classification** (Phase 4) — we'll surface
   per-recipient labels (Active buyer / Warm / Cold-but-valid /
   Dormant high-value / Unreachable / Risky) so the merchant can
   pick the right audience for the right campaign. That label
   set explicitly separates "cold but reachable" from "unreachable
   or risky" — only the latter family informs quality.

The signals that DO inform the score are all evidence of
**deliverability / reputation damage at the Meta layer**:

* ``not_on_whatsapp`` / ``invalid_phone``  — bad phone, Meta penalises
  repeats.
* ``blocked_by_user``                       — explicit negative signal.
* ``permanent_failure``                     — terminal hard-bounce.
* ``rate_limited`` (sustained)              — Meta throttling us.
* ``spam_rate_limit`` / ``policy_violation``— Meta-side alarm bell.
* ``suppressed_in_window``                  — our own engine had to
                                              shield us from the audience.

Bottom line: **a cold-but-valid customer must never count
against the merchant's score**. A customer with a bad phone
must. The distinction is what makes Nahla a marketing platform
instead of just a sender.

We do NOT use Meta-provided fields (``meta_quality_rating``,
``meta_messaging_limit``) as inputs — they're persisted on the
snapshot row alongside the score so the dashboard can plot them
together, but they are NOT used to compute ``nahla_quality_score``.
This separation lets a merchant see "Meta still says GREEN but
Nahla is warning" — the whole point of having an early-warning
score.

Tier discretisation
───────────────────
Independent layer on top of the raw 0–100. Thresholds are
calibrated against the most-common WhatsApp Business scoring
buckets so the merchant doesn't have to interpret a number:

  excellent ≥ 90
  healthy   ≥ 75
  warning   ≥ 60
  risky     ≥ 40
  critical   < 40

Sample-size guard
─────────────────
A number that sent 0 messages in the window has no signal — we
return ``None`` for the score and the tier defaults to
``"healthy"`` rather than misclassifying as ``critical``. The
sample-size threshold lives in ``MIN_SAMPLE_FOR_SCORE``.

Tests
─────
See ``tests/test_quality_score.py`` for the locked-down threshold
matrix and the metric → score → tier round-trip.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.quality_score")


# ──────────────────────────────────────────────────────────────────────
# Tunables
# ──────────────────────────────────────────────────────────────────────
#
# Module-level constants (NOT env vars) so production tuning happens
# via a code change with review. Unit tests can monkey-patch these
# but production calls go through the canonical values.

# Default window: a calendar week. The dashboard exposes a toggle
# between 7d and 30d; both write distinct snapshot rows so the
# trend chart can layer them.
DEFAULT_WINDOW_HOURS: int = 168     # 7 days
ALT_WINDOW_HOURS: int = 720         # 30 days

# Below this many delivery events in the window we refuse to score
# — the noise floor would dominate any signal.
MIN_SAMPLE_FOR_SCORE: int = 20

# Tier discretisation thresholds. Order matters: highest tier first.
# Stored as a list of (label, lower_bound) tuples so the dashboard
# can render the tier band by label without hard-coding numbers.
TIER_THRESHOLDS: List[tuple] = [
    ("excellent", 90.0),
    ("healthy",   75.0),
    ("warning",   60.0),
    ("risky",     40.0),
    ("critical",   0.0),
]

# Score weights.
#
# Shape: ``score = (delivery + read_bonus - penalties) * 100``,
# clipped to [0, 100]. ``delivery`` is the headline base — a
# tenant who delivers 100% with zero penalties hits 100 before
# any read bonus. ``read_bonus`` is purely additive (max +10),
# so a tenant who delivers reliably without any read signal
# stays above 90.
#
# All ``*_rate`` metrics consumed here are **% of total events
# in the window** (NOT % of failures) so the weights stay
# interpretable: ``W_QUALITY_RISK=1.5`` means "5% bad-phone
# events drops the score by 7.5 points".
#
# Calibration notes (May 2026):
#   * Perfect:        score = 100, tier = excellent
#   * 95% deliv / 20% read / 5% fail (all qrisk) / 0.5% supp
#                     score ≈ 87, tier = healthy
#   * 90% deliv / 0% read / 10% fail (all qrisk) / 1% supp
#                     score ≈ 70, tier = warning
#   * 1 critical event in 200 sends, otherwise clean
#                     score ≈ 89, tier = healthy (drops out of excellent)
#   * 10% deliv / 90% fail (all qrisk)
#                     score = 0, tier = critical
W_READ_BONUS:      float = 0.10   # max +10 absolute
W_FAILURE_PENALTY: float = 0.50
W_QUALITY_RISK:    float = 1.50
W_CRITICAL:        float = 12.00  # single events are rare → must hurt
W_SUPPRESS:        float = 0.50


# ──────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QualityMetrics:
    """Raw counts + derived rates over a window.

    Frozen so callers cannot accidentally mutate computed values
    between scoring and persisting. ``raw`` carries the
    numerator/denominator pairs the dashboard wants to render
    alongside each rate (e.g. "84 / 3,210 failed").
    """

    sample_size: int
    window_hours: int

    # Rates — None when sample_size is below threshold and we
    # decline to score.
    delivery_rate:     Optional[float] = None
    read_rate:         Optional[float] = None
    failure_rate:      Optional[float] = None
    quality_risk_rate: Optional[float] = None
    critical_rate:     Optional[float] = None
    suppress_rate:     Optional[float] = None
    complaint_rate:    Optional[float] = None

    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QualityScore:
    """Final score + tier + the metrics it was derived from.

    Returned by ``compute_quality_score`` and persisted onto
    ``WaNumberQualitySnapshot``.
    """

    score:        Optional[float]   # 0–100, None when insufficient sample
    tier:         str               # one of TIER_THRESHOLDS labels
    metrics:      QualityMetrics


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


def compute_quality_metrics(
    *,
    db: Session,
    tenant_id: int,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: Optional[datetime] = None,
) -> QualityMetrics:
    """Aggregate ``message_delivery_events`` + ``customer_suppressions``
    for the tenant over the given window and return a
    ``QualityMetrics`` snapshot.

    Single, paginated-free query (SQLAlchemy ``func.count`` with
    ``CASE WHEN`` rollups) so this scales to tenants with hundreds
    of thousands of events without loading rows into memory.
    """
    try:
        from models import (  # local import to avoid cycle at module load
            MessageDeliveryEvent,
            CustomerSuppression,
        )
    except Exception as exc:
        logger.warning("[quality_score] models unavailable: %s", exc)
        return QualityMetrics(sample_size=0, window_hours=window_hours)

    moment = now or datetime.now(timezone.utc)
    since = moment - timedelta(hours=window_hours)

    # ── Roll-up counters over the delivery events table ────────────
    # The CASE WHEN buckets are exclusive — a row contributes to
    # AT MOST one of {delivered, read, failed, other}. ``read``
    # implies ``delivered`` upstream, so we count both columns and
    # the dashboard can decide whether to add them.
    rollup = (
        db.query(
            func.count(MessageDeliveryEvent.id).label("total"),
            func.sum(
                case(
                    (MessageDeliveryEvent.status == "delivered", 1),
                    else_=0,
                )
            ).label("delivered"),
            func.sum(
                case(
                    (MessageDeliveryEvent.status == "read", 1),
                    else_=0,
                )
            ).label("read"),
            func.sum(
                case(
                    (MessageDeliveryEvent.status == "failed", 1),
                    else_=0,
                )
            ).label("failed"),
            func.sum(
                case(
                    (MessageDeliveryEvent.quality_tier == "quality_risk", 1),
                    else_=0,
                )
            ).label("quality_risk"),
            func.sum(
                case(
                    (MessageDeliveryEvent.quality_tier == "critical", 1),
                    else_=0,
                )
            ).label("critical"),
        )
        .filter(
            MessageDeliveryEvent.tenant_id == tenant_id,
            MessageDeliveryEvent.occurred_at >= since,
        )
        .one()
    )

    total      = int(rollup.total or 0)
    delivered  = int(rollup.delivered or 0)
    reads      = int(rollup.read or 0)
    failed     = int(rollup.failed or 0)
    risky_n    = int(rollup.quality_risk or 0)
    critical_n = int(rollup.critical or 0)

    # ── Suppress rate = active suppressions created in window /
    #     distinct phones we messaged in window ──
    suppressed_in_window = (
        db.query(func.count(CustomerSuppression.id))
        .filter(
            CustomerSuppression.tenant_id == tenant_id,
            CustomerSuppression.suppressed_at >= since,
            CustomerSuppression.source == "auto",
        )
        .scalar()
        or 0
    )
    distinct_phones = (
        db.query(func.count(func.distinct(MessageDeliveryEvent.phone_e164)))
        .filter(
            MessageDeliveryEvent.tenant_id == tenant_id,
            MessageDeliveryEvent.occurred_at >= since,
            MessageDeliveryEvent.phone_e164.isnot(None),
        )
        .scalar()
        or 0
    )

    if total < MIN_SAMPLE_FOR_SCORE:
        # Sample too small — return raw counts but no rates. The
        # caller decides whether to record a "no score yet"
        # snapshot or skip entirely.
        return QualityMetrics(
            sample_size=total,
            window_hours=window_hours,
            raw={
                "total":      total,
                "delivered":  delivered,
                "read":       reads,
                "failed":     failed,
                "quality_risk": risky_n,
                "critical":   critical_n,
                "suppressed_in_window": int(suppressed_in_window),
                "distinct_phones":      int(distinct_phones),
                "since":      since.isoformat(),
            },
        )

    # ``delivered`` count is "first delivered" only — but Meta also
    # emits a separate "read" event for the same wamid. We want
    # delivery rate to include reads (read → delivered logically).
    effective_delivered = delivered + reads

    delivery_rate     = _safe_div(effective_delivered, total)
    read_rate         = _safe_div(reads, total)
    failure_rate      = _safe_div(failed, total)
    # ``quality_risk_rate`` and ``critical_rate`` are expressed as
    # % of TOTAL events in the window — not % of failures. This
    # keeps the scoring weights intuitive (a quality_risk rate of
    # 0.05 means "5% of all sends were bad-phone failures").
    quality_risk_rate = _safe_div(risky_n, total)
    critical_rate     = _safe_div(critical_n, total)
    suppress_rate     = _safe_div(
        suppressed_in_window, max(int(distinct_phones), 1)
    )

    return QualityMetrics(
        sample_size=total,
        window_hours=window_hours,
        delivery_rate=delivery_rate,
        read_rate=read_rate,
        failure_rate=failure_rate,
        quality_risk_rate=quality_risk_rate,
        critical_rate=critical_rate,
        suppress_rate=suppress_rate,
        complaint_rate=None,    # reserved for future complaint signal
        raw={
            "total":      total,
            "delivered":  delivered,
            "read":       reads,
            "failed":     failed,
            "quality_risk": risky_n,
            "critical":   critical_n,
            "suppressed_in_window": int(suppressed_in_window),
            "distinct_phones":      int(distinct_phones),
            "since":      since.isoformat(),
        },
    )


def compute_quality_score(metrics: QualityMetrics) -> QualityScore:
    """Score the metrics and discretise into a tier.

    Returns a ``QualityScore`` with ``score=None`` when the window
    is below the sample threshold — the dashboard renders that
    as "اللا توجد بيانات كافية للتقييم بعد" rather than a
    misleading number.
    """
    if metrics.sample_size < MIN_SAMPLE_FOR_SCORE:
        # No score, but we still surface a *tier* — and we pick
        # ``healthy`` (not ``critical``) so a newly-onboarded
        # tenant isn't shown a red banner before they've sent
        # their first 20 messages.
        return QualityScore(score=None, tier="healthy", metrics=metrics)

    # Base: delivery is the headline. Read is a small additive
    # bonus so a tenant who never reads (e.g. transactional
    # notifications) still scores well as long as delivery is good.
    base = (metrics.delivery_rate or 0.0)
    read_bonus = W_READ_BONUS * (metrics.read_rate or 0.0)

    penalty = (
        W_FAILURE_PENALTY * (metrics.failure_rate or 0.0)
        + W_QUALITY_RISK   * (metrics.quality_risk_rate or 0.0)
        + W_CRITICAL       * (metrics.critical_rate or 0.0)
        + W_SUPPRESS       * (metrics.suppress_rate or 0.0)
    )

    raw_score = (base + read_bonus - penalty) * 100.0
    score = max(0.0, min(100.0, raw_score))
    return QualityScore(score=score, tier=tier_of(score), metrics=metrics)


def tier_of(score: Optional[float]) -> str:
    """Discretise a numeric score to a tier label.

    ``None`` maps to ``"healthy"`` (see ``compute_quality_score``).
    """
    if score is None:
        return "healthy"
    for label, lower in TIER_THRESHOLDS:
        if score >= lower:
            return label
    return TIER_THRESHOLDS[-1][0]


def take_snapshot(
    *,
    db: Session,
    tenant_id: int,
    connection_id: int,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    triggered_by: Optional[str] = None,
    meta_quality_rating: Optional[str] = None,
    meta_messaging_limit: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[int]:
    """Compute metrics + score, persist as a ``WaNumberQualitySnapshot``
    row, return its id.

    Returns ``None`` on failure — never raises so the scheduler
    loop can swallow per-tenant errors and keep going.
    """
    try:
        from models import WaNumberQualitySnapshot
    except Exception as exc:
        logger.warning("[quality_score] snapshot model unavailable: %s", exc)
        return None

    metrics = compute_quality_metrics(
        db=db, tenant_id=tenant_id, window_hours=window_hours, now=now,
    )
    scored = compute_quality_score(metrics)

    row = WaNumberQualitySnapshot(
        tenant_id=tenant_id,
        connection_id=connection_id,
        taken_at=now or datetime.now(timezone.utc),
        meta_quality_rating=meta_quality_rating,
        meta_messaging_limit=meta_messaging_limit,
        nahla_quality_score=scored.score,
        nahla_quality_tier=scored.tier,
        metrics_window_hours=window_hours,
        delivery_rate=metrics.delivery_rate,
        read_rate=metrics.read_rate,
        failure_rate=metrics.failure_rate,
        suppress_rate=metrics.suppress_rate,
        complaint_rate=metrics.complaint_rate,
        sample_size=metrics.sample_size,
        raw_metrics=metrics.raw,
        triggered_by=triggered_by,
    )
    db.add(row)
    try:
        with db.begin_nested():
            db.flush()
        logger.info(
            "[quality_score] snapshot tenant=%s conn=%s score=%s tier=%s",
            tenant_id, connection_id,
            (f"{scored.score:.1f}" if scored.score is not None else "n/a"),
            scored.tier,
        )
        return row.id
    except Exception as exc:
        logger.warning("[quality_score] snapshot flush failed: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _safe_div(numerator: float, denominator: float) -> Optional[float]:
    """Division that returns ``None`` on a zero denominator.

    Used so callers can distinguish "no data" from "0%": a tenant
    with zero failures returns ``failure_rate=0.0``, but a tenant
    with zero messages overall returns ``None`` for every rate.
    """
    if not denominator:
        return None
    try:
        return float(numerator) / float(denominator)
    except (TypeError, ZeroDivisionError):
        return None


__all__ = [
    "DEFAULT_WINDOW_HOURS",
    "ALT_WINDOW_HOURS",
    "MIN_SAMPLE_FOR_SCORE",
    "TIER_THRESHOLDS",
    "QualityMetrics",
    "QualityScore",
    "compute_quality_metrics",
    "compute_quality_score",
    "tier_of",
    "take_snapshot",
]
