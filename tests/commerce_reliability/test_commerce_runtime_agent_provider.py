"""The Anthropic single-step adapter, proved without a model call.

Every case drives ``AnthropicReasoningProvider`` against a recording double of
``AnthropicProvider.call_single_step``. The adapter's only job is translation,
so these prove the translation is total and closed: each shape the API can
return becomes exactly one member of the loop's result union, the request the
adapter builds asks for exactly one attempt, and nothing here composes a
customer-facing sentence.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import reply_card as rcard
from core.commerce_runtime import reply_choices as rc

INSTRUCTIONS = "EXISTING-INSTRUCTIONS-OWNED-ELSEWHERE"

CATALOG_TOOL = ac.ToolDefinition(
    name="catalog_search", description="search the catalog",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": []},
    result_kind="product_list",
)
KNOWLEDGE_TOOL = ac.ToolDefinition(
    name="merchant_knowledge_lookup", description="search merchant knowledge",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": []},
    result_kind="knowledge_entry",
)


class RecordingProvider:
    """A double for the repository's Anthropic provider. No network, ever."""

    def __init__(self, answers: List[Dict[str, Any]]) -> None:
        self._answers = list(answers)
        self.calls: List[Dict[str, Any]] = []

    def call_single_step(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        if not self._answers:
            raise AssertionError("the adapter asked for more steps than the test scripted")
        return self._answers.pop(0)


def ok(blocks: List[Dict[str, Any]], *, stop_reason: str = "tool_use",
       usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"provider": "anthropic", "model": "claude-test", "status": "ok",
            "stop_reason": stop_reason, "blocks": blocks, "usage": usage,
            "request_id": "req_1", "error": None}


def tool_use(call_id: str, name: str, **arguments: Any) -> Dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}


def reply_block(call_id: str = "call_reply", **arguments: Any) -> Dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": ap.REPLY_TOOL_NAME, "input": dict(arguments)}


def observation(call_id: str, tool: str = "catalog_search", *, ok_: bool = True,
                refs: tuple = ("catalog:product:1",), restored: bool = False) -> ac.ToolObservation:
    return ac.ToolObservation(call_id=call_id, tool_name=tool, ok=ok_,
                              result={"status": "ok", "found": True} if ok_ else None,
                              error_code=None if ok_ else "tool_failure",
                              error=None if ok_ else "boom",
                              evidence_refs=refs if ok_ else (), restored=restored)


def request(*, step_no: int = 1, observations: tuple = (), feedback: tuple = (),
            remaining_seconds: float = 40.0, tools: tuple = (CATALOG_TOOL,)) -> ac.ProviderRequest:
    return ac.ProviderRequest(
        step_no=step_no,
        context=ac.AuthorizedContext(tenant_id=4, namespace="live", conversation_id=9, turn_id=11,
                                     inbound={"text": "عندكم حذاء رياضي؟"}, state_payload={}),
        tools=tools, observations=observations, feedback=feedback,
        budget=ac.BudgetView(remaining_steps=3, remaining_tool_calls=5, remaining_seconds=remaining_seconds),
    )


def build(answers: List[Dict[str, Any]], **kwargs: Any) -> tuple:
    double = RecordingProvider(answers)
    provider = ap.AnthropicReasoningProvider(instructions=INSTRUCTIONS, tools_provider=double, **kwargs)
    return provider, double


# ── Declared capabilities ────────────────────────────────────────────────────


def test_the_adapter_declares_bounded_capabilities_the_loop_accepts():
    provider, _ = build([], max_tool_requests_per_step=3)
    caps = ac.validate_capabilities(provider.capabilities)
    assert caps.provider_name == "anthropic"
    assert caps.tool_use is True and caps.evidence_refs is True
    assert caps.max_tool_requests_per_step == 3 and caps.parallel_tool_use is True


def test_a_single_request_capability_is_declared_as_not_parallel():
    provider, _ = build([], max_tool_requests_per_step=1)
    caps = provider.capabilities
    assert caps.parallel_tool_use is False and caps.max_tool_requests_per_step == 1


def test_the_adapter_refuses_to_exist_without_the_existing_instructions():
    with pytest.raises(ValueError):
        ap.AnthropicReasoningProvider(instructions="   ", tools_provider=RecordingProvider([]))


# ── The request the adapter builds ───────────────────────────────────────────


def test_one_step_asks_for_one_attempt_with_the_loop_s_own_wait_and_prompt():
    provider, double = build([ok([reply_block(text="مرحبا", claims_commerce_facts=False)])])
    provider.step(request(remaining_seconds=17.5))
    call = double.calls[0]
    assert call["system"] == INSTRUCTIONS            # the prompt is passed through, never composed here
    assert call["timeout_seconds"] == 17.5           # the loop's remaining budget, not a library default
    assert call["tool_choice"] == {"type": "any"}    # every step is a tool call or the reply channel
    assert call["max_tokens"] == ap.MAX_OUTPUT_TOKENS


