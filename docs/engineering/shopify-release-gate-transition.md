# Shopify release gate — deployment guard and source-pin transition on the existing platform (Draft)

Status: **design + implementation for review. Nothing here has been applied.**
No platform setting, pre-deploy command, source pin, variable, secret, backup,
restore, migration, deployment, OAuth grant or Shopify app setting was changed
by this slice. PRs #1209 (`1e9f1fdaf866`) and #1211 (`8308c16e39a1`) stay
frozen; this branch starts from exact `8308c16e39a1` and adds operator files,
tests and documentation only. Every live action below is a later, separately
approved owner step, and the operational approval ships as an **unfilled
template**.

Companion documents: the exact-topology migration plan
(`shopify-multihead-migration-plan.md`, PR #1211), the deployed-compatible port
runbook (`shopify-deployed-port-staging-runbook.md`, PR #1209) and this slice's
evidence (`shopify-release-gate-transition-evidence.md`).

## 1. The operational problem

The existing platform target (API `https://api.nahlah.ai`, dashboard
`https://app.nahlah.ai`, Railway project `f0090862-0a40-4293-bd5d-e94df58762b5`,
environment `ede962ce-3042-4dae-94de-623837e83ed9`, API service
`686b36c5-a926-4e58-912a-5e9d13fbc2e7`, database service
`b77b3d27-47b0-4a3a-83fd-44def66a3a84`, volume
`009bd0d5-85ed-4de4-99fc-94ea963c9d65`, host `postgres-ancu.railway.internal`,
port 5432, database `railway`) deploys commit `462ede1aff60` on the ops branch.
Its configured pre-deploy command is a standalone inline Python catalog guard
that is **not in the repository**. It is pinned to that commit, to a source
digest `12798863bd12348de4407263fd1a1fe86ef639a0df2bfac2fc94244bd1f714c4` over
exactly 132 files, to the graph `0113->0112, 0118->0112, 0120->0118` with the
ancestor delta `{0118, 0120}`, serializes on the advisory key
`sha256(b"nahla:primary:catalog-schema:0120")[:8]` (big-endian signed), pins
`search_path=public,pg_catalog`, and accepts the already-applied state
S0 = `{0111, 0113, 0120}` — or **mutates** `{0111, 0113}` to S0 by running
`alembic upgrade 0120` itself.

The accepted Shopify source adds `0121 -> 0122 -> 0123` on the same branch.
The legacy guard would refuse it on every axis it checks, which is correct.
What is needed is a **new** normal-deployment compatibility gate for the new
source that keeps every legacy safeguard, adds the Shopify-specific ones, and
inherits **none** of the auto-upgrade behaviour: the new gate never upgrades,
stamps, merges or edits `alembic_version`, and it never runs Alembic or any
subprocess.

The owner's read-only audit of the live database (2026-10-11) observed exactly
S0 with the six retained tables of the `0111/0113/0118/0120` branches holding
90 columns, 33 constraints and 18 indexes, five serial sequence ownerships, and
no Shopify-named relation, type, constraint or function. A disposable build
from the accepted source reproduces those figures exactly. The gate
nevertheless verifies reality at execution time: an observation made today is
not an attestation for a later deploy.

## 2. What ships in this slice

| File | Role |
|------|------|
| `scripts/operators/shopify_release_gate.py` | The gate. Read-only, fail-fast, fail-closed. Executed only by the rendered bootstrap. |
| `scripts/operators/shopify_release_gate_contract.py` | Closed constants: deployment phases S0/S3, intermediate states S1/S2, graph edges and heads, inventory rule and forbidden initializers, library pins, identity variables, legacy advisory key and search path, timeouts, flag names, retained-definition pins, approval rules, reason tokens. |
| `scripts/operators/shopify_release_gate_render.py` | Operator tool (never in the container): `inventory`, `render` (the standard-library bootstrap command, readable with `--format source`), `snapshot` (schema expectations from a disposable S3 build), `approval-check`. |
| `scripts/operators/shopify_release_schema_expectations.json` | Canonical definitions — columns with type, nullability, default, identity and generation; every constraint with definition and deferrable/deferred/validated flags (PostgreSQL 18 `contype='n'` rows excluded as redundant with nullability); every index with definition and `indisvalid`/`indisready`/`indislive` — of the six retained tables, the WhatsApp nonce relation (names abstracted), `tenants`/`users` primary-key types, the conversation scope key, retained sequence ownership, **and** the fifteen S3 Shopify tables with their exact sequence set (name, owning table, owning column). Generated from a disposable S3 build; bound to the migration files by digest. |
| `scripts/operators/shopify_release_approval.template.json` | **Unfilled** template of the external approval payload. Not read by the gate, not in the inventory. |
| `tests/test_shopify_release_gate.py` | DB-free contract: trust order (tampered contract, pilot module, migration, initializer and gate are refused by the bootstrap before they can execute — proven with import-time markers), phases, inventory rule, bootstrap round trip, every evaluation, payload rules, AST preflight, static read-only guarantees, CLI and renderer secret safety. |
| `backend/tests/test_shopify_release_gate_pg.py` | Disposable PostgreSQL through the rendered bootstrap: S0/S3 matrix with S1/S2 refused under both, footprint by definition across user schemas, same-name wrong-shaped tables and keys, invalid index flags, malformed rows, retained drift and exact restoration, legacy-key contention and release on failure, lock timeout bound, read-only database, source-side refusals, restore requirement, expectations freshness on PG16 and PG18, legacy-462 bootstrap refusal. |
| `backend/tests/_shopify_release_gate_harness.py` | Synthetic container root, synthetic payload, rendered-bootstrap runner. |

Existing runtime, Salla, Meta, AI/prompt/persona/model/dialect, bootstrap,
governance and CI files are untouched. The gate reuses the pilot job's
validators (`parse_database_url`, `explicit_url`, `sanitized_environment`)
unchanged.

## 3. Trust order and bindings (none circular)

The pre-deploy command is **rendered**, never hand-written:

    python scripts/operators/shopify_release_gate_render.py render \
        --release-sha <deploy commit> --phase S0 --approval-file <filled payload>

It is one line of `python -I -c "…"` that runs a **standard-library-only
bootstrap** (base64-embedded; `--format source` prints it for review). The
bootstrap carries, as literals: the container root, the release commit, the
three platform ids, the libpq target variables to refuse, the fixed inventory
files, the initializers that must be absent, the inventory count and digest,
the gate module's SHA-256, the approval payload's SHA-256, the phase and the
database and backup targets. **Before importing anything from the
repository** it verifies, in order: root; `RAILWAY_GIT_COMMIT_SHA` equals the
release; every platform id; no inherited libpq target variable; no forbidden
initializer; the complete inventory count and digest (computed by the
bootstrap itself); the gate module's digest; the payload's digest. Only then
does it execute the verified gate bytes in the same process. Repository
source imports compile the bytes already hashed, including Alembic's direct
`SourceFileLoader` path, without consulting bytecode caches. A finder refuses
uninventoried repository origins before their loaders run: `scripts.py`,
sourceless `.pyc` files, native package initializers and dependency shadows
inside the container root. Installed dependencies outside that root retain
their normal import mechanism. A tampered contract, pilot module, migration file,
package initializer or gate therefore never executes — not even its module
body — and the tests prove it with import-time side-effect markers.

One inventory, one rule (sorted relative paths, `path NUL bytes NUL`, SHA-256;
the legacy rule reproduced exactly — it yields the legacy digest over the same
132 files at `462ede1aff60`): the legacy files (`requirements.txt`,
`database/alembic.ini`, the pilot job and its contract, every
`database/**/*.py`, which covers the `database` package initializers) plus the
gate, its contract, the schema expectations, `scripts/operators/__init__.py`,
the bootstrap contract and the three Shopify flag modules. `scripts/__init__.py`
must not exist (`scripts` is a namespace package; a planted initializer would
execute on the gate's first import). Nothing in the inventory stores its own
digest: the command carries it.

The approval record is **external** to the source: a JSON payload the owner
holds outside the repository, delivered base64-encoded through the single
service variable `NAHLA_SHOPIFY_RELEASE_APPROVAL_B64` and pinned by SHA-256 in
the command. It binds to the exact deploy commit, the inventory digest and
count, the platform target (project, environment, service, database), the
backup identity (database service and volume) and the phase, so a commit never
has to name itself and a payload for another release, source, target or phase
is refused.

## 4. Checks, in order; the first refusal stops the run

Source-side (no connection): `root`; `release_sha`; `inventory`
(`initializer_unexpected`, `inventory_file_missing`,
`inventory_count_mismatch`, `inventory_digest_mismatch`); `libraries`
(Alembic 1.13.1, SQLAlchemy 2.0.30, psycopg2-binary 2.9.9; distribution
discovery restricted to interpreter `purelib`/`platlib` outside the repository,
with each imported origin recorded by its selected distribution and the
package's actual runtime version matching the pin; repository `.dist-info`
advertisements cannot satisfy this check);
`migration_preflight` (AST only, before the script directory loads any
migration module: every file declares exactly one string `revision` id, a
single string or null `down_revision`, no `depends_on`, no `branch_labels`;
ids unique across files; parents known; unparsable files refused — the legacy
graph refusal made explicit instead of relying on Alembic warnings); `graph`
(every edge `0110->0109, 0111->0109, 0112->0110, 0113->0112, 0114->0113,
0115->0112, 0118->0112, 0120->0118, 0121->0120, 0122->0121, 0123->0122`,
repository heads exactly `{0092, 0111, 0114, 0115, 0123}`, the phase's Shopify
branch delta from `0112`, retained rows `0111`/`0113` on distinct branches that
the Shopify row is not an ancestor of); `identity`; `database_target`
(`libpq_environment_override`, then the pilot job's parser: query overrides,
loopback, dialect, exact host/port/database); `approval` (§6);
`shopify_flags` (every `NAHLA_SHOPIFY_*_ENABLED` and the dashboard's
`VITE_SHOPIFY_CONNECTION_UI` / `VITE_SHOPIFY_CATALOG_IMPORT_UI` unset or false
in the checked environment); `expectations` (bound to the migration files
present).

Database-side, one connection (NullPool, `echo=False`,
`hide_parameters=True`, library logging disabled, `connect_timeout=5`,
`statement_timeout=15000`, `lock_timeout=3000`,
`idle_in_transaction_session_timeout=20000`, `search_path=public,pg_catalog`,
read-only transaction, wall-clock alarm 120 s): `connect`; `lock`
(`pg_try_advisory_lock(8182734029736770447)` — the legacy key, so a
cooperating legacy guard, operator that actually acquires this key, or second
gate makes this deploy refuse rather than wait); `server`
(`pg_is_in_recovery()` false, effective `SHOW default_transaction_read_only`
exactly `off`, including server-wide settings, independently of the guard's
intentional read-only observation transaction; `search_path`
exactly the pinned one, current schema `public`, current database equal to
the target); `version_rows` (exactly the phase's rows; table missing,
duplicates, unknown ids and any other multiset are refusals; unknown values
are counted as `<unknown>`, never echoed); `shopify_footprint` (every
`shopify_*` relation, type or function and every `*shopify*` constraint in
**every user schema**: exactly the phase's public tables — none at S0, the
fifteen at S3 — with each S3 table compared by canonical definition against
the pinned expectations, which include `uq_shopify_connections_id_tenant` as
`UNIQUE (id, tenant_id)`; every sequence that is Shopify-named **or** owned
by an expected table, in any user schema, must equal the pinned S3 sequence
set exactly by name, owning table and owning column — an extra sequence,
even one `OWNED BY` an expected table, or a pinned sequence with another
owner is a refusal; unexpected objects are counted, never named);
`retained_schema` (the six retained tables, the nonce relation, the integer
primary keys, the conversation scope key, sequence ownership — drift reported
by allow-listed table name only); `relation_identity` (the OIDs of every
checked relation read again in a fresh transaction at the end of the run must
equal those read at the start); `unlock` (`pg_advisory_unlock` must return
true; otherwise the run fails and the connection is invalidated). Every
failure path rolls back and releases the key; a lock-timeout refusal was shown
to leave the key immediately acquirable by another session.

Output: `BOOTSTRAP PASS|FAIL reason=<token>`, then `CHECK <name> PASS|FAIL
reason=<token> {closed detail}` lines and one `RESULT=PASS|FAIL`. Exit 0 only
on PASS; 1 on any FAIL; 2 on a bootstrap refusal or an invalid invocation (any
phase other than S0 or S3 included). No DSN, environment value, argument
value, row value, database-chosen object name or exception body is ever
printed; library exceptions are reported by type.

The advisory key does not serialize plain Alembic migration commands. The
#1211 manual upgrade blocks do not acquire it. Retained OID comparisons catch
relation replacement, not every concurrent schema change: an `upgrade 0121`
can commit after S0 row/footprint observations without changing retained OIDs.
PASS is therefore an observation during a controlled window, not proof of
automatic deploy/migration exclusion. The enforced deployment freeze in
section 7 is mandatory throughout the manual migration window.

The expectations snapshot command refuses inherited libpq target variables
and validates the effective driver target with the same strict parser before
opening a connection. Query overrides (including `host`, `hostaddr`, `dbname`,
`port`, `service`), malformed targets and the existing platform database host
are refused before `_engine`; an accepted disposable target is bound to an
explicit single host, port and database. All comma-separated authority host
lists are refused, including a failover list with the existing database host;
its DNS root-dot spelling is refused too. No forbidden-target probe is made.

## 5. Source / state / phase matrix

Normal deployment is allowed in exactly two states. S1 and S2 are states of
the separately approved manual migration window, verified there by the
read-only audit script (#1211 plan), never by a deploy.

| Deployed source | Database state | Legacy inline guard (462) | New gate (this slice) | Outcome |
|---|---|---|---|---|
| `462ede1aff60` (legacy) | S0 `{0111,0113,0120}` | PASS (already applied) | not present in that source; the bootstrap refuses (`gate_digest_mismatch` / inventory) | today's production |
| `462ede1aff60` | `{0111,0113}` | **mutates** to S0 | n/a | the behaviour the new gate does not inherit |
| `462ede1aff60` | S1 / S2 / S3 | FAIL — `0121+` unknown to its graph; its bootstrap `alembic upgrade 0093` fails on the unknown revision without mutating (proven on disposable PostgreSQL with the 462 checkout at S1, S2 and S3) | n/a | **the old release cannot be redeployed against any Shopify state**; DB-first rollback to S0 required (§8); never whitelisted |
| accepted source, command phase `S0` | S0 | FAIL (release/digest/graph) | PASS | the only deploy before the migration window: dormant, flags OFF |
| accepted source, phase `S0` | S1 / S2 | — | FAIL `alembic_version_rows_mismatch` + `shopify_footprint_unexpected` | no normal deploy inside the window |
| accepted source, phase `S3` | S1 / S2 | — | FAIL `alembic_version_rows_mismatch` + `shopify_footprint_missing` | no normal deploy inside the window |
| accepted source, phase `S1` / `S2` | any | — | exit 2 `phase_not_deployable` (render refuses too) | not a deployment phase |
| accepted source, phase `S3` | S3 `{0111,0113,0123}`, fifteen tables by definition, key present | — | PASS | the only deploy after the window |
| accepted source, phase `S0` | S3 | — | FAIL rows + footprint | the 462 state is never whitelisted against S3 |
| any phase | partial footprint, same-name wrong-shaped table or key, extra or re-owned sequence, stray `shopify_*` object in any user schema, duplicate/unknown/missing row, retained drift, invalid index, flag on, legacy key held, read-only DB, wrong identity/target, stale or foreign approval | — | FAIL (named token) / bootstrap refusal | deploy refused |

Never used: `head`, `heads`, `stamp`, `merge`, version-row edits. The only
future rollback spellings remain `0123@-1`, `0122@-1`, `0121@-1`, each with
Shopify features off and its own approval.

## 6. Approval payload (external, fail-closed, unfilled template)

The gate requires, for the requested phase, a payload that: has
`record_version` 2; names exactly the deployed commit (`release_sha`), the
inventory digest and count, the platform target (`project`, `environment`,
`service`, `db` as `host:port/database`) and the phase; carries an
`approval` that is approved with reviewer, independent review reference,
`approved_at_utc` and `valid_until_utc` with the run time inside that window;
a filled **deployment rollback snapshot** (the deployment to redeploy to
withdraw the release — *not* database recovery) with a UUID deployment id, a
full 40-hex source sha, a verifier and a UTC verification time ordered
`verified ≤ approved ≤ now`; **backup evidence** for exactly the expected database service and
volume, with a positive sane size (0 < MB ≤ 10 000 000), `completed: true`,
`restore_available: true`, an observer, and UTC timestamps ordered `card ≤
observed ≤ approved ≤ now ≤ expiry`, the card no older than **72 hours** at
approval time **and** at run time (`BACKUP_MAX_AGE_HOURS`; a later deploy
needs a fresher backup); and, for the schema-changing phase **S3**,
`restore_verified: true` with a reference and a verification time between the
backup card and the approval. Anything else is a named refusal (ancient,
wrong-target, incomplete, non-restorable or mis-ordered backup; future,
malformed or out-of-order times; unverified restore; foreign release, source,
target or phase).

Facts available today, none of which is entered anywhere by this slice:

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
  and **not** permission to migrate or deploy. Under the 72-hour rule it can
  only support an approval made before `2026-10-14T16:54:10Z` and a deploy
  before that same moment. `restore_verified` stays `false`, so S3 stays
  refused until a restore rehearsal is independently verified. Schedule/PITR
  remain off; nothing was restored, exported, deleted or rescheduled.
* No deployment rollback snapshot id is recorded; the owner records the
  current 462 deployment as the withdrawal target when approving S0.

The template ships unfilled and is not part of the inventory; filling it
happens outside the repository, and the payload cannot change without a
re-rendered command (its SHA-256 is pinned there).

## 7. Transition sequence and atomic ordering (owner-executed; nothing here applies it)

1. **Source**: the accepted Shopify source reaches the ops branch through the
   frozen PR chain; the deploy commit `R` is the exact commit the owner will
   pin. Everything below is rendered from a checkout at `R`.
2. **Approval for S0** (outside the repository): copy the template, fill it
   for `R`, the inventory digest/count printed by `render … inventory`, the
   platform target, phase `S0`, the current 462 deployment as rollback
   snapshot, the reviewed backup evidence (`restore_verified: false` is
   acceptable for S0), the approval window. `approval-check` must print
   `PASS` offline.
3. **Render** `render --release-sha R --phase S0 --approval-file <payload>`;
   keep the exact text; review `--format source`.
4. **Atomic settings change (owner, single change)**: set the API service's
   pre-deploy command to the rendered text, the variable
   `NAHLA_SHOPIFY_RELEASE_APPROVAL_B64` to the base64 payload, and the source
   pin to `R`, together. Every partial order fails closed, never bypasses: the
   new pin with the legacy command fails the legacy guard; the new command
   with the 462 pin exits 2 at the bootstrap (no gate file, inventory
   mismatch); a missing or stale variable exits 2 at the bootstrap. These are
   failed deploys to avoid, not risks to data.
5. **Deploy at S0**: `BOOTSTRAP PASS` then `RESULT=PASS phase=S0`. The new
   source runs dormant: every Shopify flag unset, no secret, grant, webhook or
   redirect configured. The dashboard service is pinned to `R` afterwards
   with every `VITE_SHOPIFY_*` flag unset — the API gate cannot observe
   another service's compiled Vite configuration, so that needs the owner's
   separate evidence.
6. **Migration window S0 → S3, one separately approved manual operation, no
   normal deploy inside it**: before the first audit, the owner must enforce
   exclusion across every automated and manual deployment actor for the API
   and dashboard: block deployment triggers and manual deployment access,
   drain or cancel queued deployments, and verify no in-flight deployment or
   pre-deploy remains. Record the exclusion controls and that drain evidence.
   A warning to avoid deployments is insufficient. If exclusion cannot be
   enforced and verified, do not start the window. Keep it enforced through
   every upgrade, audit and any rollback: (a) read-only audit proves exact S0; (b) the
   three one-step blocks of the #1211 plan (`upgrade 0121`, audit exact S1,
   `upgrade 0122`, audit exact S2, `upgrade 0123`, audit exact S3), each with
   an explicit `DATABASE_URL`, never from the gate; any failure inside the
   window is rolled back branch-qualified (`0123@-1`, `0122@-1`, `0121@-1`)
   to exact S0 with Shopify features off. While the database is at S1 or S2
   the configured S0 command refuses every redeploy and restart with
   pre-deploy, by design. That refusal is a secondary safeguard, not the
   exclusion mechanism; the plain migration blocks do not acquire the gate's
   advisory key.
7. **Approval for S3** (requires a verified restore rehearsal and a backup
   observation within 72 hours), `render --phase S3`, the atomic change of
   command + variable (+ pin if `R` changed). Keep the freeze until the final
   S3 audit, reviewed approval, source pin, gate command and payload are all
   ready; then allow only the supervised deployment: `RESULT=PASS phase=S3`.
   Re-enable other deployment actors only after its checks succeed. If the
   window rolls back to S0, retain exclusion until the S0 audit and matching
   command, payload and source pin are restored and reviewed.
8. Shopify feature activation (flags, secrets, app configuration, supervised
   journey) is a later slice with its own gate rule; this gate refuses any
   enabled flag in every phase it knows.

## 8. Old-deployment startup, redeploy and rollback compatibility

The disposable tests exercise only Alembic Step C (`upgrade 0093`). They do
not run the full startup path: Salla cleanup, `create_all`, and safe alters
remain unverified at each Shopify state. Reports of unchanged rows/schema
below apply to Step C only. Full startup compatibility is not established by
the gate or those tests. Its experiment is separate from the essential guard
suite and optional for guard review; a later rollout decision must explicitly
assess the unverified startup steps rather than infer compatibility from Step C.

* **New-source Step C**: the application's background bootstrap
  (`alembic upgrade 0093`, pinned by `bootstrap_migration_contract`) is a
  no-op because `0093` is an ancestor of the retained `0111` row — proven on
  disposable PostgreSQL at S1 and S3 (rows and schema unchanged). Runtime
  behaviour with flags off still requires the full startup compatibility
  verification described above before any rollout.
* **Legacy-462 Step C against S1, S2 or S3**: its bootstrap `alembic upgrade 0093`
  fails closed because the Shopify revision is unknown to its script directory
  (non-fatal to the worker, logged as a Step C failure, nothing mutated —
  proven with the 462 checkout at every Shopify state), and its pre-deploy
  guard refuses the unknown revision. A 462 redeploy therefore **cannot**
  proceed against a Shopify state. The only way back to 462 is **database
  first**: the branch rollback steps to exact S0 (read-only audit), then the
  462 pin and the legacy command restored together. The new gate never
  whitelists S0 for 462: the gate does not exist in that source.
* **Deployment rollback snapshot vs database recovery**: redeploying a previous
  deployment withdraws the *release* and is valid only while the database is
  at the phase that deployment's command names; it never restores data. Data
  recovery is the backup/restore path, whose evidence the payload tracks
  separately (§6).

## 9. Shopify app configuration plan (not applied; no credential, grant or setting change)

Current shell (app `434031788033`, org `240110284`): `embedded = false`,
`use_legacy_install_flow = false`, default app-home URL, empty scopes and
redirects, webhook API version `2026-10`. Nothing below is configured by code;
each item is an owner action after this slice's independent review.

* **URLs (proposed, from the actual routers; dormant until flagged on)**:
  proposed App URL `https://app.nahlah.ai/integrations` (Nahlah's own
  authenticated integrations page; the shell currently carries the default
  app-home URL); the **only** OAuth allowlist redirect
  `https://api.nahlah.ai/merchant/integrations/shopify/callback`
  (`CALLBACK_PATH`, registered byte-exactly); the dashboard completion URL
  `https://app.nahlah.ai/integrations/shopify/complete` is where the callback
  sends the merchant's browser afterwards and is explicitly **not** another
  OAuth redirect — it must not be added to the allowlist; `app/uninstalled`
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
  Token exchange / managed `authenticate.admin` designs are therefore **not**
  used and not silently adopted; `embedded = false` is consistent with this but
  is not the reason for it.
* **Installation / scope source — conditional proposal, not yet proven**:
  the app configuration reference states `use_legacy_install_flow = false`
  (or omitted) means Shopify-managed scope installation, and `true` sends URL
  scopes through the legacy OAuth flow (still supported, not recommended)
  (`shopify.dev/docs/apps/build/cli-for-apps/app-configuration`). The
  proposal is to **keep `use_legacy_install_flow = false`** and declare
  `read_products` as the app's only access scope in the app configuration,
  because the frozen code's authorize URL already sends `scope=read_products`
  (the identical set) and the enforcement point — exchange-time validation
  that the granted scopes are a subset of the read-only set — is unchanged
  either way. Whether a standalone authorization-code app receives its grant
  correctly under managed installation with this shell has **not** been
  exercised by any journey in this program, and the frozen code was written
  against the legacy-style flow; so this is genuinely uncertain. The
  supervised journey must prove it (grant screen shown, `read_products`-only
  expiring offline token returned, `scope` on the token response exactly
  `read_products`). If it does not, the fallback is an explicit owner decision
  to set `use_legacy_install_flow = true` — a shell setting, no code change —
  and that fallback is as acceptable as the proposal. Nothing is flipped by
  this slice.
* **API version — reviewed choice**: the connection runtime defaults to
  `2026-07` (`SHOPIFY_ADMIN_API_VERSION` unset; still supported per the
  versioning page, with `2026-10` the latest stable), the catalog import pins
  `2026-10` by code, and the shell's webhook version is `2026-10`. For the S0
  deploy nothing changes. At activation, setting
  `SHOPIFY_ADMIN_API_VERSION=2026-10` so the identity query, the catalog
  import and webhook payloads share one version is the proposal; the
  `2026-10` activation configuration is **untested** in this program and is
  an environment change with its own approval, not part of this slice.
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
skipped cases, retained diagnostic (pre-freeze) notes and the external CI
coverage limit are recorded in `shopify-release-gate-transition-evidence.md`.
In outline: the DB-free module runs anywhere (it spawns the rendered
bootstrap against synthetic roots and an unroutable address); the PostgreSQL
module runs only with an explicit disposable admin DSN and a
non-loopback-spelled host alias for that server (the gate's target validator
refuses loopback hosts, as it must for the real target) and is reported as
skipped otherwise; the legacy-462 cases run only with a checkout of the
deployed commit and are reported as skipped otherwise. Pull requests to the
stacked branch trigger only the GOV-002 trusted-base scanner and the
merge-freeze gate on GitHub; the root CI job runs only on pull requests to
`main`/`master`/the ops base, so the GitHub checks on this Draft do **not**
execute these tests.

## 11. Independent review requirement

Before any merge or deployment: an independent reviewer verifies (1) the
bootstrap's binding chain (§3) by re-rendering from the exact deploy commit
and comparing byte for byte, and by reading `--format source`; (2) the
payload is filled outside the repository, `approval-check` passes offline and
the pinned SHA-256 matches the variable's content; (3) the refusals (tampered
module before execution, unfilled template, S1/S2, other phase, partial or
wrong-shaped footprint, retained drift, flags, legacy-key contention,
read-only database, wrong target, stale/foreign/ancient-backup payload) —
the tests in §2; (4) the backup observation is entered as evidence only and
`restore_verified` is false until a restore rehearsal is independently
verified; (5) the Shopify configuration plan (§9) is confirmed against the
official pages cited by the coordinator before any shell setting changes;
(6) the effective read-only default, source-only import controls and snapshot
target refusals are tested on the exact proposed source; (7) full startup
compatibility status is recorded separately from Step C, with any optional
startup experiment labelled as such; (8) deployment exclusion
is enforceable and its drain evidence is recorded before the manual window.
Prior PostgreSQL runs on an earlier guard do not establish these checks on
the modified guard; the current evidence document distinguishes them.

## 12. What this slice does not do

It does not disable or weaken the legacy guard, change Railway settings,
variables, permissions, deployments or resources, create an environment, take a
backup, enable PITR, run or render a migration, change runtime, Salla, Meta,
AI, bootstrap, governance or CI behaviour, retrieve or set a secret, grant
OAuth, install the app, assign a tenant, or enable any Shopify flag or UI. The
legacy inline guard's source is not in the repository; its digest rule and its
advisory key were reproduced from the owner's description and verified against
the deployed checkout, and the bounded recommendation is to archive its exact
text under the owner's reviewed operations records (not this repository) so
its behaviour remains auditable alongside this gate.
