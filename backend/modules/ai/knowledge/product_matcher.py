"""
backend/modules/ai/knowledge/product_matcher.py
───────────────────────────────────────────────
Phase 3.2 — Fuzzy product matcher used during knowledge-draft
approval flow.

Given a piece of merchant text (the body of a proposed knowledge
section) and the tenant's catalog, return the most likely products
the text is talking about. We don't need state-of-the-art NLP here:
the merchant catalog is small (10s–100s of products) and Arabic
title overlap is a strong enough signal. The matcher trades recall
for precision — we'd rather suggest nothing than suggest the wrong
product.

Algorithm
─────────
1. Normalise the text and each product title using
   :func:`normalize_arabic` (strips diacritics, unifies alef forms,
   removes punctuation).
2. Build a set of meaningful tokens from each side (drop stopwords
   and tokens of length < 2).
3. For each product, the score = (overlap_tokens) / (product_token_count).
   We weight by product tokens (not text tokens) so a long merchant
   note doesn't artificially inflate matches for short product names.
4. Return only matches above ``min_confidence`` (default 0.5),
   sorted by score desc, capped at ``limit`` (default 3).

This module is intentionally IO-free: callers pass in the tenant's
products. No DB session, no HTTP call. That keeps it cheap to test
and trivial to memoize per-tenant later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple


# Arabic-aware normalisation. Keep this list small and intentional;
# expanding it later is safe but every change should ship with a
# regression test in ``test_knowledge_phase1.py``.
_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_NON_WORD = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)


# Common Arabic stopwords + storefront filler. We keep it tight so
# real-but-short product words ("عسل", "زيت") never get filtered.
_STOPWORDS = frozenset({
    "من", "في", "على", "عن", "إلى", "هذا", "هذه", "ذلك", "تلك",
    "كل", "جميع", "بعض", "أو", "أي", "أيها", "أنه", "إن", "أن",
    "كما", "حيث", "إذا", "ثم", "قد", "لقد", "لا", "لم", "لن",
    "ما", "مع", "هو", "هي", "هم", "هن", "أنا", "نحن", "أنت",
    "the", "and", "for", "with", "without", "of", "to", "in",
    "is", "are", "by", "on", "at", "or", "as", "an", "a",
})


@dataclass(frozen=True)
class CatalogProductForMatch:
    """Subset of :class:`Product` columns the matcher needs."""

    id: int
    title: str
    sku: Optional[str]
    external_id: Optional[str]


@dataclass(frozen=True)
class ProductMatch:
    product_id: int
    title: str
    confidence: float
    matched_tokens: Tuple[str, ...]


def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه")
    text = text.replace("ـ", "")
    return text.casefold()


def tokenize(text: str) -> List[str]:
    norm = normalize_arabic(text)
    raw = [t for t in _NON_WORD.split(norm) if t]
    return [t for t in raw if len(t) >= 2 and t not in _STOPWORDS]


def match_products(
    text: str,
    products: Iterable[CatalogProductForMatch],
    *,
    limit: int = 3,
    min_confidence: float = 0.5,
) -> List[ProductMatch]:
    """Return the top-``limit`` products that look like ``text``."""
    text_tokens = set(tokenize(text))
    if not text_tokens:
        return []
    # Pre-compute the full normalized text once so SKU/external_id
    # substring matches don't get confused by the tokenizer dropping
    # punctuation (e.g. ``SDR-100`` would tokenize as ``sdr`` + ``100``,
    # neither of which equals the raw SKU string).
    text_norm = normalize_arabic(text)

    candidates: List[ProductMatch] = []
    for p in products:
        title_tokens = tokenize(p.title or "")
        if not title_tokens:
            continue
        # SKU / external_id are exact-only signals — used as a cheap
        # boost when the merchant typed an SKU directly.
        sku_norm = normalize_arabic(p.sku or "")
        ext_norm = normalize_arabic(p.external_id or "")
        title_token_set = set(title_tokens)
        overlap = text_tokens & title_token_set
        if not overlap and not (
            sku_norm and sku_norm in text_norm
        ) and not (
            ext_norm and ext_norm in text_norm
        ):
            continue

        sku_match = bool(
            (sku_norm and sku_norm in text_norm)
            or (ext_norm and ext_norm in text_norm)
        )
        # Token-overlap score, weighted by the product's token count
        # so a long merchant note can't artificially inflate a short
        # product title's match.
        score = len(overlap) / len(title_token_set) if title_token_set else 0.0

        # Anti-false-positive rule: a single shared token across a
        # multi-word product title is too weak by itself (think the
        # generic word "عسل" in a catalog of 20 honey variants).
        # Require either (a) the SKU/external_id matches verbatim,
        # (b) the overlap covers the WHOLE product title, or (c)
        # at least 2 distinct overlapping tokens.
        if not sku_match and len(overlap) < 2 and len(title_token_set) > 1:
            continue

        if sku_match:
            score = max(score, 0.9)

        if score < min_confidence:
            continue

        candidates.append(
            ProductMatch(
                product_id=int(p.id),
                title=p.title or f"product-{p.id}",
                confidence=round(min(1.0, score), 3),
                matched_tokens=tuple(sorted(overlap)),
            )
        )

    candidates.sort(key=lambda m: (-m.confidence, m.title))
    return candidates[:limit]
