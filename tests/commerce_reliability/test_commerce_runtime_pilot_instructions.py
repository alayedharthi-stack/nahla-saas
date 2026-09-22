"""The instructions and the declarations the model actually receives, together.

The previous integration reused the merchant instructions verbatim while
declaring a completely different set of tool names and a reply object that did
not exist. Nothing caught it because the two sides were only ever tested apart.
These cases assemble the real instructions and build the real registry and
adapter declarations, then hold them to each other.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_live_tools as alt
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import conversation_link as cl
from modules.ai.commerce_agent_v2 import pilot_instructions as pi
from modules.ai.commerce_agent_v2.agent import COMMERCE_AGENT_INSTRUCTIONS

LINK = cl.TrustedConversationLink(
    tenant_id=4, namespace="live", channel="wa", app_conversation_id=41,
    runtime_conversation_id=9, conversation_ref="wa:v1:conv:41",
)


def declared_tools() -> List[Dict[str, Any]]:
    """Exactly what the adapter hands the model: the registry plus the reply channel."""
    binding = alt.LiveToolBinding(context=object(), link=LINK)
    registry = alt.build_live_registry(binding)

    recorded: List[Dict[str, Any]] = []

    class _Provider:
        def call_single_step(self, **kwargs: Any) -> Dict[str, Any]:
            recorded.extend(kwargs["tools"])
            recorded_system.append(kwargs["system"])
            return {"provider": "anthropic", "model": "m", "status": "ok",
                    "stop_reason": "tool_use", "usage": None, "request_id": "r", "error": None,
                    "blocks": [{"type": "tool_use", "id": "r1", "name": ap.REPLY_TOOL_NAME,
                                "input": {"text": "ok", "claims_commerce_facts": False}}]}

    recorded_system: List[str] = []
    provider = ap.AnthropicReasoningProvider(
        instructions=pi.build_pilot_instructions(), tools_provider=_Provider())
    provider.step(ac.ProviderRequest(
        step_no=1,
        context=ac.AuthorizedContext(tenant_id=4, namespace="live", conversation_id=9, turn_id=1,
                                     inbound={"text": "مرحبا"}, state_payload={}),
        tools=registry.definitions, observations=(), feedback=(),
        budget=ac.BudgetView(remaining_steps=2, remaining_tool_calls=2, remaining_seconds=20.0),
    ))
    declared_tools.system = recorded_system[0]           # type: ignore[attr-defined]
    return recorded


# ── The two sides must agree ─────────────────────────────────────────────────


def test_every_tool_the_instructions_tell_the_model_to_call_is_declared():
    text = pi.build_pilot_instructions()
    named = [name for name in pi.INSTRUCTION_TOOL_NAMES if name in text]
    assert named, "the instructions name no tool at all — the list is stale"
    declared = {tool["name"] for tool in declared_tools()}
    missing = [name for name in named if name not in declared]
    assert missing == [], missing


def test_every_tool_the_instructions_name_is_declared_and_nothing_else_is_unaccounted_for():
    """The contract in both directions: a tool the instructions name must exist
    in the registry (a rename on one side leaves the model calling nothing),
    and a tool the registry declares beyond those names must be one the
    registry names on purpose (``PILOT_ONLY_TOOL_NAMES``: the owner-approved
    read of shareable promotions), never an accidental exposure."""
    assert set(pi.INSTRUCTION_TOOL_NAMES) <= set(alt.LIVE_TOOL_NAMES)
    assert set(alt.LIVE_TOOL_NAMES) - set(pi.INSTRUCTION_TOOL_NAMES) == set(alt.PILOT_ONLY_TOOL_NAMES)
    assert alt.PILOT_ONLY_TOOL_NAMES == ("list_shareable_promotions",)


def test_the_reply_channel_is_both_declared_and_described():
    declared = {tool["name"] for tool in declared_tools()}
    assert ap.REPLY_TOOL_NAME in declared
    assert ap.REPLY_TOOL_NAME in pi.build_pilot_instructions()


def test_the_reply_fields_the_instructions_name_are_fields_the_schema_declares():
    """The direction that could mislead the model: a field named in the
    addendum but absent from the schema would have it filling nothing."""
    reply = next(t for t in declared_tools() if t["name"] == ap.REPLY_TOOL_NAME)
    schema_fields = set(reply["input_schema"]["properties"])
    named = {field for field in schema_fields if field in pi.PILOT_REPLY_ADDENDUM}
    assert named == {"text", "evidence_refs", "claims_commerce_facts"}


def test_every_field_the_model_must_supply_is_named_in_the_addendum():
    """A **required** field the instructions never mention is a field the
    model can only discover by accident, and a reply without it is refused."""
    reply = next(t for t in declared_tools() if t["name"] == ap.REPLY_TOOL_NAME)
    for field in sorted(reply["input_schema"]["required"]):
        assert field in pi.PILOT_REPLY_ADDENDUM, field


def test_the_selector_is_optional_and_described_on_the_tool_itself():
    """``choices`` is deliberately absent from the addendum.

    Naming it there would be a prompt change, which GOV-001 forbids by
    default; a capability the model may decline is described where every tool
    interface is described — on the declaration the model receives. What this
    asserts is that the description is actually there and actually says the
    two things that make the capability safe to offer: the products must have
    been looked up in this turn, and the selector is never required.
    """
    reply = next(t for t in declared_tools() if t["name"] == ap.REPLY_TOOL_NAME)
    schema = reply["input_schema"]
    assert "choices" not in schema["required"]
    assert "choices" not in pi.PILOT_REPLY_ADDENDUM
    description = schema["properties"]["choices"]["description"]
    assert "this turn" in description and "evidence_refs" in description
    assert "never required" in description and "typing" in description


def test_the_system_prompt_the_model_receives_is_the_assembled_one():
    declared_tools()
    system = declared_tools.system                       # type: ignore[attr-defined]
    assert system == pi.build_pilot_instructions().strip()


# ── The adaptation is narrow, and the legacy path is untouched ───────────────


def test_the_merchant_instructions_are_included_verbatim():
    assert COMMERCE_AGENT_INSTRUCTIONS.strip() in pi.build_pilot_instructions()


def test_the_addendum_is_appended_once_and_only_once():
    text = pi.build_pilot_instructions()
    assert text.count(pi.PILOT_ADDENDUM_HEADING) == 1
    assert text.index(COMMERCE_AGENT_INSTRUCTIONS.strip()) < text.index(pi.PILOT_ADDENDUM_HEADING)


def test_the_legacy_instructions_do_not_carry_the_pilot_adaptation():
    assert pi.PILOT_ADDENDUM_HEADING not in COMMERCE_AGENT_INSTRUCTIONS
    assert ap.REPLY_TOOL_NAME not in COMMERCE_AGENT_INSTRUCTIONS


def test_the_agents_sdk_agent_still_uses_the_unadapted_instructions():
    from modules.ai.commerce_agent_v2.agent import build_commerce_agent  # noqa: PLC0415

    agent = build_commerce_agent(model="model-under-test")
    assert getattr(agent, "instructions", None) == COMMERCE_AGENT_INSTRUCTIONS


def test_the_addendum_corrects_the_two_statements_that_are_false_for_the_pilot():
    addendum = pi.PILOT_REPLY_ADDENDUM
    # The reply object the instructions describe does not exist on this path…
    assert "CommerceReply" in COMMERCE_AGENT_INSTRUCTIONS
    assert "CommerceReply" in addendum and ap.REPLY_TOOL_NAME in addendum
    # …and the run is not shadow-mode.
    assert "shadow" in COMMERCE_AGENT_INSTRUCTIONS
    assert "shadow" in addendum


def test_no_declaration_refers_to_a_tool_that_is_not_exposed():
    """A description naming a tool the model cannot call is an instruction to
    call something that does not exist."""
    declared = {tool["name"] for tool in declared_tools()}
    for tool in declared_tools():
        text = f"{tool['name']} {tool['description']}"
        for word in text.replace(",", " ").replace(".", " ").split():
            token = word.strip("`'\"()")
            if "_" in token and token.islower() and token.replace("_", "").isalpha():
                if token in {"order_number", "product_id", "order_id", "evidence_ref",
                             "evidence_refs", "claims_commerce_facts", "input_schema"}:
                    continue
                assert token in declared, (tool["name"], token)


def test_the_addendum_adds_no_customer_facing_wording():
    """It describes a channel and a fact. It supplies no sentence to send."""
    addendum = pi.PILOT_REPLY_ADDENDUM
    for banned in ("مرحبا", "أهلا", "عذرا", "شكرا", "كيف أقدر أساعدك"):
        assert banned not in addendum, banned


def test_an_empty_base_refuses_rather_than_composing_its_own(monkeypatch):
    monkeypatch.setattr(pi, "COMMERCE_AGENT_INSTRUCTIONS", "   ", raising=True)
    with pytest.raises(ValueError):
        pi.build_pilot_instructions()
