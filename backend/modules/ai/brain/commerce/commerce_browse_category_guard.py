"""
commerce/commerce_browse_category_guard.py
──────────────────────────────────────────
Platform-wide guard: category-scoped browse must not leak cross-category products.

When the customer asks to browse a specific category noun (e.g. honey / عسل),
catalog results stay inside that category. Cream, oil, and other derivative
forms are excluded unless the customer explicitly mentions them in the same turn.

Operational evidence uses category + title (+ tags) only — never description
copy that may mention honey while the SKU is cream/oil.

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
    r"^(?:وش|ايش|ايه|ما)\s+(?:المنتجات|المتوفر|الانواع|الأنواع)\s*$|"
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
    "خيارات", "الخيارات", "وين", "where",
    "؟", "?", ".", "!", ",",
})

# Product-form markers that usually signal a different category family.
_CROSS_FORM_MARKERS = frozenset({
    "كريم", "cream", "زيت", "oil", "lotion", "serum", "صابون", "soap",
    "shampoo", "شامبو", "balm", "مرهم", "ointment", "gel", "جل",
    "mask", "ماسك", "tonic", "تونيك",
})

# Shared hive/bee tokens must NOT satisfy a honey scope on their own.
_HIVE_BLEED_TOKENS = frozenset({
    "نحل", "النحل", "bee", "bees", "hive", "hives",
})

# Honey subtype nouns — browsing these implies honey, not bee-venom derivatives.
_HONEY_SUBTYPE_SCOPE_HINTS = frozenset({
    "سدر", "طلح", "سمر", "برسيم", "ضهيان", "شوك", "شوكة", "زهر", "مراعي", "مجرى",
    "sidr", "sider", "talh", "samr", "sumr", "clover", "dahyan", "marai",
})

# Generic options/availability browse — inherits locked session category.
_GENERIC_CATEGORY_BROWSE_RE = re.compile(
    r"(?:"
    r"^(?:وش|ايش|ايه|ما|وين|where)\s+(?:ال)?(?:خيارات|الخيارات|متوفر|المتوفر|"
    r"الانواع|الأنواع|options?)\s*[؟?!.]?$|"
    r"^(?:اعرض|وريني|ارسل|أرسل|show|list)\s+(?:ال)?(?:خيارات|الخيارات|options?)\s*[؟?!.]?$|"
    r"^(?:what|show)\s+(?:are\s+)?(?:the\s+)?options\s*[?!.]?$|"
    r"^(?:what|show)\s+(?:do\s+you\s+)?have\s*$|"
    r"^(?:what|show)\s+is\s+available\s*$"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SESSION_SCOPED_SOURCES = frozenset({
    "top_products",
    "top_products_numeric_fallback",
    "top_products_replay_fallback",
    "top_products_start_order",
    "global_browse",
    "global_browse_recovery",
    "show_more",
    "replay",
    "category_browse",
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


def is_generic_category_browse(message: str, query: str = "") -> bool:
    blob = _norm(f"{message or ''} {query or ''}")
    if not blob:
        return False
    return bool(_GENERIC_CATEGORY_BROWSE_RE.search(blob))


def active_category_from_state(state: Any) -> str:
    raw = getattr(state, "commerce_session", None) if state is not None else None
    if isinstance(raw, Mapping):
        return str(raw.get("active_category") or "").strip()
    if isinstance(raw, dict):
        return str(raw.get("active_category") or "").strip()
    return ""


def resolve_browse_category_scope(
    message: str,
    query: str = "",
    *,
    active_category: str = "",
    source: str = "",
) -> Optional[str]:
    """Resolve category scope from message, query, or locked session context."""
    scope = extract_browse_category_scope(message, query)
    if scope:
        scope_norm = _canonical_scope_token(scope)
        if scope_norm in _HONEY_SUBTYPE_SCOPE_HINTS or scope_norm == "عسل":
            return "عسل"
        return scope

    msg_norm = _norm(message or "")
    msg_tokens = set(_tokens(msg_norm))
    if msg_tokens & _HONEY_SUBTYPE_SCOPE_HINTS or any(
        hint in msg_norm for hint in _HONEY_SUBTYPE_SCOPE_HINTS
    ):
        return "عسل"

    locked = _canonical_scope_token(active_category or "")
    if locked != "عسل":
        return None

    src = str(source or "").strip().lower()
    if is_generic_category_browse(message, query):
        return "عسل"
    if src in _SESSION_SCOPED_SOURCES:
        return "عسل"
    return None


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
    active_category: str = "",
) -> bool:
    """True when this turn should keep catalog results inside one category."""
    scope = resolve_browse_category_scope(
        message,
        query,
        active_category=active_category,
        source=source,
    )
    if not scope:
        return False

    src = str(source or "").strip().lower()
    if src in {"global_browse", "global_browse_recovery", "top_products"}:
        return True
    if _canonical_scope_token(active_category or "") == "عسل":
        return True

    try:
        from .product_breadth_policy import global_availability_browse_requested  # noqa: PLC0415

        if global_availability_browse_requested(message or "") and not scope:
            return False
    except Exception:  # noqa: BLE001
        logger.exception("[BROWSE_CATEGORY_GUARD] global browse check failed")

    return True


def _product_tags(product: Mapping[str, Any]) -> List[str]:
    raw = product.get("tags")
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    if isinstance(raw, str) and raw.strip():
        return [t.strip() for t in raw.split(",") if t.strip()]
    meta = product.get("metadata")
    if isinstance(meta, dict):
        meta_tags = meta.get("tags")
        if isinstance(meta_tags, list):
            return [str(t) for t in meta_tags if t]
    return []


def _product_identity_blob(product: Mapping[str, Any]) -> str:
    """Category + title + tags only — description must not widen scope."""
    parts = [
        str(product.get("category") or ""),
        str(product.get("title") or ""),
        " ".join(_product_tags(product)),
    ]
    return _norm(" ".join(p for p in parts if p))


def _text_has_scope_token(text: str, scope: str) -> bool:
    if not text or not scope:
        return False
    variants = _scope_variants(scope)
    tokens = set(_tokens(text))
    for variant in variants:
        if variant in tokens:
            return True
        if re.search(rf"(?<!\w){re.escape(variant)}(?!\w)", text):
            return True
    return False


def _hive_only_match(identity: str, scope: str) -> bool:
    """True when identity only shares hive tokens, not the requested category."""
    scope_norm = _canonical_scope_token(scope)
    if scope_norm != "عسل":
        return False
    if _text_has_scope_token(identity, scope_norm):
        return False
    identity_tokens = set(_tokens(identity))
    return bool(identity_tokens & _HIVE_BLEED_TOKENS)


def _product_matches_scope(product: Mapping[str, Any], scope: str) -> bool:
    category = _norm(str(product.get("category") or ""))
    title = _norm(str(product.get("title") or ""))
    tags_blob = _norm(" ".join(_product_tags(product)))
    identity = _product_identity_blob(product)

    if _hive_only_match(identity, scope):
        return False

    # Structured category field is the strongest operational signal.
    if category and _text_has_scope_token(category, scope):
        return True

    if title and _text_has_scope_token(title, scope):
        return True

    if tags_blob and _text_has_scope_token(tags_blob, scope):
        return True

    return False


def _identity_has_cross_form(product: Mapping[str, Any]) -> Optional[str]:
    identity = _product_identity_blob(product)
    for marker in _CROSS_FORM_MARKERS:
        marker_norm = _norm(marker)
        if marker_norm in _tokens(identity) or marker_norm in identity.split():
            return marker_norm
        if re.search(rf"(?<!\w){re.escape(marker_norm)}(?!\w)", identity):
            return marker_norm
    return None


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
    cross_form = _identity_has_cross_form(product)
    if cross_form and not _customer_mentioned_form(message, cross_form):
        return True

    if _product_matches_scope(product, scope):
        return False

    return True


def filter_products_to_browse_category(
    products: Sequence[Mapping[str, Any]],
    *,
    message: str,
    query: str = "",
    source: str = "",
    active_category: str = "",
) -> List[Dict[str, Any]]:
    """Keep only in-scope products for category-scoped browse turns."""
    items = [dict(p) for p in (products or []) if isinstance(p, Mapping)]
    if not items:
        return items

    if not is_category_scoped_browse(
        message,
        query,
        source=source,
        active_category=active_category,
    ):
        return items

    scope = resolve_browse_category_scope(
        message,
        query,
        active_category=active_category,
        source=source,
    )
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
            "[BROWSE_CATEGORY_GUARD] scope=%r in=%d out=%d dropped=%d source=%r preview=%r",
            scope,
            len(items),
            len(kept),
            dropped,
            source,
            (message or "")[:80],
        )
    return kept


def filter_products_for_browse_turn(
    products: Sequence[Mapping[str, Any]],
    *,
    message: str = "",
    query: str = "",
    source: str = "",
    last_browse_query: str = "",
    active_category: str = "",
    state: Any = None,
) -> List[Dict[str, Any]]:
    """Shared entry for search, compose, pipeline, and replay paths."""
    locked_category = str(active_category or active_category_from_state(state) or "").strip()
    effective_query = str(query or last_browse_query or "").strip()
    if (
        not effective_query
        and not str(message or "").strip()
        and not locked_category
    ):
        return [dict(p) for p in (products or []) if isinstance(p, Mapping)]

    scoped = filter_products_to_browse_category(
        products,
        message=message or "",
        query=effective_query,
        source=source,
        active_category=locked_category,
    )

    try:
        from .honey_browse_strategy import apply_honey_browse_strategy  # noqa: PLC0415

        return apply_honey_browse_strategy(
            scoped,
            message=message or "",
            query=effective_query,
            active_category=locked_category,
            source=source,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[BROWSE_CATEGORY_GUARD] honey browse strategy failed")
        return scoped


__all__ = [
    "active_category_from_state",
    "extract_browse_category_scope",
    "filter_products_for_browse_turn",
    "filter_products_to_browse_category",
    "is_category_scoped_browse",
    "is_generic_category_browse",
    "resolve_browse_category_scope",
    "should_exclude_cross_category_product",
]
