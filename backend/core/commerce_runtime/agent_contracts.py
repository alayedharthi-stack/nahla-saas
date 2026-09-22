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

import copy
import dataclasses
import datetime as _dt
import enum
import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

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
AGENT_LOOP_STATE_VERSION = 3
# Version 2 wrote ``executed`` as a bare list of signatures, which recorded that
# work had run but not how much of its repeat allowance was left. Such a payload
# is still readable and is read **conservatively**: every signature in it counts
# as having used its whole allowance, so missing history can never be mistaken
# for permission to repeat again. No shared-data migration or backfill exists.
READABLE_STATE_VERSIONS = (2, AGENT_LOOP_STATE_VERSION)
MAX_CHECKPOINT_BYTES = 12 * 1024           # the agent_loop portion of the state payload
MAX_CHECKPOINT_OBSERVATIONS = 16           # only the most recent observations are retained at all
# One original attempt plus at most one recovery repeat, per execution signature.
MAX_TOOL_ATTEMPTS_PER_SIGNATURE = 2

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
    TURN_NOT_IN_SCOPE = "turn_not_in_scope"          # the turn is not this tenant/namespace/conversation's
    TURN_NOT_ELIGIBLE = "turn_not_eligible"
    TURN_COMPLETED = "turn_completed"
    OWNERSHIP_LOST = "ownership_lost"
    CONCURRENT_INVOCATION = "concurrent_invocation"  # another invocation advanced this turn's durable progress
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_BLOCKED = "provider_blocked"
    PROVIDER_INVALID = "provider_invalid"
    PROVIDER_TIMEOUT = "provider_timeout"            # the provider did not answer inside its enforced wait
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    VERIFICATION_FAILED = "verification_failed"
    REPEATED_TOOL_REQUEST = "repeated_tool_request"
    # A refused or timed-out tool is an *observation*, not a stop: the loop
    # reports it to the provider and keeps its budget. A provider call that
    # runs long is caught by the loop's deadline; a per-call provider timeout
    # belongs to a future adapter, which reports it as ProviderFailure.


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
    """What the loop tells the provider about one tool request. Data, never instructions.

    ``restored`` marks an observation rebuilt from the durable checkpoint of an
    earlier invocation; ``body_truncated`` says its result body did not fit the
    checkpoint bound and was dropped. Evidence references always survive, so
    verification stays exact across re-entry, but the provider is told plainly
    that it is not looking at the original body.
    """

    call_id: str
    tool_name: str
    ok: bool
    result: Optional[Mapping[str, Any]]
    error_code: Optional[str]
    error: Optional[str]
    evidence_refs: Tuple[str, ...]
    restored: bool = False
    body_truncated: bool = False


@dataclasses.dataclass(frozen=True)
class VerificationProblem:
    code: str                                # closed: "unknown_evidence" | "empty_text" | "text_too_long" | "missing_evidence" | "invalid_kind" | "coupon_code_without_evidence" | "unobserved_code" | "choice_without_evidence"
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


# A step the provider cut off at its output limit. Named in the contract rather
# than in one provider because the loop treats it as correctable while a step
# remains: being cut off is a fact about that step, not a verdict on the turn.
TRUNCATED_OUTPUT = "truncated_output"


@dataclasses.dataclass(frozen=True)
class ProviderInvalid(ProviderResult):
    """Incomplete or malformed output (truncation, unparsable structure).

    Not a usable reply. ``TRUNCATED_OUTPUT`` is the one reason the loop may
    hand back to the provider instead of stopping the turn, because the model
    can finish what it was cut off from; every other reason ends the turn.
    """

    reason: str


# ── Loop configuration, persisted progress and outcome ───────────────────────


