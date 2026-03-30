"""
BaseStoreAdapter
────────────────
Abstract interface for all store platform adapters.
The AI Sales Agent and automations MUST call only this interface.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Optional
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from store_integration.models import (
    NormalizedProduct, NormalizedVariant, NormalizedOrder,
    OrderInput, ShippingOption, NormalizedOffer,
)


class BaseStoreAdapter(ABC):
    platform: str = "unknown"

    @abstractmethod
    async def get_products(self) -> List[NormalizedProduct]: ...

    @abstractmethod
    async def get_product(self, product_id: str) -> Optional[NormalizedProduct]: ...

    @abstractmethod
    async def get_product_variants(self, product_id: str) -> List[NormalizedVariant]: ...

    @abstractmethod
    async def create_order(self, order_input: OrderInput) -> NormalizedOrder: ...

    @abstractmethod
    async def create_draft_order(self, order_input: OrderInput) -> NormalizedOrder: ...

    @abstractmethod
    async def get_order(self, order_id: str) -> Optional[NormalizedOrder]: ...

    @abstractmethod
    async def get_customer_orders(self, customer_phone: str) -> List[NormalizedOrder]: ...

    @abstractmethod
    async def generate_payment_link(self, order_id: str, amount: float) -> Optional[str]: ...

    @abstractmethod
    async def get_shipping_options(self, city: str = "") -> List[ShippingOption]: ...

    @abstractmethod
    async def get_active_offers(self) -> List[NormalizedOffer]: ...

    @abstractmethod
    async def validate_coupon(self, code: str) -> Optional[NormalizedOffer]: ...
