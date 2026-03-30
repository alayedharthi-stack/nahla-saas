"""
OrderService
────────────
Create and fetch orders through the store adapter.
When a store adapter is present, orders are created IN the real store.
When not configured, orders remain as internal Nahla drafts.
"""
from __future__ import annotations
import logging, os, sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from store_integration.registry import get_adapter
from store_integration.models import NormalizedOrder, OrderInput

logger = logging.getLogger("nahla.store_integration.order")


async def create_order(tenant_id: int, order_input: OrderInput) -> Optional[NormalizedOrder]:
    adapter = get_adapter(tenant_id)
    if not adapter:
        return None
    try:
        order = await adapter.create_order(order_input)
        logger.info(
            f"[OrderService] tenant={tenant_id} created order {order.id} "
            f"on {adapter.platform} | status={order.status}"
        )
        return order
    except Exception as exc:
        logger.error(f"[OrderService] tenant={tenant_id} create_order failed: {exc}")
        return None


async def create_draft_order(tenant_id: int, order_input: OrderInput) -> Optional[NormalizedOrder]:
    adapter = get_adapter(tenant_id)
    if not adapter:
        return None
    try:
        order = await adapter.create_draft_order(order_input)
        logger.info(
            f"[OrderService] tenant={tenant_id} created draft order {order.id} on {adapter.platform}"
        )
        return order
    except Exception as exc:
        logger.error(f"[OrderService] tenant={tenant_id} create_draft_order failed: {exc}")
        return None


async def get_order(tenant_id: int, order_id: str) -> Optional[NormalizedOrder]:
    adapter = get_adapter(tenant_id)
    if not adapter:
        return None
    try:
        return await adapter.get_order(order_id)
    except Exception as exc:
        logger.error(f"[OrderService] get_order failed: {exc}")
        return None


async def get_customer_orders(tenant_id: int, customer_phone: str) -> List[NormalizedOrder]:
    adapter = get_adapter(tenant_id)
    if not adapter:
        return []
    try:
        return await adapter.get_customer_orders(customer_phone)
    except Exception as exc:
        logger.error(f"[OrderService] get_customer_orders failed: {exc}")
        return []
