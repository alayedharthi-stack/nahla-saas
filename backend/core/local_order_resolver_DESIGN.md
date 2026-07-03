# Unified Local Order Resolver — Design (thin slice)

**Principle:** Order is Order. Once a row exists in local `orders`, it is the operational truth for AI replies and the merchant order board — regardless of how it arrived.

## Sources (local `orders.source`)

| Source | How it enters `orders` | Adapter role |
|--------|------------------------|--------------|
| `whatsapp` | `nahla_order_bridge.sync_nahla_wa_order` | None for reads — write/sync only |
| `salla` | `store_sync`, webhooks, poll | Import + refresh metadata |
| `shopify` | `store_sync` | Import + refresh metadata |
| `zid` | `store_sync` | Import + refresh metadata |
| `manual` | Dashboard / admin | None |
| future | Same pattern | Import/sync only |

Adapters **must not** be the first lookup for customer-facing order answers.

## Read path (this PR)

```
resolve_customer_order_context(db, tenant_id, conversation_id?, customer_id?, phone?, intent?)
        │
        ├─► conversation-scoped WhatsApp draft (nahla-wa-{tenant}-{conv}%)
        ├─► tenant-scoped customer orders (phone / customer_id)
        └─► classify: open / paid / shipped + priority list
```

External adapter calls are **out of scope** for the resolver itself. `CommerceToolRuntime._tool_track_order` consults local DB first; adapter is fallback only when local returns nothing.

## Priority (selection)

1. **Active WhatsApp draft** for the current `conversation_id` (open, non-terminal, `nahla-wa-*` prefix).
2. **Explicit order number** when provided (`external_order_number` / `external_id` / internal `id`).
3. **Latest open order** for the customer (any source, highest `id`, not cancelled/abandoned/delivered/completed).
4. **Latest shipped order** when `intent=track_order` and no open order matches.
5. **Latest paid order** as weaker tracking signal.

`selected_reason` records which rule won.

## Intents (thin slice)

| Intent | Reply surface | Behaviour |
|--------|---------------|-----------|
| `order_number` | OrderFlowV2 `كم رقم الطلب` | `selected_order.display_reference` or honest “no number yet” |
| `track_order` | Brain `وين طلبي` / `track_order` tool | Local order payload before adapter; no `no_orders` if local row exists |
| *(default)* | Fallback helpers | Same priority without adapter |

## What stays in adapters / import

- Pulling new rows from Salla / Shopify / Zid APIs
- Refreshing `status`, `line_items`, `checkout_url` on sync
- Platform-specific metadata in `extra_metadata`

## What moves to local resolver (now + later)

**Now (thin slice):** order number, track / `وين طلبي`, honest no-orders guard.

**Later:** payment status replies, multi-order disambiguation, shipment enrichment, dashboard context blocks.

## Non-goals (this PR)

- No coupon / #413 work
- No `store_ai_mode`, allowlist, KB, payment/shipping config changes
- No merchant-specific hardcoding
