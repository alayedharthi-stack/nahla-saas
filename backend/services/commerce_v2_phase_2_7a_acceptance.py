"""Deterministic Phase 2.7A acceptance runner for the INTERNAL_E2E channel.

The canonical acceptance matrix (A1–A4, B1–B4, C1–C4; twelve turns; no D
scenario) is a checked-in, versioned artifact.  This module loads it with
fail-closed validation, cross-checks every turn against its verbatim source
in ``corpus_v1.json``, verifies the synthetic A/B/C fixtures (including C's
synthetic order and shipment), and executes the twelve turns in canonical
order through the existing INTERNAL_E2E turn path.  Nothing here selects a
random variant, depends on a seed, or can fall back to real customer data:
every turn is bound to the ``internal_e2e:t1:customer:<alias>`` identity that
``_find_fixture`` resolves, and the runner refuses fixtures that carry a real
``customer_id``.

Execution requires the existing INTERNAL_E2E safety gates
(``assert_internal_e2e_operator_scope``); when the channel is disabled the
runner raises ``internal_e2e_disabled`` before touching any fixture or turn.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy.orm.attributes import flag_modified

from evals.commerce_agent_v2_whatsapp.scorer import score_turn
from modules.ai.commerce_agent_v2.internal_e2e_identity import (
    INTERNAL_E2E_ALIASES,
    INTERNAL_E2E_CHANNEL,
    internal_e2e_customer_identity,
)
from services.commerce_v2_internal_e2e import (
    InternalE2EContractError,
    InternalE2EFixture,
    InternalE2ETurnRequest,
    _find_fixture,
    assert_internal_e2e_operator_scope,
    submit_internal_customer_turn,
)
from services.commerce_v2_whatsapp_e2e_contract import READ_ONLY_TOOLS


ACCEPTANCE_CONTRACT_VERSION = "commerce_v2_phase_2_7a_acceptance_v1"
ACCEPTANCE_TENANT_ID = 1
ACCEPTANCE_TURN_IDS: tuple[str, ...] = (
    "A1", "A2", "A3", "A4",
    "B1", "B2", "B3", "B4",
    "C1", "C2", "C3", "C4",
)
ACCEPTANCE_OUTCOMES = frozenset({"social_reply", "grounded_reply", "safe_missing_fact"})
_EVALS_DIR = Path(__file__).resolve().parents[1] / "evals" / "commerce_agent_v2_whatsapp"
ACCEPTANCE_MATRIX_PATH = _EVALS_DIR / "phase_2_7a_acceptance_v1.json"
SOURCE_CORPUS_PATH = _EVALS_DIR / "corpus_v1.json"
SOURCE_CORPUS_CONTRACT_VERSION = "commerce_v2_real_whatsapp_e2e_v1"

_CONTROL_DIRECTION = "internal_e2e_control"
_CONTROL_EVENT_TYPE = "internal_e2e_acceptance_phase_2_7a"
_HALTING_BLOCKERS = frozenset(
    {
        "tenant_mismatch",
        "v2_not_owner",
        "v1_not_bypassed",
        "turn_not_completed",
        "external_egress",
        "external_egress_unproven",
        "guardrail_not_passed",
        "unknown_or_write_tool_observed",
        "unsupported_commercial_claims",
        "cross_tenant_leakage",
        "cross_customer_leakage",
        "duplicate_replies",
        "silent_v1_fallback",
        "write_mutations",
        "salla_mutations",
    }
)

logger = logging.getLogger("nahla.commerce_v2.phase_2_7a_acceptance")

SubmitTurn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AcceptanceTurn:
    turn_id: str
    alias: str
    sequence: int
    input: str
    expected_tools: tuple[str, ...]
    expected_outcome: str
    required_assertions: tuple[str, ...]
    requires_prior_context: bool
    fixture_prerequisites: tuple[str, ...]
    source: Mapping[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        data = asdict(self)
        data["expected_tools"] = list(self.expected_tools)
        data["required_assertions"] = list(self.required_assertions)
        data["fixture_prerequisites"] = list(self.fixture_prerequisites)
        data["source"] = dict(self.source)
        return data


@dataclass(frozen=True)
class AcceptanceMatrix:
    contract_version: str
    tenant_id: int
    turns: tuple[AcceptanceTurn, ...]
    matrix_sha256: str
    source_corpus_path: str

    @property
    def turn_ids(self) -> tuple[str, ...]:
        return tuple(turn.turn_id for turn in self.turns)

    def turns_for(self, alias: str) -> tuple[AcceptanceTurn, ...]:
        return tuple(turn for turn in self.turns if turn.alias == alias)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "tenant_id": self.tenant_id,
            "matrix_sha256": self.matrix_sha256,
            "source_corpus_path": self.source_corpus_path,
            "turns_total": len(self.turns),
            "turn_ids": list(self.turn_ids),
            "turns": [turn.to_mapping() for turn in self.turns],
        }


def _fail(code: str) -> InternalE2EContractError:
    return InternalE2EContractError(f"phase_2_7a_{code}")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _fail("matrix_missing") from exc
    except (OSError, ValueError) as exc:
        raise _fail("matrix_malformed") from exc


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_variant(corpus: Mapping[str, Any], source: Mapping[str, Any]) -> tuple[str, list[str], str]:
    """Resolve a turn's verbatim source pointer inside ``corpus_v1.json``."""
    try:
        account = next(
            acc for acc in corpus["accounts"] if acc.get("alias") == source["alias"]
        )
        segment = account["segments"][int(source["segment_index"])]
        if segment.get("category") != source["category"]:
            raise KeyError("category")
        step = segment["steps"][int(source["step_index"])]
        text = step["variants"][int(source["variant_index"])]
        return (
            str(text),
            [str(tool) for tool in step.get("expected_tools") or []],
            str(step.get("expected_outcome") or ""),
        )
    except (KeyError, IndexError, StopIteration, TypeError, ValueError) as exc:
        raise _fail("source_pointer_invalid") from exc


