# FactBoundPersonaComposer — Runtime Rollout Plan

**Status:** Phase 2 design (no runtime in this document)  
**Prerequisite:** Phase A.1 complete (social/phatic bypass, checkout-pressure guards, green `main` CI, branch protection)  
**Companion:** `docs/architecture/fact-bound-persona-composer-design.md` (contracts audit)  
**Policy:** `docs/architecture/nahla-ai-merchant-assistant-policy.md` §5.1, §11–§11.3  
**Doctrine:** `AGENTS.md`

---

## 1. Product goal

`FactBoundPersonaComposer` is the **runtime layer** that lets Nahla AI speak like a natural Saudi merchant assistant while preserving **operational truth**.

```
Facts are deterministic.
Language is natural.
Templates are fallback, not the product experience.
```

### Must not

- Invent facts (prices, availability, tracking, payment status, credentials)
- Change routing decisions or turn ownership
- Change selected assets (`media_key`, bank label, product, payment method)
- Create orders or advance checkout slots
- Override safety guards or kill-switches
- Become a template engine or fixed phrase pool

### May

- Phrase **verified facts** naturally
- Vary wording safely across turns (non-deterministic when compose succeeds)
- Use Saudi Arabic tone for Arabic surfaces
- Use natural professional English for English surfaces
- Apply light marketing warmth when context allows
- Optionally use **0–1** context-appropriate emoji (guards enforce limits)
- Keep replies short and human

---

## 2. Runtime contract

### 2.1 Input — `PersonaFactsBundle`

| Field | Source | Notes |
|-------|--------|-------|
| `surface` | Caller (Brain / webhook hook) | See surface inventory below |
| `verified_facts` | Deterministic system code only | JSON-serializable; hashed for observability |
| `customer_context` | Profile / order prep / resolver | Name, open-order hint — never guessed |
| `merchant_persona` | Tenant `ai_settings` slice | Assistant name, store tone — no secrets |
| `language_policy` | Inbound dominance detector | `ar` / `en` / mixed-dominant |
| `constraints` | Surface defaults + tenant caps | `max_chars`, emoji policy, banned sets |
| `banned_claims` | Policy + surface | IBAN, account number, payment link, unverified success |
| `guard_requirements` | Surface | e.g. `payment_credential`, `no_checkout_pressure` |
| `fallback_policy` | Surface + flags | Which deterministic fallback key to use on failure |

```python
@dataclass(frozen=True)
class PersonaFactsBundle:
    surface: str
    verified_facts: dict[str, Any]
    customer_context: dict[str, Any]
    merchant_persona: dict[str, Any]
    language_policy: str
    constraints: PersonaConstraints
    banned_claims: frozenset[str]
    guard_requirements: frozenset[str]
    fallback_policy: str
```

### 2.2 Surfaces (Phase 2 scope in **bold**)

| Surface | Phase |
|---------|-------|
| **`social_greeting`** | 2A |
| **`social_checkin`** | 2A |
| **`thanks`** | 2A |
| **`dua`** | 2A |
| **`payment_media_intro`** | 2B |
| `kb_answer` | 3 |
| `product_answer` | 3 |
| `order_status` | 4 |
| `checkout_prompt` | 4+ (after more grounding) |

### 2.3 Output — `PersonaComposeResult`

| Field | Purpose |
|-------|---------|
| `text` | Customer-facing reply |
| `source` | `persona_llm` \| `fallback_deterministic` |
| `surface` | Echo input surface |
| `facts_hash` | `sha256(canonical_json(verified_facts))` |
| `guard_passed` | All post-compose guards passed |
| `language` | `ar` \| `en` |
| `dialect` | `saudi_arabic` when `language=ar`; else `""` |
| `emoji_count` | For density guard + metrics |
| `fallback_reason` | Set when `source=fallback_deterministic` |
| `latency_ms` | Compose round-trip |
| `model` | Model id used (no secrets) |

