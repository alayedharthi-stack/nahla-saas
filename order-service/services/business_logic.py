from models.models import OrderCreate, OrderResponse, CheckoutResponse
from repositories.data_access import save_order, get_order_by_id, list_abandoned_orders_data


def create_order(payload: OrderCreate) -> OrderResponse:
    saved = save_order(payload)
    return OrderResponse(**saved)


def get_order(order_id: int) -> OrderResponse | None:
    order = get_order_by_id(order_id)
    if order is None:
        return None
    return OrderResponse(**order)


def create_checkout(order_id: int) -> CheckoutResponse:
    return CheckoutResponse(checkout_url=f"https://checkout.example.com/order/{order_id}")


def list_abandoned_orders() -> list[dict]:
    return list_abandoned_orders_data()
