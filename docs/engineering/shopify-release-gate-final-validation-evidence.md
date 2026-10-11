# Shopify release gate: frozen source and consolidated validation

Draft #1214: <https://github.com/alayedharthi-stack/nahla-saas/pull/1214>.
Functional source is frozen at **1257e4c10bad28ab189c6d1ff7d4e3c2add15781**,
tree **c40f954a3a9218b6148230cd5778e8eacb14bedd**. A later evidence-only commit
does not replace this tested source identity. Stacked base remains
`8308c16e39a18745239e869e6996c7aeaf680fd4` (#1211). #1209 stays frozen at
`1e9f1fdaf866cb27be0c33ea6b52aa2796983522`. Migration sources end at 0123;
Google 0124 was not absorbed.

Status on 2026-10-11: independent source review has closed the three residual
findings at this source/tree without weakening guards. **Essential supported
Unix/PostgreSQL validation is complete: 154 + 10 + 10 cases passed, zero skips,
failures or errors, in run 38171916514.** Parent review of the completed logs
and final acceptance remain separate; no merge or rollout approval is claimed. The
earlier 7f89dcbe evidence and Claude 2ab3042d PostgreSQL reports are historical
results, not passes for the current guard.

## Corrections included in the frozen source

The stdlib bootstrap AST allowlist now explicitly permits `importlib.machinery`;
the earlier failure was a test assertion defect and was not solely an environment
limitation. Snapshot validation rejects any comma-separated effective host list
and the production host's DNS-root-dot spelling before constructing its read
engine. Dependency pins are bound to unique trusted interpreter distributions,
recorded module origins and actual imported versions. Repository metadata cannot
advertise a pin while selecting a different installed implementation.

Verified-source loading, bytecode refusal, effective default read-only checks,
explicit target validation and the valid wrong-owner fixture remain intact. No
AI runtime, prompts, model selection, Salla implementation, migrations, protected
governance code, existing CI workflow or dependency requirement was changed by
these corrections.

## Tests actually run

All times below are UTC on 2026-10-11. Logs are retained in the local task's
`shopify-guard-validation` directory; their names identify the underlying run.
No new local software was installed.

| Run / provenance | Actual result | Evidence |
|---|---|---|
| Frozen 1257e4c, native Windows Python 3.13, available DB-free selection; 19:46:13–19:46:47 | 143 passed, 1 skipped, 10 deselected; 25.89 s; exit 0 | `final-native-available.log` |
| Frozen 1257e4c, existing pinned Windows Python 3.10.11 environment; full DB-free module; 19:42:21–19:43:08 | 142 passed, 3 failed, 8 errors, 1 skipped; 44.15 s; exit 1 | `final-pinned-db-free-full.log` |
| Focused correction controls; 19:37:19–19:37:33; same corrected code bytes, before final commit freeze | 11 passed, 143 deselected; 4.54 s; exit 0 | `review-corrections-focused.log` |
| Frozen 1257e4c trusted GOV-002 scanner, scanner extracted from exact base 8308c16e | FINDINGS=none; all six interference flags NO; exit 0 | `final-trusted-base-scan.log` |
| Frozen 1257e4c silent-exception lint | OK: 624 pre-existing entries, all baselined; exit 0 | `final-no-silent-except.log` |
| Current actual installed-library binding in the existing pinned environment | PASS: Alembic 1.13.1, SQLAlchemy 2.0.30, psycopg2-binary 2.9.9; module origins recorded | Existing environment read and gate check |

The module collects 154 cases. Native Python 3.13 has newer library versions;
deployment pins were not relaxed. Its ten deselections comprise nine Unix-rendered
harness cases and the strict-library CLI case. The one skip is the missing exact
legacy checkout. The earlier 57+41 constitutional passes were pre-freeze working
tree results; no fresh 1257e4c run of those suites is claimed.

### Full pinned Windows run: classify the failures separately

1. Eight setup errors and one handover failure reach `token_not_renderable` at
   the renderer's executable-token check: the Windows interpreter path contains
   backslashes. These cases do not reach the rendered Unix harness. The static
   production-path round-trip control now passes.
2. One positive unchecked-hash-bytecode contract-import control reaches
   `BOOTSTRAP PASS`, then refuses `repository_import_unverified`. Its `scripts`
   namespace includes both the inventoried root and pywin32's external
   `site-packages/win32/scripts` location. This is an observed compatibility
   refusal in that environment; the import guard was not weakened. A supported
   Unix positive result is still required.
3. The CLI negative control's child exits during Python initialization with
   `_Py_HashRandomization_Init: failed to get random numbers`. The minimal child
   environment lacks Windows `SystemRoot`; the intended gate assertion is not
   reached. Frozen test source was not changed to hide this result.
4. The legacy digest case skips because `NAHLA_LEGACY_462_CHECKOUT` is unset.

These are not a full-suite pass. The earlier blanket classification of all eleven
excluded cases as environment limitations was incomplete: the stale import
allowlist was a concrete defect, corrected in 1257e4c. This document supersedes
that conclusion in the historical independent-review evidence.

Exact frozen native selection:

```text
py -3.13 -m pytest tests/test_shopify_release_gate.py -k 'not bootstrap_refuses_a_tampered_module and not bootstrap_refuses_a_planted_initializer and not bootstrap_refuses_wrong_release_identity and not bootstrap_hands_over and not cli_fails_closed' -q -ra --tb=short
```

Exact pinned full-module command:

```text
<existing task-2 .venv>/Scripts/python.exe -B -m pytest tests/test_shopify_release_gate.py -q -ra --tb=short
```

## Frozen runtime inventory

| Field | Value |
|---|---|
| Gate version / inventory count | 2 / 143 |
| Gate SHA-256 | `206eb16195a8afe3307f835c047553db6c3f917d9df8f5172a90cab3dee33157` |
| Canonical Git / Linux inventory digest | `1d3a50f171b155b5615b30e020f5400e831bb194ce9c8eaf867a1707bf396ce4` |
| Database Python digest | `b3fcf5d151a106a9068985118d6489c381844c1ba5553fbffc9d94ffe272aa50` |
| Unexpected initializers | `[]` |

The earlier `83c92152e2dd7a8c04420902e5dc9fa38087eec849cec03657fe705986fbde92`
digest describes the local Windows materialization used by the reported local
runs, not the canonical Git blobs. Ten inventory files still had CRLF bytes;
all ten normalize exactly to their frozen Git blob, and every other inventory
file already matches. The gate and database digests above remain unchanged.
The initial workflow assertion mistakenly used that Windows digest. The
canonical value above was independently reproduced from a restricted `git
archive` of all 143 frozen inventory paths, hashing sorted `path NUL bytes NUL`
exactly as `digest_paths` does. [Audit record](shopify-release-gate-canonical-inventory.json).

## Supported runner assessment and concrete proposed job

The local machine has native PostgreSQL 17.10. Its Google task's disposable
cluster was separate and was not used or altered. Docker Desktop's Linux-engine
pipe was unavailable; the only WSL distribution listed was docker-desktop. No
local Unix/PostgreSQL 16/18 route was confirmed and no installation, daemon start,
host reconfiguration or shared-cluster mutation was attempted.

The repository is public. Existing `ci.yml` and `postgres18-compatibility.yml`
use standard Ubuntu runners and disposable PostgreSQL 16/18, but their triggers
exclude this PR's stacked base branch. They have no `workflow_dispatch` entry.
Their strict proof manifest does not include the release-gate module. The
supported connector exposes reruns of existing jobs, but no workflow dispatch;
no matching existing full test run was returned for frozen 1257e4c. Retargeting
the PR or broadening existing workflow triggers is not proposed.

A reviewable test-only YAML is included as
[the proposed validation job](shopify-release-gate-validation.proposed.yml), intended
for `.github/workflows/shopify-release-gate-validation.yml` on this Draft branch.
This describes the initial proposal at f103ea88; its subsequent owner-approved
activation and failed first run are recorded below. Its scope is:

- Branch-specific, workflow-file-path-specific `push` trigger; standard
  `ubuntu-24.04`, two jobs, 30-minute limit each, `contents: read` only.
- Public Git fetches of exact functional source 1257e4c and exact legacy
  462ede1, canonical LF bytes, no submodules or persisted checkout token.
- Isolated Python 3.12.10; all twelve required test packages/transitive
  dependencies pinned, including the three unchanged deployment pins.
  Dependency integrity, actual versions/origins, SHA/tree and byte inventory
  are recorded before testing.
- Disposable PostgreSQL images by the existing workflows' immutable digests;
  actual server major versions asserted. Synthetic credentials only. An
  ephemeral runner-only host alias addresses the disposable mapped service;
  no production target or inherited live environment is used.
- Full 154-case DB-free module once on Unix with the exact legacy checkout;
  all ten release-gate PostgreSQL cases on each of 16 and 18, strict required
  environment, JUnit cleanliness and exact case counts. The PG suite can still
  run if the separate DB-free step fails; any failure keeps its job red.
- No cache or artifact upload/storage. Results, counts and timestamps stay in
  job logs. Existing governance, freeze, baseline and required checks remain
  unchanged. This focused job does not replace them.

Standard GitHub-hosted runners are free for public repositories. Job logs do
not count toward artifact storage; this proposal uses neither paid/larger
runners nor stored artifacts/cache. Source:
<https://docs.github.com/en/billing/concepts/product-billing/github-actions>.
Publishing this workflow would be a separate test-only commit, followed by
normal push to the existing Draft branch; its job checks out frozen 1257e4c,
not the workflow commit's moving source.

## Owner-approved CI activation: first attempt

The owner explicitly approved publishing and running the proposed public test
workflow. Its exact reviewed YAML was installed in a separate test-only commit
`d06cbf2bcdd5d461a510659dbf0ebafe72e90f93`; functional source remains frozen at
1257e4c. [Run 38171280991](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171280991)
started at 20:13:19 UTC on 2026-10-11 and both jobs reached terminal failure.

| Job | Observed setup and result |
|---|---|
| [PostgreSQL 16 / 114564237735](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171280991/job/114564237735) | Disposable 16.15; exact source SHA/tree and legacy checkout fetched; isolated Python 3.12.10; twelve exact package versions installed; `pip check` clean. Inventory assertion failed at 20:14:04 UTC. DB-free and PG suites skipped by step conditions: **zero test cases executed**. Containers stopped. |
| [PostgreSQL 18 / 114564237598](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171280991/job/114564237598) | Disposable 18.6; same source/tree, interpreter and dependency setup. Inventory assertion failed at 20:14:02 UTC. Both test steps skipped: **zero test cases executed**. Containers stopped. |

Both jobs computed `(143, 1d3a50f171b155b5615b30e020f5400e831bb194ce9c8eaf867a1707bf396ce4)`.
The assertion carried the earlier local Windows inventory digest. The fresh
Linux checkout and exact frozen Git archive agree; this is a workflow/evidence
pinning mistake, not evidence of a changed functional source. The failure is
retained. No guard was bypassed and no current Unix/PG suite pass is claimed.

The proposed YAML now has a **single-line correction** to that expected
canonical digest. All other YAML bytes remain those reviewed at f103ea88.
At that checkpoint the active workflow was still the original d06cbf2b version;
the correction was held for parent review under the instruction to activate
only independently reviewed YAML. The parent subsequently approved the exact
one-line correction at 7494646d, and it was activated in c9909973 as recorded
below. Gate SHA, tree pin, count,
library binding, all tests, zero-skip checks and permissions remain intact.

Existing [GOV-002](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171281902)
and [merge-freeze](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171281925)
checks completed successfully at d06cbf2b. They do not replace the unrun suites.

## Completed supported Unix and PostgreSQL validation

The independently reviewed one-line correction was published in test-only
commit `c990997361a81369c3c6e1c27769793013d01d2f` under the owner's existing
explicit workflow publication/execution approval. Functional source, test
assertions, guards, permissions and all other workflow bytes remain unchanged.
[Run 38171916514](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171916514)
started at 20:22:13 UTC and completed **successfully** at 20:25:41 UTC on
2026-10-11. Both jobs reached terminal success and stopped their disposable
containers. The previous failed run above remains part of the evidence.

Actual counts below are taken from the downloaded completed job logs. Every
JUnit summary reports `ok=True`, `reason=clean`, `skipped=0`, `failures=0`,
`errors=0`; the workflow also asserts the exact 154/10 counts.

| Suite / job log | Actual result | UTC window |
|---|---|---|
| [Full DB-free module, Unix / job 114566111983](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171916514/job/114566111983) | **154 passed** in 12.18 s; JUnit tests=154, zero skips/failures/errors | 20:22:54–20:23:07 |
| [Strict PostgreSQL 16 / job 114566111983](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171916514/job/114566111983) | **10 passed** in 149.68 s; JUnit tests=10, zero skips/failures/errors; server_version_num=160015 (**16.15**) | 20:23:08–20:25:38 |
| [Strict PostgreSQL 18 / job 114566112156](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171916514/job/114566112156) | **10 passed** in 148.85 s; JUnit tests=10, zero skips/failures/errors; server_version_num=180006 (**18.6**) | 20:23:06–20:25:36 |

The DB-free module ran once, in the PG16 job. Its intentionally omitted
duplicate step in the PG18 job is not a skipped test case. No `-k` selection
or deselection was used. The exact legacy checkout enabled both the DB-free
legacy digest case and the genuine legacy bootstrap refusal/rollback proof.
Both PostgreSQL runs used strict required-PG mode, the explicit disposable
admin URL and the ephemeral host alias, with genuine upgrades and rollbacks.

Both jobs verify and record:

- Functional source SHA `1257e4c10bad28ab189c6d1ff7d4e3c2add15781` and tree
  `c40f954a3a9218b6148230cd5778e8eacb14bedd`, before and after testing, and
  a clean tracked source diff.
- Legacy SHA `462ede1aff602cad84e9569be859cca0087b190d`, tree
  `f2866da7abec02e55592360b0360279205b6c08c`.
- Count 143, canonical inventory
  `1d3a50f171b155b5615b30e020f5400e831bb194ce9c8eaf867a1707bf396ce4`, gate SHA
  `206eb16195a8afe3307f835c047553db6c3f917d9df8f5172a90cab3dee33157`.
- Isolated Python **3.12.10**, twelve pinned installed packages and clean
  `pip check`. The actual library-binding gate reports PASS; Alembic 1.13.1,
  SQLAlchemy 2.0.30 and psycopg2-binary 2.9.9 originate from the isolated
  interpreter's `lib/python3.12/site-packages`, outside the checkout.

The Unix positive import and rendered-handover controls pass without weakening
the Windows-observed namespace refusal or executable checks. The real PG cases
exercise the valid wrong-owner target and restoration, current footprint
snapshots, drift, locks/timeouts, S0/S3 acceptance, intermediate-state refusal
and legacy compatibility. This closes the essential runner gap for the frozen
guard; it does not assert full startup or live deployment compatibility.

[GOV-002](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171917813)
and [merge-freeze](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171917790)
also completed successfully at workflow commit c9909973. These focused proofs
do not replace other required repository checks or independent parent review.

## Acceptance versus rollout

Essential test coverage is now complete: supported Unix full module with zero
skipped cases and 10/10 strict PostgreSQL proofs on each of 16 and 18, with exact
source provenance. Final guard acceptance still requires the parent's independent
review of the completed logs and consolidated evidence. The PR remains Draft.

Full startup compatibility (Salla cleanup, `create_all`, safe alters) remains
a separate rollout gate. The operator plan's enforced deployment exclusion,
drained pre-deploys and non-cooperating migration limitation remain explicit.
Any future live migration requires separate approved phase/target/source
evidence, fresh backup and, for S3, restore rehearsal. A completed backup with
Restore available is not restore proof or rollout approval.

Supported browser control and `node_repl`/`cua_repl` remained unavailable at the
owner-requested 19:29 UTC retry; no new local brief or report from Claude is
claimed. No live Shopify setting, permission, credential, source pin,
migration, deployment or activation was changed. Shopify remains disabled.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
