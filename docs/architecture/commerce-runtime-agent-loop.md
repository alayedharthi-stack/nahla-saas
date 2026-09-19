# Commerce runtime — dormant agent loop core (contract)

Status: recorded 2026-09-19 as the contract of the third dormant slice;
corrected the same day after the independent review of PR #1095 (durable
attempt accounting, enforced waits and the reservation deadline, scope and
eligibility boundaries, complete boundary validation, isolation of
authoritative data, closed ownership-loss outcomes), and again for the repeat
policy (duplicates inside one bundle; a durable, crash-safe recovery
allowance). Extends
`commerce-runtime-foundation-contract.md` and
`commerce-runtime-effect-and-delivery-ledgers.md`; nothing here reopens the
closed findings of either. Authority for the implementation in
`backend/core/commerce_runtime/{agent_contracts,agent_tools,agent_scripted,agent_loop}.py`.

The loop core itself is unchanged by what came after it. Its provider boundary
is now also implemented against a live model, its fixture tools now have real
read-only counterparts, and its reserved delivery intents are now dispatched —
each in its own contract: `commerce-runtime-live-integration.md` and
`commerce-runtime-pilot-activation.md`. "Dormant" in this document's title
describes the slice as it was merged, not the package today.

## 1. Purpose and boundary

One loop accepts one eligible conversation turn, obtains a reasoning
decision, executes permitted read-only tools, returns the observations to the
reasoning provider, validates the resulting reply draft and durably hands an
accepted reply to the delivery ledger as a **delivery intent**.

The slice is dormant. It is wired into no startup path, webhook, worker,
scheduler, tenant routing or customer traffic. It calls no model: the only
provider in the slice is a deterministic script used by tests. It sends
nothing, reconciles nothing, and executes no mutating business tool.

**Ownership of the architecture is unchanged:** Nahla owns the loop, the
tools, the state, the execution policy, the deadlines and delivery. The
reasoning provider represents **one inference step** and owns none of them.

## 2. Single-step reasoning boundary

`ProviderRequest` carries the authorized context (tenant, namespace,
conversation, turn, the inbound payload and the conversation state as data),
the available tool definitions, the prior observations, prior verification
feedback and the remaining execution budget. A provider declares
`ProviderCapabilities`; the declaration itself is validated, and the loop
never assumes more than it declares.

`ProviderResult` is a closed tagged union that distinguishes, explicitly:

| Result | Meaning | Loop behaviour |
| --- | --- | --- |
| `ProviderToolRequests` | wants one or more tools | validate the whole bundle, authorize, debit, execute, observe, continue |
| `ProviderReply` | a reply draft | validate, verify, accept or feed back |
| `ProviderFailure` | could not produce a step | stop, `provider_failure` |
| `ProviderBlocked` | declined (policy) | stop, `provider_blocked`; not retried |
| `ProviderInvalid` | incomplete or malformed output | stop, `provider_invalid`; never treated as a reply |

**Model tool-call ids are correlation only.** `call_id` matches a request to
its observation, and call ids within one bundle must be distinct. A call id is
never a business idempotency key (those are the ledger's `derive_business_key`
values), never an evidence reference and never an authorization credential.

### 2.1 Complete validation before anything runs

A provider result is validated **whole, before any part of it is executed**:
the union variant, every nested field, the capability declaration, the tool
request collection and each request's argument shape and serializability, the
reply draft's text, kind, payload and evidence collection, and the
non-emptiness of a failure reason. Consequences:

* a bundle that mixes a valid and a malformed request executes **zero** tools;
* shapes such as `ProviderToolRequests(None)`, `evidence_refs=None` and a
  non-serializable argument become the declared outcome `provider_invalid`,
  never an incidental `TypeError` leaving the loop;
* a capability the provider did not declare becomes `unsupported_capability`
  before execution;
* the attempt already debited for that step **stays debited**: rejecting the
  output does not refund the attempt.

## 3. Tools

The loop exposes an **allowlist** of read-only fixture tools: `catalog_search`,
`product_lookup`, `merchant_knowledge_lookup`. Each declares a name, a
description, a JSON-schema input (`additionalProperties: false`), a result
kind and closed error codes (`unknown_tool`, `invalid_arguments`,
`scope_override_refused`, `not_read_only`, `timeout`, `tool_failure`,
`result_too_large`). A tool definition that is not read-only cannot be
registered.

* **Scope comes from the trusted runtime context.** The loop builds a
  `ToolScope` from the turn it was given. Model arguments naming `tenant_id`,
  `namespace`, `conversation_id`, `turn_id`, `token`, `owner_id` and similar
  are refused (`scope_override_refused`) before the tool runs.
* **Tool results are data.** They carry evidence references the verifier can
  check. They acquire no instruction or authorization authority, and a failed
  or timed-out call contributes no evidence.