---

## 3. Phase 2 first surfaces

### Surface A — social / thanks / dua (low risk)

**Examples:** السلام عليكم · كيف الحال · شكراً · الله يعطيك العافية · انت وش أخبارك؟

| Rule | Requirement |
|------|-------------|
| Order creation | **Forbidden** |
| Checkout pressure | **Forbidden** (address, payment, name slot, نكمل الطلب) |
| Slot prompts | **Forbidden** on pure phatic turns |
| Fake facts | **Forbidden** |
| Arabic | Saudi Arabic default |
| English | Natural professional English |
| Emoji | Optional 0–1; guards enforce |
| Wording | Non-deterministic when LLM compose succeeds |
| Failure | Deterministic safe fallback only (`social_mirror`, thanks/dua emergency paths) |

**Integration points (future runtime PRs):**

- Wrap or replace `pick_persona_greeting` / `pick_persona_social_reply` **only when flag enforced**
- Continue post-compose `social_checkout_pressure_guard` on all outbound (Phase A.1)
- OrderFlowV2 owns true checkout continuations (`نعم`, `اعتمد`, slot answers)

### Surface B — payment media intro (medium risk, facts-bound)

**Examples:** أرسل باركود الراجحي · باركود الأهلي

| Owner | Responsibility |
|-------|----------------|
| **System** | `media_key`, bank label, image sent, receipt required, verified payment method |
| **Persona** | Short natural intro text **after** media send succeeds |

| Forbidden in compose output |
|-----------------------------|
| IBAN, account number, phone credential |
| Payment link, unverified method |
| Payment success claim without evidence |
| Banned support-bot openers (`أكيد 🌷 تفضل`, …) |

**Call sites to migrate (design only — no change in this PR):**

- `whatsapp_webhook.py` early payment bypass
- Post-brain barcode route
- Owner-fallback override paths

Media send order and asset selection remain **unchanged**; compose affects intro text only.

---

## 4. Explicitly out of scope for Phase 2

Do **not** ship runtime compose for:

- Checkout slot prompts (name, address, qty, payment method selection)
- Order creation / draft proposal copy
- Catalog recommendation or browse narration
- KB long answers
- Shipping / tracking operational claims
- Availability / price claims without grounded facts

These require Phase 3–4 with additional grounding tests and guards.

---

## 5. Guard order (post-compose)

Apply in **fixed order** after `FactBoundPersonaComposer` returns text:

| # | Guard | On failure |
|---|-------|------------|
| 1 | Language / dialect guard | Repair once if safe → else fallback |
| 2 | Non-Saudi Arabic banned phrase guard | Repair once → else fallback |
| 3 | Credential / payment guard (`payment_credential_guard`) | **Immediate fallback** (no repair) |
| 4 | No fake claim guard (operational claims need evidence) | Fallback |
| 5 | Checkout-pressure guard (social surfaces) | Strip → no-silence fallback if empty |
| 6 | Known customer info re-ask guard (§5.1) | Strip / rewrite → fallback |
| 7 | Emoji density / context guard | Strip excess emoji → fallback if opener spam |
| 8 | Length guard (`max_chars`) | Truncate safe prefix → fallback |
| 9 | No-silence fallback | Emergency deterministic text |

**Repair policy:** At most **one** safe repair attempt per compose (e.g. dialect scrub). Credential and payment violations **never** repair — fallback only.

Log `guard_failed_reason` when falling back.

---

## 6. Saudi Arabic policy

Arabic output defaults to **Saudi Arabic**.

**Banned non-Saudi dialect terms** (extend in runtime; see `NON_SAUDI_ARABIC_DIALECT_TERMS`):

`شنو` · `بتاعك` · `إزاي` · `عامل إيه` · `دلوقتي` · `عايز` · `كيفك` · `شو` · `هلأ` · `بدك`

**English:** natural professional English; no forced Saudi expressions.

