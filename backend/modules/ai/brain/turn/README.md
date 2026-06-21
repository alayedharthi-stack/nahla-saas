# Turn Understanding + Turn Arbiter

## Architecture (Phase 2B)

1. **Arbiter** selects exactly one turn **owner** per inbound message.
2. **OwnerBrief** defines compose **goals and constraints** for that owner — not reply text.
3. **Persona / LLM compose** writes the final reply freely within those bounds.

OwnerBrief is **not** a template and **not** a canned reply. It carries structured fields such as `reply_goal`, `forbidden_objectives`, `required_evidence`, `tone_guidance`, and `compose_mode` (`persona`, `operational_payload`, `hybrid`).

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
2. Replaces legacy decision with `ACTION_LLM_REPLY` and passes `owner_brief` in `decision.args` for compose.
3. Logs `[TURN_ARBITER_ENFORCE] enforced=true` with `reply_goal` and `compose_mode`.

### What enforce does NOT do

- No merchant-specific logic in code.
- No new guards or regex hotfixes.
- No template replies in OwnerBrief.
- No enforce when mismatch type is not allowlisted.

## Tests

```bash
cd backend
python -m pytest tests/test_turn_arbiter_shadow.py tests/test_turn_arbiter_enforce.py -v
```
