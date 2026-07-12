"""
routers/public_catalog.py
─────────────────────────
Unauthenticated read-only public pages for Nahla-native catalog products.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.catalog import (
    CATALOG_STATUS_ACTIVE,
    OWNERSHIP_NAHLA_MANAGED,
    SOURCE_NAHLA_NATIVE,
    catalog_status_of,
)
from core.database import get_db
from core.native_product_public_url import is_valid_public_retailer_id
from models import Product, Tenant, WhatsAppConnection

logger = logging.getLogger("nahla.public_catalog")

router = APIRouter(prefix="/public/catalog", tags=["public-catalog"])

_UNAVAILABLE_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex" />
  <title>المنتج غير متاح</title>
</head>
<body style="font-family:system-ui,sans-serif;margin:2rem;text-align:center;color:#334155;">
  <h1 style="font-size:1.25rem;">المنتج غير متاح</h1>
  <p style="font-size:0.95rem;">تعذّر العثور على هذا المنتج.</p>
</body>
</html>"""


def plain_description(text: str) -> str:
    """Strip tags and escape for safe HTML rendering."""
    cleaned = re.sub(r"<[^>]+>", "", str(text or ""))
    return html.escape(cleaned.strip())


def _sanitize_wa_me_url(phone: str) -> Optional[str]:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if len(digits) < 8 or len(digits) > 15:
        return None
    return f"https://wa.me/{digits}"


def _load_public_native_product(
    db: Session,
    retailer_id: str,
) -> Optional[Tuple[Product, Tenant]]:
    """Return (product, tenant) when exactly one active native row matches."""
    rid = (retailer_id or "").strip()
    if not is_valid_public_retailer_id(rid):
        return None

    rows = (
        db.query(Product)
        .filter(
            Product.source == SOURCE_NAHLA_NATIVE,
            Product.ownership_mode == OWNERSHIP_NAHLA_MANAGED,
            Product.merchant_hidden_at.is_(None),
            Product.catalog_status == CATALOG_STATUS_ACTIVE,
            or_(
                Product.meta_retailer_id == rid,
                Product.canonical_retailer_id == rid,
            ),
        )
        .all()
    )
    if len(rows) != 1:
        return None

    product = rows[0]
    if catalog_status_of(product) != CATALOG_STATUS_ACTIVE:
        return None

    tenant = (
        db.query(Tenant)
        .filter(Tenant.id == int(product.tenant_id), Tenant.is_active.is_(True))
        .first()
    )
    if tenant is None:
        return None
    return product, tenant


def _whatsapp_cta_url(db: Session, tenant_id: int) -> Optional[str]:
    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .first()
    )
    if conn is None:
        return None
    if str(getattr(conn, "status", "") or "").strip().lower() != "connected":
        return None
    phone = getattr(conn, "phone_number", None) or ""
    return _sanitize_wa_me_url(phone)


def _render_product_html(
    *,
    title: str,
    description: str,
    image_url: str,
    price: str,
    currency: str,
    availability_ar: str,
    store_name: str,
    wa_url: Optional[str],
) -> str:
    safe_title = html.escape(title)
    safe_desc = plain_description(description)
    safe_image = html.escape(image_url) if image_url else ""
    safe_price = html.escape(price) if price else ""
    safe_currency = html.escape(currency) if currency else "SAR"
    safe_store = html.escape(store_name)
    safe_avail = html.escape(availability_ar)

    og_image = f'  <meta property="og:image" content="{safe_image}" />\n' if safe_image else ""
    img_block = (
        f'<img src="{safe_image}" alt="{safe_title}" '
        f'style="max-width:100%;height:auto;border-radius:12px;" />'
        if safe_image
        else ""
    )
    price_block = (
        f'<p style="font-size:1.1rem;font-weight:600;margin:0.5rem 0;">'
        f'{safe_price} {safe_currency}</p>'
        if safe_price
        else ""
    )
    desc_block = (
        f'<p style="color:#475569;line-height:1.6;">{safe_desc}</p>'
        if safe_desc
        else ""
    )
    wa_block = ""
    if wa_url:
        safe_wa = html.escape(wa_url, quote=True)
        wa_block = (
            f'<p style="margin-top:1.5rem;">'
            f'<a href="{safe_wa}" rel="noopener noreferrer" '
            f'style="display:inline-block;background:#25D366;color:#fff;'
            f'padding:0.75rem 1.25rem;border-radius:999px;text-decoration:none;'
            f'font-weight:600;">اطلب عبر واتساب</a></p>'
        )

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <meta property="og:title" content="{safe_title}" />
  <meta property="og:type" content="product" />
{og_image}  <meta name="robots" content="index,follow" />
</head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:0 auto;padding:1.5rem;color:#0f172a;">
  <p style="font-size:0.85rem;color:#64748b;margin:0 0 0.5rem;">{safe_store}</p>
  <h1 style="font-size:1.5rem;margin:0 0 1rem;">{safe_title}</h1>
  {img_block}
  {price_block}
  <p style="font-size:0.9rem;color:#64748b;">{safe_avail}</p>
  {desc_block}
  {wa_block}
</body>
</html>"""


def _product_view_model(
    db: Session,
    product: Product,
    tenant: Tenant,
) -> Dict[str, Any]:
    meta = product.extra_metadata or {}
    image_url = str(meta.get("image_url") or "").strip()
    currency = str(meta.get("currency") or "SAR").strip() or "SAR"
    price = str(product.price or "").strip()
    in_stock = bool(getattr(product, "in_stock", True))
    availability_ar = "متوفر" if in_stock else "غير متوفر"
    return {
        "title": (product.title or "").strip() or "منتج",
        "description": (product.description or "").strip(),
        "image_url": image_url,
        "price": price,
        "currency": currency,
        "availability_ar": availability_ar,
        "store_name": (tenant.name or "").strip() or "متجر",
        "wa_url": _whatsapp_cta_url(db, int(product.tenant_id)),
    }


@router.get("/items/{retailer_id}", response_class=HTMLResponse, include_in_schema=False)
async def public_native_product_page(
    retailer_id: str,
    response: Response,
    db: Session = Depends(get_db),
):
    """Minimal public product page for Nahla-native catalog items."""
    loaded = _load_public_native_product(db, retailer_id)
    if loaded is None:
        return HTMLResponse(content=_UNAVAILABLE_HTML, status_code=404)

    product, tenant = loaded
    vm = _product_view_model(db, product, tenant)
    html_doc = _render_product_html(**vm)
    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return HTMLResponse(content=html_doc, status_code=200)


__all__ = ["router", "plain_description", "_load_public_native_product"]
