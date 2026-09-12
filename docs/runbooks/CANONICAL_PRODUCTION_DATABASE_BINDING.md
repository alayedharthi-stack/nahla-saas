# Canonical Production Database Binding

## Closed production contract

`nahla-saas` has one approved production PostgreSQL target:

- Project: `desirable-growth`
- Environment: `production`
- Application service: `nahla-saas`
- PostgreSQL service: `nahla-postgres-prod`
- PostgreSQL service ID: `b77b3d27-47b0-4a3a-83fd-44def66a3a84`
- Volume: `postgres-volume-hU16`
- Volume ID: `009bd0d5-85ed-4de4-99fc-94ea963c9d65`
- Database name: `railway`
- Managed application reference: `${{nahla-postgres-prod.DATABASE_URL}}`

The service named `Postgres` is not an approved `nahla-saas` production
target. Its service and volume must remain untouched until a separate,
dependency-audited retirement is explicitly authorized.

The machine-readable contract is
`config/canonical_production_database.json`.

## August 2026 incident

The application lost its canonical binding when a historical connection
literal and later a managed reference to the noncanonical `Postgres` service
were used during credential recovery. Password repair restored authentication
to that service, but its `0093`/20 technical baseline proved it was not the
canonical production database. The canonical service remained online with the
verified `0096`/28 incident baseline.

The production binding was restored to
`${{nahla-postgres-prod.DATABASE_URL}}`. No restore, migration, schema change,
data transfer, volume change, or PostgreSQL restart was part of the repair.

## Mandatory identity gate

Before any production database-variable, password, service-reference, or
rotation change, run the read-only guard:

```pwsh
python scripts/operators/verify_canonical_production_database.py `
  --phase pre-change `
  --authorization-ref INC-OR-CHANGE-ID `
  --old-binding canonical_postgres_reference `
  --new-binding canonical_postgres_reference `
  --rollback-plan-id RUNBOOK-CANONICAL-DB
```

Allowed sanitized binding labels are:

- `canonical_postgres_reference`
- `legacy_postgres_reference`
- `historical_literal`
- `unknown`

The guard fails closed unless all of these match the contract:

- Railway project and environment
- canonical PostgreSQL service ID and `SUCCESS` status
- attached volume ID, name, and mount path
- database name
- Alembic revision
- recorded tenant-1 technical count reference
- `SELECT 1`
- explicit authorization and rollback-plan identifiers

It does not print or persist a connection URL or credential.

After the single authorized application deployment, run the same guard through
the application binding:

```pwsh
python scripts/operators/verify_canonical_production_database.py `
  --phase post-change `
  --authorization-ref INC-OR-CHANGE-ID `
  --old-binding OLD-SANITIZED-LABEL `
  --new-binding canonical_postgres_reference `
  --rollback-plan-id RUNBOOK-CANONICAL-DB
```

If either gate fails, stop. Do not restore, migrate, rotate again, switch to
`Postgres`, or alter a volume.

## Managed references only

`nahla-saas` must never store a resolved PostgreSQL URL. Its production
`DATABASE_URL` must be exactly:

```text
${{nahla-postgres-prod.DATABASE_URL}}
```

Do not capture resolved URLs in files, logs, shell history, incident reports,
hashes, or rollback artifacts. A rollback record names the old and new binding
using the sanitized labels above; it never stores credential material.

## Password rotation

1. Obtain explicit production authorization naming the canonical service.
2. Run the pre-change identity gate.
3. Confirm the application variable is the canonical managed reference.
4. Rotate only through Railway's supported control on
   `nahla-postgres-prod`.
5. Wait for the PostgreSQL operation to reach `SUCCESS`.
6. Confirm Railway's derived PostgreSQL variables reference
   `POSTGRES_PASSWORD`; never replace them with resolved literals.
7. Allow or trigger at most one authorized `nahla-saas` deployment.
8. Wait for its final state.
9. Run the post-change gate and sanitized authentication/log checks.

If authentication fails, stop after one attempt and escalate with sanitized
evidence. Do not rotate again or point the application at another PostgreSQL
service.

## Change record and rollback

Every database-binding change record must contain:

- explicit authorization reference
- sanitized old and new target labels
- canonical service and volume IDs
- pre-change and post-change guard results
- one deployment ID
- rollback-plan ID

The default rollback is to reissue the canonical managed reference and perform
one separately authorized application deployment. A literal URL, alternate
PostgreSQL service, restore, volume attachment, or password reversal is not an
implicit rollback and requires separate authorization.
