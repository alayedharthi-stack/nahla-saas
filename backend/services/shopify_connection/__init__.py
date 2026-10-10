"""
services/shopify_connection
───────────────────────────
Dormant, disabled-by-default Shopify secure connection foundation.

Standalone authorization-code grant with expiring offline tokens
(``docs/engineering/adr/0002-shopify-standalone-auth-code.md``). This package
owns its own tables (``models.ShopifyBase``) and never touches the shared
``integrations/shared`` OAuth helpers, the ``Integration`` table, the store
adapter registry, the catalog, the AI runtime, Salla, Meta, WhatsApp or
Moyasar.

Nothing here runs unless ``NAHLA_SHOPIFY_CONNECTION_ENABLED`` is truthy and
every other precondition in ``config.evaluate_availability`` holds.

Module map:

  config.py      flag, exact callback / dashboard targets, scopes, credentials
  shop_domain.py canonical single-label ``<name>.myshopify.com`` identity
  crypto.py      AES-256-GCM token cipher bound to tenant/shop/generation
  oauth.py       callback query HMAC, authorize URL, token exchange / refresh,
                 authenticated GraphQL shop identity check
  webhooks.py    raw-body webhook HMAC and signed uninstall payload identity
  models.py      Shopify-owned tables (not part of ``models.Base``)
  actor.py       DB revalidation of the tenant actor behind a JWT
  lifecycle.py   state, claim, refresh, disconnect, uninstall, reconcile
"""
