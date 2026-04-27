"""
OrderService
────────────
Create and fetch orders through the store adapter.
When a store adapter is present, orders are created IN the real store.
When not configured, orders remain as internal Nahla drafts.
"""
from __future__ import annotations
import logging, os, sys
from typing import Any, Dict, List, Optional  # noqa: F401

try:
    import httpx as _httpx
except ImportError:
    _httpx = None  # type: ignore[assignment]

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
        logger.warning("[OrderService] tenant=%s create_draft_order — no adapter found", tenant_id)
        return None
    try:
        order = await adapter.create_draft_order(order_input)
        logger.info(
            "[OrderService] tenant=%s created draft order id=%s on %s",
            tenant_id, order.id, adapter.platform,
        )
        return order
    except Exception as exc:
        # Surface Salla's HTTP status + full response body to make Railway logs actionable
        if _httpx and isinstance(exc, _httpx.HTTPStatusError):
            logger.error(
                "[OrderService] tenant=%s create_draft_order FAILED | "
                "salla_status=%d salla_response=%s",
                tenant_id, exc.response.status_code, exc.response.text[:2000],
            )
        else:
            logger.error(
                "[OrderService] tenant=%s create_draft_order FAILED | error=%s",
                tenant_id, exc,
            )
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


async def get_default_shipping_company_id(tenant_id: int, city: str = "") -> Optional[int]:
    """Return the first available Salla shipping company/zone ID for a tenant.

    Used by the order flow to auto-select a shipping method without asking the
    customer. Returns None if the adapter is missing or no zones are configured.
    """
    adapter = get_adapter(tenant_id)
    if not adapter:
        return None
    _fn = getattr(adapter, "_get_default_shipping_company_id", None)
    if _fn is None:
        return None
    try:
        return await _fn(city)
    except Exception as exc:
        logger.warning(
            "[OrderService] get_default_shipping_company_id failed | tenant=%s err=%s",
            tenant_id, exc,
        )
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
