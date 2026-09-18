"""Phase 2.7B acceptance v2: bound cases, a real reset boundary, honest scoring.

Run 1 (`79a306c7-…`) scored 5/16, and most of those failures were the matrix's
fault rather than the agent's.  Cases named a fixture but nothing bound them to
the product it was attached to, every case ran on one conversation whose stored
turns leaked into the next, K10 counted the tenant's *own* policy as a
cross-tenant leak, K05 and K11 demanded a citation from a reply that asserted
nothing, and K13 expected a timeout the environment could not produce.

v2 is a new, separate contract.  The v1 artifact, its digest and Run 1's record
are never touched — the corrections here are gated on the contract version, so
the failed run keeps scoring exactly as it did.

Collected by the default root suite.  No provider and no model is reached:
turns are supplied through an injected callable.
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
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "backend"), os.path.join(REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import (  # noqa: E402
    Base,
    MerchantKnowledgeSection,
    MessageEvent,
    Tenant,
)
from modules.ai.commerce_agent_v2 import knowledge_retrieval  # noqa: E402
from modules.ai.commerce_agent_v2.internal_e2e_identity import (  # noqa: E402
    internal_e2e_metadata,
)
from services.commerce_v2_internal_e2e import _find_fixture  # noqa: E402
from services.commerce_v2_phase_2_7b_environment import (  # noqa: E402
    ACCEPTANCE_TENANT_MARKER,
    AcceptanceEnvironmentError,
    provision_knowledge_acceptance_environment,
    reset_acceptance_thread,
    verify_case_bindings,
)
from services.commerce_v2_phase_2_7b_faults import (  # noqa: E402
    AcceptanceFaultError,
    fault_injection_permitted,
    knowledge_fault,
)
from services.commerce_v2_phase_2_7b_knowledge_acceptance import (  # noqa: E402
    KNOWLEDGE_CONTRACT_VERSION_V1,
    KNOWLEDGE_CONTRACT_VERSION_V2,
    KnowledgeAcceptanceError,
    load_knowledge_acceptance_matrix,
    score_knowledge_turn,
)

ISOLATED_ENV = {"NAHLA_P27B_ISOLATED_ACCEPTANCE": "true"}
ACCEPTANCE_ENV = {
    "NAHLA_P27B_ISOLATED_ACCEPTANCE": "true",
    "NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED": "true",
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
    provision_knowledge_acceptance_environment(session, env=ISOLATED_ENV)
    yield session
    session.close()
    engine.dispose()


def _tenant_id(session: Any) -> int:
    return int(
        session.query(Tenant).filter(Tenant.name == ACCEPTANCE_TENANT_MARKER).one().id
    )


def _matrix() -> Any:
    return load_knowledge_acceptance_matrix(
        contract_version=KNOWLEDGE_CONTRACT_VERSION_V2
    )


def _seed_turns(session: Any, tenant_id: int, conversation_id: int, count: int) -> None:
    for index in range(count):
        session.add(
            MessageEvent(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                direction=(
                    "internal_e2e_inbound" if index % 2 == 0 else "internal_e2e_outbound"
                ),
                body=f"سابق {index}: جاكيت شتوي",
                event_type="internal_e2e_customer_turn",
                extra_metadata=internal_e2e_metadata(tenant_id, "A"),
            )
        )
    session.commit()


# ── the reset boundary really isolates, in the database and in the prompt ────


def test_reset_deletes_the_rows_the_model_reads(db: Any) -> None:
    tenant_id = _tenant_id(db)
    conversation_id = _find_fixture(db, tenant_id, "A").conversation_id
    _seed_turns(db, tenant_id, conversation_id, 4)

    assert (
        db.query(MessageEvent)
        .filter(MessageEvent.conversation_id == conversation_id)
        .count()
        == 4
    )
    report = reset_acceptance_thread(
        db, tenant_id=tenant_id, conversation_id=conversation_id, env=ISOLATED_ENV
    )

    assert report["messages_before"] == 4
    assert report["messages_deleted"] == 4
    assert report["messages_remaining"] == 0
    assert (
        db.query(MessageEvent)
        .filter(MessageEvent.conversation_id == conversation_id)
        .count()
        == 0
    )


def test_reset_clears_the_history_the_session_would_hand_the_model(db: Any) -> None:
    """The decisive one: the prompt is built from stored rows, not from memory.

    Without this, a previous case's turns keep answering the next case's
    pronouns — which is exactly how every Run 1 case ended up talking about the
    jacket.
    """
    from modules.ai.commerce_agent_v2.context import CommerceAgentContext
    from modules.ai.commerce_agent_v2.session import ConversationMessageSession

    tenant_id = _tenant_id(db)
    fixture = _find_fixture(db, tenant_id, "A")
    _seed_turns(db, tenant_id, fixture.conversation_id, 4)

    def history_items() -> list[Any]:
        context = CommerceAgentContext.from_trusted_scope(
            db=db,
            tenant_id=tenant_id,
            conversation_id=fixture.conversation_id,
            customer_id=fixture.customer_id,
            normalized_customer_phone=fixture.identity,
            connection_id="internal_e2e",
            inbound_trace_id="test",
            channel="internal_e2e",
            synthetic_customer_alias="A",
        )
        context.bind_run_user_input("وش مصدر هذا المنتج؟")
        return asyncio.run(ConversationMessageSession(context).get_items())

    before = history_items()
    assert len(before) == 4, "a prior case's turns do reach the model"

    reset_acceptance_thread(
        db,
        tenant_id=tenant_id,
        conversation_id=fixture.conversation_id,
        env=ISOLATED_ENV,
    )

    assert history_items() == [], "after the reset the model sees no earlier turn"


def test_reset_leaves_fixtures_and_other_conversations_intact(db: Any) -> None:
    tenant_id = _tenant_id(db)
    fixture = _find_fixture(db, tenant_id, "A")
    _seed_turns(db, tenant_id, fixture.conversation_id, 2)
    sections_before = db.query(MerchantKnowledgeSection).count()

    reset_acceptance_thread(
        db,
        tenant_id=tenant_id,
        conversation_id=fixture.conversation_id,
        env=ISOLATED_ENV,
    )

    assert db.query(MerchantKnowledgeSection).count() == sections_before
    assert _find_fixture(db, tenant_id, "A").conversation_id == fixture.conversation_id


def test_reset_refuses_outside_the_isolated_acceptance_database(db: Any) -> None:
    tenant_id = _tenant_id(db)
    conversation_id = _find_fixture(db, tenant_id, "A").conversation_id
    with pytest.raises(AcceptanceEnvironmentError):
        reset_acceptance_thread(
            db, tenant_id=tenant_id, conversation_id=conversation_id, env={}
        )


def test_no_evidence_or_refs_survive_into_the_next_case(db: Any) -> None:
    """Turn-scoped state lives on the context, and each turn builds a new one."""
    from modules.ai.commerce_agent_v2.context import CommerceAgentContext

    tenant_id = _tenant_id(db)
    fixture = _find_fixture(db, tenant_id, "A")

    def fresh() -> CommerceAgentContext:
        return CommerceAgentContext.from_trusted_scope(
            db=db,
            tenant_id=tenant_id,
            conversation_id=fixture.conversation_id,
            customer_id=fixture.customer_id,
            normalized_customer_phone=fixture.identity,
            connection_id="internal_e2e",
            inbound_trace_id="test",
            channel="internal_e2e",
            synthetic_customer_alias="A",
        )

    first = fresh()
    first.bind_run_user_input("وش مصدر هذا المنتج؟")
    first.authorize_products([1], titles={1: "جاكيت شتوي"})
    first.mark_knowledge_section_emitted(3)

    second = fresh()
    second.bind_run_user_input("كيف أعتني بها؟")
    assert second.evidence == {}
    assert second.knowledge_lookups == []
    assert second.authorized_product_ids == set()
    assert second.authorized_product_anchors([1]) == ([], [])
    assert not second.knowledge_section_emitted(3)


# ── B1. every case reaches the product and section it declares ───────────────


def test_every_v2_case_declares_a_complete_binding() -> None:
    matrix = _matrix()
    assert matrix.contract_version == KNOWLEDGE_CONTRACT_VERSION_V2
    for case in matrix.cases:
        assert case.thread, case.case_id
        assert case.reset_thread_before is True, case.case_id


def test_bindings_resolve_against_the_database(db: Any) -> None:
    report = verify_case_bindings(db, _tenant_id(db), _matrix())
    cases = report["cases"]
    assert set(cases) == set(_matrix().case_ids)
    assert cases["K04"]["product_sku"] == "P27B-SKIRT"
    assert cases["K06"]["product_sku"] == "P27B-HONEY"
    assert cases["K07"]["product_sku"] == "P27B-SOAP"
    assert cases["K14"]["product_sku"] == "P27B-HONEY"
    assert cases["K15"]["product_sku"] == "P27B-SOAP"
    assert cases["K13"]["knowledge_fault_mode"] == "timeout"


def test_a_section_bound_to_the_wrong_product_fails_closed(db: Any) -> None:
    """The exact Run 1 defect: usage on the skirt, case aimed at the jacket."""
    from types import SimpleNamespace

    broken = SimpleNamespace(
        contract_version=KNOWLEDGE_CONTRACT_VERSION_V2,
        cases=[
            SimpleNamespace(
                case_id="KXX",
                thread="jacket",
                reset_thread_before=True,
                target_product_sku="P27B-JACKET",
                required_section_fixture="product_linked_usage",
                forbidden_section_fixtures=(),
                anchor_turns=(),
                knowledge_fault_mode="",
            )
        ],
    )
    with pytest.raises(AcceptanceEnvironmentError) as excinfo:
        verify_case_bindings(db, _tenant_id(db), broken)
    assert "binding_section_not_linked_to_target" in str(excinfo.value)


def test_a_missing_target_product_fails_closed(db: Any) -> None:
    from types import SimpleNamespace

    broken = SimpleNamespace(
        contract_version=KNOWLEDGE_CONTRACT_VERSION_V2,
        cases=[
            SimpleNamespace(
                case_id="KXX",
                thread="ghost",
                reset_thread_before=True,
                target_product_sku="P27B-DOES-NOT-EXIST",
                required_section_fixture="",
                forbidden_section_fixtures=(),
                anchor_turns=(),
                knowledge_fault_mode="",
            )
        ],
    )
    with pytest.raises(AcceptanceEnvironmentError) as excinfo:
        verify_case_bindings(db, _tenant_id(db), broken)
    assert "binding_target_product_missing" in str(excinfo.value)


def test_case_order_does_not_change_binding_resolution(db: Any) -> None:
    matrix = _matrix()
    forward = verify_case_bindings(db, _tenant_id(db), matrix)["cases"]

    from types import SimpleNamespace

    reversed_matrix = SimpleNamespace(
        contract_version=matrix.contract_version, cases=list(reversed(matrix.cases))
    )
    backward = verify_case_bindings(db, _tenant_id(db), reversed_matrix)["cases"]
    assert forward == backward


# ── B2. tenant isolation is decided by the database record ──────────────────


def _artifact(**overrides: Any) -> dict[str, Any]:
    artifact = {
        "tenant_id": 1,
        "knowledge_lookup_attempted": True,
        "knowledge_lookups": [],
        "structured_reply": {},
        "tool_calls": [],
    }
    artifact.update(overrides)
    return artifact


def test_citing_the_tenants_own_policy_is_not_a_leak(db: Any) -> None:
    """Run 1 failed K10 for answering from the tenant's own return policy."""
    tenant_id = _tenant_id(db)
    own = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.tenant_id == tenant_id,
            MerchantKnowledgeSection.title == "سياسة الاسترجاع",
        )
        .one()
    )
    case = _matrix().case("K10")
    artifact = _artifact(
        tenant_id=tenant_id,
        knowledge_lookups=[
            {"scope": "turn", "status": "ok", "tenant_id": tenant_id,
             "section_ids": [int(own.id)], "evidence_refs": [f"kb:section:{own.id}"]}
        ],
        structured_reply={"evidence_refs": [f"kb:section:{own.id}"]},
    )
    scored = score_knowledge_turn(
        case, artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V2
    )
    assert "cross_tenant_knowledge_exposed" not in scored["blockers"]


