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
from models import (  # noqa: E402
    Base,
    Conversation,
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
    ACCEPTANCE_MATRIX_PATH,
    ACCEPTANCE_TURN_IDS,
    load_acceptance_matrix,
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
    yield session
    session.close()
    engine.dispose()


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
        "structured_reply": {"text": "x", "response_mode": "social"},
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
    assert report["passed"] is False


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
        assert report["aliases"][alias]["all_passed"] is True
    assert all(row["passed"] for row in report["results"] if row["requires_prior_context"])


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
    assert report["turns_passed"] == 12 and report["summary"] == "12/12" and report["passed"] is True
    assert report["halted_at"] is None and report["external_egress_total"] == 0
    assert report["execution_mode"] == "INTERNAL_E2E" and report["tenant_id"] == 1
    assert report["fixtures"]["C"]["order_number"] == "IE2E-C-001"
    assert report["fixtures"]["C"]["tracking_present"] is True
    required = {
        "turn_id", "alias", "sequence", "input", "requires_prior_context", "expected",
        "required_assertions", "executed", "status", "passed", "halting", "blockers",
        "prior_turn_ids", "evidence",
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
    assert report["turns_executed"] == 6 and report["turns_passed"] == 5 and report["summary"] == "5/12"
    assert report["turns_not_executed"] == 6 and report["passed"] is False
    assert report["external_egress_total"] == 1
    assert all(row["blockers"] == ["halted_before_execution"] for row in report["results"][6:])

    # A non-halting expectation failure continues by default and halts with the flag.
    calls.clear()
    report = _run(db, _fake_submit(calls, per_turn={"A2": {"tool_calls": []}}))
    assert report["results"][1]["blockers"] == ["expected_tool_missing"]
    assert report["turns_executed"] == 12 and report["summary"] == "11/12" and report["passed"] is False
    calls.clear()
    report = _run(db, _fake_submit(calls, per_turn={"A2": {"tool_calls": []}}), halt_on_first_failure=True)
    assert report["halted_at"] == "A2" and report["turns_executed"] == 2


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
    unknown = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
    absent = client.get(f"/admin/internal-e2e/acceptance/phase-2-7a/runs/{unknown}")
    assert absent.status_code == 404 and absent.json()["detail"] == "phase_2_7a_run_not_found"
    malformed = client.get("/admin/internal-e2e/acceptance/phase-2-7a/runs/" + "0" * 36)
    assert malformed.status_code == 409 and malformed.json()["detail"] == "phase_2_7a_run_id_invalid"
