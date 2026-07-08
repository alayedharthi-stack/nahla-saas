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

_CURRENCY_TOKEN_RE = re.compile(
    r"(?:ريال|r\.?\s?س\.?|sar)\b",
    re.UNICODE | re.IGNORECASE,
)
_PRICE_CONTEXT_RE = re.compile(
    r"(?:السعر|سعر|اسعار|أسعار|الاجمالي|الإجمالي|اجمالي|إجمالي|المبلغ|مبلغ|"
    r"تكلف|قيمة|قيمته|بسعر|ب(?:كم|ـكم))",
    re.UNICODE | re.IGNORECASE,
)
_ADDRESS_CONTEXT_RE = re.compile(
    r"(?:عنوان|العنوان|حي|شارع|طريق|رمز|الوطني|المختصر|"
    r"maps\.|goo\.gl|deliver|delivery|address|location|"
    r"محمد|الكارز|بطحاء|قريش|مكه|مكة|المدين)",
    re.UNICODE | re.IGNORECASE,
)
_SHORT_ADDRESS_TOKEN_RE = re.compile(
    r"[A-Za-z]{4}\d{4}",
)
_NUMERIC_CANDIDATE_RE = re.compile(
    r"(\d{2,5})(?:\.\d{1,2})?",
)
_REPLY_PRICE_AMOUNT_RE = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d{2,6})(?:\.\d{1,2})?",
)

_ARABIC_DIGIT_TRANS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def _normalize_price_text(raw: str) -> str:
    """Map Arabic-Indic digits and decimal separators to ASCII for price parsing."""
    text = str(raw or "").translate(_ARABIC_DIGIT_TRANS)
    return text.replace("٬", ",").replace("٫", ".")


