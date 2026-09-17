"""Phase 2.7A canonical A1–C4 matrix: schema, determinism, isolation and gates.

Collected by the default root suite.  Every value is synthetic; the fake turn
submitter never reaches the Commerce Agent runtime or any provider.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from unittest.mock import patch
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "backend"), os.path.join(REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.auth import require_admin  # noqa: E402
from core.database import get_db  # noqa: E402
from evals.commerce_agent_v2_whatsapp.scorer import SAFETY_KEYS  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from models import (  # noqa: E402
    Base,
    Conversation,
    MessageEvent,
    Order,
    OrderShipment,
    Product,
    Tenant,
    TenantSettings,
)
from services import commerce_v2_phase_2_7a_acceptance as acceptance  # noqa: E402
from services.commerce_v2_internal_e2e import (  # noqa: E402
    InternalE2EContractError,
    InternalE2EFixture,
    _find_fixture,
    provision_internal_e2e_fixtures,
)
from services.commerce_v2_phase_2_7a_acceptance import (  # noqa: E402
    ACCEPTANCE_CONTRACT_VERSION,
    machine_digest,
    persist_completed_report,
    record_acceptance_review,
    ACCEPTANCE_MATRIX_PATH,
    ACCEPTANCE_TURN_IDS,
    CLASS_ACCEPTANCE_PASSED,
    CLASS_MACHINE_GATE_FAILED,
    CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING,
    CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED,
    apply_assertion_review,
    load_acceptance_matrix,
    require_phase_2_7a_support_grant,
    run_acceptance_matrix,
    verify_acceptance_fixtures,
)

# Owner-approved canonical matrix (verbatim).  The artifact must match this.
CANONICAL = {
    "A1": ("A", "السلام عليكم", [], "social_reply"),
    "A2": ("A", "وش المنتجات المتوفرة عندكم؟", ["search_products"], "grounded_reply"),
    "A3": ("A", "أبغى تفاصيل أول منتج عندكم", ["search_products", "get_product_details"], "grounded_reply"),
    "A4": ("A", "كم سعر أول منتج وهل هو متوفر؟", ["search_products"], "grounded_reply"),
    "B1": ("B", "السلام عليكم، أبغى أتصفح المنتجات", ["search_products"], "grounded_reply"),
    "B2": ("B", "اختر لي واحداً منها", ["search_products"], "grounded_reply"),
    "B3": ("B", "كم سعره وهل هو متوفر؟", ["search_products"], "grounded_reply"),
    "B4": ("B", "طيب هل عندكم معلومات إضافية عنه؟", ["search_products", "search_product_knowledge"], "grounded_reply"),
    "C1": ("C", "وش حالة آخر طلب لي؟", ["resolve_customer_order"], "grounded_reply"),
    "C2": ("C", "وش محتويات آخر طلب؟", ["resolve_customer_order", "get_order_details"], "grounded_reply"),
    "C3": ("C", "وش حالة شحنة طلبي؟", ["resolve_customer_order", "get_order_shipment"], "grounded_reply"),
    "C4": ("C", "عطني رقم التتبع", ["resolve_customer_order", "get_order_shipment"], "grounded_reply"),
}
ENABLED_ENV = {
    "NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED": "true",
    "NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS": "1",
}
DISABLED_ENV = {
    "NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED": "false",
    "NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS": "1",
}


def _grant(db: Any, *, tenant_id: int = 1, hours: float = 2, reason: str = "Phase 2.7A acceptance run", enabled: bool = True) -> None:
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).one()
    now = datetime.now(timezone.utc)
    meta = dict(settings.extra_metadata or {})
    meta["support_access"] = {
        "enabled": enabled,
        "granted_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=hours)).isoformat(),
        "granted_by": "merchant@example.test",
        "reason": reason,
        "session_version": 1,
    }
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.commit()


def _revoke(db: Any, tenant_id: int = 1) -> None:
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).one()
    meta = dict(settings.extra_metadata or {})
    meta.pop("support_access", None)
    settings.extra_metadata = meta
    flag_modified(settings, "extra_metadata")
    db.commit()


@pytest.fixture()
def db() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    saved: list[tuple[Any, Any]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id=1, name="متجر تجريبي عام", is_active=True))
    session.add(TenantSettings(tenant_id=1, ai_settings={"locale": "ar-SA"}))
    session.add_all(
        [
            Product(
                tenant_id=1, external_id="GENERIC-SHOE", title="حذاء رياضي أبيض",
                price="180", in_stock=True, stock_quantity=4, catalog_status="active",
                extra_metadata={"status": "active", "currency": "SAR"},
            ),
            Product(
                tenant_id=1, external_id="GENERIC-SHIRT", title="قميص قطني أزرق",
                price="95", in_stock=True, stock_quantity=7, catalog_status="active",
                extra_metadata={"status": "active", "currency": "SAR"},
            ),
        ]
    )
    session.commit()
    _grant(session)
    yield session
    session.close()
    engine.dispose()


# Product ids the fake replies reference: B1 shows [pid(shoe), pid(shirt)] and B2..B4 select the shoe.
def _catalog_ids(db: Any) -> list[int]:
    return [int(p.id) for p in db.query(Product).filter(Product.tenant_id == 1).order_by(Product.id.asc()).all()]


def _default_refs(db: Any, request: Any) -> list[dict[str, Any]]:
    turn_id = str(request.expected.get("turn_id") or "")
    if turn_id == "B1":
        return [{"product_id": pid, "evidence_ref": f"ev:{pid}"} for pid in _catalog_ids(db)]
    if turn_id in {"B2", "B3", "B4"}:
        return [{"product_id": _catalog_ids(db)[0], "evidence_ref": "ev:selected"}]
    return []


def _artifact(db: Any, request: Any, **overrides: Any) -> dict[str, Any]:
    fixture = _find_fixture(db, 1, request.synthetic_customer_alias)
    expected = dict(request.expected or {})
    artifact: dict[str, Any] = {
        "artifact_version": "commerce_v2_internal_e2e_turn_v1",
        "execution_mode": "INTERNAL_E2E",
        "channel": "internal_e2e",
        "tenant_id": 1,
        "case_id": request.case_id,
        "batch_id": request.batch_id,
        "account_alias": request.synthetic_customer_alias,
        "conversation_id": fixture.conversation_id,
        "customer_id": None,
        "internal_inbound_message_id": f"internal_e2e:t1:{request.synthetic_customer_alias.lower()}:in:{request.case_id}",
        "internal_outbound_message_id": f"internal_e2e:t1:{request.synthetic_customer_alias.lower()}:out:{request.case_id}",
        "trace_id": f"trace_{request.case_id}",
        "session_id": f"commerce-v2:1:{fixture.conversation_id}",
        "owner": "commerce_agent_v2",
        "v1_bypassed": True,
        "status": "completed",
        "failure_reason": None,
        "model": "synthetic-model",
        "model_attempts": 1,
        "tool_calls": list(expected.get("expected_tools") or []),
        "guardrail_passed": True,
        "customer_visible_text": f"رد تجريبي على: {request.text}",
        "structured_reply": {"text": "x", "response_mode": "social", "product_refs": _default_refs(db, request)},
        "total_runner_latency_ms": 10,
        "requested_service_tier": request.service_tier,
        "input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 2,
        "expected": expected,
        "fallback_type": "none",
        "external_egress_count": 0,
        "external_egress_denials": [],
        "safety_proofs": {key: {"proven": True, "value": 0} for key in SAFETY_KEYS},
    }
    for key in SAFETY_KEYS:
        artifact[key] = 0
    artifact.update(overrides)
    return artifact


def _fake_submit(calls: list[Any], *, per_turn: dict[str, dict[str, Any]] | None = None):
    async def submit(db: Any, request: Any, *, env: Any = None) -> dict[str, Any]:
        calls.append(request)
        turn_id = str(request.expected.get("turn_id"))
        overrides = (per_turn or {}).get(turn_id) or {}
        return _artifact(db, request, **overrides)

    return submit


def _run(db: Any, submit: Any, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_acceptance_matrix(db, env=ENABLED_ENV, submit_turn=submit, **kwargs))


def _write_variant(tmp_path: Path, mutate) -> Path:
    raw = json.loads(ACCEPTANCE_MATRIX_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


# ── 1–6. Schema, identity, verbatim inputs, determinism ─────────────────

def test_exactly_twelve_turns_are_loaded() -> None:
    matrix = load_acceptance_matrix()
    assert matrix.contract_version == ACCEPTANCE_CONTRACT_VERSION
    assert len(matrix.turns) == 12
    assert json.loads(ACCEPTANCE_MATRIX_PATH.read_text(encoding="utf-8"))["turns_total"] == 12


def test_turn_ids_are_exactly_a1_to_c4_in_canonical_order() -> None:
    matrix = load_acceptance_matrix()
    assert matrix.turn_ids == ("A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4", "C1", "C2", "C3", "C4")
    assert matrix.turn_ids == ACCEPTANCE_TURN_IDS
    for alias in "ABC":
        assert [turn.sequence for turn in matrix.turns_for(alias)] == [1, 2, 3, 4]


def test_no_d_scenario_exists(tmp_path: Path) -> None:
    raw = json.loads(ACCEPTANCE_MATRIX_PATH.read_text(encoding="utf-8"))
    assert raw["aliases"] == ["A", "B", "C"]
    assert raw["d_scenario"] is None
    assert not any(turn["turn_id"].startswith("D") or turn["alias"] == "D" for turn in raw["turns"])

    def add_d(doc: dict) -> None:
        doc["d_scenario"] = {"turns": ["D1"]}

    with pytest.raises(InternalE2EContractError, match="phase_2_7a_d_scenario_forbidden"):
        load_acceptance_matrix(_write_variant(tmp_path, add_d))

    def thirteenth(doc: dict) -> None:
        doc["turns"].append({**doc["turns"][0], "turn_id": "D1", "alias": "D"})
        doc["turns_total"] = 13

    with pytest.raises(InternalE2EContractError, match="phase_2_7a_turn_count_invalid"):
        load_acceptance_matrix(_write_variant(tmp_path, thirteenth))


def test_inputs_match_owner_approved_text_verbatim() -> None:
    matrix = load_acceptance_matrix()
    for turn in matrix.turns:
        alias, text, _tools, _outcome = CANONICAL[turn.turn_id]
        assert turn.alias == alias
        assert turn.input == text, turn.turn_id


def test_expected_tools_and_outcomes_match_exactly() -> None:
    matrix = load_acceptance_matrix()
    for turn in matrix.turns:
        _alias, _text, tools, outcome = CANONICAL[turn.turn_id]
        assert list(turn.expected_tools) == tools, turn.turn_id
        assert turn.expected_outcome == outcome, turn.turn_id
    assert not any(turn.requires_prior_context for turn in matrix.turns if turn.sequence == 1)
    assert all(turn.requires_prior_context for turn in matrix.turns if turn.turn_id in {"A3", "A4", "B2", "B3", "B4", "C2", "C3", "C4"})


def test_every_turn_points_at_its_verbatim_corpus_source_and_tampering_fails_closed(tmp_path: Path) -> None:
    corpus = json.loads((ACCEPTANCE_MATRIX_PATH.parent / "corpus_v1.json").read_text(encoding="utf-8"))
    for turn in load_acceptance_matrix().turns:
        src = turn.source
        account = next(acc for acc in corpus["accounts"] if acc["alias"] == src["alias"])
        step = account["segments"][src["segment_index"]]["steps"][src["step_index"]]
        assert step["variants"][src["variant_index"]] == turn.input
        assert step["expected_tools"] == list(turn.expected_tools)
        assert step["expected_outcome"] == turn.expected_outcome

    def tamper_text(doc: dict) -> None:
        doc["turns"][1]["input"] = "وش المنتجات المتوفرة عندكم"  # dropped the question mark

    with pytest.raises(InternalE2EContractError, match="phase_2_7a_source_mismatch:A2"):
        load_acceptance_matrix(_write_variant(tmp_path, tamper_text))

    def tamper_tools(doc: dict) -> None:
        doc["turns"][8]["expected_tools"] = ["get_order_details"]

    with pytest.raises(InternalE2EContractError, match="phase_2_7a_source_mismatch:C1"):
        load_acceptance_matrix(_write_variant(tmp_path, tamper_tools))

    def reorder(doc: dict) -> None:
        doc["turns"][0], doc["turns"][1] = doc["turns"][1], doc["turns"][0]

    with pytest.raises(InternalE2EContractError, match="phase_2_7a_turn_order_invalid"):
        load_acceptance_matrix(_write_variant(tmp_path, reorder))

    def placeholder(doc: dict) -> None:
        doc["turns"][9]["input"] = "ما حالة الطلب رقم {TEST_ORDER_NUMBER}؟"

    with pytest.raises(InternalE2EContractError, match="phase_2_7a_input_invalid:C2"):
        load_acceptance_matrix(_write_variant(tmp_path, placeholder))

    with pytest.raises(InternalE2EContractError, match="phase_2_7a_matrix_missing"):
        load_acceptance_matrix(tmp_path / "absent.json")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_matrix_malformed"):
        load_acceptance_matrix(tmp_path / "broken.json")


def test_ordering_is_deterministic_across_loads_and_execution(db: Any) -> None:
    first, second = load_acceptance_matrix(), load_acceptance_matrix()
    assert first == second
    assert first.matrix_sha256 == second.matrix_sha256
    assert [t["turn_id"] for t in json.loads(ACCEPTANCE_MATRIX_PATH.read_text(encoding="utf-8"))["turns"]] == list(ACCEPTANCE_TURN_IDS)

    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    calls_a: list[Any] = []
    report_a = _run(db, _fake_submit(calls_a))
    calls_b: list[Any] = []
    report_b = _run(db, _fake_submit(calls_b))
    assert [c.expected["turn_id"] for c in calls_a] == list(ACCEPTANCE_TURN_IDS)
    assert [c.text for c in calls_a] == [c.text for c in calls_b] == [CANONICAL[t][1] for t in ACCEPTANCE_TURN_IDS]
    assert report_a["turn_order"] == report_b["turn_order"] == list(ACCEPTANCE_TURN_IDS)
    assert all(c.service_tier == "auto" for c in calls_a)


# ── 7–8. Isolation and continuity ──────────────────────────────────────

def test_a_b_and_c_remain_isolated(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    calls: list[Any] = []
    report = _run(db, _fake_submit(calls))
    conversations = {alias: report["fixtures"][alias]["conversation_id"] for alias in "ABC"}
    assert len(set(conversations.values())) == 3
    for row in report["results"]:
        assert row["evidence"]["conversation_id"] == conversations[row["alias"]]
        assert row["evidence"]["customer_id"] is None
        assert row["evidence"]["cross_tenant_leakage"] == 0
        assert row["evidence"]["cross_customer_leakage"] == 0
    assert [c.synthetic_customer_alias for c in calls] == ["A"] * 4 + ["B"] * 4 + ["C"] * 4
    identities = {alias: report["fixtures"][alias]["identity"] for alias in "ABC"}
    assert identities == {alias: f"internal_e2e:t1:customer:{alias.lower()}" for alias in "ABC"}

    # A reply produced in another alias's conversation is a halting isolation failure.
    calls.clear()
    other = report["fixtures"]["B"]["conversation_id"]
    report = _run(db, _fake_submit(calls, per_turn={"A2": {"conversation_id": other}}))
    a2 = report["results"][1]
    assert "alias_conversation_mismatch" in a2["blockers"]
    assert a2["halting"] is True and report["halted_at"] == "A2"
    assert report["machine_passed"] is False and report["classification"] == CLASS_MACHINE_GATE_FAILED


def test_context_is_retained_within_each_alias(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    calls: list[Any] = []
    report = _run(db, _fake_submit(calls))
    by_id = {row["turn_id"]: row for row in report["results"]}
    assert by_id["A3"]["prior_turn_ids"] == ["A1", "A2"]
    assert by_id["B4"]["prior_turn_ids"] == ["B1", "B2", "B3"]
    assert by_id["C4"]["prior_turn_ids"] == ["C1", "C2", "C3"]
    assert by_id["B1"]["prior_turn_ids"] == []  # no cross-alias carry-over
    for alias in "ABC":
        convs = {row["evidence"]["conversation_id"] for row in report["results"] if row["alias"] == alias}
        assert len(convs) == 1
        assert report["aliases"][alias]["completed_turn_ids"] == [f"{alias}{n}" for n in range(1, 5)]
        assert report["aliases"][alias]["all_machine_passed"] is True
    assert all(row["machine_passed"] for row in report["results"] if row["requires_prior_context"])


# ── 9–11. Fail-closed gates ────────────────────────────────────────────

def test_missing_c_fixtures_fail_closed_before_any_turn(db: Any) -> None:
    fixtures = provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    shipment = db.query(OrderShipment).filter(OrderShipment.order_id == fixtures["C"].order_id).one()
    db.delete(shipment)
    db.commit()
    calls: list[Any] = []
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_c_shipment_fixture_missing"):
        _run(db, _fake_submit(calls))
    assert calls == []

    db.delete(db.get(Order, fixtures["C"].order_id))
    db.commit()
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_c_order_fixture_missing"):
        _run(db, _fake_submit(calls))
    assert calls == []

    conversation = db.get(Conversation, fixtures["C"].conversation_id)
    db.delete(conversation)
    db.commit()
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_fixture_missing:C"):
        _run(db, _fake_submit(calls))
    assert calls == []
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_fixture_missing:C"):
        acceptance.create_acceptance_run(db, env=ENABLED_ENV)


def test_disabled_internal_e2e_prevents_execution(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    calls: list[Any] = []
    with pytest.raises(InternalE2EContractError, match="internal_e2e_disabled"):
        asyncio.run(run_acceptance_matrix(db, env=DISABLED_ENV, submit_turn=_fake_submit(calls)))
    assert calls == []
    with pytest.raises(InternalE2EContractError, match="internal_e2e_disabled"):
        acceptance.create_acceptance_run(db, env=DISABLED_ENV)
    with pytest.raises(InternalE2EContractError, match="internal_e2e_disabled"):
        asyncio.run(run_acceptance_matrix(db, env={}, submit_turn=_fake_submit(calls)))
    assert calls == []


def test_runner_cannot_fall_back_to_a_real_customer_or_order(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fixtures = provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    calls: list[Any] = []

    real = InternalE2EFixture(
        alias="C", customer_id=4242, conversation_id=fixtures["C"].conversation_id,
        identity=fixtures["C"].identity, order_id=fixtures["C"].order_id, order_number="IE2E-C-001",
    )
    monkeypatch.setattr(acceptance, "_find_fixture", lambda _db, _t, alias: real if alias == "C" else fixtures[alias])
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_real_customer_fallback_forbidden"):
        _run(db, _fake_submit(calls))
    assert calls == []
    monkeypatch.undo()

    # An order that is not the synthetic one (real source / bound customer) is refused.
    order = db.get(Order, fixtures["C"].order_id)
    order.source = "salla"
    db.commit()
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_c_order_fixture_missing"):
        verify_acceptance_fixtures(db)
    order.source = "internal_e2e"
    db.commit()

    # A turn artifact bound to a real customer id halts the sequence.
    report = _run(db, _fake_submit(calls, per_turn={"C1": {"customer_id": 77}}))
    c1 = report["results"][8]
    assert "real_customer_fallback_forbidden" in c1["blockers"] and c1["halting"] is True
    assert report["halted_at"] == "C1"
    assert [row["status"] for row in report["results"][9:]] == ["not_executed"] * 3


# ── 12. Machine-readable output ────────────────────────────────────────

def test_output_contains_evidence_and_result_fields_for_every_turn(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    report = _run(db, _fake_submit([]))
    assert report["contract_version"] == ACCEPTANCE_CONTRACT_VERSION
    assert report["turns_total"] == 12 and report["turns_executed"] == 12
    assert report["turns_machine_passed"] == 12 and report["machine_summary"] == "12/12 machine checks"
    assert report["machine_passed"] is True
    assert report["acceptance_passed"] is False and report["review_status"] == "pending"
    assert report["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING
    assert report["halted_at"] is None and report["external_egress_total"] == 0
    assert report["execution_mode"] == "INTERNAL_E2E" and report["tenant_id"] == 1
    assert report["fixtures"]["C"]["order_number"] == "IE2E-C-001"
    assert report["fixtures"]["C"]["tracking_present"] is True
    required = {
        "turn_id", "alias", "sequence", "input", "requires_prior_context", "expected",
        "required_assertions", "executed", "status", "machine_passed", "machine_status",
        "halting", "blockers", "prior_turn_ids", "continuity", "evidence", "review",
        "acceptance_passed",
    }
    evidence_keys = {
        "conversation_id", "customer_id", "trace_id", "internal_inbound_message_id",
        "internal_outbound_message_id", "owner", "model", "tool_calls", "fallback_type",
        "guardrail_passed", "customer_visible_text", "latency_ms", "tokens",
        "external_egress_count", "cross_tenant_leakage", "cross_customer_leakage",
        "write_mutations", "salla_mutations", "duplicate_replies",
        "unsupported_commercial_claims", "silent_v1_fallback", "safety_proofs_proven",
    }
    assert [row["turn_id"] for row in report["results"]] == list(ACCEPTANCE_TURN_IDS)
    for row in report["results"]:
        assert required <= set(row)
        assert evidence_keys <= set(row["evidence"])
        assert row["expected"] == {"tools": CANONICAL[row["turn_id"]][2], "outcome": CANONICAL[row["turn_id"]][3]}
        assert row["required_assertions"]
        assert row["evidence"]["trace_id"] and row["evidence"]["safety_proofs_proven"] is True
        assert row["acceptance_passed"] is False
        assert [a["assertion"] for a in row["review"]["assertions"]] == row["required_assertions"]
        assert all(a["verdict"] == "pending" and a["human_mandatory"] for a in row["review"]["assertions"])
    json.dumps(report, ensure_ascii=False)  # serialisable


def test_halting_failure_stops_the_sequence_and_never_reports_twelve(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    calls: list[Any] = []
    report = _run(
        db,
        _fake_submit(calls, per_turn={"B2": {"status": "test_contract_failed", "failure_reason": "internal_e2e_external_egress_attempted", "external_egress_count": 1}}),
    )
    assert [c.expected["turn_id"] for c in calls] == ["A1", "A2", "A3", "A4", "B1", "B2"]
    assert report["halted_at"] == "B2" and report["halt_reason"] == "internal_e2e_external_egress_attempted"
    assert report["turns_executed"] == 6 and report["turns_machine_passed"] == 5
    assert report["machine_summary"] == "5/12 machine checks"
    assert report["turns_not_executed"] == 6 and report["machine_passed"] is False
    assert report["classification"] == CLASS_MACHINE_GATE_FAILED and report["acceptance_passed"] is False
    assert report["external_egress_total"] == 1
    assert all(row["blockers"] == ["halted_before_execution"] for row in report["results"][6:])

    # A non-halting expectation failure continues by default and halts with the flag.
    calls.clear()
    report = _run(db, _fake_submit(calls, per_turn={"A2": {"tool_calls": []}}))
    assert report["results"][1]["blockers"] == ["expected_tool_missing"]
    assert report["turns_executed"] == 12 and report["machine_summary"] == "11/12 machine checks"
    assert report["machine_passed"] is False
    calls.clear()
    report = _run(db, _fake_submit(calls, per_turn={"A2": {"tool_calls": []}}), halt_on_first_failure=True)
    assert report["halted_at"] == "A2" and report["turns_executed"] == 2


def test_guardrail_blocked_reply_fails_its_turn_without_halting_the_sequence(db: Any) -> None:
    """A rejected model reply is a quality failure, not a safety halt.

    Production Phase 2.7A run 2 (2026-09-17, run_id 1c567f37) stopped at B1 because
    the grounding guardrail rejected a reply carrying a fabricated product image
    URL. The guardrail worked: the customer received only the safe fallback. But
    the halt discarded turns B2–B4 and C1–C4, so the run produced no evidence for
    seven of twelve turns. The turn must still fail; the sequence must continue.
    """
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    blocked = {
        "status": "failed",
        "failure_reason": "output_guardrail_tripwire:claim_not_in_evidence:image_url",
        "guardrail_passed": False,
        "guardrail_blocked_reply": 1,
        "fallback_type": "unexpected_runtime_fallback",
        "customer_visible_text": "لا تتوفر لدي معلومة موثوقة كافية للإجابة الآن.",
        "structured_reply": {
            "text": "لا تتوفر لدي معلومة موثوقة كافية للإجابة الآن.",
            "response_mode": "grounded",
            "safe_fallback_reason": "output_guardrail_tripwire:claim_not_in_evidence:image_url",
            "product_refs": [],
        },
    }
    calls: list[Any] = []
    report = _run(db, _fake_submit(calls, per_turn={"B1": dict(blocked)}))

    assert [c.expected["turn_id"] for c in calls] == list(ACCEPTANCE_TURN_IDS)
    assert report["halted_at"] is None and report["turns_not_executed"] == 0
    assert report["turns_executed"] == 12

    b1 = next(row for row in report["results"] if row["turn_id"] == "B1")
    assert b1["halting"] is False and b1["machine_passed"] is False
    assert {"guardrail_not_passed", "turn_not_completed", "unexpected_fallback"} <= set(b1["blockers"])
    # The failure keeps its true cause and is never rewritten into a pass.
    assert b1["evidence"]["failure_reason"] == "output_guardrail_tripwire:claim_not_in_evidence:image_url"
    assert b1["evidence"]["guardrail_blocked_reply"] == 1
    assert b1["evidence"]["guardrail_passed"] is False
    assert b1["evidence"]["unsupported_commercial_claims"] == 0
    assert report["machine_passed"] is False
    assert report["classification"] == CLASS_MACHINE_GATE_FAILED
    assert report["acceptance_passed"] is False
    # Every later turn still ran, so the owner gets evidence for all twelve.
    assert all(row["executed"] for row in report["results"])

    # A turn that never completed for any other reason is likewise non-halting.
    calls.clear()
    report = _run(db, _fake_submit(calls, per_turn={"C2": {"status": "failed", "failure_reason": "model_timeout:attempt_2"}}))
    assert report["halted_at"] is None and report["turns_executed"] == 12


def test_an_ungrounded_claim_that_reached_the_customer_still_halts(db: Any) -> None:
    """The safety meaning of ``unsupported_commercial_claims`` is unchanged."""
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    delivered = {
        "status": "test_contract_failed",
        "failure_reason": "internal_e2e_safety_failure:unsupported_commercial_claims",
        "unsupported_commercial_claims": 1,
        "safety_proofs": {
            **{key: {"proven": True, "value": 0} for key in SAFETY_KEYS},
            "unsupported_commercial_claims": {"proven": True, "value": 1},
        },
    }
    calls: list[Any] = []
    report = _run(db, _fake_submit(calls, per_turn={"B1": delivered}))

    assert report["halted_at"] == "B1"
    assert report["halt_reason"] == "internal_e2e_safety_failure:unsupported_commercial_claims"
    assert report["turns_executed"] == 5 and report["turns_not_executed"] == 7
    b1 = next(row for row in report["results"] if row["turn_id"] == "B1")
    assert b1["halting"] is True and "unsupported_commercial_claims" in b1["blockers"]
    assert report["classification"] == CLASS_MACHINE_GATE_FAILED


# ── Admin API surface ──────────────────────────────────────────────────

@pytest.fixture()
def api(db: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FastAPI]:
    from routers import internal_commerce_e2e as router_module

    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[get_db] = lambda: db
    executed: list[str] = []

    async def _no_execute(run_id: str) -> None:
        executed.append(run_id)

    monkeypatch.setattr(router_module, "execute_acceptance_run", _no_execute)
    app.state.executed = executed
    return TestClient(app), app


def test_api_requires_admin_and_gates_on_internal_e2e(api: tuple[TestClient, FastAPI], db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, app = api
    assert client.get("/admin/internal-e2e/acceptance/phase-2-7a/matrix").status_code in {401, 403}
    assert client.post("/admin/internal-e2e/acceptance/phase-2-7a/runs", json={}).status_code in {401, 403}

    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    matrix = client.get("/admin/internal-e2e/acceptance/phase-2-7a/matrix")
    assert matrix.status_code == 200
    assert matrix.json()["turn_ids"] == list(ACCEPTANCE_TURN_IDS)
    assert [t["input"] for t in matrix.json()["turns"]] == [CANONICAL[t][1] for t in ACCEPTANCE_TURN_IDS]

    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "false")
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS", "1")
    refused = client.post("/admin/internal-e2e/acceptance/phase-2-7a/runs", json={})
    assert refused.status_code == 409 and refused.json()["detail"] == "internal_e2e_disabled"
    assert app.state.executed == []

    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "true")
    _revoke(db)
    no_grant = client.post("/admin/internal-e2e/acceptance/phase-2-7a/runs", json={})
    assert no_grant.status_code == 409 and no_grant.json()["detail"] == "phase_2_7a_support_grant_missing"
    assert app.state.executed == []
    assert client.get("/admin/internal-e2e/acceptance/phase-2-7a/matrix").status_code == 200  # no grant needed
    _grant(db)
    missing = client.post("/admin/internal-e2e/acceptance/phase-2-7a/runs", json={})
    assert missing.status_code == 409 and missing.json()["detail"] == "phase_2_7a_fixture_missing:A"
    assert app.state.executed == []

    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    assert client.post("/admin/internal-e2e/acceptance/phase-2-7a/runs", json={"seed": 1}).status_code == 422
    queued = client.post("/admin/internal-e2e/acceptance/phase-2-7a/runs", json={})
    assert queued.status_code == 202
    body = queued.json()
    assert body["status"] == "queued" and body["turn_order"] == list(ACCEPTANCE_TURN_IDS)
    assert body["contract_version"] == ACCEPTANCE_CONTRACT_VERSION and body["turns_total"] == 12
    assert app.state.executed == [body["run_id"]]
    status = client.get(f"/admin/internal-e2e/acceptance/phase-2-7a/runs/{body['run_id']}")
    assert status.status_code == 200 and status.json()["run_id"] == body["run_id"]
    assert body["support_grant"]["tenant_id"] == 1 and body["acceptance_passed"] is False
    assert body["review_status"] == "pending" and body["classification"] is None
    # Reviews are refused until the run has finished.
    review = client.post(
        f"/admin/internal-e2e/acceptance/phase-2-7a/runs/{body['run_id']}/reviews",
        json={"turn_id": "A1", "assertion_index": 0, "verdict": "approved", "reviewer": "qa-lead", "evidence": "reply is a natural greeting"},
    )
    assert review.status_code == 409 and review.json()["detail"] == "phase_2_7a_review_run_not_finished"
    unknown = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
    absent = client.get(f"/admin/internal-e2e/acceptance/phase-2-7a/runs/{unknown}")
    assert absent.status_code == 404 and absent.json()["detail"] == "phase_2_7a_run_not_found"
    malformed = client.get("/admin/internal-e2e/acceptance/phase-2-7a/runs/" + "0" * 36)
    assert malformed.status_code == 409 and malformed.json()["detail"] == "phase_2_7a_run_id_invalid"


# ── Gap 1. Machine gate vs human review vs final acceptance ─────────────

def test_machine_pass_is_never_final_acceptance_without_reviews(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    report = _run(db, _fake_submit([]))
    assert report["machine_passed"] is True
    assert report["acceptance_passed"] is False
    assert report["review_status"] == "pending"
    assert report["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING
    assert report["review"]["assertions_total"] == sum(len(CANONICAL_ASSERTION_COUNTS[t]) for t in ACCEPTANCE_TURN_IDS)
    assert report["review"]["pending"] == report["review"]["assertions_total"]
    assert set(report["review"]["human_mandatory_turns"]) == set(ACCEPTANCE_TURN_IDS)
    assert "12/12" not in json.dumps({k: v for k, v in report.items() if k != "results"}).replace("12/12 machine checks", "")


def test_assertion_reviews_record_reviewer_timestamp_verdict_and_evidence(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    report = _run(db, _fake_submit([]))
    # Invalid inputs are refused.
    for kwargs, code in (
        ({"verdict": "maybe"}, "review_verdict_invalid"),
        ({"reviewer": "x"}, "review_reviewer_invalid"),
        ({"evidence": "ok"}, "review_evidence_invalid"),
        ({"turn_id": "D1"}, "review_turn_unknown"),
        ({"assertion_index": 99}, "review_assertion_unknown"),
    ):
        base = {"turn_id": "A1", "assertion_index": 0, "verdict": "approved", "reviewer": "qa-lead", "evidence": "reply greets naturally"}
        base.update(kwargs)
        with pytest.raises(InternalE2EContractError, match=code):
            apply_assertion_review(copy.deepcopy(report), **base)
    # Approve every assertion of every turn → ACCEPTANCE_PASSED.
    stamp = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)
    for row in report["results"]:
        for item in row["review"]["assertions"]:
            report = apply_assertion_review(
                report, turn_id=row["turn_id"], assertion_index=item["index"], verdict="approved",
                reviewer="qa-lead", evidence=f"checked {row['turn_id']} against the visible reply", reviewed_at=stamp,
            )
            if not (row["turn_id"] == "C4" and item["index"] == len(row["review"]["assertions"]) - 1):
                assert report["acceptance_passed"] is False
                assert report["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING
    assert report["review_status"] == "approved" and report["review"]["pending"] == 0
    assert report["acceptance_passed"] is True and report["classification"] == CLASS_ACCEPTANCE_PASSED
    ledger = report["results"][0]["review"]["assertions"][0]
    assert ledger == {**ledger, "verdict": "approved", "reviewer": "qa-lead", "reviewed_at": stamp.isoformat()}
    assert ledger["evidence"].startswith("checked A1")
    assert all(row["acceptance_passed"] for row in report["results"])
    # One rejection flips the run to REJECTED and clears final acceptance.
    report = apply_assertion_review(report, turn_id="A4", assertion_index=1, verdict="rejected", reviewer="qa-lead", evidence="reply claimed availability without evidence")
    assert report["review_status"] == "rejected" and report["acceptance_passed"] is False
    assert report["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED
    assert report["results"][3]["acceptance_passed"] is False


def test_reviews_cannot_rescue_a_failed_machine_gate_or_unexecuted_turn(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    report = _run(db, _fake_submit([], per_turn={"A2": {"tool_calls": []}}))
    assert report["classification"] == CLASS_MACHINE_GATE_FAILED
    for row in report["results"]:
        for item in row["review"]["assertions"]:
            report = apply_assertion_review(report, turn_id=row["turn_id"], assertion_index=item["index"], verdict="approved", reviewer="qa-lead", evidence="approved after reading the reply")
    assert report["review_status"] == "approved"
    assert report["acceptance_passed"] is False and report["classification"] == CLASS_MACHINE_GATE_FAILED
    halted = _run(db, _fake_submit([], per_turn={"B2": {"status": "test_contract_failed", "external_egress_count": 1}}))
    with pytest.raises(InternalE2EContractError, match="review_turn_not_executed"):
        apply_assertion_review(halted, turn_id="C1", assertion_index=0, verdict="approved", reviewer="qa-lead", evidence="never ran")


def test_persisted_run_review_endpoint_updates_classification(db: Any, api: tuple[TestClient, FastAPI]) -> None:
    client, app = api
    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    with patch.dict(os.environ, ENABLED_ENV):
        run = acceptance.create_acceptance_run(db, env=ENABLED_ENV)
        # Simulate the background completion by running the matrix with the fake submitter.
        report = _run(db, _fake_submit([]), run_id=run["run_id"])
        persist_completed_report(db, run["run_id"], report)
        status = client.get(f"/admin/internal-e2e/acceptance/phase-2-7a/runs/{run['run_id']}").json()
        assert status["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING
        assert status["acceptance_passed"] is False and status["machine_passed"] is True
        response = client.post(
            f"/admin/internal-e2e/acceptance/phase-2-7a/runs/{run['run_id']}/reviews",
            json={"turn_id": "A1", "assertion_index": 0, "verdict": "rejected", "reviewer": "qa-lead", "evidence": "greeting was in English"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED
        assert body["review"]["rejected"] == 1 and body["acceptance_passed"] is False
        ledger = body["report"]["results"][0]["review"]["assertions"][0]
        assert ledger["reviewer"] == "qa-lead" and ledger["reviewed_at"] and ledger["evidence"] == "greeting was in English"
        bad = client.post(
            f"/admin/internal-e2e/acceptance/phase-2-7a/runs/{run['run_id']}/reviews",
            json={"turn_id": "A1", "assertion_index": 0, "verdict": "approved", "reviewer": "qa-lead"},
        )
        assert bad.status_code == 422


# ── Gap 2. Customer B seed history is verified before any turn ───────────

def _seed_rows(db: Any) -> list[MessageEvent]:
    return db.query(MessageEvent).filter(MessageEvent.event_type == "internal_e2e_seed_history").order_by(MessageEvent.id.asc()).all()


def test_b_seed_history_is_verified_complete_and_b_only(db: Any) -> None:
    fixtures = provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    summary = verify_acceptance_fixtures(db)
    seed = summary["B"]["seed_history"]
    assert seed == {**seed, "rows": 32, "pagination_rows": 24, "reference_rows": 8, "verified": True}
    assert seed["conversation_id"] == fixtures["B"].conversation_id
    assert seed["referenced_product_ids"] == _catalog_ids(db)[:2]
    assert all(r.conversation_id == fixtures["B"].conversation_id for r in _seed_rows(db))


@pytest.mark.parametrize(
    "mutation, code",
    [
        ("delete_row", "phase_2_7a_b_seed_history_count_invalid"),
        ("edit_metadata_alias", "phase_2_7a_b_seed_history_metadata_mismatch"),
        ("edit_metadata_kind", "phase_2_7a_b_seed_history_metadata_mismatch"),
        ("add_foreign_row", "phase_2_7a_b_seed_history_count_invalid"),
        ("rebind_to_other_conversation", "phase_2_7a_b_seed_history_contaminated"),
        ("seed_row_in_a_conversation", "phase_2_7a_b_seed_history_contaminated"),
        ("delete_all", "phase_2_7a_b_seed_history_missing"),
        ("reorder_ids", "phase_2_7a_b_seed_history_order_invalid"),
        ("flip_direction", "phase_2_7a_b_seed_history_row_invalid"),
    ],
)
def test_tampered_b_seed_history_fails_closed_before_first_turn(db: Any, mutation: str, code: str) -> None:
    fixtures = provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    rows = _seed_rows(db)
    if mutation == "delete_row":
        db.delete(rows[5])
    elif mutation == "edit_metadata_alias":
        meta = dict(rows[3].extra_metadata); meta["synthetic_customer_alias"] = "A"; meta["internal_e2e_alias"] = "A"
        meta = {k: ("A" if v == "B" else v) for k, v in meta.items()}
        rows[3].extra_metadata = meta; flag_modified(rows[3], "extra_metadata")
    elif mutation == "edit_metadata_kind":
        meta = dict(rows[0].extra_metadata); meta["seed_history_kind"] = "reference"
        rows[0].extra_metadata = meta; flag_modified(rows[0], "extra_metadata")
    elif mutation == "add_foreign_row":
        db.add(MessageEvent(tenant_id=1, conversation_id=fixtures["B"].conversation_id, direction="internal_e2e_inbound", body="دخيل", event_type="internal_e2e_seed_history", extra_metadata={**dict(rows[0].extra_metadata), "internal_message_id": "internal_e2e:t1:b:seed:33"}))
    elif mutation == "rebind_to_other_conversation":
        rows[7].conversation_id = fixtures["C"].conversation_id
    elif mutation == "seed_row_in_a_conversation":
        db.add(MessageEvent(tenant_id=1, conversation_id=fixtures["A"].conversation_id, direction="internal_e2e_inbound", body="تلوث", event_type="internal_e2e_seed_history", extra_metadata=dict(rows[0].extra_metadata)))
    elif mutation == "delete_all":
        for row in rows:
            db.delete(row)
    elif mutation == "reorder_ids":
        a, b = dict(rows[0].extra_metadata), dict(rows[1].extra_metadata)
        rows[0].extra_metadata, rows[1].extra_metadata = b, a
        flag_modified(rows[0], "extra_metadata"); flag_modified(rows[1], "extra_metadata")
    elif mutation == "flip_direction":
        rows[0].direction = "internal_e2e_outbound"
    db.commit()
    calls: list[Any] = []
    with pytest.raises(InternalE2EContractError, match=code):
        _run(db, _fake_submit(calls))
    assert calls == []
    with pytest.raises(InternalE2EContractError, match=code):
        acceptance.create_acceptance_run(db, env=ENABLED_ENV)
    assert db.query(MessageEvent).filter(MessageEvent.event_type == "internal_e2e_acceptance_phase_2_7a").count() == 0


# ── Gap 3. B1→B4 continuity is machine-checked; seed history cannot mask it ──

def test_b_continuity_records_shown_and_selected_products_and_verifies_b3_b4(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    shoe, shirt = _catalog_ids(db)[:2]
    report = _run(db, _fake_submit([]))
    by_id = {row["turn_id"]: row for row in report["results"]}
    assert by_id["B1"]["continuity"]["products_shown"] == [shoe, shirt]
    assert by_id["B2"]["continuity"] == {**by_id["B2"]["continuity"], "status": "verified", "selected_product": shoe}
    for turn_id in ("B3", "B4"):
        assert by_id[turn_id]["continuity"]["status"] == "verified"
        assert by_id[turn_id]["continuity"]["selected_product"] == shoe
        assert by_id[turn_id]["machine_status"] == "passed"
        assert any(a["machine_check"] == "b_continuity_verified" for a in by_id[turn_id]["review"]["assertions"])
        assert all(a["human_mandatory"] for a in by_id[turn_id]["review"]["assertions"])
    assert report["continuity"] == {"b1_products_shown": [shoe, shirt], "b2_selected_product": shoe, "unverifiable_turns": [], "violated_turns": []}


def test_fallback_to_seed_history_product_fails_even_with_expected_tools(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    shoe, shirt = _catalog_ids(db)[:2]
    # A third product exists in the catalog; B1 shows only it, so shoe/shirt are seed-history products only.
    db.add(Product(tenant_id=1, external_id="GENERIC-BAG", title="حقيبة جلدية", price="300", in_stock=True, stock_quantity=2, catalog_status="active", extra_metadata={"status": "active", "currency": "SAR"}))
    db.commit()
    bag = _catalog_ids(db)[2]
    refs = lambda *ids: {"structured_reply": {"text": "x", "response_mode": "social", "product_refs": [{"product_id": i, "evidence_ref": f"ev:{i}"} for i in ids]}}
    # B2 picks a seed-history product instead of B1's grounded result.
    report = _run(db, _fake_submit([], per_turn={"B1": refs(bag), "B2": refs(shoe), "B3": refs(shoe), "B4": refs(shoe)}))
    by_id = {row["turn_id"]: row for row in report["results"]}
    assert by_id["B2"]["evidence"]["tool_calls"] == ["search_products"]  # expected tools were called…
    assert by_id["B2"]["blockers"] == ["b_continuity_seed_history_fallback"]  # …and it still fails
    assert by_id["B2"]["machine_passed"] is False and by_id["B2"]["continuity"]["status"] == "violated"
    assert by_id["B3"]["continuity"]["status"] == "unverifiable"  # no trusted selection to compare with
    assert report["machine_passed"] is False and report["classification"] == CLASS_MACHINE_GATE_FAILED
    assert report["continuity"]["violated_turns"] == ["B2"]
    # B3 silently switches to the seed-history product after a valid B2.
    report = _run(db, _fake_submit([], per_turn={"B1": refs(bag, shirt), "B2": refs(bag), "B3": refs(shirt), "B4": refs(bag)}))
    by_id = {row["turn_id"]: row for row in report["results"]}
    assert by_id["B2"]["continuity"]["status"] == "verified"
    assert by_id["B3"]["blockers"] == ["b_continuity_seed_history_fallback"] and by_id["B3"]["machine_passed"] is False
    assert by_id["B4"]["continuity"]["status"] == "verified"
    assert report["machine_passed"] is False
    # A switch to a non-seed product is a plain product switch; an ambiguous B2 selection also fails.
    report = _run(db, _fake_submit([], per_turn={"B1": refs(bag, shirt), "B2": refs(bag), "B3": refs(bag), "B4": refs(bag, shirt)}))
    assert {row["turn_id"]: row["blockers"] for row in report["results"]}["B4"] == ["b_continuity_seed_history_fallback"]
    report = _run(db, _fake_submit([], per_turn={"B1": refs(shoe, shirt), "B2": refs(shoe, shirt)}))
    assert {row["turn_id"]: row["blockers"] for row in report["results"]}["B2"] == ["b_continuity_selection_ambiguous"]
    report = _run(db, _fake_submit([], per_turn={"B1": refs(shoe, shirt), "B2": refs(shoe), "B3": refs(shoe), "B4": refs(bag)}))
    assert {row["turn_id"]: row["blockers"] for row in report["results"]}["B4"] == ["b_continuity_product_switched"]


def test_unextractable_product_identity_keeps_continuity_human_mandatory_and_acceptance_pending(db: Any) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    no_refs = {"structured_reply": {"text": "x", "response_mode": "social", "product_refs": []}}
    report = _run(db, _fake_submit([], per_turn={"B1": no_refs, "B2": no_refs, "B3": no_refs, "B4": no_refs}))
    by_id = {row["turn_id"]: row for row in report["results"]}
    for turn_id in ("B1", "B2", "B3", "B4"):
        assert by_id[turn_id]["continuity"]["status"] == "unverifiable"
        assert by_id[turn_id]["machine_status"] == "passed_unverified_continuity"
        assert all(a["human_mandatory"] and a["machine_check"] is None for a in by_id[turn_id]["review"]["assertions"])
    assert report["continuity"]["unverifiable_turns"] == ["B1", "B2", "B3", "B4"]
    assert report["machine_summary"] == "12/12 machine checks"
    assert report["acceptance_passed"] is False
    assert report["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING


# ── Gap 4. Support-access gate on the production run path ────────────────

def test_support_grant_gate_missing_expired_wrong_tenant_wrong_purpose_and_active(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    db.add(Tenant(id=2, name="other", is_active=True)); db.add(TenantSettings(tenant_id=2, extra_metadata={})); db.commit()
    calls: list[Any] = []
    fixtures_read: list[int] = []
    original = acceptance.verify_acceptance_fixtures
    monkeypatch.setattr(acceptance, "verify_acceptance_fixtures", lambda *a, **k: (fixtures_read.append(1), original(*a, **k))[1])

    def expect(code: str) -> None:
        with pytest.raises(InternalE2EContractError, match=code):
            _run(db, _fake_submit(calls))
        with pytest.raises(InternalE2EContractError, match=code):
            acceptance.create_acceptance_run(db, env=ENABLED_ENV)
        with pytest.raises(InternalE2EContractError, match=code):
            require_phase_2_7a_support_grant(db)
        assert calls == [] and fixtures_read == []
        assert db.query(MessageEvent).filter(MessageEvent.event_type == "internal_e2e_acceptance_phase_2_7a").count() == 0

    _revoke(db); expect("phase_2_7a_support_grant_missing")                       # missing
    _grant(db, enabled=False); expect("phase_2_7a_support_grant_missing")         # disabled block
    _grant(db, hours=-1); expect("phase_2_7a_support_grant_expired")              # expired
    _grant(db, hours=0.25); expect("phase_2_7a_support_grant_insufficient_remaining")  # below minimum scope
    _revoke(db); _grant(db, tenant_id=2); expect("phase_2_7a_support_grant_missing")  # wrong tenant
    _grant(db, reason="Investigate WhatsApp template rejection"); expect("phase_2_7a_support_grant_purpose_mismatch")  # wrong purpose
    _grant(db, reason="Phase 2.7A acceptance run — Salla review readiness")
    grant = require_phase_2_7a_support_grant(db)
    assert grant["tenant_id"] == 1 and grant["remaining_minutes"] >= 119 and "Phase 2.7A" in grant["purpose"]
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_support_grant_wrong_tenant"):
        require_phase_2_7a_support_grant(db, tenant_id=2)
    report = _run(db, _fake_submit(calls))
    assert len(calls) == 12 and report["support_grant"]["purpose"].startswith("Phase 2.7A")
    assert fixtures_read == [1]
    # Gate order: INTERNAL_E2E scope is checked before the grant.
    _revoke(db)
    with pytest.raises(InternalE2EContractError, match="internal_e2e_disabled"):
        asyncio.run(run_acceptance_matrix(db, env=DISABLED_ENV, submit_turn=_fake_submit(calls)))


CANONICAL_ASSERTION_COUNTS = {
    turn.turn_id: list(turn.required_assertions) for turn in load_acceptance_matrix().turns
}


# ── Final correction: review path is independent of the execution gates ──

def _finished_run(db: Any, **run_kwargs: Any) -> tuple[str, dict[str, Any]]:
    """Create, execute (fake submitter) and persist a finished run while INTERNAL_E2E is enabled."""
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    run = acceptance.create_acceptance_run(db, env=ENABLED_ENV)
    report = _run(db, _fake_submit([], **run_kwargs), run_id=run["run_id"])
    return run["run_id"], persist_completed_report(db, run["run_id"], report)


def _turn_rows(db: Any) -> int:
    return db.query(MessageEvent).filter(
        MessageEvent.direction.in_(["internal_e2e_inbound", "internal_e2e_outbound"])
    ).count()


def _review(db: Any, run_id: str, turn_id: str, index: int, verdict: str) -> dict[str, Any]:
    return record_acceptance_review(
        db, run_id, turn_id=turn_id, assertion_index=index, verdict=verdict,
        reviewer="qa-lead", evidence=f"{verdict} {turn_id}#{index} after reading the visible reply",
    )


def test_reviews_are_recorded_after_internal_e2e_is_disabled_and_runs_are_not(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, meta = _finished_run(db)
    assert meta["status"] == "completed" and meta["machine_digest"] and meta["support_grant"]["tenant_id"] == 1
    # Safe sequence: disable the execution channel (and let the grant lapse) before reviewing.
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "false")
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS", "1")
    _revoke(db)
    # Execution paths stay closed…
    with pytest.raises(InternalE2EContractError, match="internal_e2e_disabled"):
        acceptance.create_acceptance_run(db)
    with pytest.raises(InternalE2EContractError, match="internal_e2e_disabled"):
        asyncio.run(run_acceptance_matrix(db, submit_turn=_fake_submit([])))
    # …while reading and reviewing the finished run works without the channel or a live grant.
    assert acceptance.acceptance_run_status(db, run_id)["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING
    before_rows = _turn_rows(db)

    def _forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("review must not execute a turn, tool or agent")

    monkeypatch.setattr(acceptance, "submit_internal_customer_turn", _forbidden)
    monkeypatch.setattr(acceptance, "run_acceptance_matrix", _forbidden)
    updated = _review(db, run_id, "A4", 1, "rejected")
    assert updated["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED
    assert updated["acceptance_passed"] is False and updated["review"]["rejected"] == 1
    assert _turn_rows(db) == before_rows
    updated = _review(db, run_id, "A4", 1, "approved")
    for row in updated["report"]["results"]:
        for item in row["review"]["assertions"]:
            updated = _review(db, run_id, row["turn_id"], item["index"], "approved")
    assert updated["review_status"] == "approved" and updated["review"]["pending"] == 0
    assert updated["acceptance_passed"] is True and updated["classification"] == CLASS_ACCEPTANCE_PASSED
    assert updated["machine_passed"] is True and updated["machine_digest"] == machine_digest(updated["report"])
    assert _turn_rows(db) == before_rows
    ledger = updated["report"]["results"][3]["review"]["assertions"][1]
    assert ledger["reviewer"] == "qa-lead" and ledger["reviewed_at"] and ledger["verdict"] == "approved"


def test_review_refuses_unknown_unfinished_non_synthetic_or_mismatched_runs(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=ENABLED_ENV)
    queued = acceptance.create_acceptance_run(db, env=ENABLED_ENV)
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "false")
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_run_not_found"):
        _review(db, "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f", "A1", 0, "approved")
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_review_run_not_finished"):
        _review(db, queued["run_id"], "A1", 0, "approved")

    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "true")
    run_id, _meta = _finished_run(db)
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "false")
    control = acceptance._acceptance_control(db, run_id)

    def mutate(**changes: Any) -> None:
        meta = dict(control.extra_metadata or {})
        meta.update(changes)
        control.extra_metadata = meta
        flag_modified(control, "extra_metadata")
        db.commit()

    good = dict(control.extra_metadata or {})
    for changes, code in (
        ({"synthetic": False}, "review_run_not_synthetic"),
        ({"test_only": False}, "review_run_not_synthetic"),
        ({"channel": "whatsapp"}, "review_run_not_synthetic"),
        ({"contract_version": "commerce_v2_phase_2_7a_acceptance_v0"}, "review_contract_mismatch"),
        ({"matrix_sha256": "0" * 64}, "review_matrix_hash_mismatch"),
        ({"support_grant": None}, "review_run_grant_record_missing"),
        ({"machine_digest": "0" * 64}, "review_machine_evidence_tampered"),
    ):
        mutate(**changes)
        with pytest.raises(InternalE2EContractError, match=code):
            _review(db, run_id, "A1", 0, "approved")
        mutate(**{key: good[key] for key in changes})
    # Tampering with machine evidence inside the stored report is detected too.
    tampered = json.loads(json.dumps(good["report"]))
    tampered["results"][1]["machine_passed"] = True
    tampered["results"][1]["blockers"] = []
    tampered["results"][1]["evidence"]["tool_calls"] = ["search_products", "get_product_details"]
    mutate(report=tampered)
    with pytest.raises(InternalE2EContractError, match="review_machine_evidence_tampered"):
        _review(db, run_id, "A1", 0, "approved")
    mutate(report=good["report"])
    # A run for tenant 2 is never reviewable as a Phase 2.7A run.
    control.tenant_id = 2
    db.commit()
    with pytest.raises(InternalE2EContractError, match="phase_2_7a_run_not_found"):
        _review(db, run_id, "A1", 0, "approved")
    control.tenant_id = 1
    db.commit()
    assert _review(db, run_id, "A1", 0, "approved")["review"]["approved"] == 1


def test_review_cannot_change_machine_evidence_or_machine_verdicts(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, meta = _finished_run(db, per_turn={"A2": {"tool_calls": []}})
    assert meta["classification"] == CLASS_MACHINE_GATE_FAILED
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "false")
    frozen_before = {k: v for k, v in meta["report"].items() if k not in {"review_status", "review", "acceptance_passed", "classification"}}
    updated = meta
    for row in meta["report"]["results"]:
        for item in row["review"]["assertions"]:
            updated = _review(db, run_id, row["turn_id"], item["index"], "approved")
    frozen_after = {k: v for k, v in updated["report"].items() if k not in {"review_status", "review", "acceptance_passed", "classification"}}
    for key in ("machine_passed", "machine_summary", "turns_machine_passed", "continuity", "fixtures", "support_grant", "matrix_sha256"):
        assert frozen_before[key] == frozen_after[key]
    strip = lambda rows: [{k: v for k, v in r.items() if k not in {"review", "acceptance_passed"}} for r in rows]
    assert strip(frozen_before["results"]) == strip(frozen_after["results"])
    assert updated["review_status"] == "approved"
    assert updated["acceptance_passed"] is False and updated["classification"] == CLASS_MACHINE_GATE_FAILED
    assert updated["machine_digest"] == meta["machine_digest"] == machine_digest(updated["report"])


def test_review_endpoint_works_with_internal_e2e_disabled(db: Any, api: tuple[TestClient, FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    client, app = api
    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    run_id, _meta = _finished_run(db)
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "false")
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS", "1")
    _revoke(db)
    assert client.post("/admin/internal-e2e/acceptance/phase-2-7a/runs", json={}).status_code == 409
    assert client.get(f"/admin/internal-e2e/acceptance/phase-2-7a/runs/{run_id}").status_code == 200
    response = client.post(
        f"/admin/internal-e2e/acceptance/phase-2-7a/runs/{run_id}/reviews",
        json={"turn_id": "C4", "assertion_index": 2, "verdict": "rejected", "reviewer": "qa-lead", "evidence": "tracking link was invented"},
    )
    assert response.status_code == 200
    assert response.json()["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED
    assert app.state.executed == []  # reviewing never queues an execution
