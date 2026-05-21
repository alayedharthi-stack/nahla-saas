"""
core/ai_quality_events.py
─────────────────────────
Persistence layer for the AI Quality Monitor (May 2026 #12).

The brain pipeline already emits ``[ALIGN_MISMATCH]`` warnings via
``modules.ai.brain.postprocess.answer_alignment.emit_mismatch_log``.
This module ADDITIONALLY persists each mismatch to ``ai_quality_events``
so the merchant can browse misclassifications from the admin dashboard
without grepping Railway logs.

Privacy contract (mirrors the original audit request)
─────────────────────────────────────────────────────
* Phone numbers are stored **masked** (``+9665***430``). Full E.164
  forms never enter this table — they live on
  ``conversations.customer_id → customers.phone`` only, where the
  inbox already enforces tenant scoping.
* ``inbound_preview`` / ``reply_preview`` are truncated to
  ``PREVIEW_MAX_CHARS`` (200) before write.
* No mutation of brain state, no auto-regeneration. Persistence is
  purely observational.

Surgical contract
─────────────────
This module is additive. It exposes:

  * ``mask_phone(raw)``                         — privacy-safe display.
  * ``persist_alignment_mismatch(db, ...)``     — write one event row.
  * ``aggregate_recent_mismatches(db, since)``  — counts per type.
  * ``check_threshold_and_alert(db, ...)``      — emits
    ``[AI_QUALITY_ALERT]`` warnings when a mismatch type exceeds a
    threshold in the lookback window.

Every public function is exception-safe. A failure in persistence
must never break a customer turn — we fall back to log-only.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.ai_quality_events")


# ── Constants ───────────────────────────────────────────────────────────────


PREVIEW_MAX_CHARS = 200

VALID_RESOLVED_STATUSES = frozenset({"open", "reviewed", "ignored", "fixed"})

# Default thresholds for the 6-hour periodic alert. Tuned to be
# noisy enough to surface real spikes but not so noisy that alerts
# fire on a single misfire. Override via env in production.
DEFAULT_ALERT_THRESHOLDS: Dict[str, int] = {
    "question_to_social":   10,
    "delivery_to_receipt":   5,
    "closing_to_reopen":     8,
    "religious_to_oos":      3,
}

# Lookback window for the aggregation job. Default 6 hours.
DEFAULT_LOOKBACK_HOURS = 6


# ── Phone masking ───────────────────────────────────────────────────────────


def mask_phone(raw: Optional[str]) -> str:
    """Privacy-safe display form: keep first 4 + last 3, redact middle.

    Mirrors ``backend.routers.admin_debug._mask_phone`` so the dashboard
    UI sees a single consistent shape:

        "+966537970430" → "+9665***430"
        "966537970430"  → "9665***430"
        ""              → ""
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if len(s) <= 7:
        return "***"
    head = s[:4]
    tail = s[-3:]
    return f"{head}***{tail}"


def _truncate(text: Optional[str], limit: int = PREVIEW_MAX_CHARS) -> Optional[str]:
    if text is None:
        return None
    s = str(text)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


# ── Single-row persistence ──────────────────────────────────────────────────


def persist_alignment_mismatch(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int],
    customer_phone: str,
    inbound_text: str,
    reply_text: str,
    mismatch_type: str,
    mismatch_reason: str,
    detected_intent: str = "",
    social_category: str = "",
    action_taken: str = "",
    chosen_path: str = "",
    fallback_used: bool = False,
    order_status: str = "",
    awaiting_payment_receipt: bool = False,
    model_used: str = "",
    turn: int = 0,
    alignment_passed: bool = False,
    regen_fired: bool = False,
) -> Optional[int]:
    """Append one ``ai_quality_events`` row.

    Returns the new row id on success, ``None`` on any failure.
    Never raises — the caller's reply path takes precedence over
    observability persistence.

    The phone number is masked before write; we never touch the raw
    E.164 here.
    """
    try:
        from database.models import AiQualityEvent  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[AI_QUALITY] model import failed — event dropped: %s", exc,
        )
        return None
    try:
        row = AiQualityEvent(
            tenant_id=int(tenant_id),
            conversation_id=conversation_id if conversation_id else None,
            customer_phone_masked=mask_phone(customer_phone),
            mismatch_type=str(mismatch_type or "unknown"),
            mismatch_reason=_truncate(mismatch_reason, 500),
            detected_intent=str(detected_intent or "")[:64] or None,
            social_category=str(social_category or "")[:64] or None,
            action_taken=str(action_taken or "")[:64] or None,
            chosen_path=str(chosen_path or "")[:64] or None,
            fallback_used=bool(fallback_used),
            order_status=str(order_status or "")[:64] or None,
            awaiting_payment_receipt=bool(awaiting_payment_receipt),
            model_used=str(model_used or "")[:64] or None,
            turn=int(turn) if turn else None,
            inbound_preview=_truncate(inbound_text),
            reply_preview=_truncate(reply_text),
            alignment_passed=bool(alignment_passed),
            regen_fired=bool(regen_fired),
            resolved_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.flush()  # populate ``row.id`` without committing — caller owns the txn
        return int(getattr(row, "id", 0)) or None
    except Exception as exc:  # noqa: BLE001
        # Roll back the failed insert without touching the parent
        # transaction. Best-effort; if rollback itself fails we log
        # and fall through.
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "[AI_QUALITY] persistence failed tenant=%s mismatch=%s: %s",
            tenant_id, mismatch_type, exc,
        )
        return None


