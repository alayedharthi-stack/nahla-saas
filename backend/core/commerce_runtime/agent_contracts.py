"""Contracts of the dormant agent loop core (pure; no database, no network).

The loop owns the control flow: it accepts one eligible turn, asks a
*reasoning provider* for one inference step at a time, executes only the
allowlisted read-only tools the provider asked for, feeds the observations
back, verifies the reply draft against the evidence gathered in this turn and
hands an accepted reply to the delivery ledger. The provider represents one
inference step; it owns neither the loop, nor the tools, nor the state, nor
the deadlines, nor delivery.

Everything here is typed and validated fail-closed. Model tool-call ids are
kept for request/observation correlation only: they never become business
idempotency keys, evidence references or authorization credentials.
"""
from __future__ import annotations

import dataclasses
import enum
import json
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc

MAX_TOOL_NAME_LENGTH = 64
MAX_CALL_ID_LENGTH = 128
MAX_EVIDENCE_REF_LENGTH = 128
MAX_REPLY_TEXT_LENGTH = 4000
MAX_TOOL_REQUESTS_PER_STEP = 8
MAX_ARGUMENTS_BYTES = 8 * 1024
MAX_OBSERVATION_BYTES = 16 * 1024
AGENT_LOOP_STATE_KEY = "agent_loop"        # reserved key inside the conversation's versioned state payload
AGENT_LOOP_STATE_VERSION = 1

_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVIDENCE_REF_RE = re.compile(r"^[a-z][a-z0-9_]*:[A-Za-z0-9_.:-]{1,100}$")

# Argument keys a model may never supply: scope comes from the trusted runtime
# context only. Their presence is a refused request, not an ignored field.
RESERVED_SCOPE_ARGUMENTS = frozenset({
    "tenant_id", "tenant", "namespace", "conversation_id", "conversation", "turn_id", "merchant_id", "store_id",
    "owner_id", "token", "fence", "epoch",
})


# ── Errors ───────────────────────────────────────────────────────────────────


class AgentLoopError(c.CommerceRuntimeError):
    """Base class of the loop's own errors."""


class ToolError(AgentLoopError):
    """A tool refused or failed a request. ``code`` is a closed vocabulary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


# ── Closed vocabularies ──────────────────────────────────────────────────────


class ToolErrorCode(str, enum.Enum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    SCOPE_OVERRIDE_REFUSED = "scope_override_refused"
    NOT_READ_ONLY = "not_read_only"
    TIMEOUT = "timeout"
    TOOL_FAILURE = "tool_failure"
    RESULT_TOO_LARGE = "result_too_large"


class StopReason(str, enum.Enum):
    TURN_NOT_ELIGIBLE = "turn_not_eligible"
    TURN_COMPLETED = "turn_completed"
    OWNERSHIP_LOST = "ownership_lost"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_BLOCKED = "provider_blocked"
    PROVIDER_INVALID = "provider_invalid"
    PROVIDER_TIMEOUT = "provider_timeout"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    VERIFICATION_FAILED = "verification_failed"
    REPEATED_TOOL_REQUEST = "repeated_tool_request"
    TOOL_REFUSED = "tool_refused"


class LoopStatus(str, enum.Enum):
    PENDING_DELIVERY = "pending_delivery"    # a durable delivery intent exists for the turn; nothing was sent
    STOPPED = "stopped"                      # no reply was accepted; ``stop_reason`` says why


class LoopPhase(str, enum.Enum):
    REASONING = "reasoning"
    REPLY_PENDING_DELIVERY = "reply_pending_delivery"
    STOPPED = "stopped"


# ── Provider boundary ────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider declares it can do. The loop never assumes more."""

    provider_name: str
    tool_use: bool = True                    # may request tools
    parallel_tool_use: bool = False          # may request several tools in one step
    evidence_refs: bool = True               # can cite evidence references in a draft
    max_tool_requests_per_step: int = 1


