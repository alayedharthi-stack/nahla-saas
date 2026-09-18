# Required PostgreSQL proofs — strict execution inside `lint-and-test`

Status: prepared 2026-09-18 for review. This document describes the strict
runner, its inventory and the proposed CI invocation. Nothing here changes the
commerce reliability gate, its manifest, its allowances or its acceptance.

## 1. Scope

Two proof suites must execute against a real PostgreSQL on every pull request
and must be **merge-blocking**. Owner-provided screenshots of the `main`
protection rule show the required checks `Scan repository for leaked secrets`,
`lint-and-test`, `constitution-compliance` and `merge-freeze-gate`, with
"require branches to be up to date" and "do not allow bypassing" enabled;
`a1-postgres-integration` is **not** required. The suites therefore run inside
`lint-and-test` with a disposable `postgres:16` service (Variant B).

| Suite id | Module | Origin | Required tests |
| --- | --- | --- | --- |
| `commerce_runtime_foundation` | `tests/commerce_reliability/test_commerce_runtime_foundation_pg.py` | PR #1089, dormant commerce runtime foundation | 18 |
| `global_customer_identity` | `backend/tests/test_global_customer_display_identity_pg.py` | PR #1087 (merged), 0107 persistence cases | 5 |

The exact node ids live in `scripts/required_postgres_proofs.json`
(`python scripts/required_postgres_proofs.py --manifest scripts/required_postgres_proofs.json --junit-dir /tmp/x --list`
prints them). The identity ids carry pytest's escaped parametrisation form,
for example `[مشاعل-None]`, exactly as pytest collects
and reports them.

## 2. Strict execution mechanism

`scripts/required_postgres_proofs.py` runs each suite as its own pytest
process with `--junitxml` and judges the JUnit output:

| Rule | Effect |
| --- | --- |
| Required environment missing, blank or with a wrong value | exit **2**, pytest is not started for any suite |
| Listed module missing | exit **2**, nothing runs |
| Required node id not collected | exit **1** (`missing_collection`) |
| Collected node id not in the inventory | exit **1** (`uninventoried_collection`) |
| Any skip, failure or error | exit **1**, the node id and reason are listed; a skip is never a pass |
| Leaf `testsuite` counts differ from the inventory or show skips, failures or errors | exit **1** |
| Non-zero pytest exit | exit **1** |
| Everything above satisfied for every suite | exit **0**, `PROVEN (23/23 required tests passed, 0 skips tolerated)` |

The runner is pure standard library, imports no application code and carries
no allowances. `tests/commerce_reliability/test_required_postgres_proofs_runner.py`
proves every rule with a negative control on synthetic suites and pins the
committed inventory to pytest's own collection of the two real modules, so an
added or removed test is a reviewed inventory change, never a silent drift.

### Fixture contracts (unchanged; used as they are)

* Foundation suite: `NAHLA_RELIABILITY_REQUIRE_PG=1` with
  `NAHLA_RELIABILITY_PG_ADMIN_DSN` set. The harness fixture fails (never
  skips) when the flag is set without the DSN; without the flag it skips,
  which ordinary local runs report as skipped and which is never proof. Each
  session creates and drops its own database (`nahla_runtime_*`).
* Identity suite: `CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1` makes an
  unavailable PostgreSQL a failure instead of a skip;
  `LEGACY_MIG_PG_TEST_DATABASE_URL` is the preferred admin URL. The shared
  fixture's own candidate order is that variable, then
  `A1_PG_TEST_DATABASE_URL`, then `DATABASE_URL`, then the default
  `postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas`; in CI every
  candidate resolves to the same disposable service. Ephemeral databases
  (`legacy_mig_*`) are created at revision `0107` and dropped.

## 3. Proposed CI invocation (separate `ci.yml`-only pull request)

Inside `lint-and-test`: a `postgres:16` service (user `nahla`, database
`nahla_saas`, host port `5433`, the same health options as the
`a1-postgres-integration` job) and one step directly after "Run unit tests":

