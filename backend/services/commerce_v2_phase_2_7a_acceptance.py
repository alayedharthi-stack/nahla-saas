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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy.orm.attributes import flag_modified

from evals.commerce_agent_v2_whatsapp.scorer import score_turn
from modules.ai.commerce_agent_v2.internal_e2e_identity import (
    INTERNAL_E2E_ALIASES,
    INTERNAL_E2E_CHANNEL,
    internal_e2e_customer_identity,
    metadata_matches_internal_e2e_identity,
)
from services.commerce_v2_internal_e2e import (
    B_SEED_HISTORY_CONTRACT,
    B_SEED_HISTORY_EVENT_TYPE,
    B_SEED_HISTORY_PAGINATION_ROWS,
    B_SEED_HISTORY_REFERENCE_ROWS,
    B_SEED_HISTORY_ROWS,
    B_SEED_REFERENCE_SLOTS,
    INTERNAL_E2E_INBOUND,
    INTERNAL_E2E_OUTBOUND,
    InternalE2EContractError,
    InternalE2EFixture,
    InternalE2ETurnRequest,
    _find_fixture,
    assert_internal_e2e_operator_scope,
    build_b_seed_history,
    normalize_seed_title,
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

# Support-access gate (production run path only; the read-only matrix route is exempt).
SUPPORT_GRANT_PURPOSE_TOKENS: tuple[str, ...] = ("phase 2.7a", "phase_2_7a", "phase-2-7a", "phase2.7a")
SUPPORT_GRANT_MIN_REMAINING = timedelta(minutes=30)

# Customer B's approved seed history: shape, content and reference selection all
# come from the seeding module, so the writer and this verifier cannot drift apart.

# Review / classification vocabulary.
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
CLASS_MACHINE_GATE_FAILED = "MACHINE_GATE_FAILED"
CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING = "MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING"
CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED = "MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED"
CLASS_ACCEPTANCE_PASSED = "ACCEPTANCE_PASSED"
_CONTINUITY_ASSERTION_TURNS = frozenset({"B1", "B2", "B3", "B4"})
# Only a safety-critical or state-corrupting condition stops the sequence.
# ``turn_not_completed`` and ``guardrail_not_passed`` are deliberately absent:
# a rejected model reply means the grounding guardrail did its job and the
# customer received the safe fallback instead. That is an ordinary quality
# failure — the turn still fails its machine check — and halting the campaign on
# it destroys the evidence for every later turn. Production Phase 2.7A runs 1 and
# 2 each stopped this way (A3, then B1), leaving seven of twelve turns unrun.
_HALTING_BLOCKERS = frozenset(
    {
        "tenant_mismatch",
        "v2_not_owner",
        "v1_not_bypassed",
        "external_egress",
        "external_egress_unproven",
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


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def require_phase_2_7a_support_grant(
    db: Any, *, tenant_id: int = ACCEPTANCE_TENANT_ID, now: datetime | None = None
) -> dict[str, Any]:
    """Fail closed unless Tenant 1 holds an ACTIVE support-access grant for Phase 2.7A.

    Reads the merchant-approved ``support_access`` block exactly as the
    support-access router writes it (``enabled``, ``expires_at``, ``reason``).
    Never creates, requests or approves a grant.
    """
    from models import TenantSettings

    if int(tenant_id) != ACCEPTANCE_TENANT_ID:
        raise _fail("support_grant_wrong_tenant")
    current = now or datetime.now(timezone.utc)
    settings = (
        db.query(TenantSettings).filter(TenantSettings.tenant_id == ACCEPTANCE_TENANT_ID).one_or_none()
    )
    grant = dict((dict(settings.extra_metadata or {}) if settings else {}).get("support_access") or {})
    if not grant or grant.get("enabled") is not True:
        raise _fail("support_grant_missing")
    expires_at = _parse_iso(grant.get("expires_at"))
    if expires_at is None:
        raise _fail("support_grant_expiry_missing")
    if current > expires_at:
        raise _fail("support_grant_expired")
    remaining = expires_at - current
    if remaining < SUPPORT_GRANT_MIN_REMAINING:
        raise _fail("support_grant_insufficient_remaining")
    purpose = str(grant.get("reason") or "").strip()
    if not any(token in purpose.lower() for token in SUPPORT_GRANT_PURPOSE_TOKENS):
        raise _fail("support_grant_purpose_mismatch")
    return {
        "tenant_id": ACCEPTANCE_TENANT_ID,
        "granted_at": grant.get("granted_at"),
        "expires_at": expires_at.isoformat(),
        "remaining_minutes": int(remaining.total_seconds() // 60),
        "purpose": purpose,
        "granted_by": grant.get("granted_by"),
    }


def _seed_reference_block(db: Any, *, b_conversation_id: int) -> dict[str, Any]:
    """The reference record persisted when B's history was seeded, validated in shape."""
    from models import Conversation

    conversation = db.get(Conversation, int(b_conversation_id))
    if conversation is None:
        raise _fail("b_seed_history_conversation_missing")
    block = dict(conversation.extra_metadata or {}).get("seed_history")
    if not isinstance(block, dict):
        raise _fail("b_seed_history_reference_metadata_missing")
    if block.get("contract") != B_SEED_HISTORY_CONTRACT:
        raise _fail("b_seed_history_reference_contract_mismatch")
    if (
        block.get("rows") != B_SEED_HISTORY_ROWS
        or block.get("pagination_rows") != B_SEED_HISTORY_PAGINATION_ROWS
        or block.get("reference_rows") != B_SEED_HISTORY_REFERENCE_ROWS
    ):
        raise _fail("b_seed_history_reference_metadata_invalid")
    references = block.get("references")
    if not isinstance(references, list) or len(references) != B_SEED_REFERENCE_SLOTS:
        raise _fail("b_seed_history_reference_metadata_invalid")
    resolved: dict[int, dict[str, Any]] = {}
    for slot, entry in enumerate(references, start=1):
        if not isinstance(entry, dict) or entry.get("slot") != slot:
            raise _fail("b_seed_history_reference_metadata_invalid")
        product_id = entry.get("product_id")
        title = normalize_seed_title(entry.get("title"))
        if not isinstance(product_id, int) or isinstance(product_id, bool) or not title:
            raise _fail("b_seed_history_reference_metadata_invalid")
        resolved[slot] = {"slot": slot, "product_id": int(product_id), "title": title}
    return {"block": block, "references": resolved}


def _verify_b_seed_history(db: Any, *, tenant_id: int, b_conversation_id: int) -> dict[str, Any]:
    """Customer B's approved seed history must be complete, ordered, B-only and untampered.

    The products the history names are read from the record persisted when it was
    seeded, never re-derived from the live catalog: the catalog is mutable, and a
    product added, renamed or retired afterwards must not change which products B
    is taken to have discussed.  Every stored row is compared against the history
    rebuilt from those persisted titles, so an edited body, a dropped row, a
    reordered sequence, a foreign conversation or a rewritten reference id all
    fail closed here rather than reaching the continuity check as silent truth.
    """
    from models import MessageEvent

    rows = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == tenant_id,
            MessageEvent.event_type == B_SEED_HISTORY_EVENT_TYPE,
        )
        .order_by(MessageEvent.id.asc())
        .all()
    )
    foreign = [row for row in rows if int(row.conversation_id or 0) != int(b_conversation_id)]
    if foreign:
        raise _fail("b_seed_history_contaminated")
    if not rows:
        raise _fail("b_seed_history_missing")
    if len(rows) != B_SEED_HISTORY_ROWS:
        raise _fail("b_seed_history_count_invalid")
    persisted = _seed_reference_block(db, b_conversation_id=b_conversation_id)
    references = persisted["references"]
    expected_history = build_b_seed_history(references[1]["title"], references[2]["title"])
    for index, (row, expected) in enumerate(zip(rows, expected_history), start=1):
        expected_direction, expected_body, expected_slot = expected
        meta = dict(row.extra_metadata or {})
        if meta.get("internal_message_id") != f"internal_e2e:t1:b:seed:{index:02d}":
            raise _fail("b_seed_history_order_invalid")
        if meta.get("seed_history") is not True or not metadata_matches_internal_e2e_identity(
            meta, tenant_id=tenant_id, alias="B"
        ):
            raise _fail("b_seed_history_metadata_mismatch")
        expected_kind = "pagination" if index <= B_SEED_HISTORY_PAGINATION_ROWS else "reference"
        if meta.get("seed_history_kind") != expected_kind:
            raise _fail("b_seed_history_metadata_mismatch")
        if str(row.direction or "") != expected_direction or not str(row.body or "").strip():
            raise _fail("b_seed_history_row_invalid")
        if str(row.body or "") != expected_body:
            raise _fail("b_seed_history_row_tampered")
        if meta.get("seed_reference_slot") != expected_slot:
            raise _fail("b_seed_history_reference_slot_mismatch")
        if expected_slot is not None:
            reference = references[expected_slot]
            if (
                meta.get("seed_reference_product_id") != reference["product_id"]
                or normalize_seed_title(meta.get("seed_reference_title")) != reference["title"]
            ):
                raise _fail("b_seed_history_reference_mismatch")
    referenced_products = [references[slot] for slot in sorted(references)]
    return {
        "rows": len(rows),
        "pagination_rows": B_SEED_HISTORY_PAGINATION_ROWS,
        "reference_rows": B_SEED_HISTORY_REFERENCE_ROWS,
        "conversation_id": int(b_conversation_id),
        "contract": B_SEED_HISTORY_CONTRACT,
        "referenced_products": referenced_products,
        "referenced_product_ids": [entry["product_id"] for entry in referenced_products],
        "referenced_titles": [entry["title"] for entry in referenced_products],
        "verified": True,
    }


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
    summary["B"]["seed_history"] = _verify_b_seed_history(
        db, tenant_id=tenant_id, b_conversation_id=fixtures["B"].conversation_id
    )

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


def _product_ids(artifact: Mapping[str, Any]) -> list[int]:
    """Product identities the reply is grounded on, from ``structured_reply.product_refs``."""
    ids: list[int] = []
    reply = artifact.get("structured_reply")
    refs = reply.get("product_refs") if isinstance(reply, Mapping) else None
    for ref in refs or []:
        try:
            value = int(ref.get("product_id")) if isinstance(ref, Mapping) else int(ref)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return ids


def _evidence(artifact: Mapping[str, Any]) -> dict[str, Any]:
    proofs = artifact.get("safety_proofs")
    proofs_map = proofs if isinstance(proofs, Mapping) else {}
    return {
        "product_ids": _product_ids(artifact),
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
        # 1 when a grounded, delivered reply also named a sub-detail the merchant
        # has not documented. Distinct from a fallback: the answer still carries
        # verified facts, so the turn's observed outcome stays grounded_reply.
        "knowledge_gap_disclosure": artifact.get("knowledge_gap_disclosure"),
        "guardrail_passed": artifact.get("guardrail_passed"),
        # 1 when the grounding guardrail rejected the model's reply and the
        # customer received the safe fallback instead. A quality failure, not a
        # delivered unsupported claim — see ``unsupported_commercial_claims``.
        "guardrail_blocked_reply": artifact.get("guardrail_blocked_reply"),
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


def _review_block(turn: AcceptanceTurn, *, machine_verified: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Per-assertion human review ledger: every required assertion starts ``pending``."""
    verified = dict(machine_verified or {})
    return {
        "status": REVIEW_PENDING,
        "assertions": [
            {
                "index": index,
                "assertion": assertion,
                "verdict": REVIEW_PENDING,
                "reviewer": None,
                "reviewed_at": None,
                "evidence": None,
                "human_mandatory": True,
                "machine_check": verified.get(assertion),
            }
            for index, assertion in enumerate(turn.required_assertions)
        ],
    }


def _continuity_assertions(turn: AcceptanceTurn) -> list[str]:
    return [
        assertion
        for assertion in turn.required_assertions
        if any(marker in assertion for marker in ("resolves", "selected", "context", "same product", "switching"))
    ]


def _check_b_continuity(
    turn: AcceptanceTurn,
    product_ids: list[int],
    state: dict[str, Any],
    seed_referenced: list[int],
) -> tuple[dict[str, Any], list[str]]:
    """Machine continuity check for B1→B4 (anchor → selection → same product).

    B1 anchors the set of products actually shown.  B2 must select exactly one of
    them, and B3/B4 must stay on it.  Seeded history never widens that set: a
    product B discussed before B1 but that B1 did not show is a fall-back to
    stale context and fails, while a product that appears in both the seeded
    history and B1 is an ordinary valid choice and is never penalised for having
    come up before.  A product outside B1 fails either way — ``seed_referenced``
    only names the failure, it never excuses it.
    """
    if turn.alias != "B":
        return {"status": "not_applicable"}, []
    blockers: list[str] = []
    if turn.turn_id == "B1":
        state["b1_shown"] = list(product_ids)
        status = "anchor_recorded" if product_ids else "unverifiable"
        return {
            "status": status, "role": "anchor", "products_shown": list(product_ids),
            "detail": None if product_ids else "no product identity in B1 reply",
        }, blockers
    shown = list(state.get("b1_shown") or [])
    if turn.turn_id == "B2":
        if not shown or not product_ids:
            state["b2_selected"] = []
            return {
                "status": "unverifiable", "role": "selection", "products": list(product_ids),
                "anchor_products": shown, "selected_product": None,
                "detail": "product identity unavailable for B1 or B2",
            }, blockers
        outside = [pid for pid in product_ids if pid not in shown]
        if outside:
            blockers.append(
                "b_continuity_seed_history_fallback"
                if all(pid in seed_referenced for pid in outside)
                else "b_continuity_selection_not_from_b1"
            )
        if len(product_ids) != 1:
            blockers.append("b_continuity_selection_ambiguous")
        state["b2_selected"] = [] if blockers else list(product_ids)
        return {
            "status": "violated" if blockers else "verified", "role": "selection",
            "products": list(product_ids), "anchor_products": shown,
            "selected_product": product_ids[0] if not blockers else None,
            "detail": ", ".join(blockers) or None,
        }, blockers
    selected = list(state.get("b2_selected") or [])
    if not selected or not product_ids:
        return {
            "status": "unverifiable", "role": "follow_up", "products": list(product_ids),
            "selected_product": selected[0] if selected else None,
            "detail": "product identity unavailable for B2 selection or this reply",
        }, blockers
    if set(product_ids) != set(selected):
        strays = [pid for pid in product_ids if pid not in selected]
        blockers.append(
            "b_continuity_seed_history_fallback"
            if strays and all(pid in seed_referenced for pid in strays)
            else "b_continuity_product_switched"
        )
    return {
        "status": "violated" if blockers else "verified", "role": "follow_up",
        "products": list(product_ids), "selected_product": selected[0],
        "detail": ", ".join(blockers) or None,
    }, blockers


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
        "machine_passed": False,
        "machine_status": "not_executed",
        "halting": False,
        "blockers": [reason],
        "continuity": {"status": "not_executed"},
        "evidence": None,
        "review": _review_block(turn),
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

    Gate order is deliberate: INTERNAL_E2E scope first, the Tenant 1 Phase 2.7A
    support-access grant second, matrix validation third, fixture verification
    (A/B/C, B's seed history, C's order and shipment) fourth, and only then the
    first turn.  The result separates the machine gate from the human review of
    the customer-visible replies; ``acceptance_passed`` needs both.  A halting
    failure (state-corrupting or safety-critical) stops the sequence; remaining
    turns are reported as ``not_executed`` so the summary can never claim 12/12.

    An ordinary quality failure — a rejected model reply, a missing expected
    tool, an unexpected safe fallback — does not halt: the turn fails its machine
    check and the sequence continues, so the run still yields evidence for every
    later turn.  Pass ``halt_on_first_failure`` to stop on any failure instead.
    """
    assert_internal_e2e_operator_scope(ACCEPTANCE_TENANT_ID, env=env)
    support_grant = require_phase_2_7a_support_grant(db, tenant_id=ACCEPTANCE_TENANT_ID)
    matrix = matrix or load_acceptance_matrix()
    if matrix.contract_version != ACCEPTANCE_CONTRACT_VERSION or matrix.turn_ids != ACCEPTANCE_TURN_IDS:
        raise _fail("matrix_invalid")
    fixtures = verify_acceptance_fixtures(db, tenant_id=matrix.tenant_id)
    seed_referenced = list(fixtures["B"]["seed_history"]["referenced_product_ids"])
    run_id = str(run_id or uuid.uuid4())

    results: list[dict[str, Any]] = []
    halted_at: str | None = None
    halt_reason: str | None = None
    completed_by_alias: dict[str, list[str]] = {alias: [] for alias in INTERNAL_E2E_ALIASES}
    continuity_state: dict[str, Any] = {}
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
        continuity, continuity_blockers = _check_b_continuity(
            turn, _product_ids(artifact), continuity_state, seed_referenced
        )
        blockers.extend(continuity_blockers)
        blockers = sorted(set(blockers))
        machine_verified = {}
        if continuity.get("status") == "verified":
            machine_verified = {assertion: "b_continuity_verified" for assertion in _continuity_assertions(turn)}
        halting = (
            int(artifact.get("external_egress_count") or 0) != 0
            or any(
                blocker in _HALTING_BLOCKERS or blocker.endswith("_unproven") or blocker.endswith("_proof_mismatch")
                for blocker in blockers
            )
            or "alias_conversation_mismatch" in blockers
            or "real_customer_fallback_forbidden" in blockers
        )
        passed = not blockers
        machine_status = (
            "failed" if not passed
            else "passed_unverified_continuity" if continuity.get("status") == "unverifiable"
            else "passed"
        )
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
                "machine_passed": passed,
                "machine_status": machine_status,
                "halting": halting,
                "blockers": blockers,
                "prior_turn_ids": prior,
                "continuity": continuity,
                "evidence": _evidence(artifact),
                "review": _review_block(turn, machine_verified=machine_verified),
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
    passed_rows = [row for row in results if row["machine_passed"]]
    aliases = {
        alias: {
            "conversation_id": fixtures[alias]["conversation_id"],
            "turn_ids": [turn.turn_id for turn in matrix.turns_for(alias)],
            "completed_turn_ids": completed_by_alias[alias],
            "all_machine_passed": all(
                row["machine_passed"] for row in results if row["alias"] == alias
            ),
        }
        for alias in INTERNAL_E2E_ALIASES
    }
    report = {
        "contract_version": matrix.contract_version,
        "matrix_sha256": matrix.matrix_sha256,
        "run_id": run_id,
        "tenant_id": ACCEPTANCE_TENANT_ID,
        "execution_mode": "INTERNAL_E2E",
        "channel": INTERNAL_E2E_CHANNEL,
        "support_grant": support_grant,
        "turn_order": list(matrix.turn_ids),
        "fixtures": fixtures,
        "aliases": aliases,
        "turns_total": len(matrix.turns),
        "turns_executed": len(executed),
        "turns_machine_passed": len(passed_rows),
        "turns_machine_failed": len(executed) - len(passed_rows),
        "turns_not_executed": len(results) - len(executed),
        "machine_passed": len(passed_rows) == len(matrix.turns),
        "machine_summary": f"{len(passed_rows)}/{len(matrix.turns)} machine checks",
        "continuity": {
            "b1_products_shown": list(continuity_state.get("b1_shown") or []),
            "b2_selected_product": (continuity_state.get("b2_selected") or [None])[0],
            "unverifiable_turns": [
                row["turn_id"] for row in executed if row["continuity"].get("status") == "unverifiable"
            ],
            "violated_turns": [
                row["turn_id"] for row in executed if row["continuity"].get("status") == "violated"
            ],
        },
        "halted_at": halted_at,
        "halt_reason": halt_reason,
        "external_egress_total": sum(
            int((row.get("evidence") or {}).get("external_egress_count") or 0)
            for row in executed
        ),
        "results": results,
    }
    return finalize_acceptance(report)


# ── Human review ledger and final classification ───────────────────────

def finalize_acceptance(report: dict[str, Any]) -> dict[str, Any]:
    """Recompute review status, acceptance and classification from the ledger.

    ``acceptance_passed`` is true only when every turn passed the machine gate
    AND every required assertion of every turn carries an ``approved`` verdict.
    A pending assertion keeps the run at MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING;
    machine checks alone can never produce a final PASS.
    """
    results = list(report.get("results") or [])
    machine_passed = bool(report.get("machine_passed"))
    total = approved = rejected = pending = 0
    for row in results:
        review = row.setdefault("review", {"status": REVIEW_PENDING, "assertions": []})
        verdicts = [item.get("verdict") for item in review.get("assertions") or []]
        total += len(verdicts)
        approved += sum(v == REVIEW_APPROVED for v in verdicts)
        rejected += sum(v == REVIEW_REJECTED for v in verdicts)
        pending += sum(v == REVIEW_PENDING for v in verdicts)
        review["status"] = (
            REVIEW_REJECTED if REVIEW_REJECTED in verdicts
            else REVIEW_APPROVED if verdicts and all(v == REVIEW_APPROVED for v in verdicts)
            else REVIEW_PENDING
        )
        row["acceptance_passed"] = bool(row.get("machine_passed")) and review["status"] == REVIEW_APPROVED
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
                "human_mandatory_turns": [
                    row["turn_id"] for row in results
                    if any(item.get("human_mandatory") for item in row["review"]["assertions"])
                ],
            },
            "acceptance_passed": acceptance_passed,
            "classification": classification,
        }
    )
    return report


def apply_assertion_review(
    report: dict[str, Any],
    *,
    turn_id: str,
    assertion_index: int,
    verdict: str,
    reviewer: str,
    evidence: str,
    reviewed_at: datetime | None = None,
) -> dict[str, Any]:
    """Record one reviewer verdict on one required assertion and re-finalize."""
    verdict = str(verdict or "").strip().lower()
    if verdict not in {REVIEW_APPROVED, REVIEW_REJECTED}:
        raise _fail("review_verdict_invalid")
    reviewer = str(reviewer or "").strip()
    evidence = str(evidence or "").strip()
    if len(reviewer) < 3 or len(reviewer) > 120:
        raise _fail("review_reviewer_invalid")
    if len(evidence) < 5 or len(evidence) > 2000:
        raise _fail("review_evidence_invalid")
    row = next((item for item in report.get("results") or [] if item.get("turn_id") == turn_id), None)
    if row is None:
        raise _fail("review_turn_unknown")
    if not row.get("executed") or row.get("status") == "not_executed":
        raise _fail("review_turn_not_executed")
    assertions = row.get("review", {}).get("assertions") or []
    if not 0 <= int(assertion_index) < len(assertions):
        raise _fail("review_assertion_unknown")
    stamp = (reviewed_at or datetime.now(timezone.utc)).isoformat()
    assertions[int(assertion_index)].update(
        {"verdict": verdict, "reviewer": reviewer, "reviewed_at": stamp, "evidence": evidence}
    )
    return finalize_acceptance(report)


def record_acceptance_review(
    db: Any,
    run_id: str,
    *,
    turn_id: str,
    assertion_index: int,
    verdict: str,
    reviewer: str,
    evidence: str,
) -> dict[str, Any]:
    """Persist a reviewer verdict on a finished run's control row.

    Recording a verdict runs no agent, tool or turn, so it deliberately does
    NOT require INTERNAL_E2E to be enabled or a live support grant: the safe
    sequence is run → disable INTERNAL_E2E → review evidence → record verdicts
    → clean up.  The run record must be a finished, synthetic, Tenant 1 Phase
    2.7A run whose contract version and matrix hash match the checked-in
    matrix and whose machine evidence digest is intact; only human-review
    fields change, then the final classification is recomputed.
    """
    control = _acceptance_control(db, run_id)
    meta = dict(control.extra_metadata or {})
    if int(control.tenant_id or 0) != ACCEPTANCE_TENANT_ID or (
        meta.get("channel") != INTERNAL_E2E_CHANNEL
        or meta.get("synthetic") is not True
        or meta.get("test_only") is not True
    ):
        raise _fail("review_run_not_synthetic")
    if meta.get("status") not in {"completed", "halted"}:
        raise _fail("review_run_not_finished")
    report = meta.get("report")
    if not isinstance(report, dict):
        raise _fail("review_run_not_finished")
    if (
        meta.get("contract_version") != ACCEPTANCE_CONTRACT_VERSION
        or report.get("contract_version") != ACCEPTANCE_CONTRACT_VERSION
    ):
        raise _fail("review_contract_mismatch")
    expected_hash = load_acceptance_matrix().matrix_sha256
    if meta.get("matrix_sha256") != expected_hash or report.get("matrix_sha256") != expected_hash:
        raise _fail("review_matrix_hash_mismatch")
    if not meta.get("support_grant"):
        raise _fail("review_run_grant_record_missing")
    digest = meta.get("machine_digest")
    if not digest or machine_digest(report) != digest:
        raise _fail("review_machine_evidence_tampered")
    report = apply_assertion_review(
        json.loads(json.dumps(report)), turn_id=turn_id, assertion_index=assertion_index,
        verdict=verdict, reviewer=reviewer, evidence=evidence,
    )
    if machine_digest(report) != digest:
        raise _fail("review_machine_evidence_tampered")
    meta["report"] = report
    meta.update({key: report[key] for key in ("review_status", "review", "acceptance_passed", "classification")})
    control.extra_metadata = meta
    flag_modified(control, "extra_metadata")
    db.commit()
    return dict(control.extra_metadata or {})


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


_REVIEW_ONLY_RUN_KEYS = frozenset({"review_status", "review", "acceptance_passed", "classification"})
_REVIEW_ONLY_TURN_KEYS = frozenset({"review", "acceptance_passed"})


def machine_digest(report: Mapping[str, Any]) -> str:
    """SHA-256 over everything in a report except the human-review fields.

    Recording a verdict may only change review fields; the digest is stored
    when a run finishes and re-checked on every review so machine evidence
    and machine verdicts can never be edited through the review path.
    """
    frozen = {key: value for key, value in report.items() if key not in _REVIEW_ONLY_RUN_KEYS}
    frozen["results"] = [
        {key: value for key, value in row.items() if key not in _REVIEW_ONLY_TURN_KEYS}
        for row in report.get("results") or []
    ]
    return _canonical_sha256(frozen)


def persist_completed_report(db: Any, run_id: str, report: Mapping[str, Any]) -> dict[str, Any]:
    """Store a finished run's compact report plus its machine digest on the control row."""
    control = _acceptance_control(db, run_id)
    meta = dict(control.extra_metadata or {})
    compact = _compact_results(report)
    meta.update(
        {
            "status": "completed" if report.get("halted_at") is None else "halted",
            **{
                key: report[key]
                for key in (
                    "turns_executed", "turns_machine_passed", "turns_machine_failed",
                    "turns_not_executed", "machine_passed", "machine_summary",
                    "review_status", "review", "acceptance_passed", "classification",
                    "continuity", "halted_at", "halt_reason", "external_egress_total",
                    "fixtures", "aliases",
                )
            },
            "report": compact,
            "machine_digest": machine_digest(compact),
        }
    )
    control.extra_metadata = meta
    flag_modified(control, "extra_metadata")
    db.commit()
    return dict(control.extra_metadata or {})


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
    support_grant = require_phase_2_7a_support_grant(db, tenant_id=ACCEPTANCE_TENANT_ID)
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
            "support_grant": support_grant,
            "status": "queued",
            "turns_total": len(matrix.turns),
            "turns_executed": 0,
            "turns_machine_passed": 0,
            "machine_passed": False,
            "review_status": REVIEW_PENDING,
            "acceptance_passed": False,
            "classification": None,
            "external_egress_total": 0,
        },
    )
    db.add(control)
    db.commit()
    return dict(control.extra_metadata or {})


def acceptance_run_status(db: Any, run_id: str) -> dict[str, Any]:
    """Read a run's record.  Read-only and admin-only; it does not execute anything,
    so it stays available after INTERNAL_E2E has been disabled for the review phase."""
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
        persist_completed_report(db, run_id, report)
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
    "CLASS_ACCEPTANCE_PASSED",
    "CLASS_MACHINE_GATE_FAILED",
    "CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING",
    "CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED",
    "AcceptanceMatrix",
    "AcceptanceTurn",
    "acceptance_run_status",
    "apply_assertion_review",
    "create_acceptance_run",
    "execute_acceptance_run",
    "finalize_acceptance",
    "load_acceptance_matrix",
    "machine_digest",
    "persist_completed_report",
    "record_acceptance_review",
    "require_phase_2_7a_support_grant",
    "run_acceptance_matrix",
    "verify_acceptance_fixtures",
]
