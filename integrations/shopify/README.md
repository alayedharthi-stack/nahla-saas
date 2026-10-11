# Shopify Integration

This directory is a placeholder. **No Shopify sync exists**: no product,
order, customer or coupon data is synchronised with Shopify stores.

A dormant, disabled-by-default secure connection foundation (authorization
and credential lifecycle only) lives in `backend/services/shopify_connection/`;
see `docs/engineering/shopify-connection-foundation.md` and
`docs/adr/0006-shopify-standalone-auth-code-expiring-offline-tokens.md`.

A dormant, disabled-by-default, read-only catalog import into a separate
provider-scoped mirror lives in `backend/services/shopify_catalog/`; see
`docs/engineering/shopify-catalog-import.md`. It writes nothing to the shared
catalog and reaches no channel (AI, WhatsApp, Meta).
