# Commerce Completion Policy

**Status:** **Adopted — Architecture Baseline** (2026-08-08)  
**Scope:** Platform-wide merchant commerce completion after Product Selection  
**Depends on (Adopted):** `docs/architecture/product-selection-contract.md`  
**Companions:** `docs/architecture/nahla-ai-merchant-assistant-policy.md`, `AGENTS.md`, ADR 0002  

**Hard rules:**

1. This policy **must not** redefine Product Selection or Product Focus.  
2. **Capability ≠ Policy.**  
3. No platform-specific (Salla / Shopify / Woo / WhatsApp-as-platform) branches in this contract.  
4. No Prompt / Guard / Layer / State / Router required to *define* the policy.  
5. Runtime changes require a **separate** engineering authorization scoped to Decision/Projection (or later packages).

---

## 1. Purpose

Answer two questions **separately**:

| # | Question | Owner |
|---|----------|--------|
| 1 | When does the conversation **enter** Commerce Completion? | **Completion Entry** (§3) |
| 2 | Once entered, **how** is purchase completed? | **Completion Mode Resolution** (§4–§7) |

Never conflate: choosing a channel does **not** mean purchase started; selecting a product does **not** mean purchase started.

---

## 2. Scope

### In scope

- Completion Entry triggers  
- Explicit Purchase Intent contract (semantic, not phrase lists)  
- Active checkout continuation  
- Completion modes and adaptive resolution  
- Policy SoT (conceptual schema)  
- Capability vs Policy  
- Storefront product-URL rule and homepage fallback  
- Channel switch / exit / abandonment  
- Checkout boundary  
- Multi-platform invariants  
- Expression ownership  

### Out of scope

- Runtime, Decision Engine, DB migrations, PRs  
- Prompt / Guard / Layer / Memory  
- Payment provider / shipping carrier details  
- Full Checkout Contract text (boundary defined here; sibling doc may follow)

---

## 3. Completion Entry Trigger

### Principle

**Commerce Completion does not start with Product Selection.**

The following remain in **Discovery / Product Selection / Product Conversation** and do **not** enter Completion:

- Pressing Discovery `pick_N`  
- Showing product image  
- Asking price / size / color / availability  
- Opening product details / explanation  
- Mere existence of Product Focus  

### Entry requires exactly one of

#### A. Explicit Purchase Intent

Customer expresses a **purchase-intent meaning** for the focused (or clearly referenced) product — intent / semantic classification owned by the platform decision stack.

Illustrative *meanings* (not phrase matchers, not hardcoded runtime lists):

- Want to order / buy it  
- How do I order?  
- Complete the order  
- Take it for me / get it for me  

**Forbidden:** Building production phrase-matching or canned Arabic/English lists from these examples. Examples document meaning only.

#### B. Existing Active Checkout

A **valid Checkout** already started under this policy (WhatsApp Checkout mode entered previously). The customer may continue that checkout without re-proving purchase intent on every message, subject to Checkout Contract / stale-checkout arbitration (merchant assistant policy).

### Non-entry

| Event | Enters Completion? |
|-------|-------------------|
| Product Selection / focus bind | No |
| Price / variant / stock Q&A | No |
| Product image send | No |
| Social / KB turns | No |
| CTA click after Entry already resolved to storefront | **Execution**, not new Entry (§8 Q2) |

---

## 4. Two stages inside Commerce Completion

```
Completion Entry          →  Has purchase started?
         ↓ (yes)
Completion Mode Resolution →  How will they finish?
         ↓
Execution                 →  CTA / channel UI / Checkout Contract / Order Execution
```

| Stage | Decides | Must not decide |
|-------|---------|-----------------|
| **Entry** | Purchase started? (intent or active checkout) | Which store platform; product identity |
| **Mode Resolution** | storefront / whatsapp_checkout / channel_choice / … | Whether selection happened; product identity |
| **Execution** | Emit trusted actions for the chosen mode | Redefine Selection or Entry |

**Channel choice never creates Entry.** Asking “WhatsApp or store?” is Mode Resolution **after** Entry (or hybrid resolution after Entry).

---

## 5. Product Selection & Product Truth invariants (preserved)

From the Adopted Product Selection Contract, restated for Completion:

| Statement | |
|-----------|--|
| Product Focus | **Context binding**, not purchase commitment |
| Focus present | Does **not** mean the customer decided to buy |
| Selection | Never starts Draft Order / Checkout / address / payment / shipping |
| Completion | Builds **on top of** Selection; never redefines it |
| Mode | Never determines Product Identity |