def test_citing_another_tenants_section_is_a_leak(db: Any) -> None:
    tenant_id = _tenant_id(db)
    foreign = (
        db.query(MerchantKnowledgeSection)
        .filter(MerchantKnowledgeSection.tenant_id != tenant_id)
        .first()
    )
    assert foreign is not None
    case = _matrix().case("K10")
    artifact = _artifact(
        tenant_id=tenant_id,
        knowledge_lookups=[
            {"scope": "turn", "status": "ok", "tenant_id": tenant_id,
             "section_ids": [int(foreign.id)], "evidence_refs": []}
        ],
        structured_reply={"evidence_refs": [f"kb:section:{foreign.id}"]},
    )
    scored = score_knowledge_turn(
        case, artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V2
    )
    assert "cross_tenant_knowledge_exposed" in scored["blockers"]


def test_v1_scoring_of_k10_is_unchanged(db: Any) -> None:
    """The frozen contract keeps its old, stricter reading."""
    v1_case = load_knowledge_acceptance_matrix(
        contract_version=KNOWLEDGE_CONTRACT_VERSION_V1
    ).case("K10")
    artifact = _artifact(
        knowledge_lookups=[
            {"scope": "turn", "status": "ok", "tenant_id": 1,
             "section_ids": [1], "evidence_refs": ["kb:section:1"]}
        ],
        structured_reply={"evidence_refs": ["kb:section:1"]},
    )
    scored = score_knowledge_turn(v1_case, artifact)
    assert "cross_tenant_knowledge_exposed" in scored["blockers"]


