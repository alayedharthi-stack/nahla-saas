"""The dormant agent loop core.

One loop, one eligible turn. Nahla owns the control flow: it builds the
authorized context from trusted runtime state, asks the reasoning provider
for one inference step, executes only the allowlisted read-only tools the
provider requested, feeds the observations back into the next step, verifies
the reply draft against the evidence gathered in this turn, and hands an
accepted reply to the delivery ledger as a durable delivery intent. It never
sends anything.

Invariants this module upholds:

* **No transaction across reasoning or tool execution.** Every database
  operation is a completed repository call; the provider step and the tool
  calls happen between them.
* **Ownership is re-validated after every await.** A step whose result comes
  back after the lease was lost, taken over or fenced writes no state and
  reserves no delivery.
* **Durable state, not a side channel.** Loop progress lives in the
  conversation's versioned state payload under ``agent_loop``, committed
  through the foundation's compare-and-set; ``Conversation.extra_metadata``
  is not touched, and re-entry reads the persisted budget instead of
  restarting it.
* **Generating a reply is not sending it.** The accepted reply and its
  delivery intent are persisted atomically by
  ``LedgerRepository.commit_turn_decision``; the outcome is
  ``pending_delivery``. No dispatch, reconciliation or terminal completion
  happens here.
* **Re-entry reuses existing work.** An existing delivery sequence for the
  turn is reused, never duplicated; pending, dispatching or unknown ledger
  work stops the loop explicitly instead of resending or completing.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_tools as at
from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime.ledgers import LedgerRepository

Clock = Callable[[], float]


class _Stop(Exception):
    """Internal: stop the loop with this reason and detail."""

    def __init__(self, reason: str, **detail: Any) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


class AgentLoop:
    """Runs one turn to an accepted reply, or to an explicit stop."""

    def __init__(self, ledgers: LedgerRepository, registry: at.ToolRegistry, *,
                 budget: Optional[ac.LoopBudget] = None, clock: Optional[Clock] = None) -> None:
        self._ledgers = ledgers
        self._foundation = ledgers.foundation
        self._registry = registry
        self._budget = ac.validate_budget(budget or ac.LoopBudget())
        self._clock = clock or time.monotonic

    # ── Public entry point ───────────────────────────────────────────────────

    def run_turn(self, *, tenant_id: int, namespace: Any, conversation_id: int, turn_id: int,
                 token: c.OwnershipToken, provider: Any,
                 cancelled: Optional[Callable[[], bool]] = None,
                 _fault_before_commit: Optional[Callable[[], None]] = None) -> ac.LoopOutcome:
        tenant_id, ns, conversation_id, token = self._foundation._scoped(tenant_id, namespace, conversation_id, token)
        turn_id = c.validate_counter(turn_id, field="turn_id")
        started = self._clock()
        events: List[ac.LoopEvent] = []
        state = _RunState(budget=self._budget, started=started, clock=self._clock, events=events)

        try:
            snap = self._snapshot(tenant_id, ns, conversation_id, token)
            resumed = ac.LoopProgress.from_payload(snap.state_payload.get(ac.AGENT_LOOP_STATE_KEY), turn_id=turn_id)
            if resumed is not None:
                state.adopt(resumed)
                state.record(0, "resumed", {"steps_used": resumed.steps_used,
                                            "tool_calls_used": resumed.tool_calls_used,
                                            "phase": resumed.phase})
            existing = self._existing_work(tenant_id, ns, turn_id)
            if existing is not None:
                return existing
            turn = self._turn(tenant_id, ns, conversation_id, turn_id)
            context = ac.AuthorizedContext(
                tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id, turn_id=turn_id,
                inbound=dict(turn.payload), state_payload=dict(snap.state_payload),
            )
            scope = at.ToolScope(tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id, turn_id=turn_id)
            capabilities = self._capabilities(provider)
            draft = self._reason(provider, capabilities, context, scope, state, cancelled)
            return self._accept(tenant_id, ns, conversation_id, turn_id, token, draft, state,
                                fault_before_commit=_fault_before_commit)
        except _Stop as stop:
            return self._stop(tenant_id, ns, conversation_id, turn_id, token, state, stop)

    # ── Reasoning / acting / observing ───────────────────────────────────────

    def _reason(self, provider: Any, capabilities: ac.ProviderCapabilities, context: ac.AuthorizedContext,
                scope: at.ToolScope, state: "_RunState",
                cancelled: Optional[Callable[[], bool]]) -> ac.ReplyDraft:
        while True:
            state.check_cancelled(cancelled)
            state.check_deadline()
            state.begin_step()
            request = ac.ProviderRequest(
                step_no=state.steps_used, context=context, tools=self._registry.definitions,
                observations=tuple(state.observations), feedback=tuple(state.feedback), budget=state.budget_view(),
            )
            result = self._provider_step(provider, request, state)
            # The provider has answered. Time passed and the caller may have
            # cancelled; ownership is re-judged at the write boundary (_accept).
            state.check_cancelled(cancelled)
            state.check_deadline()

            if isinstance(result, ac.ProviderReply):
                draft = self._validated_draft(result, state)
                problems = ac.verify_reply_draft(draft, state.observations)
                if not problems:
                    state.record(state.steps_used, "reply_accepted",
                                 {"evidence_refs": list(draft.evidence_refs), "kind": draft.kind})
                    return draft
                state.record(state.steps_used, "verification_failed",
                             {"problems": [p.code for p in problems]})
                if not state.steps_left():
                    raise _Stop(ac.StopReason.VERIFICATION_FAILED.value,
                                problems=[p.code for p in problems], last_step=state.steps_used)
                state.feedback.append(ac.VerificationFeedback(step_no=state.steps_used, problems=problems))
                continue

            if isinstance(result, ac.ProviderToolRequests):
                self._run_tools(result, capabilities, scope, state)
                continue

            raise self._provider_stop(result)

    def _provider_step(self, provider: Any, request: ac.ProviderRequest, state: "_RunState") -> ac.ProviderResult:
        try:
            result = provider.step(request)
        except Exception as exc:  # noqa: BLE001 - a provider exception is an explicit outcome, never a crash
            raise _Stop(ac.StopReason.PROVIDER_FAILURE.value, error=type(exc).__name__) from exc
        if not isinstance(result, ac.ProviderResult):
            raise _Stop(ac.StopReason.PROVIDER_INVALID.value, error="result is not a ProviderResult")
        state.record(state.steps_used, "provider_step", {"result": type(result).__name__})
        return result

    def _run_tools(self, result: ac.ProviderToolRequests, capabilities: ac.ProviderCapabilities,
                   scope: at.ToolScope, state: "_RunState") -> None:
        requests = tuple(result.requests)
        if not requests:
            raise _Stop(ac.StopReason.PROVIDER_INVALID.value, error="tool step carried no request")
        if not capabilities.tool_use:
            raise _Stop(ac.StopReason.UNSUPPORTED_CAPABILITY.value, capability="tool_use")
        allowed = min(capabilities.max_tool_requests_per_step if capabilities.parallel_tool_use else 1,
                      ac.MAX_TOOL_REQUESTS_PER_STEP)
        if len(requests) > allowed:
            raise _Stop(ac.StopReason.UNSUPPORTED_CAPABILITY.value,
                        capability="parallel_tool_use", requested=len(requests), allowed=allowed)
        for raw in requests:
            try:
                request = ac.validate_tool_request(raw)
            except c.ValidationError as exc:
                raise _Stop(ac.StopReason.PROVIDER_INVALID.value, error=str(exc)) from exc
            signature = state.signature(request)
            if signature in state.executed:
                raise _Stop(ac.StopReason.REPEATED_TOOL_REQUEST.value,
                            tool=request.tool_name, repeats=state.executed[signature] + 1)
            if not state.tool_calls_left():
                raise _Stop(ac.StopReason.BUDGET_EXHAUSTED.value, limit="max_tool_calls")
            state.check_deadline()
            state.tool_calls_used += 1
            state.executed[signature] = state.executed.get(signature, 0) + 1
            observation = self._registry.execute(scope, request, timeout_seconds=state.budget.tool_timeout_seconds)
            state.observations.append(observation)
            state.record(state.steps_used, "tool_observation",
                         {"tool": observation.tool_name, "ok": observation.ok,
                          "error_code": observation.error_code,
                          "evidence_refs": list(observation.evidence_refs)})

    @staticmethod
    def _validated_draft(result: ac.ProviderReply, state: "_RunState") -> ac.ReplyDraft:
        try:
            return ac.validate_reply_draft(result.draft)
        except c.ValidationError as exc:
            raise _Stop(ac.StopReason.PROVIDER_INVALID.value, error=str(exc)) from exc

    @staticmethod
    def _provider_stop(result: ac.ProviderResult) -> _Stop:
        if isinstance(result, ac.ProviderFailure):
            return _Stop(ac.StopReason.PROVIDER_FAILURE.value, provider_reason=result.reason)
        if isinstance(result, ac.ProviderBlocked):
            return _Stop(ac.StopReason.PROVIDER_BLOCKED.value, provider_reason=result.reason)
        if isinstance(result, ac.ProviderInvalid):
            return _Stop(ac.StopReason.PROVIDER_INVALID.value, provider_reason=result.reason)
        return _Stop(ac.StopReason.PROVIDER_INVALID.value, error=type(result).__name__)

    @staticmethod
    def _capabilities(provider: Any) -> ac.ProviderCapabilities:
        capabilities = getattr(provider, "capabilities", None)
        if not isinstance(capabilities, ac.ProviderCapabilities):
            raise _Stop(ac.StopReason.UNSUPPORTED_CAPABILITY.value, capability="declaration")
        return capabilities

    # ── Durable outcomes ─────────────────────────────────────────────────────

    def _accept(self, tenant_id: int, ns: str, conversation_id: int, turn_id: int, token: c.OwnershipToken,
                draft: ac.ReplyDraft, state: "_RunState",
                fault_before_commit: Optional[Callable[[], None]]) -> ac.LoopOutcome:
        """Persist the accepted reply state and its delivery intent atomically."""
        snap = self._snapshot(tenant_id, ns, conversation_id, token)     # re-validates ownership after the await
        if snap.eligible_turn_id != turn_id:
            raise _Stop(ac.StopReason.TURN_NOT_ELIGIBLE.value, eligible_turn_id=snap.eligible_turn_id)
        existing = self._ledgers.get_delivery_sequence(tenant_id=tenant_id, namespace=ns, turn_id=turn_id)
        progress = state.progress(turn_id, ac.LoopPhase.REPLY_PENDING_DELIVERY.value)
        if existing is not None:
            # Re-entry after the intent was already persisted: never a second sequence.
            state.record(state.steps_used, "delivery_intent_reused", {"sequence_id": existing.sequence_id})
            return ac.LoopOutcome(
                status=ac.LoopStatus.PENDING_DELIVERY.value, stop_reason=None, turn_id=turn_id,
                delivery_sequence_id=existing.sequence_id, reused_delivery=True, state_revision=snap.state_revision,
                steps_used=state.steps_used, tool_calls_used=state.tool_calls_used, events=tuple(state.events),
                detail={"delivery_kind": existing.intent_kind},
            )
        payload = dict(snap.state_payload)
        payload[ac.AGENT_LOOP_STATE_KEY] = progress.to_payload(state.budget)
        payload["reply"] = {"text": draft.text, "kind": draft.kind, "evidence_refs": list(draft.evidence_refs)}
        transition = c.StateTransition(expected_revision=snap.state_revision, payload=payload)
        delivery_payload = dict(draft.payload)
        delivery_payload["text"] = draft.text
        delivery_payload["evidence_refs"] = list(draft.evidence_refs)
        try:
            decision = self._ledgers.commit_turn_decision(
                tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id, token=token, turn_id=turn_id,
                state_transition=transition,
                delivery_intent=lc.DeliveryIntent(kind=draft.kind, payload=delivery_payload),
                _fault_before_commit=fault_before_commit,
            )
        except c.StateConflict as exc:
            # Another run of this turn committed first. Its delivery intent is the
            # turn's one intent: reuse it instead of reserving a second sequence.
            raced = self._ledgers.get_delivery_sequence(tenant_id=tenant_id, namespace=ns, turn_id=turn_id)
            if raced is None:
                raise _Stop(ac.StopReason.OWNERSHIP_LOST.value, rejection=exc.reason.value) from exc
            state.record(state.steps_used, "delivery_intent_reused",
                         {"sequence_id": raced.sequence_id, "raced": True})
            return ac.LoopOutcome(
                status=ac.LoopStatus.PENDING_DELIVERY.value, stop_reason=None, turn_id=turn_id,
                delivery_sequence_id=raced.sequence_id, reused_delivery=True, state_revision=None,
                steps_used=state.steps_used, tool_calls_used=state.tool_calls_used, events=tuple(state.events),
                detail={"delivery_kind": raced.intent_kind, "raced": True},
            )
        except c.OwnershipRejected as exc:
            raise _Stop(ac.StopReason.OWNERSHIP_LOST.value, rejection=exc.reason.value) from exc
        sequence = decision.delivery
        assert sequence is not None
        state.record(state.steps_used, "delivery_intent_reserved",
                     {"sequence_id": sequence.sequence_id, "kind": sequence.intent_kind})
        return ac.LoopOutcome(
            status=ac.LoopStatus.PENDING_DELIVERY.value, stop_reason=None, turn_id=turn_id,
            delivery_sequence_id=sequence.sequence_id, reused_delivery=False,
            state_revision=decision.state.revision if decision.state else snap.state_revision,
            steps_used=state.steps_used, tool_calls_used=state.tool_calls_used, events=tuple(state.events),
            detail={"delivery_kind": sequence.intent_kind, "evidence_refs": list(draft.evidence_refs)},
        )

    def _stop(self, tenant_id: int, ns: str, conversation_id: int, turn_id: int, token: c.OwnershipToken,
              state: "_RunState", stop: _Stop) -> ac.LoopOutcome:
        """Record the stop in durable state when ownership still allows it.

        A stop caused by lost ownership or an ineligible turn writes nothing:
        stale work must not touch state.
        """
        state.record(state.steps_used, "stopped", {"reason": stop.reason, **stop.detail})
        revision: Optional[int] = None
        writable = stop.reason not in {ac.StopReason.OWNERSHIP_LOST.value, ac.StopReason.TURN_NOT_ELIGIBLE.value,
                                       ac.StopReason.TURN_COMPLETED.value}
        if writable:
            progress = state.progress(turn_id, ac.LoopPhase.STOPPED.value, stop_reason=stop.reason)
            try:
                snap = self._snapshot(tenant_id, ns, conversation_id, token)
                payload = dict(snap.state_payload)
                payload[ac.AGENT_LOOP_STATE_KEY] = progress.to_payload(state.budget)
                commit = self._foundation.commit_state(
                    tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id, token=token, turn_id=turn_id,
                    expected_revision=snap.state_revision, payload=payload,
                )
                revision = commit.revision
            except (c.OwnershipRejected, c.StateConflict) as exc:
                reason = getattr(exc, "reason", None)
                state.record(state.steps_used, "stop_not_persisted",
                             {"rejection": reason.value if reason is not None else type(exc).__name__})
        return ac.LoopOutcome(
            status=ac.LoopStatus.STOPPED.value, stop_reason=stop.reason, turn_id=turn_id,
            delivery_sequence_id=None, reused_delivery=False, state_revision=revision,
            steps_used=state.steps_used, tool_calls_used=state.tool_calls_used, events=tuple(state.events),
            detail=dict(stop.detail),
        )

    # ── Reads ────────────────────────────────────────────────────────────────

    def _snapshot(self, tenant_id: int, ns: str, conversation_id: int,
                  token: c.OwnershipToken) -> c.ConversationSnapshot:
        """Read the conversation and judge the token against it on the database clock."""
        snap = self._foundation.get_conversation(tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id)
        reason = c.classify_rejection(
            current_owner=snap.lease_owner, current_fence=snap.lease_fence, current_epoch=snap.ownership_epoch,
            current_expires_at=snap.lease_expires_at, current_revision=snap.state_revision, token=token,
            db_now=snap.db_now,
        )
        if reason is not None:
            raise _Stop(ac.StopReason.OWNERSHIP_LOST.value, rejection=reason.value)
        return snap

    def _turn(self, tenant_id: int, ns: str, conversation_id: int, turn_id: int) -> c.TurnRecord:
        turns = self._foundation.list_turns(tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id)
        for turn in turns:
            if turn.turn_id == turn_id:
                return turn
        raise c.TurnNotFound(f"turn {turn_id} not found in tenant {tenant_id}/{ns}")

    def _existing_work(self, tenant_id: int, ns: str, turn_id: int) -> Optional[ac.LoopOutcome]:
        """Inspect state and ledger work before reasoning again.

        A completed turn, an existing delivery intent, or pending/unknown
        ledger work each end the run explicitly: nothing is resent, nothing
        is completed and no second sequence is created.
        """
        terminal = self._foundation.get_terminal(tenant_id=tenant_id, namespace=ns, turn_id=turn_id)
        if terminal is not None:
            return ac.LoopOutcome(
                status=ac.LoopStatus.STOPPED.value, stop_reason=ac.StopReason.TURN_COMPLETED.value, turn_id=turn_id,
                delivery_sequence_id=None, reused_delivery=False, state_revision=None, steps_used=0,
                tool_calls_used=0, events=(), detail={"processing_outcome": terminal.processing_outcome},
            )
        summary = self._ledgers.turn_ledger_summary(tenant_id=tenant_id, namespace=ns, turn_id=turn_id)
        sequence = self._ledgers.get_delivery_sequence(tenant_id=tenant_id, namespace=ns, turn_id=turn_id)
        if sequence is not None:
            return ac.LoopOutcome(
                status=ac.LoopStatus.PENDING_DELIVERY.value, stop_reason=None, turn_id=turn_id,
                delivery_sequence_id=sequence.sequence_id, reused_delivery=True, state_revision=None,
                steps_used=0, tool_calls_used=0,
                events=(ac.LoopEvent(0, "delivery_intent_reused", {"sequence_id": sequence.sequence_id,
                                                                   "delivery_outcome": summary.delivery_outcome}),),
                detail={"delivery_kind": sequence.intent_kind, "delivery_outcome": summary.delivery_outcome,
                        "delivery_attempt_count": sequence.attempt_count},
            )
        if summary.pending_effect_attempts or summary.effects_by_status.get(lc.EffectStatus.UNKNOWN.value):
            return ac.LoopOutcome(
                status=ac.LoopStatus.STOPPED.value, stop_reason=ac.StopReason.TURN_NOT_ELIGIBLE.value, turn_id=turn_id,
                delivery_sequence_id=None, reused_delivery=False, state_revision=None, steps_used=0, tool_calls_used=0,
                events=(ac.LoopEvent(0, "ledger_work_outstanding",
                                     {"pending_effect_attempts": summary.pending_effect_attempts,
                                      "effects_by_status": dict(summary.effects_by_status)}),),
                detail={"effects_by_status": dict(summary.effects_by_status)},
            )
        return None


class _RunState:
    """Mutable bookkeeping of one run: budget, observations, feedback, events."""

    def __init__(self, *, budget: ac.LoopBudget, started: float, clock: Clock, events: List[ac.LoopEvent]) -> None:
        self.budget = budget
        self._started = started
        self._clock = clock
        self.events = events
        self.observations: List[ac.ToolObservation] = []
        self.feedback: List[ac.VerificationFeedback] = []
        self.executed: Dict[Tuple[str, str], int] = {}
        self.steps_used = 0
        self.tool_calls_used = 0
        self._carried_seconds = 0.0

    # budget -------------------------------------------------------------
    def adopt(self, resumed: ac.LoopProgress) -> None:
        """Re-entry continues the persisted budget; it never resets the limits."""
        self.steps_used = resumed.steps_used
        self.tool_calls_used = resumed.tool_calls_used
        self._carried_seconds = resumed.elapsed_seconds

    def elapsed(self) -> float:
        return self._carried_seconds + (self._clock() - self._started)

    def steps_left(self) -> bool:
        return self.steps_used < self.budget.max_steps

    def tool_calls_left(self) -> bool:
        return self.tool_calls_used < self.budget.max_tool_calls

    def begin_step(self) -> None:
        if not self.steps_left():
            raise _Stop(ac.StopReason.BUDGET_EXHAUSTED.value, limit="max_steps", steps_used=self.steps_used)
        self.steps_used += 1

    def check_deadline(self) -> None:
        if self.elapsed() >= self.budget.deadline_seconds:
            raise _Stop(ac.StopReason.DEADLINE_EXCEEDED.value, elapsed_seconds=round(self.elapsed(), 3))

    def check_cancelled(self, cancelled: Optional[Callable[[], bool]]) -> None:
        if cancelled is not None and cancelled():
            raise _Stop(ac.StopReason.CANCELLED.value)

    def budget_view(self) -> ac.BudgetView:
        return ac.BudgetView(
            remaining_steps=max(0, self.budget.max_steps - self.steps_used),
            remaining_tool_calls=max(0, self.budget.max_tool_calls - self.tool_calls_used),
            remaining_seconds=round(max(0.0, self.budget.deadline_seconds - self.elapsed()), 3),
        )

    # bookkeeping --------------------------------------------------------
    @staticmethod
    def signature(request: ac.ToolRequest) -> Tuple[str, str]:
        import json  # noqa: PLC0415 - local, keeps the module's import surface small
        return request.tool_name, json.dumps(dict(request.arguments), sort_keys=True, ensure_ascii=False)

    def record(self, step_no: int, kind: str, detail: Mapping[str, Any]) -> None:
        self.events.append(ac.LoopEvent(step_no=step_no, kind=kind, detail=dict(detail)))

    def progress(self, turn_id: int, phase: str, *, stop_reason: Optional[str] = None) -> ac.LoopProgress:
        return ac.LoopProgress(turn_id=turn_id, phase=phase, steps_used=self.steps_used,
                               tool_calls_used=self.tool_calls_used, elapsed_seconds=self.elapsed(),
                               stop_reason=stop_reason)


__all__ = ["AgentLoop"]
