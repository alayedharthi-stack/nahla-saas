"""Pure contracts of the dormant agent loop (no database, no provider, no network).

Validation of the provider boundary, the tool registry's allowlist and scope
refusal, the deterministic reply verification, and the persisted loop
progress. Runs in the ordinary root suite.
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_scripted as sp
from core.commerce_runtime import agent_tools as at
from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc
from tests.commerce_reliability.agent_fixture_catalog import build_catalog, build_registry

SCOPE = at.ToolScope(tenant_id=1, namespace="live", conversation_id=10, turn_id=100)
NOW = _dt.datetime(2026, 9, 19, 12, 0, tzinfo=_dt.timezone.utc)


def _progress(*, steps_used: int = 0, tool_calls_used: int = 0,
              limits: ac.LoopBudget = ac.LoopBudget()) -> ac.LoopProgress:
    return ac.LoopProgress(turn_id=7, phase=ac.LoopPhase.REASONING.value, limits=limits, deadline_at=NOW,
                           steps_used=steps_used, tool_calls_used=tool_calls_used)


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
    progress = _progress(steps_used=2, tool_calls_used=1)
    payload = progress.to_payload()
    assert payload["limits"] == progress.limits.to_payload()
    restored = ac.LoopProgress.from_payload(payload, turn_id=7)
    assert restored == progress
    assert ac.LoopProgress.from_payload(payload, turn_id=8) is None
    assert ac.LoopProgress.from_payload({**payload, "version": 99}, turn_id=7) is None
    assert ac.LoopProgress.from_payload({"version": ac.AGENT_LOOP_STATE_VERSION, "turn_id": 7}, turn_id=7) is None
    assert ac.LoopProgress.from_payload({**payload, "limits": {"max_steps": 1}}, turn_id=7) is None
    assert ac.LoopProgress.from_payload({**payload, "deadline_at": "not-a-time"}, turn_id=7) is None
    assert ac.LoopProgress.from_payload(None, turn_id=7) is None


def test_progress_carries_the_authoritative_limits_and_deadline() -> None:
    """A restored progress is the authority: limits and deadline come from it."""
    limits = ac.LoopBudget(max_steps=2, max_tool_calls=3, deadline_seconds=45)
    progress = _progress(limits=limits)
    restored = ac.LoopProgress.from_payload(progress.to_payload(), turn_id=7)
    assert restored is not None
    assert restored.limits == limits and restored.deadline_at == progress.deadline_at
    assert restored.limits != ac.LoopBudget(), "a different caller budget cannot be mistaken for the stored one"


def test_same_debits_arbitrates_between_invocations() -> None:
    mine = _progress(steps_used=2, tool_calls_used=1)
    assert mine.same_debits(_progress(steps_used=2, tool_calls_used=1))
    assert not mine.same_debits(_progress(steps_used=3, tool_calls_used=1))
    assert not mine.same_debits(_progress(steps_used=2, tool_calls_used=2))
    assert not mine.same_debits(None)


def test_observation_checkpoints_keep_evidence_and_drop_bodies_under_the_bound() -> None:
    small = ac.ToolObservation("c1", "catalog_search", True, {"products": [{"ref": "product:x"}]}, None, None,
                               ("product:x",))
    big = ac.ToolObservation("c2", "catalog_search", True, {"blob": "y" * 20_000}, None, None, ("product:y",))
    kept = ac.checkpoint_observations([small])
    assert kept[0].body == {"products": [{"ref": "product:x"}]} and kept[0].evidence_refs == ("product:x",)
    assert kept[0].restore().result == small.result and kept[0].restore().restored is True

    bounded = ac.checkpoint_observations([small, big])
    assert [o.evidence_refs for o in bounded] == [("product:x",), ("product:y",)], "references always survive"
    assert any(o.body is None for o in bounded), "an over-large body is dropped, not stored"
    truncated = next(o for o in bounded if o.body is None).restore()
    assert truncated.body_truncated is True and truncated.result is None
    payload_bytes = len(json.dumps([o.to_payload() for o in bounded], ensure_ascii=False).encode("utf-8"))
    assert payload_bytes <= ac.MAX_CHECKPOINT_BYTES


def test_a_full_progress_payload_fits_the_state_payload_bound() -> None:
    observations = [ac.ToolObservation(f"c{i}", "catalog_search", True, {"products": [{"ref": f"product:{i}"}]},
                                       None, None, (f"product:{i}",))
                    for i in range(ac.MAX_CHECKPOINT_OBSERVATIONS + 6)]
    progress = ac.LoopProgress(
        turn_id=1, phase=ac.LoopPhase.REASONING.value, limits=ac.LoopBudget(), deadline_at=NOW,
        steps_used=4, tool_calls_used=6, observations=ac.checkpoint_observations(observations),
        feedback=((1, ("unknown_evidence",)),), executed=tuple(f"catalog_search:{i}" for i in range(8)),
    )
    assert len(progress.observations) == ac.MAX_CHECKPOINT_OBSERVATIONS
    payload = {ac.AGENT_LOOP_STATE_KEY: progress.to_payload()}
    assert c.validate_payload(payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)


# ── Complete boundary validation ─────────────────────────────────────────────


def test_a_provider_result_is_validated_whole_before_anything_in_it_runs() -> None:
    caps = ac.ProviderCapabilities(provider_name="t", parallel_tool_use=True, max_tool_requests_per_step=3)
    ok = ac.validate_provider_result(
        ac.ProviderToolRequests((ac.ToolRequest("c1", "catalog_search", {"query": "x"}),)), caps)
    assert isinstance(ok, ac.ProviderToolRequests) and len(ok.requests) == 1
    for bad in (ac.ProviderToolRequests(None), ac.ProviderToolRequests(()),                # type: ignore[arg-type]
                ac.ProviderToolRequests({"a": 1}), ac.ProviderToolRequests("c1"),          # type: ignore[arg-type]
                ac.ProviderToolRequests((ac.ToolRequest("c1", "catalog_search", {"q": {1}}),)),
                ac.ProviderToolRequests((ac.ToolRequest("c1", "catalog_search", {}),
                                         ac.ToolRequest("c1", "catalog_search", {"query": "y"}))),
                ac.ProviderReply(ac.ReplyDraft(text="hi", evidence_refs=None)),            # type: ignore[arg-type]
                ac.ProviderReply(ac.ReplyDraft(text="hi", evidence_refs="product:x")),     # type: ignore[arg-type]
                ac.ProviderReply(ac.ReplyDraft(text="hi", payload=None)),                  # type: ignore[arg-type]
                ac.ProviderFailure(""), ac.ProviderBlocked("  "), ac.ProviderInvalid(None),  # type: ignore[arg-type]
                "not a result", None, 42):
        with pytest.raises(c.ValidationError):
            ac.validate_provider_result(bad, caps)


def test_capability_limits_are_enforced_at_the_boundary() -> None:
    serial = ac.ProviderCapabilities(provider_name="t", parallel_tool_use=False)
    two = ac.ProviderToolRequests((ac.ToolRequest("c1", "catalog_search", {"query": "x"}),
                                   ac.ToolRequest("c2", "catalog_search", {"query": "y"})))
    with pytest.raises(ac.UnsupportedCapability) as parallel:
        ac.validate_provider_result(two, serial)
    assert (parallel.value.capability, parallel.value.requested, parallel.value.allowed) == (
        "parallel_tool_use", 2, 1)
    with pytest.raises(ac.UnsupportedCapability) as none_at_all:
        ac.validate_provider_result(two, ac.ProviderCapabilities(provider_name="t", tool_use=False))
    assert none_at_all.value.capability == "tool_use"
    for bad in (None, {"provider_name": "t"}, ac.ProviderCapabilities(provider_name=""),
                ac.ProviderCapabilities(provider_name="t", max_tool_requests_per_step=0)):
        with pytest.raises(c.ValidationError):
            ac.validate_capabilities(bad)


def test_a_non_serializable_argument_is_a_declared_failure_not_a_type_error() -> None:
    class Opaque:
        pass

    with pytest.raises(c.ValidationError) as exc:
        ac.validate_tool_request(ac.ToolRequest("c1", "catalog_search", {"query": Opaque()}))
    assert "not serializable" in str(exc.value)
    with pytest.raises(c.ValidationError):
        ac.validate_tool_request(ac.ToolRequest("c1", "catalog_search", {1: "numeric key"}))


def test_validated_arguments_are_detached_from_the_providers_mapping() -> None:
    arguments = {"query": "قميص", "nested": {"a": [1, 2]}}
    validated = ac.validate_tool_request(ac.ToolRequest("c1", "catalog_search", arguments))
    arguments["nested"]["a"].append(3)
    assert validated.arguments["nested"]["a"] == [1, 2]


# ── Scripted provider ────────────────────────────────────────────────────────


# Every stop reason is tied to the PostgreSQL regression that drives the loop
# into it and asserts the resulting outcome. This is coverage of behaviour, not
# a search for the constant in the source.
STOP_REASON_COVERAGE = {
    ac.StopReason.TURN_NOT_IN_SCOPE: "test_a_turn_of_another_conversation_is_refused_before_any_work_is_read",
    ac.StopReason.TURN_NOT_ELIGIBLE: "test_a_turn_that_is_not_the_oldest_unresolved_one_never_reaches_the_provider",
    ac.StopReason.TURN_COMPLETED: "test_a_completed_turn_is_never_reasoned_about_again",
    ac.StopReason.OWNERSHIP_LOST: "test_ownership_lost_while_awaiting_a_result_keeps_the_debit_and_writes_nothing_more",
    ac.StopReason.CONCURRENT_INVOCATION: "test_two_processes_on_the_same_turn_cannot_share_one_debit",
    ac.StopReason.BUDGET_EXHAUSTED: "test_budget_exhaustion_and_cancellation_stop_without_false_success",
    ac.StopReason.DEADLINE_EXCEEDED: "test_the_deadline_is_rechecked_inside_the_reservation_transaction",
    ac.StopReason.CANCELLED: "test_budget_exhaustion_and_cancellation_stop_without_false_success",
    ac.StopReason.PROVIDER_FAILURE: "test_malformed_and_blocked_provider_results_are_explicit_outcomes",
    ac.StopReason.PROVIDER_BLOCKED: "test_malformed_and_blocked_provider_results_are_explicit_outcomes",
    ac.StopReason.PROVIDER_INVALID: "test_malformed_result_collections_execute_no_tool_and_leak_no_type_error",
    ac.StopReason.PROVIDER_TIMEOUT: "test_a_hanging_provider_is_abandoned_at_its_enforced_wait",
    ac.StopReason.UNSUPPORTED_CAPABILITY:
        "test_a_provider_without_tool_use_or_parallel_capability_stops_before_executing",
    ac.StopReason.VERIFICATION_FAILED: "test_uncorrectable_verification_fails_bounded_and_reserves_no_delivery",
    ac.StopReason.REPEATED_TOOL_REQUEST: "test_repeated_tool_requests_terminate_explicitly",
}


def test_every_stop_reason_has_a_behavioural_regression() -> None:
    """The vocabulary is closed and every member is reached by a real run.

    Each reason names the PostgreSQL regression that drives the loop into it;
    that test asserts the outcome, so a decorative member cannot survive here.
    """
    import pathlib as _pathlib  # noqa: PLC0415

    assert set(STOP_REASON_COVERAGE) == set(ac.StopReason), "every stop reason needs a behavioural regression"
    module = _pathlib.Path("tests/commerce_reliability/test_commerce_runtime_agent_loop_pg.py").read_text(
        encoding="utf-8")
    missing = sorted({name for name in STOP_REASON_COVERAGE.values() if f"def {name}(" not in module})
    assert missing == [], missing


def test_the_scripted_provider_is_deterministic_and_fails_explicitly_when_exhausted() -> None:
    provider = sp.ScriptedReasoningProvider([sp.reply("مرحبًا")])
    request = ac.ProviderRequest(step_no=1, context=ac.AuthorizedContext(1, "live", 1, 1, {}, {}), tools=(),
                                 observations=(), feedback=(), budget=ac.BudgetView(1, 1, 1.0))
    first = provider.step(request)
    assert isinstance(first, ac.ProviderReply) and first.draft.text == "مرحبًا"
    assert isinstance(provider.step(request), ac.ProviderFailure)
    assert len(provider.requests) == 2
