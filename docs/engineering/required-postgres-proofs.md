# Required PostgreSQL proofs — strict execution inside `lint-and-test`

Status: prepared 2026-09-18 for review; corrected 2026-09-18 (explicit
target authority at the connection boundary, report freshness, inventory
kinds); extended 2026-09-19 with the effect and delivery ledger proofs. This document describes the strict runner, its inventory and the
proposed CI invocation. Nothing here changes the commerce reliability gate,
its manifest, its allowances or its acceptance.

## 1. Scope

The inventoried PostgreSQL suites must execute against a real PostgreSQL on
every pull request and must be **merge-blocking**. Owner-provided screenshots
of the `main` protection rule show the required checks `Scan repository for
leaked secrets`, `lint-and-test`, `constitution-compliance` and
`merge-freeze-gate`, with "require branches to be up to date" and "do not
allow bypassing" enabled; `a1-postgres-integration` is **not** required. The
suites therefore run inside `lint-and-test` with a disposable `postgres:16`
service (Variant B). The additional `postgres-18-compatibility` job runs the same
complete strict inventory on an isolated PostgreSQL 18 service, including a
server-major assertion. It supplements the existing required PostgreSQL 16
execution; no existing check or inventory identifier is removed. Both jobs must
pass before deploying this compatibility correction.

PostgreSQL 18 exposes validated NOT NULL constraints as `contype=n` in
`pg_constraint`. Revisions 0108/0109 verify nullability through the complete
column definition comparison, and exclude only validated NOT NULL catalog rows
from table-constraint name comparisons. Missing or extra nullability and an
extra CHECK whose name ends in `_not_null` still fail. Unvalidated constraints
remain visible and fail compatibility.

The inventory distinguishes two kinds of suite:

* `proof` — the PostgreSQL proofs of the dormant commerce runtime
  foundation, its migration, global customer identity, and the effect and
  delivery ledgers with their migration. These are the proofs the owner asked
  to make required.
