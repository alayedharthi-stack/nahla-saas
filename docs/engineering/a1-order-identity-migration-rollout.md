# A1-v3.7 order-customer identity — rollout



## Principle



**Current Alembic single head is `0089`.** Production and CI use `alembic upgrade head`, which today applies the committed graph through `0089_conversation_a1_subject_bindings` (down_revision `0087`).



**Do not confuse the deferred validation artifact named `0088` with an applied revision.** The file `0088_order_customer_identity_a1_validate.py` lives in `.a1-validate-deferred/` and is **not** in the Alembic `migrations/versions/` chain. Capability rows may carry `validation_revision = '0088'` as a label for that deferred artifact; that string does not mean revision `0088` ran.



| Stage | Revision / artifact | Status in repo today |

|-------|----------------------|----------------------|

| **A1-Expand** | `0087` | Applied (in chain) |

| **Conversation bindings substrate** | `0089` | Applied (current head) |

| **A1-Validate** (separate PR) | deferred `0088` artifact | **Not** in Alembic chain; separate approval + maintenance window |



---



## Applied chain (`0087` → `0089`)



### `0087` — order-customer identity expand



Included:



- Nullable `orders` identity columns (no backfill in migration)

- New tables: `external_customer_profiles`, coverage tables, `order_customer_identity_capability_state` (`expand`)

- Composite FK targets: `uq_customers_tenant_id`, `uq_integrations_tenant_id_id`

- Orders FK/CHECK added **`NOT VALID`** (legacy rows not scanned)

- Ingest hooks remain **fail-closed / degraded**



Deferred to the **validation artifact** (not yet merged as Alembic head):



- `CREATE INDEX CONCURRENTLY` on `orders`

- `ALTER TABLE … VALIDATE CONSTRAINT` on orders FK/CHECK

- Backfill inside migration



### `0089` — conversation → A1-subject bindings



- `conversation_a1_subject_bindings` with active/revoked/superseded semantics

- Partial unique index `uq_casb_tenant_conversation_active` (one active binding per tenant+conversation)

- Composite FK to conversations via `uq_conversations_tenant_id`



### Application behavior after `0089`



- A1 identity ingest paths may write new identity fields.

- **Reconciliation must not report `healthy` or `complete`** until capability state is `validated` (`order_customer_identity_capability_state`).

- **Do not enable** reconciliation consumers, feature flags, or “healthy” dashboards based on coverage rows until the **deferred validation artifact** is deployed as a future Alembic revision.



CI job `a1-postgres-integration` runs `alembic upgrade head` (currently `0089`). It does **not** apply `.a1-validate-deferred/` artifacts.



---



## Deferred validation artifact (`0088` filename) — separate maintenance window



Prerequisites:



1. Expand + bindings head (`0089`) applied in staging/production

2. Backfill / reconciliation report reviewed

3. Separate rollout approval

4. Merge deferred artifact into `migrations/versions/` as a new revision (future PR)



Includes (when promoted from `.a1-validate-deferred/`):



- `CREATE INDEX CONCURRENTLY` on `orders` (Alembic autocommit blocks)

- `VALIDATE CONSTRAINT` for all orders FK/CHECK from `0087`

- Set `order_customer_identity_capability_state.state = validated` (with `validation_revision`)

- Query-plan validation

- Rollback/runbook (see deferred migration downgrade)



After Validate deploy, reconciliation may report `complete` / `healthy` when tuple scope is fully linked.



---



## Downgrade



| Action | Scope |

|--------|--------|

| Deferred `0088` downgrade (when merged) | Drops concurrent `orders` indexes only |

| `0089` downgrade | Drops binding rows and related indexes |

| `0087` downgrade | Drops A1 tables/columns/constraints — **linkage data lost** |



Ephemeral tests may downgrade `0087→0086` only in throwaway databases.



---



## Operator checklist — current head (`0089`)



1. Confirm `alembic heads` shows single head `0089`.

2. Deploy app + `alembic upgrade head`.

3. Monitor ingest; confirm no lock incidents on `orders`.

4. Run backfill/reconciliation report — expect **degraded / incomplete** coverage until validated capability is set **after** future Validate deploy.

5. **Do not** treat coverage as production-ready or enable consumers until Validate revision merges.



## Operator checklist — Validate (later PR)



1. Separate approval granted.

2. Promote deferred artifact to Alembic; deploy `alembic upgrade head` in low-traffic window.

3. Verify `pg_constraint.convalidated = true` for all orders A1 constraints.

4. Verify indexes: `ix_orders_tenant_customer_id`, `ix_orders_tenant_external_tuple`, `ix_orders_tenant_order_source_kind`.

5. Only then enable reconciliation consumers / healthy signals.
