"""
catalog/discovery_presenter.py
──────────────────────────────
Evidence-based discovery presentation — catalog-backed Arabic replies only.

Phase 3: ``DiscoveryPresentationComposer`` for discovery plan output.
Phase 4A: merchant-defined collection / featured product presentation helpers.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..commerce.discovery_strategy import DiscoveryMode, DiscoveryStrategyResult
from ..commerce.merchant_discovery_settings import (
    DiscoveryCollectionConfig,
    FeaturedProductConfig,
    MerchantDiscoverySettings,
)
from ..discovery.entry import SHOW_MORE, TOP_PRODUCTS
from .catalog_intelligence import CatalogGroup, DiscoveryPlan
from .presentation_contract import (
    discovery_has_catalog_evidence,
    reply_contains_ungrounded_discovery_claim,
    validate_discovery_products,
)

logger = logging.getLogger("nahla.brain.catalog.discovery_presenter")

PRODUCTS_FEATURED_HEADER = "الأكثر طلباً حالياً:"
PRODUCTS_NEUTRAL_HEADER = "هذه بعض الخيارات المتوفرة:"
COLLECTIONS_HEADER = "الأقسام المتوفرة:"
PRODUCTS_CLOSING = "اكتب رقم المنتج أو اسمه وأكمل طلبك."
COLLECTIONS_CLOSING = "اختر القسم الذي تود استعراضه."
DEFAULT_GUIDED_QUESTION = "تحب أعرض لك حسب القسم أو الأكثر طلباً؟"
DEFAULT_EMPTY_REPLY = (
    "ما ظهر عندي منتجات واضحة حالياً. "
    "اكتب اسم المنتج الذي تبحث عنه أو اختر قسماً من المتجر."
)

_TOP_PRODUCT_SOURCES = frozenset({"top_products", "top_products_start_order"})

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")


@dataclass(frozen=True)
class DiscoveryPresentationResult:
    text: str
    output_kind: str
    products: List[Dict[str, Any]] = field(default_factory=list)
    collections: List[Dict[str, Any]] = field(default_factory=list)


def _norm(text: str) -> str:
    t = str(text or "").lower()
    t = _NORM_RE.sub("", t)
    return " ".join(t.split()).strip()


def _format_price(product: Dict[str, Any]) -> str:
    raw = product.get("sale_price")
    if raw is None:
        raw = product.get("price")
    if raw is None:
        return ""
    if re.match(r"^\s*\d+(\.\d+)?\s*$", str(raw)):
        return f"{raw} ريال"
    return str(raw)


def _extract_variant_label(product: Dict[str, Any]) -> str:
    for key in ("variant_name", "size", "weight", "unit", "option_label"):
        value = str(product.get(key) or "").strip()
        if value:
            return value
    return ""


def _product_display_label(product: Dict[str, Any]) -> str:
    title = str(product.get("title") or "").strip()
    variant = _extract_variant_label(product)
    if variant and variant.lower() not in title.lower():
        return f"{title} {variant}".strip()
    return title


def _uses_featured_header(
    *,
    strategy: DiscoveryStrategyResult,
    entry_source: str = "",
    entry_type: str = "",
) -> bool:
    if strategy.mode == DiscoveryMode.FEATURED_FIRST:
        return True
    if entry_type == TOP_PRODUCTS:
        return True
    source = str(entry_source or "").strip().lower()
    return source in _TOP_PRODUCT_SOURCES


def _display_title(
    product: Dict[str, Any],
    featured: Optional[FeaturedProductConfig] = None,
) -> str:
    override = str(getattr(featured, "label_override", "") or "").strip()
    if override:
        return override
    return str(product.get("title") or "").strip()


def _variant_price_label(product: Dict[str, Any], variant_id: str) -> str:
    if not variant_id:
        return _format_price(product) or "السعر غير محدد"
    for variant in list(product.get("variants") or []):
        if not isinstance(variant, dict):
            continue
        vid = str(variant.get("id") or variant.get("variant_id") or "").strip()
        if vid == str(variant_id):
            price = str(variant.get("price") or variant.get("sale_price") or product.get("price") or "").strip()
            if price and re.match(r"^\s*\d+(\.\d+)?\s*$", price):
                return f"{price} ريال"
            return price or _format_price(product) or "السعر غير محدد"
    return _format_price(product) or "السعر غير محدد"


def compose_merchant_collections(
    collections: Sequence[CatalogGroup | Dict[str, Any]],
    *,
    merchant_settings: Optional[MerchantDiscoverySettings] = None,
) -> str:
    rows: List[tuple[int, str, str]] = []
    merchant_by_label = {
        _norm(c.label): c for c in (merchant_settings.enabled_collections() if merchant_settings else [])
    }
    for idx, group in enumerate(collections, start=1):
        if isinstance(group, CatalogGroup):
            label = group.group_name
            group_id = group.group_id
        else:
            label = str(group.get("group_name") or group.get("label") or "").strip()
            group_id = str(group.get("group_id") or group.get("id") or label)
        merchant_row = merchant_by_label.get(_norm(label))
        display = merchant_row.label if merchant_row else label
        priority = merchant_row.priority if merchant_row else idx
        rows.append((priority, group_id, display))
    rows.sort(key=lambda r: (r[0], r[2]))
    lines = ["اختر القسم اللي يناسبك:", ""]
    for i, (_prio, _gid, label) in enumerate(rows, start=1):
        lines.append(f"{i}. {label}")
    lines.extend(["", COLLECTIONS_CLOSING])
    return "\n".join(lines)


def compose_collection_products(
    products: Sequence[Dict[str, Any]],
    *,
    collection: Optional[DiscoveryCollectionConfig] = None,
    merchant_settings: Optional[MerchantDiscoverySettings] = None,
    collection_label: str = "",
) -> str:
    label = collection_label or (collection.label if collection else "")
    header = f"من {label} المتوفر:" if label else "من المنتجات المتوفر:"
    featured_map: Dict[str, FeaturedProductConfig] = {}
    if collection and merchant_settings:
        for fp in merchant_settings.featured_for_collection(collection):
            featured_map[str(fp.product_id)] = fp
    lines = [header, ""]
    for i, product in enumerate(products, start=1):
        pid = str(product.get("id") or product.get("external_id") or "").strip()
        fp = featured_map.get(pid)
        title = _display_title(product, fp)
        price = _variant_price_label(product, str(getattr(fp, "variant_id", "") or ""))
        lines.append(f"{i}. {title} — {price}")
    lines.append("")
    lines.append("اكتب رقم المنتج أو اسمه ونكمل طلبك.")
    return "\n".join(lines)


class DiscoveryPresentationComposer:
    """Convert ``DiscoveryPlan`` output into concise Saudi-friendly Arabic."""

    def compose(
        self,
        *,
        plan: DiscoveryPlan,
        strategy: DiscoveryStrategyResult,
        entry_source: str = "",
        entry_type: str = "",
        merchant_settings: Optional[MerchantDiscoverySettings] = None,
        query: str = "",
    ) -> DiscoveryPresentationResult:
        kind = str(plan.output_kind or "").strip().lower()
        if kind == "guided":
            return self._compose_guided(plan, strategy)
        if kind == "collections":
            return self._compose_collections(
                plan,
                strategy,
                merchant_settings=merchant_settings,
            )
        if kind == "products":
            products = validate_discovery_products(list(plan.products or []))
            if not products:
                return self._compose_empty()
            return self._compose_products(
                products[: max(1, strategy.initial_count)],
                strategy=strategy,
                entry_source=entry_source,
                entry_type=entry_type,
                merchant_settings=merchant_settings,
                query=query,
            )
        if kind == "empty":
            return self._compose_empty()
        logger.info("[DISCOVERY_PRESENTER] unknown_output_kind kind=%r", kind)
        return self._compose_empty()

    def compose_products(
        self,
        products: Sequence[Dict[str, Any]],
        *,
        strategy: DiscoveryStrategyResult,
        entry_source: str = "",
        entry_type: str = "",
        merchant_settings: Optional[MerchantDiscoverySettings] = None,
        query: str = "",
    ) -> DiscoveryPresentationResult:
        validated = validate_discovery_products(list(products or []))
        if not validated:
            return self._compose_empty()
        return self._compose_products(
            validated[: max(1, strategy.initial_count)],
            strategy=strategy,
            entry_source=entry_source,
            entry_type=entry_type,
            merchant_settings=merchant_settings,
            query=query,
        )

    def _compose_products(
        self,
        products: List[Dict[str, Any]],
        *,
        strategy: DiscoveryStrategyResult,
        entry_source: str,
        entry_type: str,
        merchant_settings: Optional[MerchantDiscoverySettings] = None,
        query: str = "",
    ) -> DiscoveryPresentationResult:
        shown = list(products or [])[: max(1, min(3, strategy.initial_count))]
        matched_collection = (
            merchant_settings.match_collection(query) if merchant_settings else None
        )
        if merchant_settings and matched_collection:
            text = compose_collection_products(
                shown,
                collection=matched_collection,
                merchant_settings=merchant_settings,
                collection_label=matched_collection.label,
            )
            return DiscoveryPresentationResult(
                text=text,
                output_kind="products",
                products=shown,
            )

        header = (
            PRODUCTS_FEATURED_HEADER
            if _uses_featured_header(
                strategy=strategy,
                entry_source=entry_source,
                entry_type=entry_type,
            )
            else PRODUCTS_NEUTRAL_HEADER
        )
        lines = [header, ""]
        featured_map: Dict[str, FeaturedProductConfig] = {}
        if merchant_settings:
            for collection in merchant_settings.enabled_collections():
                for fp in merchant_settings.featured_for_collection(collection):
                    featured_map[str(fp.product_id)] = fp
        for idx, product in enumerate(shown, start=1):
            pid = str(product.get("id") or product.get("external_id") or "").strip()
            fp = featured_map.get(pid)
            label = _display_title(product, fp) if fp else _product_display_label(product)
            price = (
                _variant_price_label(product, str(fp.variant_id))
                if fp and fp.variant_id
                else (_format_price(product) or "")
            )
            line = f"{idx}. {label}"
            if price:
                line += f" — {price}"
            lines.append(line)
        lines.extend(["", PRODUCTS_CLOSING])
        text = "\n".join(lines).strip()
        if reply_contains_ungrounded_discovery_claim(text):
            logger.warning("[DISCOVERY_PRESENTER] blocked_ungrounded_products_reply")
            return self._compose_empty()
        return DiscoveryPresentationResult(
            text=text,
            output_kind="products",
            products=shown,
        )

    def _compose_collections(
        self,
        plan: DiscoveryPlan,
        strategy: DiscoveryStrategyResult,
        *,
        merchant_settings: Optional[MerchantDiscoverySettings] = None,
    ) -> DiscoveryPresentationResult:
        collections = [
            group.to_dict() if isinstance(group, CatalogGroup) else dict(group)
            for group in (plan.collections or [])
        ]
        collections = [
            c for c in collections if str(c.get("group_name") or "").strip()
        ]
        if not collections:
            return self._compose_empty()
        if len(collections) == 1:
            logger.info(
                "[DISCOVERY_PRESENTER] single_collection_deferred name=%r",
                collections[0].get("group_name"),
            )
        shown = collections[: max(1, min(10, strategy.initial_count + 2))]
        if merchant_settings and merchant_settings.has_merchant_collections():
            groups = [
                CatalogGroup(
                    group_id=str(c.get("group_id") or c.get("id") or ""),
                    group_name=str(c.get("group_name") or ""),
                    browse_rank=int(c.get("browse_rank") or 0),
                    product_count=int(c.get("product_count") or 0),
                )
                for c in shown
            ]
            text = compose_merchant_collections(groups, merchant_settings=merchant_settings)
        else:
            lines = [COLLECTIONS_HEADER, ""]
            for idx, group in enumerate(shown, start=1):
                lines.append(f"{idx}. {group.get('group_name')}")
            lines.extend(["", COLLECTIONS_CLOSING])
            text = "\n".join(lines).strip()
        return DiscoveryPresentationResult(
            text=text,
            output_kind="collections",
            collections=shown,
        )

    def _compose_guided(
        self,
        plan: DiscoveryPlan,
        strategy: DiscoveryStrategyResult,
    ) -> DiscoveryPresentationResult:
        question = str(plan.guided_question or strategy.guided_question or "").strip()
        if not question:
            question = DEFAULT_GUIDED_QUESTION
        question = " ".join(question.split())
        if len(question.split()) > 12:
            question = DEFAULT_GUIDED_QUESTION
        return DiscoveryPresentationResult(
            text=question,
            output_kind="guided",
        )

    def _compose_empty(self) -> DiscoveryPresentationResult:
        return DiscoveryPresentationResult(
            text=DEFAULT_EMPTY_REPLY,
            output_kind="empty",
        )


def discovery_plan_has_presentable_evidence(plan: DiscoveryPlan) -> bool:
    kind = str(plan.output_kind or "").strip().lower()
    if kind == "guided":
        return True
    if kind == "collections":
        return discovery_has_catalog_evidence(
            collections=[
                g.to_dict() if isinstance(g, CatalogGroup) else dict(g)
                for g in (plan.collections or [])
            ],
        )
    if kind == "products":
        return discovery_has_catalog_evidence(products=plan.products)
    return False


def resolve_strategy_for_presentation(
    decision_args: Optional[Dict[str, Any]],
    *,
    state: Any = None,
) -> DiscoveryStrategyResult:
    from ..commerce.discovery_strategy import strategy_from_decision_args  # noqa: PLC0415

    args = dict(decision_args or {})
    if args.get("discovery_mode"):
        return strategy_from_decision_args(args)

    last = str(getattr(state, "last_discovery_mode", "") or "").strip().lower()
    if last:
        try:
            mode = DiscoveryMode(last)
        except ValueError:
            mode = DiscoveryMode.DIRECT_CATALOG
        return DiscoveryStrategyResult(
            mode=mode,
            initial_count=int(args.get("discovery_initial_count") or 3),
        )
    return DiscoveryStrategyResult(mode=DiscoveryMode.DIRECT_CATALOG)


__all__ = [
    "COLLECTIONS_CLOSING",
    "DEFAULT_EMPTY_REPLY",
    "DEFAULT_GUIDED_QUESTION",
    "DiscoveryPresentationComposer",
    "DiscoveryPresentationResult",
    "compose_collection_products",
    "compose_merchant_collections",
    "discovery_plan_has_presentable_evidence",
    "resolve_strategy_for_presentation",
]
