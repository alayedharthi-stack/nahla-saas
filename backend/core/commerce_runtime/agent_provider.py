"""One Anthropic inference step, translated into the loop's closed result union.

The commerce runtime owns the loop. This adapter owns exactly one thing: turning
a single :class:`~core.commerce_runtime.agent_contracts.ProviderRequest` into one
model call and that call's answer into exactly one ``ProviderResult``. It starts
no loop of its own, holds no budget, reserves nothing and never decides to try
again: a failed, refused, truncated or timed-out step is *reported*, and the
loop's durable attempt accounting decides what happens next.

Ownership of the system prompt stays where it already is. The instructions come
verbatim from ``modules.ai.commerce_agent_v2.agent.COMMERCE_AGENT_INSTRUCTIONS``;
this module composes no persona, no greeting and no customer-facing sentence.

Structured reply channel
------------------------
The model answers through one declared tool, ``submit_reply``, rather than free
text, so a reply arrives with its evidence references and its own statement of
whether it asserts commerce facts — the two things the loop's verifier needs and
cannot infer from prose. ``tool_choice`` is therefore ``any``: every step is
either read-tool requests or the reply. Text alongside a reply is ignored; text
without any tool call is reported as invalid output, never delivered.

Transcript
----------
The adapter is stateless *per turn* only in the sense that it holds no
authority: for the invocation it is alive in, it remembers the assistant blocks
it received so the next step can present native ``tool_use``/``tool_result``
pairs. Observations restored from an earlier invocation's checkpoint have no
such transcript, so they are presented as a labelled data block instead, and the
model is told plainly that they were restored.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import reply_choices as rc

logger = logging.getLogger("nahla.commerce_runtime.agent_provider")

REPLY_TOOL_NAME = "submit_reply"

# The reply channel's declaration. Operational, not conversational: it tells the
# model how to hand a finished answer to the platform, and says nothing about
# tone, greeting or wording, which stay with the instructions and the model.
REPLY_TOOL_DESCRIPTION = (
    "Submit the final answer for this customer turn. Call this exactly once, "
    "on its own, when no further lookup is needed. Every commerce fact in the "
    "text must come from a tool result observed in this turn, and every "
    "evidence reference listed must be one those results returned. Optionally "
    "offer the customer a tappable selector over products you looked up this "
    "turn; the text stands on its own either way, and the customer may always "
    "answer by typing instead."
)

REPLY_TOOL_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The answer to send to the customer, in the customer's language.",
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The evidence references from this turn's tool results that support the "
                "text. Required when the text states any commerce fact."
            ),
        },
        "claims_commerce_facts": {
            "type": "boolean",
            "description": (
                "True when the text states a product, price, stock, order, shipment or "
                "merchant fact; false for a purely conversational reply."
            ),
        },
        "choices": {
            "type": "object",
            "description": (
                "Optional. Offer these products as a tappable selector beside the text. "
                "Name products only; their titles, prices and options are taken from the "
                "merchant's own records as this turn read them. Two to ten products, each "
                "looked up in this turn and cited in evidence_refs. Omit this whenever the "
                "text answers on its own \u2014 the selector is never required, and the "
                "customer can always reply by typing."
            ),
            "properties": {
                "product_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "The product ids to offer, in the order the customer should see them."
                    ),
                },
                "button": {
                    "type": "string",
                    "description": (
                        "The word on the button that opens the selector, in the customer's "
                        "language, at most 20 characters."
                    ),
                },
            },
            "required": ["product_ids"],
        },
    },
    "required": ["text", "claims_commerce_facts"],
}

_INVALID_CHOICES: Dict[str, Any] = {"__invalid__": True}


def _requested_choices(raw: Any) -> Dict[str, Any]:
    """What the model asked to offer, carried as a request and nothing more.

    Only the ids and the button word survive translation: whether the selector
    may be offered at all is established later against this turn's
    observations, and every value the customer reads is composed there from the
    merchant's own records. An absent field is the ordinary case and means a
    plain text reply.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        return _INVALID_CHOICES
    ids = raw.get("product_ids")
    if ids is None:
        return {}
    if not isinstance(ids, (list, tuple)):
        return _INVALID_CHOICES
    request: Dict[str, Any] = {"product_ids": [item for item in ids]}
    button = raw.get("button")
    if isinstance(button, str) and button.strip():
        request["button"] = button.strip()[:rc.MAX_BUTTON_LABEL]
    return {rc.REQUESTED_KEY: request}


MAX_OUTPUT_TOKENS = 1024
MIN_STEP_SECONDS = 2.0

