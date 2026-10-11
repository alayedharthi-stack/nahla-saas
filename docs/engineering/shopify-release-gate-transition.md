# Shopify release gate — deployment guard and source-pin transition on the existing platform (Draft)

Status: **design + implementation for review. Nothing here has been applied.**
No platform setting, pre-deploy command, source pin, secret, backup, restore,
migration, deployment, OAuth grant or Shopify app setting was changed by this
slice. PRs #1209 (`1e9f1fdaf866`) and #1211 (`8308c16e39a1`) stay frozen; this
branch starts from exact `8308c16e39a1` and adds operator files, tests and
documentation only. Every live action below is a later, separately approved
owner step, and the per-release operational approval ships **unfilled**.

Companion documents: the exact-topology migration plan
(`shopify-multihead-migration-plan.md`, PR #1211), the deployed-compatible port
runbook (`shopify-deployed-port-staging-runbook.md`, PR #1209) and this slice's
evidence (`shopify-release-gate-transition-evidence.md`).

## 1. The operational problem

The existing platform target (API `https://api.nahlah.ai`, dashboard
`https://app.nahlah.ai`, Railway project `f0090862-0a40-4293-bd5d-e94df58762b5`,
environment `ede962ce-3042-4dae-94de-623837e83ed9`, API service
`686b36c5-a926-4e58-912a-5e9d13fbc2e7`, database service
`b77b3d27-47b0-4a3a-83fd-44def66a3a84` on host `postgres-ancu.railway.internal`,
port 5432, database `railway`) deploys commit `462ede1aff60` on the ops branch.
Its configured pre-deploy command is a standalone inline Python catalog guard
that is **not in the repository**. It is pinned to that commit
(`FINAL_RELEASE_SHA`), to a source digest
`12798863bd12348de4407263fd1a1fe86ef639a0df2bfac2fc94244bd1f714c4` over exactly
132 files, to the migration graph `0113->0112, 0118->0112, 0120->0118` with the
ancestor delta `{0118, 0120}`, and it accepts the already-applied state
S0 = `{0111, 0113, 0120}` — or **mutates** `{0111, 0113}` to S0 by running
`alembic upgrade 0120` itself.

The accepted Shopify source (`a246580519f9` tested as `d199035ca163`, published
through #1209 and #1211) adds revisions `0121 -> 0122 -> 0123` on the same
branch and three new source files per revision. The legacy guard would refuse it
on every axis it checks (release sha, digest, graph, heads), which is correct:
the guard is doing its job. What is needed is a **new** normal-deployment
compatibility gate for the new source that keeps every one of the legacy
safeguards, adds the Shopify-specific ones, and inherits **none** of the
auto-upgrade behaviour: the new gate never upgrades, stamps, merges or edits
`alembic_version`, and it never runs Alembic or any subprocess.

The owner's read-only audit of the live database (2026-10-11) observed exactly
S0 with the six retained tables of the `0111/0113/0118/0120` branches holding
90 columns, 33 constraints and 18 indexes, five serial sequence ownerships, and
no Shopify-named relation, type, constraint or function. A disposable build of
S0 from the accepted source reproduces those exact figures (evidence §7 of the
evidence document). The gate nevertheless verifies reality at execution time:
an observation made today is not an attestation for a later deploy.

## 2. What ships in this slice

| File | Role |
|------|------|
| `scripts/operators/shopify_release_gate.py` | The gate. Read-only, phase-aware, fail-closed. Executed by the trusted command inside the deployed image. |
| `scripts/operators/shopify_release_gate_contract.py` | Closed constants: phases, graph edges and heads, inventory rule, library pins, identity variables, timeouts, flag names, retained-definition pins, approval rules, reason tokens. |
| `scripts/operators/shopify_release_gate_render.py` | Operator tool (never in the container): `inventory`, `render` (the trusted command), `snapshot` (schema expectations from a disposable build), `approval-check` (offline verdict). |
| `scripts/operators/shopify_release_schema_expectations.json` | Canonical definitions (columns, every constraint, every index) of the six retained tables, the WhatsApp nonce relation by definition with names abstracted, `tenants`/`users` primary-key types, the conversation scope key and sequence ownership. Generated from a disposable S0 build; bound to the migration files by digest. |
| `scripts/operators/shopify_release_approval.json` | The owner's per-release, per-phase operational approval and recovery record. **Shipped unfilled; the gate refuses every phase until it is filled and bound.** |
| `tests/test_shopify_release_gate.py` | DB-free contract (phases, pins, inventory rule, rendered command, every evaluation, approval rules, static read-only guarantees, CLI secret safety). |
| `backend/tests/test_shopify_release_gate_pg.py` | Disposable PostgreSQL: phase matrix forward and back, footprint, drift, malformed rows, locks and timeouts, read-only database, identity/target/flag/approval refusals, expectations freshness, old-source bootstrap behaviour. |
| `backend/tests/_shopify_release_gate_harness.py` | Synthetic container root, synthetic approval, rendered-command runner for both test modules. |

Existing runtime, Salla, Meta, AI/prompt/persona/model/dialect, bootstrap,
governance and CI files are untouched. The gate reuses the existing pilot-job
validators (`parse_database_url`, `explicit_url`, `sanitized_environment` in
`scripts/operators/commerce_runtime_pilot_migration.py`) unchanged.

## 3. Trust root and bindings (no circularity)

The pre-deploy command the owner pastes is **rendered**, not hand-written:

    python scripts/operators/shopify_release_gate_render.py render \
        --release-sha <deployed commit> --phase S0

It is one line of `python -I -c "..."` carrying, as literals: the SHA-256 of
the gate module, the release commit, the digest and count of the **full
inventory**, the phase, the three platform ids and the database target. The
command verifies the gate module's digest **before** executing it, so a changed
gate is never run; the gate then verifies an inventory whose expected digest it
was *given*, never one it stores. The gate file is inside the inventory, which
is sound because the expected value lives in the command, not in the file.

Two inventories, one rule (sorted relative paths, `path NUL bytes NUL`,
SHA-256 — the legacy guard's rule, reproduced exactly: it yields the legacy
digest over the same 132 files at `462ede1aff60`):

* **runtime inventory** = legacy rule (`requirements.txt`,
  `database/alembic.ini`, the pilot migration job and its contract, every
  `database/**/*.py`) + the gate, its contract, the schema expectations, the
  bootstrap contract and the three Shopify flag modules. The approval record
  binds to this digest and count, so filling the record cannot change what it
  approves.
* **full inventory** = runtime inventory + the approval record. The rendered
  command pins this digest and count, so the record cannot change without a
  re-rendered command.

The deployed commit is read from `RAILWAY_GIT_COMMIT_SHA` only (the looser
operator-settable variables used by staging attestation are not accepted).
Platform identity is read from `RAILWAY_PROJECT_ID`,
`RAILWAY_ENVIRONMENT_ID`, `RAILWAY_SERVICE_ID` and compared with the command's
literals. The database target is the effective target the driver would use
(query overrides such as `?host=`, `?service=`, inherited `PG*` variables and
loopback hosts are refused by the existing validator), bound explicitly, and
must equal `postgres-ancu.railway.internal:5432/railway`.

## 4. Checks, in order (closed reason tokens)

Source-side, before any connection: `root` (the gate runs from
`/app/scripts/operators/shopify_release_gate.py` and `/app/database` exists);
`release_sha`; `inventory` (`inventory_file_missing`,
`inventory_count_mismatch`, `inventory_digest_mismatch`); `libraries`
(Alembic 1.13.1, SQLAlchemy 2.0.30, psycopg2-binary 2.9.9); `graph` (every
edge `0110->0109, 0111->0109, 0112->0110, 0113->0112, 0114->0113, 0115->0112,
0118->0112, 0120->0118, 0121->0120, 0122->0121, 0123->0122`, repository heads
exactly `{0092, 0111, 0114, 0115, 0123}`, the phase's Shopify branch delta from
`0112`, and the retained rows `0111`/`0113` on distinct branches that the
Shopify row is not an ancestor of); `identity`; `database_target`;
`approval` (§6); `shopify_flags` (every
`NAHLA_SHOPIFY_*_ENABLED` unset or false); `expectations` (the schema
expectations file is bound to the migration files present).

If any of these fails, the database is **not contacted**.

Database-side, one connection (NullPool, `echo=False`,
`hide_parameters=True`, `connect_timeout=5`, `statement_timeout=15000`,
`lock_timeout=3000`, `idle_in_transaction_session_timeout=20000`, read-only
transaction, wall-clock alarm 120 s): `connect`; `lock` (session advisory lock
`pg_try_advisory_lock(0x4E41484C, 0x53524C47)`, `lock_denied` when any other
holder exists — a concurrent gate or migration operator makes the deploy
refuse, not wait); `server` (`pg_is_in_recovery()` false, no
`default_transaction_read_only=on` at database or role level, current schema
`public`, current database equal to the target); `version_rows` (exactly the
phase's rows: table missing, duplicates, unknown ids and any other multiset are
refusals; unknown values are counted as `<unknown>`, never echoed);
`shopify_footprint` (exactly the phase's `shopify_*` tables — none at S0 — the
`uq_shopify_connections_id_tenant` key present iff phase ≥ S2, and no other
`shopify_*` relation, type, function or `*shopify*` constraint anywhere);
`retained_schema` (canonical definitions of the six retained tables, the nonce
relation by definition, integer primary keys on `tenants`/`users`, the
`UNIQUE (id, tenant_id, namespace)` key on `commerce_runtime_conversations`,
sequence ownership — drift is reported by **table name only**); `unlock`
(`pg_advisory_unlock` must return true; otherwise the run fails and the
connection is invalidated, whatever the other checks said).

Output: `CHECK <name> PASS|FAIL reason=<token> {closed detail}` lines and one
`RESULT=PASS|FAIL`. Exit 0 only on PASS; 1 on any FAIL; 2 on invalid
invocation (including any phase name that is not S0..S3). No DSN, environment
value, row value or exception body is ever printed.

## 5. Source / state / phase matrix

The gate accepts **one** named phase per run. There is no blanket "any of
S0..S3": the phase is a literal in the rendered command and must be re-rendered
to move.

| Deployed source | Database state | Legacy inline guard (462) | New gate (this slice) | Outcome |
|---|---|---|---|---|
| `462ede1aff60` (legacy) | S0 `{0111,0113,0120}` | PASS (already applied) | not present in that source; the trusted command exits 2 if ever configured against it | today's production |
| `462ede1aff60` | `{0111,0113}` | **mutates** to S0 (`upgrade 0120`) | n/a | the behaviour the new gate does not inherit |
| `462ede1aff60` | S1 / S2 / S3 | FAIL — `0121+` unknown to its graph and topology checks | n/a | **old release cannot be redeployed against a Shopify state**; DB-first rollback to S0 required (§8) — never whitelisted |
| accepted Shopify source, command phase `S0` | S0 | FAIL (release/digest/graph) | PASS | first deploy of the new source, dormant, flags OFF |
| accepted source, phase `S0` | S1 / S2 / S3 | — | FAIL `alembic_version_rows_mismatch` + `shopify_footprint_unexpected` | deploy refused until the phase is re-rendered |
| accepted source, phase `S1` | S1 `{0111,0113,0121}`, five foundation tables, no key | — | PASS | after the separately approved `upgrade 0121` |
| accepted source, phase `S2` | S2 `{0111,0113,0122}`, +seven catalog tables, key present | — | PASS | after the separately approved `upgrade 0122` |
| accepted source, phase `S3` | S3 `{0111,0113,0123}`, +three privacy tables | — | PASS | after the separately approved `upgrade 0123` |
| any phase | partial footprint, stray `shopify_*` object, duplicate/unknown/missing row, retained drift, flag on, lock held, read-only DB, wrong identity/target | — | FAIL (named token) | deploy refused |

Phases are named states of the **deployed topology**; `head`, `heads`, `stamp`,
`merge` and version-row edits are never used by anything in this slice, and the
only future rollback spellings remain `0123@-1`, `0122@-1`, `0121@-1`, each
with Shopify features off and its own approval (#1211 plan §2).

## 6. Approval and recovery record (fail-closed, unfilled)

`scripts/operators/shopify_release_approval.json` is evaluated on every run.
The gate requires, for the requested phase: the record bound to the runtime
inventory (`bound_runtime_inventory_digest`/`count` equal to what the gate
computes), `phase_approvals.<phase>` approved with reviewer, independent review
reference, `approved_at_utc` and `valid_until_utc` (the run time must fall
inside that window), a filled **deployment rollback snapshot** (the deployment
that would be redeployed to withdraw the release — this is *not* database
recovery), filled **backup evidence** (service id, volume id, card timestamp,
size, who observed it and when) observed **before** the approval, and — for
every schema-changing phase S1, S2, S3 — `restore_verified: true` with a
restore verification reference and time.

Facts available today, none of which is entered in the record by this slice:

* The historical catalog backup-reuse approval was explicitly bound to release
  462 and the September-22 backup (589 MB). It is **not** approval for this
  release and is not copied.
* The coordinator reported, as owner-approved and independently verified, a
  fresh backup of the existing `nahla-postgres-prod` service: card timestamp
  `2026-10-11T16:54:10.145Z`, size 647 MB, Restore available, identified by
  service `b77b3d27-47b0-4a3a-83fd-44def66a3a84` + volume
  `009bd0d5-85ed-4de4-99fc-94ea963c9d65` + that timestamp + that size (no
  backup UUID is exposed by the UI). This is **observed completed backup
  evidence** — a candidate value for `db_recovery_evidence.backup` once the
  owner reviews it — and **not** proof of recovery, **not** a verified restore
  and **not** permission to migrate or deploy. `restore_verified` stays
  `false`, so every schema-changing phase stays refused until a restore
  rehearsal is independently verified. Schedule/PITR remain off; nothing was
  restored, exported, deleted or rescheduled.
* No deployment rollback snapshot id is recorded; the owner records the
  current 462 deployment as the withdrawal target when approving S0.

Filling the record is a dedicated approval-only change (no runtime, migration
or gate change in the same PR), after which the command is re-rendered. The
record's binding cannot be satisfied by editing the record alone.

## 7. Transition sequence and atomic ordering (owner-executed, nothing here applies it)

1. **Source**: the accepted Shopify source reaches the ops branch through the
   frozen PR chain; the deploy commit `R` is the exact commit the owner will
   pin. Everything below is rendered from a checkout at `R`.
2. **Approval for S0**: fill the record (runtime digest/count from
   `render … inventory`, rollback snapshot = the current 462 deployment,
   backup evidence as reviewed, `restore_verified: false` is acceptable for
   S0, phase `S0` approval with validity window). Commit as approval-only →
   `R`. `approval-check --phase S0` must print `PASS` offline.
3. **Render** `render --release-sha R --phase S0`. Keep the exact text.
4. **Atomic settings change (owner, single change)**: set the API service's
   pre-deploy command to the rendered text **and** its source pin to `R`
   together. Either partial order fails closed, never bypasses: the new pin
   with the legacy command fails the legacy guard (release/digest/graph); the
   new command with the 462 pin exits 2 because the gate file does not exist in
   that source. Both are failed deploys to avoid, not risks to data.
5. **Deploy at S0**: the gate must print `RESULT=PASS phase=S0`. The new
   source then runs dormant: every `NAHLA_SHOPIFY_*_ENABLED` flag unset, no
   secret, grant, webhook or redirect configured. The dashboard service is
   pinned to `R` afterwards with every `VITE_SHOPIFY_*` flag unset.
6. **Each schema-changing phase S1 → S2 → S3, separately approved**:
   (a) read-only audit proves the exact previous state
   (`scripts/audit_alembic_topology_readonly.py --expect …`); (b) the single
   approved `alembic upgrade 012x` step from the #1211 plan, with an explicit
   `DATABASE_URL`, never from the gate; (c) read-only audit proves the exact
   new state; (d) approval record for the new phase filled (`restore_verified`
   must be true) and committed → `R'`; (e) `render --phase S_n` and the
   atomic pin+command change to `R'`; (f) deploy, gate `PASS phase=S_n`.
   Between (b) and (f) the running release keeps serving; any redeploy in that
   window is refused by the still-configured previous phase — intended, keep
   the window short, and never set a phase ahead of the database.
7. Shopify feature activation (flags, secrets, app configuration, supervised
   journey) is a later slice with its own gate rule; this gate refuses any
   enabled flag in every phase it knows.

## 8. Old-deployment startup, redeploy and rollback compatibility

* **New source at any phase S0..S3**: the application's background bootstrap
  (`alembic upgrade 0093`, pinned by `bootstrap_migration_contract`) is a
  no-op on every phase because `0093` is an ancestor of the retained `0111`
  row — proven on disposable PostgreSQL at S1 and S3 (rows and schema
  unchanged). Runtime behaviour does not depend on the Shopify tables while the
  flags are off.
* **Legacy 462 against S1..S3**: its bootstrap `alembic upgrade 0093` fails
  closed because `0121` is unknown to its script directory (non-fatal to the
  worker, logged as a Step C failure, nothing mutated — proven on disposable
  PostgreSQL with the 462 checkout), and its pre-deploy guard refuses the
  unknown revision. A 462 redeploy therefore **cannot** proceed against a
  Shopify state. The only way back to 462 is **database first**: the branch
  rollback steps `0123@-1`, `0122@-1`, `0121@-1` (each separately approved,
  flags off) until the read-only audit proves exact S0, then the 462 pin and
  the legacy command restored together. S0 is never silently whitelisted for
  462 by the new gate — the new gate does not exist in that source.
* **Deployment rollback snapshot vs database recovery**: redeploying a previous
  deployment withdraws the *release* and is valid only while the database is
  at the phase that deployment's command names; it never restores data. Data
  recovery is the backup/restore path, whose evidence the record tracks
  separately (§6).

## 9. Shopify app configuration plan (not applied; no credential, grant or setting change)

Current shell (app `434031788033`, org `240110284`): `embedded = false`,
`use_legacy_install_flow = false`, default app-home URL, empty scopes and
redirects, webhook API version `2026-10`. Nothing below is configured by code;
each item is an owner action after this slice's independent review.

* **Routes (from the actual routers, dormant until flagged on)**: OAuth
  callback `https://api.nahlah.ai/merchant/integrations/shopify/callback`
  (`CALLBACK_PATH`); dashboard completion
  `https://app.nahlah.ai/integrations/shopify/complete`; `app/uninstalled`
  webhook `https://api.nahlah.ai/webhooks/shopify/app-uninstalled`
  (`WEBHOOK_UNINSTALL_PATH`); compliance webhooks under the plural prefix
  `/webhooks/shopify/privacy/`: `customers-data-request`, `customers-redact`,
  `shop-redact` (`TOPIC_PATHS`).
* **Scope**: `read_products` only — the implemented contract; the exchange
  refuses any grant that is not a subset of the read-only set.
* **Authentication — reviewed choice**: the frozen code implements the
  **authorization-code grant with expiring offline tokens** (ADR 0006).
  Official documentation rechecked by the coordinator on 2026-10-11 states that
  standalone apps cannot use Shopify ID tokens and use the authorization-code
  grant (`shopify.dev/docs/apps/build/authentication-authorization/authenticate-standalone-apps`).
  Token exchange / managed "authenticate.admin" designs are therefore **not**
  used and not silently adopted; `embedded = false` is consistent with this but
  is not the reason for it.
* **Installation / scope source — reviewed choice**: the app configuration
  reference states `use_legacy_install_flow = false` (or omitted) means
  Shopify-managed scope installation, and `true` sends URL scopes through the
  legacy OAuth flow (still supported, not recommended)
  (`shopify.dev/docs/apps/build/cli-for-apps/app-configuration`). The reviewed
  choice is to **keep `use_legacy_install_flow = false`** and declare
  `read_products` as the app's only access scope in the app configuration,
  because: the frozen code's authorize URL already sends `scope=read_products`
  (the identical set, so whichever source Shopify applies the grant is the
  same), the enforcement point is the exchange-time validation (granted ⊆
  read-only) and it is unchanged, and the not-recommended legacy setting is not
  needed by the code. This choice must be **confirmed by the supervised
  journey** (grant screen shown, `read_products`-only expiring offline token
  returned) before activation; if that journey shows the managed path does not
  present the grant for a standalone authorization-code app, the fallback is
  an explicit owner decision to set `use_legacy_install_flow = true` — a shell
  setting, no code change. Nothing is flipped by this slice.
* **API version — reviewed choice**: the connection runtime defaults to
  `2026-07` (`SHOPIFY_ADMIN_API_VERSION` unset; still supported per the
  versioning page, with `2026-10` the latest stable), the catalog import pins
  `2026-10` by code, and the shell's webhook version is `2026-10`. For the S0
  deploy nothing changes (no environment edit is needed for a dormant
  release). At activation, set `SHOPIFY_ADMIN_API_VERSION=2026-10` so the
  identity query, the catalog import and webhook payloads share one version;
  that is an environment change with its own approval, not part of this
  slice.
* **Secrets**: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`, a dedicated
  `SHOPIFY_TOKEN_ENC_KEY` (distinct from every other key; the code refuses a
  reused one) are entered out of band, later, never through a repository or a
  chat. No secret is retrieved or set here.
* **Dev store / tenant**: `nahlah-dev.myshopify.com` has the app **not**
  installed and **no** tenant assigned. The future supervised journey uses a
  **new** regular Nahlah tenant created through Nahlah's own authenticated
  creation path and the tenant id it returns; tenants 67 or 33 are never
  assumed or reused, and no tenant is ever created or bound from a Shopify
  domain or email. Connection/UI/privacy-intake activation needs its own
  review; the privacy worker and destructive executor stay off; durable
  product-import activation is a later slice.

## 10. Validation and coverage limits

Tests actually run, the exact tested head and tree, failures versus blocked or
skipped cases, and the external CI coverage limit are recorded in
`shopify-release-gate-transition-evidence.md`. In outline: the DB-free module
runs anywhere; the PostgreSQL module runs only with an explicit disposable
admin DSN and a non-loopback-spelled host alias for that server (the gate's
target validator refuses loopback hosts, as it must for the real target) and
is reported as skipped otherwise; the legacy-462 cases run only with a checkout
of the deployed commit and are reported as skipped otherwise. Pull requests to
the stacked branch trigger only the GOV-002 trusted-base scanner and the
merge-freeze gate on GitHub; the root CI job runs only on pull requests to
`main`/`master`/the ops base, so the GitHub checks on this Draft do **not**
execute these tests.

## 11. Independent review requirement

Before any merge or deployment: an independent reviewer verifies (1) the
trusted command's binding chain (§3) by re-rendering from the exact deploy
commit and comparing byte for byte; (2) the approval record is filled in a
dedicated change and `approval-check` passes offline; (3) the gate refuses the
unfilled record, every other phase, partial footprints, retained drift, flags,
lock contention, read-only databases and wrong targets (tests in §2);
(4) the backup observation is entered as evidence only and
`restore_verified` is false until a restore rehearsal is independently
verified; (5) the Shopify configuration plan (§9) is confirmed against the
official pages cited by the coordinator before any shell setting changes.

## 12. What this slice does not do

It does not disable or weaken the legacy guard, change Railway settings,
permissions, deployments or resources, create an environment, take a backup,
enable PITR, run or render a migration, change runtime, Salla, Meta, AI,
bootstrap, governance or CI behaviour, retrieve or set a secret, grant OAuth,
install the app, assign a tenant, or enable any Shopify flag or UI. The legacy
inline guard's source is not in the repository; the bounded recommendation is
to archive its exact text under the owner's reviewed operations records (not
this repository) so its digest rule and behaviour remain auditable alongside
this gate.
