from fastapi import APIRouter, HTTPException
from models.models import OrderCreate, OrderResponse, CheckoutResponse
from services.business_logic import create_order as create_order_logic, get_order as get_order_logic, create_checkout as create_checkout_logic, list_abandoned_orders as list_abandoned_orders_logic

router = APIRouter(prefix="/orders", tags=["orders"])

@router.post("/", response_model=OrderResponse)
async def create_order(payload: OrderCreate):
    return create_order_logic(payload)

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(order_id: int):
    order = get_order_logic(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@router.post("/{order_id}/checkout", response_model=CheckoutResponse)
async def create_checkout(order_id: int):
    return create_checkout_logic(order_id)

@router.get("/abandoned")
async def list_abandoned_orders():
    return list_abandoned_orders_logic()
