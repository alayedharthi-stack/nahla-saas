import sys
import os
from typing import Any, Dict

import httpx

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import Customer, SyncLog
from database.session import SessionLocal

SALLA_API_BASE = "https://api.salla.dev/admin/v2"


async def fetch_and_sync_customers(store_id: str, access_token: str, tenant_id: int) -> Dict[str, Any]:
    db = SessionLocal()
    synced = 0
    errors = 0
    page = 1

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                response = await client.get(
                    f"{SALLA_API_BASE}/customers",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                    params={"page": page, "per_page": 50},
                )

                if response.status_code == 401:
                    _write_sync_log(db, tenant_id, "customer", None, "error", "Token expired or invalid")
                    break

                response.raise_for_status()
                body = response.json()
                items = body.get("data", [])
                pagination = body.get("pagination", {})

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
                        customer.phone = item.get("mobile")
                        customer.metadata = {
                            "external_id": external_id,
                            "source": "salla",
                            "city": item.get("city"),
                            "country": item.get("country", "SA"),
                        }
                        db.commit()
                        synced += 1
                    except Exception:
                        db.rollback()
                        errors += 1

                if page >= pagination.get("total_pages", 1):
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
    """Sync-compatible wrapper used by sync_manager."""
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
