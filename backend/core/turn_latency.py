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
* TTFT is recorded only when a provider actually exposes first-token time;
  otherwise ``ttft_available=false`` (never invented).

Stage taxonomy (accounting contract)
────────────────────────────────────
Three explicit sets control what is summed into ``accounted_ms``:

1. **ACCOUNTABLE_LEAF** — mutually exclusive leaf stages summed into
   ``accounted_ms``. Each represents real work that should not overlap
   siblings in the sum.

2. **ENVELOPE** — diagnostic parent/checkpoint spans recorded in
   ``spans_ms`` but **never** summed into ``accounted_ms``. Overlapping
   parents (e.g. ``intent_routing`` wrapping ``slot_extractor``) stay
   envelope-only; no exclusive-subtract math.

3. **DETAIL_STAGES** — nested detail spans (``catalog_search``,
   ``facts_db``, ``lock_hold``, ``guards_wall``, …) for diagnosis only;
   excluded from ``accounted_ms``.

Nested exclusive exception (mechanical, not envelope demotion):
``quality_recompose`` runs inside the ``guards`` wall on recompose turns.
Both remain ACCOUNTABLE and independently observable; ``guards`` is recorded
as exclusive wall only (``guards_wall − quality_recompose``). Full wall is
kept as DETAIL ``guards_wall``.

Accounting math::

    accounted_ms = sum(ACCOUNTABLE_LEAF spans only)
    unaccounted_ms = max(0, end_to_end_total_ms - accounted_ms)
    accounted_percent = 100 * accounted_ms / total when total > 0

Snapshot semantics (v2)
───────────────────────
* ``brain_total_ms`` — wall time from turn start through the brain-return
  boundary (first mid-turn snapshot, ``finalize_total=False``). Excludes
  post-brain webhook dispatch, outbound persist, and provider send.
* ``total_turn_ms`` — end-to-end wall time for the inbound turn when
  ``finalize_total=True`` (same value as ``end_to_end_total_ms``).
* ``merge_turn_latency_into_metadata`` always builds a **fresh** snapshot;
  it never reuses a cached ``_snapshot`` from an earlier boundary.
* ``refresh_turn_latency_on_outbound_message`` writes a final
  ``finalize_total=True`` snapshot onto the persisted outbound row after
  ``provider_send`` without re-recording spans.
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

# Mutually exclusive leaf stages summed into accounted_ms (no nesting).
# Post-brain taxonomy (v4): ``post_brain_dispatch`` is ENVELOPE-only (mark→flush
# diagnostic wall). Accountable siblings under that wall: ``post_brain_remaining_prep``
# (webhook dispatch body before outbound_persist) plus ``reply_normalization``,
# ``presentation``, ``outbound_persist``, ``provider_send`` — no overlap with the
# envelope wall in accounted_ms.
ACCOUNTABLE_LEAF: tuple[str, ...] = (
    "webhook_dispatch_pre_persist",
    "inbound_persist",
    "tenant_resolution",
    "conversation_lock_wait",
    "merchant_entry_gates",
    "history_load",
    "conversation_state_load",
    "conversation_state_save",
    "customer_intelligence_upsert",
    "customer_profile_ensure",
    "store_ai_mode_lookup",
    "pre_brain_remaining_prep",
    "state_load",
    "slot_extractor",
    "permission_context_load",
    "facts_load",
    "sales_context_load",
    "commerce_bundle_load",
    "catalog_preload",
    "knowledge_retrieval",
    "commerce_turn_contract",
    "trusted_context_projection",
    "decision",
    "tool_execution",
    "state_projection",
    "persona_compose",
    "default_compose",
    "quality_recompose",
    "post_compose",
    "guards",
    "presentation",
    "state_persist",
    "memory_update",
    "silent_welcome_handling",
    "outbound_dedup",
    "truth_guards",
    "reply_normalization",
    "post_brain_remaining_prep",
    "outbound_persist",
    "provider_send",
)

