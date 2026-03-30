---
name: Real Store Integration Layer
description: Store integration layer with Salla adapter — architecture, status, and what remains
type: project
---

Real Store Integration Layer was implemented on 2026-03-30.

**Architecture:**
- `backend/store_adapters/base_adapter.py` — abstract `BaseStoreAdapter` with 11 methods
- `backend/store_adapters/salla_adapter.py` — full Salla API v2 adapter (products, orders, payments, shipping, coupons) using `@register_adapter("salla")` decorator
- `backend/store_integration/registry.py` — `get_adapter(tenant_id)` reads `Integration` table (provider, config JSONB, enabled)
- `backend/store_integration/` — ProductService, OrderService, PaymentService, ShippingService, OfferService, IntegrationLogger
- Credentials stored in existing `Integration` model (provider="salla", config={api_key, store_id, webhook_secret})

**Integration points in AI Sales Agent:**
- `_get_product_catalog()` — tries live Salla products first, falls back to Nahla DB
- `ai_sales_process_message` — shipping from live adapter when intent=shipping_info; payment link via PaymentService
- `ai_sales_create_order` — creates real Salla order for pay_now method; stores external_id on Order row

**Endpoints added:**
- GET/PUT/DELETE `/store-integration/settings`
- GET `/store-integration/test` — validates connection and returns product count

**Frontend:**
- `dashboard/src/pages/StoreIntegration.tsx` — Arabic settings UI at route /store-integration
- `dashboard/src/api/storeIntegration.ts` — typed API client
- Sidebar: "ربط المتجر" nav item in store group

**Why:** Users need real product/order/payment data from Salla instead of demo data or local DB-only records.

**What remains for full production:**
- COD orders through Salla (currently only pay_now goes to Salla)
- Token refresh: Salla OAuth tokens expire — need refresh_token flow
- Product sync: pull Salla products into local DB for offline use / faster access
- Shipping integration in orchestrator (ai-orchestrator) — it still uses local DB only
