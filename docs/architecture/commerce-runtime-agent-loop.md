# Commerce runtime — dormant agent loop core (contract)

Status: recorded 2026-09-19 as the contract of the third dormant slice.
Extends `commerce-runtime-foundation-contract.md` and
`commerce-runtime-effect-and-delivery-ledgers.md`; nothing here reopens the
closed findings of either. Authority for the implementation in
`backend/core/commerce_runtime/{agent_contracts,agent_tools,agent_scripted,agent_loop}.py`.

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
`ProviderCapabilities` (tool use, parallel tool use, evidence references, a
per-step request limit); the loop never assumes more than is declared.

`ProviderResult` is a closed tagged union that distinguishes, explicitly:

| Result | Meaning | Loop behaviour |
| --- | --- | --- |
| `ProviderToolRequests` | wants one or more tools | validate, authorize, execute, observe, continue |
| `ProviderReply` | a reply draft | validate, verify, accept or feed back |
| `ProviderFailure` | could not produce a step | stop, `provider_failure` |
| `ProviderBlocked` | declined (policy) | stop, `provider_blocked`; not retried |
| `ProviderInvalid` | incomplete or malformed output | stop, `provider_invalid`; never treated as a reply |

An unsupported capability, a malformed request and a draft that fails
validation are refused **before** any tool executes and before any delivery
is reserved. A provider that raises becomes `provider_failure`, never a crash
of the loop. Forced tool use is not imposed on any provider: a direct reply
without a tool call is a legitimate first step.

