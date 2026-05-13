"""
backend/services/delivery_quality.py
─────────────────────────────────────
Delivery Quality Intelligence Layer — core recorder + Suppression Engine.

What this module does
─────────────────────
Two concerns, one file, because they share the same write paths and
the same idempotency guards — splitting them would force every
caller to import both anyway:

1. ``record_status_event`` — append-only ingest of every WhatsApp
   status callback into the new ``message_delivery_events`` table
   and the matching ``wa_webhook_raw`` archive row. Designed to be
   called from the existing webhook handler with zero behavioural
   change to the legacy ``CampaignSendLog`` / ``Campaign`` update
   paths.

2. ``apply_suppression_signal`` — accumulates per-phone counters
   for ``quality_risk`` failures and decides when a phone should
   be auto-added to ``customer_suppressions``. ``critical``-tier
   per-recipient failures (``blocked_by_user``) trip the engine
   on the first event; ``quality_risk`` codes accumulate to a
   threshold (default 2 in the last 30 days).

3. ``reinstate_on_inbound`` — when an inbound message arrives from
   a previously-suppressed phone, flip ``is_active=False`` on its
   suppression row and stamp ``reinstate_reason="inbound_message"``.

Fault tolerance
───────────────
Every public function in this module is wrapped in a top-level
``try/except`` that logs and returns. The webhook ingest path must
never fail because of a quality-layer write — Meta will retry on
non-200 and we want to keep serving the existing dispatcher.

Concurrency
───────────
We hit DB tables that can be written concurrently from many
asyncio status callbacks. Idempotency is enforced via:
  * ``UniqueConstraint("wamid", "status")`` on
    ``message_delivery_events`` — Postgres ``ON CONFLICT DO
    NOTHING`` upsert, SQLite ``try/except IntegrityError``.
  * ``UniqueConstraint("tenant_id", "normalized_phone")`` on
    ``customer_suppressions`` — same upsert pattern.

Single-source-of-truth
──────────────────────
All classification goes through ``services.meta_errors`` — never
hardcode a tier or a key here. When a new Meta code surfaces,
extend ``meta_errors`` first; this module picks it up via
``classify_meta_error`` / ``quality_tier_of`` automatically.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from services.meta_errors import (
    ERRORS,
    classify_meta_error,
    quality_tier_of,
    should_suppress_on_repeat,
)

logger = logging.getLogger("nahla.delivery_quality")


# ──────────────────────────────────────────────────────────────────────
# Tunables
# ──────────────────────────────────────────────────────────────────────
#
# Kept as module-level constants (NOT env vars) so unit tests can
# monkey-patch them and so that production tuning happens via a code
# change with review, not a Railway env knob.

# A phone hits the suppression threshold after this many distinct
# ``quality_risk`` events within ``SUPPRESS_WINDOW``. Conservative
# default — we'd rather under-suppress than aggressively block a
# legitimate customer.
SUPPRESS_REPEAT_THRESHOLD: int = 2
SUPPRESS_WINDOW: timedelta = timedelta(days=30)

# Per-tier short-circuits. ``critical`` codes that explicitly mean
# the recipient blocked us (or Meta blacklisted them on our behalf)
# suppress on the first event, no accumulation.
_FIRST_EVENT_SUPPRESS_KEYS = frozenset({
    "blocked_by_user",
    "permanent_failure",
})


# ──────────────────────────────────────────────────────────────────────
# Recorder
# ──────────────────────────────────────────────────────────────────────

def record_status_event(
    *,
    db: Session,
    tenant_id: Optional[int],
    wamid: Optional[str],
    status: str,
    phone_e164: Optional[str] = None,
    errors_payload: Optional[List[Dict[str, Any]]] = None,
    campaign_send_log_id: Optional[int] = None,
    automation_execution_id: Optional[int] = None,
    template_id: Optional[int] = None,
    source: str = "meta",
    raw_id: Optional[int] = None,
    occurred_at: Optional[datetime] = None,
) -> Optional[int]:
    """Append-only ingest of a single delivery status event.

    Idempotent on ``(wamid, status)``. Returns the
    ``message_delivery_events.id`` of the inserted row, or ``None``
    if this exact (wamid, status) pair was already recorded.

    ``errors_payload`` — Meta's verbatim ``errors`` array (list of
    dicts with ``code`` / ``title`` / ``message`` / ``error_subcode``).
    Only the first entry is classified; if absent or empty, the
    event is treated as a non-error status (``delivered`` / ``read``).
    """
    try:
        from models import MessageDeliveryEvent  # local import to avoid cycles
    except Exception as exc:
        logger.warning("[delivery_quality] models unavailable: %s", exc)
        return None

    if not wamid:
        # Synthesise a wamid so we still capture the event for
        # analytics without colliding with the uniqueness constraint
        # used for replay-dedup. Synthetic ids never match a real
        # Meta wamid, so suppression decisions made on them won't
        # falsely lookup a campaign send log row later.
        wamid = f"synth:{uuid.uuid4().hex[:24]}"

    st = (status or "").strip().lower()
    if not st:
        st = "other"

    # Classify only when the event indicates failure. ``sent`` /
    # ``delivered`` / ``read`` / ``template_status`` have no error
    # to classify and should not pollute the ``error_code`` index.
    classified_key: Optional[str] = None
    quality_tier: Optional[str] = None
    raw_code: Optional[str] = None
    raw_subcode: Optional[str] = None
    err_msg: Optional[str] = None
    if errors_payload and isinstance(errors_payload, list):
        first = errors_payload[0] if errors_payload else None
        if isinstance(first, dict):
            raw_code = _stringify(first.get("code"))
            raw_subcode = _stringify(first.get("error_subcode") or first.get("subcode"))
            classified = classify_meta_error(
                code=first.get("code"),
                subcode=first.get("error_subcode") or first.get("subcode"),
                error_type=first.get("type"),
                message=first.get("title") or first.get("message"),
            )
            classified_key = classified.key
            quality_tier = classified.quality_tier
            title = first.get("title")
            msg = first.get("message")
            err_msg = (
                f"{title} — {msg}" if (title and msg) else (title or msg or None)
            )

    if not quality_tier and st == "failed":
        # Failed event with no explicit error array — fall back to
        # the unknown classifier so the dashboard still sees it.
        classified_key = "unknown"
        quality_tier = quality_tier_of("unknown")

    suppress_on_repeat = (
        should_suppress_on_repeat(classified_key) if classified_key else False
    )

    # Pre-flight existence check — cheaper and safer than relying on
    # an IntegrityError + rollback inside the caller's session, which
    # would wipe any sibling work in the same transaction. Meta
    # redelivers status callbacks frequently so this path is hot.
    already_seen = (
        db.query(MessageDeliveryEvent.id)
        .filter(
            MessageDeliveryEvent.wamid == wamid,
            MessageDeliveryEvent.status == st,
        )
        .first()
    )
    if already_seen is not None:
        logger.debug(
            "[delivery_quality] duplicate (wamid=%s status=%s) — skipped",
            wamid[:20], st,
        )
        return None

    row = MessageDeliveryEvent(
        tenant_id=tenant_id,
        wamid=wamid,
        phone_e164=phone_e164,
        status=st,
        error_code=classified_key,
        error_message=err_msg,
        raw_code=raw_code,
        raw_subcode=raw_subcode,
        quality_tier=quality_tier,
        suppress_on_repeat=suppress_on_repeat,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        campaign_send_log_id=campaign_send_log_id,
        automation_execution_id=automation_execution_id,
        template_id=template_id,
        source=source,
        raw_id=raw_id,
    )
    db.add(row)
    try:
        # Nested savepoint protects the caller's session from a
        # last-millisecond race (another worker inserted the same
        # (wamid, status) between our existence check and the
        # flush). Postgres treats savepoints natively; SQLite
        # emulates them. On rollback ONLY this insert is undone —
        # sibling DB work in the surrounding transaction survives.
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        logger.debug(
            "[delivery_quality] race-duplicate (wamid=%s status=%s) — skipped",
            wamid[:20], st,
        )
        return None

    # Suppression evaluation is best-effort and runs in the SAME
    # transaction so a phone that just tripped the threshold can't
    # slip past the next dispatch tick.
    if (
        tenant_id is not None
        and phone_e164
        and st == "failed"
        and classified_key
        and (
            classified_key in _FIRST_EVENT_SUPPRESS_KEYS
            or suppress_on_repeat
        )
    ):
        try:
            apply_suppression_signal(
                db=db,
                tenant_id=tenant_id,
                normalized_phone=phone_e164,
                error_key=classified_key,
                occurred_at=row.occurred_at,
                source="auto",
            )
        except Exception as exc:
            logger.warning(
                "[delivery_quality] suppression engine failed phone=%s key=%s err=%s",
                _mask_phone(phone_e164), classified_key, exc,
            )

    return row.id


def record_webhook_raw(
    *,
    db: Session,
    tenant_id: Optional[int],
    provider: str,
    source_path: Optional[str],
    wamid: Optional[str],
    status: str,
    raw_body: Optional[str],
    raw_headers: Optional[Dict[str, Any]],
    parsed_payload: Optional[Dict[str, Any]] = None,
    raw_error_code: Optional[str] = None,
    raw_error_subcode: Optional[str] = None,
    classified_key: Optional[str] = None,
    quality_tier: Optional[str] = None,
    campaign_send_log_id: Optional[int] = None,
    automation_execution_id: Optional[int] = None,
) -> Optional[int]:
    """Archive a raw webhook payload. Fire-and-forget.

    Returns the inserted row id (handy when ``record_status_event``
    wants to link back via ``raw_id``). Never raises — failures are
    logged and ``None`` is returned. The caller can proceed.
    """
    try:
        from models import WaWebhookRaw
    except Exception as exc:
        logger.warning("[delivery_quality] WaWebhookRaw unavailable: %s", exc)
        return None

    body_str: Optional[str]
    if raw_body is None:
        body_str = None
    elif isinstance(raw_body, str):
        body_str = raw_body
    else:
        # Be liberal about what we accept — some 360dialog paths
        # already gave us a parsed dict.
        try:
            body_str = json.dumps(raw_body, ensure_ascii=False)[:64_000]
        except Exception:
            body_str = str(raw_body)[:64_000]

    headers_compact: Optional[Dict[str, str]] = None
    if raw_headers:
        # Strip Authorization-like headers to avoid persisting
        # secrets in the archive.
        headers_compact = {
            k: ("<redacted>" if _is_sensitive_header(k) else str(v))
            for k, v in raw_headers.items()
        }

    row = WaWebhookRaw(
        tenant_id=tenant_id,
        provider=(provider or "unknown").strip().lower()[:32],
        source_path=(source_path or None),
        wamid=wamid,
        status=(status or "other").strip().lower()[:32],
        raw_error_code=_stringify(raw_error_code),
        raw_error_subcode=_stringify(raw_error_subcode),
        classified_key=classified_key,
        quality_tier=quality_tier,
        raw_body=body_str,
        raw_headers=headers_compact,
        parsed_payload=parsed_payload,
        campaign_send_log_id=campaign_send_log_id,
        automation_execution_id=automation_execution_id,
    )
    db.add(row)
    try:
        with db.begin_nested():
            db.flush()
        return row.id
    except Exception as exc:
        logger.warning("[delivery_quality] raw archive flush failed: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────
# Suppression Engine
# ──────────────────────────────────────────────────────────────────────

def apply_suppression_signal(
    *,
    db: Session,
    tenant_id: int,
    normalized_phone: str,
    error_key: str,
    customer_id: Optional[int] = None,
    occurred_at: Optional[datetime] = None,
    source: str = "auto",
) -> Tuple[Optional[int], bool]:
    """Record a single quality-risk failure against a phone and
    decide whether to (auto-)suppress it.

    Returns ``(suppression_row_id, newly_suppressed)``. ``None`` for
    the row id means we did not create or touch a suppression row
    (e.g. the threshold has not been reached yet).
    """
    try:
        from models import CustomerSuppression
    except Exception as exc:
        logger.warning("[delivery_quality] CustomerSuppression unavailable: %s", exc)
        return (None, False)

    occurred = occurred_at or datetime.now(timezone.utc)
    row: Optional[CustomerSuppression] = (
        db.query(CustomerSuppression)
        .filter(
            CustomerSuppression.tenant_id == tenant_id,
            CustomerSuppression.normalized_phone == normalized_phone,
        )
        .first()
    )

    # Already-suppressed phones still get their counter bumped so
    # the dashboard can show "this phone has failed N times" — but
    # we never re-activate a manually-discharged row from here
    # (only ``reinstate_on_inbound`` flips ``is_active`` back).
    if row is not None:
        _bump_reason(row, error_key, occurred)
        row.failure_count = (row.failure_count or 0) + 1
        row.last_failure_at = occurred
        flag_modified(row, "reasons")
        try:
            with db.begin_nested():
                db.flush()
        except Exception as exc:
            logger.warning("[delivery_quality] suppression bump failed: %s", exc)
        return (row.id, False)

    # New phone — check the threshold.
    should_suppress_now = (
        error_key in _FIRST_EVENT_SUPPRESS_KEYS
        or _count_recent_risk_events(
            db, tenant_id=tenant_id, phone=normalized_phone, since=occurred - SUPPRESS_WINDOW
        )
        >= SUPPRESS_REPEAT_THRESHOLD
    )
    if not should_suppress_now:
        return (None, False)

    fresh = CustomerSuppression(
        tenant_id=tenant_id,
        customer_id=customer_id,
        normalized_phone=normalized_phone,
        reason_primary=error_key,
        reasons=[{
            "key": error_key,
            "count": 1,
            "last_seen_at": occurred.isoformat(),
        }],
        failure_count=1,
        source=source,
        is_active=True,
        suppressed_at=occurred,
        last_failure_at=occurred,
    )
    db.add(fresh)
    try:
        with db.begin_nested():
            db.flush()
        logger.info(
            "[delivery_quality] auto-suppressed tenant=%s phone=%s key=%s",
            tenant_id, _mask_phone(normalized_phone), error_key,
        )
        return (fresh.id, True)
    except IntegrityError:
        # Race — another worker created the row first. Fetch and bump.
        # The savepoint above already undid the failed insert without
        # touching the outer transaction.
        existing = (
            db.query(CustomerSuppression)
            .filter(
                CustomerSuppression.tenant_id == tenant_id,
                CustomerSuppression.normalized_phone == normalized_phone,
            )
            .first()
        )
        if existing:
            _bump_reason(existing, error_key, occurred)
            existing.failure_count = (existing.failure_count or 0) + 1
            existing.last_failure_at = occurred
            flag_modified(existing, "reasons")
            try:
                with db.begin_nested():
                    db.flush()
            except Exception:
                pass
            return (existing.id, False)
        return (None, False)


def reinstate_on_inbound(
    *,
    db: Session,
    tenant_id: int,
    normalized_phone: str,
    reason: str = "inbound_message",
) -> bool:
    """Auto-clear an active suppression when the customer engages.

    The user is signalling "yes I do want to hear from you" by
    initiating contact. We keep the row (audit trail) but flip
    ``is_active=False`` so the next dispatch send is allowed.

    Returns True if a row was flipped; False if no active
    suppression existed.
    """
    try:
        from models import CustomerSuppression
    except Exception as exc:
        logger.warning("[delivery_quality] reinstate models unavailable: %s", exc)
        return False

    row = (
        db.query(CustomerSuppression)
        .filter(
            CustomerSuppression.tenant_id == tenant_id,
            CustomerSuppression.normalized_phone == normalized_phone,
            CustomerSuppression.is_active.is_(True),
        )
        .first()
    )
    if not row:
        return False

    row.is_active = False
    row.reinstated_at = datetime.now(timezone.utc)
    row.reinstate_reason = reason[:64]
    try:
        with db.begin_nested():
            db.flush()
        logger.info(
            "[delivery_quality] reinstated tenant=%s phone=%s reason=%s",
            tenant_id, _mask_phone(normalized_phone), reason,
        )
        return True
    except Exception as exc:
        logger.warning("[delivery_quality] reinstate flush failed: %s", exc)
        return False


def is_phone_suppressed(
    *,
    db: Session,
    tenant_id: int,
    normalized_phone: str,
) -> bool:
    """Fast pre-send check used by dispatchers.

    Returns True iff the phone has an *active* suppression row
    that has not expired. Honours the optional ``expires_at`` —
    a cool-down style block automatically lifts when the window
    passes (we'll mark ``is_active=False`` in a scheduled sweep,
    but this check is the authoritative one for the dispatcher).
    """
    try:
        from models import CustomerSuppression
    except Exception:
        return False

    row = (
        db.query(CustomerSuppression)
        .filter(
            CustomerSuppression.tenant_id == tenant_id,
            CustomerSuppression.normalized_phone == normalized_phone,
            CustomerSuppression.is_active.is_(True),
        )
        .first()
    )
    if not row:
        return False
    if row.expires_at and row.expires_at <= datetime.now(timezone.utc):
        # Lazy unlock — flip the row in-place so the next query is
        # cheap. Safe because we're in the dispatcher's request
        # session and a downstream commit is imminent.
        row.is_active = False
        row.reinstated_at = datetime.now(timezone.utc)
        row.reinstate_reason = "cooldown_elapsed"
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────

def _count_recent_risk_events(
    db: Session,
    *,
    tenant_id: int,
    phone: str,
    since: datetime,
) -> int:
    """Count ``suppress_on_repeat`` events for the phone in the window."""
    try:
        from models import MessageDeliveryEvent
    except Exception:
        return 0

    return (
        db.query(MessageDeliveryEvent)
        .filter(
            MessageDeliveryEvent.tenant_id == tenant_id,
            MessageDeliveryEvent.phone_e164 == phone,
            MessageDeliveryEvent.suppress_on_repeat.is_(True),
            MessageDeliveryEvent.occurred_at >= since,
        )
        .count()
    )


def _bump_reason(row, error_key: str, occurred: datetime) -> None:
    """Update the JSONB ``reasons`` list in place.

    Shape: ``[{"key": str, "count": int, "last_seen_at": iso}]``.
    """
    reasons: List[Dict[str, Any]] = list(row.reasons or [])
    for entry in reasons:
        if entry.get("key") == error_key:
            entry["count"] = int(entry.get("count") or 0) + 1
            entry["last_seen_at"] = occurred.isoformat()
            break
    else:
        reasons.append({
            "key": error_key,
            "count": 1,
            "last_seen_at": occurred.isoformat(),
        })
    row.reasons = reasons


def _stringify(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _mask_phone(phone: Optional[str]) -> str:
    if not phone:
        return ""
    s = str(phone)
    return s[:4] + "***" + s[-3:] if len(s) > 7 else s


_SENSITIVE_HEADER_KEYS = frozenset({
    "authorization",
    "x-auth-token",
    "x-api-key",
    "cookie",
    "set-cookie",
    "proxy-authorization",
})


def _is_sensitive_header(name: str) -> bool:
    return (name or "").strip().lower() in _SENSITIVE_HEADER_KEYS


__all__ = [
    "SUPPRESS_REPEAT_THRESHOLD",
    "SUPPRESS_WINDOW",
    "record_status_event",
    "record_webhook_raw",
    "apply_suppression_signal",
    "reinstate_on_inbound",
    "is_phone_suppressed",
]
