"""Pure evaluators for the Commerce Conversation Reliability Gate.

Two evaluators live here and nothing else:

* :func:`evaluate_turn` judges ONE customer turn from structured evidence
  (tenant rows touched, provider acceptances, lifecycle terminal, guardrail
  results, fallback classification, fixture bindings). It never reads
  customer prose. A runtime fallback is never a compliant answer unless the
  expectation explicitly allows a safe missing-fact fallback (Phase 2.7B v3
  rule, PR #1085).
* :func:`evaluate_gate` judges a whole gate run from JUnit records plus the
  reviewed manifest: required tests present and passed, baseline allowances
  observed exactly (never silently broadened), no unexpected skips, no empty
  selection, and required PostgreSQL configuration present for the
  PostgreSQL tier.

No pytest import here: the evaluator is testable on its own and consumed by
``scripts/commerce_reliability_gate.py``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

# ── Closed vocabularies ──────────────────────────────────────────────────────

TERMINAL_PROVIDER_ACCEPTED = "provider_accepted"
TERMINAL_HUMAN_HANDOFF = "human_handoff"
TERMINAL_EXPLICIT_FAILURE = "explicit_delivery_failure"
TERMINALS = frozenset({
    TERMINAL_PROVIDER_ACCEPTED,
    TERMINAL_HUMAN_HANDOFF,
    TERMINAL_EXPLICIT_FAILURE,
})

FALLBACK_NONE = "none"
FALLBACK_EXPECTED_SAFE = "expected_safe_fallback"
FALLBACK_EXPECTED_DELIVERY_RECOVERY = "expected_delivery_recovery"
FALLBACK_UNEXPECTED_RUNTIME = "unexpected_runtime_fallback"
# Provenance that is missing or unrecognised is never proof of "no fallback".
FALLBACK_UNKNOWN_PROVENANCE = "unknown_provenance"
FALLBACK_KINDS = frozenset({
    FALLBACK_NONE,
    FALLBACK_EXPECTED_SAFE,
    FALLBACK_EXPECTED_DELIVERY_RECOVERY,
    FALLBACK_UNEXPECTED_RUNTIME,
    FALLBACK_UNKNOWN_PROVENANCE,
})

# Transport outcome of the provider dispatch sequence. An ambiguous attempt
# (exception, timeout, no status, or HTTP 200 without a provider message id)
# is an UNKNOWN transport outcome even where the legacy lifecycle records a
# delivery failure; the terminal vocabulary below is legacy characterisation.
TRANSPORT_ACCEPTED = "accepted"
TRANSPORT_REJECTED_DEFINITIVE = "rejected_definitive"
TRANSPORT_UNKNOWN = "unknown"
TRANSPORT_NOT_ATTEMPTED = "not_attempted"
TRANSPORT_OUTCOMES = frozenset({
    TRANSPORT_ACCEPTED, TRANSPORT_REJECTED_DEFINITIVE, TRANSPORT_UNKNOWN, TRANSPORT_NOT_ATTEMPTED,
})

# Retry-evidence contract (fail closed). A dispatch that follows an ambiguous
# attempt — or follows an accepted send in a single-send case — is permitted
# only by VALIDATED evidence of one of two kinds:
#   * prior_attempt_not_accepted: bound to the exact earlier attempt of this
#     turn, consistent with that attempt's receipt, from a VERIFIED source;
#   * upstream_idempotency_guarantee: a VERIFIED guarantee for the same
#     logical delivery and the same idempotency key on both attempts.
# The registries of verified sources / guarantees are EMPTY: neither the
# WhatsApp Cloud API nor the runtime offers a bound not-accepted proof or an
# idempotency guarantee that prevents a second customer message, so no retry
# after an ambiguous outcome is accepted until an adequate evidence contract
# exists. The presence of a retry_evidence field never authorises anything.
RETRY_EVIDENCE_KIND_NOT_ACCEPTED = "prior_attempt_not_accepted"
RETRY_EVIDENCE_KIND_IDEMPOTENT = "upstream_idempotency_guarantee"
RETRY_EVIDENCE_KINDS = frozenset({RETRY_EVIDENCE_KIND_NOT_ACCEPTED, RETRY_EVIDENCE_KIND_IDEMPOTENT})
VERIFIED_NOT_ACCEPTED_SOURCES: FrozenSet[str] = frozenset()
VERIFIED_IDEMPOTENCY_GUARANTEES: FrozenSet[str] = frozenset()

# Dimensions a turn verdict reports explicitly as evaluated / not evaluated.
DIMENSION_SEMANTIC_BINDING = "semantic_binding_resolution"
DIMENSION_GUARDRAIL_EXECUTION = "application_guardrail_execution"
DIMENSION_DURABLE_DELIVERY_RECORD = "durable_delivery_record"
DIMENSION_ORDER_IDEMPOTENCY = "order_idempotency"
DIMENSION_CONVERSATION_SERIALIZATION = "conversation_serialization"

OUTCOME_GROUNDED_REPLY = "grounded_reply"
OUTCOME_SAFE_MISSING_FACT = "safe_missing_fact"
OUTCOME_EXPLICIT_FAILURE = "explicit_failure"

BASELINE_PREFIX = "RELIABILITY_BASELINE"
UNIMPLEMENTED_PREFIX = "RELIABILITY_UNIMPLEMENTED"
RECONCILE_PREFIX = "RECONCILE"

TIER_UNIT = "unit"
TIER_POSTGRES = "postgres"
TIERS = (TIER_UNIT, TIER_POSTGRES)

# Gate modes. ACCEPTANCE is the merge/release gate: every allowance must be
# approved debt (assigned owner, explicit expiry). DIAGNOSTIC measures the
# current baseline and always reports NOT ACCEPTED; it never substitutes for
# the acceptance gate.
MODE_ACCEPTANCE = "acceptance"
MODE_DIAGNOSTIC = "diagnostic"
MODES = (MODE_ACCEPTANCE, MODE_DIAGNOSTIC)
UNASSIGNED_OWNER_PREFIX = "UNASSIGNED"

PG_REQUIRE_ENV = "NAHLA_RELIABILITY_REQUIRE_PG"
PG_ADMIN_DSN_ENV = "NAHLA_RELIABILITY_PG_ADMIN_DSN"

# Lifecycle tokens (core.inbound_lifecycle) that the runtime emits today.
LIFECYCLE_END_OK = "end_ok"
LIFECYCLE_END_DELIVERY_RECOVERED = "end_delivery_recovered"
LIFECYCLE_END_DELIVERY_FAILED = "end_delivery_failed"
LIFECYCLE_END_DROPPED = "end_dropped"
LIFECYCLE_END_UNCAUGHT = "end_uncaught_exception"


# ── Turn evaluation ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnExpectation:
    tenant_id: int
    terminal: str
    expected_outcome: str = OUTCOME_GROUNDED_REPLY
    require_evidence_refs: bool = False
    required_guardrails: Tuple[str, ...] = ()
    required_fixture_bindings: Tuple[str, ...] = ()
    max_accepted_sends: int = 1


@dataclass
class TurnEvidence:
    """Structured facts observed for one inbound turn. Never prose."""

    tenant_ids_touched: List[int] = field(default_factory=list)
    accepted_wamids: List[str] = field(default_factory=list)
    send_attempts: List[Dict[str, Any]] = field(default_factory=list)
    terminal: Optional[str] = None
    terminal_source: str = ""
    persisted_outbound: List[Dict[str, Any]] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    guardrail_results: List[Dict[str, Any]] = field(default_factory=list)
    fallback_kind: str = FALLBACK_UNKNOWN_PROVENANCE  # adapters must classify explicitly
    fixture_bindings: List[str] = field(default_factory=list)
    side_effect_keys: List[str] = field(default_factory=list)
    transport_outcome: str = ""  # optional adapter report; must agree with the derivation
    not_evaluated: List[str] = field(default_factory=list)
    notes: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnVerdict:
    passed: bool
    blockers: Tuple[str, ...]
    facts: Dict[str, Any]


def derive_terminal(
    final_token: Optional[str],
    accepted_wamids: Sequence[str],
    *,
    human_handoff: bool = False,
) -> Tuple[Optional[str], str]:
    """Map today's lifecycle token + provider acceptances to a closed terminal.

    ``end_ok`` without a provider-accepted message id is NOT a terminal: it is
    the inferred success the delivery invariant forbids. Returning ``None``
    makes :func:`evaluate_turn` raise ``missing_terminal``.
    """
    accepted = [w for w in accepted_wamids if str(w or "").strip()]
    if human_handoff:
        return TERMINAL_HUMAN_HANDOFF, "ownership:human_handoff"
    if final_token == LIFECYCLE_END_DELIVERY_FAILED:
        return TERMINAL_EXPLICIT_FAILURE, f"lifecycle:{final_token}"
    if final_token in (LIFECYCLE_END_OK, LIFECYCLE_END_DELIVERY_RECOVERED) and accepted:
        return TERMINAL_PROVIDER_ACCEPTED, f"lifecycle:{final_token}+wamid"
    if final_token in (LIFECYCLE_END_OK, LIFECYCLE_END_DELIVERY_RECOVERED):
        return None, f"lifecycle:{final_token}:inferred_without_provider_acceptance"
    return None, f"lifecycle:{final_token or 'absent'}"


def _normalize_marker(text: Any) -> str:
    return " ".join(str(text or "").split())


def _accepted_attempt(attempt: Mapping[str, Any]) -> bool:
    return bool(str(attempt.get("wamid") or "").strip()) and not attempt.get("error")


def _is_ambiguous_attempt(attempt: Mapping[str, Any]) -> bool:
    """Exception/timeout, no status, or HTTP 200 without a provider message id."""
    if attempt.get("error"):
        return True
    status = attempt.get("status")
    if status is None:
        return True
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        return True
    return status_int == 200 and not str(attempt.get("wamid") or "").strip()


def _attempt_number(attempt: Mapping[str, Any]) -> Optional[int]:
    n = attempt.get("n")
    if isinstance(n, bool) or not isinstance(n, int):
        return None
    return n


def validate_retry_evidence(
    evidence: Any,
    *,
    attempt: Mapping[str, Any],
    prior_attempts: Sequence[Mapping[str, Any]],
) -> Tuple[bool, str]:
    """(permitted, reason). Fail closed: anything not validated is rejected.

    ``attempt`` is the later dispatch carrying the evidence; ``prior_attempts``
    are this turn's earlier attempts. The reason names exactly why evidence
    was rejected so a changed cause is never absorbed.
    """
    if evidence is None:
        return False, "no_retry_evidence"
    if not isinstance(evidence, Mapping):
        return False, "not_a_mapping"
    kind = evidence.get("kind")
    if kind not in RETRY_EVIDENCE_KINDS:
        return False, "kind_unknown"
    by_n = {}
    for prior in prior_attempts:
        n = _attempt_number(prior)
        if n is not None:
            by_n[n] = prior
    ref = evidence.get("attempt")
    if isinstance(ref, bool) or not isinstance(ref, int) or ref not in by_n:
        return False, "attempt_binding_mismatch"
    prior = by_n[ref]
    if kind == RETRY_EVIDENCE_KIND_NOT_ACCEPTED:
        if _accepted_attempt(prior):
            return False, "conflicts_with_accepted_receipt"
        claimed_id = str(evidence.get("provider_message_id") or "").strip()
        if claimed_id and claimed_id != str(prior.get("wamid") or "").strip():
            return False, "refers_to_other_delivery"
        source = str(evidence.get("source") or "").strip()
        reference = str(evidence.get("reference") or "").strip()
        if not source or not reference:
            return False, "incomplete"
        if source not in VERIFIED_NOT_ACCEPTED_SOURCES:
            return False, f"source_unverified:{source}"
        return True, f"verified_not_accepted:{source}"
    guarantee = str(evidence.get("guarantee") or "").strip()
    key = str(evidence.get("idempotency_key") or "").strip()
    if not guarantee or not key:
        return False, "incomplete"
    prior_key = str(prior.get("idempotency_key") or "").strip()
    later_key = str(attempt.get("idempotency_key") or "").strip()
    if not prior_key or prior_key != key or later_key != key:
        return False, "idempotency_key_mismatch"
    if guarantee not in VERIFIED_IDEMPOTENCY_GUARANTEES:
        return False, f"idempotency_guarantee_unverified:{guarantee}"
    return True, f"verified_idempotency:{guarantee}"


def derive_transport_outcome(send_attempts: Sequence[Mapping[str, Any]]) -> str:
    attempts = [a for a in send_attempts if isinstance(a, Mapping)]
    if any(_accepted_attempt(a) for a in attempts):
        return TRANSPORT_ACCEPTED
    if any(_is_ambiguous_attempt(a) for a in attempts):
        return TRANSPORT_UNKNOWN
    if attempts:
        return TRANSPORT_REJECTED_DEFINITIVE
    return TRANSPORT_NOT_ATTEMPTED


def _group_guardrail_results(
    results: Sequence[Any], blockers: List[str],
) -> Dict[str, List[Tuple[Any, List[Mapping[str, Any]]]]]:
    """name -> ordered [(attempt, [results])]. Attempt identity is preserved:
    a documented first-pass rejection followed by a retry is two attempts;
    results that share an attempt (or carry none) form one execution."""
    per_name: Dict[str, List[Tuple[Any, List[Mapping[str, Any]]]]] = {}
    for result in results:
        if not isinstance(result, Mapping) or not str(result.get("name") or "").strip():
            blockers.append("guardrail_result_unnamed")
            continue
        name = str(result["name"]).strip()
        attempt = result.get("attempt")
        groups = per_name.setdefault(name, [])
        if groups and groups[-1][0] == attempt:
            groups[-1][1].append(result)
        elif any(existing == attempt for existing, _ in groups):
            blockers.append(f"guardrail_attempt_identity_reused:{name}:attempt={attempt}")
        else:
            groups.append((attempt, [result]))
    return per_name


def evaluate_turn(expectation: TurnExpectation, evidence: TurnEvidence) -> TurnVerdict:
    blockers: List[str] = []
    accepted = [w for w in evidence.accepted_wamids if str(w or "").strip()]
    attempts = [a for a in evidence.send_attempts if isinstance(a, Mapping)]
    if len(attempts) != len(evidence.send_attempts):
        blockers.append("send_attempt_record_malformed")

    # Null / no-op result: nothing happened at all.
    if (
        not attempts
        and not accepted
        and evidence.terminal is None
        and not evidence.persisted_outbound
    ):
        blockers.append("null_result:no_send_attempt_no_terminal")

    # Tenant isolation: every touched row must belong to the expected tenant.
    foreign = sorted({int(t) for t in evidence.tenant_ids_touched if int(t) != expectation.tenant_id})
    for tenant in foreign:
        blockers.append(f"tenant_mismatch:{tenant}")
    for row in evidence.persisted_outbound:
        row_tenant = row.get("tenant_id")
        if row_tenant is not None and int(row_tenant) != expectation.tenant_id:
            blockers.append(f"tenant_mismatch:persisted_outbound:{row_tenant}")

    # Duplicate effects: more accepted sends than allowed, or repeated
    # side-effect idempotency keys.
    if len(accepted) > expectation.max_accepted_sends:
        blockers.append(f"duplicate_effects:accepted_sends={len(accepted)}")
    if len(set(accepted)) != len(accepted):
        blockers.append("duplicate_effects:repeated_provider_message_id")
    seen_keys: set = set()
    for key in evidence.side_effect_keys:
        if key in seen_keys:
            blockers.append(f"duplicate_effects:side_effect:{key}")
        seen_keys.add(key)

    # Dispatch-sequence safety: after an ambiguous attempt (the provider may
    # have delivered) another dispatch is unsafe, and after an accepted send a
    # further dispatch is a duplicate risk when the case allows one accepted
    # send only. Only evidence validated by validate_retry_evidence permits
    # it; a bare retry_evidence field never does.
    for idx, attempt in enumerate(attempts):
        prior = attempts[:idx]
        if not prior:
            continue
        ambiguous_prior = any(_is_ambiguous_attempt(p) for p in prior)
        accepted_prior = expectation.max_accepted_sends <= 1 and any(_accepted_attempt(p) for p in prior)
        if not (ambiguous_prior or accepted_prior):
            continue
        permitted, reason = validate_retry_evidence(
            attempt.get("retry_evidence"), attempt=attempt, prior_attempts=prior,
        )
        if permitted:
            continue
        label = "unsafe_dispatch_after_ambiguous_attempt" if ambiguous_prior else "dispatch_after_accepted_send"
        blockers.append(f"{label}:{idx + 1}:{reason}")
    transport = derive_transport_outcome(attempts)
    if evidence.transport_outcome and evidence.transport_outcome != transport:
        blockers.append(
            f"transport_outcome_mismatch:reported={evidence.transport_outcome}:derived={transport}"
        )

    # Terminal contract (legacy three-value characterisation).
    if evidence.terminal is None:
        blockers.append(f"missing_terminal:{evidence.terminal_source or 'unrecorded'}")
    elif evidence.terminal not in TERMINALS:
        blockers.append(f"terminal_not_closed_enum:{evidence.terminal}")
    elif evidence.terminal != expectation.terminal:
        blockers.append(
            f"terminal_mismatch:expected={expectation.terminal}:got={evidence.terminal}"
        )
    if evidence.terminal == TERMINAL_PROVIDER_ACCEPTED and not accepted:
        blockers.append("accepted_without_provider_message_id")
    if expectation.terminal == TERMINAL_EXPLICIT_FAILURE and accepted:
        blockers.append("failure_expected_but_provider_accepted")

    # Evidence references: blank or whitespace-only entries are invalid.
    refs_raw = list(evidence.evidence_refs)
    if any(not isinstance(r, str) or not r.strip() for r in refs_raw):
        blockers.append("blank_evidence_reference")
    refs = [r.strip() for r in refs_raw if isinstance(r, str) and r.strip()]
    if expectation.require_evidence_refs and not refs:
        blockers.append("missing_required_evidence")

    # Guardrail results: contradictory results for one execution are
    # rejected; "passed" without execution is rejected; the required
    # guardrails must have passed AND executed on their final attempt.
    per_name = _group_guardrail_results(evidence.guardrail_results, blockers)
    for name, groups in per_name.items():
        for attempt, results in groups:
            passed_values = {r.get("passed") is True for r in results}
            executed_values = {r.get("executed") is True for r in results}
            if len(passed_values) > 1 or len(executed_values) > 1:
                label = "unspecified" if attempt is None else attempt
                blockers.append(f"contradictory_guardrail_result:{name}:attempt={label}")
        if any(r.get("passed") is True and r.get("executed") is not True for _, rs in groups for r in rs):
            blockers.append(f"guardrail_passed_without_execution:{name}")
    for name in expectation.required_guardrails:
        groups = per_name.get(name)
        if not groups:
            blockers.append(f"missing_required_guardrail_evidence:{name}")
            continue
        final = groups[-1][1][-1]
        if final.get("executed") is None:
            blockers.append(f"guardrail_execution_unknown:{name}")
        elif final.get("executed") is not True:
            blockers.append(f"guardrail_not_executed:{name}")
        if final.get("passed") is not True:
            blockers.append(f"guardrail_failed:{name}")

    # Fixture bindings are fixture identity labels only; they never validate
    # semantic (knowledge) binding resolution, which is reported NOT EVALUATED.
    bound = set(evidence.fixture_bindings)
    for binding in expectation.required_fixture_bindings:
        if binding not in bound:
            blockers.append(f"missing_fixture_binding:{binding}")

    # Fallback honesty (Phase 2.7B v3 rule): a runtime fallback is never a
    # compliant answer; a safe fallback stands only when the case expects it;
    # unknown provenance is never proof of "no fallback".
    kind = evidence.fallback_kind or FALLBACK_UNKNOWN_PROVENANCE
    if kind not in FALLBACK_KINDS:
        blockers.append(f"fallback_kind_not_closed_enum:{kind}")
    elif kind == FALLBACK_UNKNOWN_PROVENANCE:
        blockers.append("fallback_provenance_unknown")
    elif kind == FALLBACK_UNEXPECTED_RUNTIME:
        blockers.append("unexpected_runtime_fallback_disguised_as_success")
    elif kind == FALLBACK_EXPECTED_SAFE and expectation.expected_outcome != OUTCOME_SAFE_MISSING_FACT:
        blockers.append("fallback_not_expected_by_case")
    elif expectation.expected_outcome == OUTCOME_SAFE_MISSING_FACT and kind == FALLBACK_NONE:
        # The case expected a disclosed missing fact; a grounded-looking
        # answer with no disclosure is not that.
        if not refs:
            blockers.append("expected_safe_fallback_missing")

    coverage = {
        DIMENSION_SEMANTIC_BINDING: "not_evaluated",
        DIMENSION_GUARDRAIL_EXECUTION: "evaluated" if expectation.required_guardrails else "not_evaluated",
        DIMENSION_DURABLE_DELIVERY_RECORD: "not_evaluated",
        DIMENSION_ORDER_IDEMPOTENCY: "evaluated" if evidence.side_effect_keys else "not_evaluated",
        DIMENSION_CONVERSATION_SERIALIZATION: "not_evaluated",
    }
    not_evaluated = sorted(
        set(evidence.not_evaluated) | {k for k, v in coverage.items() if v == "not_evaluated"}
    )
    facts = {
        "accepted_sends": len(accepted),
        "send_attempts": len(attempts),
        "ambiguous_attempts": sum(1 for a in attempts if _is_ambiguous_attempt(a)),
        "transport_outcome": transport,
        "terminal": evidence.terminal,
        "terminal_source": evidence.terminal_source,
        "fallback_kind": kind,
        "tenants_touched": sorted({int(t) for t in evidence.tenant_ids_touched}),
        "coverage": coverage,
        "not_evaluated": not_evaluated,
    }
    return TurnVerdict(passed=not blockers, blockers=tuple(blockers), facts=facts)


# ── Manifest ─────────────────────────────────────────────────────────────────


def load_manifest(path: str | Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    for key in (
        "manifest_version",
        "base_sha",
        "required_modules",
        "required_tests",
        "baseline_failures",
        "unimplemented_contracts",
        "postgres",
    ):
        if key not in manifest:
            raise ValueError(f"manifest_missing_key:{key}")
    for entry in manifest["baseline_failures"]:
        for key in (
            "case_id", "owner", "evidence", "test_id", "failure_signature",
            "base_sha", "expiry", "removal_condition", "tier",
        ):
            if key not in entry:
                raise ValueError(f"baseline_entry_missing_key:{entry.get('case_id')}:{key}")
    for entry in manifest["unimplemented_contracts"]:
        for key in ("contract_id", "test_id", "signature", "planned_by", "tier", "owner", "expiry"):
            if key not in entry:
                raise ValueError(f"unimplemented_entry_missing_key:{entry.get('contract_id')}:{key}")


def allowance_index(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """nodeid → allowance entry (baseline or unimplemented), with ``kind``."""
    index: Dict[str, Dict[str, Any]] = {}
    for entry in manifest.get("baseline_failures", []):
        index[entry["test_id"]] = {**entry, "kind": "baseline", "id": entry["case_id"],
                                   "signature": entry["failure_signature"]}
    for entry in manifest.get("unimplemented_contracts", []):
        index[entry["test_id"]] = {**entry, "kind": "unimplemented", "id": entry["contract_id"],
                                   "signature": entry["signature"]}
    return index


def allowance_expired(entry: Mapping[str, Any], today: _dt.date) -> bool:
    raw = entry.get("expiry")
    if not raw:
        return False
    try:
        return _dt.date.fromisoformat(str(raw)) < today
    except ValueError:
        return True  # unreadable expiry is treated as expired: never silently extend


def format_marker(kind: str, ident: str, signature: str) -> str:
    """The exact text a harness test raises for a recorded allowance."""
    if kind not in ("baseline", "unimplemented"):
        raise ValueError(f"unknown_allowance_kind:{kind}")
    prefix = BASELINE_PREFIX if kind == "baseline" else UNIMPLEMENTED_PREFIX
    return f"{prefix}[{ident}] {signature}"


def expected_marker(entry: Mapping[str, Any]) -> str:
    return format_marker(entry["kind"], entry["id"], entry["signature"])


def sha256_of(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ── JUnit parsing ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TestRecord:
    nodeid: str
    outcome: str  # passed | failed | error | skipped | xfailed
    message: str = ""
    file: str = ""


def _local_tag(elem: ET.Element) -> str:
    return (elem.tag or "").split("}")[-1]


def _nodeid_from_testcase(tc: ET.Element) -> str:
    file = str(tc.attrib.get("file") or "").strip()
    name = str(tc.attrib.get("name") or "").strip()
    classname = str(tc.attrib.get("classname") or "").strip()
    if not file:
        # xunit2 family: reconstruct from the dotted classname.
        parts = classname.split(".")
        file = "/".join(parts) + ".py" if parts else ""
        return f"{file}::{name}" if file else name
    module_dotted = file[:-3].replace("/", ".") if file.endswith(".py") else file.replace("/", ".")
    cls = ""
    if classname.startswith(module_dotted + "."):
        cls = classname[len(module_dotted) + 1:]
    return f"{file}::{cls}::{name}" if cls else f"{file}::{name}"


def parse_junit(path: str | Path) -> List[TestRecord]:
    xml_path = Path(path)
    if not xml_path.exists():
        return []
    root = ET.parse(xml_path).getroot()
    records: List[TestRecord] = []
    for tc in root.iter():
        if _local_tag(tc) != "testcase":
            continue
        nodeid = _nodeid_from_testcase(tc)
        outcome, message = "passed", ""
        for child in list(tc):
            tag = _local_tag(child)
            if tag == "failure":
                outcome, message = "failed", str(child.attrib.get("message") or child.text or "")
            elif tag == "error":
                outcome, message = "error", str(child.attrib.get("message") or child.text or "")
            elif tag == "skipped":
                message = str(child.attrib.get("message") or "")
                outcome = "xfailed" if str(child.attrib.get("type") or "") == "pytest.xfail" else "skipped"
        records.append(TestRecord(nodeid=nodeid, outcome=outcome, message=message,
                                  file=str(tc.attrib.get("file") or "")))
    return records


# ── PostgreSQL configuration ────────────────────────────────────────────────


def resolve_postgres_config(env: Mapping[str, str]) -> Dict[str, Any]:
    required = str(env.get(PG_REQUIRE_ENV) or "").strip() == "1"
    dsn = str(env.get(PG_ADMIN_DSN_ENV) or "").strip() or None
    error = None
    if required and not dsn:
        error = f"missing_required_postgres_configuration:{PG_ADMIN_DSN_ENV}"
    return {"required": required, "dsn": dsn, "error": error}


# ── Gate evaluation ──────────────────────────────────────────────────────────


@dataclass
class GateVerdict:
    passed: bool
    tier: str
    blockers: List[str]
    warnings: List[str]
    sections: Dict[str, Any]
    mode: str = MODE_ACCEPTANCE
    accepted: bool = False
    status: str = "NOT ACCEPTED"


def acceptance_status(mode: str, blockers: Sequence[str]) -> Tuple[bool, str]:
    """Acceptance is possible only in acceptance mode with no blockers."""
    if mode == MODE_DIAGNOSTIC:
        return False, "NOT ACCEPTED (diagnostic measurement; never a merge/release verdict)"
    if blockers:
        return False, "NOT ACCEPTED"
    return True, "ACCEPTED"


def evaluate_gate(
    manifest: Mapping[str, Any],
    *,
    tier: str,
    records: Sequence[TestRecord],
    env: Mapping[str, str],
    today: Optional[_dt.date] = None,
    module_hashes: Optional[Mapping[str, str]] = None,
    mode: str = MODE_ACCEPTANCE,
) -> GateVerdict:
    if tier not in TIERS:
        raise ValueError(f"unknown_tier:{tier}")
    if mode not in MODES:
        raise ValueError(f"unknown_mode:{mode}")
    today = today or _dt.date.today()
    blockers: List[str] = []
    warnings: List[str] = []
    by_id: Dict[str, TestRecord] = {r.nodeid: r for r in records}

    if not records:
        blockers.append("empty_selection:no_tests_collected")

    # Required modules (PR #1084) — executed unchanged in the unit tier.
    module_status: List[Dict[str, Any]] = []
    for module in manifest["required_modules"]:
        if module.get("tier", TIER_UNIT) != tier:
            continue
        path = module["path"]
        executed = [r for r in records if r.file == path or r.nodeid.startswith(path + "::")]
        current_hash = (module_hashes or {}).get(path)
        hash_status = "unverified"
        if current_hash:
            hash_status = "unchanged" if current_hash == module.get("content_sha256") else "changed"
            if hash_status == "changed":
                # The PR #1084 modules must run unchanged; a legitimate edit
                # updates the manifest hash in the same reviewed PR.
                blockers.append(f"required_module_changed:{path}")
        if len(executed) < int(module.get("min_tests", 1)):
            blockers.append(f"required_module_not_executed:{path}:{len(executed)}<{module.get('min_tests', 1)}")
        module_status.append({
            "path": path, "executed": len(executed), "min_tests": module.get("min_tests", 1),
            "content_sha256_manifest": module.get("content_sha256"), "content_sha256_current": current_hash,
            "hash_status": hash_status,
        })

    # Required tests must be present and passed.
    required_results: List[Dict[str, Any]] = []
    for req in manifest["required_tests"]:
        if req.get("tier", TIER_UNIT) != tier:
            continue
        rec = by_id.get(req["nodeid"])
        if rec is None:
            blockers.append(f"missing_required_test:{req['nodeid']}")
            required_results.append({"nodeid": req["nodeid"], "outcome": "missing"})
            continue
        if rec.outcome != "passed":
            blockers.append(f"required_test_not_passed:{req['nodeid']}:{rec.outcome}")
        required_results.append({"nodeid": req["nodeid"], "outcome": rec.outcome})

    # Allowances (baseline defects and unimplemented contracts) must be
    # observed exactly: expected marker present, not expired, not passing.
    allowances = allowance_index(manifest)
    allowance_results: List[Dict[str, Any]] = []
    for nodeid, entry in allowances.items():
        if entry.get("tier", TIER_UNIT) != tier:
            continue
        rec = by_id.get(nodeid)
        status: Dict[str, Any] = {
            "kind": entry["kind"], "id": entry["id"], "test_id": nodeid,
            "owner": entry.get("owner", ""), "expiry": entry.get("expiry"),
            "signature": entry["signature"], "observed": None,
        }
        if allowance_expired(entry, today):
            blockers.append(f"expired_allowance:{entry['id']}:{entry.get('expiry')}")
        if rec is None:
            blockers.append(f"allowance_test_missing:{entry['id']}:{nodeid}")
            status["observed"] = "missing"
        elif rec.outcome == "xfailed" and _normalize_marker(rec.message) == _normalize_marker(expected_marker(entry)):
            status["observed"] = "expected_failure_observed"
        elif rec.outcome == "passed":
            blockers.append(f"reconcile:unexpected_pass:{entry['id']}")
            status["observed"] = "unexpected_pass"
        else:
            blockers.append(f"reconcile:{entry['id']}:{rec.outcome}:{rec.message[:160]}")
            status["observed"] = f"{rec.outcome}:{rec.message[:120]}"
        owner = str(entry.get("owner") or "").strip()
        owner_unassigned = (not owner) or owner.upper().startswith(UNASSIGNED_OWNER_PREFIX)
        expiry_unset = not entry.get("expiry")
        debt_sink = blockers if mode == MODE_ACCEPTANCE else warnings
        if owner_unassigned:
            debt_sink.append(f"unapproved_debt:owner_unassigned:{entry['id']}")
        if expiry_unset:
            debt_sink.append(f"unapproved_debt:expiry_unset:{entry['id']}")
        status["approved"] = not (owner_unassigned or expiry_unset)
        allowance_results.append(status)

    # Any other failure / error / skip is a gate failure.
    allowed_nodeids = set(allowances)
    allowed_skip_ids = {s["nodeid"] for s in manifest.get("allowed_skips", [])}
    for rec in records:
        if rec.outcome in ("failed", "error") and rec.nodeid not in allowed_nodeids:
            blockers.append(f"test_failed:{rec.nodeid}:{rec.message[:160]}")
        if rec.outcome == "skipped" and rec.nodeid not in allowed_skip_ids:
            blockers.append(f"unexpected_skip:{rec.nodeid}:{rec.message[:120]}")
        if rec.outcome == "xfailed" and rec.nodeid not in allowed_nodeids:
            blockers.append(f"unexpected_xfail:{rec.nodeid}:{rec.message[:120]}")

    # PostgreSQL tier requirements.
    pg = resolve_postgres_config(env)
    if tier == TIER_POSTGRES:
        if pg["error"]:
            blockers.append(pg["error"])
        if not pg["required"]:
            blockers.append(f"postgres_tier_requires_{PG_REQUIRE_ENV}=1")
        pg_tests = [t["nodeid"] for t in manifest["postgres"].get("tests", [])]
        for nodeid in pg_tests:
            rec = by_id.get(nodeid)
            if rec is None:
                blockers.append(f"postgres_test_not_executed:{nodeid}")
            elif rec.outcome == "skipped":
                blockers.append(f"postgres_test_skipped:{nodeid}:{rec.message[:120]}")

    counts = {
        "collected": len(records),
        "passed": sum(1 for r in records if r.outcome == "passed"),
        "xfailed": sum(1 for r in records if r.outcome == "xfailed"),
        "skipped": sum(1 for r in records if r.outcome == "skipped"),
        "failed": sum(1 for r in records if r.outcome in ("failed", "error")),
    }
    sections = {
        "harness_integrity": {
            "tier": tier,
            "counts": counts,
            "required_modules": module_status,
            "required_tests": required_results,
            "postgres_config": {k: (v if k != "dsn" else ("set" if v else None)) for k, v in pg.items()},
        },
        "current_runtime_behavior": [
            r["nodeid"] for r in required_results
            if r["outcome"] == "passed" and r["nodeid"].startswith("tests/commerce_reliability/test_runtime_")
        ],
        "outstanding_baseline_failures": [a for a in allowance_results if a["kind"] == "baseline"],
        "unimplemented_contracts": [a for a in allowance_results if a["kind"] == "unimplemented"],
        "future_replacement_contracts": [dict(c) for c in manifest.get("replacement_contracts", [])],
        "rollout_readiness": {
            "statement": (
                "A baseline-compatible measurement proves harness integrity and "
                "records current behaviour; it is NOT production acceptance. "
                "Acceptance requires every allowance to be approved debt (assigned "
                "owner, explicit expiry) and rollout requires every baseline "
                "allowance removed by a merged fix and every unimplemented contract "
                "implemented under its own PR."
            ),
            "mode": mode,
            "baseline_allowances_open": sum(1 for a in allowance_results if a["kind"] == "baseline"),
            "unimplemented_contracts_open": sum(1 for a in allowance_results if a["kind"] == "unimplemented"),
            "unapproved_debt": [a["id"] for a in allowance_results if not a.get("approved")],
        },
    }
    accepted, status_text = acceptance_status(mode, blockers)
    return GateVerdict(
        passed=not blockers, tier=tier, blockers=blockers, warnings=warnings, sections=sections,
        mode=mode, accepted=accepted, status=status_text,
    )


def render_gate_report(verdict: GateVerdict, *, header: Optional[Mapping[str, Any]] = None) -> str:
    lines: List[str] = []
    lines.append(
        f"COMMERCE RELIABILITY GATE — tier={verdict.tier} mode={verdict.mode} "
        f"measurement={'CONSISTENT' if verdict.passed else 'BLOCKED'} :: {verdict.status}"
    )
    if verdict.mode == MODE_DIAGNOSTIC:
        lines.append("  DIAGNOSTIC MODE measures the current baseline only. It is NOT ACCEPTED and does not "
                     "substitute for the acceptance (merge/release) gate.")
    for key, value in (header or {}).items():
        lines.append(f"  {key}={value}")
    hi = verdict.sections["harness_integrity"]
    lines.append("1. HARNESS INTEGRITY")
    lines.append(f"  counts={json.dumps(hi['counts'])}")
    for module in hi["required_modules"]:
        lines.append(
            f"  module {module['path']} executed={module['executed']} "
            f"(min {module['min_tests']}) hash={module['hash_status']}"
        )
    lines.append(f"  required_tests={len(hi['required_tests'])} "
                 f"missing={sum(1 for r in hi['required_tests'] if r['outcome'] == 'missing')} "
                 f"not_passed={sum(1 for r in hi['required_tests'] if r['outcome'] not in ('passed', 'missing'))}")
    lines.append(f"  postgres_config={json.dumps(hi['postgres_config'])}")
    lines.append("2. CURRENT RUNTIME BEHAVIOUR (real entry points, scripted models, fake providers)")
    for nodeid in verdict.sections["current_runtime_behavior"]:
        lines.append(f"  PASS {nodeid}")
    lines.append("3a. OUTSTANDING BASELINE FAILURES — empirically reproduced current implementation defects "
                 "(allowed only through the reviewed manifest)")
    for a in verdict.sections["outstanding_baseline_failures"]:
        lines.append(f"  {a['id']} observed={a['observed']} approved_debt={'yes' if a.get('approved') else 'NO'} "
                     f"owner={a['owner'] or 'UNSET'} expiry={a['expiry'] or 'UNSET'} :: {a['signature']}")
    lines.append("3b. UNIMPLEMENTED CONTRACT ALLOWANCES — future work, not coverage, never completed behaviour")
    for a in verdict.sections["unimplemented_contracts"]:
        lines.append(f"  {a['id']} observed={a['observed']} approved_debt={'yes' if a.get('approved') else 'NO'} "
                     f"owner={a['owner'] or 'UNSET'} expiry={a['expiry'] or 'UNSET'} :: {a['signature']}")
    lines.append("3c. FUTURE REPLACEMENT CONTRACTS — tracked separately; not measured by this gate")
    for c in verdict.sections.get("future_replacement_contracts", []):
        lines.append(f"  {c.get('id')} replaces={c.get('replaces')} :: {c.get('summary')}")
    rr = verdict.sections["rollout_readiness"]
    lines.append("4. ROLLOUT READINESS")
    lines.append(f"  {rr['statement']}")
    lines.append(f"  mode={rr['mode']} acceptance={verdict.status}")
    lines.append(f"  baseline_allowances_open={rr['baseline_allowances_open']} "
                 f"unimplemented_contracts_open={rr['unimplemented_contracts_open']} "
                 f"unapproved_debt={','.join(rr['unapproved_debt']) or 'none'}")
    if verdict.warnings:
        lines.append("WARNINGS")
        lines.extend(f"  {w}" for w in verdict.warnings)
    if verdict.blockers:
        lines.append("BLOCKERS")
        lines.extend(f"  {b}" for b in verdict.blockers)
    return "\n".join(lines)


__all__ = [
    "BASELINE_PREFIX", "DIMENSION_CONVERSATION_SERIALIZATION", "DIMENSION_DURABLE_DELIVERY_RECORD",
    "DIMENSION_GUARDRAIL_EXECUTION", "DIMENSION_ORDER_IDEMPOTENCY", "DIMENSION_SEMANTIC_BINDING",
    "FALLBACK_EXPECTED_DELIVERY_RECOVERY", "FALLBACK_EXPECTED_SAFE",
    "FALLBACK_KINDS", "FALLBACK_NONE", "FALLBACK_UNEXPECTED_RUNTIME", "FALLBACK_UNKNOWN_PROVENANCE",
    "GateVerdict", "TRANSPORT_ACCEPTED", "TRANSPORT_NOT_ATTEMPTED", "TRANSPORT_OUTCOMES",
    "TRANSPORT_REJECTED_DEFINITIVE", "TRANSPORT_UNKNOWN", "RETRY_EVIDENCE_KINDS",
    "RETRY_EVIDENCE_KIND_IDEMPOTENT", "RETRY_EVIDENCE_KIND_NOT_ACCEPTED",
    "VERIFIED_IDEMPOTENCY_GUARANTEES", "VERIFIED_NOT_ACCEPTED_SOURCES", "derive_transport_outcome",
    "validate_retry_evidence",
    "LIFECYCLE_END_DELIVERY_FAILED", "LIFECYCLE_END_DELIVERY_RECOVERED", "LIFECYCLE_END_OK",
    "MODES", "MODE_ACCEPTANCE", "MODE_DIAGNOSTIC", "UNASSIGNED_OWNER_PREFIX", "acceptance_status",
    "OUTCOME_EXPLICIT_FAILURE", "OUTCOME_GROUNDED_REPLY", "OUTCOME_SAFE_MISSING_FACT",
    "PG_ADMIN_DSN_ENV", "PG_REQUIRE_ENV", "RECONCILE_PREFIX", "TERMINALS",
    "TERMINAL_EXPLICIT_FAILURE", "TERMINAL_HUMAN_HANDOFF", "TERMINAL_PROVIDER_ACCEPTED",
    "TIER_POSTGRES", "TIER_UNIT", "TIERS", "TestRecord", "TurnEvidence", "TurnExpectation",
    "TurnVerdict", "UNIMPLEMENTED_PREFIX", "allowance_expired", "allowance_index",
    "derive_terminal", "evaluate_gate", "evaluate_turn", "expected_marker", "format_marker",
    "load_manifest",
    "parse_junit", "render_gate_report", "resolve_postgres_config", "sha256_of",
]
