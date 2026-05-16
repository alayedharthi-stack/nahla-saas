"""
services/meta_catalog_import.py
──────────────────────────────
Pull merchant products FROM the Meta Commerce Manager catalog INTO
the Nahla Product Catalog.

Hub architecture (May 2026 #14)
───────────────────────────────
This module implements ONE leg of the catalog Hub:

    META CATALOG  ──→  NAHLA CATALOG   (this module, source = "meta")
    SALLA SYNC    ──→  NAHLA CATALOG   (integrations/salla/sync/products.py)
    MANUAL ENTRY  ──→  NAHLA CATALOG   (routers/catalog.py manual CRUD)

The opposite direction — pushing Nahla products OUT to Meta — lives in
``services/meta_catalog_export.py`` (planning-only today; see that
module for the contract).

Why import from Meta
────────────────────
A non-trivial fraction of our existing merchants pre-onboarded their
products into Meta Commerce Manager (often via Salla's auto-publish,
or directly through the Meta UI). When they connect Nahla we want to
adopt that catalog as the seed for Nahla's local catalog so:

  • the WhatsApp AI ``[PRODUCT:...]`` resolver immediately has data
    to read (it queries Nahla's ``products`` table — never Meta or
    Salla directly — by contract);
  • campaign builders can pick from a populated catalog from day one;
  • future channels (Google Merchant, checkout) inherit the same
    pre-curated set.

Import contract
───────────────
* Idempotent: rows are matched by ``meta_retailer_id`` first, then by
  ``external_id``. Re-running the import refreshes title / description
  / price / image without creating duplicates.
* Never overwrites rows whose ``source`` is ``"manual"`` — a manual
  product is the merchant's intentional, hand-curated entry and the
  Meta import must not silently wipe it. Such rows are reported as
  ``skipped_manual`` in the result summary so the merchant sees them.
* Soft-fails on individual rows: malformed Meta payloads (missing
  retailer_id, etc.) are logged + counted in ``errors`` but don't
  abort the run. The whole-import-fails-on-one-row pattern is
  hostile to support; the partial-success report is far more useful.
* Bounded: one call to this service fetches at most
  ``MAX_PAGES`` × ``PAGE_SIZE`` (= 5 × 100 = 500) products. Larger
  catalogs are paginated through ``paging.next`` URLs but capped at
  the safety budget so a runaway response can't tie up the worker.

Auth + endpoints
────────────────
We reuse the SAME credentials the WhatsApp send chain already has:

  • ``WhatsAppConnection.access_token`` — long-lived system user
    token granted during embedded-signup. Carries the
    ``catalog_management`` scope when the merchant opted into it.
  • ``WhatsAppConnection.meta_catalog_id`` — the Commerce Manager
    catalog identifier the merchant pasted on the catalog page.

Meta Graph API:
    GET https://graph.facebook.com/{v}/{catalog_id}/products
        ?fields=id,retailer_id,name,description,price,currency,url,
                image_url,availability,inventory
        &limit={PAGE_SIZE}
        &access_token={TOKEN}

Failure modes (raised as ``MetaCatalogImportError``):
    * connection_not_found       — no WhatsAppConnection row.
    * catalog_id_missing         — connection has no meta_catalog_id.
    * access_token_missing       — connection has no token (Coexistence
                                   with no Meta auth on file).
    * meta_http_error             — non-2xx on the first page (page
                                   2+ errors are soft-failed).
"""
from __future__ import annotations

import logging
import re
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from core.catalog import SOURCE_MANUAL, SOURCE_META, assign_canonical_retailer_id
from core.config import META_GRAPH_API_VERSION
from models import Product, WhatsAppConnection

logger = logging.getLogger("nahla.meta_catalog_import")


