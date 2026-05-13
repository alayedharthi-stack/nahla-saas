"""
backend/services/wave_scheduler.py
──────────────────────────────────
Wave / Batch sending architecture for marketing campaigns.

What this is
────────────
A persistence + planning layer that sits *on top of* the existing
``services/campaign_dispatcher.dispatch_campaign`` pipeline. It
does NOT rewrite the dispatcher. The dispatcher continues to
own:

* recipient resolution (``_resolve_audience``),
* snapshot inserts (``_snapshot_recipients``),
* frequency-cap application,
* the actual Meta API send loop.

This module owns the new questions:

* What is the merchant's chosen ``send_strategy`` for this
  campaign? (``immediate`` / ``batched`` / ``adaptive``.)
* If the strategy is non-immediate, how should the audience be
  split into waves? (How many waves, how big, how spaced.)
* Which wave is due to run right now?
* When the dispatcher works on a wave, which send_log rows
  belong to it?

Why Meta-aware pacing matters
─────────────────────────────
Meta's WhatsApp Business guidance for marketing broadcasts is
explicit: a number's reputation is partly a function of how it
sends, not just to whom. Sustained bursts to low-engagement
audiences trigger ``quality-pacing`` errors; one such error
during a campaign downgrades the WABA tier (1k → 250 → …) and
locks template messaging until the score recovers. The legacy
in-process pacing (``INTER_MESSAGE_DELAY=1.5s``) is fine for
small campaigns but does nothing to spread a 10,000-recipient
broadcast over enough wall-clock time to keep Meta happy.

Wave architecture solves this by spreading the same broadcast
across N hours / days, with each wave being its own audit unit:
if Meta downgrades us during wave 3, the merchant can pause
waves 4-8 from a UI and Nahla can re-recommend a tighter plan
for the remaining recipients.

Module API
──────────
Pure-logic entry points (no DB writes):

* :func:`suggest_batch_size_for_tier`
* :func:`compute_adaptive_strategy`
* :func:`plan_waves`

Persistence helpers (DB writes guarded by ``begin_nested``):

* :func:`materialise_waves`
* :func:`assign_send_logs_to_waves`
* :func:`pick_due_waves`
* :func:`mark_wave_dispatching`
* :func:`complete_wave`

Small-campaign carve-out
────────────────────────
Campaigns under :data:`WAVE_THRESHOLD_RECIPIENTS` (default 500)
**deliberately** stay on the immediate path even if the merchant
clicks "batched" — the operational complexity of a multi-wave
plan is not justified for a coffee shop sending to 80 customers.
The planner returns ``strategy='immediate'`` in that case and
the launch endpoint never creates a ``CampaignWave`` row.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.wave_scheduler")


# ──────────────────────────────────────────────────────────────────
# Public constants — tuning knobs
# ──────────────────────────────────────────────────────────────────
#
# Anything under this size stays on the legacy immediate path even
# if the merchant explicitly asks for waves. Reasoning: at <500
# recipients the dispatcher's existing 1.5s inter-message delay
# already spreads the send over ~12 minutes — that's enough
# pacing for Meta to be happy with a healthy WABA, and adding a
# scheduler + UI for "wave 1 of 1" is pure ceremony.
WAVE_THRESHOLD_RECIPIENTS: int = 500

# Strategy identifiers. Stored as plain strings on Campaign so
# we never need an Enum migration when we add a new strategy.
STRATEGY_IMMEDIATE: str = "immediate"
STRATEGY_BATCHED:   str = "batched"
STRATEGY_ADAPTIVE:  str = "adaptive"
VALID_STRATEGIES = {STRATEGY_IMMEDIATE, STRATEGY_BATCHED, STRATEGY_ADAPTIVE}

# Wave status. Mirrors the model's docstring.
WAVE_PENDING:      str = "pending"
WAVE_DISPATCHING:  str = "dispatching"
WAVE_COMPLETED:    str = "completed"
WAVE_FAILED:       str = "failed"
WAVE_PAUSED:       str = "paused"
WAVE_CANCELLED:    str = "cancelled"

# Tier → suggested batch size mapping. Calibrated against Meta's
# published throughput tiers (1k/10k/100k/unlimited) so a healthy
# tenant on the 10k tier can still finish a 5k-recipient broadcast
# in ~1h while a critical-tier tenant sends in 100-recipient sips.
#
# Tweaking note: do NOT raise these without a corresponding raise
# in ``delay_between_batches_sec`` — the goal is total wall-clock
# spread, not pure throughput.
_TIER_BATCH_SIZE: Dict[str, int] = {
    "excellent": 5000,
    "healthy":   2000,
    "warning":    500,
    "risky":      100,
    "critical":    50,
}
_TIER_BATCH_DELAY_SEC: Dict[str, int] = {
    # Tier        delay between waves (seconds)
    "excellent":  30 * 60,        # 30 min — minimal pacing
    "healthy":    60 * 60,        # 1 hour
    "warning":   120 * 60,        # 2 hours
    "risky":     360 * 60,        # 6 hours
    "critical":  720 * 60,        # 12 hours
}

# Default fallback when no quality snapshot exists yet (brand-new
# tenant). Conservative: we pretend they're on the ``healthy``
# tier rather than ``excellent`` so a green tenant doesn't get
# accidentally hammered before we have data.
_DEFAULT_TIER_FOR_NEW_TENANT: str = "healthy"


# ──────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WavePlanSpec:
    """A concrete, audience-aware wave plan.

    Returned by :func:`plan_waves` / :func:`compute_adaptive_strategy`.
    The launch endpoint persists this into ``Campaign.send_strategy``
    + ``batch_size`` + ``delay_between_batches_sec`` and uses the
    ``waves`` list to materialise ``CampaignWave`` rows.
    """
    strategy: str
    audience_size: int
    batch_size: int
    delay_between_batches_sec: int
    total_waves: int
    estimated_completion_at: Optional[datetime]
    rationale: str
    # One entry per wave. Each carries the ``planned_recipients``
    # and ``scheduled_at`` the materialiser will write.
    waves: List["WaveEntry"] = field(default_factory=list)


@dataclass(frozen=True)
class WaveEntry:
    wave_index: int        # 1-based for display
    planned_recipients: int
    scheduled_at: datetime


# ──────────────────────────────────────────────────────────────────
# Pure-logic API (no DB)
# ──────────────────────────────────────────────────────────────────


def suggest_batch_size_for_tier(tier: Optional[str]) -> int:
    """Lookup the merchant-recommended batch size for a quality tier.

    Falls back to the ``healthy`` row when the tier is unknown
    (e.g. brand-new tenant, score not computed yet). Defensive
    rather than throwing — this function is called from preflight
    APIs and should never explode the response.
    """
    if not tier:
        return _TIER_BATCH_SIZE[_DEFAULT_TIER_FOR_NEW_TENANT]
    return _TIER_BATCH_SIZE.get(tier, _TIER_BATCH_SIZE[_DEFAULT_TIER_FOR_NEW_TENANT])


def suggest_delay_for_tier(tier: Optional[str]) -> int:
    """Like :func:`suggest_batch_size_for_tier` but for the
    inter-wave delay in seconds."""
    if not tier:
        return _TIER_BATCH_DELAY_SEC[_DEFAULT_TIER_FOR_NEW_TENANT]
    return _TIER_BATCH_DELAY_SEC.get(
        tier, _TIER_BATCH_DELAY_SEC[_DEFAULT_TIER_FOR_NEW_TENANT],
    )


def compute_adaptive_strategy(
    *,
    audience_size: int,
    quality_tier: Optional[str] = None,
    meta_quality_rating: Optional[str] = None,
    now: Optional[datetime] = None,
) -> WavePlanSpec:
    """Pick a strategy + concrete batch size + delay automatically.

    Decision flow
    ─────────────
    1. **Small campaigns stay immediate.** Below
       :data:`WAVE_THRESHOLD_RECIPIENTS` we never recommend waves —
       see the module docstring.

    2. **Meta tier downgrade dominates.** If Meta's own
       ``meta_quality_rating`` is ``RED`` we override the Nahla
       tier downward to ``risky`` (or ``critical`` if already
       ``risky``). The merchant-visible reasoning explains this.

    3. **Tier → batch_size + delay** via the calibrated table.

    4. **Total waves** = ``ceil(audience / batch_size)``.

    Returns a fully-materialised :class:`WavePlanSpec` with the
    waves' scheduled timestamps already computed. The caller can
    persist it as-is.
    """
    moment = now or datetime.now(timezone.utc)

    if audience_size <= 0:
        return WavePlanSpec(
            strategy=STRATEGY_IMMEDIATE,
            audience_size=0,
            batch_size=0,
            delay_between_batches_sec=0,
            total_waves=0,
            estimated_completion_at=moment,
            rationale="جمهور الحملة فارغ — لا توجد دفعات.",
            waves=[],
        )

    if audience_size < WAVE_THRESHOLD_RECIPIENTS:
        return WavePlanSpec(
            strategy=STRATEGY_IMMEDIATE,
            audience_size=audience_size,
            batch_size=audience_size,
            delay_between_batches_sec=0,
            total_waves=1,
            estimated_completion_at=moment + _immediate_runtime_estimate(audience_size),
            rationale=(
                f"الحملة صغيرة ({audience_size} مستلم) — يتم الإرسال "
                f"مباشرة بدون دفعات. الحد الأدنى لتقسيم الحملات هو "
                f"{WAVE_THRESHOLD_RECIPIENTS} مستلم."
            ),
            waves=[
                WaveEntry(
                    wave_index=1,
                    planned_recipients=audience_size,
                    scheduled_at=moment,
                ),
            ],
        )

    # Apply Meta-rating override BEFORE picking the tier.
    effective_tier = quality_tier or _DEFAULT_TIER_FOR_NEW_TENANT
    meta_override_applied = False
    if (meta_quality_rating or "").upper() == "RED":
        meta_override_applied = True
        # Drop one band lower than whatever Nahla says.
        effective_tier = {
            "excellent": "warning",
            "healthy":   "warning",
            "warning":   "risky",
            "risky":     "critical",
            "critical":  "critical",
        }.get(effective_tier, "warning")

    batch_size = suggest_batch_size_for_tier(effective_tier)
    delay_sec  = suggest_delay_for_tier(effective_tier)
    total_waves = max(1, math.ceil(audience_size / batch_size))

    plan = plan_waves(
        audience_size=audience_size,
        batch_size=batch_size,
        delay_between_batches_sec=delay_sec,
        now=moment,
    )

    rationale_parts: List[str] = []
    if quality_tier:
        rationale_parts.append(
            f"تقييم Nahla الحالي للجودة: {_tier_label_ar(quality_tier)}."
        )
    if meta_override_applied:
        rationale_parts.append(
            "تقييم Meta الحالي للرقم (RED) فعّل خطة أكثر تحفظاً."
        )
    rationale_parts.append(
        f"الجمهور ({audience_size:,} مستلم) مقسّم على "
        f"{total_waves} دفعة بحجم {batch_size:,}، "
        f"مع فاصل {_human_seconds(delay_sec)} بين كل دفعة."
    )

    return WavePlanSpec(
        strategy=STRATEGY_ADAPTIVE,
        audience_size=audience_size,
        batch_size=batch_size,
        delay_between_batches_sec=delay_sec,
        total_waves=total_waves,
        estimated_completion_at=plan[-1].scheduled_at if plan else moment,
        rationale=" ".join(rationale_parts),
        waves=plan,
    )


def plan_waves(
    *,
    audience_size: int,
    batch_size: int,
    delay_between_batches_sec: int,
    now: Optional[datetime] = None,
) -> List[WaveEntry]:
    """Pure plan: given a batch size + delay, return the wave entries.

    No DB writes. The last wave inherits whatever residue is left
    after the floor division (e.g. 5,500 ÷ 2,000 → [2000, 2000, 1500]).

    The function tolerates degenerate inputs gracefully:
        * ``audience_size <= 0``  → empty list
        * ``batch_size <= 0``     → one wave covering everyone
    """
    moment = now or datetime.now(timezone.utc)
    if audience_size <= 0:
        return []
    if batch_size <= 0:
        return [
            WaveEntry(
                wave_index=1,
                planned_recipients=audience_size,
                scheduled_at=moment,
            ),
        ]

    total_waves = max(1, math.ceil(audience_size / batch_size))
    waves: List[WaveEntry] = []
    remaining = audience_size
    for i in range(total_waves):
        size_for_wave = min(batch_size, remaining)
        waves.append(WaveEntry(
            wave_index=i + 1,
            planned_recipients=size_for_wave,
            scheduled_at=moment + timedelta(seconds=delay_between_batches_sec * i),
        ))
        remaining -= size_for_wave
    return waves


# ──────────────────────────────────────────────────────────────────
# Persistence helpers (DB)
# ──────────────────────────────────────────────────────────────────


def materialise_waves(
    *,
    db: Session,
    campaign,
    spec: WavePlanSpec,
) -> List[Any]:
    """Create ``CampaignWave`` rows for the given plan.

    Returns the list of newly-created waves (with ids assigned).
    No commit — the caller controls the transaction so this can
    join the same tx as the snapshot insert.
    """
    from models import CampaignWave  # local import — avoid cycle

    if not spec.waves:
        return []

    rows: List[Any] = []
    for entry in spec.waves:
        row = CampaignWave(
            campaign_id=campaign.id,
            tenant_id=campaign.tenant_id,
            wave_index=entry.wave_index,
            total_waves=spec.total_waves,
            status=WAVE_PENDING,
            scheduled_at=entry.scheduled_at,
            planned_recipients=entry.planned_recipients,
            plan_strategy=spec.strategy,
            plan_rationale=spec.rationale,
        )
        db.add(row)
        rows.append(row)
    db.flush()    # populate ids without committing
    return rows


def assign_send_logs_to_waves(
    *,
    db: Session,
    campaign_id: int,
    waves: List[Any],
) -> int:
    """Distribute the campaign's ``status='queued'`` send_log rows
    across the given waves, in id-order, respecting each wave's
    ``planned_recipients`` slot.

    Returns the total number of rows assigned. Idempotent in
    principle (re-running re-distributes what's still
    ``wave_id IS NULL``) but callers should normally run it once
    immediately after the snapshot.

    Implementation note
    ───────────────────
    We hold the ids in Python because UPDATE … FROM (SELECT … LIMIT)
    syntax differs across SQLite and PostgreSQL. The list is bounded
    by the campaign's audience size — even for a 100k-recipient
    campaign that's a few MB at most, well within tolerance for a
    one-time launch operation.
    """
    from models import CampaignSendLog  # local import

    if not waves:
        return 0

    queued_ids: List[int] = [
        row.id for row in (
            db.query(CampaignSendLog.id)
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.status == "queued",
                CampaignSendLog.wave_id.is_(None),
            )
            .order_by(CampaignSendLog.id.asc())
            .all()
        )
    ]
    if not queued_ids:
        return 0

    assigned = 0
    cursor = 0
    for wave in waves:
        slot = wave.planned_recipients
        if slot <= 0:
            continue
        chunk = queued_ids[cursor:cursor + slot]
        cursor += len(chunk)
        if not chunk:
            break
        # Bulk update — single round-trip per wave.
        (
            db.query(CampaignSendLog)
            .filter(CampaignSendLog.id.in_(chunk))
            .update({"wave_id": wave.id}, synchronize_session=False)
        )
        assigned += len(chunk)
        # If the real queued count diverged from ``planned_recipients``
        # (e.g. some recipients got skipped between plan and snapshot
        # → fewer queued than planned), update the wave's
        # ``planned_recipients`` to the actual size so the UI's
        # "1,247 / 2,000 sent" stays honest.
        if len(chunk) != slot:
            wave.planned_recipients = len(chunk)

    db.flush()
    logger.info(
        "wave_assign | campaign=%s waves=%d assigned=%d",
        campaign_id, len(waves), assigned,
    )
    return assigned


def pick_due_waves(
    *,
    db: Session,
    now: Optional[datetime] = None,
    limit: int = 32,
) -> List[Any]:
    """Return up to ``limit`` waves that are pending and due.

    The scheduler loop calls this every tick. The DB index
    ``ix_campaign_waves_due`` makes this a cheap range scan.

    We deliberately serialise the wave run inside the scheduler
    (one wave at a time globally) to avoid stampedes — for the
    single-worker deployment we have today this is sufficient. A
    future multi-worker version will need to switch to row-level
    locking (``SELECT FOR UPDATE SKIP LOCKED``).
    """
    from models import CampaignWave  # local import

    moment = now or datetime.now(timezone.utc)
    return (
        db.query(CampaignWave)
        .filter(
            CampaignWave.status == WAVE_PENDING,
            CampaignWave.scheduled_at <= moment,
        )
        .order_by(CampaignWave.scheduled_at.asc(), CampaignWave.id.asc())
        .limit(limit)
        .all()
    )


def mark_wave_dispatching(*, db: Session, wave) -> None:
    """Transition a wave to ``dispatching`` so the scheduler doesn't
    re-pick it on the next tick.

    Uses ``db.begin_nested`` so a race where two scheduler ticks
    both see the same wave doesn't leave the DB in a bad state —
    one of them flushes the status transition, the other one's
    flush no-ops (we check ``wave.status`` first).
    """
    if wave.status != WAVE_PENDING:
        return
    wave.status = WAVE_DISPATCHING
    wave.started_at = datetime.now(timezone.utc)
    try:
        with db.begin_nested():
            db.flush()
    except Exception as exc:
        logger.warning("wave_dispatch_mark_failed | wave=%s err=%s", wave.id, exc)


def complete_wave(
    *,
    db: Session,
    wave,
    sent: int,
    failed: int,
    success: bool = True,
) -> None:
    """Finalise a wave at the end of its dispatcher run."""
    wave.status = WAVE_COMPLETED if success else WAVE_FAILED
    wave.sent_count = int(sent or 0)
    wave.failed_count = int(failed or 0)
    wave.completed_at = datetime.now(timezone.utc)
    try:
        with db.begin_nested():
            db.flush()
    except Exception as exc:
        logger.warning("wave_complete_failed | wave=%s err=%s", wave.id, exc)


def latest_quality_for_tenant(
    *,
    db: Session,
    tenant_id: int,
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(nahla_tier, meta_quality_rating)`` for the tenant's
    most recent quality snapshot, or ``(None, None)`` if no
    snapshot exists yet.

    Used by the preflight endpoint to seed
    :func:`compute_adaptive_strategy`. We deliberately do NOT
    recompute the score here — that's expensive and the snapshot
    is good enough for a planning hint.
    """
    try:
        from models import WaNumberQualitySnapshot
    except Exception:
        return None, None

    snap = (
        db.query(WaNumberQualitySnapshot)
        .filter(WaNumberQualitySnapshot.tenant_id == tenant_id)
        .order_by(WaNumberQualitySnapshot.taken_at.desc())
        .first()
    )
    if not snap:
        return None, None
    return (
        getattr(snap, "nahla_quality_tier", None),
        getattr(snap, "meta_quality_rating", None),
    )


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _immediate_runtime_estimate(audience_size: int) -> timedelta:
    """Cheap estimate of how long the legacy in-process loop takes
    for a campaign of N recipients.

    Used purely so the preflight UI can show "ETA: ~6 min". We use
    the dispatcher's ``INTER_MESSAGE_DELAY`` (1.5s) + a small
    per-batch pause overhead.
    """
    seconds = max(1, int(audience_size * 1.6))
    return timedelta(seconds=seconds)


