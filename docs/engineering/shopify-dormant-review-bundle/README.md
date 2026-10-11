# Existing Nahlah dormant rollout bundle — review only

**Nothing here is authorized for execution or has been applied.** This bundle
prepares the exact existing API/dashboard source and configuration changes and
the controlled Shopify schema window for review. It creates no environment,
does not merge a PR, and configures no Shopify app, credential or active feature.

Selected immutable source **R** is
`e345ecf6f637b54b9c26d95906fb6143e0a48f09`, on the now-frozen
`codex/shopify-callback-service-key-20261011` branch. Its runtime and proof
files are byte-identical to tested `47d2dcb2`; subsequent changes are evidence
docs only. Independent review accepted the callback fix with no functional or
security blocker. R also clarifies the saved-log representation: canonical
BOM-free/LF digest is `eec4624c6057ad29860429446f14a9bcc951a151d493ccc4c39d4372a3d64293`.

The accepted proof set is carried: essential guard run `38171916514`, startup
repair run `38176146625`, production ASGI comparison `38178126550` and the
passing callback-fix run `38179310679`. No test suite is rerun for this bundle.

## Exact existing target and current observation

Read-only Railway metadata was refreshed at **2026-10-11 22:29:14 UTC**. API and
dashboard remain at `462ede1aff602cad84e9569be859cca0087b190d`; no patch is staged.
The API already uses **DOCKERFILE / Dockerfile**, matching the real Python 3.11
proof, so no builder migration is proposed. Dashboard uses **RAILPACK**, root
`dashboard`; its existing preview start command is retained.

| Resource | Exact identity |
|---|---|
| Project | `f0090862-0a40-4293-bd5d-e94df58762b5` |
| Existing production environment | `ede962ce-3042-4dae-94de-623837e83ed9` |
| API, `api.nahlah.ai` | `686b36c5-a926-4e58-912a-5e9d13fbc2e7` |
| Dashboard, `app.nahlah.ai` | `446edf49-05bf-4578-b403-72e1e86b384c` |
| PostgreSQL service / backup target | `b77b3d27-47b0-4a3a-83fd-44def66a3a84` |
| PostgreSQL volume | `009bd0d5-85ed-4de4-99fc-94ea963c9d65` |
| Driver target, no credentials | `postgres-ancu.railway.internal:5432/railway` |
| Current API rollback deployment / snapshot | `cb02ce9e-dfdd-404d-b26c-5fb4d22bf1ff` / `40f812e4-8672-43f9-9b17-234d476dd0ce` |
| Current dashboard rollback deployment / snapshot | `5a31b5ec-309b-445e-9162-c4f9520cb3c6` / `4925b913-2d0a-40a6-aa4a-3f3c3f026e97` |

Both rollback deployments were reported SUCCESS, redeployable and rollbackable.
Their exact live configuration, including the old API command, is retained in
an owner-held local private snapshot. The public metadata file carries its
command hash; it does not copy the historical owner's inline approval record.
That old command pins 462 and a legacy backup-reuse record. It is incompatible
with R and provides no authorization for this Shopify release.

Only variable names/configuration metadata were read. **Effective variable
values, live DB/version/schema state, current backup/restore evidence, queued
work and actor exclusion have not been verified by this bundle.** Re-observe
them at action time; no staged patch is not proof that deployment actors are frozen.
The configured PostgreSQL image tag is 18; live server version was not queried.

## Artifacts and their fail-closed status

* `inventory.git-bytes.json`: canonical Git-byte inventory for R, **143 files**,
  digest `1d3a50f171b155b5615b30e020f5400e831bb194ce9c8eaf867a1707bf396ce4`.
  This matches accepted Linux CI. It is not computed from CRLF Windows files.
* `approval.S0.UNAPPROVED.json` and `approval.S3.UNAPPROVED.json`: source,
  inventory, target, backup service/volume and rollback deployment are filled;
  owner approval/times, fresh backup and restore fields remain unset/false.
  The unchanged contract returns **FAIL `approval_missing`** for both.
* `bootstrap.*.NOT_AUTHORIZED.preview.py` and `predeploy.*.NOT_AUTHORIZED.preview.txt`:
  readable and one-line stock-renderer previews, pinned to those actual
  unapproved payload bytes. They are parsed/decoded offline, never executed.
  The pure formatting API is used for review; the strict renderer CLI refuses
  to produce an approved command from these records. Runtime approval refusal
  precedes every DB check. These previews must not be installed.
* `api.S0.NOT_AUTHORIZED.patch.preview.json` and the S3 counterpart: exact native
  configuration preview for only the API. Source branch/SHA, matching phase
  predeploy command, sealed external approval payload and all five backend
  Shopify flags are changed together. Builder, start command, existing provider
  credentials, domains, volumes, regions/replicas and other variables are retained.
* `dashboard.NOT_AUTHORIZED.patch.preview.json`: separate source R pin and
  both compiled Vite Shopify flags `0`, retaining builder/root/start settings.
  The API gate cannot attest another service's compiled flags.
* `migration.NOT_AUTHORIZED.plan.json`: exact before/after row sets, six explicit
  forward/branch-qualified rollback argv entries and per-step audits. It is
  a plan, not an automation or an approved execution record.
* `authorizations.json`: existing owner scope and the remaining precise live
  approvals, plus separately unapproved app/activation configuration.

The phase previews have different payload hashes/commands. S0 and S3 are not
interchangeable. Approved payloads must be filled and stored **outside the
repository**, not edited into these committed templates. After approval,
rerender with the unchanged CLI, using exactly R and its canonical checkout:

```text
python scripts/operators/shopify_release_gate_render.py render --root <exact-R-checkout> --release-sha e345ecf6f637b54b9c26d95906fb6143e0a48f09 --phase S0 --approval-file <owner-held-approved-S0.json> --project f0090862-0a40-4293-bd5d-e94df58762b5 --environment ede962ce-3042-4dae-94de-623837e83ed9 --service 686b36c5-a926-4e58-912a-5e9d13fbc2e7 --db postgres-ancu.railway.internal:5432/railway --backup-service b77b3d27-47b0-4a3a-83fd-44def66a3a84 --backup-volume 009bd0d5-85ed-4de4-99fc-94ea963c9d65
```

S3 uses its own approved file and `--phase S3`. Recompute/pin exact payload bytes
and command together; changing any filled field changes the payload hash.
The local canonical export used for generation is only the gate's byte inventory,
not a deployable app checkout. No preview source or gate was executed.

## Concrete sequence after exact bundle and execution approval

1. **Dormant S0 release.** Fresh read-only checks must confirm source/deployment,
   exact target, three version rows `{0111,0113,0120}`, expected retained schema/
   OIDs and no Shopify collision/legacy footprint. Reverify eligible completed,
   restore-available backup and both rollback builds. The historically observed
   2026-10-11 16:54:10.145 UTC / 647 MB card can be reused only if re-observed
   eligible; a new backup is not required merely because a previous observation
   exists. S0 allows `restore_verified=false`; a backup must still be within
   72 hours at approval and runtime. Resolve authorization for unchanged normal
   startup provider side effects; the scheduler kill switch alone does not
   suppress early Meta/Moyasar work, and no suppression redesign is proposed.
   Fill the S0 record, rerender and independently review the final command/payload.
   Apply source/command/payload/flags as one reviewed atomic API change, not separate
   live setters that each redeploy. Inspect the exact staged diff only after such
   staging is authorized. Approve dashboard pin/build separately with Vite flags
   `0`. Observe terminal deployments, source/build/runtime manifest, gate PASS,
   actual startup DB-task completion and health; liveness alone is insufficient.
2. **Recovery and migration window.** Approve the selected actual backup's
   restore rehearsal on an owner-selected private disposable target. That target
   is presently unfilled; none is created here. Use a private target with the
   verified production PostgreSQL major and provider egress blocked; do not boot
   app workers on restored credentials. On that approved restored clone,
   rehearse the same forward/branch-qualified rollback sequence and compare
   logical data/schema plus within-clone retained identities. Verify restore and
   recovery evidence before schema change. Explicitly authorize the existing production
   target, source R, commands and recovery sequence. Enforce exclusion of every
   manual/automatic API/dashboard deployment actor, drain queued/inflight
   predeploys, and verify no DB restart/deployment conflict. The advisory key
   `8182734029736770447` coordinates cooperating holders only; these plain
   migration blocks do not acquire it. Keep every Shopify flag off.
3. **One-step migrations, separate audits.** From `/app/database`, run only the
   approved `python -m alembic upgrade 0121`, audit exact S1 `{0111,0113,0121}`;
   then `upgrade 0122`, audit S2 `{0111,0113,0122}`; then `upgrade 0123`, audit
   S3 `{0111,0113,0123}`. `0111` and `0113` stay retained. Audit topology before
   and after every step using `scripts/audit_alembic_topology_readonly.py --expect
   <exact rows>` from `/app`, and check expected definitions/constraints, retained
   identities and footprint using the accepted schema/audits. Any mismatch or
   nonzero exit stops in place with exclusion retained. No normal deployment at
   S1/S2; no `head(s)`, stamp, merge or direct version edits; Google 0124 is excluded.
4. **Dormant S3 release.** Bind a fresh S3 external record to R, inventory,
   target, rollback and the eligible backup plus **verified restore** (true,
   reference and timestamp). Rerender/review the S3 command/payload before allowing
   the supervised deployment. Verify gate/schema/source/startup/health before
   releasing deployment exclusion. Source/command/payload must remain consistent.
5. **Failure/withdrawal.** Preserve exclusion. Execute only the applicable
   separately approved `0123@-1` → audit S2, `0122@-1` → audit S1, `0121@-1` →
   audit S0 sequence. Reverify retained definitions/OIDs and exact S0. Only then
   restore the matched legacy 462 source/branch/old command and retained rollback
   deployment, or the approved R/S0 command/payload. Redeploying old 462 against
   Shopify revisions is unsupported. No automatic downgrade or data deletion is
   authorized by this bundle.

## Authorization boundary

Owner authorization already covers relevant Claude use within existing capacity/
credit limits, reversible local implementation, Draft PRs, public synthetic GitHub
CI and this offline evidence/bundle work. Independent review has accepted the
code; those steps need no repeated owner confirmation or full guard test cycles.

The remaining action-time permissions are exactly the S0 API/dashboard changes,
selected private restore rehearsal and controlled migration/recovery window,
and the S3 command/payload/deploy before exclusion release. None has been granted
by code acceptance or the historical legacy command. No environment, service,
purchase, provider credential or live app setting is changed here.

Shopify app/grant URLs, private client/encryption values, version alignment,
activation policy, fresh authenticated dev-store merchant journey, inventory
reauthorization and public privacy/distribution operations are **later separate
scope**, enumerated in `authorizations.json`. Current gate rejects enabled flags.
This dormant bundle does not authorize installation or activation, Meta/WhatsApp
work, customer/order/payment scopes, or irreversible distribution.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