# Pre/post brain reconciliation subsets (computed in snapshot; not summed twice).
PRE_BRAIN_ACCOUNTABLE: tuple[str, ...] = (
    "webhook_dispatch_pre_persist",
    "inbound_persist",
    "tenant_resolution",
    "conversation_lock_wait",
    "merchant_entry_gates",
    "history_load",
    "conversation_state_load",
    "conversation_state_save",
    "customer_intelligence_upsert",
    "customer_profile_ensure",
    "store_ai_mode_lookup",
    "pre_brain_remaining_prep",
)

POST_BRAIN_ACCOUNTABLE: tuple[str, ...] = (
    "silent_welcome_handling",
    "outbound_dedup",
    "truth_guards",
    "reply_normalization",
    "post_brain_remaining_prep",
    "outbound_persist",
    "presentation",
    "provider_send",
)

# Envelope/checkpoint spans — diagnostic only, never summed into accounted_ms.
ENVELOPE_STAGES: tuple[str, ...] = (
    "brain_boundary_enter",
    "brain_boundary_exit",
    "intent_routing",
    "post_brain_dispatch",
)

# Backward-compatible alias (accounting uses ACCOUNTABLE_LEAF only).
ACCOUNTABLE_STAGES: tuple[str, ...] = ACCOUNTABLE_LEAF

# Detail-only (not summed into accounted_ms).
DETAIL_STAGES: tuple[str, ...] = (
    "conversation_lock_hold",
    "catalog_search",
    "facts_db",
    "catalog_db",
    "state_db",
    "truth_guards_detail",
    "memory_summary_llm",
    "memory_summary_db",
    "guards_wall",
)

_CURRENT: contextvars.ContextVar[Optional["TurnLatency"]] = contextvars.ContextVar(
    "nahla_turn_latency",
    default=None,
)

_COMPOSE_ROLE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "nahla_compose_role",
    default=None,
)


def get_turn_latency() -> Optional["TurnLatency"]:
    try:
        return _CURRENT.get()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        return None


def bind_turn_latency(timing: Optional["TurnLatency"]) -> contextvars.Token:
    """Bind timing into the ContextVar. Always returns a reset token."""
    try:
        return _CURRENT.set(timing)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        return _CURRENT.set(None)


def reset_turn_latency(token: contextvars.Token) -> None:
    try:
        _CURRENT.reset(token)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def get_compose_role() -> Optional[str]:
    try:
        return _COMPOSE_ROLE.get()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        return None


def bind_compose_role(role: Optional[str]) -> contextvars.Token:
    try:
        return _COMPOSE_ROLE.set(str(role).strip() if role else None)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        return _COMPOSE_ROLE.set(None)


def reset_compose_role(token: contextvars.Token) -> None:
    try:
        _COMPOSE_ROLE.reset(token)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


@contextmanager
def compose_role_scope(role: str) -> Iterator[None]:
    """Bind compose accounting role for nested compose/LLM calls."""
    token = bind_compose_role(role)
    try:
        yield
    finally:
        reset_compose_role(token)


def safe_compose_role_scope(role: str):
    """Context manager bound to compose role (no-op on failure)."""

    @contextmanager
    def _cm() -> Iterator[None]:
        token = None
        try:
            token = bind_compose_role(role)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            token = None
        try:
            yield
        finally:
            if token is not None:
                try:
                    reset_compose_role(token)
                except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    pass

    return _cm()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        return default


