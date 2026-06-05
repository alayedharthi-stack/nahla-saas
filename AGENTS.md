# Nahla Engineering Doctrine

This document is the architectural foundation for all Nahla platform work. Every change — guards, persona, routing, integrations — must align with these rules.

## Core Principle

```
Operations  → evidence + state + guards (deterministic)
Personality → persona + context (non-deterministic)
Scope       → platform-wide, not merchant-specific
```

### Core Rules

1. **Operational correctness may be deterministic.**
2. **Personality must never be deterministic.**
3. **Solutions must be platform-wide, not merchant-specific.**

### Arabic Version

```
صحة العمليات يمكن أن تكون حتمية.
الشخصية لا يجب أن تكون حتمية.
الحلول يجب أن تكون عامة على مستوى المنصة، وليست خاصة بتاجر واحد.
```

---

## What This Means

### Operations

Anything that represents a **fact, status, action, or business outcome** must be based on **evidence, state, or deterministic validation**.

Examples:

- Payments
- Receipts
- Orders
- Shipping
- Tracking
- Escalations
- Staff contact workflows
- Pricing
- Coupons
- Inventory
- Integrations
- Notifications

**Operational claims must match operational reality.**

The system must never claim that something happened unless there is evidence or state proving it happened.

**If behavior is operational:**

- Use evidence helpers
- Use state
- Use guards
- Use deterministic validation
- Never rely on LLM wording alone

### Personality

Anything related to **conversation, identity, humor, greetings, social interaction, and human warmth** should be driven by **persona and context**, not by rigid templates or deterministic rules.

Examples:

- Greetings
- Identity questions
- Small talk
- Humor
- Compliments
- Social conversation

The goal is **natural conversation**, not scripted conversation.

**If behavior is personality:**

- Use persona compose
- Use context
- Avoid rigid template pools
- Avoid deterministic warmth/social replies

---

## Platform-Wide Thinking

Every fix must solve the **root cause at the platform level**.

A problem discovered in one merchant should be analyzed as a **platform problem first**.

| Wrong objective | Correct objective |
|-----------------|-------------------|
| Make this merchant work. | Make Nahla work correctly for every merchant. |

**If a fix is implemented:**

- It must work for all merchants
- Avoid tenant-specific hardcoding
- Avoid merchant-specific assumptions
- Use tenant KB/config where merchant differences are required

---

## Claim Rule

**If the system claims something happened, evidence or state must exist.**

| Claim | Requires |
|-------|----------|
| «تم الشحن» | Shipment evidence (tracking, structured status, trusted automation) |
| «وصل الإيصال» | Payment evidence (`confirmed`, receipt media, deterministic ack path) |
| «تم تحويلك للدعم» | Operational escalation evidence (handoff session, notification, staff contact event) |

**If evidence does not exist, the claim must not exist.**

Guards may replace false claims with soft, honest wording. They must not silence the AI.

---

## AI Continuity Rule

Staff escalation must **not** automatically stop AI.

- Do **not** use `ai_paused` as the default escalation solution
- AI should continue helping the customer while operational escalation is handled truthfully
- Escalation truthfulness and AI continuity are complementary, not mutually exclusive

---

## Validation Questions (Before Any PR)

1. Is this **operational** or **personality** behavior?
2. If operational, **what evidence or state** proves it?
3. If personality, **why is persona compose not enough**?
4. Would this still be correct for **10,000 merchants**?
5. Does this introduce **tenant-specific logic**?
6. Does the system **claim something happened without evidence**?

---

## Final Rule

```
Fix root causes.
Protect operations with evidence.
Protect personality with persona.
Build for the platform, not for a single merchant.
```

---

## Reference Implementations

These slices apply the doctrine correctly:

| Domain | Pattern |
|--------|---------|
| Shipment | `shipment_evidence.py` + `shipment_truth_guard.py` |
| Payment | `payment_evidence.py` + `payment_reply_guard.py` (tightened) |
| Staff escalation (planned) | Same pattern — evidence helper + post-compose guard |

Do not solve operational truthfulness with prompting alone. Do not solve personality with larger template pools.
