"""
core/catalog.py
───────────────
Per-tenant Meta WhatsApp Catalog helpers shared by the catalog sender,
the brain pipeline, and admin/debug endpoints.

This module owns ONE behavioural decision: "is the catalog actually
usable for THIS turn, with THESE products?". Every catalog send site
calls :func:`is_catalog_eligible` first and routes to the fallback path
on False — that keeps the kill-switch logic in a single place instead
of scattered booleans across senders.

No I/O, no DB session, no external calls — pure read of dataclass /
ORM attributes so the helpers can be reused inside tests, decision
engines, and prompt builders without dragging in a session.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("nahla.catalog")


# ─────────────────────────────────────────────────────────────────────────────
# Retailer-id resolution
# ─────────────────────────────────────────────────────────────────────────────

def effective_retailer_id(product: Any) -> str:
    """Return the Meta ``product_retailer_id`` to use for *product*.

    Resolution order (first non-empty wins):

    1. ``product.meta_retailer_id`` — explicit override populated by the
       merchant (or a future "publish to Meta" job).
    2. ``product.external_id`` — the platform-side id. This matches the
       Salla Commerce auto-publish convention: when Salla pushes a
       product to a linked Meta catalog the retailer id Salla writes is
       the Salla product id, which is exactly what we already store on
       ``Product.external_id``. Treating that as the default means 95%
       of merchants get catalog rendering with zero manual mapping.

    Returns an empty string when neither source is populated — callers
    MUST treat that as "not eligible for catalog send" and route to the
    fallback (image + CTA URL) path. Never raises.

    The function is intentionally tolerant of plain dicts (used by the
    ``[PRODUCT:...]`` resolver in ``whatsapp_webhook.py``) and ORM
    instances alike.
    """
    if product is None:
        return ""
    # ORM instance path
    rid = getattr(product, "meta_retailer_id", None)
    if rid is None and isinstance(product, dict):
        rid = product.get("meta_retailer_id")
    if rid:
        return str(rid).strip()
    ext = getattr(product, "external_id", None)
    if ext is None and isinstance(product, dict):
        ext = product.get("external_id")
    if ext:
        return str(ext).strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Canonical retailer-id + auto-mapping (May 2026 #12)
# ─────────────────────────────────────────────────────────────────────────────
#
# ``effective_retailer_id`` is a READ-only resolver and returns an empty
# string when the upstream fields are NULL. That's the right behaviour
# at SEND time — we want the catalog send to bail out cleanly when the
# product was never published to Meta — but it's the wrong behaviour at
# SYNC time, when we want every freshly-imported product to land with a
# usable retailer id so the merchant doesn't have to do anything manual.
#
# The two helpers below close that gap:
#
#   * ``canonical_retailer_id`` — same priority order as ``effective_…``
#     but with a deterministic SYNTHETIC fallback so it NEVER returns an
#     empty string. The synthetic id is namespaced (``nahla_p_<id>``)
#     so it can never collide with a real Salla / Zid product id and
#     so support can tell at a glance that the merchant hasn't published
#     to Meta yet (the catalog will still try, will not find the SKU on
#     Meta's side, and the legacy image+CTA fallback will fire).
#
#   * ``assign_canonical_retailer_id`` — writes the canonical id onto
#     ``product.meta_retailer_id`` when it's currently NULL. Idempotent;
#     returns True only when an actual change was made so the caller
#     can count writes for the resync audit endpoint. Never overwrites
#     an existing override — merchants who edit the column by hand from
#     the admin UI keep their value.


# ─────────────────────────────────────────────────────────────────────────────
# Variant-aware retailer-id resolution (migration 0064 — Phase 1)
# ─────────────────────────────────────────────────────────────────────────────
#
# After 0064 every sellable SKU lives as a ``ProductVariant`` row, with the
# parent ``Product`` carrying ``has_variants`` + ``default_variant_id``. We
# want one helper the sender / brain / Google feed can call regardless of
# what shape they're holding — a variant ORM row, a parent ORM row, or a
# plain dict (resolver still passes those in places). The resolution
# order intentionally mirrors ``effective_retailer_id`` so:
#
#   * sites that already migrated to variant rows get per-variant IDs;
#   * sites that still pass a parent get the parent's default_variant
#     retailer_id (or fall back to the legacy meta_retailer_id /
#     external_id chain) — i.e. zero regression for un-migrated callers.
#
# The function is deliberately DB-free: pass a fully populated ORM object
# (default_variant relationship already loaded) or a dict; we never open
# a session.


def _variant_retailer_id(variant: Any) -> str:
    """Return the explicit ``retailer_id`` from a variant-shaped input.

    Falls through to ``effective_retailer_id`` so a variant that was
    persisted before the migration backfill filled in ``retailer_id``
    (only theoretically possible — the migration writes it directly)
    still resolves via the legacy meta/external chain. Empty string
    means "nothing usable".
    """
    if variant is None:
        return ""
    rid = getattr(variant, "retailer_id", None)
    if rid is None and isinstance(variant, dict):
        rid = variant.get("retailer_id")
    if rid:
        return str(rid).strip()
    # Pre-backfill safety net: a variant row missing retailer_id should
    # still resolve via the legacy parent fields if they were copied
    # onto the dict / ORM row.
    return effective_retailer_id(variant)


def effective_variant_retailer_id(target: Any) -> str:
    """Variant-aware version of :func:`effective_retailer_id`.

    Accepts:

    * A ``ProductVariant`` ORM row (or any shape with ``retailer_id``)
      → returns its ``retailer_id``.
    * A ``Product`` ORM row (or dict with ``default_variant``) → returns
      the default variant's ``retailer_id``.
    * A bare dict from the legacy resolver path → falls through to the
      legacy ``effective_retailer_id`` chain (meta_retailer_id →
      external_id) so old callers don't regress.

    Returns an empty string when nothing usable is found. Never raises.
    """
    if target is None:
        return ""

    # Heuristic: a ProductVariant exposes ``product_id`` (the FK back to
    # parent). Anything carrying that is treated as variant-shaped.
    pid = getattr(target, "product_id", None)
    if pid is None and isinstance(target, dict):
        pid = target.get("product_id")
    if pid is not None:
        rid = _variant_retailer_id(target)
        if rid:
            return rid
        # Variant row exists but had no retailer_id and no legacy
        # fallback — return empty so the catalog sender bails out.
        return ""

    # Parent-shaped: try the default_variant relationship first, then
    # the legacy parent-level retailer_id.
    default_variant = getattr(target, "default_variant", None)
    if default_variant is None and isinstance(target, dict):
        default_variant = target.get("default_variant")
    if default_variant is not None:
        rid = _variant_retailer_id(default_variant)
        if rid:
            return rid

    return effective_retailer_id(target)


def canonical_retailer_id(product: Any, *, fallback_to_synthetic: bool = True) -> str:
    """Resolve a retailer id that NEVER comes back empty.

    Resolution order:

        1. ``product.meta_retailer_id``
        2. ``product.external_id``
        3. ``f"nahla_p_{product.id}"`` (when ``fallback_to_synthetic``)

    The synthetic third tier is deliberately namespaced so it can be
    spotted in logs and on Meta's side — a retailer id beginning with
    ``nahla_p_`` is by construction *not* a real Salla / Zid id, which
    tells support that this product has not been published to the Meta
    catalog yet. The catalog send for such a product will fall back to
    image + CTA — which is the desired behaviour: the merchant gets a
    rich-ish render today, and a real catalog card the moment they
    publish to Meta and resync.

    Pass ``fallback_to_synthetic=False`` when you specifically want the
    legacy empty-string behaviour (only ``effective_retailer_id`` does
    that today).
    """
    primary = effective_retailer_id(product)
    if primary:
        return primary
    if not fallback_to_synthetic:
        return ""
    pid = getattr(product, "id", None)
    if pid is None and isinstance(product, dict):
        pid = product.get("id")
    if pid is None:
        return ""
    return f"nahla_p_{pid}"


def assign_canonical_retailer_id(product: Any) -> bool:
    """Populate ``product.meta_retailer_id`` if it's currently empty.

    Returns True when a write actually happened (so callers can count
    it for audit logs / progress meters). Never overwrites a value that
    is already set, even if a "better" candidate exists — merchants who
    hand-edit the override column from the admin UI keep their value.

    Designed to be called from the Salla / Zid sync upserts AND from a
    one-shot backfill that walks every product for a tenant. Idempotent
    by construction: a second call on the same product is a no-op.
    """
    if product is None:
        return False
    current = getattr(product, "meta_retailer_id", None)
    if current and str(current).strip():
        return False
    canonical = canonical_retailer_id(product, fallback_to_synthetic=True)
    if not canonical:
        return False
    try:
        product.meta_retailer_id = canonical
    except Exception:  # noqa: BLE001
        # Plain dicts and frozen dataclasses don't allow attribute
        # writes — fall through silently so the caller can decide what
        # to do (sync passes ORM rows so this branch is dead in prod).
        if isinstance(product, dict):
            product["meta_retailer_id"] = canonical
            return True
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Product source identification (catalog source-agnostic architecture)
# ─────────────────────────────────────────────────────────────────────────────
#
# The Nahla Product Catalog is an independent first-class asset, deliberately
# decoupled from any specific upstream platform. Today it can be populated by
# the Salla sync (``services/store_sync.py``), the Zid sync, or by merchants
# entering products by hand through the dashboard. Future writers (Shopify,
# WooCommerce, CSV upload) plug in by setting ``Product.source`` and the
# usual ``Product.*`` fields — nothing else in the catalog pipeline cares.
#
# The helpers below are the SINGLE source of truth for:
#   * what ``source`` strings are recognised,
#   * how to read ``source`` off a heterogeneous Product / dict shape,
#   * how to summarise the source mix across a tenant.

# Closed set of canonical source strings. Adding a new source = adding a
# new entry here AND updating the writer. The UI badge mapping lives in
# the dashboard's CatalogPage and reads exactly these strings.
#
# Hub architecture (May 2026 #14)
# ───────────────────────────────
# Nahla Catalog is the central product hub. SOURCES feed it (one-way
# IN) and CHANNELS consume it (one-way OUT). The two directions are
# strictly separate at the data layer:
#
#     INPUT SOURCES   →   NAHLA CATALOG   →   OUTPUT CHANNELS
#     ───────────────     ─────────────       ────────────────
#     Manual entry        ``products``       WhatsApp / Meta
#     Salla sync             table           Campaigns
#     Meta import                            AI (product_resolver)
#     (future: Zid /                         (future: Google
#      Shopify / CSV)                         Merchant / Checkout)
#
# Each ``Product.source`` value names which INPUT side produced the row.
# A product imported FROM Meta has ``source = "meta"`` even if it will
# later be pushed BACK to Meta as an output channel — direction matters,
# and the input-side tag is permanent.
SOURCE_SALLA   = "salla"
SOURCE_ZID     = "zid"
SOURCE_SHOPIFY = "shopify"
SOURCE_META    = "meta"     # Legacy alias — prefer SOURCE_META_EXISTING for new writes
SOURCE_META_EXISTING = "meta_existing"
SOURCE_MANUAL  = "manual"   # Legacy alias — prefer SOURCE_NAHLA_NATIVE for new writes
SOURCE_NAHLA_NATIVE = "nahla_native"
SOURCE_NAHLA_MANAGED_META = "nahla_managed_meta"
SOURCE_UNKNOWN = "unknown"

# Legacy → canonical aliases (read normalization only).
SOURCE_ALIASES = {
    SOURCE_MANUAL: SOURCE_NAHLA_NATIVE,
    SOURCE_META: SOURCE_META_EXISTING,
}

# Sources we accept on intake. Legacy values remain readable.
KNOWN_SOURCES = frozenset({
    SOURCE_SALLA, SOURCE_ZID, SOURCE_SHOPIFY,
    SOURCE_META, SOURCE_META_EXISTING,
    SOURCE_MANUAL, SOURCE_NAHLA_NATIVE,
    SOURCE_NAHLA_MANAGED_META,
    SOURCE_UNKNOWN,
})

EXTERNAL_PLATFORM_SOURCES = frozenset({
    SOURCE_SALLA, SOURCE_ZID, SOURCE_SHOPIFY,
})

NAHLA_NATIVE_SOURCES = frozenset({
    SOURCE_MANUAL, SOURCE_NAHLA_NATIVE,
})

META_EXISTING_SOURCES = frozenset({
    SOURCE_META, SOURCE_META_EXISTING,
})

# ── Ownership modes (migration 0085) ─────────────────────────────────────

OWNERSHIP_EXTERNAL_MANAGED = "external_managed"
OWNERSHIP_NAHLA_MANAGED = "nahla_managed"
OWNERSHIP_META_READONLY = "meta_readonly"
OWNERSHIP_NAHLA_MANAGED_META = "nahla_managed_meta"
OWNERSHIP_ARCHIVED_OR_DISCONNECTED = "archived_or_disconnected"

KNOWN_OWNERSHIP_MODES = frozenset({
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_NAHLA_MANAGED,
    OWNERSHIP_META_READONLY,
    OWNERSHIP_NAHLA_MANAGED_META,
    OWNERSHIP_ARCHIVED_OR_DISCONNECTED,
})

CONFLICT_POSSIBLE_DUPLICATE = "possible_duplicate"


def normalize_source(raw: Any) -> str:
    """Map legacy source strings to canonical vocabulary."""
    if raw is None:
        return SOURCE_UNKNOWN
    s = str(raw).strip().lower()
    if not s:
        return SOURCE_UNKNOWN
    if s in SOURCE_ALIASES:
        return SOURCE_ALIASES[s]
    if s in KNOWN_SOURCES:
        return s
    return SOURCE_UNKNOWN


def infer_ownership_mode(product: Any) -> Optional[str]:
    """Infer ownership from column or source. Returns None when unknown."""
    if product is None:
        return None
    raw = getattr(product, "ownership_mode", None)
    if raw and str(raw).strip():
        mode = str(raw).strip().lower()
        if mode in KNOWN_OWNERSHIP_MODES:
            return mode
    src = normalize_source(getattr(product, "source", None))
    if src in EXTERNAL_PLATFORM_SOURCES:
        return OWNERSHIP_EXTERNAL_MANAGED
    if src in NAHLA_NATIVE_SOURCES:
        return OWNERSHIP_NAHLA_MANAGED
    if src == SOURCE_NAHLA_MANAGED_META:
        return OWNERSHIP_NAHLA_MANAGED_META
    if src in META_EXISTING_SOURCES:
        return OWNERSHIP_META_READONLY
    if src == SOURCE_UNKNOWN:
        ext = getattr(product, "external_id", None)
        if ext and str(ext).strip():
            return OWNERSHIP_EXTERNAL_MANAGED
    return None


def is_import_protected(product: Any) -> bool:
    """True when Meta import must not mutate *product*."""
    mode = infer_ownership_mode(product)
    if mode == OWNERSHIP_EXTERNAL_MANAGED:
        return True
    src = normalize_source(getattr(product, "source", None))
    return src in EXTERNAL_PLATFORM_SOURCES


def is_merchant_editable_product(product: Any) -> bool:
    """True when the merchant may edit/delete via Nahla native product CRUD."""
    if product is None:
        return False
    mode = infer_ownership_mode(product)
    if mode == OWNERSHIP_NAHLA_MANAGED:
        return True
    if mode in (
        OWNERSHIP_EXTERNAL_MANAGED,
        OWNERSHIP_META_READONLY,
        OWNERSHIP_NAHLA_MANAGED_META,
        OWNERSHIP_ARCHIVED_OR_DISCONNECTED,
    ):
        return False
    src = normalize_source(getattr(product, "source", None))
    if src in NAHLA_NATIVE_SOURCES:
        return True
    if src in EXTERNAL_PLATFORM_SOURCES or src in META_EXISTING_SOURCES:
        return False
    return False


def merchant_edit_rejection_detail(product: Any) -> Optional[str]:
    """HTTP 409 detail code when native CRUD must refuse a product row."""
    if is_merchant_editable_product(product):
        return None
    mode = infer_ownership_mode(product)
    src = normalize_source(getattr(product, "source", None))
    if mode == OWNERSHIP_META_READONLY or src in META_EXISTING_SOURCES:
        return "product_not_editable_meta_readonly"
    return "product_not_editable_external_managed"

# ── Catalog visibility (P1-G1) ─────────────────────────────────────────────
# Single vocabulary for AI + dashboard + Meta reconciliation. Legacy rows
# without ``catalog_status`` are treated as ``active``.

CATALOG_STATUS_ACTIVE = "active"
CATALOG_STATUS_ARCHIVED = "archived"
CATALOG_STATUS_REMOVED_FROM_META = "removed_from_meta"
CATALOG_STATUS_MERCHANT_HIDDEN = "merchant_hidden"

INACTIVE_CATALOG_STATUSES = frozenset({
    CATALOG_STATUS_ARCHIVED,
    CATALOG_STATUS_REMOVED_FROM_META,
    CATALOG_STATUS_MERCHANT_HIDDEN,
})


def catalog_status_of(product: Any) -> str:
    """Normalized catalog_status for ORM rows or formatted dicts."""
    if product is None:
        return CATALOG_STATUS_ACTIVE
    raw = getattr(product, "catalog_status", None)
    if raw is None and isinstance(product, dict):
        raw = product.get("catalog_status")
    status = str(raw or CATALOG_STATUS_ACTIVE).strip().lower()
    return status or CATALOG_STATUS_ACTIVE


def is_catalog_active(product: Any) -> bool:
    """True when a product may appear in AI search, recommendations, or sends.

    Enforces merchant hide, Meta removal/archive, and out-of-stock — the
    LLM must never receive inactive rows as available choices.
    """
    if product is None:
        return False
    if getattr(product, "merchant_hidden_at", None) is not None:
        return False
    if isinstance(product, dict) and product.get("merchant_hidden_at"):
        return False
    status = catalog_status_of(product)
    if status != CATALOG_STATUS_ACTIVE:
        return False
    in_stock = getattr(product, "in_stock", None)
    if in_stock is None and isinstance(product, dict):
        in_stock = product.get("in_stock", True)
    if in_stock is None:
        in_stock = True
    return bool(in_stock)


def apply_active_catalog_query_filters(query: Any, product_model: Any) -> Any:
    """SQLAlchemy filter for tenant-scoped active catalog listings."""
    return (
        query.filter(product_model.catalog_status == CATALOG_STATUS_ACTIVE)
        .filter(product_model.merchant_hidden_at.is_(None))
        .filter(product_model.in_stock.is_(True))
    )


# Output channels — for the dashboard hub diagram + future export jobs.
# Strings are stable: campaign senders + future Google Merchant export
# read this exact vocabulary when they self-report which channel a
# given send went through.
CHANNEL_WHATSAPP        = "whatsapp"
CHANNEL_META_CATALOG    = "meta_catalog"
CHANNEL_AI              = "ai"
CHANNEL_CAMPAIGNS       = "campaigns"
CHANNEL_GOOGLE_MERCHANT = "google_merchant"   # planning-only
CHANNEL_CHECKOUT        = "checkout"          # planning-only

KNOWN_CHANNELS = frozenset({
    CHANNEL_WHATSAPP, CHANNEL_META_CATALOG, CHANNEL_AI,
    CHANNEL_CAMPAIGNS, CHANNEL_GOOGLE_MERCHANT, CHANNEL_CHECKOUT,
})


def product_source(product: Any) -> str:
    """Return the canonical source string for *product*.

    Resolution order (first non-empty wins):

    1. ``product.source`` (top-level column, post-migration 0062).
    2. ``product.extra_metadata['source']`` — historical location used by
       the legacy Salla sync writer at ``integrations/salla/sync/products.py``.
    3. Heuristic: if ``external_id`` is set we assume the row came from a
       sync (most likely Salla — the longest-running writer); otherwise we
       can't tell and return ``"unknown"``. This is the same heuristic the
       0062 backfill uses, so old + new rows render consistently.

    Returns a lowercased string from the closed ``KNOWN_SOURCES`` set,
    or ``"unknown"`` when no signal can be derived. Tolerant of plain
    dicts and ORM rows alike. Never raises.
    """
    if product is None:
        return SOURCE_UNKNOWN

    raw = getattr(product, "source", None)
    if raw is None and isinstance(product, dict):
        raw = product.get("source")
    if raw:
        s = str(raw).strip().lower()
        if s in KNOWN_SOURCES:
            return normalize_source(s)
        # Unknown literal string — surface as "unknown" rather than the
        # opaque foreign value so the UI badge mapping stays a closed set.
        return SOURCE_UNKNOWN

    meta = getattr(product, "extra_metadata", None)
    if meta is None and isinstance(product, dict):
        meta = product.get("extra_metadata") or product.get("metadata")
    if isinstance(meta, dict):
        meta_src = meta.get("source")
        if meta_src:
            s = str(meta_src).strip().lower()
            if s in KNOWN_SOURCES:
                return normalize_source(s)

    # No explicit source — fall back to the same heuristic the migration
    # uses. A product with an external_id almost certainly came from a
    # sync (legacy: ``salla``); a row with neither could be a manual row
    # from before migration 0062 OR a half-written sync row.
    ext = getattr(product, "external_id", None)
    if ext is None and isinstance(product, dict):
        ext = product.get("external_id")
    if ext and str(ext).strip():
        return SOURCE_SALLA
    return SOURCE_UNKNOWN


def source_breakdown(products: Iterable[Any]) -> dict:
    """Aggregate ``product_source`` across an iterable into a count map.

    Used by the catalog diagnostics endpoint. The return shape is a plain
    ``{source: int}`` dict so it serialises straight to JSON. Sources with
    zero rows are omitted from the result — empty dicts mean "tenant has
    zero products at all".
    """
    counts: dict[str, int] = {}
    for p in products or []:
        s = product_source(p)
        counts[s] = counts.get(s, 0) + 1
    return counts


def dominant_source(breakdown: dict) -> str:
    """Pick the "main" source for badge rendering in the UI.

    Rules:
      * Empty input → ``"unknown"``.
      * Single non-zero entry → that source.
      * Multiple entries: the strict majority (>50%) wins; otherwise
        ``"mixed"``. The latter case lets the UI show "مصدر مختلط" so
        the merchant knows manual edits live next to sync rows.

    Returns one of: any KNOWN_SOURCES member, or the special string
    ``"mixed"`` for plural-source tenants.
    """
    if not breakdown:
        return SOURCE_UNKNOWN
    total = sum(breakdown.values())
    if total == 0:
        return SOURCE_UNKNOWN
    # Single-source short-circuit.
    nonzero = [(k, v) for k, v in breakdown.items() if v > 0]
    if len(nonzero) == 1:
        return nonzero[0][0]
    # Plural — does anyone strictly own the majority?
    top_key, top_val = max(nonzero, key=lambda kv: kv[1])
    if top_val * 2 > total:
        return top_key
    return "mixed"


# ─────────────────────────────────────────────────────────────────────────────
# Catalog eligibility
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CatalogEligibility:
    """Structured outcome of an eligibility check.

    The boolean ``ok`` is the simple "should we attempt a catalog send?"
    answer. ``reason`` is a stable, machine-grep-friendly token so log
    parsers and the admin debug audit endpoint can aggregate failures
    by cause without parsing free-form Arabic strings.

    Reason vocabulary (closed set — extend with care):

    * ``"ok"`` — catalog is eligible, proceed.
    * ``"connection_missing"`` — no WhatsAppConnection row at all.
    * ``"catalog_disabled"`` — per-connection ``catalog_enabled=False``.
    * ``"catalog_id_missing"`` — ``meta_catalog_id`` is NULL / empty.
    * ``"no_retailer_id"`` — neither override nor external_id resolved.
    * ``"empty_products"`` — caller passed zero products.
    """
    ok: bool
    reason: str


_OK = CatalogEligibility(ok=True, reason="ok")


def is_catalog_eligible(
    connection: Any,
    products: Optional[Iterable[Any]] = None,
) -> CatalogEligibility:
    """Decide whether *connection* can render *products* as a Meta
    Catalog message.

    Pass a single product, an iterable, or ``None`` (eligibility check
    without product context — used by admin audit). The check is
    short-circuit: returns the FIRST failing reason, so the cost is
    bounded even when the iterable is large.
    """
    if connection is None:
        return CatalogEligibility(ok=False, reason="connection_missing")
    if not bool(getattr(connection, "catalog_enabled", False)):
        return CatalogEligibility(ok=False, reason="catalog_disabled")
    catalog_id = (getattr(connection, "meta_catalog_id", "") or "").strip()
    if not catalog_id:
        return CatalogEligibility(ok=False, reason="catalog_id_missing")

    if products is None:
        return _OK

    # Materialise once so we can both count and iterate.
    product_list = list(products)
    if not product_list:
        return CatalogEligibility(ok=False, reason="empty_products")
    for p in product_list:
        if not is_catalog_active(p):
            return CatalogEligibility(ok=False, reason="product_not_active")
        if not effective_retailer_id(p):
            return CatalogEligibility(ok=False, reason="no_retailer_id")
    return _OK


# ─────────────────────────────────────────────────────────────────────────────
# Public introspection — used by admin/debug
# ─────────────────────────────────────────────────────────────────────────────

def catalog_summary(connection: Any) -> dict:
    """Return a JSON-safe dict describing the catalog binding state.

    Surfaced by ``GET /admin/debug/catalog-audit`` (extended in phase
    5) so merchants can verify that ``meta_catalog_id`` and
    ``catalog_enabled`` are set without exposing the access token. The
    dict shape is intentionally flat for easy display in the admin
    drawer.
    """
    if connection is None:
        return {
            "catalog_bound": False,
            "catalog_enabled": False,
            "meta_catalog_id": None,
            "reason": "connection_missing",
        }
    catalog_id = (getattr(connection, "meta_catalog_id", "") or "").strip()
    enabled = bool(getattr(connection, "catalog_enabled", False))
    return {
        "catalog_bound": bool(catalog_id),
        "catalog_enabled": enabled,
        "meta_catalog_id": catalog_id or None,
        "reason": (
            "ok" if (catalog_id and enabled)
            else "catalog_disabled" if catalog_id
            else "catalog_id_missing"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp commerce readiness — shared by dashboard diagnostics + orchestrator
# ─────────────────────────────────────────────────────────────────────────────
#
# Two distinct surfaces (approved architecture):
#
#   * **Operational / import diagnostics** — ``whatsapp_commerce_diagnostics_readiness``
#     includes Graph import token availability for the PR2 dashboard checklist.
#     NOT used as a hard gate for catalog card sends.
#
#   * **Send readiness** — ``evaluate_tenant_catalog_send_readiness`` mirrors
#     ``is_catalog_eligible`` plus connection health (status, sending_enabled,
#     phone_number_id). Used by the product-card orchestrator only.


@dataclass(frozen=True)
class TenantCatalogSendReadiness:
    """Runtime send gate for official WhatsApp catalog cards.

    Deliberately excludes Graph import token checks — coexistence / D360
    sends use ``provider_send_message``, not Meta Graph import tokens.
    """
    ready: bool
    reason: str
    checks: Dict[str, bool]


def evaluate_tenant_catalog_send_readiness(connection: Any) -> TenantCatalogSendReadiness:
    """Decide whether *connection* can attempt catalog card sends at all.

    Closed ``reason`` vocabulary (first failure wins):

    * ``ok``
    * ``connection_missing``
    * ``whatsapp_not_connected``
    * ``phone_number_id_missing``
    * ``catalog_disabled`` / ``catalog_id_missing`` — from ``is_catalog_eligible``
    """
    if connection is None:
        return TenantCatalogSendReadiness(
            ready=False,
            reason="connection_missing",
            checks={"connection_present": False},
        )

    wa_connected = (
        getattr(connection, "status", "") == "connected"
        and bool(getattr(connection, "sending_enabled", False))
    )
    phone_ok = bool((getattr(connection, "phone_number_id", None) or "").strip())

    checks: Dict[str, bool] = {
        "connection_present":   True,
        "whatsapp_connected":   wa_connected,
        "phone_number_id":      phone_ok,
    }

    if not wa_connected:
        return TenantCatalogSendReadiness(
            ready=False, reason="whatsapp_not_connected", checks=checks,
        )
    if not phone_ok:
        return TenantCatalogSendReadiness(
            ready=False, reason="phone_number_id_missing", checks=checks,
        )

    elig = is_catalog_eligible(connection, products=None)
    checks["catalog_enabled"] = elig.reason != "catalog_disabled"
    checks["meta_catalog_id"] = elig.reason != "catalog_id_missing"
    if not elig.ok:
        return TenantCatalogSendReadiness(
            ready=False, reason=elig.reason, checks=checks,
        )

    return TenantCatalogSendReadiness(ready=True, reason="ok", checks=checks)


def whatsapp_commerce_diagnostics_readiness(
    *,
    connection: Any,
    catalog_id: str,
    catalog_enabled: bool,
    wa_connected: bool,
    with_rid: int,
) -> Dict[str, Any]:
    """PR2 operational checklist for dashboard / support diagnostics.

    Includes ``graph_token_available`` (import path) — informational only,
    not a send gate. Lazy-imports ``_select_graph_token`` so callers that
    only need send readiness never pay the import cost.
    """
    phone_number_id = (
        (getattr(connection, "phone_number_id", None) or "").strip()
        if connection else ""
    )
    graph_token_ok = False
    graph_token_source = None
    if connection is not None:
        from services.meta_catalog_import import describe_graph_token_selection  # noqa: PLC0415
        pick = describe_graph_token_selection(connection)
        graph_token_ok = bool(pick.get("token_present"))
        graph_token_source = pick.get("token_source")

    checks: List[Dict[str, Any]] = [
        {"key": "whatsapp_connected", "ok": wa_connected},
        {"key": "phone_number_id", "ok": bool(phone_number_id)},
        {"key": "meta_catalog_id", "ok": bool(catalog_id)},
        {"key": "catalog_enabled", "ok": catalog_enabled},
        {
            "key": "graph_token_available",
            "ok": graph_token_ok,
            "token_source": graph_token_source,
        },
        {
            "key": "products_with_retailer_id",
            "ok": with_rid > 0,
            "count": with_rid,
        },
    ]
    missing = [c["key"] for c in checks if not c["ok"]]
    return {
        "ready":                len(missing) == 0,
        "checks":               checks,
        "missing_requirements": missing,
    }


def is_synthetic_retailer_id(retailer_id: str) -> bool:
    """True when *retailer_id* was assigned by ``canonical_retailer_id`` fallback."""
    return str(retailer_id or "").strip().startswith("nahla_p_")


__all__: List[str] = [
    "CatalogEligibility",
    "TenantCatalogSendReadiness",
    "assign_canonical_retailer_id",
    "canonical_retailer_id",
    "catalog_summary",
    "effective_retailer_id",
    "effective_variant_retailer_id",
    "evaluate_tenant_catalog_send_readiness",
    "is_catalog_eligible",
    "is_catalog_active",
    "catalog_status_of",
    "apply_active_catalog_query_filters",
    "CATALOG_STATUS_ACTIVE",
    "CATALOG_STATUS_ARCHIVED",
    "CATALOG_STATUS_MERCHANT_HIDDEN",
    "CATALOG_STATUS_REMOVED_FROM_META",
    "is_synthetic_retailer_id",
    "whatsapp_commerce_diagnostics_readiness",
]
