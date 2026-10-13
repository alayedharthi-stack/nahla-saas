# Production S3 approval candidate — 2026-10-13

**Prepared read-only. No production S3 approval, DDL, deployment or configuration
change has occurred.** Fresh production checks passed at 14:22 and 14:30 UTC;
Railway showed no staged changes at 14:34:05 UTC. The private clone prerequisite
independently passed at `cf01b8bd948fc8007684361006f9bf4864f3c31f`.

## Exact bindings

| Item | Binding |
| --- | --- |
| Release | `a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec` |
| Inventory | 146 files; `31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174` |
| Project / environment | `f0090862-0a40-4293-bd5d-e94df58762b5` / `ede962ce-3042-4dae-94de-623837e83ed9` |
| API service | `686b36c5-a926-4e58-912a-5e9d13fbc2e7` |
| Original database | `postgres-ancu.railway.internal:5432/railway` |
| Original DB service / volume | `b77b3d27-47b0-4a3a-83fd-44def66a3a84` / `009bd0d5-85ed-4de4-99fc-94ea963c9d65` |
| Current API deployment | `67f2a118-5733-41c8-90fc-a25bee392022`, SUCCESS, same release, provider says redeployable |
| Selected logical snapshot | `2026-10-13T08:18:50Z`, completed `08:18:55Z`, 26,077,103 bytes |
| Immutable archive SHA256 | `5d677073e9a55d6d44c1d65a808f07d7b085802d8a241091919d66f1ec2fe63c` |
| Actual archive service / volume | `6afcbd3e-15dd-46c1-9183-932fe02d075f` / `2f25c7cf-f0b1-467b-bb98-e203ec40e6d9` |
| Archive path | `/recovery/shopify-logical-20261013/nahla.dump`, mode 0400 |

The database target is the original production database. The archive identifiers
are its **actual storage location**, not the original production volume.

## Recovery binding accepted by independent review

The unchanged gate defaults to the original DB service/volume, and its template
describes `backup.card_timestamp_utc` as a native backup-card timestamp. The
selected, restore-tested artifact is a logical archive on a different retained
service/volume. Relabeling it as the older native 647 MB volume card would be
false.

The proposed payload therefore labels its kind, source DB, actual storage,
snapshot time, completed time, bytes and immutable hash separately. Its command
explicitly sets `--expect-backup-service` and `--expect-backup-volume` to the
archive service/volume. The legacy `card_timestamp_utc` field is openly identified
as the logical snapshot timestamp. These are supported command inputs; production
guard code, pinned expectations and the 146-file inventory are unchanged.

Independent review accepted this exact mapping: the private archive identifiers
are valid explicit backup inputs to the unchanged guard, while the original DB
identity, archive hash and timestamps remain separately bound. This acceptance
is distinct from the clone test result. The new owner approval must still
explicitly select the archive. Offline shape/chronology checks pass under a
simulated approval; that simulation was not persisted or used against production.
If review requires a native card instead, stop and obtain a separate plan. Do not
substitute another artifact or alter the guard to make this bundle pass.

The production gate validates the pinned external record, identifiers and
freshness; it does not fetch or checksum the remote archive itself. The archive
hash is bound through the whole-payload hash and the independently retained
verification evidence. The last actual archive verification was 14:08:10 UTC.
The gate's 72-hour age limit for this selected snapshot ends on
`2026-10-16T08:18:50Z`; an execution window must finish before that limit.

Restoration of this archive would rewind data to **08:18:50 UTC** and may lose
later writes. **No archive restore, production-volume restore or disaster recovery
execution is included in this proposal.** Such action requires separate,
incident-specific authorization. Clone restoration and DDL rehearsal do not prove
production S3 startup or the complete production disaster recovery procedure.

## Payload and command status

- [S3 payload](s3-approval.unsigned.json), [matching command](s3-command.unsigned.txt),
  [readable bootstrap](s3-bootstrap.unsigned.py).
- [S0 rollback payload](s0-rollback-approval.unsigned.json),
  [matching command](s0-rollback-command.unsigned.txt).
- [Manifest with hashes](manifest.json) and [fresh preflight](fresh-s0-preflight.json).

