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
* **Meta field mapping (stable contract for AI commerce + WhatsApp cards):**
  Meta Graph ``id`` is stored as ``Product.external_id`` (there is no
  separate ``meta_product_id`` column). Meta Graph ``retailer_id`` is
  stored as ``Product.meta_retailer_id``. Official WhatsApp product
  messages resolve the send SKU via :func:`core.catalog.effective_retailer_id`
  (``meta_retailer_id`` first, then ``external_id``). AI product lookup
  reads the same tenant-scoped ``products`` rows — never live Meta APIs.
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
The Catalog Graph API requires a Meta OAuth / system-user token —
NOT every credential on ``WhatsAppConnection`` qualifies. See
``_select_graph_token`` below for the full selection contract;
the short version is:

  1. ``WhatsAppConnection.access_token`` when
     ``WhatsAppConnection.provider == "meta"`` — long-lived
     system-user token granted during Embedded Signup. Carries
     the ``catalog_management`` scope when the merchant opted in.

  2. Platform-wide system user token (env ``WHATSAPP_TOKEN``) as
     a fallback for 360dialog / coexistence merchants whose
     ``conn.access_token`` is a ``D360-API-KEY`` — that field
     CANNOT authenticate against ``graph.facebook.com`` and is
     never sent there.

  3. ``WhatsAppConnection.meta_catalog_id`` — the Commerce Manager
     catalog identifier the merchant pasted on the catalog page.

Meta Graph API:
    GET https://graph.facebook.com/{v}/{catalog_id}/items
        ?fields=id,retailer_id,name,description,price,sale_price,
                currency,availability,url,image_url,brand,condition
        &limit={PAGE_SIZE}
        &access_token={TOKEN}

    Fallback (one attempt) when ``/items`` returns
    ``code=100 — nonexisting field``:
        GET https://graph.facebook.com/{v}/{catalog_id}/product_items
        (same fields)
    Logged as ``[META_IMPORT][ENDPOINT_FALLBACK]``.

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
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from sqlalchemy.orm import Session

from core.catalog import (
    CATALOG_STATUS_ACTIVE,
    CATALOG_STATUS_MERCHANT_HIDDEN,
    CATALOG_STATUS_REMOVED_FROM_META,
    CONFLICT_POSSIBLE_DUPLICATE,
    OWNERSHIP_META_READONLY,
    SOURCE_META,
    SOURCE_META_EXISTING,
    assign_canonical_retailer_id,
    catalog_status_of,
)
from core.catalog_write_router import (
    ACTION_CREATE,
    ACTION_FLAG_CONFLICT,
    ACTION_REFRESH_META,
    ACTION_SKIP_PROTECTED,
    conflict_detail_payload,
    resolve_meta_import_action,
)
from core.config import META_GRAPH_API_VERSION, WA_TOKEN
from models import Product, WhatsAppConnection
from services.whatsapp_platform.wa_connection_secrets import read_access_token

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

# Commerce catalogs expose rows under different edge names depending on
# catalog type / API version. Prefer ``/products`` (works for catalog
# 1430031051699225-style Commerce Manager catalogs), then ``/items``,
# then legacy ``/product_items``.
CATALOG_ITEM_EDGE_ORDER: Tuple[str, ...] = ("products", "items", "product_items")
META_CATALOG_EDGE_PRIMARY  = CATALOG_ITEM_EDGE_ORDER[0]
META_CATALOG_EDGE_FALLBACK = CATALOG_ITEM_EDGE_ORDER[1]

# Field set tuned for ``ProductItem`` objects on Commerce catalogs.
# ``sale_price`` / ``brand`` / ``condition`` weren't requested by the
# legacy ``/products`` query — adding them now means the merchant
# sees on-sale pricing, brand badges, and condition (new / used /
# refurbished) right after import, matching what Meta Commerce
# Manager shows in its own UI.
META_CATALOG_FIELDS = ",".join([
    "id",
    "retailer_id",
    "name",
    "description",
    "price",
    "sale_price",
    "currency",
    "availability",
    "url",
    "image_url",
    "brand",
    "condition",
])


# ── Catalog preflight discovery (May 2026 #19g) ──────────────────────
# Production symptom: BOTH ``/items``, ``/products`` and
# ``/product_items`` return ``code=100 — nonexisting field`` for the
# merchant's configured catalog_id. That means the object behind
# that ID is NOT a standard Commerce ProductCatalog — it might be a
# Commerce Account, a Catalog Item Set, a partner-managed feed, or
# even a deleted catalog whose ID lingered in the dashboard.
#
# We have NO business guessing which edge to call next. Instead we
# call the catalog OBJECT itself first and read what Meta says
# about it:
#
#   GET /{version}/{catalog_id}
#     ?fields=id,name,vertical,product_count,feed_count,catalog_type,
#             commerce_merchant_settings,business{id,name}
#
# Then we introspect the supported fields/edges via the Graph
# Metadata API:
#
#   GET /{version}/{catalog_id}?metadata=1
#
# Both responses are logged in full (truncated to _RESP_PREVIEW_CAP)
# under structured ``[META_IMPORT][CATALOG_INFO]`` and
# ``[META_IMPORT][CATALOG_METADATA]`` lines so support can read off
# the actual catalog_type / vertical / business binding without
# replaying the import.
#
# Behaviour during the diagnostic window:
#   * Discovery ALWAYS runs (cost = 2 cheap GETs).
#   * If the catalog_id resolves to a valid object → discovery
#     metadata is stored on the report and the import proceeds
#     with the best-guess edge (informed by what discovery
#     returned, not by hard-coded ``/items``).
#   * If the catalog_id does NOT resolve → raise the new
#     ``catalog_not_found`` / ``catalog_type_unsupported`` codes
#     with the full Meta error in ``detail`` so the dashboard
#     shows "this is not a valid catalog" instead of a 502.
#   * ``META_CATALOG_DISCOVERY_ONLY=true`` short-circuits BEFORE
#     the item-edge call: useful in production when we want
#     fresh ``[CATALOG_INFO]`` lines without paying for a full
#     import.

# The exact field projection we ask for on the catalog object.
# Keep the ``business{...}`` nested expansion so we don't need a
# separate hop to learn which Business Manager owns the catalog
# (production debugging clue: a catalog whose business doesn't
# match the merchant's WhatsApp BM is almost always the reason
# Meta returns "nonexisting field" — the merchant pasted the
# wrong catalog ID).
META_CATALOG_DISCOVERY_FIELDS = ",".join([
    "id",
    "name",
    "vertical",
    "product_count",
    "feed_count",
    "catalog_type",
    "commerce_merchant_settings",
    "business{id,name}",
])

# Catalog verticals that we KNOW how to import. ``commerce`` is the
# overwhelming majority of WhatsApp / Shopify / Salla catalogs;
# ``generic`` and ``transactable_items`` also expose ``/products``
# the same way. Anything else (hotels, flights, vehicles, jobs…)
# uses a different row schema and we refuse to import.
_KNOWN_PRODUCT_VERTICALS: set = {
    "",                    # not all catalog responses include vertical
    "commerce",
    "ecommerce",
    "e_commerce",
    "generic",
    "transactable_items",
    "transactable_item",
    "offline_commerce",
}


def _discovery_only_enabled() -> Tuple[bool, str]:
    """Kill-switch — when truthy, skip the item-edge call entirely
    and return a discovery-only ImportReport. Useful for
    production diagnostic loops (read fresh [CATALOG_INFO] without
    paying for a full crawl).

    Returns ``(enabled, raw_value)`` so the caller can log BOTH the
    parsed bool AND the exact string that arrived from the
    environment — production incident May 2026 #19g: the merchant
    set the env var on Railway but the importer kept hitting
    /product_items, so the support team needs the raw string to
    prove the variable was actually present in the process
    environment at the moment of the request (vs. having been set
    on a different service / replica that wasn't restarted).

    Parsing rule (exact, no fuzz):
        raw.strip().lower() ∈ {"1","true","yes","on"}
    """
    raw = os.getenv("META_CATALOG_DISCOVERY_ONLY", "")
    enabled = str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return enabled, raw


# ─────────────────────────────────────────────────────────────────────────────
# Result + error types
# ─────────────────────────────────────────────────────────────────────────────

class MetaCatalogImportError(RuntimeError):
    """Structured error so the FastAPI layer can map ``.code`` to an
    HTTP status without parsing free-form messages.

    Codes (closed set, mapped 1:1 in the router):

    * ``connection_not_found``        → 404
    * ``catalog_id_missing``          → 400 (merchant must wire Meta first)
    * ``access_token_missing``        → 400 (no token at all on conn)
    * ``meta_access_token_missing``   → 400 (no Graph-compatible token)
    * ``graph_token_invalid``         → 401 (OAuth code 190 / expired token)
    * ``graph_token_no_catalog_access`` → 403 (token valid but cannot read catalog)
    * ``catalog_not_found``           → 404 (preflight GET /{catalog_id}
                                             failed — the ID does not
                                             resolve to any object Meta
                                             knows about)
    * ``catalog_type_unsupported``    → 400 (preflight succeeded but the
                                             object is a vertical we
                                             don't import — hotels /
                                             flights / vehicles / jobs,
                                             or a Commerce Account /
                                             Catalog Item Set rather
                                             than a ProductCatalog)
    * ``meta_http_error``             → 502 (upstream failure)
    """
    def __init__(self, code: str, message: str = "", *, detail: Any = None):
        super().__init__(message or code)
        self.code: str = code
        self.detail: Any = detail


