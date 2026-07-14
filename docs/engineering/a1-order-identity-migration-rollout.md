# A1-v3.7 order-customer identity — two-PR rollout



## Principle



**Alembic head in A1-Expand must be `0087` only.** Production `alembic upgrade head` after Expand merge applies expand objects only — not validate/index work.



Do not rely on operator docs to “stop at 0087” while the branch still ships `0088` in `migrations/versions/`.



| PR | Revision | When |

|----|----------|------|

| **A1-Expand** (this branch) | `0087` | Now |

| **A1-Validate** (separate PR) | `0088` | After Expand merge + staging `0087` + backfill/reconciliation report + separate approval |



Deferred Validate artifacts live in `.a1-validate-deferred/` (not in Alembic chain for Expand).



---



## A1-Expand (`0087`) — safe first release



Included:



- Nullable `orders` identity columns (no backfill in migration)

- New tables: `external_customer_profiles`, coverage tables, `order_customer_identity_capability_state` (`expand`)

- Composite FK targets: `uq_customers_tenant_id`, `uq_integrations_tenant_id_id`

- Orders FK/CHECK added **`NOT VALID`** (legacy rows not scanned)

- Ingest hooks remain **fail-closed / degraded**



**Not** included (deferred to A1-Validate):



- `CREATE INDEX CONCURRENTLY` on `orders`

- `ALTER TABLE … VALIDATE CONSTRAINT` on orders FK/CHECK

- Backfill inside migration



### Application behavior after Expand merge



- A1 identity ingest paths may write new identity fields.

- **Reconciliation must not report `healthy` or `complete`** until capability state is `validated` (`order_customer_identity_capability_state`).

- **Do not enable** reconciliation consumers, feature flags, or “healthy” dashboards based on coverage rows until **A1-Validate** is deployed.



CI job `a1-postgres-integration` runs `alembic upgrade 0087` (not `head` beyond 0087).



---



## A1-Validate (`0088`) — separate maintenance window



Prerequisites:



1. A1-Expand merged and `0087` applied in staging/production

2. Backfill / reconciliation report reviewed

3. Separate rollout approval



Includes:



- `CREATE INDEX CONCURRENTLY` on `orders` (Alembic autocommit blocks)

- `VALIDATE CONSTRAINT` for all orders FK/CHECK from `0087`
- Set `order_customer_identity_capability_state.state = validated` (with `validation_revision`)
- Query-plan validation

- Rollback/runbook (see deferred migration downgrade)



Deploy:



```bash

cd database && alembic upgrade 0088

```



After Validate, reconciliation may report `complete` / `healthy` when tuple scope is fully linked.



---



## Downgrade



| Action | Scope |

|--------|--------|

| `0088` downgrade | Drops concurrent `orders` indexes only |

| `0087` downgrade | Drops A1 tables/columns/constraints — **linkage data lost** |



Ephemeral tests may downgrade `0087→0086` only in throwaway databases.



---



## Operator checklist — Expand



1. Merge **A1-Expand** only; confirm Alembic head is `0087`.

2. Deploy app + `alembic upgrade head` (equals `0087` until Validate PR merges).

3. Monitor ingest; confirm no lock incidents on `orders`.

4. Run backfill/reconciliation report — expect **degraded / incomplete** coverage (by design).

5. **Do not** treat coverage as production-ready or enable consumers.



## Operator checklist — Validate (later PR)



1. Separate approval granted.

2. `alembic upgrade 0088` in low-traffic window.

3. Verify `pg_constraint.convalidated = true` for all orders A1 constraints.

4. Verify indexes: `ix_orders_tenant_customer_id`, `ix_orders_tenant_external_tuple`, `ix_orders_tenant_order_source_kind`.

5. Only then enable reconciliation consumers / healthy signals.