# ── B5. a reply that asserts nothing is exempt; an unsupported claim is not ──


def test_safe_fallback_without_any_claim_is_not_missing_evidence(db: Any) -> None:
    """Run 1's K05 and K11 replies asserted nothing at all."""
    case = _matrix().case("K05")
    artifact = _artifact(
        structured_reply={
            "safe_fallback_reason": "insufficient_grounded_evidence",
            "fact_claims": [],
            "evidence_refs": [],
        }
    )
    scored = score_knowledge_turn(
        case, artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V2
    )
    assert "knowledge_claim_missing_evidence" not in scored["blockers"]


def test_explicit_absence_disclosure_is_not_missing_evidence(db: Any) -> None:
    case = _matrix().case("K11")
    artifact = _artifact(
        knowledge_gap_disclosure=True,
        structured_reply={"fact_claims": [], "evidence_refs": []},
    )
    scored = score_knowledge_turn(
        case, artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V2
    )
    assert "knowledge_claim_missing_evidence" not in scored["blockers"]


def test_an_unsupported_knowledge_claim_still_blocks(db: Any) -> None:
    """The exemption must never cover an actual claim without evidence."""
    case = _matrix().case("K05")
    artifact = _artifact(
        structured_reply={
            "fact_claims": [{"kind": "origin", "value": "ورشة محلية"}],
            "evidence_refs": [],
        }
    )
    scored = score_knowledge_turn(
        case, artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V2
    )
    assert "knowledge_claim_missing_evidence" in scored["blockers"]


