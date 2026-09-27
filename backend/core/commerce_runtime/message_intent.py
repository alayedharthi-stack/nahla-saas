"""An independent reading of what an incoming message means, before the store agent runs.

Experiment — off-send evaluation only. The pilot seam calls it only when
``COMMERCE_RUNTIME_INTENT_CHECK=1``, which no environment sets.

The store agent is a sales assistant whose first move is a catalogue search.
When a message sent to the store's number is not a store request at all — a
personal message to the merchant, social chat — that framing turns the words
into a product lookup, and an honest empty search becomes "we have no product
by that name". This module asks one separate, bounded question first: what does
the sender most likely mean? The answer is one of three internal decisions.

* ``store_request`` — the sender is asking the store, as a store, about
  something a store answers (a product or kind of product, prices, stock,
  orders, delivery, payment, services).
* ``non_commercial`` — clearly not addressed to the store as a shop.
* ``ambiguous`` — both readings are reasonable and nothing settles it.

What this is not. It composes no customer-facing text, carries no keyword,
phrase or sentence-shape rule, and edits nothing the agent writes. The decision
is data for the platform; the reply — including any clarifying question — stays
the agent's own. It reads only the message, the recent conversation and the
store's own product titles: no contact lists and no customer profile.

A check that cannot run answers ``decision=None`` with its status, and a caller
treats that exactly as if the check did not exist.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger("nahla.commerce_runtime.message_intent")

STORE_REQUEST = "store_request"
NON_COMMERCIAL = "non_commercial"
AMBIGUOUS = "ambiguous"
DECISIONS = (STORE_REQUEST, NON_COMMERCIAL, AMBIGUOUS)

TOOL_NAME = "record_message_intent"
MAX_HISTORY_TURNS = 6
MAX_PRODUCT_TITLES = 80
MAX_TEXT_CHARS = 600
MAX_OUTPUT_TOKENS = 300
DEFAULT_TIMEOUT_SECONDS = 8.0

INSTRUCTIONS = """
You read one incoming WhatsApp message sent to an online store's WhatsApp number, before the store's assistant answers it. You do not answer the message. You only judge what the sender most likely means, and record that with the record_message_intent tool.

You are given the store's name, titles of products the store sells, the recent conversation (oldest first; "customer" is the sender, "store" is what the store's number sent), and the new message.

Choose one decision:
- store_request: the sender is asking the store, as a store, about something a store answers: a product or a kind of product (whether or not this store has it), prices, stock, sizes or colours, orders and delivery, payment, returns, or the store's services.
- non_commercial: the sender is clearly not addressing the store as a shop: personal or family matters, social chat, a message meant for a particular person, or a greeting, compliment or thanks with no request in it.
- ambiguous: both readings are reasonable and neither the message nor the conversation settles it.

Judge the meaning, not the grammatical form: the same form of question can be about a product or about a person. The product titles are evidence of what this store sells; a name or phrase that is neither one of them nor a kind of product is not, by itself, a product request. Use the conversation: earlier messages can make a short message clearly a store request or clearly personal. If the message leaves both readings open, choose ambiguous rather than guessing.
""".strip()

TOOL: Dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Record what the sender of the new message most likely means.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": list(DECISIONS)},
            "reason": {"type": "string",
                       "description": "One short sentence for the platform's own log."},
        },
        "required": ["decision", "reason"],
        "additionalProperties": False,
    },
}


@dataclasses.dataclass(frozen=True)
class IntentAssessment:
    """One check's outcome. ``decision`` is None whenever ``status`` is not ``ok``."""

    decision: Optional[str]
    status: str
    model: str
    latency_ms: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reason: str = ""

    def as_log_fields(self) -> Dict[str, Any]:
        return {"intent_decision": self.decision, "intent_status": self.status,
                "intent_model": self.model, "intent_latency_ms": self.latency_ms,
                "intent_input_tokens": self.input_tokens,
                "intent_output_tokens": self.output_tokens}