def test_a_nearly_spent_budget_still_asks_with_a_usable_floor():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])])
    provider.step(request(remaining_seconds=0.0))
    assert double.calls[0]["timeout_seconds"] == ap.MIN_STEP_SECONDS


def test_the_reply_channel_is_declared_alongside_the_loop_s_own_tools():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])])
    provider.step(request(tools=(CATALOG_TOOL, KNOWLEDGE_TOOL)))
    names = [t["name"] for t in double.calls[0]["tools"]]
    assert names == ["catalog_search", "merchant_knowledge_lookup", ap.REPLY_TOOL_NAME]


def test_the_tool_schemas_handed_to_the_model_are_detached_copies():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])])
    provider.step(request())
    declared = double.calls[0]["tools"][0]["input_schema"]
    declared["properties"]["query"]["type"] = "integer"
    assert CATALOG_TOOL.input_schema["properties"]["query"]["type"] == "string"


# ── Tool requests ────────────────────────────────────────────────────────────


def test_tool_use_blocks_become_tool_requests_keyed_by_the_model_s_own_ids():
    provider, _ = build([ok([tool_use("toolu_a", "catalog_search", query="حذاء")])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderToolRequests)
    assert [(r.call_id, r.tool_name, dict(r.arguments)) for r in result.requests] == [
        ("toolu_a", "catalog_search", {"query": "حذاء"})
    ]


def test_a_parallel_bundle_within_the_declared_maximum_is_passed_through_whole():
    provider, _ = build([ok([tool_use("toolu_a", "catalog_search", query="حذاء"),
                             tool_use("toolu_b", "merchant_knowledge_lookup", query="التوصيل")])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderToolRequests) and len(result.requests) == 2


def test_more_requests_than_declared_are_refused_as_invalid_not_silently_trimmed():
    provider, _ = build([ok([tool_use("a", "catalog_search"), tool_use("b", "catalog_search"),
                             tool_use("c", "catalog_search")])], max_tool_requests_per_step=2)
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid)
    assert result.reason.startswith("tool_requests_exceed_declared_maximum")


@pytest.mark.parametrize("block, reason", [
    ({"type": "tool_use", "id": "", "name": "catalog_search", "input": {}}, "tool_use_block_missing_id_or_name"),
    ({"type": "tool_use", "id": "a", "name": "", "input": {}}, "tool_use_block_missing_id_or_name"),
    ({"type": "tool_use", "id": "a", "name": "catalog_search", "input": "nope"},
     "tool_use_arguments_not_an_object"),
])
def test_a_malformed_tool_use_block_is_invalid_output(block, reason):
    provider, _ = build([ok([block])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid) and result.reason == reason


def test_tool_arguments_are_detached_from_the_block_the_adapter_was_handed():
    arguments: Dict[str, Any] = {"query": "حذاء"}
    provider, _ = build([ok([{"type": "tool_use", "id": "a", "name": "catalog_search", "input": arguments}])])
    result = provider.step(request())
    arguments["query"] = "tampered"
    assert dict(result.requests[0].arguments) == {"query": "حذاء"}


# ── The reply channel ────────────────────────────────────────────────────────


def test_submit_reply_becomes_a_reply_draft_carrying_its_refs_and_its_own_commerce_flag():
    provider, _ = build([ok([reply_block(text="  السعر ٩٩ ريال  ",
                                         evidence_refs=["catalog:product:1"],
                                         claims_commerce_facts=True)], stop_reason="tool_use")])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderReply)
    assert result.draft.text == "السعر ٩٩ ريال"
    assert result.draft.evidence_refs == ("catalog:product:1",)
    assert result.draft.claims_commerce_facts is True


def test_a_conversational_reply_may_cite_nothing_and_says_so():
    provider, _ = build([ok([reply_block(text="أهلاً", claims_commerce_facts=False)])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderReply)
    assert result.draft.evidence_refs == () and result.draft.claims_commerce_facts is False


def test_a_reply_mixed_with_a_tool_request_is_refused_rather_than_guessed():
    provider, _ = build([ok([tool_use("a", "catalog_search"),
                             reply_block(text="جاهز", claims_commerce_facts=False)])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid) and result.reason == "reply_mixed_with_tool_requests"


@pytest.mark.parametrize("arguments, reason", [
    ({"claims_commerce_facts": False}, "reply_text_missing"),
    ({"text": "   ", "claims_commerce_facts": False}, "reply_text_missing"),
    ({"text": "hi", "claims_commerce_facts": False, "evidence_refs": "catalog:product:1"},
     "reply_evidence_refs_not_a_list"),
    ({"text": "hi"}, "reply_commerce_flag_missing"),
    ({"text": "hi", "claims_commerce_facts": "yes"}, "reply_commerce_flag_missing"),
])
def test_a_malformed_reply_is_invalid_output_never_a_delivered_sentence(arguments, reason):
    provider, _ = build([ok([reply_block(**arguments)])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid) and result.reason == reason


def test_reply_arguments_that_are_not_an_object_are_invalid_output():
    provider, _ = build([ok([{"type": "tool_use", "id": "r", "name": ap.REPLY_TOOL_NAME, "input": []}])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid) and result.reason == "reply_arguments_not_an_object"


def test_a_reply_may_offer_a_selector_and_carries_only_the_ids_it_named():
    """The model names products; nothing it writes becomes a row's label."""
    provider, _ = build([ok([reply_block(
        text="عندنا ثلاثة فساتين", evidence_refs=["catalog:product:23"],
        claims_commerce_facts=True,
        choices={"product_ids": [23, 37], "button": "اختاري", "title": "لا يُقرأ"})])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderReply)
    assert result.draft.payload == {rc.REQUESTED_KEY: {"product_ids": [23, 37],
                                                       "button": "اختاري"}}
    # Still a text reply at this point: whether the selector may be offered is
    # established against the turn's observations, not here.
    assert result.draft.kind == lc.DeliveryKind.TEXT.value


def test_a_reply_that_offers_no_selector_carries_an_empty_payload():
    provider, _ = build([ok([reply_block(text="أهلاً", claims_commerce_facts=False)])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderReply) and result.draft.payload == {}


def test_a_selector_the_model_asked_for_without_products_is_simply_no_selector():
    provider, _ = build([ok([reply_block(text="أهلاً", claims_commerce_facts=False,
                                         choices={"button": "اختر"})])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderReply) and result.draft.payload == {}


@pytest.mark.parametrize("choices", ["23,37", ["23"], 7])
def test_a_selector_shaped_wrongly_is_invalid_output_never_a_guess(choices):
    provider, _ = build([ok([reply_block(text="أهلاً", claims_commerce_facts=False,
                                         choices=choices)])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid) and result.reason == "reply_choices_not_an_object"


def test_the_product_ids_field_must_be_a_list_to_mean_anything():
    provider, _ = build([ok([reply_block(text="أهلاً", claims_commerce_facts=False,
                                         choices={"product_ids": "23"})])])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid) and result.reason == "reply_choices_not_an_object"


def test_the_reply_channel_declares_the_selector_as_optional():
    schema = ap.REPLY_TOOL_SCHEMA
    assert "choices" in schema["properties"]
    assert "choices" not in schema["required"]
    assert schema["properties"]["choices"]["required"] == ["product_ids"]


def test_plain_text_without_the_reply_channel_is_never_delivered():
    provider, _ = build([ok([{"type": "text", "text": "المنتج متوفر بسعر ٩٩"}], stop_reason="end_turn")])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid) and result.reason == "no_tool_use_block"


# ── Failure, refusal, truncation ─────────────────────────────────────────────


@pytest.mark.parametrize("status", sorted(ap._FAILURE_STATUSES))
def test_every_transport_or_capacity_status_is_reported_as_a_provider_failure(status):
    provider, _ = build([{"provider": "anthropic", "model": "m", "status": status, "stop_reason": None,
                          "blocks": [], "usage": None, "error": "X"}])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderFailure) and result.reason == status


def test_an_unrecognised_status_is_still_a_failure_and_names_itself():
    provider, _ = build([{"provider": "anthropic", "model": "m", "status": "brand_new", "stop_reason": None,
                          "blocks": [], "usage": None, "error": None}])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderFailure) and result.reason == "unexpected_status:brand_new"


def test_a_model_refusal_is_blocked_not_retried_as_a_failure():
    provider, _ = build([ok([], stop_reason="refusal")])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderBlocked) and result.reason == "model_refusal"


def test_a_truncated_step_is_invalid_even_when_it_carries_a_complete_looking_reply():
    provider, _ = build([ok([reply_block(text="السعر", claims_commerce_facts=True,
                                         evidence_refs=["catalog:product:1"])], stop_reason="max_tokens")])
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid) and result.reason == "truncated_output"


def test_every_result_the_adapter_can_return_passes_the_loop_s_own_validation():
    caps = ac.ProviderCapabilities(provider_name="anthropic", parallel_tool_use=True,
                                   max_tool_requests_per_step=3)
    answers = [
        ok([tool_use("a", "catalog_search", query="x")]),
        ok([reply_block(text="hi", claims_commerce_facts=False)]),
        ok([], stop_reason="refusal"),
        ok([reply_block(text="hi", claims_commerce_facts=False)], stop_reason="max_tokens"),
        {"provider": "anthropic", "model": "m", "status": "overloaded", "stop_reason": None,
         "blocks": [], "usage": None, "error": None},
    ]
    for answer in answers:
        provider, _ = build([answer])
        ac.validate_provider_result(provider.step(request()), caps)


# ── Usage ────────────────────────────────────────────────────────────────────


def test_usage_is_recorded_per_step_when_the_api_reports_it():
    provider, _ = build([ok([tool_use("a", "catalog_search")], usage={"input_tokens": 120, "output_tokens": 30}),
                         ok([reply_block(text="hi", claims_commerce_facts=False)],
                            usage={"input_tokens": 400, "output_tokens": 60})])
    provider.step(request(step_no=1))
    provider.step(request(step_no=2, observations=(observation("a"),)))
    assert [u.step_no for u in provider.usage] == [1, 2]
    assert provider.total_input_tokens == 520 and provider.total_output_tokens == 90
    assert all(u.usage_available for u in provider.usage)


def test_unavailable_usage_stays_absent_and_is_never_reported_as_zero():
    provider, _ = build([ok([reply_block(text="hi", claims_commerce_facts=False)], usage=None)])
    provider.step(request())
    step = provider.usage[0]
    assert step.input_tokens is None and step.output_tokens is None
    assert step.usage_available is False
    assert provider.total_input_tokens is None and provider.total_output_tokens is None


def test_a_failed_step_still_records_what_it_was_and_what_it_cost():
    provider, _ = build([{"provider": "anthropic", "model": "m", "status": "timeout", "stop_reason": None,
                          "blocks": [], "usage": None, "error": "APITimeoutError"}])
    provider.step(request())
    assert provider.usage[0].status == "timeout" and provider.usage[0].usage_available is False


# ── Transcript ───────────────────────────────────────────────────────────────


def test_this_invocation_s_observations_are_replayed_as_native_tool_result_pairs():
    provider, double = build([ok([tool_use("toolu_a", "catalog_search", query="حذاء")]),
                              ok([reply_block(text="متوفر", claims_commerce_facts=True,
                                              evidence_refs=["catalog:product:1"])])])
    provider.step(request(step_no=1))
    provider.step(request(step_no=2, observations=(observation("toolu_a"),)))
    messages = double.calls[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"][0]["type"] == "tool_use"
    result_block = messages[2]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "toolu_a" and result_block["is_error"] is False


def test_a_failed_observation_is_replayed_as_an_error_result_not_hidden():
    provider, double = build([ok([tool_use("toolu_a", "catalog_search")]),
                              ok([reply_block(text="ok", claims_commerce_facts=False)])])
    provider.step(request(step_no=1))
    provider.step(request(step_no=2, observations=(observation("toolu_a", ok_=False),)))
    result_block = double.calls[1]["messages"][2]["content"][0]
    assert result_block["is_error"] is True and "tool_failure" in result_block["content"]


def test_a_restored_observation_is_presented_as_labelled_data_not_a_fabricated_pair():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])])
    provider.step(request(step_no=3, observations=(observation("toolu_old", restored=True),)))
    messages = double.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user"] * len(messages)
    joined = "".join(b.get("text", "") for m in messages for b in m["content"])
    assert "earlier_tool_observations" in joined
    assert "restored_from_earlier_attempt" in joined
    assert all(b.get("type") != "tool_result" for m in messages for b in m["content"])