# ─────────────────────────────────────────────────────────────────────────────
# Temporary observability (May 2026 #19d)
# ─────────────────────────────────────────────────────────────────────────────
#
# Production symptom: ``POST /merchant/catalog/import/meta`` returns
# ``502`` with an empty body — the FastAPI layer only sees a
# ``MetaCatalogImportError`` with no upstream detail attached, so the
# merchant can't tell whether it's a token problem, a wrong catalog_id,
# a Meta rate-limit, or an httpx-level timeout.
#
# This block adds STRUCTURED log lines on every leg of the Graph API
# round-trip:
#
#   [META_IMPORT][REQ]   page=N catalog_id=… graph_url=… token_len=…
#   [META_IMPORT][RESP]  page=N status=… elapsed_ms=… body_preview=…
#   [META_IMPORT][EXC]   page=N exc_class=… exc_msg=…  ← logger.exception
#
# Plus we now ALWAYS stash the Meta response body (truncated to 2000
# chars) and the exception class onto ``MetaCatalogImportError.detail``
# so the 502 body the dashboard receives is actionable.
#
# Once the production root cause is identified and fixed, the
# verbose [REQ] / [RESP] lines can drop back to DEBUG level — the
# [EXC] line + .detail payload stay (they're cheap and only fire on
# failure).
_RESP_PREVIEW_CAP = 2000


def _mask_token(token: str) -> str:
    """Token tail-mask so log dumps never contain a full access token.
    A 4-char tail is enough to correlate two log lines that share the
    same credential without leaking it to Datadog / Railway / etc."""
    if not token:
        return "<empty>"
    if len(token) <= 6:
        return f"<len={len(token)}>"
    return f"…{token[-4:]} (len={len(token)})"


def _mask_url(url: str) -> str:
    """Strip ``access_token=…`` from a graph URL for safe logging.
    Meta echoes the full URL in paging.next, so without scrubbing
    the token would land in logs verbatim."""
    if not url:
        return ""
    return re.sub(r"access_token=[^&]+", "access_token=<masked>", url)


# ─────────────────────────────────────────────────────────────────────────────
# Tunables — pinned constants, not env-vars, so behaviour stays deterministic
# across deploys and tests can lock to specific numbers without monkey-patching
# the environment.
# ─────────────────────────────────────────────────────────────────────────────

PAGE_SIZE: int          = 100   # Meta hard-caps at 100 per page anyway
MAX_PAGES: int          = 5     # safety budget — 500 products / call
REQUEST_TIMEOUT: float  = 30.0  # seconds — Meta is usually <3s
META_CATALOG_FIELDS = ",".join([
    "id",
    "retailer_id",
    "name",
    "description",
    "price",
    "currency",
    "url",
    "image_url",
    "availability",
    "inventory",
])


# ─────────────────────────────────────────────────────────────────────────────
# Result + error types
# ─────────────────────────────────────────────────────────────────────────────

class MetaCatalogImportError(RuntimeError):
    """Structured error so the FastAPI layer can map ``.code`` to an
    HTTP status without parsing free-form messages.

    Codes (closed set, mapped 1:1 in the router):

    * ``connection_not_found`` → 404
    * ``catalog_id_missing``   → 400 (merchant must wire Meta first)
    * ``access_token_missing`` → 400 (token not present)
    * ``meta_http_error``      → 502 (upstream failure)
    """
    def __init__(self, code: str, message: str = "", *, detail: Any = None):
        super().__init__(message or code)
        self.code: str = code
        self.detail: Any = detail


