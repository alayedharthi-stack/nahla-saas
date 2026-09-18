# Commerce Conversation Reliability Gate (C1-PR1)

**Scope of PR #1086 (harness only, after the owner's governance decision and
the Astra targeted review):** the reliability harness, its runner, its tests
and this documentation. The two CI steps that execute it were removed from
this PR (GOV-002 governance split) and are preserved verbatim in
`commerce-reliability-gate-ci-steps.patch` for a separate governance-only PR.
**This PR alone does not complete required CI integration**; that milestone
needs the harness (this PR), the `ci.yml` steps (follow-up PR) and verified
enforcement (branch-protection readback), in that order.

No runtime behaviour, prompt, model, migration, activation, Railway or
deployment change ships with the harness. A test that exposes a product defect
records it in the reviewed manifest; it never repairs product code and never
weakens itself.

**Base:** `origin/main` at `4a270df82bcf11cdd88b1ef6d9946dc2dc46ca3e` (merge
of PR #1084). Every recorded baseline failure was measured against that SHA
with real application code, scripted models and fake providers.

## 1. What this harness is, and is not

The runtime tests are **isolated application-boundary characterisation**:
real entry points (`_handle_merchant_message`, `DefaultDecisionEngine`,
`ProductSearchHandler`, `DefaultComposer`, `CatalogContextBuilder`,
`claim_next_batch`, `DefaultStateStore`) driven with a scripted httpx provider,
scripted model outputs, a mocked persistence layer (PR #1084 harness) or a
disposable PostgreSQL database. They characterise what the code does today at
those boundaries. They do not prove production behaviour.

Evaluator capability (proven by self-tests on synthetic evidence) is reported
separately from application coverage (what the runtime tests actually feed
the evaluator):

| Dimension | Evaluator capability (self-tests) | Application-boundary coverage (runtime tests) |
| --- | --- | --- |
| Closed delivery terminal, provider message id, duplicate accepted sends | rejects missing / non-closed / mismatched terminals, acceptance without an id, duplicates | V1 owner turns: accepted, HTTP 200 without id, timeout, definitive rejection + recovery, replay; V2 owner rejected send (UC-01) |
| Transport outcome | derives accepted / rejected_definitive / **unknown** / not_attempted from the attempt sequence; rejects an adapter that reports otherwise | reported per turn; an ambiguous timeout stays UNKNOWN even where the legacy lifecycle records `end_delivery_failed` |
| Dispatch-sequence safety | rejects a second dispatch after an ambiguous attempt (no `retry_evidence`), and a further dispatch after an accepted send in a single-send case | every V1 scenario above: one dispatch after ambiguity, one recovery text after a definitive rejection |
| Tenant isolation | rejects foreign tenant ids in touched rows | stamps, wire attempts and persisted rows carry tenant 1 only |
| Fallback provenance | rejects unknown provenance, runtime fallbacks, and safe fallbacks the case did not expect | classified from the runtime's own metadata vocabulary (`compose_source`, `final_customer_text_source`, `fallback_reason`, `delivery_recovery`, V2 `v2_status`); unknown or unrecognised provenance is never "no fallback" |
| Evidence references | rejects blank / whitespace / non-string entries and empty required lists | lifecycle event names |
| Guardrail results | rejects contradictory results for one execution (attempt identity preserved), "passed" without execution, missing or failed required guardrails | **NOT EVALUATED**: the merchant-handler seam exposes no guardrail execution records and the runtime tests require none |
| Semantic (knowledge) binding resolution | `required_fixture_bindings` are fixture identity labels only | **NOT EVALUATED** |
| Durable delivery record | — | **NOT EVALUATED** (persistence is mocked in the delivery tests) |
| Order idempotency | rejects repeated side-effect keys | **NOT EVALUATED** (no order side effects are exercised) |
| Conversation serialisation | — | **NOT EVALUATED** (queue claiming from two processes is not per-conversation serialisation) |

Every turn verdict carries `facts["coverage"]` and `facts["not_evaluated"]`
so a report cannot imply coverage that was not measured.

The three-value terminal set (`provider_accepted`, `human_handoff`,
`explicit_delivery_failure`) is **legacy characterisation** of today's
lifecycle tokens, not an implementation of the corrected delivery invariants.

## 2. Two modes: acceptance and diagnostic

| | `--mode acceptance` (default) | `--mode diagnostic` |
| --- | --- | --- |
| Purpose | The merge/release gate | Measure the current baseline |
| Reconciliation rules (unexpected pass, changed signature, expired allowance, missing allowance test, unlisted failure/skip/xfail, missing required test, changed PR #1084 module, empty selection, PostgreSQL configuration) | blockers | blockers |
| Unapproved debt: allowance with `owner` missing/`UNASSIGNED…` or `expiry` null | **blocker** (`unapproved_debt:owner_unassigned:<id>`, `unapproved_debt:expiry_unset:<id>`) | warning |
| Result line | `ACCEPTED` only with no blockers; otherwise `NOT ACCEPTED` | always `NOT ACCEPTED (diagnostic measurement; never a merge/release verdict)` |
| Exit status | 0 accepted / 1 otherwise | **never 0**: 3 when the measurement is consistent, 1 when blocked |

**Current state (committed manifest):** every allowance carries
`owner: "UNASSIGNED (owner review)"` and `expiry: null`. These are
*proposals*, not approved exceptions. Acceptance mode therefore reports
**NOT ACCEPTED** on both tiers although the measurement is consistent;
diagnostic mode reports the measured baseline and NOT ACCEPTED. The gate is
not weakened to obtain green. Approval requires a named owner and an actual
expiry date per entry ("when scheduled" is not an expiry); the
`test_repo_manifest_acceptance_reflects_actual_approval_state` self-test
judges the committed manifest on its real values, so assigning owners and
dates is sufficient to unblock acceptance without editing any test.

## 3. Recorded baseline failures — reproduced current implementation defects

All eight allowances are empirically reproduced defects of the current
implementation at `4a270df8`, measured at the boundaries above. Identifiers
UC-01 and UC-02 are kept for traceability; they are defects, not placeholders.

| Id | Tier | Signature | Exact recorded observation (anything else is a changed cause) |
| --- | --- | --- | --- |
| RB-01 | unit | `interpreter_browse_repeats_shown_ids` | interpreter browse decision offers ids that intersect `last_search_candidates` |
| RB-02 | unit | `recovery_resends_shown_candidates` | guard-emptied "more" request: delivery closes correctly, the recovered text names already-shown candidates only |
| RB-03 | unit | `broken_plural_query_returns_no_rows` | `search_products("تنانير")` returns no rows (sqlite copy of the schema) |
| RB-03-PG | postgres | `broken_plural_query_returns_no_rows` | same on the PostgreSQL FTS path |
| RB-04 | unit | `list_pick_compose_emits_no_products_template` | executor selects candidate 11; composer output equals a `T.no_products` template |
| RB-05 | unit | `fulfillment_lock_blocks_discovery` | locked session, `عندكم تنانير؟` → `llm_reply` with the recorded "active order" reason, catalog interpreter gated off (returns None), unlocked control reaches `search_products` |
| UC-01 | unit | `v2_owner_delivery_failure_ends_with_inferred_end_ok` | V2 owner on, provider HTTP 400 on the text: transport `rejected_definitive`, no accepted id, lifecycle `end_ok`, no outbound row, no outcome record; the evaluator blockers are exactly `missing_terminal:…inferred_without_provider_acceptance` and `fallback_provenance_unknown`. Does not prove the V2 owner is active in production. |
| UC-02 | postgres | `state_store_save_lost_update` | both spawned workers report `saved`; exactly one of two independently changed fields survives with the value its worker wrote; the other still holds the pre-race value |

Predicates (`runtime_support.classify_uc01_observation`,
`classify_state_save_outcome`, `classify_lock_decision`) raise an allowance
marker only for the exact observation above; unrelated tenant / evidence /
dispatch-safety failures, partial signatures, neither update persisting,
worker failures, wrong values and other non-search decisions fail on their
own. `test_predicate_selfcheck.py` proves each changed cause is not absorbed.

### Future replacement contracts (tracked separately, never measured)

| Id | Replaces | Contract | Status |
| --- | --- | --- | --- |
| RC-01 | UC-01 | durable terminal processing record per turn; transport uncertainty and reach evidence recorded separately | unapproved future decision; not authorised by this harness |
| RC-02 | UC-02 | durable conversation ownership, fencing, versioned state, compare-and-set | unapproved future decision; not authorised by this harness |

### Owner decisions still open (proposals only; the harness assigns nothing)

| Id | Proposed owner | Expiry |
| --- | --- | --- |
| RB-01 | V1 brain catalog maintainers | owner decision; an actual date is required |
| RB-02 | delivery recovery maintainers (PR #1084 authors) | owner decision; an actual date is required |
| RB-03 / RB-03-PG | catalog search maintainers | owner decision; an actual date is required |
| RB-04 | V1 compose maintainers | owner decision; an actual date is required |
| RB-05 | order context gate maintainers | owner decision; an actual date is required |
| UC-01 | Commerce Agent V2 owners | owner decision; an actual date is required |
| UC-02 | brain state persistence maintainers | owner decision; an actual date is required |

Retiring a baseline follows verified replacement and path retirement, or a
merged change that closes the defect; it does not require repairing every
frozen V1 path, and nothing here authorises such repairs. When an entry is
retired, delete it from the manifest in the same PR; never edit a test to keep
an allowance alive, and never widen a signature.

## 4. Reconciliation rules (never silently broadened)

A test listed in the manifest must fail by raising the exact marker:

```
RELIABILITY_BASELINE[RB-04] list_pick_compose_emits_no_products_template
RELIABILITY_BASELINE[UC-01] v2_owner_delivery_failure_ends_with_inferred_end_ok
```

| Observation | Result |
| --- | --- |
| Exact marker raised | Expected failure (xfail with the marker as reason) |
| The test passes | `RECONCILE unexpected_pass:<id>` failure. Delete the manifest entry in the fixing PR. |
| The test fails differently, including the marker with extra cause text | `RECONCILE signature_changed:<id>` failure. Re-measure and re-review. |
| Expiry date passed | `RECONCILE expired_allowance:<id>` failure, whatever the test did. |
| Allowance test missing from the run | `allowance_test_missing:<id>` blocker. |
| Marker raised by a test not listed for that id | `RECONCILE allowance_not_listed_for_test` failure. |

The plugin (from the exception, exact string) and the runner (from the JUnit
message, **exact whitespace-normalised comparison**) apply the rules
independently, so neither layer can pass the gate alone.
`NAHLA_RELIABILITY_TODAY` exists only for the plugin's self-test; the runner
removes it and judges expiry against the real date.

## 5. Components

| Path | Role |
| --- | --- |
| `tests/commerce_reliability/reliability_evaluator.py` | Pure evaluators (no pytest import): `evaluate_turn` (turn evidence, closed vocabularies, fail-closed rules, coverage facts) and `evaluate_gate` (JUnit records vs. manifest, acceptance / diagnostic modes) |
| `tests/commerce_reliability/reliability_manifest.json` | Reviewed manifest: base SHA, pinned PR #1084 module hashes, required tests per tier, baseline failures, replacement contracts, debt policy, PostgreSQL requirements |
| `tests/commerce_reliability/reliability_plugin.py` | pytest hooks applying the reconciliation rules; `baseline` / `unimplemented` fixtures |
| `tests/commerce_reliability/conftest.py` | Plugin wiring; sqlite catalog fixture; disposable PostgreSQL fixture |
| `tests/commerce_reliability/runtime_support.py` | Loads PR #1084's incident harness by path (unchanged); records provider responses; classifies fallback provenance; derives transport outcome; builds evidence with explicit NOT EVALUATED dimensions; exact baseline predicates |
| `tests/commerce_reliability/pg_workers.py` | Entry points run in separate spawned processes |
| `tests/commerce_reliability/test_evaluator_selfcheck.py` | Evaluator self-tests incl. negative cases for every demonstrated false positive and an inner pytest session proving the reconciliation rules |
| `tests/commerce_reliability/test_predicate_selfcheck.py` | Adversarial self-tests for the provenance classifier and the UC-01 / UC-02 / RB-05 predicates |
| `tests/commerce_reliability/test_runtime_v1_delivery.py` | Real `_handle_merchant_message` through PR #1084's harness |
| `tests/commerce_reliability/test_runtime_v1_browse.py` | Real decision engine / executor / composer / catalog search |
| `tests/commerce_reliability/test_runtime_state_postgres.py` | PostgreSQL tier: disposable UTF-8 database, spawned-process concurrency, FTS search |
| `scripts/commerce_reliability_gate.py` | Runner: native invocation of the PR #1084 modules, harness invocation per tier, JUnit evaluation, report, mode-aware exit status |
| `docs/engineering/commerce-reliability-gate-ci-steps.patch` | The exact `ci.yml` hunk removed from PR #1086 (§7) |
| `docs/evidence/commerce-reliability-gate/pr1086-e12ecba4-ci-record.md` | Terminal CI results of the only head that carried the steps |

## 6. Running it

```bash
# acceptance (merge/release gate) — exit 0 only when accepted
python scripts/commerce_reliability_gate.py --tier unit \
  --junit-dir /tmp/commerce-reliability --report /tmp/commerce-reliability/gate-unit.json

# diagnostic (measure the baseline) — exit 3 when consistent, never 0
python scripts/commerce_reliability_gate.py --tier unit --mode diagnostic \
  --junit-dir /tmp/commerce-reliability --report /tmp/commerce-reliability/gate-unit-diag.json

# PostgreSQL tier: the role must be able to CREATE DATABASE; the gate creates
# nahla_reliability_<random> (ENCODING 'UTF8' TEMPLATE template0) and drops it WITH (FORCE)
NAHLA_RELIABILITY_REQUIRE_PG=1 \
NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://nahla:nahla_password@127.0.0.1:5433/postgres \
python scripts/commerce_reliability_gate.py --tier postgres [--mode diagnostic] \
  --junit-dir /tmp/commerce-reliability --report /tmp/commerce-reliability/gate-postgres.json
```

Runner rules: `NAHLA_RELIABILITY_REQUIRE_PG=1` without the DSN fails before
pytest; with neither variable the PostgreSQL tests skip in a developer's
default run and the PostgreSQL-tier gate fails. An empty selection, a missing
required test, a required test that did not pass, an unlisted
failure/skip/xfail, or a pytest exit code other than 0/1 blocks the
measurement. The PR #1084 modules must execute at least 17 and 24 tests and
their content hash must equal the manifest hash (`required_module_changed`
otherwise).

The default repository run (`python -m pytest -q --maxfail=1`) also collects
this directory: recorded baselines show as `xfailed` and PostgreSQL tests
skip. **Root-run success is not reliability acceptance**; only the runner in
acceptance mode judges acceptance.

## 7. Governance split and safe integration order

GOV-002 (`scripts/lint_intelligence_non_interference.py`) lists
`.github/workflows/ci.yml` as governance core and rejects any PR that changes
it together with non-governance files (`GOVERNANCE_CORE_CHANGE`, digest
`ea73362e0d7a87a14e58f649bd69d1efee7994eb4da436c198260f5ec58fe35d` on head
`e12ecba4`). The owner chose the split; no exception, bypass,
branch-protection change or optional-workflow workaround is authorised.

Integration order (do not reorder):

1. **PR #1086 (harness only)** merges. Nothing in CI executes the gate yet.
2. **Governance-only follow-up PR** containing exactly the `ci.yml` hunk in
   `commerce-reliability-gate-ci-steps.patch` (`git apply` on a `main` that
   already contains step 1). It must not be opened before step 1 merges: the
   step references `scripts/commerce_reliability_gate.py`, absent on today's
   `main`. In acceptance mode that step stays red until the debt in §3 is
   approved.
3. **Enforcement verification** (§8) before the milestone "required CI
   integration" is claimed.

## 8. Required-check enforcement: evidence and limitation

| Evidence | Source | Value |
| --- | --- | --- |
| `main` is a protected branch | GitHub API readback (branches list) on 2026-09-18 | `protected: true` |
| Required status checks on `main` | branch-protection / rulesets endpoints not readable with the access available (403 in the Astra review; no endpoint in this session) | **UNVERIFIED** |
| Documented readback | `docs/engineering/merge-and-ci-policy.md`, `gov002-workflow-trust-root.md` | lists `lint-and-test`, `constitution-compliance`, gitleaks as required; documentation is not proof of the current setting |

Consequences:

* `lint-and-test`: documented as required, **unverified**.
* `a1-postgres-integration`: **NOT VERIFIED REQUIRED**. It is absent from the
  documented set, which does not establish that it is non-required; only an
  authoritative readback can settle it either way.
* The follow-up must therefore either (A) run both tiers inside a verified
  required job, provisioning a `postgres:16` service there for the PostgreSQL
  tier, or (B) establish through authoritative readback that both jobs are
  required. Nothing in this PR changes branch protection, rulesets, bypass
  policy or CODEOWNERS.

## 9. Report sections

1. **Harness integrity** — counts, PR #1084 module execution and hash status,
   required-test status, PostgreSQL configuration (DSN reported as `set`,
   never echoed).
2. **Current runtime behaviour** — the runtime tests that passed at the
   isolated application boundary.
3. **3a Outstanding baseline failures** (reproduced current implementation
   defects) with observed status, `approved_debt`, owner, expiry, signature;
   **3b unimplemented contract allowances** (none today); **3c future
   replacement contracts** (tracked separately, not measured).
4. **Rollout readiness** — mode, acceptance status, open allowances and the
   list of unapproved debt; a baseline-compatible measurement is not
   production acceptance.

## 10. Astra targeted review (2026-09-18) — reconciliation

| Astra finding | Correction | Regression test | Remaining limitation |
| --- | --- | --- | --- |
| 1A blank required evidence `[""]` passed | `blank_evidence_reference`; blank entries never satisfy a required list | `test_blank_required_evidence_reference_rejected` | — |
| 1B same guardrail failed then passed → last wins | results grouped by (name, attempt); mixed results in one execution → `contradictory_guardrail_result`; a documented retry (distinct attempts) is allowed; reused attempt identity rejected | `test_contradictory_guardrail_results_rejected_but_documented_retry_allowed` | application guardrail execution remains NOT EVALUATED at the delivery seam |
| 1C `passed=True, executed=False` passed | `guardrail_passed_without_execution`; required guardrails need `executed: True` (`guardrail_not_executed` / `guardrail_execution_unknown`) | `test_guardrail_passed_without_execution_rejected` | same |
| 1D timeout then another send with one accepted id passed | `unsafe_dispatch_after_ambiguous_attempt` unless the later attempt carries `retry_evidence`; `dispatch_after_accepted_send` for single-send cases | `test_dispatch_after_ambiguous_attempt_rejected` | what counts as authoritative retry evidence is an explicit field, not derived from the runtime |
| 1E adapter discarded deterministic-fallback provenance (`fallback_kind="none"`) | adapter classifies from the runtime's own vocabulary; unknown/unrecognised → `unknown_provenance` → `fallback_provenance_unknown`; evaluator default is unknown | `test_fallback_provenance_classifier_matrix`, `test_unknown_fallback_provenance_rejected` | provenance keys outside the enumerated vocabulary are unknown by design |
| 1 bindings are string-set checks | documented as fixture identity; semantic binding resolution reported NOT EVALUATED in every verdict | `test_valid_reference_turn_passes` (coverage facts) | not evaluated |
| 2 UC-01 predicate absorbed unrelated failures | `classify_uc01_observation`: exact blocker set, `end_ok`, no accepted id, `rejected_definitive`, no records; unrelated blockers fail independently | `test_uc01_predicate_rejects_changed_causes` | — |
| 2 UC-02 predicate treated any missing field as the lost update | `classify_state_save_outcome`: both workers `saved`; one field written, the other at the pre-race baseline; neither / worker failure / wrong value are changed causes | `test_uc02_predicate_distinguishes_lost_update_from_no_op` | — |
| 2 RB-05 predicate treated any non-search decision as the lock defect | `classify_lock_decision`: locked, `llm_reply`, recorded "active order" reason, interpreter gated, unlocked control reaches search | `test_rb05_predicate_requires_recorded_conditions` | — |
| 2 JUnit substring matching | exact whitespace-normalised comparison; extra cause text fails reconciliation in both layers | `test_junit_marker_with_extra_cause_text_fails_reconciliation`, `test_reconciliation_plugin_rejects_marker_with_extra_cause_text` | — |
| 3 required self-test hardcoded unapproved debt | replaced by `test_repo_manifest_acceptance_reflects_actual_approval_state` (judges the real manifest on its real values); synthetic manifests test rejection and approval | `test_acceptance_mode_rejects_unapproved_debt`, `test_gate_accepts_exact_baseline_observation` | debt stays unapproved pending the owner's decision |
| 6 UC-01 / UC-02 classified as future contracts | reclassified as reproduced current defects (ids kept); replacement contracts RC-01 / RC-02 tracked separately | `test_manifest_in_repo_is_well_formed_and_self_consistent` | — |
| 9.3 legacy terminal set vs. unknown transport outcomes | transport outcome derived and reported; UNKNOWN preserved for ambiguous attempts | `test_transport_outcome_preserves_unknown` | terminal vocabulary remains legacy characterisation |
| 8 enforcement | `a1-postgres-integration` recorded NOT VERIFIED REQUIRED; both-tiers path stated | — | required-check readback still unavailable |