def test_a_refused_draft_comes_back_to_the_model_with_the_reason_it_was_refused():
    provider, double = build([ok([reply_block("r1", text="السعر ٩٩", claims_commerce_facts=True,
                                              evidence_refs=["catalog:product:404"])]),
                              ok([reply_block("r2", text="لحظة", claims_commerce_facts=False)])])
    provider.step(request(step_no=1))
    feedback = (ac.VerificationFeedback(
        step_no=1, problems=(ac.VerificationProblem("unknown_evidence", "catalog:product:404 was not observed"),)),)
    provider.step(request(step_no=2, feedback=feedback))
    messages = double.calls[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    refusal = messages[2]["content"][0]
    assert refusal["type"] == "tool_result" and refusal["tool_use_id"] == "r1"
    assert refusal["is_error"] is True and "unknown_evidence" in refusal["content"]
    # The refusal is stated once, as that call's result — not also as loose data.
    assert all("verification_problems" not in b.get("text", "")
               for m in messages for b in m["content"] if b.get("type") == "text")


def test_feedback_without_a_replayable_draft_is_still_shown_as_data():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])])
    feedback = (ac.VerificationFeedback(step_no=1, problems=(ac.VerificationProblem("empty_text", "restored"),)),)
    provider.step(request(step_no=4, feedback=feedback))
    joined = "".join(b.get("text", "") for m in double.calls[0]["messages"] for b in m["content"])
    assert "verification_problems" in joined and "empty_text" in joined


