"""Pure contracts of the dormant agent loop (no database, no provider, no network).

Validation of the provider boundary, the tool registry's allowlist and scope
refusal, the deterministic reply verification, and the persisted loop
progress. Runs in the ordinary root suite.
"""
from __future__ import annotations

import pytest

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_scripted as sp
from core.commerce_runtime import agent_tools as at
from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc
from tests.commerce_reliability.agent_fixture_catalog import build_catalog, build_registry

SCOPE = at.ToolScope(tenant_id=1, namespace="live", conversation_id=10, turn_id=100)


def _observation(call_id: str = "c1", *, ok: bool = True, refs=("product:blue_cotton_shirt",)) -> ac.ToolObservation:
    return ac.ToolObservation(call_id=call_id, tool_name="catalog_search", ok=ok,
                              result={"products": []} if ok else None,
                              error_code=None if ok else "tool_failure", error=None,
                              evidence_refs=tuple(refs) if ok else ())


# ── Provider boundary ────────────────────────────────────────────────────────


def test_tool_call_ids_are_correlation_only_and_never_business_keys() -> None:
    request = ac.validate_tool_request(ac.ToolRequest("toolu_01ABC", "catalog_search", {"query": "x"}))
    assert request.call_id == "toolu_01ABC"
    # A call id is not an idempotency key: the ledger's key format rejects it outright.
    with pytest.raises(c.ValidationError):
        lc.validate_idempotency_key(request.call_id + " ")
    # And it carries no scope or authority of its own.
    assert not hasattr(request, "tenant_id") and not hasattr(request, "token")


def test_malformed_provider_output_is_refused_before_anything_runs() -> None:
    with pytest.raises(c.ValidationError):
        ac.validate_tool_request(ac.ToolRequest("bad id!", "catalog_search", {}))
    with pytest.raises(c.ValidationError):
        ac.validate_tool_request(ac.ToolRequest("c1", "Catalog-Search", {}))
    with pytest.raises(c.ValidationError):
        ac.validate_tool_request(ac.ToolRequest("c1", "catalog_search", {"q": "x" * 20_000}))
    with pytest.raises(c.ValidationError):
        ac.validate_reply_draft(ac.ReplyDraft(text=b"bytes"))          # type: ignore[arg-type]
    with pytest.raises(c.ValidationError):
        ac.validate_reply_draft(ac.ReplyDraft(text="hi", kind="carousel"))
    with pytest.raises(c.ValidationError):
        ac.validate_reply_draft(ac.ReplyDraft(text="hi", evidence_refs=("not a ref",)))


def test_reply_kinds_are_the_ledger_delivery_kinds() -> None:
    for kind in lc.DeliveryKind:
        assert ac.validate_reply_draft(ac.ReplyDraft(text="hi", kind=kind.value)).kind == kind.value


def test_budget_validation_is_fail_closed() -> None:
    assert ac.validate_budget(ac.LoopBudget()).max_steps >= 1
    for bad in (ac.LoopBudget(max_steps=0), ac.LoopBudget(max_tool_calls=-1),
                ac.LoopBudget(tool_timeout_seconds=0), ac.LoopBudget(deadline_seconds=-1)):
        with pytest.raises(c.ValidationError):
            ac.validate_budget(bad)
    with pytest.raises(c.ValidationError):
        ac.validate_budget({"max_steps": 2})                            # type: ignore[arg-type]


# ── Verification ─────────────────────────────────────────────────────────────


def test_verification_accepts_only_evidence_observed_in_this_turn() -> None:
    observations = [_observation()]
    assert ac.verify_reply_draft(
        ac.ReplyDraft(text="متوفر", evidence_refs=("product:blue_cotton_shirt",), claims_commerce_facts=True),
        observations) == ()
    problems = ac.verify_reply_draft(
        ac.ReplyDraft(text="متوفر", evidence_refs=("product:invented",), claims_commerce_facts=True), observations)
    assert [p.code for p in problems] == ["unknown_evidence"]
    # Evidence from a failed observation is not evidence.
    assert [p.code for p in ac.verify_reply_draft(
        ac.ReplyDraft(text="متوفر", evidence_refs=("product:blue_cotton_shirt",)),
        [_observation(ok=False)])] == ["unknown_evidence"]


