from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class NormalizedVariant(BaseModel):
    id: str
    title: str
    price: Optional[float] = None
    sku: Optional[str] = None
    in_stock: bool = True
    stock_quantity: Optional[int] = None


class NormalizedProduct(BaseModel):
    id: str
    title: str
    price: Optional[float] = None
    currency: str = "SAR"
    sku: Optional[str] = None
    in_stock: bool = True
    stock_quantity: Optional[int] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    product_url: Optional[str] = None
    tags: List[str] = []
    variants: List[NormalizedVariant] = []


class OrderItemInput(BaseModel):
    product_id: str
    variant_id: Optional[str] = None
    quantity: int = 1


class OrderInput(BaseModel):
    customer_name: str
    customer_phone: str
    customer_email: Optional[str] = None
    # Saudi national address fields
    building_number: str = ""
    street: str = ""
    district: str = ""
    postal_code: str = ""
    city: str = ""
    address: str = ""
    payment_method: str = "cod"
    items: List[OrderItemInput]
    notes: Optional[str] = None


class OrderItem(BaseModel):
    product_id: str
    product_title: str
    variant_id: Optional[str] = None
    quantity: int
    unit_price: Optional[float] = None


class NormalizedOrder(BaseModel):
    id: str
    # Human-visible order number from the platform (Salla `reference_id`,
    # Zid `code`, Shopify `name`). Distinct from `id` (the platform's
    # internal numeric primary key). Falls back to `id` when the platform
    # doesn't expose a separate human number.
    reference_id: Optional[str] = None
    status: str
    total: float
    currency: str = "SAR"
    payment_link: Optional[str] = None
    customer_name: str
    customer_phone: str
    items: List[OrderItem] = []
    created_at: Optional[str] = None
    # Origin platform for this order. One of: salla | zid | shopify |
    # whatsapp | manual. Set by the adapter; the sync layer falls back to
    # the adapter's `platform` attribute when unset.
    source: Optional[str] = None


class ShippingOption(BaseModel):
    name: str
    cost: float
    currency: str = "SAR"
    estimated_days: Optional[str] = None
    zone: Optional[str] = None
    courier: Optional[str] = None


class NormalizedOffer(BaseModel):
    code: Optional[str] = None
    type: str = "percentage"
    value: float
    min_order: Optional[float] = None
    expires_at: Optional[str] = None
    description: Optional[str] = None
    valid: bool = True