@dataclass
class CatalogDiscovery:
    """Result of the preflight ``GET /{catalog_id}`` + ``?metadata=1``
    introspection pair. Always populated, even on failure, so the
    dashboard / support never see a 502 with no upstream context.
    """
    catalog_id:           str = ""
    ok:                   bool = False
    http_status:          Optional[int] = None
    # The headline fields Meta exposes on a ProductCatalog object.
    catalog_type:         str = ""
    vertical:             str = ""
    name:                 str = ""
    product_count:        Optional[int] = None
    feed_count:           Optional[int] = None
    business_id:          str = ""
    business_name:        str = ""
    # The raw introspection result from ``?metadata=1`` — the
    # ``connections`` keys are the actual edge names this object
    # supports, and the ``fields`` keys are its readable scalar
    # fields. We carry the lists separately for ergonomic logging
    # plus stash the full raw dict for support deep-dives.
    supported_edges:      List[str] = field(default_factory=list)
    supported_fields:     List[str] = field(default_factory=list)
    raw_object:           Dict[str, Any] = field(default_factory=dict)
    raw_metadata:         Dict[str, Any] = field(default_factory=dict)
    # Populated on failure: Meta's structured error envelope from
    # whichever preflight call broke.
    error:                Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "catalog_id":       self.catalog_id,
            "ok":               self.ok,
            "http_status":      self.http_status,
            "catalog_type":     self.catalog_type or None,
            "vertical":         self.vertical or None,
            "name":             self.name or None,
            "product_count":    self.product_count,
            "feed_count":       self.feed_count,
            "business_id":      self.business_id or None,
            "business_name":    self.business_name or None,
            "supported_edges":  list(self.supported_edges),
            "supported_fields": list(self.supported_fields),
            "error":            self.error or None,
        }


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
    skipped_protected: int = 0
    flagged_conflict: int = 0
    refreshed_meta:   int = 0
    errors:          int = 0
    pages_fetched:   int = 0
    truncated:       bool = False    # True when MAX_PAGES hit + Meta had more
    error_samples:   List[Dict[str, Any]] = field(default_factory=list)
    # Populated by the preflight discovery hop — null on hard
    # preflight failure (we raise before assigning).
    discovery:       Optional[CatalogDiscovery] = None
    # True when ``META_CATALOG_DISCOVERY_ONLY`` short-circuited
    # the run after preflight without touching item edges. The
    # dashboard renders this as "discovery-only run — re-enable
    # full import to fetch products".
    discovery_only:  bool = False
    # The edge actually used to fetch items. Empty when the run
    # was discovery-only or when no edge was attempted at all.
    edge_used:         str = ""
    attempted_edges:   List[str] = field(default_factory=list)
    unsupported_edges: List[str] = field(default_factory=list)
    # P1-G1 — Meta reconciliation (full import only).
    seen_meta_external_ids: Set[str] = field(default_factory=set)
    seen_meta_retailer_ids: Set[str] = field(default_factory=set)
    reconciled_missing:     int = 0
    restored_from_meta:     int = 0
    reconciliation_skipped: bool = False
    reconciliation_skip_reason: str = ""
    pagination_incomplete:  bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanned":           self.scanned,
            "created":           self.created,
            "updated":           self.updated,
            "skipped_manual":    self.skipped_manual,
            "skipped_protected": self.skipped_protected,
            "flagged_conflict":  self.flagged_conflict,
            "refreshed_meta":    self.refreshed_meta,
            "errors":            self.errors,
            "pages_fetched":     self.pages_fetched,
            "truncated":         self.truncated,
            "error_samples":     self.error_samples[:10],
            "discovery":         self.discovery.to_dict() if self.discovery else None,
            "discovery_only":    self.discovery_only,
            "edge_used":         self.edge_used or None,
            "selected_edge":     self.edge_used or None,
            "attempted_edges":   list(self.attempted_edges),
            "unsupported_edges": list(self.unsupported_edges),
            "reconciled_missing": self.reconciled_missing,
            "restored_from_meta": self.restored_from_meta,
            "reconciliation_skipped": self.reconciliation_skipped,
            "reconciliation_skip_reason": self.reconciliation_skip_reason or None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Catalog preflight discovery (May 2026 #19g)
# ─────────────────────────────────────────────────────────────────────────────


def _parse_meta_error(resp: httpx.Response) -> Dict[str, Any]:
    """Pull Meta's structured ``{"error": {...}}`` envelope off a
    Graph response. Returns an empty dict when the body isn't JSON
    or doesn't carry an error block."""
    try:
        body = resp.json() or {}
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"]
    return {}


def _preflight_catalog_discovery(
    client: httpx.Client,
    *,
    tenant_id: int,
    catalog_id: str,
    token: str,
) -> CatalogDiscovery:
    """Inspect the catalog OBJECT before attempting any item edge.

    Performs TWO cheap GETs:

      1. ``GET /{catalog_id}?fields=id,name,vertical,product_count,
                                    feed_count,catalog_type,
                                    commerce_merchant_settings,
                                    business{id,name}``
         → tells us if the ID is real, what kind of catalog it is,
           how many products Meta has on file, and which Business
           Manager owns it.

      2. ``GET /{catalog_id}?metadata=1``
         → returns ``metadata.connections`` (the actual edge names
           this object supports — /products, /product_items,
           /items, /product_sets, /assigned_users, …) and
           ``metadata.fields`` (the readable scalar fields). This
           IS the authoritative source of truth for which edge
           we should call next.

    Both responses are logged in full under
    ``[META_IMPORT][CATALOG_INFO]`` and
    ``[META_IMPORT][CATALOG_METADATA]``. The function NEVER raises
    — it returns a ``CatalogDiscovery`` with ``ok=False`` and the
    Meta error block populated on failure. The caller decides
    whether the import should proceed.
    """
    out = CatalogDiscovery(catalog_id=catalog_id)

    # ── Hop 1: object fields ──────────────────────────────────
    info_url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{catalog_id}"
    info_params = {
        "fields":       META_CATALOG_DISCOVERY_FIELDS,
        "access_token": token,
    }
    _t0 = time.perf_counter()
    try:
        resp = client.get(info_url, params=info_params)
    except Exception as exc:  # noqa: BLE001
        out.error = {
            "stage":      "catalog_info_transport",
            "exc_class":  exc.__class__.__name__,
            "exc_msg":    str(exc)[:_RESP_PREVIEW_CAP],
        }
        logger.exception(
            "[META_IMPORT][CATALOG_INFO][EXC] tenant=%s catalog_id=%s "
            "exc_class=%s",
            tenant_id, catalog_id, exc.__class__.__name__,
        )
        return out

    _elapsed_ms = (time.perf_counter() - _t0) * 1000.0
    out.http_status = resp.status_code
    try:
        info_preview = (resp.text or "")[:_RESP_PREVIEW_CAP]
    except Exception:  # noqa: BLE001
        info_preview = "<text_unavailable>"

    if resp.status_code >= 400:
        meta_err = _parse_meta_error(resp)
        out.error = {
            "stage":          "catalog_info_http_error",
            "status":         resp.status_code,
            "body_preview":   info_preview,
            "meta_code":      meta_err.get("code"),
            "meta_subcode":   meta_err.get("error_subcode"),
            "meta_type":      meta_err.get("type"),
            "meta_message":   str(meta_err.get("message") or "")[:_RESP_PREVIEW_CAP],
            "fbtrace_id":     meta_err.get("fbtrace_id"),
        }
        logger.error(
            "[META_IMPORT][CATALOG_INFO][HTTP_ERROR] tenant=%s catalog_id=%s "
            "status=%d meta_code=%s meta_subcode=%s meta_type=%s "
            "meta_message=%r fbtrace_id=%s elapsed_ms=%.0f body_preview=%r",
            tenant_id, catalog_id, resp.status_code,
            meta_err.get("code"), meta_err.get("error_subcode"),
            meta_err.get("type"),
            str(meta_err.get("message") or "")[:_RESP_PREVIEW_CAP],
            meta_err.get("fbtrace_id"),
            _elapsed_ms, info_preview,
        )
        return out

    try:
        body = resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        out.error = {
            "stage":        "catalog_info_json_decode",
            "exc_class":    exc.__class__.__name__,
            "body_preview": info_preview,
        }
        logger.exception(
            "[META_IMPORT][CATALOG_INFO][EXC] tenant=%s catalog_id=%s "
            "json_decode exc_class=%s",
            tenant_id, catalog_id, exc.__class__.__name__,
        )
        return out

    out.raw_object   = body if isinstance(body, dict) else {}
    out.name         = str(out.raw_object.get("name") or "").strip()
    out.catalog_type = str(out.raw_object.get("catalog_type") or "").strip()
    out.vertical     = str(out.raw_object.get("vertical") or "").strip()
    _pc = out.raw_object.get("product_count")
    out.product_count = int(_pc) if isinstance(_pc, (int, float)) else None
    _fc = out.raw_object.get("feed_count")
    out.feed_count    = int(_fc) if isinstance(_fc, (int, float)) else None
    biz = out.raw_object.get("business") or {}
    if isinstance(biz, dict):
        out.business_id   = str(biz.get("id") or "").strip()
        out.business_name = str(biz.get("name") or "").strip()

    logger.info(
        "[META_IMPORT][CATALOG_INFO] tenant=%s catalog_id=%s status=%d "
        "elapsed_ms=%.0f name=%r catalog_type=%r vertical=%r "
        "product_count=%s feed_count=%s business_id=%s business_name=%r "
        "body_preview=%r",
        tenant_id, catalog_id, resp.status_code, _elapsed_ms,
        out.name, out.catalog_type, out.vertical,
        out.product_count, out.feed_count,
        out.business_id or "<unset>", out.business_name,
        info_preview,
    )

    # ── Hop 2: ``?metadata=1`` edge / field introspection ─────
    meta_url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{catalog_id}"
    meta_params = {
        "metadata":     "1",
        "access_token": token,
    }
    _t1 = time.perf_counter()
    try:
        meta_resp = client.get(meta_url, params=meta_params)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[META_IMPORT][CATALOG_METADATA][EXC] tenant=%s catalog_id=%s "
            "exc_class=%s",
            tenant_id, catalog_id, exc.__class__.__name__,
        )
        # We still consider the discovery a success — the object
        # exists (hop 1 returned 2xx); we just couldn't enumerate
        # its edges. Caller falls back to the default edge.
        out.ok = True
        return out

    _elapsed_ms2 = (time.perf_counter() - _t1) * 1000.0
    try:
        meta_preview = (meta_resp.text or "")[:_RESP_PREVIEW_CAP]
    except Exception:  # noqa: BLE001
        meta_preview = "<text_unavailable>"

    if meta_resp.status_code >= 400:
        meta_err = _parse_meta_error(meta_resp)
        logger.warning(
            "[META_IMPORT][CATALOG_METADATA][HTTP_ERROR] tenant=%s "
            "catalog_id=%s status=%d meta_code=%s meta_message=%r "
            "fbtrace_id=%s elapsed_ms=%.0f body_preview=%r",
            tenant_id, catalog_id, meta_resp.status_code,
            meta_err.get("code"),
            str(meta_err.get("message") or "")[:_RESP_PREVIEW_CAP],
            meta_err.get("fbtrace_id"), _elapsed_ms2, meta_preview,
        )
        out.ok = True  # hop 1 already proved the object exists
        return out

    try:
        meta_body = meta_resp.json() or {}
    except Exception:  # noqa: BLE001
        out.ok = True
        return out

    if isinstance(meta_body, dict):
        metadata = meta_body.get("metadata") or {}
        if isinstance(metadata, dict):
            out.raw_metadata = metadata
            connections = metadata.get("connections") or {}
            if isinstance(connections, dict):
                out.supported_edges = sorted(str(k) for k in connections.keys())
            fields_block = metadata.get("fields") or []
            if isinstance(fields_block, list):
                names: List[str] = []
                for f in fields_block:
                    if isinstance(f, dict):
                        n = f.get("name")
                        if n:
                            names.append(str(n))
                    elif isinstance(f, str):
                        names.append(f)
                out.supported_fields = sorted(set(names))

    logger.info(
        "[META_IMPORT][CATALOG_METADATA] tenant=%s catalog_id=%s status=%d "
        "elapsed_ms=%.0f supported_edges=%s supported_fields_count=%d "
        "body_preview=%r",
        tenant_id, catalog_id, meta_resp.status_code, _elapsed_ms2,
        out.supported_edges, len(out.supported_fields),
        meta_preview,
    )

    out.ok = True
    return out