def test_knowledge_used_in_reply_without_citation_still_blocks(db: Any) -> None:
    case = _matrix().case("K05")
    artifact = _artifact(
        knowledge_evidence_used_in_reply=["kb:section:3"],
        structured_reply={"fact_claims": [], "evidence_refs": []},
    )
    scored = score_knowledge_turn(
        case, artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V2
    )
    assert "knowledge_claim_missing_evidence" in scored["blockers"]


def test_v1_scoring_of_a_claimless_reply_is_unchanged() -> None:
    v1_case = load_knowledge_acceptance_matrix(
        contract_version=KNOWLEDGE_CONTRACT_VERSION_V1
    ).case("K05")
    artifact = _artifact(
        structured_reply={"safe_fallback_reason": "insufficient_grounded_evidence"}
    )
    scored = score_knowledge_turn(v1_case, artifact)
    assert "knowledge_claim_missing_evidence" in scored["blockers"]


# ── B3. the fault is deterministic, narrow, and removes itself ──────────────


def test_fault_requires_both_acceptance_guards() -> None:
    assert fault_injection_permitted(ACCEPTANCE_ENV) is True
    assert fault_injection_permitted({}) is False
    assert (
        fault_injection_permitted({"NAHLA_P27B_ISOLATED_ACCEPTANCE": "true"}) is False
    )
    assert (
        fault_injection_permitted(
            {"NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED": "true"}
        )
        is False
    )