**Mixed inbound:** use dominant language; any Arabic portion remains Saudi.

---

## 7. Marketing warmth and emoji policy

- Emoji is **optional**, not mandatory.
- **0–1** emoji in normal replies; up to **2** only for campaigns/celebration when policy allows.
- **No fixed mapping:** delivery ≠ always 🚚; payment ≠ always 🧾; social ≠ always 🌷.
- Vocabulary in policy §11.3 is **guidance + guardrails**, not a template system.
- `marketing_emoji_policy` post-polish may suggest one emoji for weak output — must not become surface→emoji mapper.

---

## 8. Known customer information policy (§5.1)

Composer must **not** ask for known valid information.

| Do not ask when known | Use instead |
|-----------------------|-------------|
| Phone (WhatsApp sender exists) | Use sender; never re-collect |
| Full name (stored + valid) | Confirm: «الاسم عندي: هشام العتيبي. نعتمده؟» |
| Address (saved + valid) | Confirm saved address |
| Payment method (already selected) | Reference selected method |
| Product/qty (grounded in cart) | Reference line items |

**Bad:** `اسمك الكامل لو تكرمت؟`  
**Good:** `الاسم عندي: هشام العتيبي. نعتمده؟`

Social surfaces: **no slot re-ask at all** (Phase A.1 guards remain safety net).

---

## 9. Fallback policy

Deterministic fallback allowed only when:

- LLM timeout
- Guard failure (after optional repair)
- `persona_composer_enabled=false` or surface not in allowlist
- Kill-switch / safety risk
- Shadow mode (log only — current path unchanged)

Fallback must be:

- Short, safe, no fake facts
- No checkout pressure on social surfaces
- No credentials
- Not the primary product experience

**Metrics:** `persona_compose_fallback_total{surface,reason}`, `persona_compose_latency_ms`.

Existing fallbacks (`payment_barcode_intro_text`, `social_mirror_fallback_reply`, emergency thanks/dua paths) become **fallback implementations**, not targets.

---

## 10. Observability

Persist in `message_events.metadata` (or structured log line) when compose runs:

| Key | Example |
|-----|---------|
| `persona_compose_surface` | `social_checkin` |
| `persona_compose_source` | `persona_llm` |
| `persona_compose_facts_hash` | `a3f2…` |
| `persona_compose_guard_passed` | `true` |
| `persona_compose_guard_failed_reason` | `non_saudi_dialect` |
| `persona_compose_fallback_reason` | `timeout` |
| `persona_compose_language` | `ar` |
| `persona_compose_dialect` | `saudi_arabic` |
| `persona_compose_emoji_count` | `1` |
| `persona_compose_latency_ms` | `842` |
| `persona_compose_model` | `claude-haiku-4-5` |
| `persona_compose_shadow` | `true` (when shadow mode) |

Do **not** store raw IBAN, credentials, or full PII in metadata.

**Shadow mode:** log `persona_compose_shadow_text` alongside actual outbound for diff tooling — never send shadow text to customer.

---

## 11. Latency and cost

| Parameter | Default | Notes |
|-----------|---------|-------|
| `PERSONA_COMPOSE_TIMEOUT_MS` | 1500 | Social / payment intro |
| Model tier | Haiku-class | No expensive model for trivial social unless tenant override |
| Cache | Facts bundle only | **Do not** cache final wording (kills non-determinism) |
| On timeout | `fallback_deterministic` | Log `fallback_reason=timeout` |

Payment bypass: compose **before** intro text send (Option A in design audit). Media send path unchanged.

---

## 12. Rollout strategy

### Feature flags

| Flag | Purpose |
|------|---------|
| `persona_composer_enabled` | Master kill-switch |
| `persona_composer_surfaces` | Allowlist of surfaces (`social_greeting`, `thanks`, …) |
| `persona_composer_shadow_mode` | Log persona output; send current path |
| `persona_composer_allowlist_tenants` | e.g. `[33]` initially |
| `persona_composer_allowlist_phones` | Test phones only in shadow/enforce |

