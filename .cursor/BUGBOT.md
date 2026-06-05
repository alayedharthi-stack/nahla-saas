# Nahla Bugbot Review Constitution

This file is a **top-priority governance document** for Cursor Bugbot reviews on the Nahla platform. It extends and enforces `AGENTS.md` (Nahla Engineering Doctrine). When any rule here conflicts with a generic code-quality suggestion, **this file wins**.

---

## Core Principles (Non-Negotiable)

```
Operations  → evidence + state + guards (deterministic)
Personality → persona + context (non-deterministic)
Scope       → platform-wide, not merchant-specific
```

1. **Operational correctness may be deterministic.**
2. **Personality must never be deterministic.**
3. **All fixes must be general and platform-wide.**
4. **Never introduce tenant-specific, customer-specific, conversation-specific, phrase-specific, or keyword-specific logic.**
5. **Tenant 33 is a real test store, not an architectural reference.** Do not treat Tenant 33 traces, scripts, or regression comments as justification for special-case code paths.
6. **Every issue must be investigated through root-cause analysis.**
7. **Fix systems, not symptoms.**
8. **Improve architecture, decision models, safety systems, scalability, and platform-wide behavior.**
9. **Reject hardcoded patches and special-case logic.**
10. **Every solution must scale from 1 store to 10,000+ stores.**

---

## Review Mandate

Bugbot must review every PR change through the lens of **platform integrity**, not local convenience. Before approving or suggesting a fix, ask:

1. Is this **operational** or **personality** behavior?
2. If operational, **what evidence or state** proves the claim or transition?
3. If personality, **why is persona compose not enough** — and does the PR actually target persona?
4. Would this still be correct for **10,000 merchants**?
5. Does this introduce **tenant-specific, customer-specific, phrase-specific, or keyword-specific logic**?
6. Does the system **claim something happened without evidence**?

---

## Protected Domains (Flag Violations Aggressively)

### Customer identity integrity

- All customer resolution must go through unified, tenant-scoped identity paths (e.g. `CustomerIntelligenceService.upsert_customer_identity`).
- Flag any bypass of the unified upsert path, raw `Customer(...)` inserts, or duplicate-identity creation.
- Flag phone normalization mismatches (raw vs normalized vs E.164) that can split one person into multiple customer rows.
- Flag writes that merge, overwrite, or orphan identity fields without deterministic resolution order.
- Flag missing `tenant_id` scoping on customer lookups, updates, or joins.

### CRM persistence

- Flag changes that skip, truncate, or conditionally omit CRM writes (customer profile, metadata, interaction timestamps, external IDs).
- Flag silent failure paths where CRM state is lost instead of surfaced (HTTP errors, swallowed exceptions, early returns).
- Flag migrations or backfills that drop or rewrite customer data without reversible, auditable steps.
- Flag `last_interaction_at` or acquisition-channel updates driven by the wrong event source (e.g. merchant echo treated as customer activity).

### Conversation linking and relinking

- Flag logic that creates orphan conversations, breaks conversation ↔ customer FK integrity, or relinks without evidence.
- Flag ambiguous conversation selection (multiple open threads, wrong thread reuse) without deterministic selection rules.
- Flag recovery/relink paths that can attach inbound messages to the wrong customer or tenant.
- Reference: conversation recovery and mode routing must preserve traceability.

### Ownership-state correctness

- Flag changes to ownership / takeover / release logic that rely on LLM wording, keyword triggers, or merchant-specific heuristics.
- Ownership transitions must be state-driven and auditable (see `ownership_state` patterns).
- Flag implicit takeover or release without logged reason and before/after state.
- Flag use of `ai_paused` as the default escalation solution (AI continuity rule).

### WhatsApp, Meta, and Salla integrations

- **WhatsApp / Meta:** Flag webhook routing that guesses tenant when `phone_number_id` is ambiguous — the system must **drop, not guess** (0 or >1 match).
- Flag violations of one-`phone_number_id`-per-tenant and one-`waba_id`-per-tenant invariants.
- Flag reconnect paths that silently overwrite another tenant's connected identity instead of failing with HTTP 409 or running controlled eviction.
- **Salla:** Flag store-claim bypasses (`assert_store_not_claimed`), cross-tenant store binding, or external_id resolution without tenant scope.
- Flag integration token, webhook, or OAuth handling that can attach resources to the wrong tenant.
- Reference implementations: `backend/core/tenant_integrity.py`, `docs/TENANT_INTEGRITY.md`.

### Tenant isolation

- Flag any query, cache key, job payload, or webhook handler that can read or write across tenants.
- Flag shared mutable state (in-memory globals, module-level caches) keyed without `tenant_id`.
- Flag admin or reconciliation paths that move data without dry-run, audit logging, or explicit operator intent.
- Flag missing guards on write paths listed in tenant integrity documentation.

---

## Critical Risk Classes (Always Elevate Severity)

When reviewing diffs, **actively hunt** for these failure modes and report them as high-severity findings:

