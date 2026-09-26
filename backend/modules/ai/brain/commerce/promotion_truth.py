"""Tenant-scoped shareable promotion/coupon truth for Brain compose.

Coupons and offers are structured commerce facts. This resolver never
invents codes and never materialises a new coupon from a generation
rule merely because a customer asked. Integration may change the data
source (native / Salla / imported); the semantic contract does not.
"""
from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from services.coupon_sync_visibility import is_dashboard_authored_coupon

logger = logging.getLogger("nahla.brain.promotion_truth")


_MAX_SHAREABLE = 8
# A read with ``read_first`` pages through the rows the caller would accept,
# newest first, until it holds ``limit`` codes the caller keeps or has read them
# all. The cap bounds one call's work; a read it cuts short says so
# (``coupon_read_complete=False``) instead of passing for a store with nothing.
_READ_FIRST_PAGE = 100
_READ_FIRST_SCAN_CAP = 1000
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
    # ``read_first`` only: False when the scan cap ended the read before it held
    # ``limit`` accepted codes or had read every row in scope. None otherwise.
    coupon_read_complete: Optional[bool] = None


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


# Metadata keys under which a code is issued to ONE customer (the promotion
# engine's personal codes and the generator's customer assignments). Such a
# code is that customer's, never a store-wide offer: it is shareable only in a
# conversation with that customer.
_CUSTOMER_BINDING_KEYS = ("customer_id", "assigned_customer_id", "owner_customer_id")


