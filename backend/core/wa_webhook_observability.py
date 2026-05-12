"""
core/wa_webhook_observability.py
────────────────────────────────
In-memory ring buffer of recent 360dialog webhook events
(BOTH routed and unrouted), used by the read-only admin endpoint:

    GET /admin/debug/recent-webhook-events?tenant_id=<id>&minutes=30

Why this exists
───────────────
The 360dialog webhook handler (``_handle_360dialog_body``) has six
silent drop points that look identical from outside Railway logs:

  1. metadata.phone_number_id is empty / missing
  2. no WhatsAppConnection matches phone_number_id
  3. multiple connections match the same phone_number_id
     (ambiguous — dropped to prevent cross-tenant leak)
  4. matched connection's provider is not dialog360
  5. shared secret missing / mismatched (X-Nahla-Coexistence-Secret)
  6. scope mismatch — coexistence event arrived on channel URL etc.
     (recorded on the connection but not processed downstream)

All six look like "webhook accepted (HTTP 200)" to 360dialog, but
the merchant never sees the message in their inbox. Pre-F19 there
was no way to tell from outside the logs which case bit.

This module captures every incoming 360dialog event into a global
ring buffer (5xx events, FIFO eviction) tagged with the route
outcome. The admin endpoint then renders the recent history filtered
by tenant_id (matched), phone_number_id (raw), and time window.

What's stored
─────────────
* ts (unix), scope, field
* phone_number_id_from_payload, display_phone_number
* matched_connection_id / matched_tenant_id /
  matched_phone_number_id (the connection's stored
  phone_number_id, which may differ from the payload's — that's the
  drift case we hunt)
* route_status: ``matched`` / ``unrouted_unknown_phone_id`` /
  ``unrouted_missing_phone_id`` / ``unrouted_ambiguous`` /
  ``unrouted_wrong_provider`` / ``unrouted_bad_secret`` /
  ``scope_mismatch``
* secret_check: ``ok`` / ``mismatch`` / ``not_required``
* messages_count, statuses_count, echoes_count, coex_events_count
  (so the operator sees "an inbound was on this delivery but it
   was dropped")
* error_text on exception during routing

What's NOT stored
─────────────────
* The actual message bodies. We surface counts and metadata only —
  this is a routing diagnostic, not a message inspector.
* The shared secret value (only ``secret_check`` outcome).

Thread-safety
─────────────
Mutations go through a single ``threading.Lock``. The webhook
handler runs as a fire-and-forget task spawned via
``spawn_background`` — multiple tasks can land in the recorder
concurrently.

Process-local
─────────────
Wiped on every Railway redeploy. This is a "what just happened?"
tool, not an audit log.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

# ── Tunables ───────────────────────────────────────────────────────
_MAX_EVENTS_TOTAL = 500
_MAX_STR_FIELD_BYTES = 2048


# ── Route status codes (stable strings the dashboard switches on)
ROUTE_MATCHED                   = "matched"
ROUTE_UNROUTED_MISSING_PHONE_ID = "unrouted_missing_phone_id"
ROUTE_UNROUTED_UNKNOWN_PHONE_ID = "unrouted_unknown_phone_id"
ROUTE_UNROUTED_AMBIGUOUS        = "unrouted_ambiguous"
ROUTE_UNROUTED_WRONG_PROVIDER   = "unrouted_wrong_provider"
ROUTE_UNROUTED_BAD_SECRET       = "unrouted_bad_secret"
ROUTE_SCOPE_MISMATCH            = "scope_mismatch"
ROUTE_EXCEPTION                 = "exception"

SECRET_OK            = "ok"
SECRET_MISMATCH      = "mismatch"
SECRET_NOT_REQUIRED  = "not_required"


# ── State (process-local) ──────────────────────────────────────────
_LOCK: threading.Lock = threading.Lock()
_EVENTS: Deque[Dict[str, Any]] = deque(maxlen=_MAX_EVENTS_TOTAL)


# ── Helpers ────────────────────────────────────────────────────────

def _truncate(value: Any, *, max_bytes: int = _MAX_STR_FIELD_BYTES) -> Any:
    """Bound any single string field so a misbehaving caller can't
    OOM us with a huge ``error_text``. Dicts / lists / numbers /
    None pass through unchanged."""
    if isinstance(value, str) and len(value) > max_bytes:
        return value[:max_bytes] + f"... ({len(value)} bytes truncated)"
    return value


# ── Public: write side (called by _handle_360dialog_body) ──────────

def record_event(
    *,
    scope:                          str,
    field:                          str,
    phone_number_id_from_payload:   Optional[str],
    display_phone_number:           Optional[str],
    matched_tenant_id:              Optional[int],
    matched_connection_id:          Optional[int],
    matched_phone_number_id:        Optional[str],
    route_status:                   str,
    secret_check:                   Optional[str] = None,
    messages_count:                 int = 0,
    statuses_count:                 int = 0,
    echoes_count:                   int = 0,
    coex_events_count:              int = 0,
    candidate_tenant_ids:           Optional[List[int]] = None,
    candidate_connection_ids:       Optional[List[int]] = None,
    error_text:                     Optional[str] = None,
) -> None:
    """Append one routing decision to the global ring. Never raises:
    an observability bug must never break a real webhook delivery.

    ``matched_phone_number_id`` is the phone_number_id stored on the
    connection that we resolved to — it may differ from
    ``phone_number_id_from_payload`` when the merchant reconnected
    the channel under a different phone_id. That drift is the
    primary mystery this module exists to surface.
    """
    try:
        entry = {
            "ts":                            time.time(),
            "scope":                         scope,
            "field":                         field,
            "phone_number_id_from_payload":  phone_number_id_from_payload,
            "display_phone_number":          display_phone_number,
            "matched_tenant_id":             matched_tenant_id,
            "matched_connection_id":         matched_connection_id,
            "matched_phone_number_id":       matched_phone_number_id,
            "route_status":                  route_status,
            "secret_check":                  secret_check,
            "messages_count":                int(messages_count or 0),
            "statuses_count":                int(statuses_count or 0),
            "echoes_count":                  int(echoes_count or 0),
            "coex_events_count":             int(coex_events_count or 0),
            "candidate_tenant_ids":          candidate_tenant_ids or [],
            "candidate_connection_ids":      candidate_connection_ids or [],
            "error_text":                    _truncate(error_text),
            "phone_id_mismatch": bool(
                phone_number_id_from_payload
                and matched_phone_number_id
                and str(phone_number_id_from_payload) != str(matched_phone_number_id)
            ),
        }
        with _LOCK:
            _EVENTS.append(entry)
    except Exception:
        # Observability must never break a real send. Swallow.
        pass


# ── Public: read side (called by the admin endpoint) ───────────────

def get_recent_events(
    *,
    tenant_id:        Optional[int] = None,
    phone_number_id:  Optional[str] = None,
    minutes:          int = 30,
    include_unrouted: bool = True,
    limit:            int = 200,
) -> List[Dict[str, Any]]:
    """Return events newest first, filtered by:

    * ``tenant_id``       — keep events where ``matched_tenant_id``
                            equals this. Unrouted events
                            (``matched_tenant_id is None``) are
                            included when ``include_unrouted=True``
                            so the operator can see "messages came in
                            but landed nowhere".
    * ``phone_number_id`` — keep events whose
                            ``phone_number_id_from_payload`` OR
                            ``matched_phone_number_id`` equals this.
                            Useful when the operator only knows the
                            number from the merchant's panel.
    * ``minutes``         — sliding window. Default 30; max bounded
                            by the global ring's capacity.
    * ``limit``           — cap returned rows so a busy tenant
                            doesn't shovel hundreds of records.

    Returns ``[]`` on any error or empty buffer — never raises.
    """
    if limit <= 0:
        return []
    cutoff_ts = time.time() - max(0, int(minutes) * 60)
    with _LOCK:
        snap = list(_EVENTS)

    out: List[Dict[str, Any]] = []
    for entry in reversed(snap):  # newest first
        if entry.get("ts", 0) < cutoff_ts:
            continue
        if tenant_id is not None:
            matched_t = entry.get("matched_tenant_id")
            if matched_t == tenant_id:
                # accept
                pass
            elif include_unrouted and matched_t is None:
                # accept — unrouted bucket
                pass
            else:
                continue
        if phone_number_id is not None:
            pid = str(phone_number_id)
            if (
                str(entry.get("phone_number_id_from_payload") or "") != pid
                and str(entry.get("matched_phone_number_id") or "") != pid
            ):
                continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def get_distinct_payload_phone_ids(*, minutes: int = 60) -> Dict[str, int]:
    """Return ``{phone_number_id_from_payload: count}`` across the
    window. The dashboard uses this to detect "you're getting webhook
    deliveries with TWO different phone_number_ids" — the exact
    failure mode the merchant in 2026-05-12 is hitting.
    """
    cutoff_ts = time.time() - max(0, int(minutes) * 60)
    out: Dict[str, int] = {}
    with _LOCK:
        snap = list(_EVENTS)
    for e in snap:
        if e.get("ts", 0) < cutoff_ts:
            continue
        pid = str(e.get("phone_number_id_from_payload") or "")
        if not pid:
            continue
        out[pid] = out.get(pid, 0) + 1
    return out


def get_route_status_counts(
    *,
    tenant_id:       Optional[int] = None,
    phone_number_id: Optional[str] = None,
    minutes:         int = 30,
) -> Dict[str, int]:
    """Aggregate count of events per ``route_status`` value in the
    window. Useful for the dashboard's red/green summary tile."""
    events = get_recent_events(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        minutes=minutes,
        include_unrouted=True,
        limit=_MAX_EVENTS_TOTAL,
    )
    counts: Dict[str, int] = {}
    for e in events:
        rs = e.get("route_status") or "unknown"
        counts[rs] = counts.get(rs, 0) + 1
    return counts


def reset_for_tests() -> None:
    """Clear the global ring buffer. Test-only — not imported from
    outside ``tests/``."""
    with _LOCK:
        _EVENTS.clear()