def test_the_customer_turn_opens_the_transcript_verbatim():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])])
    provider.step(request())
    first = double.calls[0]["messages"][0]
    assert first["role"] == "user"
    assert first["content"][-1]["text"] == "عندكم حذاء رياضي؟"


def test_trusted_context_the_runtime_supplies_is_shown_before_the_customer_turn():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])],
                             context_preamble={"customer_name": "نورة عبدالله", "locale": "ar-SA"})
    provider.step(request())
    blocks = double.calls[0]["messages"][0]["content"]
    assert "conversation_context" in blocks[0]["text"] and "نورة عبدالله" in blocks[0]["text"]
    assert blocks[1]["text"] == "عندكم حذاء رياضي؟"


def test_an_incomplete_tool_result_set_is_never_sent_as_a_partial_pair():
    provider, double = build([ok([tool_use("toolu_a", "catalog_search"),
                                  tool_use("toolu_b", "merchant_knowledge_lookup")]),
                              ok([reply_block(text="ok", claims_commerce_facts=False)])])
    provider.step(request(step_no=1))
    provider.step(request(step_no=2, observations=(observation("toolu_a"),)))
    messages = double.calls[1]["messages"]
    assert all(m["role"] == "user" for m in messages)
    joined = "".join(b.get("text", "") for m in messages for b in m["content"])
    assert "earlier_tool_observations" in joined


