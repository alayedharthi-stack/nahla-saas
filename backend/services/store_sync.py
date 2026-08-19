"""
services/store_sync.py
â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Store Knowledge Sync Service.

Responsibilities
  â€¢ Initial full sync â€” fetch everything from the store adapter after connection
  â€¢ Incremental sync  â€” called by platform webhooks for individual entity updates
  â€¢ Snapshot update   â€” maintain StoreKnowledgeSnapshot so AI always has fresh data
  â€¢ Job tracking      â€” write StoreSyncJob rows so dashboard can show progress

Usage
  svc = StoreSyncService(db, tenant_id)
  await svc.full_sync()                    # full historical sync (first time)
  await svc.full_sync(incremental=True)    # incremental sync (subsequent times)
  await svc.sync_products()                # triggered by product webhook
  status = svc.get_status()
"""
from __future__ import annotations

import logging
import os
import re as _re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# A job stuck in "running" for longer than this is considered timed out
_SYNC_JOB_TIMEOUT_MINUTES = 10

_SCRIPT_BLOCK_RE = _re.compile(r"<(script|style)[^>]*>.*?<\/\1>", _re.DOTALL | _re.IGNORECASE)
_HTML_TAG_RE     = _re.compile(r"<[^>]+>", _re.DOTALL)
_WHITESPACE_RE   = _re.compile(r"\s+")


def _strip_html(html: str, max_length: Optional[int] = 500) -> str:
    """Strip HTML tags and collapse whitespace into a plain-text summary.

    Designed for Salla CMS page content:
      1. Removes entire <script> and <style> blocks (tags + inner code).
      2. Strips remaining HTML tags.
      3. Collapses whitespace so the AI sees clean prose.
    When ``max_length`` is None or <= 0, the full normalized body is kept
    (Pack A1 long-form MerchantKnowledgeSection path).
    """
    if not html:
        return ""
    text = _SCRIPT_BLOCK_RE.sub(" ", html)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if max_length is None or max_length <= 0:
        return text
    return text[:max_length]

from sqlalchemy import func
from sqlalchemy.orm import Session

# Allow importing from project root
_THIS = os.path.dirname(os.path.abspath(__file__))
_DB   = os.path.abspath(os.path.join(_THIS, "../../database"))
for _p in (_THIS, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import (  # noqa: E402
    Coupon,
    Customer,
    CustomerProfile,
    Order,
    Product,
    ProductInterest,
    StoreSyncJob,
    StoreKnowledgeSnapshot,
    TenantSettings,
)
from services.customer_intelligence import (  # noqa: E402
    CustomerIntelligenceService,
    extract_order_datetime as intelligence_extract_order_datetime,
    normalize_phone as intelligence_normalize_phone,
)
from utils.phone_utils import normalize_to_e164 as _normalize_to_e164  # noqa: E402
from core.catalog_image import (  # noqa: E402
    coerce_image_url,
    extract_sync_additional_images,
    extract_sync_product_image,
)

logger = logging.getLogger("nahla-backend")


# â”€â”€ Data normalisation helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

import re as _re

def _normalize_phone(raw_phone) -> str:
    """Returns E.164 or '' â€” backward-compat wrapper."""
    return intelligence_normalize_phone(raw_phone)


def _e164(raw_phone) -> Optional[str]:
    """Returns E.164 or None â€” used for normalized_phone column."""
    return _normalize_to_e164(str(raw_phone or "").strip())


def _extract_order_datetime(raw: Any) -> Optional[datetime]:
    return intelligence_extract_order_datetime(raw)


def _normalise_product(raw: Any) -> Dict:
    """Convert a store-adapter product object/dict to a normalised internal dict."""
    if hasattr(raw, "dict"):
        raw = raw.dict()
    image_url = extract_sync_product_image(raw)
    additional_images = extract_sync_additional_images(raw, primary=image_url)
    variants_raw = raw.get("variants") or []
    variants_out: List[Dict[str, Any]] = []
    for v in variants_raw:
        v_dict = _coerce_variant_dict(v)
        v_img = coerce_image_url(v_dict.get("image_url"))
        if v_img:
            v_dict["image_url"] = v_img
        elif "image_url" in v_dict and not v_dict.get("image_url"):
            v_dict.pop("image_url", None)
        for price_key in ("price", "sale_price", "regular_price"):
            if v_dict.get(price_key) is not None:
                v_dict[price_key] = str(v_dict[price_key])
        variants_out.append(v_dict)
    return {
        "external_id":   str(raw.get("id", raw.get("external_id", ""))),
        "sku":           raw.get("sku", ""),
        "title":         raw.get("title", raw.get("name", "")),
        "description":   raw.get("description", ""),
        "price":         str(raw.get("price", raw.get("regular_price", ""))),
        "sale_price":    str(raw.get("sale_price", raw.get("promo_price", "")) or ""),
        "regular_price": str(raw.get("regular_price", "") or ""),
        "status":        _extract_status_string(raw.get("status"), fallback="active"),
        "category":      raw.get("category", raw.get("main_category", "")),
        "brand":         raw.get("brand", ""),
        "image_url":     image_url,
        "additional_images": additional_images,
        "product_url":   (raw.get("product_url") or raw.get("url") or "").strip(),
        "currency":      raw.get("currency", "SAR"),
        "in_stock":      raw.get("in_stock", True),
        "stock_qty":     raw.get("quantity", raw.get("stock_quantity", None)),
        "tags":          raw.get("tags", []),
        "variants":      variants_out,
        "options":       raw.get("options", []),
        "has_required_options": bool(raw.get("has_required_options", False)),
        "metadata":      raw.get("metadata", {}),
    }


def _preserve_existing_product_images(
    normalised: Dict[str, Any],
    existing_meta: Any,
) -> None:
    """In-place: keep stored images when a sync payload omits them."""
    if not isinstance(existing_meta, dict):
        return
    if not coerce_image_url(normalised.get("image_url")):
        previous_url = coerce_image_url(existing_meta.get("image_url"))
        if previous_url:
            normalised["image_url"] = previous_url
    if not normalised.get("additional_images"):
        previous_extra = existing_meta.get("additional_images")
        if isinstance(previous_extra, (list, tuple)) and previous_extra:
            normalised["additional_images"] = list(previous_extra)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Parent / variant catalog layer (migration 0064 â€” Phase 2)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#
# Every parent ``Product`` row must end up with at least one sellable
# ``ProductVariant`` row after a sync. There are three cases we handle:
#
#   1. Salla returned a non-empty ``variants`` array â€” one variant row
#      per element. Match by ``salla_variant_id`` (== the platform
#      variant id) so a re-sync updates in place instead of duplicating.
#
#   2. Salla returned an empty array â€” we create exactly one
#      ``is_default=True`` synthetic variant mirroring the parent. This
#      keeps the downstream contract simple: senders / brain / Google
#      feed always go through ``product_variants``, never through
#      "the parent itself".
#
#   3. A variant that used to be there disappears from Salla â€” we
#      SOFT-DELETE (``in_stock=False``) rather than ``db.delete`` so:
#         * order_items.variant_id history stays referentially clean
#           once that FK lands later, and
#         * a merchant who pauses a variant on Salla can resume it on
#           a future sync without losing analytics / affinity.
#
# This helper is invoked from ``sync_products`` AFTER the parent row
# has been flushed (so ``product.id`` is populated for the FK). Soft-
# disabled via the ``CATALOG_VARIANT_SYNC`` env flag â€” flip to "false"
# to roll back variant writes without touching the parent path.


_CATALOG_VARIANT_SYNC_DISABLED_VALUES = {"false", "0", "off", "no", ""}


def _variant_sync_enabled() -> bool:
    raw = os.getenv("CATALOG_VARIANT_SYNC", "true")
    return (raw or "").strip().lower() not in _CATALOG_VARIANT_SYNC_DISABLED_VALUES


def _human_option_label(text: Any) -> Optional[str]:
    """Reject raw option ids / list reprs; keep human labels only."""
    if text is None:
        return None
    label = str(text).strip()
    if not label:
        return None
    if label.isdigit():
        return None
    if label.startswith("[") and label.endswith("]"):
        inner = label[1:-1].strip()
        if not inner:
            return None
        parts = [
            part.strip().strip("'\"")
            for part in inner.split(",")
            if part.strip()
        ]
        if parts and all(part.isdigit() for part in parts):
            return None
    return label


def _variant_option_summary(variant_dict: Dict[str, Any]) -> str:
    """Build a short single-line summary from a variant's options/title.

    Falls through priorities so we always get *something* readable:
      1. Explicit ``option_summary`` set by the adapter.
      2. Pretty-print of human ``options`` dict values (e.g. ``"M / Red"``).
      3. ``title`` / ``name`` (Salla often labels variants by joining option names).
      4. ``sku`` as a last resort.
    """
    summary = _human_option_label(variant_dict.get("option_summary"))
    if summary:
        return summary[:255]
    options = variant_dict.get("options")
    if isinstance(options, dict) and options:
        human_values: List[str] = []
        for key, value in options.items():
            if key == "option_value_ids":
                continue
            if isinstance(value, (list, tuple)):
                continue
            label = _human_option_label(value)
            if label:
                human_values.append(label)
        if human_values:
            return " / ".join(human_values)[:255]
    for key in ("title", "name"):
        label = _human_option_label(variant_dict.get(key))
        if label:
            return label[:255]
    sku = variant_dict.get("sku")
    if sku:
        return str(sku).strip()[:255]
    return ""


def _coerce_variant_dict(raw: Any) -> Dict[str, Any]:
    """Normalise an inbound variant entry (Pydantic / dict / object)
    into a plain dict the upsert can read fields off."""
    if raw is None:
        return {}
    if hasattr(raw, "model_dump"):
        try:
            return dict(raw.model_dump())
        except Exception:  # noqa: BLE001
            pass
    if hasattr(raw, "dict"):
        try:
            return dict(raw.dict())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(raw, dict):
        return dict(raw)
    # Bare object â€” best-effort attribute scrape.
    return {
        k: getattr(raw, k, None)
        for k in ("id", "title", "price", "sale_price", "regular_price",
                  "sku", "in_stock", "stock_quantity", "options",
                  "option_summary", "image_url", "currency")
        if hasattr(raw, k)
    }


def _resolve_variant_retailer_id(parent: Any, variant_id: Optional[int],
                                 salla_variant_id: Optional[str]) -> str:
    """Pick a per-variant retailer_id.

    Order:
      1. The merchant's parent-level override (``meta_retailer_id``)
         when it carries a hyphenated shape like ``parent-variant``;
         we split it so per-variant ids round-trip cleanly.
      2. ``{parent.external_id}-{salla_variant_id}`` when both exist â€”
         the convention Salla's Meta Commerce auto-publish uses.
      3. ``nahla_v_{variant_id}`` synthetic fallback so a send never
         goes out without a retailer_id.
    """
    salla_id = (salla_variant_id or "").strip()
    parent_ext = (getattr(parent, "external_id", "") or "").strip()
    parent_override = (getattr(parent, "meta_retailer_id", "") or "").strip()
    if parent_override and "-" in parent_override and salla_id:
        # Merchant put a ``parent-variant`` shape on the parent â€”
        # respect it by swapping in the per-variant suffix.
        head, _, _tail = parent_override.partition("-")
        if head:
            return f"{head}-{salla_id}"
    if parent_ext and salla_id:
        return f"{parent_ext}-{salla_id}"
    if salla_id:
        return salla_id
    if variant_id is not None:
        return f"nahla_v_{variant_id}"
    return parent_override or parent_ext or ""


def _upsert_variants_for(db: Session, product: Any,
                         normalised: Dict[str, Any]) -> None:
    """Reconcile ``product_variants`` rows with the adapter's payload.

    Behaviour:

      * Variants present in ``normalised['variants']`` are upserted
        keyed by ``(product_id, salla_variant_id)``.
      * Variants previously persisted but not in this run are SOFT-
        PRUNED â€” ``in_stock=False`` rather than deleted.
      * If the adapter returned an empty array we ensure exactly one
        ``is_default=True`` synthetic row exists.
      * Parent flags ``has_variants`` / ``default_variant_id`` are
        re-stamped at the end.

    No-op when the env flag is off. Tolerant of partial / malformed
    variant payloads â€” we log and skip individual rows rather than
    abort the whole sync.
    """
    if not _variant_sync_enabled():
        return

    # Lazy import to avoid a cycle with models on first run.
    try:
        from models import ProductVariant  # noqa: PLC0415
    except ImportError:  # noqa: BLE001
        from database.models import ProductVariant  # type: ignore  # noqa: PLC0415

    raw_variants = normalised.get("variants") or []
    parent_currency = normalised.get("currency") or "SAR"
    parent_price = normalised.get("price") or ""
    parent_image = coerce_image_url(normalised.get("image_url")) or ""

    # Index existing rows for O(1) lookup. ``salla_variant_id`` may be
    # NULL (synthetic default rows) â€” we match those by ``is_default``.
    existing_rows = (
        db.query(ProductVariant)
          .filter(ProductVariant.product_id == product.id)
          .all()
    )
    by_salla_id: Dict[str, Any] = {}
    default_row = None
    for row in existing_rows:
        if row.salla_variant_id:
            by_salla_id[row.salla_variant_id] = row
        if row.is_default:
            default_row = row

    seen_salla_ids: set = set()

    if raw_variants:
        # â”€â”€ Case 1: real variants from the adapter â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        for raw_v in raw_variants:
            v_dict = _coerce_variant_dict(raw_v)
            sid = str(v_dict.get("id") or "").strip() or None
            if not sid:
                # No platform id â€” we can't upsert reliably. Skip.
                logger.warning(
                    "[catalog/variants] tenant=%s product=%s skipping "
                    "variant without id: %r",
                    product.tenant_id, product.id, v_dict,
                )
                continue
            seen_salla_ids.add(sid)
            price = v_dict.get("price")
            if price in (None, ""):
                price = parent_price
            else:
                price = str(price)
            stock_qty = _coerce_int(v_dict.get("stock_quantity"))
            in_stock = bool(v_dict.get("in_stock", True))
            if stock_qty is not None and stock_qty <= 0:
                in_stock = False
            options = v_dict.get("options")
            if not isinstance(options, dict):
                options = None
            image_url = coerce_image_url(v_dict.get("image_url")) or None
            meta = dict(v_dict)
            for price_key in ("sale_price", "regular_price"):
                val = v_dict.get(price_key)
                if val not in (None, ""):
                    meta[price_key] = str(val)
            row = by_salla_id.get(sid)
            if row is None:
                row = ProductVariant(
                    tenant_id=product.tenant_id,
                    product_id=product.id,
                    salla_variant_id=sid,
                    sku=v_dict.get("sku") or None,
                    price=price or None,
                    currency=v_dict.get("currency") or parent_currency,
                    stock_quantity=stock_qty,
                    in_stock=in_stock,
                    options=options,
                    option_summary=_variant_option_summary(v_dict) or None,
                    image_url=image_url,
                    is_default=False,
                    extra_metadata=meta,
                )
                db.add(row)
                db.flush()  # populate row.id for retailer_id synthesis
            else:
                row.sku = v_dict.get("sku") or row.sku
                row.price = price or row.price
                row.currency = (v_dict.get("currency")
                                or row.currency or parent_currency)
                row.stock_quantity = stock_qty
                row.in_stock = in_stock
                if options is not None:
                    row.options = options
                summary = _variant_option_summary(v_dict)
                if summary:
                    row.option_summary = summary
                if image_url:
                    row.image_url = image_url
                row.extra_metadata = meta
            # Stamp retailer_id only when missing (never overwrite an
            # explicit publish that came in via the dashboard / admin).
            if not row.retailer_id:
                row.retailer_id = _resolve_variant_retailer_id(
                    product, row.id, sid,
                )
        # Soft-prune the ones that vanished from this payload.
        for sid, row in by_salla_id.items():
            if sid not in seen_salla_ids:
                row.in_stock = False
        # If a synthetic default existed earlier (e.g. the product
        # used to be option-less and now sprouted variants) flag it
        # as out of stock so it doesn't pollute sends â€” but DON'T
        # delete it; orders may still reference it.
        if default_row is not None:
            default_row.in_stock = False
    else:
        # â”€â”€ Case 2: no variants â†’ ensure one synthetic default exists.
        if default_row is None and not existing_rows:
            new_default_rid = _resolve_variant_retailer_id(
                product, None, None,
            )
            if not new_default_rid:
                # Fall back to the canonical chain â€” this never returns
                # empty for a flushed parent (synthesises nahla_p_<id>).
                try:
                    from core.catalog import canonical_retailer_id  # noqa: PLC0415
                    new_default_rid = canonical_retailer_id(product) or ""
                except Exception:  # noqa: BLE001
                    new_default_rid = ""
            db.add(ProductVariant(
                tenant_id=product.tenant_id,
                product_id=product.id,
                salla_variant_id=None,
                sku=getattr(product, "sku", None),
                retailer_id=new_default_rid or None,
                price=parent_price or None,
                currency=parent_currency,
                stock_quantity=_coerce_int(normalised.get("stock_qty")),
                in_stock=bool(normalised.get("in_stock", True)),
                options=None,
                option_summary=None,
                image_url=parent_image or None,
                is_default=True,
            ))
            db.flush()
        elif default_row is not None:
            # Keep the synthetic default in sync with the parent.
            default_row.price = parent_price or default_row.price
            default_row.in_stock = bool(normalised.get("in_stock", True))
            default_row.stock_quantity = _coerce_int(normalised.get("stock_qty"))
            default_row.image_url = parent_image or default_row.image_url

    # Re-stamp parent flags from the current variant set.
    fresh_rows = (
        db.query(ProductVariant)
          .filter(ProductVariant.product_id == product.id)
          .all()
    )
    non_default = [r for r in fresh_rows if not r.is_default]
    in_stock_rows = [r for r in fresh_rows if r.in_stock]
    product.has_variants = bool(
        len(in_stock_rows) > 1 or bool(non_default)
        or normalised.get("has_required_options")
    )
    if not product.default_variant_id and fresh_rows:
        # Prefer an in-stock variant; prefer is_default among them;
        # fall back to lowest id for determinism.
        candidates = in_stock_rows or fresh_rows
        candidates_sorted = sorted(
            candidates,
            key=lambda r: (not r.is_default, r.id),
        )
        product.default_variant_id = candidates_sorted[0].id


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort conversion of stock_qty (str/int/None) to a real int."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _sale_meta_from_extra(meta: Any) -> Dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    return {
        "price": meta.get("price"),
        "sale_price": meta.get("sale_price"),
        "regular_price": meta.get("regular_price"),
    }


