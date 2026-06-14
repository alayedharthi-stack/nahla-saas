"""
commerce/commerce_browse_category_guard.py
──────────────────────────────────────────
Platform-wide guard: category-scoped browse must not leak cross-category products.

When the customer asks to browse a specific category noun (e.g. honey / عسل),
catalog results stay inside that category. Cream, oil, and other derivative
forms are excluded unless the customer explicitly mentions them in the same turn.

Operational — deterministic token/evidence only; no LLM wording.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger("nahla.brain.commerce.browse_category_guard")

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")

# Browse / inventory lead-ins stripped before scope extraction.
_BROWSE_LEAD_RE = re.compile(
    r"^(?:"
    r"(?:وش|ايش|ايه|ما|what|show|list|display|browse|send|give)\s+"
    r"(?:عندكم|عندك|لديكم|لديك|available|have|me|us|the|ال)?\s*"
    r"|(?:ابي|أبي|ابغ|أبغ|اريد|أريد|want|need)\s+"
    r"|(?:اعرض|وريني|ارسل|أرسل|show|display)\s+(?:ال|the\s+)?"
    r"|(?:انواع|أنواع|types?\s+(?:of\s+)?(?:ال|the\s+)?)"
    r")+",
    re.UNICODE | re.IGNORECASE,
)

# Store-wide browse — no category scope unless a category noun is explicit.
_GLOBAL_ONLY_RE = re.compile(
    r"(?:"
    r"^(?:وش|ايش|ايه|ما)\s+(?:عندكم|عندك|لديكم|لديك)\s*$|"
    r"^(?:وش|ايش|ايه)\s+(?:المنتجات|المتوفر|الانواع|الأنواع)\s*$|"
    r"^(?:اعرض|وريني|ارسل|أرسل)\s+(?:كل\s+)?(?:المنتجات|المتوفر)\s*$|"
    r"^(?:what|show)\s+(?:do\s+you\s+)?have\s*$|"
    r"^(?:what|show)\s+is\s+available\s*$|"
    r"^(?:show|list|display)\s+(?:all\s+)?products\s*$|"
    r"^(?:top\s+products|best\s+sellers)\s*$"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SCOPE_STOPWORDS = frozenset({
    "وش", "ايش", "ايه", "ما", "عندكم", "عندك", "لديكم", "لديك",
    "متوفر", "متوفره", "متاح", "available", "products", "product",
    "المنتجات", "منتج", "منتجات", "الانواع", "انواع", "أنواع",
    "اعرض", "وريني", "ارسل", "أرسل", "show", "display", "browse",
    "ابي", "أبي", "ابغ", "أبغ", "اريد", "أريد", "want", "need",
    "the", "of", "all", "كل", "جميع", "please", "pls",
    "what", "do", "you", "have", "is", "are", "me", "us", "list",
    "collection", "options", "option", "items", "item", "line", "lines",
    "season", "seasonal", "batch", "inventory", "stock",
    "؟", "?", ".", "!", ",",
})

# Product-form markers that usually signal a different category family.
_CROSS_FORM_MARKERS = frozenset({
    "كريم", "cream", "زيت", "oil", "lotion", "serum", "صابون", "soap",
    "shampoo", "شامبو", "balm", "مرهم", "ointment", "gel", "جل",
    "mask", "ماسك", "tonic", "تونيك",
})

# Common Arabic broken-plural → singular hints (platform-wide, not merchant-specific).
_PLURAL_TO_SINGULAR = (
    (re.compile(r"^اعسال$|^أعسال$|^الاعسال$|^الأعسال$", re.I), "عسل"),
    (re.compile(r"^زيوت$|^الزيوت$", re.I), "زيت"),
    (re.compile(r"^كريمات$|^الكريمات$", re.I), "كريم"),
)


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text).strip().lower())
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"\s+", " ", s).strip()


def _tokens(text: str) -> List[str]:
    norm = _norm(text)
    if not norm:
        return []
    return [t for t in re.split(r"[\s,،]+", norm) if t and len(t) >= 2]


def _canonical_scope_token(token: str) -> str:
    raw = _norm(token)
    if not raw:
        return ""
    raw = re.sub(r"^(?:ال|the)\s+", "", raw)
    for pattern, singular in _PLURAL_TO_SINGULAR:
        if pattern.search(raw):
            return singular
    return raw


def _is_valid_scope_token(token: str) -> bool:
    tok = _canonical_scope_token(token)
    if not tok or tok in _SCOPE_STOPWORDS:
        return False
    if tok.isdigit():
        return False
    if tok.isascii() and len(tok) < 3:
        return False
    return len(tok) >= 2


def _scope_variants(scope: str) -> frozenset[str]:
    base = _canonical_scope_token(scope)
    if not base:
        return frozenset()
    variants = {base}
    if base.startswith("ال") and len(base) > 3:
        variants.add(base[2:])
    elif len(base) >= 2:
        variants.add(f"ال{base}")
    return frozenset(v for v in variants if v)


def extract_browse_category_scope(
    message: str,
    query: str = "",
) -> Optional[str]:
    """Return the primary category noun when browse is category-scoped."""
    msg = (message or "").strip()
    q = (query or "").strip()
    if not msg and not q:
        return None

    msg_norm = _norm(msg)
    if msg_norm and _GLOBAL_ONLY_RE.search(msg_norm):
        return None

    try:
        from ..product_discovery_gate import (  # noqa: PLC0415
            extract_types_overview_query,
            has_types_overview_ask,
        )

        if has_types_overview_ask(msg, q):
            subject = extract_types_overview_query(msg) or q
            scope = _canonical_scope_token(subject)
            if _is_valid_scope_token(scope):
                return scope
    except Exception:  # noqa: BLE001
        logger.exception("[BROWSE_CATEGORY_GUARD] types_overview extract failed")

    for candidate in (q, msg):
        if not candidate:
            continue
        stripped = _BROWSE_LEAD_RE.sub("", _norm(candidate)).strip(" ؟?!.")
        stripped = re.sub(r"^(?:ال|the)\s+", "", stripped)
        for tok in _tokens(stripped):
            scope = _canonical_scope_token(tok)
            if _is_valid_scope_token(scope):
                return scope

    return None


def is_category_scoped_browse(
    message: str,
    query: str = "",
    *,
    source: str = "",
) -> bool:
    """True when this turn should keep catalog results inside one category."""
    src = str(source or "").strip().lower()
    if src in {"global_browse", "global_browse_recovery", "top_products"}:
        if not extract_browse_category_scope(message, query):
            return False

    try:
        from .product_breadth_policy import global_availability_browse_requested  # noqa: PLC0415

        if global_availability_browse_requested(message or "") and not extract_browse_category_scope(
            message, query
        ):
            return False
    except Exception:  # noqa: BLE001
        logger.exception("[BROWSE_CATEGORY_GUARD] global browse check failed")

    return bool(extract_browse_category_scope(message, query))


def _product_text_blob(product: Mapping[str, Any]) -> str:
    parts = [
        str(product.get("title") or ""),
        str(product.get("category") or ""),
        str(product.get("description") or "")[:120],
    ]
    return _norm(" ".join(p for p in parts if p))


def _product_matches_scope(product: Mapping[str, Any], scope: str) -> bool:
    blob = _product_text_blob(product)
    if not blob or not scope:
        return False
    variants = _scope_variants(scope)
    blob_tokens = set(_tokens(blob))
    for variant in variants:
        if variant in blob_tokens:
            return True
        if re.search(rf"(?<!\w){re.escape(variant)}(?!\w)", blob):
            return True
    return False


def _customer_mentioned_form(message: str, form_marker: str) -> bool:
    msg_norm = _norm(message or "")
    marker = _norm(form_marker)
    if not msg_norm or not marker:
        return False
    return marker in msg_norm.split() or marker in msg_norm


def should_exclude_cross_category_product(
    product: Mapping[str, Any],
    *,
    scope: str,
    message: str,
) -> bool:
    """Exclude when product is outside scope or an unrequested derivative form."""
    if _product_matches_scope(product, scope):
        return False

    blob = _product_text_blob(product)
    msg = message or ""

    for marker in _CROSS_FORM_MARKERS:
        marker_norm = _norm(marker)
        if marker_norm in blob and not _customer_mentioned_form(msg, marker_norm):
            return True

    return True


def filter_products_to_browse_category(
    products: Sequence[Mapping[str, Any]],
    *,
    message: str,
    query: str = "",
    source: str = "",
) -> List[Dict[str, Any]]:
    """Keep only in-scope products for category-scoped browse turns."""
    items = [dict(p) for p in (products or []) if isinstance(p, Mapping)]
    if not items:
        return items

    if not is_category_scoped_browse(message, query, source=source):
        return items

    scope = extract_browse_category_scope(message, query)
    if not scope:
        return items

    kept: List[Dict[str, Any]] = []
    dropped = 0
    for product in items:
        if should_exclude_cross_category_product(product, scope=scope, message=message):
            dropped += 1
            continue
        kept.append(product)

    if dropped:
        logger.info(
            "[BROWSE_CATEGORY_GUARD] scoped tenant=? scope=%r in=%d out=%d dropped=%d preview=%r",
            scope,
            len(items),
            len(kept),
            dropped,
            (message or "")[:80],
        )
    return kept


__all__ = [
    "extract_browse_category_scope",
    "filter_products_to_browse_category",
    "is_category_scoped_browse",
    "should_exclude_cross_category_product",
]