def test_a_commerce_claim_needs_evidence_and_text_is_bounded() -> None:
    assert [p.code for p in ac.verify_reply_draft(
        ac.ReplyDraft(text="سعره ١٠", claims_commerce_facts=True), [])] == ["missing_evidence"]
    assert [p.code for p in ac.verify_reply_draft(ac.ReplyDraft(text="   "), [])] == ["empty_text"]
    assert [p.code for p in ac.verify_reply_draft(
        ac.ReplyDraft(text="x" * (ac.MAX_REPLY_TEXT_LENGTH + 1)), [])] == ["text_too_long"]
    # A conversational reply without commerce facts needs no evidence at all.
    assert ac.verify_reply_draft(ac.ReplyDraft(text="أهلاً"), []) == ()


def test_verification_proves_reference_existence_not_semantic_grounding() -> None:
    """Documented limitation: a valid reference does not make every sentence true."""
    observations = [_observation()]
    wrong_but_referenced = ac.ReplyDraft(text="السعر ٥ ريال فقط ومجاني الشحن للقمر.",
                                         evidence_refs=("product:blue_cotton_shirt",), claims_commerce_facts=True)
    assert ac.verify_reply_draft(wrong_but_referenced, observations) == ()


# ── Tool registry ────────────────────────────────────────────────────────────


def test_only_read_only_tools_can_be_registered() -> None:
    mutating = at.RegisteredTool(
        ac.ToolDefinition(name="place_order", description="mutates", input_schema={"type": "object"},
                          result_kind="product", read_only=False),
        lambda scope, arguments: at.ToolResult(result={}, evidence_refs=()))
    with pytest.raises(c.ValidationError):
        at.ToolRegistry([mutating])
    with pytest.raises(c.ValidationError):
        at.ToolRegistry(list(at.build_fixture_tools(build_catalog())) * 2)


def test_every_exposed_tool_declares_a_schema_description_and_result_kind() -> None:
    definitions = build_registry().definitions
    assert {d.name for d in definitions} == {"catalog_search", "product_lookup", "merchant_knowledge_lookup"}
    for definition in definitions:
        assert definition.read_only is True
        assert len(definition.description) > 40 and "Read-only" in definition.description
        assert definition.input_schema["type"] == "object"
        assert definition.input_schema["additionalProperties"] is False
        assert definition.input_schema["required"]
        assert definition.result_kind in {"product_list", "product", "knowledge_entry"}


def test_scope_arguments_are_refused_and_never_reach_a_tool() -> None:
    registry = build_registry()
    for forged in ("tenant_id", "namespace", "conversation_id", "turn_id", "token", "owner_id"):
        observation = registry.execute(SCOPE, ac.ToolRequest("c1", "catalog_search", {"query": "x", forged: 9}),
                                       timeout_seconds=1.0)
        assert observation.ok is False
        assert observation.error_code == ac.ToolErrorCode.SCOPE_OVERRIDE_REFUSED.value
        assert forged in observation.error


def test_tool_results_are_data_with_checkable_evidence_references() -> None:
    registry = build_registry()
    observation = registry.execute(SCOPE, ac.ToolRequest("c1", "catalog_search", {"query": ""}), timeout_seconds=1.0)
    assert observation.ok and observation.result["total_matches"] == 3
    assert observation.evidence_refs == ("product:blue_cotton_shirt", "product:white_sneaker",
                                         "product:rose_perfume_100")
    assert set(ac.evidence_index([observation])) == set(observation.evidence_refs)
    # Another tenant's scope sees its own catalogue only.
    other = registry.execute(at.ToolScope(2, "live", 1, 1), ac.ToolRequest("c2", "catalog_search", {"query": ""}),
                             timeout_seconds=1.0)
    assert other.evidence_refs == ("product:leather_belt",)