def _choose_item_edges(discovery: CatalogDiscovery) -> List[str]:
    """Return an ordered list of item-edge names to try for *discovery*.

    When ``supported_edges`` from ``?metadata=1`` is non-empty, prefer
    edges Meta advertised — still ordered ``products`` → ``items`` →
    ``product_items``. When metadata is empty (common when ``?metadata=1``
    fails), try the full default chain so catalogs that only expose
    ``/products`` are not stuck on legacy ``/items`` defaults.
    """
    advertised = {
        str(e).strip()
        for e in (discovery.supported_edges or [])
        if str(e).strip()
    }
    if advertised:
        ordered = [e for e in CATALOG_ITEM_EDGE_ORDER if e in advertised]
        if ordered:
            return ordered
    return list(CATALOG_ITEM_EDGE_ORDER)


def _choose_item_edge(discovery: CatalogDiscovery) -> Tuple[str, str]:
    """Pick primary + first fallback edge — backward-compatible wrapper."""
    edges = _choose_item_edges(discovery)
    primary = edges[0]
    try:
        idx = CATALOG_ITEM_EDGE_ORDER.index(primary)
        fallback = (
            CATALOG_ITEM_EDGE_ORDER[idx + 1]
            if idx + 1 < len(CATALOG_ITEM_EDGE_ORDER)
            else primary
        )
    except ValueError:
        fallback = edges[1] if len(edges) > 1 else primary
    return primary, fallback