def _safe_float_ms(started: float) -> int:
    try:
        return max(0, int((time.monotonic() - started) * 1000.0))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
    llm_call_role: str = ""

    def to_dict(self) -> Dict[str, Any]:
        role = self.llm_call_role or self.purpose or None
        return {
            "model": self.model or None,
            "provider": self.provider or None,
            "purpose": self.purpose or None,
            "llm_call_role": role,
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

    memory_update_mode: Optional[str] = None
    memory_summary_llm_ms: Optional[int] = None
    memory_summary_db_ms: Optional[int] = None
    memory_summarise_deferred_scheduled: Optional[bool] = None

    _open_spans: Dict[str, float] = field(default_factory=dict, repr=False)
    _finalized: bool = field(default=False, repr=False)
    _snapshot: Optional[Dict[str, Any]] = field(default=None, repr=False)
    _brain_boundary_monotonic: Optional[float] = field(default=None, repr=False)
    _webhook_pre_persist_start: Optional[float] = field(default=None, repr=False)
    _post_brain_dispatch_start: Optional[float] = field(default=None, repr=False)
    _accountable_once: set[str] = field(default_factory=set, repr=False)

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
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    # ── span API ──────────────────────────────────────────────────

    def start(self, name: str) -> None:
        try:
            key = str(name or "").strip()
            if not key:
                return
            self._open_spans[key] = time.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            return None

    def record_accountable_once(self, name: str, duration_ms: Any) -> Optional[int]:
        """Record an accountable span only if not already present (first wins)."""
        try:
            key = str(name or "").strip()
            if not key:
                return None
            if key in ACCOUNTABLE_LEAF and (
                key in self._accountable_once
                or int(self.span_counts.get(key, 0) or 0) > 0
            ):
                return int(self.spans_ms.get(key, 0) or 0)
            recorded = self.record_ms(key, duration_ms)
            if recorded is not None and key in ACCOUNTABLE_LEAF:
                self._accountable_once.add(key)
            return recorded
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            return None

    def record_guards_exclusive(self, wall_ms: Any) -> Optional[int]:
        """Record guards exclusive of nested quality_recompose (same wall once).

        Keeps both leaves observable: full wall as DETAIL ``guards_wall``,
        exclusive remainder as ACCOUNTABLE ``guards``.
        """
        try:
            wall = max(0, _safe_int(wall_ms, 0))
            self.record_ms("guards_wall", wall)
            nested = max(0, int(self.spans_ms.get("quality_recompose", 0) or 0))
            exclusive = max(0, wall - nested)
            return self.record_ms("guards", exclusive)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            return None

    def mark_webhook_pre_persist_start(self) -> None:
        try:
            self._webhook_pre_persist_start = time.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    def flush_webhook_pre_persist(self) -> None:
        """Record ``webhook_dispatch_pre_persist`` once at inbound_persist entry."""
        try:
            started = self._webhook_pre_persist_start
            if started is None:
                return
            self._webhook_pre_persist_start = None
            self.record_ms("webhook_dispatch_pre_persist", _safe_float_ms(started))
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    def mark_brain_boundary(self) -> None:
        """Freeze brain-return wall clock for mid-turn ``brain_total_ms``."""
        try:
            self._brain_boundary_monotonic = time.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    def mark_post_brain_dispatch_start(self) -> None:
        try:
            self._post_brain_dispatch_start = time.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    def flush_post_brain_dispatch(self) -> None:
        """Record envelope ``post_brain_dispatch`` + leaf ``post_brain_remaining_prep`` at outbound_persist entry."""
        try:
            started = self._post_brain_dispatch_start
            if started is None:
                return
            self._post_brain_dispatch_start = None
            ms = _safe_float_ms(started)
            self.record_ms("post_brain_dispatch", ms)
            self.record_ms("post_brain_remaining_prep", ms)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    def set_memory_update_mode(self, mode: str) -> None:
        try:
            key = str(mode or "").strip().lower()
            if key in ("normal", "summarise", "summarise_deferred"):
                self.memory_update_mode = key
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    def set_memory_summarise_deferred_scheduled(self, scheduled: bool) -> None:
        try:
            self.memory_summarise_deferred_scheduled = bool(scheduled)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    def record_memory_summary_timing(
        self,
        *,
        llm_ms: Any = None,
        db_ms: Any = None,
    ) -> None:
        try:
            if llm_ms is not None:
                ms = max(0, _safe_int(llm_ms, 0))
                self.memory_summary_llm_ms = ms
                self.record_ms("memory_summary_llm", ms)
            if db_ms is not None:
                ms = max(0, _safe_int(db_ms, 0))
                self.memory_summary_db_ms = ms
                self.record_ms("memory_summary_db", ms)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    def brain_total_ms(self) -> int:
        try:
            boundary = self._brain_boundary_monotonic
            if boundary is not None:
                return max(0, int((boundary - self.started_monotonic) * 1000.0))
            return _safe_float_ms(self.started_monotonic)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            return 0

    def add_db_queries(self, stage: str, count: int = 1) -> None:
        try:
            key = str(stage or "").strip()
            if not key:
                return
            self.db_query_counts[key] = int(self.db_query_counts.get(key, 0) or 0) + max(
                0, int(count or 0)
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    def record_llm_call(self, **kwargs: Any) -> None:
        try:
            _purpose = str(kwargs.get("purpose") or "")
            _role = str(kwargs.get("llm_call_role") or _purpose or "")
            call = LlmCallTiming(
                model=str(kwargs.get("model") or ""),
                provider=str(kwargs.get("provider") or ""),
                purpose=_purpose,
                llm_call_role=_role,
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
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            return 0

    def accounted_ms(self) -> int:
        total = 0
        try:
            for name in ACCOUNTABLE_LEAF:
                total += int(self.spans_ms.get(name, 0) or 0)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            return 0
        return total

    def snapshot(
        self,
        *,
        finalize_total: bool = True,
        cache: bool = True,
    ) -> Dict[str, Any]:
        """Build a JSON-safe timing dict. Fail-open; never raises."""
        try:
            brain_ms = self.brain_total_ms()
            if finalize_total:
                if "total_turn" not in self.spans_ms:
                    self.record_ms("total_turn", self.total_turn_ms())
                total = int(self.spans_ms.get("total_turn") or self.total_turn_ms() or 0)
            else:
                total = brain_ms
            accounted = self.accounted_ms()
            unaccounted = max(0, total - accounted)
            percent = round((100.0 * accounted / total), 1) if total > 0 else 0.0
            pre_brain_total = sum(
                int(self.spans_ms.get(name, 0) or 0) for name in PRE_BRAIN_ACCOUNTABLE
            )
            post_brain_total = sum(
                int(self.spans_ms.get(name, 0) or 0) for name in POST_BRAIN_ACCOUNTABLE
            )
            llm_call_roles = sorted(
                {
                    str(c.llm_call_role or c.purpose or "").strip()
                    for c in self.llm_calls
                    if str(c.llm_call_role or c.purpose or "").strip()
                }
            )
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
                "brain_total_ms": brain_ms,
                "pre_brain_total_ms": pre_brain_total,
                "post_brain_total_ms": post_brain_total,
                "total_turn_ms": total,
                "accounted_ms": accounted,
                "unaccounted_ms": unaccounted,
                "accounted_percent": percent,
                "llm_call_roles": llm_call_roles,
                "accountable_stages": list(ACCOUNTABLE_LEAF),
                "envelope_stages": list(ENVELOPE_STAGES),
                "ttft_available": any(bool(c.ttft_available) for c in self.llm_calls),
                "snapshot_finalized": bool(finalize_total),
            }
            if self.memory_update_mode is not None:
                out["memory_update_mode"] = self.memory_update_mode
            if self.memory_summary_llm_ms is not None:
                out["memory_summary_llm_ms"] = int(self.memory_summary_llm_ms)
            if self.memory_summary_db_ms is not None:
                out["memory_summary_db_ms"] = int(self.memory_summary_db_ms)
            if self.memory_summarise_deferred_scheduled is not None:
                out["memory_summarise_deferred_scheduled"] = bool(
                    self.memory_summarise_deferred_scheduled
                )
            if finalize_total:
                out["end_to_end_total_ms"] = total
            if cache:
                self._snapshot = out
                self._finalized = bool(finalize_total)
            return out
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            try:
                logger.debug("[TURN_LATENCY] emit failed", exc_info=False)
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                pass


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
    *,
    finalize_total: bool = False,
) -> None:
    """Attach a fresh ``turn_timing`` snapshot into message metadata. Fail-open."""
    try:
        if metadata is None or timing is None:
            return
        metadata["turn_timing"] = timing.snapshot(
            finalize_total=finalize_total,
            cache=False,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def refresh_turn_latency_on_outbound_message(
    db: Any,
    timing: Optional[TurnLatency],
    *,
    message_event_id: Optional[int] = None,
) -> bool:
    """Write final turn_timing onto a persisted outbound MessageEvent. Fail-open."""
    try:
        if db is None or timing is None or not message_event_id:
            return False
        snap = timing.snapshot(finalize_total=True, cache=True)
        from models import MessageEvent  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        row = (
            db.query(MessageEvent)
            .filter(MessageEvent.id == int(message_event_id))
            .first()
        )
        if row is None:
            return False
        meta = dict(row.extra_metadata or {})
        meta["turn_timing"] = snap
        row.extra_metadata = meta
        flag_modified(row, "extra_metadata")
        db.add(row)
        db.flush()
        return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        # Never rollback the ambient session here — that would undo outbound
        # persist / send stamps. Fail-open means abandon the refresh only.
        return False


def safe_span(name: str):
    """Context manager bound to current turn latency (no-op if unbound)."""

    @contextmanager
    def _cm() -> Iterator[None]:
        timing = None
        try:
            timing = get_turn_latency()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            timing = None
        if timing is None:
            yield
            return
        started = False
        try:
            timing.start(name)
            started = True
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            started = False
        try:
            yield
        finally:
            if started:
                try:
                    timing.end(name)
                except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    pass

    return _cm()


def safe_record_ms(name: str, duration_ms: Any) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.record_ms(name, duration_ms)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_record_guards_exclusive(wall_ms: Any) -> None:
    """Record guards exclusive of nested quality_recompose; fail-open."""
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.record_guards_exclusive(wall_ms)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_record_accountable_once(name: str, duration_ms: Any) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.record_accountable_once(name, duration_ms)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_mark_webhook_pre_persist_start() -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.mark_webhook_pre_persist_start()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_flush_webhook_pre_persist() -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.flush_webhook_pre_persist()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_mark_brain_boundary() -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.mark_brain_boundary()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_mark_post_brain_dispatch_start() -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.mark_post_brain_dispatch_start()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_flush_post_brain_dispatch() -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.flush_post_brain_dispatch()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_set_memory_update_mode(mode: str) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.set_memory_update_mode(mode)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_set_memory_summarise_deferred_scheduled(scheduled: bool) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.set_memory_summarise_deferred_scheduled(scheduled)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_record_memory_summary_timing(**kwargs: Any) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.record_memory_summary_timing(**kwargs)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_record_llm_call(**kwargs: Any) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.record_llm_call(**kwargs)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def safe_record_lock(**kwargs: Any) -> None:
    try:
        timing = get_turn_latency()
        if timing is None:
            return
        timing.record_lock(**kwargs)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def attach_timing_to_trace_extra(extra: Optional[MutableMapping[str, Any]], timing: Optional[TurnLatency]) -> None:
    """Attach a JSON-safe timing *snapshot* only (never the live object)."""
    try:
        if extra is None or timing is None:
            return
        extra["turn_timing_snapshot"] = timing.snapshot(finalize_total=False)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass


def timing_from_trace_extra(extra: Optional[Mapping[str, Any]]) -> Optional[TurnLatency]:
    """Deprecated helper — live objects are not stored on trace.extra."""
    try:
        return get_turn_latency()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        return None


__all__ = [
    "ACCOUNTABLE_LEAF",
    "ACCOUNTABLE_STAGES",
    "DETAIL_STAGES",
    "ENVELOPE_STAGES",
    "POST_BRAIN_ACCOUNTABLE",
    "PRE_BRAIN_ACCOUNTABLE",
    "LlmCallTiming",
    "TurnLatency",
    "attach_timing_to_trace_extra",
    "bind_compose_role",
    "bind_turn_latency",
    "compose_role_scope",
    "get_compose_role",
    "get_turn_latency",
    "merge_turn_latency_into_metadata",
    "new_turn_latency",
    "refresh_turn_latency_on_outbound_message",
    "reset_compose_role",
    "reset_turn_latency",
    "safe_compose_role_scope",
    "safe_flush_post_brain_dispatch",
    "safe_flush_webhook_pre_persist",
    "safe_mark_brain_boundary",
    "safe_mark_post_brain_dispatch_start",
    "safe_mark_webhook_pre_persist_start",
    "safe_record_memory_summary_timing",
    "safe_record_accountable_once",
    "safe_record_guards_exclusive",
    "safe_set_memory_summarise_deferred_scheduled",
    "safe_set_memory_update_mode",
    "safe_record_llm_call",
    "safe_record_lock",
    "safe_record_ms",
    "safe_span",
    "timing_from_trace_extra",
]
