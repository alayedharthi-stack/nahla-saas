# Minimal startup repair for the dropped legacy WhatsApp usage column

Separate Draft [#1216](https://github.com/alayedharthi-stack/nahla-saas/pull/1216),
stacked on [#1214](https://github.com/alayedharthi-stack/nahla-saas/pull/1214) at
`7031e3efec639625dd537026d5a80376f3d679c9`. **#1214 remains frozen. This repair
requires independent review before any rollout; no live action is authorized by
the test result.**

Functional repair source: `5eafa3b71bea4ca01dbfa585a39aa6962c000f37`, tree
`c88c3308fcf86c4a48b64729d126a14df516c71e`. Test collector correction:
`71e699b41b8598bc2865dbe413989a5ca072dc49`. The repair is exactly 12 added lines
in `backend/main.py`; the collector correction changes no runtime bytes.

## First divergence, change and scope

Migration 0013 copies `conversations_used` into replacement counters and drops
the old column. Two legacy startup statements still alter its default and
nullability. The unchanged deployed 462 and accepted #1214 source both log
`UndefinedColumn` twice on a genuine migrated database. The first divergence
is stale startup SQL against persisted schema, not model behavior or Shopify
authentication.

Inside the existing per-statement transaction, only these two statements now
query `pg_catalog.pg_attribute` for a live, non-dropped column of
`pg_catalog.to_regclass('whatsapp_usage')`. This resolves the same unqualified
relation/search path as the ALTER itself. When present, both existing repairs
execute unchanged (default 0, allow NULL). When absent, only those statements
skip; no legacy column is recreated and no counter value changes. The same
error handler still logs unexpected failures. The metadata check does not claim
to serialize an unrelated concurrent schema change.

The 64 SQL statements remain unchanged. Source comparison removes only this
added guard from the candidate AST and then requires the **entire main-module
AST** to equal frozen #1214, including router mounts, scheduler dispatch and
other DB tasks. Other runtime, requirements, Dockerfile/Nixpacks, migrations,
Salla, Shopify, Meta/Moyasar, operator guards, expectations and protected policy
files are byte-identical to the frozen base. No exception or assertion was
waived. The old #1214 failure evidence and tests remain unchanged.

## Focused proof and provenance

The new workflow is `.github/workflows/startup-legacy-whatsapp-usage.yml` and
collects only `backend/tests/test_startup_legacy_whatsapp_usage_pg.py`: ten cases.
It reuses the already approved Ubuntu 24.04 disposable PG16/18 route, immutable
server images, isolated Python 3.12.10 and pinned focused dependencies. It has
read-only repository access, synthetic PostgreSQL service credentials, no
deployment permission, no live secrets and no cache/artifact upload.

Four cases execute the exact source loop against real PostgreSQL, with the old
column present or dropped (including its catalog tombstone), under `public` and
a non-default search path. Opposite-state public decoy tables verify that the
lookup follows the ALTER's real target. They reproduce the baseline failure
when absent, retain default/nullability behavior when present, allow insertion
without a legacy count and explicit NULL after repair, preserve existing
counts/rows/relation OIDs, and run the candidate twice.

At S0 and S3, genuine migration chains build the database; deployed-source DB
startup establishes its real footprint, accepted baseline startup reproduces
the two known errors, and two candidate startups require **zero** SQL/bootstrap
errors. Full public schema definitions and table/sequence/index OIDs, exact
version-row multisets, 11 generic sentinel tables and every integration/Shopify
row digest must remain unchanged, including independent tenant ownership,
synthetic active Shopify credentials and refresh lease. The unchanged post-boot
gate must pass all 19 checks at both phases. Existing production-preflight and
provider/scheduler/network controls are reused unchanged.

The source callback's two DB executors and real Salla cleanup/Alembic subprocesses
run concurrently. Other provider coroutines are withheld before execution by
the existing test-only boundary; HTTP/scheduler traps and a forbidden-network
canary remain active. This does not mount the complete ASGI application.

Final [run 38176146625](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38176146625)
ran on 2026-10-11, **21:23:11–21:24:45 UTC**, at exact tested source 71e699b4,
tree `a2e3112b80fa6e13c9ec558c57654c15f7bcd189`. Both jobs reached terminal success:

| Server / job | Actual completed result |
|---|---|
| PG16.15 / `114578843373` | **10 passed**, 37.18 s; JUnit clean: 10 tests, zero skipped/failures/errors |
| PG18.6 / `114578843204` | **10 passed**, 36.30 s; JUnit clean: 10 tests, zero skipped/failures/errors |

All four focused old/dropped-column cases passed on each server. Both candidate
boots at each S0/S3 had zero SQL/bootstrap errors; accepted and deployed baselines
each reproduced exactly the two known failures. S0 preserved 128 public table
definitions and 695 table/sequence/index identities; S3 preserved 143 and 775.
The full row/version/ownership assertions described above passed. Both post-boot
gates passed all 19 checks. Each server emitted nine source reports (eight real
DB callbacks and one dispatch canary), all with zero provider network attempts,
HTTP client calls and scheduler registrations. The real DB callbacks completed
cleanup and upgrade 0093 with rc 0. Each job verified source SHA/tree before and
after, untouched tracked source, actual server major and clean pinned dependency
requirements, and stopped its disposable container successfully.

[Structured downloaded-log evidence](startup-legacy-whatsapp-usage-results.json)
records hashes, source identities, actual counters and completed checks. Full job
logs are also retained locally by job ID. GOV-002 and merge-freeze passed at
71e699b4. This is a scoped source-loop/startup-DB proof, not full ASGI/image
acceptance or independent review of this repair.

The first
run [38175875839](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38175875839)
at 5eafa3b7 returned **8 passed, 2 failed** on each server (PG16: 42.25 s; PG18:
25.60 s). The startup comparison already passed with zero candidate errors,
preserved state and S0/S3 gate PASS. The two absent-column focused cases failed
because importing the read-only release gate intentionally disables global
logging; the test's logging handler therefore missed real baseline SQL errors.
The test-only correction collects source warning calls directly, preserving
the global logging safeguard and every strict assertion. Runtime repair source
did not change.

Local syntax, ten-case collection, exact-Git full-main AST scope, trusted-base
GOV-002 and silent-exception lint passed (624 pre-existing baseline entries,
unchanged). The initial local AST comparison failed after PowerShell decoded
Git output through its native text encoding; materializing raw Git bytes fixed
the comparison without relaxing it. No local PostgreSQL result is claimed.
The completed 154+10+10 essential #1214 guard proof is carried and not rerun.

## Remaining full-ASGI and production-image gaps, classified by source delta

| Gap | Classification and practical consequence | Bounded next verification |
|---|---|---|
| Shopify router/service/model imports and mounts | **Material Shopify regression risk.** Four routers are mounted unconditionally before route feature checks. Import, declarative model or dependency failure can prevent API startup even with Shopify disabled. The callback proof deliberately does not exercise this layer. This 12-line SQL repair adds no imports or mounts. | On the independently reviewed candidate and actual production build, use synthetic settings and provider egress prevention to import/mount the API and exercise dormant routes and health. Do not infer this pass from DB callback results. |
| Shared JWT callback exception and credential redaction/Sentry integration | **Material Shopify ASGI integration risk.** Relative to deployed 462, accepted source adds one exact Shopify callback exemption and changes shared redaction hooks. Request-level middleware ordering, authentication and secret-safe logging are outside this DB proof. This repair leaves all those files unchanged. | Review the exact exemption and run focused callback/unauthenticated/disabled-route and secret-safe HTTP checks against the mounted app; retain existing Salla behavior. |
| Python/package/image parity | **Pre-existing build uncertainty with a material verification requirement for the new Shopify import graph.** Dockerfile remains `python:3.11-slim` with requirements installation; Nixpacks also installs requirements and runs start.sh but does not pin Python here. The focused CI uses 3.12.10. Neither image/build configuration nor dependency requirements change in this repair. Package availability and import compatibility in the actual selected build are unproven, rather than an observed new dependency defect. | First read the selected production builder/runtime/image identity; validate that same build path and its new Shopify import graph before deployment. Do not claim Dockerfile alone identifies the running Railway image or install a different environment to manufacture parity. |
| Full pre-existing API/AI router graph and native/runtime dependencies | **Baseline operational surface.** Heavy imports, middleware construction, uvicorn binding and shared system dependencies already exist in deployed 462. No redesign is justified by this SQL failure. A complete mounted candidate still needs verification because a new router can interact with that graph. | If full boot fails, compare the same controlled baseline and candidate and stop at the first source-backed divergence; do not patch prompts, models or unrelated runtimes. |
| Early Meta subscription, Moyasar sweep, heartbeat and coexistence repair | **Pre-existing operational behavior, unchanged.** These tasks are queued before the scheduler disable switch. Their suppression in test infrastructure is not evidence that production suppresses them. The source-scope assertion protects their order and bodies. | Resolve the authorized provider-side-effect boundary before any supervised full-app/live boot. No scheduler redesign or new production suppression switch is included. |
| Concurrent best-effort DB executors and liveness-before-readiness | **Pre-existing operational behavior.** Source logs/continues on DB failures and can complete lifespan before migrations finish. This repair removes only the observed stale-column failures; it does not redefine readiness, timeouts or concurrency. | Supervised rollout must observe actual DB-task completion plus application health, under the existing controlled migration/deployment sequence. |

These are scoped distinctions, not full-startup approval. The existing conditional
[rollout sequence](shopify-startup-compatibility-evidence.md) still applies:
independent source review; fresh source/target/backup and rollback observations;
separate S0/S3 approvals; verified restore and enforced deployment exclusion
through manual migration; later reviewed activation policy/app settings and an
explicitly authenticated fresh merchant tenant journey. No scheduler redesign,
Shopify activation, live migration/deploy, credential access or purchase occurred.
Google 0124 remains excluded.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