def _variant_rows_have_real_variants(variant_rows: List[Dict[str, Any]]) -> bool:
    return any(not row.get("is_default") for row in variant_rows)


def _product_db_summary(
    db: Session,
    tenant_id: int,
    *,
    external_id: Optional[str] = None,
    product_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Read-only snapshot of a Nahla product row (+ variants) for sync reports."""
    try:
        from models import ProductVariant  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        from database.models import ProductVariant  # type: ignore  # noqa: PLC0415

    query = db.query(Product).filter(Product.tenant_id == tenant_id)
    if product_id is not None:
        query = query.filter(Product.id == product_id)
    elif external_id:
        query = query.filter(Product.external_id == external_id)
    else:
        return None

    product = query.first()
    if not product:
        return None

    variants = (
        db.query(ProductVariant)
        .filter(ProductVariant.product_id == product.id)
        .all()
    )
    variant_rows = [
        {
            "id": variant.id,
            "salla_variant_id": variant.salla_variant_id,
            "is_default": variant.is_default,
            "price": str(variant.price) if variant.price is not None else None,
            "in_stock": variant.in_stock,
            "stock_quantity": variant.stock_quantity,
        }
        for variant in variants
    ]
    return {
        "nahla_product_id": product.id,
        "external_id": product.external_id,
        "title": product.title,
        "price": str(product.price) if product.price is not None else None,
        "in_stock": product.in_stock,
        "stock_quantity": product.stock_quantity,
        "has_variants": product.has_variants,
        "extra_metadata": _sale_meta_from_extra(product.extra_metadata),
        "variants_count": len(variant_rows),
        "variants": variant_rows,
        "db_had_real_variants": _variant_rows_have_real_variants(variant_rows),
    }


def _salla_live_summary(normalised: Dict[str, Any]) -> Dict[str, Any]:
    variants = normalised.get("variants") or []
    variant_rows = [
        {
            "salla_variant_id": row.get("salla_variant_id") or row.get("id"),
            "is_default": row.get("is_default"),
            "price": row.get("price"),
            "sale_price": row.get("sale_price"),
            "regular_price": row.get("regular_price"),
            "in_stock": row.get("in_stock"),
            "stock_quantity": _coerce_int(row.get("stock_quantity") or row.get("stock_qty")),
            "option_summary": row.get("option_summary"),
        }
        for row in variants
        if isinstance(row, dict)
    ]
    return {
        "external_id": normalised.get("external_id"),
        "title": normalised.get("title"),
        "price": normalised.get("price"),
        "in_stock": normalised.get("in_stock"),
        "stock_quantity": _coerce_int(normalised.get("stock_qty")),
        "extra_metadata": _sale_meta_from_extra(normalised),
        "variants_count": len(variant_rows),
        "variants": variant_rows,
        "salla_has_real_variants": _variant_rows_have_real_variants(variant_rows),
    }


def _compute_one_product_sync_diff(
    db_before: Optional[Dict[str, Any]],
    normalised: Dict[str, Any],
) -> Dict[str, Any]:
    live_in_stock = bool(normalised.get("in_stock", True))
    live_qty = _coerce_int(normalised.get("stock_qty"))
    live_meta = _sale_meta_from_extra(normalised)
    variants = normalised.get("variants") or []
    variant_rows = [row for row in variants if isinstance(row, dict)]
    salla_has_real_variants = _variant_rows_have_real_variants(variant_rows)

    if not db_before:
        return {
            "in_stock_changed": True,
            "stock_quantity_changed": live_qty is not None,
            "price_changed": bool(normalised.get("price")),
            "sale_meta_changed": bool(
                live_meta.get("sale_price") or live_meta.get("regular_price")
            ),
            "variants_count_changed": len(variant_rows) > 0,
            "db_had_real_variants": False,
            "salla_has_real_variants": salla_has_real_variants,
        }

    db_meta = db_before.get("extra_metadata") or {}
    return {
        "in_stock_changed": db_before.get("in_stock") != live_in_stock,
        "stock_quantity_changed": db_before.get("stock_quantity") != live_qty,
        "price_changed": str(db_before.get("price")) != str(normalised.get("price")),
        "sale_meta_changed": (
            str(db_meta.get("sale_price")) != str(live_meta.get("sale_price"))
            or str(db_meta.get("regular_price")) != str(live_meta.get("regular_price"))
        ),
        "variants_count_changed": db_before.get("variants_count", 0) != len(variant_rows),
        "db_had_real_variants": bool(db_before.get("db_had_real_variants")),
        "salla_has_real_variants": salla_has_real_variants,
    }


def _extract_status_string(status: Any, fallback: str = "unknown") -> str:
    """
    Salla (and some other platforms) return order/product status as either:
      â€¢ a plain string  e.g. "under_review"
      â€¢ a dict          e.g. {"id": 566146469, "name": "ط¨ط¥ظ†طھط¸ط§ط± ط§ظ„ظ…ط±ط§ط¬ط¹ط©",
                               "slug": "under_review", "customized": {...}}

    The DB column is VARCHAR â€” always return a plain string.
    Priority: slug â†’ name â†’ str(fallback)
    """
    if isinstance(status, dict):
        return str(status.get("slug") or status.get("name") or fallback)
    if status is None:
        return fallback
    s = str(status).strip()
    return s if s else fallback


def _extract_amount_string(value: Any) -> str:
    """
    Salla sometimes sends monetary fields as:
      â€¢ a plain number/string  â†’ return as-is
      â€¢ a dict {"amount": 100, "currency": "SAR"} â†’ extract amount
      â€¢ an ``amounts`` container â†’ extract grand total

    Always returns a string safe for the VARCHAR `total` column.
    """
    from core.salla_order_fidelity import (  # noqa: PLC0415
        extract_salla_grand_total,
        extract_salla_money_amount,
    )

    if isinstance(value, dict) and (
        "total" in value or "sub_total" in value or "tax" in value or "shipping" in value
    ):
        nested = extract_salla_grand_total({"amounts": value})
        return nested or ""
    amt = extract_salla_money_amount(value)
    return amt if amt is not None else (str(value) if value is not None else "")


def _normalise_order(raw: Any) -> Dict:
    if hasattr(raw, "dict"):
        raw = raw.dict()
    customer_info = raw.get("customer") or raw.get("customer_info") or {}
    if not customer_info:
        customer_name = raw.get("customer_name", "")
        customer_phone = raw.get("customer_phone", "")
        if customer_name or customer_phone:
            customer_info = {
                "name": customer_name,
                "mobile": _normalize_phone(customer_phone),
                "phone": _normalize_phone(customer_phone),
            }
    else:
        customer_info = dict(customer_info)
        normalized_phone = _normalize_phone(customer_info.get("mobile", customer_info.get("phone", "")))
        if normalized_phone:
            customer_info["mobile"] = normalized_phone
            customer_info["phone"] = normalized_phone
    order_dt = _extract_order_datetime(raw)
    from core.salla_order_fidelity import (  # noqa: PLC0415
        apply_salla_order_normalisation,
        extract_salla_grand_total,
        looks_like_salla_order,
    )

    if looks_like_salla_order(raw):
        raw_total = extract_salla_grand_total(raw)
    else:
        raw_total = raw.get("total") or raw.get("sub_total") or raw.get("amounts", {})

    external_id = str(raw.get("id", raw.get("external_id", ""))).strip()
    # Human-visible order number â€” prefer the platform's explicit
    # reference_id (Salla), fall back to a few common synonyms (Zid uses
    # `code`, Shopify uses `name`/`order_number`), and finally to the
    # external_id so the column is never blank.
    external_order_number = str(
        raw.get("reference_id")
        or raw.get("order_number")
        or raw.get("number")
        or raw.get("code")
        or raw.get("name")
        or external_id
    ).strip() or external_id

    _ci = customer_info if isinstance(customer_info, dict) else {}
    customer_name = (
        _ci.get("name")
        or " ".join(filter(None, [_ci.get("first_name"), _ci.get("last_name")])).strip()
        or raw.get("customer_name")
        or ""
    )
    customer_name = str(customer_name).strip()
    # Backfill "name" into customer_info so future ci.get("name") lookups work.
    if customer_name and isinstance(customer_info, dict) and not customer_info.get("name"):
        customer_info["name"] = customer_name

    # Extract payment method so COD orders can be detected later.
    # Salla sends it under payment.method (webhook) or payment_method (some endpoints).
    _payment_block = raw.get("payment") or {}
    payment_method = str(
        (_payment_block.get("method") if isinstance(_payment_block, dict) else None)
        or raw.get("payment_method")
        or ""
    ).strip().lower()

    result = {
        "external_id":           external_id,
        "external_order_number": external_order_number,
        "status":                _extract_status_string(raw.get("status"), fallback="unknown"),
        "total":                 _extract_amount_string(raw_total),
        "customer_name":         customer_name,
        "customer_info":         customer_info,
        "line_items":            raw.get("items", raw.get("line_items", [])),
        "checkout_url":          raw.get("checkout_url", ""),
        "is_abandoned":          raw.get("is_abandoned", raw.get("abandoned", False)),
        "source":                str(raw.get("source") or "").strip().lower() or None,
        "created_at":            order_dt.isoformat() if order_dt else raw.get("created_at"),
        "payment_method":        payment_method,
    }
    return apply_salla_order_normalisation(raw, result)


def _record_external_lifecycle_shadow_best_effort(
    sync: "StoreSyncService",
    *,
    order: Any,
    provider: str,
    raw_previous_status: Optional[str],
    raw_current_status: str,
    normalized_order: Dict[str, Any],
    raw_payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Isolated best-effort PR 2C shadow write â€” never fails order ingestion."""
    try:
        from core.commerce_lifecycle.external_shadow_producer import (  # noqa: PLC0415
            record_external_order_transition_shadow,
        )

        record_external_order_transition_shadow(
            sync.db,
            tenant_id=sync.tenant_id,
            order=order,
            provider=provider,
            raw_previous_status=raw_previous_status,
            raw_current_status=raw_current_status,
            normalized_order=normalized_order,
            raw_payload=raw_payload,
        )
    except Exception:
        logger.exception(
            "[StoreSync] external lifecycle shadow failed tenant=%s order=%s",
            sync.tenant_id,
            getattr(order, "id", None),
        )


async def _handle_external_lifecycle_transition_best_effort(
    sync: "StoreSyncService",
    *,
    order: Any,
    provider: str,
    raw_previous_status: Optional[str],
    raw_current_status: str,
    normalized_order: Dict[str, Any],
    raw_payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Dispatch lifecycle templates when enabled; otherwise shadow-only audit."""
    try:
        from core.commerce_lifecycle.dispatch import (  # noqa: PLC0415
            commerce_lifecycle_dispatch_enabled,
            dispatch_external_lifecycle_notification,
        )

        if commerce_lifecycle_dispatch_enabled():
            await dispatch_external_lifecycle_notification(
                sync.db,
                tenant_id=sync.tenant_id,
                order=order,
                provider=provider,
                raw_previous_status=raw_previous_status,
                raw_current_status=raw_current_status,
                normalized_order=normalized_order,
                raw_payload=raw_payload,
            )
            return
    except Exception:
        logger.exception(
            "[StoreSync] external lifecycle dispatch failed tenant=%s order=%s",
            sync.tenant_id,
            getattr(order, "id", None),
        )

    _record_external_lifecycle_shadow_best_effort(
        sync,
        order=order,
        provider=provider,
        raw_previous_status=raw_previous_status,
        raw_current_status=raw_current_status,
        normalized_order=normalized_order,
        raw_payload=raw_payload,
    )


def _merge_order_extra_metadata(
    existing: Optional[Dict[str, Any]],
    normalised: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge Salla fidelity metadata without clobbering merchant-only fields."""
    merged = dict(existing or {})
    for key in ("created_at", "payment_method"):
        val = normalised.get(key)
        if val:
            merged[key] = val
    salla_meta = normalised.get("salla_metadata") or {}
    for key in (
        "created_at",
        "salla_created_at",
        "salla_local_at",
        "salla_date",
        "salla_timezone",
        "salla_amounts",
        "payment_method",
        "shipping_method",
        "tracking_number",
    ):
        if salla_meta.get(key) is not None:
            merged[key] = salla_meta[key]
    return merged


def _flatten_salla_datetime(value: Any) -> str:
    """
    Salla's abandoned-cart payload returns timestamps as a nested object:

        {"date": "2026-04-19 10:00:00.000000", "timezone_type": 3,
         "timezone": "Asia/Riyadh"}

    Older endpoints (and a few storefront webhooks) still send a flat
    string. We accept either shape and always return a string the
    downstream parser can read. Returning "" rather than None keeps the
    column non-NULL and the dashboard timestamp formatter happy.
    """
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("date") or value.get("iso") or value.get("formatted") or "")
    return str(value)


def _normalise_abandoned_cart(raw: Any) -> Dict:
    """Convert a Salla-style abandoned-cart payload into the internal Order shape.

    Salla's ``GET /admin/v2/carts/abandoned`` endpoint returns a shape
    that differs from ``/orders`` in a few important ways
    (docs: https://docs.salla.dev/api-5394138):

      * The cart's primary key is ``id`` (small integer).
      * ``total`` is ``{"amount": <number>, "currency": "SAR"}``.
      * ``customer`` carries ``{id, name, mobile, email, ...}`` â€” the
        phone is in ``mobile``, not ``phone``.
      * ``checkout_url`` is the resume-cart URL we surface to the dashboard
        and to the WhatsApp recovery flow â€” never compute it ourselves.
      * ``items`` is the line-item list; we keep it verbatim so the cart
        editor / recovery message can render product names.
      * ``created_at`` and ``updated_at`` are nested objects
        ``{date, timezone, timezone_type}``, NOT plain strings. The old
        normalizer fell through ``str(dict)`` here and produced a row
        with an unparseable timestamp â€” handled now via
        ``_flatten_salla_datetime``.

    We intentionally prefix the external_id with ``cart-`` so an abandoned
    cart can NEVER collide with a real Salla order that happens to share
    the same numeric id (Salla's cart and order id-spaces are independent).
    Without this prefix a later ``sync_orders`` could overwrite the saved
    cart row when an order with the same id eventually exists.
    """
    if hasattr(raw, "dict"):
        raw = raw.dict()
    if not isinstance(raw, dict):
        raw = {}

    customer_info = dict(raw.get("customer") or {})
    if customer_info:
        normalized_phone = _normalize_phone(
            customer_info.get("mobile", customer_info.get("phone", ""))
        )
        if normalized_phone:
            customer_info["mobile"] = normalized_phone
            customer_info["phone"] = normalized_phone

    raw_total = raw.get("total") or raw.get("sub_total") or raw.get("amount") or raw.get("amounts", {})

    cart_id = str(raw.get("id") or raw.get("cart_id") or raw.get("token") or "").strip()
    external_id = f"cart-{cart_id}" if cart_id else ""
    external_order_number = str(
        raw.get("reference_id")
        or raw.get("number")
        or raw.get("code")
        or cart_id
    ).strip() or cart_id

    customer_name = (
        (customer_info.get("name") if isinstance(customer_info, dict) else None)
        or raw.get("customer_name")
        or ""
    )
    customer_name = str(customer_name).strip()

    # Flatten the Salla nested {date, timezone} shape BEFORE handing
    # the dict to ``_extract_order_datetime`` â€” that function does
    # ``str(value)`` on whatever is at ``raw["created_at"]`` and would
    # otherwise produce a literal "{'date': ...}" string that no parser
    # accepts. We mutate a copy so we never alter the adapter's payload.
    raw_for_dt = dict(raw)
    flat_created = _flatten_salla_datetime(raw.get("created_at"))
    flat_updated = _flatten_salla_datetime(raw.get("updated_at"))
    if flat_created:
        raw_for_dt["created_at"] = flat_created
    if flat_updated:
        raw_for_dt["updated_at"] = flat_updated
    cart_dt = _extract_order_datetime(raw_for_dt)

    return {
        "external_id":           external_id,
        "external_order_number": external_order_number,
        "status":                "abandoned",
        "total":                 _extract_amount_string(raw_total),
        "customer_name":         customer_name,
        "customer_info":         customer_info,
        "line_items":            raw.get("items") or raw.get("line_items") or [],
        "checkout_url":          raw.get("checkout_url", "") or raw.get("url", ""),
        "is_abandoned":          True,
        "source":                "salla",
        "created_at":            cart_dt.isoformat() if cart_dt else (flat_created or flat_updated),
        "raw_cart_id":           cart_id,
    }


def _normalise_coupon(raw: Any) -> Dict:
    if hasattr(raw, "dict"):
        raw = raw.dict()
    discount_val = raw.get("amount", raw.get("percent", raw.get("value", "")))
    raw_type = str(raw.get("type", raw.get("discount_type", "percentage")) or "").lower()
    if raw_type in ("fixed", "amount"):
        discount_type = "fixed"
    elif raw.get("percent"):
        discount_type = "percentage"
    else:
        discount_type = "percentage" if raw_type in ("percentage", "percent", "") else raw_type
    status = raw.get("status", "active")
    min_order = raw.get("minimum_amount", raw.get("minimum_order", None))
    if isinstance(min_order, dict):
        min_order = min_order.get("amount")
    usage_limit = raw.get("usage_limit", raw.get("maximum_uses", None))
    start_raw = raw.get("start_date", raw.get("starts_at", None))
    from services.coupon_salla_push import extract_salla_coupon_name, parse_salla_datetime  # noqa: PLC0415
    expires_raw = raw.get("expire_date", raw.get("expiry_date", raw.get("expires_at", None)))
    raw_dict = raw if isinstance(raw, dict) else {}
    coupon_name = extract_salla_coupon_name(raw_dict)
    return {
        "code":           raw.get("code", ""),
        "name":           coupon_name or "",
        "description":    coupon_name or raw.get("description", raw.get("name", "")),
        "discount_type":  discount_type,
        "discount_value": str(discount_val) if discount_val else "",
        "starts_at":      parse_salla_datetime(start_raw),
        "expires_at":     parse_salla_datetime(expires_raw) or expires_raw,
        "active":         status == "active" if isinstance(status, str) else raw.get("active", True),
        "minimum_order":  min_order,
        "usage_limit":    usage_limit,
        "maximum_uses":   usage_limit,
    }


# NOTE: The segment classifier used to live here as ``_compute_segment`` with
# a different label set (``churned|new|vip|active``) than the authoritative
# one in ``services/customer_intelligence.compute_customer_status`` (``lead|
# new|active|vip|at_risk|inactive``). It was never called from anywhere but
# caused confusion during refactors, so it was deleted as part of the
# 2026-04-16 root-cause fix. Use ``CustomerIntelligenceService`` for ALL
# classification decisions.


# â”€â”€ Sync service â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class StoreSyncService:
    """
    Orchestrates syncing a tenant's store data into Nahla's DB and
    building the AI-ready StoreKnowledgeSnapshot.
    """

    def __init__(
        self,
        db: Session,
        tenant_id: int,
        *,
        integration_connection_id: int | None = None,
        adapter: Any = None,
    ):
        self.db        = db
        self.tenant_id = tenant_id
        self._integration_connection_id = integration_connection_id
        self._adapter  = adapter   # lazy-loaded when None
        self._customer_intelligence = CustomerIntelligenceService(db, tenant_id)

    # â”€â”€ Adapter access â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_adapter(self):
        if self._adapter is None:
            try:
                sys.path.insert(0, os.path.abspath(os.path.join(_THIS, "..")))
                if self._integration_connection_id is not None:
                    from store_integration.registry import adapter_for_integration  # noqa: PLC0415
                    from models import Integration  # noqa: PLC0415

                    intg = (
                        self.db.query(Integration)
                        .filter_by(id=int(self._integration_connection_id), tenant_id=self.tenant_id)
                        .first()
                    )
                    if intg is not None:
                        self._adapter = adapter_for_integration(intg)
                if self._adapter is None:
                    from store_integration.registry import get_adapter  # noqa: PLC0415
                    self._adapter = get_adapter(self.tenant_id)
            except Exception as exc:
                logger.warning("tenant=%s store adapter unavailable: %s", self.tenant_id, exc)
        return self._adapter

    def _apply_a1_external_identity(
        self,
        order_row: Any,
        *,
        order_payload: dict,
        integration_resolution: Any,
        ingest_source: str,
    ) -> None:
        from services.order_customer_identity_service import (  # noqa: PLC0415
            apply_external_order_identity_from_salla,
        )

        try:
            apply_external_order_identity_from_salla(
                self.db,
                order=order_row,
                tenant_id=self.tenant_id,
                integration_resolution=integration_resolution,
                order_payload=order_payload if isinstance(order_payload, dict) else {},
                ingest_source=ingest_source,
            )
        except Exception as exc:  # noqa: BLE001
            from services.order_customer_identity_logging import (  # noqa: PLC0415
                log_identity_sync_failure,
            )

            log_identity_sync_failure(
                tenant_id=self.tenant_id,
                ingest_source=ingest_source,
                exception_class=type(exc).__name__,
                link_outcome="exception",
            )

    # â”€â”€ Job helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _start_job(self, sync_type: str, triggered_by: str = "system") -> StoreSyncJob:
        job = StoreSyncJob(
            tenant_id    = self.tenant_id,
            status       = "running",
            sync_type    = sync_type,
            triggered_by = triggered_by,
            started_at   = datetime.now(timezone.utc),
        )
        self.db.add(job)
        self.db.flush()
        return job

    def _finish_job(self, job: StoreSyncJob, **counts):
        job.status       = "completed"
        job.completed_at = datetime.now(timezone.utc)
        for k, v in counts.items():
            if hasattr(job, k):
                setattr(job, k, v)
        self.db.commit()

    def _fail_job(self, job: StoreSyncJob, error: str):
        job.status        = "failed"
        job.completed_at  = datetime.now(timezone.utc)
        job.error_message = error[:2000]
        self.db.commit()

    # â”€â”€ Snapshot builder â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _rebuild_snapshot(self, products_count: int, orders_count: int, coupons_count: int):
        """Rebuild the AI-ready knowledge snapshot from DB contents."""
        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if not snap:
            snap = StoreKnowledgeSnapshot(tenant_id=self.tenant_id)
            self.db.add(snap)

        # Build catalog summary (top 50 in-stock products for AI context)
        top_products = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id)
            .limit(50)
            .all()
        )
        catalog_items = []
        categories: set = set()
        for p in top_products:
            meta = p.extra_metadata or {}
            catalog_items.append({
                "id":        p.id,
                "external_id": p.external_id,
                "title":     p.title,
                "sku":       p.sku,
                "price":     p.price,
                "sale_price": meta.get("sale_price"),
                "in_stock":  meta.get("in_stock", True),
                "category":  meta.get("category", ""),
                "brand":     meta.get("brand", ""),
                "image_url": meta.get("image_url", ""),
            })
            if meta.get("category"):
                categories.add(meta["category"])

        # Active coupons
        active_coupons = (
            self.db.query(Coupon)
            .filter(
                Coupon.tenant_id == self.tenant_id,
                (Coupon.expires_at == None) | (Coupon.expires_at > datetime.now(timezone.utc)),  # noqa: E711
            )
            .all()
        )
        coupon_list = [
            {
                "code":           c.code,
                "description":    c.description,
                "discount_type":  c.discount_type,
                "discount_value": c.discount_value,
                "expires_at":     c.expires_at.isoformat() if c.expires_at else None,
            }
            for c in active_coupons
        ]

        # Store profile from TenantSettings
        settings = (
            self.db.query(TenantSettings)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        store_cfg  = (settings.store_settings or {}) if settings else {}

        # Pack A1: Salla /store/info lands namespaced â€” never silently flatten
        # into manual store_settings fields without provenance.
        salla_store_info = dict(store_cfg.get("salla_store_info") or {})
        # Pages index only (bodies live in MerchantKnowledgeSection).
        pages_index = []
        for pg in list(store_cfg.get("pages") or []):
            if not isinstance(pg, dict):
                continue
            pages_index.append({
                "id": pg.get("id") or pg.get("page_id") or "",
                "page_id": pg.get("page_id") or pg.get("id") or "",
                "title": pg.get("title") or "",
                "slug": pg.get("slug") or "",
                "kind": pg.get("kind") or "custom",
                "active": bool(pg.get("active", True)),
                "content_hash": pg.get("content_hash") or "",
                "doc_ref": pg.get("doc_ref"),
                "knowledge_section_id": pg.get("knowledge_section_id"),
            })

        snap.store_profile = {
            "store_name":    store_cfg.get("store_name", ""),
            "store_url":     store_cfg.get("store_url", ""),
            # Physical-location URL (Google / Apple / Waze maps).
            # Mirrored here so the merchant brain can deliver the maps
            # link deterministically without hitting the relational
            # store every turn â€” see May 2026 #36 maps stack.
            "maps_url":      store_cfg.get("google_maps_location", ""),
            "logo_url":      store_cfg.get("logo_url", ""),
            "description":   store_cfg.get("store_description", ""),
            # Pack A2: public profile phone only â€” never WhatsApp owner number.
            "contact_phone": (
                store_cfg.get("store_phone")
                or store_cfg.get("public_phone")
                or store_cfg.get("contact_phone_public")
                or ""
            ),
            "contact_email": store_cfg.get("contact_email", ""),
            # Legacy pages index (if any). Pack A1 does NOT refresh this from
            # Salla /pages â€” that Merchant API route is unproven (404).
            "pages":         pages_index,
            # Pack A1 namespaced Salla profile (source of truth for Salla-owned facts).
            # Manual fields above remain the Nahla override surface â€” no silent merge.
            "salla_store_info": salla_store_info,
        }
        snap.catalog_summary = {
            "total_products": products_count,
            "categories":     list(categories)[:30],
            "top_products":   catalog_items,
        }
        snap.coupon_summary = {
            "active_count": len(coupon_list),
            "coupons":      coupon_list[:20],
        }

        # Shipping from store settings
        snap.shipping_summary = {
            "methods": store_cfg.get("shipping_methods", []),
            "notes":   store_cfg.get("delivery_notes", ""),
        }

        # Policy
        snap.policy_summary = {
            "return_policy":   store_cfg.get("return_policy", ""),
            "shipping_policy": store_cfg.get("shipping_policy", ""),
            "payment_methods": store_cfg.get("payment_methods", []),
            "support_hours":   store_cfg.get("support_hours", ""),
        }

        snap.product_count  = products_count
        snap.order_count    = orders_count
        snap.coupon_count   = len(coupon_list)
        snap.customer_count = self.db.query(Customer).filter_by(tenant_id=self.tenant_id).count()
        snap.category_count = len(categories)
        snap.sync_version   = (snap.sync_version or 0) + 1
        snap.last_full_sync_at = datetime.now(timezone.utc)
        snap.updated_at        = datetime.now(timezone.utc)
        self.db.commit()

    # â”€â”€ Incremental timestamp helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _last_sync_timestamp(self) -> Optional[str]:
        """Return ISO timestamp of the last successful full sync, or None if never synced."""
        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if snap and snap.last_full_sync_at:
            return snap.last_full_sync_at.isoformat()
        return None

    # â”€â”€ Products sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _enrich_normalised_variants_from_adapter(
        self,
        adapter: Any,
        normalised: Dict[str, Any],
    ) -> None:
        """Fill ``normalised['variants']`` from Salla /variants when missing."""
        if normalised.get("variants"):
            return
        ext_id = (normalised.get("external_id") or "").strip()
        if not ext_id or not hasattr(adapter, "get_raw_variants"):
            return
        try:
            raw_vars = await adapter.get_raw_variants(ext_id)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "[catalog/variants] tenant=%s product=%s dedicated variants "
                "fetch failed: %s",
                self.tenant_id, ext_id, exc,
            )
            return
        if not raw_vars:
            return
        product_options = normalised.get("options") or []
        variants_out: List[Dict[str, Any]] = []
        for raw_v in raw_vars:
            if not isinstance(raw_v, dict):
                continue
            if hasattr(adapter, "_normalize_variant"):
                norm = adapter._normalize_variant(raw_v, product_options)
                variants_out.append(_coerce_variant_dict(norm))
            else:
                variants_out.append(_coerce_variant_dict(raw_v))
        for v_dict in variants_out:
            for price_key in ("price", "sale_price", "regular_price"):
                if v_dict.get(price_key) is not None:
                    v_dict[price_key] = str(v_dict[price_key])
        normalised["variants"] = variants_out

    def _apply_normalised_product(
        self,
        normalised: Dict[str, Any],
        adapter_source: str,
    ) -> Dict[str, Any]:
        """Upsert one normalised product row (+ variants). Shared by bulk and one-product sync."""
        ext_id = normalised["external_id"]
        new_qty = _coerce_int(normalised.get("stock_qty"))
        new_in_stock = bool(normalised.get("in_stock", True))
        new_available = new_in_stock and (new_qty is None or new_qty > 0)
        row_source = normalised.get("source") or adapter_source

        existing = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        restocked: Optional[Dict[str, Any]] = None

        if existing:
            try:
                from core.catalog import is_merchant_editable_product  # noqa: PLC0415

                if is_merchant_editable_product(existing):
                    logger.info(
                        "[StoreSync] skip protected nahla-native/manual product "
                        "tenant=%s product=%s external_id=%s",
                        self.tenant_id, existing.id, ext_id,
                    )
                    return {
                        "action": "skipped_protected",
                        "product_id": existing.id,
                        "restocked": None,
                    }
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[StoreSync] native-product guard check failed tenant=%s "
                    "product=%s external_id=%s â€” continuing normal upsert",
                    self.tenant_id,
                    getattr(existing, "id", None),
                    ext_id,
                    exc_info=True,
                )
            was_unavailable = (
                (getattr(existing, "in_stock", True) is False)
                or (existing.stock_quantity is not None and existing.stock_quantity <= 0)
            )
            existing.title = normalised["title"]
            existing.description = normalised["description"]
            existing.price = normalised["price"]
            existing.sku = normalised["sku"]
            existing.in_stock = new_in_stock
            existing.stock_quantity = new_qty
            _preserve_existing_product_images(normalised, existing.extra_metadata)
            existing.extra_metadata = normalised
            if (existing.source or "").lower() != "manual":
                existing.source = row_source
            try:
                from core.catalog import assign_canonical_retailer_id  # noqa: PLC0415
                assign_canonical_retailer_id(existing)
            except Exception:  # noqa: BLE001
                pass
            try:
                _upsert_variants_for(self.db, existing, normalised)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[catalog/variants] tenant=%s product=%s upsert "
                    "failed â€” continuing parent sync",
                    self.tenant_id, existing.id,
                )
            product_row = existing
            action = "updated"
            if was_unavailable and new_available:
                restocked = {
                    "product_id": existing.id,
                    "external_id": ext_id,
                    "title": normalised["title"],
                }
        else:
            product_row = Product(
                tenant_id=self.tenant_id,
                external_id=ext_id,
                title=normalised["title"],
                description=normalised["description"],
                price=normalised["price"],
                sku=normalised["sku"],
                in_stock=new_in_stock,
                stock_quantity=new_qty,
                extra_metadata=normalised,
                source=row_source,
            )
            self.db.add(product_row)
            self.db.flush()
            try:
                _upsert_variants_for(self.db, product_row, normalised)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[catalog/variants] tenant=%s product=%s "
                    "(new) variant upsert failed â€” continuing",
                    self.tenant_id, product_row.id,
                )
            action = "created"

        return {
            "action": action,
            "product_id": product_row.id,
            "restocked": restocked,
        }

    async def sync_one_product_by_external_id(
        self,
        external_id: str,
        *,
        dry_run: bool = True,
        nahla_product_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Fetch one Salla product and optionally upsert it into Nahla."""
        adapter = self._get_adapter()
        if adapter is None:
            return {
                "ok": False,
                "error": "adapter_unavailable",
                "message": (
                    f"Store adapter unavailable for tenant={self.tenant_id} "
                    "(integration may need reauth)"
                ),
            }

        ext_id = str(external_id or "").strip()
        if not ext_id:
            return {
                "ok": False,
                "error": "missing_external_id",
                "message": "external_id is required",
            }

        db_before = _product_db_summary(
            self.db,
            self.tenant_id,
            external_id=ext_id,
            product_id=nahla_product_id,
        )
        resolved_nahla_id = (db_before or {}).get("nahla_product_id") or nahla_product_id

        try:
            raw = await adapter.get_product(ext_id)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": "salla_fetch_failed",
                "message": str(exc),
                "tenant_id": self.tenant_id,
                "external_id": ext_id,
            }

        if raw is None:
            return {
                "ok": False,
                "error": "salla_product_not_found",
                "message": f"Salla product not found for external_id={ext_id}",
                "tenant_id": self.tenant_id,
                "external_id": ext_id,
            }

        normalised = _normalise_product(raw)
        await self._enrich_normalised_variants_from_adapter(adapter, normalised)
        salla_live = _salla_live_summary(normalised)
        diff = _compute_one_product_sync_diff(db_before, normalised)

        report: Dict[str, Any] = {
            "ok": True,
            "dry_run": dry_run,
            "tenant_id": self.tenant_id,
            "external_id": ext_id,
            "nahla_product_id": resolved_nahla_id,
            "db_before": db_before,
            "salla_live": salla_live,
            "diff": diff,
        }

        if dry_run:
            return report

        adapter_source = getattr(adapter, "platform", None) or "salla"
        apply_result = self._apply_normalised_product(normalised, adapter_source)
        self.db.flush()
        self.db.commit()

        db_after = _product_db_summary(self.db, self.tenant_id, external_id=ext_id)
        report["db_after"] = db_after
        report["action"] = apply_result["action"]
        report["nahla_product_id"] = apply_result["product_id"]
        return report

    async def sync_products(self, incremental: bool = False) -> int:
        """Fetch products from the store adapter and upsert into DB.

        If incremental=True and a previous full sync exists, only fetch
        products updated since that timestamp.
        """
        adapter = self._get_adapter()
        if not adapter:
            return 0

        updated_since = None
        if incremental:
            updated_since = self._last_sync_timestamp()

        try:
            raw_list = await adapter.get_products(updated_since=updated_since)
        except Exception as exc:
            logger.warning("tenant=%s product sync failed: %s", self.tenant_id, exc)
            return 0

        logger.info(
            "tenant=%s syncing %d products (incremental=%s, since=%s)",
            self.tenant_id, len(raw_list), incremental, updated_since or "beginning",
        )

        created = 0
        updated = 0
        # (product_id, external_id, title) for products that just transitioned
        # from out-of-stock â†’ in-stock. Fan-out is performed once after the
        # loop so we don't slow down each iteration with ProductInterest queries
        # for products no one is waiting on.
        restocked: List[Dict[str, Any]] = []
        # Resolve the product source for THIS sync run once â€” the value is
        # the same for every row coming out of a given adapter. Falls back
        # to the registered adapter platform name (salla/zid/shopify) so
        # that even adapters that forget to stamp ``source`` on normalised
        # rows still get a meaningful column value. We deliberately do NOT
        # default to ``"manual"`` here â€” a sync that can't identify itself
        # is still a sync, not a hand-entered product. The diagnostics
        # endpoint reads ``Product.source`` exclusively, so getting this
        # right at intake means we never have to scan ``extra_metadata``
        # JSONB at read time.
        adapter_source = (
            getattr(adapter, "platform", None)
            or "salla"
        )

        for raw in raw_list:
            normalised = _normalise_product(raw)
            await self._enrich_normalised_variants_from_adapter(adapter, normalised)
            apply_result = self._apply_normalised_product(normalised, adapter_source)
            if apply_result["action"] == "created":
                created += 1
            elif apply_result["action"] == "updated":
                updated += 1
            if apply_result.get("restocked"):
                restocked.append(apply_result["restocked"])
        self.db.flush()
        # Auto-map retailer ids on freshly-created rows. We do this
        # after ``flush()`` so the synthetic-id fallback can read
        # ``p.id`` (newly assigned by Postgres). Idempotent â€” only
        # touches rows whose meta_retailer_id is still NULL.
        try:
            from core.catalog import assign_canonical_retailer_id  # noqa: PLC0415

            for p in (
                self.db.query(Product)
                .filter(Product.tenant_id == self.tenant_id)
                .filter(Product.meta_retailer_id.is_(None))
                .all()
            ):
                assign_canonical_retailer_id(p)
            self.db.flush()
        except Exception:  # noqa: BLE001
            pass

        # â”€â”€ Back-in-stock fan-out â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # For each product that just came back, emit one
        # `product_back_in_stock` AutomationEvent per pending ProductInterest
        # row. The engine then processes each event as a normal single-customer
        # send, so all the existing idempotency/delay/condition machinery
        # continues to apply (including per-execution metrics).
        if restocked:
            self._fan_out_back_in_stock(restocked)

        logger.info(
            "tenant=%s products sync done â€” created=%d updated=%d total_upserted=%d restocked=%d",
            self.tenant_id, created, updated, created + updated, len(restocked),
        )
        return created + updated

    def _fan_out_back_in_stock(self, restocked: List[Dict[str, Any]]) -> None:
        """
        Emit one product_back_in_stock event per pending ProductInterest row
        for each restocked product. Called from sync_products after the
        upsert loop has flushed the new stock state.
        """
        from core.automation_engine import emit_automation_event  # noqa: PLC0415

        # Look up the merchant's store URL once so we can synthesize a
        # clickable product URL in the event payload â€” the named slot
        # `product_url` is the contract every back_in_stock_* template uses.
        store_cfg = (
            self.db.query(TenantSettings)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        store_url_root = ""
        if store_cfg and store_cfg.store_settings:
            store_url_root = str(store_cfg.store_settings.get("store_url") or "").rstrip("/")

        emitted = 0
        for prod in restocked:
            interests: List[ProductInterest] = (
                self.db.query(ProductInterest)
                .filter(
                    ProductInterest.tenant_id  == self.tenant_id,
                    ProductInterest.product_id == prod["product_id"],
                    ProductInterest.notified   == False,  # noqa: E712
                )
                .all()
            )
            if not interests:
                continue
            now = datetime.now(timezone.utc)
            for interest in interests:
                product_url = ""
                if store_url_root and prod["external_id"]:
                    product_url = f"{store_url_root}/p/{prod['external_id']}"
                emit_automation_event(
                    self.db,
                    self.tenant_id,
                    "product_back_in_stock",
                    customer_id=interest.customer_id,
                    payload={
                        "product_id":          prod["product_id"],
                        "product_external_id": prod["external_id"],
                        "product_name":        prod["title"],
                        "product_url":         product_url,
                        "store_url":           store_url_root,
                        "interest_id":         interest.id,
                    },
                    commit=False,
                )
                # Mark the interest as notified up-front. If the engine fails
                # to actually send (no template, no WA connection), the
                # AutomationExecution row records the failure â€” re-arming the
                # waitlist on every restock would double-spam customers.
                interest.notified = True
                interest.notified_at = now
                emitted += 1
        if emitted:
            self.db.flush()
            logger.info(
                "tenant=%s back-in-stock fan-out â€” products=%d events=%d",
                self.tenant_id, len(restocked), emitted,
            )

    # â”€â”€ Orders sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def sync_orders(
        self,
        incremental: bool = False,
        updated_since: str | None = None,
        triggered_by: str = "manual",
    ) -> int:
        adapter = self._get_adapter()
        if not adapter:
            return 0

        _since = updated_since  # explicit override takes priority
        has_local_orders = self.db.query(Order).filter(Order.tenant_id == self.tenant_id).first() is not None
        if _since is None and incremental and has_local_orders:
            _since = self._last_sync_timestamp()

        try:
            raw_list = await adapter.get_orders(updated_since=_since)
        except Exception as exc:
            logger.warning("tenant=%s orders sync failed: %s", self.tenant_id, exc)
            raise

        logger.info(
            "tenant=%s syncing %d orders (incremental=%s, since=%s, triggered_by=%s)",
            self.tenant_id, len(raw_list), incremental and has_local_orders, _since or "beginning",
            triggered_by,
        )

        created = 0
        updated = 0
        status_counter: Dict[str, int] = {}
        zero_total_count = 0
        for raw in raw_list:
            # Capture the upstream status BEFORE normalisation so we can audit
            # any future divergence between Salla's slug and our DB string.
            raw_status = None
            try:
                raw_status = (
                    raw.status if hasattr(raw, "status")
                    else (raw.get("status") if isinstance(raw, dict) else None)
                )
            except Exception:
                raw_status = None

            normalised = _normalise_order(raw)
            ext_id = normalised["external_id"]
            normalised_status = normalised["status"]
            status_counter[normalised_status] = status_counter.get(normalised_status, 0) + 1

            # If the normalised status looks like a Python repr, that means
            # the upstream was a dict the adapter failed to unwrap. Surface
            # loudly â€” historically this corruption silently mapped every
            # order to "ظ…ظ„ط؛ظٹ" in the dashboard.
            if normalised_status.startswith("{"):
                logger.warning(
                    "tenant=%s order=%s status looks like a repr (%r) â€” adapter "
                    "failed to extract slug from raw=%r",
                    self.tenant_id, ext_id, normalised_status, raw_status,
                )

            try:
                _amount = float(normalised["total"] or 0)
            except (TypeError, ValueError):
                _amount = 0.0
            if _amount == 0.0:
                zero_total_count += 1

            # Resolve the order's source: prefer what the adapter put on
            # the normalised row, else fall back to the registered adapter
            # platform name (salla/zid/shopify). Never leave it blank for a
            # platform-synced order.
            adapter_source = (
                normalised.get("source")
                or getattr(adapter, "platform", None)
                or "salla"
            )

            existing = (
                self.db.query(Order)
                .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
                .first()
            )
            if existing:
                prev_status = existing.status
                existing.status                = normalised_status
                existing.total                 = normalised["total"]
                existing.customer_info         = normalised["customer_info"]
                existing.line_items            = normalised["line_items"]
                existing.is_abandoned          = normalised["is_abandoned"]
                existing.external_order_number = normalised["external_order_number"]
                if normalised["customer_name"]:
                    existing.customer_name = normalised["customer_name"]
                existing.source = adapter_source
                existing.extra_metadata = _merge_order_extra_metadata(
                    existing.extra_metadata, normalised,
                )
                from core.order_delivered_stamp import stamp_order_delivered_at_if_needed  # noqa: PLC0415
                stamp_order_delivered_at_if_needed(existing, previous_status=prev_status)
                await _handle_external_lifecycle_transition_best_effort(
                    self,
                    order=existing,
                    provider=adapter_source,
                    raw_previous_status=prev_status,
                    raw_current_status=normalised_status,
                    normalized_order=normalised,
                    raw_payload=raw if isinstance(raw, dict) else None,
                )
                if self._integration_connection_id is not None:
                    from services.salla_integration_resolver import (  # noqa: PLC0415
                        ResolvedSallaIntegration,
                    )

                    self._apply_a1_external_identity(
                        existing,
                        order_payload=raw if isinstance(raw, dict) else normalised,
                        integration_resolution=ResolvedSallaIntegration(
                            integration_id=int(self._integration_connection_id),
                            tenant_id=int(self.tenant_id),
                            matched_via="api_poll_explicit",
                        ),
                        ingest_source="store_sync.sync_orders",
                    )
                updated += 1
            else:
                new_row = Order(
                    tenant_id             = self.tenant_id,
                    external_id           = ext_id,
                    external_order_number = normalised["external_order_number"],
                    status                = normalised_status,
                    total                 = normalised["total"],
                    customer_name         = normalised["customer_name"] or None,
                    customer_info         = normalised["customer_info"],
                    line_items            = normalised["line_items"],
                    checkout_url          = normalised["checkout_url"],
                    is_abandoned          = normalised["is_abandoned"],
                    source                = adapter_source,
                    extra_metadata        = _merge_order_extra_metadata(
                        None, normalised,
                    ),
                )
                self.db.add(new_row)
                self.db.flush()  # assign PK so we can reference new_row.id below
                from core.order_delivered_stamp import stamp_order_delivered_at_if_needed  # noqa: PLC0415
                stamp_order_delivered_at_if_needed(new_row, previous_status=None)

                # â”€â”€ Fire automation events for new orders found via API poll â”€â”€
                # Mirrors handle_order_webhook so confirmation messages are sent
                # even when the webhook was missed or delayed by Salla.
                if not normalised.get("is_abandoned"):
                    try:
                        from core.automation_engine import emit_automation_event  # noqa: PLC0415
                        from core.automation_triggers import AutomationTrigger    # noqa: PLC0415

                        _pm     = str(normalised.get("payment_method") or "").lower()
                        _status = str(normalised_status or "").lower()

                        emit_automation_event(
                            self.db,
                            self.tenant_id,
                            AutomationTrigger.ORDER_NOTIFICATIONS.value,
                            payload={
                                "external_id":           ext_id,
                                "order_id":              new_row.id,
                                "order_internal_id":     new_row.id,
                                "status":                _status,
                                "total":                 normalised.get("total"),
                                "order_number":          normalised.get("external_order_number") or ext_id,
                                "external_order_number": normalised.get("external_order_number"),
                                "checkout_url":          normalised.get("checkout_url") or "",
                                "payment_url":           normalised.get("checkout_url") or "",
                                "payment_method":        _pm,
                                "source":                f"store_sync.api_poll.{triggered_by}",
                            },
                            commit=False,
                        )

                        _COD_METHODS = {"cod", "cash_on_delivery", "cash", "ط§ظ„ط¯ظپط¹ ط¹ظ†ط¯ ط§ظ„ط§ط³طھظ„ط§ظ…"}
                        _is_cod = bool(
                            _pm and any(_pm == m or m in _pm for m in _COD_METHODS)
                        )
                        if _is_cod:
                            emit_automation_event(
                                self.db,
                                self.tenant_id,
                                AutomationTrigger.ORDER_COD_PENDING.value,
                                payload={
                                    "external_id":           ext_id,
                                    "order_id":              new_row.id,
                                    "order_number":          normalised.get("external_order_number") or ext_id,
                                    "status":                _status,
                                    "total":                 normalised.get("total"),
                                    "payment_method":        _pm,
                                    "checkout_url":          normalised.get("checkout_url") or "",
                                    "payment_url":           normalised.get("checkout_url") or "",
                                    "source":                f"store_sync.api_poll.{triggered_by}",
                                    "step_idx":              0,
                                    "message_type":          "initial_confirmation",
                                },
                                commit=False,
                            )
                        # Mark on the new row so the safety-net poller in
                        # services/salla_orders_poller.py knows we already
                        # fired and never double-emits for this order.
                        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                        _meta = dict(new_row.extra_metadata or {})
                        _meta["notifications_emitted"]    = True
                        _meta["notifications_emitted_at"] = datetime.now(timezone.utc).isoformat()
                        _meta["notifications_emitted_by"] = f"sync_orders.{triggered_by}"
                        new_row.extra_metadata = _meta
                        flag_modified(new_row, "extra_metadata")

                        self.db.commit()
                        logger.info(
                            "[StoreSync/poll] automation events emitted tenant=%s order=%s pm=%s",
                            self.tenant_id, ext_id, _pm or "unknown",
                        )
                    except Exception as _ae:
                        logger.warning(
                            "[StoreSync/poll] automation emit failed tenant=%s order=%s: %s",
                            self.tenant_id, ext_id, _ae,
                        )

                await _handle_external_lifecycle_transition_best_effort(
                    self,
                    order=new_row,
                    provider=adapter_source,
                    raw_previous_status=None,
                    raw_current_status=normalised_status,
                    normalized_order=normalised,
                    raw_payload=raw if isinstance(raw, dict) else None,
                )
                if self._integration_connection_id is not None:
                    from services.salla_integration_resolver import (  # noqa: PLC0415
                        ResolvedSallaIntegration,
                    )

                    self._apply_a1_external_identity(
                        new_row,
                        order_payload=raw if isinstance(raw, dict) else normalised,
                        integration_resolution=ResolvedSallaIntegration(
                            integration_id=int(self._integration_connection_id),
                            tenant_id=int(self.tenant_id),
                            matched_via="api_poll_explicit",
                        ),
                        ingest_source="store_sync.sync_orders",
                    )

                created += 1

        logger.info(
            "tenant=%s orders sync done â€” created=%d updated=%d total_upserted=%d "
            "status_distribution=%s zero_total=%d",
            self.tenant_id, created, updated, created + updated,
            status_counter, zero_total_count,
        )
        self.db.flush()
        return created + updated

    # â”€â”€ Abandoned carts sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #
    # Salla's abandoned carts live behind a SEPARATE endpoint (/admin/v2/carts).
    # The /orders endpoint never returns them, so until we wired this method
    # in the dashboard read at /autopilot/queues â€” which filters
    # `Order.is_abandoned == True` â€” was always empty regardless of how many
    # carts the merchant had abandoned in Salla. That is the root cause of
    # the "Salla shows 2, Nahla shows 0" inconsistency the merchant reported.
    #
    # Behaviour & invariants:
    #   1. We persist abandoned carts INTO the same `orders` table the
    #      dashboard already reads from, with `is_abandoned=True` and
    #      `status="abandoned"`. This avoids a parallel data path and reuses
    #      every existing recovery automation.
    #   2. We prefix the external_id with `cart-` so a future order with the
    #      same numeric Salla id never overwrites the cart row (Salla's
    #      cart and order id-spaces are independent integers).
    #   3. Reconcile: any cart row we previously stored that no longer
    #      appears in Salla's response (because the customer either
    #      converted it to an order or Salla aged it out) is flipped back
    #      to `is_abandoned=False` so the dashboard count tracks Salla's
    #      live state rather than a growing backlog.
    #   4. SILENT-FAIL guard: if Salla returns zero carts but we still have
    #      previously-saved carts, we log a loud warning AND keep the old
    #      rows visible. This is the same protection pattern already used
    #      for the live-totals snapshot (see test_store_sync_live_totals).
    #      A transient 401 / empty-page response must never wipe the
    #      dashboard.
    async def sync_abandoned_carts(self) -> Dict[str, int]:
        adapter = self._get_adapter()
        result = {
            "salla_count":     0,
            "saved":           0,
            "updated":         0,
            "reconciled":      0,
            "skipped_no_id":   0,
            "skipped_no_data": 0,
            "fetched":         False,
        }
        if not adapter or not hasattr(adapter, "get_abandoned_carts"):
            logger.info(
                "tenant=%s abandoned-cart sync skipped â€” adapter missing get_abandoned_carts",
                self.tenant_id,
            )
            return result

        try:
            raw_list = await adapter.get_abandoned_carts() or []
            result["fetched"] = True
        except Exception as exc:
            logger.warning(
                "tenant=%s abandoned-cart fetch failed (%s) â€” KEEPING existing rows visible",
                self.tenant_id, exc,
            )
            return result

        result["salla_count"] = len(raw_list)
        # â”€â”€ BEFORE-normalization log â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Print the raw cart count + the first few external ids verbatim
        # so an operator grepping logs can confirm the adapter actually
        # delivered carts to the sync layer (separately from how many
        # we *kept* after normalization).
        _raw_id_preview = [
            str((c.get("id") or c.get("cart_id") or c.get("token") or "?"))
            for c in raw_list[:5] if isinstance(c, dict)
        ]
        logger.info(
            "[StoreSync] ABANDONED_SYNC_FETCHED tenant=%s raw_cart_count=%d "
            "first_ids=%s",
            self.tenant_id, len(raw_list), _raw_id_preview,
        )

        # Track failures per stage for the end-of-run summary.
        result["normalize_errors"] = 0   # type: ignore[assignment]
        result["save_errors"]      = 0   # type: ignore[assignment]
        normalised_external_ids: List[str] = []

        # Existing cart rows for this tenant â€” keyed by external_id so we
        # can both upsert and reconcile in a single pass.
        existing_carts: Dict[str, Order] = {
            o.external_id: o
            for o in self.db.query(Order)
            .filter(Order.tenant_id == self.tenant_id, Order.is_abandoned == True)  # noqa: E712
            .all()
        }
        previous_count = len(existing_carts)

        # â”€â”€ SILENT-FAIL guard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if previous_count > 0 and result["salla_count"] == 0:
            logger.warning(
                "tenant=%s âڑ ï¸ڈ abandoned-cart sync returned ZERO from Salla "
                "but %d carts were previously saved â€” KEEPING existing rows "
                "to avoid wiping the merchant dashboard on a transient empty "
                "response. Investigate adapter / token / Salla state.",
                self.tenant_id, previous_count,
            )
            return result

        seen_external_ids = set()

        for raw in raw_list:
            # â”€â”€ Per-cart isolation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # Wrap each cart's normalization+persist in its own try block.
            # Without this, ONE malformed cart from Salla (unexpected
            # field shape, NaN, infinite recursion in nested totalsâ€¦)
            # crashed the whole loop and silently wiped the rest of the
            # batch â€” exactly the "Salla shows N, Nahla shows 0" symptom
            # we're trying to eradicate.
            try:
                normalised = _normalise_abandoned_cart(raw)
            except Exception as exc:
                result["normalize_errors"] += 1
                logger.exception(
                    "[StoreSync] tenant=%s NORMALIZE_FAILED â€” skipping one "
                    "cart, continuing batch | error=%s | raw_keys=%s | "
                    "raw_preview=%s",
                    self.tenant_id, exc,
                    sorted(list(raw.keys())) if isinstance(raw, dict) else type(raw).__name__,
                    str(raw)[:300],
                )
                continue

            ext_id = normalised["external_id"]
            if not ext_id:
                result["skipped_no_id"] += 1
                logger.warning(
                    "[StoreSync] tenant=%s SKIPPED_NO_ID â€” abandoned cart had "
                    "no usable id field | raw_keys=%s | raw_preview=%s",
                    self.tenant_id,
                    sorted(list(raw.keys())) if isinstance(raw, dict) else type(raw).__name__,
                    str(raw)[:300],
                )
                continue

            normalised_external_ids.append(ext_id)

            # NOTE: previously this loop also dropped carts where BOTH
            # customer_info and line_items were empty. That filter is
            # gone â€” we now persist any cart that has a stable id, even
            # if it's a bare draft. The recovery flow can decide later
            # whether it's actionable; the dashboard should never lose
            # visibility into a real cart just because the customer
            # hasn't entered their phone yet.
            if not normalised["customer_info"] and not normalised["line_items"]:
                logger.info(
                    "[StoreSync] tenant=%s PERSISTING_EMPTY_SHELL ext_id=%s "
                    "(no customer + no items) â€” kept for dashboard visibility",
                    self.tenant_id, ext_id,
                )

            seen_external_ids.add(ext_id)
            try:
                existing = existing_carts.get(ext_id) or (
                    self.db.query(Order)
                    .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
                    .first()
                )

                if existing:
                    existing.status                = normalised["status"]
                    existing.total                 = normalised["total"]
                    existing.customer_info         = normalised["customer_info"]
                    existing.line_items            = normalised["line_items"]
                    existing.is_abandoned          = True
                    existing.checkout_url          = normalised["checkout_url"]
                    existing.external_order_number = normalised["external_order_number"]
                    if normalised["customer_name"]:
                        existing.customer_name = normalised["customer_name"]
                    existing.source = "salla"
                    meta = dict(existing.extra_metadata or {})
                    meta["created_at"]    = normalised.get("created_at") or meta.get("created_at")
                    meta["abandoned_at"]  = meta.get("abandoned_at") or normalised.get("created_at")
                    meta["last_synced_at"] = datetime.now(timezone.utc).isoformat()
                    meta["source_kind"]   = "abandoned_cart"
                    meta["raw_cart_id"]   = normalised.get("raw_cart_id")
                    existing.extra_metadata = meta
                    result["updated"] += 1
                    logger.info(
                        "[StoreSync] tenant=%s UPDATED_abandoned_cart "
                        "ext_id=%s order_id=%s items=%d total=%s "
                        "customer=%s",
                        self.tenant_id, ext_id, existing.id,
                        len(normalised["line_items"] or []),
                        normalised["total"],
                        bool(normalised["customer_info"]),
                    )
                else:
                    self.db.add(Order(
                        tenant_id             = self.tenant_id,
                        external_id           = ext_id,
                        external_order_number = normalised["external_order_number"],
                        status                = normalised["status"],
                        total                 = normalised["total"],
                        customer_name         = normalised["customer_name"] or None,
                        customer_info         = normalised["customer_info"],
                        line_items            = normalised["line_items"],
                        checkout_url          = normalised["checkout_url"],
                        is_abandoned          = True,
                        source                = "salla",
                        extra_metadata        = {
                            "created_at":     normalised.get("created_at"),
                            "abandoned_at":   normalised.get("created_at"),
                            "last_synced_at": datetime.now(timezone.utc).isoformat(),
                            "source_kind":    "abandoned_cart",
                            "raw_cart_id":    normalised.get("raw_cart_id"),
                        },
                    ))
                    result["saved"] += 1
                    logger.info(
                        "[StoreSync] tenant=%s SAVED_abandoned_cart "
                        "ext_id=%s items=%d total=%s customer=%s "
                        "checkout_url=%s",
                        self.tenant_id, ext_id,
                        len(normalised["line_items"] or []),
                        normalised["total"],
                        bool(normalised["customer_info"]),
                        bool(normalised["checkout_url"]),
                    )
            except Exception as exc:
                result["save_errors"] += 1
                self.db.rollback()
                logger.exception(
                    "[StoreSync] tenant=%s SAVE_FAILED ext_id=%s â€” "
                    "rolling back this cart only, continuing batch | "
                    "error=%s",
                    self.tenant_id, ext_id, exc,
                )

        # â”€â”€ Reconcile: clear is_abandoned on rows Salla no longer lists â”€
        # The customer either resumed â†’ became a real order, or Salla aged
        # the cart out. Either way the dashboard must stop showing it.
        for ext_id, row in existing_carts.items():
            if ext_id in seen_external_ids:
                continue
            row.is_abandoned = False
            meta = dict(row.extra_metadata or {})
            meta["recovered_or_expired_at"] = datetime.now(timezone.utc).isoformat()
            row.extra_metadata = meta
            result["reconciled"] += 1

        self.db.flush()

        # â”€â”€ Kick off the recovery automation for newly-seen carts â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Idempotent on ``Order.extra_metadata.recovery_event_id`` so a
        # cart that already produced an event (via the webhook path or a
        # previous sweep) does NOT double-emit. The emit happens AFTER
        # the upsert flush so the marker write and the parent row are in
        # a consistent state. We commit eagerly inside the emitter so a
        # later cart's failure cannot rollback an already-emitted one.
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.exception(
                "[StoreSync] tenant=%s commit before recovery emit failed",
                self.tenant_id,
            )

        emit_count = 0
        emit_failures = 0
        try:
            from services.cart_recovery_emitter import (  # noqa: PLC0415
                emit_cart_abandoned_if_new,
            )
            for raw in raw_list:
                if not isinstance(raw, dict):
                    continue
                try:
                    emit_normalised = _normalise_abandoned_cart(raw)
                except Exception:
                    continue
                ext_id = emit_normalised.get("external_id") or ""
                if not ext_id or ext_id not in seen_external_ids:
                    continue
                cart_row = (
                    self.db.query(Order)
                    .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
                    .first()
                )
                if cart_row is None:
                    continue
                try:
                    new_id = emit_cart_abandoned_if_new(
                        self.db,
                        tenant_id=self.tenant_id,
                        cart_row=cart_row,
                        normalised=emit_normalised,
                        source="store_sync",
                    )
                    if new_id is not None and (cart_row.extra_metadata or {}).get(
                        "recovery_event_id"
                    ) == new_id:
                        # Only count rows where THIS sweep actually wrote
                        # the marker (the helper returns the existing id
                        # for already-emitted carts).
                        emit_count += 1
                except Exception:
                    emit_failures += 1
                    logger.exception(
                        "[StoreSync] tenant=%s cart=%s emit_cart_abandoned_if_new failed",
                        self.tenant_id, ext_id,
                    )
        except Exception:
            logger.exception(
                "[StoreSync] tenant=%s recovery emit pass aborted", self.tenant_id,
            )

        result["recovery_events_emitted"] = emit_count    # type: ignore[assignment]
        result["recovery_emit_failures"] = emit_failures  # type: ignore[assignment]

        # â”€â”€ AFTER-normalization log + structured summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Single grep-friendly line so the operator can answer
        # "did this run succeed end-to-end?" with one log search.
        # If raw_cart_count > 0 but saved+updated == 0, the bug is
        # downstream of normalize/save and the per-cart logs above will
        # tell us exactly which stage dropped the cart.
        logger.info(
            "[StoreSync] ABANDONED_SYNC_SUMMARY tenant=%s "
            "raw_cart_count=%d normalized_cart_count=%d "
            "saved=%d updated=%d reconciled=%d "
            "skipped_no_id=%d normalize_errors=%d save_errors=%d "
            "previous=%d external_ids=%s",
            self.tenant_id,
            result["salla_count"],
            len(normalised_external_ids),
            result["saved"], result["updated"], result["reconciled"],
            result["skipped_no_id"], result["normalize_errors"], result["save_errors"],
            previous_count,
            normalised_external_ids[:10],
        )

        # Surface counts in the JSON result so the debug endpoints
        # (and the scheduler audit log) can show them without reading
        # raw application logs.
        result["normalized_count"] = len(normalised_external_ids)  # type: ignore[assignment]
        return result

    # â”€â”€ Coupons sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def sync_coupons(self) -> int:
        adapter = self._get_adapter()
        if not adapter or not hasattr(adapter, "get_coupons"):
            return 0

        try:
            raw_list = await adapter.get_coupons()
        except Exception as exc:
            logger.warning("tenant=%s coupons sync failed: %s", self.tenant_id, exc)
            return 0

        logger.info("tenant=%s syncing %d coupons", self.tenant_id, len(raw_list))

        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        from services.coupon_sync_visibility import (  # noqa: PLC0415
            build_salla_import_metadata,
            build_salla_reconcile_metadata,
            is_nahla_system_coupon,
            merge_salla_import_metadata,
            should_mark_imported_source_type,
        )

        created = 0
        updated = 0
        for raw in raw_list:
            normalised = _normalise_coupon(raw)
            code = normalised["code"]
            if not code:
                continue
            existing = (
                self.db.query(Coupon)
                .filter_by(tenant_id=self.tenant_id, code=code)
                .first()
            )
            exp = None
            if normalised["expires_at"]:
                try:
                    exp = datetime.fromisoformat(str(normalised["expires_at"]).replace("Z", "+00:00"))
                except Exception:
                    pass

            synced_at = datetime.now(timezone.utc)

            if existing:
                preserve_origin = is_nahla_system_coupon(
                    getattr(existing, "source_type", None),
                    existing.extra_metadata,
                )
                if preserve_origin:
                    import_meta = build_salla_reconcile_metadata(
                        raw, synced_at, existing.extra_metadata,
                    )
                else:
                    import_meta = build_salla_import_metadata(raw, normalised, synced_at)

                existing.description    = normalised.get("name") or normalised["description"]
                existing.discount_type  = normalised["discount_type"]
                existing.discount_value = normalised["discount_value"]
                existing.expires_at     = exp
                merged_meta = merge_salla_import_metadata(
                    existing.extra_metadata,
                    import_meta,
                    preserve_origin=preserve_origin,
                )
                if should_mark_imported_source_type(
                    getattr(existing, "source_type", None),
                    existing.extra_metadata,
                ):
                    existing.source_type = "imported"
                existing.extra_metadata = merged_meta
                flag_modified(existing, "extra_metadata")
                updated += 1
            else:
                import_meta = build_salla_import_metadata(raw, normalised, synced_at)
                self.db.add(Coupon(
                    tenant_id      = self.tenant_id,
                    code           = code,
                    description    = normalised.get("name") or normalised["description"],
                    discount_type  = normalised["discount_type"],
                    discount_value = normalised["discount_value"],
                    expires_at     = exp,
                    source_type    = "imported",
                    extra_metadata = import_meta,
                ))
                created += 1
        self.db.flush()
        logger.info(
            "tenant=%s coupons sync done â€” created=%d updated=%d",
            self.tenant_id, created, updated,
        )
        return created + updated

    # â”€â”€ Store profile sync (Pack A1 â€” Salla /store/info) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def sync_store_info(self) -> bool:
        """Fetch customer-facing store profile from Salla GET /store/info.

        Persists under store_settings['salla_store_info'] with provenance.
        Failure preserves prior profile and records source_error (failure â‰  empty).
        """
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        adapter = self._get_adapter()
        if not adapter:
            return False
        if not hasattr(adapter, "get_store_info_profile"):
            logger.info(
                "tenant=%s adapter does not support get_store_info_profile â€” skipping",
                self.tenant_id,
            )
            return False

        try:
            outcome = await adapter.get_store_info_profile()
        except Exception as exc:
            logger.warning(
                "tenant=%s store_info sync failed (non-fatal, prior preserved): %s",
                self.tenant_id, exc,
            )
            outcome = {
                "ok": False,
                "profile": {},
                "http_status": None,
                "error_class": "fetch_error",
                "fetched_at": None,
            }

        if not isinstance(outcome, dict):
            outcome = {"ok": False, "profile": {}, "error_class": "invalid_outcome"}

        settings = (
            self.db.query(TenantSettings)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if not settings:
            return False

        current = dict(settings.store_settings or {})
        prior = dict(current.get("salla_store_info") or {})
        now_iso = datetime.now(timezone.utc).isoformat()

        if outcome.get("ok"):
            profile = dict(outcome.get("profile") or {})
            profile["fetched_at"] = outcome.get("fetched_at") or now_iso
            profile["sync_ok"] = True
            profile["error_class"] = None
            profile["http_status"] = outcome.get("http_status") or 200
            profile["provenance"] = {
                "source": "salla",
                "endpoint": "/store/info",
                "tenant_id": int(self.tenant_id),
                "precedence": (
                    "For Salla-connected tenants, salla_store_info owns "
                    "Salla-sourced profile facts. Manual store_settings remain "
                    "a separate Nahla override surface â€” no silent contradictory merge."
                ),
            }
            current["salla_store_info"] = profile
        else:
            # Preserve prior profile body; mark freshness/source error.
            preserved = dict(prior) if prior else {}
            preserved["sync_ok"] = False
            preserved["error_class"] = outcome.get("error_class") or "fetch_error"
            preserved["http_status"] = outcome.get("http_status")
            preserved["last_error_at"] = now_iso
            if prior:
                preserved.setdefault("fetched_at", prior.get("fetched_at"))
            current["salla_store_info"] = preserved

        settings.store_settings = current
        flag_modified(settings, "store_settings")
        try:
            self.db.commit()
        except Exception as db_exc:
            logger.error(
                "tenant=%s failed to persist salla_store_info: %s",
                self.tenant_id, db_exc,
            )
            try:
                self.db.rollback()
            except Exception:  # noqa: silent-ok
                pass
            return False

        logger.info(
            "tenant=%s store_info sync ok=%s error_class=%s",
            self.tenant_id,
            bool(outcome.get("ok")),
            outcome.get("error_class"),
        )
        return bool(outcome.get("ok"))

    # â”€â”€ Pages sync (DEFERRED â€” Salla Merchant API has no proven /pages source) â”€

    async def sync_pages(self) -> int:
        """No-op for Pack A1 profile-only.

        Source gate (2026-08): Salla GET /pages is not an official Merchant API
        route (live 404). Automatic CMS import / reconcile is deferred until a
        proven source exists. Long-form knowledge is merchant-authored via
        MerchantKnowledgeSection; structured profile uses GET /store/info.
        """
        logger.info(
            "tenant=%s sync_pages deferred â€” Salla CMS auto-import not in Pack A1 "
            "(no proven Merchant API /pages source)",
            self.tenant_id,
        )
        return 0

    # â”€â”€ Checkout profile sync (Pack B merchant-enabled shipping/payment) â”€â”€â”€â”€â”€â”€â”€

    async def sync_checkout_profile(self) -> bool:
        """Fetch merchant-enabled Salla shipping/payment capabilities.

        Persists into Integration.config['checkout_profile'] (tenant-scoped).
        Non-fatal: failures leave prior profile untouched and return False.
        """
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        from core.salla_merchant_capabilities import (  # noqa: PLC0415
            merge_checkout_profile_into_config,
        )
        from store_integration.registry import pick_active_salla_integration  # noqa: PLC0415

        adapter = self._get_adapter()
        if not adapter or not hasattr(adapter, "sync_store_checkout_profile"):
            logger.info(
                "tenant=%s checkout_profile sync skipped â€” no Salla adapter",
                self.tenant_id,
            )
            return False

        intg = pick_active_salla_integration(self.db, self.tenant_id)
        if not intg:
            return False

        try:
            profile = await adapter.sync_store_checkout_profile()
        except Exception as exc:
            logger.warning(
                "tenant=%s checkout_profile fetch failed (non-fatal): %s",
                self.tenant_id, exc,
            )
            return False

        try:
            new_config = merge_checkout_profile_into_config(
                intg.config if isinstance(intg.config, dict) else {},
                profile,
            )
            intg.config = new_config
            flag_modified(intg, "config")
            self.db.commit()
            logger.info(
                "tenant=%s checkout_profile persisted | payments=%s "
                "companies=%s zones=%s",
                self.tenant_id,
                (profile.get("payments") or {}).get("status"),
                ((profile.get("shipping") or {}).get("companies") or {}).get("status"),
                ((profile.get("shipping") or {}).get("zones") or {}).get("status"),
            )
            return True
        except Exception as db_exc:
            logger.error(
                "tenant=%s failed to persist checkout_profile: %s",
                self.tenant_id, db_exc,
            )
            try:
                self.db.rollback()
            except Exception:  # noqa: silent-ok
                pass
            return False

    # â”€â”€ Customers sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _upsert_legacy_customer_from_salla_sync_payload(
        self,
        raw: Dict,
        *,
        sync_source: str,
        adapter_platform: str,
    ) -> Optional[str]:
        """Legacy Customer upsert/update (pre-A1). Writes Customer.salla_customer_id."""
        ext_id = str(raw.get("id", ""))
        if not ext_id:
            return None

        name = (raw.get("first_name", "") + " " + raw.get("last_name", "")).strip()
        if not name:
            name = raw.get("name", "")
        email = raw.get("email", "")
        raw_phone_str = raw.get("mobile", raw.get("phone", ""))
        phone = _normalize_phone(raw_phone_str)
        norm_phone = _e164(raw_phone_str)

        existing = (
            self.db.query(Customer)
            .filter(
                Customer.tenant_id == self.tenant_id,
                Customer.salla_customer_id == ext_id,
            )
            .first()
        ) if ext_id else None

        if not existing and ext_id:
            existing = (
                self.db.query(Customer)
                .filter(
                    Customer.tenant_id == self.tenant_id,
                    Customer.extra_metadata["salla_id"].astext == ext_id,
                )
                .first()
            )
            if existing:
                existing.salla_customer_id = ext_id

        if not existing and norm_phone:
            existing = (
                self.db.query(Customer)
                .filter(
                    Customer.tenant_id == self.tenant_id,
                    Customer.normalized_phone == norm_phone,
                )
                .first()
            )
        if not existing and phone:
            existing = (
                self.db.query(Customer)
                .filter(
                    Customer.tenant_id == self.tenant_id,
                    Customer.phone == phone,
                )
                .first()
            )

        if existing:
            if name:
                from core.customer_identity_resolver import apply_customer_name  # noqa: PLC0415

                apply_customer_name(
                    existing,
                    name,
                    source=sync_source,
                    platform=adapter_platform,
                )
            if email:
                existing.email = email
            if phone:
                existing.phone = phone
            if norm_phone:
                existing.normalized_phone = norm_phone
            if ext_id and not existing.salla_customer_id:
                existing.salla_customer_id = ext_id
            if not existing.acquisition_channel:
                existing.acquisition_channel = "salla_sync"

            prev_meta = dict(existing.extra_metadata or {})
            tags = set(prev_meta.get("source_tags") or [])
            tags.add(sync_source)
            prev_meta.update({
                "salla_id": ext_id,
                "source_tags": sorted(tags),
                "city": raw.get("city", "") or prev_meta.get("city", ""),
                "country": raw.get("country", "SA") or prev_meta.get("country", "SA"),
            })
            existing.extra_metadata = prev_meta
            return "updated"

        from datetime import timezone as _tz  # noqa: PLC0415

        new_customer = Customer(
            tenant_id=self.tenant_id,
            name=None,
            email=email or None,
            phone=phone or None,
            normalized_phone=norm_phone,
            extra_metadata={
                "salla_id": ext_id,
                "source": sync_source,
                "source_tags": [sync_source],
                "city": raw.get("city", ""),
                "country": raw.get("country", "SA"),
            },
            salla_customer_id=ext_id or None,
            acquisition_channel=sync_source,
            first_seen_at=datetime.now(_tz.utc),
        )
        if name:
            from core.customer_identity_resolver import apply_customer_name  # noqa: PLC0415

            apply_customer_name(
                new_customer,
                name,
                source=sync_source,
                platform=adapter_platform,
            )
        self.db.add(new_customer)
        return "created"

    def _upsert_a1_external_profile_side_effect(
        self,
        payload: Dict,
        *,
        integration_connection_id: int | None = None,
    ) -> None:
        """Best-effort A1 ExternalCustomerProfile upsert parallel to legacy Customer sync."""
        conn_id = (
            integration_connection_id
            if integration_connection_id is not None
            else self._integration_connection_id
        )
        if conn_id is None:
            return
        try:
            from services.external_customer_profile_service import (  # noqa: PLC0415
                upsert_profile_from_salla_customer_sync,
            )

            upsert_profile_from_salla_customer_sync(
                self.db,
                tenant_id=self.tenant_id,
                integration_connection_id=int(conn_id),
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            from services.order_customer_identity_logging import log_identity_sync_failure  # noqa: PLC0415

            log_identity_sync_failure(
                tenant_id=self.tenant_id,
                ingest_source="salla_customer_sync_a1_side_effect",
                exception_class=type(exc).__name__,
                link_outcome="profile_side_effect_failed",
            )

    async def sync_customers(self, incremental: bool = False) -> int:
        adapter = self._get_adapter()
        if not adapter or not hasattr(adapter, "get_customers"):
            return 0
        adapter_platform = str(getattr(adapter, "platform", None) or "salla").strip().lower() or "salla"
        sync_source = f"{adapter_platform}_sync"

        updated_since = None
        if incremental:
            updated_since = self._last_sync_timestamp()

        try:
            raw_list = await adapter.get_customers(updated_since=updated_since)
        except Exception as exc:
            logger.warning("tenant=%s customers sync failed: %s", self.tenant_id, exc)
            return 0

        logger.info(
            "tenant=%s syncing %d customers (incremental=%s, since=%s)",
            self.tenant_id, len(raw_list), incremental, updated_since or "beginning",
        )

        created = 0
        updated = 0
        for raw in raw_list:
            try:
                outcome = self._upsert_legacy_customer_from_salla_sync_payload(
                    raw,
                    sync_source=sync_source,
                    adapter_platform=adapter_platform,
                )
                if outcome == "created":
                    created += 1
                elif outcome == "updated":
                    updated += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "tenant=%s legacy customer sync row failed ext_id=%s: %s",
                    self.tenant_id,
                    raw.get("id"),
                    exc,
                )
            self._upsert_a1_external_profile_side_effect(raw)
        self.db.flush()
        logger.info(
            "tenant=%s customers sync done â€” created=%d updated=%d",
            self.tenant_id, created, updated,
        )
        return created + updated

    # â”€â”€ Customer profile builder â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _build_customer_profiles(self) -> int:
        """Create/update CustomerProfile for every customer using unified intelligence rules."""
        return self._customer_intelligence.rebuild_profiles_for_tenant(
            reason="store_sync_build_profiles",
            commit=True,
            emit_event=True,
        )

    # â”€â”€ Full sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def full_sync(self, triggered_by: str = "merchant", incremental: bool = False) -> Dict:
        """Sync store data into the local DB.

        Args:
            triggered_by: who initiated the sync (merchant / scheduler / oauth_connect).
            incremental: if True, only fetch items updated since last full sync.
                         First sync is always full regardless of this flag.
        """
        # â”€â”€ Pre-sync guard: refuse if binding is invalid â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            from services.salla_guard import validate_before_sync  # noqa: PLC0415
            ok, reason = validate_before_sync(self.db, self.tenant_id)
            if not ok:
                logger.warning(
                    "tenant=%s â›” SYNC_BLOCKED â€” %s (triggered_by=%s)",
                    self.tenant_id, reason, triggered_by,
                )
                # Persist a failed job so the frontend can show a meaningful error.
                blocked_job = self._start_job("full", triggered_by)
                self._fail_job(blocked_job, f"ظ…ط²ط§ظ…ظ†ط© ظ…ط­ط¸ظˆط±ط©: {reason}")
                return {"status": "blocked", "message": reason}
        except Exception as guard_exc:
            logger.warning("tenant=%s salla_guard check failed (non-fatal): %s", self.tenant_id, guard_exc)

        has_previous = self._last_sync_timestamp() is not None
        is_incremental = incremental and has_previous

        sync_type = "incremental" if is_incremental else "full"
        job = self._start_job(sync_type, triggered_by)
        logger.info(
            "tenant=%s â–¶ %s sync started (triggered_by=%s, has_previous=%s)",
            self.tenant_id, sync_type.upper(), triggered_by, has_previous,
        )

        try:
            products_n  = await self.sync_products(incremental=is_incremental)
            orders_n    = await self.sync_orders(incremental=is_incremental)
            # Abandoned carts come from a SEPARATE Salla endpoint
            # (/admin/v2/carts), not from /orders. We sync them right after
            # orders so any customer who just resumed their cart into a
            # real order is reconciled correctly (sync_orders runs first
            # â†’ row exists with is_abandoned=False; then sync_abandoned_carts
            # only re-flags rows Salla still lists as abandoned).
            try:
                carts_result = await self.sync_abandoned_carts()
                abandoned_n  = carts_result.get("saved", 0) + carts_result.get("updated", 0)
            except Exception as cart_exc:
                logger.warning(
                    "tenant=%s abandoned-cart sync raised â€” orders pipeline kept alive: %s",
                    self.tenant_id, cart_exc,
                )
                carts_result = {}
                abandoned_n  = 0
            coupons_n   = await self.sync_coupons()
            customers_n = await self.sync_customers(incremental=is_incremental)
            profiles_n  = self._customer_intelligence.rebuild_profiles_for_tenant(
                reason=f"full_sync:{triggered_by}",
                commit=True,
                emit_event=True,
            )

            try:
                await self.sync_store_info()
            except Exception as store_info_exc:
                logger.warning(
                    "tenant=%s store_info sync raised unexpectedly (non-fatal): %s",
                    self.tenant_id, store_info_exc,
                )

            try:
                pages_n = await self.sync_pages()
            except Exception as pages_exc:
                logger.warning(
                    "tenant=%s pages sync raised unexpectedly (non-fatal): %s",
                    self.tenant_id, pages_exc,
                )
                pages_n = 0

            try:
                checkout_profile_ok = await self.sync_checkout_profile()
            except Exception as checkout_exc:
                logger.warning(
                    "tenant=%s checkout_profile sync raised (non-fatal): %s",
                    self.tenant_id, checkout_exc,
                )
                checkout_profile_ok = False

            self._rebuild_snapshot(products_n, orders_n, coupons_n)
            self._finish_job(
                job,
                products_synced   = products_n,
                orders_synced     = orders_n,
                coupons_synced    = coupons_n,
                customers_synced  = customers_n,
            )
            total_items = products_n + orders_n + coupons_n + customers_n
            if total_items == 0:
                logger.warning(
                    "tenant=%s âڑ ï¸ڈ %s sync completed but ALL counts are ZERO â€” "
                    "store may be empty or token may lack permissions",
                    self.tenant_id, sync_type.upper(),
                )
            else:
                logger.info(
                    "tenant=%s âœ… %s sync completed â€” products=%d orders=%d coupons=%d customers=%d profiles=%d",
                    self.tenant_id, sync_type.upper(), products_n, orders_n, coupons_n, customers_n, profiles_n,
                )

            result = {
                "status":                   "completed",
                "sync_type":                sync_type,
                "products_synced":          products_n,
                "orders_synced":            orders_n,
                "coupons_synced":           coupons_n,
                "customers_synced":         customers_n,
                "profiles_updated":         profiles_n,
                "abandoned_carts_synced":   abandoned_n,
                "abandoned_carts_detail":   carts_result,
                "checkout_profile_synced":  bool(checkout_profile_ok),
                "job_id":                   job.id,
            }
            if total_items == 0:
                result["message"] = (
                    "طھظ… ط§ظ„ط±ط¨ط· ط¨ظ†ط¬ط§ط­ ظ„ظƒظ† ط§ظ„ظ…طھط¬ط± ظ„ط§ ظٹط­طھظˆظٹ ط¹ظ„ظ‰ ط¨ظٹط§ظ†ط§طھ ظ‚ط§ط¨ظ„ط© ظ„ظ„ظ…ط²ط§ظ…ظ†ط© ط­ط§ظ„ظٹط§ظ‹. "
                    "ط£ط¶ظپ ظ…ظ†طھط¬ط§طھ ظپظٹ ط³ظ„ط© ط«ظ… ط£ط¹ط¯ ط§ظ„ظ…ط²ط§ظ…ظ†ط©."
                )
            return result
        except Exception as exc:
            self._fail_job(job, str(exc))
            logger.error("tenant=%s â‌Œ %s sync error: %s", self.tenant_id, sync_type.upper(), exc)
            return {"status": "failed", "error": str(exc), "job_id": job.id}

    # â”€â”€ Incremental product update (called by webhook) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def handle_product_webhook(self, payload: Dict) -> None:
        """Process a single product update from a platform webhook."""
        normalised = _normalise_product(payload)
        ext_id     = normalised["external_id"]
        if not ext_id:
            return

        adapter = self._get_adapter()
        if adapter:
            await self._enrich_normalised_variants_from_adapter(adapter, normalised)

        existing = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        if existing:
            existing.title         = normalised["title"]
            existing.price         = normalised["price"]
            existing.description   = normalised.get("description", existing.description)
            existing.sku           = normalised.get("sku", existing.sku)
            existing.in_stock      = bool(normalised.get("in_stock", True))
            existing.stock_quantity = _coerce_int(normalised.get("stock_qty"))
            _preserve_existing_product_images(normalised, existing.extra_metadata)
            existing.extra_metadata = normalised
            product_row = existing
        else:
            product_row = Product(
                tenant_id      = self.tenant_id,
                external_id    = ext_id,
                title          = normalised["title"],
                description    = normalised["description"],
                price          = normalised["price"],
                sku            = normalised["sku"],
                in_stock       = bool(normalised.get("in_stock", True)),
                stock_quantity = _coerce_int(normalised.get("stock_qty")),
                extra_metadata = normalised,
            )
            self.db.add(product_row)
        self.db.flush()
        try:
            _upsert_variants_for(self.db, product_row, normalised)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[catalog/variants] tenant=%s product=%s webhook variant "
                "upsert failed",
                self.tenant_id, ext_id,
            )
        self.db.commit()

        # Rebuild snapshot counts (lightweight)
        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if snap:
            snap.product_count             = (
                self.db.query(Product).filter_by(tenant_id=self.tenant_id).count()
            )
            snap.last_incremental_sync_at  = datetime.now(timezone.utc)
            snap.updated_at                = datetime.now(timezone.utc)
            self.db.commit()

    # â”€â”€ Incremental order update (called by webhook) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def handle_abandoned_cart_webhook(self, payload: Dict) -> None:
        """
        Process a single ``abandoned.cart`` event from a Salla webhook.

        Persists the cart payload into the same ``orders`` table the
        dashboard reads from with ``is_abandoned=True``. Idempotent by the
        ``cart-{cart_id}`` external_id we assign in
        ``_normalise_abandoned_cart``, which is intentionally namespaced so
        a real order with the same numeric Salla id can never collide.
        """
        normalised = _normalise_abandoned_cart(payload)
        ext_id     = normalised["external_id"]
        if not ext_id:
            logger.info(
                "tenant=%s abandoned_cart webhook ignored â€” payload had no cart id",
                self.tenant_id,
            )
            return

        cart_row = (
            self.db.query(Order)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        if cart_row is not None:
            cart_row.status                = normalised["status"]
            cart_row.total                 = normalised["total"]
            cart_row.customer_info         = normalised["customer_info"]
            cart_row.line_items            = normalised["line_items"]
            cart_row.is_abandoned          = True
            cart_row.checkout_url          = normalised["checkout_url"] or cart_row.checkout_url
            cart_row.external_order_number = normalised["external_order_number"]
            if normalised["customer_name"]:
                cart_row.customer_name = normalised["customer_name"]
            cart_row.source = "salla"
            meta = dict(cart_row.extra_metadata or {})
            meta["created_at"]    = normalised.get("created_at") or meta.get("created_at")
            meta["abandoned_at"]  = meta.get("abandoned_at") or normalised.get("created_at") or now_iso
            meta["last_synced_at"] = now_iso
            meta["source_kind"]   = "abandoned_cart"
            meta["raw_cart_id"]   = normalised.get("raw_cart_id")
            cart_row.extra_metadata = meta
        else:
            cart_row = Order(
                tenant_id             = self.tenant_id,
                external_id           = ext_id,
                external_order_number = normalised["external_order_number"],
                status                = normalised["status"],
                total                 = normalised["total"],
                customer_name         = normalised["customer_name"] or None,
                customer_info         = normalised["customer_info"],
                line_items            = normalised["line_items"],
                checkout_url          = normalised["checkout_url"],
                is_abandoned          = True,
                source                = "salla",
                extra_metadata        = {
                    "created_at":     normalised.get("created_at") or now_iso,
                    "abandoned_at":   normalised.get("created_at") or now_iso,
                    "last_synced_at": now_iso,
                    "source_kind":    "abandoned_cart",
                    "raw_cart_id":    normalised.get("raw_cart_id"),
                },
            )
            self.db.add(cart_row)

        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        logger.info(
            "tenant=%s abandoned_cart webhook upserted cart external_id=%s",
            self.tenant_id, ext_id,
        )

        # Fire the recovery flow. Idempotent on
        # ``Order.extra_metadata.recovery_event_id`` so concurrent
        # webhook + sweep calls cannot double-emit. Failures here log
        # at WARNING but never break the webhook ingest path â€” the cart
        # row is already durably stored.
        try:
            from services.cart_recovery_emitter import (  # noqa: PLC0415
                emit_cart_abandoned_if_new,
            )
            emit_cart_abandoned_if_new(
                self.db,
                tenant_id=self.tenant_id,
                cart_row=cart_row,
                normalised=normalised,
                source="webhook",
            )
        except Exception:
            logger.exception(
                "[StoreSync] cart_abandoned emit failed tenant=%s cart=%s "
                "(cart row saved, recovery flow will not start until next sweep)",
                self.tenant_id, ext_id,
            )

    async def handle_order_webhook(
        self,
        payload: Dict,
        *,
        integration_resolution: Any = None,
    ) -> None:
        """
        Process a single order create/update from a platform webhook.

        Idempotent by ``(tenant_id, external_id)`` â€” the DB enforces this via
        the partial unique index ``uq_orders_tenant_external_id`` added in
        migration 0023. Concurrent webhooks can no longer double-insert.
        """
        from core.obs import EVENTS, log_event  # noqa: PLC0415
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

        normalised = _normalise_order(payload)
        ext_id     = normalised["external_id"]
        if not ext_id:
            log_event(
                EVENTS.ORDER_UPSERT_ERROR,
                tenant_id=self.tenant_id,
                reason="missing_external_id",
            )
            return

        is_new = False
        lifecycle_prev_status: Optional[str] = None
        order_row = (
            self.db.query(Order)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        # Webhook payload doesn't always tell us which adapter it came
        # from; resolve from the registered adapter for this tenant so the
        # source column stays accurate (salla/zid/shopify).
        webhook_source = (
            normalised.get("source")
            or getattr(self._get_adapter(), "platform", None)
            or "salla"
        )

        if order_row is not None:
            lifecycle_prev_status = order_row.status
            prev_status = order_row.status
            order_row.status                = normalised["status"]
            order_row.total                 = normalised["total"]
            order_row.customer_info         = normalised["customer_info"]
            order_row.line_items            = normalised["line_items"]
            order_row.is_abandoned          = normalised["is_abandoned"]
            order_row.external_order_number = normalised["external_order_number"]
            if normalised["customer_name"]:
                order_row.customer_name = normalised["customer_name"]
            order_row.source = webhook_source
            order_row.extra_metadata = _merge_order_extra_metadata(
                order_row.extra_metadata, normalised,
            )
            from core.order_delivered_stamp import stamp_order_delivered_at_if_needed  # noqa: PLC0415
            stamp_order_delivered_at_if_needed(order_row, previous_status=prev_status)
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        else:
            order_row = Order(
                tenant_id             = self.tenant_id,
                external_id           = ext_id,
                external_order_number = normalised["external_order_number"],
                status                = normalised["status"],
                total                 = normalised["total"],
                customer_name         = normalised["customer_name"] or None,
                customer_info         = normalised["customer_info"],
                line_items            = normalised["line_items"],
                checkout_url          = normalised["checkout_url"],
                is_abandoned          = normalised["is_abandoned"],
                source                = webhook_source,
                extra_metadata        = _merge_order_extra_metadata(
                    None, normalised,
                ),
            )
            self.db.add(order_row)
            from core.order_delivered_stamp import stamp_order_delivered_at_if_needed  # noqa: PLC0415
            stamp_order_delivered_at_if_needed(order_row, previous_status=None)
            try:
                self.db.commit()
                is_new = True
            except IntegrityError:
                # Concurrent writer beat us to it â€” fall back to UPDATE path.
                self.db.rollback()
                log_event(
                    EVENTS.ORDER_UPSERT_CONFLICT,
                    tenant_id=self.tenant_id,
                    external_id=ext_id,
                )
                order_row = (
                    self.db.query(Order)
                    .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
                    .first()
                )
                if order_row is None:
                    # Should be impossible, but fail loudly rather than silently.
                    raise
                lifecycle_prev_status = order_row.status
                prev_status = order_row.status
                order_row.status                = normalised["status"]
                order_row.total                 = normalised["total"]
                order_row.customer_info         = normalised["customer_info"]
                order_row.line_items            = normalised["line_items"]
                order_row.is_abandoned          = normalised["is_abandoned"]
                order_row.external_order_number = normalised["external_order_number"]
                if normalised["customer_name"]:
                    order_row.customer_name = normalised["customer_name"]
                order_row.source = webhook_source
                order_row.extra_metadata = _merge_order_extra_metadata(
                    order_row.extra_metadata, normalised,
                )
                from core.order_delivered_stamp import stamp_order_delivered_at_if_needed  # noqa: PLC0415
                stamp_order_delivered_at_if_needed(order_row, previous_status=prev_status)
                try:
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise

        log_event(
            EVENTS.ORDER_UPSERT_SUCCESS,
            tenant_id=self.tenant_id,
            external_id=ext_id,
            order_id=order_row.id,
            is_new=is_new,
            status=normalised["status"],
        )

        customer = None
        if integration_resolution is not None:
            self._apply_a1_external_identity(
                order_row,
                order_payload=payload,
                integration_resolution=integration_resolution,
                ingest_source="store_sync.order_webhook",
            )
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        else:
            customer = self._customer_intelligence.upsert_customer_from_order(
                normalised,
                source="order_webhook",
                commit=False,
            )
            if customer:
                self._customer_intelligence.recompute_profile_for_customer(
                    customer.id,
                    reason="order_webhook",
                    commit=True,
                    emit_event=True,
                )

        if is_new:
            try:
                from core.automation_engine import emit_automation_event  # noqa: PLC0415
                from core.automation_triggers import AutomationTrigger    # noqa: PLC0415
                _payment_method = str(normalised.get("payment_method") or "").lower()
                _order_status   = str(normalised.get("status") or "").lower()

                # â”€â”€ order_notifications â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                # Fire for every new order regardless of payment method or status.
                # This is the universal order-confirmation trigger â€” the
                # order_notifications SmartAutomation sends the merchant's
                # chosen confirmation template (order summary, COD confirmation,
                # etc.) to the customer as soon as their order lands in Nahla.
                # The engine deduplicates on (event_id, automation_id) so even
                # if a future webhook update re-emits, only one execution fires.
                emit_automation_event(
                    self.db,
                    self.tenant_id,
                    AutomationTrigger.ORDER_NOTIFICATIONS.value,
                    customer_id=customer.id if customer else None,
                    payload={
                        "external_id":           ext_id,
                        "order_id":              order_row.id,
                        "order_internal_id":     order_row.id,
                        "status":                _order_status,
                        "total":                 normalised.get("total"),
                        "order_number":          normalised.get("external_order_number") or ext_id,
                        "external_order_number": normalised.get("external_order_number"),
                        "checkout_url":          normalised.get("checkout_url") or "",
                        "payment_url":           normalised.get("checkout_url") or "",
                        "payment_method":        _payment_method,
                        "source":                "store_sync.order_webhook",
                    },
                    commit=True,
                )
                logger.info(
                    "[StoreSync] order_notifications event emitted tenant=%s order=%s status=%s payment=%s",
                    self.tenant_id, ext_id, _order_status, _payment_method or "unknown",
                )

                # â”€â”€ order_created (legacy / backward compat) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                # Keep emitting order_created so any custom automations wired
                # to that event string continue to work.
                emit_automation_event(
                    self.db,
                    self.tenant_id,
                    "order_created",
                    customer_id=customer.id if customer else None,
                    payload={
                        "external_id":           ext_id,
                        "order_id":              order_row.id,
                        "status":                _order_status,
                        "total":                 normalised.get("total"),
                        "order_number":          normalised.get("external_order_number") or ext_id,
                        "external_order_number": normalised.get("external_order_number"),
                        "checkout_url":          normalised.get("checkout_url") or "",
                        "payment_url":           normalised.get("checkout_url") or "",
                        "payment_method":        _payment_method,
                    },
                    commit=True,
                )

                # â”€â”€ COD-specific: also emit order_cod_pending â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                # When the payment method is COD AND the order has arrived in a
                # confirmed/active state (in_progress, under_review, etc.), emit
                # order_cod_pending so the dedicated cod_confirmation automation
                # fires alongside the general order_notifications one.  The COD
                # template typically includes a QUICK_REPLY button for the
                # customer to confirm receipt intention.
                _COD_METHODS = {"cod", "cash_on_delivery", "cash", "ط§ظ„ط¯ظپط¹ ط¹ظ†ط¯ ط§ظ„ط§ط³طھظ„ط§ظ…"}
                _is_cod = bool(
                    _payment_method
                    and any(
                        _payment_method == m or m in _payment_method
                        for m in _COD_METHODS
                    )
                )
                _COD_ACTIVE_STATUSES = {
                    "in_progress", "under_review", "in_review",
                    "pending_confirmation", "awaiting_confirmation",
                    "pending", "new",
                }
                meta = dict(order_row.extra_metadata or {})
                if _is_cod and not meta.get("cod_webhook_triggered"):
                    emit_automation_event(
                        self.db,
                        self.tenant_id,
                        AutomationTrigger.ORDER_COD_PENDING.value,
                        customer_id=customer.id if customer else None,
                        payload={
                            "external_id":           ext_id,
                            "order_id":              order_row.id,
                            "order_internal_id":     order_row.id,
                            "order_number":          normalised.get("external_order_number") or ext_id,
                            "external_order_number": normalised.get("external_order_number"),
                            "status":                _order_status,
                            "total":                 normalised.get("total"),
                            "payment_method":        _payment_method,
                            "checkout_url":          normalised.get("checkout_url") or "",
                            "payment_url":           normalised.get("checkout_url") or "",
                            "source":                "store_sync.cod_webhook",
                            "step_idx":              0,
                            "message_type":          "initial_confirmation",
                        },
                        commit=False,
                    )
                    meta["cod_webhook_triggered"] = True
                    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                    order_row.extra_metadata = meta
                    flag_modified(order_row, "extra_metadata")
                    self.db.commit()
                    logger.info(
                        "[StoreSync] order_cod_pending event emitted tenant=%s order=%s",
                        self.tenant_id, ext_id,
                    )

            except Exception as exc:
                # Automation failures are logged at ERROR so they are visible,
                # but do not fail the whole webhook â€” the order is already
                # durably stored and the dispatcher will retry this webhook
                # if we re-raise, potentially double-inserting automation rows.
                logger.exception(
                    "[StoreSync] order notification emit failed tenant=%s order=%s: %s",
                    self.tenant_id, getattr(order_row, "id", ext_id), exc,
                )

        # â”€â”€ P0: Cancel any in-flight abandoned-cart recovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # The single worst UX bug the cart-recovery flow can ship is a
        # "you forgot your cart" reminder that arrives AFTER the customer
        # has already paid. We have three layers of defence
        # (pre-send conversion-layer guard, sweeper guard, and this hook)
        # but the event-driven cancellation here is what kills any
        # already-queued future-dated AutomationEvent before it ever
        # reaches the engine â€” which the other two guards cannot do for
        # rescheduled events that have a future ``created_at``.
        #
        # Triggered on every webhook that lands a "real purchase" status
        # (anything not in the cancelled / pre-payment exclusion set).
        # We cover both the new-order case AND the case where Salla
        # only fires order.payment.updated after a previously-pending
        # order is paid (covered because we re-run on every webhook).
        try:
            from services.cart_recovery_cancel import (  # noqa: PLC0415
                cancel_recovery_for_customer,
                order_is_a_purchase,
            )
            current_status = normalised.get("status")
            if (
                customer
                and customer.id
                and order_is_a_purchase(current_status)
            ):
                # Re-derive prev_status here for the existing-order path
                # so we only cancel on a transition INTO a paid state
                # (avoids re-stamping every metadata refresh).
                prev_status_for_cancel = None
                if not is_new and order_row is not None:
                    prev_status_for_cancel = (order_row.extra_metadata or {}).get(
                        "prev_status"
                    )
                if is_new or not order_is_a_purchase(prev_status_for_cancel):
                    cancel_recovery_for_customer(
                        self.db,
                        tenant_id=self.tenant_id,
                        customer_id=customer.id,
                        reason="customer_purchased",
                        order_id=order_row.id if order_row else None,
                        order_external_id=ext_id,
                        order_status=current_status,
                        commit=True,
                    )
        except Exception as exc:
            logger.exception(
                "[StoreSync] cancel_recovery_for_customer failed tenant=%s "
                "customer=%s order=%s: %s",
                self.tenant_id,
                getattr(customer, "id", None),
                getattr(order_row, "id", None),
                exc,
            )

        # Emit order_shipped when the order transitions into a shipped/in-transit
        # status so that the shipping_update automation can fire and send a
        # tracking-link message to the customer.
        _SHIPPED_STATUSES = {"shipped", "in_transit", "out_for_delivery", "delivering"}
        prev_status = None
        if order_row is not None and not is_new:
            # The status has already been overwritten on the row â€” check
            # extra_metadata for the previously recorded status.
            prev_status = (order_row.extra_metadata or {}).get("prev_status")
        if (
            normalised.get("status") in _SHIPPED_STATUSES
            and prev_status not in _SHIPPED_STATUSES
            and customer
        ):
            try:
                from core.automation_engine import emit_automation_event  # noqa: PLC0415
                # Build tracking URL: Salla sometimes sends it as shipping.tracking_link
                # in the raw payload; fall back to checkout_url for direct pay link.
                raw_payload = payload if isinstance(payload, dict) else {}
                shipping_info = raw_payload.get("shipping") or {}
                tracking_url = (
                    shipping_info.get("tracking_link")
                    or raw_payload.get("tracking_url")
                    or raw_payload.get("tracking_link")
                    or ""
                )
                emit_automation_event(
                    self.db,
                    self.tenant_id,
                    "order_shipped",
                    customer_id=customer.id,
                    payload={
                        "external_id":           ext_id,
                        "order_id":              order_row.id,
                        "order_number":          normalised.get("external_order_number") or ext_id,
                        "external_order_number": normalised.get("external_order_number"),
                        "status":                normalised.get("status"),
                        "total":                 normalised.get("total"),
                        "tracking_url":          tracking_url,
                        "checkout_url":          normalised.get("checkout_url") or "",
                    },
                    commit=True,
                )
                logger.info(
                    "[StoreSync] order_shipped event emitted tenant=%s order=%s tracking=%s",
                    self.tenant_id, ext_id, bool(tracking_url),
                )
            except Exception as exc:
                logger.exception(
                    "[StoreSync] emit order_shipped failed tenant=%s order=%s: %s",
                    self.tenant_id, order_row.id if order_row else ext_id, exc,
                )

        # Record current status in metadata so we can detect future transitions.
        if order_row is not None:
            meta = dict(order_row.extra_metadata or {})
            meta["prev_status"] = normalised.get("status")
            order_row.extra_metadata = meta
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()

            await _handle_external_lifecycle_transition_best_effort(
                self,
                order=order_row,
                provider=webhook_source,
                raw_previous_status=lifecycle_prev_status,
                raw_current_status=normalised["status"],
                normalized_order=normalised,
                raw_payload=payload if isinstance(payload, dict) else None,
            )

            # Close the offer-decision attribution loop. We do this on every
            # *new* order â€” not only on `order_paid` â€” because Salla orders
            # frequently arrive already paid, and waiting for `order_paid`
            # would miss them. Idempotent on re-runs.
            try:
                from services.offer_attribution_service import (  # noqa: PLC0415
                    attribute_order_to_decision,
                )
                attribute_order_to_decision(
                    self.db,
                    tenant_id=self.tenant_id,
                    order_id=order_row.id,
                    payload={
                        "total":  normalised.get("total"),
                        "status": normalised.get("status"),
                    },
                )
            except Exception as exc:
                logger.debug(
                    "[StoreSync] offer attribution failed tenant=%s order=%s: %s",
                    self.tenant_id, order_row.id, exc,
                )

        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if snap:
            snap.order_count              = (
                self.db.query(Order).filter_by(tenant_id=self.tenant_id).count()
            )
            snap.last_incremental_sync_at = datetime.now(timezone.utc)
            snap.updated_at               = datetime.now(timezone.utc)
            self.db.commit()

    def _update_customer_profile_from_order(self, normalised: Dict) -> None:
        """Backward-compatible wrapper around the unified customer intelligence service."""
        customer = self._customer_intelligence.upsert_customer_from_order(
            normalised,
            source="order_incremental",
            commit=False,
        )
        if customer:
            self._customer_intelligence.recompute_profile_for_customer(
                customer.id,
                reason="order_incremental",
                commit=True,
                emit_event=True,
            )

    # â”€â”€ Incremental customer update (called by webhook) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def handle_customer_webhook(
        self,
        payload: Dict,
        *,
        integration_connection_id: int | None = None,
    ) -> None:
        """Process a single customer create/update from a platform webhook."""
        ext_id = str(payload.get("id", ""))
        if not ext_id:
            return

        name = (payload.get("first_name", "") + " " + payload.get("last_name", "")).strip()
        if not name:
            name = payload.get("name", "")
        email = payload.get("email", "")
        phone = _normalize_phone(payload.get("mobile", payload.get("phone", "")))

        existing = self._customer_intelligence.upsert_customer_identity(
            phone=phone,
            name=name,
            email=email,
            external_id=ext_id,
            source="customer_webhook",
            extra_metadata=payload.get("metadata", {}) or {},
            seen_at=datetime.now(timezone.utc),
        )
        if existing and existing.id:
            self._customer_intelligence.recompute_profile_for_customer(
                existing.id,
                reason="customer_webhook",
                commit=True,
                emit_event=True,
            )
        else:
            self.db.commit()

        effective_conn = (
            integration_connection_id
            if integration_connection_id is not None
            else self._integration_connection_id
        )
        if effective_conn is not None:
            self._upsert_a1_external_profile_side_effect(
                payload,
                integration_connection_id=int(effective_conn),
            )
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()

        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if snap:
            snap.customer_count = (
                self.db.query(Customer).filter_by(tenant_id=self.tenant_id).count()
            )
            snap.last_incremental_sync_at = datetime.now(timezone.utc)
            snap.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    # â”€â”€ Product deletion (called by webhook) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def handle_product_deleted(self, external_id: str) -> None:
        """Remove a product that was deleted in the store."""
        if not external_id:
            return
        deleted = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id, external_id=external_id)
            .delete()
        )
        if deleted:
            self.db.commit()
            snap = (
                self.db.query(StoreKnowledgeSnapshot)
                .filter_by(tenant_id=self.tenant_id)
                .first()
            )
            if snap:
                snap.product_count            = (
                    self.db.query(Product).filter_by(tenant_id=self.tenant_id).count()
                )
                snap.last_incremental_sync_at = datetime.now(timezone.utc)
                snap.updated_at               = datetime.now(timezone.utc)
                self.db.commit()
            logger.info("tenant=%s product deleted | external_id=%s", self.tenant_id, external_id)

    # â”€â”€ Status â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _compute_live_totals(self) -> Dict:
        """
        Live counts straight from the source-of-truth tables.

        Why this exists: the snapshot columns (``snap.product_count``,
        ``snap.order_count``, ``snap.coupon_count``, ``snap.category_count``)
        are written by ``_rebuild_snapshot`` from the **delta** of the most
        recent sync run (``created + updated``), NOT the live totals. When
        an incremental sync returns 0 rows (e.g. Salla 401, empty window,
        rate-limit), those columns are silently overwritten with 0 even
        though the real ``orders`` / ``coupons`` tables still hold the
        previously-synced data. The Connection screen then displays
        misleading zeros while the dedicated /orders and /coupons pages
        show the correct figures. To fix the inconsistency we always
        report counts derived from the same tables those pages read from.
        """
        now = datetime.now(timezone.utc)

        product_count  = (
            self.db.query(Product).filter_by(tenant_id=self.tenant_id).count()
        )
        order_count    = (
            self.db.query(Order).filter_by(tenant_id=self.tenant_id).count()
        )
        customer_count = (
            self.db.query(Customer).filter_by(tenant_id=self.tenant_id).count()
        )

        # Coupons: we want both "all stored" (matches the /coupons list page)
        # and "active right now" (what the merchant most likely cares about
        # when they look at their integration health card). We compute
        # `active` in Python so we honour the optional
        # ``extra_metadata.active`` override the same way ``GET /coupons``
        # does â€” keeping the two surfaces consistent.
        coupon_rows = (
            self.db.query(Coupon).filter_by(tenant_id=self.tenant_id).all()
        )
        total_coupons = len(coupon_rows)
        active_coupons = 0
        for coupon in coupon_rows:
            meta = coupon.extra_metadata or {}
            override = meta.get("active")
            if isinstance(override, bool):
                if override:
                    active_coupons += 1
                continue
            expires = coupon.expires_at
            if expires is None:
                active_coupons += 1
                continue
            if getattr(expires, "tzinfo", None) is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires > now:
                active_coupons += 1

        # Categories: distinct, non-empty values from product metadata.
        # We previously sampled only the first 50 products, which is what
        # made categories show 0 for many catalogues. Now we scan ALL
        # products for this tenant; with a typical catalogue size this is
        # well below 1ms and is far less misleading.
        category_rows = (
            self.db.query(Product.extra_metadata)
            .filter(Product.tenant_id == self.tenant_id)
            .all()
        )
        categories: set[str] = set()
        for (meta,) in category_rows:
            if not isinstance(meta, dict):
                continue
            cat = meta.get("category")
            if isinstance(cat, str) and cat.strip():
                categories.add(cat.strip())
            elif isinstance(cat, list):
                for c in cat:
                    if isinstance(c, str) and c.strip():
                        categories.add(c.strip())

        return {
            "product_count":   product_count,
            "order_count":     order_count,
            "coupon_count":    active_coupons,
            "coupon_total":    total_coupons,
            "category_count":  len(categories),
            "customer_count":  customer_count,
        }

    def get_status(self, period: str = "today") -> Dict:
        """Return current sync status + real-time dashboard KPIs.

        ``period`` is forwarded to :meth:`_compute_dashboard_kpis` so the
        Overview page can pivot all four KPI cards + recent lists between
        ``today`` / ``last_7_days`` / ``this_month`` from one query param
        without each card hitting its own endpoint. Default stays
        ``"today"`` so existing callers see no behavioural change.
        """
        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        last_job = (
            self.db.query(StoreSyncJob)
            .filter_by(tenant_id=self.tenant_id)
            .order_by(StoreSyncJob.id.desc())
            .first()
        )
        # Auto-expire stale "running" jobs so the UI never gets stuck forever.
        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=_SYNC_JOB_TIMEOUT_MINUTES)
        stale_jobs = (
            self.db.query(StoreSyncJob)
            .filter(
                StoreSyncJob.tenant_id == self.tenant_id,
                StoreSyncJob.status    == "running",
                StoreSyncJob.created_at < stale_cutoff,
            )
            .all()
        )
        for stale in stale_jobs:
            stale.status        = "timed_out"
            stale.error_message = (
                f"طھط¬ط§ظˆط² ط§ظ„ط­ط¯ ط§ظ„ط²ظ…ظ†ظٹ ({_SYNC_JOB_TIMEOUT_MINUTES} ط¯ظ‚ظٹظ‚ط©). "
                "ظ‚ط¯ ظٹظƒظˆظ† ط§ظ„ط®ط§ط¯ظ… ط£ظڈط¹ظٹط¯ طھط´ط؛ظٹظ„ظ‡ ط£ط«ظ†ط§ط، ط§ظ„ظ…ط²ط§ظ…ظ†ط©."
            )
            stale.completed_at  = datetime.now(timezone.utc)
        if stale_jobs:
            self.db.commit()

        running_job = (
            self.db.query(StoreSyncJob)
            .filter_by(tenant_id=self.tenant_id, status="running")
            .first()
        )

        # â”€â”€ Real-time dashboard KPIs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # These are shown in the Overview page (revenue, orders, conversations).
        # We reuse the same date-extraction logic as the orders router so the
        # numbers match exactly what the merchant sees in /orders.
        dashboard_kpis = self._compute_dashboard_kpis(period=period)

        # Source of truth: live counts from the same tables that /orders,
        # /coupons and /products read from. The snapshot deltas are kept
        # alongside (under ``snapshot_*``) for debugging / observability.
        live = self._compute_live_totals()

        return {
            "has_snapshot":           snap is not None,
            "product_count":          live["product_count"],
            "category_count":         live["category_count"],
            "order_count":            live["order_count"],
            "coupon_count":           live["coupon_count"],
            "coupon_total":           live["coupon_total"],
            "customer_count":         live["customer_count"],
            # Last-sync deltas (NOT totals) â€” useful to know how the most
            # recent sync run performed. Frontends should not display these
            # as "your store has X items"; use the live counts above.
            "snapshot_product_count": snap.product_count  if snap else 0,
            "snapshot_order_count":   snap.order_count    if snap else 0,
            "snapshot_coupon_count":  snap.coupon_count   if snap else 0,
            "snapshot_category_count": snap.category_count if snap else 0,
            "last_full_sync_at":      snap.last_full_sync_at.isoformat() if (snap and snap.last_full_sync_at) else None,
            "last_incremental_sync_at": snap.last_incremental_sync_at.isoformat() if (snap and snap.last_incremental_sync_at) else None,
            "sync_version":           snap.sync_version   if snap else 0,
            "sync_running":           running_job is not None,
            "last_job_status":        last_job.status     if last_job else None,
            "last_job_id":            last_job.id         if last_job else None,
            "last_job_error":         last_job.error_message if last_job else None,
            **dashboard_kpis,
        }

    def _compute_dashboard_kpis(self, period: str = "today") -> Dict:
        """Overview KPIs — owned by `core.merchant_overview_analytics`."""
        from core.merchant_overview_analytics import compute_overview_kpis  # noqa: PLC0415

        return compute_overview_kpis(self.db, self.tenant_id, period)
