# FactBoundPersonaComposer — Design Audit

**Status:** Phase 1 design (no runtime behavior)  
**Policy:** `docs/architecture/nahla-ai-merchant-assistant-policy.md` §11–§11.2, §12  
**Companion:** `AGENTS.md`

---

## 1. Purpose

`FactBoundPersonaComposer` is a **platform-wide layer** that converts **verified facts** into short, natural, Saudi-merchant Arabic customer-facing text.

It must **never**:

- Change operational truth
- Change decisions (routing, asset selection, order state)
- Change execution (send media, create order, escalate)
- Invent credentials, prices, availability, tracking, or policies

It must **always**:

- Receive facts authored by deterministic system code
- Produce non-deterministic phrasing when compose succeeds
- Fall back to safe deterministic text only on failure or kill-switch

**Anti-goal:** Replacing `أكيد 🌷 تفضل` with `أبشر يا غالي` as a new fixed template.

---

## 2. Contract

### 2.1 Surfaces (initial inventory)

| Surface | When | Facts source | Phase |
|---------|------|--------------|-------|
| `payment_media_intro` | After verified media send queued | `media_key`, registry label, receipt_required | 2 |
| `social_greeting` | Greeting / كيف الحال / وش أخبارك | customer_name, open_order_hint | 2 |
| `thanks` | شكراً / مشكور | customer_name | 2 |
| `dua` | الله يعطيك العافية / بارك الله فيك | — | 2 |
| `kb_answer` | FAQ / policy / store questions | KB section IDs + bodies (capped) | 3 |
| `product_answer` | Catalog / availability | product cards, prices (grounded) | 3 |
| `order_status` | Track order | local_order_resolver snapshot | 4 |
| `checkout_prompt` | Slot collection | missing field, grounded line items | 4 |

### 2.2 Input bundle

```python
@dataclass(frozen=True)
class PersonaFactsBundle:
    surface: str
    verified_facts: dict[str, Any]   # system-authored only; JSON-serializable
    customer_context: dict[str, Any] # name, open_order_hint — optional, safe
    merchant_persona: dict[str, Any] # tenant ai_settings slice
    constraints: PersonaConstraints

@dataclass(frozen=True)
class PersonaConstraints:
    max_chars: int = 220
    allow_emoji: bool = True
    tone: str = "saudi_merchant_short"
    language_policy: str = "dominant_customer_language"
    dialect: str = "saudi_arabic"  # Arabic surfaces only; English uses language="en"
    language: str = "ar"  # "ar" | "en" — set from inbound dominance
    banned_claims: frozenset[str] = frozenset()  # iban, account_number, payment_link, ...
    banned_phrases: frozenset[str] = frozenset() # support-bot openers + non-Saudi dialect
    guard_requirements: frozenset[str] = frozenset({"payment_credential"})
```

### 2.3 Language policy

| Field | Arabic customer | English customer |
|-------|-----------------|------------------|
| `language` | `ar` | `en` |
| `dialect` | `saudi_arabic` | *(not applied)* |
| `tone` | `saudi_merchant_short` | `professional_natural` |

**`language_policy` rules:**

- Arabic inbound → Saudi Arabic phrasing (`dialect=saudi_arabic`).
- English inbound → natural professional English (`language=en`); no Saudi expressions forced into English.
- Mixed inbound → dominant language; Arabic portions remain Saudi when Arabic is used.

**Banned phrase guard (non-Saudi Arabic — extend in runtime):**

`شنو`, `بتاعك`, `إزاي`, `عامل إيه`, `دلوقتي`, `عايز`, `كيفك`, `شو`, `هلأ`, `بدك`

Plus support-bot openers from policy §11.1 (`كيف أقدر أساعدك اليوم؟`, `تم استلام رسالتك`, …).

**Social surface examples** in policy and tests are **illustrative**, not fixed outputs.

### 2.4 Non-determinism requirement

The same verified facts bundle may produce **different safe wording** across compose calls.

| Invariant | Must hold |
|-----------|-----------|
| Operational facts | Unchanged |
| Allowed action | Unchanged |
| Asset / method selection | Unchanged |
| Exact customer-facing string | **May vary** when persona compose succeeds |

Deterministic fallback strings are **not** the product target — only the safety net.

### 2.5 Output

```python
@dataclass(frozen=True)
class PersonaComposeResult:
    text: str
    source: str          # persona_llm | fallback_deterministic
    surface: str
    facts_hash: str      # sha256 of canonical facts JSON
    guard_passed: bool
    latency_ms: float
    fallback_reason: str = ""
```

---

## 3. Where the layer is used (Phase 2+)

| Call site today | Current phrasing | Migration |
|-----------------|------------------|-----------|
| `whatsapp_webhook.py` early payment bypass L6628 | `payment_barcode_intro_text()` / hardcoded bank line | Compose after media send succeeds |
| `whatsapp_webhook.py` post-brain barcode route L11285 | `payment_barcode_intro_text()` | Compose in bypass path |
| `whatsapp_webhook.py` owner-fallback override L11772 | hardcoded bank line | Compose with facts |
| `responder._compose_social_persona_ack` | Existing LLM social compose | **Align** with composer contract (may wrap) |
| `templates.py` track/ledger/clarify | Fixed Arabic | Phase 3–4 only |

### Where NOT to use yet

- OrderFlowV2 slot prompts — until Phase A (social bypass) + Phase B (line-item guard) land
- Checkout payment method selection replies — Phase 4
- Staff handoff canned acks — remain deterministic (operational evidence)

---

## 4. Latency strategy