def load_acceptance_matrix(
    path: Path | str = ACCEPTANCE_MATRIX_PATH,
    *,
    corpus_path: Path | str = SOURCE_CORPUS_PATH,
) -> AcceptanceMatrix:
    """Load and validate the canonical matrix.  Every defect fails closed."""
    raw = _load_json(Path(path))
    if not isinstance(raw, Mapping):
        raise _fail("matrix_malformed")
    if raw.get("contract_version") != ACCEPTANCE_CONTRACT_VERSION:
        raise _fail("contract_version_mismatch")
    if int(raw.get("tenant_id") or 0) != ACCEPTANCE_TENANT_ID:
        raise _fail("tenant_must_be_1")
    if raw.get("d_scenario") is not None or "D" in raw or "d" in raw:
        raise _fail("d_scenario_forbidden")
    if list(raw.get("aliases") or []) != list(INTERNAL_E2E_ALIASES):
        raise _fail("aliases_invalid")
    rows = raw.get("turns")
    if not isinstance(rows, list) or len(rows) != len(ACCEPTANCE_TURN_IDS):
        raise _fail("turn_count_invalid")
    if int(raw.get("turns_total") or 0) != len(ACCEPTANCE_TURN_IDS):
        raise _fail("turn_count_invalid")

    corpus = _load_json(Path(corpus_path))
    if not isinstance(corpus, Mapping) or corpus.get("contract_version") != SOURCE_CORPUS_CONTRACT_VERSION:
        raise _fail("source_corpus_invalid")

    turns: list[AcceptanceTurn] = []
    sequence_by_alias: dict[str, int] = {alias: 0 for alias in INTERNAL_E2E_ALIASES}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise _fail("turn_malformed")
        turn_id = str(row.get("turn_id") or "")
        if turn_id != ACCEPTANCE_TURN_IDS[index]:
            raise _fail(f"turn_order_invalid:{turn_id or index}")
        alias = str(row.get("alias") or "")
        if alias not in INTERNAL_E2E_ALIASES or alias != turn_id[0]:
            raise _fail(f"alias_invalid:{turn_id}")
        sequence = int(row.get("sequence") or 0)
        sequence_by_alias[alias] += 1
        if sequence != sequence_by_alias[alias] or sequence != int(turn_id[1:]):
            raise _fail(f"sequence_invalid:{turn_id}")
        text = str(row.get("input") or "")
        if not text.strip() or text != text.strip() or "{" in text or "}" in text:
            raise _fail(f"input_invalid:{turn_id}")
        tools = row.get("expected_tools")
        if not isinstance(tools, list) or any(tool not in READ_ONLY_TOOLS for tool in tools):
            raise _fail(f"expected_tools_invalid:{turn_id}")
        outcome = str(row.get("expected_outcome") or "")
        if outcome not in ACCEPTANCE_OUTCOMES:
            raise _fail(f"expected_outcome_invalid:{turn_id}")
        assertions = row.get("required_assertions")
        if not isinstance(assertions, list) or not assertions or not all(
            isinstance(item, str) and item.strip() for item in assertions
        ):
            raise _fail(f"required_assertions_invalid:{turn_id}")
        if not isinstance(row.get("requires_prior_context"), bool):
            raise _fail(f"context_flag_invalid:{turn_id}")
        if row["requires_prior_context"] and sequence == 1:
            raise _fail(f"context_flag_invalid:{turn_id}")
        prerequisites = row.get("fixture_prerequisites")
        if not isinstance(prerequisites, list) or not prerequisites:
            raise _fail(f"fixture_prerequisites_invalid:{turn_id}")
        source = row.get("source")
        if not isinstance(source, Mapping) or source.get("alias") != alias:
            raise _fail(f"source_pointer_invalid:{turn_id}")
        src_text, src_tools, src_outcome = _source_variant(corpus, source)
        if src_text != text or src_tools != list(tools) or src_outcome != outcome:
            raise _fail(f"source_mismatch:{turn_id}")
        turns.append(
            AcceptanceTurn(
                turn_id=turn_id,
                alias=alias,
                sequence=sequence,
                input=text,
                expected_tools=tuple(str(tool) for tool in tools),
                expected_outcome=outcome,
                required_assertions=tuple(str(item) for item in assertions),
                requires_prior_context=bool(row["requires_prior_context"]),
                fixture_prerequisites=tuple(str(item) for item in prerequisites),
                source=dict(source),
            )
        )
    if [turn.turn_id for turn in turns] != list(ACCEPTANCE_TURN_IDS):
        raise _fail("turn_order_invalid")
    return AcceptanceMatrix(
        contract_version=ACCEPTANCE_CONTRACT_VERSION,
        tenant_id=ACCEPTANCE_TENANT_ID,
        turns=tuple(turns),
        matrix_sha256=_canonical_sha256([turn.to_mapping() for turn in turns]),
        source_corpus_path=str(raw.get("source_corpus", {}).get("path") or ""),
    )