* `runner_regression` — **2 regressions of the strict harness itself** (the
  runner and the shared PostgreSQL fixture's connection selection). They run
  on the same service and are equally required, but they prove that the
  harness fails the way it must; they are not proofs of commerce runtime or
  identity behaviour, and the verdict counts them separately.

| Suite id | Kind | Module | Origin | Tests |
| --- | --- | --- | --- | --- |
| `commerce_runtime_foundation` | proof | `tests/commerce_reliability/test_commerce_runtime_foundation_pg.py` | PR #1089, dormant commerce runtime foundation, including the lock-wait, scope-binding and ordered-processing regressions | 27 |
| `commerce_runtime_migration` | proof | `tests/commerce_reliability/test_commerce_runtime_migration_pg.py` | PR #1089, revision 0108 reconciliation (fresh, compatible pre-creation, refused incompatible shapes, trigger on the correct relation; nullability drift and constraint-kind controls) | 13 |
| `global_customer_identity` | proof | `backend/tests/test_global_customer_display_identity_pg.py` | PR #1087 (merged), 0107 persistence cases | 5 |
| `commerce_runtime_ledgers` | proof | `tests/commerce_reliability/test_commerce_runtime_ledgers_pg.py` | ledger PR, dormant effect and delivery ledgers (business-action identity, dispatch reservation, honest outcomes, bounded recovery, atomic decision commit, ledger-derived terminals, completion boundary on both terminal entry points, distinct business identities, schema-state completion guard with the standalone-0108 control, reservation/completion race in both lock orders) | 30 |
| `commerce_runtime_ledgers_migration` | proof | `tests/commerce_reliability/test_commerce_runtime_ledgers_migration_pg.py` | ledger PR, revision 0109 reconciliation (fresh, compatible pre-creation, refused incompatible shapes, append-only triggers on the correct relations, foundation tables required; nullability drift and constraint-kind controls) | 15 |
| `commerce_runtime_agent_loop` | proof | `tests/commerce_reliability/test_commerce_runtime_agent_loop_pg.py` | agent loop PR, dormant agent loop core and its durable guarantees (reasoning with read-only fixture tools and observations feeding the next decision; revision-bound attempt debits, concurrency arbitration and re-entry restoration; enforced provider and tool waits with the deadline re-checked inside the reservation transaction; scope, eligibility and ownership boundaries; complete provider-result validation; isolation of authoritative schemas and context; closed ownership-loss outcomes; bundle-duplicate refusal and the durable crash-safe recovery allowance) | 39 |
| `commerce_runtime_pilot` | proof | `tests/commerce_reliability/test_commerce_runtime_pilot_pg.py` | pilot integration and corrected admission, ownership, delivery and recovery boundaries with scripted provider/transport | 57 |
| `commerce_runtime_handover_migration` | proof | `tests/commerce_reliability/test_commerce_runtime_handover_migration_pg.py` | revision 0111 compatibility and sibling migration controls | 25 |
| `commerce_runtime_pilot_handover_controls` | proof | `tests/commerce_reliability/test_commerce_runtime_pilot_handover_controls_pg.py` | durable acceptance, handover, recovery, scoped disposition and retirement | 83 |
| `commerce_runtime_trial_evidence` | proof | `tests/commerce_reliability/test_commerce_runtime_trial_evidence_pg.py` | repository-written turn evidence; live/tenant isolation; normal resolved-state accounting; read-only snapshot against an independent completing connection | 13 |
| `runner_connection_regressions` | runner_regression | `tests/commerce_reliability/test_required_postgres_proofs_connection_pg.py` | runner PR, explicit target authority at the connection boundary (section 2.2) | 2 |
| `salla_customer_address_candidates` | proof | `backend/tests/test_salla_customer_address_candidates_pg.py` | Salla address-candidate PR, revision 0110 (fresh, `create_all` reconciliation, reversible), candidate durability across commit/session/conversation, one provenance row per address, tenant isolation, selected-address reuse after reset, and the independent review's closure cases: evidence absent before commit / present after / gone after rollback, two concurrent first imports committing one address, an absent provenance table still committing the confirmed address, and a concurrent refresh between offer and selection refused | 31 |

The integrated branch inventories 340 cases (338 proofs + 2 runner regressions):
all 321 identifiers from address integration `8f2bd079`, plus the collector's
13, followed by six nullability/constraint-kind controls for PostgreSQL 18
compatibility. All 334 pre-correction identifiers remain. All 290 earlier
runtime identifiers and all 31 address identifiers are
retained. This is an inventory statement, not a claim that PostgreSQL execution
on the integrated head has passed.

The counts above are informational. Nothing in the runner or its self-test
pins a count: the inventory must equal pytest's own collection of each
module, so a test added without an inventory update fails
(`uninventoried_collection`) rather than being omitted, and a listed test
that disappears fails (`missing_collection`).

The exact node ids live in `scripts/required_postgres_proofs.json`
(`python scripts/required_postgres_proofs.py --manifest scripts/required_postgres_proofs.json --junit-dir /tmp/x --list`
prints them with their kind). The identity ids carry pytest's escaped
parametrisation form, for example `[مشاعل-None]`, exactly as pytest collects
and reports them.

Six further pure tests, `tests/commerce_reliability/test_pg_connection_selection.py`,
exercise the shared fixture's candidate selection with a recording fake
engine and no database. They belong to the ordinary root suite, not to the
strict inventory, because they need no PostgreSQL.

## 2. Strict execution mechanism

`scripts/required_postgres_proofs.py` runs each suite as its own pytest
process with `--junitxml` and judges the JUnit output:

| Rule | Effect |
| --- | --- |
| Manifest unreadable, invalid, or a suite with an unknown `kind` | exit **2**, nothing runs |
| Required environment missing, blank or with a wrong value | exit **2**, pytest is not started for any suite |
| Listed module missing | exit **2**, nothing runs |
| Required node id not collected | exit **1** (`missing_collection`) |
| Collected node id not in the inventory | exit **1** (`uninventoried_collection`) |
| Any skip, failure or error | exit **1**, the node id and reason are listed; a skip is never a pass |
| Leaf `testsuite` counts differ from the inventory or show skips, failures or errors | exit **1** |
| Non-zero pytest exit | exit **1** |
| Everything above satisfied for every suite | exit **0**, `PROVEN`, with actual inventory totals, per-kind counts and zero skips |

The runner is pure standard library, imports no application code and carries
no allowances. `tests/commerce_reliability/test_required_postgres_proofs_runner.py`
proves every rule with a negative control on synthetic suites, proves that
a test added to a module without an inventory update is refused, proves the
report freshness rule below, and pins the committed inventory to pytest's
own collection of every inventoried module, so an added or removed test
is a reviewed inventory change, never a silent drift.

### 2.1 Report freshness

Before the manifest is read, the runner **invalidates the previous
outputs**: it deletes any report at `--report` and any `*.xml` in
`--junit-dir`, then writes a placeholder report
`{"verdict": "NOT RUN", "reason": "validation_not_started", ...}`. Every
early exit replaces that placeholder with the failure it hit:

| Early failure | Report written | Exit |
| --- | --- | --- |
| Manifest unreadable or invalid | `{"verdict": "NOT RUN", "reason": "manifest", "detail": ...}` | 2 |
| Required environment missing | `{"verdict": "NOT RUN", "reason": "configuration", "suites": {...}}` | 2 |
| Run completed | `PROVEN` or `NOT PROVEN` with per-suite results and `by_kind` counts | 0 / 1 |

A previous PROVEN report therefore never appears current after a later
failed or aborted run at the same path. `--list` neither resets nor writes
outputs. Regression: `test_stale_report_and_junit_are_invalidated_before_validation`
in the runner self-test module.

### 2.2 Explicit target authority at the connection boundary (R1)

The identity proofs connect through the shared fixture helper
`backend/tests/legacy_migration_drift_postgres_fixtures.py`
(`connect_engine`). Its rule:

* When `LEGACY_MIG_PG_TEST_DATABASE_URL` is set, that URL is
  **authoritative and the only candidate**. A connection failure fails the
  caller when integration is required (`LEGACY_MIG_PG_INTEGRATION_REQUIRED=1`
  directly, or `CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1` through the identity
  fixture, which turns the helper's skip into a failure) and skips otherwise.
  It never falls through to `A1_PG_TEST_DATABASE_URL`, `DATABASE_URL` or the
  default service URL, so no connection and no `legacy_mig_*` database
  creation can happen on an alternate service. The message begins with
  `no fallback attempted`, names the explicit target with its password
  redacted, and scrubs the password from the driver's error text.
* Without the explicit variable the historical order is unchanged
  (`A1_PG_TEST_DATABASE_URL`, then `DATABASE_URL`, then the default
  `postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas`). The
  `a1-postgres-integration` job never sets the explicit variable, so its
  behaviour is unchanged.

The runner's configuration preflight (every required variable present before
pytest starts) remains, but it is not the enforcement. Enforcement is at the
connection boundary, inside the fixture that every identity proof uses; a
preflight cannot see which URL a fixture actually connected to.

Regressions:

* `tests/commerce_reliability/test_pg_connection_selection.py` (6 pure
  tests, root suite): the explicit target is the only candidate; an
  unreachable explicit target with every alternate reachable fails
  (required) or skips (not required) after attempting exactly one URL, with
  the target named and the password absent; the historical order and its
  later-candidate fallback are unchanged without the explicit variable.
* `runner_connection_regressions` (2 tests, strict inventory, PostgreSQL):
  the runner executed on the identity suite with the explicit URL pointing
  at a dead port while `A1_PG_TEST_DATABASE_URL` and `DATABASE_URL` point at
  the live service exits 1 with `NOT PROVEN`, 0 passed, 0 skipped, errors
  plus failures equal to the required count, a blocker naming the dead
  target without its password, and the set of `legacy_mig_*` databases on
  the live service unchanged. The positive control (explicit URL live, every
  alternate dead) is PROVEN with no leftover database.

### Fixture contracts

* Foundation, migration, ledger and runner regression suites:
  `NAHLA_RELIABILITY_REQUIRE_PG=1` with `NAHLA_RELIABILITY_PG_ADMIN_DSN` set.
  The harness fixture fails (never skips) when the flag is set without the
  DSN; without the flag it skips, which ordinary local runs report as
  skipped and which is never proof. Each session creates and drops its own
  database (`nahla_runtime_*`).
* Identity suite: `CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1` makes an
  unavailable PostgreSQL a failure instead of a skip;
  `LEGACY_MIG_PG_TEST_DATABASE_URL` is the authoritative admin URL
  (section 2.2). Ephemeral databases (`legacy_mig_*`) are created at
  revision `0107` and dropped.
* Address-candidate suite: gated on `LEGACY_MIG_PG_TEST_DATABASE_URL`
  alone, which the existing required-proofs step already provides — the
  suite therefore needs **no workflow change**. An explicit target is
  authoritative, so the module fails rather than skips when it is
  unreachable, and the runner counts a skip as a failure regardless.
  `CUSTOMER_ADDRESS_CANDIDATES_PG_REQUIRED=1` and
  `LEGACY_MIG_PG_INTEGRATION_REQUIRED=1` also force it, for operators and
  for any later `ci.yml`-only pull request that wants an explicit flag.
  Ephemeral databases (`legacy_mig_*`) are created at revision `0109` or
  `0110` and dropped.

## 3. Proposed CI invocation (separate `ci.yml`-only pull request)

Inside `lint-and-test`: a `postgres:16` service (user `nahla`, database
`nahla_saas`, host port `5433`, the same health options as the
`a1-postgres-integration` job) and one step directly after "Run unit tests":

```yaml
      - name: Required PostgreSQL proofs (commerce runtime foundation, migration, customer identity)
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

The step executes the whole inventory: the 80 proofs and the 2 runner/fixture
regressions. The environment is **step-scoped**. No job-level variable is
added, so the root `python -m pytest -q --maxfail=1` step and every later
step see exactly the environment they see today.

### Why the service does not redirect existing tests

* The root collection contains no test that connects to `127.0.0.1:5433`
  without the step variables; the only mentions are a fake DSN inside the
  reliability evaluator's pure self-tests and the dead-port and
  service-URL constants of the connection regressions, which skip without
  the harness variables. The harness's own PostgreSQL tier keeps skipping in
  the root step because its variables are not set there.
* The `backend/tests` modules that `lint-and-test` runs explicitly build
  their engines on `sqlite+pysqlite:///:memory:` (staging migration and
  tenant clone operators), use must-not-connect sentinels or fake
  `postgres://staging` values, or are gated on `TENANT_CLONE_PG_*` variables
  that the job does not set.
