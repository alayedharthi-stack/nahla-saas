"""
services/meta_catalog_export.py
───────────────────────────────
PLANNING-ONLY skeleton for publishing Nahla Catalog products OUT to
Meta Commerce Manager. **No live network calls today.**

This file exists so:

  1. the contract is committed to the repo (reviewers and merchants
     can read it without spelunking through a PR description);
  2. unit tests / type checks can already reference the public API;
  3. the implementation PR can land as a pure
     "fill in the bodies" change without first introducing the
     module surface.

Hub architecture context
────────────────────────
This is the OUTPUT direction in the catalog Hub:

    NAHLA CATALOG  ──→  META CATALOG    (this module, future)
    NAHLA CATALOG  ──→  GOOGLE MERCHANT (future, separate module)
    NAHLA CATALOG  ──→  WHATSAPP / AI   (already shipped — read-only
                                         resolvers in routers/whatsapp
                                         and services/product_resolver)
    NAHLA CATALOG  ──→  CAMPAIGNS       (already shipped — campaign
                                         dispatcher reads catalog rows)

The INPUT direction (Meta → Nahla, Salla → Nahla, manual → Nahla) is
fully implemented; see ``services/meta_catalog_import.py``,
``backend/services/store_sync.py`` and the manual CRUD endpoints in
``routers/catalog.py``.

Why we're deferring
───────────────────
Publishing to Meta requires:

  • a Meta-side ``Commerce Account`` with ``catalog_management``
    permission on the long-lived system user token,
  • per-row Meta product creation calls
    (``POST /{catalog_id}/products``) with strict field validation
    (currency must be ISO-4217, price must be in minor units, image
    URL must be publicly reachable for Meta's crawler),
  • a reconciliation loop because Meta validates asynchronously —
    a product can be accepted at create time and rejected hours
    later for image / policy reasons.

None of that is hard, but it's a non-trivial chunk of work that
doesn't unblock the current merchant pain point ("my Salla products
aren't showing in WhatsApp"). We're shipping import-from-Meta now
and queuing publish-to-Meta as a follow-up.

The public contract below is final — the implementation PR will only
fill in the function bodies, not rename anything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.meta_catalog_export")


# ─────────────────────────────────────────────────────────────────────────────
# Error type — same closed-set pattern as the import service
# ─────────────────────────────────────────────────────────────────────────────

class MetaCatalogExportError(RuntimeError):
    """Raised by ``export_to_meta`` on hard preflight failures.

    Planned codes (closed set, mapped to HTTP in the router):

    * ``connection_not_found``   → 404
    * ``catalog_id_missing``     → 400
    * ``access_token_missing``   → 400
    * ``permission_missing``     → 403  (token lacks
                                          ``catalog_management``)
    * ``meta_http_error``        → 502
    * ``rate_limited``           → 429  (Meta returned X-Business-…
                                          Usage > 95%)
    """
    def __init__(self, code: str, message: str = "", *, detail: Any = None):
        super().__init__(message or code)
        self.code: str = code
        self.detail: Any = detail


@dataclass
class ExportReport:
    """Outcome of an export run.

    ``pending_async`` is intentionally separate from ``created`` —
    Meta returns 200 on submission but validates asynchronously, so
    successful submission ≠ a live product. A separate reconciliation
    pass (planned: ``reconcile_meta_catalog()``) will move rows from
    ``pending_async`` to ``confirmed`` after Meta finishes validating.
    """
    scanned:         int = 0
    submitted:       int = 0   # 200 from Meta /products
    pending_async:   int = 0   # accepted but not yet validated
    confirmed:       int = 0   # already validated on a prior run
    skipped_meta:    int = 0   # source=meta — already lives in Meta
    errors:          int = 0
    error_samples:   List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanned":        self.scanned,
            "submitted":      self.submitted,
            "pending_async":  self.pending_async,
            "confirmed":      self.confirmed,
            "skipped_meta":   self.skipped_meta,
            "errors":         self.errors,
            "error_samples":  self.error_samples[:10],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Public surface — bodies are stubs that raise NotImplementedError
# ─────────────────────────────────────────────────────────────────────────────

def export_to_meta(
    db: Session,
    tenant_id: int,
    *,
    only_unpublished: bool = True,
    dry_run: bool = False,
) -> ExportReport:
    """Push Nahla Catalog products to the merchant's Meta Catalog.

    Parameters
    ----------
    only_unpublished:
        When True (the default), only rows where
        ``meta_catalog_published_at IS NULL`` are submitted. Set
        False to force re-submission of every row — useful after a
        catalog-level Meta change (currency switch, etc.).
    dry_run:
        Build the per-product payloads and run all preflight
        validation, but do NOT call Meta. Lets the merchant preview
        "what would be sent" before committing.

    Returns
    -------
    :class:`ExportReport`

    Raises
    ------
    :class:`MetaCatalogExportError` on preflight failure. Per-row
    errors are collected in the report, never raised.

    Skips
    -----
    * Rows where ``source == "meta"`` (already live in Meta — re-
      submitting would create a duplicate retailer_id collision).
    * Rows with missing image_url (Meta hard-requires an image).
    """
    raise NotImplementedError(
        "export_to_meta is planning-only — see module docstring. "
        "Implementation tracked in May 2026 #14 follow-up.",
    )


def reconcile_meta_catalog(db: Session, tenant_id: int) -> Dict[str, int]:
    """Asynchronous reconciliation pass.

    After ``export_to_meta`` submits products, Meta validates them in
    the background and exposes per-product status via
    ``GET /{retailer_id}?fields=review_status,errors``. This helper:

    1. queries Meta for every Nahla product with
       ``source != "meta"`` and ``meta_catalog_published_at IS NULL``;
    2. flips ``meta_catalog_published_at`` to NOW() for accepted
       rows;
    3. records rejection reasons in ``extra_metadata.meta_export_errors``.

    Designed to run on the same scheduler as the campaign-wave loop —
    every 5 minutes at most.

    Returns a ``{confirmed, rejected, still_pending}`` summary so the
    dashboard can render a progress meter.
    """
    raise NotImplementedError(
        "reconcile_meta_catalog is planning-only — see module docstring.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-product payload builder — pure function, easy to unit-test
# even while the network path is unimplemented.
# ─────────────────────────────────────────────────────────────────────────────

def build_meta_product_payload(product: Any) -> Dict[str, Any]:
    """Translate a Nahla ``Product`` row into a Meta-side payload.

    Pure function — no I/O. Suitable to unit-test today, even though
    the surrounding ``export_to_meta`` is stubbed.

    Field mapping (Nahla → Meta):

    * ``product.title``             → ``name``
    * ``product.description``       → ``description``
    * ``effective_retailer_id(p)``  → ``retailer_id``
    * ``product.extra_metadata.image_url``  → ``image_url``
    * ``product.extra_metadata.product_url`` → ``url``
    * ``product.price``             → ``price`` (formatted as
                                       ``"<value> <currency>"`` per
                                       Meta's spec; currency falls
                                       back to ``WhatsAppConnection``
                                       default which is wired by the
                                       caller).
    * ``in_stock``                  → ``availability`` (``"in stock"`` /
                                       ``"out of stock"``).
    """
    from core.catalog import effective_retailer_id  # noqa: PLC0415

    meta = getattr(product, "extra_metadata", None) or {}
    return {
        "retailer_id":   effective_retailer_id(product),
        "name":          getattr(product, "title", None),
        "description":   getattr(product, "description", None),
        "image_url":     meta.get("image_url"),
        "url":           meta.get("product_url") or meta.get("url"),
        "price":         getattr(product, "price", None),
        "availability":  "in stock" if getattr(product, "in_stock", True) else "out of stock",
    }


def preflight_check(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Diagnose readiness for export without performing one.

    Returns a structured payload the dashboard can render as a
    "What's missing before I can publish to Meta?" checklist:

    * ``connection_present``     bool
    * ``catalog_id_present``     bool
    * ``token_present``          bool
    * ``permission_catalog_mgmt`` bool (best-effort — we infer from
      a previous "introspect token" call cache, NULL until checked)
    * ``products_total``         int
    * ``products_missing_image`` int
    * ``products_missing_price`` int
    * ``ready_to_export``        bool

    Implementation can land independently of the actual export — it
    only reads existing tables. Stub today.
    """
    raise NotImplementedError(
        "preflight_check is planning-only — see module docstring.",
    )


__all__ = [
    "ExportReport",
    "MetaCatalogExportError",
    "build_meta_product_payload",
    "export_to_meta",
    "preflight_check",
    "reconcile_meta_catalog",
]
