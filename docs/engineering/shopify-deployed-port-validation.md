# Shopify deployed-compatible port — validation evidence (Draft PR #1209)

Status: **dormant port, source frozen, validation terminal.** This document
records what was run against the port of the reviewed Shopify stack onto the
exact deployed ops base, with the results as observed. It is not a merge or
deployment proposal: the known `main` versus deployed-catalog divergence remains
a release blocker for the Shopify stack, no live schema, preflight or activation
step was performed, and every Shopify flag defaults OFF.

Machine-readable companion (sanitized subset of the retained artifacts):
[`shopify-deployed-port-validation-evidence.json`](shopify-deployed-port-validation-evidence.json).
Staging runbook (not executed):
[`shopify-deployed-port-staging-runbook.md`](shopify-deployed-port-staging-runbook.md).

## 1. Sources

| Item | Value |
|---|---|
| Target base (`ops/tenant1-haiku-20260921-f9021b59`, unchanged during the slice) | [`462ede1aff60`](https://github.com/alayedharthi-stack/nahla-saas/commit/462ede1aff60) |
| `main` at port time (the reviewed stack's base) | [`b7d2d6811911`](https://github.com/alayedharthi-stack/nahla-saas/commit/b7d2d6811911) |
| **Tested source** (the acceptance chain's checkout) | [`d199035ca163`](https://github.com/alayedharthi-stack/nahla-saas/commit/d199035ca163) |
| **Final published source** (PR head; parent accepted the dormant source here) | [`a246580519f9`](https://github.com/alayedharthi-stack/nahla-saas/commit/a246580519f9) |

Commit chain on the PR branch (three logical commits, 68 files added, 29
modified, 0 deleted against the base):

| Commit | Content | Original (tested) SHA | Rebuilt (published) SHA |
|---|---|---|---|
| 1 | connection foundation + merchant UI (29 added, 26 shared paths) | `47817b46d032` | `47817b46d032` (unchanged) |
| 2 | catalog mirror + privacy webhooks + migrations 0122/0123 + proof entries | `4ff69a9d397d` | [`6a4453c3ff04`](https://github.com/alayedharthi-stack/nahla-saas/commit/6a4453c3ff04) |
| 3 | accepted test-isolation repair on the deployed test files | `d199035ca163` | [`a246580519f9`](https://github.com/alayedharthi-stack/nahla-saas/commit/a246580519f9) |

### 1.1 Tested versus published source

The GitHub secret scan flagged one dictionary literal in
`scripts/audit_shopify_privacy_readonly.py` of the tested commit 2 (a
`generic-api-key` false positive on two adjacent keys). The fix is a
whitespace-only reformat of that literal (one entry per line, same keys, same
order); commits 2 and 3 were rebuilt on it in an isolated worktree and the
branch was force-pushed with lease against `d199035ca163`. Verified in the
evidence JSON (`source_mapping`):

* `git diff d199035ca163 a246580519f9` names exactly one path, with 8
  insertions and 3 deletions in that script; every other blob is identical.
* The script's AST is identical, and its compiled bytecode is identical when
  line tables are ignored.
* Commit 1 is byte-identical in both histories.

Runs made on the tested source are reported as such below and are **not**
relabelled as runs of the published head. The published head was separately
re-checked (§6, §7, §8, §9). The tested SHAs stay recoverable from this
document and the PR body.

## 2. What was ported and how shared paths were handled

* Reused, unchanged in behaviour: the reviewed default-off namespaces of
  #1202 (foundation), #1203 (UI), #1204 (catalog import) and #1205 (privacy
  webhooks), plus the accepted test-isolation repair of #1207.
* Shared paths (26, touched by the stack and present on the base): 15 were
  byte-identical between `main` and the base and took the reviewed hunks
  as-is; 11 differed and received Shopify-only hunks on the deployed content
  (`backend/main.py`, `dashboard/package.json`, the proofs manifest, the
  proofs runner regression test, `docs/engineering/required-postgres-proofs.md`,
  `scripts/operators/bootstrap_migration_contract.py` and the dashboard
  route/metadata/i18n files). No `main`-only slice was imported: the
  `catalog_review_environment_guard` proof suite and the dashboard
  `check:review-env-api-base` script are absent by design.
* The deployed `backend/tests/test_meta_catalog_consent.py` (blob
  `c45ae6df…`) was kept; only the `world` fixture hunks of the repair were
  applied. Assert statements 197 → 197, test functions 91 → 91, deployed assert
  sequence preserved. `test_log_redaction_meta_credentials.py` 42 → 42
  asserts, 16 → 16 tests. `test_salla_embedded_reconcile_telemetry.py` 5 → 7
  asserts, 3 → 4 tests (the repair's canary test). The two originally failing
  target files are unchanged against the base.
* The 29 modified paths are the 26 shared stack paths plus the three test
  files of the isolation repair (12 of the 29 differ between `main` and the
  base, 17 are identical).
* Unchanged against the base: `backend/core/auth.py`, `backend/core/config.py`,
  `database/models.py`, migration `0120`.
* Required-PostgreSQL manifest: the 25 deployed suite objects are unchanged
  and three suites were appended (`shopify_connection_foundation` 73 ids,
  `shopify_catalog_import` 55, `shopify_privacy_webhooks` 60): 611 → 799
  required ids, 25 → 28 suites. The runner regression test pins the target's
  inventory, not `main`'s.

## 3. Environment

* Python 3.12.3, pytest 9.1.1 in a dedicated virtual environment; dashboard
  dependencies from the repository lockfile (`npm ci`) in both checkouts.
* Disposable PostgreSQL 16.15 (Ubuntu package) on `127.0.0.1:5433`, SCRAM
  authentication over loopback TCP, a synthetic CI role and password that
  exist nowhere else. The container rebooted at 11:34:15Z; the server was
  restarted from its data directory at 11:51:54Z (crash recovery, SCRAM
  re-verified; evidence lines in the JSON). The postmaster PID was recorded
  before and after every PostgreSQL stage and never changed.
* Candidate and base ran on their own clean checkouts (`HEAD` and
  `git status --porcelain` checked before every stage, generated acceptance
  JSON and `dashboard/dist` restored afterwards). All Shopify and
  strict-PostgreSQL variables were unset for non-strict stages.

## 4. Acceptance chain on the tested source `d199035ca163` versus base `462ede1aff60`

| Stage | Checkout | Started (UTC) | Finished (UTC) | Result |
|---|---|---|---|---|
| dashboard: tsc, build, platform-policy, two Shopify UI checks, two browser harnesses | `d199035ca163` | 2026-10-11T11:52:07Z | 2026-10-11T11:59:42Z | rc 0: tsc rc 0, build rc 0, platform-policy rc 0, connection-ui rc 0, catalog-import-ui rc 0, harness-connection rc 0, harness-catalog rc 0 |
| dashboard: tsc, build, platform-policy | `462ede1aff60` | 2026-10-11T11:59:42Z | 2026-10-11T12:01:46Z | rc 0: tsc rc 0, build rc 0, platform-policy rc 0 |
| DB-free Shopify suites + bootstrap target + isolation helper + proofs-runner regression | `d199035ca163` | 2026-10-11T12:01:46Z | 2026-10-11T12:03:24Z | rc 0: 296 passed, 1 warning in 96.18s (0:01:36) |
| no-silent-except lint | `d199035ca163` | 2026-10-11T12:03:24Z | 2026-10-11T12:03:38Z | rc 0: OK (624 pre-existing, all baselined) |
| GOV-002 scanner from the exact base, trusted-base mode | `d199035ca163` | 2026-10-11T12:03:39Z | 2026-10-11T12:03:39Z | rc 0: `FINDINGS=none`, all seven flags `NO` |
| constitution compliance + non-interference guard | `d199035ca163` | 2026-10-11T12:03:39Z | 2026-10-11T12:06:15Z | rc 0: 98 passed in 153.53s (0:02:33) |
| owner 65-file regression gate (JUnit) | `d199035ca163` | 2026-10-11T12:06:15Z | 2026-10-11T12:14:44Z | rc 0: 1083 passed, 41 skipped, 2096 warnings in 499.02s (0:08:19) |
| owner 65-file regression gate (JUnit) | `462ede1aff60` | 2026-10-11T12:14:44Z | 2026-10-11T12:23:27Z | **rc 1**: 2 failed, 1080 passed, 41 skipped, 2096 warnings in 512.53s (0:08:32) |
| strict required-PostgreSQL proofs | `d199035ca163` | 2026-10-11T12:23:27Z | 2026-10-11T12:44:01Z | rc 0: REQUIRED POSTGRES PROOFS: PROVEN (799/799 required tests passed: 797/797 proofs + 2/2 runner/fixture regressions, 0 skips tolerated) |
| strict required-PostgreSQL proofs | `462ede1aff60` | 2026-10-11T12:44:01Z | 2026-10-11T13:00:49Z | rc 0: REQUIRED POSTGRES PROOFS: PROVEN (611/611 required tests passed: 609/609 proofs + 2/2 runner/fixture regressions, 0 skips tolerated) |
| deployed CI non-PostgreSQL steps (17) | `d199035ca163` | 2026-10-11T13:00:49Z | — | rc 0: 17/17 steps rc 0 |
| deployed CI non-PostgreSQL steps (17) | `462ede1aff60` | 2026-10-11T13:01:04Z | — | rc 0: 17/17 steps rc 0 |

PostgreSQL postmaster unchanged across every PostgreSQL stage: **yes**. Every stage ended with `HEAD` unchanged and a clean tree (`porcelain=0`).

### 4.1 Owner 65-file regression gate (JUnit, candidate versus base)

| | Base `462ede1aff60` | Candidate `d199035ca163` |
|---|---|---|
| JUnit cases | 1123 | 1124 |
| passed / failed / error / skipped | 1080 / 2 / 0 / 41 | 1083 / 0 / 0 / 41 |
| sorted-case digest (sha256) | `89be64721c234294…` | `eb8ad98a27f974a3…` |
| sorted case+outcome digest (sha256) | `348222fe90ba3cc2…` | `4b82ac8530ec5755…` |

* Cases only in the base: 0; only in the candidate: 1 (`backend.tests.test_salla_embedded_reconcile_telemetry::test_requests_here_run_under_the_app_log_redaction`).
* Outcome changes on shared cases: 2 — `test_rejection_emits_structured_log` failure → passed; `test_building_the_sqlite_schema_leaves_jsonb_intact_for_postgresql` failure → passed.
* Skipped identities equal: True; skip message attributes equal: True; the 41 skip rows (raw JUnit message attribute, collection-time module skips parsed from the element text) are listed in the JSON.
* Failed cases: base 2, candidate 0.

| Side | Case | Error |
|---|---|---|
| gate_base | `backend.tests.test_salla_embedded_app_identity::test_rejection_emits_structured_log` | `E       assert False` |
| gate_base | `backend.tests.test_salla_shipment_tracking::test_building_the_sqlite_schema_leaves_jsonb_intact_for_postgresql` | `E       AssertionError: assert False` |
* Full digests are the sha256 of the sorted `classname\tname` lines (and `\toutcome`), each line followed by LF; file list digest `ff606274c0bb84b5…` over the 65 paths in order, all present at the base.

### 4.2 Strict required-PostgreSQL proofs

| | Base `462ede1aff60` | Candidate `d199035ca163` |
|---|---|---|
| verdict | PROVEN | PROVEN |
| required ids | 611 | 799 |
| passed ids | 611 | 799 |
| suites proven / total | 25 / 25 | 28 / 28 |
| final line | `REQUIRED POSTGRES PROOFS: PROVEN (611/611 required tests passed: 609/609 proofs + 2/2 runner/fixture regressions, 0 skips tolerated)` | `REQUIRED POSTGRES PROOFS: PROVEN (799/799 required tests passed: 797/797 proofs + 2/2 runner/fixture regressions, 0 skips tolerated)` |

* Manifest: 25 deployed suite objects unchanged, non-suite values equal, 3 appended suites `shopify_connection_foundation` (73 ids), `shopify_catalog_import` (55 ids), `shopify_privacy_webhooks` (60 ids); required ids 611 → 799.
* Deployed suites with different (collected, passed, skipped, failed, errors, exit code) between base and candidate: 0.
* Unproven suites: none.
* Per-suite rows (id, kind, required, collected, passed, skipped, failed, errors, pytest_exit_code, proven) are in the JSON.

### 4.3 Deployed CI non-PostgreSQL steps (17, verbatim `run:` blocks of `lint-and-test`)

| # | Step | Base `462ede1aff60` | Candidate `d199035ca163` |
|---|---|---|---|
| 0 | Lint — no new silent excepts | rc 0 | rc 0 |
| 1 | Run unit tests | rc 0 | rc 0 |
| 2 | WhatsApp OAuth callback auth boundary | rc 0 | rc 0 |
| 3 | Canonical sibling identity unit tests | rc 0 | rc 0 |
| 4 | Manual gift grant permanence tests | rc 0 | rc 0 |
| 5 | Orders list newest-first tests | rc 0 | rc 0 |
| 6 | Salla broad browse ownership tests | rc 0 | rc 0 |
| 7 | Salla presented identity product card tests | rc 0 | rc 0 |
| 8 | Salla compose trusted singleton handoff tests | rc 0 | rc 0 |
| 9 | Salla PR 888 remediation unit tests | rc 0 | rc 0 |
| 10 | Customer conditional coupon Layer 0 consumer tests | rc 0 | rc 0 |
| 11 | Coupon shadow observation probe tests | rc 0 | rc 0 |
| 12 | Conditional coupon consumer verify operator tests | rc 0 | rc 0 |
| 13 | Real-channel conversational acceptance probe tests | rc 0 | rc 0 |
| 14 | Guarded staging migration operator tests | rc 0 | rc 0 |
| 15 | Tenant merchant clone operator tests | rc 0 | rc 0 |
| 16 | Staging DR canonical parity unit tests | rc 0 | rc 0 |

Last output line of each step is in the JSON. Overall: base rc 0, candidate rc 0. Run with the virtual environment first on PATH (the deployed job uses setup-python 3.12 with the requirements installed), strict-PostgreSQL and Shopify variables unset; candidate started 2026-10-11T13:02:26Z, base started 2026-10-11T13:24:38Z.

**Invalid first attempt (kept, not counted):** both stages first ran at 2026-10-11T13:01:04Z / 2026-10-11T13:01:18Z with a scratch-harness defect: the step harness ran the deployed run: blocks without the virtual environment on PATH, so python resolved to the system interpreter (3.13) without pytest; no test executed. 16 of 17 steps per side exited immediately with `/usr/bin/python: No module named pytest`; only `Lint — no new silent excepts` ran. These are unexecuted commands, not test failures. The attempt is kept under `invalid_first_attempt` in the JSON; both stages were re-run with the virtual environment on PATH and no other change, and the chain's final completion line (13:45:49Z) follows both corrected stage ends.

Root-suite step 1 output: base `9583 passed, 790 skipped, 7 xfailed, 45657 warnings in 1100.71s (0:18:20)`; candidate `9890 passed, 790 skipped, 7 xfailed, 45657 warnings in 1165.52s (0:19:25)` (the candidate collects the DB-free Shopify suites and the repair's added tests; skipped and xfailed counts are identical).

## 5. Three pre-existing failures, tested on the deployed target

These are **not** inherited from the `main`-based evidence of #1205/#1207.
Each node was run alone on the exact deployed base and on the exact final
head (DB-free, same interpreter), on 2026-10-11 at 12:10Z:

| Node | Base `462ede1aff60` | Final `a246580519f9` | Error |
|---|---|---|---|
| `backend/tests/test_log_redaction.py::test_graph_error_sanitized_no_token_in_message` | 1 failed | 1 failed | `ImportError: cannot import name '_sanitize_sync_error' from 'services.meta_catalog_sync_confirm'` |
| `backend/tests/test_log_redaction_meta_credentials.py::test_exact_token_refresh_path_logs_no_credential` | 1 failed | 1 failed | `AttributeError: module 'services.whatsapp_platform.token_manager' … does not have the attribute 'wa_provider'` |
| `backend/tests/test_log_redaction_meta_credentials.py::test_exact_token_refresh_exception_path_logs_no_credential` | 1 failed | 1 failed | same `AttributeError` |

On the deployed base and on the port alike, `services.meta_catalog_sync_confirm`
defines no `_sanitize_sync_error` (a function of that name lives in
`native_meta_sync_orchestrator.py`) and `token_manager` has no `wa_provider`
attribute. The port does not add, fix, skip or rename these tests; a runtime
API change is outside this slice. Neither file is in the owner gate list,
which is why the gate itself shows no failure.

## 6. Leakage reproductions bound to exact SHAs

Two-node reproductions of the baseline test-pollution defects that #1207
repaired, re-run on the exact final head and the exact deployed base at
12:12–12:13Z (DB-free):

| Reproduction (order as run) | Final `a246580519f9` | Base `462ede1aff60` |
|---|---|---|
| R1 telemetry file, then identity file `-k test_rejection_emits_structured_log` | 1 passed | **1 failed** (`test_rejection_emits_structured_log`) |
| R1d consent `test_unmarked_database_is_refused`, then `test_rejection_emits_structured_log` | 2 passed | 1 failed (target) |
| R2 consent `test_unmarked_database_is_refused`, then JSONB `…leaves_jsonb_intact_for_postgresql` | 2 passed | 1 failed (JSONB node) |
| R3a `test_exact_token_refresh_path_logs_no_credential`, then target | 1 failed (the §5 node), target passed | 2 failed (both) |
| R3b `test_uvicorn_access_log_redacts_inbound_query_secrets`, then target | 2 passed | 1 failed (target) |
| R3c `test_install_covers_root_handlers_and_named_loggers`, then target | 2 passed | 1 failed (target) |

The same reproductions were first run at 11:41Z on the uncommitted port
working tree before the commits were created (diagnostic only; recorded in the
JSON under `leakage_reproductions.pre_commit_smoke`, not bound to a SHA).

## 7. Final-head checks at `a246580519f9`

Re-run on a clean checkout of the published head on 2026-10-11:

| Check | Result |
|---|---|
| DB-free Shopify suites + bootstrap target + isolation helper + proofs-runner regression | 296 passed (12:03:55Z) |
| `scripts/lint_no_silent_except.py` | OK, 624 pre-existing, all baselined |
| GOV-002 scanner taken from the exact base (`462ede1aff60`), `--trusted-base-scanner` | `FINDINGS=none`; all seven flags `NO` |
| Constitution + non-interference guard | 98 passed (12:06:50Z) |
| Shopify audit scripts against the disposable PostgreSQL | 8 passed, 52 deselected |

## 8. GitHub checks on the published head

Check runs for `a246580519f9` as read from GitHub at 2026-10-11T12:35:30Z:

| Check | Status | Conclusion | Completed (UTC) |
|---|---|---|---|
| [Scan repository for leaked secrets](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613771/job/114464617651) | completed | success | 2026-10-11T12:00:24Z |
| [a1-postgres-integration](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613761/job/114464617742) | completed | success | 2026-10-11T12:04:33Z |
| [constitution-compliance](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613761/job/114464617475) | completed | success | 2026-10-11T12:02:31Z |
| [dashboard-platform-policy](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613761/job/114464617829) | completed | success | 2026-10-11T12:00:34Z |
| [gov002-trusted-base-scanner](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137612022/job/114464613544) | completed | success | 2026-10-11T12:00:23Z |
| [lint-and-test](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613761/job/114464617749) | completed | success | 2026-10-11T12:23:56Z |
| [postgres-18-compatibility](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613805/job/114464617534) | completed | success | 2026-10-11T12:10:27Z |
| [publish-merge-freeze-gate](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137612061/job/114464613778) | completed | success | 2026-10-11T12:00:18Z |
| [staging-dr-executor-artifact](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613761/job/114464617720) | completed | success | 2026-10-11T12:00:30Z |
| [trusted-context-layer1](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613761/job/114464617622) | completed | success | 2026-10-11T12:04:26Z |
| [whatsapp-catalog-sync-postgres](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613761/job/114464617795) | completed | success | 2026-10-11T12:01:17Z |

All 11 check runs completed with conclusion `success`: **yes**.

Lines of the [lint-and-test job log](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38137613761/job/114464617749) (job 114464617749, run 38137613761), **independently read by the coordinator through the authorized GitHub connector and relayed verbatim at 2026-10-11T12:39Z**:

```text
2026-10-11T12:12:40.5008968Z 9890 passed, 790 skipped, 7 xfailed, 45657 warnings in 683.90s (0:11:23)
2026-10-11T12:22:38.3108987Z REQUIRED POSTGRES PROOFS: PROVEN (799/799 required tests passed: 797/797 proofs + 2/2 runner/fixture regressions, 0 skips tolerated)
```

This session's own read: the signed log URL download was blocked: the job-log blob host is denied by the environment network policy (proxy CONNECT 403) and was not retried; the full log was then read through the authorized GitHub tool (done at 12:38Z, 3085 lines), and the same two lines were found verbatim (agreement with the coordinator's read: **True**). The job's server log line reads `starting PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`; all 28 per-suite lines carry `verdict=PROVEN`, including the three Shopify suites (73, 55 and 60 required, all passed, 0 skipped).

## 9. Session and selector

Coding assistant session `session_014dVKx9VnJZo2BVmixe6FTE`, created with
`claude-opus-5-5`, switched by the user to `claude-fable-5-1`, which served
every turn of this slice (session API at 12:11Z); effort **Extra**, fast mode
**off** (operator-stated). This selector has no relation to the Nahla runtime
model, prompts, persona or compose paths, none of which the port changes.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```

## 10. Constraints honoured, limits and blocked items

* No live database, schema, Alembic, preflight or activation step; no
  credential retrieval, OAuth grant, Shopify app configuration, deployment,
  merge, purchase or extra-usage enrollment. Only the PR branch was pushed
  (force-with-lease once, §1.1); `ops` and `main` were never pushed.
* Synthetic credentials only; no password, DSN or session-scratch path appears
  in the published evidence (asserted by the JSON builder).
* No test was weakened, skipped, renamed or excluded; the three §5 failures
  stay explicit.
* Known limits carried from the reviewed stack: the catalog import runs as an
  in-process FastAPI `BackgroundTask`; `inventory_mode=scope_missing` with
  null counts; privacy worker and destructive executor OFF with no approved
  policy; live staging schema state unverified.
* Blocked, invalid or failed runs, exactly: `gate_base` ended with rc 1 (the two baseline-only failures of §4.1, expected on the unrepaired base); the signed job-log download URL is blocked by the environment network policy (proxy 403), not retried; the job log was read through the authorized GitHub tool instead (§8); the first 17-step replay attempt on both sides is invalid as test evidence (scratch-harness PATH defect, no test executed) and was re-run with only that corrected (§4.3). Every planned stage otherwise reached a terminal result; the only test failures observed are the two baseline-only gate failures (§4.1) and the three pre-existing nodes of §5, on the base and on the port alike.