# ── The repository's own Anthropic entry point ───────────────────────────────


class _FakeUsage:
    input_tokens = 11
    output_tokens = 3
    cache_read_input_tokens = None
    cache_creation_input_tokens = None


class _FakeText:
    type = "text"
    text = "hello"


class _FakeResponse:
    id = "msg_1"
    stop_reason = "end_turn"
    usage = _FakeUsage()
    content = [_FakeText()]


class _FakeClient:
    created: List[Dict[str, Any]] = []
    constructed: List[Dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        _FakeClient.constructed.append(kwargs)
        self.messages = self

    def create(self, **kwargs: Any) -> _FakeResponse:
        _FakeClient.created.append(kwargs)
        return _FakeResponse()


class _FakeSDK:
    Anthropic = _FakeClient

    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class APITimeoutError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    class APIStatusError(Exception):
        status_code = 500


@pytest.fixture()
def anthropic_double(monkeypatch):
    from modules.ai.orchestrator.providers import anthropic_provider as module

    _FakeClient.created.clear()
    _FakeClient.constructed.clear()
    monkeypatch.setattr(module, "_API_KEY", "test-key", raising=True)
    monkeypatch.setattr(module, "_SDK_AVAILABLE", True, raising=True)
    monkeypatch.setattr(module, "_anthropic_sdk", _FakeSDK, raising=True)
    monkeypatch.setattr(module, "emit_llm_cost_audit", lambda **kwargs: None, raising=True)
    monkeypatch.setattr(module, "record_ai_usage_from_anthropic", lambda **kwargs: None, raising=True)
    return module


def test_the_single_step_call_resolves_the_same_model_as_the_legacy_path(anthropic_double, monkeypatch):
    """The new entry point must never become a second model-selection surface."""
    seen: List[tuple] = []

    def resolver(audit_context, *, provider, default):
        seen.append((audit_context, provider, default))
        return "claude-resolved"

    monkeypatch.setattr(anthropic_double, "resolve_model_for_provider", resolver, raising=True)
    monkeypatch.setattr(anthropic_double, "resolve_anthropic_model", lambda: "claude-default", raising=True)
    provider = anthropic_double.AnthropicProvider()
    audit = {"tenant_id": 4}
    legacy = provider.call(message="hi", prompt="p", audit_context=audit)
    single = provider.call_single_step(messages=[{"role": "user", "content": "hi"}], system="p",
                                       audit_context=audit)
    assert legacy["model"] == single["model"] == "claude-resolved"
    assert seen[0] == seen[1] == (audit, "anthropic", "claude-default")


def test_the_single_step_call_disables_library_retries_and_uses_the_caller_s_timeout(anthropic_double):
    provider = anthropic_double.AnthropicProvider()
    provider.call_single_step(messages=[{"role": "user", "content": "hi"}], system="p",
                              timeout_seconds=12.5)
    assert _FakeClient.constructed[-1]["max_retries"] == 0
    assert _FakeClient.constructed[-1]["timeout"] == 12.5


def test_the_single_step_call_reports_blocks_stop_reason_and_usage_the_legacy_path_drops(anthropic_double):
    provider = anthropic_double.AnthropicProvider()
    answer = provider.call_single_step(messages=[{"role": "user", "content": "hi"}], system="p")
    assert answer["status"] == "ok" and answer["stop_reason"] == "end_turn"
    assert answer["blocks"] == [{"type": "text", "text": "hello"}]
    assert answer["usage"]["input_tokens"] == 11 and answer["usage"]["output_tokens"] == 3
    assert answer["request_id"] == "msg_1"


@pytest.mark.parametrize("error, status", [
    (_FakeSDK.AuthenticationError, "auth_error"),
    (_FakeSDK.RateLimitError, "rate_limited"),
    (_FakeSDK.APITimeoutError, "timeout"),
    (_FakeSDK.APIConnectionError, "connection_error"),
])
def test_each_sdk_error_becomes_a_named_status_and_never_raises(anthropic_double, monkeypatch, error, status):
    class Failing(_FakeClient):
        def create(self, **kwargs: Any):
            raise error("x")

    monkeypatch.setattr(_FakeSDK, "Anthropic", Failing, raising=True)
    answer = anthropic_double.AnthropicProvider().call_single_step(
        messages=[{"role": "user", "content": "hi"}], system="p")
    assert answer["status"] == status and answer["blocks"] == []


def test_an_overloaded_api_status_is_named_apart_from_other_api_errors(anthropic_double, monkeypatch):
    class Overloaded(_FakeClient):
        def create(self, **kwargs: Any):
            error = _FakeSDK.APIStatusError("overloaded")
            error.status_code = 529
            raise error

    monkeypatch.setattr(_FakeSDK, "Anthropic", Overloaded, raising=True)
    answer = anthropic_double.AnthropicProvider().call_single_step(
        messages=[{"role": "user", "content": "hi"}], system="p")
    assert answer["status"] == "overloaded"


def test_a_missing_key_or_sdk_is_reported_rather_than_falling_back_to_another_path(anthropic_double,
                                                                                   monkeypatch):
    monkeypatch.setattr(anthropic_double, "_API_KEY", "", raising=True)
    assert anthropic_double.AnthropicProvider().call_single_step(
        messages=[], system="p")["status"] == "no_api_key"
    monkeypatch.setattr(anthropic_double, "_API_KEY", "k", raising=True)
    monkeypatch.setattr(anthropic_double, "_SDK_AVAILABLE", False, raising=True)
    assert anthropic_double.AnthropicProvider().call_single_step(
        messages=[], system="p")["status"] == "sdk_unavailable"


# ── The prior conversation ───────────────────────────────────────────────────


def history(*turns) -> list:
    return [{"role": role, "text": text} for role, text in turns]


def test_the_prior_conversation_is_presented_as_real_chat_turns():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])],
                             history=history(("user", "عندكم أحذية؟"), ("assistant", "نعم، عندنا")))
    provider.step(request())
    messages = double.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"][0]["text"] == "عندكم أحذية؟"
    assert messages[1]["content"][0]["text"] == "نعم، عندنا"
    assert messages[-1]["content"][-1]["text"] == "عندكم حذاء رياضي؟"