@dataclass
class ImportReport:
    """Outcome of a single import call.

    Counts are mutually exclusive — every row Meta returned ends up in
    exactly one bucket (created / updated / skipped_manual / errors).
    The merchant-facing UI shows these as a one-line summary; support
    drills into ``error_samples`` for the first 10 failing rows.
    """
    scanned:         int = 0
    created:         int = 0
    updated:         int = 0
    skipped_manual:  int = 0
    errors:          int = 0
    pages_fetched:   int = 0
    truncated:       bool = False    # True when MAX_PAGES hit + Meta had more
    error_samples:   List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanned":         self.scanned,
            "created":         self.created,
            "updated":         self.updated,
            "skipped_manual":  self.skipped_manual,
            "errors":          self.errors,
            "pages_fetched":   self.pages_fetched,
            "truncated":       self.truncated,
            "error_samples":   self.error_samples[:10],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def import_from_meta(db: Session, tenant_id: int) -> ImportReport:
    """Import products from the merchant's Meta Catalog into the
    Nahla catalog. See module docstring for full contract.

    Returns an :class:`ImportReport`. Raises
    :class:`MetaCatalogImportError` only on hard preflight failures
    (missing credentials / 4xx on first page); page-level upserts
    soft-fail into ``report.errors``.

    Designed to be called from a FastAPI endpoint (synchronously — Meta
    Catalog API is fast enough that the typical import finishes inside
    a single HTTP request) and from a future scheduled "auto-resync"
    job.
    """
    # First-line entry log so a single Railway grep on
    # ``[META_IMPORT][START]`` confirms the request reached the
    # service layer at all — separate from the ``[META_IMPORT][REQ]``
    # log that fires per HTTP page. Useful when the preflight
    # rejects the call (no token / no catalog_id) and we want
    # proof the function actually ran.
    logger.info(
        "[META_IMPORT][START] tenant=%s graph_api_version=%s",
        tenant_id, META_GRAPH_API_VERSION,
    )

    conn = (
        db.query(WhatsAppConnection)
          .filter(WhatsAppConnection.tenant_id == tenant_id)
          .first()
    )
    if conn is None:
        raise MetaCatalogImportError(
            "connection_not_found",
            "No WhatsApp connection on file for this tenant.",
        )
    catalog_id = (conn.meta_catalog_id or "").strip()
    if not catalog_id:
        raise MetaCatalogImportError(
            "catalog_id_missing",
            "Meta Catalog ID is not configured on the WhatsApp connection.",
            detail={"hint": "WhatsAppConnection.meta_catalog_id is empty"},
        )
    token = (conn.access_token or "").strip()
    if not token:
        raise MetaCatalogImportError(
            "access_token_missing",
            "Meta access token is not present on the connection.",
            detail={"hint": "WhatsAppConnection.access_token is empty"},
        )

    report = ImportReport()
    next_url: Optional[str] = (
        f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{catalog_id}/products"
    )
    # First page is built explicitly; subsequent pages come back with a
    # ready-to-call ``paging.next`` URL from Meta that already carries
    # the cursor + the fields + the access token.
    first_params: Optional[Dict[str, str]] = {
        "fields":       META_CATALOG_FIELDS,
        "limit":        str(PAGE_SIZE),
        "access_token": token,
    }

    logger.info(
        "[META_IMPORT][READY] tenant=%s catalog_id=%s graph_api_version=%s "
        "token=%s page_size=%d max_pages=%d timeout=%.1fs",
        tenant_id, catalog_id, META_GRAPH_API_VERSION,
        _mask_token(token), PAGE_SIZE, MAX_PAGES, REQUEST_TIMEOUT,
    )

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        for page_idx in range(MAX_PAGES):
            if next_url is None:
                break

            # Build a logging-friendly view of the URL we're about
            # to hit. For the first page ``first_params`` carries
            # the access_token separately; for subsequent pages
            # Meta echoes the token inside ``next_url`` directly,
            # which is why ``_mask_url`` exists.
            log_url = _mask_url(next_url)
            logger.info(
                "[META_IMPORT][REQ] page=%d tenant=%s catalog_id=%s "
                "graph_url=%s has_first_params=%s token=%s",
                page_idx, tenant_id, catalog_id, log_url,
                first_params is not None, _mask_token(token),
            )

            _t0 = time.perf_counter()
            try:
                resp = client.get(next_url, params=first_params)
            except httpx.HTTPError as exc:
                _elapsed_ms = (time.perf_counter() - _t0) * 1000.0
                # httpx transport error — DNS / TLS / timeout /
                # connection-reset. Always logged with full
                # traceback so Railway captures the underlying
                # urllib3 / ssl error class.
                logger.exception(
                    "[META_IMPORT][EXC] page=%d tenant=%s catalog_id=%s "
                    "transport_error exc_class=%s elapsed_ms=%.0f graph_url=%s",
                    page_idx, tenant_id, catalog_id,
                    exc.__class__.__name__, _elapsed_ms, log_url,
                )
                if page_idx == 0:
                    raise MetaCatalogImportError(
                        "meta_http_error",
                        f"Meta Catalog transport error: "
                        f"{exc.__class__.__name__}: {exc}",
                        detail={
                            "stage":        "transport",
                            "page":         page_idx,
                            "exc_class":    exc.__class__.__name__,
                            "exc_msg":      str(exc)[:_RESP_PREVIEW_CAP],
                            "graph_url":    log_url,
                            "catalog_id":   catalog_id,
                            "elapsed_ms":   round(_elapsed_ms, 1),
                        },
                    ) from exc
                logger.warning(
                    "meta_catalog_import: page %d transport error: %s",
                    page_idx, exc,
                )
                break
            except Exception as exc:  # noqa: BLE001
                # Anything that ISN'T an ``httpx.HTTPError`` — e.g.
                # an OSError leaked from a third-party DNS resolver,
                # a tracing-layer monkey-patch raising. Used to
                # bubble up unhandled and FastAPI would render an
                # empty 502 with no body. Now we always wrap it as
                # ``MetaCatalogImportError`` carrying the exception
                # class / message in ``detail`` so the dashboard
                # sees what really happened.
                _elapsed_ms = (time.perf_counter() - _t0) * 1000.0
                logger.exception(
                    "[META_IMPORT][EXC] page=%d tenant=%s catalog_id=%s "
                    "unexpected_error exc_class=%s elapsed_ms=%.0f graph_url=%s",
                    page_idx, tenant_id, catalog_id,
                    exc.__class__.__name__, _elapsed_ms, log_url,
                )
                raise MetaCatalogImportError(
                    "meta_http_error",
                    f"Meta Catalog request raised "
                    f"{exc.__class__.__name__}: {exc}",
                    detail={
                        "stage":        "unexpected",
                        "page":         page_idx,
                        "exc_class":    exc.__class__.__name__,
                        "exc_msg":      str(exc)[:_RESP_PREVIEW_CAP],
                        "graph_url":    log_url,
                        "catalog_id":   catalog_id,
                        "elapsed_ms":   round(_elapsed_ms, 1),
                        "traceback":    traceback.format_exc()[-_RESP_PREVIEW_CAP:],
                    },
                ) from exc

            first_params = None  # only applied on the first call
            _elapsed_ms = (time.perf_counter() - _t0) * 1000.0

            # Always log the response shape — gives us status_code +
            # body preview (truncated to ``_RESP_PREVIEW_CAP``) for
            # every page, success or failure. Once the bug is fixed
            # this line drops to DEBUG; keeping it at INFO during
            # the diagnostic window so a single ``[META_IMPORT]``
            # grep in Railway shows the full round-trip.
            try:
                _body_preview = (resp.text or "")[:_RESP_PREVIEW_CAP]
            except Exception:  # noqa: BLE001
                _body_preview = "<text_unavailable>"
            logger.info(
                "[META_IMPORT][RESP] page=%d tenant=%s catalog_id=%s "
                "status=%d elapsed_ms=%.0f body_len=%d body_preview=%r",
                page_idx, tenant_id, catalog_id,
                resp.status_code, _elapsed_ms,
                len(resp.content or b""),
                _body_preview,
            )

            if resp.status_code >= 400:
                # Try to parse Meta's structured error envelope —
                # they return ``{"error": {"message": "...",
                # "type": "...", "code": N, "error_subcode": N,
                # "fbtrace_id": "..."}}`` which is far more useful
                # than the raw text dump.
                _meta_err: Dict[str, Any] = {}
                try:
                    _parsed = resp.json() or {}
                    if isinstance(_parsed, dict) and isinstance(_parsed.get("error"), dict):
                        _meta_err = _parsed["error"]
                except Exception:  # noqa: BLE001
                    _meta_err = {}

                logger.error(
                    "[META_IMPORT][HTTP_ERROR] page=%d tenant=%s catalog_id=%s "
                    "status=%d meta_code=%s meta_subcode=%s meta_type=%s "
                    "meta_message=%r fbtrace_id=%s",
                    page_idx, tenant_id, catalog_id, resp.status_code,
                    _meta_err.get("code"),
                    _meta_err.get("error_subcode"),
                    _meta_err.get("type"),
                    str(_meta_err.get("message") or "")[:_RESP_PREVIEW_CAP],
                    _meta_err.get("fbtrace_id"),
                )

                if page_idx == 0:
                    raise MetaCatalogImportError(
                        "meta_http_error",
                        f"Meta returned {resp.status_code}: "
                        f"{_body_preview}",
                        detail={
                            "stage":          "http_error",
                            "status":         resp.status_code,
                            "page":           page_idx,
                            "graph_url":      log_url,
                            "catalog_id":     catalog_id,
                            "elapsed_ms":     round(_elapsed_ms, 1),
                            "body_preview":   _body_preview,
                            "meta_code":      _meta_err.get("code"),
                            "meta_subcode":   _meta_err.get("error_subcode"),
                            "meta_type":      _meta_err.get("type"),
                            "meta_message":   str(_meta_err.get("message") or "")[:_RESP_PREVIEW_CAP],
                            "fbtrace_id":     _meta_err.get("fbtrace_id"),
                        },
                    )
                logger.warning(
                    "meta_catalog_import: page %d HTTP %d body=%s",
                    page_idx, resp.status_code, _body_preview,
                )
                break

            try:
                body = resp.json() or {}
            except Exception as exc:  # noqa: BLE001
                # 2xx response but body isn't JSON — very unusual
                # for Graph API. Log + abort.
                logger.exception(
                    "[META_IMPORT][EXC] page=%d tenant=%s catalog_id=%s "
                    "json_decode_error exc_class=%s body_preview=%r",
                    page_idx, tenant_id, catalog_id,
                    exc.__class__.__name__, _body_preview,
                )
                raise MetaCatalogImportError(
                    "meta_http_error",
                    f"Meta returned non-JSON body: "
                    f"{exc.__class__.__name__}: {exc}",
                    detail={
                        "stage":        "json_decode",
                        "page":         page_idx,
                        "exc_class":    exc.__class__.__name__,
                        "body_preview": _body_preview,
                        "catalog_id":   catalog_id,
                    },
                ) from exc

            page_rows = body.get("data") or []
            report.pages_fetched += 1
            logger.info(
                "[META_IMPORT][PAGE_OK] page=%d tenant=%s catalog_id=%s "
                "row_count=%d total_scanned_before=%d",
                page_idx, tenant_id, catalog_id,
                len(page_rows), report.scanned,
            )
            for row in page_rows:
                _process_one_meta_product(db, tenant_id, row, report)
            # Commit each page so a later page failure doesn't roll
            # back earlier progress — the merchant sees partial success
            # rather than nothing.
            try:
                db.commit()
            except Exception as commit_exc:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "[META_IMPORT][EXC] page=%d tenant=%s catalog_id=%s "
                    "per_page_commit_failed exc_class=%s",
                    page_idx, tenant_id, catalog_id,
                    commit_exc.__class__.__name__,
                )

            paging = body.get("paging") or {}
            next_url = paging.get("next")
            if next_url is None:
                break

        if next_url is not None:
            report.truncated = True

    logger.info(
        "meta_catalog_import: tenant=%s scanned=%d created=%d updated=%d "
        "skipped_manual=%d errors=%d truncated=%s",
        tenant_id, report.scanned, report.created, report.updated,
        report.skipped_manual, report.errors, report.truncated,
    )
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Per-product upsert
# ─────────────────────────────────────────────────────────────────────────────

