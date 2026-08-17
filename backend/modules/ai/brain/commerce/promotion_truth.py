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
PROMOTION_PARTIAL_FAILURE = "PROMOTION_PARTIAL_FAILURE"

SOURCE_OK = "ok"
SOURCE_FAILED = "failed"
SOURCE_NOT_QUERIED = "not_queried"

GENERATION_PRESENT = "present"
GENERATION_ABSENT = "absent"
GENERATION_FAILED = "failed"
GENERATION_NOT_QUERIED = "not_queried"


@dataclass(frozen=True)
class PromotionTruthResult:
    tenant_id: int
    query_run: bool
    candidate_count: int
    shareable: List[Dict[str, Any]] = field(default_factory=list)
    offers: List[Dict[str, Any]] = field(default_factory=list)
    generation_rules_present: Optional[bool] = None
    generation_rules_state: str = GENERATION_NOT_QUERIED
    generation_authorized: bool = False
    invented_codes: bool = False
    source: str = "native_coupons"
    query_failed: bool = False
    query_outcome: str = NO_VALID_PROMOTIONS
    coupon_source: str = SOURCE_NOT_QUERIED
    offer_source: str = SOURCE_NOT_QUERIED
    generation_rule_source: str = SOURCE_NOT_QUERIED


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


def _as_id_list(value: Any) -> List[Any]:
    """Project JSON id fields without treating scalars as iterables."""
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item not in (None, "")]
    if isinstance(value, dict):
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    return []


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
    product_ids = _as_id_list(
        meta.get("product_ids") or meta.get("applicable_products")
    )
    if product_ids:
        conditions["product_ids"] = product_ids
    category_ids = _as_id_list(
        meta.get("category_ids") or meta.get("applicable_categories")
    )
    if category_ids:
        conditions["category_ids"] = category_ids
    rules = getattr(row, "rules", None) or []
    for rule in rules:
        rule_type = str(getattr(rule, "rule_type", "") or "").strip().lower()
        cfg = getattr(rule, "rule_config", None) or {}
        if not isinstance(cfg, dict):
            continue
        if rule_type in {"product", "products"}:
            ids = _as_id_list(cfg.get("product_ids") or cfg.get("ids"))
            if ids:
                conditions.setdefault("product_ids", [])
                conditions["product_ids"] = list(
                    dict.fromkeys([*conditions["product_ids"], *ids])
                )
        if rule_type in {"category", "categories"}:
            ids = _as_id_list(cfg.get("category_ids") or cfg.get("ids"))
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
    if _row_is_globally_exhausted(row, meta):
        return False
    return True


def _as_nonneg_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _row_is_globally_exhausted(row: Any, meta: Optional[Dict[str, Any]] = None) -> bool:
    """Exclude only when authoritative global usage evidence proves exhaustion.

    Per-customer limits without a current-customer counter stay unknown.
    """
    data = meta if isinstance(meta, dict) else _meta_dict(row)
    usage_count = _as_nonneg_int(
        data.get("usage_count")
        if data.get("usage_count") not in (None, "")
        else getattr(row, "usage_count", None)
    )
    usage_limit = _as_nonneg_int(
        data.get("usage_limit")
        if data.get("usage_limit") not in (None, "")
        else data.get("max_uses")
        if data.get("max_uses") not in (None, "")
        else getattr(row, "usage_limit", None)
    )
    used_flag = data.get("used")
    if used_flag is True and usage_limit == 1:
        return True
    if used_flag is True and usage_count is None:
        usage_count = 1
    if usage_count is None:
        usage_count = 0
    return bool(usage_limit is not None and usage_limit > 0 and usage_count >= usage_limit)


def _session_is_poisoned(exc: BaseException) -> bool:
    """True when further queries on this session would be unsafe."""
    name = type(exc).__name__
    if name in {"PendingRollbackError", "InternalError"}:
        return True
    text = str(exc).lower()
    if "current transaction is aborted" in text:
        return True
    if "infailedsqltransaction" in text:
        return True
    if "can't reconnect until invalid" in text:
        return True
    return False


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


