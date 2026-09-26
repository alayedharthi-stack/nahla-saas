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
from core.commerce_runtime import browse as br
from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import navigation as nav
from core.commerce_runtime import presentation_policy as pp
from core.commerce_runtime import reply_card as rcard
from core.commerce_runtime import reply_choices as rc
from core.commerce_runtime import search_candidates as sc
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


# Time kept back for reserving the reply once it is shaped. Asking the model
# for a paged list's words needs the provider's wait, the page-one catalogue
# read and this much left, so the reply is still reserved however the step
# asked for ends; and page one is read only in the time before it.
WORDS_RESERVE_SECONDS = 5.0
# Less than this for page one's read is a read that would only time out and
# spend the binding's session for nothing; the model's selector goes instead.
MIN_PAGE_READ_SECONDS = 1.0

# Stops that end the step asked for a paged list's words without ending the
# turn: the reply verified before it is still the answer. Every other stop —
# cancellation, lost ownership, a concurrent invocation, the deadline — would
# have ended the turn without the question and still does.
_WORDS_FALLBACK_STOPS = frozenset({
    ac.StopReason.BUDGET_EXHAUSTED.value,
    ac.StopReason.PROVIDER_FAILURE.value,
    ac.StopReason.PROVIDER_BLOCKED.value,
    ac.StopReason.PROVIDER_INVALID.value,
    ac.StopReason.PROVIDER_TIMEOUT.value,
    ac.StopReason.UNSUPPORTED_CAPABILITY.value,
})


