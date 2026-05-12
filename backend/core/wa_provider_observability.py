"""
core/wa_provider_observability.py
─────────────────────────────────
In-memory ring buffer of recent outbound WhatsApp provider attempts
(Meta + 360dialog), used by the read-only debug endpoint:

    GET /admin/debug/last-provider-send?tenant_id=<id>

Why this lives in core/ rather than at the wire layer
─────────────────────────────────────────────────────
The wire layer (``services.whatsapp_platform.service``) writes
attempts; the admin endpoint (``routers.admin_debug``) reads them.
Putting the ring buffer here decouples those modules — neither one
imports the other.

What's stored
─────────────
The last ``_MAX_ATTEMPTS_PER_TENANT`` attempts per tenant, plus a
global cap of ``_MAX_TENANTS`` tenants. The buffer is intentionally
size-bounded — this is a *recent activity* tool, not an audit log.
After a process restart everything is gone (Railway redeploy =
clean slate).

What's NOT stored
─────────────────
The actual API key / bearer token. We store
``token_source`` and ``token_tail`` (last 4 chars) only, and even
the tail is dropped from the response by ``admin_debug`` if the
caller is at risk of leaking it (it isn't — admin-only — but
defense in depth).

Thread-safety
─────────────
Mutations go through ``_LOCK`` (a ``threading.Lock``). The wire
layer is async but the underlying httpx response handling can fan
out across event-loop tasks; the lock makes the ring-buffer
appends safe across asyncio + worker threads.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

# ── Tunables ───────────────────────────────────────────────────────
# Keep small: this is observed traffic at the wire layer, every
# outbound message triggers one entry. A tenant sending 100 msgs/min
# rolls the buffer over every 30s — that's the right granularity for
# a "what just happened?" tool.
_MAX_ATTEMPTS_PER_TENANT = 50
# Hard cap so a misbehaving multi-tenant runtime can't OOM the worker.
_MAX_TENANTS             = 200
# Truncate any single field stored as a string to this length so a
# misbehaving caller can't OOM us via a multi-MB payload echo.
_MAX_STR_FIELD_BYTES     = 4096


# ── State (process-local) ──────────────────────────────────────────
_LOCK    = threading.Lock()
_BUFFERS: Dict[Optional[int], Deque[Dict[str, Any]]] = {}


# ── Public: classification codes ───────────────────────────────────
# Stable strings the debug endpoint surfaces in its response so the
# dashboard can switch on them without parsing free-form text.
CLASSIFICATION_OK              = "ok"
CLASSIFICATION_NON_2XX         = "non_2xx"
CLASSIFICATION_PROVIDER_ERROR  = "provider_error_field"
CLASSIFICATION_MISSING_WAMID   = "missing_wamid"
CLASSIFICATION_EXCEPTION       = "exception"


# ── Helpers ────────────────────────────────────────────────────────

def _truncate_for_log(value: Any, *, max_bytes: int = _MAX_STR_FIELD_BYTES) -> Any:
    """JSON-serialize-friendly truncation. We keep dicts/lists intact
    (the endpoint downstream uses their structure) and only trim
    strings or stringified payloads."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_bytes:
            return value
        return value[:max_bytes] + f"... ({len(value)} bytes truncated)"
    if isinstance(value, (dict, list)):
        # Don't deep-copy — caller passes us a fresh dict.
        try:
            serialized = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)[:max_bytes]
        if len(serialized) <= max_bytes:
            return value
        return {
            "_truncated":     True,
            "_original_size": len(serialized),
            "_preview":       serialized[:max_bytes],
        }
    return str(value)[:max_bytes]


def _mask_token_tail(token: Optional[str]) -> Optional[str]:
    """Return only the last 4 characters of a secret — same shape
    the admin_debug ``_mask_secret_tail`` helper uses, kept here to
    avoid pulling the router into the wire layer."""
    s = (token or "").strip()
    if not s:
        return None
    if len(s) <= 4:
        return "***"
    return "***" + s[-4:]


