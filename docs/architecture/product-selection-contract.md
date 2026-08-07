# Product Selection Contract

**Status:** **Adopted — Architecture Baseline** (2026-08-07)  
**Scope:** Platform-wide — all merchants, all store platforms, all sales channels  
**Companions:**  
- `docs/architecture/nahla-ai-merchant-assistant-policy.md`  
- `docs/architecture/commerce-completion-policy.md` (Adopted — Architecture Baseline)  
- `AGENTS.md`  
- ADR 0002  

**Runtime note:** Adoption is **architectural**. Decision Engine / webhook behavior may still violate this contract until a later authorized implementation package. Until then, known defect (`pick_N` → Draft Order) remains a **runtime gap**, not a license to redefine this contract.

---

## 0. Why this contract exists

Production RCA showed the defect was not LLM, Prompt, or Product Focus lifecycle alone. It was the absence of an architectural contract separating:

1. **Selecting** a product in the conversation  
2. **Completing** a purchase (Commerce Completion Policy)  
3. **Executing** checkout / order creation  

This document is the **Architecture Baseline** for (1).

---

## 1. Official principle

**Product Selection** is the act of binding a **trusted catalog product identity** into the current conversation context.

It **never** means, by itself:

- Draft Order  
- Checkout  
- Address collection  
- Payment  
- Shipping  
- Order creation  

This remains true **regardless of**:

- Store platform (Salla, Shopify, WooCommerce, Meta catalog, manual, future)  
- Sales channel (WhatsApp, storefront, showroom, hybrid)  
- Payment method  
- Completion mode  

---

## 2. Product Selection Invariants

### INV-1 — Channel-neutral

Product Selection **must be channel-neutral**.

It does **not** depend on WhatsApp, Salla, Shopify, WooCommerce, or any commerce platform.

Its **only required outcome** is:

> A trusted **Product Identity** exists in the conversation context.

### INV-2 — Platform-neutral identity

Identity comes from the **Nahla catalog hub** (synced / local `products`), not from live platform APIs on the critical path, and not from LLM invention.

### INV-3 — Not checkout

```
Product Selection ⊂ Conversation Context Binding
Product Selection ⊄ Commerce Completion
Product Selection ⊄ Checkout
Product Selection ⊄ Order Execution
```

### INV-4 — No settings reinterpretation

Existing settings such as `default_order_channel`, `sales_channels`, or `online_store` **must not** be reinterpreted as the meaning of Product Selection unless a future document **explicitly** promotes them for that purpose. Today they are **not** Product Selection SoT.

### INV-5 — No Discovery → Checkout shortcut

Future implementations **must not** allow a direct transition from Discovery product pick to Checkout. The path must respect the Contract Hierarchy (§4).

---

## 3. Product Focus

**Product Focus** (`current_product_focus` and equivalent focus surfaces) is the **authoritative outcome of Product Selection**.

| Rule | Statement |
|------|-----------|
| Focus is produced by | Product Selection (and explicit focus updates that are still selection/browse acts) |
| Focus is **not** produced by | Checkout, Draft Order, address collection, or payment |
| Later stages | May **read** focus as input; must **not** redefine what Product Selection means |

Checkout and Order Execution may consume focus as “which product is being bought,” but they do not own the definition of selection.

---

## 4. Contract Hierarchy

Official platform sequence:

```
Conversation
    ↓
Product Selection Contract          ← this document (adopted)
    ↓
Commerce Completion Policy          ← separate SoT (design → then adopt)
    ↓
Checkout Contract                   ← slot/continuation ownership (OrderFlow / draft)
    ↓
Order Execution                     ← create/update order, payments, shipping evidence
```

**Forbidden:** Discovery → Checkout (skipping Selection semantics and Completion Policy).

---

## 5. Product Selection Contract (obligations)

### What Product Selection *is*

| Obligation | Owner |
|------------|--------|
| Resolve identity from grounded candidates (list index, last shown products, etc.) | Decision + catalog state |
| Set / update **Product Focus** from trusted catalog fields | Commerce focus owner / state |
| Expose catalog facts for compose (title, price, availability, variants, image, URL when projected) | Catalog hub |
| Allow natural follow-ups against that focus | Brain + compose (LLM owns wording) |

