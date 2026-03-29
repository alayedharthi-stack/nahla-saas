from models.models import CouponCreate, CouponResponse, CouponRuleCreate
from repositories.data_access import save_coupon, add_coupon_rule_to_coupon, get_coupon_policy_data


def create_coupon(payload: CouponCreate) -> CouponResponse:
    saved = save_coupon(payload)
    return CouponResponse(**saved)


def add_coupon_rule(coupon_id: int, payload: CouponRuleCreate) -> dict:
    return add_coupon_rule_to_coupon(coupon_id, payload)


def get_coupon_policy() -> dict:
    return get_coupon_policy_data()
