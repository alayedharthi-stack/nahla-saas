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

## Mandatory Natural Language Rule

Hardcoded **customer-facing conversational templates** are prohibited in normal AI runtime flows.

### Ownership split

**Normal AI runtime customer-facing conversational prose is LLM-owned.**

The **platform** owns:

- trusted facts
- routing
- state
- verified values
- constraints
- structured actions
- CTA/vCard payloads
- execution state
- truth and safety

The **LLM** owns:

- greetings
- apologies
- acknowledgements
- explanations
- clarifications
- transitions
- concise conversational wording

The platform must pass **trusted facts** and **structured actions** to the LLM. The LLM must compose customer-facing wording naturally (tone, greetings, apologies, transitions, concise context-aware phrasing).

### Prohibited in normal AI runtime

**Do not add or use in normal runtime paths:**

- fixed customer-facing replies
- deterministic prose builders
- hardcoded greetings, apologies, or clarifications
- hidden template pools
- fixed operational sentences
- postprocessors that replace valid LLM text with deterministic prose
- dedup substitutes that introduce fixed conversational wording
- labeling a primary deterministic path as an emergency fallback
- fixed showroom/location paragraphs or arrival replies
- fixed employee-contact sentences
- deterministic prose builders that only substitute merchant facts (name, branch, URL, phone)
- reusable canned conversational paragraphs or template pools hidden inside runtime code

**Before adding any customer-facing text constant, ask:**

> Is this exact wording required by law, security, protocol, safety, or an explicitly approved merchant/system template?

If **no** — do not add fixed wording; pass structured facts to a grounded compose surface and let the LLM phrase the response.

### Allowed exact-text exceptions only

1. Nahla Templates Library
2. Templates explicitly created or approved by the merchant
3. Official WhatsApp/Meta templates
4. OTP / authentication messages
5. Legally required notices
6. Security, payment, consent, or safety notices requiring exact deterministic wording
7. A **minimal emergency fallback** used only after a genuine natural-compose failure

Closed registry: `backend/modules/ai/compose/constitutional_policy.py` (`DETERMINISTIC_EXCEPTIONS`).

### Emergency fallback requirements

- composition attempted first
- one short factual line
- trusted facts only
- no invented facts
- not the primary path
- metadata must record:
  - `compose_source=fallback_deterministic`
  - `fallback_reason`
  - `fallback_action_type`
  - `chosen_path`
- measurable and auditable in production

### Runtime metadata contract

For normal AI customer replies, require auditable metadata:

- `compose_source`
- `response_mode`
- `chosen_path`
- `llm_candidate_present`
- `final_text_transformed`
- `final_transform_reasons`

For deterministic fallback additionally require:

- `fallback_reason`
- `fallback_action_type`
- evidence that natural composition failed before fallback

Allowed `compose_source` values (closed):

- `llm` / `persona_llm`
- `merchant_template`
- `meta_template`
- `legal_exact_text`
- `security_exact_text`
- `fallback_deterministic`

Do not accept ambiguous values such as `template` without an approved exception class.

### No phrase-ban fixes

Fix ownership, state, evidence, routing, composition, and postprocessing. Paraphrasing the same deterministic action remains a violation.

### CI enforcement

Required check: **`constitution-compliance`** (`backend/tests/test_constitution_compliance.py`).

Pre-existing violations must be tracked in `TRACKED_VIOLATIONS` with violation ID, owner, expiry, and removal PR — never silently grandfathered.

### AI PR review checklist

Permanent checklist: `docs/engineering/ai-pr-constitution-checklist.md`. A PR cannot receive PASS or merge approval without completing it.

This rule cannot be bypassed for convenience, fewer files, lower latency, or easier testing.

### Testing guidance

Assert **behavior and structured delivery** (CTA URL, vCard payload, routing, metadata, tenant isolation). Do **not** assert exact Arabic sentences unless the exact phrase is the bug under test.

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

## Generic Commerce Regression Tests

Nahla AI is a **multi-merchant** WhatsApp commerce platform. Every AI commerce fix and regression test must be **platform-wide and merchant-agnostic**.

### Core rule

Do not make runtime logic, tests, prompts, or examples behave like one honey store or one merchant (e.g. Al Ayed) only. The system must work across normal catalog merchants: food, clothing, shoes, perfumes, cosmetics, accessories, gifts, and similar categories.

### Test data policy

When adding regression tests, do **not** rely only on production-store examples such as sidr, honey-only product names, one real customer name, or one city.

For every platform-level behavior fix, include **at least one neutral generic commerce scenario**, for example:

- Merchant: `متجر تجريبي عام`
- Product: `حذاء رياضي أبيض` / `قميص قطني أزرق` / `عطر ورد 100ml`
- Customer: generic verified profile (e.g. `أحمد سالم`, `نورة عبدالله`)
- City / short code: generic values (e.g. `الرياض` + `RRRD1234`)

Rotate categories across tests when product examples are needed.

### Required coverage for AI commerce regressions

For every root fix in OrderFlowV2, catalog checkout, customer context, tracking, payment, shipping, availability, or FAQ:

1. Add a regression test for the **real observed case** (if one exists).
2. Add **at least one generic merchant/category test** proving the fix is not category-specific.
3. Avoid hardcoding product keywords in runtime logic.
4. Assert **system behavior and state**, not rigid Arabic phrases, unless the phrase itself is the bug.
5. Phrase triggers (e.g. previous-address claims) may appear in tests, but **truth must come from persisted state** (`CustomerAddress`, profile, order context) — not from the phrase alone.

### Guards unchanged by test additions

Regression tests must not bypass or weaken: `store_ai_enabled`, `store_ai_mode`, pause, handoff, blocklist, subscription, dry-run/playground boundaries, or real WhatsApp provider calls.

---

## Reference Implementations

These slices apply the doctrine correctly:

| Domain | Pattern |
|--------|---------|
| Shipment | `shipment_evidence.py` + `shipment_truth_guard.py` |
| Payment | `payment_evidence.py` + `payment_reply_guard.py` (tightened) |
| Staff escalation (planned) | Same pattern — evidence helper + post-compose guard |

Do not solve operational truthfulness with prompting alone. Do not solve personality with larger template pools.
