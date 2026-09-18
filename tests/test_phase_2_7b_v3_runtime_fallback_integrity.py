"""Phase 2.7B v3: a runtime fallback is never a compliant answer.

Run 2 (`d2571f64-…`) passed 16/16, but its persisted artifacts show two of those
passes were the runner's own substitute text, not answers:

* K16 failed ``output_guardrail_tripwire:claim_span_not_equivalent:product_knowledge``
  — the model cited the right section but paraphrased it — and the code was not
  retryable, so the guardrail's fallback replaced the reply.  The v2 scorer read
  the fallback's ``safe_fallback_reason`` as "asserts nothing" and passed a case
  whose citation was *required*.
* K15 failed ``availability_in_text_without_verified_claim`` twice.  Its one
  grounding retry ran in a fresh model session, yet ``_knowledge_sections_emitted``
  survived the retry, so the retry's tool results carried no sections while the
  ledger recorded hits.  The fallback was then read as an absence disclosure.

Three corrections, each held here: the retry re-exposes sections; a paraphrased
knowledge span earns the single re-grounding retry; and v3 — a new contract, v1
and v2 untouched — scores status, failure reason and fallback classification.

Collected by the default root suite.  No provider is reached: the model is a
script, the tools and guardrails are real.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tracing import set_trace_provider
from agents.tracing.provider import DefaultTraceProvider
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "backend"), os.path.join(REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Base, MerchantKnowledgeSection, Tenant  # noqa: E402
from modules.ai.commerce_agent_v2 import knowledge_retrieval  # noqa: E402
from modules.ai.commerce_agent_v2.knowledge_retrieval import SCOPE_PRODUCT  # noqa: E402
from modules.ai.commerce_agent_v2.output import (  # noqa: E402
    CommerceReply,
    FactClaim,
    ProductReference,
)
from modules.ai.commerce_agent_v2.runner import (  # noqa: E402
    GROUNDING_RETRY_FACTUAL,
    GROUNDING_RETRY_KNOWLEDGE_SPAN,
    run_commerce_agent,
    safe_fallback_reply,
)
from modules.ai.commerce_agent_v2.tools.catalog import search_products  # noqa: E402
from modules.ai.commerce_agent_v2.tools.knowledge import (  # noqa: E402
    search_product_knowledge,
)
from services.commerce_v2_phase_2_7b_environment import (  # noqa: E402
    ACCEPTANCE_TENANT_MARKER,
    FIXTURE_SECTION_TITLES,
    provision_knowledge_acceptance_environment,
    reset_acceptance_thread,
    verify_case_bindings,
)
from services.commerce_v2_phase_2_7b_knowledge_acceptance import (  # noqa: E402
    FALLBACK_KIND_EXPECTED,
    FALLBACK_KIND_MODEL_DISCLOSURE,
    FALLBACK_KIND_NONE,
    FALLBACK_KIND_UNEXPECTED,
    KNOWLEDGE_CONTRACT_VERSION_V1,
    KNOWLEDGE_CONTRACT_VERSION_V2,
    KNOWLEDGE_CONTRACT_VERSION_V3,
    KNOWLEDGE_CONTRACT_VERSIONS,
    load_knowledge_acceptance_matrix,
    score_knowledge_turn,
)

from tests.test_phase_2_7b_knowledge_grounding import (  # noqa: E402
    Seed,
    _context,
    _invoke,
    seeded,  # noqa: F401 — pytest fixture
)

set_trace_provider(DefaultTraceProvider())

ISOLATED_ENV = {"NAHLA_P27B_ISOLATED_ACCEPTANCE": "true"}
K16_CODE = "claim_span_not_equivalent:product_knowledge"
K15_CODE = "availability_in_text_without_verified_claim"
FALLBACK_TEXT = safe_fallback_reply("x").text


# ── helpers ─────────────────────────────────────────────────────────────────


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
    return int(session.query(Tenant).filter(Tenant.name == ACCEPTANCE_TENANT_MARKER).one().id)


def _section_id(session: Any, tenant_id: int, fixture: str, *, foreign: bool = False) -> int:
    query = session.query(MerchantKnowledgeSection).filter(
        MerchantKnowledgeSection.title == FIXTURE_SECTION_TITLES[fixture]
    )
    if foreign:
        query = query.filter(MerchantKnowledgeSection.tenant_id != tenant_id)
    else:
        query = query.filter(MerchantKnowledgeSection.tenant_id == tenant_id)
    return int(query.one().id)


def _v3() -> Any:
    return load_knowledge_acceptance_matrix(contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)


def _v2() -> Any:
    return load_knowledge_acceptance_matrix(contract_version=KNOWLEDGE_CONTRACT_VERSION_V2)


def _artifact(**overrides: Any) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "tenant_id": 1,
        "status": "completed",
        "failure_reason": "",
        "fallback_type": "none",
        "knowledge_gap_disclosure": 0,
        "guardrail_result": [],
        "tool_trace": [],
        "knowledge_lookup_attempted": True,
        "knowledge_lookups": [],
        "knowledge_evidence_refs": [],
        "knowledge_evidence_used_in_reply": [],
        "structured_reply": {},
        "tool_calls": [],
    }
    artifact.update(overrides)
    return artifact


def _tripped(code: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "commerce_v2_grounded_structured_output",
            "tripwire_triggered": True,
            "output_info": {"passed": False, "errors": [code]},
        }
    ]


def _retry_event(code: str, reason: str) -> dict[str, Any]:
    return {
        "kind": "grounding_retry",
        "reason": reason,
        "tool_choice": "required",
        "guardrail": "commerce_v2_grounded_structured_output",
        "errors": [code],
    }


def _lookup(scope: str, section_ids: list[int], *, tenant_id: int = 1, status: str = "ok") -> dict:
    return {
        "scope": scope,
        "status": status,
        "tenant_id": tenant_id,
        "section_ids": section_ids,
        "evidence_refs": [f"kb:section:{sid}" for sid in section_ids],
    }


def _knowledge_reply(seed: Seed, text: str, span: str) -> CommerceReply:
    catalog_ref = f"catalog:product:{seed.jacket.id}"
    kb_ref = f"kb:section:{seed.origin_section.id}"
    return CommerceReply(
        text=text,
        evidence_refs=[catalog_ref, kb_ref],
        fact_claims=[
            FactClaim(
                kind="product_knowledge",
                value=seed.origin_section.body,
                evidence_ref=kb_ref,
                subject_product_id=seed.jacket.id,
                text_span=span,
            )
        ],
        product_refs=[ProductReference(product_id=seed.jacket.id, evidence_ref=catalog_ref)],
    )


def _paraphrase(seed: Seed) -> CommerceReply:
    """K16: the right section, cited, with a span the guardrail cannot support."""
    text = "الجاكيت مصنوع يدويًا في ورشة محلية."
    return _knowledge_reply(seed, text, text)


def _verbatim(seed: Seed) -> CommerceReply:
    """K03: the section body, reproduced as it is."""
    body = seed.origin_section.body
    return _knowledge_reply(seed, body, body)


def _tool_output_seen_by(model: ScriptedModel, call_index: int) -> str:
    return json.dumps(model.calls[call_index].input, ensure_ascii=False, default=str)


def _search(call_id: str) -> Any:
    return function_call("search_products", {"query": "جاكيت", "limit": 5}, call_id=call_id)


# ── Runtime fix 2: K16's persisted failure now earns one re-grounding retry ──


def test_k16_paraphrased_span_retries_once_and_cites_the_section(seeded: Seed) -> None:
    """Persisted K16: pass 1 rejected with the exact code; the retry sees §3 again."""
    model = ScriptedModel(
        [
            [_search("call-1")],
            [assistant_message(_paraphrase(seeded).model_dump_json())],
            [_search("call-2")],
            [assistant_message(_verbatim(seeded).model_dump_json())],
        ]
    )
    result = asyncio.run(run_commerce_agent(
        context=_context(seeded, user_input="وش مصدر هذا المنتج؟"),
        user_input="وش مصدر هذا المنتج؟",
        model=model,
        model_name="k16-regrounding",
        execution_mode="outbound",
    ))

    assert result.status == "completed"
    assert result.reply == _verbatim(seeded)
    retries = [e for e in result.tool_trace if e.get("kind") == "grounding_retry"]
    assert len(retries) == 1
    assert retries[0]["reason"] == GROUNDING_RETRY_KNOWLEDGE_SPAN
    assert retries[0]["errors"] == [K16_CODE]
    # The delivered reply's guardrails are the retry's, and they passed.
    assert result.guardrail_results[-1]["tripwire_triggered"] is False
    # The retry carries the knowledge-specific instruction and requires a tool.
    retry_input = json.dumps(model.calls[2].input, ensure_ascii=False)
    assert "الاقتباس الحرفي" in retry_input
    assert "غير متوفرة لديك" in retry_input
    assert model.calls[2].model_settings.tool_choice == "required"
    # Section 3 reached the model in BOTH passes.
    kb_ref = f"kb:section:{seeded.origin_section.id}"
    assert kb_ref in _tool_output_seen_by(model, 1)
    assert kb_ref in _tool_output_seen_by(model, 3)
    # Delivered evidence is unique; the ledger still shows the attempt.
    kb_refs = [ref for ref in result.reply.evidence_refs if ref.startswith("kb:section:")]
    assert kb_refs == [kb_ref]
    product_lookups = [i for i in result.knowledge_lookups if i["scope"] == SCOPE_PRODUCT]
    assert product_lookups and product_lookups[0]["status"] == "ok"


def test_k16_second_paraphrase_stays_failed_with_no_second_retry(seeded: Seed) -> None:
    model = ScriptedModel(
        [
            [_search("call-1")],
            [assistant_message(_paraphrase(seeded).model_dump_json())],
            [_search("call-2")],
            [assistant_message(_paraphrase(seeded).model_dump_json())],
        ]
    )
    result = asyncio.run(run_commerce_agent(
        context=_context(seeded, user_input="وش مصدر هذا المنتج؟"),
        user_input="وش مصدر هذا المنتج؟",
        model=model,
        model_name="k16-regrounding-failed",
        execution_mode="outbound",
    ))

    assert result.status == "failed"
    assert result.failure_reason == f"output_guardrail_tripwire:{K16_CODE}"
    assert result.reply.text == FALLBACK_TEXT
    assert result.reply.safe_fallback_reason == result.failure_reason
    assert sum(e.get("kind") == "grounding_retry" for e in result.tool_trace) == 1
    assert len(model.calls) == 4, "fail closed after the single retry"
    assert result.guardrail_results[0]["output_info"]["errors"] == [K16_CODE]


def test_non_knowledge_span_codes_are_still_not_retried(seeded: Seed) -> None:
    """Only the two knowledge-span codes joined the retry path."""
    catalog_ref = f"catalog:product:{seeded.jacket.id}"
    rejected = CommerceReply(
        text="افتح الصفحة.",
        evidence_refs=["kb:section:999999"],  # unknown ref: never retryable
        product_refs=[ProductReference(product_id=seeded.jacket.id, evidence_ref=catalog_ref)],
    )
    model = ScriptedModel([[_search("call-1")], [assistant_message(rejected.model_dump_json())]])
    result = asyncio.run(run_commerce_agent(
        context=_context(seeded, user_input="وش مصدر هذا المنتج؟"),
        user_input="وش مصدر هذا المنتج؟",
        model=model,
        model_name="no-retry",
        execution_mode="outbound",
    ))
    assert result.status == "failed"
    assert not [e for e in result.tool_trace if e.get("kind") == "grounding_retry"]
    assert len(model.calls) == 2


# ── Runtime fix 1: the retry sees the sections again ────────────────────────


def test_grounding_retry_re_exposes_sections_without_a_second_lookup(seeded: Seed) -> None:
    calls = {"count": 0}
    real = knowledge_retrieval._retrieve

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        return real(*args, **kwargs)

    knowledge_retrieval._retrieve = counted  # type: ignore[assignment]
    try:
        context = _context(seeded, user_input="وش مصدر هذا المنتج؟")
        first = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
        assert [s.section_id for s in first.knowledge_sections] == [seeded.origin_section.id]
        # Within a pass, the model's own lookup does not repeat the section.
        again = asyncio.run(
            _invoke(
                search_product_knowledge,
                context,
                {"product_id": seeded.jacket.id, "query": "مصدر الجاكيت", "limit": 4},
            )
        )
        assert again.status == "no_evidence"
        ledger_before = [dict(i) for i in context.knowledge_lookups]
        retrieves_before = calls["count"]
        assert context.knowledge_sections_emitted

        context.activate_grounding_retry()

        assert context.grounding_retry_active is True
        assert context.knowledge_sections_emitted == []
        assert context.knowledge_lookups == ledger_before, "the ledger is not cleared"
        titles, _ = context.authorized_product_anchors([seeded.jacket.id])
        assert titles == [seeded.jacket.title], "authorization survives the retry"

        second = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
        assert [s.section_id for s in second.knowledge_sections] == [seeded.origin_section.id]
        assert second.knowledge_sections[0].body == seeded.origin_section.body
        assert calls["count"] == retrieves_before, "cached rows answered; no new database lookup"
        assert context.knowledge_lookups == ledger_before, "an identical repeat adds no ledger row"
        # A differently worded attempt is still recorded, section and all.
        asyncio.run(
            _invoke(
                search_product_knowledge,
                context,
                {"product_id": seeded.jacket.id, "query": "من أين يأتي هذا الجاكيت", "limit": 4},
            )
        )
        assert len(context.knowledge_lookups) == len(ledger_before) + 1
        assert context.knowledge_lookups[-1]["section_ids"] == [seeded.origin_section.id]
    finally:
        knowledge_retrieval._retrieve = real  # type: ignore[assignment]


def test_k15_retry_sees_the_sections_the_first_pass_saw(seeded: Seed) -> None:
    """Persisted K15: availability without a claim, one factual retry.

    In Run 2 the retry's tool results carried no sections at all.  Now the
    retry's ``search_products`` carries the section again, and both of the
    model's differently worded knowledge lookups stay in the ledger.
    """
    catalog_ref = f"catalog:product:{seeded.jacket.id}"
    ungrounded = CommerceReply(
        text="الجاكيت متوفر حاليًا، لكن ما عندي معلومة موثقة عن مصدره.",
        evidence_refs=[catalog_ref],
        product_refs=[ProductReference(product_id=seeded.jacket.id, evidence_ref=catalog_ref)],
    )
    disclosed = CommerceReply(
        text="ما عندي معلومة موثقة عن مصدر هذا الجاكيت.",
        evidence_refs=[catalog_ref],
        product_refs=[ProductReference(product_id=seeded.jacket.id, evidence_ref=catalog_ref)],
    )
    kb_query = {"product_id": seeded.jacket.id, "limit": 5}
    model = ScriptedModel(
        [
            [_search("call-1")],
            # Worded unlike the customer's question: «مصدر الجاكيت» normalises to
            # the same signature as the turn itself and would resolve to the
            # recorded catalog attempt instead of adding a ledger row.
            [function_call("search_product_knowledge", {**kb_query, "query": "خامة الجاكيت وطريقة صنعه"}, call_id="kb-1")],
            [assistant_message(ungrounded.model_dump_json())],
            [_search("call-2")],
            [function_call("search_product_knowledge", {**kb_query, "query": "من أين يأتي الجاكيت"}, call_id="kb-2")],
            [assistant_message(disclosed.model_dump_json())],
        ]
    )
    result = asyncio.run(run_commerce_agent(
        context=_context(seeded, user_input="وش مصدر الجاكيت؟"),
        user_input="وش مصدر الجاكيت؟",
        model=model,
        model_name="k15-retry-visibility",
        execution_mode="outbound",
    ))

    assert result.status == "completed"
    retries = [e for e in result.tool_trace if e.get("kind") == "grounding_retry"]
    assert len(retries) == 1
    assert retries[0]["reason"] == GROUNDING_RETRY_FACTUAL
    assert K15_CODE in retries[0]["errors"]
    kb_ref = f"kb:section:{seeded.origin_section.id}"
    # Pass 1: the catalog tool exposed the section; the model's own lookup did not repeat it.
    assert kb_ref in _tool_output_seen_by(model, 1)
    assert "no_matching_knowledge" in _tool_output_seen_by(model, 2)
    # Pass 2 (the retry): the catalog tool exposes the section AGAIN.
    assert kb_ref in _tool_output_seen_by(model, 4)
    # Every attempt stays in the ledger: one catalog lookup, two model lookups.
    purposes = [i["purpose"] for i in result.knowledge_lookups]
    assert purposes.count("catalog_product_knowledge") == 1
    assert purposes.count("model_product_knowledge") == 2
    assert all(i["status"] == "ok" for i in result.knowledge_lookups if i["scope"] == SCOPE_PRODUCT)


# ── v3 scorer: K16 ──────────────────────────────────────────────────────────


def _k16_persisted(section_id: int, other_id: int, *, tenant_id: int) -> dict[str, Any]:
    """The K16 artifact exactly as Run 2 persisted it (redacted shape)."""
    fallback = safe_fallback_reply(f"output_guardrail_tripwire:{K16_CODE}")
    return _artifact(
        tenant_id=tenant_id,
        status="failed",
        failure_reason=f"output_guardrail_tripwire:{K16_CODE}",
        fallback_type="unexpected_runtime_fallback",
        guardrail_result=_tripped(K16_CODE),
        knowledge_lookups=[
            _lookup("turn", [], tenant_id=tenant_id, status="no_results"),
            _lookup("product", [section_id, other_id], tenant_id=tenant_id),
        ],
        knowledge_evidence_refs=[f"kb:section:{other_id}", f"kb:section:{section_id}"],
        structured_reply=fallback.model_dump(mode="json"),
        tool_calls=["search_products"],
    )


def test_k16_persisted_failure_passes_v2_and_fails_v3(db: Any) -> None:
    tenant_id = _tenant_id(db)
    origin = _section_id(db, tenant_id, "product_linked_origin")
    description = _section_id(db, tenant_id, "product_linked_description")
    artifact = _k16_persisted(origin, description, tenant_id=tenant_id)

    v2 = score_knowledge_turn(_v2().case("K16"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V2)
    assert v2["passed"] is True, "this is the gap: Run 2 scored the fallback as a pass"

    v3 = score_knowledge_turn(_v3().case("K16"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert v3["passed"] is False
    blockers = set(v3["blockers"])
    assert {
        "knowledge_claim_missing_evidence",
        f"unexpected_runtime_fallback:output_guardrail_tripwire:{K16_CODE}",
        f"turn_not_completed:output_guardrail_tripwire:{K16_CODE}",
        "required_citation_replaced_by_fallback",
        "required_section_retrieved_but_unused",
        "required_section_not_cited",
        "knowledge_refs_delivered_count_mismatch:0",
    } <= blockers
    assert v3["status"] == "failed"
    assert v3["failure_reason"] == f"output_guardrail_tripwire:{K16_CODE}"
    assert v3["fallback_type"] == "unexpected_runtime_fallback"
    assert v3["fallback_kind"] == FALLBACK_KIND_UNEXPECTED
    assert v3["guardrail_passed"] is False
    assert v3["guardrail_error_codes"] == [K16_CODE]
    assert v3["grounding_retries"] == 0
    assert v3["required_section_id"] == origin


def test_k16_cited_once_after_one_retry_passes_v3(db: Any) -> None:
    tenant_id = _tenant_id(db)
    origin = _section_id(db, tenant_id, "product_linked_origin")
    description = _section_id(db, tenant_id, "product_linked_description")
    ref = f"kb:section:{origin}"
    artifact = _artifact(
        tenant_id=tenant_id,
        guardrail_result=[{"name": "commerce_v2_grounded_structured_output", "tripwire_triggered": False, "output_info": {"passed": True, "errors": []}}],
        tool_trace=[_retry_event(K16_CODE, GROUNDING_RETRY_KNOWLEDGE_SPAN)],
        knowledge_lookups=[
            _lookup("turn", [], tenant_id=tenant_id, status="no_results"),
            _lookup("product", [origin, description], tenant_id=tenant_id),
        ],
        knowledge_evidence_refs=[f"kb:section:{description}", ref],
        knowledge_evidence_used_in_reply=[ref],
        structured_reply={
            "evidence_refs": [f"catalog:product:1", ref],
            "fact_claims": [{"kind": "product_knowledge", "evidence_ref": ref, "subject_product_id": 1}],
        },
        tool_calls=["search_products", "search_products"],
    )
    scored = score_knowledge_turn(_v3().case("K16"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert scored["blockers"] == []
    assert scored["passed"] is True
    assert scored["fallback_kind"] == FALLBACK_KIND_NONE
    assert scored["grounding_retries"] == 1
    assert scored["original_guardrail_errors"] == [K16_CODE], "the report keeps the first rejection"
    assert scored["knowledge_refs_delivered"] == [ref]


def test_k16_two_sections_cited_fails_the_exactly_one_rule(db: Any) -> None:
    tenant_id = _tenant_id(db)
    origin = _section_id(db, tenant_id, "product_linked_origin")
    description = _section_id(db, tenant_id, "product_linked_description")
    artifact = _artifact(
        tenant_id=tenant_id,
        knowledge_lookups=[_lookup("product", [origin, description], tenant_id=tenant_id)],
        knowledge_evidence_used_in_reply=[f"kb:section:{origin}", f"kb:section:{description}"],
        structured_reply={"evidence_refs": [f"kb:section:{origin}", f"kb:section:{description}"]},
        tool_calls=["search_products"],
    )
    scored = score_knowledge_turn(_v3().case("K16"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert "knowledge_refs_delivered_count_mismatch:2" in scored["blockers"]


def test_k16_retrieved_but_unused_section_fails_even_with_a_disclosure(db: Any) -> None:
    tenant_id = _tenant_id(db)
    origin = _section_id(db, tenant_id, "product_linked_origin")
    artifact = _artifact(
        tenant_id=tenant_id,
        knowledge_gap_disclosure=1,
        knowledge_lookups=[_lookup("product", [origin], tenant_id=tenant_id)],
        knowledge_evidence_refs=[f"kb:section:{origin}"],
        structured_reply={"evidence_refs": ["catalog:product:1"], "fact_claims": []},
        tool_calls=["search_products"],
    )
    scored = score_knowledge_turn(_v3().case("K16"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert scored["fallback_kind"] == FALLBACK_KIND_MODEL_DISCLOSURE
    assert {
        "knowledge_claim_missing_evidence",
        "required_section_retrieved_but_unused",
        "required_citation_replaced_by_fallback",
    } <= set(scored["blockers"])


def test_v3_duplicate_rule_targets_delivered_evidence_not_ledger_attempts(db: Any) -> None:
    """A retry repeats a lookup; the ledger keeps both; delivery stays unique."""
    tenant_id = _tenant_id(db)
    origin = _section_id(db, tenant_id, "product_linked_origin")
    ref = f"kb:section:{origin}"
    base = dict(
        tenant_id=tenant_id,
        tool_trace=[_retry_event(K16_CODE, GROUNDING_RETRY_KNOWLEDGE_SPAN)],
        knowledge_lookups=[
            _lookup("product", [origin], tenant_id=tenant_id),
            {**_lookup("product", [origin], tenant_id=tenant_id), "purpose": "model_product_knowledge"},
        ],
        knowledge_evidence_refs=[ref],
        knowledge_evidence_used_in_reply=[ref],
        tool_calls=["search_products"],
    )
    once = _artifact(**base, structured_reply={
        "evidence_refs": [ref],
        "fact_claims": [{"kind": "product_knowledge", "evidence_ref": ref, "subject_product_id": 1}],
    })
    scored = score_knowledge_turn(_v3().case("K16"), once, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert "duplicate_knowledge_evidence" not in scored["blockers"]
    assert scored["passed"] is True

    twice = _artifact(**base, structured_reply={
        "evidence_refs": [ref],
        "fact_claims": [
            {"kind": "product_knowledge", "evidence_ref": ref, "subject_product_id": 1},
            {"kind": "product_knowledge", "evidence_ref": ref, "subject_product_id": 1},
        ],
    })
    scored = score_knowledge_turn(_v3().case("K16"), twice, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert "duplicate_knowledge_evidence" in scored["blockers"]


# ── v3 scorer: K15 ──────────────────────────────────────────────────────────


def _k15_fallback(fallback_type: str, *, tenant_id: int, stale_id: int) -> dict[str, Any]:
    fallback = safe_fallback_reply(f"output_guardrail_tripwire:{K15_CODE}")
    return _artifact(
        tenant_id=tenant_id,
        status="failed",
        failure_reason=f"output_guardrail_tripwire:{K15_CODE}",
        fallback_type=fallback_type,
        guardrail_result=_tripped(K15_CODE),
        tool_trace=[_retry_event(K15_CODE, GROUNDING_RETRY_FACTUAL)],
        knowledge_lookups=[
            _lookup("turn", [], tenant_id=tenant_id, status="no_results"),
            _lookup("product", [stale_id], tenant_id=tenant_id),
        ],
        knowledge_evidence_refs=[f"kb:section:{stale_id}"],
        structured_reply=fallback.model_dump(mode="json"),
        tool_calls=["search_products", "search_product_knowledge", "search_products", "search_product_knowledge"],
    )


def test_k15_expected_safe_fallback_passes_and_the_report_keeps_the_failure(db: Any) -> None:
    tenant_id = _tenant_id(db)
    stale = _section_id(db, tenant_id, "product_linked_stale_availability")
    artifact = _k15_fallback("expected_safe_fallback", tenant_id=tenant_id, stale_id=stale)
    scored = score_knowledge_turn(_v3().case("K15"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert scored["blockers"] == []
    assert scored["fallback_kind"] == FALLBACK_KIND_EXPECTED
    assert scored["guardrail_passed"] is False
    assert scored["guardrail_error_codes"] == [K15_CODE]
    assert scored["original_guardrail_errors"] == [K15_CODE]
    assert scored["grounding_retries"] == 1
    assert scored["status"] == "failed"


def test_k15_unexpected_runtime_fallback_passes_v2_and_fails_v3(db: Any) -> None:
    tenant_id = _tenant_id(db)
    stale = _section_id(db, tenant_id, "product_linked_stale_availability")
    artifact = _k15_fallback("unexpected_runtime_fallback", tenant_id=tenant_id, stale_id=stale)

    v2 = score_knowledge_turn(_v2().case("K15"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V2)
    assert v2["passed"] is True, "this is the gap: Run 2 read the fallback as a disclosure"

    v3 = score_knowledge_turn(_v3().case("K15"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert v3["passed"] is False
    assert f"unexpected_runtime_fallback:output_guardrail_tripwire:{K15_CODE}" in v3["blockers"]
    assert "absent_knowledge_not_disclosed" in v3["blockers"]
    assert v3["fallback_kind"] == FALLBACK_KIND_UNEXPECTED


def test_k15_fallback_that_the_channel_marks_expected_but_the_case_does_not_fails(db: Any) -> None:
    """The scorer checks the case's own expectation, not just the channel's label."""
    tenant_id = _tenant_id(db)
    origin = _section_id(db, tenant_id, "product_linked_origin")
    artifact = _artifact(
        tenant_id=tenant_id,
        status="failed",
        failure_reason=f"output_guardrail_tripwire:{K16_CODE}",
        fallback_type="expected_safe_fallback",
        guardrail_result=_tripped(K16_CODE),
        knowledge_lookups=[_lookup("product", [origin], tenant_id=tenant_id)],
        structured_reply=safe_fallback_reply("x").model_dump(mode="json"),
        tool_calls=["search_products"],
    )
    scored = score_knowledge_turn(_v3().case("K03"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert any(b.startswith("fallback_not_expected_by_case:") for b in scored["blockers"])
    assert "required_citation_replaced_by_fallback" in scored["blockers"]


def test_k15_model_authored_disclosure_passes(db: Any) -> None:
    tenant_id = _tenant_id(db)
    stale = _section_id(db, tenant_id, "product_linked_stale_availability")
    artifact = _artifact(
        tenant_id=tenant_id,
        knowledge_gap_disclosure=1,
        knowledge_lookups=[_lookup("product", [stale], tenant_id=tenant_id)],
        knowledge_evidence_refs=[f"kb:section:{stale}"],
        structured_reply={"evidence_refs": ["catalog:product:3"], "fact_claims": []},
        tool_calls=["search_products"],
    )
    scored = score_knowledge_turn(_v3().case("K15"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert scored["blockers"] == []
    assert scored["fallback_kind"] == FALLBACK_KIND_MODEL_DISCLOSURE


def test_k15_cannot_pass_through_an_unrelated_availability_claim(db: Any) -> None:
    tenant_id = _tenant_id(db)
    stale = _section_id(db, tenant_id, "product_linked_stale_availability")
    artifact = _artifact(
        tenant_id=tenant_id,
        knowledge_lookups=[_lookup("product", [stale], tenant_id=tenant_id)],
        structured_reply={
            "evidence_refs": ["catalog:product:3"],
            "fact_claims": [{"kind": "availability", "value": False, "evidence_ref": "catalog:product:3", "subject_product_id": 3}],
        },
        tool_calls=["search_products"],
    )
    scored = score_knowledge_turn(_v3().case("K15"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert "absent_knowledge_not_disclosed" in scored["blockers"]
    assert scored["fallback_kind"] == FALLBACK_KIND_NONE


def test_k15_citing_the_deleted_section_fails(db: Any) -> None:
    tenant_id = _tenant_id(db)
    deleted = _section_id(db, tenant_id, "deleted_section")
    artifact = _artifact(
        tenant_id=tenant_id,
        knowledge_gap_disclosure=1,
        knowledge_lookups=[_lookup("product", [deleted], tenant_id=tenant_id)],
        knowledge_evidence_used_in_reply=[f"kb:section:{deleted}"],
        structured_reply={"evidence_refs": [f"kb:section:{deleted}"]},
        tool_calls=["search_products"],
    )
    scored = score_knowledge_turn(_v3().case("K15"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert "knowledge_cited_when_forbidden" in scored["blockers"]
    assert f"forbidden_section_touched:{deleted}" in scored["blockers"]


# ── v3 scorer: the exemption is gone for a required citation ────────────────


def test_required_citation_cannot_pass_through_a_fallback(db: Any) -> None:
    tenant_id = _tenant_id(db)
    origin = _section_id(db, tenant_id, "product_linked_origin")
    fallback = safe_fallback_reply("insufficient_grounded_evidence")
    artifact = _artifact(
        tenant_id=tenant_id,
        status="failed",
        failure_reason="run_deadline_exceeded",
        fallback_type="unexpected_runtime_fallback",
        knowledge_lookups=[_lookup("product", [origin], tenant_id=tenant_id)],
        structured_reply=fallback.model_dump(mode="json"),
        tool_calls=["search_products"],
    )
    v2 = score_knowledge_turn(_v2().case("K03"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V2)
    assert "knowledge_claim_missing_evidence" not in v2["blockers"]
    v3 = score_knowledge_turn(_v3().case("K03"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert "knowledge_claim_missing_evidence" in v3["blockers"]
    assert "turn_not_completed:run_deadline_exceeded" in v3["blockers"]
    assert "unexpected_runtime_fallback:run_deadline_exceeded" in v3["blockers"]


def test_safe_fallback_reason_alone_never_overrides_a_required_citation(db: Any) -> None:
    tenant_id = _tenant_id(db)
    artifact = _artifact(
        tenant_id=tenant_id,
        knowledge_lookups=[_lookup("product", [], tenant_id=tenant_id, status="no_results")],
        structured_reply={"safe_fallback_reason": "insufficient_grounded_evidence", "fact_claims": [], "evidence_refs": []},
        tool_calls=["search_products"],
    )
    scored = score_knowledge_turn(_v3().case("K03"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert "knowledge_claim_missing_evidence" in scored["blockers"]


def test_a_cited_answer_that_also_discloses_a_gap_is_not_a_fallback(db: Any) -> None:
    """K05 answers price and origin and may still say a sub-detail is undocumented."""
    tenant_id = _tenant_id(db)
    origin = _section_id(db, tenant_id, "product_linked_origin")
    ref = f"kb:section:{origin}"
    artifact = _artifact(
        tenant_id=tenant_id,
        knowledge_gap_disclosure=1,
        knowledge_lookups=[_lookup("product", [origin], tenant_id=tenant_id)],
        knowledge_evidence_refs=[ref],
        knowledge_evidence_used_in_reply=[ref],
        structured_reply={
            "evidence_refs": ["catalog:product:1", ref],
            "fact_claims": [
                {"kind": "price", "evidence_ref": "catalog:product:1", "subject_product_id": 1},
                {"kind": "product_knowledge", "evidence_ref": ref, "subject_product_id": 1},
            ],
        },
        tool_calls=["search_products"],
    )
    scored = score_knowledge_turn(_v3().case("K05"), artifact, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)
    assert scored["blockers"] == []
    assert scored["fallback_kind"] == FALLBACK_KIND_MODEL_DISCLOSURE


# ── v3 keeps v2's tenant rule; v1 and v2 stay frozen ────────────────────────


def test_v3_cross_tenant_rule_resolves_owners_from_the_database(db: Any) -> None:
    tenant_id = _tenant_id(db)
    own = _section_id(db, tenant_id, "store_policy_relevant")
    foreign = _section_id(db, tenant_id, "foreign_tenant_section", foreign=True)
    case = _v3().case("K10")
    fine = _artifact(
        tenant_id=tenant_id,
        knowledge_lookups=[_lookup("turn", [own], tenant_id=tenant_id)],
        knowledge_evidence_used_in_reply=[f"kb:section:{own}"],
        structured_reply={"evidence_refs": [f"kb:section:{own}"]},
    )
    assert score_knowledge_turn(case, fine, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)["blockers"] == []
    leak = _artifact(
        tenant_id=tenant_id,
        knowledge_lookups=[_lookup("turn", [own, foreign], tenant_id=tenant_id)],
        structured_reply={"evidence_refs": [f"kb:section:{own}"]},
    )
    blockers = score_knowledge_turn(case, leak, db=db, contract_version=KNOWLEDGE_CONTRACT_VERSION_V3)["blockers"]
    assert "cross_tenant_knowledge_exposed" in blockers
    assert f"forbidden_section_touched:{foreign}" in blockers


def test_v1_and_v2_artifacts_are_byte_identical_and_v3_is_separate() -> None:
    v1 = load_knowledge_acceptance_matrix(contract_version=KNOWLEDGE_CONTRACT_VERSION_V1)
    v2 = _v2()
    v3 = _v3()
    assert v1.matrix_sha256 == "46f04522b947fc1aaf29090a51a67139290955264abe72a9018d338c7448fcc3"
    assert v2.matrix_sha256 == "76ac2c45c1ff5f2c201798eb5e3eb24ee16d0305c5bc29dc1e3d1307c3c35da9"
    assert v3.contract_version == KNOWLEDGE_CONTRACT_VERSION_V3
    assert v3.matrix_sha256 not in {v1.matrix_sha256, v2.matrix_sha256}
    assert KNOWLEDGE_CONTRACT_VERSIONS == (
        KNOWLEDGE_CONTRACT_VERSION_V1, KNOWLEDGE_CONTRACT_VERSION_V2, KNOWLEDGE_CONTRACT_VERSION_V3
    )
    assert len(v3.cases) == 16
    assert v3.case("K15").expected_outcome == "safe_missing_fact"
    assert v3.case("K16").expected["knowledge_refs_delivered_exactly"] == 1
    assert v3.case("K16").expected["required_section_must_be_cited"] is True
    assert v2.case("K16").expected.get("knowledge_refs_delivered_exactly") is None


def test_v2_scoring_output_shape_is_unchanged(db: Any) -> None:
    """v3 fields never leak into a v2 score, so Run 2's digest stays reproducible."""
    tenant_id = _tenant_id(db)
    scored = score_knowledge_turn(
        _v2().case("K01"),
        _artifact(tenant_id=tenant_id, knowledge_lookups=[_lookup("turn", [], tenant_id=tenant_id)], tool_calls=["search_products"]),
        db=db,
        contract_version=KNOWLEDGE_CONTRACT_VERSION_V2,
    )
    assert set(scored) == {
        "case_id", "passed", "blockers", "knowledge_lookup_attempted", "knowledge_scopes",
        "knowledge_statuses", "knowledge_evidence_cited", "knowledge_conflicts", "review",
    }


