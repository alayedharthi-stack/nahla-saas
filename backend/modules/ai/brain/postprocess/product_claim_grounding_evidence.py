"""
product_claim_grounding_evidence.py
───────────────────────────────────
Assemble deterministic evidence for product claim grounding:
catalog prices, availability, merchant KB text, and recent catalog-miss
signals from conversation history.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.brain.postprocess.product_claim_grounding_evidence")

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")

_CATALOG_MISS_MARKERS = (
    "ما لقيت تطابق",
    "ما ظهر عندي في الكتالوج",
    "ما لقيت ",
    "لم أتمكن من العثور على منتجات",
    "ما وجدت منتجات متوفرة",
)

_NO_SYNCED_MARKERS = (
    "لا توجد منتجات مزامنة",
    "no_products_in_catalog",
)

_KB_CLAIM_KINDS = frozenset({
    "quick_update",
    "custom",
    "faq",
    "product_benefit",
    "product_usage",
})

_PRICE_PARSE_RE = re.compile(r"(\d{2,5})")


def _norm(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = _NORMALISE_AR_RE.sub("", text)
    t = t.replace("ـ", "")
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ى", "ي").replace("ة", "ه")
    )
    return t.lower().strip()


def parse_price_amount(value: Any) -> Optional[int]:
    if value is None:
        return None
    raw = str(value).replace(",", "").strip()
    if not raw:
        return None
    match = _PRICE_PARSE_RE.search(raw)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def extract_reply_prices(reply: str) -> Set[int]:
    """Return SAR-like amounts mentioned in outbound reply text."""
    prices: Set[int] = set()
    if not reply:
        return prices
    for match in re.finditer(
        r"(\d{2,5})\s*(?:ريال|r(?:iyal)?|sar|ر\.?\s?س\.?)?",
        reply,
        re.UNICODE | re.IGNORECASE,
    ):
        try:
            prices.add(int(match.group(1)))
        except (TypeError, ValueError):
            continue
    return prices


def _distinctive_title_tokens(title: str) -> Set[str]:
    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    stop = frozenset({
        "منتج", "product", "عسل", "حجم", "وزن", "كيلو", "نصف", "ربع", "جرام",
    })
    return {
        t
        for t in tokenize(normalize_arabic(title or ""))
        if len(t) >= 3 and t not in stop
    }


def _text_references_product(text: str, title: str) -> bool:
    norm = _norm(text)
    if not norm:
        return False
    title_norm = _norm(title)
    if title_norm and title_norm in norm:
        return True
    toks = _distinctive_title_tokens(title)
    if not toks:
        return False
    hits = sum(1 for t in toks if t in norm)
    need = 2 if len(toks) >= 2 else 1
    return hits >= need


def scan_recent_catalog_miss_signals(
    history: Sequence[Any],
    *,
    lookback: int = 8,
) -> Tuple[bool, bool]:
    """Return (recent_catalog_miss, recent_no_synced) from prior outbound turns."""
    catalog_miss = False
    no_synced = False
    if not history:
        return catalog_miss, no_synced
    turns = list(history)[-lookback:]
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        direction = str(turn.get("direction") or turn.get("role") or "").lower()
        if direction not in ("outbound", "assistant", "bot", "out"):
            continue
        body = str(turn.get("body") or turn.get("content") or "")
        if not body.strip():
            continue
        norm = _norm(body)
        if any(_norm(m) in norm for m in _NO_SYNCED_MARKERS):
            no_synced = True
        if any(_norm(m) in norm for m in _CATALOG_MISS_MARKERS):
            catalog_miss = True
        if catalog_miss and no_synced:
            break
    return catalog_miss, no_synced


@dataclass(frozen=True)
class ProductClaimGroundingEvidence:
    grounded_prices: frozenset[int] = frozenset()
    grounded_text_corpus: str = ""
    available_products: Tuple[Dict[str, Any], ...] = ()
    unavailable_products: Tuple[Dict[str, Any], ...] = ()
    catalog_products_this_turn: bool = False
    catalog_miss_this_turn: bool = False
    recent_catalog_miss: bool = False
    recent_no_synced: bool = False
    has_checkout_catalog: bool = False
    executor_product_ids: frozenset[int] = frozenset()
    kb_section_ids: frozenset[int] = frozenset()
    reason: str = ""


def build_product_claim_grounding_evidence(
    db: Optional[Session],
    tenant_id: Optional[int],
    *,
    availability_context: Optional[Dict[str, Any]] = None,
    executor_products: Optional[Sequence[Dict[str, Any]]] = None,
    chosen_path: str = "",
    history: Optional[Sequence[Any]] = None,
) -> ProductClaimGroundingEvidence:
    """Build evidence bundle for product claim grounding guard."""
    ctx = availability_context or {}
    catalog_skus = list(ctx.get("catalog_skus") or [])
    executor_rows = [dict(p) for p in (executor_products or []) if isinstance(p, dict)]

    path = str(chosen_path or "").strip().lower()
    catalog_miss_turn = (
        "catalog_miss" in path
        or path == "no_products_in_catalog"
        or bool(not executor_rows and "search" in path and "miss" in path)
    )
    catalog_hit_turn = bool(executor_rows)

    recent_miss, recent_no_sync = scan_recent_catalog_miss_signals(history or [])

    available: List[Dict[str, Any]] = []
    unavailable: List[Dict[str, Any]] = []
    for row in catalog_skus:
        item = dict(row)
        if item.get("can_checkout"):
            available.append(item)
        else:
            unavailable.append(item)

    grounded_prices: Set[int] = set()
    corpus_parts: List[str] = []
    kb_ids: Set[int] = set()
    executor_ids: Set[int] = set()

    for row in executor_rows:
        pid = row.get("id")
        if isinstance(pid, int):
            executor_ids.add(pid)
        for key in ("title", "description", "body"):
            val = row.get(key)
            if val:
                corpus_parts.append(str(val))
        price = parse_price_amount(row.get("price"))
        if price is not None:
            grounded_prices.add(price)
        for variant in row.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            vp = parse_price_amount(variant.get("price"))
            if vp is not None:
                grounded_prices.add(vp)

    if db is not None and tenant_id is not None:
        try:
            from models import MerchantKnowledgeSection, Product, ProductVariant  # noqa: PLC0415
            from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415

            products = (
                db.query(Product)
                .filter(
                    Product.tenant_id == tenant_id,
                    Product.external_id.isnot(None),
                    Product.external_id != "",
                )
                .all()
            )
            product_ids = [p.id for p in products]
            variants_by_product: Dict[int, List[Any]] = {}
            if product_ids:
                variants = (
                    db.query(ProductVariant)
                    .filter(
                        ProductVariant.tenant_id == tenant_id,
                        ProductVariant.product_id.in_(product_ids),
                    )
                    .all()
                )
                for v in variants:
                    variants_by_product.setdefault(v.product_id, []).append(v)

            for p in products:
                if p.description:
                    corpus_parts.append(str(p.description))
                price = parse_price_amount(p.price)
                if price is not None:
                    grounded_prices.add(price)
                for v in variants_by_product.get(p.id, []):
                    if not v.in_stock:
                        continue
                    qty = v.stock_quantity
                    if qty is not None and int(qty or 0) <= 0:
                        continue
                    vp = parse_price_amount(v.price)
                    if vp is not None:
                        grounded_prices.add(vp)

            sections = (
                apply_ai_visible_kb_query_filters(
                    db.query(MerchantKnowledgeSection)
                )
                .filter(
                    MerchantKnowledgeSection.tenant_id == tenant_id,
                    MerchantKnowledgeSection.kind.in_(tuple(_KB_CLAIM_KINDS)),
                )
                .all()
            )
            for section in sections:
                kb_ids.add(section.id)
                if section.title:
                    corpus_parts.append(str(section.title))
                if section.body:
                    corpus_parts.append(str(section.body))
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[PRODUCT_CLAIM_GROUNDING] evidence build partial fail tenant=%s",
                tenant_id,
            )

    return ProductClaimGroundingEvidence(
        grounded_prices=frozenset(grounded_prices),
        grounded_text_corpus=_norm("\n".join(corpus_parts)),
        available_products=tuple(available),
        unavailable_products=tuple(unavailable),
        catalog_products_this_turn=catalog_hit_turn,
        catalog_miss_this_turn=catalog_miss_turn,
        recent_catalog_miss=recent_miss,
        recent_no_synced=recent_no_sync,
        has_checkout_catalog=bool(available),
        executor_product_ids=frozenset(executor_ids),
        kb_section_ids=frozenset(kb_ids),
    )
