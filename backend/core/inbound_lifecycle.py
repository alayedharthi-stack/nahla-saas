"""
core/inbound_lifecycle.py
─────────────────────────
Wave 2.0 Phase 1 (May 2026) — Structured inbound-lifecycle telemetry.

Why this module exists
──────────────────────
Production audit during Eid season uncovered a class of "vanished
inbound" failures: WhatsApp messages that arrive at the webhook but
never materialize into a Conversation row in Nahla. The architectural
investigation (see ``docs/adr/0004-inbound-lifecycle-telemetry.md``)
identified at least 27 silent drop-points across the ingestion path —
HTTP-layer rejects, 360dialog routing gates, dispatcher early returns,
short-circuits that save ``MessageEvent`` rows with ``conversation_id``
NULL, swallow-except blocks that roll back uncommitted Conversation
flushes.

We do NOT yet know which drop-points fire most often in production.
Before any behavioural fix (W2.0.3 conversation linking, W2.0.4
idempotency reorder, etc.), we need real data.

This module is the visibility layer:

  * One ``trace_id`` per inbound message, threaded through every
    layer via :class:`contextvars.ContextVar`.
  * Closed-vocabulary event tokens — operators grep
    ``[INBOUND_LIFECYCLE]`` and see exactly where each message ended
    up and which drop point it hit.
  * Single canonical summary line per message, emitted at the end of
    dispatch (success OR failure). Contains:
        trace_id, provider, phone_number_id, msg_id, msg_type,
        tenant_id, sender_phone (masked), elapsed_ms,
        convo_created, convo_id, message_saved, orphan_messages,
        final, path

  * Pure-function decision: telemetry is gated by a single env flag
    (``INBOUND_LIFECYCLE_TELEMETRY_ENABLED``, default ON because this
    is observation-only). Operators can flip OFF in seconds without
    a deploy.

Architectural rules (locked)
────────────────────────────
1. **Telemetry only.** No state writes. No behavioural change. Never
   raise. If recording fails, the inbound flow continues unaltered.
2. **No coupling.** Module imports nothing from the routers, webhook,
   or persistence layers. Wiring is one-way: those layers call into
   here.
3. **Closed event vocabulary.** Adding a new event constant is an
   intentional change; the test suite enumerates the public set.
4. **Never logs PII.** Phone numbers are masked to last-4 digits.
   Body text is not recorded — only its length.
5. **Cheap.** Every recording is O(1); the trace holds at most ~50
   events; the summary line is one ``logger.info`` call.

Out of scope
────────────
This module does NOT decide policy. The downstream waves
(W2.0.3 / W2.0.4 / …) decide what to DO when a drop is observed; this
module only tells us where drops happen.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

logger = logging.getLogger("nahla.inbound_lifecycle")


# ── Kill switch ─────────────────────────────────────────────────────
# Default ON because this layer is purely observational and adds zero
# behavioural risk. Operators can flip it OFF in seconds if telemetry
# volume becomes an issue.

_FLAG_NAME = "INBOUND_LIFECYCLE_TELEMETRY_ENABLED"


def is_inbound_lifecycle_telemetry_enabled() -> bool:
    """Read the kill switch. Defaults to **ON** (telemetry-only layer).

    Truthy: ``"1"`` / ``"true"`` / ``"yes"`` / ``"on"`` (case-insensitive).
    Falsy:  ``"0"`` / ``"false"`` / ``"no"`` / ``"off"``.
    Anything else (including missing) → ON.
    """
    raw = os.getenv(_FLAG_NAME, "")
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ── Closed event vocabulary ────────────────────────────────────────
# Stable string tokens. Adding to this list is a deliberate change —
# the test suite enumerates the public set and fails on drift. Token
# names match the architectural drop-point matrix in the W2.0
# investigation report.

# HTTP / batch layer
EVENT_HTTP_RECEIVED               = "http_received"
EVENT_HTTP_SIGNATURE_REJECT       = "http_signature_reject"
EVENT_HTTP_REPLAY_REJECT          = "http_replay_reject"
EVENT_HTTP_PARSE_FAIL             = "http_parse_fail"
EVENT_BG_REJECTED                 = "bg_rejected"

# 360dialog routing gates
EVENT_FIELD_NOT_MESSAGES          = "field_not_messages"
EVENT_FIELD_MESSAGES              = "field_messages"
EVENT_SECRET_MISMATCH             = "secret_mismatch"
EVENT_SCOPE_MISMATCH              = "scope_mismatch"
EVENT_AMBIGUOUS_PHONE_ID          = "ambiguous_phone_id"
EVENT_UNKNOWN_PHONE_ID            = "unknown_phone_id"
EVENT_MISSING_PHONE_ID            = "missing_phone_id"
EVENT_PROVIDER_MISMATCH           = "provider_mismatch"

# Per-message lifecycle
EVENT_RECEIVED                    = "received"
EVENT_DEDUP_DROP_MEMORY           = "dedup_drop_memory"
EVENT_DEDUP_DROP_DB               = "dedup_drop_db"
EVENT_DEDUP_MARKED                = "dedup_marked"
EVENT_DB_SESSION_FAIL             = "db_session_fail"
EVENT_TENANT_RESOLVED             = "tenant_resolved"
EVENT_CUSTOMER_LEAD_UPSERT        = "customer_lead_upsert"
EVENT_NORMALIZER_OK               = "normalizer_ok"
EVENT_NORMALIZER_FAIL             = "normalizer_fail"
EVENT_UNSUPPORTED_TYPE            = "unsupported_type"
EVENT_EMPTY_TEXT_FALLBACK         = "empty_text_fallback"
EVENT_EMPTY_TEXT_NO_FALLBACK      = "empty_text_no_fallback"
EVENT_HANDOFF_PRE_BRAIN           = "handoff_pre_brain"
EVENT_UNSUB_SHORT_CIRCUIT         = "unsub_short_circuit"
EVENT_PAYMENT_SHORT_CIRCUIT       = "payment_short_circuit"
EVENT_RECEIPT_SHORT_CIRCUIT       = "receipt_short_circuit"
EVENT_MAP_SHORT_CIRCUIT           = "map_short_circuit"
EVENT_PERSIST_INBOUND_ONLY_OK     = "persist_inbound_only_ok"
EVENT_PERSIST_INBOUND_ONLY_FAIL   = "persist_inbound_only_fail"
EVENT_MEDIA_FALLBACK_OK           = "media_fallback_ok"
EVENT_MEDIA_FALLBACK_FAIL         = "media_fallback_fail"
EVENT_BRAIN_INVOKED               = "brain_invoked"

# Persistence layer (called from helpers)
EVENT_CONVERSATION_CREATED        = "conversation_created"
EVENT_CONVERSATION_LOOKUP_HIT     = "conversation_lookup_hit"
EVENT_MESSAGE_SAVED               = "message_saved"
EVENT_MESSAGE_SAVED_ORPHAN        = "message_saved_orphan"
EVENT_MESSAGE_SAVE_ROLLBACK       = "message_save_rollback"

# Conversation-linking integrity (W2.0.3, May 2026). Recorded by the
# order-flow short-circuits that historically called save_message
# without conversation_id and produced orphans. AUTO_LINK_OK means
# the dispatcher resolved a Conversation row before persisting; all
# downstream save_message calls in that branch carry the id.
# AUTO_LINK_FAILED means the resolver raised — the branch fell open
# to legacy orphan behaviour to preserve forward progress (the
# user's media still goes through), and the row counts as an orphan
# in the summary.
EVENT_AUTO_LINK_OK                = "auto_link_ok"
EVENT_AUTO_LINK_FAILED            = "auto_link_failed"

# Terminal markers
EVENT_END_OK                      = "end_ok"
EVENT_END_DROPPED                 = "end_dropped"
EVENT_END_UNCAUGHT                = "end_uncaught_exception"


ALL_EVENTS: Tuple[str, ...] = (
    EVENT_HTTP_RECEIVED,
    EVENT_HTTP_SIGNATURE_REJECT,
    EVENT_HTTP_REPLAY_REJECT,
    EVENT_HTTP_PARSE_FAIL,
    EVENT_BG_REJECTED,
    EVENT_FIELD_NOT_MESSAGES,
    EVENT_FIELD_MESSAGES,
    EVENT_SECRET_MISMATCH,
    EVENT_SCOPE_MISMATCH,
    EVENT_AMBIGUOUS_PHONE_ID,
    EVENT_UNKNOWN_PHONE_ID,
    EVENT_MISSING_PHONE_ID,
    EVENT_PROVIDER_MISMATCH,
    EVENT_RECEIVED,
    EVENT_DEDUP_DROP_MEMORY,
    EVENT_DEDUP_DROP_DB,
    EVENT_DEDUP_MARKED,
    EVENT_DB_SESSION_FAIL,
    EVENT_TENANT_RESOLVED,
    EVENT_CUSTOMER_LEAD_UPSERT,
    EVENT_NORMALIZER_OK,
    EVENT_NORMALIZER_FAIL,
    EVENT_UNSUPPORTED_TYPE,
    EVENT_EMPTY_TEXT_FALLBACK,
    EVENT_EMPTY_TEXT_NO_FALLBACK,
    EVENT_HANDOFF_PRE_BRAIN,
    EVENT_UNSUB_SHORT_CIRCUIT,
    EVENT_PAYMENT_SHORT_CIRCUIT,
    EVENT_RECEIPT_SHORT_CIRCUIT,
    EVENT_MAP_SHORT_CIRCUIT,
    EVENT_PERSIST_INBOUND_ONLY_OK,
    EVENT_PERSIST_INBOUND_ONLY_FAIL,
    EVENT_MEDIA_FALLBACK_OK,
    EVENT_MEDIA_FALLBACK_FAIL,
    EVENT_BRAIN_INVOKED,
    EVENT_CONVERSATION_CREATED,
    EVENT_CONVERSATION_LOOKUP_HIT,
    EVENT_MESSAGE_SAVED,
    EVENT_MESSAGE_SAVED_ORPHAN,
    EVENT_MESSAGE_SAVE_ROLLBACK,
    EVENT_AUTO_LINK_OK,
    EVENT_AUTO_LINK_FAILED,
    EVENT_END_OK,
    EVENT_END_DROPPED,
    EVENT_END_UNCAUGHT,
)


# ── Internal dataclasses ───────────────────────────────────────────


@dataclass(frozen=True)
class LifecycleEvent:
    """One recorded event in a trace."""

    seq: int
    name: str
    monotonic_at: float
    detail: str = ""


@dataclass
class InboundLifecycleTrace:
    """Per-inbound trace. Lives for the duration of one
    ``inbound_lifecycle_trace(...)`` context. NEVER persisted.

    The fields are intentionally flat so the summary log line is
    cheap to format. ``events`` keeps the recorded path; the summary
    emits the trailing slice (last ~12 events) so we never blow up a
    single log line on extreme inputs.
    """

    trace_id: str
    started_monotonic: float
    provider: str = ""
    phone_number_id: str = ""
    msg_id: str = ""
    msg_type: str = ""
    sender_phone_masked: str = ""
    tenant_id: Optional[int] = None
    body_len: int = 0
    has_caption: Optional[bool] = None

    events: List[LifecycleEvent] = field(default_factory=list)

    # Aggregate state — derived from events as they are recorded.
    conversation_created: bool = False
    conversation_lookup_hit: bool = False
    conversation_id: Optional[int] = None
    message_saved: bool = False
    orphan_message_count: int = 0
    rollback_count: int = 0
    persist_inbound_only_attempts: int = 0
    persist_inbound_only_failures: int = 0
    media_fallback_attempts: int = 0
    media_fallback_failures: int = 0
    final_token: str = ""

    def record(self, event_name: str, *, detail: str = "", **kwargs: Any) -> None:
        """Append an event and update aggregate state. Safe to call
        from anywhere; never raises."""
        try:
            seq = len(self.events) + 1
            self.events.append(LifecycleEvent(
                seq=seq,
                name=event_name,
                monotonic_at=time.monotonic(),
                detail=detail or "",
            ))
            self._apply(event_name, kwargs)
        except Exception:
            pass

    def _apply(self, event_name: str, kwargs: Mapping[str, Any]) -> None:
        if event_name == EVENT_CONVERSATION_CREATED:
            self.conversation_created = True
            cid = kwargs.get("conversation_id")
            if isinstance(cid, int):
                self.conversation_id = cid
        elif event_name == EVENT_CONVERSATION_LOOKUP_HIT:
            self.conversation_lookup_hit = True
            cid = kwargs.get("conversation_id")
            if isinstance(cid, int):
                self.conversation_id = cid
        elif event_name == EVENT_AUTO_LINK_OK:
            # W2.0.3: surface the resolved conversation_id on the
            # summary line even when persistence later fails — the
            # operator needs to see "we DID resolve a row, the
            # downstream save just blew up" vs. "we never resolved
            # anything to begin with".
            cid = kwargs.get("conversation_id")
            if isinstance(cid, int):
                self.conversation_id = cid
        elif event_name == EVENT_MESSAGE_SAVED:
            self.message_saved = True
            cid = kwargs.get("conversation_id")
            if isinstance(cid, int):
                if self.conversation_id is None:
                    self.conversation_id = cid
            elif cid is None:
                # Saved without a conversation_id — orphan. Recorded
                # under a separate event for crisp log filtering, but
                # the count also bumps here for completeness.
                self.orphan_message_count += 1
        elif event_name == EVENT_MESSAGE_SAVED_ORPHAN:
            self.message_saved = True
            self.orphan_message_count += 1
        elif event_name == EVENT_MESSAGE_SAVE_ROLLBACK:
            self.rollback_count += 1
        elif event_name == EVENT_PERSIST_INBOUND_ONLY_OK:
            self.persist_inbound_only_attempts += 1
        elif event_name == EVENT_PERSIST_INBOUND_ONLY_FAIL:
            self.persist_inbound_only_attempts += 1
            self.persist_inbound_only_failures += 1
        elif event_name == EVENT_MEDIA_FALLBACK_OK:
            self.media_fallback_attempts += 1
        elif event_name == EVENT_MEDIA_FALLBACK_FAIL:
            self.media_fallback_attempts += 1
            self.media_fallback_failures += 1
        elif event_name == EVENT_TENANT_RESOLVED:
            tid = kwargs.get("tenant_id")
            if isinstance(tid, int):
                self.tenant_id = tid
        elif event_name in (EVENT_END_OK, EVENT_END_DROPPED, EVENT_END_UNCAUGHT):
            self.final_token = event_name


# ── ContextVar ─────────────────────────────────────────────────────


_active: ContextVar[Optional[InboundLifecycleTrace]] = ContextVar(
    "inbound_lifecycle_trace", default=None,
)


def current_trace() -> Optional[InboundLifecycleTrace]:
    """Return the active trace, or ``None`` if telemetry is off /
    no inbound is currently being processed."""
    return _active.get()


# ── Helpers ────────────────────────────────────────────────────────


def _mask_phone(phone: Optional[str]) -> str:
    """Mask a phone to last-4 digits with a leading ``*``. Empty
    input → empty string. Never raises."""
    try:
        if not phone:
            return ""
        s = str(phone)
        if len(s) <= 4:
            return "*" + s
        return "*" + s[-4:]
    except Exception:
        return ""


def make_trace_id(*, provider: str = "", phone_number_id: str = "",
                  msg_id: str = "") -> str:
    """Build a stable trace id. Prefers ``msg_id`` when present so
    retries / dedup hits show up under the same trace_id; falls back
    to a uuid suffix when no msg_id is available."""
    try:
        prov = (provider or "x")[:8]
        if msg_id:
            tail = str(msg_id).strip()
            if tail:
                return f"il_{prov}_{tail}"[:96]
        suffix = uuid.uuid4().hex[:12]
        if phone_number_id:
            return f"il_{prov}_{phone_number_id[:24]}_{suffix}"
        return f"il_{prov}_{suffix}"
    except Exception:
        return f"il_x_{uuid.uuid4().hex[:12]}"


# ── Public recording API ───────────────────────────────────────────


def record_lifecycle(event_name: str, *, detail: str = "", **kwargs: Any) -> None:
    """Append an event to the active trace. No-op when telemetry is
    off or no trace is active. Never raises (the docstring contract;
    callers sprinkle this across hot paths so a swallowed bug must
    never reach the dispatcher)."""
    try:
        if not is_inbound_lifecycle_telemetry_enabled():
            return
        trace = _active.get()
        if trace is None:
            return
        trace.record(event_name, detail=detail, **kwargs)
    except Exception:
        pass


def attach_tenant(tenant_id: Optional[int]) -> None:
    """Convenience wrapper used by the dispatcher right after tenant
    resolution. Records :data:`EVENT_TENANT_RESOLVED` and stamps
    ``trace.tenant_id`` for the summary line."""
    record_lifecycle(EVENT_TENANT_RESOLVED, tenant_id=tenant_id)


def attach_normalizer_outcome(*, normalized_type: Optional[str],
                              text_len: int,
                              fallback_set: bool) -> None:
    """Convenience wrapper for the normalizer success path."""
    detail = (
        f"type={normalized_type or ''} "
        f"text_len={int(text_len or 0)} "
        f"fallback={'1' if fallback_set else '0'}"
    )
    record_lifecycle(EVENT_NORMALIZER_OK, detail=detail)


# ── Context manager ────────────────────────────────────────────────


@contextmanager
def inbound_lifecycle_trace(
    *,
    provider: str = "",
    phone_number_id: str = "",
    msg: Optional[Mapping[str, Any]] = None,
    sender_phone: Optional[str] = None,
) -> Iterator[Optional[InboundLifecycleTrace]]:
    """Open a per-inbound trace. Yields the trace (or ``None`` when
    telemetry is off). Always emits the canonical summary line on
    exit, including when the body raises.

    Idempotent w.r.t. nesting: if a trace is already active in this
    context, the inner ``with`` is a no-op pass-through (the outer
    trace continues to accumulate events). This avoids double
    summary lines if a future caller adds redundant wrapping.
    """
    if not is_inbound_lifecycle_telemetry_enabled():
        yield None
        return

    parent = _active.get()
    if parent is not None:
        # Re-entrant — share the parent trace, do not emit a second
        # summary.
        yield parent
        return

    msg_dict: Dict[str, Any] = dict(msg or {})
    msg_id = str(msg_dict.get("id") or "")
    msg_type = str(msg_dict.get("type") or "")
    body_len = 0
    has_caption: Optional[bool] = None
    try:
        if msg_type == "text":
            body = (msg_dict.get("text") or {}).get("body") or ""
            body_len = len(body)
        elif msg_type in ("image", "video", "document", "audio"):
            cap = ((msg_dict.get(msg_type) or {}).get("caption") or "")
            has_caption = bool(cap)
            body_len = len(cap)
    except Exception:
        pass

    sender = sender_phone or msg_dict.get("from") or ""
    trace = InboundLifecycleTrace(
        trace_id=make_trace_id(
            provider=provider,
            phone_number_id=phone_number_id,
            msg_id=msg_id,
        ),
        started_monotonic=time.monotonic(),
        provider=provider or "",
        phone_number_id=phone_number_id or "",
        msg_id=msg_id,
        msg_type=msg_type,
        sender_phone_masked=_mask_phone(sender),
        body_len=body_len,
        has_caption=has_caption,
    )
    trace.record(EVENT_RECEIVED)

    token = _active.set(trace)
    raised: Optional[BaseException] = None
    try:
        yield trace
    except BaseException as exc:
        raised = exc
        try:
            trace.record(
                EVENT_END_UNCAUGHT,
                detail=type(exc).__name__,
            )
        except Exception:
            pass
        raise
    finally:
        try:
            if raised is None and not trace.final_token:
                # Caller did not stamp a terminal — infer one from
                # what happened. Conservative defaults: any successful
                # message_saved → end_ok; otherwise end_dropped.
                terminal = (
                    EVENT_END_OK if trace.message_saved else EVENT_END_DROPPED
                )
                trace.record(terminal)
        except Exception:
            pass
        try:
            _active.reset(token)
        except Exception:
            pass
        try:
            emit_lifecycle_summary(trace)
        except Exception:
            pass


# ── Summary emission ───────────────────────────────────────────────


# Trailing slice cap on the path token. Keeps the log line within
# Cloud Logging single-line budgets even when an inbound walks
# through many states (e.g. normalizer + short-circuits + handoff
# logic). Operators always see the FINAL events; the early ones are
# still recoverable from per-event side logs if needed.
_PATH_TAIL_CAP = 16


def _format_path(events: List[LifecycleEvent]) -> str:
    if not events:
        return ""
    tail = events[-_PATH_TAIL_CAP:]
    omitted = len(events) - len(tail)
    body = "->".join(e.name for e in tail)
    if omitted > 0:
        return f"...({omitted})->" + body
    return body


def emit_lifecycle_summary(trace: Optional[InboundLifecycleTrace]) -> None:
    """Emit the canonical ``[INBOUND_LIFECYCLE]`` line for ``trace``.

    Called automatically at the end of :func:`inbound_lifecycle_trace`.
    Operators normally never call this directly; it is exposed for
    unit-test ergonomics.
    """
    if trace is None:
        return
    if not is_inbound_lifecycle_telemetry_enabled():
        return
    try:
        elapsed_ms = int((time.monotonic() - trace.started_monotonic) * 1000)
        final = trace.final_token or (
            trace.events[-1].name if trace.events else "unknown"
        )
        path = _format_path(trace.events)
        logger.info(
            "[INBOUND_LIFECYCLE] trace_id=%s provider=%s phone_id=%s "
            "msg_id=%s msg_type=%s tenant_id=%s sender=%s body_len=%d "
            "has_caption=%s elapsed_ms=%d "
            "convo_created=%s convo_lookup_hit=%s convo_id=%s "
            "message_saved=%s orphan_messages=%d rollbacks=%d "
            "persist_only=%d/%d media_fallback=%d/%d "
            "final=%s path=%s",
            trace.trace_id,
            trace.provider or "",
            trace.phone_number_id or "",
            trace.msg_id or "",
            trace.msg_type or "",
            trace.tenant_id if trace.tenant_id is not None else "",
            trace.sender_phone_masked or "",
            int(trace.body_len or 0),
            "" if trace.has_caption is None else (
                "true" if trace.has_caption else "false"
            ),
            elapsed_ms,
            "true" if trace.conversation_created else "false",
            "true" if trace.conversation_lookup_hit else "false",
            trace.conversation_id if trace.conversation_id is not None else "",
            "true" if trace.message_saved else "false",
            int(trace.orphan_message_count),
            int(trace.rollback_count),
            int(trace.persist_inbound_only_failures),
            int(trace.persist_inbound_only_attempts),
            int(trace.media_fallback_failures),
            int(trace.media_fallback_attempts),
            final,
            path,
        )
    except Exception:
        pass


# ── Standalone events (no active trace) ────────────────────────────
# For HTTP-layer rejects that fire BEFORE we have a per-message
# trace (signature reject, replay reject, JSON parse failure, BG
# spawn rejection from a non-message context). We log a one-shot
# ``[INBOUND_LIFECYCLE]`` line with whatever fields are available
# so operators can grep the same prefix for everything.


def emit_standalone_event(
    event_name: str,
    *,
    provider: str = "",
    phone_number_id: str = "",
    detail: str = "",
    **fields: Any,
) -> None:
    """Emit a single lifecycle event without a per-message trace.

    Used at the HTTP boundary (signature reject, replay reject, parse
    failure, BG rejection) where no per-inbound context exists yet.
    Never raises.
    """
    if not is_inbound_lifecycle_telemetry_enabled():
        return
    try:
        kv_extra = " ".join(
            f"{k}={v}" for k, v in fields.items() if v not in (None, "")
        )
        logger.info(
            "[INBOUND_LIFECYCLE] standalone event=%s provider=%s "
            "phone_id=%s detail=%r %s",
            event_name, provider or "", phone_number_id or "",
            detail or "", kv_extra,
        )
    except Exception:
        pass


__all__ = [
    "is_inbound_lifecycle_telemetry_enabled",
    "current_trace",
    "make_trace_id",
    "record_lifecycle",
    "attach_tenant",
    "attach_normalizer_outcome",
    "inbound_lifecycle_trace",
    "emit_lifecycle_summary",
    "emit_standalone_event",
    "InboundLifecycleTrace",
    "LifecycleEvent",
    "ALL_EVENTS",
    # Event tokens — flat re-export so consumers can `from
    # core.inbound_lifecycle import EVENT_*`.
    "EVENT_HTTP_RECEIVED",
    "EVENT_HTTP_SIGNATURE_REJECT",
    "EVENT_HTTP_REPLAY_REJECT",
    "EVENT_HTTP_PARSE_FAIL",
    "EVENT_BG_REJECTED",
    "EVENT_FIELD_NOT_MESSAGES",
    "EVENT_FIELD_MESSAGES",
    "EVENT_SECRET_MISMATCH",
    "EVENT_SCOPE_MISMATCH",
    "EVENT_AMBIGUOUS_PHONE_ID",
    "EVENT_UNKNOWN_PHONE_ID",
    "EVENT_MISSING_PHONE_ID",
    "EVENT_PROVIDER_MISMATCH",
    "EVENT_RECEIVED",
    "EVENT_DEDUP_DROP_MEMORY",
    "EVENT_DEDUP_DROP_DB",
    "EVENT_DEDUP_MARKED",
    "EVENT_DB_SESSION_FAIL",
    "EVENT_TENANT_RESOLVED",
    "EVENT_CUSTOMER_LEAD_UPSERT",
    "EVENT_NORMALIZER_OK",
    "EVENT_NORMALIZER_FAIL",
    "EVENT_UNSUPPORTED_TYPE",
    "EVENT_EMPTY_TEXT_FALLBACK",
    "EVENT_EMPTY_TEXT_NO_FALLBACK",
    "EVENT_HANDOFF_PRE_BRAIN",
    "EVENT_UNSUB_SHORT_CIRCUIT",
    "EVENT_PAYMENT_SHORT_CIRCUIT",
    "EVENT_RECEIPT_SHORT_CIRCUIT",
    "EVENT_MAP_SHORT_CIRCUIT",
    "EVENT_PERSIST_INBOUND_ONLY_OK",
    "EVENT_PERSIST_INBOUND_ONLY_FAIL",
    "EVENT_MEDIA_FALLBACK_OK",
    "EVENT_MEDIA_FALLBACK_FAIL",
    "EVENT_BRAIN_INVOKED",
    "EVENT_CONVERSATION_CREATED",
    "EVENT_CONVERSATION_LOOKUP_HIT",
    "EVENT_MESSAGE_SAVED",
    "EVENT_MESSAGE_SAVED_ORPHAN",
    "EVENT_MESSAGE_SAVE_ROLLBACK",
    "EVENT_AUTO_LINK_OK",
    "EVENT_AUTO_LINK_FAILED",
    "EVENT_END_OK",
    "EVENT_END_DROPPED",
    "EVENT_END_UNCAUGHT",
]