# What the turn does with a decision (stage 2 of the experiment). Only a check
# that ran and did *not* read a store request changes anything: the store's
# read tools are not offered for that one message, and the agent is told so,
# as data, beside the reading itself. A store request, a failed check or no
# check leaves the turn exactly as it was. The reply is the agent's own either
# way; nothing here is wording.
READING_KEY = "new_message_reading"
TOOLS_OFFERED_KEY = "store_tools_offered_for_this_message"
READINGS = {
    NON_COMMERCIAL: "not_addressed_to_the_store_as_a_shop",
    AMBIGUOUS: "unclear_whether_addressed_to_the_store_as_a_shop",
}


def withholds_store_tools(assessment: Optional["IntentAssessment"]) -> bool:
    return (assessment is not None and assessment.status == "ok"
            and assessment.decision in READINGS)


def context_facts(assessment: Optional["IntentAssessment"]) -> Dict[str, Any]:
    if not withholds_store_tools(assessment):
        return {}
    return {READING_KEY: READINGS[assessment.decision], TOOLS_OFFERED_KEY: False}


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit]


def build_input(*, message: str, history: Sequence[Mapping[str, Any]], store_name: str,
                product_titles: Sequence[str]) -> str:
    """The check's whole view of the turn, as one data block.

    ``history`` uses the runtime's own turn shape (``role`` user/assistant,
    ``text``); only the last few turns are kept. Product titles are distinct and
    bounded, and the block says when the list is partial.
    """
    turns: List[Dict[str, str]] = []
    for turn in list(history or [])[-MAX_HISTORY_TURNS:]:
        text = _clip(turn.get("text"), MAX_TEXT_CHARS)
        if text:
            turns.append({"from": "customer" if turn.get("role") == "user" else "store",
                          "text": text})
    titles = list(dict.fromkeys(_clip(t, 120) for t in product_titles or () if str(t or "").strip()))
    payload = {
        "store": {"name": _clip(store_name, 120),
                  "product_titles": titles[:MAX_PRODUCT_TITLES],
                  "product_titles_complete": len(titles) <= MAX_PRODUCT_TITLES},
        "conversation": turns,
        "new_message": _clip(message, MAX_TEXT_CHARS),
    }
    return json.dumps(payload, ensure_ascii=False)


def assess(*, message: str, history: Sequence[Mapping[str, Any]], store_name: str,
           product_titles: Sequence[str], provider: Any, model: str,
           audit_context: Optional[Mapping[str, Any]] = None,
           timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> IntentAssessment:
    """Ask the model the one question and read back one decision. Never raises."""
    started = time.monotonic()

    def done(status: str, decision: Optional[str] = None, reason: str = "",
             usage: Optional[Mapping[str, Any]] = None) -> IntentAssessment:
        usage = usage or {}
        return IntentAssessment(
            decision=decision if status == "ok" else None, status=status, model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            reason=_clip(reason, 300))

    if not str(message or "").strip():
        return done("empty_message")
    audit = dict(audit_context or {})
    audit.update({"model": model, "reason": "message_intent_check", "stage": "intent_check"})
    try:
        result = provider.call_single_step(
            messages=[{"role": "user", "content": build_input(
                message=message, history=history, store_name=store_name,
                product_titles=product_titles)}],
            system=INSTRUCTIONS, tools=[TOOL],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            max_tokens=MAX_OUTPUT_TOKENS, timeout_seconds=timeout_seconds,
            audit_context=audit)
    except Exception as exc:  # noqa: BLE001 - the check is advisory; it never breaks a turn
        logger.warning("[MESSAGE_INTENT] check raised error=%s", type(exc).__name__)
        return done("error")
    result = result if isinstance(result, Mapping) else {}
    usage = result.get("usage") if isinstance(result.get("usage"), Mapping) else None
    if result.get("status") != "ok":
        return done(str(result.get("status") or "error"), usage=usage)
    for block in result.get("blocks") or []:
        if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
            payload = block.get("input") if isinstance(block.get("input"), Mapping) else {}
            decision = payload.get("decision")
            if decision in DECISIONS:
                return done("ok", decision, str(payload.get("reason") or ""), usage)
            return done("invalid_output", usage=usage)
    return done("no_decision", usage=usage)
