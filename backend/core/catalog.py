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
    "catalog_summary",
    "effective_retailer_id",
    "is_catalog_eligible",
]
