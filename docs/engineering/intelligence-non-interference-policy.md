# GOV-001 — Intelligence Non-Interference Policy

**Status:** Adopted (permanent governance)  
**ID:** GOV-001  
**Effective:** 2026-09-01  
**Arabic title:** سياسة عدم التدخل في الذكاء

This document is law. It does not change runtime Brain, coupons, routing, or production settings.

Authoritative companions:

- `AGENTS.md`
- `.cursor/rules/intelligence-non-interference.mdc`
- `docs/engineering/root-cause-first-policy.md`
- `docs/engineering/ai-pr-constitution-checklist.md`

---

## Explicit contract

```text
INTELLIGENCE_POLICY=KEEP_MODEL_FREE_FIX_SYSTEM_AROUND_IT

MODEL_CHANGE=FORBIDDEN_BY_DEFAULT
PROMPT_CHANGE=FORBIDDEN_BY_DEFAULT
PERSONA_CHANGE=FORBIDDEN_BY_DEFAULT

PHRASE_MAPS=FORBIDDEN
KEYWORD_INTENT_HACKS=FORBIDDEN
CUSTOMER_REGEX_INTENT_REPAIR=FORBIDDEN

DEFAULT_FIX_ORDER=
STATE
→ TRUTH
→ CONTEXT
→ ROUTING
→ CAPABILITY
→ EXECUTION
→ PERSISTENCE
→ POSTPROCESS
→ ONLY THEN RAW MODEL EVALUATION

MODEL/PROMPT MAY BE TOUCHED ONLY IF:
correct authoritative context and capabilities reached the model
AND
raw model output itself is proven to be the first executable divergence
AND
owner explicitly approves the exception.
```

---

## Mandatory report for every Agent / assignment / AI PR

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

Any **YES** requires the model-touch exception gate below plus `OWNER_APPROVAL_REQUIRED=YES`.

---

## Core principle

The model is not the default repair surface.

```text
KEEP THE MODEL FREE.
FIX THE SYSTEM AROUND THE MODEL.
```

```text
MODEL UNDERSTANDS.
PLATFORM PROVIDES TRUTH, CONTEXT, STATE AND CAPABILITIES.
```

The model must receive the current customer goal, correct context, correct state, trusted facts, correct merchant data, appropriate tools and capabilities, and correct tool results. Then the model understands and composes naturally.

Law stays in governance. System defects are fixed in engineering PRs. Nobody may change intelligence in order to paper over a platform bug.

---

## Do not repair the model

Do not start any RCA or fix by changing:

```text
system prompt
developer prompt
persona
model
temperature
reasoning style
response style
wording strategy
language style
```

Do not use the prompt to hide defects in state, routing, truth, context, capability, tool execution, persistence, ownership, or post-processing.

If the model received wrong, missing, or stale information, fix the source or the path that delivered it.

Do not tell the model via prompt to "ignore" a wrong state while the platform still sends that state.

---

## No intent hard-coding

Forbidden as intelligence repair:

- customer phrase maps
- keyword routing
- regex customer-intent repair
- Arabic word lists
- canned response trees
- product-specific phrase rules
- bank-specific phrase rules
- tenant-specific / customer-specific / phone-specific hacks

Forbidden examples:

```text
if "الراجحي" in message:
if "عسل الطلح" in message:
if "ابي احول" in message:
```

Those phrases may be used only as test examples, live evidence, or acceptance cases. They must not become architectural logic that decides what the customer meant.

---

## Pre-model fix first

```text
customer message
→ persisted state
→ active goal
→ truth sources
→ trusted context
→ BrainContext
→ capability exposure
→ decision / ownership
→ executor / tools
→ model
```

If any part fails before the request reaches the model: fix there. Do not modify model behavior.

---

## Post-model fixes

Downstream layers may be fixed when they own the defect: executor results, tool-result validation, persistence, transport, WhatsApp formatting, media, security/truth guards, deterministic post-processing.

```text
POSTPROCESS MAY ENFORCE TRUTH, SAFETY, VALIDITY OR DELIVERY CONTRACTS.
POSTPROCESS MUST NOT BECOME A SECOND HARD-CODED BRAIN.
```

If postprocess turns a correct reply into a wrong one, fix the postprocess owner. Do not change the model prompt so it avoids the wrong guard.

---

## Model-blame gate

Do not conclude `THE MODEL DID NOT UNDERSTAND` until all of the following are proven:

1. The latest explicit customer goal reached the model correctly.
2. All authoritative merchant/customer facts needed for the answer reached the model.
3. No stale state was presented as current authoritative state.
4. The correct capability/domain owner was selected.
5. Required tools/executors ran correctly.
6. Tool results reached compose correctly.
7. No deterministic guard or postprocessor corrupted or replaced the response afterward.

If those conditions are not met: `MODEL_CHANGE=OUT_OF_SCOPE`.

Model, prompt, or persona may be touched only if:

1. correct authoritative context and capabilities reached the model, AND
2. raw model output itself is proven to be the first executable divergence, AND
3. the owner explicitly approves the exception.

---

## First-divergence rule

Every RCA must find `FIRST_PLATFORM_DIVERGENCE`, not only the last wrong reply.

Example: the customer asks for Product B, the platform keeps Product A active, BrainContext receives Product A, the model talks about Product A. The defect is state/ownership before the model.