# Closed mapping from the provider module's status to how this step is reported.
# ``failure`` is a transport or capacity outcome the loop may see again on a
# later turn; ``invalid`` is output that cannot be used as it stands.
_FAILURE_STATUSES = frozenset({
    "no_api_key", "sdk_unavailable", "auth_error", "rate_limited", "overloaded",
    "timeout", "connection_error", "api_error", "sdk_error",
})


@dataclasses.dataclass(frozen=True)
class StepUsage:
    """What one step reported about its own cost. Absent fields stay absent."""

    step_no: int
    model: Optional[str]
    status: str
    stop_reason: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    request_id: Optional[str]

    @property
    def usage_available(self) -> bool:
        return self.input_tokens is not None or self.output_tokens is not None


def _json_block(label: str, value: Any) -> str:
    return f"<{label}>\n{json.dumps(value, ensure_ascii=False, sort_keys=True)}\n</{label}>"


def _observation_payload(observation: ac.ToolObservation) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "call_id": observation.call_id,
        "tool": observation.tool_name,
        "ok": observation.ok,
        "evidence_refs": list(observation.evidence_refs),
    }
    if observation.ok:
        payload["result"] = observation.result
    else:
        payload["error_code"] = observation.error_code
        payload["error"] = observation.error
    if observation.restored:
        payload["restored_from_earlier_attempt"] = True
    if observation.body_truncated:
        payload["result_body_dropped_by_checkpoint_bound"] = True
    return payload


MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 1200


def _clean_history(history: Optional[Sequence[Mapping[str, Any]]]) -> List[Dict[str, str]]:
    """The prior conversation as bounded, alternating chat turns.

    The platform owns what the earlier turns *were*; this only shapes them into
    messages the model can read. Empty entries are dropped, consecutive turns
    from the same side are merged so the transcript stays alternating, a leading
    assistant turn is dropped because the conversation must open with the
    customer, and only the most recent ``MAX_HISTORY_MESSAGES`` survive.
    """
    cleaned: List[Dict[str, str]] = []
    for entry in list(history or [])[-(MAX_HISTORY_MESSAGES * 2):]:
        if not isinstance(entry, Mapping):
            continue
        role = "assistant" if str(entry.get("role") or "") == "assistant" else "user"
        text = str(entry.get("text") or "").strip()[:MAX_HISTORY_CHARS]
        if not text:
            continue
        if cleaned and cleaned[-1]["role"] == role:
            merged = (cleaned[-1]["text"] + "\n" + text)[:MAX_HISTORY_CHARS]
            cleaned[-1] = {"role": role, "text": merged}
            continue
        cleaned.append({"role": role, "text": text})
    while cleaned and cleaned[0]["role"] == "assistant":
        cleaned.pop(0)
    return cleaned[-MAX_HISTORY_MESSAGES:]


def _inbound_text(inbound: Mapping[str, Any]) -> str:
    for key in ("text", "body", "message"):
        value = inbound.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(dict(inbound), ensure_ascii=False, sort_keys=True)


