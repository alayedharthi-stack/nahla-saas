from models.models import CouponCreate, CouponRuleCreate


def save_coupon(payload: CouponCreate) -> dict:
    return {
        "id": 1,
        "tenant_id": payload.tenant_id,
        "code": payload.code,
        "description": payload.description,
        "discount_type": payload.discount_type,
        "discount_value": payload.discount_value,
        "metadata": payload.metadata,
        "expires_at": payload.expires_at,
    }


def add_coupon_rule_to_coupon(coupon_id: int, payload: CouponRuleCreate) -> dict:
    return {"status": "success", "coupon_id": coupon_id, "rule": payload.dict()}


def get_coupon_policy_data() -> dict:
    return {
        "min_discount": 5,
        "max_discount": 50,
        "allowed_types": ["percentage", "fixed_amount"],
        "auto_generation_enabled": True,
    }
