"""The message-intent experiment through the real runtime entry, on PostgreSQL.

What is proved: a reading that is not a store request reaches the model as data
beside the turn, with the store's read tools not offered; a store request, a
failed check or no check leaves the turn exactly as it was; a tool the turn did
not offer reads nothing even if the model asks for it. The reply is the
model's own in every case. No model and no network: the Anthropic HTTP call is
the same scripted double the pilot tests use.

Requires ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import pytest

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import delivery_dispatch as dd
from core.commerce_runtime import message_intent as mi
from core.commerce_runtime import runtime_entry as entry
from tests.commerce_reliability.test_commerce_runtime_pilot_pg import (  # noqa: F401 (fixtures)
    MODEL,
    PHONE,
    ScriptedAnthropic,
    Transport,
    _fresh_probe,
    accepted,
    pilot,
    reply,
    step,
    tool_use,
)

SOCIAL = "ابداع روعه"


def assessed(decision: Optional[str], status: str = "ok") -> mi.IntentAssessment:
    return mi.IntentAssessment(decision=decision if status == "ok" else None, status=status,
                               model="check-model", latency_ms=5)


def run(pilot, answers: List[Dict[str, Any]], *, intent: Optional[mi.IntentAssessment],
        question: str = SOCIAL) -> Any:
    model = ScriptedAnthropic(answers)
    report = entry.run_commerce_runtime_turn(
        engine=pilot.engine, session_factory=pilot.session_factory, tenant_id=pilot.tenant_a,
        conversation_id=pilot.conversation_id, connection_ref=f"wa:{pilot.connection_id}",
        connection_id=str(pilot.connection_id), customer_id=pilot.customer_id,
        normalized_customer_phone=PHONE, provider_message_id="wamid." + uuid.uuid4().hex,
        inbound_text=question, inbound_metadata={"source": "test"},
        transport=Transport([accepted("wamid." + uuid.uuid4().hex[:8])]),
        instructions="EXISTING-INSTRUCTIONS", model=MODEL,
        budget=ac.LoopBudget(max_steps=3, max_tool_calls=4, tool_timeout_seconds=10.0,
                             provider_timeout_seconds=15.0, deadline_seconds=45.0),
        anthropic_provider=model, message_intent=intent)
    return report, model.calls


def offered(call: Dict[str, Any]) -> List[str]:
    return [tool["name"] for tool in call["tools"]]


def context_block(call: Dict[str, Any]) -> Dict[str, Any]:
    opening = call["messages"][-1]["content"] if call["messages"][-1]["role"] == "user" else []
    for block in call["messages"][0]["content"] + opening:
        text = str(block.get("text") or "")
        if "conversation_context" in text:
            return json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
    return {}


@pytest.mark.parametrize("decision", [mi.NON_COMMERCIAL, mi.AMBIGUOUS])
def test_not_a_store_request_is_answered_by_the_model_without_store_tools(pilot, decision):
    report, calls = run(pilot, [step([reply("رد النموذج نفسه", call_id="r1")])],
                        intent=assessed(decision))
    assert offered(calls[0]) == [ap.REPLY_TOOL_NAME]
    facts = context_block(calls[0])
    assert facts[mi.READING_KEY] == mi.READINGS[decision]
    assert facts[mi.TOOLS_OFFERED_KEY] is False
    assert calls[0]["system"] == "EXISTING-INSTRUCTIONS"          # the instructions are untouched
    assert report.store_tools_withheld is True
    assert report.dispatch_status == dd.SENT_ACCEPTED
    assert report.reply_text == "رد النموذج نفسه"                  # the model's words, as written


@pytest.mark.parametrize("intent", [None, assessed(mi.STORE_REQUEST), assessed(None, "api_error")])
def test_a_store_request_or_no_reading_is_the_turn_exactly_as_before(pilot, intent):
    report, calls = run(pilot, [step([reply("رد", call_id="r1")])], intent=intent)
    assert "search_products" in offered(calls[0]) and ap.REPLY_TOOL_NAME in offered(calls[0])
    facts = context_block(calls[0])
    assert mi.READING_KEY not in facts and mi.TOOLS_OFFERED_KEY not in facts
    assert report.store_tools_withheld is False
    assert report.dispatch_status == dd.SENT_ACCEPTED


def test_a_tool_the_turn_did_not_offer_reads_nothing_even_when_asked_for(pilot):
    ref = f"catalog:product:{pilot.product_id}"
    report, calls = run(pilot, [step([tool_use("t1", "search_products", query="روعه")]),
                                step([reply("رد", call_id="r1")])],
                        intent=assessed(mi.NON_COMMERCIAL))
    assert ref not in report.evidence_refs
    assert report.store_tools_withheld is True
    assert report.dispatch_status == dd.SENT_ACCEPTED
    assert offered(calls[1]) == [ap.REPLY_TOOL_NAME]
