# Salla Commerce Sync — Review Readiness Audit

**Date:** 2026-07-04  
**Branch:** `audit/salla-review-commerce-sync-readiness`  
**Scope:** Coupons (final validation), orders (price fidelity), offers/promotions, products/settings.  
**Out of scope:** AI, OrderFlow behavior, prompts, WhatsAppConnect, Meta Catalog, shipping, billing, token encryption.

**Merged baseline (main):** PR #421 (coupon dates), #431 (coupon name + timeout UX), #453 (merchant Salla integration UI).

---

## Executive summary

| Area | Review-ready? | Blocker |
|------|---------------|---------|
| Coupons | **Pass (basic scope)** | Technical details hidden; advanced rules backlog |
| Orders | **Fail — price mismatch** | Monetary breakdown not preserved; webhook total + line-item price parsing gaps |
| Offers / promotions | **Fail — not synced** | No Salla offer adapter; Nahla promotions are internal-only |
| Products | **Partial** | Import works; sale/promo prices and settings alignment incomplete |

**Do not tell Salla that review is ready until all required areas pass.**

---

## 1. Coupons — final validation (post PR #431)

### 1.1 Nahla → Salla: does `name` push?

**Yes.** Push path:

- `backend/services/coupon_salla_push.py` — `resolve_nahla_coupon_push_name()` reads `metadata.salla_coupon_name`, `metadata.name`, `metadata.title`, then `Coupon.description`, else `كوبون {code}`.
- `backend/store_adapters/salla_adapter.py` — `create_coupon(..., name=)` adds `payload["name"]` when non-empty (max 200 chars).
- Salla field used: **`name`** (not `title` in create payload).

### 1.2 Salla → Nahla: which fields are read?

Import name resolution (`extract_salla_coupon_name` in `coupon_salla_push.py`):

| Salla field | Read? | Nahla storage |
|-------------|-------|---------------|
| `name` | Yes (priority 1) | `coupons.metadata.salla_coupon_name`, `metadata.name`; `description` on reconcile |
| `title` | Yes (priority 2) | Same metadata keys |
| `description` | Yes (priority 3, if ≠ code) | Same metadata keys |
| `marketing_name` | Yes (when `marketing_active`) | Same |
| `group_name` | Yes (when `is_group`) | Same |

`sync_coupons()` (`store_sync.py`) and `build_salla_import_metadata()` (`coupon_sync_visibility.py`) persist `salla_coupon_name` in `extra_metadata`.

### 1.3 Dashboard exposure

- `GET /coupons` exposes `salla_coupon_name` via `coupon_sync_visibility.py`.
- `dashboard/src/pages/Coupons.tsx` shows subtitle when `salla_coupon_name !== code`.
- Sync badges: **متزامن مع سلة** (pushed), **مستورد من سلة** (imported).

### 1.4 Salla merchant UI for coupon name

Salla coupon edit page display is **not verifiable from code** — production validation (tenant 1) reported name alignment working after #431. Code sends the documented `name` field Salla accepts on `POST /coupons`.

### 1.5 `SHOW_COUPON_SYNC_TECH_DETAILS`

Set to **`false`** in `dashboard/src/utils/couponSallaSyncError.tsx` for merchant release. Sync errors show Arabic-friendly summaries only.

### 1.6 Coupon sign-off checklist

**Salla review scope (basic fields only):** Nahla ↔ Salla create/import, name, code, discount type/value, start/end dates, sync status badge. Advanced rule mapping (products, categories, brands, customer groups, countries, per-customer usage, max discount cap, combine-with-offers, platform restrictions) is **backlog** — do not block review unless Salla explicitly requests it.

