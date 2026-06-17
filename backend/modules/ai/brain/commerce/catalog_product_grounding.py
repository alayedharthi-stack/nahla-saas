"""
commerce/catalog_product_grounding.py
─────────────────────────────────────
Verified catalog product titles for customer-facing product lists.

Platform-wide: product names in replies must come from synced catalog
evidence — never LLM memory, KB marketing copy, or hardcoded examples.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from ..types import BrainContext

_HONEY_RE = re.compile(r"عسل", re.UNICODE)

_SEASONAL_AVAILABILITY_RE = re.compile(
    r"(?:"
    r"متى\s+(?:يجي|يوصل|ينزل|يتوفر|يكون|يوصلون)|"
    r"امتى\s+(?:يجي|يوصل|ينزل|يتوفر)|"
    r"متى\s+(?:بيكون|راح\s+يجي|راح\s+يوصل)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SEASONAL_PRODUCT_TAIL_RE = re.compile(
    r"(?:ال)?(?:عسل|منتج)\s+(.+?)(?:\s*[؟?!.]|$)",
    re.UNICODE | re.IGNORECASE,
)

_SAFE_NO_CATALOG_AR = (
    "ما ظهرت لي قائمة المنتجات المتوفرة الآن، "
    "خليني أتأكد من الكتالوج قبل أعطيك الأنواع."
)

_SAFE_SEASONAL_UNCERTAIN_AR = (
    "ما أقدر أؤكد موعد توفره من الكتالوج الحالي."
)


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t).strip()


def _product_title(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("title") or "").strip()


def collect_verified_catalog_titles(
    *,
    state: Any = None,
    facts: Any = None,
    candidates: Optional[Sequence[Dict[str, Any]]] = None,
    recommended: Optional[Sequence[Dict[str, Any]]] = None,
    top_products: Optional[Sequence[Dict[str, Any]]] = None,
    limit: int = 8,
) -> List[str]:
    """Merge unique product titles from verified catalog evidence sources."""
    seen: List[str] = []
    sources: List[Sequence[Any]] = []

    if candidates is not None:
        sources.append(candidates)
    if recommended is not None:
        sources.append(recommended)
    if top_products is not None:
        sources.append(top_products)

    if state is not None:
        sources.extend([
            list(getattr(state, "last_search_candidates", None) or []),
            list(getattr(state, "last_recommended_products", None) or []),
        ])
    if facts is not None:
        sources.append(list(getattr(facts, "top_products", None) or []))

    for source in sources:
        for prod in source:
            title = _product_title(prod)
            if title and title not in seen:
                seen.append(title)
    return seen[:limit]


def collect_verified_catalog_titles_from_ctx(ctx: BrainContext, *, limit: int = 8) -> List[str]:
    return collect_verified_catalog_titles(
        state=getattr(ctx, "state", None),
        facts=getattr(ctx, "facts", None),
        limit=limit,
    )


def _join_names(names: List[str], *, limit: int = 5) -> str:
    items = [n for n in names if n][:limit]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} و{items[1]}"
    return "، ".join(items[:-1]) + f" و{items[-1]}"


def is_honey_category_hint(text: str) -> bool:
    return bool(_HONEY_RE.search(text or ""))


def is_seasonal_availability_ask(message: str) -> bool:
    return bool(_SEASONAL_AVAILABILITY_RE.search(message or ""))


def extract_seasonal_product_subject(message: str) -> str:
    """Extract the product/category noun from a seasonal timing ask."""
    raw = (message or "").strip()
    if not raw:
        return ""
    m = _SEASONAL_PRODUCT_TAIL_RE.search(raw)
    if not m:
        return ""
    subject = (m.group(1) or "").strip(" ؟?!.")
    subject = re.sub(r"^(?:ال|about|the)\s+", "", subject, flags=re.UNICODE | re.IGNORECASE)
    return subject.strip(" ؟?!.")


def seasonal_subject_in_catalog(subject: str, catalog_titles: Sequence[str]) -> bool:
    """True when any catalog title matches the seasonal product subject."""
    from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
        _text_references_product,
    )

    subj = (subject or "").strip()
    if not subj:
        return False
    for title in catalog_titles:
        if _text_references_product(subj, title) or _text_references_product(title, subj):
            return True
    return False


def build_catalog_grounded_list_reply(
    catalog_titles: Sequence[str],
    *,
    category_hint: str = "",
    greeting: str = "",
    ask_prices: bool = True,
) -> str:
    """Build a catalog-grounded availability list — never invents product names."""
    names = [t for t in catalog_titles if t]
    joined = _join_names(names)
    prefix = f"{greeting} " if greeting else ""
    label = category_hint.strip() or "المنتجات"

    if is_honey_category_hint(category_hint) or (
        names and all(is_honey_category_hint(t) for t in names)
    ):
        if joined:
            body = f"المتوفر حاليًا عندنا من العسل:\n- {joined.replace(' و', '\n- ')}"
        else:
            return _SAFE_NO_CATALOG_AR
    elif joined:
        body = f"المتوفر حاليًا عندنا من {label}:\n- {joined.replace(' و', '\n- ')}"
    else:
        return _SAFE_NO_CATALOG_AR

    if ask_prices:
        body += "\n\nتحب أعرض لك الأسعار والأحجام المتوفرة؟"
    return f"{prefix}{body}".strip()


def build_seasonal_uncertainty_suffix(
    seasonal_subject: str,
    catalog_titles: Sequence[str],
) -> str:
    if seasonal_subject_in_catalog(seasonal_subject, catalog_titles):
        return ""
    return f"\n\nوبالنسبة لـ«{seasonal_subject}»، {_SAFE_SEASONAL_UNCERTAIN_AR}"


def build_uncertain_catalog_reply(
    *,
    category_hint: str = "",
    seasonal_subject: str = "",
    catalog_titles: Optional[Sequence[str]] = None,
    greeting: str = "",
) -> str:
    """Safe reply when catalog evidence is missing or seasonal product is unknown."""
    titles = list(catalog_titles or [])
    if titles:
        base = build_catalog_grounded_list_reply(
            titles,
            category_hint=category_hint,
            greeting=greeting,
        )
        if seasonal_subject and not seasonal_subject_in_catalog(seasonal_subject, titles):
            return base + build_seasonal_uncertainty_suffix(seasonal_subject, titles)
        return base
    if seasonal_subject:
        return (
            f"{greeting} {_SAFE_SEASONAL_UNCERTAIN_AR} "
            "أقدر أوصلك بالمتجر أو أطلب لك تحديث الكتالوج."
        ).strip()
    return _SAFE_NO_CATALOG_AR


__all__ = [
    "build_catalog_grounded_list_reply",
    "build_seasonal_uncertainty_suffix",
    "build_uncertain_catalog_reply",
    "collect_verified_catalog_titles",
    "collect_verified_catalog_titles_from_ctx",
    "extract_seasonal_product_subject",
    "is_honey_category_hint",
    "is_seasonal_availability_ask",
    "seasonal_subject_in_catalog",
]
