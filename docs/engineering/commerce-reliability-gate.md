# Commerce Conversation Reliability Gate (C1-PR1)

**Scope of PR #1086 after the owner's governance decision (2026-09-18):** the
reliability harness only. The two CI steps that execute it were removed from
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

## 1. Two modes: acceptance and diagnostic

| | `--mode acceptance` (default) | `--mode diagnostic` |
| --- | --- | --- |
| Purpose | The merge/release gate | Measure the current baseline |
| Reconciliation rules (unexpected pass, changed signature, expired allowance, missing allowance test, unlisted failure/skip/xfail, missing required test, changed PR #1084 module, empty selection, PostgreSQL configuration) | blockers | blockers |
| Unapproved debt: allowance with `owner` missing/`UNASSIGNED…` or `expiry` null | **blocker** (`unapproved_debt:owner_unassigned:<id>`, `unapproved_debt:expiry_unset:<id>`) | warning |
| Result line | `ACCEPTED` only with no blockers; otherwise `NOT ACCEPTED` | always `NOT ACCEPTED (diagnostic measurement; never a merge/release verdict)` |
| Exit status | 0 accepted / 1 otherwise | **never 0**: 3 when the measurement is consistent, 1 when blocked |

The diagnostic mode exists so the current baseline can be measured and
reported honestly while debt is still unapproved. Because it never exits 0, a
CI step cannot use it as a stand-in for the acceptance gate.

**Current state (committed manifest):** every allowance carries
`owner: "UNASSIGNED (owner review)"` and `expiry: null`. These are
*proposals*, not approved exceptions. Acceptance mode therefore reports
**NOT ACCEPTED** on both tiers even though the measurement is consistent;
diagnostic mode reports the measured baseline and NOT ACCEPTED. The gate is
not weakened to obtain green; the owner's assignment decision unblocks it.

## 2. Debt requiring owner decision

Two categories are kept separate and are never merged in the report:

* **Baseline failures** — empirically reproduced *current-runtime* defects
  (section 3a of the report).
* **Unimplemented contracts** — *future* runtime contracts that the harness
  tests for and that do not exist yet (section 3b). They are never described
  as coverage or as completed behaviour.

Proposed assignments (proposals only; the harness invents neither owners nor
dates; the owner fills `owner` and `expiry` in the manifest):

| Id | Kind | Signature | Proposed owner (proposal) | Proposed expiry (proposal) | Removal condition |
| --- | --- | --- | --- | --- | --- |
| RB-01 | baseline | `interpreter_browse_repeats_shown_ids` | V1 brain catalog maintainers | when the C1-PR2 browse-cursor fix is scheduled | browse decision excludes shown ids / advances a cursor |
| RB-02 | baseline | `recovery_resends_shown_candidates` | delivery recovery maintainers (PR #1084 authors) | when the C1-PR2 recovery-scope fix is scheduled | recovery for a "more" request presents unseen candidates or an honest no-more text |
| RB-03 | baseline | `broken_plural_query_returns_no_rows` (sqlite copy) | catalog search maintainers | when the search redesign is scheduled | `تنانير` resolves to `تنورة` rows for the same tenant; not a hardcoded noun table as final design |
| RB-03-PG | baseline | same, PostgreSQL FTS path | catalog search maintainers | together with RB-03 | same as RB-03, proven on PostgreSQL |
| RB-04 | baseline | `list_pick_compose_emits_no_products_template` | V1 compose maintainers | when the C1-PR2 compose fix is scheduled | composer grounds the selected singleton |
| RB-05 | baseline | `fulfillment_lock_blocks_discovery` | order context gate maintainers | when the C1-PR2 gate fix is scheduled | explicit new-category discovery reaches product search without weakening fulfillment answers |
| UC-01 | unimplemented | `v2_owner_delivery_failure_ends_with_inferred_end_ok` | Commerce Agent V2 owners (C1-PR2) | when C1-PR2 is scheduled | V2 owner path closes every turn with a provider-accepted id, human handoff or explicit failure |
| UC-02 | unimplemented | `state_store_save_lost_update` | brain state persistence maintainers (C1-PR3) | when C1-PR3 is scheduled | concurrent `DefaultStateStore.save` cannot lose an update (compare-and-set) |

### Removing an allowance

1. Land the product fix in its own PR (C1-PR2 / C1-PR3 scope, not this gate).
2. In the same PR delete the manifest entry; the test now passes and moves to
   `required_tests`.
3. Never edit a test to keep an allowance alive, and never widen a signature.

## 3. Reconciliation rules (never silently broadened)

A test listed as a baseline failure or an unimplemented contract must fail by
raising the exact marker:

```
RELIABILITY_BASELINE[RB-04] list_pick_compose_emits_no_products_template
RELIABILITY_UNIMPLEMENTED[UC-01] v2_owner_delivery_failure_ends_with_inferred_end_ok
```

| Observation | Result |
| --- | --- |
| Exact marker raised | Expected failure (xfail with the marker as reason) |
| The test passes | `RECONCILE unexpected_pass:<id>` failure. Delete the manifest entry in the fixing PR. |
| The test fails differently | `RECONCILE signature_changed:<id>` failure. Re-measure and re-review. |
| Expiry date passed | `RECONCILE expired_allowance:<id>` failure, whatever the test did. |
| Allowance test missing from the run | `allowance_test_missing:<id>` blocker. |
| Marker raised by a test not listed for that id | `RECONCILE allowance_not_listed_for_test` failure. |

The plugin (from the exception) and the runner (from the JUnit file) apply the
rules independently, so neither layer can pass the gate alone.
`NAHLA_RELIABILITY_TODAY` exists only for the plugin's self-test; the runner
removes it and judges expiry against the real date.

## 4. Components

| Path | Role |
| --- | --- |
| `tests/commerce_reliability/reliability_evaluator.py` | Pure evaluators (no pytest import): `evaluate_turn` judges one customer turn from structured evidence; `evaluate_gate` judges a whole run from JUnit records against the manifest in acceptance or diagnostic mode. |
| `tests/commerce_reliability/reliability_manifest.json` | Reviewed manifest: base SHA, pinned PR #1084 module hashes, required tests per tier, baseline failures, unimplemented contracts, debt policy, PostgreSQL requirements. |
| `tests/commerce_reliability/reliability_plugin.py` | pytest hooks that apply the reconciliation rules; `baseline` / `unimplemented` fixtures. |
| `tests/commerce_reliability/conftest.py` | Plugin wiring; sqlite catalog fixture; disposable PostgreSQL fixture. |
| `tests/commerce_reliability/runtime_support.py` | Loads PR #1084's incident harness by path (unchanged), records provider responses, builds evaluator evidence, real `BrainContext` with scripted models. |
| `tests/commerce_reliability/pg_workers.py` | Entry points run in separate spawned processes (`DefaultStateStore.save`, `claim_next_batch`). |
| `tests/commerce_reliability/test_evaluator_selfcheck.py` | Evaluator self-tests (30), incl. an inner pytest session proving the reconciliation rules and the fail-closed debt rules. |
| `tests/commerce_reliability/test_runtime_v1_delivery.py` | Real `_handle_merchant_message` through PR #1084's harness: V1 terminals, replay, tenant evidence, RB-02, UC-01. |
| `tests/commerce_reliability/test_runtime_v1_browse.py` | Real decision engine / executor / composer / catalog search: RB-01, RB-03, RB-04, RB-05 and their controls. |
| `tests/commerce_reliability/test_runtime_state_postgres.py` | PostgreSQL tier: disposable UTF-8 database, spawned-process concurrency, FTS search, RB-03-PG, UC-02. |
| `scripts/commerce_reliability_gate.py` | Runner: native invocation of the PR #1084 modules, harness invocation per tier, JUnit evaluation, four-section report, JSON report, mode-aware exit status. |
| `docs/engineering/commerce-reliability-gate-ci-steps.patch` | The exact `ci.yml` hunk removed from PR #1086 (see §6). |
| `docs/evidence/commerce-reliability-gate/pr1086-e12ecba4-ci-record.md` | Terminal CI results of the only head that carried the steps. |

## 5. Running it

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
pytest (`missing_required_postgres_configuration`); with neither variable the
PostgreSQL tests skip in a developer's default run and the PostgreSQL-tier gate
fails. An empty selection, a missing required test, a required test that did
not pass, an unlisted failure/skip/xfail, or a pytest exit code other than
0/1 blocks the measurement. The PR #1084 modules must execute at least 17 and
24 tests and their content hash must equal the manifest hash
(`required_module_changed` otherwise).

The default repository run (`python -m pytest -q --maxfail=1`) also collects
this directory: recorded baselines show as `xfailed`, PostgreSQL tests skip,
and a baseline that unexpectedly passes fails that run too, as intended.

## 6. Governance split and safe integration order

GOV-002 (`scripts/lint_intelligence_non_interference.py`) lists
`.github/workflows/ci.yml` as governance core and rejects any PR that changes
it together with non-governance files (`GOVERNANCE_CORE_CHANGE`,
digest `ea73362e0d7a87a14e58f649bd69d1efee7994eb4da436c198260f5ec58fe35d` on
head `e12ecba4`). The owner chose the split; no exception, bypass,
branch-protection change or optional-workflow workaround is authorised.

Integration order (do not reorder):

1. **PR #1086 (this PR, harness only)** merges. Nothing in CI executes the
   gate yet; the harness runs inside the default root pytest run only.
2. **Governance-only follow-up PR** containing exactly the `ci.yml` hunk in
   `commerce-reliability-gate-ci-steps.patch` (`git apply` on a `main` that
   already contains step 1). It must not be opened before step 1 merges: a
   `ci.yml` step that references `scripts/commerce_reliability_gate.py` on a
   base without that script would fail its own CI. That PR changes only
   `.github/workflows/ci.yml`, which GOV-002 allows without an exception.
3. **Enforcement verification**: read branch protection / rulesets and record
   which job actually blocks merge (see §7). Only then may the milestone
   "required CI integration" be claimed.

The preserved hunk places the unit tier in `lint-and-test` and the PostgreSQL
tier in `a1-postgres-integration` (acceptance mode, so it stays red until the
debt is approved). Variant for the owner to consider in step 2 (see §7): run
the PostgreSQL tier inside `lint-and-test` too, with a `postgres:16` service
on that job, so both tiers ride the same required check.

## 7. Required-check enforcement: evidence and limitation

| Evidence | Source | Value |
| --- | --- | --- |
| `main` is a protected branch | GitHub API readback (branches list) on 2026-09-18 | `protected: true` |
| Required status checks on `main` | not readable with the access available to this session (no branch-protection or rulesets endpoint) | **UNVERIFIED** |
| Documented readback | `docs/engineering/merge-and-ci-policy.md`, `gov002-workflow-trust-root.md` | lists `lint-and-test`, `constitution-compliance`, gitleaks as required; documentation is not proof of the current setting |

Consequences:

* If `lint-and-test` is required, a gate step inside it is merge-blocking; a
  step inside `a1-postgres-integration` is not, because that job is not in the
  documented required set. The execution path that makes **both** tiers
  merge-blocking without touching protection settings is: both steps inside
  `lint-and-test`, with a `postgres:16` service added to that job for the
  PostgreSQL tier (the alternative is the owner adding
  `a1-postgres-integration` to the required checks, a settings change outside
  any PR).
* Until a readback of the required checks exists, enforcement of either tier
  is recorded as **UNVERIFIED**. Nothing in this PR changes branch protection,
  rulesets, bypass policy or CODEOWNERS.

## 8. Report sections

1. **Harness integrity** — counts (collected / passed / xfailed / skipped /
   failed), PR #1084 module execution and hash status, required-test status,
   PostgreSQL configuration (DSN reported as `set`, never echoed).
2. **Current runtime behaviour** — the runtime tests that passed: what the code
   does today at real entry points with scripted models.
3. **3a Outstanding baseline failures** (reproduced current-runtime defects)
   and **3b Unimplemented contracts** (future work, not coverage), each with
   observed status, `approved_debt`, owner, expiry, signature.
4. **Rollout readiness** — mode, acceptance status, open allowances and the
   list of unapproved debt; always states that a baseline-compatible
   measurement is not production acceptance.

## 9. Recorded baseline failures (measured at `4a270df8`)

| Id | Tier | Signature | Location |
| --- | --- | --- | --- |
| RB-01 | unit | `interpreter_browse_repeats_shown_ids` | `catalog_request_interpreter` browse branch returns snapshot rows without a cursor |
| RB-02 | unit | `recovery_resends_shown_candidates` | `product_reply_recovery` eligible candidates come from `last_search_candidates` only |
| RB-03 | unit | `broken_plural_query_returns_no_rows` | `CatalogContextBuilder.search_products` (sqlite copy) has no broken-plural handling |
| RB-03-PG | postgres | `broken_plural_query_returns_no_rows` | same, PostgreSQL FTS path |
| RB-04 | unit | `list_pick_compose_emits_no_products_template` | executor returns `products=[]` with `product=selected`; composer falls to `T.no_products(variant=2)` |
| RB-05 | unit | `fulfillment_lock_blocks_discovery` | locked session: `عندكم تنانير؟` → `llm_reply`, catalog interpreter gated off; `فيه تنانير؟` → `propose_draft_order` |

## 10. Unimplemented contracts (future work; never reported as completed)

| Id | Tier | Signature | Evidence | Planned by |
| --- | --- | --- | --- | --- |
| UC-01 | unit | `v2_owner_delivery_failure_ends_with_inferred_end_ok` | V2 owner gate on, provider rejects the text (HTTP 400): lifecycle `end_ok`, no provider message id, no outcome record, no outbound row | C1-PR2, not authorised here |
| UC-02 | postgres | `state_store_save_lost_update` | two spawned processes calling the real `DefaultStateStore.save` on one conversation: one field update is lost | C1-PR3, not authorised here |

## 11. Astra red-team reconciliation

The owner's decision message states that the complete Astra report was
supplied with it. **No report content reached this session**: the message
body contained none, and no attachment appeared in the container
(`/mnt/user-data`, `/mnt/attach`, the repository and the scratchpad were
searched). The report text therefore could not be read, and no finding can be
attributed to it. What follows reconciles the harness against the six topics
the owner named, which is all that could be done without the document:

| Topic named by the owner | How the harness treats it | Gap / owner input needed |
| --- | --- | --- |
| Missing guardrail evidence | `TurnExpectation.required_guardrails` → `missing_required_guardrail_evidence:<name>` / `guardrail_failed:<name>`; proven by the self-test. The delivery runtime tests currently require no guardrail names because the merchant-handler seam exposes guard results only through the recovery outcome (`fallback_kind`). | Naming the guardrails whose evidence must be mandatory per case (e.g. availability truth guard) needs the Astra case list. |
| Missing or ambiguous required bindings | `required_fixture_bindings` → `missing_fixture_binding:<name>`; every delivery test binds `provider:scripted_httpx` and `brain:scripted`. | Astra's binding vocabulary, if different, must be mapped before it can be enforced. |
| Separation of answer quality, runtime completion and delivery | The evaluator judges only runtime completion and delivery (closed terminals, provider acceptance, duplicate effects, tenant isolation, fallback honesty). Answer quality is out of scope here (Phase 2.7B / PR #1085 evaluators) and is never inferred from delivery success. | none known |
| Oracle / null validation | Self-tests: a valid reference passes, a null/no-op result fails, an inferred `end_ok` without a provider id is not a terminal. | none known |
| Governed baseline allowances | Reviewed manifest; exact-signature xfail; RECONCILE on unexpected pass / changed signature / expiry; acceptance mode rejects unapproved debt; diagnostic mode is never accepted. | Owner assignment of owners and expiries (§2). |
| Real-code testing with fake external services | Real `_handle_merchant_message`, `DefaultDecisionEngine`, `ProductSearchHandler`, `DefaultComposer`, `CatalogContextBuilder`, `claim_next_batch`, `DefaultStateStore` with a scripted httpx provider and scripted model outputs; no production connection, customer data or live provider call. | none known |

When the report is delivered, each Astra finding should be mapped to an
existing case or added as a new manifest entry with its own measured
signature; nothing in the existing entries should be edited to fit it.
