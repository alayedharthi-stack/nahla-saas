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
    except ValueError as exc:
        logger.error(
            "[OrderService] tenant=%s create_order BLOCKED before POST | reason=%s",
            tenant_id, exc,
        )
        # Re-raise so the caller (runtime / brain) can see the specific
        # reason and react (e.g. ask the customer for product options
        # instead of treating this as a generic Salla failure).
        raise
    except Exception as exc:
        logger.error(f"[OrderService] tenant={tenant_id} create_order failed: {exc}")
        return None


async def create_draft_order(tenant_id: int, order_input: OrderInput) -> Optional[NormalizedOrder]:
    # ── Entry: every call to this function must produce a log ────────────────
    logger.error(
        "[ORDER FLOW] attempting create_order | tenant=%s product=%s "
        "has_city=%s has_address=%s has_options=%s qty=%s "
        "first_name=%s last_name=%s phone_set=%s",
        tenant_id,
        (order_input.items[0].product_id if order_input.items else "?"),
        bool(getattr(order_input, "city", None)),
        bool(
            getattr(order_input, "short_address_code", None)
            or getattr(order_input, "google_maps_url", None)
            or getattr(order_input, "latitude", None)
        ),
        bool(order_input.items and order_input.items[0].options),
        (order_input.items[0].quantity if order_input.items else 0),
        bool(getattr(order_input, "customer_first_name", "")),
        bool(getattr(order_input, "customer_last_name", "")),
        bool(getattr(order_input, "customer_phone", "")),
    )
    adapter = get_adapter(tenant_id)
    if not adapter:
        # This is a hard failure: no adapter = Salla not connected.
        logger.error(
            "[ORDER FLOW] create_order BLOCKED — no Salla adapter for tenant=%s "
            "(integration missing, disabled, or needs_reauth=True)",
            tenant_id,
        )
        return None
    logger.error(
        "[ORDER FLOW] runtime create_order called | tenant=%s "
        "adapter=%s integration_id=%s",
        tenant_id, adapter.platform,
        getattr(adapter, "_integration_id", "unknown"),
    )
    try:
        order = await adapter.create_draft_order(order_input)
        logger.info(
            "[OrderService] tenant=%s created draft order id=%s reference=%s on %s",
            tenant_id, order.id, getattr(order, "reference_id", None), adapter.platform,
        )
        return order
    except ValueError as exc:
        # Adapter-level pre-flight guard (e.g. required_product_options_missing
        # or SallaOrderValidationError). No POST /orders was issued — surface
        # the reason loudly and propagate so the runtime/brain can handle it
        # explicitly (ask the customer for the missing field).
        try:
            from store_adapters.salla_adapter import SallaOrderValidationError  # noqa: PLC0415
        except Exception:
            SallaOrderValidationError = None  # type: ignore
        if SallaOrderValidationError and isinstance(exc, SallaOrderValidationError):
            logger.error(
                "[OrderService] tenant=%s create_draft_order BLOCKED before POST | "
                "reason=salla_payload_invalid missing=%s",
                tenant_id, getattr(exc, "missing", []),
            )
        else:
            logger.error(
                "[OrderService] tenant=%s create_draft_order BLOCKED before POST | reason=%s",
                tenant_id, exc,
            )
        raise
    except Exception as exc:
        # Surface Salla's HTTP status + full response body to make Railway logs actionable
        if _httpx and isinstance(exc, _httpx.HTTPStatusError):
            _status = exc.response.status_code
            _body_text = exc.response.text or ""
            logger.error(
                "[OrderService] tenant=%s create_draft_order FAILED | "
                "salla_status=%d salla_response=%s",
                tenant_id, _status, _body_text[:2000],
            )
            # Parse Salla's structured 422 response so we can react
            # specifically to options-related rejections instead of
            # falling through to a generic retry/escalate.
            if _status == 422:
                try:
                    _body_json = exc.response.json()
                except Exception:
                    _body_json = {}
                _err_node = _body_json.get("error") if isinstance(_body_json, dict) else None
                _fields = (_err_node or {}).get("fields") if isinstance(_err_node, dict) else None
                if isinstance(_fields, dict):
                    _options_keys = [
                        k for k in _fields.keys()
                        if "option" in str(k).lower() or "خيار" in str(k)
                    ]
                    if _options_keys:
                        logger.error(
                            "[OrderService] tenant=%s Salla 422 → options field rejected | "
                            "fields=%s — re-raising as required_product_options_missing",
                            tenant_id, _options_keys,
                        )
                        raise ValueError("required_product_options_missing") from exc
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