Both payloads currently have `approved: false` and no approval timestamps. Their
commands bind those exact unsigned bytes; strict approval validation correctly
returns `FAIL / approval_missing`. **Do not install or execute candidate commands.**
After actual main-chat approval, the local-only
[finalizer](finalize_after_owner_approval.py) sets the real approval time, 30-minute
expiry and evidence references, validates both payloads using the unchanged
renderer, then regenerates their matching commands and two-field API patches.
It performs no network, database, staging or deployment operation. The actual
signed payload/command hashes must be recorded before any production change.

## Practical window and owner prerequisite

Use a 30-minute window starting at the actual new approval time **T**. Begin by
T+5 minutes; stop new forward work by T+20; reserve the final ten minutes for a
bounded rollback or safe stop. If prerequisites are not ready by T+5, do no DDL
and obtain a fresh binding for another window. No action extends an expired
approval automatically.

The owner must approve this scope in the **main chat**, explicitly accept the
selected logical archive and its recovery limitation, and confirm that no team
member or automation will deploy or migrate the API, dashboard or original DB
during the window. Check native pending work again. The planned same-release API
validation deployment and, if required, same-source API validation after S0
rollback are the only deployment exceptions. Any platform approval
prompt must also be surfaced in the main chat; none has been requested here.

The operator enforces the 30-minute execution window, deployment exclusion,
empty-Shopify-table prerequisite, rollback scope and checks before each step.
The approval record's extra fields document these conditions; they do not add
enforcement to the unchanged release guard. The final owner text explicitly
covers the matching S0 rollback payload/command and a same-source API validation
redeploy if required after a qualifying rollback. Owner S3 approval has not yet
been requested.

All five backend Shopify flags and both dashboard flags remain OFF throughout.
Do not change AI/runtime/persona/model settings, Salla behavior, dashboard build,
secrets, domains, permissions, other services, or source branch/release. Existing
business traffic may continue; no claim of protected row-value equality is made
across concurrent live traffic. DDL has a 3-second lock timeout and 120-second
statement timeout. Any error stops progression and triggers a fresh phase audit.

## Exact execution and audits — only after new approval

Use the existing official Railway SSH relay to the bound API service. Do not
copy or print `DATABASE_URL`; the identity-bound read-only auditor validates its
driver host, port and database before connecting. No new service or access grant
is needed. The source branch stays frozen at the release above.

1. Repeat current API/dashboard source and flags, library/inventory, health,
   catalog integrity and empty pending-work checks within five minutes of DDL.
   Obtain a new S0 metadata baseline using the supplied auditor:

   ```powershell
   py run_production_phase_audit.py --phase S0
   ```

   Retain the resulting local `production-phase-S0-preapproval.json` for all
   later comparisons. This file contains metadata and hashes, no customer rows.
   The relay also saves a unique timestamped report for every audit, so forward
   and rollback evidence cannot overwrite one another. Keep the initial S0
   baseline unchanged through the entire window.
   The checked-in 14:30 observation is review evidence, not a substitute for the
   execution-time baseline. The auditor checks stock retained expectations,
   16 protected table definitions/OIDs, exact phase footprint, exact heads,
   empty Shopify tables, original target, all flags OFF and source inventory.

2. From the existing API container, run **one block at a time**, only after the
   prior command and phase audit pass. The inherited URL is never echoed.

   ```bash
   set -euo pipefail
   cd /app
   python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0120 --json
   (cd /app/database && PGOPTIONS='-c search_path=public,pg_catalog -c lock_timeout=3000 -c statement_timeout=120000' python -B -m alembic upgrade 0121)
   python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0121 --json
   ```

   Run locally through the read-only relay:

   ```powershell
   py run_production_phase_audit.py --phase S1 --baseline-file production-phase-S0-preapproval.json
   ```

   Then the next separately checked block:

   ```bash
   set -euo pipefail
   cd /app
   python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0121 --json
   (cd /app/database && PGOPTIONS='-c search_path=public,pg_catalog -c lock_timeout=3000 -c statement_timeout=120000' python -B -m alembic upgrade 0122)
   python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0122 --json
   ```

   ```powershell
   py run_production_phase_audit.py --phase S2 --baseline-file production-phase-S0-preapproval.json
   ```

   Then the final separately checked block:

   ```bash
   set -euo pipefail
   cd /app
   python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0122 --json
   (cd /app/database && PGOPTIONS='-c search_path=public,pg_catalog -c lock_timeout=3000 -c statement_timeout=120000' python -B -m alembic upgrade 0123)
   python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0123 --json
   ```

   ```powershell
   py run_production_phase_audit.py --phase S3 --baseline-file production-phase-S0-preapproval.json
   ```