def verify_acceptance_fixtures(
    db: Any, *, tenant_id: int = ACCEPTANCE_TENANT_ID
) -> dict[str, dict[str, Any]]:
    """Fail closed unless A/B/C synthetic fixtures (and C's order + shipment) exist.

    The runner never provisions or resets fixtures itself and never resolves a
    real customer, conversation, order or shipment: only rows bound to the
    ``internal_e2e:t1:customer:<alias>`` identity qualify.
    """
    from models import Order, OrderShipment

    if int(tenant_id) != ACCEPTANCE_TENANT_ID:
        raise _fail("tenant_must_be_1")
    fixtures: dict[str, InternalE2EFixture] = {}
    for alias in INTERNAL_E2E_ALIASES:
        try:
            fixtures[alias] = _find_fixture(db, tenant_id, alias)
        except InternalE2EContractError as exc:
            raise _fail(f"fixture_missing:{alias}") from exc
    summary: dict[str, dict[str, Any]] = {}
    for alias, fixture in fixtures.items():
        expected_identity = internal_e2e_customer_identity(tenant_id, alias)
        if fixture.customer_id is not None or fixture.identity != expected_identity:
            raise _fail("real_customer_fallback_forbidden")
        summary[alias] = {
            "conversation_id": int(fixture.conversation_id),
            "identity": fixture.identity,
            "customer_id": None,
        }
    conversation_ids = [entry["conversation_id"] for entry in summary.values()]
    if len(set(conversation_ids)) != len(conversation_ids):
        raise _fail("alias_conversations_not_isolated")

    c_fixture = fixtures["C"]
    if c_fixture.order_id is None or not c_fixture.order_number:
        raise _fail("c_order_fixture_missing")
    order = db.get(Order, int(c_fixture.order_id))
    if (
        order is None
        or int(order.tenant_id) != tenant_id
        or order.customer_id is not None
        or str(order.source or "") != INTERNAL_E2E_CHANNEL
    ):
        raise _fail("c_order_fixture_missing")
    shipment = (
        db.query(OrderShipment)
        .filter(
            OrderShipment.tenant_id == tenant_id,
            OrderShipment.order_id == int(order.id),
        )
        .one_or_none()
    )
    if shipment is None or not str(shipment.tracking_number or "").strip():
        raise _fail("c_shipment_fixture_missing")
    summary["C"].update(
        {
            "order_id": int(order.id),
            "order_number": str(order.external_order_number or ""),
            "order_source": str(order.source or ""),
            "shipment_id": int(shipment.id),
            "shipment_status": str(shipment.status or ""),
            "tracking_present": True,
        }
    )
    return summary