* Evidence, first runner head: the full root suite run locally with a
  PostgreSQL 16 service reachable on `127.0.0.1:5433` (database `nahla_saas`
  present) and no step variables produced 6751 passed, 63 skipped,
  7 xfailed, exit 0; the run without a service on the same base gave
  6742 passed, 63 skipped, 7 xfailed, and the 9 extra passes were exactly
  that head's runner self-tests. No skip count moved, so no existing test
  found and used the service.
* Evidence, corrected head: the same root suite with the service reachable
  and no step variables gave 6771 passed, 84 skipped, 7 xfailed, exit 0
  (206 s). Of the 84 skips, 39 are the harness-gated PostgreSQL modules
  (27 foundation, 10 migration, 2 connection regressions) skipping because
  the step variables are not set in that step; the remaining 45 equal the
  first head's non-PostgreSQL skips (63 minus its 18 foundation skips).
  Again no test found and used the service.

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
2. PR #1089 — dormant persistence foundation (provides the 37 foundation
   and migration proofs).
3. Runner PR — this document, `scripts/required_postgres_proofs.py`,
   `scripts/required_postgres_proofs.json`, the runner self-test module,
   the shared fixture correction
   (`backend/tests/legacy_migration_drift_postgres_fixtures.py`) with its
   pure regressions and the PostgreSQL connection regressions. Based on
   #1089 because the inventory pin needs the foundation module.
