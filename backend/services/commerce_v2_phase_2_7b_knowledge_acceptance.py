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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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


__all__ = [
    "COMMERCIAL_FACT_KINDS",
    "KNOWLEDGE_CASES_TOTAL",
    "KNOWLEDGE_CONTRACT_VERSION",
    "KNOWLEDGE_MATRIX_PATH",
    "KnowledgeAcceptanceError",
    "KnowledgeCase",
    "KnowledgeMatrix",
    "load_knowledge_acceptance_matrix",
    "score_knowledge_turn",
]