def _evidence(artifact: Mapping[str, Any]) -> dict[str, Any]:
    proofs = artifact.get("safety_proofs")
    proofs_map = proofs if isinstance(proofs, Mapping) else {}
    return {
        "conversation_id": artifact.get("conversation_id"),
        "customer_id": artifact.get("customer_id"),
        "trace_id": artifact.get("trace_id"),
        "session_id": artifact.get("session_id"),
        "internal_inbound_message_id": artifact.get("internal_inbound_message_id"),
        "internal_outbound_message_id": artifact.get("internal_outbound_message_id"),
        "owner": artifact.get("owner"),
        "model": artifact.get("model"),
        "model_attempts": artifact.get("model_attempts"),
        "requested_service_tier": artifact.get("requested_service_tier"),
        "tool_calls": list(artifact.get("tool_calls") or []),
        "fallback_type": artifact.get("fallback_type"),
        "guardrail_passed": artifact.get("guardrail_passed"),
        "customer_visible_text": artifact.get("customer_visible_text"),
        "structured_reply": artifact.get("structured_reply"),
        "latency_ms": artifact.get("total_runner_latency_ms"),
        "tokens": {
            "input": artifact.get("input_tokens"),
            "cached_input": artifact.get("cached_input_tokens"),
            "output": artifact.get("output_tokens"),
            "total": artifact.get("total_tokens"),
        },
        "external_egress_count": artifact.get("external_egress_count"),
        "external_egress_denials": artifact.get("external_egress_denials"),
        "cross_tenant_leakage": artifact.get("cross_tenant_leakage"),
        "cross_customer_leakage": artifact.get("cross_customer_leakage"),
        "write_mutations": artifact.get("write_mutations"),
        "salla_mutations": artifact.get("salla_mutations"),
        "duplicate_replies": artifact.get("duplicate_replies"),
        "unsupported_commercial_claims": artifact.get("unsupported_commercial_claims"),
        "silent_v1_fallback": artifact.get("silent_v1_fallback"),
        "safety_proofs_proven": bool(proofs_map)
        and all(
            isinstance(proof, Mapping) and proof.get("proven") is True
            for proof in proofs_map.values()
        ),
        "failure_reason": artifact.get("failure_reason"),
    }


