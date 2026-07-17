# A1 generic-commerce evidence fixture operator runbook (G4 pre-0088)

**Experimental staging-only tooling.** This harness seeds deterministic A1 order
evidence so the G4 reconciliation report can be exercised **non-vacuously** before
any `0088` validation decision. It is **not** production fixture tooling, not a
merchant onboarding path, and not customer-visible behavior.

## Purpose

After **A1-Expand** (`0087`) is deployed on staging, operators may need authoritative
internal + external order tuples in a **single tenant** to prove G4 gates with real
linkage evidence — without running reconciliation write, migrations, validations, or
provider/network calls.

The harness:

- Creates **one** internal authoritative order (`nahla_internal`) and **one** external
  authoritative order (`external_provider`) per tenant via existing service APIs:
  `apply_nahla_internal_order_identity` and `apply_external_order_identity_from_salla`
- Uses a closed deterministic namespace (`a1_g4_generic_commerce_v1`) and
  `A1G4FX-{tenant_id}-*` external IDs so reruns are idempotent and production rows
  cannot be selected
- Emits privacy-safe aggregate JSON (`a1_evidence_fixture_v1`) — no names, phones,
  raw IDs, refs, credentials, or exception text
- Provides a separately confirmed **cleanup** mode that deletes only namespace-owned
  rows in dependency-safe order

## What this is NOT

- Does **not** run `reconcile_*` write helpers or the reconciliation write operator
- Does **not** apply migration `0088` or change capability state
- Does **not** enable flags, dashboards, or reconciliation consumers
- Does **not** call Salla/provider APIs or webhooks
- Does **not** write A1 linkage via direct ORM/SQL (service-mediated only)
- Does **not** scan or clean all tenant rows — only provably fixture-owned rows

---

## Preconditions (all modes)

1. **Staging identity** — `RAILWAY_PROJECT_NAME=desirable-growth`,
   `RAILWAY_ENVIRONMENT_NAME=staging`. Production markers are rejected.
2. **Database allowlist** — `DATABASE_URL` host must be
   `postgres-staging.railway.internal`.
3. **Alembic exactly `0087`** — rejects `0088`, `0089`, and unknown revisions.
4. **Capability `expand` only** — `order_customer_identity_capability_state.state = expand`
   and `validation_revision IS NULL`.
5. **Explicit `--tenant-id`** (positive integer). No all-tenant mode.

Dry-run (default) validates gates and reports `would_create` without mutations.

---

## Seed command (dry-run default)

```bash
export DATABASE_URL='postgresql://…'   # staging allowlist host only
export RAILWAY_PROJECT_NAME='desirable-growth'
export RAILWAY_ENVIRONMENT_NAME='staging'

python backend/scripts/seed_a1_generic_commerce_evidence_fixture.py \
  --tenant-id <TENANT_ID> \
  --pretty
```

### Persist fixtures (write)

Requires a separate confirmation token from reconciliation write:

```bash
export NAHLA_A1_EVIDENCE_FIXTURE_WRITE_CONFIRM=RUN_A1_EVIDENCE_FIXTURE_WRITE

python backend/scripts/seed_a1_generic_commerce_evidence_fixture.py \
  --tenant-id <TENANT_ID> \
  --write \
  --pretty
```

**Bounded shape per tenant (strict caps):**

| Kind | Max |
|------|-----|
| Integrations | 1 |
| Internal customers | 1 |
| Internal authoritative orders | 1 |
| External authoritative orders | 1 |
| External profiles | 1 |

Generic commerce labels (shoes `حذاء رياضي أبيض`, perfume `عطر ورد 100ml`, city
`الرياض`, short code `RRRD1234`) are stored only on fixture rows and are **never**
emitted in operator JSON.

---

## Cleanup command (separately confirmed)

Preview:

```bash
python backend/scripts/seed_a1_generic_commerce_evidence_fixture.py \
  --tenant-id <TENANT_ID> \
  --cleanup \
  --pretty
```

Execute (requires cleanup token **and** `--write`):

```bash
export NAHLA_A1_EVIDENCE_FIXTURE_CLEANUP_CONFIRM=RUN_A1_EVIDENCE_FIXTURE_CLEANUP

python backend/scripts/seed_a1_generic_commerce_evidence_fixture.py \
  --tenant-id <TENANT_ID> \
  --cleanup \
  --write \
  --pretty
```

Deletion order: fixture orders → coverage rows for linked profiles/customers →
fixture external profiles → fixture internal customers → fixture integration.

Rows without the fixture namespace marker and `A1G4FX-{tenant_id}-*` ownership are
never deleted.

---

## Output schema (`a1_evidence_fixture_v1`)

| Field | Meaning |
|-------|---------|
| `fixture_schema_version` | Always `a1_evidence_fixture_v1` |
| `tenant_id` | Scoped tenant |
| `mode` | `seed` or `cleanup` |
| `dry_run` / `read_only` | `true` unless `--write` |
| `outcome` | `success`, `failed`, or `aborted` |
| `access_status` | `ok`, `gate_rejected`, `tenant_missing`, `execution_failed` |
| `capability` | State, revision, Alembic pin (aggregates only) |
| `fixture_namespace` | `a1_g4_generic_commerce_v1` |
| `shape` | `existing`, `would_create`, `created`, `skipped_existing` counts |
| `authoritative` | Internal/external authoritative order counts |
| `cleanup` | `selected` / `deleted` aggregate counts |
| `committed` | Whether a write transaction committed |

**Privacy:** Output must not contain phone, email, name, order ID, customer ID,
external reference, profile UUID, raw SQL, DB URLs, or exception text.

---

## Recommended G4 workflow (staging)

1. Confirm `0087` Expand + capability `expand` on staging.
2. **Seed fixtures** (this harness) for the target tenant if no non-vacuous evidence exists.
3. Run the **read-only reconciliation report**
   (`report_order_customer_identity_reconciliation.py`).
4. If coverage/watermarks are missing, run the **reconciliation write operator**
   (`reconcile_order_customer_identity_coverage.py --write`) in a separate step.
5. Re-run the read-only report until `ready_for_validate: true`.
6. Proceed to deferred `0088` only with separate rollout approval — see
   `docs/engineering/staging-migration-0087-to-0088-runbook.md`.
7. **Retain fixture rows until validation outcome**; clean up only after success
   or explicit no-go (see cleanup section below).
7. **Cleanup fixtures** when the staging experiment ends.

---

## Idempotency

Re-running seed `--write` on the same tenant skips existing namespace-owned rows.
Identical database state yields identical aggregate JSON (timestamp excluded).

---

## CI

Unit and PostgreSQL integration tests:

`backend/tests/test_order_customer_identity_evidence_fixture.py`

Wired in `.github/workflows/ci.yml` under **A1 evidence fixture operator tests**.

---

## Operational limits

- One tenant at a time in a maintenance window.
- Never use on production hosts or without staging identity markers.
- Treat all fixture rows as disposable experimental evidence only.