def coupon_policy_for_compose(
    facts: Any,
    *,
    discount_ok_now: bool = False,
    coupon_logic_considered: bool = False,
) -> Dict[str, Any]:
    """Structured coupon truth for compose. Never customer error text."""
    outcome = str(getattr(facts, "promotion_query_outcome", "") or "")
    query_failed = bool(getattr(facts, "promotion_query_failed", False))
    return {
        "has_coupons": bool(getattr(facts, "has_coupons", False)),
        "eligible_code": getattr(facts, "coupon_eligibility", "") or "",
        "shareable_promotions": list(
            getattr(facts, "shareable_promotions", None) or []
        )[:8],
        "shareable_offers": list(
            getattr(facts, "shareable_offers", None) or []
        )[:8],
        "eligibility_guaranteed": False,
        "discount_ok_now": bool(discount_ok_now),
        "coupon_logic_considered": bool(coupon_logic_considered),
        "query_outcome": outcome,
        "query_failed": query_failed,
        "coupon_source": str(getattr(facts, "promotion_coupon_source", "") or ""),
        "offer_source": str(getattr(facts, "promotion_offer_source", "") or ""),
        "generation_rule_source": str(
            getattr(facts, "promotion_generation_rule_source", "") or ""
        ),
        "generation_rules_state": str(
            getattr(facts, "generation_rules_state", "") or ""
        ),
        "generation_authorized": False,
        "invented_codes": False,
        "no_valid_promotions": (
            outcome == NO_VALID_PROMOTIONS and not query_failed
        ),
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
            coupon_source=SOURCE_NOT_QUERIED,
            offer_source=SOURCE_NOT_QUERIED,
            generation_rule_source=SOURCE_NOT_QUERIED,
            generation_rules_state=GENERATION_NOT_QUERIED,
        )
    now_ = now or datetime.now(timezone.utc)
    if now_.tzinfo is None:
        now_ = now_.replace(tzinfo=timezone.utc)

    session_poisoned = False
    coupon_source = SOURCE_NOT_QUERIED
    offer_source = SOURCE_NOT_QUERIED
    generation_rule_source = SOURCE_NOT_QUERIED
    generation_rules_state = GENERATION_NOT_QUERIED
    generation_rules_present: Optional[bool] = None

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
        coupon_source = SOURCE_OK
    except Exception as exc:  # noqa: silent-ok — coupon source fail-open; other sources still queried unless session is poisoned
        coupon_source = SOURCE_FAILED
        session_poisoned = _session_is_poisoned(exc)
        logger.info(
            "[PROMOTION_TRUTH] tenant=%s source=coupon outcome=%s poisoned=%s",
            tid,
            PROMOTION_QUERY_FAILED,
            int(session_poisoned),
        )

    shareable: List[Dict[str, Any]] = []
    for row in rows:
        try:
            if not _row_is_currently_valid(row, now=now_):
                continue
            fact = _row_to_coupon_fact(row)
            if not fact["code"]:
                continue
            shareable.append(fact)
            if len(shareable) >= int(limit):
                break
        except Exception:  # noqa: silent-ok — skip malformed coupon row; other sources still queried
            logger.info(
                "[PROMOTION_TRUTH] tenant=%s source=coupon skipped_malformed_row",
                tid,
            )

    offers: List[Dict[str, Any]] = []
    if session_poisoned:
        offer_source = SOURCE_NOT_QUERIED
        generation_rule_source = SOURCE_NOT_QUERIED
        generation_rules_state = GENERATION_NOT_QUERIED
    else:
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
            offer_source = SOURCE_OK
            for promo in promo_rows:
                try:
                    if not is_promotion_active(promo, now=now_):
                        continue
                    offers.append(_offer_to_fact(promo))
                    if len(offers) >= int(limit):
                        break
                except Exception:  # noqa: silent-ok — skip malformed offer row
                    logger.info(
                        "[PROMOTION_TRUTH] tenant=%s source=offers skipped_malformed_row",
                        tid,
                    )
        except Exception as exc:  # noqa: silent-ok — offer source fail-open; verified coupons remain
            offer_source = SOURCE_FAILED
            session_poisoned = session_poisoned or _session_is_poisoned(exc)
            logger.info(
                "[PROMOTION_TRUTH] tenant=%s source=offers outcome=%s poisoned=%s",
                tid,
                PROMOTION_QUERY_FAILED,
                int(session_poisoned),
            )

        if session_poisoned:
            generation_rule_source = SOURCE_NOT_QUERIED
            generation_rules_state = GENERATION_NOT_QUERIED
        else:
            try:
                from models import Coupon, CouponRule  # noqa: PLC0415

                generation_rules_present = (
                    db.query(CouponRule.id)
                    .join(Coupon, CouponRule.coupon_id == Coupon.id)
                    .filter(Coupon.tenant_id == tid)
                    .first()
                    is not None
                )
                generation_rule_source = SOURCE_OK
                generation_rules_state = (
                    GENERATION_PRESENT if generation_rules_present else GENERATION_ABSENT
                )
            except Exception as exc:  # noqa: silent-ok — generation lookup failure is UNKNOWN, not absent
                generation_rule_source = SOURCE_FAILED
                generation_rules_state = GENERATION_FAILED
                generation_rules_present = None
                logger.info(
                    "[PROMOTION_TRUTH] tenant=%s source=coupon_rules outcome=%s poisoned=%s",
                    tid,
                    PROMOTION_QUERY_FAILED,
                    int(_session_is_poisoned(exc)),
                )

    any_source_failed = SOURCE_FAILED in {
        coupon_source, offer_source, generation_rule_source,
    }
    has_verified = bool(shareable or offers)
    if has_verified and not any_source_failed:
        outcome = QUERY_OK
    elif has_verified and any_source_failed:
        outcome = PROMOTION_PARTIAL_FAILURE
    elif any_source_failed:
        outcome = PROMOTION_QUERY_FAILED
    else:
        outcome = NO_VALID_PROMOTIONS
    logger.info(
        "[PROMOTION_TRUTH] tenant=%s outcome=%s coupon=%s offer=%s gen=%s "
        "candidate_count=%s shareable=%s offers=%s gen_state=%s",
        tid,
        outcome,
        coupon_source,
        offer_source,
        generation_rule_source,
        len(rows),
        len(shareable),
        len(offers),
        generation_rules_state,
    )
    return PromotionTruthResult(
        tenant_id=tid,
        query_run=True,
        candidate_count=len(rows),
        shareable=shareable,
        offers=offers,
        generation_rules_present=generation_rules_present,
        generation_rules_state=generation_rules_state,
        generation_authorized=False,
        invented_codes=False,
        source="native_coupons",
        query_failed=any_source_failed,
        query_outcome=outcome,
        coupon_source=coupon_source,
        offer_source=offer_source,
        generation_rule_source=generation_rule_source,
    )


__all__ = [
    "coupon_policy_for_compose",
    "GENERATION_ABSENT",
    "GENERATION_FAILED",
    "GENERATION_NOT_QUERIED",
    "GENERATION_PRESENT",
    "NO_VALID_PROMOTIONS",
    "PROMOTION_PARTIAL_FAILURE",
    "PROMOTION_QUERY_FAILED",
    "QUERY_OK",
    "SOURCE_FAILED",
    "SOURCE_NOT_QUERIED",
    "SOURCE_OK",
    "PromotionTruthResult",
    "resolve_shareable_promotions",
]