| Risk | What to flag |
|------|----------------|
| **Data-loss** | Deletes, overwrites, upserts without merge strategy, migrations without backfill, `ON DELETE CASCADE` surprises, silent no-ops |
| **Customer disappearance** | Filters that hide customers from CRM, failed upserts with swallowed errors, identity splits, orphaned FKs |
| **Identity corruption** | Conflicting external IDs, phone/name overwrite without evidence, duplicate rows, metadata promotion bugs |
| **Cross-tenant leakage** | Missing `tenant_id` filter, ambiguous webhook resolution, shared identifiers across tenants, wrong tenant in async jobs |

---

## Operational vs Personality Boundary

### Operational (deterministic)

Payments, receipts, orders, shipping, tracking, escalations, staff contact, pricing, coupons, inventory, integrations, notifications — **must use evidence, state, guards, deterministic validation**. Never rely on LLM wording alone.

**Claim rule:** If the system claims something happened, evidence or state must exist.

| Claim | Requires |
|-------|----------|
| Shipment sent | Shipment evidence (tracking, structured status, trusted automation) |
| Receipt received | Payment evidence (`confirmed`, receipt media, deterministic ack path) |
| Escalated to support | Operational escalation evidence (handoff session, notification, staff contact event) |

Reference patterns: `payment_evidence.py` + `payment_reply_guard.py`, `shipment_evidence.py` + `shipment_truth_guard.py`.

Flag PRs that solve operational truthfulness with prompting alone or with larger template pools.

### Personality (non-deterministic)

Greetings, identity questions, small talk, humor, compliments, social conversation — **persona compose + context only**.

**Do not suggest personality changes unless the PR explicitly targets persona behavior.**

Flag:

- Rigid template pools or deterministic warmth/social reply rules added for operational fixes
- Keyword/phrase-based routing disguised as "personality"
- Hardcoded Arabic/English phrase lists used to drive business logic

---

## Fix Philosophy (How Bugbot Must Suggest Remediation)

### Prefer

- **Root-cause fixes** over symptom patches
- **Architectural solutions** (evidence helpers, guards, state machines, integrity guards) over hardcoded exceptions
- **Minimal safe changes** over broad rewrites
- **Platform-wide invariants** enforced at service/domain layer
- **Tenant KB/config** where merchant differences are legitimately required — not inline `if tenant_id == …`

### Reject and block suggestions that

- Hardcode a tenant ID, store ID, phone number, customer ID, or conversation ID
- Add `if tenant_id == 33` (or any specific tenant) branches
- Patch a single merchant's wording, SKU, or workflow
- Match specific Arabic/English phrases or keywords to alter routing, guards, or state
- Add conversation-specific or customer-specific exceptions
- Copy a production trace from Tenant 33 into permanent architecture
- Silence the AI instead of replacing false operational claims with honest wording

---

## Scalability Gate

Before endorsing any fix, verify:

- Works for **1 store and 10,000+ stores** without per-tenant branching
- Does not increase **O(n)** scans over tenant tables where indexed tenant-scoped lookups exist
- Does not introduce unbounded in-memory growth per webhook or message
- Does not require manual ops intervention per merchant for the fix to hold

If a proposed fix only works when an operator manually reconciles one store, **reject it** and recommend a platform invariant or automated guard instead.

---

## Files and Patterns Worth Checking on Related Changes

When the PR touches these areas, apply extra scrutiny:

| Area | Key locations |
|------|----------------|
| Tenant integrity | `backend/core/tenant_integrity.py`, `docs/TENANT_INTEGRITY.md` |
| Customer identity | `backend/services/customer_intelligence.py`, `backend/routers/conversations.py` |
| WhatsApp ingest | `backend/routers/whatsapp_webhook.py` |
| Ownership state | `backend/core/ownership_state.py` |
| Payment truth | `backend/services/payment_evidence.py`, payment reply guards |
| Shipment truth | `backend/services/shipment_evidence.py`, shipment truth guards |
| AI routing / gating | `backend/modules/ai/routing/` |
| Doctrine | `AGENTS.md` |

---

## Bugbot Comment Standards

When flagging issues:

1. **Name the invariant violated** (e.g. tenant isolation, claim rule, identity integrity).
2. **Explain the failure mode** at scale (what breaks at 10,000 stores, not just one trace).
3. **Recommend a platform-wide fix** — evidence helper, guard, state check, or integrity guard — not a special case.
4. **Distinguish operational vs personality** and route the fix to the correct layer.
5. **Do not recommend persona/template changes** for operational bugs.

When a PR aligns with this constitution, say so briefly. Do not nitpick style when platform integrity is preserved.

---

## Final Rule

```
Fix root causes.
Protect operations with evidence.
Protect personality with persona.
Build for the platform, not for a single merchant.
Protect customer identity, CRM data, conversations, ownership state, integrations, and tenant isolation on every review.
```

---

## Nahla System Principle

We do not fix a conversation.

We do not fix a store.

We fix the system.

Every accepted fix must improve Nahla for all merchants, not only for the case that exposed the issue.
