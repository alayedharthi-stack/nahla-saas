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
from typing import Any, Iterable, List, Optional

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


__all__: List[str] = [
    "CatalogEligibility",
    "assign_canonical_retailer_id",
    "canonical_retailer_id",
    "catalog_summary",
    "effective_retailer_id",
    "is_catalog_eligible",
]
