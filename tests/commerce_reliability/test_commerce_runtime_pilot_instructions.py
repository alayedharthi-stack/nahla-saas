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
    # Reads the owner approved and the instructions deliberately never name, so
    # when to use them stays the model's call rather than a step it is ordered
    # through.
    assert alt.PILOT_ONLY_TOOL_NAMES == ("get_customer_addresses",
                                         "list_shareable_promotions")


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


def test_the_selector_is_described_on_the_tool_itself_with_its_one_stated_use():
    """``choices`` is deliberately absent from the addendum; it is described
    where every tool interface is described — on the declaration the model
    receives. It stays optional in the schema (a reply about one product, a
    recommendation or a question offers none), and by the owner's approval of
    26 September 2026 the declaration states the one use: an answer that
    offers two or more products this turn's search returned to choose from.
    The products must still have been looked up in this turn, and the customer
    can always answer by typing.
    """
    reply = next(t for t in declared_tools() if t["name"] == ap.REPLY_TOOL_NAME)
    schema = reply["input_schema"]
    assert "choices" not in schema["required"]
    assert "choices" not in pi.PILOT_REPLY_ADDENDUM
    description = schema["properties"]["choices"]["description"]
    assert "this turn" in description and "evidence_refs" in description
    assert "two or more products" in description and "optional" in description
    assert "typing" in description


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
                             "evidence_refs", "claims_commerce_facts", "input_schema",
                             "more_results", "exclude_shown"}:
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


def test_the_owner_approved_clauses_name_the_settings_and_the_shapes_without_a_field():
    """Clauses 3 and 4 (owner-approved, 25 September 2026; clause 3 extended to
    the Arabic-dialect setting on 26 September 2026). They point at the
    context the platform delivers and at what a list row and a card already
    show. They name no optional reply field (the guard above), and no sentence
    to send (the guard below)."""
    addendum = pi.PILOT_REPLY_ADDENDUM
    for key in ("reply_language", "reply_dialect", "reply_tone", "conversation_context"):
        assert key in addendum, key
    assert "choices" not in addendum and "card" not in addendum
    for shown in ("اسم المنتج", "مختصرًا", "سعره", "بعض خياراته", "صورته", "صفحته",
                  "لا يتسع له الصف"):
        assert shown in addendum, shown
    assert "إلا إذا طلب العميل الرابط" in addendum


def _clause(number: int) -> str:
    """One numbered clause of the addendum, whitespace-normalised."""
    addendum = pi.PILOT_REPLY_ADDENDUM
    start = addendum.index(f"\n{number}) ")
    end = addendum.find(f"\n{number + 1}) ", start)
    return " ".join(addendum[start:end if end != -1 else None].split())


def test_clause_3_makes_the_current_settings_the_reference_over_earlier_replies():
    """Clause 3, extended on 26 September 2026 (owner-approved) for the separate
    Arabic-dialect setting. It points at the three settings the platform
    delivers, makes the merchant's current values the reference over earlier
    replies (not over what the customer asks for now), keeps the language as the sole judge of Arabic or English,
    and applies the dialect to Arabic replies only."""
    clause = _clause(3)
    for key in ("reply_language", "reply_dialect", "reply_tone", "conversation_context"):
        assert key in clause, key
    # The current settings are the reference; earlier replies are not.
    assert "الحالية" in clause and "وحدها" not in clause
    assert "ردود سابقة" in clause and "لا تأخذ اللغة أو اللهجة" in clause
    # The language decides Arabic or English; the dialect never changes that.
    assert "reply_language يحدد متى تردّ بالعربية ومتى بالإنجليزية" in clause
    assert "اللهجة لا تغيّر لغة الرد" in clause
    # The dialect governs every Arabic reply; Saudi applies only without one.
    assert "إذا وُجد reply_dialect فاتبعه في كل رد عربي" in clause
    assert clause.index("وإن لم يوجد") < clause.index("السعودية")


def test_clause_3_names_no_other_dialect_and_carries_no_dialect_meaning():
    """What each dialect means is data the platform delivers per merchant
    (``core.reply_dialect``), never text written into the shared instructions."""
    from core.reply_dialect import ARABIC_DIALECT_MEANING  # noqa: PLC0415

    addendum = pi.PILOT_REPLY_ADDENDUM
    for meaning in ARABIC_DIALECT_MEANING.values():
        assert meaning not in addendum
    for other in ("العراقية", "المصرية", "الشامية", "الفصحى"):
        assert other not in addendum, other


def test_clause_5_covers_both_cases_and_names_no_phrase():
    """Clause 5 (owner-approved, 26 September 2026, after tenant 33 turn 68).
    One general rule for a message that is not clearly a product request: a
    clear product request is searched and answered without asking; a message
    that may mean something else is clarified rather than treated as a product
    request; and an empty search for such a phrase says only that the text
    matched no product. It quotes no customer phrase — no word list — and
    supplies no reply."""
    clause = _clause(5)
    assert "في سياق المحادثة قبل أن تبحث" in clause
    assert "فابحث وأجب مباشرة دون أن تستوضح" in clause
    assert "فاستوضح قصده" in clause and "ولا تعاملها كطلب منتج" in clause
    assert "لا أن العميل سأل عن منتج غير موجود" in clause
    assert "بدل أن تجزم بعدم وجوده" in clause
    for quote in ("«", "»", '"'):
        assert quote not in clause, quote