```
Product Focus = Context Binding ≠ Purchase Commitment
```

### Commerce Completion Invariant — Product Truth

**Commerce Completion may change the purchase path, but it must never change Product Truth.**

| Must not happen | |
|-----------------|--|
| Choosing a channel | Does **not** change the product |
| Changing Completion Mode | Does **not** change Product Focus |
| Storefront mode | Does **not** change Product Identity |
| WhatsApp Checkout mode | Does **not** change Product Identity |
| Checkout Contract | Does **not** reinterpret Product Selection |

Product Identity / Focus remain owned by **Product Selection** + catalog truth. Completion only chooses **how** purchase finishes.

---

## 6. Capability vs Policy

| Concept | Meaning |
|---------|---------|
| **Capability** | What the merchant **can** do (evidence): storefront URL / product_url, WhatsApp checkout enabled, showroom maps, etc. |
| **Policy** | What the merchant **chooses** as allowed / default completion behavior |

Examples:

- `online_store.available = true` → storefront completion is **possible**. It does **not** mean policy selected storefront.  
- `whatsapp_quick_order.enabled = true` → in-conversation checkout is **possible**. It does **not** mean every purchase intent must use it.  

**Rule:** Policy selects among **executable** capabilities. Capability alone never implies Entry or Mode.

Legacy settings (`sales_channels`, `default_order_channel`) remain capability / preference signals unless a future amendment **explicitly** maps them into Policy SoT. They are **not** silently reinterpreted here.

---

## 7. Policy Source of Truth (conceptual schema)

### SoT name

**`commerce_completion_policy`** — dedicated merchant policy object (conceptual).  
Not implemented in DB/runtime in this phase.

### Conceptual fields

| Field | Role |
|-------|------|
| `allowed_modes` | Ordered or unordered set of completion mode ids that policy permits |
| `default_mode` | Mode used when Entry is true and resolution does not require asking |
| `resolution` | `fixed` \| `prefer_default` \| `ask_if_multiple` (see Adaptive §9) |
| `storefront_cta_timing` | `on_purchase_intent` \| `on_selection_presentation` \| `never` — **default for current phase: `on_purchase_intent`** |
| `allow_store_homepage_fallback` | boolean — **default: `false`** (see §11) |

### Mode ids (official — platform-neutral)

| Mode id | Meaning |
|---------|---------|
| `storefront` | Complete purchase on merchant storefront (off-conversation checkout) |
| `whatsapp_checkout` | Complete purchase in-conversation (Checkout Contract → Order Execution) |
| `channel_choice` | After Entry, ask customer to choose among executable allowed modes |
| `showroom` | Optional physical-visit completion (capability-gated); may be omitted from early rollouts |

**Named profiles** (optional sugar over the same schema — not platform names):

| Profile | Typical encoding |
|---------|------------------|
| Storefront-only | `allowed_modes=[storefront]`, `default_mode=storefront` |
| WhatsApp-checkout | `allowed_modes=[whatsapp_checkout]`, `default_mode=whatsapp_checkout` |
| Adaptive | `allowed_modes` includes both executable modes; `resolution=ask_if_multiple` or `prefer_default` |

Profiles describe **policy shapes**, not Salla/Shopify/WhatsApp products.

### Current-phase merchant outcome (Salla storefront-first without naming Salla in the contract)

Desired experience is expressed as:

- Policy: **storefront-only** (or adaptive with storefront default)  
- Capability: integration supplies **product_url** + storefront evidence  
- Execution: open **exact product page** after Entry  

Same contract later applies to Shopify/Woo by swapping capabilities, not rewriting policy language.

---

## 8. Explicit Purchase Intent contract

| Item | Contract |
|------|----------|
| **Definition** | Customer turn whose **semantic intent** is to buy / order / start or resume purchase completion for a grounded product (or clear product reference), not merely to learn about it |
| **Classification** | Platform intent / semantic decision owners — **not** hardcoded phrase tables as the primary SoT |
| **Examples in docs** | Meaning illustrations only; **forbidden** as runtime phrase matchers |
| **Requires Product Focus?** | Prefer yes; if absent, Completion may clarify product first (**still Selection/clarify**), then re-evaluate Entry — does not invent focus |
| **Does not include** | Price, size, color, stock, “tell me about it”, Discovery pick, image request alone |

---

## 9. Adaptive resolution & channel choice

### Adaptive

When more than one mode is in `allowed_modes` **and** executable via Capability:

| `resolution` | Behavior |
|--------------|----------|
| `fixed` | Ignore extras; always use `default_mode` if executable, else fail closed |
| `prefer_default` | Use `default_mode` if executable; else next executable allowed mode; else fail closed |
| `ask_if_multiple` | Enter Mode Resolution with **channel_choice** UI/actions; wait for customer choice |

**Priority order** when not asking: `default_mode` first, then remaining `allowed_modes` in declared order, filtered by Capability. Never invent a mode without capability.

### Who chooses the channel?

| Situation | Owner |
|-----------|--------|
| Single executable allowed mode | Policy (`default_mode` / that mode) — system selects |
| Multiple + `ask_if_multiple` | **Customer** chooses; platform offers structured channel actions |
| Multiple + `prefer_default` / `fixed` | **Policy** selects; LLM may explain naturally, not invent mode |

Channel choice is **Mode Resolution**, not Entry.

### Channel commitment during checkout

Once Mode Resolution selects `whatsapp_checkout` and Checkout Contract becomes active:

- Persist a **completion commitment** in existing commerce/session surfaces (e.g. preferred completion mode / checkout channel fields already used for channel commitment — **reuse of meaning must be documented at implementation time**; this design does not invent new State in this phase).  
- Commitment lasts until Exit (§12), successful order handoff, or explicit channel switch (§12).  
- Product Focus may remain; commitment is about **how** to complete, not **which** product.

---

## 10. Interaction timeline

```
Conversation
  → Discover / browse
  → Product Selection → Product Focus          [Selection Contract]
  → Product conversation (price, size, image, explain)   [NOT Completion]
  → Explicit Purchase Intent  OR  Active Checkout
  → Completion Entry
  → Completion Mode Resolution (policy ∩ capability)
       ├─ storefront → trusted product_url CTA / open product page
       ├─ whatsapp_checkout → Checkout Contract → Order Execution
       ├─ channel_choice → customer picks → then one mode
       └─ showroom → location action (if allowed)
```

**Forbidden:**

```
Discover → pick_N → Draft Order / Address / STAGE_ORDERING
```

---

## 11. Storefront product URL rule & homepage fallback

### Product page first

In `storefront` mode, if Product Focus is trusted **and** catalog provides a valid **product_url**:

→ Open / CTA that **exact product page**.

Not the store homepage.

### Homepage fallback

| Rule | Value |
|------|--------|
| Automatic homepage fallback | **Not allowed** in current baseline |
| `allow_store_homepage_fallback` | Default **`false`** |
| If policy later sets `true` | Only with **trusted** storefront root evidence; never invented URL |
| If storefront mode selected but **no** `product_url` | **Fail closed** for product CTA: do not invent URL; do not silently send homepage unless fallback explicitly enabled **and** evidence exists; prefer honest capability gap (clarify / offer other executable mode if policy allows) |

---

## 12. Channel switch, exit, abandonment

### Channel switch (Completion Mode change — not Selection rewrite)

| Customer move | Contract response |
|---------------|-------------------|
| In storefront completion, then wants in-chat order | Re-resolve Mode → `whatsapp_checkout` if allowed+capable; **Entry already true**; do not redefine Product Selection |
| In WhatsApp Checkout, then wants store link | Re-resolve Mode → `storefront` if allowed+capable; exit or pause Checkout slots per Checkout boundary; keep Product Focus unless product changes |
| Leaves checkout to browse another product | **Exit Completion** (or suspend); new Discovery/Selection; previous checkout must not hijack (stale-checkout rules) |

### Completion exit / abandonment

Customer may abandon completion (semantic cancel / “not now” / “show another product”):

- End or suspend **Completion** state and WhatsApp Checkout continuation as applicable.  
- Do **not** delete the meaning of Product Selection as a contract.  
- Product Focus may clear or switch via normal Selection rules when they pick another product — without merging old checkout slots into the new product.  
- No forced re-entry into Completion without new Entry trigger.

---

## 13. Checkout boundary

| Step | Allowed only when |
|------|-------------------|
| Draft Order | Completion Entry **and** Mode = `whatsapp_checkout` |
| Address / payment / shipping collection | Same |
| `STAGE_ORDERING` / equivalent ordering stage | Same |
| Order Execution claims | Evidence + Order Execution contract |

In **`storefront` mode**: real checkout happens **outside** Nahla on the merchant storefront. Nahla must not start Draft Order / address / payment / shipping collection for that path.

---

## 14. Decision ownership