### What Product Selection *is not*

| Non-obligation | Must not happen on selection alone |
|----------------|-------------------------------------|
| Start Draft Order | No checkout initiation from selection alone |
| Enter ordering / checkout stage | No stage advance to ordering from selection alone |
| Collect address / name / payment | No checkout slots |
| Claim order or “choices saved for checkout” | No draft-ack for selection |
| Invent URL, image, price, or stock | Catalog SoT only |

---

## 6. Product Button Semantics

| Button class | Example | Meaning under this contract |
|--------------|---------|-----------------------------|
| **Product pick (Discovery)** | `pick_N` | **Product Selection** → Product Focus |
| **Option pick (active checkout)** | `opt_N` while draft/checkout active | Checkout continuation — not Discovery selection |
| **Purchase-channel pick** | channel buttons | Completion-channel choice — not product identity |
| **Storefront CTA** | `cta_url` to product page | Commerce Completion / presentation action — not selection itself |

### Discovery product button

1. **Primary:** Product Selection (focus binding).  
2. **Presentation:** Platform may show catalog image/facts; may attach product-page CTA **only** when Commerce Completion Policy authorizes that presentation.  
3. **Never implied:** Draft Order, address, payment, shipping, order creation.

Durable identity = candidate at index N in remembered candidates — not the button label string alone.

### Explicit purchase is a different act

Purchase / completion starts only via:

- Explicit buy / start-order intent (with focus already set), or  
- Active checkout continuation, or  
- Commerce Completion Policy outcomes after a true purchase-intent turn  

— never from Product Selection alone.

---

## 7. Relationship to other contracts

| Contract | Relationship |
|----------|----------------|
| **Commerce Completion Policy** | Builds **on top of** Product Selection; must not redefine it. See `commerce-completion-policy.md`. |
| **Checkout Contract** | Owns real in-conversation checkout continuation after Completion Policy selects that mode. |
| **Order Execution** | Owns durable order records and evidence-backed claims (ADR 0002). |
| **Merchant Assistant Policy** | Nahla is not a checkout bot; browse ≠ checkout slots. |

---

## 8. Decision ownership (selection only)

| Layer | Owns | Does not own |
|-------|------|--------------|
| **This contract** | Meaning of Discovery product selection | Completion modes, checkout slots, wording |
| **Decision Engine** (future implementation) | Route Discovery pick → selection / focus — not draft order | Customer-facing prose |
| **Commerce focus / state** | Persist Product Focus | Claiming purchase happened |
| **Catalog hub** | Product identity and media/URL facts | Inventing missing fields |
| **LLM / Compose** | Natural explanation | Identity, URLs, images, prices, routing |
| **Completion / Checkout / Execution** | Downstream contracts only | Redefining Product Selection |

---

## 9. Runtime gap (evidence)

| Item | Current runtime | This baseline |
|------|-----------------|---------------|
| Discovery `pick_N` | Often → Draft Order | Must be Product Selection only (when implemented) |
| `product_url` projection | Gap in catalog `_format` | Required for honest storefront CTA under Completion Policy |
| `image_url` projection | Present | Keep |
| Channel settings as selection SoT | Must not be overloaded | INV-4 |

---

## 10. Implementation gate

1. This Product Selection Contract is **Adopted**.  
2. Commerce Completion Policy is **Adopted** (`commerce-completion-policy.md`).  
3. Runtime: Discovery `pick_N` must bind Product Focus via Product Selection — never Draft Order — unless an **active WhatsApp checkout** continuation applies (options / draft / ordering slots).

Purchase / storefront CTA entry remains gated by Commerce Completion Entry (Explicit Purchase Intent or active checkout), not by Selection alone.

---

## 11. One-line summary

**Product Selection binds trusted Product Identity into the conversation (Product Focus). It never starts checkout.**  
How purchase is completed is owned by **Commerce Completion Policy**, then Checkout, then Order Execution.
