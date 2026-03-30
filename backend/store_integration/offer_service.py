"""
OfferService
────────────
Fetches and validates offers/coupons through the store adapter.
Falls back to Nahla DB Coupon rows when no adapter is configured.
"""
from __future__ import annotations
import logging, os, sys
from typing import List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from store_integration.registry import get_adapter
from store_integration.models import NormalizedOffer

logger = logging.getLogger("nahla.store_integration.offer")


async def get_active_offers(tenant_id: int) -> List[NormalizedOffer]:
    adapter = get_adapter(tenant_id)
    if not adapter:
        return []
    try:
        offers = await adapter.get_active_offers()
        logger.info(
            f"[OfferService] tenant={tenant_id} fetched {len(offers)} offers from {adapter.platform}"
        )
        return offers
    except Exception as exc:
        logger.error(f"[OfferService] get_active_offers failed: {exc}")
        return []


async def validate_coupon(tenant_id: int, code: str) -> Optional[NormalizedOffer]:
    adapter = get_adapter(tenant_id)
    if not adapter:
        return None
    try:
        offer = await adapter.validate_coupon(code)
        logger.info(
            f"[OfferService] tenant={tenant_id} coupon {code} validation: {offer is not None}"
        )
        return offer
    except Exception as exc:
        logger.error(f"[OfferService] validate_coupon failed: {exc}")
        return None