@dataclasses.dataclass(frozen=True)
class LoopBudget:
    max_steps: int = 4
    max_tool_calls: int = 6
    tool_timeout_seconds: float = 5.0
    provider_timeout_seconds: float = 30.0
    deadline_seconds: float = 60.0

    def to_payload(self) -> Dict[str, Any]:
        return {"max_steps": self.max_steps, "max_tool_calls": self.max_tool_calls,
                "tool_timeout_seconds": self.tool_timeout_seconds,
                "provider_timeout_seconds": self.provider_timeout_seconds,
                "deadline_seconds": self.deadline_seconds}

    @classmethod
    def from_payload(cls, raw: Any) -> Optional["LoopBudget"]:
        if not isinstance(raw, Mapping):
            return None
        try:
            return validate_budget(cls(
                max_steps=int(raw["max_steps"]), max_tool_calls=int(raw["max_tool_calls"]),
                tool_timeout_seconds=float(raw["tool_timeout_seconds"]),
                provider_timeout_seconds=float(raw["provider_timeout_seconds"]),
                deadline_seconds=float(raw["deadline_seconds"]),
            ))
        except (KeyError, TypeError, ValueError, c.ValidationError):
            return None


@dataclasses.dataclass(frozen=True)
class ObservationCheckpoint:
    """The durable projection of one observation.

    Evidence references and the repeat-detection identity always survive; the
    result body survives only while the checkpoint stays inside its bound.
    """

    call_id: str
    tool_name: str
    ok: bool
    error_code: Optional[str]
    evidence_refs: Tuple[str, ...]
    digest: str
    body: Optional[Mapping[str, Any]]

    def to_payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"call_id": self.call_id, "tool": self.tool_name, "ok": self.ok,
                               "error_code": self.error_code, "refs": list(self.evidence_refs),
                               "digest": self.digest}
        if self.body is not None:
            out["body"] = copy.deepcopy(dict(self.body))
        return out

    @classmethod
    def from_payload(cls, raw: Any) -> Optional["ObservationCheckpoint"]:
        if not isinstance(raw, Mapping):
            return None
        try:
            refs = tuple(str(r) for r in raw.get("refs") or ())
            body = raw.get("body")
            return cls(call_id=str(raw["call_id"]), tool_name=str(raw["tool"]), ok=bool(raw["ok"]),
                       error_code=raw.get("error_code"), evidence_refs=refs, digest=str(raw.get("digest", "")),
                       body=dict(body) if isinstance(body, Mapping) else None)
        except (KeyError, TypeError, ValueError):
            return None

    def restore(self) -> ToolObservation:
        return ToolObservation(
            call_id=self.call_id, tool_name=self.tool_name, ok=self.ok,
            result=copy.deepcopy(dict(self.body)) if self.body is not None else None,
            error_code=self.error_code, error=None, evidence_refs=self.evidence_refs,
            restored=True, body_truncated=self.body is None and self.ok,
        )


def observation_digest(observation: ToolObservation) -> str:
    material = {"call_id": observation.call_id, "tool": observation.tool_name, "ok": observation.ok,
                "error_code": observation.error_code, "refs": list(observation.evidence_refs),
                "result": observation.result}
    try:
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, default=repr)
    except (TypeError, ValueError):                       # pragma: no cover - default=repr covers it
        encoded = repr(material)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def checkpoint_observations(observations: Sequence[ToolObservation], *,
                            budget_bytes: int = MAX_CHECKPOINT_BYTES) -> Tuple[ObservationCheckpoint, ...]:
    """Project observations for durable storage, newest bodies kept first.

    The projection is bounded twice over. Only the most recent
    ``MAX_CHECKPOINT_OBSERVATIONS`` observations are retained at all; an older
    one is dropped whole, references included. For each **retained**
    observation the identity, outcome and evidence references are always kept,
    and bodies are dropped oldest-first until the payload fits its bound.
    """
    kept = list(observations)[-MAX_CHECKPOINT_OBSERVATIONS:]
    projections: List[ObservationCheckpoint] = [
        ObservationCheckpoint(
            call_id=o.call_id, tool_name=o.tool_name, ok=o.ok, error_code=o.error_code,
            evidence_refs=tuple(o.evidence_refs), digest=observation_digest(o),
            body=copy.deepcopy(dict(o.result)) if isinstance(o.result, Mapping) else None,
        )
        for o in kept
    ]

    def size(items: Sequence[ObservationCheckpoint]) -> int:
        return _canonical_bytes([i.to_payload() for i in items])

    for index in range(len(projections)):
        if size(projections) <= budget_bytes:
            break
        projections[index] = dataclasses.replace(projections[index], body=None)
    return tuple(projections)