# Meta's ``price`` field is returned as ``"19.99 USD"`` (or similar
# locale-aware string with the ISO code appended). We keep the full
# string in ``Product.price`` for display fidelity, but try to extract
# the numeric portion + currency for the JSONB metadata so the catalog
# UI can render structured prices later.
_PRICE_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*([A-Za-z]{3})?")


def _parse_meta_price(raw: Any) -> Dict[str, Any]:
    """Normalise Meta's price string into ``{value, currency}``.

    Resilient to:
      * trailing whitespace / extra symbols (``"199.00 SAR"`` →
        value=199.00 currency=SAR);
      * pure-numeric strings without currency (carried over as
        currency=None);
      * dicts (some Meta SKUs return ``{"amount": "19.99", "currency": "USD"}``).
    """
    if raw is None:
        return {"value": None, "currency": None, "raw": ""}
    if isinstance(raw, dict):
        return {
            "value":    raw.get("amount") or raw.get("value"),
            "currency": (raw.get("currency") or "").upper() or None,
            "raw":      str(raw),
        }
    s = str(raw).strip()
    m = _PRICE_RE.search(s)
    if m:
        return {
            "value":    m.group(1).replace(",", "."),
            "currency": (m.group(2) or "").upper() or None,
            "raw":      s,
        }
    return {"value": None, "currency": None, "raw": s}


