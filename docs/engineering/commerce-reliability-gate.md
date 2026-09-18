# Commerce Conversation Reliability Gate (C1-PR1)

**Scope:** the reliability harness and its CI execution path only. No runtime
behaviour, prompt, model, migration, activation or deployment change ships
with this gate. A test that exposes a product defect records it in the
reviewed manifest; it never repairs product code and never weakens itself.

**Base:** `origin/main` at `4a270df82bcf11cdd88b1ef6d9946dc2dc46ca3e` (merge
of PR #1084). Every recorded baseline failure below was measured against
that SHA with real application code, scripted models and fake providers.

## What the gate proves, and what it does not

| Proves | Does not prove |
| --- | --- |
| The PR #1084 regression modules run **unchanged** (content hash pinned) and pass | That production delivery is fixed |
| The reliability evaluator rejects null results, wrong-tenant evidence, duplicate effects, missing terminal records, missing required evidence, missing guardrail evidence, missing fixture bindings and runtime fallbacks disguised as success | Anything about real customer traffic |
| Current V1 delivery behaviour at the merchant-handler boundary: accepted send → closed terminal; missing provider message id / timeout → explicit failure without blind resend; definitive rejection → one grounded text recovery; webhook replay → no second send; single-tenant evidence | That the six V1 browse defects are fixed (they are recorded, see below) |
| Real PostgreSQL semantics in a disposable database with **separate spawned processes**: `claim_next_batch` never double-claims; catalog search is tenant-isolated | That brain-state persistence is race-safe (it is not: UC-02) |
| Every recorded baseline still reproduces with its exact signature | Production acceptance of any kind |

**A baseline-compatible gate result is not production acceptance.** The gate
passes while known defects stand precisely so that the defects cannot be
forgotten: each one is an explicit, reviewed allowance with a removal
condition, and the gate fails the moment the defect stops reproducing.

## Components

| Path | Role |
| --- | --- |
| `tests/commerce_reliability/reliability_evaluator.py` | Pure evaluators. `evaluate_turn` judges one customer turn from structured evidence (never prose). `evaluate_gate` judges a whole run from JUnit records against the manifest. No pytest import. |
| `tests/commerce_reliability/reliability_manifest.json` | The reviewed manifest: base SHA, pinned PR #1084 module hashes, required test ids per tier, baseline failures, unimplemented contracts, PostgreSQL requirements. |
| `tests/commerce_reliability/reliability_plugin.py` | pytest hooks that apply the reconciliation rules, plus the `baseline` / `unimplemented` fixtures used to raise the exact recorded marker. |
| `tests/commerce_reliability/conftest.py` | Wires the plugin and provides the sqlite catalog fixture and the disposable PostgreSQL fixture. |
| `tests/commerce_reliability/runtime_support.py` | Loads the PR #1084 incident harness by path (unchanged), records provider responses, turns observations into evaluator evidence, builds real `BrainContext` objects with scripted models. |
| `tests/commerce_reliability/pg_workers.py` | Entry points executed in separate spawned processes (`DefaultStateStore.save`, `claim_next_batch`). |
| `tests/commerce_reliability/test_evaluator_selfcheck.py` | Evaluator self-tests (distinct from runtime tests), including an inner pytest session that proves the reconciliation rules fail an unexpected pass and a changed signature. |
| `tests/commerce_reliability/test_runtime_v1_delivery.py` | Real `_handle_merchant_message` through PR #1084's harness: V1 delivery terminals, replay, tenant evidence, RB-02, UC-01. |
| `tests/commerce_reliability/test_runtime_v1_browse.py` | Real decision engine, executor, composer and catalog search: list pick, "more products", fulfillment lock, broken plural (RB-01, RB-03, RB-04, RB-05). |
| `tests/commerce_reliability/test_runtime_state_postgres.py` | PostgreSQL tier: disposable UTF-8 database, spawned-process concurrency, FTS search (RB-03-PG, UC-02). |
| `scripts/commerce_reliability_gate.py` | Runner: native invocation of the PR #1084 modules, harness invocation per tier, JUnit evaluation, four-section report, JSON report, exit status. |

## Running it

Unit tier (what `lint-and-test` runs):

```bash
python scripts/commerce_reliability_gate.py --tier unit \
  --junit-dir /tmp/commerce-reliability --report /tmp/commerce-reliability/gate-unit.json
```

PostgreSQL tier (what `a1-postgres-integration` runs). The admin DSN must
point at a server where the role may `CREATE DATABASE`; the gate creates
`nahla_reliability_<random>` with `ENCODING 'UTF8' TEMPLATE template0` and
drops it `WITH (FORCE)` at the end of the session:

```bash
NAHLA_RELIABILITY_REQUIRE_PG=1 \
NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://nahla:nahla_password@127.0.0.1:5433/postgres \
python scripts/commerce_reliability_gate.py --tier postgres \
  --junit-dir /tmp/commerce-reliability --report /tmp/commerce-reliability/gate-postgres.json
```

Rules the runner enforces before and after pytest:

- `NAHLA_RELIABILITY_REQUIRE_PG=1` without `NAHLA_RELIABILITY_PG_ADMIN_DSN`
  fails with `missing_required_postgres_configuration` before pytest starts.
  With neither variable, the PostgreSQL tests **skip** in a developer's default
  `python -m pytest` run and the PostgreSQL-tier gate **fails**
  (`postgres_tier_requires_NAHLA_RELIABILITY_REQUIRE_PG=1`).
- An empty selection, a missing required test, a required test that did not
  pass, an unlisted failure, an unlisted skip, an unlisted xfail, or a pytest
  exit code other than 0/1 fails the gate.
- The PR #1084 modules must execute at least 17 and 24 tests respectively and
  their content hash must equal the manifest hash (`required_module_changed`
  otherwise).

The default repository run (`python -m pytest -q --maxfail=1`, `testpaths =
tests`) also collects this directory: the recorded baselines show as
`xfailed`, the PostgreSQL tests skip, and a baseline that unexpectedly passes
fails that run too, which is intended.

## Reconciliation rules (never silently broadened)

A test listed in the manifest as a baseline failure or an unimplemented
contract must fail by raising the exact marker:

```
RELIABILITY_BASELINE[RB-04] list_pick_compose_emits_no_products_template
RELIABILITY_UNIMPLEMENTED[UC-01] v2_owner_delivery_failure_ends_with_inferred_end_ok
```

| Observation | Result |
| --- | --- |
| Exact marker raised | Reported as an expected failure (xfail with the marker as reason); gate stays baseline-compatible |
| The test passes | `RECONCILE unexpected_pass:<id>` failure. Delete the manifest entry in the fixing PR. |
| The test fails differently | `RECONCILE signature_changed:<id>` failure. Re-measure and re-review. |
| Expiry date passed | `RECONCILE expired_allowance:<id>` failure, whatever the test did. |
| Allowance test missing from the run | `allowance_test_missing:<id>` gate blocker. |
| Marker raised by a test not listed for that id | `RECONCILE allowance_not_listed_for_test` failure. |

The plugin and the runner apply these rules independently (the plugin from
the exception, the runner from the JUnit file), so neither layer can pass
the gate alone. `NAHLA_RELIABILITY_TODAY` exists only for the plugin's
self-test; the runner removes it and judges expiry against the real date.

### Removing an allowance

1. Land the product fix in its own PR (C1-PR2 / C1-PR3 scope, not this gate).
2. In the same PR delete the manifest entry; the test now passes and moves
   from `baseline_failures` / `unimplemented_contracts` to `required_tests`.
3. Never edit a test to keep an allowance alive, and never widen a signature.

### Owners and expiry

Every entry carries `owner: "UNASSIGNED (owner review)"` and `expiry: null`
with a `proposed_owner` / `proposed_expiry` note. These are proposals for
owner review, not assignments; the gate warns
(`allowance_owner_unassigned_owner_review`, `allowance_expiry_unset_owner_review`)
until a reviewer fills them in. The harness never invents names or dates.

## Report sections

The runner prints and stores four sections:

1. **Harness integrity** — counts (collected / passed / xfailed / skipped /
   failed), PR #1084 module execution and hash status, required-test status,
   PostgreSQL configuration (DSN reported as `set`, never echoed).
2. **Current runtime behaviour** — the runtime tests that passed, i.e. what
   the code does today at real entry points with scripted models.
3. **Outstanding baseline failures** — each allowance with its observed
   status, owner, expiry and signature; unimplemented contracts listed
   separately and never as completed behaviour.
4. **Rollout readiness** — always states that a baseline-compatible result
   is not production acceptance, with the count of open allowances.

## Recorded baseline failures (measured at `4a270df8`)

| Id | Tier | Signature | Location | Removal condition |
| --- | --- | --- | --- | --- |
| RB-01 | unit | `interpreter_browse_repeats_shown_ids` | `catalog_request_interpreter` browse branch returns snapshot rows without a cursor | Browse decision excludes shown ids or advances a cursor |
| RB-02 | unit | `recovery_resends_shown_candidates` | `product_reply_recovery` eligible candidates come from `last_search_candidates` only | Recovery for a "more" request presents unseen candidates or an honest no-more text |
| RB-03 | unit | `broken_plural_query_returns_no_rows` | `CatalogContextBuilder.search_products` (sqlite copy) — no broken-plural handling | Search resolves `تنانير` to `تنورة` rows, same tenant only; not a hardcoded noun table as final design |
| RB-03-PG | postgres | `broken_plural_query_returns_no_rows` | Same, on the PostgreSQL FTS path | Same as RB-03 |
| RB-04 | unit | `list_pick_compose_emits_no_products_template` | Executor returns `products=[]` with `product=selected`; composer falls to `T.no_products(variant=2)` | Composer grounds the selected singleton |
| RB-05 | unit | `fulfillment_lock_blocks_discovery` | Locked fulfillment session: `عندكم تنانير؟` → `llm_reply`; catalog interpreter gated off; `فيه تنانير؟` → `propose_draft_order` | Explicit new-category discovery reaches product search without weakening fulfillment answers |

## Unimplemented runtime contracts (never reported as completed)

| Id | Tier | Signature | Evidence | Planned by |
| --- | --- | --- | --- | --- |
| UC-01 | unit | `v2_owner_delivery_failure_ends_with_inferred_end_ok` | With the V2 owner gate on and the provider rejecting the text send (HTTP 400), the lifecycle ends `end_ok` with no provider message id, no outcome record and no outbound row | C1-PR2 (delivery terminal contract), not authorised here |
| UC-02 | postgres | `state_store_save_lost_update` | Two spawned processes calling the real `DefaultStateStore.save` on the same conversation: one field update is lost | C1-PR3 (state compare-and-set), not authorised here |

## Enforcement evidence and limitations

- The unit tier runs inside the `lint-and-test` job. Repository
  documentation (`docs/engineering/merge-and-ci-policy.md`,
  `docs/engineering/gov002-workflow-trust-root.md` readback) records
  `lint-and-test`, `constitution-compliance` and the gitleaks scan as the
  required status checks on `main`. That readback was not repeated for this
  PR; branch protection is external to the repository and unchanged by it.
- The PostgreSQL tier runs inside the `a1-postgres-integration` job, which
  the same documentation does **not** list as required. Until a branch
  protection or ruleset readback shows otherwise, treat the PostgreSQL tier
  as executed on every PR but **not merge-blocking**.
- Nothing in this PR changes branch protection, rulesets, bypass policy or
  CODEOWNERS.

## Inputs used and a discrepancy note

The harness follows the Phase C0 architecture report (this repository's
audit) and PR #1084's harness conventions. The Astra red-team report named
in the authorisation was not available to the harness author, so no
discrepancy between it and the C0 report could be checked; owner review
should compare its findings against the recorded baselines above and add
any missing case as a new manifest entry with its own measured signature.
