"""Read-only Commerce Agent tools, grouped by delivery phase."""

from .catalog import get_product_details, search_products
from .knowledge import search_merchant_knowledge, search_product_knowledge
from .orders import get_order_details, get_order_shipment, resolve_customer_order

PHASE1_TOOLS = [
    search_products,
    get_product_details,
    search_merchant_knowledge,
    search_product_knowledge,
]

PHASE2_TOOLS = [
    resolve_customer_order,
    get_order_details,
    get_order_shipment,
]

COMMERCE_AGENT_TOOLS = [*PHASE1_TOOLS, *PHASE2_TOOLS]

__all__ = [
    "COMMERCE_AGENT_TOOLS",
    "PHASE1_TOOLS",
    "PHASE2_TOOLS",
    "get_order_details",
    "get_order_shipment",
    "get_product_details",
    "search_merchant_knowledge",
    "search_product_knowledge",
    "search_products",
    "resolve_customer_order",
]
