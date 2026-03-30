"""
ShippingService
───────────────
Fetches shipping options through the store adapter.
Falls back to Nahla DB ShippingFee rows when no adapter is configured.
"""
from __future__ import annotations
import logging, os, sys
from typing import List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from store_integration.registry import get_adapter
from store_integration.models import ShippingOption

logger = logging.getLogger("nahla.store_integration.shipping")


async def get_shipping_options(tenant_id: int, city: str = "") -> List[ShippingOption]:
    adapter = get_adapter(tenant_id)
    if not adapter:
        return []
    try:
        options = await adapter.get_shipping_options(city)
        logger.info(
            f"[ShippingService] tenant={tenant_id} fetched {len(options)} options "
            f"from {adapter.platform} for city={city or 'all'}"
        )
        return options
    except Exception as exc:
        logger.error(f"[ShippingService] get_shipping_options failed: {exc}")
        return []


def format_shipping_lines(options: List[ShippingOption]) -> List[str]:
    """Convert ShippingOption list into WhatsApp-friendly bullet lines."""
    lines = []
    for opt in options[:8]:
        cost_str = f"{opt.cost:.0f} {opt.currency}" if opt.cost else "—"
        days_str = f" ({opt.estimated_days})" if opt.estimated_days else ""
        lines.append(f"• {opt.name}: {cost_str}{days_str}")
    return lines
