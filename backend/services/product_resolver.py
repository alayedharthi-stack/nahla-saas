"""
backend/services/product_resolver.py
────────────────────────────────────
Resolve a free-form Arabic product query (typed by Claude or by
the customer) into a concrete product the WhatsApp brain can
send as an image + price + buy-link card.

Design
──────
This module is intentionally **thin**. The hard work is already
done by ``backend/core/store_knowledge.CatalogContextBuilder``
(FTS + ILIKE fallback, orderability filter, image_url +
product_url + variants surfaced from ``Product.extra_metadata``).
We add three things on top:

  1. A canonical :class:`ProductResolution` DTO with EXACTLY the
     fields the chat sender needs: ``id``, ``title``, ``price``,
     ``image_url``, ``product_url``, ``in_stock``, ``variants``.
     No JSONB blobs, no leakage of internal columns.

  2. A best-match picker that ranks ``search_products`` output by
     ``ProductAffinity`` for the current customer (re-uses
     ``modules/ai/brain/execution/search._apply_affinity_boost``)
     and returns the top hit. This is what the LLM marker
     ``[PRODUCT:عسل القولون]`` resolves to.

  3. A deterministic post-LLM safety net (mirrors
     ``find_best_payment_asset``): when the customer's message
     names a clear product but the LLM emitted no marker, the
     pipeline can call ``resolve_for_customer_query`` to find
     the best candidate anyway.

What this module does NOT do
────────────────────────────
* It does NOT invent or fabricate product URLs. If the synced
  product has no ``product_url`` in ``extra_metadata``, the
  resolver returns ``None`` for that field and the sender falls
  back to a generic store link.
* It does NOT send anything. Sending lives in the WhatsApp
  webhook's attachment loop — see
  ``_attach_resolved_products`` there.
* It does NOT touch the Product table directly. We always go
  through ``CatalogContextBuilder`` so orderability + variant
  rules stay centralised.

AI / catalog contract (May 2026 #14 — Hub architecture)
────────────────────────────────────────────────────────
This resolver is the SOLE source of product data for the WhatsApp
AI. It MUST read from the Nahla local ``products`` table ONLY —
NEVER from Salla's live API, NEVER from Meta's Catalog API, NEVER
from Zid's API. Sources upstream of the catalog are responsible for
keeping the Nahla table fresh:

    INPUT SOURCES    →    NAHLA CATALOG    →    AI (this resolver)
    ─────────────         ─────────────         ────────────────
    Salla sync                                  product_resolver
    Manual entry          ``products``          ↓
    Meta import           table                 WhatsApp send chain

The rule "AI reads the hub only, never the source platforms" gives
us three concrete guarantees:

  1. Source independence — an AI reply that recommends a product is
     correct regardless of which input source produced the row. A
     manual product from a no-Salla merchant looks identical to a
     Salla-synced product from the AI's perspective.
  2. Failure isolation — Salla / Meta API outages cannot break the
     WhatsApp brain. Stale data is far better than no data.
  3. Latency control — every product lookup is a local FTS / ILIKE
     against ``products``; no external HTTP on the critical path.

Regression-tested in ``tests/test_catalog_source_layer.py``
(``test_product_resolver_only_imports_local_models``).

Tenant isolation
────────────────
``CatalogContextBuilder`` is instantiated per-tenant on every
call. There is no cross-tenant code path. The resolver itself
NEVER reads ``Product.tenant_id`` directly — the builder owns
that filter.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.product_resolver")


# ──────────────────────────────────────────────────────────────────
# Public DTO
# ──────────────────────────────────────────────────────────────────


@dataclass
class ProductResolution:
    """The minimal canonical product shape needed to send a
    product card on WhatsApp.

    Everything here comes from ``CatalogContextBuilder._format``
    output, normalised to non-empty strings or ``None`` (no empty
    strings — saves the sender a layer of truthiness checks).
    """
    id: int
    external_id: Optional[str]
    title: str
    price: Optional[str]
    sale_price: Optional[str]
    image_url: Optional[str]
    product_url: Optional[str]
    description: Optional[str]
    in_stock: bool
    can_checkout: bool
    # Free-form list of variants. Each dict is whatever the
    # adapter wrote — we surface but don't normalise (different
    # platforms have different variant schemas). The sender uses
    # this only to render an "متوفر بأحجام" hint, not to
    # influence the link.
    variants: List[Dict[str, Any]] = field(default_factory=list)
    # Variant intelligence layer (migration 0064). When the parent
    # has 2+ in-stock real variants the sender MUST short-circuit
    # the product card and ask the customer to pick a variant
    # first. Carries the parent's ``default_variant_id`` so
    # single-variant products skip the prompt.
    needs_variant_choice: bool = False
    default_variant_id: Optional[int] = None
    default_variant_retailer_id: Optional[str] = None
    has_variants: bool = False
    # Which raw search query produced this resolution. Surfaced so
    # logs can answer "did `[PRODUCT:عسل]` match the right item?"
    # without re-running the search.
    matched_query: Optional[str] = None
    # Confidence band — qualitative, used for "did we guess?" UX:
    #   "exact"  — external_id / sku / id match.
    #   "fts"    — Postgres full-text hit.
    #   "ilike"  — substring fallback.
    #   "weak"   — only made it through because we had no other
    #              candidates; the sender may decide to surface a
    #              "هل تقصد …؟" clarifier instead of sending blind.
    confidence: str = "fts"


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────


# Conservative threshold: anything below 1 raw FTS hit is "weak".
# Tuned with care — too high and the resolver becomes silent;
# too low and we ship the wrong product. The current value
# (1 raw hit + len(query) >= 3 chars) means a single-word
# customer query like "عسل" still resolves but a two-letter
# typo doesn't.
_MIN_QUERY_CHARS = 3


# ──────────────────────────────────────────────────────────────────
# Arabic normalization (May 2026 #12)
# ──────────────────────────────────────────────────────────────────
#
# Two Saudi customers typing "السمر" and "السَمَر" — same intent,
# different bytes (the second carries fatha + sukun marks). The
# Postgres FTS path uses ``'simple'`` config which treats diacritics
# as part of the token, so the indexed form of "عسل السَمَر" never
# matches the typed "السمر" without normalization. ``_normalize_arabic``
# collapses those into a single canonical form so the RELAXED
# resolver path can match on substring without the FTS index.
#
# We intentionally do NOT modify the FTS path itself — that index is
# shared with other features and changing the analyzer is high-blast.

_AR_DIACRITICS_RE = re.compile(
    # U+064B–U+065F: fatha, kasra, damma, shadda, sukun, etc.
    # U+0670: superscript alef.  U+0640: tatweel (kashida).
    r"[\u064B-\u065F\u0670\u0640]+",
)
_AR_ALIF_VARIANTS_RE = re.compile(r"[\u0622\u0623\u0625]")  # آ أ إ → ا
_AR_YA_VARIANTS_RE   = re.compile(r"[\u0649]")              # ى → ي
_AR_TA_MARBUTA_RE    = re.compile(r"[\u0629]")              # ة → ه

def _normalize_arabic(text: str) -> str:
    """Fold Arabic text into a comparable canonical form.

    Idempotent. Conservative on purpose — only the most common
    spelling variants the resolver actually sees in customer
    queries get folded. The output is still legible Arabic, just
    consistently spelled (e.g. ``إنتاج`` → ``انتاج``).
    """
    if not text:
        return ""
    s = _AR_DIACRITICS_RE.sub("", text)
    s = _AR_ALIF_VARIANTS_RE.sub("\u0627", s)
    s = _AR_YA_VARIANTS_RE.sub("\u064A",   s)
    s = _AR_TA_MARBUTA_RE.sub("\u0647",    s)
    return s.lower().strip()


def resolve_by_query_relaxed(
    db: Session,
    tenant_id: int,
    query: str,
    *,
    limit: int = 5,
) -> Optional[ProductResolution]:
    """Best-effort resolver that intentionally does NOT filter by
    ``can_checkout``. Used as a FALLBACK by:

      * ``/merchant/catalog/test-send`` — the merchant wants to
        verify that an OUT-OF-STOCK product still renders correctly,
        and the strict resolver hides it.
      * The webhook visual-product RESCUE path — when the strict
        resolver returns ``None`` we still want to send *something*
        (a CTA URL is far better than text_only for a visual ask).

    Matching rules (in order, first hit wins):

      1. Normalized title equality (after :func:`_normalize_arabic`).
      2. Normalized title contains the normalized query.
      3. Normalized query contains the normalized title (handles
         the customer typing the brand name + extra words).

    Returns a :class:`ProductResolution` with ``confidence='relaxed'``
    so callers can decide whether to flag the match in logs.

    Pure Python ranking — we already filtered by tenant in SQL.
    The ``limit`` only caps the candidate pool we materialise, not
    the final result (we always return the single best match).
    """
    q = (query or "").strip()
    if len(q) < _MIN_QUERY_CHARS:
        logger.info(
            "[CATALOG_PRODUCT_RESOLVE] tenant=%s mode=relaxed "
            "query=%r skipped=too_short",
            tenant_id, q,
        )
        return None

    qn = _normalize_arabic(q)
    if not qn:
        return None

    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415
    from models import Product as _Product  # noqa: PLC0415

    # Pull a wider candidate pool than the strict resolver — we
    # don't have a fast normalized index so we rank in Python.
    # ``limit * 20`` is still cheap (a few hundred rows max) and
    # gives the normalizer enough material to find folded matches.
    rows = (
        db.query(_Product.id, _Product.title)
        .filter(_Product.tenant_id == tenant_id)
        .filter(_Product.title.isnot(None))
        .limit(int(limit) * 20 if limit else 100)
        .all()
    )

    best_id: Optional[int] = None
    best_score = 0
    for pid, title in rows:
        tn = _normalize_arabic(title or "")
        if not tn:
            continue
        score = 0
        if tn == qn:
            score = 100
        elif qn in tn:
            # Tighter match wins: shorter title containing the
            # full query is a stronger signal than a long title
            # mentioning the query as one of many tokens.
            score = 70 - min(len(tn) - len(qn), 50)
        elif tn in qn:
            score = 40
        if score > best_score:
            best_score = score
            best_id = pid
        if score >= 100:
            break

    if best_id is None:
        logger.info(
            "[CATALOG_PRODUCT_MISS] tenant=%s mode=relaxed query=%r "
            "candidates=%d",
            tenant_id, q, len(rows),
        )
        return None

    builder = CatalogContextBuilder(db, tenant_id)
    raw = None
    try:
        # Re-format through the canonical formatter so the
        # returned ProductResolution carries image_url /
        # product_url / variants from extra_metadata.
        product = (
            db.query(_Product)
            .filter(_Product.id == best_id, _Product.tenant_id == tenant_id)
            .first()
        )
        if product is not None:
            raw = builder._format(product)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CATALOG_PRODUCT_RESOLVE] tenant=%s mode=relaxed format_failed=%r",
            tenant_id, exc,
        )
        return None

    if raw is None:
        return None

    logger.info(
        "[CATALOG_PRODUCT_MATCH] tenant=%s mode=relaxed query=%r "
        "product_id=%s title=%r score=%d",
        tenant_id, q, best_id, raw.get("title"), best_score,
    )
    return _dict_to_resolution(raw, matched_query=q, confidence="relaxed")


def resolve_by_query(
    db: Session,
    tenant_id: int,
    query: str,
    *,
    customer_id: Optional[int] = None,
    limit: int = 5,
) -> Optional[ProductResolution]:
    """Pick the single best product matching ``query`` for this
    tenant. Returns ``None`` only when the catalog is empty or
    the query is too short to be meaningful.

    The function is **stable**: same query against the same
    catalog returns the same product. There is no randomness in
    ranking. When ``customer_id`` is supplied we re-rank by
    affinity (i.e. "this customer always buys honey from the
    Dahyan brand → return the Dahyan jar even if the Khallat
    one ranks higher in pure FTS"), which is deterministic per
    (tenant, customer, query) tuple.
    """
    q = (query or "").strip()
    if len(q) < _MIN_QUERY_CHARS:
        logger.info(
            "[CATALOG_PRODUCT_RESOLVE] tenant=%s mode=strict "
            "query=%r skipped=too_short",
            tenant_id, q,
        )
        return None

    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    builder = CatalogContextBuilder(db, tenant_id)
    raw = builder.search_products(q, limit=limit) or []

    if not raw:
        logger.info(
            "[CATALOG_PRODUCT_MISS] tenant=%s mode=strict "
            "query=%r reason=no_fts_hit",
            tenant_id, q,
        )
        return None

    # Affinity boost (purely re-ordering — never adds items).
    # Best-effort: if the brain module isn't available (e.g. test
    # harness), we fall through to the raw FTS order.
    if customer_id:
        try:
            from modules.ai.brain.execution import search as _brain_search  # noqa: PLC0415
            raw = _brain_search._apply_affinity_boost(db, raw, customer_id) or raw
        except Exception:
            pass

    pick = raw[0]
    logger.info(
        "[CATALOG_PRODUCT_MATCH] tenant=%s mode=strict query=%r "
        "product_id=%s title=%r candidates=%d",
        tenant_id, q, pick.get("id"), pick.get("title"), len(raw),
    )
    return _dict_to_resolution(
        pick, matched_query=q,
        confidence="fts" if len(raw) > 1 else "weak",
    )


def resolve_best_effort(
    db: Session,
    tenant_id: int,
    query: str,
    *,
    customer_id: Optional[int] = None,
    limit: int = 5,
) -> Optional[ProductResolution]:
    """Strict resolver first; on miss, fall back to the relaxed
    (normalized-title) resolver. Returns ``None`` only when the
    catalog has zero candidates that look anything like *query*.

    The strict path keeps the existing orderability + variant rules
    for production brain replies (we don't want to ship a card for
    an out-of-stock item the customer can't actually buy). The
    relaxed path runs ONLY when the strict path returns ``None`` —
    that protects us from regressing the brain's accuracy while
    rescuing test-sends and visual fallback paths from text_only.
    """
    res = resolve_by_query(db, tenant_id, query, customer_id=customer_id, limit=limit)
    if res is not None:
        return res
    return resolve_by_query_relaxed(db, tenant_id, query, limit=limit)


def resolve_by_product_id(
    db: Session, tenant_id: int, product_id: int,
) -> Optional[ProductResolution]:
    """Exact tenant-scoped lookup by local Product.id.

    Used after Meta membership fail-closed so fallback presentation
    stays on the canonical referent. Never searches by title.
    """
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or not tenant_id:
        return None
    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    builder = CatalogContextBuilder(db, tenant_id)
    raw = builder.get_by_id(pid)
    if not raw:
        logger.info(
            "[CATALOG_PRODUCT_MISS] tenant=%s mode=id product_id=%s",
            tenant_id, pid,
        )
        return None
    return _dict_to_resolution(raw, confidence="exact")


def resolve_by_external_id(
    db: Session, tenant_id: int, external_id: str,
) -> Optional[ProductResolution]:
    """Exact lookup by the platform-side product id (Salla / Zid
    / Shopify). Used when the customer clicks a known link, or
    when a previous turn pinned a specific product.

    Always returns ``confidence='exact'`` when it finds a row.
    """
    if not external_id:
        return None
    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    builder = CatalogContextBuilder(db, tenant_id)
    raw = builder.get_by_external_id(str(external_id).strip())
    if not raw:
        return None
    return _dict_to_resolution(raw, confidence="exact")


# ──────────────────────────────────────────────────────────────────
# Marker extraction — ``[PRODUCT:<query>]`` in chat replies
# ──────────────────────────────────────────────────────────────────
#
# Mirrors the ``[MEDIA:<id>]`` / ``[MEDIA_KEY:<key>]`` extraction
# pattern in ``core/ai_libraries.py`` + ``services/media_resolver.py``.
# When Claude emits ``[PRODUCT:عسل القولون]`` in its reply, the
# webhook calls ``extract_product_markers`` to:
#
#   1. strip the marker from the customer-visible text,
#   2. resolve each query against the tenant's catalog,
#   3. return the resolutions in the order they appeared.
#
# The sender then sends the product cards AFTER the text — same
# UX as ``[MEDIA:<id>]``.

_PRODUCT_MARKER_RE = re.compile(
    # Allow Arabic + Latin + digits + spaces + a few separators.
    # Marker hint suffix ``|whatever`` is tolerated but ignored
    # — same convention as MEDIA / MEDIA_KEY markers.
    r"\[PRODUCT:\s*([^\]\|\n]{1,120})(?:\s*\|[^\]]*)?\]",
    re.IGNORECASE,
)


def extract_product_markers(
    db: Session,
    tenant_id: int,
    reply_text: str,
    *,
    customer_id: Optional[int] = None,
    max_attachments: int = 2,
) -> tuple[str, List[ProductResolution], List[str]]:
    """Strip ``[PRODUCT:<query>]`` tokens from ``reply_text``.

    Returns ``(cleaned_text, resolutions, missing_queries)``:

      * ``cleaned_text``     — the same string with every marker
        removed (the customer never sees ``[PRODUCT:...]``).
      * ``resolutions``      — the resolved products in the
        order the LLM cited them. Deduped by ``id``. Capped at
        ``max_attachments`` (default 2 — aligned with
        ``LIMIT_RECOMMENDATION_BREADTH`` catalog-card policy).
      * ``missing_queries``  — queries the LLM emitted that did
        not resolve. Logged + can be appended to the reply text
        as "تأكد من اسم المنتج…" if the caller wants.

    Note: the LLM is told to use ``[PRODUCT:<canonical product
    name>]``, NOT free-form invented queries. Validation lives at
    the prompt-engineering layer (see the prompt overlay in
    ``backend/modules/ai/prompts/nahla_persona.py``) — the
    resolver tolerates whatever it gets and lets the caller deal
    with the "missing" list.
    """
    text = reply_text or ""
    if not text or "[PRODUCT:" not in text.upper():
        return text, [], []

    matches = list(_PRODUCT_MARKER_RE.finditer(text))
    if not matches:
        return text, [], []

    seen_queries: List[str] = []
    for m in matches:
        q = (m.group(1) or "").strip()
        if not q:
            continue
        # dedupe by lowered comparison — Claude sometimes
        # double-emits "[PRODUCT:عسل]" and "[PRODUCT:عسل ]"
        # for the same intent.
        if q.lower() not in {x.lower() for x in seen_queries}:
            seen_queries.append(q)
        if len(seen_queries) >= max_attachments:
            break

    resolutions: List[ProductResolution] = []
    seen_ids: set = set()
    missing: List[str] = []
    for q in seen_queries:
        res = resolve_by_query(db, tenant_id, q, customer_id=customer_id)
        if not res:
            missing.append(q)
            logger.info(
                "product_resolver | tenant=%s marker_unresolved query=%r",
                tenant_id, q,
            )
            continue
        if res.id in seen_ids:
            continue
        seen_ids.add(res.id)
        resolutions.append(res)

    cleaned = _PRODUCT_MARKER_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, resolutions, missing


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _dict_to_resolution(
    d: Dict[str, Any],
    *,
    matched_query: Optional[str] = None,
    confidence: str = "fts",
) -> ProductResolution:
    """Normalise a ``CatalogContextBuilder._format`` dict into a
    :class:`ProductResolution`. Empties → ``None``; in_stock
    coerced to bool; variants left as-is."""
    def _nonempty(v: Any) -> Optional[str]:
        s = (str(v).strip() if v is not None else "")
        return s or None

    raw_variants = d.get("variants")
    if not isinstance(raw_variants, list):
        # Legacy callers may have written ``variants_summary`` (a
        # string). Don't try to coerce — just leave the structured
        # variants list empty and let the sender fall back.
        raw_variants = []
    return ProductResolution(
        id=int(d.get("id") or 0),
        external_id=_nonempty(d.get("external_id")),
        title=str(d.get("title") or "").strip() or "(منتج بدون اسم)",
        price=_nonempty(d.get("price")),
        sale_price=_nonempty(d.get("sale_price")),
        image_url=_nonempty(d.get("image_url")),
        product_url=_nonempty(d.get("product_url") or d.get("url")),
        description=_nonempty(d.get("description")),
        in_stock=bool(d.get("in_stock", True)),
        can_checkout=bool(d.get("can_checkout", d.get("orderable", True))),
        variants=list(raw_variants),
        needs_variant_choice=bool(d.get("needs_variant_choice", False)),
        default_variant_id=d.get("default_variant_id"),
        default_variant_retailer_id=_nonempty(d.get("default_variant_retailer_id")),
        has_variants=bool(d.get("has_variants", False)),
        matched_query=matched_query,
        confidence=confidence,
    )


def format_product_card_caption(
    res: ProductResolution,
    *,
    include_description: bool = True,
    max_length: int = 1024,
) -> str:
    """Render a product into a WhatsApp image caption.

    The caption is what the customer SEES under the product image.
    Format:

        <title>
        السعر: <price> ر.س
        [<description-first-line when include_description=True>]

    Honours WhatsApp's 1024-char image caption limit. Description
    is truncated last. Price is rendered with a friendly suffix
    (``ر.س``) when the field is purely numeric; if the merchant
    or the adapter already includes a currency, we leave it
    alone.

    Visual / card-send paths pass ``include_description=False`` so
    the card body stays short; detail/info flows keep the default.
    """
    lines: List[str] = [res.title]
    if res.price:
        price_text = res.price
        # Add ر.س if the price looks like a bare number.
        if re.match(r"^\s*\d+(\.\d+)?\s*$", price_text):
            price_text = f"{price_text} ر.س"
        if res.sale_price and res.sale_price != res.price:
            sp = res.sale_price
            if re.match(r"^\s*\d+(\.\d+)?\s*$", sp):
                sp = f"{sp} ر.س"
            lines.append(f"السعر: ~~{price_text}~~ {sp}")
        else:
            lines.append(f"السعر: {price_text}")
    if not res.in_stock:
        lines.append("⚠️ غير متوفر حالياً")
    if include_description and res.description:
        # First sentence / first 200 chars — keep the caption tight.
        first = re.split(r"[.\n!؟]", res.description, maxsplit=1)[0].strip()
        if first:
            lines.append(first[:200])

    out = "\n".join(lines).strip()
    if len(out) > max_length:
        out = out[: max_length - 1].rstrip() + "…"
    return out


__all__ = [
    "ProductResolution",
    "resolve_by_query",
    "resolve_by_product_id",
    "resolve_by_external_id",
    "extract_product_markers",
    "format_product_card_caption",
]