@dataclasses.dataclass(frozen=True)
class ToolDefinition:
    """A tool as presented to the provider. ``read_only`` is asserted by the registry."""

    name: str
    description: str
    input_schema: Mapping[str, Any]          # JSON-schema-like: {"type": "object", "properties": {...}, "required": [...]}
    result_kind: str                         # closed, informational: "product_list" | "product" | "knowledge_entry"
    read_only: bool = True


@dataclasses.dataclass(frozen=True)
class ToolRequest:
    call_id: str                             # provider correlation id only
    tool_name: str
    arguments: Mapping[str, Any]


@dataclasses.dataclass(frozen=True)
class ToolObservation:
    """What the loop tells the provider about one tool request. Data, never instructions."""

    call_id: str
    tool_name: str
    ok: bool
    result: Optional[Mapping[str, Any]]
    error_code: Optional[str]
    error: Optional[str]
    evidence_refs: Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class VerificationProblem:
    code: str                                # closed: "unknown_evidence" | "empty_text" | "text_too_long" | "missing_evidence" | "invalid_kind"
    detail: str


@dataclasses.dataclass(frozen=True)
class VerificationFeedback:
    """Correctable verification failure of a previous draft, fed back to the provider."""

    step_no: int
    problems: Tuple[VerificationProblem, ...]


@dataclasses.dataclass(frozen=True)
class BudgetView:
    """What the provider may know about the remaining execution budget."""

    remaining_steps: int
    remaining_tool_calls: int
    remaining_seconds: float


@dataclasses.dataclass(frozen=True)
class AuthorizedContext:
    """Trusted runtime context. The provider receives it; it can never change it."""

    tenant_id: int
    namespace: str
    conversation_id: int
    turn_id: int
    inbound: Mapping[str, Any]               # the customer turn payload as admitted (data)
    state_payload: Mapping[str, Any]         # the conversation's versioned state at loop start (data)


@dataclasses.dataclass(frozen=True)
class ProviderRequest:
    step_no: int
    context: AuthorizedContext
    tools: Tuple[ToolDefinition, ...]
    observations: Tuple[ToolObservation, ...]
    feedback: Tuple[VerificationFeedback, ...]
    budget: BudgetView


@dataclasses.dataclass(frozen=True)
class ReplyDraft:
    text: str
    kind: str = lc.DeliveryKind.TEXT.value
    evidence_refs: Tuple[str, ...] = ()
    claims_commerce_facts: bool = False      # set by the provider when the text states catalog/merchant facts
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)


class ProviderResult:
    """Tagged union base. Exactly one of the subclasses below is returned per step."""


@dataclasses.dataclass(frozen=True)
class ProviderToolRequests(ProviderResult):
    requests: Tuple[ToolRequest, ...]


@dataclasses.dataclass(frozen=True)
class ProviderReply(ProviderResult):
    draft: ReplyDraft


@dataclasses.dataclass(frozen=True)
class ProviderFailure(ProviderResult):
    """The provider could not produce a step (transport error, quota, internal failure)."""

    reason: str


@dataclasses.dataclass(frozen=True)
class ProviderBlocked(ProviderResult):
    """The provider declined to answer (policy refusal). Not retried by the loop."""

    reason: str


@dataclasses.dataclass(frozen=True)
class ProviderInvalid(ProviderResult):
    """Incomplete or malformed output (truncation, unparsable structure). Not a usable reply."""

    reason: str


# ── Loop configuration, persisted progress and outcome ───────────────────────


@dataclasses.dataclass(frozen=True)
class LoopBudget:
    max_steps: int = 4
    max_tool_calls: int = 6
    tool_timeout_seconds: float = 5.0
    deadline_seconds: float = 60.0


