# Shopify catalog import (dormant, read-only) — work in progress

Status: **Draft, in progress, disabled by default, not deployed.** Stacked on
the Shopify connection foundation (PR #1202) and the merchant connection UI
(PR #1203). Nothing here is "Shopify sync" and nothing here is ready for
merchants.

This document is replaced by the full engineering record as the slice lands.

## Scope of this slice

* Read-only GraphQL Admin API **2026-10** import of products, variants,
  images (media), inventory items and — only where the granted scopes allow
  it — per-location inventory levels and locations.
* Provider-scoped identity in Shopify-owned tables (`ShopifyCatalogBase`,
  revision `0122`), never in `products` / `product_variants`; no Salla, Meta,
  WhatsApp, AI runtime, prompt, persona or model change.
* Truthful import state (`not_started`, `importing`, `imported`, `error`)
  behind a new default-off flag.

## Not in this slice

Projection into the shared catalog hub, Meta export, WhatsApp publication,
webhooks / subscriptions, scope widening or reauthorization, orders,
customers, payments, markets / contextual pricing, bulk operations, live
validation against a real store.
