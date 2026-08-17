"""Tenant-scoped shareable promotion/coupon truth for Brain compose.

Coupons and offers are structured commerce facts. This resolver never
invents codes and never materialises a new coupon from a generation
rule merely because a customer asked. Integration may change the data
source (native / Salla / imported); the semantic contract does not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.promotion_truth")


_MAX_SHAREABLE = 8
_CAMPAIGN_ONLY_CHANNELS = frozenset({"campaign", "email", "sms", "autopilot"})

QUERY_OK = "ok"
NO_VALID_PROMOTIONS = "NO_VALID_PROMOTIONS"
PROMOTION_QUERY_FAILED = "PROMOTION_QUERY_FAILED"


@dataclass(frozen=True)
class PromotionTruthResult:
    tenant_id: int
    query_run: bool
    candidate_count: int
    shareable: List[Dict[str, Any]] = field(default_factory=list)
    offers: List[Dict[str, Any]] = field(default_factory=list)
    generation_rules_present: bool = False
    generation_authorized: bool = False
    invented_codes: bool = False
    source: str = "native_coupons"
    query_failed: bool = False
    query_outcome: str = NO_VALID_PROMOTIONS


def _as_utc(dt: Any) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _meta_dict(row: Any) -> Dict[str, Any]:
    meta = getattr(row, "extra_metadata", None) or getattr(row, "metadata", None) or {}
    return dict(meta) if isinstance(meta, dict) else {}


def _conditions_from_row(row: Any) -> Dict[str, Any]:
    meta = _meta_dict(row)
    conditions: Dict[str, Any] = {}
    for src_key, out_key in (
        ("min_order_amount", "min_order_amount"),
        ("minimum_basket", "min_order_amount"),
        ("usage_limit", "usage_limit"),
        ("max_uses", "usage_limit"),
        ("per_customer_limit", "per_customer_limit"),
        ("customer_limit", "per_customer_limit"),
    ):
        if meta.get(src_key) not in (None, "") and out_key not in conditions:
            conditions[out_key] = meta.get(src_key)
    product_ids = meta.get("product_ids") or meta.get("applicable_products")
    if product_ids:
        conditions["product_ids"] = list(product_ids)
    category_ids = meta.get("category_ids") or meta.get("applicable_categories")
    if category_ids:
        conditions["category_ids"] = list(category_ids)
    rules = getattr(row, "rules", None) or []
    for rule in rules:
        rule_type = str(getattr(rule, "rule_type", "") or "").strip().lower()
        cfg = getattr(rule, "rule_config", None) or {}
        if not isinstance(cfg, dict):
            continue
        if rule_type in {"product", "products"}:
            ids = cfg.get("product_ids") or cfg.get("ids") or []
            if ids:
                conditions.setdefault("product_ids", [])
                conditions["product_ids"] = list(
                    dict.fromkeys([*conditions["product_ids"], *ids])
                )
        if rule_type in {"category", "categories"}:
            ids = cfg.get("category_ids") or cfg.get("ids") or []
            if ids:
                conditions.setdefault("category_ids", [])
                conditions["category_ids"] = list(
                    dict.fromkeys([*conditions["category_ids"], *ids])
                )
        if rule_type in {"min_order", "minimum_basket", "min_spend"}:
            amount = cfg.get("amount") or cfg.get("min_order_amount")
            if amount not in (None, ""):
                conditions["min_order_amount"] = amount
    return conditions


def _row_is_currently_valid(row: Any, *, now: datetime) -> bool:
    expires = _as_utc(getattr(row, "expires_at", None))
    if expires is not None and expires <= now:
        return False
    meta = _meta_dict(row)
    starts = _as_utc(meta.get("starts_at") or meta.get("start_at") or getattr(row, "starts_at", None))
    if starts is not None and starts > now:
        return False
    status = str(meta.get("status") or meta.get("state") or "").strip().lower()
    if status in {"disabled", "inactive", "expired", "revoked"}:
        return False
    if meta.get("enabled") is False or meta.get("is_active") is False:
        return False
    if meta.get("active") is False or meta.get("disabled") is True:
        return False
    channel = str(getattr(row, "allocation_channel", "") or meta.get("allocation_channel") or "").strip().lower()
    if channel in _CAMPAIGN_ONLY_CHANNELS:
        return False
    return True


def _row_to_coupon_fact(row: Any) -> Dict[str, Any]:
    expires = getattr(row, "expires_at", None)
    conditions = _conditions_from_row(row)
    source_type = str(getattr(row, "source_type", "") or "manual")
    return {
        "id": getattr(row, "id", None),
        "code": str(getattr(row, "code", "") or ""),
        "discount_type": str(getattr(row, "discount_type", "") or ""),
        "discount_value": str(getattr(row, "discount_value", "") or ""),
        "description": str(getattr(row, "description", "") or ""),
        "expires_at": expires.isoformat() if hasattr(expires, "isoformat") else (str(expires) if expires else ""),
        "source_type": source_type,
        "allocation_channel": str(getattr(row, "allocation_channel", "") or ""),
        "conditions": conditions,
        "eligibility_determined": False,
        "eligibility_note": "conditions_not_fully_evaluated",
        "record_kind": "coupon",
    }


def _offer_to_fact(row: Any) -> Dict[str, Any]:
    ends = getattr(row, "ends_at", None)
    conditions = getattr(row, "conditions", None)
    if not isinstance(conditions, dict):
        conditions = {}
    return {
        "id": getattr(row, "id", None),
        "name": str(getattr(row, "name", "") or ""),
        "description": str(getattr(row, "description", "") or ""),
        "promotion_type": str(getattr(row, "promotion_type", "") or ""),
        "discount_value": str(getattr(row, "discount_value", "") or ""),
        "ends_at": ends.isoformat() if hasattr(ends, "isoformat") else (str(ends) if ends else ""),
        "conditions": conditions,
        "code": "",
        "eligibility_determined": False,
        "eligibility_note": "offer_terms_only_no_code_invented",
        "record_kind": "offer",
        "source_type": "promotion_rule",
    }


def resolve_shareable_promotions(
    db: Any,
    tenant_id: int,
    *,
    now: Optional[datetime] = None,
    limit: int = _MAX_SHAREABLE,
) -> PromotionTruthResult:
    """Load currently valid shareable coupons/offers for one tenant at query time."""
    tid = int(tenant_id or 0)
    if db is None or tid <= 0:
        return PromotionTruthResult(
            tenant_id=tid,
            query_run=False,
            candidate_count=0,
            query_failed=False,
            query_outcome=NO_VALID_PROMOTIONS,
        )
    now_ = now or datetime.now(timezone.utc)
    if now_.tzinfo is None:
        now_ = now_.replace(tzinfo=timezone.utc)

    rows: List[Any] = []
    try:
        from models import Coupon  # noqa: PLC0415

        rows = (
            db.query(Coupon)
            .filter(Coupon.tenant_id == tid)
            .order_by(Coupon.id.desc())
            .limit(max(int(limit) * 3, int(limit)))
            .all()
        )
    except Exception:  # noqa: silent-ok — coupon query fail-open; Brain still answers without promotions
        logger.info(
            "[PROMOTION_TRUTH] tenant=%s outcome=%s",
            tid,
            PROMOTION_QUERY_FAILED,
        )
        return PromotionTruthResult(
            tenant_id=tid,
            query_run=True,
            candidate_count=0,
            query_failed=True,
            query_outcome=PROMOTION_QUERY_FAILED,
        )

    shareable: List[Dict[str, Any]] = []
    for row in rows:
        if not _row_is_currently_valid(row, now=now_):
            continue
        fact = _row_to_coupon_fact(row)
        if not fact["code"]:
            continue
        shareable.append(fact)
        if len(shareable) >= int(limit):
            break

    offers: List[Dict[str, Any]] = []
    generation_rules_present = False
    offers_query_failed = False
    try:
        from models import Promotion  # noqa: PLC0415
        from services.promotion_engine import is_promotion_active  # noqa: PLC0415

        promo_rows = (
            db.query(Promotion)
            .filter(Promotion.tenant_id == tid)
            .order_by(Promotion.id.desc())
            .limit(max(int(limit) * 2, int(limit)))
            .all()
        )
        generation_rules_present = any(
            str(getattr(p, "status", "") or "").strip().lower()
            in {"active", "scheduled", "draft"}
            for p in promo_rows
        )
        for promo in promo_rows:
            if not is_promotion_active(promo, now=now_):
                continue
            offers.append(_offer_to_fact(promo))
            if len(offers) >= int(limit):
                break
    except Exception:  # noqa: silent-ok — offer query fail-open for customer; diagnostics must not look like an empty catalog
        offers_query_failed = True
        logger.info(
            "[PROMOTION_TRUTH] tenant=%s outcome=%s source=offers",
            tid,
            PROMOTION_QUERY_FAILED,
        )

    if not generation_rules_present:
        try:
            from models import CouponRule  # noqa: PLC0415

            if shareable:
                ids = [int(item["id"]) for item in shareable if item.get("id")]
                if ids:
                    generation_rules_present = (
                        db.query(CouponRule.id)
                        .filter(CouponRule.coupon_id.in_(ids))
                        .first()
                        is not None
                    )
        except Exception:  # noqa: silent-ok — coupon-rule probe fail-open; generation_authorized stays false
            logger.info(
                "[PROMOTION_TRUTH] tenant=%s outcome=%s source=coupon_rules",
                tid,
                PROMOTION_QUERY_FAILED,
            )
            generation_rules_present = False

    if shareable or offers:
        outcome = QUERY_OK
        query_failed = False
    elif offers_query_failed:
        outcome = PROMOTION_QUERY_FAILED
        query_failed = True
    else:
        outcome = NO_VALID_PROMOTIONS
        query_failed = False
    logger.info(
        "[PROMOTION_TRUTH] tenant=%s outcome=%s candidate_count=%s shareable=%s offers=%s offers_query_failed=%s",
        tid,
        outcome,
        len(rows),
        len(shareable),
        len(offers),
        int(offers_query_failed),
    )
    return PromotionTruthResult(
        tenant_id=tid,
        query_run=True,
        candidate_count=len(rows),
        shareable=shareable,
        offers=offers,
        generation_rules_present=bool(generation_rules_present),
        generation_authorized=False,
        invented_codes=False,
        source="native_coupons",
        query_failed=query_failed,
        query_outcome=outcome,
    )


__all__ = [
    "NO_VALID_PROMOTIONS",
    "PROMOTION_QUERY_FAILED",
    "QUERY_OK",
    "PromotionTruthResult",
    "resolve_shareable_promotions",
]
