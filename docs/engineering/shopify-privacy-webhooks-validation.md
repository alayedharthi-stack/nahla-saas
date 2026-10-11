# Shopify privacy compliance webhooks — validation evidence (Draft PR #1205)

Evidence-only record. **Tested source: `3681ebdb4f6f922e4f2f4bd415d8963c45ef3f6c`.**
**Baseline: `08428cb7edf9666a0b31874e8f0256764fd11c90`** (the #1204 head this
Draft is stacked on). The commit that adds this file changes nothing else;
its SHA is not the tested source. Nothing here was deployed, activated,
registered with Shopify or run against a live database, a live Shopify store
or real customer data. All three flags remain off.

**Machine-readable evidence:**
[`shopify-privacy-webhooks-validation-evidence.json`](shopify-privacy-webhooks-validation-evidence.json)
is a compact, sanitized subset derived from the retained raw artifacts (raw
logs and JUnit XML stayed in the session scratchpad and are not committed).
It holds the ordered 65-file list and its digest, the gate command and
environment switches, candidate and baseline counts / exits / timings, the
two failures (pytest node ids, JUnit identities, source lines, assertions and
reported values), all 41 skipped identities (5 collection-time), JUnit case
counts with reproducible sorted-case and case-outcome digests (format defined
in the file), D1a–D4 commands and outcomes, the root inventory and collection
skip, strict proof suites, CI steps and governance results. Sanitization:
absolute checkout and scratch paths made repository-relative or removed,
Python object addresses replaced by `<addr>`, host name and credentials
omitted; nothing else rewritten, and candidate and baseline values are kept
separate. It was built and then cross-checked by a second, independent
script against the retained files (167 checks, 0 failures), without
re-running any test.

## Verdict summary

| Gate | Candidate `3681ebd` | Baseline `08428cb` |
|---|---|---|
| Strict PostgreSQL proofs, PG16.15, SCRAM | **PROVEN 815/815** (813/813 proofs + 2/2 runner regressions), 29/29 suites, 0 skips, RC 0 | not applicable (baseline manifest has 755) |
| Strict PostgreSQL proofs, PG18.6, SCRAM | **PROVEN 815/815**, 29/29 suites, 0 skips, RC 0 | not applicable |
| `lint-and-test` non-PostgreSQL steps (17) | all 17 RC 0 | all 17 RC 0 |
| **Owner 65-file regression set** | **FAILED** — RC 1: 2 failed, 1075 passed, 41 skipped | **FAILED** — RC 1: 2 failed, 1075 passed, 41 skipped |
| GOV-002 (trusted base scanner) | RC 0, `FINDINGS=none`, every change flag `NO` | — |
| Constitution compliance | RC 0, 98 passed | — |

The owner 65-file regression gate is **FAILED**. The same two tests fail
with the same assertions on the baseline, the JUnit node identities and
outcomes are identical at both SHAs, and none of the 65 files differs
between the SHAs. This is recorded as **reproduced pre-existing baseline
debt**, not as a pass. Isolated diagnostic successes below do not replace
the FAILED gate, and no node was excluded, no assertion changed and no
failure suppressed.

## Environment

* Python 3.12.3 virtualenv (`requirements.txt`), pytest 9.1.1.
* Candidate tree: the repository checkout at `3681ebd`. Baseline tree: a
  separate `git worktree` at `08428cb`. Both trees had an empty
  `git status --porcelain` before every recorded run (captured as
  `status.txt` / `status_before.txt` in each evidence directory).
* PostgreSQL 16.15 (Ubuntu package `16.15-0ubuntu0.24.04.1`), disposable
  cluster on `127.0.0.1:5433`.
* PostgreSQL 18.6 from Maven Central
  `io.zonky.test.postgres:embedded-postgres-binaries-linux-amd64:18.6.0`
  (fetched from the Google Cloud Maven Central mirror after a 429 from
  `repo1.maven.org`; SHA-1 `0a36625fa44dc0d65cb1cee6041e468d96bdbb8d`
  verified), disposable cluster on `127.0.0.1:5434`.
