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
import re
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from core.catalog import SOURCE_MANUAL, SOURCE_META, assign_canonical_retailer_id
from core.config import META_GRAPH_API_VERSION, WA_TOKEN
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

# ── Catalog edge selection (May 2026 #19f) ───────────────────────────
# Meta exposes the same catalog rows under multiple edge names
# depending on the catalog type / API version. Production incident:
#
#   GET /v21.0/{catalog_id}/products
#   → (#100) Tried accessing nonexisting field (products)
#
# Commerce catalogs (the ones bound to WhatsApp / Instagram Shopping)
# expose their rows under ``/items``. The legacy "Marketing API
# product catalog" edge ``/products`` was retired for those catalogs
# somewhere around v18 / v19. We default to ``/items`` and fall back
# to ``/product_items`` if Meta tells us that edge doesn't exist
# either (some very old catalogs only respond to product_items).
META_CATALOG_EDGE_PRIMARY  = "items"
META_CATALOG_EDGE_FALLBACK = "product_items"

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


# ─────────────────────────────────────────────────────────────────────────────
# Result + error types
# ─────────────────────────────────────────────────────────────────────────────

class MetaCatalogImportError(RuntimeError):
    """Structured error so the FastAPI layer can map ``.code`` to an
    HTTP status without parsing free-form messages.

    Codes (closed set, mapped 1:1 in the router):

    * ``connection_not_found``       → 404
    * ``catalog_id_missing``         → 400 (merchant must wire Meta first)
    * ``access_token_missing``       → 400 (no token at all on conn)
    * ``meta_access_token_missing``  → 400 (token exists but is the wrong
                                            kind — typically a 360dialog
                                            D360-API-KEY where Graph
                                            requires Meta OAuth)
    * ``meta_http_error``            → 502 (upstream failure)
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
    raw_token    = str(getattr(conn, "access_token", "") or "").strip()

    considered: List[Dict[str, Any]] = []

    # ── Slot 1: merchant Meta OAuth (only when provider=meta) ──
    if provider == "meta":
        if raw_token:
            return {
                "token":           raw_token,
                "token_source":    _TOKEN_SOURCE_MERCHANT_OAUTH,
                "provider":        provider,
                "connection_type": connection_t,
                "token_tail":      _mask_token(raw_token),
                "token_len":       len(raw_token),
                "considered":      considered,
            }
        considered.append({
            "source": _TOKEN_SOURCE_MERCHANT_OAUTH,
            "reason": "provider=meta but conn.access_token is empty",
        })
    else:
        considered.append({
            "source": _TOKEN_SOURCE_MERCHANT_OAUTH,
            "reason": (
                f"skipped — provider={provider or '<unset>'} is not 'meta', "
                f"so conn.access_token is a non-Graph credential "
                f"(typically a 360dialog D360-API-KEY) and would trigger "
                f"OAuthException code=190 against graph.facebook.com."
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
    # ── Graph token selection (May 2026 #19e) ─────────────────
    # NOT a simple ``conn.access_token`` read anymore — for
    # dialog360 / coexistence merchants that field holds a
    # D360-API-KEY which Meta rejects with code=190. See
    # ``_select_graph_token`` docstring for the full ordering.
    token_pick = _select_graph_token(conn)
    token = token_pick["token"]
    if not token:
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
                "hint": (
                    "Ask the merchant to reconnect WhatsApp via Meta "
                    "Embedded Signup and grant the 'catalog_management' "
                    "permission, OR set WHATSAPP_TOKEN to a platform "
                    "system-user token with catalog_management on the "
                    "target catalog."
                ),
            },
        )

    report = ImportReport()
    # Edge defaults to /items; we may downgrade to /product_items
    # once on first-page failure (see fallback block below).
    current_edge = META_CATALOG_EDGE_PRIMARY
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
        "provider=%s connection_type=%s token_source=%s token_tail=%s "
        "token_len=%d page_size=%d max_pages=%d timeout=%.1fs",
        tenant_id, catalog_id, META_GRAPH_API_VERSION,
        token_pick["provider"] or "<unset>",
        token_pick["connection_type"] or "<unset>",
        token_pick["token_source"],
        token_pick["token_tail"],
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
    _fallback_tried     = False

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        # MAX_PAGES + 1 budget so a single ``/items`` → ``/product_items``
        # fallback retry doesn't eat into the 5-page paging budget.
        for page_idx in range(MAX_PAGES + 1):
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
                "edge=%s graph_url=%s has_first_params=%s token=%s "
                "fallback_tried=%s first_real_attempt=%s",
                page_idx, tenant_id, catalog_id, current_edge, log_url,
                first_params is not None, _mask_token(token),
                _fallback_tried, _first_real_attempt,
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

                # ── Edge fallback (May 2026 #19f) ─────────────────
                # Meta returns code=100 / "nonexisting field" when
                # the catalog isn't a Commerce catalog and doesn't
                # expose ``/items``. Retry once on ``/product_items``
                # before bubbling up the error. We only attempt the
                # fallback on the FIRST page of the FIRST edge —
                # any other failure mode (auth, rate-limit) shouldn't
                # cycle through edge names.
                _meta_msg = str(_meta_err.get("message") or "")
                _is_nonexisting_field = (
                    page_idx == 0
                    and current_edge == META_CATALOG_EDGE_PRIMARY
                    and _meta_err.get("code") == 100
                    and "nonexisting field" in _meta_msg.lower()
                )
                if _is_nonexisting_field and not _fallback_tried:
                    logger.warning(
                        "[META_IMPORT][ENDPOINT_FALLBACK] tenant=%s "
                        "catalog_id=%s from=/%s to=/%s reason=%r",
                        tenant_id, catalog_id,
                        META_CATALOG_EDGE_PRIMARY,
                        META_CATALOG_EDGE_FALLBACK,
                        _meta_msg[:_RESP_PREVIEW_CAP],
                    )
                    _fallback_tried = True
                    # NOTE: we KEEP _first_real_attempt=True so a
                    # subsequent failure still raises hard rather
                    # than being silently swallowed as a "page 1+"
                    # soft-fail.
                    current_edge = META_CATALOG_EDGE_FALLBACK
                    next_url = (
                        f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
                        f"/{catalog_id}/{current_edge}"
                    )
                    # Restore first_params so the retry carries
                    # fields + limit + access_token again.
                    first_params = {
                        "fields":       META_CATALOG_FIELDS,
                        "limit":        str(PAGE_SIZE),
                        "access_token": token,
                    }
                    continue

                if _first_real_attempt:
                    raise MetaCatalogImportError(
                        "meta_http_error",
                        f"Meta returned {resp.status_code}: "
                        f"{_body_preview}",
                        detail={
                            "stage":          "http_error",
                            "status":         resp.status_code,
                            "page":           page_idx,
                            "edge":           current_edge,
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
            # First successful page locks the edge in — any later
            # failure during pagination is treated as a soft-fail.
            _first_real_attempt = False
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

        # Refuse to overwrite a manual row. Manual rows are the
        # merchant's intentional, hand-curated entries — if a Meta
        # import happens to match the same retailer_id (because the
        # merchant typed the same ID in both places) we surface a
        # ``skipped_manual`` counter rather than silently winning.
        if existing is not None and (existing.source or "").lower() == SOURCE_MANUAL:
            report.skipped_manual += 1
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
            "source":       SOURCE_META,
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

        if existing is None:
            p = Product(
                tenant_id        = tenant_id,
                external_id      = meta_id,
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
            # Make sure the retailer-id columns reflect the new
            # semantics — external_id always holds Meta's id, and
            # meta_retailer_id holds the merchant SKU. Backfill on
            # any legacy row that has either column NULL.
            if not (existing.meta_retailer_id or "").strip():
                existing.meta_retailer_id = retailer_id
            # Migrate legacy rows whose external_id is the
            # retailer_id (old behaviour) over to the Meta id. We
            # only overwrite when the new id is non-empty AND the
            # existing value differs from it — preserves rows where
            # external_id was already set by a non-Meta writer.
            if meta_id and (existing.external_id or "").strip() != meta_id:
                existing.external_id = meta_id
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