| Check | Status |
|-------|--------|
| Nahla → Salla push (code, discount, dates) | Pass (prod-validated) |
| Salla → Nahla import | Pass (prod-validated) |
| Code matches | Pass |
| Name matches | Pass (code + prod) |
| Discount matches | Pass |
| Dates acceptable (#421 `start_date`) | Pass |
| No technical error details in UI | Pass (`SHOW_COUPON_SYNC_TECH_DETAILS=false`) |
| Full API link required for push | Documented; merchant UI in #453 |

**Remaining coupon work:** None for basic Salla review scope. Advanced coupon rules remain backlog.

---

## 2. Orders — price mismatch audit

### 2.A Source payload (Salla)

Primary code paths:

| Path | Entry | Normalization |
|------|-------|---------------|
| Bulk sync | `SallaAdapter.get_orders()` → `GET /orders` | `_normalize_order()` → `NormalizedOrder` |
| Webhook | `webhook_dispatcher` → `StoreSyncService.handle_order_webhook()` | `_normalise_order(raw_payload)` |
| Single fetch | `get_customer_orders()` | Same adapter normalizer |

**Salla fields consumed today (adapter `_normalize_order`):**

| Concept | Salla field(s) | Handled? |
|---------|----------------|----------|
| Order id | `id` | Yes → `external_id` |
| Human number | `reference_id` | Yes → `external_order_number` |
| Status | `status.slug` / `status.name` | Yes |
| Customer | `customer.name`, `customer.mobile` | Yes |
| Line items | `items` / `line_items` | Partial |
| Product id | `product_id` | Yes |
| Variant id | `variant_id` | Yes (when present) |
| Item name | `name` / `product_name` | Yes |
| Quantity | `quantity` | Yes |
| Unit price | `price.amount` (dict) or flat | Adapter only — float on `OrderItem.unit_price` |
| Item total | `total`, `amount` on line | **Not extracted** |
| Grand total | `amounts.total`, fallbacks `sub_total`, `raw.total` | Adapter yes; store_sync partial |
| Subtotal | `amounts.sub_total` | Used as **fallback for total** (risk) |
| Tax / VAT | `amounts.tax` | **Not stored** |
| Discount | `amounts.discount` | **Not stored** |
| Coupon | coupon block on order | **Not stored** |
| Shipping | `amounts.shipping` / shipping block | **Not stored** |
| Payment / COD fees | payment fees on order | **Not stored** |
| Currency | `amounts.*.currency` | Adapter only on `NormalizedOrder.currency` — **not persisted on Order** |
| Raw payload | full webhook body | **Not preserved** (only `metadata.created_at`, `payment_method`) |

**Listing vs detail:** `get_orders` uses paginated list endpoint. Line items may lack full `price` objects compared to webhook/detail payloads.

### 2.B Nahla storage

**`orders` table** (`database/models.py`):

| Column | Content |
|--------|---------|
| `total` | VARCHAR — single grand-total string |
| `line_items` | JSONB — shape varies by ingest path |
| `customer_info` | JSONB |
| `status`, `source`, `external_id`, `external_order_number` | As expected |
| `extra_metadata` | Minimal: `created_at`, `payment_method` — **no Salla `amounts` blob** |

**No `order_items` table.** No columns for subtotal, tax, shipping, discount, coupon, fees, currency.

**Two ingest shapes for `line_items`:**

1. **After `sync_orders`:** Pydantic `OrderItem` dicts — `{product_id, product_title, variant_id, quantity, unit_price}` (float or null).
2. **After webhook:** Raw Salla items — often `price: {amount, currency}` dicts; `normalize_line_item` fails to coerce dict prices to float.

### 2.C Price mismatch — root-cause hypotheses (ranked)

1. **Webhook grand-total extraction bug (high confidence)**  
   `store_sync._normalise_order` sets:
   ```python
   raw_total = raw.get("total") or raw.get("sub_total") or raw.get("amounts", {})
   total = _extract_amount_string(raw_total)
   ```
   When only `amounts.total.amount` exists, `raw_total` becomes the whole `amounts` object. `_extract_amount_string` looks for top-level `amount`/`value` on that dict — **empty string / wrong total**.

2. **Line-item price dict not parsed (high confidence)**  
   `parse_unit_price()` (`wa_order_line_item_evidence.py`) does not handle `{"amount": N, "currency": "SAR"}`.  
   `normalize_line_item()` (`wa_cart_line_items.py`) tries `float(str(dict))` and leaves the dict unchanged.  
   Dashboard detail for non-WhatsApp orders uses `sanitize_line_item_without_db` → **unit_price and line_total show null** even when Salla sent prices.

3. **Adapter sub_total fallback understates grand total (medium)**  
   `_normalize_order` tries `amounts.sub_total` before `raw.total` when `amounts.total` is missing/zero. Merchant sees subtotal without tax/shipping/discount.

4. **No monetary breakdown persisted (medium)**  
   Discounts, coupons, VAT, shipping, COD fees are dropped at ingest. Nahla cannot display Salla's breakdown or explain total vs sum of lines.

5. **Sync path simplifies line items (medium)**  
   `sync_orders` → `_normalise_order(NormalizedOrder.dict())` stores adapter-simplified items. Listing API may omit per-line `unit_price` → null prices in DB.

6. **Catalog vs order price confusion (medium, products-related)**  
   `enrich_line_item_with_catalog` overwrites `unit_price` with catalog variant/product price — **only for `source=whatsapp`**. Salla orders use `sanitize_line_item_without_db`, but merchants comparing catalog list price to order lines will see drift when catalog price ≠ paid price (sales, coupons, overrides).

7. **String/float VARCHAR total (low)**  
   `Order.total` is string; parsing is generally OK via `parse_amount_sar`, but empty string from bug #1 yields 0.00 display.

8. **Rounding (low)**  
   No evidence of systematic rounding errors; issue is missing/wrong fields, not arithmetic.

9. **Status mapping (orthogonal)**  
   Status extraction is well-tested (`test_order_status_pipeline.py`); not the reported price issue.

10. **Refunds/cancellations (unknown)**  
    No dedicated refund amount sync; cancelled orders may retain original total.

### 2.D Required policy (proposal)

For **`source=salla`** (and platform-synced orders generally):

1. **Salla `amounts.total` (grand total) is source of truth** for dashboard order amount.
2. **Preserve full Salla monetary payload** in `orders.extra_metadata.salla_amounts` (or dedicated JSONB) on every webhook and sync upsert.
3. **Line items:** store Salla's per-line `unit_price`, `total`, and raw `price` object; do not replace with catalog price for display.
4. **Display:** dashboard shows Salla totals and breakdown when `source=salla`; label recomputed values if shown at all.
5. **Internal recomputation** (analytics, AI) may derive estimates but must not overwrite persisted Salla evidence.

### 2.E Minimum safe fix PR (proposed — not in this audit)

**Title:** `fix(orders): preserve Salla monetary totals`

**Smallest correct diff:**

1. Fix `_extract_amount_string` / `_normalise_order` to resolve nested `amounts.total` (mirror adapter logic).
2. Extend `parse_unit_price` (or Salla-specific helper) to read `price.amount` dicts.
3. On webhook + sync upsert, persist `amounts`, `currency`, and raw line items (or normalized + `salla_raw`).
4. Dashboard: for `source=salla`, show persisted grand total; parse line prices from Salla shapes; optional breakdown section from `salla_amounts`.
5. Tests (`test_order_status_pipeline.py` or new `test_salla_order_monetary_fidelity.py`):
   - subtotal, VAT, discount, coupon, shipping, grand total
   - nested `amounts` webhook
   - `price: {amount}` line items
   - variant line price
   - tenant isolation
   - rounding edge (e.g. 99.99)

**Do not implement in audit PR** — scope is documentation only.

---

## 3. Offers / promotions audit

### 3.1 What Nahla calls an "offer"

| Nahla concept | Location | Salla sync? |
|---------------|----------|-------------|
| **Promotion** | `promotions` table, `routers/promotions.py`, `dashboard/pages/Promotions.tsx` | **No** |
| **Promotion engine** | `services/promotion_engine.py` — `materialise_for_customer()` issues personal `Coupon` rows | Sets `metadata.salla_synced: false` |
| **Campaign offers** | Automations seed, seasonal (National Day, etc.) | Internal |
| **Coupon levels** | `coupons.coupon_level` (bronze/silver/gold/vip) | Coupons sync separately |
| **Offer decisions** | `routers/offer_decisions.py` — analytics/KPIs | N/A |
| **WhatsApp templates** | `special_offer` template keys | Meta templates, not Salla |

`Promotion` model docstring (`database/models.py`) explicitly states promotions are Nahla source-of-truth and **do not depend on each platform's promotional API**.

### 3.2 What Salla calls an "offer"

In Nahla's Salla adapter today:

| Salla concept | Nahla mapping | API |
|---------------|---------------|-----|
| Coupons | `get_coupons`, `create_coupon`, `get_active_offers` | `GET/POST /coupons` |
| "Offers" in adapter | `get_active_offers()` → **aliases active coupons** | Same `/coupons` |
| Special offers / product sales | **Not implemented** | Salla has separate merchandising APIs (product sale price, special offers) — **no Nahla adapter methods** |
| Marketing campaigns | **Not implemented** | — |

**No methods found:** `get_offers`, `create_offer`, `get_promotions`, `create_promotion`, special-offer webhooks.

### 3.3 Webhooks

`webhook_dispatcher.py` handles `order.*`, `product.*` — **no coupon or offer webhooks**.

### 3.4 Sync direction today

| Direction | Status |
|-----------|--------|
| Salla coupons → Nahla | Yes (`sync_coupons`) |
| Nahla coupons → Salla | Yes (manual push + generator paths) |
| Salla offers/promotions → Nahla | **No** |
| Nahla promotions → Salla | **No** |
| Product sale prices as offers | **No** |

### 3.5 Salla API limitations (document for review)

Without a dedicated adapter audit against live Salla OpenAPI:

- **Coupon-type discounts** — supported (implemented).
- **Automatic cart rules / buy-X-get-Y / free shipping promotions** — Nahla models these in `Promotion` but **does not map to Salla promotion APIs**. Salla may expose "Special Offers" or cart rules under separate endpoints; **not wired in Nahla**.
- **Product-level sale price** — Salla product payload may include sale pricing; Nahla `NormalizedProduct` only maps `price.amount`, **not sale/promo price** (see §4).

### 3.6 Source-of-truth proposal

| Created on | Policy |
|------------|--------|
| Salla | Import to Nahla when API supports the offer type; today = coupons only |
| Nahla | Push only when Salla API supports matching type; promotions/campaigns must show **"غير متزامن مع سلة"** until implemented |

### 3.7 Minimum safe PRs (order)

1. `fix(offers): show Salla offer sync status` — Promotions UI: badge when promotion is Nahla-only; link to coupon sync for code-based offers.
2. Research + `chore(offers): audit Salla offer API surface` — map Salla special offers / cart rules to Nahla promotion types.
3. `feat(offers): import Salla-supported offer types` — only after API mapping confirmed.
4. `feat(offers): push Nahla promotions to Salla` — only for types with 1:1 API support.

---

## 4. Products / settings audit

### 4.1 Import status

| Capability | Status |
|------------|--------|
| Products imported from Salla | Yes — `StoreSyncService.sync_products()` |
| Variants | Yes — `_upsert_variants_for()` when `CATALOG_VARIANT_SYNC` enabled |
| Webhook incremental | `product.created/updated/deleted` |
| Full API required | OAuth + `api_sync_enabled` for adapter calls |
| Merchant visibility | Store integration page (#453); sync errors logged — **no dedicated product sync error UX on catalog page** |

### 4.2 Price fields

| Field | Salla adapter | Persisted |
|-------|---------------|-----------|
| Regular price | `price.amount` → `NormalizedProduct.price` | `products.price` (string) |
| Sale / promo price | **Not read** in `_normalize_product` | `_normalise_product` has `sale_price` key but `NormalizedProduct` lacks field → **usually empty**; only in `extra_metadata` if present |
| Variant price | `variant.price.amount` | `product_variants.price` |
| Tax settings | Not synced | — |
| Options / required flags | Yes | `extra_metadata` + variant rows |

### 4.3 Alignment gaps

| Question | Answer |
|----------|--------|
| Prices matching Salla? | Regular price generally yes; **sale/promotional prices often no** |
| Variants matching? | Yes when variant sync on |
| Nahla shows catalog or order price? | Catalog uses `Product.price`; orders should use Salla line prices (see §2 — often broken) |
| Product settings for coupon/offer logic? | Coupons can set `exclude_sale_products` on Salla push; Nahla does not track Salla sale state on products |

### 4.4 Minimum safe PR (later)

`fix(products): align Salla product sale price and variant sync` — read Salla `sale_price` / `regular_price` / `is_on_sale`; persist in metadata + optional column; surface in catalog UI.

**Not in this audit.**

---

## 5. Review readiness matrix

| Area | Current status | Works? | Gaps | Required PR |
|------|----------------|--------|------|-------------|
| **Coupons** | Two-way sync prod-validated; basic fields aligned | **Pass** | Advanced Salla rules are backlog only | None for basic review scope |
| **Orders** | Sync + webhooks ingest; status OK | **Fail** | Price/tax/shipping/discount not preserved; webhook total bug; line price dict parsing | `fix(orders): preserve Salla monetary totals` |
| **Offers** | Nahla-internal promotions + coupon sync only | **Fail** | No Salla offer import/push; UI implies broader "عروض" without sync truth | `fix(offers): show Salla offer sync status` then API audit/import |
| **Products** | Full/incremental import + variants | **Partial** | Sale price, tax settings, sync error visibility | `fix(products): align Salla product price and variant sync` |
| **AI** | Out of scope | Other agent | — | None here |

---

## 6. Recommended PR order (after this audit)

1. `fix(orders): preserve Salla monetary totals`
2. `fix(offers): show Salla offer sync status` — after §3.8 Salla UI mapping
3. `fix(products): align Salla product price and variant sync`

(Coupons basic review scope: complete — advanced rules backlog.)

---

## 7. Files inspected

### Coupons
- `backend/services/coupon_salla_push.py`
- `backend/services/coupon_sync_visibility.py`
- `backend/services/store_sync.py` (`sync_coupons`, `_normalise_coupon`)
- `backend/store_adapters/salla_adapter.py` (`create_coupon`, `get_coupons`, `get_active_offers`)
- `backend/routers/coupons.py`
- `dashboard/src/pages/Coupons.tsx`
- `dashboard/src/utils/couponSallaSyncError.tsx`

### Orders
- `backend/store_adapters/salla_adapter.py` (`get_orders`, `_normalize_order`)
- `backend/services/store_sync.py` (`sync_orders`, `handle_order_webhook`, `_normalise_order`, `_extract_amount_string`)
- `backend/core/webhook_dispatcher.py`
- `backend/core/order_amount_display.py`
- `backend/core/wa_order_line_item_evidence.py`
- `backend/core/wa_cart_line_items.py`
- `backend/routers/orders.py`
- `database/models.py` (`Order`)
- `tests/test_order_status_pipeline.py`

### Offers / promotions
- `database/models.py` (`Promotion`, `Coupon`)
- `backend/services/promotion_engine.py`
- `backend/services/offer_decision_service.py`
- `backend/routers/promotions.py`
- `backend/routers/offer_decisions.py`
- `dashboard/src/pages/Promotions.tsx`

### Products
- `backend/store_adapters/salla_adapter.py` (`get_products`, `_normalize_product`, `_normalize_variant`)
- `backend/services/store_sync.py` (`sync_products`, `_normalise_product`, `_upsert_variants_for`)
- `database/models.py` (`Product`, `ProductVariant`)

### Merchant integration UI (context)
- `dashboard/src/pages/StoreIntegration.tsx`
- `dashboard/src/utils/sallaMerchantIntegration.ts`

### Prior audit docs (reference)
- `docs/audits/salla-coupons-sync.md`
- `docs/audits/salla-full-api-link-coupons.md`

---

## 8. Warning

**Salla commerce review is not ready.**

Coupons are one cleanup flag away from sign-off. Orders have confirmed code-level gaps that explain reported price mismatches. Offers and product sale pricing are not aligned with Salla. Complete the PR sequence above before claiming review readiness.
