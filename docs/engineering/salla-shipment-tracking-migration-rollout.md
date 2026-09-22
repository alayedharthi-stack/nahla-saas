# Salla shipment tracking — schema rollout

Revision `0112` adds the nullable, tenant-scoped tracking evidence columns and
the Salla external-shipment identity constraint on `order_shipments`.

## Required deployment order

1. Take the normal database backup and confirm the intended database. Do not
   use an application startup task as the migration runner.
2. With application code that does **not** select the `0112` tracking columns
   active, run `alembic upgrade 0112` against the intended database. Do not
   use `alembic upgrade head`: `0111` remains an independent sibling.
3. Treat a preflight failure as schema drift. `0112` makes no DDL change or
   Alembic stamp when `order_shipments` is absent, missing foundation columns
   or constraints, or has incompatible definitions. Repair the known schema
   discrepancy in a separately reviewed operation, then rerun the explicit
   migration; never stamp `0112` to bypass the check.
4. Confirm the migration completed and the tracking columns plus
   `uq_order_shipments_tenant_tracking_source_ref` exist.
5. Deploy the application code that reads/writes tracking evidence only after
   that confirmation. Current ORM tracking queries are intentionally not
   compatible with a pre-`0112` schema.

The migration does not call an external carrier, backfill shipment history, or
enable a general tracking lookup. Rollback of application code should precede
any schema downgrade.