def test_a_history_ending_with_the_customer_joins_the_current_turn_rather_than_inventing_a_reply():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])],
                             history=history(("user", "أول سؤال"), ("assistant", "جواب"),
                                             ("user", "سؤال متأخر")))
    provider.step(request())
    messages = double.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    texts = [block["text"] for block in messages[-1]["content"]]
    assert texts == ["سؤال متأخر", "عندكم حذاء رياضي؟"]
    # Nothing was fabricated on the assistant's behalf.
    assert messages[1]["content"][0]["text"] == "جواب"


def test_a_history_that_opens_with_the_assistant_drops_that_opening():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])],
                             history=history(("assistant", "أهلاً"), ("user", "سؤال"),
                                             ("assistant", "جواب")))
    provider.step(request())
    roles = [m["role"] for m in double.calls[0]["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_consecutive_turns_from_one_side_are_merged_so_the_transcript_alternates():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])],
                             history=history(("user", "واحد"), ("user", "اثنان"),
                                             ("assistant", "أ"), ("assistant", "ب")))
    provider.step(request())
    messages = double.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"][0]["text"] == "واحد\nاثنان"
    assert messages[1]["content"][0]["text"] == "أ\nب"


def test_empty_and_malformed_history_entries_are_dropped_not_rendered():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])],
                             history=[{"role": "user", "text": "  "}, "not-a-mapping",
                                      {"role": "user", "text": "سؤال"},
                                      {"role": "assistant", "text": None}])
    provider.step(request())
    messages = double.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["user"]
    assert [b["text"] for b in messages[0]["content"]] == ["سؤال", "عندكم حذاء رياضي؟"]


def test_the_history_is_bounded_in_turns_and_in_characters():
    long_turn = "ط" * (ap.MAX_HISTORY_CHARS + 500)
    turns = []
    for index in range(ap.MAX_HISTORY_MESSAGES * 3):
        turns.append(("user" if index % 2 == 0 else "assistant", f"{long_turn}{index}"))
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])],
                             history=history(*turns))
    provider.step(request())
    messages = double.calls[0]["messages"]
    assert len(messages) <= ap.MAX_HISTORY_MESSAGES + 1
    for message in messages[:-1]:
        assert len(message["content"][0]["text"]) <= ap.MAX_HISTORY_CHARS


def test_no_history_is_the_same_single_turn_transcript_as_before():
    provider, double = build([ok([reply_block(text="ok", claims_commerce_facts=False)])])
    provider.step(request())
    messages = double.calls[0]["messages"]
    assert len(messages) == 1 and messages[0]["role"] == "user"