def _process_one_meta_product(
    db: Session, tenant_id: int, row: Dict[str, Any], report: ImportReport,
) -> None:
    """Upsert one Meta-side product row into the Nahla catalog.

    Caller owns the commit boundary (we commit per-page, not per-row,
    so a bad row in the middle doesn't trigger a stray rollback that
    loses prior good rows).
    """
    report.scanned += 1
    try:
        retailer_id = (row.get("retailer_id") or "").strip()
        title       = (row.get("name") or "").strip()
        if not retailer_id or not title:
            report.errors += 1
            if len(report.error_samples) < 10:
                report.error_samples.append({
                    "id":          row.get("id"),
                    "retailer_id": row.get("retailer_id"),
                    "reason":      "missing_retailer_id_or_name",
                })
            return

        # Match by retailer_id → external_id (we use ``external_id`` to
        # store Meta's retailer_id when importing FROM Meta — same
        # column-space convention as the Salla writer, which keeps the
        # resolver / sender / dispatch path source-agnostic).
        existing = (
            db.query(Product)
              .filter(Product.tenant_id == tenant_id)
              .filter(
                  (Product.meta_retailer_id == retailer_id)
                  | (Product.external_id == retailer_id)
              )
              .first()
        )

        # Refuse to overwrite a manual row. Manual rows are the
        # merchant's intentional, hand-curated entries — if a Meta
        # import happens to match the same retailer_id (because the
        # merchant typed the same ID in both places) we surface a
        # ``skipped_manual`` counter rather than silently winning.
        if existing is not None and (existing.source or "").lower() == SOURCE_MANUAL:
            report.skipped_manual += 1
            return

        price_blob = _parse_meta_price(row.get("price"))
        currency   = price_blob["currency"] or (row.get("currency") or None)
        availability = (row.get("availability") or "").lower()
        in_stock = availability in {"", "in stock", "in_stock", "available"}
        meta_blob = {
            "source":       SOURCE_META,
            "meta_id":      row.get("id"),
            "image_url":    row.get("image_url") or None,
            "product_url":  row.get("url") or None,
            "currency":     currency,
            "availability": availability or None,
            "inventory":    row.get("inventory"),
        }
        # Drop empty values so the JSONB stays compact.
        meta_blob = {k: v for k, v in meta_blob.items() if v is not None}

        if existing is None:
            p = Product(
                tenant_id        = tenant_id,
                external_id      = retailer_id,
                meta_retailer_id = retailer_id,
                title            = title,
                description      = row.get("description") or None,
                price            = price_blob["raw"] or None,
                in_stock         = in_stock,
                extra_metadata   = meta_blob,
                source           = SOURCE_META,
            )
            db.add(p)
            db.flush()
            try:
                assign_canonical_retailer_id(p)
            except Exception:  # noqa: BLE001
                pass
            report.created += 1
        else:
            existing.title       = title
            existing.description = row.get("description") or existing.description
            existing.price       = price_blob["raw"] or existing.price
            existing.in_stock    = in_stock
            # Merge metadata instead of replacing so anything other
            # writers stamped (e.g. recommendation tags) survives.
            merged = dict(existing.extra_metadata or {})
            merged.update(meta_blob)
            existing.extra_metadata = merged
            # Stamp ``source`` so a row that was previously "unknown"
            # (legacy backfill heuristic) gets correctly tagged.
            existing.source = SOURCE_META
            # Make sure the retailer-id columns are populated — many
            # legacy rows have NULL ``meta_retailer_id``; an import is
            # exactly the moment to fill that in.
            if not (existing.meta_retailer_id or "").strip():
                existing.meta_retailer_id = retailer_id
            if not (existing.external_id or "").strip():
                existing.external_id = retailer_id
            report.updated += 1
    except Exception as exc:  # noqa: BLE001
        report.errors += 1
        if len(report.error_samples) < 10:
            report.error_samples.append({
                "id":          row.get("id"),
                "retailer_id": row.get("retailer_id"),
                "reason":      f"upsert_failed: {exc}",
            })
        # Don't rollback here — caller commits per-page. A single bad
        # row will be retried implicitly on the next import; we don't
        # want it to take down the rest of the page.


__all__ = [
    "ImportReport",
    "MetaCatalogImportError",
    "import_from_meta",
]
