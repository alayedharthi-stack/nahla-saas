"""
routers/admin_inbound_debug.py
──────────────────────────────
Tactical admin endpoint for triaging "the customer sent something but
nothing showed up in Nahla" reports — the May 2026 #41 incident.

Why a separate router:
  ``admin_ai_quality.py`` exposes ``ai_quality_events`` rows with full
  filtering, but that table only carries the *drop* perspective. After
  the May 2026 #41 fix, ``whatsapp_webhook._persist_inbound_only`` now
  ALSO writes a placeholder ``MessageEvent`` row for every inbound that
  cannot reach the brain. Triage needs both perspectives at once:

    * Did the message get persisted at all?           → MessageEvent
    * Why didn't the brain answer?                    → AiQualityEvent

This router unifies the two views in a single response so on-call can
answer "did the customer's video reach Nahla?" with one query — no
need to grep production logs or correlate two API calls.

Routes
──────
* ``GET /admin/inbound/recent`` — returns the last ``limit`` inbound
  events (default 50, max 200), interleaved by ``created_at``. Each
  entry exposes ``message_type``, ``normalized_type``, ``has_caption``,
  ``persisted``, ``drop_reason``, ``wa_message_id``, ``mime`` so the
  caller can render a triage table without further joins.

Auth
────
``require_admin`` — same policy as ``admin_ai_quality.py``.

Privacy
───────
Phone numbers in inbound rows pass through ``mask_phone`` before being
returned, matching the masking policy of ``ai_quality_events``. The
raw E.164 stays inside the database.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from core.ai_quality_events import mask_phone
from core.auth import require_admin
from core.database import get_db
from core.inbound_observability import (
    CATEGORY_INBOUND_DROP,
    CATEGORY_MEDIA_FAILURE,
    CATEGORY_WEBHOOK_ROUTING,
)
from database.models import AiQualityEvent, MessageEvent

logger = logging.getLogger("nahla.admin.inbound_debug")

router = APIRouter(tags=["Admin · Inbound Debug"])


_INBOUND_DROP_CATEGORIES = (
    CATEGORY_INBOUND_DROP,
    CATEGORY_MEDIA_FAILURE,
    CATEGORY_WEBHOOK_ROUTING,
)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_DEFAULT_LOOKBACK_HOURS = 24

# Saudi Arabia is UTC+3 year-round (no DST). Pinning to a fixed offset
# avoids the pitfall of zoneinfo not being installed on some Python
# builds (Windows default lacks the IANA database). The merchant's
# specification — surface BOTH UTC and KSA timestamps so 24/25 May
# events can never be confused — only requires a stable offset.
KSA_UTC_OFFSET = timezone(timedelta(hours=3), name="Asia/Riyadh")

_TABLE_MESSAGE_EVENT  = "message_events"
_TABLE_AI_QUALITY     = "ai_quality_events"


def _to_utc_iso(value: Optional[datetime]) -> str:
    """Render ``value`` as an ISO-8601 UTC string, never naive."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _to_ksa_iso(value: Optional[datetime]) -> str:
    """Render ``value`` in KSA local time so on-call can compare a
    customer-reported timestamp ("الساعة ٢:٠٦ مساءً") against the log
    without mental UTC arithmetic."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KSA_UTC_OFFSET).isoformat()


def _summarise_message_event(row: MessageEvent) -> Dict[str, Any]:
    """Project a MessageEvent into a triage-friendly dict.

    The dashboard uses ``persisted=True`` to indicate the row reached
    the conversation table; ``drop_reason`` is non-empty only for the
    placeholder rows the May 2026 #41 fix writes when the brain was
    skipped.

    May 2026 #42 — surface BOTH UTC and KSA timestamps + the source
    table so triage never confuses 24 May (UTC) with 25 May (KSA).
    """
    meta = row.extra_metadata or {}
    norm_inbound = meta.get("normalized_inbound") or {}
    drop_reason = (
        meta.get("drop_reason")
        or ("media_fallback" if meta.get("media_fallback") else "")
        or ("media_persist_only" if meta.get("media_persist_only") else "")
        or ""
    )
    body = (row.body or "")[:120]
    return {
        "id":               row.id,
        "kind":             "message_event",
        "source_table":     _TABLE_MESSAGE_EVENT,
        "tenant_id":        row.tenant_id,
        "conversation_id":  row.conversation_id,
        "direction":        row.direction or "",
        "body_preview":     body,
        "wa_message_id":    meta.get("wa_message_id") or "",
        "normalized_type":  norm_inbound.get("source_type") or "",
        "mime":             norm_inbound.get("mime_type") or "",
        "has_caption":      bool((norm_inbound.get("caption") or "").strip()),
        "media_id":         norm_inbound.get("media_id") or "",
        "transcript_status": norm_inbound.get("transcript_status") or "",
        "drop_reason":      drop_reason,
        "persisted":        True,
        "created_at":              _to_utc_iso(row.created_at),
        "event_created_at_utc":    _to_utc_iso(row.created_at),
        "event_created_at_ksa":    _to_ksa_iso(row.created_at),
    }


def _summarise_drop_event(row: AiQualityEvent) -> Dict[str, Any]:
    """Project an AiQualityEvent (inbound categories only) into the
    same triage shape as :func:`_summarise_message_event`.

    ``persisted`` is set to ``None`` on purpose: the drop event itself
    does not know whether ``_persist_inbound_only`` later wrote a
    placeholder row. The dashboard correlates by ``wa_message_id`` to
    decide what to render.
    """
    return {
        "id":               row.id,
        "kind":             "drop_event",
        "source_table":     _TABLE_AI_QUALITY,
        "tenant_id":        row.tenant_id,
        "conversation_id":  row.conversation_id,
        "direction":        "inbound",
        "body_preview":     (row.inbound_preview or "")[:120],
        "wa_message_id":    "",
        "normalized_type":  (row.chosen_path or "").split("=", 1)[-1] if row.chosen_path else "",
        "mime":             "",
        "has_caption":      False,
        "media_id":         "",
        "transcript_status": "",
        "drop_reason":      row.mismatch_type or "",
        "persisted":        None,
        "created_at":              _to_utc_iso(row.created_at),
        "event_created_at_utc":    _to_utc_iso(row.created_at),
        "event_created_at_ksa":    _to_ksa_iso(row.created_at),
        "category":         row.category or "",
        "detail":           (row.mismatch_reason or "")[:300],
        "phone_masked":     row.customer_phone_masked or "",
    }


@router.get(
    "/admin/inbound/recent",
    summary="Last inbound webhook events for triage (May 2026 #41)",
    dependencies=[Depends(require_admin)],
)
def list_recent_inbound_events(
    db: Session = Depends(get_db),
    tenant_id: Optional[int] = Query(
        None, description="Scope to a single tenant. Omit for platform-wide.",
    ),
    limit: int = Query(
        _DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT,
        description="Max rows per source (message_event + drop_event).",
    ),
    lookback_hours: int = Query(
        _DEFAULT_LOOKBACK_HOURS, ge=1, le=24 * 30,
        description=(
            "How far back to scan, in hours. Lookback window starts "
            "from server NOW (UTC). Increase to 48-168 hours when a "
            "merchant reports an event from a previous calendar day."
        ),
    ),
) -> Dict[str, Any]:
    """Return the most recent inbound events for triage.

    The response carries TWO views correlated by timestamp:

      * ``message_events`` — actual rows in ``message_events`` table
        with ``direction='inbound'``. After May 2026 #41 every inbound
        webhook produces one of these (real text, AI-handled media,
        media-fallback courtesy reply, or persist-only placeholder).
        ``persisted=True`` always.

      * ``drop_events`` — rows in ``ai_quality_events`` for the
        inbound-side categories (``inbound_drop``, ``media_failure``,
        ``webhook_routing``). These are the "why no AI reply?"
        sidecars. ``persisted=None`` because the drop row alone
        cannot tell us whether the message ALSO got a placeholder
        MessageEvent (the dashboard correlates by ``wa_message_id``).

    Use the response to answer:

      * "Did the customer's video at 1:05 PM reach Nahla?"  → search
        ``message_events`` by sender + timestamp.
      * "Why didn't Nahla reply to it?"                     → search
        ``drop_events`` for the same window for the reason.
      * "Are we losing any messages silently?"              → expect
        every webhook turn to surface in ``message_events`` after
        the fix; if not, escalate to engineering.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=int(lookback_hours))

    me_query = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.direction == "inbound",
            MessageEvent.created_at >= since,
        )
        .order_by(desc(MessageEvent.created_at))
    )
    if tenant_id is not None:
        me_query = me_query.filter(MessageEvent.tenant_id == int(tenant_id))
    message_rows = me_query.limit(int(limit)).all()

    drop_query = (
        db.query(AiQualityEvent)
        .filter(
            AiQualityEvent.category.in_(_INBOUND_DROP_CATEGORIES),
            AiQualityEvent.created_at >= since,
        )
        .order_by(desc(AiQualityEvent.created_at))
    )
    if tenant_id is not None:
        drop_query = drop_query.filter(AiQualityEvent.tenant_id == int(tenant_id))
    drop_rows = drop_query.limit(int(limit)).all()

    now_utc = datetime.now(timezone.utc)
    return {
        "since":              since.isoformat(),
        "since_utc":          _to_utc_iso(since),
        "since_ksa":          _to_ksa_iso(since),
        "now_utc":            _to_utc_iso(now_utc),
        "now_ksa":            _to_ksa_iso(now_utc),
        "lookback_hours":     int(lookback_hours),
        "ksa_utc_offset":     "+03:00",
        "tenant_id":          tenant_id,
        "limit":              limit,
        "message_events":     [_summarise_message_event(r) for r in message_rows],
        "drop_events":        [_summarise_drop_event(r) for r in drop_rows],
        "counts": {
            "message_events": len(message_rows),
            "drop_events":    len(drop_rows),
        },
    }
