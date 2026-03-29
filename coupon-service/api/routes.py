from fastapi import APIRouter, HTTPException
from models.models import CouponCreate, CouponResponse, CouponRuleCreate
from services.business_logic import create_coupon as create_coupon_logic, add_coupon_rule as add_coupon_rule_logic, get_coupon_policy as get_coupon_policy_logic

router = APIRouter(prefix="/coupons", tags=["coupons"])

@router.post("/", response_model=CouponResponse)
async def create_coupon(payload: CouponCreate):
    return create_coupon_logic(payload)

@router.post("/{coupon_id}/rules")
async def add_coupon_rule(coupon_id: int, payload: CouponRuleCreate):
    return add_coupon_rule_logic(coupon_id, payload)

@router.get("/policy")
async def get_coupon_policy():
    return get_coupon_policy_logic()
