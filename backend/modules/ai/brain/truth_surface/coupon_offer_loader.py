"""
coupon_offer_loader.py
──────────────────────
Shadow-only coupon and promotion eligibility facts for Trusted Context.

Read-only: no materialisation, no usage_count mutation, no customer prose.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from .contract import TrustedDomain, TrustedFact, TruthSource

_COUPON_OFFER_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"كوبون",
        r"كود\s*خصم",
        r"خصم",
        r"عرض(?:ات)?",
        r"\bcoupon\b",
        r"\bpromotion\b",
        r"\bdiscount\b",
        r"\boffer\b",
    )
)

_OFFER_PRODUCT_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"عرض", re.IGNORECASE | re.UNICODE),
    re.compile(r"\boffer\b", re.IGNORECASE),
)

_DISCOUNT_CART_PATTERNS: Tuple[re.Pattern[str], ...] = _COUPON_OFFER_PATTERNS[:4]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _prep_dict(brain_state: Any) -> Dict[str, Any]:
    prep = getattr(brain_state, "order_prep", None) if brain_state else None
    if prep is None:
        return {}
    if isinstance(prep, dict):
        return dict(prep)
    out: Dict[str, Any] = {}
    for key in (
        "line_items",
        "catalog_line_items_authoritative",
        "catalog_checkout_total",
        "coupon_code",
        "applied_coupon_code",
        "discount_code",
    ):
        if hasattr(prep, key):
            val = getattr(prep, key, None)
            if val not in (None, ""):
                out[key] = val
    return out


def mask_coupon_code(code: str) -> str:
    """Hash-mask a coupon code for logs and observability."""
    raw = (code or "").strip()
    if not raw:
        return ""
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    tail = raw[-2:] if len(raw) > 2 else "**"
    return f"***{tail}#{digest}"


def should_load_coupon_promotion_facts(
    message: str = "",
    brain_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Lazy-load gate — semantic / entity based, not tenant-specific.

  Triggers when coupon, discount, or offer intent is present, or when an
  active cart/order needs eligibility evaluation with discount context.
    """
    text = (message or "").strip()
    meta = inbound_metadata or {}
    prep = _prep_dict(brain_state)

    for key in ("coupon_code", "promotion_id", "discount_code", "applied_coupon_code"):
        if meta.get(key):
            return True

    for pat in _COUPON_OFFER_PATTERNS:
        if pat.search(text):
            return True

    product_focus = getattr(brain_state, "current_product_focus", None) if brain_state else None
    if product_focus and any(p.search(text) for p in _OFFER_PRODUCT_PATTERNS):
        return True

    if _has_cart_context(prep, meta):
        if any(p.search(text) for p in _DISCOUNT_CART_PATTERNS):
            return True
        for key in ("coupon_code", "applied_coupon_code", "discount_code"):
            if prep.get(key) or meta.get(key):
                return True

    return False


def _has_cart_context(prep: Dict[str, Any], meta: Dict[str, Any]) -> bool:
    if prep.get("line_items") or prep.get("catalog_line_items_authoritative"):
        return True
    if prep.get("catalog_checkout_total") not in (None, "", 0, 0.0):
        return True
    if meta.get("cart_total") not in (None, "", 0, 0.0):
        return True
    return False


def _resolve_basket_total(
    prep: Dict[str, Any],
    inbound_metadata: Optional[Dict[str, Any]],
) -> Optional[float]:
    meta = inbound_metadata or {}
    for source in (
        prep.get("catalog_checkout_total"),
        meta.get("cart_total"),
        meta.get("basket_total"),
    ):
        if source in (None, ""):
            continue
        try:
            return float(source)
        except (TypeError, ValueError):
            continue
    return None


def _resolve_customer_id(
    db: Any,
    tenant_id: int,
    customer_phone: str,
    conversation: Any,
) -> Optional[int]:
    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415

        order_ctx = build_order_context(
            db,
            tenant_id,
            customer_phone=customer_phone,
            conversation=conversation,
        )
        identity = getattr(order_ctx, "identity", None)
        cid = getattr(identity, "customer_id", None) if identity else None
        if cid is not None:
            return int(cid)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — customer id resolution is best-effort in shadow
        pass
    return None


