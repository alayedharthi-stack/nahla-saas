# Turn Understanding + Turn Arbiter

## Architecture (Phase 2B+)

1. **Arbiter** selects exactly one turn **owner** per inbound message.
2. **OwnerBrief** defines compose **goals and constraints** for that owner — not reply text.
3. **Persona / LLM compose** writes the final reply freely within those bounds.

OwnerBrief is **not** a template and **not** a canned reply.

## Phase 3A — OwnerBrief Native Compose (default off)

When `TURN_ARBITER_OWNER_BRIEF_COMPOSE_ENABLED=true`, the pipeline attaches
`owner_brief` to `decision.args` before compose even when enforce did not fire.
Enforce-injected briefs take priority.

```env
TURN_ARBITER_OWNER_BRIEF_COMPOSE_ENABLED=true
```

Grep: `[TURN_OWNER_BRIEF_COMPOSE]`

## Production observation (Phase 3 rollout)

After enabling shadow + enforce, monitor:

| Grep pattern | Purpose |
|--------------|---------|
| `[TURN_ARBITER_SHADOW]` | proposed vs legacy owner, mismatch, brief fields |
| `[TURN_ARBITER_ENFORCE] enforced=true` | enforce interventions |
| `[TURN_ARBITER_OUTCOME]` | classified outcome per turn |
| `[TURN_OWNER_BRIEF_COMPOSE]` | native brief attach (Phase 3A) |

Outcome categories (`turn/observability.py`):

- `success` — mismatch handled with enforce or brief-guided compose
- `missed_mismatch` — mismatch detected but not enforced
- `false_enforce` — enforced without valid mismatch
- `composer_tone_issue` — brief present but reply still template-heavy
- `no_mismatch` — owners aligned

Manual scenarios to spot-check:

- Complaint during stale checkout → support brief, no city slot replay
- Discount during stale checkout → discovery brief, answer first
- Gratitude / identity → persona LLM, no handoff
- City during real checkout → checkout continuation preserved
- Payment / tracking → no claims without evidence

## Phase 1 — Shadow (default on)

Measure owner mismatch without changing replies. Grep `[TURN_ARBITER_SHADOW]`.

## Phase 2A — Emergency Enforce (default off)

Override legacy `Decision` only when **all** of:

- `TURN_ARBITER_ENFORCE_ENABLED=true`
- Tenant is eligible (platform-wide by default)
- Shadow detects `owner_mismatch=true`
- `mismatch_type` is in `TURN_ARBITER_ENFORCE_MISMATCH_TYPES`

Default enforce mismatch types:

- `checkout_vs_support`
- `checkout_vs_discovery`
- `staff_vs_persona`

### Recommended env (platform-wide rescue)

```env
TURN_ARBITER_SHADOW_ENABLED=true
TURN_ARBITER_ENFORCE_ENABLED=true
# Optional next step after stability review:
# TURN_ARBITER_OWNER_BRIEF_COMPOSE_ENABLED=true
```

### What enforce does

1. Suspends stale checkout scope when understanding says so.
2. Replaces legacy decision with `ACTION_LLM_REPLY` + `owner_brief`.
3. Logs `[TURN_ARBITER_ENFORCE] enforced=true` with `reply_goal` and `compose_mode`.

### What enforce does NOT do

- No merchant-specific logic in code.
- No new guards or regex hotfixes.
- No template replies in OwnerBrief.

## Phase 3B — Current-turn Understanding

`turn/understanding.py` gives the **current inbound message** highest authority.
Semantic interpreter and topic-shift signals can override stale checkout context.
Persisted workflow is evidence (`active_objective_candidate`), not automatic owner.

## Tests

```bash
cd backend
python -m pytest tests/test_turn_arbiter_shadow.py tests/test_turn_arbiter_enforce.py tests/test_turn_arbiter_observability.py tests/test_turn_arbiter_compose_bridge.py -v
```
