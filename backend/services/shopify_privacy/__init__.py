"""
services/shopify_privacy
────────────────────────
Dormant, disabled-by-default intake and evidence handling for Shopify's
mandatory privacy compliance webhooks (``customers/data_request``,
``customers/redact``, ``shop/redact``). See
``docs/engineering/shopify-privacy-webhooks.md``.

This package registers no subscription, calls no Shopify API, erases no real
data, answers no merchant and discloses nothing. It owns its own tables
(``models.ShopifyPrivacyBase``, revision 0123) and reads — never rewrites —
the connection foundation's ownership rows; the only foundation write is the
worker's request for a revalidation probe through the foundation's own
durable reconcile schedule. It never touches the AI runtime, Salla, Meta,
WhatsApp, Moyasar, ``products`` or any other shared table.

Module map:

  config.py    flags (intake / worker / destructive, all off), bounds, audit version
  payloads.py  strict JSON and signed-body topic classification (no PII returned)
  headers.py   unsigned diagnostic headers reduced to canonical UUID / timestamp
  models.py    ShopifyPrivacyBase tables (not models.Base)
  store.py     identity resolution and durable, idempotent receipt
  audit.py     counts-only audited-schema and legacy-footprint checks
  worker.py    leased, fenced evaluation into open outcome states
  cleanup.py   fixture-tested mirror cleanup helper and policy-gated executor
"""
