"""The dormant agent loop core.

One loop, one eligible turn. Nahla owns the control flow: it builds the
authorized context from trusted runtime state, asks the reasoning provider for
one inference step, executes only the allowlisted read-only tools the provider
requested, feeds the observations back into the next step, verifies the reply
draft against the evidence gathered in this turn, and hands an accepted reply
to the delivery ledger as a durable delivery intent. It never sends anything.

Guarantees this module upholds:

* **Durable attempt accounting.** Before any provider call and before any tool
  call, the consumed attempt — and, for tools, the execution signature it is
  charged to — is written to the conversation's versioned state under a
  compare-and-set bound to the revision the counters were computed from. A crash after the provider or tool ran leaves that debit behind, and
  two invocations can never proceed on the same debit: the loser of the
  compare-and-set stops with ``concurrent_invocation``. Re-entry restores the
  authoritative limits, deadline, consumed attempts, observations, feedback
  and repeat history; a caller cannot reset or enlarge them.
* **Enforced waits.** The provider call and every tool call run under a wait
  the loop enforces itself, capped to the smaller of the per-call limit and
  the time left before the turn's deadline. An abandoned call's late value is
  discarded and has no path to progress or delivery. Abandonment is local: it
  is not proof that the remote work stopped.
* **Authoritative deadline at the reservation boundary.** The deadline is an
  absolute database timestamp. It is re-checked inside the reservation
  transaction, after the conversation row lock, before any write, so a reply
  accepted before expiry cannot reserve delivery after it.
* **Scope and eligibility before work.** The turn must belong to the
  authorized tenant, namespace and conversation before anything about it is
  read or returned, and must be the eligible turn before any reasoning starts.
  Ownership is re-validated at every debit, so ownership lost during a tool
  call cannot buy another reasoning step.
* **Complete boundary validation.** A provider result is validated whole
  before any part of it runs; a bundle mixing a valid and a malformed request
  executes no tool, and malformed output becomes a declared outcome while the
  attempt already debited stays debited.
* **No transaction across reasoning or tool execution**, and generating a
  reply is never sending one: the loop returns ``pending_delivery`` and never
  dispatches, reconciles or finalizes a turn.
"""
from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy.engine import Connection

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_tools as at
from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import reply_card as rcard
from core.commerce_runtime import reply_choices as rc
from core.commerce_runtime.ledgers import LedgerRepository

Clock = Callable[[], float]


class _Stop(Exception):
    """Internal: stop the loop with this reason and detail. Never escapes ``run_turn``."""

    def __init__(self, reason: str, **detail: Any) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


class _DeadlinePassed(Exception):
    """Raised inside the reservation transaction when the turn's deadline expired."""

    def __init__(self, db_now: _dt.datetime, deadline_at: _dt.datetime) -> None:
        self.db_now = db_now
        self.deadline_at = deadline_at
        super().__init__(f"deadline {deadline_at.isoformat()} passed at {db_now.isoformat()}")