def test_production_construction_rejects_the_fault_flag() -> None:
    """No production environment can arm this."""
    with pytest.raises(AcceptanceFaultError) as excinfo:
        with knowledge_fault("timeout", env={}):
            pass
    assert "not_permitted" in str(excinfo.value)
    assert knowledge_retrieval.active_knowledge_fault() is None


def test_unsupported_fault_mode_is_refused() -> None:
    with pytest.raises(AcceptanceFaultError):
        with knowledge_fault("explode", env=ACCEPTANCE_ENV):
            pass


def test_fault_is_armed_only_inside_its_case_and_then_removed() -> None:
    assert knowledge_retrieval.active_knowledge_fault() is None
    with knowledge_fault("timeout", env=ACCEPTANCE_ENV):
        assert knowledge_retrieval.active_knowledge_fault() == "timeout"
    assert knowledge_retrieval.active_knowledge_fault() is None


def test_fault_is_removed_even_when_the_case_raises() -> None:
    with pytest.raises(ValueError):
        with knowledge_fault("error", env=ACCEPTANCE_ENV):
            raise ValueError("case blew up")
    assert knowledge_retrieval.active_knowledge_fault() is None


def test_two_faults_cannot_be_armed_at_once() -> None:
    with knowledge_fault("timeout", env=ACCEPTANCE_ENV):
        with pytest.raises(AcceptanceFaultError):
            with knowledge_fault("error", env=ACCEPTANCE_ENV):
                pass
    assert knowledge_retrieval.active_knowledge_fault() is None


# ── v1 stays frozen ─────────────────────────────────────────────────────────


def test_the_v1_artifact_and_its_digest_are_unchanged() -> None:
    v1 = load_knowledge_acceptance_matrix(
        contract_version=KNOWLEDGE_CONTRACT_VERSION_V1
    )
    assert v1.contract_version == KNOWLEDGE_CONTRACT_VERSION_V1
    assert (
        v1.matrix_sha256
        == "46f04522b947fc1aaf29090a51a67139290955264abe72a9018d338c7448fcc3"
    )
    assert len(v1.cases) == 16
    for case in v1.cases:
        assert dict(case.binding) == {}


def test_v2_is_a_separate_artifact_with_its_own_digest() -> None:
    v1 = load_knowledge_acceptance_matrix(
        contract_version=KNOWLEDGE_CONTRACT_VERSION_V1
    )
    v2 = _matrix()
    assert v2.matrix_sha256 != v1.matrix_sha256
    assert v2.contract_version != v1.contract_version


def test_an_unknown_contract_version_fails_closed() -> None:
    with pytest.raises(KnowledgeAcceptanceError):
        load_knowledge_acceptance_matrix(contract_version="v99")


# ── B3 continued: the fault hits knowledge only, and the catalog still answers ─


