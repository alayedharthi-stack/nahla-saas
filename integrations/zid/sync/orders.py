import sys
import os
from typing import Any, Dict

import httpx

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import Order, SyncLog
from database.session import SessionLocal

ZID_API_BASE = "https://api.zid.sa/v1"


def _zid_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Manager-Token": access_token,
        "Accept": "application/json",
    }


async def fetch_and_sync_orders(store_id: str, access_token: str, tenant_id: int) -> Dict[str, Any]:
    db = SessionLocal()
    synced = 0
    errors = 0
    page = 1

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                response = await client.get(
                    f"{ZID_API_BASE}/managers/orders/",
                    headers=_zid_headers(access_token),
                    params={"page": page, "limit": 50},
                )

                if response.status_code == 401:
                    _write_sync_log(db, tenant_id, "order", None, "error", "Token expired or invalid")
                    break

                response.raise_for_status()
                body = response.json()
                orders_block = body.get("orders", body)
                items = orders_block.get("data", orders_block) if isinstance(orders_block, dict) else orders_block
                meta = orders_block.get("meta", {}) if isinstance(orders_block, dict) else {}

                for item in items:
                    try:
                        external_id = str(item.get("id", ""))
                        order = db.query(Order).filter(
                            Order.tenant_id == tenant_id,
                            Order.external_id == external_id,
                        ).first()

                        if not order:
                            order = Order(tenant_id=tenant_id, external_id=external_id)
                            db.add(order)

                        order.status = str(item.get("status", "pending"))
                        order.total = str(item.get("total", item.get("total_amount", "")))
                        customer = item.get("customer", item.get("consumer", {}))
                        order.customer_info = {
                            "name": customer.get("name"),
                            "phone": customer.get("mobile", customer.get("phone")),
                            "email": customer.get("email"),
                        }
                        order.line_items = [
                            {
                                "product_id": str(li.get("product_id", li.get("id", ""))),
                                "name": li.get("name"),
                                "quantity": li.get("quantity"),
                                "price": str(li.get("price", li.get("unit_price", ""))),
                            }
                            for li in item.get("products", item.get("items", []))
                        ]
                        order.metadata = {"source": "zid"}
                        db.commit()
                        synced += 1
                    except Exception:
                        db.rollback()
                        errors += 1

                total_pages = meta.get("last_page", meta.get("total_pages", 1))
                if page >= total_pages:
                    break
                page += 1

        _write_sync_log(db, tenant_id, "order", None, "completed", f"Synced {synced} orders, {errors} errors")
        return {"success": True, "synced_records": synced, "details": {"errors": errors, "pages": page}}

    except httpx.HTTPError as exc:
        _write_sync_log(db, tenant_id, "order", None, "error", str(exc))
        return {"success": False, "synced_records": synced, "details": {"error": str(exc)}}
    finally:
        db.close()


def fetch_orders(store_id: str) -> Dict[str, Any]:
    return {"store_id": store_id, "orders": [], "synced": 0}


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