4. `ci.yml`-only PR — the service and the step above, nothing else. Based on
   the runner PR so its CI run exercises the new step with the new runner;
   it carries no fixture, runner or test edits of its own.
5. Ledger PR (after the four above merged) — revision `0109`, the ledger
   modules, their proofs and the two inventory entries above. The step's
   `ci.yml` is unchanged; the inventory file alone extends what it proves.

## 6. Local usage

```bash
# PROVEN run (a local PostgreSQL 16 bound to 127.0.0.1:5433, role nahla)
NAHLA_RELIABILITY_REQUIRE_PG=1 \
NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://nahla:nahla_password@127.0.0.1:5433/postgres \
CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1 \
LEGACY_MIG_PG_TEST_DATABASE_URL=postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas \
python scripts/required_postgres_proofs.py --manifest scripts/required_postgres_proofs.json --junit-dir /tmp/required-postgres-proofs --report /tmp/required-postgres-proofs/report.json

# Negative control for explicit target authority: the same command with
# LEGACY_MIG_PG_TEST_DATABASE_URL pointing at a dead port (for example 5499)
# and A1_PG_TEST_DATABASE_URL / DATABASE_URL pointing at the live service
# exits 1, NOT PROVEN, with the identity suite in error and no legacy_mig_*
# database created on the live service.

# Ordinary local run of the modules without the variables: the proofs skip and are reported as skipped.
python -m pytest tests/commerce_reliability/test_commerce_runtime_foundation_pg.py -q -rs
```