def _not_executed(turn: AcceptanceTurn, reason: str) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "alias": turn.alias,
        "sequence": turn.sequence,
        "input": turn.input,
        "requires_prior_context": turn.requires_prior_context,
        "expected": {"tools": list(turn.expected_tools), "outcome": turn.expected_outcome},
        "required_assertions": list(turn.required_assertions),
        "executed": False,
        "status": "not_executed",
        "passed": False,
        "halting": False,
        "blockers": [reason],
        "evidence": None,
    }


async def run_acceptance_matrix(
    db: Any,
    *,
    env: Mapping[str, str] | None = None,
    submit_turn: SubmitTurn = submit_internal_customer_turn,
    matrix: AcceptanceMatrix | None = None,
    run_id: str | None = None,
    halt_on_first_failure: bool = False,
    service_tier: str = "auto",
) -> dict[str, Any]:
    """Execute A1→A4, B1→B4, C1→C4 sequentially and return machine-readable results.

    Gate order is deliberate: INTERNAL_E2E scope first, matrix validation second,
    fixture verification third, and only then the first turn.  A halting
    failure (state-corrupting or safety-critical) stops the sequence; remaining
    turns are reported as ``not_executed`` so the summary can never claim 12/12.
    """
    assert_internal_e2e_operator_scope(ACCEPTANCE_TENANT_ID, env=env)
    matrix = matrix or load_acceptance_matrix()
    if matrix.contract_version != ACCEPTANCE_CONTRACT_VERSION or matrix.turn_ids != ACCEPTANCE_TURN_IDS:
        raise _fail("matrix_invalid")
    fixtures = verify_acceptance_fixtures(db, tenant_id=matrix.tenant_id)
    run_id = str(run_id or uuid.uuid4())

    results: list[dict[str, Any]] = []
    halted_at: str | None = None
    halt_reason: str | None = None
    completed_by_alias: dict[str, list[str]] = {alias: [] for alias in INTERNAL_E2E_ALIASES}
    for turn in matrix.turns:
        if halted_at is not None:
            results.append(_not_executed(turn, "halted_before_execution"))
            continue
        prior = list(completed_by_alias[turn.alias])
        request = InternalE2ETurnRequest(
            tenant_id=ACCEPTANCE_TENANT_ID,
            synthetic_customer_alias=turn.alias,
            text=turn.input,
            case_id=f"P27A:{turn.turn_id}",
            service_tier=service_tier,
            expected={
                "expected_tools": list(turn.expected_tools),
                "expected_outcome": turn.expected_outcome,
                "common_turn": True,
                "turn_id": turn.turn_id,
                "contract_version": matrix.contract_version,
            },
            batch_id=f"p27a:{run_id}"[:64],
        )
        try:
            artifact = await submit_turn(db, request, env=env)
        except Exception as exc:  # noqa: BLE001 — recorded, halts the sequence
            logger.exception(
                "Phase 2.7A turn raised turn_id=%s error_class=%s", turn.turn_id, type(exc).__name__
            )
            result = _not_executed(turn, f"turn_exception:{type(exc).__name__}")
            result.update({"executed": True, "status": "exception", "halting": True})
            results.append(result)
            halted_at, halt_reason = turn.turn_id, f"turn_exception:{type(exc).__name__}"
            continue
        score = score_turn(
            {
                "case_id": request.case_id,
                "expected_tools": list(turn.expected_tools),
                "expected_outcome": turn.expected_outcome,
            },
            artifact,
        )
        blockers = list(score["blockers"])
        expected_conversation = fixtures[turn.alias]["conversation_id"]
        if artifact.get("conversation_id") != expected_conversation:
            blockers.append("alias_conversation_mismatch")
        if artifact.get("customer_id") is not None:
            blockers.append("real_customer_fallback_forbidden")
        if turn.requires_prior_context and len(prior) != turn.sequence - 1:
            blockers.append("prior_context_incomplete")
        blockers = sorted(set(blockers))
        halting = (
            artifact.get("status") != "completed"
            or int(artifact.get("external_egress_count") or 0) != 0
            or any(
                blocker in _HALTING_BLOCKERS or blocker.endswith("_unproven") or blocker.endswith("_proof_mismatch")
                for blocker in blockers
            )
            or "alias_conversation_mismatch" in blockers
            or "real_customer_fallback_forbidden" in blockers
        )
        passed = not blockers
        results.append(
            {
                "turn_id": turn.turn_id,
                "alias": turn.alias,
                "sequence": turn.sequence,
                "input": turn.input,
                "requires_prior_context": turn.requires_prior_context,
                "expected": {"tools": list(turn.expected_tools), "outcome": turn.expected_outcome},
                "required_assertions": list(turn.required_assertions),
                "executed": True,
                "status": artifact.get("status"),
                "passed": passed,
                "halting": halting,
                "blockers": blockers,
                "prior_turn_ids": prior,
                "evidence": _evidence(artifact),
            }
        )
        if artifact.get("status") == "completed":
            completed_by_alias[turn.alias].append(turn.turn_id)
        if halting or (halt_on_first_failure and not passed):
            halted_at = turn.turn_id
            halt_reason = (
                str(artifact.get("failure_reason") or "")
                or (blockers[0] if blockers else "halted")
            )

    executed = [row for row in results if row["executed"]]
    passed_rows = [row for row in results if row["passed"]]
    aliases = {
        alias: {
            "conversation_id": fixtures[alias]["conversation_id"],
            "turn_ids": [turn.turn_id for turn in matrix.turns_for(alias)],
            "completed_turn_ids": completed_by_alias[alias],
            "all_passed": all(
                row["passed"] for row in results if row["alias"] == alias
            ),
        }
        for alias in INTERNAL_E2E_ALIASES
    }
    return {
        "contract_version": matrix.contract_version,
        "matrix_sha256": matrix.matrix_sha256,
        "run_id": run_id,
        "tenant_id": ACCEPTANCE_TENANT_ID,
        "execution_mode": "INTERNAL_E2E",
        "channel": INTERNAL_E2E_CHANNEL,
        "turn_order": list(matrix.turn_ids),
        "fixtures": fixtures,
        "aliases": aliases,
        "turns_total": len(matrix.turns),
        "turns_executed": len(executed),
        "turns_passed": len(passed_rows),
        "turns_failed": len(executed) - len(passed_rows),
        "turns_not_executed": len(results) - len(executed),
        "passed": len(passed_rows) == len(matrix.turns),
        "summary": f"{len(passed_rows)}/{len(matrix.turns)}",
        "halted_at": halted_at,
        "halt_reason": halt_reason,
        "external_egress_total": sum(
            int((row.get("evidence") or {}).get("external_egress_count") or 0)
            for row in executed
        ),
        "results": results,
    }