def test_catalog_answer_survives_the_knowledge_fault_and_ledger_records_it(
    db: Any,
) -> None:
    """K13's whole point: Salla facts survive while knowledge fails loudly."""
    from modules.ai.commerce_agent_v2.context import CommerceAgentContext
    from modules.ai.commerce_agent_v2.tools.catalog import search_products
    from tests.test_phase_2_7b_knowledge_grounding import _invoke

    tenant_id = _tenant_id(db)
    fixture = _find_fixture(db, tenant_id, "A")
    context = CommerceAgentContext.from_trusted_scope(
        db=db,
        tenant_id=tenant_id,
        conversation_id=fixture.conversation_id,
        customer_id=fixture.customer_id,
        normalized_customer_phone=fixture.identity,
        connection_id="internal_e2e",
        inbound_trace_id="test",
        channel="internal_e2e",
        synthetic_customer_alias="A",
    )
    context.bind_run_user_input("وش سعره ووش مصدره؟")

    with knowledge_fault("timeout", env=ACCEPTANCE_ENV):
        result = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    # The catalog answer is untouched.
    assert result.status == "ok"
    assert result.products, "the catalog still returns the product"
    assert str(result.products[0].price) == "169"
    # Knowledge returned nothing, and said so.
    assert not result.knowledge_sections
    lookups = context.knowledge_lookups
    assert lookups, "the attempt is still recorded"
    assert any(item["status"] == "timeout" for item in lookups)
    assert all(
        str(item.get("failure_reason") or "").startswith(
            "phase_2_7b_injected_knowledge_fault"
        )
        for item in lookups
        if item["status"] == "timeout"
    )
    assert knowledge_retrieval.active_knowledge_fault() is None


def test_after_the_fault_case_knowledge_retrieval_works_again(db: Any) -> None:
    from modules.ai.commerce_agent_v2.context import CommerceAgentContext
    from modules.ai.commerce_agent_v2.tools.catalog import search_products
    from tests.test_phase_2_7b_knowledge_grounding import _invoke

    tenant_id = _tenant_id(db)
    fixture = _find_fixture(db, tenant_id, "A")

    def run_turn() -> Any:
        context = CommerceAgentContext.from_trusted_scope(
            db=db,
            tenant_id=tenant_id,
            conversation_id=fixture.conversation_id,
            customer_id=fixture.customer_id,
            normalized_customer_phone=fixture.identity,
            connection_id="internal_e2e",
            inbound_trace_id="test",
            channel="internal_e2e",
            synthetic_customer_alias="A",
        )
        context.bind_run_user_input("وش مصدر هذا المنتج؟")
        return context, asyncio.run(
            _invoke(search_products, context, {"query": "جاكيت", "limit": 5})
        )

    with knowledge_fault("timeout", env=ACCEPTANCE_ENV):
        faulted_context, _ = run_turn()
    assert any(item["status"] == "timeout" for item in faulted_context.knowledge_lookups)

    healthy_context, healthy = run_turn()
    assert any(item["status"] == "ok" for item in healthy_context.knowledge_lookups)
    assert healthy.knowledge_sections