### 3.1 Isolation of authoritative data

The registry owns the authoritative schema of every tool as a **private deep
copy** and never hands it out. `definitions` builds a detached copy on each
call, and validated arguments, observation bodies, the authorized context and
every event detail are detached copies too. A frozen dataclass does not
protect the nested dictionaries inside it; these copies do. Consequently a
holder of a provider-visible definition can widen a declared maximum, change a
declared type or empty the required list and **nothing changes** about what
the registry enforces, what the conversation state holds, or what the inbound
turn says.

## 4. Verification of the reply draft

Before a draft may be handed to delivery, deterministic **structural** checks
run:

* every cited evidence reference must exist among **this turn's** successful
  observations (including observations restored from a durable checkpoint);
* a draft whose `claims_commerce_facts` flag is set must cite at least one
  reference;
* the text must be present and within bounds.

A correctable failure becomes `VerificationFeedback` fed into the next
reasoning step **within the same budget**; when no step remains the loop stops
with `verification_failed` and reserves nothing.

**What this proves, stated exactly.** It proves that each cited reference was
produced by a tool call made in this turn, inside this tenant's scope, and
that a draft declaring commerce facts cites at least one such reference. It
proves nothing about the reply's content: reference membership is **not**
evidence that a sentence is factually accurate, that the cited data supports
what the sentence says, or that the reply answers the customer's question
completely. `claims_commerce_facts` is a **provider-declared** flag, so a
provider that does not set it is not checked for citations at all. Semantic
grounding, answer completeness and truthful flagging are not established by
this slice.

## 5. Durable attempt accounting

Every attempt is paid for **before** the work it pays for runs. Before a
provider call, and before a bundle of tool calls, the loop writes the consumed
attempts into the conversation's versioned state under the reserved key
`agent_loop`, through the foundation's compare-and-set state commit.

* **Bound to one revision.** The counters are computed from the progress found
  at revision *R* and written with a compare-and-set on *R*. The loop never
  reads a fresh revision and overwrites it with counters derived from an older
  snapshot.
* **Arbitration, including within one ownership epoch.** A debit is refused
  (`concurrent_invocation`) when the durable progress is not the one this
  invocation last wrote, or when the compare-and-set loses. The comparison is
  narrow and deliberate: it matches the **turn id and the two consumed
  counters**, which are what a debit is bound to, not the whole checkpoint.
  Two callers holding the same token therefore cannot execute on the same
  debit, nor spend the same repeat allowance; the loser's conclusions are
  discarded and the winner's progress is preserved.
* **Survives a crash.** A process that dies after a provider or tool call but
  before the acceptance commit leaves its debits behind and leaves no reply
  state and no delivery sequence.
* **Restored on re-entry, never enlarged.** A later invocation restores the
  authoritative limits, the absolute deadline, the consumed attempts, the
  checkpointed observations, the verification feedback and the per-signature
  repeat allowances. A caller that passes a different budget does not change
  the stored one; the difference is recorded in the `resumed` event.
* **Progress format.** The durable payload is version 3. A version 2 payload,
  which recorded signatures without their attempt counts, is still readable and
  is read **conservatively**: each of its signatures counts as having spent its
  whole allowance, so absent history is never mistaken for permission to repeat
  again. An unknown version is not partially restored. No shared-data
  migration and no production backfill exist; the slice is dormant and no
  environment holds such a payload.
* **Checkpoint bound.** The projection is bounded twice. Only the most recent
  16 observations are **retained** at all; an older one is dropped whole, its
  evidence references included. For each retained observation the identity,
  outcome and evidence references always survive, and result **bodies** are
  dropped oldest-first when the checkpoint would exceed its byte bound. A
  restored observation says plainly that it was restored and whether its body
  was dropped. Verification is therefore exact for the references of retained
  observations; a reference whose observation fell outside the retention
  window is no longer citable and a draft citing it is refused.
* **Repeat policy, durably enforced.** A tool call's identity is its
  **execution signature**: the tool name with its validated arguments. The
  correlation id plays no part, so two requests with different call ids and
  identical work are the same signature. Each signature has an allowance of
  **one original attempt plus at most one recovery repeat across later
  invocations**; a third request for it is refused
  (`repeated_tool_request`, `allowance_exhausted`).
  * A bundle is preflighted whole, against the signatures this invocation has
    already charged **and** against the signatures seen earlier in the same
    bundle. A bundle containing a duplicate is refused before its **first**
    tool runs (`duplicate_in_bundle`), and no tool attempt is charged for work
    never admitted. The reasoning attempt already spent to obtain that bundle
    stays spent.
  * Repeating a signature already charged within the *same* invocation is
    refused (`repeat_in_invocation`).
  * The attempt count for each admitted signature is written in the **same**
    compare-and-set as the consumed counters, before any tool runs. A crash
    between that debit and the execution therefore leaves the identity
    recorded and the allowance spent. That is deliberate and is the honest
    reading: **a debit can consume an allowance even when the crash prevents
    any proof that the tool actually ran.** Read-only repetition is a bounded
    recovery policy, not exactly-once execution; an abandoned or crashed call
    may have run.
  * The allowance is restored from the durable record and only ever
    incremented. It is never rebuilt from an invocation's memory, so a spent
    allowance stays spent.