* Both clusters: `host all all 127.0.0.1/32 scram-sha-256` (TCP SCRAM;
  local socket trust is not used by the runs), role `nahla` /
  `nahla_password` — the synthetic credentials of CI's disposable
  PostgreSQL service. No live database or credential is involved.
* Network: this environment cannot reach `shopify.dev` (WebFetch
  `ENOTFOUND`; `curl` through the proxy: `CONNECT 403`). Shopify facts used
  by the design are parent-verified references; the limitation was not
  bypassed.

Evidence directories below are in the session scratchpad
(`$SP` = the session scratchpad directory); they are not committed.

## 1. Strict PostgreSQL proofs

Executed by a local wrapper (`run_strict.sh <port> <label>`) from the
repository root:

```bash
export NAHLA_RELIABILITY_REQUIRE_PG=1
export NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://nahla:nahla_password@127.0.0.1:${PORT}/postgres
export CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1
export LEGACY_MIG_PG_TEST_DATABASE_URL=postgresql://nahla:nahla_password@127.0.0.1:${PORT}/nahla_saas
PATH=$SP/venv312/bin:$PATH python scripts/required_postgres_proofs.py \
  --manifest scripts/required_postgres_proofs.json \
  --junit-dir "$OUT/junit" --report "$OUT/report.json"
```

| Run | Port / server | Started → finished (UTC) | HEAD / tree | RC | Result |
|---|---|---|---|---|---|
| `final_pg16_3681ebd` | 5433 / `server_version_num=160015` | 2026-10-11 07:37:11 → 07:56:55 | `3681ebd` / clean | 0 | `REQUIRED POSTGRES PROOFS: PROVEN (815/815 required tests passed: 813/813 proofs + 2/2 runner/fixture regressions, 0 skips tolerated)` |
| `final_pg18_3681ebd` | 5434 / `server_version_num=180006` | 2026-10-11 07:57:00 → 08:15:37 | `3681ebd` / clean | 0 | same line, PROVEN 815/815 |

Per-suite: every one of the 29 suites `verdict=PROVEN` with
`skipped=0 failed=0 errors=0`; the new `shopify_privacy_webhooks` suite
`required=60 collected=60 passed=60`; `shopify_connection_foundation`
73/73 and `shopify_catalog_import` 55/55 unchanged and passing.

Manifest: `scripts/required_postgres_proofs.json` was compared as **parsed
JSON objects**, not byte for byte. At `3681ebd` the first 28 suite objects
are equal, in order, to the baseline's 28 suite objects at `08428cb` (every
field: `id`, `kind`, `module`, `origin`, `required_env` and the 755 node
ids); the non-suite values `manifest_version`, `title` and `rules` are
equal; one suite object with 60 node ids is appended — 815 in 29 suites.

**Superseded attempt (not evidence).** An earlier strict PG16 run started
at `6c3aba0` with a clean tree (`hist_6c3aba0_pg16_STOPPED_mixed_source`)
was **STOPPED** manually after 7 of 29 suites (each of those 7 reported
pytest exit code 0). The working tree was edited while it ran, so it is
mixed-source. The runner never completed: it wrote **no final runner
verdict** (its `report.json` is still the runner's `NOT RUN` /
`validation_not_started` placeholder) and no runner exit status was
recorded. It is not counted.

**Historical pre-freeze failures (superseded; not acceptance evidence).**
Development runs before the source was frozen, recovered from the retained
session transcript (their output was not kept as separate log files). Each
ran on an **uncommitted working tree**; the branch HEAD at the time is
inferred from commit timestamps, and no commit SHA identifies the tested
tree.

