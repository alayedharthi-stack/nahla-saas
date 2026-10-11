# Shopify migrations on the deployed multi-head Alembic topology — exact-topology plan

Status: **plan and disposable-PostgreSQL proof only.** Nothing in this document
has been executed against a live or staging database, and nothing here is an
automatic executor. Every live step is a later, separately approved operator
action on an approved clone first. All Shopify flags stay OFF throughout.

This plan complements, and does not replace,
[`shopify-deployed-port-staging-runbook.md`](shopify-deployed-port-staging-runbook.md):
that runbook's §1 gate requires a **single** `alembic_version` row `0120` and
therefore refuses the topology actually observed. The observation approved by
the owner is **three rows** `{0111, 0113, 0120}`. This plan is the closed
procedure for exactly that topology; any other observation stops.

Official reference for branches and the branch-qualified `@` syntax:
<https://alembic.sqlalchemy.org/en/latest/branches.html>. The behaviour below
was **proved on the repository's pinned Alembic (1.13.1) and SQLAlchemy
(2.0.30)** on disposable PostgreSQL 16, not read off the documentation; see
the evidence report named in §7.

## 1. What the three rows mean (database rows ≠ repository heads)

The repository migration graph forks twice above the deployed base:

```text
0093 → … → 0106 → 0107 → 0108 → 0109 ─┬─ 0111                       (application branch; repository head)
                                       └─ 0110 → 0112 ─┬─ 0113 → 0114   (navigation branch; 0114 not recorded in the observed topology)
                                                       ├─ 0115          (payments branch; not recorded in the observed topology)
                                                       └─ 0118 → 0120 → 0121 → 0122 → 0123   (catalog / Shopify branch)
0087 ─┬─ 0089 → 0091 → 0093 (above)
      └─ 0088 → 0090 → 0092                                          (validation sibling; not recorded in the observed topology)
```

* **Repository heads** (`alembic heads`) are `{0092, 0111, 0114, 0115, 0123}`.
  They describe the code, not any database.
* **Database rows** are one row per *applied* branch. A row may be an interior
  revision of the repository (`0113` recorded while `0114` exists in the
  repository) and need not include every repository head: `0092`, `0114` and
  `0115` are **not recorded in the observed topology**. The version table
  alone does not prove history (whether a revision was ever applied and rolled
  back is not visible in it); what the repository states is that normal
  bootstrap stays pinned to `0093` and that the accepted parallel-head
  topologies are those named in
  `scripts/operators/bootstrap_migration_contract.py`.
* The observed `{0111, 0113, 0120}` is therefore a **legitimate three-branch
  state**: the application branch at `0111`, the navigation branch at `0113`,
  the catalog branch at `0120`. It is exactly what the repository's own chain
  proofs build by genuine upgrades (`upgrade 0093`, `0111`, `0113`, `0120`).
* The Shopify revisions `0121 → 0122 → 0123` extend **only the catalog
  branch**. `0111` and `0113` are untouched by every forward and rollback
  step; three rows stay three rows.

## 2. States and the only commands

| State | `alembic_version` rows (exact, 3 rows) | Reached by |
|---|---|---|
| S0 | `0111, 0113, 0120` | the approved observation |
| S1 | `0111, 0113, 0121` | `alembic upgrade 0121` from S0 |
| S2 | `0111, 0113, 0122` | `alembic upgrade 0122` from S1 |
| S3 | `0111, 0113, 0123` | `alembic upgrade 0123` from S2 |

Rollback is **branch-qualified**, one revision at a time, in reverse order:

| From | Command | To |
|---|---|---|
| S3 | `alembic downgrade 0123@-1` | S2 |
| S2 | `alembic downgrade 0122@-1` | S1 |
| S1 | `alembic downgrade 0121@-1` | S0 |

All commands run from `database/` with `DATABASE_URL` set for the target
database only. Each is a single explicit revision; nothing here uses `head`,
`heads`, `base`, a bare relative step or a stamp.

### Forms that are never used

| Never | Why |
|---|---|
| `alembic upgrade head` / `heads` | with several repository heads it is ambiguous, and it would apply `0092`, `0114` and `0115`, which the observed topology does not record and this plan must not add |
| `alembic downgrade -1` (bare) | ambiguous with several database rows; Alembic refuses or picks a branch you did not choose |
| `alembic downgrade 0120` / `0122` etc. (parent id, unqualified) | resolves across branches to a common ancestor and can remove `0110`–`0113` work along the way; the repository's pilot runbook proved this for `0109`/`0111-1` |
| `alembic stamp …` | rewrites rows without running migrations: it fabricates state and hides drift |
| `alembic merge …` | a new merge revision changes the repository graph; out of scope and unnecessary |
| deleting or editing `alembic_version` rows, "purge", "reset" | manual fabrication; the preflight below fails closed on anything but the exact rows |

