"""Phase 2.7B knowledge-grounding acceptance contract: load, validate, score.

Phase 2.7A proved the commerce turn is grounded in structured Salla evidence.
This contract covers the other half: every product or store-information turn
must actually consult the merchant's own knowledge base, and whatever it finds
must stay subordinate to structured commerce facts.

The module is read-only and offline.  It loads the checked-in matrix, fails
closed on a tampered or incomplete one, and scores a turn artifact against one
case.  It never runs a turn, never reaches a provider, and never writes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy.orm.attributes import flag_modified

KNOWLEDGE_CONTRACT_VERSION = "commerce_v2_phase_2_7b_knowledge_acceptance_v1"
KNOWLEDGE_MATRIX_PATH = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "commerce_agent_v2_whatsapp"
    / "phase_2_7b_knowledge_acceptance_v1.json"
)
KNOWLEDGE_CASES_TOTAL = 16
KNOWLEDGE_TENANT_ID = 1

# Commercial truth is Salla's. A knowledge section may never carry these.
COMMERCIAL_FACT_KINDS = frozenset(
    {
        "price",
        "sale_price",
        "regular_price",
        "currency",
        "availability",
        "stock_quantity",
        "product_url",
        "image_url",
        "order_total",
        "order_status",
        "shipment_status",
        "tracking_number",
    }
)
KNOWLEDGE_EVIDENCE_PREFIX = "kb:section:"
MAX_KNOWLEDGE_LOOKUPS = 4

_REQUIRED_CASE_KEYS = frozenset({"case_id", "title", "input", "expected", "required_assertions"})

# The run record lives on its own control row, written before the first case so
# a finished run never has to be reconstructed afterwards.
CONTROL_DIRECTION = "internal_e2e_control"
CONTROL_EVENT_TYPE = "internal_e2e_knowledge_acceptance_phase_2_7b"

REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
CLASS_MACHINE_GATE_FAILED = "KNOWLEDGE_MACHINE_GATE_FAILED"
CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING = (
    "KNOWLEDGE_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING"
)
CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED = (
    "KNOWLEDGE_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED"
)
CLASS_ACCEPTANCE_PASSED = "KNOWLEDGE_ACCEPTANCE_PASSED"

_REVIEW_ONLY_RUN_KEYS = frozenset(
    {"review_status", "review", "acceptance_passed", "classification"}
)
_REVIEW_ONLY_CASE_KEYS = frozenset({"review", "acceptance_passed"})

logger = logging.getLogger("nahla.commerce_v2.phase_2_7b_knowledge")

SubmitCase = Callable[..., Awaitable[Mapping[str, Any]]]


class KnowledgeAcceptanceError(RuntimeError):
    """Fail-closed contract error for the Phase 2.7B knowledge matrix."""


def _fail(code: str) -> KnowledgeAcceptanceError:
    return KnowledgeAcceptanceError(f"phase_2_7b_{code}")


@dataclass(frozen=True)
class KnowledgeCase:
    case_id: str
    title: str
    input: str
    knowledge_fixture: str
    expected: Mapping[str, Any]
    required_assertions: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeMatrix:
    contract_version: str
    matrix_name: str
    tenant_id: int
    matrix_sha256: str
    source_authority: Mapping[str, Any]
    cases: tuple[KnowledgeCase, ...]

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.case_id for case in self.cases)

    def case(self, case_id: str) -> KnowledgeCase:
        for item in self.cases:
            if item.case_id == case_id:
                return item
        raise _fail("case_unknown")


def load_knowledge_acceptance_matrix(path: Path | None = None) -> KnowledgeMatrix:
    """Load and validate the checked-in matrix; any deviation fails closed."""
    source = Path(path or KNOWLEDGE_MATRIX_PATH)
    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        raise _fail("matrix_unreadable") from exc
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("matrix_invalid_json") from exc
    if not isinstance(raw, dict):
        raise _fail("matrix_invalid")
    if raw.get("contract_version") != KNOWLEDGE_CONTRACT_VERSION:
        raise _fail("matrix_contract_mismatch")
    if int(raw.get("tenant_id") or 0) != KNOWLEDGE_TENANT_ID:
        raise _fail("matrix_tenant_invalid")
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or len(cases_raw) != KNOWLEDGE_CASES_TOTAL:
        raise _fail("matrix_case_count_invalid")
    if int(raw.get("cases_total") or 0) != KNOWLEDGE_CASES_TOTAL:
        raise _fail("matrix_case_count_invalid")
    cases: list[KnowledgeCase] = []
    seen: set[str] = set()
    for entry in cases_raw:
        if not isinstance(entry, dict) or not _REQUIRED_CASE_KEYS.issubset(entry):
            raise _fail("matrix_case_invalid")
        case_id = str(entry["case_id"]).strip()
        if not case_id or case_id in seen:
            raise _fail("matrix_case_ids_not_unique")
        seen.add(case_id)
        expected = entry["expected"]
        assertions = entry["required_assertions"]
        if not isinstance(expected, dict) or not isinstance(assertions, list) or not assertions:
            raise _fail("matrix_case_invalid")
        if not str(entry["input"]).strip():
            raise _fail("matrix_case_invalid")
        cases.append(
            KnowledgeCase(
                case_id=case_id,
                title=str(entry["title"]),
                input=str(entry["input"]),
                knowledge_fixture=str(entry.get("knowledge_fixture") or ""),
                expected=dict(expected),
                required_assertions=tuple(str(item) for item in assertions),
            )
        )
    return KnowledgeMatrix(
        contract_version=str(raw["contract_version"]),
        matrix_name=str(raw.get("matrix_name") or ""),
        tenant_id=KNOWLEDGE_TENANT_ID,
        matrix_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        source_authority=dict(raw.get("source_authority") or {}),
        cases=tuple(cases),
    )


def _knowledge_refs(values: Any) -> set[str]:
    return {
        str(ref)
        for ref in (values or [])
        if str(ref).startswith(KNOWLEDGE_EVIDENCE_PREFIX)
    }


def score_knowledge_turn(case: KnowledgeCase, artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Score one turn artifact against one case.  Blockers are the verdict.

    Every check answers a question the owner asked of this phase: did a lookup
    actually run, was it scoped to this tenant, did structured commerce data
    stay authoritative, and did an absent or failed lookup end in a disclosure
    rather than an invention.
    """
    expected = dict(case.expected)
    blockers: list[str] = []
    lookups = list(artifact.get("knowledge_lookups") or [])
    attempted = bool(artifact.get("knowledge_lookup_attempted")) or bool(lookups)
    statuses = {str(item.get("status") or "") for item in lookups}
    scopes = {str(item.get("scope") or "") for item in lookups}
    reply = dict(artifact.get("structured_reply") or {})
    claims = list(reply.get("fact_claims") or [])
    cited = _knowledge_refs(reply.get("evidence_refs"))
    cited |= {
        str(claim.get("evidence_ref"))
        for claim in claims
        if str(claim.get("evidence_ref") or "").startswith(KNOWLEDGE_EVIDENCE_PREFIX)
    }

    if expected.get("knowledge_lookup_required") and not attempted:
        blockers.append("knowledge_lookup_not_attempted")
    for scope in expected.get("required_scopes") or []:
        if attempted and str(scope) not in scopes:
            blockers.append(f"knowledge_scope_missing:{scope}")
    allowed_statuses = {str(item) for item in (expected.get("allowed_statuses") or [])}
    if allowed_statuses and statuses and not (statuses & allowed_statuses):
        blockers.append("knowledge_status_unexpected")

    for claim in claims:
        kind = str(claim.get("kind") or "")
        ref = str(claim.get("evidence_ref") or "")
        if kind in COMMERCIAL_FACT_KINDS and ref.startswith(KNOWLEDGE_EVIDENCE_PREFIX):
            blockers.append("commercial_fact_sourced_from_knowledge")

    citation = str(expected.get("knowledge_citation") or "optional")
    if citation == "required" and not cited:
        blockers.append("knowledge_claim_missing_evidence")
    if citation == "forbidden" and cited:
        blockers.append("knowledge_cited_when_forbidden")

    if expected.get("absence_disclosure_required"):
        disclosed = bool(artifact.get("knowledge_gap_disclosure")) or bool(
            reply.get("safe_fallback_reason")
        )
        if not disclosed:
            blockers.append("absent_knowledge_not_disclosed")

    if expected.get("cross_tenant_forbidden"):
        foreign = [
            item
            for item in lookups
            if int(item.get("tenant_id") or 0) != KNOWLEDGE_TENANT_ID
        ]
        if foreign or cited:
            blockers.append("cross_tenant_knowledge_exposed")

    conflicts = list(artifact.get("knowledge_conflicts") or [])
    for kind in expected.get("expected_conflicts") or []:
        if not any(str(item.get("kind")) == str(kind) for item in conflicts):
            blockers.append(f"knowledge_conflict_not_detected:{kind}")
    for conflict in conflicts:
        if str(conflict.get("resolution")) != "structured_catalog_wins":
            blockers.append("knowledge_conflict_resolution_invalid")

    max_lookups = int(expected.get("max_lookups") or MAX_KNOWLEDGE_LOOKUPS)
    if len(lookups) > max_lookups:
        blockers.append("knowledge_lookups_unbounded")
    if expected.get("duplicate_evidence_forbidden"):
        refs = [ref for item in lookups for ref in (item.get("evidence_refs") or [])]
        if len(refs) != len(set(refs)):
            blockers.append("duplicate_knowledge_evidence")

    for tool in expected.get("expected_tools") or []:
        if str(tool) not in list(artifact.get("tool_calls") or []):
            blockers.append(f"expected_tool_missing:{tool}")

    blockers = sorted(set(blockers))
    return {
        "case_id": case.case_id,
        "passed": not blockers,
        "blockers": blockers,
        "knowledge_lookup_attempted": attempted,
        "knowledge_scopes": sorted(scope for scope in scopes if scope),
        "knowledge_statuses": sorted(status for status in statuses if status),
        "knowledge_evidence_cited": sorted(cited),
        "knowledge_conflicts": conflicts,
        "review": {
            "status": "pending",
            "assertions": [
                {
                    "assertion": assertion,
                    "index": index,
                    "verdict": "pending",
                    "human_mandatory": True,
                    "reviewer": None,
                    "reviewed_at": None,
                    "evidence": None,
                }
                for index, assertion in enumerate(case.required_assertions)
            ],
        },
    }