# ── full v2 lifecycle on PostgreSQL ─────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("A1_PG_TEST_DATABASE_URL"),
    reason="PostgreSQL lifecycle requires A1_PG_TEST_DATABASE_URL",
)
def test_v2_lifecycle_provision_to_cleanup_on_postgresql() -> None:
    """provision → resolve → create → execute → score → review → cleanup."""
    from services.commerce_v2_phase_2_7b_environment import (
        cleanup_knowledge_acceptance_environment,
        describe_knowledge_acceptance_environment,
        verify_acceptance_fixtures,
    )
    from services.commerce_v2_phase_2_7b_knowledge_acceptance import (
        create_knowledge_acceptance_run,
        execute_knowledge_acceptance_run,
        knowledge_run_status,
        record_knowledge_review,
    )

    url = os.environ["A1_PG_TEST_DATABASE_URL"]
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        provision_knowledge_acceptance_environment(session, env=ISOLATED_ENV)
        environment = describe_knowledge_acceptance_environment(session, env=ISOLATED_ENV)
        tenant_id = int(environment["tenant_id"])
        conversation_id = int(sorted(environment["conversations"].values())[0])

        verify_acceptance_fixtures(session, tenant_id)
        matrix = _matrix()
        bindings = verify_case_bindings(session, tenant_id, matrix)
        assert len(bindings["cases"]) == 16

        meta = create_knowledge_acceptance_run(
            session,
            commit="c8f59cd2bf473d9904e354d2b0fbe3c9a33c19c1",
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            contract_version=matrix.contract_version,
            matrix=matrix,
        )
        assert meta["status"] == "queued"
        assert meta["matrix_sha256"] == matrix.matrix_sha256

        order: list[str] = []
        resets: list[int] = []

        async def submit(db_session: Any, case: Any) -> dict[str, Any]:
            if case.reset_thread_before:
                report = reset_acceptance_thread(
                    db_session,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    env=ISOLATED_ENV,
                )
                resets.append(int(report["messages_remaining"]))
            order.append(case.case_id)
            return _artifact(
                tenant_id=tenant_id,
                knowledge_lookups=[
                    {"scope": scope, "status": "ok", "tenant_id": tenant_id,
                     "section_ids": [], "evidence_refs": []}
                    for scope in (case.expected.get("required_scopes") or ["turn"])
                ],
                structured_reply={"safe_fallback_reason": "insufficient_grounded_evidence"},
                tool_calls=list(case.expected.get("expected_tools") or []),
            )

        finished = asyncio.run(
            execute_knowledge_acceptance_run(
                session, meta["run_id"], submit_case=submit,
                tenant_id=tenant_id, matrix=matrix,
            )
        )
        assert order == list(matrix.case_ids), "cases run in matrix order"
        assert resets and all(count == 0 for count in resets), "every reset emptied the thread"
        assert finished["cases_executed"] == 16
        assert finished["contract_version"] == KNOWLEDGE_CONTRACT_VERSION_V2

        status = knowledge_run_status(session, meta["run_id"])
        digest_before = status["machine_digest"]
        assert status["review"]["pending"] > 0

        first = matrix.cases[0]
        reviewed = record_knowledge_review(
            session, meta["run_id"], case_id=first.case_id, assertion_index=0,
            verdict="approved", reviewer="test", evidence="lifecycle test",
        )
        assert reviewed["review"]["approved"] == 1
        assert knowledge_run_status(session, meta["run_id"])["machine_digest"] == digest_before

        report = cleanup_knowledge_acceptance_environment(session, env=ISOLATED_ENV)
        assert int(report["remaining_rows"]) == 0
    finally:
        session.close()
        engine.dispose()


def test_reset_never_deletes_the_run_control_row(db: Any) -> None:
    """The control row lives on the same conversation; the reset must spare it."""
    from services.commerce_v2_phase_2_7b_knowledge_acceptance import (
        CONTROL_DIRECTION,
        create_knowledge_acceptance_run,
    )

    tenant_id = _tenant_id(db)
    conversation_id = _find_fixture(db, tenant_id, "A").conversation_id
    matrix = _matrix()
    meta = create_knowledge_acceptance_run(
        db,
        commit="c8f59cd2bf473d9904e354d2b0fbe3c9a33c19c1",
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        contract_version=matrix.contract_version,
        matrix=matrix,
    )
    _seed_turns(db, tenant_id, conversation_id, 3)

    report = reset_acceptance_thread(
        db, tenant_id=tenant_id, conversation_id=conversation_id, env=ISOLATED_ENV
    )

    assert report["messages_deleted"] == 3
    control_rows = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.conversation_id == conversation_id,
            MessageEvent.direction == CONTROL_DIRECTION,
        )
        .all()
    )
    assert len(control_rows) == 1
    assert dict(control_rows[0].extra_metadata or {})["run_id"] == meta["run_id"]