| Time (UTC) | Branch HEAD then | Command | Result | Resolution before freeze |
|---|---|---|---|---|
| 06:56:17 | `2f6a351` (+ uncommitted edits) | `python -m pytest -q tests/test_shopify_privacy_webhooks.py` | 1 failed, 128 passed (14.19s): `test_delayed_inspection_is_bounded_and_never_blocks_the_event_loop` | router re-checks the deadline before the receipt SQL; committed in `5a963d8` |
| 07:05:57 | `5a963d8` (+ uncommitted edits) | PG16, `python -m pytest -q -x backend/tests/test_shopify_privacy_webhooks_pg.py -p no:cacheprovider` | 1 failed, 8 passed (42.92s; `-x` stopped at the first failure): `test_composite_key_isolates_tenants_and_checks_refuse_fabricated_states` | malformed test insert replaced by a bounded-counter (`attempts = -1`) check; present in `6c3aba0` |
| 07:08:17 | `5a963d8` (+ uncommitted edits) | PG16, same without `-x` | 5 failed, 54 passed (128.28s): `test_any_legacy_shopify_footprint_blocks_the_no_data_outcome[external_customer_profiles]`, `test_connect_committing_between_unknown_evaluation_and_write_is_fenced`, `test_confirmed_uninstall_yields_a_counts_only_blocked_proposal`, `test_reinstall_racing_the_executor_waits_and_builds_a_new_generation`, `test_audit_script_never_prints_stored_strings_of_malformed_tables` | fixed before the freeze at `3681ebd` (the commit in which each fix landed is not separately recorded); all pass in the final runs above |

The transcript also shows two earlier transient DB-free failures
(06:48:18, 1 failed / 75 passed; 06:55:54, 1 failed / 113 passed), fixed
before `5a963d8`. None of these runs is acceptance evidence.

## 2. `lint-and-test` non-PostgreSQL steps (candidate vs baseline)

Each step's `run:` block from `.github/workflows/ci.yml`, verbatim, under
`bash -o pipefail -c`, with the PostgreSQL environment variables unset. 17
steps (index 00–16):

| # | Step | Candidate `3681ebd` | Baseline `08428cb` |
|---|---|---|---|
| 00 | Lint — no new silent excepts | RC 0 — OK (624 pre-existing, all baselined) | RC 0 — same |
| 01 | Run unit tests (`python -m pytest -q --maxfail=1`) | RC 0 — 9669 passed, 790 skipped, 7 xfailed (1035.14s) | RC 0 — 9529 passed, 790 skipped, 7 xfailed (1012.07s) |
| 02 | WhatsApp OAuth callback auth boundary | RC 0 — junit guard clean, 16 tests, 0 skipped | RC 0 — same |
| 03 | Canonical sibling identity | RC 0 — 94 passed | RC 0 — 94 passed |
| 04 | Manual gift grant permanence | RC 0 — 36 passed | RC 0 — 36 passed |
| 05 | Orders list newest-first | RC 0 — 15 passed | RC 0 — 15 passed |
| 06 | Salla broad browse ownership | RC 0 — 41 passed | RC 0 — 41 passed |
| 07 | Salla presented identity product card | RC 0 — 75 passed | RC 0 — 75 passed |
| 08 | Salla compose trusted singleton handoff | RC 0 — 85 passed | RC 0 — 85 passed |
| 09 | Salla PR 888 remediation | RC 0 — 69 passed | RC 0 — 69 passed |
| 10 | Customer conditional coupon Layer 0 consumer | RC 0 — 156 passed | RC 0 — 156 passed |
| 11 | Coupon shadow observation probe | RC 0 — 10 passed | RC 0 — 10 passed |
| 12 | Conditional coupon consumer verify operator | RC 0 — 29 passed | RC 0 — 29 passed |
| 13 | Real-channel conversational acceptance probe | RC 0 — 59 passed | RC 0 — 59 passed |
| 14 | Guarded staging migration operators (4 commands) | RC 0 — 39 / 37 / 49 / 16 passed | RC 0 — 39 / 37 / 49 / 16 passed |
| 15 | Tenant merchant clone operator | RC 0 — 112 passed, 1 skipped | RC 0 — 112 passed, 1 skipped |
| 16 | Staging DR canonical parity (2 commands) | RC 0 — 17 / 7 passed | RC 0 — 17 / 7 passed |

Step 15's skip: the step ran `-q` without `-rs`, so its log has the count
only. The file's only skip marker is the `skipif` on
`backend/tests/test_tenant_merchant_clone_operator.py::test_pg_preserve_tenant_33_bootstrap_cleanup_and_shell_guards`,
reason `tenant clone PostgreSQL source/target databases unavailable`; its
condition holds because `TENANT_CLONE_PG_SOURCE_DATABASE_URL` /
`TENANT_CLONE_PG_TARGET_DATABASE_URL` were unset (file blob `a6807830`
identical at both SHAs).