# ── Executable run lifecycle ─────────────────────────────────────────────────


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def machine_digest(report: Mapping[str, Any]) -> str:
    """SHA-256 over everything in a report except the human-review fields.

    Stored when a run finishes and re-checked on every review, so machine
    evidence and machine verdicts can never be edited through the review path.
    """
    frozen = {key: value for key, value in report.items() if key not in _REVIEW_ONLY_RUN_KEYS}
    frozen["cases"] = [
        {key: value for key, value in row.items() if key not in _REVIEW_ONLY_CASE_KEYS}
        for row in report.get("cases") or []
    ]
    return _canonical_sha256(frozen)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _control_rows(db: Any, *, tenant_id: int) -> list[Any]:
    from models import MessageEvent

    return (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == int(tenant_id),
            MessageEvent.direction == CONTROL_DIRECTION,
            MessageEvent.event_type == CONTROL_EVENT_TYPE,
        )
        .all()
    )


def _knowledge_control(db: Any, run_id: str, *, tenant_id: int = KNOWLEDGE_TENANT_ID) -> Any:
    """Resolve exactly one control row for this run, or fail closed."""
    try:
        parsed = uuid.UUID(str(run_id or ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise _fail("run_id_invalid") from exc
    if str(parsed) != str(run_id):
        raise _fail("run_id_invalid")
    matches = [
        row
        for row in _control_rows(db, tenant_id=tenant_id)
        if dict(row.extra_metadata or {}).get("run_id") == str(run_id)
    ]
    if len(matches) != 1:
        raise _fail("run_not_found")
    control = matches[0]
    meta = dict(control.extra_metadata or {})
    if (
        int(control.tenant_id or 0) != int(tenant_id)
        or int(meta.get("tenant_id") or 0) != int(tenant_id)
        or meta.get("synthetic") is not True
        or meta.get("test_only") is not True
    ):
        raise _fail("run_not_synthetic")
    return control


def create_knowledge_acceptance_run(
    db: Any,
    *,
    commit: str,
    conversation_id: int,
    tenant_id: int = KNOWLEDGE_TENANT_ID,
    matrix: KnowledgeMatrix | None = None,
) -> dict[str, Any]:
    """Write the official run record BEFORE the first case executes.

    Phase 2.7A executed its matrix through a CLI that never created a run row,
    so the finished report had to be reconstructed onto a control row afterwards.
    Here the record exists first: the contract version, matrix hash and commit
    are fixed at creation and the run can only be filled in, never invented later.
    """
    from models import MessageEvent

    matrix = matrix or load_knowledge_acceptance_matrix()
    if matrix.tenant_id != int(tenant_id):
        raise _fail("matrix_tenant_invalid")
    if not str(commit or "").strip():
        raise _fail("run_commit_required")
    for row in _control_rows(db, tenant_id=tenant_id):
        meta = dict(row.extra_metadata or {})
        if str(meta.get("status") or "") in {"queued", "running"}:
            raise _fail("run_already_active")
    run_id = str(uuid.uuid4())
    control = MessageEvent(
        tenant_id=int(tenant_id),
        conversation_id=int(conversation_id),
        direction=CONTROL_DIRECTION,
        body="",
        event_type=CONTROL_EVENT_TYPE,
        extra_metadata={
            "channel": "internal_e2e",
            "synthetic": True,
            "test_only": True,
            "external_egress_allowed": False,
            "run_id": run_id,
            "tenant_id": int(tenant_id),
            "contract_version": matrix.contract_version,
            "matrix_sha256": matrix.matrix_sha256,
            "commit": str(commit).strip(),
            "case_order": list(matrix.case_ids),
            "cases_total": len(matrix.cases),
            "cases_executed": 0,
            "cases_machine_passed": 0,
            "machine_passed": False,
            "status": "queued",
            "created_at": _utcnow(),
            "review_status": REVIEW_PENDING,
            "acceptance_passed": False,
            "classification": None,
        },
    )
    db.add(control)
    db.commit()
    return dict(control.extra_metadata or {})


def finalize_knowledge_acceptance(report: dict[str, Any]) -> dict[str, Any]:
    """Recompute review status, acceptance and classification from the ledger."""
    cases = list(report.get("cases") or [])
    machine_passed = bool(report.get("machine_passed"))
    total = approved = rejected = pending = 0
    for row in cases:
        review = row.setdefault("review", {"status": REVIEW_PENDING, "assertions": []})
        verdicts = [item.get("verdict") for item in review.get("assertions") or []]
        total += len(verdicts)
        approved += sum(verdict == REVIEW_APPROVED for verdict in verdicts)
        rejected += sum(verdict == REVIEW_REJECTED for verdict in verdicts)
        pending += sum(verdict == REVIEW_PENDING for verdict in verdicts)
        review["status"] = (
            REVIEW_REJECTED if REVIEW_REJECTED in verdicts
            else REVIEW_APPROVED if verdicts and all(v == REVIEW_APPROVED for v in verdicts)
            else REVIEW_PENDING
        )
        row["acceptance_passed"] = bool(row.get("passed")) and review["status"] == REVIEW_APPROVED
    review_status = (
        REVIEW_REJECTED if rejected else REVIEW_APPROVED if total and not pending else REVIEW_PENDING
    )
    acceptance_passed = machine_passed and review_status == REVIEW_APPROVED and total > 0
    if not machine_passed:
        classification = CLASS_MACHINE_GATE_FAILED
    elif review_status == REVIEW_REJECTED:
        classification = CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED
    elif acceptance_passed:
        classification = CLASS_ACCEPTANCE_PASSED
    else:
        classification = CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING
    report.update(
        {
            "review_status": review_status,
            "review": {
                "assertions_total": total,
                "approved": approved,
                "rejected": rejected,
                "pending": pending,
                "human_mandatory_cases": [row["case_id"] for row in cases],
            },
            "acceptance_passed": acceptance_passed,
            "classification": classification,
        }
    )
    return report


async def execute_knowledge_acceptance_run(
    db: Any,
    run_id: str,
    *,
    submit_case: SubmitCase,
    tenant_id: int = KNOWLEDGE_TENANT_ID,
    matrix: KnowledgeMatrix | None = None,
) -> dict[str, Any]:
    """Execute K01→K16 in order against an already-created run record.

    ``submit_case`` is the only path to a turn, and the internal channel it
    drives never reaches a provider dispatcher: no WhatsApp or customer
    outbound message can leave, and no Salla mutation is performed.
    """
    control = _knowledge_control(db, run_id, tenant_id=tenant_id)
    meta = dict(control.extra_metadata or {})
    if str(meta.get("status") or "") != "queued":
        raise _fail("run_not_queued")
    matrix = matrix or load_knowledge_acceptance_matrix()
    if meta.get("contract_version") != matrix.contract_version:
        raise _fail("run_contract_mismatch")
    if meta.get("matrix_sha256") != matrix.matrix_sha256:
        raise _fail("run_matrix_hash_mismatch")
    meta["status"] = "running"
    meta["started_at"] = _utcnow()
    control.extra_metadata = meta
    flag_modified(control, "extra_metadata")
    db.commit()

    results: list[dict[str, Any]] = []
    for case in matrix.cases:
        artifact = await submit_case(db, case)
        if int(dict(artifact).get("tenant_id") or tenant_id) != int(tenant_id):
            raise _fail("case_tenant_mismatch")
        scored = score_knowledge_turn(case, artifact)
        scored.update(
            {
                "title": case.title,
                "input": case.input,
                "knowledge_fixture": case.knowledge_fixture,
                "expected": dict(case.expected),
                "required_assertions": list(case.required_assertions),
                "tool_calls": list(artifact.get("tool_calls") or []),
                "knowledge_lookups": list(artifact.get("knowledge_lookups") or []),
                "customer_visible_text": str(artifact.get("customer_visible_text") or "")[:1500],
                "external_egress_count": int(artifact.get("external_egress_count") or 0),
                "acceptance_passed": False,
            }
        )
        results.append(scored)

    machine_failed = [row["case_id"] for row in results if not row["passed"]]
    egress_total = sum(int(row.get("external_egress_count") or 0) for row in results)
    report: dict[str, Any] = {
        "run_id": str(run_id),
        "tenant_id": int(tenant_id),
        "contract_version": matrix.contract_version,
        "matrix_sha256": matrix.matrix_sha256,
        "commit": str(meta.get("commit") or ""),
        "case_order": list(matrix.case_ids),
        "cases_total": len(matrix.cases),
        "cases_executed": len(results),
        "cases_machine_passed": len(results) - len(machine_failed),
        "cases_machine_failed": machine_failed,
        "machine_passed": not machine_failed and egress_total == 0,
        "machine_summary": f"{len(results) - len(machine_failed)}/{len(matrix.cases)} machine checks",
        "external_egress_total": egress_total,
        "cases": results,
    }
    finalize_knowledge_acceptance(report)
    return persist_knowledge_report(db, run_id, report, tenant_id=tenant_id)


def persist_knowledge_report(
    db: Any,
    run_id: str,
    report: Mapping[str, Any],
    *,
    tenant_id: int = KNOWLEDGE_TENANT_ID,
) -> dict[str, Any]:
    """Store the finished report and its machine digest on the run record."""
    control = _knowledge_control(db, run_id, tenant_id=tenant_id)
    meta = dict(control.extra_metadata or {})
    stored = json.loads(json.dumps(dict(report), ensure_ascii=False, default=str))
    meta.update(
        {
            "status": "completed",
            "finished_at": _utcnow(),
            "report": stored,
            "machine_digest": machine_digest(stored),
            **{
                key: stored[key]
                for key in (
                    "cases_executed",
                    "cases_machine_passed",
                    "cases_machine_failed",
                    "machine_passed",
                    "machine_summary",
                    "external_egress_total",
                    "review_status",
                    "review",
                    "acceptance_passed",
                    "classification",
                )
            },
        }
    )
    control.extra_metadata = meta
    flag_modified(control, "extra_metadata")
    db.commit()
    return dict(control.extra_metadata or {})


def knowledge_run_status(
    db: Any, run_id: str, *, tenant_id: int = KNOWLEDGE_TENANT_ID
) -> dict[str, Any]:
    """Read a run record.  Read-only, and deliberately usable after
    INTERNAL_E2E has been disabled for the review phase."""
    return dict(_knowledge_control(db, run_id, tenant_id=tenant_id).extra_metadata or {})


def record_knowledge_review(
    db: Any,
    run_id: str,
    *,
    case_id: str,
    assertion_index: int,
    verdict: str,
    reviewer: str,
    evidence: str,
    tenant_id: int = KNOWLEDGE_TENANT_ID,
) -> dict[str, Any]:
    """Record one human verdict on one assertion of a finished run.

    Recording runs no turn and needs no INTERNAL_E2E grant.  The run must be a
    finished, synthetic, in-tenant run whose contract, matrix hash and machine
    digest are intact; only review fields change, and the digest is re-checked
    afterwards so machine evidence cannot be edited through this path.
    """
    verdict = str(verdict or "").strip().lower()
    if verdict not in {REVIEW_APPROVED, REVIEW_REJECTED}:
        raise _fail("review_verdict_invalid")
    reviewer = str(reviewer or "").strip()
    evidence = str(evidence or "").strip()
    if not 3 <= len(reviewer) <= 120:
        raise _fail("review_reviewer_invalid")
    if not 5 <= len(evidence) <= 2000:
        raise _fail("review_evidence_invalid")
    control = _knowledge_control(db, run_id, tenant_id=tenant_id)
    meta = dict(control.extra_metadata or {})
    if str(meta.get("status") or "") != "completed":
        raise _fail("review_run_not_finished")
    report = meta.get("report")
    if not isinstance(report, dict):
        raise _fail("review_run_not_finished")
    matrix = load_knowledge_acceptance_matrix()
    if (
        meta.get("contract_version") != matrix.contract_version
        or report.get("contract_version") != matrix.contract_version
    ):
        raise _fail("review_contract_mismatch")
    if (
        meta.get("matrix_sha256") != matrix.matrix_sha256
        or report.get("matrix_sha256") != matrix.matrix_sha256
    ):
        raise _fail("review_matrix_hash_mismatch")
    digest = meta.get("machine_digest")
    if not digest or machine_digest(report) != digest:
        raise _fail("review_machine_evidence_tampered")
    updated = json.loads(json.dumps(report, ensure_ascii=False))
    row = next((item for item in updated.get("cases") or [] if item.get("case_id") == case_id), None)
    if row is None:
        raise _fail("review_case_unknown")
    assertions = row.get("review", {}).get("assertions") or []
    if not 0 <= int(assertion_index) < len(assertions):
        raise _fail("review_assertion_unknown")
    assertions[int(assertion_index)].update(
        {
            "verdict": verdict,
            "reviewer": reviewer,
            "reviewed_at": _utcnow(),
            "evidence": evidence,
        }
    )
    finalize_knowledge_acceptance(updated)
    if machine_digest(updated) != digest:
        raise _fail("review_machine_evidence_tampered")
    meta["report"] = updated
    meta.update(
        {key: updated[key] for key in ("review_status", "review", "acceptance_passed", "classification")}
    )
    control.extra_metadata = meta
    flag_modified(control, "extra_metadata")
    db.commit()
    return dict(control.extra_metadata or {})


__all__ = [
    "COMMERCIAL_FACT_KINDS",
    "CLASS_ACCEPTANCE_PASSED",
    "CLASS_MACHINE_GATE_FAILED",
    "CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING",
    "CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED",
    "CONTROL_EVENT_TYPE",
    "create_knowledge_acceptance_run",
    "execute_knowledge_acceptance_run",
    "finalize_knowledge_acceptance",
    "knowledge_run_status",
    "machine_digest",
    "persist_knowledge_report",
    "record_knowledge_review",
    "KNOWLEDGE_CASES_TOTAL",
    "KNOWLEDGE_CONTRACT_VERSION",
    "KNOWLEDGE_MATRIX_PATH",
    "KnowledgeAcceptanceError",
    "KnowledgeCase",
    "KnowledgeMatrix",
    "load_knowledge_acceptance_matrix",
    "score_knowledge_turn",
]