```yaml
      - name: Required PostgreSQL proofs (commerce runtime foundation + customer identity)
        env:
          NAHLA_RELIABILITY_REQUIRE_PG: "1"
          NAHLA_RELIABILITY_PG_ADMIN_DSN: postgresql://nahla:nahla_password@127.0.0.1:5433/postgres
          CUSTOMER_NAME_PROVENANCE_PG_REQUIRED: "1"
          LEGACY_MIG_PG_TEST_DATABASE_URL: postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas
        run: |
          set -euo pipefail
          python scripts/required_postgres_proofs.py \
            --manifest scripts/required_postgres_proofs.json \
            --junit-dir /tmp/required-postgres-proofs \
            --report /tmp/required-postgres-proofs/report.json
```

The environment is **step-scoped**. No job-level variable is added, so the
root `python -m pytest -q --maxfail=1` step and every later step see exactly
the environment they see today.

### Why the service does not redirect existing tests

* The root collection (283 files under `tests/`) contains no test that
  connects to `127.0.0.1:5433`; the only mention is a fake DSN inside the
  reliability evaluator's pure self-tests. The harness's own PostgreSQL tier
  keeps skipping in that step because its variables are not set there.
* The `backend/tests` modules that `lint-and-test` runs explicitly build
  their engines on `sqlite+pysqlite:///:memory:` (staging migration and
  tenant clone operators), use must-not-connect sentinels or fake
  `postgres://staging` values, or are gated on `TENANT_CLONE_PG_*` variables
  that the job does not set.
* Evidence: the full root suite run locally with a PostgreSQL 16 service
  reachable on `127.0.0.1:5433` (database `nahla_saas` present) and no step
  variables produced 6751 passed, 63 skipped, 7 xfailed, exit 0; the run
  without a service on the same base gave 6742 passed, 63 skipped, 7 xfailed,
  and the 9 extra passes are exactly this branch's runner self-tests. No
  skip count moved, so no existing test found and used the service.

## 4. Separation from the commerce reliability gate

This step proves the inventoried suites only. It does not execute
`scripts/commerce_reliability_gate.py`, and a PROVEN verdict says nothing
about that gate's acceptance, which remains **NOT ACCEPTED** while its eight
baseline allowances are unapproved (owners `UNASSIGNED`, expiry `null`).
Running the gate's acceptance tiers inside `lint-and-test` (the "full Variant
B") is prepared but cannot pass until the owner approves that debt, so it is
not proposed in the same change; nothing here suppresses an acceptance
failure, substitutes diagnostic mode or creates an allowance.

## 5. Dependency and merge order

1. PR #1088 — contract record (governance only).
2. PR #1089 — dormant persistence foundation (provides the 18 proofs).
3. Runner PR — this document, `scripts/required_postgres_proofs.py`,
   `scripts/required_postgres_proofs.json`, the self-test module. Based on
   #1089 because the inventory pin needs the foundation module.
4. `ci.yml`-only PR — the service and the step above. Based on the runner
   PR so its CI run exercises the new step with the new runner.

## 6. Local usage

```bash
# PROVEN run (a local PostgreSQL 16 bound to 127.0.0.1:5433, role nahla)
NAHLA_RELIABILITY_REQUIRE_PG=1 \
NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://nahla:nahla_password@127.0.0.1:5433/postgres \
CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1 \
LEGACY_MIG_PG_TEST_DATABASE_URL=postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas \
python scripts/required_postgres_proofs.py --manifest scripts/required_postgres_proofs.json --junit-dir /tmp/required-postgres-proofs

# Ordinary local run of the modules without the variables: the proofs skip and are reported as skipped.
python -m pytest tests/commerce_reliability/test_commerce_runtime_foundation_pg.py -q -rs
```

Recorded on 2026-09-18 (PostgreSQL 16.13, Python 3.11): PROVEN, 23/23 passed,
0 skipped, 0 databases left behind; with the variables unset the runner exits
2 before starting pytest; with the variables set and no server listening, the
foundation suite reports 2 failed + 16 errors and the identity suite 5 errors
(its module-scoped fixture fails instead of skipping), verdict NOT PROVEN, 0/23.