def _row_customer_binding(row: Any, meta: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """The customer id a coupon row is bound to, or ``None`` for a store-wide code."""
    meta = meta if meta is not None else _meta_dict(row)
    for key in _CUSTOMER_BINDING_KEYS:
        value = meta.get(key)
        if value in (None, "", 0, "0"):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1          # bound to someone, unreadably: never shareable
    return None


_PERCENT_TYPES = frozenset({"percentage", "percent", "pct"})
_FIXED_TYPES = frozenset({"fixed", "amount", "fixed_amount", "money"})


def _plain_number(raw: Any) -> str:
    """``"5"`` for 5, 5.0 or "5.00"; ``"12.5"`` for 12.5; ``""`` when not a number."""
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if not text:
        return ""
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return ""
    if not number.is_finite():
        return ""
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


def _money_mapping(text: str) -> Optional[Dict[str, Any]]:
    """The mapping a stored money value was written as, or ``None``.

    A reconcile once wrote Salla's money object into a text column, so the
    value arrives as that object's Python or JSON rendering. Both are parsed
    as literals — never evaluated — and anything that is not a mapping reads
    as nothing.
    """
    candidate = text.strip()
    if not candidate.startswith("{"):
        return None
    for parse in (ast.literal_eval, json.loads):
        try:
            parsed = parse(candidate)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _discount_number(value: Any) -> Tuple[str, str]:
    """``(number, currency)`` read out of a stored discount value.

    The value may be a number, a numeric string, Salla's money object
    ``{"amount": 5, "currency": "SAR"}``, or the string that object was
    written as. Anything else reads as ``("", "")``.
    """
    if isinstance(value, bool):
        return "", ""
    if isinstance(value, (int, float, Decimal)):
        return _plain_number(value), ""
    if isinstance(value, dict):
        number, _ = _discount_number(value.get("amount", value.get("value")))
        return number, str(value.get("currency") or "").strip().upper()
    text = str(value if value is not None else "").strip()
    if not text:
        return "", ""
    number = _plain_number(text)
    if number:
        return number, ""
    mapping = _money_mapping(text)
    if mapping is None:
        return "", ""
    return _discount_number(mapping)


def _normalised_discount(discount_type: Any, value: Any, meta: Dict[str, Any]) -> Tuple[str, str]:
    """``(discount_value, discount)``: the number the record supports and the one
    reading of it — ``"5%"`` for a percentage, ``"20 SAR"`` for a fixed amount.

    Tenant 1, September 2026: a coupon issued as 5% and reconciled from Salla
    carried ``discount_type="percentage"`` beside ``discount_value="{'amount':
    5, 'currency': 'SAR'}"``; handed both, the model told the customer "5 SAR".
    Here the type decides the reading, a percentage whose value is unreadable
    falls back to the generator's ``discount_pct``, and nothing readable
    yields ``("", "")`` rather than a guess.
    """
    kind = str(discount_type or "").strip().lower()
    number, currency = _discount_number(value)
    if kind in _PERCENT_TYPES:
        if not number:
            number, _ = _discount_number(meta.get("discount_pct"))
        return number, (f"{number}%" if number else "")
    if kind in _FIXED_TYPES:
        return number, (f"{number} {currency}".strip() if number else "")
    return number, number


def _row_to_coupon_fact(row: Any) -> Dict[str, Any]:
    expires = getattr(row, "expires_at", None)
    conditions = _conditions_from_row(row)
    source_type = str(getattr(row, "source_type", "") or "manual")
    binding = _row_customer_binding(row)
    discount_type = str(getattr(row, "discount_type", "") or "")
    discount_value, discount = _normalised_discount(
        discount_type, getattr(row, "discount_value", None), _meta_dict(row))
    return {
        "id": getattr(row, "id", None),
        "code": str(getattr(row, "code", "") or ""),
        "discount_type": discount_type,
        "discount_value": discount_value,
        "discount": discount,
        "description": str(getattr(row, "description", "") or ""),
        "expires_at": expires.isoformat() if hasattr(expires, "isoformat") else (str(expires) if expires else ""),
        "source_type": source_type,
        # The merchant's own creation act, as the create endpoint recorded it.
        # A reader deciding whether a code was published for everyone needs the
        # act, not the absence of a rung — see the promotions tool's gate.
        "merchant_authored": is_dashboard_authored_coupon(source_type, _meta_dict(row)),
        "allocation_channel": str(getattr(row, "allocation_channel", "") or ""),
        "coupon_level": str(getattr(row, "coupon_level", "") or "").lower(),
        "conditions": conditions,
        "customer_bound": binding is not None,
        "bound_customer_id": binding,
        "eligibility_determined": False,
        "eligibility_note": "conditions_not_fully_evaluated",
        "record_kind": "coupon",
    }


def _offer_to_fact(row: Any) -> Dict[str, Any]:
    ends = getattr(row, "ends_at", None)
    conditions = getattr(row, "conditions", None)
    if not isinstance(conditions, dict):
        conditions = {}
    promotion_type = str(getattr(row, "promotion_type", "") or "")
    discount_value, discount = _normalised_discount(
        promotion_type, getattr(row, "discount_value", None), _meta_dict(row))
    return {
        "id": getattr(row, "id", None),
        "name": str(getattr(row, "name", "") or ""),
        "description": str(getattr(row, "description", "") or ""),
        "promotion_type": promotion_type,
        "discount_value": discount_value,
        "discount": discount,
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


@dataclass(frozen=True)
class CouponReadScope:
    """The coupons a caller will accept, to be read before any other.

    ``valid_until_at_least``: a code expiring earlier is not acceptable.
    ``refused_levels``: rungs the caller always refuses; a code on one of them
    is read only after everything else. A rung not named here stays in scope.
    ``untiered_source_types``: when given, a code on no rung is in scope only
    if its ``source_type`` (``manual`` when blank, as ``_row_to_coupon_fact``
    reads it) is one of these; ``None`` keeps every code on no rung in scope.

    Every narrowing here must only move rows the caller would refuse anyway:
    a row the caller could accept that falls outside the scope is read late or
    not at all.
    """

    valid_until_at_least: datetime
    refused_levels: Tuple[str, ...] = ()
    untiered_source_types: Optional[Tuple[str, ...]] = None


# What Python's ``str.strip()`` removes from a rung in the caller's judgement,
# as far as SQL ``btrim`` can be told: a rung that differs only by such
# padding is compared as the rung it is.
_PAD = " \t\n\r\x0b\x0c"


def _read_first_clauses(coupon: Any, now: datetime, audience: Optional[int],
                        scope: CouponReadScope) -> Tuple[List[Any], Any]:
    """``(filters, wanted)`` for a read that puts ``scope`` first.

    The filters drop only rows the row-by-row checks below refuse anyway — an
    expired code, a campaign-only one, a code bound (by ``customer_id``, the
    first binding key read) to another customer as a plain positive integer —
    so nothing the unscoped read could return is lost. ``wanted`` orders the
    rows the caller would accept ahead of the rest; the rest still follow, so
    a caller that counts why rows were refused still meets them when there is
    room.
    """
    from sqlalchemy import and_, func, or_, true  # noqa: PLC0415

    def naive(moment: datetime) -> datetime:
        # ``expires_at`` is stored without a zone and read as UTC (``_as_utc``).
        return moment.astimezone(timezone.utc).replace(tzinfo=None)

    channel = func.lower(func.trim(func.coalesce(coupon.allocation_channel, "")))
    bound = func.coalesce(coupon.extra_metadata[_CUSTOMER_BINDING_KEYS[0]].astext, "")
    another_customers = bound.op("~")("^[1-9][0-9]*$")
    if audience is not None:
        another_customers = and_(another_customers, bound != str(int(audience)))
    filters = [
        or_(coupon.expires_at.is_(None), coupon.expires_at >= naive(now)),
        channel.notin_(sorted(_CAMPAIGN_ONLY_CHANNELS)),
        ~another_customers,
    ]
    edge = max(_as_utc(scope.valid_until_at_least) or now, now)
    lasts = or_(coupon.expires_at.is_(None), coupon.expires_at >= naive(edge))
    rung = func.lower(func.btrim(func.coalesce(coupon.coupon_level, ""), _PAD))
    refused = sorted({str(level or "").strip().lower() for level in scope.refused_levels} - {""})
    on_a_kept_rung = rung.notin_(refused) if refused else true()
    if scope.untiered_source_types is None:
        untiered_ok = true()
    else:
        raw_source = func.coalesce(coupon.source_type, "")
        source = func.lower(func.btrim(func.coalesce(func.nullif(raw_source, ""), "manual"), _PAD))
        untiered_ok = or_(
            rung != "",
            source.in_(sorted({str(t or "").strip().lower() for t in scope.untiered_source_types})),
            # Anything but printable ASCII may be padding Python strips and SQL
            # cannot: such a row stays in scope rather than be judged here.
            raw_source.op("~")("[^ -~]"))
    return filters, and_(lasts, on_a_kept_rung, untiered_ok)


def _coupon_facts(rows: Iterable[Any], now: datetime, audience: Optional[int],
                  tid: int) -> Iterator[Dict[str, Any]]:
    """The shareable fact of each row, in order: valid now, not another
    customer's personal code, carrying a code. A malformed row is skipped."""
    for row in rows:
        try:
            if not _row_is_currently_valid(row, now=now):
                continue
            binding = _row_customer_binding(row)
            if binding is not None and (audience is None or binding != audience):
                continue          # someone else's personal code: never shareable here
            fact = _row_to_coupon_fact(row)
            if not fact["code"]:
                continue
        except Exception:  # noqa: silent-ok — skip malformed coupon row; other sources still queried
            logger.info(
                "[PROMOTION_TRUTH] tenant=%s source=coupon skipped_malformed_row",
                tid,
            )
            continue
        yield fact


def _read_first_coupons(db: Any, coupon: Any, tid: int, now: datetime, audience: Optional[int],
                        scope: CouponReadScope, accept: Optional[Callable[[Dict[str, Any]], bool]],
                        limit: int) -> Tuple[List[Any], List[Dict[str, Any]], bool]:
    """``(rows_read, facts, complete)`` for a read that puts ``scope`` first.

    In-scope rows are read a page at a time, newest first (keyset on ``id``),
    until ``limit`` facts were accepted, the scope ran out, or the scan cap was
    reached; only the last is incomplete. Every fact read is kept, accepted or
    not. When the scope ran out with room left, the newest out-of-scope facts
    fill it, so a caller counting refusals still meets them.
    """
    from sqlalchemy import not_  # noqa: PLC0415
    from sqlalchemy.orm import selectinload  # noqa: PLC0415

    keeps = accept if accept is not None else (lambda _fact: True)
    filters, wanted = _read_first_clauses(coupon, now, audience, scope)
    # Each fact reads the coupon's rules; loaded with the page, not per row.
    in_scope_ids = db.query(coupon.id).filter(coupon.tenant_id == tid, *filters)
    base = (db.query(coupon).options(selectinload(coupon.rules))
            .filter(coupon.tenant_id == tid, *filters))
    rows: List[Any] = []
    facts: List[Dict[str, Any]] = []
    accepted = 0
    exhausted = False
    last_id: Optional[int] = None
    while accepted < limit and len(rows) < _READ_FIRST_SCAN_CAP:
        page = base.filter(wanted)
        if last_id is not None:
            page = page.filter(coupon.id < last_id)
        size = min(_READ_FIRST_PAGE, _READ_FIRST_SCAN_CAP - len(rows))
        batch = page.order_by(coupon.id.desc()).limit(size).all()
        rows.extend(batch)
        for fact in _coupon_facts(batch, now, audience, tid):
            facts.append(fact)
            if keeps(fact):
                accepted += 1
                if accepted >= limit:
                    break
        if len(batch) < size:
            exhausted = True
            break
        last_id = int(batch[-1].id)
    if not exhausted and accepted < limit and last_id is not None:
        # The cap was reached exactly at a page boundary: the scope is read to
        # its end only if nothing in it is older than the last row read.
        exhausted = in_scope_ids.filter(wanted, coupon.id < last_id).first() is None
    complete = accepted >= limit or exhausted
    room = limit - len(facts)
    if exhausted and room > 0:
        rest = (base.filter(not_(wanted)).order_by(coupon.id.desc())
                .limit(max(limit * 3, limit)).all())
        rows.extend(rest)
        for fact in _coupon_facts(rest, now, audience, tid):
            facts.append(fact)
            room -= 1
            if room <= 0:
                break
    return rows, facts, complete


def resolve_shareable_promotions(
    db: Any,
    tenant_id: int,
    *,
    now: Optional[datetime] = None,
    limit: int = _MAX_SHAREABLE,
    customer_id: Optional[int] = None,
    read_first: Optional[CouponReadScope] = None,
    accept: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> PromotionTruthResult:
    """Load currently valid shareable coupons/offers for one tenant at query time.

    A coupon issued to one customer (a personal code) is shareable only when
    ``customer_id`` names that customer; without a customer, or for any other
    customer, it is left out. Store-wide codes are unaffected.

    Unscoped, the coupon read looks at the tenant's newest rows and judges them
    after the cut: a store whose newest rows are expired, sit on a rung the
    caller refuses, or belong to other customers can hide every code the caller
    would accept. ``read_first`` names what the caller accepts, and ``accept``
    is the caller's own judgement of one fact. The read then leaves out expired
    and campaign-only rows, pages through the in-scope rows newest first, and
    stops only when ``limit`` facts were accepted, every in-scope row was read,
    or the scan cap was reached — the last reported as an incomplete read.
    Every fact read is returned, accepted or not, so the caller can say why it
    refused one; when room is left, out-of-scope facts follow for the same
    reason. Each row is still judged exactly as without a scope.
    """
    tid = int(tenant_id or 0)
    audience = int(customer_id) if customer_id not in (None, "", 0) else None
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
    shareable: List[Dict[str, Any]] = []
    coupon_read_complete: Optional[bool] = None
    try:
        from models import Coupon  # noqa: PLC0415

        if read_first is None:
            rows = (
                db.query(Coupon)
                .filter(Coupon.tenant_id == tid)
                .order_by(Coupon.id.desc())
                .limit(max(int(limit) * 3, int(limit)))
                .all()
            )
            for fact in _coupon_facts(rows, now_, audience, tid):
                shareable.append(fact)
                if len(shareable) >= int(limit):
                    break
        else:
            rows, shareable, coupon_read_complete = _read_first_coupons(
                db, Coupon, tid, now_, audience, read_first, accept, int(limit))
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
        coupon_read_complete=coupon_read_complete,
    )


__all__ = [
    "coupon_policy_for_compose",
    "CouponReadScope",
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
