# Shopify startup compatibility: isolated proof and conditional rollout

Draft [#1214](https://github.com/alayedharthi-stack/nahla-saas/pull/1214).
The accepted functional source stays frozen at `1257e4c10bad28ab189c6d1ff7d4e3c2add15781`.
The experiment checks out accepted evidence head
`7188ddbdcc6c1b0aa0688244c0f3103d7671d52f` and deployed source
`462ede1aff602cad84e9569be859cca0087b190d` separately. Test driver
`30f5b0a255b967655a28fec0ff03a8d3800b9835` changes only the new test files and
workflow. Application, migration, Salla, Meta/Moyasar, operator guard,
expectations, requirements and protected policy source are unchanged. Google
0124 is excluded; the accepted stacked base remains `8308c16e39a18745239e869e6996c7aeaf680fd4`.

**Status: startup cleanliness is blocked by an existing stale-column error;
no full-startup PASS, merge, migration or live deployment approval is claimed.**
The completed 154 Unix + 10 PG16 + 10 PG18 essential guard proof in
[38171916514](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171916514)
is carried unchanged and was not rerun for this slice.

## Exact startup path and source comparison

`start.sh` runs the real production preflight before starting
`uvicorn backend.main:app` (unless an existing emergency/minimal override is
explicitly selected). The ordinary `on_startup` callback then does this:

1. Queues the coexistence client-ID repair, which waits five seconds.
2. Dispatches bootstrap on an independent executor: real Salla duplicate
   cleanup with `--execute`; the tables-without-Alembic stamp precheck;
   contract-pinned `alembic upgrade 0093`. The S0/S3 proof does not stamp.
3. Concurrently dispatches a second executor using the real
   `database.models.Base.metadata.create_all` and 64 best-effort SQL statements.
4. Can queue a Meta subscription HTTP request when platform configuration is
   present. It always queues the delayed Moyasar reconciliation sweep and
   heartbeat before evaluating `NAHLA_DISABLE_SCHEDULERS`.
5. With that flag set, returns before later scheduler registration. Otherwise
   it proceeds with the existing scheduler factories and the dormant Shopify
   recovery/privacy factories.

Both DB tasks catch and log failures; lifespan completion alone is insufficient
evidence of schema readiness. The source comparison found byte-identical
models, session, Salla cleanup, start script and preflight. All four nested DB
function ASTs and the 64-statement list are identical between candidate and
deployed source. The main-file delta adds dormant Shopify routers and gated
workers after the scheduler switch. See the exact Git-blob hashes in
[the source comparison](shopify-startup-source-comparison.json).

## What the experiment executes

The test-only probe compiles the exact source callback with its decorator
removed, supplies its ordinary module globals, and runs its two DB tasks
unchanged and concurrently. Cleanup and Alembic subprocesses are real, not
mocked successes. Source and targets are pinned; provider coroutines are closed
before execution by a test-only task boundary. Empty provider configuration,
HTTP-client and scheduler traps, and a child-process audit hook limit network
connections to the explicit disposable PostgreSQL endpoint. A forbidden-host
canary proves refusal. No live service configuration is read.

For each S0/S3 fixture the test performs genuine upgrades to
`{0111,0113,0120}`, runs the deployed-source DB startup once, and, for S3,
genuinely applies 0121, 0122 and 0123. It plants generic sentinels and independent
synthetic Salla/Shopify tenant integrations sharing an external store identifier.
S3 also has a synthetic active Shopify credential pair and refresh lease.
It snapshots every public table definition, table/sequence/index OID, retained
row counts, whole-row sentinel digests and all integration/Shopify row digests,
then runs candidate startup twice. Assertions precede the strict zero-error
check, so a startup warning cannot prevent the preservation evidence being
collected. No existing guard or baseline was weakened.

The same suite executes the actual production preflight with synthetic settings,
then refuses a missing JWT secret. A separate dispatch canary enables synthetic
Meta configuration while withholding the resulting coroutine; no HTTP client
or provider network call executes and no scheduler registers.

**Boundary:** this is the actual startup callback's DB and dispatch proof. It
does not import/mount the complete API, start uvicorn, issue HTTP health requests,
execute provider workers or build the production container. The focused CI
interpreter is Python 3.12.10; Dockerfile currently selects Python 3.11-slim.
Production image and complete ASGI import compatibility remain separate checks.
The task boundary is test infrastructure, not a new production worker switch.

## Results actually observed

Both jobs in final run
[38174676864](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38174676864)
ran on 2026-10-11, **21:02:04–21:03:43 UTC**, and terminated with the strict
startup failure retained. Downloaded job logs and
[structured results](shopify-startup-compatibility-results.json) establish:

| Server / job | Actual suite result | Completed preservation checks | Post-startup gate |
|---|---|---|---|
| PG16.15, `114574436601` | **4 passed, 2 failed**, 36.61 s; no skips/errors | S0 and S3 PASS after two candidate boots | S0 and S3 PASS, all 19 checks |
| PG18.6, `114574436387` | **4 passed, 2 failed**, 41.08 s; no skips/errors | S0 and S3 PASS after two candidate boots | S0 and S3 PASS, all 19 checks |

The failures are the S0/S3 preservation case's final strict zero-error assertion,
after every preservation assertion and its PASS marker completed. They are not
a full-suite pass. Each server recorded 128 unchanged public table definitions
and 695 unchanged table/sequence/index identities at S0; 143 definitions and
775 identities at S3; 11 generic sentinel tables; whole-row integration digests
at S0 and integration plus all 15 Shopify-table digests at S3. Exact version rows
remain `{0111,0113,0120}` and `{0111,0113,0123}`. S0 acquired no Shopify table.
Synthetic Salla and independent Shopify tenant records, the active S3 encrypted
credential placeholders and refresh lease survived untouched. Both controls
(dispatch/network/scheduler canary and real preflight) passed on each server.

Seven reports per server cover six real DB callbacks plus the dispatch canary.
All have zero provider network attempts, client calls and scheduler registrations;
the six DB callbacks each completed real cleanup and `upgrade 0093` with rc 0,
and each logged only the two unchanged stale-column failures described below.
Both jobs checked actual server majors, clean pinned dependency requirements,
exact candidate/legacy/driver SHA and tree, clean candidate/legacy source before
and after, and stopped their disposable containers successfully. Because pytest
returned 1, the workflow's subsequent JUnit-clean checker did not execute; no
clean-JUnit result is claimed. GOV-002 and merge-freeze passed at test driver
30f5b0a2. Logs were also retained in the local validation directory by job ID.

The workflow and tests are in `.github/workflows/shopify-startup-compatibility.yml`,
`backend/tests/test_shopify_startup_compatibility_pg.py` and its probe. The route
uses the same approved standard Ubuntu 24.04 GitHub runners and immutable PG16/18
images, read-only repository permission, synthetic service credentials, isolated
dependencies, no deploy permission, and no cache/artifact upload.

The first driver `ebb7a4508b2cebf43c4981faef7c67df45dee803`, run
[38174416444](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38174416444),
returned **4 passed, 2 failed** on PG16.15 (28.73 s). PG18 initially ran zero tests:
the public ECR image pull returned `toomanyrequests: Data limit exceeded` three
times. After the run terminated, a retry of that job reached PG18.6 and returned
**4 passed, 2 failed** (34.58 s). The premature rerun API request was refused
because the original workflow was still running; this was a GitHub run-state
refusal, not an authorization denial. The final driver moves the strict error
assertion after preservation checks and avoids a secondary identity-step failure
when source checkout never happened. It does not permit the stale-column error.

Local Windows controls returned one pass (real preflight) and one failure:
the strict socket audit refused asyncio's internal Windows loopback socket.
The earlier local SQLAlchemy URL-parser dependency also encountered an audited
Windows platform subprocess; the probe now uses the stdlib URL parser. Neither
failure is counted as a Linux pass. Local YAML/Python syntax, trusted-base
GOV-002 and silent-exception lint passed; the baseline remains 624 entries.

## Concrete first divergence and remaining decision

Both source versions issue these unchanged statements in `backend/main.py`:

```sql
ALTER TABLE whatsapp_usage ALTER COLUMN conversations_used SET DEFAULT 0;
ALTER TABLE whatsapp_usage ALTER COLUMN conversations_used DROP NOT NULL;
```

Migration `0013_wa_conversation_logs.py` replaces that column with
`service_conversations_used` and `marketing_conversations_used`, copies the
old count and drops `conversations_used`. Current models use the replacement
columns. A genuine migrated database therefore raises `UndefinedColumn` for
both stale statements on deployed and candidate startup. Lifespan and the DB
tasks complete because existing source logs and continues. This is a persisted
schema/startup mismatch, not an AI or Shopify credential failure.

Retain this failure for independent review. A future narrowly reviewed repair
could condition those legacy compatibility statements on column existence;
that would change frozen application source and require a new reviewed source
identity and targeted proof. It is not implemented or waived here. A live
read-only inspection can determine whether the real database still carries
the legacy column, but cannot replace the disposable regression evidence.

## Conditional action-time sequence for existing Nahlah and a new merchant

Nothing below has been executed. Each live action requires its concrete reviewed
target/source/action approval; the current development/test approval does not
authorize it.

1. **Finish source review before live work.** Resolve the startup first
   divergence, full-image/ASGI import boundary and platform side effects. Pin
   the final deploy commit `R`, reproduce its inventory and review its source
   delta. The current gate permits disabled Shopify only; a future activation
   needs an independently reviewed activation policy, not a bypass.
2. **Re-observe the existing platform read-only at action time.** Confirm exact
   live SHA/deployment, project/environment/API/database/volume identities,
   version rows, retained definitions and OIDs, provider configuration state,
   API flags and the dashboard's compiled Vite flags. Do not assume the earlier
   S0/462 observation still holds. Capture a redeployable rollback deployment
   and fresh completed/restorable backup. The observed backup card dated
   2026-10-11 16:54:10.145 UTC, 647 MB, is historical evidence, not restore proof.
3. **Approve and deploy dormant source at S0.** The external S0 approval must
   bind exact `R`, count/digest, project/environment/API service/database and
   backup service/volume, reviewer, validity window and rollback deployment.
   Render and independently review the stdlib predeploy bootstrap; stage the
   source pin, matching command and pinned external payload as one reviewed
   change. Keep all API/Vite Shopify flags false. Verify S0 gate, startup DB
   completion and service health; approve the dashboard source pin separately.
   Ordinary startup can contact Meta/Moyasar before the scheduler switch:
   explicitly resolve that side-effect boundary before deploying. Test-only
   suppression must not be mistaken for production protection or applied as an
   unreviewed change to current Salla/WhatsApp behavior.
4. **Approve one controlled S0-to-S3 migration window.** Verify a disposable
   restore rehearsal of the selected backup; enforce exclusion of every manual
   and automatic API/dashboard deployment actor and drain queued/in-flight
   predeploys. Approve the exact database and source for the three manual
   one-step commands: `upgrade 0121`, audit S1, `upgrade 0122`, audit S2,
   `upgrade 0123`, audit S3, preserving the retained 0111/0113 rows and data.
   No normal deployment at S1/S2. The advisory key only coordinates cooperating
   holders; the existing plain migration blocks do not acquire it. On failure,
   keep exclusion and obtain the approved branch-qualified recovery sequence
   `0123@-1`, `0122@-1`, `0121@-1` as applicable. Never use `head(s)`, stamp,
   merge or direct version-row edits.
5. **Approve the S3 gate/settings before releasing exclusion.** Bind a fresh
   S3 external payload to `R`, target, inventory, rollback snapshot, backup
   observation within 72 hours and verified restore evidence. Review the
   matching command/payload/pin, audit exact S3, allow only the supervised
   dormant deploy and verify startup/health before restoring other deployment
   actors. Redeploying old 462 against Shopify revisions is unsupported;
   returning to it requires an approved database-first rollback to exact S0
   and restoration of the matching legacy pin/command.
6. **Approve the Shopify dev-store connection separately.** Reverify app
   `434031788033` / organization `240110284`. Retain the reviewed standalone
   authorization-code design for Nahlah's existing authenticated dashboard;
   embedding currently off does not determine the final architecture. Owner
   approval must name the exact app-home/callback/uninstall/privacy URLs,
   `read_products`-only scope, grant-installation design and API version.
   Managed-install grant behavior and any change from the connection's 2026-07
   default to 2026-10 need a supervised proof. Configure a dedicated encryption
   key and client credentials out of band, without retrieving/sharing secrets.
   Do not select irreversible distribution or order/customer/payment scopes.
7. **Approve activation and the merchant journey.** After the activation gate
   is reviewed, create/confirm a fresh regular merchant account/tenant for the
   owner's dev store, authenticate that actor and explicitly install/complete
   its own shop binding. No tenant guessing, existing-tenant reuse or automatic
   rebinding. Prove canonical shop ownership, two-tenant refusal, expiring
   encrypted access/refresh credentials, signed uninstall quarantine and safe
   reinstall. Approve provider-scoped product/variant/inventory import as its
   own next slice/flag; the connection journey alone does not prove sync.
   Existing Nahlah/Salla tenants continue using their current source architecture.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