def _scrub_payload(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Mask the recipient phone number inside a copy of the outbound
    payload so a leaked log file / debug endpoint response doesn't
    expose customer phones in full."""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    if "to" in out:
        to_raw = str(out.get("to") or "")
        if len(to_raw) > 7:
            out["to"] = to_raw[:4] + "***" + to_raw[-3:]
        elif to_raw:
            out["to"] = "***"
    return out


# ── Public: write side (called by the wire layer) ──────────────────

def record_attempt(
    *,
    tenant_id:        Optional[int],
    operation:        str,
    provider:         str,
    method:           str,
    full_url:         str,
    path:             str,
    request_payload:  Optional[Dict[str, Any]],
    headers_summary:  Dict[str, Any],
    response_status:  Optional[int],
    response_body:    Any,
    parsed_wamid:     Optional[str],
    classification:   str,
    duration_ms:      Optional[float],
    error_text:       Optional[str] = None,
    connection_phone_number_id: Optional[str] = None,
    connection_id:    Optional[int] = None,
    connection_type:  Optional[str] = None,
) -> None:
    """Append one attempt to the per-tenant ring. Never raises:
    an observability bug must never break a real send."""
    try:
        entry = {
            "ts":                  time.time(),
            "operation":           operation,
            "tenant_id":           tenant_id,
            "provider":            provider,
            "method":              method,
            "full_url":            full_url,
            "path":                path,
            "request_payload":     _truncate_for_log(_scrub_payload(request_payload)),
            "headers_summary":     headers_summary,
            "response_status":     response_status,
            "response_body":       _truncate_for_log(response_body),
            "parsed_wamid":        parsed_wamid,
            "parsed_wamid_present": bool(parsed_wamid),
            "classification":      classification,
            "duration_ms":         (
                round(duration_ms, 2) if isinstance(duration_ms, (int, float)) else None
            ),
            "error_text":          _truncate_for_log(error_text),
            "connection_phone_number_id": connection_phone_number_id,
            "connection_id":              connection_id,
            "connection_type":            connection_type,
        }
        with _LOCK:
            buf = _BUFFERS.get(tenant_id)
            if buf is None:
                # Soft cap on total tenants tracked. If we're at the cap
                # evict the *least-recently-used* tenant. Implementation
                # is best-effort — picking the dict's first key suffices
                # because Python preserves insertion order.
                if len(_BUFFERS) >= _MAX_TENANTS:
                    try:
                        oldest = next(iter(_BUFFERS))
                        _BUFFERS.pop(oldest, None)
                    except StopIteration:
                        pass
                buf = deque(maxlen=_MAX_ATTEMPTS_PER_TENANT)
                _BUFFERS[tenant_id] = buf
            buf.append(entry)
    except Exception:
        # The wire layer must keep working even if our bookkeeping
        # explodes. Swallow.
        pass


# ── Public: read side (called by the admin endpoint) ───────────────

def get_recent_attempts(
    tenant_id: Optional[int],
    *,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return the most recent attempts for a tenant, newest first.
    Never raises; returns ``[]`` when the tenant has no entries
    (including immediately after a process restart)."""
    if limit <= 0:
        return []
    with _LOCK:
        buf = _BUFFERS.get(tenant_id)
        if not buf:
            return []
        # ``deque`` doesn't support negative indexing on a slice — we
        # materialise the snapshot then trim from the end.
        snap = list(buf)
    snap.reverse()
    return snap[:limit]


def reset_for_tests() -> None:
    """Clear the ring buffer. Test-only — not exported for prod
    callers. Live state cannot be flushed via this in production
    because nothing imports it from outside ``tests/``."""
    with _LOCK:
        _BUFFERS.clear()


# ── Public: header summarisation (called by the wire layer) ────────

def summarize_headers(
    headers: Optional[Dict[str, str]],
    *,
    token_source: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a sanitised snapshot of the headers we sent. We never
    return the raw secret — only the header NAME present plus the
    masked tail of the bearer / D360-API-KEY so support can verify
    the right key is being used without it leaking.

    ``token_source`` (``"merchant_oauth"`` / ``"platform"`` /
    ``"missing"``) comes from the WhatsAppTokenContext — we attach
    it here so the endpoint can render it next to the masked tail.
    """
    summary: Dict[str, Any] = {
        "token_source":          token_source,
        "auth_header_name":      None,
        "auth_header_tail":      None,
        "content_type":          None,
    }
    if not isinstance(headers, dict):
        return summary
    # Case-insensitive lookup — httpx may have normalised the casing.
    lowered = {k.lower(): v for k, v in headers.items()}
    if "d360-api-key" in lowered:
        summary["auth_header_name"] = "D360-API-KEY"
        summary["auth_header_tail"] = _mask_token_tail(lowered["d360-api-key"])
    elif "authorization" in lowered:
        summary["auth_header_name"] = "Authorization"
        raw = lowered["authorization"] or ""
        # Strip "Bearer " prefix so the masked tail compares with
        # what's stored on the connection row.
        token_part = raw.split(" ", 1)[-1] if raw else ""
        summary["auth_header_tail"] = _mask_token_tail(token_part)
    summary["content_type"] = lowered.get("content-type")
    return summary