### Root suite: execution reports versus collected node ids

The root counts are recorded separately, not normalized together:

| | Candidate `3681ebd` | Baseline `08428cb` |
|---|---|---|
| Execution reports (step 01) | 10466 = 9669 passed + 790 skipped + 7 xfailed | 10326 = 9529 passed + 790 skipped + 7 xfailed |
| Collected node ids (`--collect-only -q`) | 10465 | 10325 |
| Collection-time skip reports | 1 | 1 |

The one extra report at each SHA is a **collection-time module skip** that
has no node id. Saved step-01 logs ran `-q` without `-rs`, so the reason was
absent; it was read back with a collect-only run (no test executed) on each
unchanged SHA:

```bash
python -m pytest --collect-only -q -rs -p no:cacheprovider
```

| SHA | Started (UTC) | RC | Tree before / after | Node ids | Collection skip |
|---|---|---|---|---|---|
| `3681ebd` | 2026-10-11 09:32:05 | 0 | clean / clean | 10465 | `SKIPPED [1] tests/tenant_isolation_test.py:26: Live E2E tenant-isolation script — set NAHLA_LIVE_E2E=1 (and run `uvicorn backend.main:app` separately) to enable.` |
| `08428cb` | 2026-10-11 09:32:20 | 0 | clean / clean | 10325 | identical line |

`tests/tenant_isolation_test.py` is blob `e894a02c` at both SHAs.

Of the 790 skipped reports at each SHA, therefore, 1 is this collection-time
skip and 789 are test-level skips. Only the **counts** of skipped and
xfailed reports are shown equal; the root run had no `-rs` / JUnit output,
so the identities of the 789 test-level skips and 7 xfails are **not**
compared and are not claimed identical.

### Root inventory difference

Sorted node-id lists from `python -m pytest --collect-only -q -p no:cacheprovider`
(RC 0 at both SHAs; 10465 vs 10325 unique ids): **+140 / −0**.

* +130 — `tests/test_shopify_privacy_webhooks.py` (the new DB-free tests).
* +10 — `tests/test_single_automation_engine.py::test_no_log_autopilot_event_callers[py_file1151]`
  … `[py_file1160]`: an existing guard parametrized over every backend
  `.py` file (`rglob`); #1205 adds 10 non-test backend files
  (`backend/routers/shopify_privacy_webhooks.py` and nine under
  `backend/services/shopify_privacy/`), so the guard gains 10 cases. These
  are existing per-file automation guard cases, not edits to agent runtime.
  The `py_fileN` ids are positional, so an id does not name a fixed file.
* −0 — no baseline node id is missing from the candidate.

## 3. Owner 65-file regression set — FAILED at both SHAs

The owner's Salla / Meta catalog / tenant isolation / billing / agent
non-interference set: 65 test files, list sha256
`ff606274c0bb84b57bee3310306ff00f80057c12b1cbaa05bde959b8a06460e1`;
`git diff --name-only 08428cb 3681ebd -- <the 65 files>` is empty.

```bash
python -m pytest -q -p no:cacheprovider --junitxml=$OUT/junit.xml $(cat owner_regression_files.txt)
```

| SHA | Finished (UTC) | RC | Result | Tree before |
|---|---|---|---|---|
| `3681ebd` | 2026-10-11 09:02:41 | **1** | **FAILED** — 2 failed, 1075 passed, 41 skipped (452.06s) | clean |
| `08428cb` | 2026-10-11 09:10:39 | **1** | **FAILED** — 2 failed, 1075 passed, 41 skipped (469.29s) | clean |

Failures, identical at both SHAs:

1. `backend/tests/test_salla_embedded_app_identity.py::test_rejection_emits_structured_log`
   (line 93) — source assertion
   `assert any("[SallaEmbeddedAppId] rejected code=invalid_salla_app_id" in r.message for r in caplog.records)`;
   pytest reports `assert False` (file blob `1571e59f` at both SHAs).
2. `backend/tests/test_salla_shipment_tracking.py::test_building_the_sqlite_schema_leaves_jsonb_intact_for_postgresql`
   (line 250) — source assertion
   `assert isinstance(Coupon.__table__.c["metadata"].type, JSONB)`; pytest
   reports `assert False` where `isinstance(JSON(), JSONB)` and
   `JSON() = Column('metadata', JSON(), table=<coupons>).type` (file blob
   `f8c8a8a2` at both SHAs).