Recorded on 2026-09-18 (PostgreSQL 16.13, Python 3.11), corrected head:
PROVEN 44/44 (27 + 10 + 5 proofs, 2 runner/fixture regressions), 0 skipped,
70 s, 0 databases left behind. With the variables unset the runner exits 2
before starting pytest and the report reads `NOT RUN` / `configuration`;
with an unreadable manifest at a path that already held a PROVEN report,
the runner exits 2, the report reads `NOT RUN` / `manifest`, and the old
JUnit files are gone. With the explicit identity URL pointing at a dead port
and every alternate pointing at the live service, the run exits 1, NOT
PROVEN (39/44: 37/42 proofs + 2/2 regressions), the identity suite reports
5 errors whose message begins `no fallback attempted` and names
`postgresql://nahla:***@127.0.0.1:5499/nahla_saas`, the live service's
`legacy_mig_*` set is unchanged, and PostgreSQL connection logging on the
live service recorded zero connections from the identity suite during that
run (the positive control, explicit live and alternates dead, recorded its
connections on the explicit target only).

Recorded on 2026-09-19 (PostgreSQL 16.13, Python 3.11), ledger head: PROVEN
78/78 (27 + 10 + 5 + 22 + 12 proofs, 2 runner/fixture regressions), 0 skipped,
132 s, 0 databases left behind. The root suite with the service reachable
and no step variables gave 6797 passed, 118 skipped, 7 xfailed, exit 0; the
34 additional skips are exactly the two new PostgreSQL modules (22 + 12)
skipping without the step variables, and the additional passes are the new
pure ledger contract tests. No existing test found and used the service.

Recorded on 2026-09-19 (PostgreSQL 16.13, Python 3.11), corrected ledger
head: PROVEN 82/82 (27 + 10 + 5 + 26 + 12 proofs, 2 runner/fixture
regressions), 0 skipped, 122 s, 0 databases left behind. The root suite with
the service reachable and no step variables gave 6798 passed, 122 skipped,
7 xfailed, exit 0; the four additional skips are the four new PostgreSQL
completion-boundary and identity proofs skipping without the step variables.

Recorded on 2026-09-19 (PostgreSQL 16.13, Python 3.11), final bounded
completion correction head: PROVEN 86/86 (27 + 10 + 5 + 30 + 12 proofs, 2
runner/fixture regressions), 0 skipped, 136 s, 0 databases left behind. The
four additional ledger proofs are the partial-schema fail-closed cases in
both directions, the standalone-0108 positive control and the
reservation/completion race in both lock orders.

Recorded on 2026-09-19 (PostgreSQL 16.13, Python 3.11), agent loop head:
PROVEN 105/105 (27 + 10 + 5 + 30 + 12 + 19 proofs, 2 runner/fixture
regressions), 0 skipped, 128 s, 0 databases left behind. The nineteen added
proofs are the dormant agent loop core; they call no model and send nothing.

Recorded on 2026-09-19 (PostgreSQL 16.13, Python 3.11), corrected agent loop
head: PROVEN 119/119 (27 + 10 + 5 + 30 + 12 + 33 proofs, 2 runner/fixture
regressions), 0 skipped. The agent loop suite grew from 19 to 33 with the
durable-accounting, enforced-wait, scope, validation, isolation and
ownership-loss regressions of the review corrections.

Recorded on 2026-09-19 (PostgreSQL 16.13, Python 3.11), repeat-policy head:
PROVEN 125/125 (27 + 10 + 5 + 30 + 12 + 39 proofs, 2 runner/fixture
regressions), 0 skipped. The agent loop suite grew from 33 to 39 with the
bundle-duplicate, allowance-lifecycle, crash-after-debit and concurrent
allowance regressions.