## 3. Closed gates (every one must pass on the approved clone before the next)

1. **Exact topology, fail closed.** The read-only preflight compares the exact
   row multiset (count and ids; duplicates and unknown ids fail):

   ```text
   DATABASE_URL=<clone> python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0120
   ```

   Exit 0 only for exactly `{0111, 0113, 0120}`. Any other outcome (a single
   `0120` row, a `0093` row, two rows, four rows, an unknown id, a missing
   table) is a **STOP** with a separate plan and approval; the script never
   coerces, repairs or relabels. Before each later step the same script is
   run with the state it must find (`--expect 0111,0113,0121` before
   `upgrade 0122`, and so on), and after each step with the state reached.
2. **Schema matches the recorded revisions.** The objects that `0111`, `0113`
   and `0120` create exist with their definitions (tables, columns, primary
   keys, unique, check and foreign-key constraints, indexes). Drift between
   the rows and the observed schema is a **STOP**. The chain proofs compare
   this inventory by definition before and after every step.
3. **Clone, backup, restore proven.** The procedure is run first on an
   approved staging **clone** of the observed database; a full backup is
   taken and its restore proven on a disposable copy before any change; the
   restored copy is kept until the rollback path has been exercised on the
   clone.
4. **Flags OFF.** `NAHLA_SHOPIFY_CONNECTION_ENABLED`,
   `NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED`, the privacy intake, worker and
   destructive flags and both Vite flags are unset or off throughout the
   migration and any rollback, before and after.
5. **No Shopify footprint.** `scripts/audit_shopify_catalog_collisions_readonly.py`
   and `scripts/audit_shopify_privacy_readonly.py` run read-only on the
   clone; any pre-existing `shopify_*` object is a STOP (the migrations refuse
   pre-existing objects and change nothing in that case).
6. **Protected rows.** Row counts of the retained-branch and catalog tables
   that the proofs protect (`tenants`, `products`, `customers`,
   `customer_addresses`, `customer_address_provenance`,
   `commerce_runtime_conversations`, `commerce_runtime_navigation_snapshots`,
   `commerce_runtime_handover_workers`, `meta_catalog_memberships`,
   `catalog_channel_retirements`, `meta_catalog_authorizations`) are recorded
   before and compared after every step on the clone.

## 4. Procedure on the approved clone (then, separately approved, on the target)

```text
# gate 1 (exact S0), gates 2–6
DATABASE_URL=<clone> python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0120
# forward, one explicit revision at a time, re-running the preflight between steps
(cd database && alembic upgrade 0121) ; python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0121
(cd database && alembic upgrade 0122) ; python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0122
(cd database && alembic upgrade 0123) ; python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0123
# rollback rehearsal on the clone (branch-qualified only), back to the exact observation
(cd database && alembic downgrade 0123@-1) ; python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0122
(cd database && alembic downgrade 0122@-1) ; python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0121
(cd database && alembic downgrade 0121@-1) ; python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0120
```

Record the preflight output (revision ids only; it prints no DSN, token or
row data) and the row counts before and after each command. A non-zero exit
anywhere is a STOP at that state; the recorded branch-qualified rollback of
the last applied revision is the only way back, never a stamp or a row edit.

## 5. Forward compatibility: a later `0124` on the same branch

A future revision with `down_revision = "0123"` (the Google schema worker's
planned `0124`) extends the same catalog branch and nothing else:

* forward: `{0111, 0113, 0123}` → `alembic upgrade 0124` → `{0111, 0113, 0124}`;
* rollback: `alembic downgrade 0124@-1` → `{0111, 0113, 0123}`; `0111` and
  `0113` are retained;
* the preflight is run with `--expect 0111,0113,0123` before and
  `--expect 0111,0113,0124` after.

No single-head, stamp or reset assumption is needed or allowed for it. That
revision is **not** implemented here; this plan only keeps its extension
compatible.

## 6. What this plan does not do

* It performs no live or staging action, verifies no live state and grants no
  credential; the production observation is the owner's and is not
  re-inspected here.
* It changes no migration revision, `env.py`, runtime, bootstrap or CI file.
* It does not recommend a merge revision, a stamp, a purge, `head`/`heads`
  or a bare `-1`.

## 7. Evidence

The disposable-PostgreSQL proof of every row in §2 (exact rows after each
forward and rollback step, retained `0111`/`0113`, non-Shopify schema by
definition, synthetic sentinel rows in generic, catalog and retained-branch
tables) is the three strengthened chain cases of the Shopify proof suites,
and the preflight's fail-closed behaviour is pinned by
`tests/test_alembic_topology_audit_script.py` and exercised on real
PostgreSQL inside the connection chain case. The run bound to this
document's source is recorded in
[`shopify-multihead-migration-plan-evidence.md`](shopify-multihead-migration-plan-evidence.md).