def _read_executed(raw: Any) -> Tuple[Tuple[str, int], ...]:
    """Read the repeat record from either payload shape.

    Version 3 stores ``[signature, attempts]`` pairs. A version 2 payload stores
    bare signatures, which say that work ran but not how much allowance it left;
    each is therefore read as having spent its **whole** allowance, never as an
    unused one.
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    charged: Dict[str, int] = {}
    for item in raw:
        if isinstance(item, str):
            charged[item] = MAX_TOOL_ATTEMPTS_PER_SIGNATURE
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            signature, attempts = item
            try:
                charged[str(signature)] = max(charged.get(str(signature), 0), int(attempts))
            except (TypeError, ValueError):
                charged[str(signature)] = MAX_TOOL_ATTEMPTS_PER_SIGNATURE
        else:
            continue
    return tuple(sorted(charged.items()))


@dataclasses.dataclass(frozen=True)
class LoopProgress:
    """The turn's durable progress: the authoritative budget, the consumed
    attempts, the deadline and the checkpointed reasoning context.

    It is written under the conversation's compare-and-set state commit, so a
    debit is bound to the revision it was computed from and two invocations can
    never proceed on the same debit. The delivery sequence is *not* copied here:
    it is found by turn id (unique per turn), so this payload can never disagree
    with the ledger.
    """

    turn_id: int
    phase: str
    limits: LoopBudget
    deadline_at: _dt.datetime
    steps_used: int
    tool_calls_used: int
    observations: Tuple[ObservationCheckpoint, ...] = ()
    feedback: Tuple[Tuple[int, Tuple[str, ...]], ...] = ()      # (step_no, problem codes)
    # (execution signature, attempts already charged to it). The attempts are the
    # repeat allowance: they are written with the pre-execution debit, so a crash
    # before the tool runs still shows the attempt as spent.
    executed: Tuple[Tuple[str, int], ...] = ()
    stop_reason: Optional[str] = None

    def attempts(self, signature: str) -> int:
        """How much of ``signature``'s repeat allowance the durable record has spent."""
        for recorded, count in self.executed:
            if recorded == signature:
                return count
        return 0

    def with_attempts(self, signatures: Sequence[str]) -> Tuple[Tuple[str, int], ...]:
        """This record's attempts with one more charged to each of ``signatures``."""
        charged: Dict[str, int] = {recorded: count for recorded, count in self.executed}
        for signature in signatures:
            charged[signature] = charged.get(signature, 0) + 1
        return tuple(sorted(charged.items()))

    def to_payload(self) -> Dict[str, Any]:
        return {
            "version": AGENT_LOOP_STATE_VERSION, "turn_id": self.turn_id, "phase": self.phase,
            "limits": self.limits.to_payload(), "deadline_at": self.deadline_at.isoformat(),
            "steps_used": self.steps_used, "tool_calls_used": self.tool_calls_used,
            "stop_reason": self.stop_reason,
            "observations": [o.to_payload() for o in self.observations],
            "feedback": [[step, list(codes)] for step, codes in self.feedback],
            "executed": [[signature, attempts] for signature, attempts in self.executed],
        }

    @classmethod
    def from_payload(cls, raw: Any, *, turn_id: int) -> Optional["LoopProgress"]:
        """The durable progress of *this* turn, or None when absent, for another
        turn, of another version, or not readable. Never a partial restore."""
        if not isinstance(raw, Mapping) or raw.get("version") not in READABLE_STATE_VERSIONS:
            return None
        if raw.get("turn_id") != turn_id:
            return None
        limits = LoopBudget.from_payload(raw.get("limits"))
        if limits is None:
            return None
        try:
            deadline_at = _dt.datetime.fromisoformat(str(raw["deadline_at"]))
            observations = tuple(
                o for o in (ObservationCheckpoint.from_payload(item) for item in raw.get("observations") or ())
                if o is not None)
            feedback = tuple(
                (int(step), tuple(str(code) for code in codes))
                for step, codes in (raw.get("feedback") or ()))
            return cls(
                turn_id=turn_id, phase=str(raw["phase"]), limits=limits, deadline_at=deadline_at,
                steps_used=int(raw["steps_used"]), tool_calls_used=int(raw["tool_calls_used"]),
                observations=observations, feedback=feedback,
                executed=_read_executed(raw.get("executed")),
                stop_reason=raw.get("stop_reason"),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def same_debits(self, other: Optional["LoopProgress"]) -> bool:
        """Whether ``other`` is this turn with exactly these consumed counters.

        It compares the turn id and the two counters only, not the whole
        checkpoint: those counters are what a debit is bound to.
        """
        if other is None:
            return False
        return (other.turn_id, other.steps_used, other.tool_calls_used) == (
            self.turn_id, self.steps_used, self.tool_calls_used)


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


# ── Isolation of authoritative data ──────────────────────────────────────────


def public_copy(value: Any) -> Any:
    """A detached, plain copy of ``value`` for handing across the boundary.

    Mappings and sequences are rebuilt as ordinary ``dict`` / ``list`` objects
    all the way down, so a holder of the copy cannot reach the authoritative
    object it came from. Frozen dataclasses do not protect nested containers;
    this does.
    """
    if isinstance(value, Mapping):
        return {str(key): public_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
        return [public_copy(item) for item in value]
    return copy.deepcopy(value)


class UnsupportedCapability(c.CommerceRuntimeError):
    """The provider asked for something it did not declare it can do."""

    def __init__(self, capability: str, *, requested: int, allowed: int) -> None:
        self.capability = capability
        self.requested = requested
        self.allowed = allowed
        super().__init__(f"{capability}: requested {requested}, allowed {allowed}")


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
    if any(not isinstance(key, str) for key in value.arguments):
        raise c.ValidationError("tool argument names must be strings")
    try:
        size = _canonical_bytes(dict(value.arguments))
    except (TypeError, ValueError) as exc:
        # A non-serializable argument is a boundary-validation failure, not an
        # incidental TypeError escaping into the caller.
        raise c.ValidationError(f"tool arguments are not serializable: {type(exc).__name__}") from exc
    if size > MAX_ARGUMENTS_BYTES:
        raise c.ValidationError("tool arguments exceed the size bound")
    return ToolRequest(call_id=validate_call_id(value.call_id), tool_name=validate_tool_name(value.tool_name),
                       arguments=public_copy(value.arguments))


def validate_reply_draft(value: Any) -> ReplyDraft:
    if not isinstance(value, ReplyDraft):
        raise c.ValidationError("reply draft must be a ReplyDraft")
    if not isinstance(value.text, str):
        raise c.ValidationError("reply text must be a string")
    kind = c.validate_enum(value.kind, lc.DeliveryKind, field="kind")
    if isinstance(value.evidence_refs, (str, bytes)) or not isinstance(value.evidence_refs, Sequence):
        raise c.ValidationError("evidence_refs must be a sequence of references")
    refs = tuple(validate_evidence_ref(r) for r in value.evidence_refs)
    if not isinstance(value.payload, Mapping):
        raise c.ValidationError("reply payload must be a mapping")
    payload = c.validate_payload(value.payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES)
    if not isinstance(value.claims_commerce_facts, bool):
        raise c.ValidationError("claims_commerce_facts must be a boolean")
    return ReplyDraft(text=value.text, kind=kind, evidence_refs=refs,
                      claims_commerce_facts=value.claims_commerce_facts, payload=payload)


def validate_capabilities(value: Any) -> ProviderCapabilities:
    """A provider must declare what it can do, in the declared shape."""
    if not isinstance(value, ProviderCapabilities):
        raise c.ValidationError("provider capabilities must be a ProviderCapabilities")
    if not isinstance(value.provider_name, str) or not value.provider_name.strip():
        raise c.ValidationError("provider_name must be a non-empty string")
    for field in ("tool_use", "parallel_tool_use", "evidence_refs"):
        if not isinstance(getattr(value, field), bool):
            raise c.ValidationError(f"{field} must be a boolean")
    limit = value.max_tool_requests_per_step
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise c.ValidationError("max_tool_requests_per_step must be a positive integer")
    return value


def validate_provider_result(value: Any, capabilities: ProviderCapabilities) -> ProviderResult:
    """Validate a provider result **completely**, before anything in it runs.

    Every variant, every nested field and every collection is checked here, so
    a bundle that mixes a valid and a malformed request executes no tool at
    all, and no incidental ``TypeError`` escapes as a crash: a malformed result
    becomes a declared outcome.
    """
    if not isinstance(value, ProviderResult):
        raise c.ValidationError(f"provider result must be a ProviderResult, got {type(value).__name__}")

    if isinstance(value, (ProviderFailure, ProviderBlocked, ProviderInvalid)):
        if not isinstance(value.reason, str) or not value.reason.strip():
            raise c.ValidationError(f"{type(value).__name__}.reason must be a non-empty string")
        return value

    if isinstance(value, ProviderReply):
        return ProviderReply(validate_reply_draft(value.draft))

    if isinstance(value, ProviderToolRequests):
        requests = value.requests
        if isinstance(requests, (str, bytes, Mapping)) or not isinstance(requests, Sequence):
            raise c.ValidationError("tool requests must be a sequence of ToolRequest")
        if not requests:
            raise c.ValidationError("a tool step carried no request")
        if len(requests) > MAX_TOOL_REQUESTS_PER_STEP:
            raise c.ValidationError(f"a step may request at most {MAX_TOOL_REQUESTS_PER_STEP} tools")
        if not capabilities.tool_use:
            raise UnsupportedCapability("tool_use", requested=len(requests), allowed=0)
        allowed = capabilities.max_tool_requests_per_step if capabilities.parallel_tool_use else 1
        if len(requests) > allowed:
            raise UnsupportedCapability("parallel_tool_use", requested=len(requests), allowed=allowed)
        validated = tuple(validate_tool_request(request) for request in requests)
        seen = [r.call_id for r in validated]
        if len(set(seen)) != len(seen):
            raise c.ValidationError("tool requests must carry distinct call ids")
        return ProviderToolRequests(requests=validated)

    raise c.ValidationError(f"unknown provider result variant {type(value).__name__}")


def validate_budget(value: Any) -> LoopBudget:
    if not isinstance(value, LoopBudget):
        raise c.ValidationError("budget must be a LoopBudget")
    if not (isinstance(value.max_steps, int) and value.max_steps >= 1):
        raise c.ValidationError("max_steps must be a positive integer")
    if not (isinstance(value.max_tool_calls, int) and value.max_tool_calls >= 0):
        raise c.ValidationError("max_tool_calls must be a non-negative integer")
    if not (isinstance(value.tool_timeout_seconds, (int, float)) and value.tool_timeout_seconds > 0):
        raise c.ValidationError("tool_timeout_seconds must be positive")
    if not (isinstance(value.provider_timeout_seconds, (int, float)) and value.provider_timeout_seconds > 0):
        raise c.ValidationError("provider_timeout_seconds must be positive")
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


PROMOTIONS_TOOL_NAME = "list_shareable_promotions"
# A coupon-code-shaped token: Latin capitals and digits, four to twenty-four
# characters, starting with a letter, standing alone. Only the reply text of a
# turn in which the promotions tool ran is scanned for these.
_CODE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{3,23}(?![A-Za-z0-9])")


def observed_promotion_codes(observations: Sequence[ToolObservation]) -> Optional[Dict[str, str]]:
    """``code -> evidence ref`` for every coupon the promotions tool returned this turn.

    ``{}`` when the tool did not run; ``None`` when it ran but a body is not
    available (an observation restored without its body), in which case the
    codes cannot be known and nothing about them can be checked.
    """
    codes: Dict[str, str] = {}
    for obs in observations:
        if obs.tool_name != PROMOTIONS_TOOL_NAME or not obs.ok:
            continue
        if obs.result is None or obs.body_truncated:
            return None
        for item in list(obs.result.get("promotions") or ()):
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("code") or "").strip().upper()
            ref = str(item.get("evidence_ref") or "").strip()
            if code and ref:
                codes[code] = ref
    return codes


def observed_code_tokens(observations: Sequence[ToolObservation]) -> Set[str]:
    """Every code-shaped token that appears anywhere in this turn's observation bodies."""
    tokens: Set[str] = set()
    for obs in observations:
        if not obs.ok or obs.result is None:
            continue
        rendered = json.dumps(obs.result, ensure_ascii=False, default=str)
        tokens.update(match.upper() for match in _CODE_TOKEN_RE.findall(rendered))
    return tokens


# The slots an inbound turn payload carries the customer's own words in. Only
# these are read: everything else in the payload is platform-added (message and
# context identifiers, routing metadata), and admitting those would let a value
# the platform generated pass as something the customer said.
CUSTOMER_TEXT_KEYS = ("text", "body", "message")


def customer_supplied_tokens(inbound: Optional[Mapping[str, Any]]) -> Set[str]:
    """Every code-shaped token the customer's own message carries this turn.

    ``None`` or a payload with no text is an empty set, so a caller that knows
    nothing about the inbound gets the strictest reading.
    """
    if not isinstance(inbound, Mapping):
        return set()
    tokens: Set[str] = set()
    for key in CUSTOMER_TEXT_KEYS:
        value = inbound.get(key)
        if isinstance(value, str) and value.strip():
            tokens.update(match.upper() for match in _CODE_TOKEN_RE.findall(value))
    return tokens


def verify_reply_draft(draft: ReplyDraft, observations: Sequence[ToolObservation], *,
                       inbound: Optional[Mapping[str, Any]] = None) -> Tuple[VerificationProblem, ...]:
    """Deterministic checks a draft must pass before it may be handed to delivery.

    They prove that every cited evidence reference exists among the
    observations this loop gathered for this tenant/conversation/turn, that a
    draft which claims commerce facts cites at least one, and that the text
    is present and bounded. In a turn where the promotions tool ran they also
    prove that every coupon code the text carries is one that tool returned
    and that its evidence is cited. Beyond that they hold the reply to one
    rule about code-shaped tokens generally: the agent may not put one in
    front of a customer unless it did not originate it. There are exactly two
    ways it did not — this turn's observations returned it, or the customer's
    own message this turn carried it (``inbound``). Everything else is the
    agent stating a coupon, order or tracking identifier out of nothing.

    The second source is why ``inbound`` exists. A customer who writes «وش حال
    طلبي RRRD1234؟» has named that number; repeating it back to look it up, to
    say it was not found, or to ask whether it was typed correctly is quoting,
    not claiming, and a guard that refused it would force the agent to discuss
    the customer's order without being able to name it. The customer's words
    are authoritative about what the customer said. Note the narrowness: only
    *this turn's* inbound, never the conversation's history, and never the
    agent's own earlier replies — a code the agent said yesterday is the
    defect this check exists for, since validity and eligibility are exactly
    what goes stale.

    A draft that offers a tappable selector is held to
    the same standard for every product in it: the row the customer reads
    carries the merchant's title and price, so the product must have been
    looked up in this turn and cited here. They do **not** prove that the
    text's sentences are consistent with the evidence: semantic grounding is
    not established by this slice. In particular, a token the customer
    supplied may be *named* freely but nothing here proves the sentence around
    it is true — an agent that confirms a customer-quoted code's terms without
    a lookup is caught, if at all, by ``missing_evidence`` on a draft that
    claims commerce facts. That is a semantic judgement this slice does not
    make and does not pretend to.
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
    # A coupon code is an operational claim about what the merchant's records
    # say *now*. The conversation's history is context, not evidence: a code
    # that was valid yesterday may since have expired, been disabled or run
    # out, and repeating it from memory states a discount nobody re-checked.
    # So a code-shaped token is held to this turn's own observations whether or
    # not the promotions tool ran. When it is unsupported the model is told so
    # and can call that tool for the current truth or drop the code; either way
    # it still answers, with its whole context intact.
    #
    # What the rule must not do is stop the agent repeating the customer. A
    # number the customer wrote this turn to ask about is not something the
    # agent asserted, so it is excluded — quoting it, saying it was not found
    # and asking whether it is right all stay possible. The exclusion is the
    # customer's message only: the agent's own earlier wording is never a
    # source of truth about itself.
    text_tokens = {token.upper() for token in _CODE_TOKEN_RE.findall(text)}
    promotions_ran = any(o.tool_name == PROMOTIONS_TOOL_NAME and o.ok for o in observations)
    codes = observed_promotion_codes(observations) if promotions_ran else {}
    if text_tokens and codes is not None:
        cited = set(draft.evidence_refs)
        for code in sorted(text_tokens & set(codes)):
            if codes[code] not in cited:
                problems.append(VerificationProblem(
                    "coupon_code_without_evidence", f"{code} appears in the text but {codes[code]} is not cited"))
        not_originated = observed_code_tokens(observations) | customer_supplied_tokens(inbound)
        for token in sorted(text_tokens - set(codes) - not_originated):
            problems.append(VerificationProblem(
                "unobserved_code",
                f"{token} was neither returned by this turn's tools nor written by the customer"))
    from core.commerce_runtime import reply_choices as _rc  # noqa: PLC0415

    for product_id in _rc.unobserved_choices(draft, observations):
        problems.append(VerificationProblem(
            "choice_without_evidence",
            f"product {product_id} is offered as a choice but was not looked up and cited in this turn"))
    return tuple(problems)


__all__ = [
    "AGENT_LOOP_STATE_KEY", "AGENT_LOOP_STATE_VERSION", "AgentLoopError", "AuthorizedContext", "BudgetView",
    "LoopBudget", "LoopEvent", "LoopOutcome", "LoopPhase", "LoopProgress", "LoopStatus", "MAX_ARGUMENTS_BYTES",
    "MAX_CHECKPOINT_BYTES", "MAX_CHECKPOINT_OBSERVATIONS", "MAX_OBSERVATION_BYTES", "MAX_REPLY_TEXT_LENGTH",
    "MAX_TOOL_ATTEMPTS_PER_SIGNATURE", "MAX_TOOL_REQUESTS_PER_STEP", "ObservationCheckpoint", "ProviderBlocked",
    "ProviderCapabilities", "ProviderFailure", "ProviderInvalid", "ProviderReply", "ProviderRequest",
    "ProviderResult", "ProviderToolRequests", "READABLE_STATE_VERSIONS", "RESERVED_SCOPE_ARGUMENTS", "ReplyDraft",
    "TRUNCATED_OUTPUT", "StopReason", "ToolDefinition", "ToolError", "ToolErrorCode", "ToolObservation", "ToolRequest",
    "UnsupportedCapability", "VerificationFeedback", "VerificationProblem", "checkpoint_observations",
    "customer_supplied_tokens", "evidence_index", "observation_digest", "public_copy", "validate_budget",
    "validate_call_id",
    "CUSTOMER_TEXT_KEYS", "validate_capabilities", "validate_evidence_ref", "validate_provider_result",
    "validate_reply_draft",
    "validate_tool_name", "validate_tool_request", "verify_reply_draft"
]
