# A1-v3.7 order-customer identity — rollout

## Principle

**Repository Alembic graph has parallel heads `0088` and `0089`**, both with
`down_revision = '0087'`.

| Path | Revision | Purpose |
|------|----------|---------|
| **A1-Validate** | `0088` | Deferred constraint validation + indexes + capability `validated` |
| **Conversation bindings** | `0089` | `conversation_a1_subject_bindings` substrate (PR #596) |

**CI and integration fixtures pin to `0089` explicitly** — never `head` — so
capability remains `expand` until a deliberate Validate deploy.

**Staging experimental (Jul 2026)** is pinned at `0087` with tenant 1 G4
`ready_for_validate=true` evidence. Advance to `0088` only via the guarded
operator after separate approval.

| Stage | Revision | Status |
|-------|----------|--------|
| **A1-Expand** | `0087` | Applied (common ancestor) |
| **A1-Validate** | `0088` | In repo; sibling head; staging operator slice |
| **Conversation bindings** | `0089` | In repo; sibling head; default CI/integration path |

---

## `0087` — order-customer identity expand

Included:

- Nullable `orders` identity columns (no backfill in migration)
- New tables: `external_customer_profiles`, coverage tables, `order_customer_identity_capability_state` (`expand`)
- Composite FK targets: `uq_customers_tenant_id`, `uq_integrations_tenant_id_id`
- Orders FK/CHECK added **`NOT VALID`** (legacy rows not scanned)
- Ingest hooks remain **fail-closed / degraded**

Deferred to **`0088`**:

- `CREATE INDEX CONCURRENTLY` on `orders`
- `ALTER TABLE … VALIDATE CONSTRAINT` on orders FK/CHECK
- Capability `validated` + `validation_revision = '0088'`

---

## `0088` — A1-Validate (separate PR / maintenance window)

Prerequisites:

1. `0087` applied exactly (staging at `0087`, not `0089`)
2. G4 per-tenant `ready_for_validate: true`
3. Operator preflight: zero constraint violation aggregates
4. Separate rollout approval

Includes:

- `CREATE INDEX CONCURRENTLY` on `orders` (Alembic autocommit blocks)
- `VALIDATE CONSTRAINT` for all orders FK/CHECK from `0087`
- Set `order_customer_identity_capability_state.state = validated`

Operator: `scripts/operators/staging_migration_0087_to_0088.py`
Runbook: `docs/engineering/staging-migration-0087-to-0088-runbook.md`

After Validate deploy, reconciliation may report `complete` / `healthy` when tuple scope is fully linked.

---

## `0089` — conversation → A1-subject bindings

Unchanged sibling path from `0087`. Validate PR does **not** modify `0089`.

- `conversation_a1_subject_bindings` with active/revoked/superseded semantics
- Partial unique index `uq_casb_tenant_conversation_active`
- Composite FK to conversations via `uq_conversations_tenant_id`

---

## Downgrade

| Action | Scope |
|--------|--------|
| `0088` downgrade | Drops concurrent `orders` indexes; capability back to `expand` |
| `0089` downgrade | Drops binding rows and related indexes |
| `0087` downgrade | Drops A1 tables/columns/constraints — **linkage data lost** |

Ephemeral tests may downgrade `0087→0086` only in throwaway databases.

---

## Operator checklist — CI / integration (`0089` path)

1. Confirm `alembic heads` shows `0088` and `0089`.
2. Apply migrations with `alembic upgrade 0089` (not `head` for integration tests).
3. Capability remains `expand` until Validate operator runs in staging.

## Operator checklist — staging Validate (`0087 → 0088`)

1. Confirm staging `alembic_version = 0087` and no `0089` tables.
2. G4 report: `ready_for_validate: true` for scoped tenant(s).
3. Operator preflight + run (`staging_migration_0087_to_0088.py`).
4. Post-check: all constraints validated, indexes valid, capability `validated`.
5. Fixture cleanup per evidence runbook.
6. Enable reconciliation consumers only after post-validation contract passes.
