"""
services/shopify_catalog
────────────────────────
Dormant, disabled-by-default, read-only Shopify catalog import (Admin GraphQL
API pinned to 2026-10) into a provider-scoped mirror.

"Imported" means: one committed, complete, Shopify-confirmed snapshot of the
shop's products, variants, inventory items and — only when ``read_inventory``
is actually granted — locations and per-location levels, stored in this
package's own tables (``models.ShopifyCatalogBase``, revision 0122).

It is **not** the shared catalog hub. Nothing here writes ``products`` /
``product_variants``, registers a store adapter, feeds the AI runtime,
WhatsApp, Meta, campaigns or any channel, or changes Salla, prompts, persona
or model selection. Projection into the hub (and any channel publication) is
a later, separately approved slice.

Nothing runs unless ``NAHLA_SHOPIFY_CONNECTION_ENABLED`` and
``NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED`` are both truthy and the connection
foundation's availability gate holds.

Module map:

  config.py     flag, pinned API version, scopes, fixed bounds and page sizes
  errors.py     closed set of outcome codes
  models.py     mirror, run and staging tables (not part of ``models.Base``)
  graphql.py    pinned, fenced, bounded GraphQL client
  queries.py    GraphQL documents
  parse.py      strict node validation (GIDs, money, enums, images)
  traversal.py  complete traversal with nested pagination and completeness proofs
  store.py      runs, leases, fences, staging, single-transaction publish, read model
  importer.py   one run end to end
"""
