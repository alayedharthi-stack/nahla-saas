"""Phase 2.7B executable acceptance: run record first, review after, digest always.

Phase 2.7A executed its matrix through a CLI that never wrote a run record, so
the finished report had to be reconstructed onto a control row afterwards.  The
Phase 2.7B harness writes the record before the first case and fills it in, and
these tests hold that lifecycle: creation, ordered execution, persistence,
review integrity under a machine-evidence digest, and retry protection.

Collected by the default root suite.  No provider and no outbound path is
reached: cases are submitted through an injected callable.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "backend"), os.path.join(REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Base, MessageEvent, Tenant  # noqa: E402
from services.commerce_v2_phase_2_7b_environment import (  # noqa: E402
    ACCEPTANCE_TENANT_MARKER,
    AcceptanceEnvironmentError,
    assert_isolated_acceptance_database,
    cleanup_knowledge_acceptance_environment,
    describe_knowledge_acceptance_environment,
    provision_knowledge_acceptance_environment,
)
from services.commerce_v2_phase_2_7b_knowledge_acceptance import (  # noqa: E402
    CLASS_ACCEPTANCE_PASSED,
    CLASS_MACHINE_GATE_FAILED,
    CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING,
    CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED,
    CONTROL_EVENT_TYPE,
    KnowledgeAcceptanceError,
    create_knowledge_acceptance_run,
    execute_knowledge_acceptance_run,
    knowledge_run_status,
    load_knowledge_acceptance_matrix,
    machine_digest,
    record_knowledge_review,
)

ISOLATED_ENV = {"NAHLA_P27B_ISOLATED_ACCEPTANCE": "true"}
COMMIT = "5eacc6d150115dbc664f6272448b592d44516849"


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
    provision_knowledge_acceptance_environment(session, env=ISOLATED_ENV)
    yield session
    session.close()
    engine.dispose()


def _environment(db: Any) -> dict[str, Any]:
    return describe_knowledge_acceptance_environment(db, env=ISOLATED_ENV)


def _create(db: Any, commit: str = COMMIT) -> dict[str, Any]:
    environment = _environment(db)
    return create_knowledge_acceptance_run(
        db,
        commit=commit,
        conversation_id=sorted(environment["conversations"].values())[0],
        tenant_id=int(environment["tenant_id"]),
    )


def _case_artifact(case: Any, tenant_id: int, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    expected = dict(case.expected)
    citation = str(expected.get("knowledge_citation") or "optional")
    cites_knowledge = citation == "required"
    lookups = [
        {
            "scope": scope,
            "purpose": "catalog_product_knowledge",
            "tenant_id": tenant_id,
            "status": (expected.get("allowed_statuses") or ["ok"])[0],
            "hit_count": 1 if cites_knowledge else 0,
            "section_ids": [11] if cites_knowledge else [],
            "evidence_refs": ["kb:section:11"] if cites_knowledge else [],
        }
        for scope in (expected.get("required_scopes") or ["turn"])
    ]
    claims: list[dict[str, Any]] = [
        {"kind": "price", "evidence_ref": "catalog:product:1", "value": 169}
    ]
    refs = ["catalog:product:1"]
    if cites_knowledge:
        claims.append(
            {"kind": "product_knowledge", "evidence_ref": "kb:section:11", "value": "مصدر"}
        )
        refs.append("kb:section:11")
    artifact: dict[str, Any] = {
        "tenant_id": tenant_id,
        "tool_calls": list(expected.get("expected_tools") or []),
        "knowledge_lookup_attempted": 1,
        "knowledge_lookups": lookups,
        "knowledge_conflicts": (
            [
                {
                    "kind": kind,
                    "product_id": 1,
                    "catalog_value": 169.0,
                    "knowledge_values": [99.0],
                    "resolution": "structured_catalog_wins",
                }
                for kind in (expected.get("expected_conflicts") or [])
            ]
        ),
        "knowledge_gap_disclosure": 1 if expected.get("absence_disclosure_required") else 0,
        "customer_visible_text": f"رد تجريبي على: {case.input}",
        "external_egress_count": 0,
        "structured_reply": {
            "evidence_refs": refs,
            "fact_claims": claims,
            "safe_fallback_reason": None,
        },
    }
    artifact.update(overrides or {})
    return artifact


def _submit(calls: list[str], tenant_id: int, *, per_case: dict[str, dict[str, Any]] | None = None):
    async def submit_case(_db: Any, case: Any) -> dict[str, Any]:
        calls.append(case.case_id)
        return _case_artifact(case, tenant_id, (per_case or {}).get(case.case_id))

    return submit_case


def _execute(db: Any, run_id: str, calls: list[str], **kwargs: Any) -> dict[str, Any]:
    environment = _environment(db)
    return asyncio.run(
        execute_knowledge_acceptance_run(
            db,
            run_id,
            submit_case=_submit(calls, int(environment["tenant_id"]), **kwargs),
            tenant_id=int(environment["tenant_id"]),
        )
    )


# ── Creation: the record exists before the first case ────────────────────────

def test_the_run_record_is_written_before_any_case_executes(db: Any) -> None:
    matrix = load_knowledge_acceptance_matrix()
    meta = _create(db)

    control = (
        db.query(MessageEvent).filter(MessageEvent.event_type == CONTROL_EVENT_TYPE).one()
    )
    stored = dict(control.extra_metadata)
    assert stored["status"] == "queued"
    assert stored["run_id"] == meta["run_id"]
    assert stored["contract_version"] == matrix.contract_version
    assert stored["matrix_sha256"] == matrix.matrix_sha256
    assert stored["commit"] == COMMIT
    assert stored["case_order"] == list(matrix.case_ids)
    assert stored["synthetic"] is True and stored["test_only"] is True
    assert stored["cases_executed"] == 0
    assert "report" not in stored


def test_a_second_active_run_is_refused(db: Any) -> None:
    _create(db)
    with pytest.raises(KnowledgeAcceptanceError, match="phase_2_7b_run_already_active"):
        _create(db)


def test_creation_requires_a_commit(db: Any) -> None:
    environment = _environment(db)
    with pytest.raises(KnowledgeAcceptanceError, match="phase_2_7b_run_commit_required"):
        create_knowledge_acceptance_run(
            db,
            commit="  ",
            conversation_id=sorted(environment["conversations"].values())[0],
            tenant_id=int(environment["tenant_id"]),
        )


# ── Execution: ordered, persisted, digest-protected ──────────────────────────

def test_execution_runs_k01_to_k16_in_order_and_persists_the_report(db: Any) -> None:
    matrix = load_knowledge_acceptance_matrix()
    meta = _create(db)
    calls: list[str] = []

    finished = _execute(db, meta["run_id"], calls)

    assert calls == list(matrix.case_ids)
    assert finished["status"] == "completed"
    assert finished["cases_executed"] == 16
    assert finished["machine_summary"] == "16/16 machine checks"
    assert finished["machine_passed"] is True
    assert finished["external_egress_total"] == 0
    assert finished["machine_digest"] == machine_digest(finished["report"])
    assert finished["report"]["commit"] == COMMIT
    assert finished["report"]["matrix_sha256"] == matrix.matrix_sha256
    assert [row["case_id"] for row in finished["report"]["cases"]] == list(matrix.case_ids)
    for row in finished["report"]["cases"]:
        assert row["knowledge_lookup_attempted"] is True
        assert row["review"]["status"] == "pending"
        assert row["tool_calls"] == list(matrix.case(row["case_id"]).expected.get("expected_tools") or [])


def test_machine_pass_is_never_acceptance(db: Any) -> None:
    meta = _create(db)
    finished = _execute(db, meta["run_id"], [])

    assert finished["machine_passed"] is True
    assert finished["acceptance_passed"] is False
    assert finished["review_status"] == "pending"
    assert finished["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING
    assert finished["review"]["pending"] == finished["review"]["assertions_total"]


def test_a_failing_case_fails_the_machine_gate(db: Any) -> None:
    meta = _create(db)
    finished = _execute(
        db,
        meta["run_id"],
        [],
        per_case={"K01": {"knowledge_lookup_attempted": 0, "knowledge_lookups": []}},
    )

    assert finished["machine_passed"] is False
    assert finished["cases_machine_failed"] == ["K01"]
    assert finished["classification"] == CLASS_MACHINE_GATE_FAILED
    failed = next(row for row in finished["report"]["cases"] if row["case_id"] == "K01")
    assert "knowledge_lookup_not_attempted" in failed["blockers"]


def test_any_external_egress_fails_the_machine_gate(db: Any) -> None:
    meta = _create(db)
    finished = _execute(db, meta["run_id"], [], per_case={"K05": {"external_egress_count": 1}})

    assert finished["external_egress_total"] == 1
    assert finished["machine_passed"] is False


def test_a_case_from_another_tenant_fails_closed(db: Any) -> None:
    meta = _create(db)
    with pytest.raises(KnowledgeAcceptanceError, match="phase_2_7b_case_tenant_mismatch"):
        _execute(db, meta["run_id"], [], per_case={"K01": {"tenant_id": 999}})


def test_a_run_cannot_be_executed_twice(db: Any) -> None:
    meta = _create(db)
    _execute(db, meta["run_id"], [])

    with pytest.raises(KnowledgeAcceptanceError, match="phase_2_7b_run_not_queued"):
        _execute(db, meta["run_id"], [])


def test_an_unknown_run_fails_closed(db: Any) -> None:
    with pytest.raises(KnowledgeAcceptanceError, match="phase_2_7b_run_not_found"):
        knowledge_run_status(db, "0f50ca43-b7d4-42f5-8b57-b20ea907a034")
    with pytest.raises(KnowledgeAcceptanceError, match="phase_2_7b_run_id_invalid"):
        knowledge_run_status(db, "not-a-uuid")


# ── Review: after the run, with INTERNAL_E2E disabled, digest enforced ───────

def _approve_all(db: Any, run_id: str) -> dict[str, Any]:
    meta = knowledge_run_status(db, run_id)
    for row in meta["report"]["cases"]:
        for index in range(len(row["review"]["assertions"])):
            meta = record_knowledge_review(
                db,
                run_id,
                case_id=row["case_id"],
                assertion_index=index,
                verdict="approved",
                reviewer="owner:acceptance-test",
                evidence=f"reviewed {row['case_id']} assertion {index}",
            )
    return meta


def test_review_works_after_internal_e2e_is_disabled_and_only_touches_review_fields(
    db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _create(db)
    finished = _execute(db, meta["run_id"], [])
    digest = finished["machine_digest"]
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "false")

    reviewed = record_knowledge_review(
        db,
        meta["run_id"],
        case_id="K01",
        assertion_index=0,
        verdict="approved",
        reviewer="owner:acceptance-test",
        evidence="owner reviewed the visible reply",
    )

    assert reviewed["machine_digest"] == digest
    assert reviewed["review"]["approved"] == 1
    assert reviewed["acceptance_passed"] is False
    assert reviewed["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_PENDING
    row = next(item for item in reviewed["report"]["cases"] if item["case_id"] == "K01")
    assert row["review"]["assertions"][0]["reviewer"] == "owner:acceptance-test"
    assert row["review"]["assertions"][0]["reviewed_at"]
    assert row["blockers"] == []


def test_acceptance_passes_only_when_every_assertion_is_approved(db: Any) -> None:
    meta = _create(db)
    _execute(db, meta["run_id"], [])

    final = _approve_all(db, meta["run_id"])

    assert final["review"]["pending"] == 0
    assert final["review"]["rejected"] == 0
    assert final["acceptance_passed"] is True
    assert final["classification"] == CLASS_ACCEPTANCE_PASSED


def test_one_rejection_blocks_acceptance(db: Any) -> None:
    meta = _create(db)
    _execute(db, meta["run_id"], [])
    _approve_all(db, meta["run_id"])

    rejected = record_knowledge_review(
        db,
        meta["run_id"],
        case_id="K14",
        assertion_index=0,
        verdict="rejected",
        reviewer="owner:acceptance-test",
        evidence="the reply expanded a merchant statement into medical advice",
    )

    assert rejected["acceptance_passed"] is False
    assert rejected["classification"] == CLASS_MACHINE_GATE_PASSED_HUMAN_REVIEW_REJECTED


def test_review_cannot_rescue_a_failed_machine_gate(db: Any) -> None:
    meta = _create(db)
    _execute(db, meta["run_id"], [], per_case={"K01": {"knowledge_lookups": []}})

    final = _approve_all(db, meta["run_id"])

    assert final["review"]["pending"] == 0
    assert final["acceptance_passed"] is False
    assert final["classification"] == CLASS_MACHINE_GATE_FAILED


def test_tampered_machine_evidence_is_refused(db: Any) -> None:
    meta = _create(db)
    _execute(db, meta["run_id"], [])
    control = db.query(MessageEvent).filter(MessageEvent.event_type == CONTROL_EVENT_TYPE).one()
    stored = json.loads(json.dumps(dict(control.extra_metadata)))
    stored["report"]["cases"][0]["blockers"] = []
    stored["report"]["cases"][0]["passed"] = True
    stored["report"]["machine_summary"] = "16/16 machine checks (edited)"
    control.extra_metadata = stored
    flag_modified(control, "extra_metadata")
    db.commit()

    with pytest.raises(KnowledgeAcceptanceError, match="phase_2_7b_review_machine_evidence_tampered"):
        record_knowledge_review(
            db,
            meta["run_id"],
            case_id="K01",
            assertion_index=0,
            verdict="approved",
            reviewer="owner:acceptance-test",
            evidence="attempted review over edited evidence",
        )


def test_review_refuses_an_unfinished_run_and_invalid_input(db: Any) -> None:
    meta = _create(db)
    with pytest.raises(KnowledgeAcceptanceError, match="phase_2_7b_review_run_not_finished"):
        record_knowledge_review(
            db, meta["run_id"], case_id="K01", assertion_index=0, verdict="approved",
            reviewer="owner:acceptance-test", evidence="too early",
        )
    _execute(db, meta["run_id"], [])
    for kwargs, code in (
        ({"verdict": "maybe"}, "phase_2_7b_review_verdict_invalid"),
        ({"reviewer": "x"}, "phase_2_7b_review_reviewer_invalid"),
        ({"evidence": "no"}, "phase_2_7b_review_evidence_invalid"),
        ({"case_id": "K99"}, "phase_2_7b_review_case_unknown"),
        ({"assertion_index": 99}, "phase_2_7b_review_assertion_unknown"),
    ):
        call = {
            "case_id": "K01",
            "assertion_index": 0,
            "verdict": "approved",
            "reviewer": "owner:acceptance-test",
            "evidence": "valid evidence line",
        }
        call.update(kwargs)
        with pytest.raises(KnowledgeAcceptanceError, match=code):
            record_knowledge_review(db, meta["run_id"], **call)


def test_a_non_synthetic_run_record_is_refused(db: Any) -> None:
    meta = _create(db)
    control = db.query(MessageEvent).filter(MessageEvent.event_type == CONTROL_EVENT_TYPE).one()
    stored = dict(control.extra_metadata)
    stored["synthetic"] = False
    control.extra_metadata = stored
    flag_modified(control, "extra_metadata")
    db.commit()

    with pytest.raises(KnowledgeAcceptanceError, match="phase_2_7b_run_not_synthetic"):
        knowledge_run_status(db, meta["run_id"])


# ── Isolated environment: guard, idempotency, cleanup reporting ──────────────

def test_the_environment_is_idempotent_and_reports_its_contents(db: Any) -> None:
    first = _environment(db)
    again = provision_knowledge_acceptance_environment(db, env=ISOLATED_ENV)

    assert first["tenant_id"] == again["tenant_id"]
    assert len(again["knowledge_sections"]) == len(first["knowledge_sections"]) == 10
    assert len(again["products"]) == 4
    assert again["orders"] == 1
    assert any(
        entry["tenant_id"] != again["tenant_id"] for entry in again["knowledge_sections"].values()
    ), "the cross-tenant case needs a neighbouring tenant's section"
    assert any(not entry["is_active"] for entry in again["knowledge_sections"].values())


def test_cleanup_reports_exactly_what_it_removed(db: Any) -> None:
    report = cleanup_knowledge_acceptance_environment(db, env=ISOLATED_ENV)

    assert report["deleted"]["tenants"] == 2
    assert report["deleted"]["knowledge_sections"] == 10
    assert report["deleted"]["products"] == 4
    assert report["remaining_rows"] == 0
    assert describe_knowledge_acceptance_environment(db, env=ISOLATED_ENV)["provisioned"] is False


def test_the_guard_refuses_a_database_that_holds_anything_real(db: Any) -> None:
    db.add(Tenant(name="متجر حقيقي", is_active=True))
    db.commit()

    with pytest.raises(AcceptanceEnvironmentError, match="database_contains_foreign_tenants"):
        assert_isolated_acceptance_database(db, env=ISOLATED_ENV)
    with pytest.raises(AcceptanceEnvironmentError, match="database_contains_foreign_tenants"):
        provision_knowledge_acceptance_environment(db, env=ISOLATED_ENV)


def test_the_guard_refuses_without_the_explicit_isolation_flag(db: Any) -> None:
    with pytest.raises(AcceptanceEnvironmentError, match="not_declared_isolated"):
        assert_isolated_acceptance_database(db, env={})
    with pytest.raises(AcceptanceEnvironmentError, match="not_declared_isolated"):
        provision_knowledge_acceptance_environment(db, env={"NAHLA_P27B_ISOLATED_ACCEPTANCE": "no"})


def test_the_acceptance_tenant_is_recognisable_and_self_contained(db: Any) -> None:
    tenants = [row.name for row in db.query(Tenant).all()]

    assert all(name.startswith(ACCEPTANCE_TENANT_MARKER) for name in tenants)
    assert len(tenants) == 2


@pytest.mark.skipif(
    not os.environ.get("A1_PG_TEST_DATABASE_URL"),
    reason="PostgreSQL integration URL not configured",
)
def test_the_run_record_survives_a_postgresql_round_trip() -> None:
    """The control row is JSONB in production; a review must re-read it intact."""
    url = os.environ["A1_PG_TEST_DATABASE_URL"]
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    try:
        provision_knowledge_acceptance_environment(db, env=ISOLATED_ENV)
        environment = describe_knowledge_acceptance_environment(db, env=ISOLATED_ENV)
        tenant_id = int(environment["tenant_id"])
        meta = create_knowledge_acceptance_run(
            db,
            commit=COMMIT,
            conversation_id=sorted(environment["conversations"].values())[0],
            tenant_id=tenant_id,
        )
        calls: list[str] = []
        finished = asyncio.run(
            execute_knowledge_acceptance_run(
                db,
                meta["run_id"],
                submit_case=_submit(calls, tenant_id),
                tenant_id=tenant_id,
            )
        )
        assert finished["machine_passed"] is True
        digest = finished["machine_digest"]
        db.close()

        fresh = session_factory()
        try:
            reloaded = knowledge_run_status(fresh, meta["run_id"], tenant_id=tenant_id)
            assert reloaded["machine_digest"] == digest
            assert machine_digest(reloaded["report"]) == digest
            reviewed = record_knowledge_review(
                fresh,
                meta["run_id"],
                case_id="K01",
                assertion_index=0,
                verdict="approved",
                reviewer="owner:acceptance-test",
                evidence="reviewed on PostgreSQL",
                tenant_id=tenant_id,
            )
            assert reviewed["machine_digest"] == digest
            assert reviewed["review"]["approved"] == 1
            cleanup = cleanup_knowledge_acceptance_environment(fresh, env=ISOLATED_ENV)
            assert cleanup["deleted"]["tenants"] == 2
            assert cleanup["remaining_rows"] == 0
        finally:
            fresh.close()
    finally:
        db.close()
        engine.dispose()
