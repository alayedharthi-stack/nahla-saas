"""
GraphQL documents of the catalog import (Admin API 2026-10, read-only).

Every connection is selected with ``pageInfo { hasNextPage endCursor }`` and
followed to its end by a dedicated follow-up query; nothing relies on a
single page. Field choices (coordinator-verified against the official
2026-10 reference on 2026-10-11; not fetched from this environment, and
not validated against a live store):

* ``Product.status`` — ACTIVE / ARCHIVED / DRAFT / UNLISTED. Status is not
  publication; ``publishedAt`` (Online Store) is recorded separately.
* ``featuredMedia`` / ``media`` (product) and ``media`` (variant); the
  deprecated ``featuredImage`` / ``images`` / ``ProductVariant.image`` are not
  used.
* ``ProductVariant.price`` / ``compareAtPrice`` — decimal strings in the
  shop's default currency (``shop.currencyCode``); contextual (market)
  pricing is not read.
* ``InventoryItem { id tracked updatedAt }`` — readable with ``read_products``.
* ``InventoryLevel`` (``quantities(names: ["available"])``, ``updatedAt``,
  ``location``) and ``Location`` (``isActive``, ``fulfillsOnlineOrders``) —
  only requested when ``read_inventory`` is granted.
* ``ProductVariant.inventoryQuantity`` is deliberately **not** read: it is a
  total across locations, including ones that cannot fulfil online orders.
"""
from __future__ import annotations

MEDIA_FIELDS = """
fragment NahlaMedia on Media {
  id
  mediaContentType
  ... on MediaImage { image { url altText } }
}
"""

VARIANT_FIELDS = """
fragment NahlaVariant on ProductVariant {
  id
  legacyResourceId
  title
  sku
  barcode
  position
  price
  compareAtPrice
  availableForSale
  inventoryPolicy
  requiresComponents
  createdAt
  updatedAt
  selectedOptions { name value }
  inventoryItem { id tracked updatedAt }
}
"""

PRODUCT_FIELDS = """
fragment NahlaProduct on Product {
  id
  legacyResourceId
  title
  handle
  status
  vendor
  productType
  createdAt
  updatedAt
  publishedAt
  isGiftCard
  requiresSellingPlan
  combinedListingRole
  hasOnlyDefaultVariant
  options { name position optionValues { name } }
  featuredMedia { ...NahlaMedia }
  variantsCount { count precision }
}
"""

PREFLIGHT = """
query NahlaCatalogPreflight {
  shop { id myshopifyDomain currencyCode }
  currentAppInstallation { accessScopes { handle } }
  productsCount(limit: null) { count precision }
}
"""

PRODUCTS_COUNT = """
query NahlaCatalogCount {
  shop { currencyCode }
  productsCount(limit: null) { count precision }
}
"""

PRODUCTS = """
query NahlaCatalogProducts($first: Int!, $after: String, $variants: Int!, $media: Int!, $variantMedia: Int!) {
  products(first: $first, after: $after, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ...NahlaProduct
      media(first: $media) { pageInfo { hasNextPage endCursor } nodes { ...NahlaMedia } }
      variants(first: $variants) {
        pageInfo { hasNextPage endCursor }
        nodes {
          ...NahlaVariant
          media(first: $variantMedia) { pageInfo { hasNextPage endCursor } nodes { ...NahlaMedia } }
        }
      }
    }
  }
}
""" + PRODUCT_FIELDS + VARIANT_FIELDS + MEDIA_FIELDS

PRODUCT_VARIANTS = """
query NahlaCatalogProductVariants($id: ID!, $first: Int!, $after: String, $variantMedia: Int!) {
  product(id: $id) {
    id
    variantsCount { count precision }
    variants(first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        ...NahlaVariant
        media(first: $variantMedia) { pageInfo { hasNextPage endCursor } nodes { ...NahlaMedia } }
      }
    }
  }
}
""" + VARIANT_FIELDS + MEDIA_FIELDS

PRODUCT_MEDIA = """
query NahlaCatalogProductMedia($id: ID!, $first: Int!, $after: String) {
  product(id: $id) {
    id
    media(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { ...NahlaMedia } }
  }
}
""" + MEDIA_FIELDS

VARIANT_MEDIA = """
query NahlaCatalogVariantMedia($id: ID!, $first: Int!, $after: String) {
  productVariant(id: $id) {
    id
    media(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { ...NahlaMedia } }
  }
}
""" + MEDIA_FIELDS

LOCATIONS = """
query NahlaCatalogLocations($first: Int!, $after: String) {
  locations(first: $first, after: $after, sortKey: ID, includeInactive: true, includeLegacy: true) {
    pageInfo { hasNextPage endCursor }
    nodes { id name isActive fulfillsOnlineOrders }
  }
}
"""

LEVEL_FIELDS = """
fragment NahlaLevel on InventoryLevel {
  id
  updatedAt
  quantities(names: ["available"]) { name quantity }
  location { id }
}
"""

INVENTORY_ITEMS = """
query NahlaCatalogInventory($ids: [ID!]!, $first: Int!) {
  nodes(ids: $ids) {
    __typename
    ... on InventoryItem {
      id
      tracked
      updatedAt
      inventoryLevels(first: $first) { pageInfo { hasNextPage endCursor } nodes { ...NahlaLevel } }
    }
  }
}
""" + LEVEL_FIELDS

INVENTORY_LEVELS = """
query NahlaCatalogInventoryLevels($id: ID!, $first: Int!, $after: String) {
  inventoryItem(id: $id) {
    id
    inventoryLevels(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { ...NahlaLevel } }
  }
}
""" + LEVEL_FIELDS

CONFIRM_NODES = """
query NahlaCatalogConfirm($ids: [ID!]!) {
  nodes(ids: $ids) { __typename id }
}
"""