Early payment bypass runs **pre-Brain** and currently returns immediately after send.

| Option | Pros | Cons | Recommendation |
|--------|------|------|----------------|
| **A. Compose before text send** | Single outbound message order | Adds 0.5–2s before text | **Default for Phase 2** with 1.5s timeout |
| **B. Send image first, compose text async** | Fast media | Text may arrive late; ordering risk | Avoid unless A fails SLA |
| **C. Re-route through Brain** | Reuses pipeline | Breaks pause/handoff bypass; high risk | **Reject** for payment bypass |

**Budget:** `PERSONA_COMPOSE_TIMEOUT_MS` default 1500; on timeout → `fallback_deterministic`.

Media send path is **unchanged** — compose affects **intro text only**.

---

## 5. Fallback catalog (safety only)

Existing strings remain **fallback implementations**, not product targets:

| Key | Current source | Role after Phase 2 |
|-----|----------------|-------------------|
| `payment_barcode_rajhi` | `payment_barcode_intro_text` rajhi branch | fallback |
| `payment_barcode_generic` | `payment_barcode_intro_text` default | fallback |
| `payment_bank_generic` | webhook hardcoded `هذه بيانات التحويل البنكي` | fallback |
| `payment_unconfigured` | `payment_credential_guard` / order_flow_v2 | fallback |

Fallback must pass `payment_credential_guard` unchanged.

---

## 6. Preventing invention

### Pre-compose

- `verified_facts` built by resolver helpers only — never pass raw customer message as fact
- `media_key` and `bank_label_ar` from `media_key_registry` for selected asset
- `receipt_required` from checkout/order state — boolean only

### Prompt contract (LLM path)

```
You are phrasing a short WhatsApp reply for a Saudi merchant assistant.
You receive VERIFIED FACTS ONLY in JSON. Do not add facts.
Do not mention IBAN, account numbers, phone credentials, or payment links.
Do not mention payment methods not listed in facts.
Max {max_chars} characters. Natural Saudi tone. Short.
```

### Post-compose guards (mandatory)

1. `payment_credential_guard` — all `payment_media_intro` outputs
2. `apply_payment_credential_guard` must run even on fallback
3. Reject compose output containing `SA[0-9]{22}` not in verified accounts
4. Reject output mentioning bank brand not matching `facts.media_key`

### Persona cannot change decisions

Composer API returns **text only**. Callers pass immutable `asset_id`, `media_key` already chosen. Tests assert compose does not mutate facts dict.

---

## 7. Monitoring

| Metric | Purpose |
|--------|---------|
| `persona_compose_total{surface,source}` | Volume by surface and source |
| `persona_compose_fallback_rate{surface}` | Alert if fallback > 15% (configurable) |
| `persona_compose_latency_ms{surface}` | p50/p95 |
| `persona_guard_reject_total{guard}` | Credential invention blocked |
| `persona_facts_hash` (log field) | Audit trail per reply |

Log tag: `[PERSONA_COMPOSE] surface=… source=… facts_hash=… guard_passed=…`

**Success criteria for non-deterministic persona:** fallback rate low, guard pass rate high, no increase in invented-credential incidents, customer-facing variation across repeated surface requests in tests.

---

## 8. Module layout (proposed)

```
backend/modules/ai/brain/persona/
  __init__.py
  fact_bound_composer.py      # FactBoundPersonaComposer
  facts_bundle.py             # PersonaFactsBundle, builders per surface
  fallback_catalog.py         # deterministic safety strings
  prompts.py                  # surface-specific fact-only prompts
```

**Integration points:**

- `whatsapp_webhook.py` — payment intro (Phase 2)
- `compose/responder.py` — unify social compose (Phase 2)
- `pipeline.py` — optional KB/product surfaces (Phase 3)

---

## 9. Testing strategy

| Layer | File |
|-------|------|
| Constitution regressions | `backend/tests/test_merchant_assistant_constitution.py` |
| Composer unit | `backend/tests/test_fact_bound_persona_composer.py` (Phase 2) |
| Payment intro integration | extend `tests/test_payment_early_bypass.py` (Phase 2) |

Policy tests use `@pytest.mark.constitution_target` for not-yet-implemented behavior and `@pytest.mark.persona_policy` for anti-template future strictness.

---

## 10. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| LLM latency on early bypass | Medium | Timeout + fallback; image send first |
| Credential hallucination | High | Facts-only prompt + mandatory guard |
| Template regression disguised as persona | High | Policy §8 + anti-template tests |
| Composer changes asset | High | Text-only API + integration tests |
| Fallback becomes de facto path | Medium | Metrics + alert on fallback rate |
| Duplicate compose with social path | Low | Phase 2 unifies under one module |

---

## 11. Phase gate

**Do not implement Phase 2 until:**

- [ ] PR 1 policy merged
- [ ] This design reviewed
- [ ] PR 3 constitution tests merged
- [ ] Phase A/B behavioral PRs scoped separately (turn ownership + line-item guard)

---

## 12. Related code today

| File | Relevance |
|------|-----------|
| `modules/ai/brain/decision/payment_barcode_routing.py` | `payment_barcode_intro_text` — fallback source |
| `routers/whatsapp_webhook.py` | Early bypass — primary integration |
| `modules/ai/brain/postprocess/payment_credential_guard.py` | Post-compose guard |
| `modules/ai/brain/compose/responder.py` | Social LLM compose pattern |
| `modules/ai/brain/persona_ownership.py` | Ownership telemetry |
| `modules/ai/brain/persona_expression.py` | Goals / kill switches |