## 6. Enforced waits, deadlines and cancellation

* **The loop enforces its own waits.** The provider call and each tool call
  run under a wait the loop imposes at the orchestration boundary; telling a
  provider how many seconds remain is not a limit. Each wait is capped to the
  smaller of its per-call limit (`provider_timeout_seconds`,
  `tool_timeout_seconds`) and the time left before the turn's deadline.
* **The deadline is an absolute database timestamp**, fixed from the database
  clock when the turn's progress is first written and persisted with it, so
  re-entry cannot extend it. It is checked before each debit.
* **Rechecked at the reservation boundary.** The acceptance transaction
  re-checks the deadline *inside* the transaction, after the conversation row
  lock and before any write, on the database clock. A reply accepted before
  expiry therefore cannot reserve delivery after expiry, even when the
  transaction waited on a lock in between. This uses the ledger's additive
  `precondition` hook (section 7.1).
* **Remaining database-wait limits.** The row-lock wait itself is not bounded
  by this slice: PostgreSQL's `lock_timeout` is not set here, so a contended
  lock can delay the transaction. The recheck above converts such a delay into
  a refusal rather than a late reservation; bounding the wait itself is left
  to deployment configuration and is stated here rather than implied away.
* **Cancellation is local.** An abandoned provider or tool call may still be
  running; its late value is discarded and has no path to progress, state or
  delivery. Abandoning a call is **not** proof that the remote work stopped,
  and the loop never claims it is.

## 7. Ownership, scope, state and delivery

* Every database operation is a completed repository call. **No transaction is
  held across a reasoning step or a tool call** (foundation §3.7).
* **Membership before anything else.** The turn must belong to the authorized
  tenant, namespace **and** conversation before any of its work is read or
  returned. A turn from another conversation stops the run with
  `turn_not_in_scope` and returns no terminal, no delivery sequence and no
  reuse.
* **Eligibility before reasoning.** Fresh work requires the turn to be the
  conversation's eligible (oldest unresolved) turn; otherwise the run stops
  before the provider is ever called.
* **Ownership at every debit.** The token is re-judged on the database clock
  before each debit, so ownership lost during a provider or tool call cannot
  buy another reasoning step, a state write or a reservation.
* **Existing work versus new work.** When the turn already has a terminal, an
  existing delivery sequence, or pending / `unknown` effect work, the loop
  returns a **read-only** outcome describing it and writes nothing: reuse
  grants no dispatch and no write authority, and never creates a second
  sequence. Only a turn with no such work proceeds to reasoning and may create
  new work.
* **Atomic hand-off.** The accepted reply state and its delivery intent are
  persisted in one `commit_turn_decision`; the outcome is `pending_delivery`
  with the sequence id. **Generating a reply is not sending it**: the loop
  never dispatches, never records a receipt and never finalizes a turn.
* **Closed outcomes.** Every stop is named and the vocabulary is closed:
  `turn_not_in_scope`, `turn_not_eligible`, `turn_completed`,
  `ownership_lost`, `concurrent_invocation`, `budget_exhausted`,
  `deadline_exceeded`, `cancelled`, `provider_failure`, `provider_blocked`,
  `provider_invalid`, `provider_timeout`, `unsupported_capability`,
  `verification_failed`, `repeated_tool_request`. Each is reached by a
  PostgreSQL regression that asserts the resulting outcome. The internal stop
  signal never escapes the loop: when ownership turns out to be gone while a
  stop is being recorded, the returned reason becomes `ownership_lost` and the
  original reason is kept in the detail, because that conclusion was never
  made durable and another owner may already hold the turn.

### 7.1 Narrow dependency extension

`LedgerRepository.commit_turn_decision` gained one optional parameter,
`precondition(conn, snapshot)`, evaluated after the conversation row lock and
after the **ownership, revision and eligibility** checks, and before any write
of that transaction; raising from it aborts the transaction and writes
nothing. Those three checks therefore still run first and are unaffected. The
**one-sequence-per-turn** guarantee is not one of them: it is enforced later
in the same transaction, by the reservation itself and the unique constraint
on the turn, so the hook sits between the guards and that enforcement and
weakens neither. Omitting the parameter reproduces the previous behaviour
exactly, so every existing caller is unchanged. The loop uses it for the
deadline recheck of section 6. The change is recorded in the ledger contract
and exercised by the reservation-deadline regression.

