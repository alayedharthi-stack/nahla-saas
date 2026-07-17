# Conditional-coupon shadow observation fixture operator runbook

**Experimental staging-only tooling.** This harness seeds the minimal valid tuple so
operators can later collect schema-valid Layer 0 `fact_record` + `telemetry` during a
**separate, explicitly gated shadow-only observation window**. It does **not** enable
shadow flags, compose, canary, or customer messaging.

## Purpose

After experimental staging reaches **dual-head** Alembic `{0088, 0089}` with A1
capability **validated** at `0088`, the manual shadow-review checklist may be blocked
solely because no schema-valid Layer 0 sample exists. This harness creates:

| Tuple row | Mechanism |
|-----------|-----------|
| Authoritative internal customer + completed orders | `apply_nahla_internal_order_identity` |
| A1 coverage reconciliation | `reconcile_internal_customer_coverage` |
| Conversation | fixture-namespace `Conversation` row |
| Authoritative conversation → A1 subject binding | `write_authoritative_internal_binding_from_verified_order` |
| Active conditional promotion | fixture-namespace `Promotion` with `min_orders_for_eligibility` |

Bounded generic-commerce labels (`متجر تجريبي عام`, `حذاء رياضي أبيض`, etc.) are used
in persisted rows only — **never** in operator JSON output.

## What this is NOT

- Does **not** set `NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED`
- Does **not** enable compose, canary, or trusted-context coupon consumers
- Does **not** call WhatsApp/provider APIs or dispatch customer messages
- Does **not** write A1 linkage via direct SQL (service-mediated only)
- Does **not** scan or delete non-fixture tenant rows

---

## Preconditions (all modes)

1. **Staging identity** — `RAILWAY_PROJECT_NAME=desirable-growth`,
   `RAILWAY_ENVIRONMENT_NAME=staging`.
2. **Database allowlist** — `DATABASE_URL` host must be
   `postgres-staging.railway.internal`.
3. **Explicit `--tenant-id`** (positive integer). No all-tenant mode.
4. **Alembic exactly `{0088, 0089}`** — rejects single-head `0088`/`0089`, `0087`,
   and unknown revision sets.
5. **Capability validated at 0088** — `order_customer_identity_capability_state.state =
   validated` and `validation_revision = '0088'`.

Dry-run (default) validates gates and reports `would_create` without mutations.

---

## Seed command (dry-run default)

```bash
export DATABASE_URL='postgresql://…'   # staging allowlist host only
export RAILWAY_PROJECT_NAME='desirable-growth'
export RAILWAY_ENVIRONMENT_NAME='staging'

python backend/scripts/seed_customer_conditional_coupon_shadow_fixture.py \
  --tenant-id <TENANT_ID> \
  --pretty
```

Expect `outcome=success`, `dry_run=true`, `observation_readiness.bridge_resolved=true`
after a prior successful write (or `would_create` counts on first dry-run).

## Write seed (separate confirmation)

```bash
export NAHLA_COUPON_SHADOW_FIXTURE_WRITE_CONFIRM=RUN_COUPON_SHADOW_FIXTURE_WRITE

python backend/scripts/seed_customer_conditional_coupon_shadow_fixture.py \
  --tenant-id <TENANT_ID> \
  --write \
  --pretty
```

## Cleanup (separate confirmation)

```bash
export NAHLA_COUPON_SHADOW_FIXTURE_CLEANUP_CONFIRM=RUN_COUPON_SHADOW_FIXTURE_CLEANUP

python backend/scripts/seed_customer_conditional_coupon_shadow_fixture.py \
  --tenant-id <TENANT_ID> \
  --cleanup \
  --write \
  --pretty
```

Cleanup deletes only namespace `cc_shadow_g4_generic_v1` rows in dependency order:
bindings → promotions → orders → coverage → conversations → customers.

---

## Output contract (`coupon_shadow_fixture_v1`)

Privacy-safe aggregate JSON only:

- `fixture_schema_version`, `tenant_id`, `mode`, `dry_run`, `outcome`
- `capability.alembic_revisions`, `capability.alembic_revision_is_dual_0088_0089`
- `shape.existing` / `shape.would_create` / `shape.created`
- `observation_readiness.authoritative_internal_orders`
- `observation_readiness.bridge_resolved`
- `observation_readiness.active_conditional_targets`
- `cleanup.selected` / `cleanup.deleted`

**Forbidden in operator JSON:** customer names, phones, raw IDs, order refs, message
text, credentials, stack traces.

---

## Future shadow-only observation window (separate step)

This harness **prepares data only**. Shadow observation is a **later, separately
approved** operator action:

1. Confirm fixture seed succeeded (`bridge_resolved=true`,
   `active_conditional_targets>=1`).
2. Obtain change approval for a **time-boxed** staging window.
3. Set `NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED=true` only
   for that window (never default-on in production).
4. Trigger a **non-customer** internal observation path (playground / controlled
   inbound replay) and archive sanitized `fact_record` + `telemetry` only.
5. **Unset** the shadow flag immediately after the window.
6. Complete the manual checklist in
   [customer-conditional-coupon-shadow-readiness-runbook.md](./customer-conditional-coupon-shadow-readiness-runbook.md)
   using the archived observation — not this fixture JSON alone.

Compose/canary flags remain forbidden regardless of fixture or checklist outcome.

---

## Related runbooks

- [customer-conditional-coupon-shadow-readiness-runbook.md](./customer-conditional-coupon-shadow-readiness-runbook.md) — manual checklist encoder
- [staging-migration-0088-to-0089-runbook.md](./staging-migration-0088-to-0089-runbook.md) — attach `0089` onto validated `0088`
- [a1-evidence-fixture-operator-runbook.md](./a1-evidence-fixture-operator-runbook.md) — pre-0088 A1 evidence (orthogonal)