class AgentLoop:
    """Runs one turn to an accepted reply, or to an explicit stop."""

    # No browse runtime unless one is handed in: the loop then shapes replies
    # exactly as it did before paging existed.
    _browse: Optional[br.BrowseRuntime] = None

    def __init__(self, ledgers: LedgerRepository, registry: at.ToolRegistry, *,
                 budget: Optional[ac.LoopBudget] = None, clock: Optional[Clock] = None,
                 browse: Optional[br.BrowseRuntime] = None) -> None:
        self._ledgers = ledgers
        self._foundation = ledgers.foundation
        self._registry = registry
        self._requested_budget = ac.validate_budget(budget or ac.LoopBudget())
        self._clock = clock or time.monotonic
        # Present only where this database can store a browse's continuation.
        # Absent, a reply offers exactly the selector the model asked for.
        self._browse = browse

    # ── Public entry point ───────────────────────────────────────────────────

    def run_turn(self, *, tenant_id: int, namespace: Any, conversation_id: int, turn_id: int,
                 token: c.OwnershipToken, provider: Any,
                 cancelled: Optional[Callable[[], bool]] = None,
                 presentation: Optional[pp.PresentationContext] = None,
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
            draft = self._reason(provider, context, scope, session, cancelled, _fault_after_tool_debit,
                                 presentation=presentation)
            return self._accept(session, draft, fault_before_commit=_fault_before_commit)
        except _Stop as stop:
            return self._stop(session, stop)

    # ── Reasoning / acting / observing ───────────────────────────────────────

    def _reason(self, provider: Any, context: ac.AuthorizedContext, scope: at.ToolScope, session: "_Session",
                cancelled: Optional[Callable[[], bool]],
                fault_after_tool_debit: Optional[Callable[[], None]] = None,
                presentation: Optional[pp.PresentationContext] = None) -> ac.ReplyDraft:
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
                    offer, more = self._list_left_out(draft, scope, session, presentation)
                    if offer:
                        draft = self._ask_for_list(provider, context, scope, session, cancelled,
                                                   draft, offer, more)
                    missing = self._paging_words_missing(draft, scope, session, presentation)
                    if missing:
                        draft = self._ask_for_words(provider, context, scope, session, cancelled,
                                                    draft, missing)
                    return self._verified(draft, scope, session, presentation)
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

    def _verified(self, draft: ac.ReplyDraft, scope: at.ToolScope, session: "_Session",
                  presentation: Optional[pp.PresentationContext]) -> ac.ReplyDraft:
        """Shape the reply verification accepted, keeping what reshaping it needs.

        The model chose whether to offer a selector and which products belong
        in it; what each row *says* is composed here from this turn's own
        observations, so the structured payload states the merchant's values
        and never the model's. The text is carried through untouched, and a
        selector that cannot be offered whole simply is not.
        """
        session.verified_draft = draft
        session.presentation = presentation
        return self._shape_reply(draft, scope, session, presentation)

    def _paging_words_missing(self, draft: ac.ReplyDraft, scope: at.ToolScope, session: "_Session",
                              presentation: Optional[pp.PresentationContext]) -> Tuple[str, ...]:
        """The words a list the platform will page needs and the model left out.

        Asked only where a list can page at all, only of a selector the model
        requested that ``_shape_reply`` would open a browse for, and only once
        in a turn — also across a resumed invocation, whose restored feedback
        still names the request. Whether the list pages was decided without
        them; this names what the customer would read on it and nobody has
        written. Nothing is asked without two steps left, nor without time for
        the step, the page-one read and the reservation after them: the reply
        then goes as the model asked.
        """
        if self._browse is None or not self._can_ask(session, br.WORDS_NEEDED):
            return ()
        try:
            shape = pp.decide(draft=draft, observations=session.observations,
                              definitions=self._registry.definitions, presentation=presentation)
            if shape.kind != pp.SHAPE_LIST or shape.reason != pp.MODEL_REQUESTED:
                return ()
            eligible, _reason = br.eligibility(draft, session.observations, scope=scope,
                                               search_tool_names=self._browse.search_tool_names)
        except Exception as exc:  # noqa: BLE001 - paging is an affordance; the answer still goes
            session.record("browse_failed", {"error": type(exc).__name__})
            return ()
        return eligible.words_missing(draft) if eligible is not None else ()

    def _can_ask(self, session: "_Session", code: str) -> bool:
        """Whether this turn may still ask the model one question under ``code``."""
        return not self._ask_blocker(session, code)

    def _ask_blocker(self, session: "_Session", code: str) -> str:
        """Why this turn may not ask the model one question under ``code``, or "".

        Once per turn — also across a resumed invocation, whose restored
        feedback still names the question — and only with two steps left (the
        one asked for, and one more a resumed invocation can still answer with)
        and time for the step, a page-one read and the reservation after them.
        """
        if session.progress is None:
            return "no_progress"
        if any(p.code == code for f in session.feedback for p in f.problems):
            return "already_asked"
        if session.limits.max_steps - session.progress.steps_used < 2:
            return "no_steps"
        needed = (session.limits.provider_timeout_seconds + session.limits.tool_timeout_seconds
                  + WORDS_RESERVE_SECONDS)
        if session.remaining_seconds() < needed:
            return "no_time"
        return ""

    def _list_left_out(self, draft: ac.ReplyDraft, scope: at.ToolScope, session: "_Session",
                       presentation: Optional[pp.PresentationContext]) -> Tuple[Tuple[int, ...], bool]:
        """The products the platform decided to list and the model's reply did not offer.

        All of these, read from structure alone:

        * the presentation policy itself decided a list — several products this
          turn's search returned, no single focus, no shape the model asked for;
        * no product was read deliberately this turn (two such reads are a
          comparison, never widened into the search they came from);
        * the reply itself cites at least two of the products the most recent
          search showed the model that can be bought now — an answer about one
          product, or about none, is not an offer to choose.

        The products are that search's buyable ones, in its order; the flag
        says whether its stored result holds more than it showed (a "More" row
        can exist). A question that is due but cannot be asked — no step or no
        time left — is recorded as skipped with its reason. ``()`` otherwise.
        """
        try:
            shape = pp.decide(draft=draft, observations=session.observations,
                              definitions=self._registry.definitions, presentation=presentation)
            if shape.kind != pp.SHAPE_LIST or shape.reason != pp.MULTIPLE_CANDIDATES:
                return (), False
            if pp.provenance(session.observations, self._registry.definitions).focused:
                return (), False
            offer, more = _latest_search_offer(
                session.observations, self._registry.definitions, scope,
                self._browse.search_tool_names if self._browse is not None else ())
            cited = set(getattr(draft, "evidence_refs", ()) or ())
            if len([pid for pid in offer if rc.product_ref(pid) in cited]) < rc.MIN_CHOICES:
                return (), False
        except Exception as exc:  # noqa: BLE001 - asking is an affordance; the answer still goes
            session.record("list_offer_failed", {"error": type(exc).__name__})
            return (), False
        blocker = self._ask_blocker(session, rc.LIST_OFFER_NEEDED)
        if blocker:
            if blocker != "already_asked":
                session.record("list_offer_skipped", {"reason": blocker})
            return (), False
        return offer, more

    def _ask_for_list(self, provider: Any, context: ac.AuthorizedContext, scope: at.ToolScope,
                      session: "_Session", cancelled: Optional[Callable[[], bool]],
                      draft: ac.ReplyDraft, offer: Sequence[int], more: bool) -> ac.ReplyDraft:
        """One more step for the selector the platform decided and the model left out.

        The same question-and-answer ``_ask_for_words`` runs, with its one
        problem: the products, the words a list needs, and that the decision
        whether the answer is such an offer stays the model's. Whatever comes
        back that verification accepts is the answer — with a selector or
        without one — and anything else leaves the reply as it was.
        """
        session.record("list_offer_requested", {"products": [int(p) for p in offer],
                                                "more_results": bool(more)})
        problem = ac.VerificationProblem(rc.LIST_OFFER_NEEDED,
                                         rc.list_offer_detail(offer, more_results=more))
        return self._ask_once(provider, context, session, cancelled, draft, problem,
                              answered_event="list_offer_answer")

    def _ask_for_words(self, provider: Any, context: ac.AuthorizedContext, scope: at.ToolScope,
                       session: "_Session", cancelled: Optional[Callable[[], bool]],
                       draft: ac.ReplyDraft, missing: Sequence[str]) -> ac.ReplyDraft:
        """One more step for the words a paged list needs, and the reply to shape.

        The model is shown its reply as not yet accepted, with the one problem
        that it lacks ``missing`` — the same channel verification speaks on —
        and given exactly one step to answer. A verified reply from that step is
        the answer. Anything else — no reply, a lookup instead of a reply, a
        reply verification refuses, a provider that fails or times out — leaves
        the reply already verified as the answer, shaped as the model asked it,
        so asking never leaves the customer worse off than not asking. Only a
        stop that would have ended the turn anyway — cancellation, lost
        ownership, a concurrent invocation, the deadline — still ends it.
        """
        session.record("paging_words_requested", {"missing": list(missing)})
        problem = ac.VerificationProblem(br.WORDS_NEEDED, br.words_needed_detail(missing))
        return self._ask_once(provider, context, session, cancelled, draft, problem,
                              answered_event="paging_words_answer")

    def _ask_once(self, provider: Any, context: ac.AuthorizedContext, session: "_Session",
                  cancelled: Optional[Callable[[], bool]], draft: ac.ReplyDraft,
                  problem: ac.VerificationProblem, *, answered_event: str) -> ac.ReplyDraft:
        """Show the model its verified reply as not yet accepted, with one problem.

        Exactly one step is given. A verified reply from it is the answer;
        anything else — no reply, a lookup instead of a reply, a reply
        verification refuses, a provider that fails or times out — leaves
        ``draft`` as the answer, so asking never leaves the customer worse off
        than not asking. Only a stop that would have ended the turn anyway —
        cancellation, lost ownership, a concurrent invocation, the deadline —
        still ends it.
        """
        session.feedback.append(ac.VerificationFeedback(
            step_no=session.progress.steps_used, problems=(problem,)))
        answer: Optional[ac.ReplyDraft] = None
        outcome = "not_run"
        try:
            session.check_cancelled(cancelled)
            self._debit(session, steps=1, phase=ac.LoopPhase.REASONING.value)
            request = ac.ProviderRequest(
                step_no=session.progress.steps_used, context=context, tools=self._registry.definitions,
                observations=session.provider_observations(), feedback=session.provider_feedback(),
                budget=session.budget_view(),
            )
            result = self._provider_step(provider, request, session)
            session.check_cancelled(cancelled)
            if isinstance(result, ac.ProviderReply):
                problems = ac.verify_reply_draft(result.draft, session.observations,
                                                 inbound=context.inbound)
                if problems:
                    outcome = "verification_failed"
                    session.record("verification_failed", {"problems": [p.code for p in problems]})
                else:
                    answer, outcome = result.draft, "answered"
            else:
                # A lookup, or a step cut off before it finished. Neither is
                # run on: the question was for the words, and one step was all
                # it was given.
                outcome = type(result).__name__
        except _Stop as stop:
            outcome = stop.reason
            if stop.reason not in _WORDS_FALLBACK_STOPS:
                raise
        finally:
            # Recorded however the step ended, a stop that ends the turn included.
            session.record(answered_event, {"outcome": outcome})
        return answer if answer is not None else draft

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

    def _shape_reply(self, draft: ac.ReplyDraft, scope: at.ToolScope, session: "_Session",
                     presentation: Optional[pp.PresentationContext], *,
                     navigation_enabled: bool = True) -> ac.ReplyDraft:
        """Give the verified answer the shape the platform decides it takes.

        The whole presentation boundary, in one place, so there is one path to
        read and one path to test: decide the shape from structured provenance,
        read a tapped product if one was selected, then compose the structured
        payloads. Called only after ``verify_reply_draft`` passed, so nothing
        here is a truth decision.

        A list the platform composes itself — a later page a verified "More"
        tap opened, or page one of a browse the model asked to be continuable —
        is composed here too, and the navigation writes it needs are handed to
        ``_accept`` to make inside the reply's own reservation. With
        ``navigation_enabled`` off, as after those writes were refused, the
        reply is shaped exactly as it would be without paging.

        The model's text is not touched on any branch but one: a selector that
        stands down for something the customer selected is carried as lines of
        the merchant's values under it. What changes is otherwise only which
        structured payload the reply carries, and the reason is recorded either
        way so a production turn's shape is auditable from the log alone.
        """
        shape = pp.decide(draft=draft, observations=session.observations,
                          definitions=self._registry.definitions, presentation=presentation)
        shape = self._hydrate(shape, scope, session)
        session.navigation_plan = nav.Plan()
        page = presentation.browse_page if presentation is not None else None
        composed: Optional[br.Composed] = None
        browse_outcome = ""
        try:
            if shape.reason == pp.NAVIGATION_PAGE and page is not None:
                if navigation_enabled:
                    composed = br.continue_browse(page)
                    browse_outcome = composed.reason
                else:
                    # The page could not be spent with this reply. Its products,
                    # read moments ago, still follow the model's text as lines, so
                    # the answer to "More" is not an empty sentence.
                    draft = br.page_as_lines(draft, page)
                    browse_outcome = br.PAGE_AS_LINES
            elif (navigation_enabled and self._browse is not None and shape.kind == pp.SHAPE_LIST
                  and shape.reason == pp.MODEL_REQUESTED):
                # Page one is read only in the time before the reservation's
                # own; a list that cannot be read in it is the model's selector.
                read_for = min(float(session.limits.tool_timeout_seconds),
                               session.remaining_seconds() - WORDS_RESERVE_SECONDS)
                if read_for >= MIN_PAGE_READ_SECONDS:
                    composed, browse_outcome = br.open_browse(
                        draft, session.observations, scope=scope, runtime=self._browse,
                        timeout_seconds=read_for)
                else:
                    browse_outcome = br.NO_TIME
        except Exception as exc:  # noqa: BLE001 - paging is an affordance; the answer still goes
            # A composition that fails for any reason is a list not paged, never
            # a turn not answered: the reply is shaped as it would be without it.
            session.record("browse_failed", {"error": type(exc).__name__})
            composed, browse_outcome = None, br.BROWSE_FAILED

        # Rows the customer already has on screen: sent earlier in this
        # conversation, or on the page this reply answers with. A stood-down
        # selector's options that are among them are not repeated as lines.
        sent_rows = tuple(presentation.rows_already_sent) if presentation is not None else ()
        if composed is not None and composed.selection is not None:
            draft, choices = rc.finalize_composed(
                draft, session.observations, composed.selection, composed.reason,
                navigation=composed.navigation, row_refs=composed.row_refs,
                stand_down=rc.NAVIGATION_ANSWERED_FIRST if shape.reason == pp.NAVIGATION_PAGE else "",
                already_listed=composed.already_listed + sent_rows)
            session.navigation_plan = composed.plan
        else:
            if composed is not None:
                # A later page with nothing left to show and nothing after it.
                # The token it named is still spent with this reply.
                session.navigation_plan = composed.plan
            # A requested selector stands down only for a card that will
            # actually be there. Asked of the composer itself, before anything
            # is changed, so a customer whose tapped product turns out to have
            # no photo still gets the selector the model offered rather than
            # neither shape.
            withhold = ""
            if shape.withhold_selector and shape.reason == pp.NAVIGATION_PAGE:
                # The tapped page is still the answer, as lines or not at all;
                # the model's own selector stands down for it exactly as it
                # does when the page is a list.
                withhold = rc.NAVIGATION_ANSWERED_FIRST
            elif shape.withhold_selector:
                card_composed, _reason = rcard.card(draft, session.observations,
                                                    determined_product_id=shape.determined_product_id)
                withhold = rc.TAP_ANSWERED_FIRST if card_composed is not None else ""
            on_screen: Tuple[int, ...] = ()
            if withhold == rc.NAVIGATION_ANSWERED_FIRST:
                page_ids = tuple(int(p.get("product_id") or 0) for p in (page.products if page else ()))
                on_screen = page_ids + sent_rows
            draft, choices = rc.finalize(draft, session.observations, withhold=withhold,
                                         already_listed=on_screen)
        # A card is the same split for the shape that follows a choice rather
        # than offering one. The selector wins when both are on the table: a
        # customer who still has to choose is not helped by one product's photo.
        selector_offered = choices == rc.OFFERED or (composed is not None
                                                     and composed.selection is not None)
        draft, card = rcard.finalize(
            draft, session.observations, selector_offered=selector_offered,
            determined_product_id=shape.determined_product_id)
        # A determination that failed closed never reached ``card`` with a
        # product, so it has no reason of its own to carry. Name it here, on the
        # payload production persists, or the turn reads as one that simply
        # never wanted a card.
        draft = rcard.note_withheld(draft, pp.withheld_reason(shape))
        session.record("reply_accepted",
                       {"evidence_refs": list(draft.evidence_refs), "kind": draft.kind,
                        "choices": choices, "card": card,
                        "shape": shape.kind, "shape_reason": shape.reason,
                        **({"browse": browse_outcome} if browse_outcome else {}),
                        **({"navigation": dict(composed.navigation)}
                           if composed is not None else {})})
        return draft

    def _hydrate(self, shape: pp.Shape, scope: at.ToolScope, session: "_Session") -> pp.Shape:
        """Read the product a verified tap selected, through the tools' own contract.

        A tap is the most explicit product selection the channel allows, so the
        card that follows it must not depend on the model having happened to
        look the product up. The platform therefore reads it itself — by running
        the registry's own ``get_product_details`` under this turn's scope, so
        tenant isolation, the product-identity guard, the merchant's current
        catalogue and the evidence reference all come with it. None of that is
        restated here; this call *is* the contract.

        It is not debited against the model's tool budget and does not need to
        be: it is the platform's own read, at most one per turn, made after the
        model has already finished, and it is read-only — a re-entry that makes
        it again changes nothing. A read that fails is not a failure of the
        turn: ``after_hydration`` fails the *card* closed with a named reason
        and the answer goes out as text.
        """
        request = pp.hydration_request(shape)
        if request is None or pp.already_read(shape, session.observations):
            return pp.after_hydration(shape, session.observations)
        wait = session.wait_for(session.limits.tool_timeout_seconds)
        observation = self._registry.execute(scope, request, timeout_seconds=wait)
        session.observations.append(observation)
        session.record("platform_hydration",
                       {"tool": observation.tool_name, "ok": observation.ok,
                        "error_code": observation.error_code,
                        "product_id": shape.product_id,
                        "evidence_refs": list(observation.evidence_refs)})
        return pp.after_hydration(shape, session.observations)

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
        """Persist the accepted reply, its delivery intent and its navigation atomically.

        A reply carrying a paged list reserves its navigation writes — the
        tapped token spent, the next one minted — in the same transaction. When
        the store refuses them (the token was spent or expired after it was
        read, or the next could not be written), nothing at all was written:
        the reply is shaped again without the navigation and reserved once
        more, so the customer is still answered and no row points at a page
        that was never sent.
        """
        plan = session.navigation_plan
        try:
            return self._accept_once(session, draft, fault_before_commit, plan)
        except nav.NavigationNotPersisted as refused:
            session.record("navigation_not_persisted", {"reason": refused.reason})
            scope = session.scope
            reshaped = self._shape_reply(
                session.verified_draft or draft,
                at.ToolScope(tenant_id=scope.tenant_id, namespace=scope.namespace,
                             conversation_id=scope.conversation_id, turn_id=scope.turn_id),
                session, session.presentation, navigation_enabled=False)
            return self._accept_once(session, reshaped, fault_before_commit, nav.Plan())

    def _accept_once(self, session: "_Session", draft: ac.ReplyDraft,
                     fault_before_commit: Optional[Callable[[], None]],
                     plan: nav.Plan) -> ac.LoopOutcome:
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
            # The page this reply shows is spent, and the next one minted, on
            # this same locked connection: they commit with the reply or not at
            # all. A refusal raises out of here and nothing is written.
            if plan:
                nav.apply(conn, plan, tenant_id=scope.tenant_id, namespace=scope.namespace,
                          conversation_id=scope.conversation_id, turn_id=scope.turn_id,
                          db_now=locked.db_now)

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
        # The verified draft before it was shaped, and what shaping it needs,
        # kept so a reply whose navigation writes were refused can be shaped
        # again without them. ``navigation_plan`` is what the reservation writes.
        self.verified_draft: Optional[ac.ReplyDraft] = None
        self.presentation: Optional[pp.PresentationContext] = None
        self.navigation_plan: nav.Plan = nav.Plan()
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


def _latest_search_offer(observations: Sequence[Any], definitions: Sequence[Any], scope: Any,
                         search_tool_names: Sequence[str]) -> Tuple[Tuple[int, ...], bool]:
    """The products the most recent search showed the model that can be bought
    now, in its order, and whether that search's stored result holds more than
    it showed — so a "More" row, and the word for it, can exist.

    Read from the observation body the model was given and the platform's own
    typed result of the same call; a product the model was shown as not
    orderable is never added by the platform. Fewer than two leaves nothing.
    """
    kinds = {str(getattr(d, "name", "") or ""): str(getattr(d, "result_kind", "") or "")
             for d in definitions or ()}
    for obs in reversed(list(observations or ())):
        if not getattr(obs, "ok", False) or getattr(obs, "body_truncated", False):
            continue
        if kinds.get(str(getattr(obs, "tool_name", "") or "")) != pp.CANDIDATE_KIND:
            continue
        result = getattr(obs, "result", None)
        observed = rc.observed_products([obs])
        offer = tuple(pid for pid in sc.window_ids(result)
                      if pid in observed and bool(observed[pid].get("orderable")))[:rc.MAX_CHOICES]
        if len(offer) < rc.MIN_CHOICES:
            return (), False
        more = False
        if search_tool_names:
            candidates, _why = sc.from_observation(
                obs, tenant_id=scope.tenant_id, namespace=scope.namespace,
                conversation_id=scope.conversation_id, turn_id=scope.turn_id,
                search_tool_names=search_tool_names)
            more = bool(candidates is not None and candidates.extends_beyond_window)
        return offer, more
    return (), False


__all__ = ["AgentLoop"]
