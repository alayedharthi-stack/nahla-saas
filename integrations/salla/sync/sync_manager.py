import asyncio
from typing import Any, Dict

from sync.products import fetch_and_sync_products
from sync.orders import fetch_and_sync_orders
from sync.customers import fetch_and_sync_customers


async def sync_all(store_id: str, access_token: str, tenant_id: int) -> Dict[str, Any]:
    """Run all three sync tasks concurrently after a store install."""
    products_result, orders_result, customers_result = await asyncio.gather(
        fetch_and_sync_products(store_id, access_token, tenant_id),
        fetch_and_sync_orders(store_id, access_token, tenant_id),
        fetch_and_sync_customers(store_id, access_token, tenant_id),
        return_exceptions=True,
    )
    return {
        "products": products_result if not isinstance(products_result, Exception) else {"success": False, "error": str(products_result)},
        "orders": orders_result if not isinstance(orders_result, Exception) else {"success": False, "error": str(orders_result)},
        "customers": customers_result if not isinstance(customers_result, Exception) else {"success": False, "error": str(customers_result)},
    }


async def sync_products_for_store(store_id: str, access_token: str, tenant_id: int) -> Dict[str, Any]:
    return await fetch_and_sync_products(store_id, access_token, tenant_id)


async def sync_orders_for_store(store_id: str, access_token: str, tenant_id: int) -> Dict[str, Any]:
    return await fetch_and_sync_orders(store_id, access_token, tenant_id)


async def sync_customers_for_store(store_id: str, access_token: str, tenant_id: int) -> Dict[str, Any]:
    return await fetch_and_sync_customers(store_id, access_token, tenant_id)