class AnthropicReasoningProvider:
    """A single-step reasoning provider over the repository's Anthropic integration."""

    def __init__(
        self,
        *,
        instructions: str,
        tools_provider: Any,
        max_tool_requests_per_step: int = 3,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
        audit_context: Optional[Mapping[str, Any]] = None,
        context_preamble: Optional[Mapping[str, Any]] = None,
        history: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        self._instructions = str(instructions or "").strip()
        if not self._instructions:
            raise ValueError("the reasoning provider needs the existing instructions; it composes none")
        self._provider = tools_provider
        self._max_requests = max(1, min(int(max_tool_requests_per_step), ac.MAX_TOOL_REQUESTS_PER_STEP))
        self._max_output_tokens = int(max_output_tokens)
        self._audit_context = dict(audit_context or {})
        self._context_preamble = dict(context_preamble or {})
        self._history = _clean_history(history)
        # Per-invocation transcript: one entry per step, holding the raw
        # assistant blocks, the tool_use ids that step emitted (in order) and
        # whether the step was read requests or the reply channel.
        self._assistant_blocks: List[Tuple[int, List[Dict[str, Any]], List[str], str]] = []
        self.usage: List[StepUsage] = []

    # ── Declared capabilities ────────────────────────────────────────────────

    @property
    def capabilities(self) -> ac.ProviderCapabilities:
        return ac.ProviderCapabilities(
            provider_name="anthropic",
            tool_use=True,
            parallel_tool_use=self._max_requests > 1,
            evidence_refs=True,
            max_tool_requests_per_step=self._max_requests,
        )

    @property
    def total_input_tokens(self) -> Optional[int]:
        values = [u.input_tokens for u in self.usage if u.input_tokens is not None]
        return sum(values) if values else None

    @property
    def total_output_tokens(self) -> Optional[int]:
        values = [u.output_tokens for u in self.usage if u.output_tokens is not None]
        return sum(values) if values else None

    # ── One step ─────────────────────────────────────────────────────────────

    def step(self, request: ac.ProviderRequest) -> ac.ProviderResult:
        tools = [self._tool_declaration(d) for d in request.tools]
        tools.append({
            "name": REPLY_TOOL_NAME,
            "description": REPLY_TOOL_DESCRIPTION,
            "input_schema": ac.public_copy(REPLY_TOOL_SCHEMA),
        })
        messages = self._messages(request)
        wait = max(MIN_STEP_SECONDS, float(request.budget.remaining_seconds))
        answer = self._provider.call_single_step(
            messages=messages,
            system=self._instructions,
            tools=tools,
            tool_choice={"type": "any"},
            max_tokens=self._max_output_tokens,
            timeout_seconds=wait,
            audit_context=dict(self._audit_context, stage=f"agent_loop_step_{request.step_no}"),
        )
        status = str(answer.get("status") or "sdk_error")
        self.usage.append(StepUsage(
            step_no=request.step_no,
            model=answer.get("model"),
            status=status,
            stop_reason=answer.get("stop_reason"),
            input_tokens=(answer.get("usage") or {}).get("input_tokens"),
            output_tokens=(answer.get("usage") or {}).get("output_tokens"),
            request_id=answer.get("request_id"),
        ))
        if status in _FAILURE_STATUSES:
            return ac.ProviderFailure(status)
        if status != "ok":
            return ac.ProviderFailure(f"unexpected_status:{status}")

        blocks = list(answer.get("blocks") or [])
        stop_reason = answer.get("stop_reason")
        if stop_reason == "refusal":
            return ac.ProviderBlocked("model_refusal")
        tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]
        if stop_reason == "max_tokens":
            # A truncated step is not a usable answer even when it carries a
            # complete-looking block: the model was cut off mid-decision.
            return ac.ProviderInvalid("truncated_output")
        if not tool_blocks:
            return ac.ProviderInvalid("no_tool_use_block")

        reply_blocks = [b for b in tool_blocks if b.get("name") == REPLY_TOOL_NAME]
        if reply_blocks and len(tool_blocks) > 1:
            return ac.ProviderInvalid("reply_mixed_with_tool_requests")
        if reply_blocks:
            self._remember(request.step_no, blocks, [str(reply_blocks[0].get("id") or "")], "reply")
            return self._reply(reply_blocks[0])
        return self._requests(request, blocks, tool_blocks)

    # ── Translation ──────────────────────────────────────────────────────────

    def _reply(self, block: Mapping[str, Any]) -> ac.ProviderResult:
        raw = block.get("input")
        if not isinstance(raw, Mapping):
            return ac.ProviderInvalid("reply_arguments_not_an_object")
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            return ac.ProviderInvalid("reply_text_missing")
        refs = raw.get("evidence_refs") or []
        if not isinstance(refs, (list, tuple)):
            return ac.ProviderInvalid("reply_evidence_refs_not_a_list")
        commerce = raw.get("claims_commerce_facts")
        if not isinstance(commerce, bool):
            return ac.ProviderInvalid("reply_commerce_flag_missing")
        payload = _requested_choices(raw.get("choices"))
        if payload is _INVALID_CHOICES:
            return ac.ProviderInvalid("reply_choices_not_an_object")
        return ac.ProviderReply(ac.ReplyDraft(
            text=text.strip(),
            evidence_refs=tuple(str(r) for r in refs),
            claims_commerce_facts=commerce,
            payload=payload,
        ))

    def _requests(self, request: ac.ProviderRequest, blocks: Sequence[Mapping[str, Any]],
                  tool_blocks: Sequence[Mapping[str, Any]]) -> ac.ProviderResult:
        if len(tool_blocks) > self._max_requests:
            # Declared capability is the ceiling; the loop would refuse it, and
            # refusing here names the reason precisely.
            return ac.ProviderInvalid(
                f"tool_requests_exceed_declared_maximum:{len(tool_blocks)}>{self._max_requests}")
        requests: List[ac.ToolRequest] = []
        for block in tool_blocks:
            call_id = str(block.get("id") or "")
            name = str(block.get("name") or "")
            arguments = block.get("input")
            if not call_id or not name:
                return ac.ProviderInvalid("tool_use_block_missing_id_or_name")
            if not isinstance(arguments, Mapping):
                return ac.ProviderInvalid("tool_use_arguments_not_an_object")
            requests.append(ac.ToolRequest(call_id=call_id, tool_name=name,
                                           arguments=ac.public_copy(arguments)))
        self._remember(request.step_no, list(blocks), [r.call_id for r in requests], "tools")
        return ac.ProviderToolRequests(requests=tuple(requests))

    @staticmethod
    def _tool_declaration(definition: ac.ToolDefinition) -> Dict[str, Any]:
        return {
            "name": definition.name,
            "description": definition.description,
            "input_schema": ac.public_copy(definition.input_schema),
        }

    def _remember(self, step_no: int, blocks: List[Dict[str, Any]], call_ids: List[str],
                  kind: str) -> None:
        self._assistant_blocks = [e for e in self._assistant_blocks if e[0] != step_no]
        self._assistant_blocks.append((step_no, blocks, call_ids, kind))
        self._assistant_blocks.sort(key=lambda e: e[0])

    # ── Transcript ───────────────────────────────────────────────────────────

    def _messages(self, request: ac.ProviderRequest) -> List[Dict[str, Any]]:
        by_call = {o.call_id: o for o in request.observations}
        opening: List[Dict[str, Any]] = [{"type": "text", "text": _inbound_text(request.context.inbound)}]
        if self._context_preamble:
            opening.insert(0, {"type": "text",
                               "text": _json_block("conversation_context", self._context_preamble)})
        earlier = list(self._history)
        # The current turn is the customer's. A history ending in a customer turn
        # would put two user messages in a row, so those trailing turns join the
        # current one as earlier blocks of the same message: nothing is invented
        # and nothing is dropped.
        trailing: List[Dict[str, Any]] = []
        while earlier and earlier[-1]["role"] == "user":
            trailing.insert(0, {"type": "text", "text": earlier.pop()["text"]})
        messages: List[Dict[str, Any]] = [
            {"role": entry["role"], "content": [{"type": "text", "text": entry["text"]}]}
            for entry in earlier
        ]
        messages.append({"role": "user", "content": trailing + opening})

        problems_by_step = {f.step_no: f for f in request.feedback}
        replayed: set = set()
        for step_no, blocks, call_ids, kind in self._assistant_blocks:
            if not call_ids:
                continue
            if kind == "reply":
                # A reply that verification refused. The model is shown its own
                # draft and, as that call's result, exactly why it was not
                # accepted — the loop's judgement, not a rewritten answer.
                feedback = problems_by_step.get(step_no)
                if feedback is None:
                    continue
                messages.append({"role": "assistant", "content": blocks})
                messages.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": call_ids[0],
                    "is_error": True,
                    "content": json.dumps(
                        {"accepted": False,
                         "problems": [{"code": p.code, "detail": p.detail} for p in feedback.problems]},
                        ensure_ascii=False, sort_keys=True),
                }]})
                continue
            results = [by_call.get(cid) for cid in call_ids]
            if any(o is None for o in results):
                # A partial tool_result turn is not a valid transcript, so the
                # pair is left out and the data block below carries it.
                continue
            messages.append({"role": "assistant", "content": blocks})
            content: List[Dict[str, Any]] = []
            for observation in results:
                assert observation is not None
                content.append({
                    "type": "tool_result",
                    "tool_use_id": observation.call_id,
                    "is_error": not observation.ok,
                    "content": json.dumps(_observation_payload(observation),
                                          ensure_ascii=False, sort_keys=True),
                })
                replayed.add(observation.call_id)
            messages.append({"role": "user", "content": content})

        trailing: List[Dict[str, Any]] = []
        leftover = [o for o in request.observations if o.call_id not in replayed]
        if leftover:
            trailing.append({"type": "text", "text": _json_block(
                "earlier_tool_observations", [_observation_payload(o) for o in leftover])})
        unshown = [f for f in request.feedback
                   if f.step_no not in {e[0] for e in self._assistant_blocks if e[3] == "reply"}]
        if unshown:
            trailing.append({"type": "text", "text": _json_block("verification_problems", [
                {"step": f.step_no, "problems": [{"code": p.code, "detail": p.detail} for p in f.problems]}
                for f in unshown
            ])})
        if trailing:
            messages.append({"role": "user", "content": trailing})
        return messages


__all__ = [
    "AnthropicReasoningProvider", "MAX_HISTORY_CHARS", "MAX_HISTORY_MESSAGES", "MAX_OUTPUT_TOKENS", "REPLY_TOOL_DESCRIPTION", "REPLY_TOOL_NAME",
    "REPLY_TOOL_SCHEMA", "StepUsage",
]
