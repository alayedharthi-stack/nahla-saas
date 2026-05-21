"""
routers/admin_ai_quality.py
───────────────────────────
Admin / owner surface for the in-product **AI Quality Monitor**
(May 2026 #12).

The brain pipeline writes one ``ai_quality_events`` row whenever
``modules.ai.brain.postprocess.answer_alignment.check_alignment``
flags a reply that does not actually answer the customer's last
message. This router exposes those rows so the merchant can browse
misclassifications, mark them ``reviewed`` / ``ignored`` / ``fixed``,
and watch trend counts roll up by mismatch type.

Routes
──────
* ``GET   /admin/ai-quality/events``         — paginated list with
  filters: ``tenant_id``, ``mismatch_type``, ``resolved_status``,
  ``since`` (ISO datetime), ``until`` (ISO datetime), ``limit``,
  ``offset``.
* ``GET   /admin/ai-quality/summary``        — aggregate counts by
  ``mismatch_type``, the ``top_conversations`` (most-flagged
  ``conversation_id`` values), and the ``latest_events`` ring (50).
* ``PATCH /admin/ai-quality/events/{id}``    — set ``resolved_status``
  (one of ``open``/``reviewed``/``ignored``/``fixed``) plus optional
  ``resolved_note``. Stamps ``resolved_by`` from the admin token.

Auth
────
Every route depends on ``core.auth.require_admin`` — same policy
used by ``admin_debug`` and ``admin_webhook_security``.

Privacy
───────
Phone numbers are stored masked at the model layer (see
``database.models.AiQualityEvent.customer_phone_masked``) so this
router never has to redact in flight. Truncation of
``inbound_preview`` / ``reply_preview`` happens at write time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from core.ai_quality_events import VALID_RESOLVED_STATUSES
from core.auth import require_admin
from core.database import get_db
from database.models import AiQualityEvent

logger = logging.getLogger("nahla.admin.ai_quality")

router = APIRouter(tags=["Admin · AI Quality"])


# ── Constants ───────────────────────────────────────────────────────────


MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50
SUMMARY_LATEST_LIMIT = 50
SUMMARY_TOP_CONVERSATIONS_LIMIT = 10
DEFAULT_SUMMARY_LOOKBACK_HOURS = 24


# ── Pydantic schemas ────────────────────────────────────────────────────


class AiQualityEventOut(BaseModel):
    """Single ``ai_quality_events`` row, privacy-safe by construction.

    The ``customer_phone_masked`` field is the only phone form ever
    returned — full E.164 lives on ``customers.phone`` and is not
    joined here.
    """
    id: int
    tenant_id: int
    conversation_id: Optional[int] = None
    customer_phone_masked: str

    mismatch_type: str
    mismatch_reason: Optional[str] = None

    detected_intent: Optional[str] = None
    social_category: Optional[str] = None
    action_taken: Optional[str] = None
    chosen_path: Optional[str] = None
    fallback_used: Optional[bool] = None
    order_status: Optional[str] = None
    awaiting_payment_receipt: Optional[bool] = None
    model_used: Optional[str] = None
    turn: Optional[int] = None

    inbound_preview: Optional[str] = None
    reply_preview: Optional[str] = None

    alignment_passed: bool
    regen_fired: bool

    resolved_status: str
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolved_note: Optional[str] = None

    created_at: datetime

    class Config:
        from_attributes = True  # pydantic v2 ORM mode


class AiQualityEventListResponse(BaseModel):
    items: List[AiQualityEventOut]
    total: int
    limit: int
    offset: int


class AiQualityCountByType(BaseModel):
    mismatch_type: str
    count: int


class AiQualityTopConversation(BaseModel):
    conversation_id: int
    count: int
    last_seen: datetime


class AiQualitySummaryResponse(BaseModel):
    window_start: datetime
    window_hours: int
    total_open: int
    total_in_window: int
    counts_by_type: List[AiQualityCountByType]
    top_conversations: List[AiQualityTopConversation]
    latest_events: List[AiQualityEventOut]


class AiQualityResolvePayload(BaseModel):
    resolved_status: str = Field(
        ...,
        description="One of: open / reviewed / ignored / fixed",
    )
    resolved_note: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Optional free-form note from the operator.",
    )


# ── Helpers ─────────────────────────────────────────────────────────────


def _admin_actor_label(admin_claims: Dict[str, Any]) -> str:
    """Best-effort actor label for the ``resolved_by`` audit column.

    Uses ``email`` if the JWT carries one, else ``role``+``user_id``,
    else ``"admin"``. Bounded to 120 chars defensively.
    """
    email = (admin_claims or {}).get("email") or ""
    if email:
        return str(email)[:120]
    role = (admin_claims or {}).get("role") or "admin"
    uid = (admin_claims or {}).get("user_id") or (admin_claims or {}).get("sub")
    if uid:
        return f"{role}:{uid}"[:120]
    return str(role)[:120]


def _parse_iso_datetime(raw: Optional[str], *, field_name: str) -> Optional[datetime]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        # ``fromisoformat`` accepts ``2026-05-21T00:00:00+00:00`` and
        # ``2026-05-21T00:00:00Z`` (after the ``Z`` swap).
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: must be ISO-8601 datetime ({exc})",
        )
    # Treat naive timestamps as UTC — the dashboard sends UTC by
    # default.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── GET /admin/ai-quality/events ────────────────────────────────────────


@router.get(
    "/admin/ai-quality/events",
    response_model=AiQualityEventListResponse,
)
def list_ai_quality_events(
    tenant_id:       Optional[int] = Query(default=None, description="Filter by tenant."),
    mismatch_type:   Optional[str] = Query(default=None,
                       description="Filter by mismatch type (question_to_social, "
                                   "delivery_to_receipt, closing_to_reopen, "
                                   "religious_to_oos, ...)."),
    resolved_status: Optional[str] = Query(default=None,
                       description="Filter by triage state (open / reviewed / "
                                   "ignored / fixed)."),
    since:           Optional[str] = Query(default=None,
                       description="ISO-8601 lower bound on ``created_at`` (UTC)."),
    until:           Optional[str] = Query(default=None,
                       description="ISO-8601 upper bound on ``created_at`` (UTC)."),
    limit:           int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset:          int = Query(default=0, ge=0),
    db:              Session       = Depends(get_db),
    _admin:          Dict[str, Any] = Depends(require_admin),
) -> AiQualityEventListResponse:
    """Paginated, filterable browse of mismatch events.

    All filters are optional. The default sort is newest-first to
    match the dashboard's "what just happened?" reading order.
    """
    if resolved_status is not None:
        resolved_status = resolved_status.strip().lower()
        if resolved_status not in VALID_RESOLVED_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    "resolved_status must be one of: "
                    f"{sorted(VALID_RESOLVED_STATUSES)}"
                ),
            )

    since_dt = _parse_iso_datetime(since, field_name="since")
    until_dt = _parse_iso_datetime(until, field_name="until")

    q = db.query(AiQualityEvent)
    if tenant_id is not None:
        q = q.filter(AiQualityEvent.tenant_id == int(tenant_id))
    if mismatch_type:
        q = q.filter(AiQualityEvent.mismatch_type == str(mismatch_type).strip())
    if resolved_status:
        q = q.filter(AiQualityEvent.resolved_status == resolved_status)
    if since_dt is not None:
        q = q.filter(AiQualityEvent.created_at >= since_dt)
    if until_dt is not None:
        q = q.filter(AiQualityEvent.created_at <= until_dt)

    # ``count()`` BEFORE pagination — the dashboard pager needs the
    # total to draw page numbers.
    total = q.count()
    rows = (
        q.order_by(desc(AiQualityEvent.created_at), desc(AiQualityEvent.id))
         .offset(int(offset))
         .limit(int(limit))
         .all()
    )

    return AiQualityEventListResponse(
        items=[AiQualityEventOut.model_validate(r) for r in rows],
        total=int(total or 0),
        limit=int(limit),
        offset=int(offset),
    )


# ── GET /admin/ai-quality/summary ───────────────────────────────────────


@router.get(
    "/admin/ai-quality/summary",
    response_model=AiQualitySummaryResponse,
)
def ai_quality_summary(
    tenant_id:     Optional[int] = Query(default=None, description="Filter by tenant."),
    window_hours:  int           = Query(default=DEFAULT_SUMMARY_LOOKBACK_HOURS,
                                         ge=1, le=24 * 30,
                                         description="Lookback window in hours."),
    db:            Session        = Depends(get_db),
    _admin:        Dict[str, Any] = Depends(require_admin),
) -> AiQualitySummaryResponse:
    """Rollup view: counts by type, top conversations, latest 50.

    Powers the dashboard's hero panel. Runs three lightweight queries
    against the same ``ai_quality_events`` table — no joins, all
    backed by the indexes added in ``0066``.
    """
    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - timedelta(hours=int(window_hours))

    base = db.query(AiQualityEvent).filter(
        AiQualityEvent.created_at >= window_start
    )
    if tenant_id is not None:
        base = base.filter(AiQualityEvent.tenant_id == int(tenant_id))

    total_in_window = int(base.count() or 0)

    # ── 1. counts by mismatch type ──────────────────────────────────
    counts_q = (
        base.with_entities(
            AiQualityEvent.mismatch_type,
            func.count(AiQualityEvent.id),
        )
        .group_by(AiQualityEvent.mismatch_type)
        .order_by(desc(func.count(AiQualityEvent.id)))
    )
    counts_by_type = [
        AiQualityCountByType(
            mismatch_type=str(row[0] or "unknown"),
            count=int(row[1] or 0),
        )
        for row in counts_q.all()
    ]

    # ── 2. top conversations (most flagged in window) ───────────────
    top_q = (
        base.with_entities(
            AiQualityEvent.conversation_id,
            func.count(AiQualityEvent.id),
            func.max(AiQualityEvent.created_at),
        )
        .filter(AiQualityEvent.conversation_id.isnot(None))
        .group_by(AiQualityEvent.conversation_id)
        .order_by(desc(func.count(AiQualityEvent.id)))
        .limit(SUMMARY_TOP_CONVERSATIONS_LIMIT)
    )
    top_conversations = [
        AiQualityTopConversation(
            conversation_id=int(row[0]),
            count=int(row[1] or 0),
            last_seen=row[2] or now_utc,
        )
        for row in top_q.all()
    ]

    # ── 3. latest events ring ───────────────────────────────────────
    latest_rows = (
        base.order_by(desc(AiQualityEvent.created_at), desc(AiQualityEvent.id))
            .limit(SUMMARY_LATEST_LIMIT)
            .all()
    )
    latest_events = [AiQualityEventOut.model_validate(r) for r in latest_rows]

    # ── 4. open-state counter (across all time, scoped) ─────────────
    open_q = db.query(func.count(AiQualityEvent.id)).filter(
        AiQualityEvent.resolved_status == "open"
    )
    if tenant_id is not None:
        open_q = open_q.filter(AiQualityEvent.tenant_id == int(tenant_id))
    total_open = int(open_q.scalar() or 0)

    return AiQualitySummaryResponse(
        window_start=window_start,
        window_hours=int(window_hours),
        total_open=total_open,
        total_in_window=total_in_window,
        counts_by_type=counts_by_type,
        top_conversations=top_conversations,
        latest_events=latest_events,
    )


# ── PATCH /admin/ai-quality/events/{id} ─────────────────────────────────


@router.patch(
    "/admin/ai-quality/events/{event_id}",
    response_model=AiQualityEventOut,
)
def resolve_ai_quality_event(
    event_id: int,
    payload:  AiQualityResolvePayload,
    db:       Session        = Depends(get_db),
    _admin:   Dict[str, Any] = Depends(require_admin),
) -> AiQualityEventOut:
    """Set the operator triage state for one event.

    Stamps ``resolved_by`` (from the admin JWT) and ``resolved_at``
    when the new status is anything other than ``open``. Emits a
    structured info log so the action shows up in Railway too.
    """
    new_status = (payload.resolved_status or "").strip().lower()
    if new_status not in VALID_RESOLVED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                "resolved_status must be one of: "
                f"{sorted(VALID_RESOLVED_STATUSES)}"
            ),
        )

    row: Optional[AiQualityEvent] = (
        db.query(AiQualityEvent).filter(AiQualityEvent.id == int(event_id)).one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")

    actor = _admin_actor_label(_admin)
    row.resolved_status = new_status
    if new_status == "open":
        # Reopening clears any prior resolution metadata so the
        # audit log doesn't lie about "reviewed_by" on an open row.
        row.resolved_by = None
        row.resolved_at = None
        row.resolved_note = payload.resolved_note
    else:
        row.resolved_by = actor
        row.resolved_at = datetime.now(timezone.utc)
        if payload.resolved_note is not None:
            row.resolved_note = payload.resolved_note

    db.commit()
    db.refresh(row)

    logger.info(
        "[AI_QUALITY] event=%s tenant=%s status=%s actor=%s",
        row.id, row.tenant_id, row.resolved_status, actor,
    )
    return AiQualityEventOut.model_validate(row)