def test_a_failing_tool_becomes_an_observation_not_an_exception() -> None:
    def broken(scope: at.ToolScope, arguments) -> at.ToolResult:
        raise ZeroDivisionError("bad fixture")

    registry = at.ToolRegistry([at.RegisteredTool(
        ac.ToolDefinition(name="broken_tool", description="A tool that raises. Read-only.",
                          input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                          result_kind="product"), broken)])
    observation = registry.execute(SCOPE, ac.ToolRequest("c1", "broken_tool", {}), timeout_seconds=1.0)
    assert (observation.ok, observation.error_code) == (False, ac.ToolErrorCode.TOOL_FAILURE.value)
    assert observation.error == "ZeroDivisionError"


# ── Persisted progress ───────────────────────────────────────────────────────


def test_loop_progress_round_trips_and_ignores_another_turn_or_version() -> None:
    budget = ac.LoopBudget(max_steps=3, max_tool_calls=4)
    progress = ac.LoopProgress(turn_id=7, phase=ac.LoopPhase.REASONING.value, steps_used=2, tool_calls_used=1,
                               elapsed_seconds=1.25)
    payload = progress.to_payload(budget)
    assert payload["budget"] == {"max_steps": 3, "max_tool_calls": 4, "deadline_seconds": budget.deadline_seconds}
    restored = ac.LoopProgress.from_payload(payload, turn_id=7)
    assert restored is not None and (restored.steps_used, restored.tool_calls_used) == (2, 1)
    assert ac.LoopProgress.from_payload(payload, turn_id=8) is None
    assert ac.LoopProgress.from_payload({**payload, "version": 99}, turn_id=7) is None
    assert ac.LoopProgress.from_payload({"version": ac.AGENT_LOOP_STATE_VERSION, "turn_id": 7}, turn_id=7) is None
    assert ac.LoopProgress.from_payload(None, turn_id=7) is None


def test_progress_lives_in_the_versioned_state_payload_and_fits_its_bound() -> None:
    payload = {ac.AGENT_LOOP_STATE_KEY: ac.LoopProgress(
        turn_id=1, phase=ac.LoopPhase.STOPPED.value, steps_used=4, tool_calls_used=6, elapsed_seconds=12.5,
        stop_reason=ac.StopReason.BUDGET_EXHAUSTED.value).to_payload(ac.LoopBudget())}
    assert c.validate_payload(payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)


# ── Scripted provider ────────────────────────────────────────────────────────


def test_every_declared_stop_reason_is_one_the_loop_can_actually_raise() -> None:
    """The stop vocabulary is closed *and* exhaustive: no member is decorative."""
    import pathlib  # noqa: PLC0415

    source = pathlib.Path("backend/core/commerce_runtime/agent_loop.py").read_text(encoding="utf-8")
    unraisable = [reason.name for reason in ac.StopReason if f"StopReason.{reason.name}.value" not in source]
    assert unraisable == []


def test_the_scripted_provider_is_deterministic_and_fails_explicitly_when_exhausted() -> None:
    provider = sp.ScriptedReasoningProvider([sp.reply("مرحبًا")])
    request = ac.ProviderRequest(step_no=1, context=ac.AuthorizedContext(1, "live", 1, 1, {}, {}), tools=(),
                                 observations=(), feedback=(), budget=ac.BudgetView(1, 1, 1.0))
    first = provider.step(request)
    assert isinstance(first, ac.ProviderReply) and first.draft.text == "مرحبًا"
    assert isinstance(provider.step(request), ac.ProviderFailure)
    assert len(provider.requests) == 2
