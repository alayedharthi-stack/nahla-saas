# Turn Understanding + Turn Arbiter

## Phase 1 — Shadow (default on)

Measure owner mismatch without changing replies. Grep `[TURN_ARBITER_SHADOW]`.

## Phase 2A — Emergency Enforce (default off)

Override legacy `Decision` only when **all** of:

- `TURN_ARBITER_ENFORCE_ENABLED=true`
- Tenant is eligible (platform-wide by default; optional allowlist for gradual rollout)
- Shadow detects `owner_mismatch=true`
- `mismatch_type` is in `TURN_ARBITER_ENFORCE_MISMATCH_TYPES`

Default enforce mismatch types:

- `checkout_vs_support`
- `checkout_vs_discovery`
- `staff_vs_persona`

### Recommended env (platform-wide)

```env
TURN_ARBITER_SHADOW_ENABLED=true
TURN_ARBITER_ENFORCE_ENABLED=true
```

`TURN_ARBITER_ENFORCE_ENABLED=true` alone is enough — enforce applies to **all tenants**.

### Optional gradual rollout

Set `TURN_ARBITER_ENFORCE_TENANTS` only when you need a subset:

```env
TURN_ARBITER_ENFORCE_TENANTS=33,44
```

- Empty / unset → all tenants
- Set → only listed tenant ids

### What enforce does

1. Suspends stale checkout scope when understanding says so (`clear_active_order_context`, clear pending question).
2. Replaces legacy decision with owner-appropriate action (support LLM, discovery search/coupon, social reply).
3. Logs `[TURN_ARBITER_ENFORCE] enforced=true`.

### What enforce does NOT do

- No merchant-specific logic in code.
- No new guards or regex hotfixes.
- No enforce when mismatch type is not allowlisted.

## Tests

```bash
cd backend
python -m pytest tests/test_turn_arbiter_shadow.py tests/test_turn_arbiter_enforce.py -v
```
