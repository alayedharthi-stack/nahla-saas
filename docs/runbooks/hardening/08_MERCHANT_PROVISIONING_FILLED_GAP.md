# 08 — Merchant provisioning: `filled_gap` email collision hardening

| Field | Value |
|---|---|
| **Status** | **DEFERRED** — execute only after Salla Embedded path is fully stable (locale + theme + auth verified in prod) |
| **Priority** | P1 — prevents recurrence for **any new** Salla store, not a one-off data fix |
| **Owner module** | `backend/core/merchant_provisioning.py` |
| **Target tests** | `backend/tests/test_merchant_provisioning_filled_gap.py` (new) |
| **Out of scope** | Salla Embedded UI/locale/theme/JWT batches; one-off SQL tenant realignment for a specific store |

---

## Why this exists

### Production incident (2026-06, store `22825873`)

Symptom on embedded open:

```text
duplicate key value violates unique constraint "users_email_key"
```

during `POST /salla/token-login` → `get_or_create_merchant_user`.

Data shape:

| Entity | Problem |
|---|---|
| `integrations` id=3 | `provider=salla`, `external_store_id=22825873`, **`tenant_id=1`** (stale/wrong) |
| `users` id=16 | Same derived email `store-22825873@salla-merchant.nahlah.ai`, **`tenant_id=47`** (correct merchant home) |

Flow:

1. Branch 1 found the integration → `tenant_id=1`.
2. No user on tenant 1 with that email → **`filled_gap`** path ran.
3. `_insert_user(..., tenant_id=1)` → **unique violation** because email already exists globally on tenant 47.

**Data fix applied (prod, manual):** `UPDATE integrations SET tenant_id = 47 WHERE id = 3 AND ...`

That fix does **not** protect the next store with the same drift pattern.

---

## Current code behaviour

### Branch 1 → `filled_gap` (gap today)

```212:244:backend/core/merchant_provisioning.py
        # Integration exists but the user row is missing.
        new_user = _insert_user(db, email=canonical_email, tenant_id=tenant_id)
```

Lookup before insert is **scoped only**:

```186:192:backend/core/merchant_provisioning.py
        existing_user = (
            db.query(User)
            .filter(
                User.tenant_id == tenant_id,
                User.email == canonical_email,
            )
            .first()
        )
```

**Missing:** global `User.email` check (unique across all tenants).

### Branch 4 — collision handling (reference implementation)

```275:285:backend/core/merchant_provisioning.py
    if db.query(User).filter(User.email == owner_email).first() is not None:
        safe_name = ...
        chosen_email = f"{safe_name or 'store'}-{suffix}@{provider}-merchant.nahlah.ai"
```

Branch 4 **does** avoid `users_email_key` by deriving a store-scoped email.

**Goal:** reuse the same *class* of logic in `filled_gap` (and optionally repair integration tenant linkage).

---

## Hardening requirements (implementation checklist)

Execute **only** when operator sends explicit **GO** after Embedded sign-off.

### 1. Review `filled_gap` branch

Before `_insert_user` in the integration-exists / user-missing path:

1. **Global email lookup**

   ```python
   global_user = db.query(User).filter(User.email == canonical_email).first()
   ```

2. **If `global_user` exists:**

   | Sub-case | Action |
   |---|---|
   | `global_user.tenant_id == integration.tenant_id` | Treat as `linked_existing` (should have been caught by tenant-scoped query — log warning, return existing user). |
   | `global_user.tenant_id != integration.tenant_id` | **Integration drift** — prefer repair + link, not blind insert. |

3. **Integration tenant repair (when safe)**

   - If `global_user.tenant_id` is the merchant’s real home tenant (e.g. only user on that tenant, or integration has no other active users/data — define explicit rules in code + tests):
     - `integration.tenant_id = global_user.tenant_id`
     - Persist audit fields in `integration.config` (e.g. `tenant_repaired_at`, `tenant_repair_reason=filled_gap_email_collision`).
   - If repair is **not** safe (multi-tenant ambiguity, multiple integrations, admin tenants): **do not** auto-repair; fall through to collision email strategy (below).

4. **Collision email strategy (same spirit as Branch 4)**

   - If cannot link/repair: do **not** call `_insert_user` with `canonical_email`.
   - Derive `store-scoped` email: `{safe_name}-{store_id}@{provider}-merchant.nahlah.ai`.
   - Update `integration.config["salla_owner_email"]` to the chosen email so future lookups are stable.
   - Log structured event: `provisioning_filled_gap_email_collision`.

5. **Never rely only on `(tenant_id, email)`** for uniqueness — DB constraint is **global** on `users.email`.

### 2. Optional: config canonical email vs `owner_email`

When `cfg["salla_owner_email"]` disagrees with introspected `owner_email`, document resolution order in tests (prefer stored canonical unless derived-placeholder rules say otherwise).

### 3. Regression tests (required before merge)

New file: `backend/tests/test_merchant_provisioning_filled_gap.py`

Use in-memory SQLite or existing test DB fixture pattern from other `backend/tests/test_*.py`.

| Case | Setup | Expect |
|---|---|---|
| **A — happy filled_gap** | Integration on tenant T, no user on T, email unused globally | New user on T, `filled_gap=True`, no IntegrityError |
| **B — integration wrong tenant** | Integration `tenant_id=T1`, User same email on `T2` | No `users_email_key`; either repair integration → T2 + link user, or derived email + config update (per chosen policy) |
| **C — linked_existing** | Integration on T, user on T with same email | `linked_existing=True`, no insert |
| **D — Branch 4 parity** | No integration, global email exists | Derived email, new tenant (existing Branch 4 behaviour — guard against regression) |
| **E — derived email** | `is_email_derived=True`, store_id set | No cross-store email linking (existing guard) |

Assert **`IntegrityError` never propagates** from provisioning for cases B and D.

### 4. Observability

- Audit / log lines distinguish:
  - `filled_gap_repaired_integration_tenant`
  - `filled_gap_email_collision_derived`
  - `filled_gap_linked_global_user`
- `salla_oauth` / `token-login` should surface a clear 500 only for unexpected failures, not constraint violations.

---

## Theme logic comparison (for reviewers)

### Before (filled_gap)

```
integration.tenant_id → user WHERE tenant_id AND email
  → miss → INSERT(email)  → 💥 if email exists on another tenant
```

### After (target)

```
integration.tenant_id → user WHERE tenant_id AND email
  → miss → user WHERE email (global)
       → found + same tenant → link
       → found + other tenant → repair integration OR derived email (Branch-4-style)
       → not found → INSERT
```

---

## Prerequisites (GO criteria)

All must be true before implementing this runbook:

- [ ] Salla Embedded: token-login, session, subscription/settings 200 (no JWT public-path regression)
- [ ] Locale: Arabic default inside Arabic Salla iframe (commit `a27133ca` or later)
- [ ] Theme: Light default inside light Salla iframe (commit `f8cc029c` or later)
- [ ] Operator sign-off on prod smoke from real merchant iframe

---

## Explicit non-goals

- Do **not** bundle with Salla Embedded frontend deploys.
- Do **not** change OAuth middleware / `JWT_PUBLIC_PREFIXES` in this task.
- Do **not** assume prod SQL fixes for individual stores replace code hardening.

---

## Execution log

When implemented, append results to:

`docs/runbooks/exec-logs/YYYY-MM-DD-08-filled-gap.md`

Include: PR link, test command output, and whether a backfill script for orphaned `integrations.tenant_id` is needed.
