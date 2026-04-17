"""
routers/offer_decisions.py
──────────────────────────
Telemetry surface for the OfferDecisionService ledger.

Read-only aggregates over the `offer_decisions` table, scoped per-tenant:

    GET  /offers/decisions/summary           headline KPIs
    GET  /offers/decisions/breakdown         counts grouped by surface /
                                             chosen_source / reason_code

The merchant dashboard's Analytics page consumes the summary endpoint to
render the "Smart Offer Performance" widget. We deliberately keep this
router CRUD-free: ledger rows are written by the service and updated
only by the attribution hook — never by an admin UI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import resolve_tenant_id
from models import OfferDecisionLedger


router = APIRouter(prefix="/offers/decisions", tags=["Offer Decisions"])


# ── Helpers ──────────────────────────────────────────────────────────────

def _window_start(days: int) -> datetime:
    days = max(1, min(int(days or 30), 365))
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)


def _decimal_to_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(Decimal(str(value)))
    except Exception:
        return 0.0


# ── GET /offers/decisions/summary ────────────────────────────────────────

@router.get("/summary")
async def decisions_summary(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Headline KPIs for the Analytics widget.

    Returns:
      {
        "window_days": 30,
        "decisions_total":      <int>   total decisions written (incl. 'none')
        "offers_issued":        <int>   decisions where chosen_source != 'none'
        "offers_attributed":    <int>   redeemed by an attributed order
        "redemption_rate_pct":  <float> attributed / issued (0–100, 1 dp)
        "attributed_revenue":   <float> sum(revenue_amount) where attributed
        "policy_version":       <str>   currently most-used version (audit)
        "by_surface":           {"automation": ..., "chat": ..., "segment_change": ...}
        "by_source":            {"promotion": ..., "coupon": ..., "none": ...}
      }
    """
    tenant_id = resolve_tenant_id(request)
    since = _window_start(days)

    base_q = (
        db.query(OfferDecisionLedger)
        .filter(
            OfferDecisionLedger.tenant_id == tenant_id,
            OfferDecisionLedger.created_at >= since,
        )
    )
    decisions_total = base_q.count()
    offers_issued = base_q.filter(OfferDecisionLedger.chosen_source != "none").count()
    offers_attributed = base_q.filter(OfferDecisionLedger.attributed.is_(True)).count()

    redemption_rate = 0.0
    if offers_issued:
        redemption_rate = round((offers_attributed / offers_issued) * 100.0, 1)

    revenue_total = (
        db.query(func.coalesce(func.sum(OfferDecisionLedger.revenue_amount), 0))
        .filter(
            OfferDecisionLedger.tenant_id == tenant_id,
            OfferDecisionLedger.attributed.is_(True),
            OfferDecisionLedger.created_at >= since,
        )
        .scalar()
    )

    by_surface_rows = (
        db.query(OfferDecisionLedger.surface, func.count(OfferDecisionLedger.id))
        .filter(
            OfferDecisionLedger.tenant_id == tenant_id,
            OfferDecisionLedger.created_at >= since,
        )
        .group_by(OfferDecisionLedger.surface)
        .all()
    )
    by_surface = {row[0]: int(row[1]) for row in by_surface_rows}

    by_source_rows = (
        db.query(OfferDecisionLedger.chosen_source, func.count(OfferDecisionLedger.id))
        .filter(
            OfferDecisionLedger.tenant_id == tenant_id,
            OfferDecisionLedger.created_at >= since,
        )
        .group_by(OfferDecisionLedger.chosen_source)
        .all()
    )
    by_source = {row[0]: int(row[1]) for row in by_source_rows}

    # Most-used policy version in window — useful when we start running
    # multiple deterministic versions side-by-side (or a bandit later).
    pv_row = (
        db.query(OfferDecisionLedger.policy_version, func.count(OfferDecisionLedger.id).label("c"))
        .filter(
            OfferDecisionLedger.tenant_id == tenant_id,
            OfferDecisionLedger.created_at >= since,
        )
        .group_by(OfferDecisionLedger.policy_version)
        .order_by(func.count(OfferDecisionLedger.id).desc())
        .first()
    )
    policy_version = pv_row[0] if pv_row else "v1.0-deterministic"

    return {
        "window_days":        days,
        "decisions_total":    decisions_total,
        "offers_issued":      offers_issued,
        "offers_attributed":  offers_attributed,
        "redemption_rate_pct": redemption_rate,
        "attributed_revenue": _decimal_to_float(revenue_total),
        "policy_version":     policy_version,
        "by_surface":         by_surface,
        "by_source":          by_source,
    }


# ── GET /offers/decisions/breakdown ──────────────────────────────────────

@router.get("/breakdown")
async def decisions_breakdown(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    More granular splits driving the dashboard widget tabs:
      • reason_code → count (the policy's explainability trail)
      • surface × chosen_source → count (heat-map cell)
      • discount-bucket attribution (lift by 5pp bucket)
    """
    tenant_id = resolve_tenant_id(request)
    since = _window_start(days)

    rows = (
        db.query(OfferDecisionLedger)
        .filter(
            OfferDecisionLedger.tenant_id == tenant_id,
            OfferDecisionLedger.created_at >= since,
        )
        .all()
    )

    reason_counts: Dict[str, int] = {}
    matrix: Dict[str, Dict[str, int]] = {}
    by_bucket: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        for code in (r.reason_codes or []):
            reason_counts[code] = reason_counts.get(code, 0) + 1

        cell = matrix.setdefault(r.surface, {})
        cell[r.chosen_source] = cell.get(r.chosen_source, 0) + 1

        if r.chosen_source != "none" and r.discount_type == "percentage" and r.discount_value is not None:
            try:
                pct = float(Decimal(str(r.discount_value)))
            except Exception:
                continue
            bucket = f"{int((pct // 5) * 5)}-{int((pct // 5) * 5) + 4}%"
            entry = by_bucket.setdefault(bucket, {"issued": 0, "attributed": 0, "revenue": 0.0})
            entry["issued"] += 1
            if r.attributed:
                entry["attributed"] += 1
                entry["revenue"] += _decimal_to_float(r.revenue_amount)

    # Sort reason_codes by descending count for stable rendering.
    sorted_reasons = sorted(reason_counts.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "window_days":   days,
        "reason_codes":  [{"code": k, "count": v} for k, v in sorted_reasons],
        "matrix":        matrix,
        "by_discount_bucket": by_bucket,
    }