class AgentLoop:
    """Runs one turn to an accepted reply, or to an explicit stop."""

    def __init__(self, ledgers: LedgerRepository, registry: at.ToolRegistry, *,
                 budget: Optional[ac.LoopBudget] = None, clock: Optional[Clock] = None) -> None:
        self._ledgers = ledgers
        self._foundation = ledgers.foundation
        self._registry = registry
        self._requested_budget = ac.validate_budget(budget or ac.LoopBudget())
        self._clock = clock or time.monotonic

    # ── Public entry point ───────────────────────────────────────────────────

    def run_turn(self, *, tenant_id: int, namespace: Any, conversation_id: int, turn_id: int,
                 token: c.OwnershipToken, provider: Any,
                 cancelled: Optional[Callable[[], bool]] = None,
                 _fault_before_commit: Optional[Callable[[], None]] = None,
                 _fault_after_tool_debit: Optional[Callable[[], None]] = None) -> ac.LoopOutcome:
        tenant_id, ns, conversation_id, token = self._foundation._scoped(tenant_id, namespace, conversation_id, token)
        turn_id = c.validate_counter(turn_id, field="turn_id")
        session = _Session(scope=_Scope(tenant_id, ns, conversation_id, turn_id), token=token,
                           requested=self._requested_budget, clock=self._clock)
        try:
            snap = self._snapshot(session)
            # F3: membership first — nothing about a turn outside this scope is
            # read, reused or returned.
            turn = self._turn_in_scope(session)
            session.adopt(snap, self._restore(snap, turn_id), self._requested_budget)
            existing = self._existing_work(session)
            if existing is not None:
                return existing
            # F3: fresh work needs the eligible turn before any reasoning.
            if snap.eligible_turn_id != turn_id:
                raise _Stop(ac.StopReason.TURN_NOT_ELIGIBLE.value, eligible_turn_id=snap.eligible_turn_id)
            context = ac.AuthorizedContext(
                tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id, turn_id=turn_id,
                inbound=ac.public_copy(turn.payload), state_payload=ac.public_copy(snap.state_payload),
            )
            scope = at.ToolScope(tenant_id=tenant_id, namespace=ns, conversation_id=conversation_id, turn_id=turn_id)
            session.capabilities = self._capabilities(provider)
            draft = self._reason(provider, context, scope, session, cancelled, _fault_after_tool_debit)
            return self._accept(session, draft, fault_before_commit=_fault_before_commit)
        except _Stop as stop:
            return self._stop(session, stop)

    # ── Reasoning / acting / observing ───────────────────────────────────────

    def _reason(self, provider: Any, context: ac.AuthorizedContext, scope: at.ToolScope, session: "_Session",
                cancelled: Optional[Callable[[], bool]],
                fault_after_tool_debit: Optional[Callable[[], None]] = None) -> ac.ReplyDraft:
        while True:
            session.check_cancelled(cancelled)
            # F1: the attempt is debited durably, under ownership and revision
            # guards, *before* the provider is invoked.
            self._debit(session, steps=1, phase=ac.LoopPhase.REASONING.value)
            request = ac.ProviderRequest(
                step_no=session.progress.steps_used, context=context, tools=self._registry.definitions,
                observations=session.provider_observations(), feedback=session.provider_feedback(),
                budget=session.budget_view(),
            )
            result = self._provider_step(provider, request, session)
            session.check_cancelled(cancelled)

            if isinstance(result, ac.ProviderReply):
                draft = result.draft
                # ``inbound`` is the customer's own turn as admitted — trusted data,
                # never instructions. Verification reads it for one purpose: an
                # identifier the customer wrote is not one the agent asserted.
                problems = ac.verify_reply_draft(draft, session.observations,
                                                 inbound=context.inbound)
                if not problems:
                    # The model chose whether to offer a selector and which
                    # products belong in it; what each row *says* is composed
                    # here from this turn's own observations, so the structured
                    # payload states the merchant's values and never the
                    # model's. The text is carried through untouched, and a
                    # selector that cannot be offered whole simply is not.
                    draft, choices = rc.finalize(draft, session.observations)
                    # A card is the same split for the shape that follows a
                    # choice rather than offering one. The selector wins when
                    # both were asked for: a customer who still has to choose
                    # is not helped by one product's photo.
                    draft, card = rcard.finalize(draft, session.observations,
                                                 selector_offered=choices == rc.OFFERED)
                    session.record("reply_accepted",
                                   {"evidence_refs": list(draft.evidence_refs), "kind": draft.kind,
                                    "choices": choices, "card": card})
                    return draft
                session.record("verification_failed", {"problems": [p.code for p in problems]})
                if not session.steps_left():
                    raise _Stop(ac.StopReason.VERIFICATION_FAILED.value,
                                problems=[p.code for p in problems], last_step=session.progress.steps_used)
                session.feedback.append(ac.VerificationFeedback(step_no=session.progress.steps_used,
                                                                problems=problems))
                continue

            if isinstance(result, ac.ProviderToolRequests):
                self._run_tools(result, scope, session, fault_after_tool_debit)
                continue

            if isinstance(result, ac.ProviderInvalid):
                # Only a truncated step reaches here; everything else stopped
                # the turn inside ``_provider_step``. Being cut off is a fact
                # about that step, not a verdict on the turn: the model keeps
                # its whole context and is told plainly what happened, so it
                # can finish the answer instead of the customer getting none.
                session.feedback.append(ac.VerificationFeedback(
                    step_no=session.progress.steps_used,
                    problems=(ac.VerificationProblem(
                        "output_truncated",
                        "the previous step reached the output limit and was cut off before it "
                        "finished; nothing from it was used"),)))
                continue

            raise self._provider_stop(result)          # pragma: no cover - validation narrows the union

    def _provider_step(self, provider: Any, request: ac.ProviderRequest, session: "_Session") -> ac.ProviderResult:
        """Invoke the provider under an enforced wait and validate its result whole."""
        wait = session.wait_for(session.limits.provider_timeout_seconds)
        try:
            raw = _bounded_call(lambda: provider.step(request), wait)
        except concurrent.futures.TimeoutError as exc:
            # The call is abandoned locally; a late value can reach nothing.
            raise _Stop(ac.StopReason.PROVIDER_TIMEOUT.value, waited_seconds=round(wait, 3)) from exc
        except Exception as exc:  # noqa: BLE001 - a provider exception is an explicit outcome, never a crash
            raise _Stop(ac.StopReason.PROVIDER_FAILURE.value, error=type(exc).__name__) from exc
        # F4: the whole result is validated before any part of it runs.
        try:
            result = ac.validate_provider_result(raw, session.capabilities)
        except ac.UnsupportedCapability as exc:
            raise _Stop(ac.StopReason.UNSUPPORTED_CAPABILITY.value, capability=exc.capability,
                        requested=exc.requested, allowed=exc.allowed) from exc
        except c.ValidationError as exc:
            raise _Stop(ac.StopReason.PROVIDER_INVALID.value, error=str(exc)) from exc
        session.record("provider_step", {"result": type(result).__name__})
        if (isinstance(result, ac.ProviderInvalid) and result.reason == ac.TRUNCATED_OUTPUT
                and session.steps_left()):
            # A step that ran out of room is recoverable while the budget still
            # allows another: the loop hands the fact back rather than ending a
            # turn the customer is waiting on. With no step left it stops as
            # before — an unfinished answer is never sent.
            session.record("output_truncated", {"step_no": request.step_no})
            return result
        if isinstance(result, (ac.ProviderFailure, ac.ProviderBlocked, ac.ProviderInvalid)):
            raise self._provider_stop(result)
        return result

    def _run_tools(self, result: ac.ProviderToolRequests, scope: at.ToolScope, session: "_Session",
                   fault_after_tool_debit: Optional[Callable[[], None]] = None) -> None:
        """Authorize the whole bundle, debit it once, then run it.

        The bundle is preflighted against the signatures this invocation has
        already debited **and** against the signatures seen earlier in the same
        bundle, so distinct correlation ids cannot make identical tool and
        argument work distinct. A refused bundle executes no tool and consumes
        no tool attempt; the reasoning attempt already paid for obtaining the
        result stays paid.
        """
        requests = list(result.requests)
        admitted: List[Tuple[ac.ToolRequest, str, bool]] = []
        seen_in_bundle: Dict[str, str] = {}
        for request in requests:
            signature = session.signature(request)
            first = seen_in_bundle.get(signature)
            if first is not None:
                raise _Stop(ac.StopReason.REPEATED_TOOL_REQUEST.value, tool=request.tool_name,
                            repeat="duplicate_in_bundle", call_id=request.call_id, first_call_id=first)
            seen_in_bundle[signature] = request.call_id
            if signature in session.debited_here:
                raise _Stop(ac.StopReason.REPEATED_TOOL_REQUEST.value, tool=request.tool_name,
                            repeat="repeat_in_invocation", call_id=request.call_id)
            spent = session.attempts(signature)
            if spent >= ac.MAX_TOOL_ATTEMPTS_PER_SIGNATURE:
                raise _Stop(ac.StopReason.REPEATED_TOOL_REQUEST.value, tool=request.tool_name,
                            repeat="allowance_exhausted", attempts=spent,
                            allowance=ac.MAX_TOOL_ATTEMPTS_PER_SIGNATURE)
            admitted.append((request, signature, spent >= 1))
        if session.progress.tool_calls_used + len(admitted) > session.limits.max_tool_calls:
            raise _Stop(ac.StopReason.BUDGET_EXHAUSTED.value, limit="max_tool_calls",
                        requested=len(admitted),
                        remaining=session.limits.max_tool_calls - session.progress.tool_calls_used)
        # F1: the tool attempts and the identities they are charged to are
        # debited durably, in one compare-and-set, before any tool runs.
        self._debit(session, tool_calls=len(admitted), phase=ac.LoopPhase.REASONING.value,
                    admitted=[signature for _, signature, _ in admitted])
        if fault_after_tool_debit is not None:
            fault_after_tool_debit()
        for request, signature, recovery in admitted:
            wait = session.wait_for(session.limits.tool_timeout_seconds)
            observation = self._registry.execute(scope, request, timeout_seconds=wait)
            session.observations.append(observation)
            session.record("tool_observation",
                           {"tool": observation.tool_name, "ok": observation.ok,
                            "error_code": observation.error_code,
                            "evidence_refs": list(observation.evidence_refs),
                            **({"recovery_repeat": True} if recovery else {})})

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
        try:
            return ac.validate_capabilities(getattr(provider, "capabilities", None))
        except c.ValidationError as exc:
            raise _Stop(ac.StopReason.UNSUPPORTED_CAPABILITY.value, capability="declaration",
                        error=str(exc)) from exc

    # ── Durable progress ─────────────────────────────────────────────────────

    def _debit(self, session: "_Session", *, steps: int = 0, tool_calls: int = 0, phase: str,
               admitted: Sequence[str] = ()) -> None:
        """Persist the consumed attempts before the work they pay for runs.

        Read, judge and write are bound to one revision: the counters are
        computed from the progress found at revision *R* and written with a
        compare-and-set on *R*. A concurrent invocation whose turn and counters
        are not the ones this session last wrote is detected, or loses the
        compare-and-set; either way it cannot share this debit, nor spend a
        repeat allowance this one is spending.

        ``admitted`` names the execution signatures this debit charges. Their
        attempt counts are written in the **same** compare-and-set as the
        counters, so a crash between the debit and the execution leaves the
        identity recorded and the allowance spent.
        """
        snap = self._snapshot(session)                       # re-validates ownership after any wait
        persisted = self._restore(snap, session.scope.turn_id)
        if not session.owns_persisted(persisted):
            raise _Stop(ac.StopReason.CONCURRENT_INVOCATION.value,
                        expected={"steps_used": session.progress.steps_used,
                                  "tool_calls_used": session.progress.tool_calls_used},
                        found=({"steps_used": persisted.steps_used,
                                "tool_calls_used": persisted.tool_calls_used} if persisted else None))
        self._check_deadline(session, snap.db_now)
        if steps and session.progress.steps_used + steps > session.limits.max_steps:
            raise _Stop(ac.StopReason.BUDGET_EXHAUSTED.value, limit="max_steps",
                        steps_used=session.progress.steps_used)
        advanced = session.advanced(steps=steps, tool_calls=tool_calls, phase=phase, admitted=admitted)
        payload = ac.public_copy(snap.state_payload)
        payload[ac.AGENT_LOOP_STATE_KEY] = advanced.to_payload()
        try:
            commit = self._foundation.commit_state(
                tenant_id=session.scope.tenant_id, namespace=session.scope.namespace,
                conversation_id=session.scope.conversation_id, token=session.token,
                turn_id=session.scope.turn_id, expected_revision=snap.state_revision, payload=payload)
        except c.StateConflict as exc:
            raise _Stop(ac.StopReason.CONCURRENT_INVOCATION.value, rejection=exc.reason.value,
                        expected_revision=snap.state_revision) from exc
        except c.OwnershipRejected as exc:
            raise _Stop(ac.StopReason.OWNERSHIP_LOST.value, rejection=exc.reason.value) from exc
        session.commit_progress(advanced, commit.revision)
        session.debited_here.update(admitted)
        session.record("attempt_debited", {"steps_used": advanced.steps_used,
                                           "tool_calls_used": advanced.tool_calls_used,
                                           "revision": commit.revision,
                                           **({"charged": list(admitted)} if admitted else {})})

    # ── Durable outcomes ─────────────────────────────────────────────────────

    def _accept(self, session: "_Session", draft: ac.ReplyDraft,
                fault_before_commit: Optional[Callable[[], None]]) -> ac.LoopOutcome:
        """Persist the accepted reply state and its delivery intent atomically."""
        scope = session.scope
        snap = self._snapshot(session)                       # ownership re-validated after the last await
        if snap.eligible_turn_id != scope.turn_id:
            raise _Stop(ac.StopReason.TURN_NOT_ELIGIBLE.value, eligible_turn_id=snap.eligible_turn_id)
        existing = self._ledgers.get_delivery_sequence(tenant_id=scope.tenant_id, namespace=scope.namespace,
                                                       turn_id=scope.turn_id)
        if existing is not None:
            session.record("delivery_intent_reused", {"sequence_id": existing.sequence_id})
            return session.outcome(status=ac.LoopStatus.PENDING_DELIVERY.value,
                                   delivery_sequence_id=existing.sequence_id, reused=True,
                                   revision=snap.state_revision, detail={"delivery_kind": existing.intent_kind})
        persisted = self._restore(snap, scope.turn_id)
        if not session.owns_persisted(persisted):
            raise _Stop(ac.StopReason.CONCURRENT_INVOCATION.value,
                        found=({"steps_used": persisted.steps_used,
                                "tool_calls_used": persisted.tool_calls_used} if persisted else None))
        accepted = session.advanced(phase=ac.LoopPhase.REPLY_PENDING_DELIVERY.value)
        payload = ac.public_copy(snap.state_payload)
        payload[ac.AGENT_LOOP_STATE_KEY] = accepted.to_payload()
        payload["reply"] = {"text": draft.text, "kind": draft.kind, "evidence_refs": list(draft.evidence_refs)}
        transition = c.StateTransition(expected_revision=snap.state_revision, payload=payload)
        delivery_payload = ac.public_copy(draft.payload)
        delivery_payload["text"] = draft.text
        delivery_payload["evidence_refs"] = list(draft.evidence_refs)
        deadline_at = session.progress.deadline_at

        def deadline_still_open(conn: Connection, locked: c.ConversationSnapshot) -> None:
            # F2: the authoritative recheck, on the database clock read after the
            # row lock and before this transaction writes anything.
            if locked.db_now >= deadline_at:
                raise _DeadlinePassed(locked.db_now, deadline_at)

        try:
            decision = self._ledgers.commit_turn_decision(
                tenant_id=scope.tenant_id, namespace=scope.namespace, conversation_id=scope.conversation_id,
                token=session.token, turn_id=scope.turn_id, state_transition=transition,
                delivery_intent=lc.DeliveryIntent(kind=draft.kind, payload=delivery_payload),
                precondition=deadline_still_open, _fault_before_commit=fault_before_commit,
            )
        except _DeadlinePassed as exc:
            raise _Stop(ac.StopReason.DEADLINE_EXCEEDED.value, at="reservation_boundary",
                        deadline_at=exc.deadline_at.isoformat(), db_now=exc.db_now.isoformat()) from exc
        except c.StateConflict as exc:
            raced = self._ledgers.get_delivery_sequence(tenant_id=scope.tenant_id, namespace=scope.namespace,
                                                        turn_id=scope.turn_id)
            if raced is None:
                raise _Stop(ac.StopReason.CONCURRENT_INVOCATION.value, rejection=exc.reason.value) from exc
            session.record("delivery_intent_reused", {"sequence_id": raced.sequence_id, "raced": True})
            return session.outcome(status=ac.LoopStatus.PENDING_DELIVERY.value,
                                   delivery_sequence_id=raced.sequence_id, reused=True, revision=None,
                                   detail={"delivery_kind": raced.intent_kind, "raced": True})
        except c.OwnershipRejected as exc:
            raise _Stop(ac.StopReason.OWNERSHIP_LOST.value, rejection=exc.reason.value) from exc
        sequence = decision.delivery
        assert sequence is not None
        session.commit_progress(accepted, decision.state.revision if decision.state else snap.state_revision)
        session.record("delivery_intent_reserved", {"sequence_id": sequence.sequence_id,
                                                    "kind": sequence.intent_kind})
        return session.outcome(status=ac.LoopStatus.PENDING_DELIVERY.value,
                               delivery_sequence_id=sequence.sequence_id, reused=False,
                               revision=session.revision,
                               detail={"delivery_kind": sequence.intent_kind,
                                       "evidence_refs": list(draft.evidence_refs)})

    def _stop(self, session: "_Session", stop: _Stop) -> ac.LoopOutcome:
        """Record the stop durably when ownership still allows it, and always return.

        This method never raises: ownership lost while persisting a stop is
        itself an outcome, not an exception escaping the loop. Stops caused by
        lost ownership, a turn outside the scope, an ineligible or completed
        turn, or a concurrent invocation write nothing at all.

        When ownership turns out to be gone while the stop is being recorded,
        the returned reason becomes ``ownership_lost`` and the original reason
        is kept in the detail: the caller's own conclusion was never made
        durable, and another owner may already be working the turn.
        """
        session.record("stopped", {"reason": stop.reason, **stop.detail})
        reason = stop.reason
        detail: Dict[str, Any] = dict(stop.detail)
        revision: Optional[int] = None
        silent = {ac.StopReason.OWNERSHIP_LOST.value, ac.StopReason.TURN_NOT_ELIGIBLE.value,
                  ac.StopReason.TURN_COMPLETED.value, ac.StopReason.TURN_NOT_IN_SCOPE.value,
                  ac.StopReason.CONCURRENT_INVOCATION.value}
        if stop.reason not in silent and session.progress is not None:
            try:
                snap = self._snapshot(session)
                persisted = self._restore(snap, session.scope.turn_id)
                if not session.owns_persisted(persisted):
                    raise _Stop(ac.StopReason.CONCURRENT_INVOCATION.value)
                stopped = session.advanced(phase=ac.LoopPhase.STOPPED.value, stop_reason=stop.reason)
                payload = ac.public_copy(snap.state_payload)
                payload[ac.AGENT_LOOP_STATE_KEY] = stopped.to_payload()
                commit = self._foundation.commit_state(
                    tenant_id=session.scope.tenant_id, namespace=session.scope.namespace,
                    conversation_id=session.scope.conversation_id, token=session.token,
                    turn_id=session.scope.turn_id, expected_revision=snap.state_revision, payload=payload)
                revision = commit.revision
                session.commit_progress(stopped, commit.revision)
            except _Stop as nested:
                session.record("stop_not_persisted", {"reason": nested.reason, **nested.detail})
                if nested.reason == ac.StopReason.OWNERSHIP_LOST.value:
                    reason, detail = nested.reason, {**nested.detail, "original_reason": stop.reason,
                                                     "original_detail": dict(stop.detail)}
                else:
                    detail["not_persisted"] = nested.reason
            except c.CommerceRuntimeError as exc:
                rejection = getattr(exc, "reason", None)
                rejected_as = rejection.value if rejection is not None else type(exc).__name__
                session.record("stop_not_persisted", {"rejection": rejected_as})
                if isinstance(exc, c.OwnershipRejected) and not isinstance(exc, c.StateConflict):
                    reason, detail = (ac.StopReason.OWNERSHIP_LOST.value,
                                      {"rejection": rejected_as, "original_reason": stop.reason,
                                       "original_detail": dict(stop.detail)})
                else:
                    detail["not_persisted"] = rejected_as
        return session.outcome(status=ac.LoopStatus.STOPPED.value, stop_reason=reason,
                               delivery_sequence_id=None, reused=False, revision=revision, detail=detail)

    # ── Reads ────────────────────────────────────────────────────────────────

    def _snapshot(self, session: "_Session") -> c.ConversationSnapshot:
        """Read the conversation and judge the token against it on the database clock."""
        scope = session.scope
        snap = self._foundation.get_conversation(tenant_id=scope.tenant_id, namespace=scope.namespace,
                                                 conversation_id=scope.conversation_id)
        reason = c.classify_rejection(
            current_owner=snap.lease_owner, current_fence=snap.lease_fence, current_epoch=snap.ownership_epoch,
            current_expires_at=snap.lease_expires_at, current_revision=snap.state_revision, token=session.token,
            db_now=snap.db_now,
        )
        if reason is not None:
            raise _Stop(ac.StopReason.OWNERSHIP_LOST.value, rejection=reason.value)
        session.observe_clock(snap.db_now)
        return snap

    def _turn_in_scope(self, session: "_Session") -> c.TurnRecord:
        """The turn must belong to this tenant, namespace *and* conversation."""
        scope = session.scope
        for turn in self._foundation.list_turns(tenant_id=scope.tenant_id, namespace=scope.namespace,
                                                conversation_id=scope.conversation_id):
            if turn.turn_id == scope.turn_id:
                return turn
        raise _Stop(ac.StopReason.TURN_NOT_IN_SCOPE.value, turn_id=scope.turn_id,
                    conversation_id=scope.conversation_id)

    @staticmethod
    def _restore(snap: c.ConversationSnapshot, turn_id: int) -> Optional[ac.LoopProgress]:
        return ac.LoopProgress.from_payload(snap.state_payload.get(ac.AGENT_LOOP_STATE_KEY), turn_id=turn_id)

    @staticmethod
    def _check_deadline(session: "_Session", db_now: _dt.datetime) -> None:
        if db_now >= session.progress.deadline_at:
            raise _Stop(ac.StopReason.DEADLINE_EXCEEDED.value, at="debit_boundary",
                        deadline_at=session.progress.deadline_at.isoformat(), db_now=db_now.isoformat())

    def _existing_work(self, session: "_Session") -> Optional[ac.LoopOutcome]:
        """Inspect state and ledger work before reasoning again.

        Reached only for a turn already proven to belong to this scope. It
        returns read-only outcomes and writes nothing: reuse grants no dispatch
        and no write authority.
        """
        scope = session.scope
        terminal = self._foundation.get_terminal(tenant_id=scope.tenant_id, namespace=scope.namespace,
                                                 turn_id=scope.turn_id)
        if terminal is not None:
            session.record("turn_already_completed", {"processing_outcome": terminal.processing_outcome})
            return session.outcome(status=ac.LoopStatus.STOPPED.value,
                                   stop_reason=ac.StopReason.TURN_COMPLETED.value, delivery_sequence_id=None,
                                   reused=False, revision=None,
                                   detail={"processing_outcome": terminal.processing_outcome})
        summary = self._ledgers.turn_ledger_summary(tenant_id=scope.tenant_id, namespace=scope.namespace,
                                                    turn_id=scope.turn_id)
        sequence = self._ledgers.get_delivery_sequence(tenant_id=scope.tenant_id, namespace=scope.namespace,
                                                       turn_id=scope.turn_id)
        if sequence is not None:
            session.record("delivery_intent_reused", {"sequence_id": sequence.sequence_id,
                                                      "delivery_outcome": summary.delivery_outcome})
            return session.outcome(status=ac.LoopStatus.PENDING_DELIVERY.value,
                                   delivery_sequence_id=sequence.sequence_id, reused=True, revision=None,
                                   detail={"delivery_kind": sequence.intent_kind,
                                           "delivery_outcome": summary.delivery_outcome,
                                           "delivery_attempt_count": sequence.attempt_count})
        if summary.pending_effect_attempts or summary.effects_by_status.get(lc.EffectStatus.UNKNOWN.value):
            session.record("ledger_work_outstanding",
                           {"pending_effect_attempts": summary.pending_effect_attempts,
                            "effects_by_status": dict(summary.effects_by_status)})
            return session.outcome(status=ac.LoopStatus.STOPPED.value,
                                   stop_reason=ac.StopReason.TURN_NOT_ELIGIBLE.value, delivery_sequence_id=None,
                                   reused=False, revision=None,
                                   detail={"effects_by_status": dict(summary.effects_by_status)})
        return None