3. Run existing read-only Shopify catalog and privacy audits, plus the existing
   product identity and active binding audits. Keep diagnostic samples private;
   publish counts and verdicts only. Exact deployed audit entry points are:

   ```bash
   set -euo pipefail
   cd /app
   python -B scripts/audit_shopify_catalog_collisions_readonly.py
   python -B scripts/audit_shopify_privacy_readonly.py
   python -B scripts/audit_catalog_product_identity_readonly.py
   python -B scripts/audit_active_meta_item_bindings_readonly.py
   ```

4. With no inherited libpq overrides (the command-local `PGOPTIONS` above has
   ended), run the **final signed** S3 bootstrap in a one-off process with its
   exact approval payload, then require the full gate PASS. Do not claim the
   clone harness or phase audit as this production gate. Only after S3 checks
   pass, use the reviewed two-field API patch to bind the signed S3 payload and
   matching pre-deploy command. The new approval must cover this configuration
   update and one controlled redeploy of the same `a8fb88e3` API release.
   Verify real startup, terminal deployment success, health, schema and flags.
   The dashboard needs no deployment. The current S0 pre-deploy payload is
   expired and must not be reused or relabeled.

All seven Bash blocks use `set -euo pipefail`. Local Git Bash syntax checks and
24 stubbed success/failure cases verify that a failed topology precheck prevents
DDL, failed DDL prevents the postcheck, and a failed postcheck stops the block.
These checks executed no real migration or database connection; see the
[shell validation record](shell-validation.json).

No `upgrade head/heads`, `stamp`, `merge`, bare `downgrade -1`, manual version-row
DML, other migrations, schema repair, product sync, or Shopify activation is
authorized by this proposal.

## Narrow normal rollback

Use only if the execution-window approval explicitly includes it, the reached
phase is known, protected definitions/OIDs still match, all Shopify tables are
empty, all Shopify flags remain OFF, and the exclusion window still holds.
Apply only the necessary suffix of this rehearsed reverse path, one command
and phase audit at a time:

```bash
set -euo pipefail
cd /app
python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0123 --json
(cd /app/database && PGOPTIONS='-c search_path=public,pg_catalog -c lock_timeout=3000 -c statement_timeout=120000' python -B -m alembic downgrade 0123@-1)
python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0122 --json
```

Require the S2 phase audit before the next rollback block:

```powershell
py run_production_phase_audit.py --phase S2 --baseline-file production-phase-S0-preapproval.json
```

```bash
set -euo pipefail
cd /app
python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0122 --json
(cd /app/database && PGOPTIONS='-c search_path=public,pg_catalog -c lock_timeout=3000 -c statement_timeout=120000' python -B -m alembic downgrade 0122@-1)
python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0121 --json
```

Require the S1 phase audit before the last rollback block:

```powershell
py run_production_phase_audit.py --phase S1 --baseline-file production-phase-S0-preapproval.json
```

```bash
set -euo pipefail
cd /app
python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0121 --json
(cd /app/database && PGOPTIONS='-c search_path=public,pg_catalog -c lock_timeout=3000 -c statement_timeout=120000' python -B -m alembic downgrade 0121@-1)
python -B scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0120 --json
```

Require the final S0 phase audit:

```powershell
py run_production_phase_audit.py --phase S0 --baseline-file production-phase-S0-preapproval.json
```

Stop/cancel only this window's failed or pending S3 validation deployment before
branch rollback, and verify that no S3 deployment remains in flight. Keep the
deployment exclusion until the final schema and binding agree. Do not cancel
an unrelated deployment or service.

After S0 is proven, use the separately signed S0 rollback payload/command for
the same API source if a platform binding change/redeploy is needed. Never
switch to legacy source or reuse the expired S0 record. Unexpected rows,
retained-schema drift, unknown heads, expiry, or uncertain transaction state
are **STOP**, not permission to drop objects or restore the archive.
