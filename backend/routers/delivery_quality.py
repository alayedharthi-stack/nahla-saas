"""
routers/delivery_quality.py
────────────────────────────
Delivery Quality Intelligence Layer — read-only API.

Routes
──────
* ``GET /quality/numbers``           — one row per WABA number with
                                       its latest snapshot + a
                                       fresh live computation.
* ``GET /quality/numbers/{id}/history``
                                     — time-series of snapshots for
                                       a single number (charting).
* ``POST /quality/numbers/{id}/snapshot``
                                     — manually trigger a snapshot.
                                       Useful before launching a
                                       large campaign so the
                                       merchant sees the live
                                       number, not the last
                                       scheduler tick.

Design
──────
This router is **analytical** — no side effects on send behaviour.
Phase 2 explicitly carries no pre-send gating; the Quality Score
is consumed by the dashboard only. Pre-send gating is a Phase 4
feature that will live in ``core/send_governor.py``.

Tenant isolation
────────────────
Every endpoint resolves the tenant from the request and scopes
every query by ``tenant_id`` — there is no admin-cross-tenant
view here. Admins query through ``/admin/...`` routes for that.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from datetime import timedelta

from sqlalchemy import case, desc, func

from core.database import get_db
from core.tenant import resolve_tenant_id
from models import (
    MessageDeliveryEvent,
    WaNumberQualitySnapshot,
    WhatsAppConnection,
)
from services.meta_errors import ERRORS as META_ERRORS
from services.quality_score import (
    ALT_WINDOW_HOURS,
    DEFAULT_WINDOW_HOURS,
    TIER_THRESHOLDS,
    compute_quality_metrics,
    compute_quality_score,
    take_snapshot,
)

logger = logging.getLogger("nahla-backend")

router = APIRouter()


# ──────────────────────────────────────────────────────────────────────
# Serialisers
# ──────────────────────────────────────────────────────────────────────


def _serialise_connection(conn: WhatsAppConnection) -> Dict[str, Any]:
    """Public-safe representation of a WABA number.

    Note we never include the access token even though we have it
    on the model — Pydantic equivalence is enforced by hand here
    so a future field addition can't accidentally leak credentials.
    """
    return {
        "id":                     conn.id,
        "provider":               conn.provider,
        "phone_number":           conn.phone_number,
        "phone_number_id":        conn.phone_number_id,
        "business_display_name":  conn.business_display_name,
        "status":                 conn.status,
        "connection_type":        conn.connection_type,
        "meta_quality_rating":    conn.meta_quality_rating,
        "meta_messaging_limit":   conn.meta_messaging_limit,
        "meta_tier_updated_at":   _isoformat(conn.meta_tier_updated_at),
        "sending_enabled":        bool(conn.sending_enabled),
        "connected_at":           _isoformat(conn.connected_at),
    }


def _serialise_snapshot(snap: WaNumberQualitySnapshot) -> Dict[str, Any]:
    return {
        "id":                    snap.id,
        "taken_at":              _isoformat(snap.taken_at),
        "metrics_window_hours":  snap.metrics_window_hours,
        "meta_quality_rating":   snap.meta_quality_rating,
        "meta_messaging_limit":  snap.meta_messaging_limit,
        "nahla_quality_score":   snap.nahla_quality_score,
        "nahla_quality_tier":    snap.nahla_quality_tier,
        "delivery_rate":         snap.delivery_rate,
        "read_rate":             snap.read_rate,
        "failure_rate":          snap.failure_rate,
        "suppress_rate":         snap.suppress_rate,
        "complaint_rate":        snap.complaint_rate,
        "sample_size":           snap.sample_size,
        "raw_metrics":           snap.raw_metrics,
        "triggered_by":          snap.triggered_by,
    }


def _serialise_live(metrics, scored) -> Dict[str, Any]:
    """Render the in-memory ``QualityMetrics`` + ``QualityScore``
    in the same shape as a persisted snapshot — keeps the
    frontend renderer simple."""
    return {
        "id":                    None,             # not persisted
        "taken_at":              _isoformat(datetime.now(timezone.utc)),
        "metrics_window_hours":  metrics.window_hours,
        "meta_quality_rating":   None,
        "meta_messaging_limit":  None,
        "nahla_quality_score":   scored.score,
        "nahla_quality_tier":    scored.tier,
        "delivery_rate":         metrics.delivery_rate,
        "read_rate":             metrics.read_rate,
        "failure_rate":          metrics.failure_rate,
        "suppress_rate":         metrics.suppress_rate,
        "complaint_rate":        metrics.complaint_rate,
        "sample_size":           metrics.sample_size,
        "raw_metrics":           metrics.raw,
        "triggered_by":          "live",
    }


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# ──────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────


@router.get("/quality/numbers")
def list_quality_numbers(
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """List every WABA number for the tenant alongside its quality state.

    The response is intentionally chunky — each row carries:
      * ``connection``       — the WABA number record itself
      * ``live``             — freshly-computed score using the
                               default 7d window (NOT persisted)
      * ``latest_snapshot``  — most recent persisted snapshot
      * ``tier_thresholds``  — the discretisation cuts so the
                               frontend can label its gauge
                               without baking in numbers.

    Today most tenants have a single ``WhatsAppConnection`` (unique
    constraint on ``tenant_id``) but the endpoint is shaped to
    handle the future multi-number case.
    """
    tenant_id = resolve_tenant_id(request)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="tenant_unresolved")

    connections: List[WhatsAppConnection] = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .all()
    )
    if not connections:
        return {
            "numbers":          [],
            "tier_thresholds":  _tier_payload(),
            "default_window_hours": DEFAULT_WINDOW_HOURS,
        }

    numbers: List[Dict[str, Any]] = []
    for conn in connections:
        # Live snapshot — fresh aggregate against the DB. Cheap
        # query, single round-trip, safe to do per request.
        metrics = compute_quality_metrics(
            db=db, tenant_id=tenant_id,
            window_hours=DEFAULT_WINDOW_HOURS,
        )
        scored = compute_quality_score(metrics)

        latest = (
            db.query(WaNumberQualitySnapshot)
            .filter(
                WaNumberQualitySnapshot.tenant_id == tenant_id,
                WaNumberQualitySnapshot.connection_id == conn.id,
            )
            .order_by(WaNumberQualitySnapshot.taken_at.desc())
            .first()
        )

        numbers.append({
            "connection":      _serialise_connection(conn),
            "live":            _serialise_live(metrics, scored),
            "latest_snapshot": _serialise_snapshot(latest) if latest else None,
        })

    return {
        "numbers":              numbers,
        "tier_thresholds":      _tier_payload(),
        "default_window_hours": DEFAULT_WINDOW_HOURS,
        "alt_window_hours":     ALT_WINDOW_HOURS,
    }


@router.get("/quality/numbers/{connection_id}/history")
def get_quality_history(
    connection_id: int,
    request: Request,
    limit: int = 90,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Time-series of snapshots for one WABA number.

    ``limit`` is capped at 365 — the dashboard never needs more
    than a year of history for the chart, and unbounded
    pagination on this path would let a tenant pull the entire
    snapshot table. (We do not expose pagination keys here on
    purpose — if a use-case for >365 surfaces, build a dedicated
    export endpoint.)
    """
    tenant_id = resolve_tenant_id(request)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="tenant_unresolved")

    capped = max(1, min(int(limit or 90), 365))

    conn = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.id == connection_id,
            WhatsAppConnection.tenant_id == tenant_id,
        )
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="connection_not_found")

    snaps: List[WaNumberQualitySnapshot] = (
        db.query(WaNumberQualitySnapshot)
        .filter(
            WaNumberQualitySnapshot.tenant_id == tenant_id,
            WaNumberQualitySnapshot.connection_id == connection_id,
        )
        .order_by(WaNumberQualitySnapshot.taken_at.desc())
        .limit(capped)
        .all()
    )

    return {
        "connection":       _serialise_connection(conn),
        "snapshots":        [_serialise_snapshot(s) for s in snaps],
        "tier_thresholds":  _tier_payload(),
    }