def test_the_history_never_displaces_the_tool_result_replay():
    provider, double = build([ok([tool_use("toolu_a", "catalog_search", query="حذاء")]),
                              ok([reply_block(text="متوفر", claims_commerce_facts=False)])],
                             history=history(("user", "سؤال سابق"), ("assistant", "جواب سابق")))
    provider.step(request(step_no=1))
    provider.step(request(step_no=2, observations=(observation("toolu_a"),)))
    roles = [m["role"] for m in double.calls[1]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert double.calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"


# ── Malformed tool input at the SDK boundary ─────────────────────────────────


class _Block:
    def __init__(self, type_: str, **fields: Any) -> None:
        self.type = type_
        for key, value in fields.items():
            setattr(self, key, value)


class _MalformedResponse:
    id = "msg_x"
    stop_reason = "tool_use"
    usage = None

    def __init__(self, raw_input: Any) -> None:
        self.content = [_Block("tool_use", id="toolu_1", name="search_products", input=raw_input)]


@pytest.mark.parametrize("raw_input", [None, [], {}, "", 0, False, "query=shoes",
                                       ["query", "shoes"], 7, True])
def test_the_sdk_entry_point_reports_tool_input_exactly_as_it_arrived(anthropic_double, monkeypatch,
                                                                      raw_input):
    class Client(_FakeClient):
        def create(self, **kwargs: Any):
            return _MalformedResponse(raw_input)

    monkeypatch.setattr(_FakeSDK, "Anthropic", Client, raising=True)
    answer = anthropic_double.AnthropicProvider().call_single_step(
        messages=[{"role": "user", "content": "hi"}], system="p")
    assert answer["blocks"][0]["input"] is raw_input or answer["blocks"][0]["input"] == raw_input


@pytest.mark.parametrize("raw_input", [None, [], "", 0, False, "query=shoes",
                                       ["query", "shoes"], 7, True])
def test_a_malformed_tool_input_never_becomes_an_executable_default_argument_call(
        anthropic_double, monkeypatch, raw_input):
    """A falsy or wrongly-shaped input used to arrive as {} — a runnable request."""
    class Client(_FakeClient):
        def create(self, **kwargs: Any):
            return _MalformedResponse(raw_input)

    monkeypatch.setattr(_FakeSDK, "Anthropic", Client, raising=True)
    provider = ap.AnthropicReasoningProvider(
        instructions=INSTRUCTIONS, tools_provider=anthropic_double.AnthropicProvider())
    result = provider.step(request())
    assert isinstance(result, ac.ProviderInvalid)
    assert result.reason == "tool_use_arguments_not_an_object"


def test_a_genuine_no_argument_call_is_still_a_runnable_request(anthropic_double, monkeypatch):
    class Client(_FakeClient):
        def create(self, **kwargs: Any):
            return _MalformedResponse({})

    monkeypatch.setattr(_FakeSDK, "Anthropic", Client, raising=True)
    provider = ap.AnthropicReasoningProvider(
        instructions=INSTRUCTIONS, tools_provider=anthropic_double.AnthropicProvider())
    result = provider.step(request())
    assert isinstance(result, ac.ProviderToolRequests)
    assert dict(result.requests[0].arguments) == {}


# ── The capability declaration ────────────────────────────────────────────────
#
# What these prove: the declaration states what the tool accepts, and states the
# relationship between the text and each structured shape the way the platform
# actually implements it.
#
# What they CANNOT prove — and nothing offline can: that a model reading the
# corrected declaration will choose to ask for a selector or a card. That is a
# non-deterministic decision and its only evidence is a live A/B on the real
# path, measured on ``choices_outcome`` / ``card_outcome``. These cases exist so
# the contract cannot silently drift back, not to stand in for that round.

_OMITTED_ALWAYS = "omit this whenever the text answers on its own"


def _declarations() -> dict:
    """The three model-facing strings, lowercased, keyed by what they declare."""
    schema = ap.REPLY_TOOL_SCHEMA
    return {
        "tool": ap.REPLY_TOOL_DESCRIPTION.lower(),
        "choices": schema["properties"]["choices"]["description"].lower(),
        "card": schema["properties"]["card"]["description"].lower(),
    }


def test_the_declaration_names_both_structured_shapes_the_tool_accepts():
    """The defect the Tenant 1 round exposed: the tool's own description
    advertised the selector and never mentioned the card, while the pilot
    instructions mention neither. A declaration that names one of two accepted
    shapes is an incomplete statement of the interface."""
    tool = _declarations()["tool"]
    assert "selector" in tool
    assert "card" in tool


def test_no_shape_is_gated_on_a_condition_the_platform_always_satisfies():
    """``reply_card`` states the answer 'already stood on its own prose, which
    is why a card is an affordance over it rather than a part of it'. Telling
    the model to omit the shape *because* the text stands on its own therefore
    names a condition that is true on every turn, and asks for the shape to be
    omitted every time."""
    for where, text in _declarations().items():
        assert _OMITTED_ALWAYS not in text, where


def test_each_shape_declares_itself_an_addition_over_the_text_not_a_replacement():
    """The relationship the implementation actually has, stated where the model
    can read it."""
    declarations = _declarations()
    for where in ("choices", "card"):
        assert "addition over the text" in declarations[where], where
        assert "never a replacement" in declarations[where], where
        assert "never required" in declarations[where] or "is never required" in declarations[where]
    assert "addition over the text" in declarations["tool"]
    assert "never a replacement" in declarations["tool"]


def test_the_declaration_says_what_each_shape_already_shows():
    """Tenant 1, 25 September: beside a selector the text listed five products
    with prices and sizes the rows already carried, and beside a card it gave the
    photo's raw address. The declaration had said only that each shape is an
    addition; it never said what the shape shows. These are those facts, as
    ``choice_rows`` and ``reply_card`` implement them."""
    declarations = _declarations()
    for fact in ("name", "price", "options"):
        assert fact in declarations["tool"], fact
    for fact in ("short title", "current price", "a few of its options"):
        assert fact in declarations["choices"], fact
    # A row is bounded; what it cannot hold stays the text's to say.
    assert "what a row has no room for" in declarations["choices"]
    assert "what a row has no room for" in declarations["tool"]
    assert "photo" in declarations["card"] and "page" in declarations["card"]


def test_the_text_is_released_from_repeating_the_shape_never_from_answering():
    declarations = _declarations()
    assert "need not repeat what the shape already shows" in declarations["tool"]
    # The text still answers, with whatever detail the turn calls for.
    for duty in ("introduction", "explanation", "comparison", "as much detail"):
        assert duty in declarations["tool"], duty
    assert "need not list them again" in declarations["choices"]
    assert "web address unless the customer asked for a link" in declarations["card"]
    assert "carries the answer by itself" not in declarations["tool"]


def test_the_fallbacks_the_declaration_promises_are_the_ones_implemented():
    """A shorter text is safe only because a selector that cannot be shown
    becomes lines, and a card that cannot be shown leaves the text alone — both
    stated, so the model can tell which case it is writing for."""
    declarations = _declarations()
    assert "adds its rows to the text as lines" in declarations["choices"]
    for condition in ("https photo", "https page link", "button_label",
                      "text is all the customer receives"):
        assert condition in declarations["card"], condition
    assert rcard.NO_IMAGE and rcard.INSECURE_LINK and rcard.NO_LABEL


def test_the_declaration_still_says_nothing_about_when_to_reach_for_either():
    """Deliberately unstated, and guarded so it stays that way. Telling the
    model which customer turn deserves a selector would be routing the
    conversation from the platform, which is exactly what this contract must
    not do — the correction restores the truth about the interface and leaves
    the judgement where it was."""
    for where, text in _declarations().items():
        for rule in ("when the customer", "if the customer", "use this when",
                     "always offer", "you should offer", "whenever the customer"):
            assert rule not in text, f"{where}: {rule!r} would be a usage rule"


def test_the_accepted_shape_itself_is_untouched_by_the_correction():
    """A model that already asked correctly sends exactly the same arguments.

    ``card.product_id`` became optional, which only ever *widens* what is
    accepted: nothing a model could send before is refused now, and nothing it
    sent before means something different. The widening exists because the
    button word is the one thing on a card the customer reads — it is the
    model's, in the customer's language — while which product the card shows is
    the platform's, from the customer's own verified selection. Requiring the
    two together made the wording unobtainable without also claiming the
    identity; see ``card.button_label`` below and
    docs/engineering/commerce-runtime-product-presentation.md.
    """
    schema = ap.REPLY_TOOL_SCHEMA
    assert set(schema["properties"]) == {"text", "evidence_refs", "claims_commerce_facts",
                                         "choices", "card"}
    assert schema["properties"]["choices"]["required"] == ["product_ids"]
    assert schema["properties"]["card"]["required"] == ["button_label"]
    assert set(schema["properties"]["choices"]["properties"]) == {"product_ids", "button"}
    assert set(schema["properties"]["card"]["properties"]) == {"product_id", "button_label"}
    for optional in ("choices", "card"):
        assert optional not in schema["required"]


def test_a_card_request_naming_both_is_translated_exactly_as_it_was():
    """The widening changes no accepted request. The shape a model already
    sends arrives as the same payload it always did."""
    both = ap._requested_card({"product_id": 23, "button_label": "اطلب الآن"})
    assert both == {rcard.REQUESTED_KEY: {"product_id": 23, "button_label": "اطلب الآن"}}


def test_the_button_word_survives_without_a_product_and_claims_no_identity():
    """The new shape: wording offered for whatever card the platform shows.

    It carries no product, so it cannot stand in front of the customer's own
    verified selection — and ``requested_product_id`` reads no identity out of
    it, which is what keeps the two authorities apart.
    """
    only_words = ap._requested_card({"button_label": "View product"})
    assert only_words == {rcard.REQUESTED_KEY: {"button_label": "View product"}}
    draft = ac.ReplyDraft(text="x", evidence_refs=(), claims_commerce_facts=True,
                          payload=only_words)
    assert rcard.requested_product_id(draft) is None
    assert rcard.requested_label(draft) == "View product"


def test_a_card_object_with_neither_field_is_no_request_at_all():
    assert ap._requested_card({}) == {}
    assert ap._requested_card(None) == {}
