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

CI job: **`constitution-compliance`** (`backend/tests/test_constitution_compliance.py`).

`constitution-compliance` is currently a **required merge-blocking check on `main`** through GitHub branch protection. It must be green together with the other required checks (`lint-and-test`, `Scan repository for leaked secrets`). Repository files cannot themselves enforce GitHub branch protection; that configuration lives in GitHub. See `docs/engineering/merge-and-ci-policy.md`.

GOV-002 makes GOV-001 executable: the same `constitution-compliance` job runs a **trusted BASE scanner** (`scripts/lint_intelligence_non_interference.py` loaded from the PR base commit, not HEAD) before the constitutional pytest suite. A PR cannot pass merely by declaring `CUSTOMER_REGEX_CHANGED=NO`. After GOV-002 is on `main`, `TRUSTED_BASE_SCANNER_REQUIRED=yes`. `BASE_NOT_AVAILABLE` fails closed.

Owner exceptions cannot be created in the same PR as the runtime change. Authorization-only PRs must have zero AI runtime changes. Protected tests marked `governance_contract` cannot be removed or weakened without a pre-existing BASE exception.

A partial first-divergence repair may remain Draft, but must not merge or deploy when deterministic replay proves the customer-visible path becomes worse at the next known owner.

Pre-existing violations must be tracked in `tracked_violations_baseline.json` with violation ID, owner, `added_at`, `expiry_date`, `approved_by`, and removal reference — never silently grandfathered. New violation IDs require `governance_baseline_version` bump in a dedicated governance PR.

### AI PR review checklist

Permanent checklist: `docs/engineering/ai-pr-constitution-checklist.md`.

**Merge gate note:** `constitution-compliance` is a required merge-blocking check on `main` and must be green on the PR together with the other required checks. A PR cannot receive PASS or merge approval without completing this checklist.

### Final customer text provenance rule

Any review of a PR that touches AI, conversation, compose, routing, dedup, sanitizer, guards, or templates is **incomplete** until the reviewer identifies the **true source** of the final customer-facing text.

The reviewer must state explicitly:

- Did the text come from the **LLM**?
- From a **template** (and which approved exception class)?
- From a **sanitizer** replacement?
- From **dedup** substitution?
- From an emergency **fallback**?
- From a **guard** rewrite?
- Or was a valid LLM candidate **replaced** somewhere along the path?

Trace the full path: **decision → facts → compose → guards → sanitizer → dedup → wire**.

This rule exists because constitutional violations such as `track_order_not_found` hid inside deterministic compose paths until the final outbound source was traced. If this rule had been applied from the start, the violation would have surfaced in the first review.

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

## Root Cause First (permanent engineering policy)

Authoritative policy: `docs/engineering/root-cause-first-policy.md`  
Agent rule: `.cursor/rules/root-cause-first.mdc`

**Live-test failures must not drive MerchantBrain / Prompt / Compose / State / Memory patches** until Channel → Webhook → Tenant → Identity → Persistence → Retrieval → State → Tools → Structured Facts are proven healthy. Stop at the first failing layer. RCA must state First Divergence, Source of Truth, Provenance, and Evidence. Classify as LLM Behavior only after all lower layers pass.

**Principle of Evidence:** The burden of proof is on the proposed fix, not on the observed symptom. Symptoms alone never justify modifying AI behavior.

---

## GOV-001 — Intelligence Non-Interference Policy (permanent)

Authoritative policy: `docs/engineering/intelligence-non-interference-policy.md`  
Agent rule: `.cursor/rules/intelligence-non-interference.mdc`

```text
INTELLIGENCE_POLICY=KEEP_MODEL_FREE_FIX_SYSTEM_AROUND_IT
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE

MODEL_CHANGE=FORBIDDEN_BY_DEFAULT
PROMPT_CHANGE=FORBIDDEN_BY_DEFAULT
PERSONA_CHANGE=FORBIDDEN_BY_DEFAULT

PHRASE_MAPS=FORBIDDEN
KEYWORD_INTENT_HACKS=FORBIDDEN
CUSTOMER_REGEX_INTENT_REPAIR=FORBIDDEN

DEFAULT_FIX_ORDER=
STATE → TRUTH → CONTEXT → ROUTING → CAPABILITY → EXECUTION → PERSISTENCE → POSTPROCESS
→ ONLY THEN RAW MODEL EVALUATION
```

Every Nahla AI defect assignment, Agent task, and AI PR must report:

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```

Expected default for all change flags: **NO**.

Model, prompt, or persona may be touched only if correct authoritative context and capabilities reached the model, **and** raw model output is the first executable divergence, **and** the owner explicitly approves.

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