Example: the customer asks for a bank account, merchant bank facts never enter BrainContext, contact fallback owns the turn. Fix truth/context/ownership/routing. Do not add prompt instructions about bank names.

---

## Model freedom

When the correct truth reaches the model, let the model choose natural wording. Do not impose fixed customer sentences unless law, security, or an explicit product contract requires exact text. Give structured facts and let the model ask naturally.

---

## Architectural responsibility

```text
MODEL OWNS:
- language understanding
- semantic interpretation
- natural response composition
- conversational flexibility

PLATFORM OWNS:
- truth
- state
- permissions
- tenant isolation
- merchant configuration
- available capabilities
- tool execution
- deterministic business rules
- persistence
- delivery
- safety/truth enforcement
```

Do not make the model compensate for platform gaps. Do not make the platform rebuild model intelligence with word rules.

---

## Final engineering rule

```text
DO NOT TEACH THE MODEL AROUND PLATFORM BUGS.
DO NOT RESTRICT THE MODEL TO COMPENSATE FOR BAD STATE.
DO NOT HARD-CODE CUSTOMER LANGUAGE TO SIMULATE INTELLIGENCE.
DELIVER THE RIGHT TRUTH, STATE, CONTEXT, AND CAPABILITIES TO THE MODEL.
THEN LET THE MODEL THINK AND RESPOND FREELY.
IF SOMETHING BREAKS AFTER THE MODEL, FIX THAT DOWNSTREAM OWNER.
KEEP THE MODEL FREE.
FIX THE SYSTEM AROUND THE MODEL.
```

---

## GOV-002 — executable enforcement

GOV-001 is law. GOV-002 is the CI mechanism that inspects the actual diff.

Authoritative scanner: `scripts/lint_intelligence_non_interference.py`

On `pull_request` CI for **future** PRs, the scanner is loaded from **BASE**.
PR **#924** is the bootstrap: BASE has no scanner, so
`BOOTSTRAP_HEAD_TRUST_EXCEPTION=YES_ONE_TIME` — the HEAD scanner is used
because this PR is owner-reviewed. After #924:

```text
TRUSTED_BASE_SCANNER_REQUIRED=yes
```

Workflow YAML in `.github/workflows/ci.yml` is still PR-controlled for the
legacy `constitution-compliance` job. The dedicated trust root is
`.github/workflows/gov002-intelligence-non-interference.yml` on
`pull_request_target` (Actions loads BASE YAML) plus a repository ruleset
workflow pin documented in `docs/engineering/gov002-workflow-trust-root.md`.

A PR must not edit the guard in the same change and teach it to accept the
violation.

`HEAD_SCANNER_CAN_SELF_BYPASS=no` is **not** claimed at the complete workflow
level until the ruleset/required-check readback proves
`HEAD_CAN_SKIP_GOV002_AND_STILL_SATISFY_REQUIRED_CHECK=no`.

Wired into the required **`constitution-compliance`** job — not an optional check.

### Change classes (default FAIL without a BASE-owned exception)

`MODEL_CHANGE` `PROMPT_CHANGE` `PERSONA_CHANGE` `PHRASE_MAP_CHANGE` `KEYWORD_ROUTER_CHANGE` `CUSTOMER_REGEX_CHANGE` `CANNED_REPLY_CHANGE` `TENANT_SPECIFIC_SEMANTIC_CHANGE` `PHONE_SPECIFIC_SEMANTIC_CHANGE` `PRODUCT_SPECIFIC_SEMANTIC_CHANGE`

Customer wording is reproduction/regression/acceptance input only. It is not the repair mechanism.

Structured evidence serialization (`customer_order_evidence`, merchant truth, tool facts) is not automatically `PROMPT_CHANGE`. URL/ID/parser regex outside customer-semantic ownership is not automatically `CUSTOMER_REGEX_CHANGE`.

### Owner exceptions — anti-self-waiver

Registry: `backend/modules/ai/governance/intelligence_exceptions.json`

The trusted scanner reads exceptions from **BASE**. Matching is exact:

```text
change_class + exact_file_scope + exact_symbol_scope + expected_change_digest
```

`exact_reason` is descriptive only. `owner_approval_ref`, `expires_at`,
`expected_change_digest`, `single_use`, and `consumed` are required.
Malformed rows fail closed. Digest mismatch does not authorize. Consumed or
expired exceptions do not authorize. Same file + same class + a
different change is a new digest and fails. Implementation + new exception in
the same PR is `SAME_PR_SELF_WAIVER`. Historical commit `02aff345` is
retrospective-only and is not a runtime registry waiver.

Sequence:

```text
authorization-only PR (AUTHORIZATION_PR_RUNTIME_AI_CHANGES=ZERO)
→ merge
→ implementation PR
→ BASE scanner sees the authorization
```

### Protected contracts

Tests marked `pytest.mark.governance_contract` (and the initial Order Support / Product Information modules listed in the registry) are fingerprinted from BASE. Removal, rename-to-evade, or assertion/fixture weakening fails unless a pre-existing BASE exception exists.

### Partial-repair release safety

A partial first-divergence repair may remain Draft. It must **not** merge or deploy when deterministic replay proves the customer-visible path becomes worse at the next known owner. Combining a semantic-ownership production change with protected-contract weakening is `UNSAFE_PARTIAL_REPAIR`.
