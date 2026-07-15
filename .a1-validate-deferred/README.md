# A1-Validate (deferred — separate PR)

This folder holds the **A1-Validate** rollout artifacts. They are **not** part of the
A1-Expand branch/PR and must not be merged until:

1. A1-Expand and the conversation binding PR are merged, with the in-tree
   migration chain applied through `0089`.
2. Backfill/reconciliation report is reviewed.
3. Separate rollout approval is granted.

## Contents

- `0088_order_customer_identity_a1_validate.py` — deferred migration content
  (CONCURRENTLY indexes + VALIDATE CONSTRAINT). The filename is historical only;
  it is not an Alembic revision in the current tree.
- `test_order_customer_identity_migration_validate_pg.py` — deferred PostgreSQL
  tests to copy when opening the Validate PR.

## Promotion plan

When Validate is approved:

1. Promote the deferred migration content as new revision **`0090`** with
   `down_revision = "0089"`.
2. Rename/adapt the deferred PostgreSQL tests to target `0090`.
3. Review the migration and validation plan in its separate PR before any
   environment execution.

Do **not** run `alembic upgrade 0088`, do not copy `0088` into
`database/migrations/versions`, and do not create a second Alembic head.
