"""
commerce/honey_browse_strategy.py
─────────────────────────────────
Honey-store browse ladder (P0-C): when the customer is in a honey context
but has not picked a type yet, surface one representative per honey type
(طلح / سمر / سدر …) instead of random SKUs, creams, or multiple sizes.

Operational — deterministic title/token evidence only.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .commerce_browse_category_guard import (
    resolve_browse_category_scope,
)

logger = logging.getLogger("nahla.brain.commerce.honey_browse")

_DIA = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")

_HONEY_TYPE_RULES: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("طلح", re.compile(r"طلح|talh", re.I)),
    ("سمر", re.compile(r"سمر|samr|sumr", re.I)),
    ("سدر", re.compile(r"سدر|sidr|sider", re.I)),
    ("برسيم", re.compile(r"برسيم|clover", re.I)),
    ("ضهيان", re.compile(r"ضهيان|dahyan", re.I)),
    ("شوكة", re.compile(r"شوك(?:ة)?|thistle", re.I)),
    ("زهر", re.compile(r"زهر(?:ي)?|floral", re.I)),
    ("مراعي", re.compile(r"مراعي|marai", re.I)),
    ("مجرى", re.compile(r"مجر(?:ى|ي)", re.I)),
)

_SIZE_OR_WEIGHT_RE = re.compile(
    r"(?:"
    r"250|500|750|1000|"
    r"ربع|نصف|"
    r"كilo|كيلo|كيلو|كجم|kg|"
    r"gram|جرام|ج\s*ر\s*ا\s*م|"
    r"\d+\s*(?:g|gr|جرام|كيلo|كيلو)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_GENERIC_BROWSE_RE = re.compile(
    r"(?:"
    r"^(?:وش|ايش|ايه|ما|وين|where)\s+(?:ال)?(?:خيارات|الخيارات|متوفر|المتوفر|"
    r"الانواع|الأنواع|options?)\s*[؟?!.]?$|"
    r"^(?:اعرض|وريني|ارسل|أرسل|show|list)\s+(?:ال)?(?:خيارات|الخيارات|options?)\s*[؟?!.]?$|"
    r"^(?:what|show)\s+(?:are\s+)?(?:the\s+)?options\s*[?!.]?$"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_TYPE_BROWSE_SOURCES = frozenset({
    "top_products",
    "top_products_numeric_fallback",
    "top_products_replay_fallback",
    "top_products_start_order",
    "category_browse",
    "global_browse",
    "global_browse_recovery",
})


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text).strip().lower())
    s = _ZW.sub("", s)
    s = _DIA.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"\s+", " ", s).strip()


def classify_honey_type(title: str) -> Optional[str]:
    blob = _norm(title or "")
    if not blob:
        return None
    for label, pattern in _HONEY_TYPE_RULES:
        if pattern.search(blob):
            return label
    return None


def customer_specified_honey_type(message: str, query: str = "") -> Optional[str]:
    types = customer_specified_honey_types(message, query)
    return types[0] if types else None


def customer_specified_honey_types(message: str, query: str = "") -> List[str]:
    blob = _norm(f"{message or ''} {query or ''}")
    if not blob:
        return []
    found: List[str] = []
    for label, pattern in _HONEY_TYPE_RULES:
        if pattern.search(blob) and label not in found:
            found.append(label)
    return found


def filter_products_to_requested_honey_types(
    products: Sequence[Mapping[str, Any]],
    requested_types: Sequence[str],
) -> List[Dict[str, Any]]:
    """Keep only catalog rows whose honey type matches an explicit customer ask."""
    labels = [str(t or "").strip() for t in (requested_types or []) if str(t or "").strip()]
    if not labels:
        return [dict(p) for p in products if isinstance(p, Mapping)]
    label_set = set(labels)
    kept: List[Dict[str, Any]] = []
    for row in products:
        if not isinstance(row, Mapping):
            continue
        honey_type = classify_honey_type(str(row.get("title") or ""))
        if honey_type and honey_type in label_set:
            kept.append(dict(row))
    return kept


def customer_specified_size(message: str) -> bool:
    return bool(_SIZE_OR_WEIGHT_RE.search(message or ""))


def is_generic_honey_options_browse(message: str, query: str = "") -> bool:
    """Deprecated alias — use :func:`is_generic_options_browse`."""
    return is_generic_options_browse(message, query)


def is_generic_options_browse(message: str, query: str = "") -> bool:
    blob = _norm(f"{message or ''} {query or ''}")
    if not blob:
        return False
    return bool(_GENERIC_BROWSE_RE.search(blob))


def catalog_has_honey_skus_in_products(
    products: Sequence[Mapping[str, Any]],
) -> bool:
    """True when catalog rows contain honey-family SKUs (tenant evidence)."""
    for row in products or []:
        if not isinstance(row, Mapping):
            continue
        title = str(row.get("title") or "")
        if classify_honey_type(title):
            return True
        if "عسل" in _norm(title):
            return True
    return False


def should_collapse_to_honey_types(
    message: str,
    *,
    query: str = "",
    active_category: str = "",
    source: str = "",
    products: Sequence[Mapping[str, Any]] = (),
) -> bool:
    """Deprecated alias — use :func:`should_collapse_to_category_types`."""
    return should_collapse_to_category_types(
        message,
        query=query,
        active_category=active_category,
        source=source,
        products=products,
    )


def should_collapse_to_category_types(
    message: str,
    *,
    query: str = "",
    active_category: str = "",
    source: str = "",
    products: Sequence[Mapping[str, Any]] = (),
) -> bool:
    scope = resolve_browse_category_scope(
        message,
        query,
        active_category=active_category,
        source=source,
    )
    locked = str(active_category or "").strip()
    if not scope and locked and locked != "عسل":
        src = str(source or "").strip().lower()
        if is_generic_options_browse(message, query) or src in _TYPE_BROWSE_SOURCES:
            scope = locked
    if scope == "عسل":
        if products and not catalog_has_honey_skus_in_products(products):
            return False
        if customer_specified_honey_type(message, query):
            return False
        if customer_specified_size(message):
            return False
        src = str(source or "").strip().lower()
        if is_generic_options_browse(message, query):
            return True
        if src in _TYPE_BROWSE_SOURCES and not customer_specified_honey_type(message, query):
            return True
        return False
    if scope:
        if customer_specified_size(message):
            return False
        src = str(source or "").strip().lower()
        if is_generic_options_browse(message, query):
            return True
        if src in _TYPE_BROWSE_SOURCES:
            return True
    return False


def _product_is_available(product: Mapping[str, Any]) -> bool:
    """True when catalog evidence shows the SKU is orderable/in stock."""
    try:
        from core.catalog import is_catalog_active  # noqa: PLC0415

        return bool(is_catalog_active(product))
    except Exception:  # noqa: silent-ok — is_catalog_active optional; fall back to row stock fields
        pass

    orderable = product.get("orderable", product.get("can_checkout"))
    if orderable is False:
        return False

    in_stock = product.get("in_stock")
    if in_stock is False:
        return False

    avail = product.get("available")
    if avail is False:
        return False

    qty = product.get("quantity")
    if qty is None:
        qty = product.get("stock")
    if qty is not None:
        try:
            return int(qty) > 0
        except (TypeError, ValueError):
            return True

    return True


def _available_products(
    products: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    return [dict(p) for p in products if isinstance(p, Mapping) and _product_is_available(p)]


def _size_rank(title: str) -> tuple[int, int]:
    """Prefer type representatives without weight tokens; then smaller weights."""
    blob = title or ""
    if not _SIZE_OR_WEIGHT_RE.search(blob):
        return (0, 0)
    weight_match = re.search(r"(\d+)", blob)
    weight = int(weight_match.group(1)) if weight_match else 9999
    return (1, weight)


def collapse_products_to_honey_types(
    products: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep one available catalog row per honey type — catalog-grounded only."""
    available = _available_products(products)
    if not available:
        return []

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    plain_honey: List[Dict[str, Any]] = []

    for row in available:
        honey_type = classify_honey_type(str(row.get("title") or ""))
        if honey_type:
            buckets.setdefault(honey_type, []).append(row)
        elif "عسل" in _norm(str(row.get("title") or "")):
            plain_honey.append(row)

    if not buckets and not plain_honey:
        return available

    picked: List[Dict[str, Any]] = []
    skipped_unavailable_types: List[str] = []
    all_types = {label for label, _ in _HONEY_TYPE_RULES}
    for label in sorted(buckets.keys()):
        items = buckets[label]
        if not items:
            skipped_unavailable_types.append(label)
            continue
        rep = sorted(
            items,
            key=lambda r: _size_rank(str(r.get("title") or "")),
        )[0]
        picked.append(rep)

    for label in sorted(all_types - set(buckets.keys())):
        if any(
            classify_honey_type(str((p or {}).get("title") or "")) == label
            for p in (products or [])
            if isinstance(p, Mapping)
        ):
            skipped_unavailable_types.append(label)

    if plain_honey and not picked:
        picked.extend(plain_honey[:5])
    elif plain_honey:
        picked.extend(plain_honey[: max(0, 5 - len(picked))])

    if picked or skipped_unavailable_types:
        logger.info(
            "[HONEY_BROWSE] collapsed in=%d available_in=%d out=%d types=%s skipped_unavailable=%s",
            len(products),
            len(available),
            len(picked),
            sorted(buckets.keys()),
            skipped_unavailable_types,
        )
    return picked or available


