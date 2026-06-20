# Discovery settings — merchant API usage

Merchants configure catalog discovery through **generic collections** and **featured products**.
Settings are stored per tenant in `tenant_settings.ai_settings["discovery_settings"]`.
There is no platform seed data and no merchant-specific logic in the codebase.

Configure via the dashboard (planned) or the API below. The authenticated JWT resolves the tenant;
do not hardcode `tenant_id` in client code.

## Prerequisites

```bash
export NAHLA_API="https://api.nahlah.ai"   # or your backend URL
export NAHLA_JWT="<merchant_session_jwt>"
```

Support impersonation tokens cannot mutate discovery settings (`require_not_support_impersonation`).

## 1. Inspect current settings

```http
GET /settings/discovery
Authorization: Bearer $NAHLA_JWT
```

Response shape:

```json
{
  "tenant_id": 123,
  "discovery_settings": {
    "default_mode": "",
    "initial_product_count": 3,
    "featured_product_ids": [],
    "collections": [],
    "guided_question": "وش نوع المنتج اللي تدور عليه؟",
    "small_catalog_threshold": 5
  }
}
```

## 2. Resolve product and variant IDs from the catalog

Look up IDs from the merchant's synced catalog — never guess or hardcode IDs in platform code.

```http
GET /merchant/catalog/products?limit=50
Authorization: Bearer $NAHLA_JWT
```

For each product row, use:

- `id` → `product_id` in discovery settings
- `default_variant_id` or a variant row `id` → `variant_id` (empty string when the product has no variant rows)

Repeat for every product you want to feature inside a collection.

## 3. Replace full discovery settings

`PUT` validates product/variant IDs against the tenant catalog and drops invalid references.

```http
PUT /settings/discovery
Authorization: Bearer $NAHLA_JWT
Content-Type: application/json
```

Example payload (replace labels, IDs, and priorities for your catalog):

```json
{
  "default_mode": "collections_first",
  "initial_product_count": 3,
  "featured_product_ids": [],
  "guided_question": "وش نوع المنتج اللي تدور عليه؟",
  "small_catalog_threshold": 5,
  "collections": [
    {
      "id": "primary_category",
      "label": "Primary category label",
      "priority": 1,
      "enabled": true,
      "catalog_match": "optional catalog search hint",
      "featured_products": [
        {
          "product_id": "<PRODUCT_ID>",
          "variant_id": "<VARIANT_ID_OR_EMPTY>",
          "priority": 1,
          "label_override": "Optional display label"
        },
        {
          "product_id": "<PRODUCT_ID_2>",
          "variant_id": "",
          "priority": 2,
          "label_override": ""
        }
      ]
    },
    {
      "id": "secondary_category",
      "label": "Secondary category label",
      "priority": 2,
      "enabled": true,
      "catalog_match": "",
      "featured_products": []
    },
    {
      "id": "wholesale",
      "label": "Wholesale / bulk label",
      "priority": 3,
      "enabled": true,
      "catalog_match": "bulk",
      "featured_products": []
    }
  ]
}
```

`default_mode` values used by the brain: `collections_first`, `featured_first`, or empty string for platform defaults.

## 4. Incremental updates (optional)

Reorder collections:

```http
POST /settings/discovery/collections/reorder
Content-Type: application/json

{ "collection_ids": ["primary_category", "secondary_category", "wholesale"] }
```

Enable or disable one collection:

```http
PATCH /settings/discovery/collections/{collection_id}/enabled
Content-Type: application/json

{ "enabled": true }
```

Add or update one featured product inside a collection:

```http
POST /settings/discovery/collections/{collection_id}/featured
Content-Type: application/json

{
  "product_id": "<PRODUCT_ID>",
  "variant_id": "<VARIANT_ID_OR_EMPTY>",
  "priority": 1,
  "label_override": ""
}
```

Remove a featured product:

```http
DELETE /settings/discovery/collections/{collection_id}/featured/{product_id}
```

## 5. Verify browse behavior

After saving, exercise the WhatsApp discovery flow for the merchant:

1. Broad browse (e.g. catalog inventory question) → should list **collections** when `default_mode` is `collections_first`.
2. Category follow-up matching a collection label or `catalog_match` → should list **featured products** for that collection, ordered by `priority`.

Regression coverage lives in `backend/tests/test_merchant_discovery_settings_phase4a.py` and discovery browse tests; those use synthetic IDs only.

## Platform rules

- Operational discovery layout is **merchant data**, not repository code.
- Do not commit tenant-specific setup scripts, product ID lists, or category-specific seed logic.
- Dashboard UI for this API is the preferred long-term configuration surface.
