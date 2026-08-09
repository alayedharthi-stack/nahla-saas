"""
core/turn_latency.py
────────────────────
Fail-open, low-overhead per-turn latency spans for Merchant AI.

Observability only — never changes reply behavior, models, or timeouts.

Contract
────────
* One ``TurnLatency`` per inbound turn, correlated by tenant_id /
  conversation_id / turn_id / message_event_id.
* Stage timers are monotonic; exceptions inside telemetry are swallowed.
* Nested detail spans (``catalog_search``, ``llm_calls``, ``lock_hold``)
  are recorded for diagnosis but excluded from ``accounted_ms`` so totals
  are not double-counted.
* TTFT is recorded only when a provider actually exposes first-token time;
  otherwise ``ttft_available=false`` (never invented).
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, MutableMapping, Optional

logger = logging.getLogger("nahla.turn_latency")

# Mutually exclusive stages summed into accounted_ms (no nesting).
ACCOUNTABLE_STAGES: tuple[str, ...] = (
    "inbound_persist",
    "tenant_resolution",
    "conversation_lock_wait",
    "state_load",
    "slot_extractor",
    "permission_context_load",
    "facts_load",
    "catalog_preload",
    "knowledge_retrieval",
    "decision",
    "tool_execution",
    "persona_compose",
    "post_compose",
    "guards",
    "presentation",
    "state_persist",
    "outbound_persist",
    "provider_send",
)

# Detail-only (not summed into accounted_ms).
DETAIL_STAGES: tuple[str, ...] = (
    "conversation_lock_hold",
    "catalog_search",
    "facts_db",
    "catalog_db",
    "state_db",
)

_CURRENT: contextvars.ContextVar[Optional["TurnLatency"]] = contextvars.ContextVar(
    "nahla_turn_latency",
    default=None,
)


def get_turn_latency() -> Optional["TurnLatency"]:
    try:
        return _CURRENT.get()
    except Exception:  # noqa: BLE001
        return None


def bind_turn_latency(timing: Optional["TurnLatency"]) -> contextvars.Token:
    """Bind timing into the ContextVar. Always returns a reset token."""
    try:
        return _CURRENT.set(timing)
    except Exception:  # noqa: BLE001
        return _CURRENT.set(None)


def reset_turn_latency(token: contextvars.Token) -> None:
    try:
        _CURRENT.reset(token)
    except Exception:  # noqa: BLE001
        pass


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:  # noqa: BLE001
        return default


def _safe_float_ms(started: float) -> int:
    try:
        return max(0, int((time.monotonic() - started) * 1000.0))
    except Exception:  # noqa: BLE001
        return 0


@dataclass
class LlmCallTiming:
    model: str = ""
    provider: str = ""
    request_start_ms: Optional[int] = None
    request_end_ms: Optional[int] = None
    first_token_ms: Optional[int] = None
    duration_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    timeout_seconds: Optional[float] = None
    retry_count: int = 0
    fallback_reason: str = ""
    ttft_available: bool = False
    purpose: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model or None,
            "provider": self.provider or None,
            "purpose": self.purpose or None,
            "request_start_ms": self.request_start_ms,
            "request_end_ms": self.request_end_ms,
            "first_token_ms": self.first_token_ms if self.ttft_available else None,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "timeout_seconds": self.timeout_seconds,
            "retry_count": int(self.retry_count or 0),
            "fallback_reason": self.fallback_reason or None,
            "ttft_available": bool(self.ttft_available),
        }


@dataclass
class TurnLatency:
    """Per-inbound-turn latency accumulator (fail-open)."""

    tenant_id: int = 0
    conversation_id: Optional[int] = None
    message_event_id: Optional[int] = None
    message_id: str = ""
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    started_monotonic: float = field(default_factory=time.monotonic)
    started_wall_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    spans_ms: Dict[str, int] = field(default_factory=dict)
    span_counts: Dict[str, int] = field(default_factory=dict)
    db_query_counts: Dict[str, int] = field(default_factory=dict)
    llm_calls: List[LlmCallTiming] = field(default_factory=list)

    lock_wait_ms: Optional[int] = None
    lock_hold_ms: Optional[int] = None
    waiters_ahead: Optional[int] = None

    _open_spans: Dict[str, float] = field(default_factory=dict, repr=False)
    _finalized: bool = field(default=False, repr=False)
    _snapshot: Optional[Dict[str, Any]] = field(default=None, repr=False)

    # ── identity ──────────────────────────────────────────────────

    def set_identity(
        self,
        *,
        tenant_id: Optional[int] = None,
        conversation_id: Optional[int] = None,
        message_event_id: Optional[int] = None,
        message_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        try:
            if tenant_id is not None:
                self.tenant_id = int(tenant_id)
            if conversation_id is not None:
                self.conversation_id = int(conversation_id)
            if message_event_id is not None:
                self.message_event_id = int(message_event_id)
            if message_id is not None:
                self.message_id = str(message_id or "")[:128]
            if turn_id is not None and str(turn_id).strip():
                self.turn_id = str(turn_id).strip()[:64]
        except Exception:  # noqa: BLE001
            pass

    # ── span API ──────────────────────────────────────────────────

    def start(self, name: str) -> None:
        try:
            key = str(name or "").strip()
            if not key:
                return
            self._open_spans[key] = time.monotonic()
        except Exception:  # noqa: BLE001
            pass

    def end(self, name: str) -> Optional[int]:
        try:
            key = str(name or "").strip()
            if not key:
                return None
            started = self._open_spans.pop(key, None)
            if started is None:
                return None
            return self.record_ms(key, _safe_float_ms(started))
        except Exception:  # noqa: BLE001
            return None

    def record_ms(self, name: str, duration_ms: Any) -> Optional[int]:
        try:
            key = str(name or "").strip()
            if not key:
                return None
            ms = max(0, _safe_int(duration_ms, 0))
            prev = int(self.spans_ms.get(key, 0) or 0)
            self.spans_ms[key] = prev + ms
            self.span_counts[key] = int(self.span_counts.get(key, 0) or 0) + 1
            if key == "conversation_lock_wait":
                self.lock_wait_ms = int(self.spans_ms[key])
            elif key == "conversation_lock_hold":
                self.lock_hold_ms = int(self.spans_ms[key])
            return ms
        except Exception:  # noqa: BLE001
            return None

    def add_db_queries(self, stage: str, count: int = 1) -> None:
        try:
            key = str(stage or "").strip()
            if not key:
                return
            self.db_query_counts[key] = int(self.db_query_counts.get(key, 0) or 0) + max(
                0, int(count or 0)
            )
        except Exception:  # noqa: BLE001
            pass

    def record_lock(
        self,
        *,
        wait_ms: Any = None,
        hold_ms: Any = None,
        waiters_ahead: Any = None,
    ) -> None:
        try:
            if wait_ms is not None:
                self.record_ms("conversation_lock_wait", wait_ms)
            if hold_ms is not None:
                self.record_ms("conversation_lock_hold", hold_ms)
            if waiters_ahead is not None:
                self.waiters_ahead = max(0, _safe_int(waiters_ahead, 0))
        except Exception:  # noqa: BLE001
            pass

    def record_llm_call(self, **kwargs: Any) -> None:
        try:
            call = LlmCallTiming(
                model=str(kwargs.get("model") or ""),
                provider=str(kwargs.get("provider") or ""),
                purpose=str(kwargs.get("purpose") or ""),
                request_start_ms=_optional_int(kwargs.get("request_start_ms")),
                request_end_ms=_optional_int(kwargs.get("request_end_ms")),
                first_token_ms=_optional_int(kwargs.get("first_token_ms")),
                duration_ms=_optional_int(kwargs.get("duration_ms")),
                input_tokens=_optional_int(kwargs.get("input_tokens")),
                output_tokens=_optional_int(kwargs.get("output_tokens")),
                cached_tokens=_optional_int(kwargs.get("cached_tokens")),
                timeout_seconds=_optional_float(kwargs.get("timeout_seconds")),
                retry_count=max(0, _safe_int(kwargs.get("retry_count"), 0)),
                fallback_reason=str(kwargs.get("fallback_reason") or ""),
                ttft_available=bool(kwargs.get("ttft_available")),
            )
            if call.first_token_ms is not None and not call.ttft_available:
                # Never invent TTFT — drop first_token unless explicitly available.
                call.first_token_ms = None
            self.llm_calls.append(call)
        except Exception:  # noqa: BLE001
            pass

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        self.start(name)
        try:
            yield
        finally:
            self.end(name)

    # ── finalize / export ─────────────────────────────────────────

    def total_turn_ms(self) -> int:
        try:
            if "total_turn" in self.spans_ms:
                return int(self.spans_ms["total_turn"])
            return _safe_float_ms(self.started_monotonic)
        except Exception:  # noqa: BLE001
            return 0

    def accounted_ms(self) -> int:
        total = 0
        try:
            for name in ACCOUNTABLE_STAGES:
                total += int(self.spans_ms.get(name, 0) or 0)
        except Exception:  # noqa: BLE001
            return 0
        return total

    def snapshot(self, *, finalize_total: bool = True) -> Dict[str, Any]:
        """Build a JSON-safe timing dict. Fail-open; never raises."""
        try:
            if finalize_total and "total_turn" not in self.spans_ms:
                self.record_ms("total_turn", self.total_turn_ms())
            total = int(self.spans_ms.get("total_turn") or self.total_turn_ms() or 0)
            accounted = self.accounted_ms()
            unaccounted = max(0, total - accounted)
            percent = round((100.0 * accounted / total), 1) if total > 0 else 0.0
            out: Dict[str, Any] = {
                "tenant_id": int(self.tenant_id or 0) or None,
                "conversation_id": self.conversation_id,
                "message_event_id": self.message_event_id,
                "message_id": self.message_id or None,
                "turn_id": self.turn_id,
                "started_wall_ms": self.started_wall_ms,
                "spans_ms": {k: int(v) for k, v in sorted(self.spans_ms.items())},
                "span_counts": {k: int(v) for k, v in sorted(self.span_counts.items())},
                "db_query_counts": {
                    k: int(v) for k, v in sorted(self.db_query_counts.items())
                },
                "lock_wait_ms": self.lock_wait_ms,
                "lock_hold_ms": self.lock_hold_ms,
                "waiters_ahead": self.waiters_ahead,
                "llm_calls": [c.to_dict() for c in self.llm_calls],
                "total_turn_ms": total,
                "accounted_ms": accounted,
                "unaccounted_ms": unaccounted,
                "accounted_percent": percent,
                "accountable_stages": list(ACCOUNTABLE_STAGES),
                "ttft_available": any(bool(c.ttft_available) for c in self.llm_calls),
            }
            self._snapshot = out
            self._finalized = True
            return out
        except Exception:  # noqa: BLE001
            return {
                "turn_id": getattr(self, "turn_id", None),
                "total_turn_ms": 0,
                "accounted_ms": 0,
                "unaccounted_ms": 0,
                "accounted_percent": 0.0,
                "spans_ms": {},
                "error": "turn_latency_snapshot_failed",
            }

    def emit_log(self) -> None:
        """Emit one compact ``[TURN_LATENCY]`` line. Never raises."""
        try:
            snap = self._snapshot or self.snapshot()
            logger.info(
                "[TURN_LATENCY] tenant=%s conv=%s turn_id=%s msg_event=%s "
                "total_ms=%s accounted_ms=%s unaccounted_ms=%s accounted_pct=%s "
                "lock_wait_ms=%s lock_hold_ms=%s waiters_ahead=%s "
                "persona_ms=%s provider_ms=%s llm_calls=%s spans=%s",
                snap.get("tenant_id"),
                snap.get("conversation_id"),
                snap.get("turn_id"),
                snap.get("message_event_id"),
                snap.get("total_turn_ms"),
                snap.get("accounted_ms"),
                snap.get("unaccounted_ms"),
                snap.get("accounted_percent"),
                snap.get("lock_wait_ms"),
                snap.get("lock_hold_ms"),
                snap.get("waiters_ahead"),
                (snap.get("spans_ms") or {}).get("persona_compose"),
                (snap.get("spans_ms") or {}).get("provider_send"),
                len(snap.get("llm_calls") or []),
                snap.get("spans_ms"),
            )
        except Exception:  # noqa: BLE001
            try:
                logger.debug("[TURN_LATENCY] emit failed", exc_info=False)
            except Exception:  # noqa: BLE001
                pass


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def new_turn_latency(
    *,
    tenant_id: int = 0,
    conversation_id: Optional[int] = None,
    message_event_id: Optional[int] = None,
    message_id: str = "",
    turn_id: Optional[str] = None,
) -> TurnLatency:
    timing = TurnLatency(
        tenant_id=int(tenant_id or 0),
        conversation_id=conversation_id,
        message_event_id=message_event_id,
        message_id=message_id or "",
        turn_id=(turn_id or uuid.uuid4().hex[:16]),
    )
    return timing


def merge_turn_latency_into_metadata(
    metadata: Optional[MutableMapping[str, Any]],
    timing: Optional[TurnLatency],
) -> None:
    """Attach ``turn_timing`` snapshot into message_events metadata. Fail-open."""
    try:
        if metadata is None or timing is None:
            return
        snap = timing._snapshot or timing.snapshot(finalize_total=False)
        # Avoid rewriting total if caller will finalize later; still export spans.
        metadata["turn_timing"] = snap
    except Exception:  # noqa: BLE001
        pass


def safe_span(name: str):
    """Context manager bound to current turn latency (no-op if unbound)."""

    @contextmanager
    def _cm() -> Iterator[None]:
        timing = None
        try:
            timing = get_turn_latency()
        except Exception:  # noqa: BLE001
            timing = None
        if timing is None:
            yield
            return
        started = False
        try:
            timing.start(name)
            started = True
        except Exception:  # noqa: BLE001
            started = False
        try:
            yield
        finally:
            if started:
                try:
                    timing.end(name)
                except Exception:  # noqa: BLE001
                    pass

    return _cm()


def safe_record_ms(name: str, duration_ms: Any) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.record_ms(name, duration_ms)
    except Exception:  # noqa: BLE001
        pass


def safe_record_llm_call(**kwargs: Any) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.record_llm_call(**kwargs)
    except Exception:  # noqa: BLE001
        pass


def safe_record_lock(**kwargs: Any) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.record_lock(**kwargs)
    except Exception:  # noqa: BLE001
        pass


def attach_timing_to_trace_extra(extra: Optional[MutableMapping[str, Any]], timing: Optional[TurnLatency]) -> None:
    try:
        if extra is None or timing is None:
            return
        extra["turn_timing"] = timing
    except Exception:  # noqa: BLE001
        pass


def timing_from_trace_extra(extra: Optional[Mapping[str, Any]]) -> Optional[TurnLatency]:
    try:
        if not isinstance(extra, Mapping):
            return None
        value = extra.get("turn_timing")
        if isinstance(value, TurnLatency):
            return value
        return None
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "ACCOUNTABLE_STAGES",
    "DETAIL_STAGES",
    "LlmCallTiming",
    "TurnLatency",
    "attach_timing_to_trace_extra",
    "bind_turn_latency",
    "get_turn_latency",
    "merge_turn_latency_into_metadata",
    "new_turn_latency",
    "reset_turn_latency",
    "safe_record_llm_call",
    "safe_record_lock",
    "safe_record_ms",
    "safe_span",
    "timing_from_trace_extra",
]