### Recommended sequence

| Step | Action | Behavior change? |
|------|--------|------------------|
| 1 | Shadow on tenant **33** only | **No** — log compare only |
| 2 | Review shadow diffs (social/thanks/dua) | No |
| 3 | Enforce social/thanks/dua on allowlisted test phones | **Yes** — phrasing only |
| 4 | Shadow then enforce `payment_media_intro` | **Yes** — intro text only |
| 5 | Expand tenants slowly with metrics | Yes |

**Rollback:** disable flag → immediate return to current deterministic/LLM paths; guards unchanged.

---

## 13. Tests to flip gradually (not in this PR)

Constitution xfails in `backend/tests/test_merchant_assistant_constitution.py` to target per runtime PR:

| Test / group | Target runtime PR | Notes |
|--------------|-------------------|-------|
| `test_arabic_social_output_no_non_saudi_dialect` | Social enforce PR | Saudi dialect guard |
| `test_arabic_operational_output_no_non_saudi_dialect` | Payment intro PR | Arabic on payment surface |
| `test_social_checkin_not_always_same_phrase` | Social shadow/enforce | Non-determinism |
| `test_thanks_dua_not_fixed_global_string` | Social enforce | Variation |
| `test_social_output_rejects_banned_support_bot_openers` | Social enforce | Banned opener guard |
| `test_social_output_rejects_checkout_pressure` | Social enforce | Compose + existing guard |
| `test_composer_selects_emoji_by_context_not_fixed_rose` | Social enforce | Optional emoji |
| `test_payment_intro_not_primary_static_template` | Payment intro PR | Replace `payment_barcode_intro_text` primary |
| `test_payment_intro_must_not_use_banned_opener_primary` | Payment intro PR | Banned opener |
| `test_brain_compose_must_not_emit_name_reask_when_name_stored` | Phase 3+ / checkout | Fact-aware compose before slots |

**Do not flip xfails in design PRs.** Each runtime PR flips only its scoped tests.

---

## 14. First runtime PR proposals (after design approval)

### PR 1 — Shadow only (no customer behavior change)

**Title:** `feat(ai): add FactBoundPersonaComposer shadow mode for social surfaces`

| In scope | Out of scope |
|----------|--------------|
| `social_greeting`, `social_checkin`, `thanks`, `dua` | Payment media intro |
| Shadow mode on tenant 33 | Production enforcement |
| Log `persona_compose_*` metadata | Changing outbound text |
| Compare persona vs current in logs | OrderFlowV2 / checkout |

### PR 2 — Enforce social (test mode)

**Title:** `feat(ai): enforce FactBoundPersonaComposer for social surfaces in tenant test mode`

| In scope | Out of scope |
|----------|--------------|
| Allowlisted phones + `store_ai_mode=test` | All tenants |
| Surfaces A only | Payment intro |
| Full guard chain | KB / catalog |

### PR 3 — Payment intro

**Title:** `feat(payments): persona compose payment media intro from verified facts`

| In scope | Out of scope |
|----------|--------------|
| `payment_media_intro` after verified media send | IBAN / credentials in text |
| Migrate `payment_barcode_intro_text` to fallback | Checkout prompts |

---

## Related documents

- `docs/architecture/fact-bound-persona-composer-design.md` — contract audit (Phase 1)
- `docs/architecture/nahla-ai-merchant-assistant-policy.md` — behavior policy
- `docs/engineering/merge-and-ci-policy.md` — merge / CI rules
- `backend/tests/test_merchant_assistant_constitution.py` — constitution regressions
- `backend/tests/constitution_helpers.py` — `try_compose_persona_samples` stub

---

## Confirmation

This document introduces **zero runtime behavior change**. Implementation PRs require green `lint-and-test`, constitution suite, and confidence gate per `merge-and-ci-policy.md`.
