"""A reply citing a non-reference, through the real runtime entry and adapter.

Tenant 33 turns 99 and 100 (2026-09-27): the model's first step was a reply
whose evidence_refs held values that are not references, the shape check ended
the turn, and the customer got nothing. Here the whole path runs on real
PostgreSQL with the real Anthropic adapter: the refused reply is shown back to
the model as its own call's result, the model looks the product up and answers,
and the corrected reply is what is sent. Only the HTTP call and the WhatsApp
transport are doubles.

Requires ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``.
"""
from __future__ import annotations

import json
import uuid

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import delivery_dispatch as dd
from core.commerce_runtime import runtime_entry as entry
from tests.commerce_reliability.test_commerce_runtime_pilot_pg import (  # noqa: F401 (fixtures)
    MODEL,
    PHONE,
    QUESTION,
    ScriptedAnthropic,
    Transport,
    _fresh_probe,
    accepted,
    pilot,
    reply,
    step,
    tool_use,
)


class Recording(ScriptedAnthropic):
    def call_single_step(self, **kwargs):
        self.seen = getattr(self, "seen", []) + [kwargs]
        return super().call_single_step(**kwargs)


def test_a_bare_id_in_evidence_refs_is_shown_back_and_the_corrected_reply_is_sent(pilot):
    ref = f"catalog:product:{pilot.product_id}"
    transport = Transport([accepted("wamid.CORRECTED")])
    answers = [
        step([reply("متوفر", refs=(str(pilot.product_id),), commerce=True, call_id="r1")]),
        step([tool_use("t1", "search_products", query="حذاء")]),
        step([reply("متوفر", refs=(ref,), commerce=True, call_id="r2")]),
    ]
    model = Recording(answers)
    report = entry.run_commerce_runtime_turn(
        engine=pilot.engine, session_factory=pilot.session_factory, tenant_id=pilot.tenant_a,
        conversation_id=pilot.conversation_id, connection_ref=f"wa:{pilot.connection_id}",
        connection_id=str(pilot.connection_id), customer_id=pilot.customer_id,
        normalized_customer_phone=PHONE, provider_message_id="wamid." + uuid.uuid4().hex,
        inbound_text=QUESTION, inbound_metadata={"source": "test"}, transport=transport,
        instructions="EXISTING-INSTRUCTIONS", model=MODEL,
        budget=ac.LoopBudget(max_steps=4, max_tool_calls=4, tool_timeout_seconds=10.0,
                             provider_timeout_seconds=15.0, deadline_seconds=45.0),
        anthropic_provider=model)
    model_calls = model.seen

    assert report.dispatch_status == dd.SENT_ACCEPTED
    assert transport.sent[0]["text"] == "متوفر"
    assert ref in report.evidence_refs
    # The second model call shows the refused reply back as that call's result.
    shown = [block for message in model_calls[1]["messages"] if message["role"] == "user"
             for block in message["content"] if isinstance(block, dict)
             and block.get("type") == "tool_result" and block.get("tool_use_id") == "r1"]
    assert len(shown) == 1 and shown[0]["is_error"] is True
    problems = json.loads(shown[0]["content"])["problems"]
    assert [p["code"] for p in problems] == [ac.MALFORMED_EVIDENCE]
    assert f'"{pilot.product_id}"' in problems[0]["detail"]
    assert model_calls[1]["system"] == "EXISTING-INSTRUCTIONS"       # instructions untouched