**Model tool-call ids are correlation only.** `call_id` matches a request to
its observation. It is never a business idempotency key (those are the
ledger's `derive_business_key` values), never an evidence reference and never
an authorization credential.

## 3. Tools

The loop exposes an **allowlist** of read-only fixture tools: `catalog_search`,
`product_lookup`, `merchant_knowledge_lookup`. Each declares a name, a
description, a JSON-schema input (`additionalProperties: false`), a result
kind and closed error codes (`unknown_tool`, `invalid_arguments`,
`scope_override_refused`, `not_read_only`, `timeout`, `tool_failure`,
`result_too_large`). A tool definition that is not read-only cannot be
registered.

* **Scope comes from the trusted runtime context.** The loop builds a
  `ToolScope` from the turn it was given. Model arguments naming
  `tenant_id`, `namespace`, `conversation_id`, `turn_id`, `token`, `owner_id`
  and similar are refused (`scope_override_refused`) before the tool runs;
  they are never merged, ignored or "sanitised".
* **Tool results are data.** They carry evidence references the verifier can
  check. They acquire no instruction or authorization authority, and a failed
  or timed-out call contributes no evidence.
* Each call is bounded by a per-call timeout; a late result is discarded and
  reported as `timeout`.

## 4. Verification of the reply draft

Before a draft may be handed to delivery, deterministic checks run:

* every cited evidence reference must exist among **this turn's** successful
  observations;
* a draft that claims commerce facts must cite at least one reference;
* the text must be present and within bounds.

A correctable failure becomes `VerificationFeedback` fed into the next
reasoning step **within the same budget**; when no step remains the loop
stops with `verification_failed` and reserves nothing.

**What this proves and what it does not.** It proves that the cited evidence
was actually observed in this turn, in this tenant's scope, and that a
commerce claim is not unsupported. It does **not** prove that every sentence
of the reply is consistent with that evidence: semantic grounding of the
wording is not established by this slice, and a valid reference alone does
not make a sentence correct.

## 5. Bounded execution

Bounded inference steps, bounded tool calls, a per-call timeout and an
overall deadline. A repeated identical tool request (same tool, same
arguments) terminates the loop rather than looping. Cancellation is checked
between steps. Every stop is explicit and named: `budget_exhausted`,
`deadline_exceeded`, `cancelled`, `repeated_tool_request`,
`verification_failed`, `provider_*`, `unsupported_capability`,
`ownership_lost`, `turn_not_eligible`, `turn_completed`. The vocabulary is
closed **and** exhaustive: every declared reason is one the loop raises. A
refused or timed-out tool is an *observation*, not a stop, so the loop reports
it and keeps its budget; a provider call that runs long is caught by the
deadline, and a per-call provider timeout belongs to a future adapter, which
reports it as `ProviderFailure`.

Budget information is **persisted** in the conversation's versioned state
under the reserved key `agent_loop` (steps used, tool calls used, elapsed
seconds, phase, stop reason). Re-entry adopts those counters, so re-entering
a turn cannot reset the limits. `Conversation.extra_metadata` is not used and
is not the authority for loop progress.

## 6. Ownership, state and delivery

* Every database operation is a completed repository call. **No transaction
  is held across a reasoning step or a tool call** (foundation §3.7).
* Ownership is re-validated on the database clock **after every await**:
  before accepting a draft and before any write. A run whose lease expired,
  was taken over or was fenced writes no state and reserves no delivery;
  stops caused by lost ownership or an ineligible turn persist nothing.
* The accepted reply state and its delivery intent are persisted **atomically**
  by `LedgerRepository.commit_turn_decision`. The outcome is
  `pending_delivery` with the sequence id.
* **Generating a reply is not sending it.** The loop never dispatches, never
  records a receipt, and never finalizes a turn. Transport outcome stays
  `not_attempted` and no terminal exists when the loop returns.
* **Re-entry reuses existing work.** Before reasoning again the loop inspects
  the terminal, the ledger summary and the delivery sequence: a completed
  turn stops (`turn_completed`), an existing delivery sequence is reused
  (never a second sequence, including for a concurrent run that loses the
  compare-and-set), and pending or `unknown` effect work stops the run
  instead of resending or completing.

## 7. Alignment with the public Anthropic agent guidance

The two references — *Building effective agents* and *Building agents with
the Claude Agent SDK* — guide this implementation; the merged Nahla contracts
remain authoritative. No SDK-owned lifecycle is introduced, no dependency is
replaced and no model is selected here. This is not a claim of literal recipe
completion.

| Principle | Implementation | Test evidence | Remaining limitation |
| --- | --- | --- | --- |
| Simple composition: one model, tools, a loop — not a framework | `agent_loop.AgentLoop.run_turn`: a single loop over provider step → tools → observations | `test_turn_reasoning_tool_observation_reasoning_reply_and_one_delivery_intent` | No planner, router, multi-agent orchestration or subagents; a single conversation turn only |
| Clear tool interfaces (a documented "agent–computer interface") | `agent_tools.ToolDefinition` with name, description, JSON schema, result kind, closed error codes | `test_every_exposed_tool_declares_a_schema_description_and_result_kind` | Three read-only fixture tools; no real catalogue, order or knowledge source is connected |
| Observations feed the next decision | Observations are appended to `ProviderRequest.observations` each step | `test_different_observations_lead_to_different_next_decisions` | Observations are in-memory for one turn; no cross-turn memory or context compaction |
| Verification / feedback loop before acting on output | `verify_reply_draft` plus `VerificationFeedback` re-entering the next step | `test_invalid_evidence_feeds_verification_back_and_the_correction_is_accepted`, `test_uncorrectable_verification_fails_bounded_and_reserves_no_delivery` | Rules-based verification of evidence existence only; no LLM judge, no semantic grounding check |
| Bounded stopping conditions | Steps, tool calls, per-call timeout, deadline, repeat detection, cancellation | `test_budget_exhaustion_and_deadline_and_cancellation_stop_without_false_success`, `test_repeated_tool_requests_terminate_explicitly`, `test_tool_timeout_is_an_observation_and_the_loop_stays_honest` | Budgets are fixed per run; there is no adaptive effort control and no token accounting |
| Guardrails at the action boundary | Allowlist, read-only registration, scope-override refusal, capability checks | `test_unknown_tools_invalid_arguments_and_forged_scope_cannot_execute_work`, `test_only_read_only_tools_can_be_registered` | Guardrails cover the tool boundary; the provider's wording is not policed here |
| Transparency: show the agent's steps | `LoopEvent` stream and `LoopOutcome.stop_reason` on every run | `_kinds(outcome)` assertions in the path tests | Events carry no private model reasoning and no secrets, by design; they are returned, not persisted as a log |
| Checkpoints / durable progress | Progress in the versioned state; atomic decision commit; re-entry reuse | `test_re_entry_reuses_the_existing_delivery_intent_and_the_persisted_budget`, `test_crash_before_the_atomic_accept_leaves_no_state_and_no_delivery_sequence` | One checkpoint shape (per turn); no resumable mid-tool-call checkpointing |

## 8. Out of scope (unchanged decisions)

No real reasoning adapter, no live model call, no prompt, persona or model
selection; no dispatch, transport adapter or reconciliation worker; no
mutating business tools; no search projections, signed callbacks or
multi-agent orchestration; no startup, webhook, worker, scheduler or routing
wiring; no migration (this slice adds no schema), no bootstrap-target change,
no activation flag, no tenant allowlist and no customer data. V1 and PR #1085
stay frozen; #835, #865 and the deferred customer-identity work stay
separate. UC-01 and UC-02 remain open in the active legacy paths and commerce
reliability acceptance remains **NOT ACCEPTED**.

## 9. What a real reasoning adapter still needs

1. An adapter implementing `capabilities` and `step(ProviderRequest)` against
   a real model, translating `ToolDefinition` to that model's tool schema and
   its response to the closed `ProviderResult` union, including explicit
   `ProviderInvalid` on truncation and `ProviderBlocked` on refusal.
2. A prompt/persona surface owned by the existing AI modules and governed by
   the constitution, not by this package (this slice contains no prompt).
3. Per-call timeouts, retries and cost/token accounting at the adapter
   boundary, feeding the loop's existing budget.
4. Real tools replacing the fixtures, each still read-only at this stage,
   with the same scope-from-context rule.
5. Separate authorization for dispatch: a transport adapter and the ledger's
   dispatch reservation path, which this slice deliberately does not touch.
6. End-to-end validation on a shared environment, which needs the merge,
   deployment and activation decisions that are explicitly not part of this
   slice.