JUnit comparison of the two runs: 1118 testcases each, identical
`(classname, name)` sets, **0 outcome differences**; 1075 passed / 41
skipped / 2 failure each, with identical failed and skipped identities. Of
the 41 skipped, 5 are collection-time module skips (JUnit testcases with an
empty classname): `backend/tests/test_salla_cart_recovery_postgres.py`,
`test_salla_commerce_reconciler_postgres.py`,
`test_salla_coupons_poller_race_postgres.py`,
`test_salla_delete_401_branch_closure_postgres.py`,
`test_salla_delete_401_refresh_postgres.py` — reason
`PostgreSQL integration tests require A1_PG_INTEGRATION_REQUIRED=1`.

### Diagnostics (labelled diagnostic; they do not replace the FAILED gate)

Run on both unchanged SHAs with the PostgreSQL variables unset; each tree
was clean before and after (`status_before.txt` / `status_after.txt`,
`D4_status_before.txt` / `D4_status_after.txt` all empty).

| ID | Command (`python -m pytest -q -p no:cacheprovider …`) | `3681ebd` | `08428cb` |
|---|---|---|---|
| D1a | failure 1 alone | RC 0 — 1 passed | RC 0 — 1 passed |
| D1b | failure 2 alone | RC 0 — 1 passed | RC 0 — 1 passed |
| D2 | `backend/tests/test_admin_tenant_list_billing_badge.py` then failure 2 | RC 0 — 6 passed | RC 0 — 6 passed |
| D3 | files 1–38 of the list | RC 1 — 1 failed (failure 1, same assertion), 640 passed, 30 skipped | RC 1 — identical |
| D4 | the full 65-file list with `-rA -k 'test_rejection_emits_structured_log or test_building_the_sqlite_schema_leaves_jsonb_intact_for_postgresql'` | RC 1 — 1 failed, 1 passed, 5 skipped, 1111 deselected | RC 1 — identical |

D4 detail (both SHAs): failure 1 FAILED with the same `caplog` assertion;
failure 2 PASSED; the 5 skipped are the 5 collection-time
`A1_PG_INTEGRATION_REQUIRED` module skips listed above (module-level skips
are reported before `-k` deselection); 1 + 1 + 5 + 1111 = 1118, the full
run's report count. D4 executed the two selected test nodes **together with
their fixtures and the session's conftest / plugin hooks**, after collecting
(importing) the full list.

Interpretation, bounded by this evidence:

* Failure 1 passes alone (D1a) but fails in D4, where only the two selected
  test nodes executed (no unrelated test case ran), with their fixtures and
  hooks, inside the collection context of the full list. This shows
  **sensitivity to the full list's collection context**; it does not isolate
  an import or collection cause. The responsible logger / module is **not
  identified**.
* Failure 2 passes alone (D1b), in the two-file D2 run and in D4; it fails
  only when earlier tests execute in the full list. D2 did **not** rule out
  the billing-badge file: `backend/tests/test_admin_tenant_list_billing_badge.py`
  registers a module-level `before_create` listener on `Base.metadata` that
  rewrites `JSONB` column types to `JSON` and is never removed (verified in
  the source, identical at both SHAs), so it remains a verified suspect,
  including module-alias and ordering interactions. That two-file run was
  insufficient to reproduce the failure. **The polluting fixture or test
  remains unidentified**; this limitation is retained.
* Both failures reproduce identically on the baseline, in files #1205 does
  not change. Neither is fixed here: legacy tests are not silently fixed,
  globals, assertions and source are unchanged, and no failure is
  suppressed.

### Test-generated artifacts

