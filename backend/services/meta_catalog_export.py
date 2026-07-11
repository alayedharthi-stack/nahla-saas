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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.catalog import normalize_catalog_price_amount
from core.native_product_public_url import resolve_meta_export_product_url

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
# Per-product / per-variant payload builders — pure functions, no I/O.
# ─────────────────────────────────────────────────────────────────────────────

def _row_metadata(row: Any) -> Dict[str, Any]:
    meta = getattr(row, "extra_metadata", None) or {}
    return meta if isinstance(meta, dict) else {}


def format_meta_price(amount: Any, currency: str = "SAR") -> Optional[str]:
    """Format a Nahla price for human review (e.g. ``59.00 SAR``)."""
    numeric = meta_price_amount(amount)
    if numeric is None:
        return None
    cur = (currency or "SAR").strip().upper() or "SAR"
    return f"{numeric:.2f} {cur}"


def meta_price_amount(amount: Any) -> Optional[float]:
    """Normalize a Nahla price to a major-unit float for human display."""
    normalized = normalize_catalog_price_amount(amount)
    if normalized is None:
        return None
    try:
        return float(normalized)
    except (TypeError, ValueError):
        return None


def meta_price_minor_units(amount: Any) -> Optional[int]:
    """Normalize a Nahla price to Meta Graph minor units (e.g. 59.00 SAR -> 5900)."""
    normalized = normalize_catalog_price_amount(amount)
    if normalized is None:
        return None
    try:
        major = Decimal(normalized)
    except (TypeError, ValueError, InvalidOperation):
        return None
    minor = (major * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(minor)


def _looks_like_raw_option_ids(text: str) -> bool:
    """True when *text* is a raw id/list repr, not a human option label."""
    text = (text or "").strip()
    if not text:
        return True
    if text.isdigit():
        return True
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return True
        parts = [part.strip().strip("'\"") for part in inner.split(",") if part.strip()]
        if parts and all(part.isdigit() for part in parts):
            return True
    return False


def _human_option_summary(variant: Any) -> Optional[str]:
    summary = (getattr(variant, "option_summary", None) or "").strip()
    if not summary or _looks_like_raw_option_ids(summary):
        return None
    return summary


def _extract_variant_attribute(options: Any, *keys: str) -> Optional[str]:
    """Return the first non-empty option value for any of *keys* (case-insensitive)."""
    if not isinstance(options, dict) or not options:
        return None
    lookup = {
        str(k).lower(): v
        for k, v in options.items()
        if k and str(k).lower() != "option_value_ids"
    }
    for key in keys:
        val = lookup.get(key.lower())
        if val is None or isinstance(val, (list, tuple)):
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def build_meta_variant_display_name(parent: Any, variant: Any) -> str:
    """Human-facing catalog name — identity remains ``retailer_id``."""
    title = (getattr(parent, "title", None) or "").strip() or "Product"
    label = _human_option_summary(variant)
    if label:
        return f"{title} - {label}"[:150]
    return title[:150]


def resolve_meta_item_group_id(parent: Any, variant: Any) -> Optional[str]:
    """Return shared Meta ``item_group_id`` for real multi-SKU variants only.

    Simple default-only products omit the field. Group id is the parent
    product identifier (Salla ``external_id``, else ``meta_retailer_id``).
    """
    salla_vid = str(getattr(variant, "salla_variant_id", "") or "").strip()
    if not salla_vid:
        return None
    group = str(getattr(parent, "external_id", "") or "").strip()
    if not group:
        group = str(getattr(parent, "meta_retailer_id", "") or "").strip()
    if not group:
        return None
    variant_rid = str(getattr(variant, "retailer_id", "") or "").strip()
    if variant_rid and group == variant_rid:
        return None
    return group


def resolve_variant_image_url(parent: Any, variant: Any) -> tuple[Optional[str], str]:
    """Return (image_url, source) where source is variant|parent|none."""
    variant_image = (getattr(variant, "image_url", None) or "").strip()
    if variant_image:
        return variant_image, "variant"
    parent_image = (_row_metadata(parent).get("image_url") or "").strip()
    if parent_image:
        return parent_image, "parent"
    return None, "none"


def build_meta_variant_payload(parent: Any, variant: Any) -> Dict[str, Any]:
    """Translate a Nahla variant (+ parent context) into a Meta item payload.

    Uses ``variant.retailer_id`` — never the parent retailer id — so
    multi-variant products map one Meta catalog item per sellable SKU.
  """
    retailer_id = (getattr(variant, "retailer_id", None) or "").strip() or None
    parent_meta = _row_metadata(parent)
    currency = (
        (getattr(variant, "currency", None) or "").strip()
        or str(parent_meta.get("currency") or "SAR").strip()
        or "SAR"
    ).upper()
    image_url, _image_source = resolve_variant_image_url(parent, variant)
    product_url = resolve_meta_export_product_url(parent, variant)
    description = (getattr(parent, "description", None) or "").strip()
    if not description:
        description = (getattr(parent, "title", None) or "").strip() or None

    in_stock = bool(getattr(variant, "in_stock", True))
    stock_qty = getattr(variant, "stock_quantity", None)
    if stock_qty is not None:
        try:
            if int(stock_qty) <= 0:
                in_stock = False
        except (TypeError, ValueError):
            pass

    payload: Dict[str, Any] = {
        "retailer_id": retailer_id,
        "name": build_meta_variant_display_name(parent, variant),
        "description": description,
        "image_url": image_url,
        "url": product_url,
        "price": meta_price_minor_units(getattr(variant, "price", None)),
        "currency": currency,
        "availability": "in stock" if in_stock else "out of stock",
    }
    item_group_id = resolve_meta_item_group_id(parent, variant)
    if item_group_id:
        payload["item_group_id"] = item_group_id

    opts = getattr(variant, "options", None) or {}
    size = _extract_variant_attribute(opts, "size", "حجم", "مقاس", "المقاس")
    color = _extract_variant_attribute(opts, "color", "colour", "لون", "اللون")
    material = _extract_variant_attribute(opts, "material", "خامة", "مادة", "الخامة")
    if size:
        payload["size"] = size
    if color:
        payload["color"] = color
    if material:
        payload["material"] = material
    return payload


def preview_meta_variant_payload(parent: Any, variant: Any) -> Dict[str, Any]:
    """Build payload + debug fields + warnings for operator review."""
    payload = build_meta_variant_payload(parent, variant)
    variant_meta = _row_metadata(variant)
    image_url, image_source = resolve_variant_image_url(parent, variant)

    warnings: List[str] = []
    if not payload.get("retailer_id"):
        warnings.append("missing_retailer_id")
    if payload.get("price") is None:
        warnings.append("missing_price")
    if not payload.get("image_url"):
        warnings.append("missing_image_url")
    if not payload.get("url"):
        warnings.append("missing_url")
    if payload.get("availability") == "out of stock":
        warnings.append("out_of_stock")

    fatal_codes = {
        "missing_retailer_id",
        "missing_price",
        "missing_image_url",
        "missing_url",
    }
    has_fatal = any(code in fatal_codes for code in warnings)

    return {
        "product": {
            "id": getattr(parent, "id", None),
            "title": getattr(parent, "title", None),
            "external_id": getattr(parent, "external_id", None),
        },
        "variant": {
            "id": getattr(variant, "id", None),
            "salla_variant_id": getattr(variant, "salla_variant_id", None),
            "retailer_id": getattr(variant, "retailer_id", None),
        },
        "payload": payload,
        "debug": {
            "sale_price": variant_meta.get("sale_price"),
            "regular_price": variant_meta.get("regular_price"),
            "stock_quantity": getattr(variant, "stock_quantity", None),
            "options": getattr(variant, "options", None),
            "image_source": image_source,
            "url_present": bool(payload.get("url")),
            "image_present": bool(image_url),
        },
        "warnings": warnings,
        "fatal": has_fatal,
    }


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
    "build_meta_variant_payload",
    "build_meta_variant_display_name",
    "format_meta_price",
    "meta_price_amount",
    "meta_price_minor_units",
    "preview_meta_variant_payload",
    "resolve_meta_item_group_id",
    "resolve_variant_image_url",
    "export_to_meta",
    "preflight_check",
    "reconcile_meta_catalog",
]