@router.get("/quality/numbers/{connection_id}/failures")
def get_failure_breakdown(
    connection_id: int,
    request: Request,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Per-``error_code`` rollup of failed delivery events for the
    tenant in the requested window.

    The dashboard renders this as a ranked table so the merchant
    can see at a glance *which* failure category is hurting their
    deliverability the most — and act on the right lever
    (audience clean-up vs. template review vs. throttling).

    ``connection_id`` is REQUIRED in the route to keep the URL
    schema consistent with the other ``/quality/numbers/{id}/*``
    endpoints, but ``MessageDeliveryEvent`` is keyed by tenant
    (each tenant owns at most one connection today), so the
    aggregate is scoped to ``tenant_id``. If a tenant ever owns
    multiple numbers we'll add a ``phone_number_id`` filter here.
    """
    tenant_id = resolve_tenant_id(request)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="tenant_unresolved")

    # Clamp the window to sane bounds — anything <1h is noise,
    # anything >90d is a "wrong endpoint" call (the snapshots
    # table is the right place to query for that).
    capped_hours = max(1, min(int(window_hours or DEFAULT_WINDOW_HOURS), 2160))
    since = datetime.now(timezone.utc) - timedelta(hours=capped_hours)

    conn = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.id == connection_id,
            WhatsAppConnection.tenant_id == tenant_id,
        )
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="connection_not_found")

    # ── Per-error_code rollup ──────────────────────────────────────
    # ``COALESCE(error_code, 'unknown')`` covers the rare case where
    # a webhook lands with ``status="failed"`` but no error block —
    # we still want the row counted so the totals add up.
    coalesced = func.coalesce(MessageDeliveryEvent.error_code, "unknown_error")
    rows = (
        db.query(
            coalesced.label("error_key"),
            func.count(MessageDeliveryEvent.id).label("count"),
            func.max(MessageDeliveryEvent.quality_tier).label("tier_hint"),
            func.count(
                func.distinct(MessageDeliveryEvent.phone_e164)
            ).label("distinct_phones"),
        )
        .filter(
            MessageDeliveryEvent.tenant_id == tenant_id,
            MessageDeliveryEvent.occurred_at >= since,
            MessageDeliveryEvent.status == "failed",
        )
        .group_by(coalesced)
        .order_by(desc("count"))
        .all()
    )

    total_failures = sum(int(r.count or 0) for r in rows)
    breakdown: List[Dict[str, Any]] = []
    for r in rows:
        key = r.error_key or "unknown_error"
        # Enrich with the canonical classifier metadata so the
        # frontend doesn't need to ship the same dictionary twice.
        meta = META_ERRORS.get(key)
        count = int(r.count or 0)
        breakdown.append({
            "error_key":   key,
            "count":       count,
            "share":       (count / total_failures) if total_failures else 0.0,
            "distinct_phones": int(r.distinct_phones or 0),
            "quality_tier": (meta.quality_tier if meta else r.tier_hint) or "warning",
            "suppress_on_repeat": bool(meta.suppress_on_repeat) if meta else False,
            "label_ar":    (meta.label_ar if meta else key),
            "advice_ar":   (meta.advice_ar if meta else None),
        })

    return {
        "connection_id":  connection_id,
        "window_hours":   capped_hours,
        "since":          since.isoformat(),
        "total_failures": total_failures,
        "breakdown":      breakdown,
    }


@router.post("/quality/numbers/{connection_id}/snapshot")
def force_snapshot(
    connection_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Force a fresh snapshot. Useful before launching a campaign.

    Persists a row tagged ``triggered_by="manual"`` so analytics
    queries can tell the two apart from scheduler-driven rows.
    """
    tenant_id = resolve_tenant_id(request)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="tenant_unresolved")

    conn = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.id == connection_id,
            WhatsAppConnection.tenant_id == tenant_id,
        )
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="connection_not_found")

    snap_id = take_snapshot(
        db=db,
        tenant_id=tenant_id,
        connection_id=conn.id,
        triggered_by="manual",
        meta_quality_rating=conn.meta_quality_rating,
        meta_messaging_limit=conn.meta_messaging_limit,
    )
    db.commit()
    if not snap_id:
        raise HTTPException(status_code=500, detail="snapshot_failed")

    snap = (
        db.query(WaNumberQualitySnapshot)
        .filter(WaNumberQualitySnapshot.id == snap_id)
        .first()
    )
    return {
        "snapshot": _serialise_snapshot(snap) if snap else None,
    }


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _tier_payload() -> List[Dict[str, Any]]:
    """Stable JSON for the discretisation table.

    The frontend uses this to colour the gauge / tier badge
    without baking the numbers into its own code.
    """
    return [
        {"label": label, "lower_bound": lower}
        for label, lower in TIER_THRESHOLDS
    ]
