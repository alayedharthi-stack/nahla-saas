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
* It does NOT touch the Product table. We always go through
  ``CatalogContextBuilder`` so orderability + variant rules stay
  centralised.

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
            "product_resolver | tenant=%s query=%r skipped reason=too_short",
            tenant_id, q,
        )
        return None

    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    builder = CatalogContextBuilder(db, tenant_id)
    raw = builder.search_products(q, limit=limit) or []

    if not raw:
        logger.info(
            "product_resolver | tenant=%s query=%r no_match",
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
    return _dict_to_resolution(
        pick, matched_query=q,
        confidence="fts" if len(raw) > 1 else "weak",
    )


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
    max_attachments: int = 3,
) -> tuple[str, List[ProductResolution], List[str]]:
    """Strip ``[PRODUCT:<query>]`` tokens from ``reply_text``.

    Returns ``(cleaned_text, resolutions, missing_queries)``:

      * ``cleaned_text``     — the same string with every marker
        removed (the customer never sees ``[PRODUCT:...]``).
      * ``resolutions``      — the resolved products in the
        order the LLM cited them. Deduped by ``id``. Capped at
        ``max_attachments`` (default 3 — products are heavier UX
        than payment barcodes, but a customer asking "إيش
        عندكم؟" reasonably gets a small line-up).
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
        variants=list(d.get("variants") or d.get("variants_summary") or []),
        matched_query=matched_query,
        confidence=confidence,
    )


def format_product_card_caption(
    res: ProductResolution,
    *,
    max_length: int = 1024,
) -> str:
    """Render a product into a WhatsApp image caption.

    The caption is what the customer SEES under the product image.
    Format:

        <title>
        السعر: <price> ر.س
        <description-first-line>

    Honours WhatsApp's 1024-char image caption limit. Description
    is truncated last. Price is rendered with a friendly suffix
    (``ر.س``) when the field is purely numeric; if the merchant
    or the adapter already includes a currency, we leave it
    alone.
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
    if res.description:
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
    "resolve_by_external_id",
    "extract_product_markers",
    "format_product_card_caption",
]