# ── Aggregation helpers (used by the periodic alert job + summary API) ─────


def aggregate_recent_mismatches(
    db: Any,
    *,
    since: datetime,
    tenant_id: Optional[int] = None,
) -> Dict[str, int]:
    """Return ``{mismatch_type: count}`` for events created at or after
    ``since``. When ``tenant_id`` is provided, scope to that tenant
    (the periodic job aggregates platform-wide, the dashboard scopes
    per-tenant).

    Never raises — returns ``{}`` on any failure.
    """
    try:
        from database.models import AiQualityEvent  # noqa: PLC0415
        from sqlalchemy import func  # noqa: PLC0415
    except Exception:
        return {}
    try:
        q = (
            db.query(
                AiQualityEvent.mismatch_type,
                func.count(AiQualityEvent.id),
            )
            .filter(AiQualityEvent.created_at >= since)
        )
        if tenant_id is not None:
            q = q.filter(AiQualityEvent.tenant_id == int(tenant_id))
        rows = q.group_by(AiQualityEvent.mismatch_type).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AI_QUALITY] aggregation failed: %s", exc)
        return {}
    return {str(row[0] or "unknown"): int(row[1] or 0) for row in rows}


# ── Threshold-based alert emission ──────────────────────────────────────────


def _resolve_thresholds() -> Dict[str, int]:
    """Pull thresholds from env when set, fall back to defaults.

    ENV format: ``AI_QUALITY_THRESHOLDS=question_to_social:10,delivery_to_receipt:5``
    """
    raw = (os.environ.get("AI_QUALITY_THRESHOLDS") or "").strip()
    if not raw:
        return dict(DEFAULT_ALERT_THRESHOLDS)
    out = dict(DEFAULT_ALERT_THRESHOLDS)
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, v = part.split(":", 1)
        k = k.strip()
        try:
            out[k] = int(v.strip())
        except (ValueError, TypeError):
            continue
    return out


def _resolve_lookback_hours() -> int:
    raw = (os.environ.get("AI_QUALITY_LOOKBACK_HOURS") or "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return DEFAULT_LOOKBACK_HOURS


def check_threshold_and_alert(
    db: Any,
    *,
    now: Optional[datetime] = None,
    lookback_hours: Optional[int] = None,
    thresholds: Optional[Dict[str, int]] = None,
) -> List[Tuple[str, int, int]]:
    """Aggregate mismatches in the lookback window and emit an
    ``[AI_QUALITY_ALERT]`` warning for every type that breached its
    threshold.

    Returns the list of breaches as ``[(type, count, threshold), ...]``
    so callers / tests can introspect what fired.

    Never raises — alerting is best-effort.
    """
    try:
        now_utc = now or datetime.now(timezone.utc)
        lb = lookback_hours if lookback_hours is not None else _resolve_lookback_hours()
        thr = thresholds if thresholds is not None else _resolve_thresholds()
        since = now_utc - timedelta(hours=lb)
        counts = aggregate_recent_mismatches(db, since=since)

        breaches: List[Tuple[str, int, int]] = []
        for mtype, threshold in thr.items():
            n = int(counts.get(mtype, 0))
            if n >= int(threshold):
                breaches.append((mtype, n, int(threshold)))

        if breaches:
            for mtype, n, threshold in breaches:
                logger.warning(
                    "[AI_QUALITY_ALERT] mismatch=%s count=%d threshold=%d "
                    "lookback_hours=%d window_start=%s",
                    mtype, n, threshold, lb, since.isoformat(),
                )
        else:
            logger.info(
                "[AI_QUALITY_ALERT] all clear — counts=%s lookback_hours=%d",
                counts, lb,
            )
        return breaches
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AI_QUALITY_ALERT] threshold check failed: %s", exc)
        return []


__all__ = [
    "PREVIEW_MAX_CHARS",
    "VALID_RESOLVED_STATUSES",
    "DEFAULT_ALERT_THRESHOLDS",
    "DEFAULT_LOOKBACK_HOURS",
    "mask_phone",
    "persist_alignment_mismatch",
    "aggregate_recent_mismatches",
    "check_threshold_and_alert",
]
