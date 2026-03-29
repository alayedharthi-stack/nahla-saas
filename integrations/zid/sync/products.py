import sys
import os
from typing import Any, Dict

import httpx

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import Product, SyncLog
from database.session import SessionLocal

ZID_API_BASE = "https://api.zid.sa/v1"


def _zid_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Manager-Token": access_token,
        "Accept": "application/json",
    }


async def fetch_and_sync_products(store_id: str, access_token: str, tenant_id: int) -> Dict[str, Any]:
    db = SessionLocal()
    synced = 0
    errors = 0
    page = 1

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                response = await client.get(
                    f"{ZID_API_BASE}/managers/products/",
                    headers=_zid_headers(access_token),
                    params={"page": page, "limit": 50},
                )

                if response.status_code == 401:
                    _write_sync_log(db, tenant_id, "product", None, "error", "Token expired or invalid")
                    break

                response.raise_for_status()
                body = response.json()
                # Zid response: {"products": {"data": [...], "meta": {...}}}
                products_block = body.get("products", body)
                items = products_block.get("data", products_block) if isinstance(products_block, dict) else products_block
                meta = products_block.get("meta", {}) if isinstance(products_block, dict) else {}

                for item in items:
                    try:
                        external_id = str(item.get("id", ""))
                        product = db.query(Product).filter(
                            Product.tenant_id == tenant_id,
                            Product.external_id == external_id,
                        ).first()

                        if not product:
                            product = Product(tenant_id=tenant_id, external_id=external_id)
                            db.add(product)

                        product.title = item.get("name", "")
                        product.description = item.get("description")
                        product.sku = item.get("sku")
                        raw_price = item.get("price", item.get("sale_price", ""))
                        product.price = str(raw_price.get("amount", raw_price) if isinstance(raw_price, dict) else raw_price)
                        images = item.get("images", [])
                        product.metadata = {
                            "status": item.get("status", item.get("is_active")),
                            "thumbnail": images[0].get("url") if images else None,
                            "quantity": item.get("quantity"),
                            "source": "zid",
                        }
                        db.commit()
                        synced += 1
                    except Exception:
                        db.rollback()
                        errors += 1

                total_pages = meta.get("last_page", meta.get("total_pages", 1))
                if page >= total_pages:
                    break
                page += 1

        _write_sync_log(db, tenant_id, "product", None, "completed", f"Synced {synced} products, {errors} errors")
        return {"success": True, "synced_records": synced, "details": {"errors": errors, "pages": page}}

    except httpx.HTTPError as exc:
        _write_sync_log(db, tenant_id, "product", None, "error", str(exc))
        return {"success": False, "synced_records": synced, "details": {"error": str(exc)}}
    finally:
        db.close()


def fetch_products(store_id: str) -> Dict[str, Any]:
    return {"store_id": store_id, "products": [], "synced": 0}


def _write_sync_log(db, tenant_id: int, resource_type: str, external_id, status: str, message: str = "") -> None:
    log = SyncLog(
        tenant_id=tenant_id,
        resource_type=resource_type,
        external_id=external_id,
        status=status,
        message=message,
    )
    db.add(log)
    db.commit()