def is_unsupported_catalog_edge_error(meta_err: Optional[Dict[str, Any]]) -> bool:
    """True when Meta rejects an item *edge* name (code 100 nonexisting field).

    Example: ``(#100) Tried accessing nonexistent field (product_items)``.
    This is NOT a token/permission failure — caller should try the next edge.
    """
    err = meta_err or {}
    code = err.get("code")
    if code is None:
        code = err.get("meta_code")
    if int(code or 0) != 100:
        return False
    msg = str(err.get("message") or err.get("meta_message") or "").lower()
    if "nonexisting field" not in msg and "nonexistent field" not in msg:
        return False
    for edge in CATALOG_ITEM_EDGE_ORDER:
        if edge in msg:
            return True
    # Generic code-100 on an item-edge GET — still treat as edge mismatch.
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Graph API token selection (May 2026 #19e)
# ─────────────────────────────────────────────────────────────────────────────
#
# Production incident (tenant=33, catalog_id=2426534581035003):
# Meta rejected our call with ``code=190 — Invalid OAuth access token``.
# Root cause: the merchant connected via 360dialog (coexistence), so
# their ``WhatsAppConnection.access_token`` column holds a 360dialog
# ``D360-API-KEY``, NOT a Meta Graph API token. The original importer
# blindly read ``conn.access_token`` and sent it to
# ``graph.facebook.com``, which can never authenticate a D360 key.
#
# Token sources we accept for the Catalog Graph API (ordered):
#
#   1. ``merchant_meta_oauth``  — ``conn.access_token`` when
#      ``conn.provider == "meta"``. This is the Meta system-user /
#      OAuth token the merchant produced through Embedded Signup;
#      it natively carries the ``catalog_management`` scope when the
#      merchant clicked that permission during signup.
#
#   2. ``platform_system_user`` — the platform-wide system user
#      token in ``WA_TOKEN`` (env ``WHATSAPP_TOKEN``). Used as a
#      fallback for merchants whose connection has no merchant-side
#      Meta OAuth token (coexistence / 360dialog merchants, or
#      merchants who signed up before catalog_management was added
#      to the Embedded Signup config). Whether the platform's
#      system user actually has catalog_management on the target
#      catalog is enforced upstream by Meta — we let that surface
#      as ``[META_IMPORT][HTTP_ERROR]`` rather than guessing here.
#
# What we NEVER do:
#
#   * ``conn.access_token`` for ``provider="dialog360"`` — that's a
#     D360-API-KEY, not a Graph token. Sending it would produce
#     exactly the ``code=190`` error this incident surfaced.
#
# When neither source is available we raise the new
# ``meta_access_token_missing`` code (mapped to 400 in the router)
# so the merchant gets a clear "this needs a Meta OAuth token" copy
# in the dashboard instead of an opaque 502.
_TOKEN_SOURCE_MERCHANT_OAUTH  = "merchant_meta_oauth"
_TOKEN_SOURCE_PLATFORM_SYSTEM = "platform_system_user"
_TOKEN_SOURCE_NONE            = "none"

# Closed result codes for diagnostics / API (never include token values).
GRAPH_RESULT_OK                        = "ok"
GRAPH_RESULT_TOKEN_MISSING             = "graph_token_missing"
GRAPH_RESULT_TOKEN_INVALID             = "graph_token_invalid"
GRAPH_RESULT_TOKEN_NO_CATALOG_ACCESS   = "graph_token_no_catalog_access"
GRAPH_RESULT_CATALOG_NOT_FOUND         = "catalog_not_found"
GRAPH_RESULT_CATALOG_ID_MISSING        = "catalog_id_missing"
GRAPH_RESULT_CONNECTION_MISSING        = "connection_missing"
GRAPH_RESULT_META_HTTP_ERROR           = "meta_http_error"
GRAPH_RESULT_TRANSPORT_ERROR           = "transport_error"
GRAPH_RESULT_UNSUPPORTED_CATALOG_EDGE  = "unsupported_catalog_edge"


def _looks_like_meta_graph_token(token: str) -> bool:
    """True when *token* plausibly is a Meta Graph OAuth / system-user token.

    Coexistence / 360dialog merchants normally store a short D360 API key in
    ``access_token`` — those must never be sent to graph.facebook.com. A
    long ``EAA…`` token stored on the same row is treated as merchant Meta
    OAuth and preferred over the platform fallback.
    """
    t = (token or "").strip()
    if len(t) < 80:
        return False
    if t.upper().startswith("D360-"):
        return False
    return True


def classify_meta_graph_error(
    error: Optional[Dict[str, Any]] = None,
    *,
    http_status: Optional[int] = None,
    token_source: Optional[str] = None,
    stage: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a Meta Graph failure to a safe, closed result vocabulary.

    Returns ``result_code``, ``permission_category``, and ``action_required``
    — never raw tokens.
    """
    err = dict(error or {})
    meta_code = err.get("meta_code")
    if meta_code is None:
        meta_code = err.get("code")
    meta_subcode = err.get("meta_subcode") or err.get("error_subcode")
    meta_type = str(err.get("meta_type") or err.get("type") or "")
    meta_message = str(err.get("meta_message") or err.get("message") or "")
    status = http_status if http_status is not None else err.get("status")

    if token_source in (_TOKEN_SOURCE_NONE, None, "") and stage != "catalog_id_check":
        return {
            "result_code":          GRAPH_RESULT_TOKEN_MISSING,
            "permission_category":  "token_missing",
            "action_required": (
                "Connect WhatsApp via Meta Embedded Signup with catalog_management, "
                "or configure the platform WHATSAPP_TOKEN system-user token in Railway."
            ),
            "meta_code":            meta_code,
            "meta_subcode":         meta_subcode,
            "meta_type":            meta_type or None,
            "http_status":          status,
        }

    msg_lower = meta_message.lower()
    if is_unsupported_catalog_edge_error(
        {"code": meta_code, "message": meta_message},
    ):
        return {
            "result_code":          GRAPH_RESULT_UNSUPPORTED_CATALOG_EDGE,
            "permission_category":  "unsupported_catalog_edge",
            "action_required":      (
                "Meta rejected the catalog item edge — try another edge "
                "(products / items / product_items). This is not a token issue."
            ),
            "meta_code":            meta_code,
            "meta_subcode":         meta_subcode,
            "meta_type":            meta_type or None,
            "http_status":          status,
        }

    if meta_code in (190, 102) or (
        meta_type == "OAuthException" and "invalid" in msg_lower and "token" in msg_lower
    ):
        src = token_source or _TOKEN_SOURCE_NONE
        if src == _TOKEN_SOURCE_PLATFORM_SYSTEM:
            action = (
                "The platform WHATSAPP_TOKEN is invalid or expired. Update it in "
                "Railway with a valid Meta system-user token that has "
                "catalog_management on this catalog's Business Manager."
            )
        else:
            action = (
                "The merchant Meta OAuth token is invalid or expired. Ask the merchant "
                "to reconnect WhatsApp via Meta Embedded Signup and grant "
                "catalog_management."
            )
        return {
            "result_code":          GRAPH_RESULT_TOKEN_INVALID,
            "permission_category":  "invalid_token",
            "action_required":      action,
            "meta_code":            meta_code,
            "meta_subcode":         meta_subcode,
            "meta_type":            meta_type or None,
            "http_status":          status,
        }

    is_not_found = (
        meta_code in (803, 100)
        and ("does not exist" in msg_lower or "cannot be loaded" in msg_lower)
    ) or status == 404
    if is_not_found:
        return {
            "result_code":          GRAPH_RESULT_CATALOG_NOT_FOUND,
            "permission_category":  "catalog_not_found",
            "action_required": (
                "Verify the Catalog ID in Nahla matches Commerce Manager → Catalog → "
                "Settings. If the ID is correct, the selected token may belong to a "
                "different Business Manager than the catalog owner."
            ),
            "meta_code":            meta_code,
            "meta_subcode":         meta_subcode,
            "meta_type":            meta_type or None,
            "http_status":          status,
        }

    permission_codes = {10, 200, 294}
    if (
        status in (401, 403)
        or meta_code in permission_codes
        or (meta_type == "OAuthException" and meta_code not in (190, 102, 803, 100))
    ):
        src = token_source or _TOKEN_SOURCE_NONE
        if src == _TOKEN_SOURCE_PLATFORM_SYSTEM:
            action = (
                "The platform WHATSAPP_TOKEN cannot read this catalog. Grant the Nahla "
                "system user catalog_management (and Business access to the merchant's "
                "catalog) in Meta Business Settings, or ask the merchant to reconnect "
                "via Meta Embedded Signup so a merchant OAuth token is on file."
            )
        elif src == _TOKEN_SOURCE_MERCHANT_OAUTH:
            action = (
                "The merchant Meta token lacks catalog_management on this catalog. "
                "Ask the merchant to reconnect via Meta Embedded Signup and grant "
                "catalog permissions, or share the catalog with Nahla's Business Manager."
            )
        else:
            action = (
                "No Graph-compatible token can access this catalog. Configure "
                "WHATSAPP_TOKEN or merchant Meta OAuth with catalog_management."
            )
        return {
            "result_code":          GRAPH_RESULT_TOKEN_NO_CATALOG_ACCESS,
            "permission_category":  "insufficient_catalog_access",
            "action_required":      action,
            "meta_code":            meta_code,
            "meta_subcode":         meta_subcode,
            "meta_type":            meta_type or None,
            "http_status":          status,
        }

    if stage in ("catalog_info_transport", "transport"):
        return {
            "result_code":          GRAPH_RESULT_TRANSPORT_ERROR,
            "permission_category":  "transport",
            "action_required":      "Meta Graph API transport error — retry or check network.",
            "meta_code":            meta_code,
            "meta_subcode":         meta_subcode,
            "meta_type":            meta_type or None,
            "http_status":          status,
        }

    return {
        "result_code":          GRAPH_RESULT_META_HTTP_ERROR,
        "permission_category":  "meta_http_error",
        "action_required":      (
            "Meta Graph API returned an error during catalog import. Check admin "
            "graph-import diagnostics for result_code and fbtrace_id."
        ),
        "meta_code":            meta_code,
        "meta_subcode":         meta_subcode,
        "meta_type":            meta_type or None,
        "http_status":          status,
    }


def _import_error_code_from_classification(result_code: str) -> str:
    """Map diagnostics result_code → :class:`MetaCatalogImportError` ``.code``."""
    return {
        GRAPH_RESULT_TOKEN_MISSING:           "meta_access_token_missing",
        GRAPH_RESULT_TOKEN_INVALID:           "graph_token_invalid",
        GRAPH_RESULT_TOKEN_NO_CATALOG_ACCESS: "graph_token_no_catalog_access",
        GRAPH_RESULT_CATALOG_NOT_FOUND:       "catalog_not_found",
        GRAPH_RESULT_TRANSPORT_ERROR:         "meta_http_error",
        GRAPH_RESULT_META_HTTP_ERROR:         "meta_http_error",
    }.get(result_code, "meta_http_error")


def sanitize_token_pick(token_pick: Dict[str, Any]) -> Dict[str, Any]:
    """Strip secrets from :func:`_select_graph_token` output for API responses."""
    return {
        "token_source":              token_pick.get("token_source"),
        "provider":                  token_pick.get("provider"),
        "connection_type":           token_pick.get("connection_type"),
        "token_tail":                token_pick.get("token_tail"),
        "token_len":                 token_pick.get("token_len"),
        "token_present":             bool(token_pick.get("token")),
        "platform_token_configured": bool((WA_TOKEN or "").strip()),
        "considered":                list(token_pick.get("considered") or []),
    }


def describe_graph_token_selection(conn: Any) -> Dict[str, Any]:
    """Safe token-selection snapshot for diagnostics — no raw token."""
    return sanitize_token_pick(_select_graph_token(conn))


def _probe_products_page(
    client: httpx.Client,
    *,
    catalog_id: str,
    token: str,
    edge: str = "products",
    limit: int = 1,
) -> Dict[str, Any]:
    """Cheap ``GET /{catalog_id}/{edge}?limit=1`` probe — diagnostics only."""
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{catalog_id}/{edge}"
    params = {
        "fields":       "id,name,retailer_id",
        "limit":        str(max(1, int(limit))),
        "access_token": token,
    }
    out: Dict[str, Any] = {"edge": edge, "ok": False}
    try:
        resp = client.get(url, params=params)
    except Exception as exc:  # noqa: BLE001
        out.update({
            "http_status": None,
            "error": {
                "stage":     "products_probe_transport",
                "exc_class": exc.__class__.__name__,
                "exc_msg":   str(exc)[:200],
            },
        })
        return out

    out["http_status"] = resp.status_code
    if resp.status_code >= 400:
        meta_err = _parse_meta_error(resp)
        out["error"] = {
            "stage":        "products_probe_http_error",
            "status":       resp.status_code,
            "meta_code":    meta_err.get("code"),
            "meta_subcode": meta_err.get("error_subcode"),
            "meta_type":    meta_err.get("type"),
            "meta_message": str(meta_err.get("message") or "")[:500],
            "fbtrace_id":   meta_err.get("fbtrace_id"),
        }
        return out

    try:
        body = resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        out["error"] = {
            "stage":     "products_probe_json_decode",
            "exc_class": exc.__class__.__name__,
        }
        return out

    data = body.get("data") if isinstance(body, dict) else None
    out["ok"] = True
    out["sample_count"] = len(data) if isinstance(data, list) else 0
    return out


def build_graph_import_diagnostics(
    conn: Any,
    *,
    tenant_id: Optional[int] = None,
    run_preflight: bool = False,
) -> Dict[str, Any]:
    """Safe Graph import diagnostics — token source + optional live preflight.

    Never includes raw access tokens. When *run_preflight* is True, performs
    read-only Graph GETs against the configured ``meta_catalog_id``.
    """
    if conn is None:
        return {
            "provider":               None,
            "connection_type":        None,
            "meta_catalog_id_present": False,
            "meta_catalog_id":        "",
            "token_selection":        None,
            "preflight":              None,
            "products_probe":         None,
            "result_code":            GRAPH_RESULT_CONNECTION_MISSING,
            "action_required":        "Connect WhatsApp before importing a Meta catalog.",
        }

    catalog_id = (getattr(conn, "meta_catalog_id", None) or "").strip()
    token_pick = _select_graph_token(conn)
    safe_pick = sanitize_token_pick(token_pick)
    provider = safe_pick.get("provider")
    connection_type = safe_pick.get("connection_type")

    base: Dict[str, Any] = {
        "provider":                provider,
        "connection_type":         connection_type,
        "meta_catalog_id_present": bool(catalog_id),
        "meta_catalog_id":         catalog_id,
        "token_selection":         safe_pick,
        "preflight":               None,
        "products_probe":          None,
        "result_code":             None,
        "action_required":         None,
    }

    if not catalog_id:
        base["result_code"] = GRAPH_RESULT_CATALOG_ID_MISSING
        base["action_required"] = "Set the Meta Catalog ID on the WhatsApp connection."
        return base

    if not token_pick.get("token"):
        classified = classify_meta_graph_error(token_source=_TOKEN_SOURCE_NONE)
        base.update({
            "result_code":           classified["result_code"],
            "action_required":       classified["action_required"],
            "permission_category":   classified["permission_category"],
        })
        return base

    if not run_preflight:
        base["result_code"] = GRAPH_RESULT_OK if safe_pick.get("token_present") else GRAPH_RESULT_TOKEN_MISSING
        return base

    token = token_pick["token"]
    token_source = token_pick.get("token_source")
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        discovery = _preflight_catalog_discovery(
            client,
            tenant_id=int(tenant_id or getattr(conn, "tenant_id", 0) or 0),
            catalog_id=catalog_id,
            token=token,
        )
        preflight: Dict[str, Any] = {
            "ok":            discovery.ok,
            "http_status":   discovery.http_status,
            "catalog_type":  discovery.catalog_type or None,
            "vertical":      discovery.vertical or None,
            "name":          discovery.name or None,
            "product_count": discovery.product_count,
            "business_id":   discovery.business_id or None,
            "business_name": discovery.business_name or None,
            "supported_edges": list(discovery.supported_edges or []),
        }
        if not discovery.ok:
            classified = classify_meta_graph_error(
                discovery.error,
                http_status=discovery.http_status,
                token_source=token_source,
            )
            preflight.update({
                "result_code":         classified["result_code"],
                "permission_category": classified["permission_category"],
                "meta_code":           classified.get("meta_code"),
                "meta_subcode":        classified.get("meta_subcode"),
                "meta_type":           classified.get("meta_type"),
                "meta_message_category": (
                    str((discovery.error or {}).get("meta_message") or "")[:200] or None
                ),
            })
            base["preflight"] = preflight
            base["result_code"] = classified["result_code"]
            base["action_required"] = classified["action_required"]
            base["permission_category"] = classified["permission_category"]
            return base

        edge = "products"
        if discovery.supported_edges:
            if "products" in discovery.supported_edges:
                edge = "products"
            elif "product_items" in discovery.supported_edges:
                edge = "product_items"
            elif "items" in discovery.supported_edges:
                edge = "items"
        products_probe = _probe_products_page(
            client, catalog_id=catalog_id, token=token, edge=edge, limit=1,
        )
        products_probe["edge"] = edge
        if products_probe.get("ok"):
            classified = {"result_code": GRAPH_RESULT_OK, "permission_category": "ok", "action_required": None}
        else:
            classified = classify_meta_graph_error(
                products_probe.get("error") or {},
                http_status=products_probe.get("http_status"),
                token_source=token_source,
                stage=str((products_probe.get("error") or {}).get("stage") or ""),
            )
            products_probe["result_code"] = classified["result_code"]
            products_probe["permission_category"] = classified["permission_category"]

        preflight["result_code"] = GRAPH_RESULT_OK
        base["preflight"] = preflight
        base["products_probe"] = products_probe
        base["result_code"] = classified["result_code"]
        base["action_required"] = classified.get("action_required")
        base["permission_category"] = classified.get("permission_category")
        return base


def _select_graph_token(conn: Any) -> Dict[str, Any]:
    """Pick a Graph API-compatible token for the Catalog endpoints.

    Returns a dict carrying the resolved token alongside enough
    metadata for both the ``[META_IMPORT][READY]`` log line and the
    structured ``meta_access_token_missing`` response payload:

        {
            "token":            str | None,
            "token_source":     "merchant_meta_oauth"
                              | "platform_system_user"
                              | "none",
            "provider":         "meta" | "dialog360" | "",
            "connection_type":  "direct" | "embedded" | "coexistence" | "",
            "token_tail":       last 4 chars or "<empty>",
            "token_len":        int,
            "considered":       list[dict]  ← which sources were
                                             tried, in order, with
                                             reason they were rejected
                                             (so support can see why
                                             the merchant_oauth slot
                                             was skipped).
        }
    """
    provider     = str(getattr(conn, "provider", "") or "").lower()
    connection_t = str(getattr(conn, "connection_type", "") or "").lower()
    plain_token  = read_access_token(conn)

    considered: List[Dict[str, Any]] = []

    # ── Slot 1: merchant Meta OAuth ─────────────────────────────
    # provider=meta always; coexistence may store a long EAA token on
    # the same row alongside (or instead of) a short D360 API key.
    # Use read_access_token so encrypted-at-rest (enc1:) values decrypt
    # before eligibility checks and Graph calls — never send ciphertext.
    merchant_oauth_eligible = (
        provider == "meta" or _looks_like_meta_graph_token(plain_token)
    )
    if merchant_oauth_eligible:
        if plain_token:
            return {
                "token":           plain_token,
                "token_source":    _TOKEN_SOURCE_MERCHANT_OAUTH,
                "provider":        provider,
                "connection_type": connection_t,
                "token_tail":      _mask_token(plain_token),
                "token_len":       len(plain_token),
                "considered":      considered,
            }
        considered.append({
            "source": _TOKEN_SOURCE_MERCHANT_OAUTH,
            "reason": "provider=meta but merchant access token is empty",
        })
    else:
        considered.append({
            "source": _TOKEN_SOURCE_MERCHANT_OAUTH,
            "reason": (
                f"skipped — provider={provider or '<unset>'} and "
                f"merchant access token is not a Meta Graph token "
                f"(len={len(plain_token)}, typically a 360dialog D360-API-KEY). "
                f"Sending it to graph.facebook.com would trigger OAuthException code=190."
            ),
        })

    # ── Slot 2: platform system user token (WA_TOKEN env) ──────
    platform_token = (WA_TOKEN or "").strip()
    if platform_token:
        return {
            "token":           platform_token,
            "token_source":    _TOKEN_SOURCE_PLATFORM_SYSTEM,
            "provider":        provider,
            "connection_type": connection_t,
            "token_tail":      _mask_token(platform_token),
            "token_len":       len(platform_token),
            "considered":      considered,
        }
    considered.append({
        "source": _TOKEN_SOURCE_PLATFORM_SYSTEM,
        "reason": "WA_TOKEN env (WHATSAPP_TOKEN) is empty",
    })

    return {
        "token":           None,
        "token_source":    _TOKEN_SOURCE_NONE,
        "provider":        provider,
        "connection_type": connection_t,
        "token_tail":      "<none>",
        "token_len":       0,
        "considered":      considered,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Import metadata persistence (PR2 — diagnostics only)
# ─────────────────────────────────────────────────────────────────────────────


def _persist_import_running(
    db: Session, conn: WhatsAppConnection, token_source: str,
) -> None:
    conn.meta_import_status = "running"
    conn.meta_import_token_source = token_source or None
    db.commit()


def _persist_import_success(
    db: Session,
    conn: WhatsAppConnection,
    report: ImportReport,
    token_source: str,
) -> None:
    conn.meta_import_status = "success"
    conn.meta_import_last_at = datetime.now(timezone.utc)
    conn.meta_import_last_error = None
    conn.meta_import_last_report = report.to_dict()
    conn.meta_import_token_source = token_source or None
    db.commit()


def _persist_import_discovery_only(
    db: Session,
    conn: WhatsAppConnection,
    report: ImportReport,
    token_source: str,
) -> None:
    """Discovery-only kill-switch — preflight ran but no products were fetched."""
    conn.meta_import_status = "discovery_only"
    conn.meta_import_last_at = datetime.now(timezone.utc)
    conn.meta_import_last_error = None
    conn.meta_import_last_report = report.to_dict()
    conn.meta_import_token_source = token_source or None
    db.commit()


def _persist_import_failed(
    db: Session,
    conn: WhatsAppConnection,
    error: str,
    *,
    token_source: Optional[str] = None,
) -> None:
    conn.meta_import_status = "failed"
    conn.meta_import_last_at = datetime.now(timezone.utc)
    conn.meta_import_last_error = (error or "")[:2000]
    if token_source:
        conn.meta_import_token_source = token_source
    db.commit()


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

    # ── Env-var visibility (May 2026 #19g hardening) ──────────
    # Production incident: a merchant set
    # ``META_CATALOG_DISCOVERY_ONLY=true`` on Railway and
    # redeployed, but the importer still hit /product_items.
    # Root cause turned out to be a stale deploy of an older
    # commit that didn't read the variable yet. To prove the
    # env contract on every single request we now log the RAW
    # value (exactly what ``os.getenv`` returned) AND the
    # parsed bool. If this line shows ``raw='' parsed=False``
    # while the dashboard insists the variable is set, the
    # process is reading from a different environment than
    # Railway claims — that's an ops problem, not a code
    # problem, and the log proves it.
    _discovery_only, _discovery_only_raw = _discovery_only_enabled()
    logger.info(
        "[META_IMPORT][ENV] tenant=%s META_CATALOG_DISCOVERY_ONLY raw=%r parsed=%s",
        tenant_id, _discovery_only_raw, _discovery_only,
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
        _persist_import_failed(db, conn, "catalog_id_missing")
        raise MetaCatalogImportError(
            "catalog_id_missing",
            "Meta Catalog ID is not configured on the WhatsApp connection.",
            detail={"hint": "WhatsAppConnection.meta_catalog_id is empty"},
        )
    # ── Graph token selection (May 2026 #19e) ─────────────────
    # NOT a simple ``conn.access_token`` read anymore — for
    # dialog360 / coexistence merchants that field holds a
    # D360-API-KEY which Meta rejects with code=190. See
    # ``_select_graph_token`` docstring for the full ordering.
    token_pick = _select_graph_token(conn)
    token = token_pick["token"]
    if not token:
        _persist_import_failed(
            db, conn, "meta_access_token_missing",
            token_source=token_pick.get("token_source"),
        )
        raise MetaCatalogImportError(
            "meta_access_token_missing",
            "Meta catalog import requires a Meta Graph API access "
            "token, not a 360dialog API key. No merchant Meta OAuth "
            "token is on file for this WhatsApp connection and the "
            "platform system-user fallback (WA_TOKEN) is not "
            "configured.",
            detail={
                "provider":         token_pick["provider"],
                "connection_type":  token_pick["connection_type"],
                "catalog_id":       catalog_id,
                "token_source":     token_pick["token_source"],
                "considered":       token_pick["considered"],
                "result_code":      GRAPH_RESULT_TOKEN_MISSING,
                "permission_category": "token_missing",
                "action_required":  (
                    "Connect WhatsApp via Meta Embedded Signup with "
                    "catalog_management, or set WHATSAPP_TOKEN in Railway "
                    "to a platform system-user token with catalog access."
                ),
                "platform_token_configured": bool((WA_TOKEN or "").strip()),
                "hint": (
                    "Ask the merchant to reconnect WhatsApp via Meta "
                    "Embedded Signup and grant the 'catalog_management' "
                    "permission, OR set WHATSAPP_TOKEN to a platform "
                    "system-user token with catalog_management on the "
                    "target catalog."
                ),
            },
        )

    _persist_import_running(db, conn, token_pick["token_source"])
    report = ImportReport()
    try:
        report = _import_from_meta_body(
            db, tenant_id, conn, catalog_id, token, token_pick, report,
            discovery_only=_discovery_only,
        )
    except MetaCatalogImportError as exc:
        _persist_import_failed(
            db, conn, exc.code,
            token_source=token_pick.get("token_source"),
        )
        raise
    except Exception as exc:
        _persist_import_failed(
            db, conn, f"{type(exc).__name__}: {exc}"[:500],
            token_source=token_pick.get("token_source"),
        )
        raise
    if report.discovery_only:
        _persist_import_discovery_only(
            db, conn, report, token_pick["token_source"],
        )
    else:
        _persist_import_success(db, conn, report, token_pick["token_source"])
    return report


def _import_from_meta_body(
    db: Session,
    tenant_id: int,
    conn: WhatsAppConnection,
    catalog_id: str,
    token: str,
    token_pick: Dict[str, Any],
    report: ImportReport,
    *,
    discovery_only: bool,
) -> ImportReport:
    """Core import logic — separated so ``import_from_meta`` can wrap
    persistence around hard failures without changing upsert behaviour."""
    # ── Catalog preflight discovery (May 2026 #19g) ───────────
    # Two cheap GETs against the catalog object itself BEFORE we
    # try any item edge. This is the authoritative source of
    # truth for catalog_type / vertical / supported edges — we
    # no longer guess between /items / /products / /product_items
    # by trial and error.
    with httpx.Client(timeout=REQUEST_TIMEOUT) as _disc_client:
        discovery = _preflight_catalog_discovery(
            _disc_client,
            tenant_id=tenant_id,
            catalog_id=catalog_id,
            token=token,
        )
    report.discovery = discovery

    # Hard failure: the catalog_id doesn't resolve. Most common
    # cause is a Commerce Account ID or a deleted catalog pasted
    # into the dashboard by the merchant.
    if not discovery.ok:
        classified = classify_meta_graph_error(
            discovery.error,
            http_status=discovery.http_status,
            token_source=token_pick.get("token_source"),
            stage="preflight_discovery",
        )
        err_code = _import_error_code_from_classification(classified["result_code"])
        raise MetaCatalogImportError(
            err_code,
            "Preflight catalog discovery failed — the configured "
            "catalog_id does not resolve to a Meta object the "
            "current token can read.",
            detail={
                "stage":               "preflight_discovery",
                "catalog_id":          catalog_id,
                "discovery":           discovery.to_dict(),
                "token_source":        token_pick["token_source"],
                "provider":            token_pick["provider"],
                "connection_type":     token_pick.get("connection_type"),
                "result_code":         classified["result_code"],
                "permission_category": classified["permission_category"],
                "action_required":     classified["action_required"],
                "meta_code":           classified.get("meta_code"),
                "meta_subcode":        classified.get("meta_subcode"),
                "meta_type":           classified.get("meta_type"),
                "http_status":         classified.get("http_status"),
            },
        )

    # Soft refusal: the object exists but its vertical isn't one
    # we know how to import (hotels / flights / vehicles / jobs).
    if (discovery.vertical or "").lower() not in _KNOWN_PRODUCT_VERTICALS:
        raise MetaCatalogImportError(
            "catalog_type_unsupported",
            f"Catalog vertical {discovery.vertical!r} is not "
            f"supported by Nahla's product importer. We only "
            f"import 'commerce' / 'generic' / 'transactable_items' "
            f"catalogs today.",
            detail={
                "stage":      "preflight_vertical_check",
                "catalog_id": catalog_id,
                "discovery":  discovery.to_dict(),
                "hint": (
                    "Ask the merchant whether this catalog ID was "
                    "intentional. Vehicles / hotels / flights / "
                    "jobs catalogs use a different row schema and "
                    "need a dedicated importer."
                ),
            },
        )

    # Pick the item-edge name from the discovery output instead of
    # hard-coding ``/items``. ``_choose_item_edge`` reads
    # ``discovery.supported_edges`` (populated by ``?metadata=1``)
    # and prefers the actual edge Meta says this catalog exposes.
    # We compute this BEFORE the discovery_only short-circuit so
    # the [DISCOVERY_ONLY_STOP] log can also report what edge
    # WOULD have been chosen — operators reading the diagnostic
    # log can sanity-check the routing without re-running the
    # import with the kill-switch flipped off.
    edge_candidates = _choose_item_edges(discovery)
    primary_edge, fallback_edge = _choose_item_edge(discovery)
    edge_choice: Dict[str, Any] = {
        "primary":           primary_edge,
        "fallback":          fallback_edge,
        "candidates":        list(edge_candidates),
        "supported_edges":   list(discovery.supported_edges or []),
        "attempted_edges":   report.attempted_edges,
        "unsupported_edges": report.unsupported_edges,
        "selected_edge":     None,
    }
    logger.info(
        "[META_IMPORT][EDGE_CHOICE] tenant=%s catalog_id=%s "
        "candidates=%s primary_edge=/%s fallback_edge=/%s supported_edges=%s "
        "catalog_type=%r vertical=%r",
        tenant_id, catalog_id, edge_candidates, primary_edge, fallback_edge,
        discovery.supported_edges,
        discovery.catalog_type, discovery.vertical,
    )

    # Diagnostic kill-switch: when ON, stop here so production
    # logs surface fresh [CATALOG_INFO] lines without paying for
    # a full import. Default OFF — discovery is always logged
    # regardless of this flag. Uses the SAME parsed value the
    # [META_IMPORT][ENV] line already reported at the top of this
    # call so the two logs are guaranteed to agree (no risk of a
    # race between two os.getenv reads if some upstream code
    # mutates the env mid-request).
    if discovery_only:
        report.discovery_only = True
        # ``logger.warning`` (not info) so this branch is visually
        # impossible to miss in Railway's default INFO-and-above
        # filter. When ops flip the kill-switch they expect to see
        # something LOUD, not buried in a sea of [REQ] / [RESP]
        # noise — and warning is the cheapest level that satisfies
        # that without being a spurious error.
        logger.warning(
            "[META_IMPORT][DISCOVERY_ONLY_STOP] tenant=%s catalog_id=%s "
            "edge_choice=%s discovery_summary=%s — "
            "META_CATALOG_DISCOVERY_ONLY is on, no item-edge call will "
            "fire. Set META_CATALOG_DISCOVERY_ONLY=false (or unset it) "
            "to perform a full import.",
            tenant_id, catalog_id, edge_choice,
            {
                "ok":             discovery.ok,
                "catalog_type":   discovery.catalog_type,
                "vertical":       discovery.vertical,
                "product_count":  discovery.product_count,
            },
        )
        return report

    current_edge = edge_candidates[0]
    edge_candidate_idx = 0
    next_url: Optional[str] = (
        f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
        f"/{catalog_id}/{current_edge}"
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
        "provider=%s connection_type=%s token_source=%s "
        "token_len=%d page_size=%d max_pages=%d timeout=%.1fs",
        tenant_id, catalog_id, META_GRAPH_API_VERSION,
        token_pick["provider"] or "<unset>",
        token_pick["connection_type"] or "<unset>",
        token_pick["token_source"],
        token_pick["token_len"],
        PAGE_SIZE, MAX_PAGES, REQUEST_TIMEOUT,
    )

    # ``_first_real_attempt`` stays True until we get a successful
    # response from EITHER edge. It's the real "is this still the
    # first request from the merchant's POV" signal — Python's for-
    # loop counter increments even when we ``continue`` after an
    # edge fallback, so we can't use page_idx for the raise-vs-
    # soft-fail decision anymore.
    _first_real_attempt = True

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        page_idx = 0
        while page_idx <= MAX_PAGES:
            if next_url is None:
                break

            if page_idx == 0 and current_edge not in report.attempted_edges:
                report.attempted_edges.append(current_edge)

            log_url = _mask_url(next_url)
            logger.info(
                "[META_IMPORT][REQ] page=%d tenant=%s catalog_id=%s "
                "edge=%s graph_url=%s has_first_params=%s token_present=%s "
                "edge_candidate_idx=%d first_real_attempt=%s",
                page_idx, tenant_id, catalog_id, current_edge, log_url,
                first_params is not None, bool(token),
                edge_candidate_idx, _first_real_attempt,
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
                    "edge=%s transport_error exc_class=%s elapsed_ms=%.0f "
                    "graph_url=%s",
                    page_idx, tenant_id, catalog_id, current_edge,
                    exc.__class__.__name__, _elapsed_ms, log_url,
                )
                if _first_real_attempt:
                    raise MetaCatalogImportError(
                        "meta_http_error",
                        f"Meta Catalog transport error: "
                        f"{exc.__class__.__name__}: {exc}",
                        detail={
                            "stage":        "transport",
                            "page":         page_idx,
                            "edge":         current_edge,
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
                "edge=%s status=%d elapsed_ms=%.0f body_len=%d body_preview=%r",
                page_idx, tenant_id, catalog_id, current_edge,
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

                _meta_msg = str(_meta_err.get("message") or "")

                logger.error(
                    "[META_IMPORT][HTTP_ERROR] page=%d tenant=%s catalog_id=%s "
                    "edge=%s status=%d meta_code=%s meta_subcode=%s meta_type=%s "
                    "meta_message=%r fbtrace_id=%s",
                    page_idx, tenant_id, catalog_id, current_edge,
                    resp.status_code,
                    _meta_err.get("code"),
                    _meta_err.get("error_subcode"),
                    _meta_err.get("type"),
                    str(_meta_err.get("message") or "")[:_RESP_PREVIEW_CAP],
                    _meta_err.get("fbtrace_id"),
                )

                # ── Edge fallback — try next candidate on page 0 ──
                # Meta code=100 / "nonexisting field (EDGE)" means this
                # catalog does not expose that edge — NOT a permission error.
                if (
                    page_idx == 0
                    and _first_real_attempt
                    and is_unsupported_catalog_edge_error(_meta_err)
                ):
                    if current_edge not in report.unsupported_edges:
                        report.unsupported_edges.append(current_edge)
                    edge_candidate_idx += 1
                    if edge_candidate_idx < len(edge_candidates):
                        next_edge = edge_candidates[edge_candidate_idx]
                        logger.warning(
                            "[META_IMPORT][ENDPOINT_FALLBACK] tenant=%s "
                            "catalog_id=%s from=/%s to=/%s reason=%r "
                            "unsupported_edges=%s candidates=%s",
                            tenant_id, catalog_id,
                            current_edge, next_edge,
                            _meta_msg[:_RESP_PREVIEW_CAP],
                            report.unsupported_edges,
                            edge_candidates,
                        )
                        current_edge = next_edge
                        next_url = (
                            f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
                            f"/{catalog_id}/{current_edge}"
                        )
                        first_params = {
                            "fields":       META_CATALOG_FIELDS,
                            "limit":        str(PAGE_SIZE),
                            "access_token": token,
                        }
                        continue  # retry page 0 — do not advance page_idx

                    classified = classify_meta_graph_error(
                        {
                            "meta_code":    _meta_err.get("code"),
                            "meta_subcode": _meta_err.get("error_subcode"),
                            "meta_type":    _meta_err.get("type"),
                            "meta_message": str(_meta_err.get("message") or ""),
                            "status":       resp.status_code,
                        },
                        http_status=resp.status_code,
                        token_source=token_pick.get("token_source"),
                        stage="http_error",
                    )
                    raise MetaCatalogImportError(
                        "meta_http_error",
                        f"Meta returned {resp.status_code}: {_body_preview}",
                        detail={
                            "stage":               "http_error",
                            "status":              resp.status_code,
                            "page":                page_idx,
                            "edge":                current_edge,
                            "graph_url":           log_url,
                            "catalog_id":          catalog_id,
                            "body_preview":        _body_preview,
                            "meta_code":           _meta_err.get("code"),
                            "meta_subcode":        _meta_err.get("error_subcode"),
                            "meta_type":           _meta_err.get("type"),
                            "meta_message":        str(_meta_err.get("message") or "")[:_RESP_PREVIEW_CAP],
                            "fbtrace_id":          _meta_err.get("fbtrace_id"),
                            "attempted_edges":     list(report.attempted_edges),
                            "unsupported_edges":   list(report.unsupported_edges),
                            "edge_candidates":     list(edge_candidates),
                            "result_code":         classified["result_code"],
                            "permission_category": classified["permission_category"],
                            "action_required":     classified["action_required"],
                        },
                    )

                if _first_real_attempt:
                    classified = classify_meta_graph_error(
                        {
                            "meta_code":    _meta_err.get("code"),
                            "meta_subcode": _meta_err.get("error_subcode"),
                            "meta_type":    _meta_err.get("type"),
                            "meta_message": str(_meta_err.get("message") or ""),
                            "status":       resp.status_code,
                        },
                        http_status=resp.status_code,
                        token_source=token_pick.get("token_source"),
                        stage="http_error",
                    )
                    err_code = _import_error_code_from_classification(
                        classified["result_code"],
                    )
                    raise MetaCatalogImportError(
                        err_code,
                        f"Meta returned {resp.status_code}: "
                        f"{_body_preview}",
                        detail={
                            "stage":               "http_error",
                            "status":              resp.status_code,
                            "page":                page_idx,
                            "edge":                current_edge,
                            "graph_url":           log_url,
                            "catalog_id":          catalog_id,
                            "elapsed_ms":          round(_elapsed_ms, 1),
                            "body_preview":        _body_preview,
                            "meta_code":           _meta_err.get("code"),
                            "meta_subcode":        _meta_err.get("error_subcode"),
                            "meta_type":           _meta_err.get("type"),
                            "meta_message":        str(_meta_err.get("message") or "")[:_RESP_PREVIEW_CAP],
                            "fbtrace_id":          _meta_err.get("fbtrace_id"),
                            "token_source":        token_pick.get("token_source"),
                            "provider":            token_pick.get("provider"),
                            "connection_type":     token_pick.get("connection_type"),
                            "result_code":         classified["result_code"],
                            "permission_category": classified["permission_category"],
                            "action_required":     classified["action_required"],
                        },
                    )
                logger.warning(
                    "meta_catalog_import: page %d HTTP %d body=%s",
                    page_idx, resp.status_code, _body_preview,
                )
                report.pagination_incomplete = True
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
            # First successful page locks the edge in — any later
            # failure during pagination is treated as a soft-fail.
            _first_real_attempt = False
            # Record the edge that actually returned 2xx so the
            # report (and the dashboard) can show "imported via
            # /{edge}" — important for support when production
            # surfaces a fallback chain.
            if not report.edge_used:
                report.edge_used = current_edge
                edge_choice["selected_edge"] = current_edge
            logger.info(
                "[META_IMPORT][PAGE_OK] page=%d tenant=%s catalog_id=%s "
                "edge=%s row_count=%d total_scanned_before=%d",
                page_idx, tenant_id, catalog_id, current_edge,
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
            page_idx += 1

        if next_url is not None:
            report.truncated = True

    logger.info(
        "meta_catalog_import: tenant=%s scanned=%d created=%d updated=%d "
        "skipped_manual=%d errors=%d truncated=%s",
        tenant_id, report.scanned, report.created, report.updated,
        report.skipped_manual, report.errors, report.truncated,
    )
    _maybe_reconcile_meta_missing(db, tenant_id, report)
    return report


def _import_eligible_for_reconciliation(report: ImportReport) -> bool:
    """Reconcile only after a complete, successful Meta import."""
    if report.discovery_only:
        return False
    if report.truncated:
        return False
    if report.pagination_incomplete:
        return False
    if report.pages_fetched < 1:
        return False
    return True


def _meta_product_seen_in_import(
    product: Any,
    seen_external_ids: Set[str],
    seen_retailer_ids: Set[str],
) -> bool:
    """True when *product* matches any Meta row from the import pass.

    Mirrors the upsert matcher in ``_process_one_meta_product``:
      * ``external_id`` vs Meta global id
      * ``meta_retailer_id`` vs Meta retailer_id
      * legacy ``external_id`` holding the retailer_id
    """
    ext = str(getattr(product, "external_id", None) or "").strip()
    rid = str(getattr(product, "meta_retailer_id", None) or "").strip()
    if ext and ext in seen_external_ids:
        return True
    if rid and rid in seen_retailer_ids:
        return True
    if ext and ext in seen_retailer_ids:
        return True
    return False


def _maybe_reconcile_meta_missing(
    db: Session, tenant_id: int, report: ImportReport,
) -> None:
    if not _import_eligible_for_reconciliation(report):
        report.reconciliation_skipped = True
        if report.discovery_only:
            report.reconciliation_skip_reason = "discovery_only"
        elif report.truncated:
            report.reconciliation_skip_reason = "truncated"
        elif report.pagination_incomplete:
            report.reconciliation_skip_reason = "pagination_incomplete"
        elif report.pages_fetched < 1:
            report.reconciliation_skip_reason = "no_pages_fetched"
        else:
            report.reconciliation_skip_reason = "not_eligible"
        return

    seen_ext = set(report.seen_meta_external_ids or set())
    seen_rid = set(report.seen_meta_retailer_ids or set())
    now = datetime.now(timezone.utc)
    rows = (
        db.query(Product)
        .filter(Product.tenant_id == tenant_id)
        .filter(Product.source == SOURCE_META)
        .all()
    )
    for p in rows:
        if _meta_product_seen_in_import(p, seen_ext, seen_rid):
            continue
        ext = str(getattr(p, "external_id", None) or "").strip()
        rid = str(getattr(p, "meta_retailer_id", None) or "").strip()
        if not ext and not rid:
            continue
        if catalog_status_of(p) == CATALOG_STATUS_REMOVED_FROM_META:
            continue
        p.catalog_status = CATALOG_STATUS_REMOVED_FROM_META
        p.in_stock = False
        p.meta_removed_at = now
        report.reconciled_missing += 1
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            "[META_IMPORT][RECONCILE] tenant=%s commit_failed=%r",
            tenant_id, exc,
        )
        report.reconciliation_skipped = True
        report.reconciliation_skip_reason = "reconcile_commit_failed"


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
        # ── Field normalization (May 2026 #19f) ────────────────
        # /items returns a ProductItem object — same field names
        # the user spec wants stored:
        #   id           → Product.external_id  (Meta-side global ID)
        #   retailer_id  → Product.meta_retailer_id (merchant SKU;
        #                  fallback to id when Meta didn't send it,
        #                  e.g. items created via Commerce Manager
        #                  UI without a SKU)
        #   name         → Product.title
        #   description  → Product.description
        #   price        → Product.price (raw display string)
        #   sale_price   → extra_metadata.sale_price
        #   currency     → extra_metadata.currency
        #   availability → extra_metadata.availability + in_stock
        #   url          → extra_metadata.product_url
        #   image_url    → extra_metadata.image_url
        #   brand        → extra_metadata.brand
        #   condition    → extra_metadata.condition
        meta_id     = str(row.get("id") or "").strip()
        retailer_id = (row.get("retailer_id") or "").strip() or meta_id
        title       = (row.get("name") or "").strip()

        if not meta_id or not title:
            report.errors += 1
            if len(report.error_samples) < 10:
                report.error_samples.append({
                    "id":          row.get("id"),
                    "retailer_id": row.get("retailer_id"),
                    "reason":      "missing_id_or_name",
                })
            return

        report.seen_meta_external_ids.add(meta_id)
        report.seen_meta_retailer_ids.add(retailer_id)
        now = datetime.now(timezone.utc)
        # Match by Meta's global id first (``external_id``), then
        # by retailer_id on either column for backwards-compat with
        # the previous import (which stored retailer_id in BOTH
        # external_id and meta_retailer_id).
        existing = (
            db.query(Product)
              .filter(Product.tenant_id == tenant_id)
              .filter(
                  (Product.external_id == meta_id)
                  | (Product.meta_retailer_id == retailer_id)
                  | (Product.external_id == retailer_id)
              )
              .first()
        )

        decision = resolve_meta_import_action(
            existing,
            {"meta_id": meta_id, "retailer_id": retailer_id},
        )

        if decision.action == ACTION_SKIP_PROTECTED:
            report.skipped_protected += 1
            return

        if decision.action == ACTION_FLAG_CONFLICT:
            report.flagged_conflict += 1
            from core.catalog import NAHLA_NATIVE_SOURCES, normalize_source  # noqa: PLC0415
            if existing is not None:
                if normalize_source(existing.source) in NAHLA_NATIVE_SOURCES:
                    report.skipped_manual += 1
                existing.source_conflict_status = CONFLICT_POSSIBLE_DUPLICATE
                existing.source_conflict_detail = conflict_detail_payload(
                    existing=existing,
                    meta_id=meta_id,
                    retailer_id=retailer_id,
                    reason=decision.reason,
                )
            return

        price_blob      = _parse_meta_price(row.get("price"))
        sale_price_blob = _parse_meta_price(row.get("sale_price"))
        currency        = (
            price_blob["currency"]
            or sale_price_blob["currency"]
            or (row.get("currency") or None)
        )
        availability    = (row.get("availability") or "").lower()
        in_stock        = availability in {"", "in stock", "in_stock", "available"}
        meta_blob = {
            "source":       SOURCE_META_EXISTING,
            "meta_id":      meta_id or None,
            "image_url":    row.get("image_url") or None,
            "product_url":  row.get("url") or None,
            "currency":     currency,
            "availability": availability or None,
            "sale_price":   sale_price_blob["raw"] or None,
            "sale_price_value":    sale_price_blob["value"],
            "sale_price_currency": sale_price_blob["currency"],
            "brand":        (row.get("brand") or None),
            "condition":    (row.get("condition") or None),
        }
        # Drop empty values so the JSONB stays compact.
        meta_blob = {k: v for k, v in meta_blob.items() if v is not None}

        if decision.action == ACTION_CREATE:
            p = Product(
                tenant_id         = tenant_id,
                external_id       = meta_id,
                meta_retailer_id  = retailer_id,
                title             = title,
                description       = row.get("description") or None,
                price             = price_blob["raw"] or None,
                in_stock          = in_stock,
                extra_metadata    = meta_blob,
                source            = SOURCE_META_EXISTING,
                ownership_mode    = OWNERSHIP_META_READONLY,
                source_external_id = retailer_id or meta_id,
                meta_item_id      = meta_id,
                catalog_status    = CATALOG_STATUS_ACTIVE,
                meta_last_seen_at = now,
                imported_at       = now,
            )
            db.add(p)
            db.flush()
            try:
                assign_canonical_retailer_id(p)
            except Exception:  # noqa: BLE001
                pass
            report.created += 1
        elif decision.action == ACTION_REFRESH_META and existing is not None:
            existing.title       = title
            existing.description = row.get("description") or existing.description
            existing.price       = price_blob["raw"] or existing.price
            existing.in_stock    = in_stock
            existing.meta_last_seen_at = now
            if not (getattr(existing, "meta_item_id", None) or "").strip():
                existing.meta_item_id = meta_id
            if getattr(existing, "merchant_hidden_at", None) is None:
                prev_status = catalog_status_of(existing)
                if prev_status == CATALOG_STATUS_REMOVED_FROM_META:
                    existing.catalog_status = CATALOG_STATUS_ACTIVE
                    existing.meta_removed_at = None
                    report.restored_from_meta += 1
                elif prev_status != CATALOG_STATUS_MERCHANT_HIDDEN:
                    existing.catalog_status = CATALOG_STATUS_ACTIVE
            merged = dict(existing.extra_metadata or {})
            merged.update(meta_blob)
            existing.extra_metadata = merged
            if not (existing.meta_retailer_id or "").strip():
                existing.meta_retailer_id = retailer_id
            # Legacy meta rows may still carry source=meta — do not re-stamp.
            if not (existing.source or "").strip():
                existing.source = SOURCE_META_EXISTING
            if not (getattr(existing, "ownership_mode", None) or "").strip():
                existing.ownership_mode = OWNERSHIP_META_READONLY
            report.updated += 1
            report.refreshed_meta += 1
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
    "is_unsupported_catalog_edge_error",
    "_choose_item_edges",
]