The full regression run rewrites three tracked files at both SHAs:
`backend/tests/salla_acceptance/ACCEPTANCE_RESULTS.json`,
`LAYER2_ACCEPTANCE_RESULTS.json`, `LAYER3_ACCEPTANCE_RESULTS.json` (HEAD
blobs identical at both SHAs). Their diffs and copies were preserved in
`$SP/regression/{cand_3681ebd,base_08428cb}/generated_artifacts/`, then the
files were restored to each pinned HEAD with `git checkout --`; they are not
committed. Clean checks after restoration, at both SHAs:
`git status --porcelain` empty; each file's worktree hash equals its HEAD
blob; the preserved diff applies cleanly to HEAD (`git apply --check`); HEAD
plus the preserved diff reproduces each preserved copy byte-for-byte. The
two preserved diffs differ only in run-specific values (the recorded
`base_sha` and random `dedup_msg_id`s).

## 4. Governance

| Check | Command | Result |
|---|---|---|
| GOV-002 (scanner taken from the baseline) | `git show 08428cb7edf9666a0b31874e8f0256764fd11c90:scripts/lint_intelligence_non_interference.py > $SP/gov_final/scanner.py && python $SP/gov_final/scanner.py --repo . --base 08428cb7edf9666a0b31874e8f0256764fd11c90 --head 3681ebdb4f6f922e4f2f4bd415d8963c45ef3f6c --trusted-base-scanner` | RC 0 — `TRUSTED_BASE_SCANNER_REQUIRED=yes`, `BOOTSTRAP_HEAD_TRUST_EXCEPTION=no`, `FINDINGS=none` |
| Constitution compliance | `python -m pytest backend/tests/test_constitution_compliance.py backend/tests/test_intelligence_non_interference_guard.py -q -p no:cacheprovider` | RC 0 — 98 passed (145.22s) |

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```

`git diff --name-only 08428cb 3681ebd` lists 22 files: the new
`backend/services/shopify_privacy/*` modules, router, migration 0123,
tests, audit script and documents; the manifest suite append and its runner
pin; the bootstrap migration contract and its test; and two wiring edits —
`backend/main.py` (router include and a flag-gated scheduler start) and
`backend/core/observability_sentry.py` (header / body scrubbing). No AI
runtime, prompt, persona, model, dialect, provider registry, Salla, Meta,
WhatsApp or Moyasar file is among them.

## 5. GitHub checks

On the Draft's head `3681ebd`, exactly two check runs exist:
`gov002-trusted-base-scanner` (success, 07:37:09 → 07:37:18Z) and
`publish-merge-freeze-gate` (success, 07:37:08 → 07:37:12Z). `ci.yml` and the
PG18 workflow trigger only for pull requests into `main` / `master`, so
`lint-and-test`, `constitution-compliance` and the strict PostgreSQL jobs
have not run on GitHub for this stacked Draft; the local results above are
their only evidence and are not GitHub check results.

## Not proven / open

* The owner 65-file gate is **FAILED** (reproduced baseline debt above).
* The caplog failure's responsible logger / module and the JSONB failure's
  polluting fixture or test are unidentified (D2 did not rule out the
  billing-badge listener; D4 shows collection-context sensitivity only).
* Root skip / xfail identities are not compared (counts only).
* The 3 s receipt limit is a **handler wait bound**: the HTTP handler stops
  waiting after 3 s and answers 503. The database thread and any COMMIT may
  continue beyond it and keep their slot until they finish. An HTTP receipt
  timeout therefore does not terminate the receipt thread. The dedicated executor (4 threads), its admission semaphore and
  its dedicated pool (`pool_size` 4, `max_overflow` 0) cap concurrent
  receipts at 4 per application process. `statement_timeout`,
  `lock_timeout` and `connect_timeout` limit server-side work and connection
  attempts; they do **not** guarantee termination of a client receive
  stalled on the network or of a COMMIT already in flight. If such a COMMIT
  lands after the 503, Shopify's redelivery joins that request while it is
  still open; a completed request is never reused (a later identical
  delivery opens a new request).
* Bodies are bounded at 64 KiB (the foundation's limit, reused unchanged).
* Owner decisions listed in `shopify-privacy-webhooks.md` are unresolved;
  `APPROVED_POLICY` is `None` and destructive processing is disabled.
* No real Shopify development store, subscription, live delivery or real
  erasure was exercised.

## Release blocker

The known divergence between `main` and the deployed catalog release is a
**release blocker**: this `main`-based stack must never be deployed over the
working catalog. No merge or deployment is proposed; independent review is
required first.
