# Baseline test isolation — validation evidence (Draft PR #1207)

Evidence-only record. **Tested source: `7ae59f4d8635efa84b50b14d0da379cc5b4ad928`**
(frozen). **Baseline: `08428cb7edf9666a0b31874e8f0256764fd11c90`** (the #1204 head
and this Draft's base). **Privacy source: `3681ebdb4f6f922e4f2f4bd415d8963c45ef3f6c`**
(the #1205 tested source). The commit that adds this file and its JSON
companion changes nothing else; its SHA is not the tested source. The
machine-readable subset is
[`baseline-test-isolation-validation-evidence.json`](baseline-test-isolation-validation-evidence.json)
(sanitized: repository-relative paths, no credentials, no host names, no
object addresses; derived from the retained artifacts without re-running
any test).

The #1205 validation record and its FAILED 65-file gate at `3681ebd` are
unchanged by this Draft.

## Verdict

| Gate (original ordered 65-file command, disposable PostgreSQL 16.15 up and stable) | Result |
|---|---|
| Baseline `08428cb` (rerun) | RC 1 — **FAILED**: 2 failed / 1075 passed / 41 skipped (1118 cases), identical identities, outcomes and skip reasons to the original baseline JUnit |
| Repair `7ae59f4` | RC 0 — **PASSED**: 1078 passed / 41 skipped (1119 cases) |
| Privacy source `3681ebd` + the three repair commits (temporary local head `3a1bcfa`, never pushed) | RC 0 — **PASSED**: 1078 passed / 41 skipped (1119 cases) |

Against the baseline rerun, the repair gate has the same 1118 case
identities plus one new case (the telemetry redaction canary); the only
outcome changes are the two isolation failures becoming PASS; the 41 skipped
identities and reasons are identical. The 37 PostgreSQL-backed cases that
skipped in the post-reboot diagnostic runs **executed and passed** in all
three gates.

## Scope of the change

`git diff --name-only 08428cb 7ae59f4` lists exactly five files, all under
`backend/tests/`:

- `backend/tests/_log_redaction_isolation.py` (new helper)
- `backend/tests/test_log_redaction_test_isolation.py` (new tests)
- `backend/tests/test_meta_catalog_consent.py`
- `backend/tests/test_salla_embedded_reconcile_telemetry.py`
- `backend/tests/test_log_redaction_meta_credentials.py`

Commits: `c4ecda0` (consent and telemetry), `3affff6` (consent cleanup made
exception-safe), `7ae59f4` (third installer). No production, Salla,
catalog, billing or agent file changed. The two originally failing test
files are byte-identical to the baseline.

**Assertion retention**, counted as `ast.Assert` statements in source
order (helper calls such as `_assert_no_canary(...)` are not assert
statements; an earlier figure of 54 for the credentials file counted lines
containing the word "assert"):

| File | Assert statements (baseline → head) | Test functions | Baseline sequence preserved |
|---|---|---|---|
| `test_meta_catalog_consent.py` | 191 → 191 | 90 → 90 | yes, identical |
| `test_log_redaction_meta_credentials.py` | 42 → 42 | 16 → 16 | yes, identical |
| `test_salla_embedded_reconcile_telemetry.py` | 5 → 7 | 3 → 4 | yes; one canary test with 2 asserts added |

No skip was added, no node excluded, no run order changed.

## Root causes and minimized reproductions

| Failure | Polluter | Mechanism |
|---|---|---|
| `backend/tests/test_salla_shipment_tracking.py::test_building_the_sqlite_schema_leaves_jsonb_intact_for_postgresql` | the `world` fixture in `test_meta_catalog_consent.py` | Its `before_create` listener rewrites the shared JSONB column objects of **every** table on `models.Base.metadata` to `JSON()`; it removes the listener afterwards but never restores the types. |
| `backend/tests/test_salla_embedded_app_identity.py::test_rejection_emits_structured_log` | three installers of `core.log_redaction.install_log_redaction()`: (a) the same `world` fixture at run time; (b) the module-level `from main import app` in `test_salla_embedded_reconcile_telemetry.py`, executed at collection; (c) `test_exact_token_refresh_path_logs_no_credential`, `test_uvicorn_access_log_redacts_inbound_query_secrets` and `test_install_covers_root_handlers_and_named_loggers` in `test_log_redaction_meta_credentials.py` | Each attaches the shared `SecretRedactingFilter` to the root handlers that exist at that moment — inside pytest, its session-long handlers — and leaves it there. The filter rewrites the shared `LogRecord` in place, so `caplog` later sees `rejected code=REDACTED` instead of `code=invalid_salla_app_id`. |

`models` and `database.models` are separate module objects (both
`database/` and the repository root are on `sys.path`); the billing-badge
listener registers on `database.models.Base.metadata`, which is why it did
not reproduce the JSONB failure in #1205's diagnostic D2.

Two-node reproductions, plain command `python -m pytest -q -p no:cacheprovider …`,
run in the final chain with PostgreSQL up:

| ID | Nodes | Baseline `08428cb` | Repair `7ae59f4` |
|---|---|---|---|
| R1 | telemetry file + embedded-identity file, `-k test_rejection_emits_structured_log` | RC 1 — target FAILED | RC 0 — 1 passed |
| R1d | `test_meta_catalog_consent.py::test_unmarked_database_is_refused`, then the structured-log target | RC 1 — target FAILED | RC 0 — 2 passed |
| R2 | the same consent test, then the JSONB target | RC 1 — target FAILED | RC 0 — 2 passed |
| R3a | `…meta_credentials.py::test_exact_token_refresh_path_logs_no_credential`, then the structured-log target | RC 1 — both FAILED | RC 1 — only the credentials test FAILED (its standalone failure, below); the target passed |
| R3b | `…meta_credentials.py::test_uvicorn_access_log_redacts_inbound_query_secrets`, then the target | RC 1 — target FAILED | RC 0 — 2 passed |
| R3c | `…meta_credentials.py::test_install_covers_root_handlers_and_named_loggers`, then the target | RC 1 — target FAILED | RC 0 — 2 passed |

Earlier controls at `08428cb` (not in the final chain): each target passes
alone; a non-installer neighbour before the structured-log target passes;
the JSONB target before the consent test passes.

## The change

- **`_log_redaction_isolation.py`** snapshots the filter lists of the root
  handlers and of the redacted library loggers
  (`DEFAULT_REDACTED_LOGGERS`) and afterwards removes **only** filters added
  since the snapshot. Pre-existing filters (including an earlier redaction
  filter), handlers and levels are kept. `test_log_redaction_test_isolation.py`
  proves those three properties.
- **Consent `world` fixture:** snapshots the JSONB column types and restores
  the exact type objects after `create_all`; runs its
  `install_log_redaction()`, the rest of its setup and the `yield` inside
  `restoring_log_filters()`, so its filters come off at teardown and also
  when setup fails after the install. Injected-failure check: at the repair
  head no handler keeps the filter; at the baseline both pytest session
  handlers keep it.
- **Telemetry module:** keeps `main`'s import-time filters off pytest's
  handlers; an autouse fixture installs the same redaction for each test
  and removes it afterwards; one canary test proves requests in the module
  run under redaction. The `client` fixture is unchanged
  (`TestClient(app)` without entering its context), so no application
  lifespan or startup runs.
- **`test_log_redaction_meta_credentials.py`:** each installer test runs
  its install, its whole action and every original assertion inside
  `restoring_log_filters()`; the exception-path test, which previously
  depended on the preceding test's leaked install, now installs explicitly
  in the same way. No autouse install, no global reset. Per-test outcomes of
  the file are unchanged (50 nodes, the same 2 failures as before).

**Caveat — unit observation versus app-level redaction.** The change
separates direct observation of a service's log line from the app-level
redaction that `backend/main.py` installs. Production behaviour is not
changed and not claimed fixed: with that redaction installed, the
application still logs this rejection as `code=REDACTED`. Whether the
structured code should survive redaction is a runtime and security
decision for separate review.

## Environment

- Python 3.12.3, pytest 9.1.1; the four PostgreSQL environment variables
  used by the reliability suites, `A1_PG_TEST_DATABASE_URL` and
  `DATABASE_URL` were all **unset** for every stage. PostgreSQL-backed
  cases reach the database through the test fixtures' default service URL
  on `127.0.0.1:5433` (`legacy_migration_drift_postgres_fixtures.DEFAULT_SERVICE_URL`).
- **Reboot.** The machine rebooted at 2026-10-11 09:49:30Z, which stopped
  the disposable clusters. The original #1205 gate runs (candidate `3681ebd`
  08:55:00 → 09:02:41Z, baseline `08428cb` 09:02:41 → 09:10:39Z) ran with
  the disposable PG16 up; 37 of their passing cases are PostgreSQL-backed.
- **Disposable PG16 restored** at 10:36Z with the originally recorded
  command: Ubuntu `postgresql-16 16.15-0ubuntu0.24.04.1` binaries, the
  existing data directory, stale `postmaster.pid` (PID 2350) confirmed not
  running before the start, automatic crash recovery from the 09:31:19Z
  checkpoint, same system identifier. Authentication: `host … 127.0.0.1/32
  scram-sha-256` (loopback only), `password_encryption=scram-sha-256`, the
  synthetic CI role's password stored as SCRAM-SHA-256, correct password
  connects over TCP, wrong password refused. No credential or DSN is
  recorded. No PostgreSQL 18 cluster was needed or started.
- The postmaster PID was checked before and after every stage and never
  changed.

## Gates (final chain, `7ae59f4`, started 10:41:50Z, finished 11:08:55Z)

Every stage's exact argv, working tree, stage order and the six unset
PostgreSQL / database variables are recorded in the JSON (`argv`, `cwd`,
`env_unset`, `stage_order`). Command for each gate, from the repository
root:

```bash
python -m pytest -q -p no:cacheprovider --junitxml=<out>/<label>_junit.xml $(cat owner_regression_files.txt)
```

`owner_regression_files.txt` is the original ordered 65-file list (sha256
`ff606274c0bb84b57bee3310306ff00f80057c12b1cbaa05bde959b8a06460e1`; the list
and the digest format are in the JSON).

| Label | Tree | Started → finished (UTC) | RC | Summary |
|---|---|---|---|---|
| `base_gate` | `08428cb`, clean | 10:41:51 → 10:49:21 | 1 | 2 failed, 1075 passed, 41 skipped (441.31s) |
| `fix_gate` | `7ae59f4`, clean | 10:49:21 → 10:56:50 | 0 | 1078 passed, 41 skipped (440.82s) |
| `compat_gate` | `3a1bcfa`, clean | 10:56:52 → 11:04:28 | 0 | 1078 passed, 41 skipped (447.07s) |
| `compat_privacy_dbfree` (`tests/test_shopify_privacy_webhooks.py`) | `3a1bcfa` | 11:04:29 → 11:04:45 | 0 | 130 passed |

Each gate rewrote the three tracked Salla acceptance JSON files; their diffs
were preserved outside the repository and the files restored with
`git checkout --`; every tree was clean afterwards.

**Temporary privacy+repair head `3a1bcfa3d2267b006b63ffea98a55ee32f405ead`**
(local worktree only; never pushed): parents `acd1d3e` ← `2949b3d` ←
`3681ebd`, i.e. the privacy source plus the three repair commits
cherry-picked in order (`c4ecda0`, `3affff6`, `7ae59f4`). `git diff
--name-only 3681ebd 3a1bcfa` lists exactly the five repair files; each of
their blob ids equals the blob at `7ae59f4`
(`158cfa71…`, `e19fbba0…`, `2318fb2d…`, `42a1dbf5…`, `62465548…`); the rest of
the tree is identical to `3681ebd` (`git ls-tree -r` comparison with those
five paths removed). The full blob ids, the tree digests and the
cherry-pick ancestry are in the JSON (`temporary_head_verification`), as
are the sorted-case and case-outcome digests of the five JUnit files and the
41 skipped identities with reasons (`junit_digests`, `skipped_41`).

### JUnit comparisons (case = JUnit `classname` + `name`)

| Comparison | Cases | Identities | Outcome changes | Skipped |
|---|---|---|---|---|
| original baseline JUnit (`08428cb`, 09:02:41 → 09:10:39Z) vs `base_gate` | 1118 → 1118 | same set | none | identical identities and reasons |
| `base_gate` vs `fix_gate` | 1118 → 1119 | +1: `test_salla_embedded_reconcile_telemetry::test_requests_here_run_under_the_app_log_redaction` | the two isolation failures → passed | identical identities and reasons |
| original #1205 candidate JUnit (`3681ebd`, 08:55:00 → 09:02:41Z) vs `compat_gate` | 1118 → 1119 | +1, the same canary | the two isolation failures → passed | identical identities and reasons |
| `fix_gate` vs `compat_gate` | 1119 → 1119 | same set | none | identical |

The 37 cases that skipped as PostgreSQL-unavailable in the post-reboot
diagnostic runs: 37 passed in `base_gate`, 37 in `fix_gate`, 37 in
`compat_gate`.

## Focused coverage

Files: the four log-redaction test files, the consent, telemetry,
embedded-identity and shipment-tracking files; at the head also
`test_log_redaction_test_isolation.py`.

| Tree | RC | Summary |
|---|---|---|
| `7ae59f4` | 1 | 3 failed, 253 passed |
| `08428cb` | 1 | 5 failed, 247 passed (the 3 below plus the two isolation targets) |

The three remaining failures are **retained as FAILED**. Each fails alone:
controls at `08428cb` and at `3affff6` (≈10:38Z, before `7ae59f4` existed,
each `1 failed`), and a bounded verification at a detached checkout of
exactly `7ae59f4` (11:15:32 → 11:15:38Z, `python -m pytest -q -p no:cacheprovider <node>`
per node, each RC 1, `1 failed`, the same error lines as in the table). So
none is leakage; each import resolves to the real module file and the
referenced name is absent from it:

| Node id | Error | Where the name is actually defined |
|---|---|---|
| `backend/tests/test_log_redaction.py::test_graph_error_sanitized_no_token_in_message` | `ImportError: cannot import name '_sanitize_sync_error' from 'services.meta_catalog_sync_confirm'` (`backend/services/meta_catalog_sync_confirm.py`), at `backend/tests/test_log_redaction.py:53` | `backend/services/native_meta_sync_orchestrator.py:144` |
| `backend/tests/test_log_redaction_meta_credentials.py::test_exact_token_refresh_path_logs_no_credential` | `AttributeError: <module 'services.whatsapp_platform.token_manager' from '…/backend/services/whatsapp_platform/token_manager.py'> does not have the attribute 'wa_provider'`, raised by `unittest.mock.patch.object` | `backend/services/whatsapp_platform/provider_utils.py:53` |
| `backend/tests/test_log_redaction_meta_credentials.py::test_exact_token_refresh_exception_path_logs_no_credential` | the same `AttributeError` | same |

The clone is shallow, so the commit at which each name diverged cannot be
established here. No assertion was weakened and no runtime API was changed.

## Governance (at `7ae59f4`)

| Check | Result |
|---|---|
| `python scripts/lint_no_silent_except.py` | RC 0 — OK (624 pre-existing, all baselined) |
| GOV-002, scanner taken from `08428cb`, `--base 08428cb --head 7ae59f4 --trusted-base-scanner` | RC 0 — `FINDINGS=none`; MODEL / PROMPT / PERSONA / PHRASE_MAP / KEYWORD_ROUTER / CUSTOMER_REGEX changed = NO |
| `python -m pytest backend/tests/test_constitution_compliance.py backend/tests/test_intelligence_non_interference_guard.py -q -p no:cacheprovider` | RC 0 — 98 passed |

GitHub: only `gov002-trusted-base-scanner` and `publish-merge-freeze-gate`
run for this stacked Draft; the local results above are not GitHub check
results.

## Superseded runs (not acceptance)

Between the reboot and the restore there was no PostgreSQL:

- baseline `08428cb`, instrumented probe configuration: 2 failed / 1038
  passed / 78 skipped; a `-rs` readback showed the 37 extra skips are all
  PostgreSQL-unavailable (31 legacy-migration-drift "connection refused", 4
  "PostgreSQL DSN is not configured", 2 "PostgreSQL not available") with no
  other outcome change;
- `3affff6`, plain command: 1041 passed / 78 skipped;
- the queued no-PostgreSQL duplicate gates were cancelled before they
  started; no active test was interrupted.

## Not changed / open

- Production code, including `core/log_redaction.py` and
  `backend/main.py`, is unchanged; the app-level `code=REDACTED` behaviour
  stands and is not assessed here.
- The three standalone focused failures above remain FAILED.
- Other test modules that import `main` or call `install_log_redaction()`
  outside the 65-file gate and the focused set were not changed.
- No merge or deployment is proposed; the `main` versus deployed-catalog
  divergence remains a release blocker for the Shopify stack.
