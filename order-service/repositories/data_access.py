from models.models import OrderCreate


def save_order(payload: OrderCreate) -> dict:
    return {
        "id": 1,
        "tenant_id": payload.tenant_id,
        "external_id": payload.external_id or "order-1",
        "status": "draft",
        "total": payload.total,
        "customer_info": payload.customer_info,
        "line_items": payload.line_items,
        "checkout_url": None,
        "is_abandoned": False,
        "metadata": payload.metadata or {},
    }


def get_order_by_id(order_id: int) -> dict | None:
    if order_id != 1:
        return None
    return {
        "id": order_id,
        "tenant_id": 1,
        "external_id": "order-1",
        "status": "pending",
        "total": "120.00",
        "customer_info": {"name": "Demo Customer"},
        "line_items": [{"sku": "SKU-1", "quantity": 1, "price": "120.00"}],
        "checkout_url": "https://example.com/checkout/1",
        "is_abandoned": False,
        "metadata": {},
    }


def list_abandoned_orders_data() -> list[dict]:
    return [{"order_id": 1, "external_id": "order-1", "status": "abandoned"}]
