"""The message-intent check: one bounded question, one decision, never a reply.

The provider is a recording double; these tests prove what the check sends and
how it reads the answer back, never what a real model decides.
"""
from __future__ import annotations

import json
import re

import pytest

from core.commerce_runtime import message_intent as mi

TITLES = ["حذاء رياضي أبيض", "قميص قطني أزرق", "عطر ورد 100ml"]


class Recording:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def call_single_step(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.result


def ok(decision, reason="r"):
    return {"status": "ok", "blocks": [{"type": "tool_use", "id": "t1", "name": mi.TOOL_NAME,
                                        "input": {"decision": decision, "reason": reason}}],
            "usage": {"input_tokens": 900, "output_tokens": 40}}


def run(provider, message="نص", history=()):
    return mi.assess(message=message, history=list(history), store_name="متجر تجريبي عام",
                     product_titles=TITLES, provider=provider, model="configured-test-model",
                     audit_context={"tenant_id": 7})


@pytest.mark.parametrize("decision", mi.DECISIONS)
def test_each_decision_is_read_back_with_its_usage(decision):
    result = run(Recording(ok(decision)))
    assert result.status == "ok" and result.decision == decision
    assert (result.input_tokens, result.output_tokens) == (900, 40)


def test_the_call_is_one_forced_tool_step_with_the_checks_own_instructions():
    provider = Recording(ok(mi.AMBIGUOUS))
    run(provider)
    call = provider.calls[0]
    assert call["system"] == mi.INSTRUCTIONS
    assert call["tools"] == [mi.TOOL]
    assert call["tool_choice"] == {"type": "tool", "name": mi.TOOL_NAME}
    assert call["audit_context"]["model"] == "configured-test-model"
    assert call["audit_context"]["tenant_id"] == 7
    assert call["max_tokens"] == mi.MAX_OUTPUT_TOKENS


@pytest.mark.parametrize("result", [
    {"status": "api_error", "blocks": []},
    {"status": "ok", "blocks": [{"type": "text", "text": "store_request"}]},
    {"status": "ok", "blocks": [{"type": "tool_use", "name": mi.TOOL_NAME,
                                 "input": {"decision": "maybe"}}]},
    {"status": "ok", "blocks": [{"type": "tool_use", "name": mi.TOOL_NAME, "input": None}]},
    None,
])
def test_anything_but_a_valid_decision_is_no_decision(result):
    assessment = run(Recording(result))
    assert assessment.decision is None and assessment.status != "ok"


def test_a_provider_that_raises_is_no_decision_not_a_crash():
    assessment = run(Recording(exc=RuntimeError("boom")))
    assert assessment.decision is None and assessment.status == "error"


def test_an_empty_message_is_not_sent_to_the_model():
    provider = Recording(ok(mi.STORE_REQUEST))
    assessment = run(provider, message="   ")
    assert provider.calls == [] and assessment.decision is None


def test_the_view_is_bounded_and_says_who_wrote_each_turn():
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"t{i}"} for i in range(10)]
    payload = json.loads(mi.build_input(message="m", history=history, store_name="s",
                                        product_titles=TITLES + TITLES))
    assert [t["text"] for t in payload["conversation"]] == [f"t{i}" for i in range(4, 10)]
    assert payload["conversation"][0]["from"] == "customer"
    assert payload["conversation"][1]["from"] == "store"
    assert payload["store"]["product_titles"] == TITLES
    assert payload["store"]["product_titles_complete"] is True
    many = [f"p{i}" for i in range(mi.MAX_PRODUCT_TITLES + 5)]
    payload = json.loads(mi.build_input(message="m", history=[], store_name="s", product_titles=many))
    assert len(payload["store"]["product_titles"]) == mi.MAX_PRODUCT_TITLES
    assert payload["store"]["product_titles_complete"] is False


def test_the_instructions_carry_no_customer_phrase():
    """The check judges meaning; it holds no phrase, keyword or example message.
    Its instructions contain no Arabic text at all, so none can hide there."""
    assert not re.search(r"[؀-ۿ]", mi.INSTRUCTIONS)
    assert not re.search(r"[؀-ۿ]", json.dumps(mi.TOOL))