def _bounded_call(work: Callable[[], Any], wait_seconds: float) -> Any:
    """Run ``work`` in a worker thread and wait at most ``wait_seconds`` for it.

    The wait is enforced here, at the orchestration boundary. On timeout the
    call is abandoned: its thread may still be running and its value, whenever
    it arrives, is discarded. That is local abandonment, not proof that the
    work stopped.
    """
    if wait_seconds <= 0:
        raise concurrent.futures.TimeoutError("no time remained inside the turn's deadline")
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(work)
        return future.result(timeout=wait_seconds)
    finally:
        executor.shutdown(wait=False)


class _Scope:
    __slots__ = ("tenant_id", "namespace", "conversation_id", "turn_id")

    def __init__(self, tenant_id: int, namespace: str, conversation_id: int, turn_id: int) -> None:
        self.tenant_id = tenant_id
        self.namespace = namespace
        self.conversation_id = conversation_id
        self.turn_id = turn_id


class _Session:
    """One invocation's view of a turn: its durable progress and its events."""

    def __init__(self, *, scope: _Scope, token: c.OwnershipToken, requested: ac.LoopBudget, clock: Clock) -> None:
        self.scope = scope
        self.token = token
        self.limits = requested
        self.progress: Optional[ac.LoopProgress] = None
        self.written: Optional[ac.LoopProgress] = None     # what this session last persisted, if anything
        self.revision: Optional[int] = None
        self.observations: List[ac.ToolObservation] = []
        self.feedback: List[ac.VerificationFeedback] = []
        self.debited_here: Set[str] = set()      # signatures this invocation has already charged
        self.events: List[ac.LoopEvent] = []
        self.capabilities = ac.ProviderCapabilities(provider_name="unknown")
        self._clock = clock
        self._db_now: Optional[_dt.datetime] = None
        self._db_read_at: float = clock()

    # progress ------------------------------------------------------------
    def adopt(self, snap: c.ConversationSnapshot, restored: Optional[ac.LoopProgress],
              requested: ac.LoopBudget) -> None:
        """Restore the authoritative progress, or start it for a fresh turn.

        The persisted limits and deadline win: a later invocation can neither
        reset nor enlarge them.
        """
        self.written = restored
        if restored is not None:
            self.progress = restored
            self.limits = restored.limits
            self.observations = [o.restore() for o in restored.observations]
            self.feedback = [ac.VerificationFeedback(step, tuple(ac.VerificationProblem(code, "restored")
                                                                for code in codes))
                             for step, codes in restored.feedback]
            self.record("resumed", {"steps_used": restored.steps_used,
                                    "tool_calls_used": restored.tool_calls_used, "phase": restored.phase,
                                    "limits_restored": True,
                                    "requested_limits_differ": restored.limits != requested})
        else:
            self.limits = requested
            self.progress = ac.LoopProgress(
                turn_id=self.scope.turn_id, phase=ac.LoopPhase.REASONING.value, limits=requested,
                deadline_at=snap.db_now + _dt.timedelta(seconds=requested.deadline_seconds),
                steps_used=0, tool_calls_used=0,
            )
        self.revision = snap.state_revision

    def advanced(self, *, steps: int = 0, tool_calls: int = 0, phase: Optional[str] = None,
                 stop_reason: Optional[str] = None, admitted: Sequence[str] = ()) -> ac.LoopProgress:
        """The next durable progress: the counters advanced and one attempt
        charged to each admitted signature.

        The repeat record is carried forward from the durable one and only ever
        incremented; it is never rebuilt from this invocation's memory, so a
        restored allowance stays spent.
        """
        base = self.progress
        assert base is not None
        return ac.LoopProgress(
            turn_id=base.turn_id, phase=phase or base.phase, limits=base.limits, deadline_at=base.deadline_at,
            steps_used=base.steps_used + steps, tool_calls_used=base.tool_calls_used + tool_calls,
            observations=ac.checkpoint_observations(self.observations),
            feedback=tuple((f.step_no, tuple(p.code for p in f.problems)) for f in self.feedback),
            executed=base.with_attempts(admitted),
            stop_reason=stop_reason,
        )

    def attempts(self, signature: str) -> int:
        """The durable attempts already charged to ``signature`` for this turn."""
        return self.progress.attempts(signature) if self.progress is not None else 0

    def commit_progress(self, progress: ac.LoopProgress, revision: int) -> None:
        self.progress = progress
        self.written = progress
        self.revision = revision

    def owns_persisted(self, persisted: Optional[ac.LoopProgress]) -> bool:
        """Whether the durable progress is exactly what this session last wrote.

        A fresh turn legitimately has nothing persisted; anything else means
        another invocation advanced this turn and this one may not debit on top
        of it.
        """
        if self.written is None:
            return persisted is None
        return self.written.same_debits(persisted)

    # budget --------------------------------------------------------------
    def observe_clock(self, db_now: _dt.datetime) -> None:
        self._db_now = db_now
        self._db_read_at = self._clock()

    def remaining_seconds(self) -> float:
        """Time left before the authoritative deadline, from the last database
        clock read plus the monotonic time since."""
        if self.progress is None or self._db_now is None:
            return float(self.limits.deadline_seconds)
        elapsed_since_read = max(0.0, self._clock() - self._db_read_at)
        return (self.progress.deadline_at - self._db_now).total_seconds() - elapsed_since_read

    def wait_for(self, per_call_limit: float) -> float:
        """Cap a wait to the smaller of its own limit and the time left."""
        return min(float(per_call_limit), self.remaining_seconds())

    def steps_left(self) -> bool:
        return self.progress is not None and self.progress.steps_used < self.limits.max_steps

    def check_cancelled(self, cancelled: Optional[Callable[[], bool]]) -> None:
        if cancelled is not None and cancelled():
            raise _Stop(ac.StopReason.CANCELLED.value)

    def budget_view(self) -> ac.BudgetView:
        assert self.progress is not None
        return ac.BudgetView(
            remaining_steps=max(0, self.limits.max_steps - self.progress.steps_used),
            remaining_tool_calls=max(0, self.limits.max_tool_calls - self.progress.tool_calls_used),
            remaining_seconds=round(max(0.0, self.remaining_seconds()), 3),
        )

    # provider-facing copies ----------------------------------------------
    def provider_observations(self) -> Tuple[ac.ToolObservation, ...]:
        """Observations detached from the ones verification reads."""
        return tuple(
            ac.ToolObservation(call_id=o.call_id, tool_name=o.tool_name, ok=o.ok,
                               result=ac.public_copy(o.result) if o.result is not None else None,
                               error_code=o.error_code, error=o.error, evidence_refs=tuple(o.evidence_refs),
                               restored=o.restored, body_truncated=o.body_truncated)
            for o in self.observations
        )

    def provider_feedback(self) -> Tuple[ac.VerificationFeedback, ...]:
        return tuple(ac.VerificationFeedback(step_no=f.step_no, problems=tuple(f.problems)) for f in self.feedback)

    # bookkeeping ---------------------------------------------------------
    @staticmethod
    def signature(request: ac.ToolRequest) -> str:
        return request.tool_name + ":" + json.dumps(dict(request.arguments), sort_keys=True, ensure_ascii=False)

    def record(self, kind: str, detail: Mapping[str, Any]) -> None:
        step_no = self.progress.steps_used if self.progress is not None else 0
        self.events.append(ac.LoopEvent(step_no=step_no, kind=kind, detail=ac.public_copy(detail)))

    def outcome(self, *, status: str, delivery_sequence_id: Optional[int], reused: bool,
                revision: Optional[int], detail: Mapping[str, Any],
                stop_reason: Optional[str] = None) -> ac.LoopOutcome:
        return ac.LoopOutcome(
            status=status, stop_reason=stop_reason, turn_id=self.scope.turn_id,
            delivery_sequence_id=delivery_sequence_id, reused_delivery=reused, state_revision=revision,
            steps_used=self.progress.steps_used if self.progress is not None else 0,
            tool_calls_used=self.progress.tool_calls_used if self.progress is not None else 0,
            events=tuple(self.events), detail=ac.public_copy(detail),
        )


__all__ = ["AgentLoop"]