| Decision | Owner |
|----------|--------|
| Product Selection / Focus meaning | Product Selection Contract |
| Completion Entry (intent vs active checkout) | Decision / intent owners per Entry contract |
| Allowed modes & defaults | **`commerce_completion_policy` SoT** |
| Executable filter | Capabilities (evidence) |
| Mode after Entry | Policy resolution (§9) |
| Checkout slots | Checkout Contract |
| Durable order / payment / shipping claims | Order Execution + evidence |
| Natural language | LLM / Compose |

**Expression Ownership:** Platform owns truth and structured actions (mode, URL, CTA payload). LLM owns natural expression. No canned closers or fixed sales copy as the primary path.

---

## 15. Multi-platform invariant

This contract must work without essential rewrite for:

- Salla · Shopify · WooCommerce · custom storefront · WhatsApp Checkout · future platforms  

Platforms supply **Capabilities**, URLs, and execution adapters only.

---

## 16. Relationship to Product Selection & Checkout

| Contract | Role |
|----------|------|
| **Product Selection** | Focus binding; never Entry |
| **Commerce Completion Policy** | Entry + Mode Resolution |
| **Checkout Contract** | In-conversation checkout after `whatsapp_checkout` mode |
| **Order Execution** | Orders and evidence-backed operational claims |

---

## 17. Closed design questions (adoption checklist)

| # | Question | Closed answer |
|---|----------|---------------|
| 1 | Official Explicit Purchase Intent? | Semantic purchase-intent meaning; platform intent owners; examples are non-normative for matching (§8) |
| 2 | CTA click = Entry or Execution? | **Execution** of an already resolved storefront path (or Mode action). Click alone does not create Entry if Entry was never established; sending CTA happens after Entry+storefront resolution (or, if `storefront_cta_timing=on_selection_presentation`, CTA is presentation — still not Entry; purchase still needs Entry later). **Current-phase default timing = `on_purchase_intent`**, so CTA follows Entry. |
| 3 | Official Completion Modes? | `storefront`, `whatsapp_checkout`, `channel_choice`, optional `showroom` (§7) |
| 4 | Adaptive resolution? | `fixed` / `prefer_default` / `ask_if_multiple` over `allowed_modes` ∩ capabilities (§9) |
| 5 | Priority if multiple channels? | `default_mode` then declared order; or ask if `ask_if_multiple` (§9) |
| 6 | System vs customer chooses? | System if single/prefer_default/fixed; customer if `ask_if_multiple` (§9) |
| 7 | Channel commitment during checkout? | Persist completion mode commitment for active WhatsApp checkout until exit/switch/success (§9) |
| 8 | When Completion ends? | Abandonment, successful handoff, channel exit, or product-browse that suspends checkout (§12) |
| 9 | Product Focus changes during Completion? | Treat as Selection update; do not silently continue old checkout for new product; re-validate Entry/Mode (§12) |
| 10 | Storefront policy but no `product_url`? | Fail closed for product CTA; no invented URL; optional other executable mode if policy allows (§11) |
| 11 | Homepage fallback? | **Not** automatic; only if `allow_store_homepage_fallback=true` **and** trusted storefront root evidence (§11) |
| 12 | Completion vs Checkout Contract? | Completion = Entry + Mode; Checkout = in-chat slots/continuation only after `whatsapp_checkout` (§13, §16) |

**Open questions remaining:** none that block architecture_ready (implementation schema storage location and migration of existing merchants are **implementation adoption tasks**, not open semantic questions).

---

## 18. Adoption criteria (self-check)

| Criterion | Met? |
|-----------|------|
| Product Selection does not start Checkout | Yes |
| Purchase Intent is an independent Entry gate | Yes |
| Capability ≠ Policy | Yes |
| Completion Mode does not define Product Identity | Yes |
| Checkout does not start before Completion Entry + whatsapp mode | Yes |
| Storefront & WhatsApp are execution modes only | Yes |
| No Salla-specific logic in the general contract | Yes |
| No Prompt/Guard/Layer required to define the policy | Yes |

---

## 19. Sign-off gate

- **Architecture Ready:** Yes.  
- **Adopted Baseline:** **Yes** (2026-08-08 final architecture sign-off).  
- **Runtime:** Separate authorization may open the smallest Decision/Projection package for Discovery `pick_N` → Product Selection (no Prompt/Guard/Layer/new State/Checkout redesign).

---

## 20. One-line summary

**Customers may explore and select products freely; they become checkout buyers only after Explicit Purchase Intent or an already-active Checkout — then Policy ∩ Capability chooses how to finish, without redefining Product Selection.**
