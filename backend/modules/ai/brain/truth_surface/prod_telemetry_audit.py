"""
prod_telemetry_audit.py
───────────────────────
Read-only acceptance checks for Trusted Context shadow production telemetry.

Accepts log lines from stdin or a local file. No DB, no WhatsApp, no flags, no
production fetches.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_MIN_SAMPLES_FOR_PASS = 20

_EVENT_MARKER = "[TRUSTED_CONTEXT_SHADOW]"
_SUCCESS_EVENT = "TRUSTED_CONTEXT_SHADOW"
_ERROR_KINDS = ("build_failed", "wire_failed", "layer2_failed")

# Strict safe error line: only tenant, stage, error_class key=value pairs.
_SAFE_ERROR_LINE_RE = re.compile(
    rf"^\s*.*?{re.escape(_EVENT_MARKER)}\s+"
    r"(?P<kind>build_failed|wire_failed|layer2_failed)\s+"
    r"tenant=(?P<tenant>\d+)\s+"
    r"stage=(?P<stage>[A-Za-z0-9_]+)\s+"
    r"error_class=(?P<error_class>[A-Za-z0-9_]+)\s*$",
)

# Raw-line patterns for leaks outside structured JSON.
_FORBIDDEN_RAW_PATTERNS = (
    re.compile(r'"facts"\s*:'),
    re.compile(r'"customer_phone"\s*:\s*"[0-9]{8,}"'),
    re.compile(r'\bcustomer_phone=\d{8,}\b'),
    re.compile(r'Traceback \(most recent call last\)'),
    re.compile(r'Exception:\s+\S'),
    re.compile(r'\berr(or)?=\S'),
    re.compile(r'\bmessage=\S'),
)

# JSON keys that must never appear in success telemetry payloads.
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "facts",
    "code",
    "applicable_products",
    "promotion_conditions",
    "conditions",
    "customer_phone",
    "exception",
    "error_message",
    "exc_info",
})

_PROMOTION_CONDITION_KEYS = frozenset({
    "applicable_products",
    "promotion_conditions",
    "conditions",
    "raw_conditions",
})

_BASE_ACCEPTANCE_GAPS: Tuple[str, ...] = (
    "one_snapshot_per_turn_not_provable_until_runtime_emits_turn_correlation_key",
    "coupon_offer_domain_relevance_not_provable_from_shadow_log_alone",
    "social_turn_lazy_loading_not_provable_from_shadow_log_alone",
)

_RAW_KV_LEAK_PATTERNS = (
    (re.compile(r'\bcode=(?!\*\*\*)[^ \t]{4,}', re.IGNORECASE), "raw_code_kv"),
    (re.compile(r'\bapplicable_products=', re.IGNORECASE), "raw_applicable_products_kv"),
    (re.compile(r'\bconditions=', re.IGNORECASE), "raw_conditions_kv"),
    (re.compile(r'\bpromotion_conditions=', re.IGNORECASE), "raw_promotion_conditions_kv"),
    (re.compile(r'\braw_conditions=', re.IGNORECASE), "raw_raw_conditions_kv"),
)


class TelemetryVerdict(str, Enum):
    PASS = "PASS"
    PASS_WITH_FOLLOW_UP = "PASS_WITH_FOLLOW_UP"
    FAIL = "FAIL"


@dataclass
class TelemetrySample:
    raw_line: str
    payload: Dict[str, Any] = field(default_factory=dict)
    is_success: bool = False
    is_error_event: bool = False


@dataclass
class TelemetryAuditReport:
    verdict: TelemetryVerdict
    success_event_count: int
    snapshot_event_duplicate_count: int
    forbidden_leak_count: int
    has_loader_duration: bool
    required_min_samples: int
    error_event_count: int
    unsafe_error_event_count: int
    acceptance_gaps: Tuple[str, ...] = field(default_factory=lambda: _BASE_ACCEPTANCE_GAPS)
    notes: Tuple[str, ...] = ()

    @property
    def telemetry_log_safety_verdict(self) -> TelemetryVerdict:
        return self.verdict

    def to_dict(self) -> Dict[str, Any]:
        return {
            "telemetry_log_safety_verdict": self.verdict.value,
            "verdict": self.verdict.value,
            "success_event_count": self.success_event_count,
            "snapshot_event_duplicate_count": self.snapshot_event_duplicate_count,
            "forbidden_leak_count": self.forbidden_leak_count,
            "has_loader_duration": self.has_loader_duration,
            "required_min_samples": self.required_min_samples,
            "error_event_count": self.error_event_count,
            "unsafe_error_event_count": self.unsafe_error_event_count,
            "acceptance_gaps": list(self.acceptance_gaps),
            "notes": list(self.notes),
        }


def _extract_json_payload(line: str) -> Optional[Dict[str, Any]]:
    idx = line.find("{")
    if idx < 0:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(line[idx:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _is_error_event(line: str) -> bool:
    return any(kind in line for kind in _ERROR_KINDS)


def _is_success_event(line: str, payload: Dict[str, Any]) -> bool:
    if _is_error_event(line):
        return False
    if payload.get("event") == _SUCCESS_EVENT:
        return True
    return bool(payload.get("snapshot_id"))


def _payload_has_forbidden_keys(obj: Any, *, depth: int = 0) -> List[str]:
    """Return forbidden key names found in nested dict payloads."""
    if depth > 6 or not isinstance(obj, dict):
        return []
    found: List[str] = []
    for key, value in obj.items():
        key_l = str(key).lower()
        if key_l in _FORBIDDEN_PAYLOAD_KEYS or key_l in _PROMOTION_CONDITION_KEYS:
            found.append(str(key))
        found.extend(_payload_has_forbidden_keys(value, depth=depth + 1))
    return found


def _looks_like_full_coupon_code(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or text.startswith("***"):
        return False
    return bool(re.fullmatch(r"[A-Z0-9]{4,}", text))


def _scan_payload_for_leaks(payload: Dict[str, Any]) -> List[str]:
    leaks: List[str] = []
    leaks.extend(_payload_has_forbidden_keys(payload))

    code_val = payload.get("code")
    if _looks_like_full_coupon_code(code_val):
        leaks.append("full_code")

    obs = payload.get("shadow_observability")
    if isinstance(obs, dict):
        for key in _PROMOTION_CONDITION_KEYS:
            if key in obs:
                leaks.append(key)

    return leaks


def _scan_line_for_raw_leaks(line: str) -> List[str]:
    hits: List[str] = []
    for pattern in _FORBIDDEN_RAW_PATTERNS:
        if pattern.search(line):
            hits.append(pattern.pattern)
    if re.search(r'"code"\s*:\s*"(?!(\*\*\*))[^"]{4,}"', line, re.IGNORECASE):
        hits.append("raw_code_pattern")
    for pattern, label in _RAW_KV_LEAK_PATTERNS:
        if pattern.search(line):
            hits.append(label)
    return hits


def _is_safe_error_line(line: str) -> bool:
    if not _is_error_event(line):
        return True
    if _scan_line_for_raw_leaks(line):
        return False
    return _SAFE_ERROR_LINE_RE.match(line) is not None


def parse_shadow_log_lines(lines: Sequence[str]) -> List[TelemetrySample]:
    samples: List[TelemetrySample] = []
    for line in lines:
        if _EVENT_MARKER not in line:
            continue
        payload = _extract_json_payload(line) or {}
        is_error = _is_error_event(line)
        is_success = _is_success_event(line, payload)
        samples.append(
            TelemetrySample(
                raw_line=line,
                payload=payload,
                is_success=is_success,
                is_error_event=is_error,
            ),
        )
    return samples


def _validate_min_samples_for_pass(min_samples_for_pass: int) -> int:
    if isinstance(min_samples_for_pass, bool) or not isinstance(min_samples_for_pass, int):
        raise ValueError("min_samples_for_pass must be an integer >= 1")
    if min_samples_for_pass < 1:
        raise ValueError("min_samples_for_pass must be an integer >= 1")
    return min_samples_for_pass


def audit_shadow_telemetry(
    lines: Sequence[str],
    *,
    min_samples_for_pass: int = DEFAULT_MIN_SAMPLES_FOR_PASS,
) -> TelemetryAuditReport:
    """Evaluate log lines for shadow telemetry log safety (not full production acceptance)."""
    min_samples_for_pass = _validate_min_samples_for_pass(min_samples_for_pass)
    acceptance_gaps = _BASE_ACCEPTANCE_GAPS
    samples = parse_shadow_log_lines(lines)
    success_samples = [s for s in samples if s.is_success]
    error_samples = [s for s in samples if s.is_error_event]
    notes: List[str] = []

    forbidden = 0
    for sample in success_samples:
        line_leaks = _scan_line_for_raw_leaks(sample.raw_line)
        payload_leaks = _scan_payload_for_leaks(sample.payload) if sample.payload else []
        if line_leaks or payload_leaks:
            forbidden += len(line_leaks) + len(payload_leaks)

    unsafe_error_events = sum(
        1 for sample in error_samples if not _is_safe_error_line(sample.raw_line)
    )

    if forbidden:
        notes.append("forbidden_sensitive_payload_detected")
        return TelemetryAuditReport(
            verdict=TelemetryVerdict.FAIL,
            success_event_count=len(success_samples),
            snapshot_event_duplicate_count=0,
            forbidden_leak_count=forbidden,
            has_loader_duration=False,
            required_min_samples=min_samples_for_pass,
            error_event_count=len(error_samples),
            unsafe_error_event_count=unsafe_error_events,
            acceptance_gaps=acceptance_gaps,
            notes=tuple(notes),
        )

    if unsafe_error_events:
        notes.append("unsafe_error_events_detected")
        return TelemetryAuditReport(
            verdict=TelemetryVerdict.FAIL,
            success_event_count=len(success_samples),
            snapshot_event_duplicate_count=0,
            forbidden_leak_count=0,
            has_loader_duration=False,
            required_min_samples=min_samples_for_pass,
            error_event_count=len(error_samples),
            unsafe_error_event_count=unsafe_error_events,
            acceptance_gaps=acceptance_gaps,
            notes=tuple(notes),
        )

    if not success_samples:
        return TelemetryAuditReport(
            verdict=TelemetryVerdict.PASS_WITH_FOLLOW_UP,
            success_event_count=0,
            snapshot_event_duplicate_count=0,
            forbidden_leak_count=0,
            has_loader_duration=False,
            required_min_samples=min_samples_for_pass,
            error_event_count=len(error_samples),
            unsafe_error_event_count=0,
            acceptance_gaps=acceptance_gaps,
            notes=tuple(notes) + ("no_trusted_context_shadow_success_events",),
        )

    has_loader_duration = False
    per_snapshot: Dict[str, int] = {}

    for sample in success_samples:
        snapshot_id = str(sample.payload.get("snapshot_id", ""))
        if snapshot_id:
            per_snapshot[snapshot_id] = per_snapshot.get(snapshot_id, 0) + 1
        obs = sample.payload.get("shadow_observability") or {}
        if isinstance(obs, dict) and obs.get("loader_duration_ms") is not None:
            has_loader_duration = True
        if sample.payload.get("loader_duration_ms") is not None:
            has_loader_duration = True

    snapshot_duplicates = sum(1 for count in per_snapshot.values() if count > 1)

    if snapshot_duplicates:
        notes.append("duplicate_snapshot_log_events_detected")
    if len(success_samples) < min_samples_for_pass:
        notes.append("insufficient_success_event_count")
    if not has_loader_duration:
        notes.append("loader_duration_ms_missing")

    if (
        snapshot_duplicates
        or len(success_samples) < min_samples_for_pass
        or not has_loader_duration
    ):
        verdict = TelemetryVerdict.PASS_WITH_FOLLOW_UP
    else:
        verdict = TelemetryVerdict.PASS

    return TelemetryAuditReport(
        verdict=verdict,
        success_event_count=len(success_samples),
        snapshot_event_duplicate_count=snapshot_duplicates,
        forbidden_leak_count=0,
        has_loader_duration=has_loader_duration,
        required_min_samples=min_samples_for_pass,
        error_event_count=len(error_samples),
        unsafe_error_event_count=0,
        acceptance_gaps=acceptance_gaps,
        notes=tuple(notes),
    )


__all__ = [
    "DEFAULT_MIN_SAMPLES_FOR_PASS",
    "_BASE_ACCEPTANCE_GAPS",
    "TelemetryAuditReport",
    "TelemetrySample",
    "TelemetryVerdict",
    "audit_shadow_telemetry",
    "parse_shadow_log_lines",
]