# ── Background execution for the admin API ─────────────────────────────

def _compact_results(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the control row small: drop long reply texts, keep every result field."""
    compact = dict(report)
    compact["results"] = [
        {
            **row,
            "evidence": (
                {
                    **row["evidence"],
                    "customer_visible_text": (
                        str(row["evidence"].get("customer_visible_text") or "")[:500]
                    ),
                    "structured_reply": None,
                }
                if isinstance(row.get("evidence"), Mapping)
                else row.get("evidence")
            ),
        }
        for row in report.get("results") or []
    ]
    return compact


def _acceptance_control(db: Any, run_id: str) -> Any:
    from models import MessageEvent

    try:
        parsed = uuid.UUID(str(run_id or ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise _fail("run_id_invalid") from exc
    if str(parsed) != str(run_id):
        raise _fail("run_id_invalid")
    rows = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == ACCEPTANCE_TENANT_ID,
            MessageEvent.direction == _CONTROL_DIRECTION,
            MessageEvent.event_type == _CONTROL_EVENT_TYPE,
        )
        .all()
    )
    matches = [row for row in rows if dict(row.extra_metadata or {}).get("run_id") == str(run_id)]
    if len(matches) != 1:
        raise _fail("run_not_found")
    return matches[0]


def create_acceptance_run(
    db: Any, *, env: Mapping[str, str] | None = None, halt_on_first_failure: bool = False
) -> dict[str, Any]:
    """Gate, validate the matrix, verify fixtures, then queue a run (no turn yet)."""
    from models import MessageEvent

    assert_internal_e2e_operator_scope(ACCEPTANCE_TENANT_ID, env=env)
    matrix = load_acceptance_matrix()
    fixtures = verify_acceptance_fixtures(db, tenant_id=matrix.tenant_id)
    run_id = str(uuid.uuid4())
    control = MessageEvent(
        tenant_id=ACCEPTANCE_TENANT_ID,
        conversation_id=fixtures["A"]["conversation_id"],
        direction=_CONTROL_DIRECTION,
        body="",
        event_type=_CONTROL_EVENT_TYPE,
        extra_metadata={
            "channel": INTERNAL_E2E_CHANNEL,
            "synthetic": True,
            "test_only": True,
            "run_id": run_id,
            "contract_version": matrix.contract_version,
            "matrix_sha256": matrix.matrix_sha256,
            "turn_order": list(matrix.turn_ids),
            "halt_on_first_failure": bool(halt_on_first_failure),
            "status": "queued",
            "turns_total": len(matrix.turns),
            "turns_executed": 0,
            "turns_passed": 0,
            "external_egress_total": 0,
        },
    )
    db.add(control)
    db.commit()
    return dict(control.extra_metadata or {})


def acceptance_run_status(db: Any, run_id: str) -> dict[str, Any]:
    assert_internal_e2e_operator_scope(ACCEPTANCE_TENANT_ID)
    return dict(_acceptance_control(db, run_id).extra_metadata or {})


async def execute_acceptance_run(run_id: str) -> None:
    """Starlette background task: runs the twelve turns and persists the report."""
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        control = _acceptance_control(db, run_id)
        meta = dict(control.extra_metadata or {})
        meta["status"] = "running"
        control.extra_metadata = meta
        flag_modified(control, "extra_metadata")
        db.commit()
        report = await run_acceptance_matrix(
            db,
            run_id=run_id,
            halt_on_first_failure=bool(meta.get("halt_on_first_failure")),
        )
        control = _acceptance_control(db, run_id)
        meta = dict(control.extra_metadata or {})
        meta.update(
            {
                "status": "completed" if report["halted_at"] is None else "halted",
                **{
                    key: report[key]
                    for key in (
                        "turns_executed", "turns_passed", "turns_failed",
                        "turns_not_executed", "passed", "summary", "halted_at",
                        "halt_reason", "external_egress_total", "fixtures", "aliases",
                    )
                },
                "report": _compact_results(report),
            }
        )
        control.extra_metadata = meta
        flag_modified(control, "extra_metadata")
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception(
            "Phase 2.7A acceptance run failed run_id=%s error_class=%s", run_id, type(exc).__name__
        )
        try:
            control = _acceptance_control(db, run_id)
            meta = dict(control.extra_metadata or {})
            meta.update({"status": "failed", "failure_reason": type(exc).__name__})
            control.extra_metadata = meta
            flag_modified(control, "extra_metadata")
            db.commit()
        except Exception:  # noqa: silent-ok — original failure is logged above
            db.rollback()
    finally:
        db.close()


__all__ = [
    "ACCEPTANCE_CONTRACT_VERSION",
    "ACCEPTANCE_MATRIX_PATH",
    "ACCEPTANCE_TENANT_ID",
    "ACCEPTANCE_TURN_IDS",
    "AcceptanceMatrix",
    "AcceptanceTurn",
    "acceptance_run_status",
    "create_acceptance_run",
    "execute_acceptance_run",
    "load_acceptance_matrix",
    "run_acceptance_matrix",
    "verify_acceptance_fixtures",
]
