"""
modules/ai/brain/postprocess/availability_context_builder.py
────────────────────────────────────────────────────────────
Builds the availability_context bundle for the truth guard (per turn).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from core.product_entity_resolution import (
    extract_years,
    family_key_from_title,
    primary_year_from_text,
)

logger = logging.getLogger("nahla.brain.postprocess.availability_context")

_AVAIL_POS = re.compile(
    r"(?:\u0645\u062a\u0648\u0641\u0631|\u0645\u062a\u0627\u062d|available|in\s*stock)",
    re.I,
)
_AVAIL_NEG = re.compile(
    r"(?:\u063a\u064a\u0631\s*\u0645\u062a\u0648\u0641\u0631|\u063a\u064a\u0631\s*\u0645\u062a\u0627\u062d|"
    r"\u0646\u0641\u062f|\u0646\u0641\u0630|unavailable|out\s*of\s*stock)",
    re.I,
)

_OPS_KINDS = frozenset({
    "quick_update", "custom", "faq", "product_benefit", "product_usage",
})


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _can_checkout_from_row(row: Any, variants_ok: bool = True) -> bool:
    meta = getattr(row, "extra_metadata", None) or {}
    ext = (getattr(row, "external_id", None) or "").strip()
    status = str(meta.get("status") or "active").lower()
    in_stock = meta.get("in_stock", getattr(row, "in_stock", True))
    stock_qty = meta.get("stock_qty", getattr(row, "stock_quantity", None))
    qty_ok = stock_qty is None or _safe_int(stock_qty, 0) > 0
    return bool(ext) and status == "active" and bool(in_stock) and qty_ok and variants_ok


def _kb_polarity(title: str, body: str) -> str:
    joined = f"{title}\n{body}"
    pos = bool(_AVAIL_POS.search(joined))
    neg = bool(_AVAIL_NEG.search(joined))
    if pos and not neg:
        return "positive"
    if neg and not pos:
        return "negative"
    return "none"


def build_availability_context(
    db: Session,
    tenant_id: int,
    *,
    focus_product: Optional[Dict[str, Any]] = None,
    recommended_product_ids: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """Assemble availability_context for evaluate_product_availability_evidence."""
    from models import Integration, MerchantKnowledgeSection, Product  # noqa: PLC0415
    from models import MerchantKnowledgeSectionProduct  # noqa: PLC0415

    ctx: Dict[str, Any] = {
        "platform_connected": False,
        "focus_product": focus_product,
        "recommended_product_ids": list(recommended_product_ids or []),
        "catalog_skus": [],
        "kb_signals": [],
        "product_links": [],
    }

    try:
        has_integration = (
            db.query(Integration.id)
            .filter(
                Integration.tenant_id == tenant_id,
                Integration.enabled.is_(True),
            )
            .first()
            is not None
        )
        products = (
            db.query(Product)
            .filter(
                Product.tenant_id == tenant_id,
                Product.external_id.isnot(None),
                Product.external_id != "",
            )
            .all()
        )
        ctx["platform_connected"] = has_integration or bool(products)

        catalog_skus: List[Dict[str, Any]] = []
        for p in products:
            title = p.title or ""
            catalog_skus.append({
                "id": p.id,
                "title": title,
                "sku": p.sku,
                "external_id": p.external_id,
                "can_checkout": _can_checkout_from_row(p),
                "in_stock": bool(p.in_stock),
                "years": extract_years(title),
                "weights": [],
                "family_key": family_key_from_title(title),
            })
        ctx["catalog_skus"] = catalog_skus

        sections = (
            db.query(MerchantKnowledgeSection)
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.is_active.is_(True),
                MerchantKnowledgeSection.kind.in_(tuple(_OPS_KINDS)),
            )
            .all()
        )
        kb_signals: List[Dict[str, Any]] = []
        for s in sections:
            title = s.title or ""
            body = s.body or ""
            pol = _kb_polarity(title, body)
            if pol == "none":
                continue
            kb_signals.append({
                "section_id": s.id,
                "kind": s.kind,
                "avail_polarity": pol,
                "primary_year": primary_year_from_text(title, body),
                "linked_product_ids": [],
            })
        ctx["kb_signals"] = kb_signals

        links = (
            db.query(MerchantKnowledgeSectionProduct)
            .join(
                MerchantKnowledgeSection,
                MerchantKnowledgeSection.id == MerchantKnowledgeSectionProduct.section_id,
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.is_active.is_(True),
            )
            .all()
        )
        product_links: List[Dict[str, Any]] = []
        link_ids_by_section: Dict[int, List[int]] = {}
        for lk in links:
            product_links.append({
                "section_id": lk.section_id,
                "product_id": lk.product_id,
                "source": lk.source,
                "confidence": lk.confidence,
            })
            link_ids_by_section.setdefault(lk.section_id, []).append(lk.product_id)

        for sig in kb_signals:
            sig["linked_product_ids"] = link_ids_by_section.get(sig["section_id"], [])

        ctx["product_links"] = product_links
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[AVAILABILITY_CONTEXT] build failed tenant=%s err=%s",
            tenant_id,
            exc,
        )

    return ctx
