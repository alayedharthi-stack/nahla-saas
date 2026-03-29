import sys
import os
from typing import Any, Dict

import httpx

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import Customer, SyncLog
from database.session import SessionLocal

ZID_API_BASE = "https://api.zid.sa/v1"


def _zid_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Manager-Token": access_token,
        "Accept": "application/json",
    }


async def fetch_and_sync_customers(store_id: str, access_token: str, tenant_id: int) -> Dict[str, Any]:
    db = SessionLocal()
    synced = 0
    errors = 0
    page = 1

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                response = await client.get(
                    f"{ZID_API_BASE}/profile/customers/",
                    headers=_zid_headers(access_token),
                    params={"page": page, "limit": 50},
                )

                if response.status_code == 401:
                    _write_sync_log(db, tenant_id, "customer", None, "error", "Token expired or invalid")
                    break

                response.raise_for_status()
                body = response.json()
                customers_block = body.get("customers", body)
                items = customers_block.get("data", customers_block) if isinstance(customers_block, dict) else customers_block
                meta = customers_block.get("meta", {}) if isinstance(customers_block, dict) else {}

                for item in items:
                    try:
                        external_id = str(item.get("id", ""))
                        customer = db.query(Customer).filter(
                            Customer.tenant_id == tenant_id,
                            Customer.metadata["external_id"].astext == external_id,
                        ).first()

                        if not customer:
                            customer = Customer(tenant_id=tenant_id)
                            db.add(customer)

                        customer.name = item.get("name")
                        customer.email = item.get("email")
                        customer.phone = item.get("mobile", item.get("phone"))
                        customer.metadata = {
                            "external_id": external_id,
                            "source": "zid",
                            "city": item.get("city"),
                            "country": item.get("country", "SA"),
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

        _write_sync_log(db, tenant_id, "customer", None, "completed", f"Synced {synced} customers, {errors} errors")
        return {"success": True, "synced_records": synced, "details": {"errors": errors, "pages": page}}

    except httpx.HTTPError as exc:
        _write_sync_log(db, tenant_id, "customer", None, "error", str(exc))
        return {"success": False, "synced_records": synced, "details": {"error": str(exc)}}
    finally:
        db.close()


def fetch_customers(store_id: str) -> Dict[str, Any]:
    return {"store_id": store_id, "customers": [], "synced": 0}


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