## 8. Alignment with the public Anthropic agent guidance

The two references — *Building effective agents* and *Building agents with the
Claude Agent SDK* — guide this implementation; the merged Nahla contracts
remain authoritative. No SDK-owned lifecycle is introduced, no dependency is
replaced and no model is selected here. This is not a claim of literal recipe
completion.

| Principle | Implementation | Test evidence | Remaining limitation |
| --- | --- | --- | --- |
| Simple composition: one model, tools, a loop | `agent_loop.AgentLoop.run_turn` | `test_turn_reasoning_tool_observation_reasoning_reply_and_one_delivery_intent` | No planner, router, subagents or multi-agent orchestration; one turn only |
| Clear tool interfaces | `agent_tools.ToolDefinition` and the private registry schemas | `test_every_exposed_tool_declares_a_schema_description_and_result_kind`, `test_provider_visible_schemas_and_context_cannot_change_what_is_enforced` | Three read-only fixture tools; no real catalogue, order or knowledge source |
| Observations feed the next decision | observations appended to each `ProviderRequest`, checkpointed durably | `test_same_question_and_query_with_different_observations_take_different_paths`, `test_checkpointed_observations_and_repeat_history_survive_re_entry` | One turn's observations; bodies may be dropped at the checkpoint bound; no cross-turn memory |
| Verification / feedback before acting | `verify_reply_draft` plus `VerificationFeedback` | `test_invalid_evidence_feeds_verification_back_and_the_correction_is_accepted` | Structural reference checks only; no semantic grounding and no completeness check |
| Bounded stopping conditions | enforced waits, absolute deadline, step and tool budgets, repeat detection, cancellation | `test_a_hanging_provider_is_abandoned_at_its_enforced_wait`, `test_a_tool_wait_is_capped_by_the_remaining_overall_deadline`, `test_budget_exhaustion_and_cancellation_stop_without_false_success` | Fixed budgets; no adaptive effort or token accounting; cancellation is local |
| Guardrails at the action boundary | allowlist, read-only registration, scope refusal, complete result validation | `test_unknown_tools_invalid_arguments_and_forged_scope_cannot_execute_work`, `test_a_bundle_mixing_a_valid_and_a_malformed_request_executes_no_tool` | Guards the tool boundary; the provider's wording is not policed |
| Transparency of steps | `LoopEvent` stream, named `stop_reason`, durable progress | event-sequence assertions in the path tests | No secrets and no private model reasoning are recorded, by design |
| Durable checkpoints | revision-bound debits, per-signature allowances, checkpointed context, atomic decision commit | `test_a_crash_after_the_tool_ran_keeps_the_debits_and_leaves_no_reservation`, `test_a_crash_right_after_the_tool_debit_keeps_the_identity_and_the_charge`, `test_re_entry_restores_the_authoritative_limits_and_cannot_enlarge_them` | One checkpoint shape per turn; no mid-tool-call resume; a charge can outlive the proof that the tool ran |

## 9. Out of scope (unchanged decisions)

*Recorded as of this slice. Items 1, 2, 4 and 5 of §10 were delivered by the two
pilot-integration changes that followed; the rest still stand.*

No real reasoning adapter, no live model call, no prompt, persona or model
selection; no dispatch, transport adapter or reconciliation worker; no
mutating business tools; no search projections, signed callbacks or
multi-agent orchestration; no startup, webhook, worker, scheduler or routing
wiring; no migration (this slice adds no schema), no bootstrap-target change,
no activation flag, no tenant allowlist and no customer data. V1 and PR #1085
stay frozen; #835, #865 and the deferred customer-identity work stay separate.
UC-01 and UC-02 remain open in the active legacy paths and commerce
reliability acceptance remains **NOT ACCEPTED**.

## 10. What a real reasoning adapter still needs

1. An adapter implementing `capabilities` and `step(ProviderRequest)` against
   a real model, translating `ToolDefinition` to that model's tool schema and
   its response to the closed `ProviderResult` union, including explicit
   `ProviderInvalid` on truncation and `ProviderBlocked` on refusal.
2. A prompt/persona surface owned by the existing AI modules and governed by
   the constitution, not by this package (this slice contains no prompt).
3. Transport retries and cost/token accounting at the adapter boundary; the
   loop's own bounded wait is implemented, an adapter's internal retry budget
   is not.
4. Real tools replacing the fixtures, each still read-only at this stage, with
   the same scope-from-context rule.
5. Separate authorization for dispatch: a transport adapter and the ledger's
   dispatch reservation path, which this slice deliberately does not touch.
6. End-to-end validation on a shared environment, which needs the merge,
   deployment and activation decisions that are explicitly not part of this
   slice.