def _cluster_key_from_title(title: str) -> str:
    """First significant token cluster for generic browse representatives."""
    norm = _norm(title or "")
    if not norm:
        return ""
    without_size = _SIZE_OR_WEIGHT_RE.sub(" ", norm).strip()
    tokens = [t for t in without_size.split() if len(t) >= 2]
    return tokens[0] if tokens else norm[:24]


def collapse_products_to_category_representatives(
    products: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep one available catalog row per title cluster — category-agnostic."""
    available = _available_products(products)
    if not available:
        return []
    if len(available) <= 5:
        return available

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in available:
        key = _cluster_key_from_title(str(row.get("title") or ""))
        if not key:
            key = str(row.get("id") or row.get("external_id") or len(buckets))
        buckets.setdefault(key, []).append(row)

    picked: List[Dict[str, Any]] = []
    for key in sorted(buckets.keys()):
        items = buckets[key]
        rep = sorted(
            items,
            key=lambda r: _size_rank(str(r.get("title") or "")),
        )[0]
        picked.append(rep)

    if picked:
        logger.info(
            "[CATEGORY_BROWSE] collapsed in=%d available_in=%d out=%d clusters=%d",
            len(products),
            len(available),
            len(picked),
            len(buckets),
        )
    return picked[:8] or available


def apply_category_browse_strategy(
    products: Sequence[Mapping[str, Any]],
    *,
    message: str = "",
    query: str = "",
    active_category: str = "",
    source: str = "",
) -> List[Dict[str, Any]]:
    items = [dict(p) for p in (products or []) if isinstance(p, Mapping)]
    if not items:
        return items

    requested_types = customer_specified_honey_types(message, query)
    if requested_types and catalog_has_honey_skus_in_products(items):
        filtered = filter_products_to_requested_honey_types(items, requested_types)
        if filtered:
            items = filtered

    if not should_collapse_to_category_types(
        message,
        query=query,
        active_category=active_category,
        source=source,
        products=items,
    ):
        return items

    scope = resolve_browse_category_scope(
        message,
        query,
        active_category=active_category,
        source=source,
    )
    if scope == "عسل" and catalog_has_honey_skus_in_products(items):
        return collapse_products_to_honey_types(items)
    return collapse_products_to_category_representatives(items)


def apply_honey_browse_strategy(
    products: Sequence[Mapping[str, Any]],
    *,
    message: str = "",
    query: str = "",
    active_category: str = "",
    source: str = "",
) -> List[Dict[str, Any]]:
    """Deprecated alias — use :func:`apply_category_browse_strategy`."""
    return apply_category_browse_strategy(
        products,
        message=message,
        query=query,
        active_category=active_category,
        source=source,
    )


__all__ = [
    "apply_category_browse_strategy",
    "apply_honey_browse_strategy",
    "catalog_has_honey_skus_in_products",
    "classify_honey_type",
    "collapse_products_to_category_representatives",
    "collapse_products_to_honey_types",
    "customer_specified_honey_type",
    "customer_specified_honey_types",
    "filter_products_to_requested_honey_types",
    "is_generic_honey_options_browse",
    "is_generic_options_browse",
    "should_collapse_to_category_types",
    "should_collapse_to_honey_types",
]