def test_v3_bindings_resolve_against_the_database(db: Any) -> None:
    tenant_id = _tenant_id(db)
    bound = verify_case_bindings(db, tenant_id, _v3())
    assert bound["contract_version"] == KNOWLEDGE_CONTRACT_VERSION_V3
    assert len(bound["cases"]) == 16
    assert bound["cases"]["K16"]["section_id"] == _section_id(db, tenant_id, "product_linked_origin")
    assert bound["cases"]["K15"]["forbidden_section_ids"] == [_section_id(db, tenant_id, "deleted_section")]


# ── full v3 lifecycle on PostgreSQL ─────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("A1_PG_TEST_DATABASE_URL"),
    reason="PostgreSQL lifecycle requires A1_PG_TEST_DATABASE_URL",
)
def test_v3_lifecycle_provision_to_cleanup_on_postgresql() -> None:
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

    engine = create_engine(os.environ["A1_PG_TEST_DATABASE_URL"])
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        provision_knowledge_acceptance_environment(session, env=ISOLATED_ENV)
        environment = describe_knowledge_acceptance_environment(session, env=ISOLATED_ENV)
        tenant_id = int(environment["tenant_id"])
        conversation_id = int(sorted(environment["conversations"].values())[0])
        verify_acceptance_fixtures(session, tenant_id)
        matrix = _v3()
        assert len(verify_case_bindings(session, tenant_id, matrix)["cases"]) == 16
        origin = _section_id(session, tenant_id, "product_linked_origin")

        meta = create_knowledge_acceptance_run(
            session, commit="4a270df82bcf11cdd88b1ef6d9946dc2dc46ca3e",
            conversation_id=conversation_id, tenant_id=tenant_id,
            contract_version=matrix.contract_version, matrix=matrix,
        )
        assert meta["contract_version"] == KNOWLEDGE_CONTRACT_VERSION_V3

        async def submit(db_session: Any, case: Any) -> dict[str, Any]:
            if case.reset_thread_before:
                reset_acceptance_thread(db_session, tenant_id=tenant_id, conversation_id=conversation_id, env=ISOLATED_ENV)
            if case.case_id == "K16":
                return _k16_persisted(origin, origin + 1, tenant_id=tenant_id)
            return _artifact(
                tenant_id=tenant_id,
                knowledge_lookups=[_lookup(scope, [], tenant_id=tenant_id, status="no_results") for scope in (case.expected.get("required_scopes") or ["turn"])],
                knowledge_gap_disclosure=1,
                structured_reply={"evidence_refs": ["catalog:product:1"], "fact_claims": []},
                tool_calls=list(case.expected.get("expected_tools") or []),
            )

        finished = asyncio.run(execute_knowledge_acceptance_run(
            session, meta["run_id"], submit_case=submit, tenant_id=tenant_id, matrix=matrix,
        ))
        assert finished["contract_version"] == KNOWLEDGE_CONTRACT_VERSION_V3
        rows = {row["case_id"]: row for row in finished["report"]["cases"]}
        assert rows["K16"]["passed"] is False
        assert rows["K16"]["fallback_kind"] == FALLBACK_KIND_UNEXPECTED
        assert rows["K16"]["guardrail_error_codes"] == [K16_CODE]
        assert {"status", "failure_reason", "fallback_type", "grounding_retries"} <= set(rows["K16"])
        assert "K16" in finished["cases_machine_failed"]

        digest = knowledge_run_status(session, meta["run_id"])["machine_digest"]
        record_knowledge_review(
            session, meta["run_id"], case_id="K01", assertion_index=0,
            verdict="approved", reviewer="test", evidence="v3 lifecycle",
        )
        assert knowledge_run_status(session, meta["run_id"])["machine_digest"] == digest
        assert int(cleanup_knowledge_acceptance_environment(session, env=ISOLATED_ENV)["remaining_rows"]) == 0
    finally:
        session.close()
        engine.dispose()