def parse_price_amount(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if value <= 0:
            return None
        whole = int(value)
        return whole if float(whole) == float(value) else int(round(value))
    normalized = _normalize_price_text(str(value))
    stripped = re.sub(r"[^\d,.\s]", "", normalized)
    compact = stripped.replace(",", "").strip()
    if not compact:
        return None
    match = re.search(r"(\d{1,6})(?:\.\d{1,2})?", compact)
    if not match:
        return None
    try:
        val = float(match.group(0))
        if val <= 0:
            return None
        whole = int(val)
        return whole if float(whole) == val else int(round(val))
    except (TypeError, ValueError):
        return None


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


def _span_in_short_address_token(text: str, start: int, end: int) -> bool:
    for token in _SHORT_ADDRESS_TOKEN_RE.finditer(text or ""):
        if token.start() <= start and end <= token.end():
            return True
    return False


def extract_reply_prices(reply: str) -> Set[int]:
    """Return explicit price claims in outbound text (not address digits)."""
    prices: Set[int] = set()
    text = _normalize_price_text(reply or "")
    if not text.strip():
        return prices

    for match in _REPLY_PRICE_AMOUNT_RE.finditer(text):
        start, end = match.span()
        if _span_in_short_address_token(text, start, end):
            continue

        window_before = text[max(0, start - 48):start]
        window_after = text[end: min(len(text), end + 24)]
        local = f"{window_before}{match.group(0)}{window_after}"

        has_currency = bool(_CURRENCY_TOKEN_RE.search(window_after))
        has_price_context = bool(_PRICE_CONTEXT_RE.search(window_before))
        if not has_currency and not has_price_context:
            continue

        if _ADDRESS_CONTEXT_RE.search(local) and not has_price_context:
            continue

        amount = parse_price_amount(match.group(0))
        if amount is not None:
            prices.add(amount)
    return prices


def collect_whatsapp_catalog_grounded_prices(
    *,
    order_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Set[int]:
    """Trusted WA native catalog prices from metadata and checkout line_items."""
    prices: Set[int] = set()
    meta = dict(inbound_metadata or {})
    if meta.get("source_type") == "catalog_order":
        tp = parse_price_amount(meta.get("total_price"))
        if tp is not None:
            prices.add(tp)
        try:
            total_f = float(meta.get("total_price"))
            if total_f > 0:
                prices.add(int(total_f))
        except (TypeError, ValueError):
            pass
        for item in meta.get("product_items") or []:
            if not isinstance(item, dict):
                continue
            qty = 1
            try:
                qty = max(1, int(float(item.get("quantity") or 1)))
            except (TypeError, ValueError):
                qty = 1
            unit = parse_price_amount(item.get("item_price"))
            if unit is not None:
                prices.add(unit)
                prices.add(unit * qty)

    prep: Dict[str, Any] = {}
    cart_items: List[Any] = []
    if order_state is not None:
        if isinstance(order_state, dict):
            prep = dict(order_state.get("order_prep") or {})
            cart_items = list(
                prep.get("line_items")
                or prep.get("cart_items")
                or order_state.get("cart_items")
                or []
            )
        else:
            op = getattr(order_state, "order_prep", None)
            if op is not None:
                if isinstance(op, dict):
                    prep = dict(op)
                elif hasattr(op, "to_dict"):
                    prep = dict(op.to_dict() or {})
            cart_items = list(getattr(order_state, "cart_items", None) or [])
            if not cart_items and prep:
                cart_items = list(prep.get("line_items") or prep.get("cart_items") or [])

    for item in cart_items:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").lower()
        trusted_item = bool(
            item.get("product_retailer_id")
            or item.get("from_catalog_order")
            or "catalog" in source
            or "whatsapp" in source
        )
        if not trusted_item:
            continue
        for key in ("unit_price", "price"):
            p = parse_price_amount(item.get(key))
            if p is not None:
                prices.add(p)
        qty = 1
        try:
            qty = max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            qty = 1
        unit = parse_price_amount(item.get("unit_price") or item.get("price"))
        if unit is not None and qty > 1:
            prices.add(unit * qty)

    return prices


def collect_saved_open_draft_grounded_prices(
    db: Optional[Session],
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Set[int]:
    """Trusted prices from a saved open WhatsApp draft order in the DB."""
    prices: Set[int] = set()
    conv_id = conversation_id
    if conv_id is None and inbound_metadata:
        raw = inbound_metadata.get("conversation_id")
        if raw is not None:
            try:
                conv_id = int(raw)
            except (TypeError, ValueError):
                conv_id = None
    if db is None or tenant_id is None or not conv_id:
        return prices
    try:
        from core.order_context_builder import (  # noqa: PLC0415
            load_saved_open_checkout_draft,
            saved_open_checkout_draft_is_grounded,
        )

        draft = load_saved_open_checkout_draft(
            db,
            tenant_id=int(tenant_id),
            conversation_id=int(conv_id),
        )
        if not saved_open_checkout_draft_is_grounded(draft):
            return prices
        total = parse_price_amount(getattr(draft, "total", None))
        if total is not None:
            prices.add(total)
        for item in getattr(draft, "line_items", None) or []:
            if not isinstance(item, dict):
                continue
            for key in ("unit_price", "price", "item_price"):
                unit = parse_price_amount(item.get(key))
                if unit is not None:
                    prices.add(unit)
            qty = 1
            try:
                qty = max(1, int(item.get("quantity") or 1))
            except (TypeError, ValueError):
                qty = 1
            unit = parse_price_amount(
                item.get("unit_price") or item.get("price") or item.get("item_price")
            )
            if unit is not None and qty > 1:
                prices.add(unit * qty)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — saved draft lookup must not block grounding guard
        logger.exception(
            "[PRODUCT_CLAIM_GROUNDING] saved draft prices skipped tenant=%s",
            tenant_id,
        )
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
    whatsapp_catalog_trusted: bool = False
    executor_product_ids: frozenset[int] = frozenset()
    kb_section_ids: frozenset[int] = frozenset()
    reason: str = ""


def _catalog_product_ids_from_metadata(
    inbound_metadata: Optional[Dict[str, Any]],
) -> Set[int]:
    ids: Set[int] = set()
    for raw in (inbound_metadata or {}).get("catalog_product_ids") or []:
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def _add_catalog_row_prices(
    row: Dict[str, Any],
    *,
    grounded_prices: Set[int],
    corpus_parts: List[str],
    executor_ids: Set[int],
) -> None:
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


def build_product_claim_grounding_evidence(
    db: Optional[Session],
    tenant_id: Optional[int],
    *,
    availability_context: Optional[Dict[str, Any]] = None,
    executor_products: Optional[Sequence[Dict[str, Any]]] = None,
    catalog_fact_products: Optional[Sequence[Dict[str, Any]]] = None,
    chosen_path: str = "",
    history: Optional[Sequence[Any]] = None,
    order_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[int] = None,
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
    wa_prices = collect_whatsapp_catalog_grounded_prices(
        order_state=order_state,
        inbound_metadata=inbound_metadata,
    )
    grounded_prices.update(wa_prices)
    draft_prices = collect_saved_open_draft_grounded_prices(
        db,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        inbound_metadata=inbound_metadata,
    )
    grounded_prices.update(draft_prices)
    whatsapp_catalog_trusted = bool(wa_prices) or bool(draft_prices) or (
        str((inbound_metadata or {}).get("source_type") or "") == "catalog_order"
    )
    corpus_parts: List[str] = []
    kb_ids: Set[int] = set()
    executor_ids: Set[int] = set()

    meta = dict(inbound_metadata or {})
    fact_rows = [
        dict(p) for p in (catalog_fact_products or [])
        if isinstance(p, dict)
    ]
    if not fact_rows:
        fact_rows = [
            dict(p) for p in (meta.get("catalog_fact_products") or [])
            if isinstance(p, dict)
        ]
    catalog_fact_ids = _catalog_product_ids_from_metadata(meta)

    for row in fact_rows:
        pid = row.get("id")
        if catalog_fact_ids and pid is not None:
            try:
                if int(pid) not in catalog_fact_ids:
                    continue
            except (TypeError, ValueError):
                continue
        _add_catalog_row_prices(
            row,
            grounded_prices=grounded_prices,
            corpus_parts=corpus_parts,
            executor_ids=executor_ids,
        )

    if fact_rows:
        catalog_hit_turn = True

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
        whatsapp_catalog_trusted=whatsapp_catalog_trusted,
        executor_product_ids=frozenset(executor_ids),
        kb_section_ids=frozenset(kb_ids),
    )