def _tier_label_ar(tier: str) -> str:
    return {
        "excellent": "ممتاز",
        "healthy":   "جيد",
        "warning":   "تحذير",
        "risky":     "محفوف بالمخاطر",
        "critical":  "حرج",
    }.get(tier, tier)


def _human_seconds(seconds: int) -> str:
    """Render a seconds value as a merchant-friendly Arabic duration."""
    if seconds <= 0:
        return "بدون فاصل"
    if seconds < 60:
        return f"{seconds} ثانية"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} دقيقة"
    hours = round(seconds / 3600, 1)
    if hours < 24:
        return f"{hours} ساعة"
    days = round(seconds / 86400, 1)
    return f"{days} يوم"


__all__ = [
    "WAVE_THRESHOLD_RECIPIENTS",
    "STRATEGY_IMMEDIATE",
    "STRATEGY_BATCHED",
    "STRATEGY_ADAPTIVE",
    "VALID_STRATEGIES",
    "WAVE_PENDING",
    "WAVE_DISPATCHING",
    "WAVE_COMPLETED",
    "WAVE_FAILED",
    "WAVE_PAUSED",
    "WAVE_CANCELLED",
    "WavePlanSpec",
    "WaveEntry",
    "suggest_batch_size_for_tier",
    "suggest_delay_for_tier",
    "compute_adaptive_strategy",
    "plan_waves",
    "materialise_waves",
    "assign_send_logs_to_waves",
    "pick_due_waves",
    "mark_wave_dispatching",
    "complete_wave",
    "latest_quality_for_tenant",
]