@dataclasses.dataclass(frozen=True)
class LoopProgress:
    """The part of the loop's progress that survives re-entry, kept in the
    conversation's versioned state under ``AGENT_LOOP_STATE_KEY``."""

    turn_id: int
    phase: str
    steps_used: int
    tool_calls_used: int
    elapsed_seconds: float
    # The turn's delivery sequence is found by turn id (unique per turn); it is
    # not copied here, so this payload can never disagree with the ledger.
    stop_reason: Optional[str] = None

    def to_payload(self, budget: LoopBudget) -> Dict[str, Any]:
        return {
            "version": AGENT_LOOP_STATE_VERSION, "turn_id": self.turn_id, "phase": self.phase,
            "steps_used": self.steps_used, "tool_calls_used": self.tool_calls_used,
            "elapsed_seconds": round(self.elapsed_seconds, 3), "stop_reason": self.stop_reason,
            "budget": {"max_steps": budget.max_steps, "max_tool_calls": budget.max_tool_calls,
                       "deadline_seconds": budget.deadline_seconds},
        }

    @classmethod
    def from_payload(cls, raw: Any, *, turn_id: int) -> Optional["LoopProgress"]:
        """The persisted progress of *this* turn, or None when absent or for another turn."""
        if not isinstance(raw, Mapping) or raw.get("version") != AGENT_LOOP_STATE_VERSION:
            return None
        if raw.get("turn_id") != turn_id:
            return None
        try:
            return cls(
                turn_id=turn_id, phase=str(raw["phase"]), steps_used=int(raw["steps_used"]),
                tool_calls_used=int(raw["tool_calls_used"]), elapsed_seconds=float(raw["elapsed_seconds"]),
                stop_reason=raw.get("stop_reason"),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclasses.dataclass(frozen=True)
class LoopEvent:
    step_no: int
    kind: str
    detail: Mapping[str, Any]


@dataclasses.dataclass(frozen=True)
class LoopOutcome:
    status: str                              # LoopStatus
    stop_reason: Optional[str]               # StopReason when stopped
    turn_id: int
    delivery_sequence_id: Optional[int]
    reused_delivery: bool
    state_revision: Optional[int]
    steps_used: int
    tool_calls_used: int
    events: Tuple[LoopEvent, ...]
    detail: Mapping[str, Any] = dataclasses.field(default_factory=dict)


# ── Validation ───────────────────────────────────────────────────────────────


def _canonical_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def validate_call_id(value: Any) -> str:
    if not isinstance(value, str) or not _CALL_ID_RE.match(value):
        raise c.ValidationError("call_id must be a short opaque correlation id")
    return value


def validate_tool_name(value: Any) -> str:
    if not isinstance(value, str) or not _TOOL_NAME_RE.match(value):
        raise c.ValidationError("tool_name must be a lowercase identifier")
    return value


def validate_evidence_ref(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_EVIDENCE_REF_LENGTH or not _EVIDENCE_REF_RE.match(value):
        raise c.ValidationError("evidence reference must look like '<kind>:<id>'")
    return value


def validate_tool_request(value: Any) -> ToolRequest:
    if not isinstance(value, ToolRequest):
        raise c.ValidationError("tool request must be a ToolRequest")
    if not isinstance(value.arguments, Mapping):
        raise c.ValidationError("tool arguments must be a mapping")
    if _canonical_bytes(dict(value.arguments)) > MAX_ARGUMENTS_BYTES:
        raise c.ValidationError("tool arguments exceed the size bound")
    return ToolRequest(call_id=validate_call_id(value.call_id), tool_name=validate_tool_name(value.tool_name),
                       arguments=dict(value.arguments))


def validate_reply_draft(value: Any) -> ReplyDraft:
    if not isinstance(value, ReplyDraft):
        raise c.ValidationError("reply draft must be a ReplyDraft")
    if not isinstance(value.text, str):
        raise c.ValidationError("reply text must be a string")
    kind = c.validate_enum(value.kind, lc.DeliveryKind, field="kind")
    refs = tuple(validate_evidence_ref(r) for r in tuple(value.evidence_refs))
    payload = c.validate_payload(value.payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)
    return ReplyDraft(text=value.text, kind=kind, evidence_refs=refs,
                      claims_commerce_facts=bool(value.claims_commerce_facts), payload=payload)


def validate_budget(value: Any) -> LoopBudget:
    if not isinstance(value, LoopBudget):
        raise c.ValidationError("budget must be a LoopBudget")
    if not (isinstance(value.max_steps, int) and value.max_steps >= 1):
        raise c.ValidationError("max_steps must be a positive integer")
    if not (isinstance(value.max_tool_calls, int) and value.max_tool_calls >= 0):
        raise c.ValidationError("max_tool_calls must be a non-negative integer")
    if not (isinstance(value.tool_timeout_seconds, (int, float)) and value.tool_timeout_seconds > 0):
        raise c.ValidationError("tool_timeout_seconds must be positive")
    if not (isinstance(value.deadline_seconds, (int, float)) and value.deadline_seconds > 0):
        raise c.ValidationError("deadline_seconds must be positive")
    return value


# ── Evidence verification (pure) ─────────────────────────────────────────────


def evidence_index(observations: Sequence[ToolObservation]) -> Dict[str, str]:
    """Evidence references gathered in this turn, mapped to the call that produced them."""
    index: Dict[str, str] = {}
    for obs in observations:
        if obs.ok:
            for ref in obs.evidence_refs:
                index.setdefault(ref, obs.call_id)
    return index


def verify_reply_draft(draft: ReplyDraft, observations: Sequence[ToolObservation]) -> Tuple[VerificationProblem, ...]:
    """Deterministic checks a draft must pass before it may be handed to delivery.

    They prove that every cited evidence reference exists among the
    observations this loop gathered for this tenant/conversation/turn, that a
    draft which claims commerce facts cites at least one, and that the text
    is present and bounded. They do **not** prove that the text's sentences
    are consistent with the evidence: semantic grounding is not established
    by this slice.
    """
    problems = []
    text = draft.text.strip()
    if not text:
        problems.append(VerificationProblem("empty_text", "the reply text is empty"))
    elif len(text) > MAX_REPLY_TEXT_LENGTH:
        problems.append(VerificationProblem("text_too_long", f"the reply exceeds {MAX_REPLY_TEXT_LENGTH} characters"))
    known = evidence_index(observations)
    for ref in draft.evidence_refs:
        if ref not in known:
            problems.append(VerificationProblem("unknown_evidence", f"{ref} was not observed in this turn"))
    if draft.claims_commerce_facts and not draft.evidence_refs:
        problems.append(VerificationProblem("missing_evidence", "a commerce reply must cite observed evidence"))
    return tuple(problems)


__all__ = [
    "AGENT_LOOP_STATE_KEY", "AGENT_LOOP_STATE_VERSION", "AgentLoopError", "AuthorizedContext", "BudgetView",
    "LoopBudget", "LoopEvent", "LoopOutcome", "LoopPhase", "LoopProgress", "LoopStatus", "MAX_ARGUMENTS_BYTES",
    "MAX_OBSERVATION_BYTES", "MAX_REPLY_TEXT_LENGTH", "MAX_TOOL_REQUESTS_PER_STEP", "ProviderBlocked",
    "ProviderCapabilities", "ProviderFailure", "ProviderInvalid", "ProviderReply", "ProviderRequest",
    "ProviderResult", "ProviderToolRequests", "RESERVED_SCOPE_ARGUMENTS", "ReplyDraft", "StopReason",
    "ToolDefinition", "ToolError", "ToolErrorCode", "ToolObservation", "ToolRequest", "VerificationFeedback",
    "VerificationProblem", "evidence_index", "validate_budget", "validate_call_id", "validate_evidence_ref",
    "validate_reply_draft", "validate_tool_name", "validate_tool_request", "verify_reply_draft",
]