def _resolve_customer_profile(db: Any, tenant_id: int, customer_id: Optional[int]) -> Any:
    if db is None or customer_id is None:
        return None
    try:
        from models import CustomerProfile  # noqa: PLC0415

        return (
            db.query(CustomerProfile)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.customer_id == customer_id,
            )
            .first()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — profile lookup is best-effort in shadow
        return None


def _applied_coupon_codes(
    prep: Dict[str, Any],
    inbound_metadata: Optional[Dict[str, Any]],
) -> Set[str]:
    codes: Set[str] = set()
    meta = inbound_metadata or {}
    for source in (prep, meta):
        for key in ("coupon_code", "applied_coupon_code", "discount_code"):
            val = source.get(key)
            if val:
                codes.add(str(val).strip().upper())
    return codes


def _line_item_product_ids(prep: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()
    items = prep.get("line_items") or prep.get("catalog_line_items_authoritative") or []
    if not isinstance(items, list):
        return ids
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("product_id", "sku", "external_id", "id"):
            val = item.get(key)
            if val not in (None, ""):
                ids.add(str(val))
    return ids


def _meta_bool(meta: Dict[str, Any], key: str, *, default: Optional[bool] = None) -> Optional[bool]:
    if key not in meta:
        return default
    val = meta.get(key)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        lowered = val.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    if val is None:
        return default
    return bool(val)


def _coupon_usage(meta: Dict[str, Any]) -> Tuple[int, Optional[int], bool]:
    usage_count = 0
    try:
        usage_count = int(meta.get("usage_count") or 0)
    except (TypeError, ValueError):
        usage_count = 0
    usage_limit_raw = meta.get("usage_limit")
    if usage_limit_raw is None:
        usage_limit_raw = meta.get("max_uses")
    usage_limit: Optional[int] = None
    if usage_limit_raw not in (None, "", 0):
        try:
            usage_limit = int(usage_limit_raw)
        except (TypeError, ValueError):
            usage_limit = None
    used_flag = _meta_bool(meta, "used", default=False) is True
    if used_flag:
        usage_count = max(usage_count, 1)
    limit_reached = (
        usage_limit is not None
        and usage_limit > 0
        and usage_count >= usage_limit
    )
    return usage_count, usage_limit, limit_reached


def _coupon_product_restrictions(
    coupon: Any,
    meta: Dict[str, Any],
    line_product_ids: Set[str],
) -> Tuple[List[Any], List[Any], str, Optional[bool]]:
    product_ids = list(meta.get("product_ids") or meta.get("applicable_products") or [])
    category_ids = list(meta.get("category_ids") or meta.get("applicable_categories") or [])

    rules = getattr(coupon, "rules", None) or []
    for rule in rules:
        rule_type = (getattr(rule, "rule_type", None) or "").lower()
        cfg = getattr(rule, "rule_config", None) or {}
        if rule_type in {"product", "products"}:
            product_ids.extend(cfg.get("product_ids") or cfg.get("ids") or [])
        if rule_type in {"category", "categories"}:
            category_ids.extend(cfg.get("category_ids") or cfg.get("ids") or [])

    product_ids = [str(x) for x in product_ids if x not in (None, "")]
    category_ids = [str(x) for x in category_ids if x not in (None, "")]

    if not product_ids and not category_ids:
        return [], [], "not_applicable", None

    if category_ids:
        return product_ids, category_ids, "advisory", None

    if not product_ids:
        return [], category_ids, "advisory", None

    if not line_product_ids:
        return product_ids, category_ids, "unknown", None

    matched = any(pid in line_product_ids for pid in product_ids)
    if matched:
        return product_ids, category_ids, "pass", True
    return product_ids, category_ids, "fail", False


def build_coupon_eligibility_record(
    coupon: Any,
    *,
    tenant_id: int,
    customer_id: Optional[int],
    basket_total: Optional[float],
    applied_codes: Set[str],
    observed_at: str,
    include_code: bool = False,
    line_product_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    meta = dict(getattr(coupon, "extra_metadata", None) or {})
    now = _utcnow()
    expires = _as_aware(getattr(coupon, "expires_at", None))
    expired = expires is not None and expires <= now

    active_state = _meta_bool(meta, "active", default=True)
    disabled_state = active_state is False or _meta_bool(meta, "disabled", default=False) is True

    usage_count, usage_limit, usage_limit_reached = _coupon_usage(meta)

    restricted_customer = meta.get("customer_id")
    customer_restriction = str(restricted_customer) if restricted_customer not in (None, "") else None
    is_personalised = customer_restriction is not None
    customer_eligible: Optional[bool]
    if is_personalised:
        if customer_id is None:
            customer_eligible = None
        else:
            customer_eligible = str(customer_id) == str(restricted_customer)
    else:
        customer_eligible = True

    min_basket_raw = meta.get("min_order_amount") or meta.get("minimum_basket")
    minimum_basket: Optional[float] = None
    if min_basket_raw not in (None, ""):
        try:
            minimum_basket = float(min_basket_raw)
        except (TypeError, ValueError):
            minimum_basket = None

    basket_eligible: Optional[bool] = None
    if minimum_basket is not None:
        if basket_total is None:
            basket_eligible = None
        else:
            basket_eligible = basket_total >= minimum_basket

    prep_ids = line_product_ids or set()
    product_restrictions, category_restrictions, pc_state, pc_eligible = _coupon_product_restrictions(
        coupon, meta, prep_ids,
    )

    code = (getattr(coupon, "code", None) or "").strip()
    code_upper = code.upper()
    applied_state = code_upper in applied_codes or _meta_bool(meta, "used", default=False) is True
    available_state = not applied_state and not expired and not disabled_state and not usage_limit_reached

    eligible: Optional[bool] = True
    reason_when_unavailable: Optional[str] = None
    verified = True

    if int(getattr(coupon, "tenant_id", 0) or 0) != int(tenant_id):
        eligible = False
        verified = True
        reason_when_unavailable = "tenant_mismatch"
    elif expired:
        eligible = False
        reason_when_unavailable = "expired"
    elif disabled_state:
        eligible = False
        reason_when_unavailable = "disabled"
    elif usage_limit_reached:
        eligible = False
        reason_when_unavailable = "usage_limit_reached"
    elif customer_eligible is False:
        eligible = False
        reason_when_unavailable = "customer_restriction"
    elif customer_eligible is None and is_personalised:
        eligible = None
        verified = False
        reason_when_unavailable = "customer_unverified"
    elif basket_eligible is False:
        eligible = False
        reason_when_unavailable = "minimum_basket_not_met"
    elif basket_eligible is None and minimum_basket is not None:
        eligible = None
        verified = False
        reason_when_unavailable = "minimum_basket_unverified"
    elif pc_state == "fail":
        eligible = False
        reason_when_unavailable = "product_restriction_not_met"
    elif pc_state in {"advisory", "unknown"}:
        eligible = None
        verified = False
        reason_when_unavailable = "product_category_advisory_unverified"
    elif applied_state:
        eligible = False
        reason_when_unavailable = "already_applied"
    elif is_personalised and customer_eligible is not True:
        eligible = None
        verified = False
        reason_when_unavailable = "personalised_unverified"
    else:
        eligible = True

    record: Dict[str, Any] = {
        "domain": TrustedDomain.COUPONS.value,
        "coupon_id": int(coupon.id),
        "tenant_id": int(tenant_id),
        "source": str(getattr(coupon, "source_type", None) or "coupon_table"),
        "code_masked": mask_coupon_code(code),
        "active_state": active_state is not False and not expired,
        "disabled_state": disabled_state,
        "expires_at": expires.isoformat() if expires else None,
        "usage_count": usage_count,
        "usage_limit": usage_limit,
        "usage_limit_reached": usage_limit_reached,
        "customer_restriction": customer_restriction,
        "customer_eligible": customer_eligible,
        "minimum_basket": minimum_basket,
        "basket_total": basket_total,
        "basket_eligible": basket_eligible,
        "product_restrictions": product_restrictions,
        "category_restrictions": category_restrictions,
        "product_category_eligibility_state": pc_state,
        "applied_state": applied_state,
        "available_state": available_state,
        "personalised": is_personalised,
        "eligible": eligible,
        "verified": verified,
        "reason_when_unavailable": reason_when_unavailable,
        "observed_at": observed_at,
        "freshness": "live_read",
        "actionability": "shadow_read_only",
    }
    if include_code and code:
        record["code"] = code
    return record


def build_promotion_eligibility_record(
    promo: Any,
    *,
    tenant_id: int,
    customer_profile: Any,
    basket_total: Optional[float],
    observed_at: str,
    conflict_state: str = "none",
) -> Dict[str, Any]:
    from services.promotion_engine import (  # noqa: PLC0415
        evaluate_conditions,
        is_promotion_active,
    )

    now = _utcnow()
    cond = dict(getattr(promo, "conditions", None) or {})
    usage_count = int(getattr(promo, "usage_count", 0) or 0)
    usage_limit_raw = getattr(promo, "usage_limit", None)
    usage_limit = int(usage_limit_raw) if usage_limit_raw not in (None, "") else None
    usage_available = not (
        usage_limit is not None and usage_limit > 0 and usage_count >= usage_limit
    )

    active_window = is_promotion_active(promo, now=now)
    active_window_result = "active" if active_window else "inactive"

    cart_decimal = Decimal(str(basket_total)) if basket_total is not None else None
    cond_passed, cond_reason = evaluate_conditions(
        promo,
        customer_profile=customer_profile,
        cart_total=cart_decimal,
    )

    segments_required = cond.get("customer_segments") or []
    if segments_required:
        if cond_reason and "segment_mismatch" in str(cond_reason):
            segment_result = "fail"
        else:
            segment_result = "pass"
    else:
        segment_result = "not_applicable"

    min_amount = cond.get("min_order_amount")
    minimum_basket: Optional[float] = None
    if min_amount is not None:
        try:
            minimum_basket = float(min_amount)
        except (TypeError, ValueError):
            minimum_basket = None

    if minimum_basket is not None:
        if basket_total is None:
            basket_result = "unknown"
            basket_eligible = None
        elif basket_total < minimum_basket:
            basket_result = "fail"
            basket_eligible = False
        else:
            basket_result = "pass"
            basket_eligible = True
    else:
        basket_result = "not_applicable"
        basket_eligible = None

    applicable_products = list(cond.get("applicable_products") or [])
    applicable_categories = list(cond.get("applicable_categories") or [])
    has_product_cond = bool(applicable_products)
    has_category_cond = bool(applicable_categories)
    has_bxgy = (
        (getattr(promo, "promotion_type", None) or "") == "buy_x_get_y"
        or cond.get("x_quantity") is not None
    )

    product_result = "advisory" if has_product_cond else "not_applicable"
    category_result = "advisory" if has_category_cond else "not_applicable"
    buy_x_get_y_result = "unknown" if has_bxgy else "not_applicable"

    eligible: Optional[bool] = True
    reason_when_unavailable: Optional[str] = None
    verified = True

    if int(getattr(promo, "tenant_id", 0) or 0) != int(tenant_id):
        eligible = False
        reason_when_unavailable = "tenant_mismatch"
    elif not active_window:
        eligible = False
        reason_when_unavailable = "outside_active_window"
    elif not usage_available:
        eligible = False
        reason_when_unavailable = "usage_limit_reached"
    elif segment_result == "fail":
        eligible = False
        reason_when_unavailable = "segment_mismatch"
    elif basket_result == "fail":
        eligible = False
        reason_when_unavailable = "below_min_order_amount"
    elif basket_result == "unknown":
        eligible = None
        verified = False
        reason_when_unavailable = "minimum_basket_unverified"
    elif has_product_cond or has_category_cond or has_bxgy:
        eligible = None
        verified = False
        reason_when_unavailable = "advisory_conditions_unverified"
    elif not cond_passed and cond_reason:
        eligible = False
        reason_when_unavailable = cond_reason
    else:
        eligible = True

    if conflict_state != "none" and eligible is True:
        eligible = None
        verified = False
        reason_when_unavailable = conflict_state

    starts = _as_aware(getattr(promo, "starts_at", None))
    ends = _as_aware(getattr(promo, "ends_at", None))

    return {
        "domain": TrustedDomain.PROMOTIONS.value,
        "promotion_id": int(promo.id),
        "tenant_id": int(tenant_id),
        "source": "promotion_table",
        "status": str(getattr(promo, "status", None) or ""),
        "starts_at": starts.isoformat() if starts else None,
        "ends_at": ends.isoformat() if ends else None,
        "active_window_result": active_window_result,
        "usage_count": usage_count,
        "usage_limit": usage_limit,
        "usage_available": usage_available,
        "segment_result": segment_result,
        "minimum_basket": minimum_basket,
        "basket_total": basket_total,
        "basket_result": basket_result,
        "applicable_products": applicable_products,
        "applicable_categories": applicable_categories,
        "product_result": product_result,
        "category_result": category_result,
        "buy_x_get_y_result": buy_x_get_y_result,
        "priority": (getattr(promo, "extra_metadata", None) or {}).get("priority"),
        "conflict_state": conflict_state,
        "eligible": eligible,
        "verified": verified,
        "reason_when_unavailable": reason_when_unavailable,
        "observed_at": observed_at,
        "freshness": "live_read",
        "actionability": "shadow_read_only",
    }


def load_coupon_promotion_facts(
    *,
    db: Any,
    tenant_id: int,
    customer_phone: str,
    message: str = "",
    brain_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    conversation: Any = None,
) -> Tuple[List[TrustedFact], Dict[str, Any]]:
    """
    Load coupon and promotion eligibility facts (read-only).

    Returns ``(facts, shadow_observability)``.
    """
    started = time.perf_counter()
    facts: List[TrustedFact] = []
    observed_at = _utcnow().isoformat()
    prep = _prep_dict(brain_state)
    meta = inbound_metadata or {}
    basket_total = _resolve_basket_total(prep, meta)
    applied_codes = _applied_coupon_codes(prep, meta)
    customer_id = _resolve_customer_id(db, tenant_id, customer_phone, conversation)
    customer_profile = _resolve_customer_profile(db, tenant_id, customer_id)
    line_product_ids = _line_item_product_ids(prep)

    coupon_reason_codes: List[str] = []
    promotion_reason_codes: List[str] = []
    eligible_coupon_count = 0
    eligible_promotion_count = 0

    from models import Coupon, Promotion  # noqa: PLC0415

    coupons = (
        db.query(Coupon)
        .filter(Coupon.tenant_id == tenant_id)
        .limit(50)
        .all()
    )

    include_code_in_snapshot = bool(
        meta.get("coupon_code") or meta.get("discount_code") or applied_codes,
    )

    for coupon in coupons:
        record = build_coupon_eligibility_record(
            coupon,
            tenant_id=tenant_id,
            customer_id=customer_id,
            basket_total=basket_total,
            applied_codes=applied_codes,
            observed_at=observed_at,
            include_code=include_code_in_snapshot,
            line_product_ids=line_product_ids,
        )
        # Product restriction re-evaluation is handled inside build_coupon_eligibility_record.

        if record.get("eligible") is True:
            eligible_coupon_count += 1
        elif record.get("reason_when_unavailable"):
            coupon_reason_codes.append(str(record["reason_when_unavailable"]))

        facts.append(
            TrustedFact(
                domain=TrustedDomain.COUPONS,
                key=f"coupon:{record['coupon_id']}",
                value=record,
                source=TruthSource.COUPON_TABLE,
                path=f"coupon_table.id={record['coupon_id']}",
            )
        )

    if not coupons:
        missing_record = {
            "domain": TrustedDomain.COUPONS.value,
            "tenant_id": int(tenant_id),
            "eligible": None,
            "verified": False,
            "available_state": False,
            "reason_when_unavailable": "no_coupon_data",
            "observed_at": observed_at,
            "freshness": "live_read",
            "actionability": "shadow_read_only",
        }
        facts.append(
            TrustedFact(
                domain=TrustedDomain.COUPONS,
                key="coupon:unavailable",
                value=missing_record,
                source=TruthSource.COUPON_TABLE,
                path="coupon_table.empty",
            )
        )
        coupon_reason_codes.append("no_coupon_data")

    promotions = (
        db.query(Promotion)
        .filter(Promotion.tenant_id == tenant_id)
        .limit(50)
        .all()
    )

    promo_records: List[Dict[str, Any]] = []
    for promo in promotions:
        record = build_promotion_eligibility_record(
            promo,
            tenant_id=tenant_id,
            customer_profile=customer_profile,
            basket_total=basket_total,
            observed_at=observed_at,
        )
        promo_records.append(record)

    would_be_eligible_count = sum(1 for r in promo_records if r.get("eligible") is True)
    conflict_state = (
        "multiple_active_unresolved" if would_be_eligible_count > 1 else "none"
    )
    if conflict_state != "none":
        promo_records = [
            build_promotion_eligibility_record(
                promo,
                tenant_id=tenant_id,
                customer_profile=customer_profile,
                basket_total=basket_total,
                observed_at=observed_at,
                conflict_state=conflict_state,
            )
            for promo in promotions
        ]

    for record in promo_records:
        if record.get("eligible") is True:
            eligible_promotion_count += 1
        elif record.get("reason_when_unavailable"):
            promotion_reason_codes.append(str(record["reason_when_unavailable"]))

        facts.append(
            TrustedFact(
                domain=TrustedDomain.PROMOTIONS,
                key=f"promotion:{record['promotion_id']}",
                value=record,
                source=TruthSource.PROMOTION_TABLE,
                path=f"promotion_table.id={record['promotion_id']}",
            )
        )

    if not promotions:
        missing_promo = {
            "domain": TrustedDomain.PROMOTIONS.value,
            "tenant_id": int(tenant_id),
            "eligible": None,
            "verified": False,
            "reason_when_unavailable": "no_promotion_data",
            "observed_at": observed_at,
            "freshness": "live_read",
            "actionability": "shadow_read_only",
        }
        facts.append(
            TrustedFact(
                domain=TrustedDomain.PROMOTIONS,
                key="promotion:unavailable",
                value=missing_promo,
                source=TruthSource.PROMOTION_TABLE,
                path="promotion_table.empty",
            )
        )
        promotion_reason_codes.append("no_promotion_data")

    duration_ms = int((time.perf_counter() - started) * 1000)
    observability = {
        "snapshot_domains_loaded": [TrustedDomain.COUPONS.value, TrustedDomain.PROMOTIONS.value],
        "coupon_count": len(coupons),
        "eligible_coupon_count": eligible_coupon_count,
        "unavailable_coupon_reason_codes": sorted(set(coupon_reason_codes)),
        "promotion_count": len(promotions),
        "eligible_promotion_count": eligible_promotion_count,
        "unavailable_promotion_reason_codes": sorted(set(promotion_reason_codes)),
        "source_tenant_id": int(tenant_id),
        "loader_duration_ms": duration_ms,
        "freshness_status": "live_read",
    }
    return facts, observability


__all__ = [
    "build_coupon_eligibility_record",
    "build_promotion_eligibility_record",
    "load_coupon_promotion_facts",
    "mask_coupon_code",
    "should_load_coupon_promotion_facts",
]
