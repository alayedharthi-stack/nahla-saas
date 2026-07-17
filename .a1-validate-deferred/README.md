# A1-Validate (deferred — promoted in feat/a1-validate-0088)

This folder is the **source archive** for the A1-Validate rollout. The tracked
artifacts now live in-tree:

| Archive | Promoted location |
|---------|-------------------|
| `0088_order_customer_identity_a1_validate.py` | `database/migrations/versions/0088_order_customer_identity_a1_validate.py` |
| `test_order_customer_identity_migration_validate_pg.py` | `backend/tests/test_order_customer_identity_migration_validate_pg.py` |

Operator + runbook:

- `scripts/operators/staging_migration_0087_to_0088.py`
- `scripts/operators/staging_migration_0087_to_0088_contract.py`
- `docs/engineering/staging-migration-0087-to-0088-runbook.md`

## Topology

- Revision **`0088`** branches from **`0087`** (sibling to **`0089`**).
- Staging operator targets **`0087 → 0088` only** — never `head` or `0089`.
- CI/integration fixtures continue to use **`alembic upgrade 0089`**.

Do not re-copy archive files into `migrations/versions/` without a deliberate
rollout PR.
