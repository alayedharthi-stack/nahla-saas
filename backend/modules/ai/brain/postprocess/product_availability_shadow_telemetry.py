"""Closed, privacy-safe shadow telemetry for product availability truth guard."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping, Optional

logger = logging.getLogger(
    "nahla.brain.postprocess.product_availability_shadow_telemetry",
)

SHADOW_TELEMETRY_SCHEMA_VERSION = "product_availability_shadow_v1"
_LOG_PREFIX = "[PRODUCT_AVAILABILITY_SHADOW_OBSERVATION]"

_ALLOWED_EVIDENCE_STATES = frozenset(
    {
        "resolved_available",
        "resolved_unavailable",
        "variant_options",
        "conflict",
        "unknown",
        "-",
    }
)

_turn_invocation_counts: ContextVar[Optional[MutableMapping[str, int]]] = ContextVar(
    "product_availability_shadow_turn_invocations",
    default=None,
)


@dataclass(frozen=True)
class ProductAvailabilityShadowObservation:
    schema_version: str
    tenant_id: int
    turn_fingerprint: str
    invocation_site: str
    guard_mode: str
    evidence_state: str
    conflict_type: str
    guard_action: str
    would_rewrite: bool
    reason_code: str
    customer_text_changed: bool
    additional_llm_calls: int
    guard_duration_ms: int
    duplicate_invocation: bool
    invocation_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "additional_llm_calls": self.additional_llm_calls,
            "conflict_type": self.conflict_type,
            "customer_text_changed": self.customer_text_changed,
            "duplicate_invocation": self.duplicate_invocation,
            "evidence_state": self.evidence_state,
            "guard_action": self.guard_action,
            "guard_duration_ms": self.guard_duration_ms,
            "guard_mode": self.guard_mode,
            "invocation_index": self.invocation_index,
            "invocation_site": self.invocation_site,
            "reason_code": self.reason_code,
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "turn_fingerprint": self.turn_fingerprint,
            "would_rewrite": self.would_rewrite,
        }


def reset_turn_invocation_scope() -> None:
    """Clear per-turn invocation counters (probe harness / tests)."""
    _turn_invocation_counts.set({})


def build_turn_fingerprint(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    invocation_site: str,
    turn_token: str = "",
) -> str:
    material = "|".join(
        (
            str(tenant_id or 0),
            str(conversation_id or 0),
            str(turn_token or ""),
            str(invocation_site or "unknown"),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _register_invocation(turn_fingerprint: str) -> tuple[int, bool]:
    counts = _turn_invocation_counts.get()
    if counts is None:
        counts = {}
        _turn_invocation_counts.set(counts)
    next_index = int(counts.get(turn_fingerprint, 0)) + 1
    counts[turn_fingerprint] = next_index
    return next_index, next_index > 1


def build_shadow_observation(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    invocation_site: str,
    guard_mode: str,
    evidence_state: str,
    conflict_type: str,
    guard_action: str,
    would_rewrite: bool,
    reason: str,
    customer_text_changed: bool,
    guard_duration_ms: int,
    turn_token: str = "",
) -> ProductAvailabilityShadowObservation:
    site = str(invocation_site or "unknown").strip() or "unknown"
    mode = str(guard_mode or "off").strip().lower() or "off"
    state = str(evidence_state or "-").strip() or "-"
    if state not in _ALLOWED_EVIDENCE_STATES:
        state = "unknown"
    action = str(guard_action or "allowed").strip() or "allowed"
    conflict = str(conflict_type or "-").strip() or "-"
    reason_code = str(reason or "-").strip() or "-"
    fingerprint = build_turn_fingerprint(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        invocation_site=site,
        turn_token=turn_token,
    )
    invocation_index, duplicate = _register_invocation(fingerprint)
    return ProductAvailabilityShadowObservation(
        schema_version=SHADOW_TELEMETRY_SCHEMA_VERSION,
        tenant_id=int(tenant_id or 0),
        turn_fingerprint=fingerprint,
        invocation_site=site,
        guard_mode=mode,
        evidence_state=state,
        conflict_type=conflict,
        guard_action=action,
        would_rewrite=bool(would_rewrite),
        reason_code=reason_code,
        customer_text_changed=bool(customer_text_changed),
        additional_llm_calls=0,
        guard_duration_ms=max(0, int(guard_duration_ms)),
        duplicate_invocation=duplicate,
        invocation_index=invocation_index,
    )


def emit_shadow_observation(observation: ProductAvailabilityShadowObservation) -> None:
    payload = observation.to_dict()
    try:
        logger.info("%s %s", _LOG_PREFIX, json.dumps(payload, sort_keys=True, separators=(",", ":")))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry emit must not block guard
        logger.debug("%s emit_failed", _LOG_PREFIX)


def aggregate_shadow_observations(
    observations: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate closed observation records for operator polling."""
    evaluated_turns = len(observations)
    would_rewrite_count = sum(1 for row in observations if row.get("would_rewrite"))
    duplicate_count = sum(1 for row in observations if row.get("duplicate_invocation"))
    customer_text_changed_count = sum(
        1 for row in observations if row.get("customer_text_changed")
    )
    additional_llm_calls = sum(int(row.get("additional_llm_calls") or 0) for row in observations)

    evidence_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    duration_ms_total = 0
    tenant_ids: set[int] = set()

    for row in observations:
        state = str(row.get("evidence_state") or "-")
        evidence_counts[state] = evidence_counts.get(state, 0) + 1
        action = str(row.get("guard_action") or "allowed")
        action_counts[action] = action_counts.get(action, 0) + 1
        reason = str(row.get("reason_code") or "-")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        duration_ms_total += int(row.get("guard_duration_ms") or 0)
        tenant_id = int(row.get("tenant_id") or 0)
        if tenant_id:
            tenant_ids.add(tenant_id)

    would_rewrite_rate = (
        float(would_rewrite_count) / float(evaluated_turns) if evaluated_turns else 0.0
    )
    return {
        "additional_llm_calls": additional_llm_calls,
        "customer_text_changed_count": customer_text_changed_count,
        "duplicate_invocation_count": duplicate_count,
        "evaluated_turns": evaluated_turns,
        "evidence_state_counts": dict(sorted(evidence_counts.items())),
        "guard_action_counts": dict(sorted(action_counts.items())),
        "guard_duration_ms_total": duration_ms_total,
        "reason_code_counts": dict(sorted(reason_counts.items())),
        "schema_version": SHADOW_TELEMETRY_SCHEMA_VERSION,
        "tenant_count": len(tenant_ids),
        "would_rewrite_count": would_rewrite_count,
        "would_rewrite_rate": round(would_rewrite_rate, 6),
    }


class ShadowObservationTimer:
    def __init__(self) -> None:
        self._started = time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)


__all__ = [
    "ProductAvailabilityShadowObservation",
    "SHADOW_TELEMETRY_SCHEMA_VERSION",
    "ShadowObservationTimer",
    "aggregate_shadow_observations",
    "build_shadow_observation",
    "build_turn_fingerprint",
    "emit_shadow_observation",
    "reset_turn_invocation_scope",
]
