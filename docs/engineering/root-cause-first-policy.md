# Engineering Policy — Root Cause First

**Status:** Adopted (permanent)  
**Effective:** 2026-08-06  
**Triggering evidence:** Tenant 1 live WhatsApp vs Dashboard divergence RCA — `الدفع عند الاستلام` was **not** a MerchantBrain / memory defect; it was **cross-tenant channel mix** (T1 Meta Cloud API vs T33 Dialog360 coexistence / forwarded / merchant-app echo).

Authoritative companions:

- `AGENTS.md` — Operations vs Personality doctrine
- `docs/engineering/ai-pr-constitution-checklist.md` — AI PR gate
- `.cursor/rules/root-cause-first.mdc` — agent enforcement

---

## Policy

**It is forbidden** to change MerchantBrain, Prompt, Compose, State, Memory, or any AI reasoning path because of a live-test outcome **until evidence proves** the defect is not in transport, data, identity, or integration layers.

Every new incident must follow this **mandatory sequence**. Stop at the first failing layer; do not escalate upward.

1. Did the message arrive from the channel?
2. Did it enter the correct webhook?
3. Was the correct tenant resolved?
4. Was the correct `phone_number_id` resolved?
5. Was the message persisted?
6. Was it retrieved into conversation context?
7. Was the correct tool invoked?
8. Did the tool return a correct result?
9. Did structured facts reach Compose?
10. **Only then** may the defect be attributed to model behavior.

---

## Project philosophy

Nahla is **not** a dialogue-rules engine.

Rejected patterns:

- If the customer says X, reply Y
- If they ask X, force path Y
- Add a prompt special-case for this incident
- Fix the test by adding an exception

Adopted philosophy:

- The platform supplies structured truth
- Tools retrieve the correct truth
- State stores the truth
- The model reasons freely
- The model composes the reply

Needing more rules to steer the model usually means a gap in **data, tools, state, or retrieval** — not a missing hardcoded branch.

---

## Root Cause rule (RCA gate)

Every RCA must state:

| Required field | Meaning |
|----------------|---------|
| **First Divergence** | First layer where WhatsApp / expected path and Nahla SoT disagree |
| **Source of Truth** | Which store/API is authoritative for that claim |
| **Provenance** | Tenant, connection, `phone_number_id`, provider, `wamid`, direction, source |
| **Evidence** | Logs + DB + message IDs (not intuition) |

**No Patch proposal is accepted** before the root cause is proven with evidence.

---

## Official investigation stack

```text
Channel
  ↓
Webhook
  ↓
Tenant Resolution
  ↓
Identity
  ↓
Persistence
  ↓
Conversation Retrieval
  ↓
State
  ↓
Tools
  ↓
Structured Facts
  ↓
Compose
  ↓
LLM
```

Skipping a layer is a process violation.

---

## Defect classification

Classify every defect into exactly one primary type first:

- Channel / Transport
- Webhook
- Tenant Resolution
- Identity Resolution
- Persistence
- Retrieval
- State
- Tool Availability
- Tool Permission
- Tool Result
- Structured Facts
- Runtime Exception
- **LLM Behavior**

**LLM Behavior is allowed only when every prior layer is proven healthy.**

---

## Live-test hygiene (from adoption incident)

- Pin the exact business number, `phone_number_id`, provider, connection id, and tenant before smoke.
- Do not mix Tenant 1 Meta Cloud API with Tenant 33 Dialog360 coexistence in the same evaluation thread.
- Record each inbound/outbound `wamid` as the turn happens.
- If Dashboard and WhatsApp diverge, run **thread provenance RCA** before any Brain/Prompt change.

---

## Goal

Not score-chasing. Build a system that trusts structured facts and correct tools, and leaves the model free to reason and phrase — without hardcoding, special paths, or per-incident exceptions.

---

## Principle of Evidence

> **The burden of proof is on the proposed fix, not on the observed symptom.**
>
> Every code change must be justified by evidence identifying the first verified divergence from the expected execution path. Symptoms alone are never sufficient justification for modifying AI behavior.

This keeps investigation anchored on: **Where did execution first leave the correct path?** — not on “the reply looked wrong, so change the Prompt.”
