"""Customer-request coupon service — additive issuance, existing segment APIs unchanged."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Coupon, Customer, Order, Tenant, TenantSettings
from backend.routers.coupons import DEFAULT_COUPON_LEVELS, _normalise_levels
from backend.services.coupon_generator import (
    CouponGeneratorService,
    _segment_to_level,
)
from backend.services.customer_intelligence import normalize_phone
from backend.services import customer_request_coupon_service as coupon_request_mod
from backend.services.customer_request_coupon_service import (
    COUNT_SOURCE_CI_PHONE_INDEX,
    CUSTOMER_COUPON_LIVE_ISSUANCE,
    CUSTOMER_COUPON_LIVE_ROUTING,
    ISSUED_REASON_CUSTOMER_REQUEST,
    REASON_AI_POLICY_DISABLED,
    REASON_IDENTITY_UNAVAILABLE,
    REASON_LEVEL_NOT_ALLOWED_FOR_AI,
    REASON_LIVE_ISSUANCE_DISABLED,
    REASON_NO_LEVEL,
    REASON_POOL_EMPTY,
    REASON_SALLA_UNAVAILABLE,
    count_customer_orders,
    find_reusable_assigned_coupon,
    issue_customer_coupon,
)


PHONE_A = "+966500000001"
PHONE_B = "+966500000002"


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _fake_adapter():
    async def fake_create_coupon(code: str, discount_type: str, discount_value: int, expiry_days: int):
        return {
            "code": code,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat(),
        }

    async def fake_delete(code: str):
        return True

    return SimpleNamespace(create_coupon=fake_create_coupon, delete_coupon_by_code=fake_delete)


def _levels(*, include_ai_for: Optional[tuple[str, ...]] = None) -> list[dict]:
    rows = _normalise_levels(DEFAULT_COUPON_LEVELS)
    if include_ai_for is None:
        return rows
    wanted = set(include_ai_for)
    out = []
    for row in rows:
        item = dict(row)
        channels = list(item.get("allowed_channels") or [])
        if item["id"] in wanted and "ai" not in channels:
            channels.append("ai")
        item["allowed_channels"] = channels
        out.append(item)
    return out


def _make_db(
    *,
    ai_policy: Optional[dict] = None,
    levels: Optional[list] = None,
    first_purchase: bool = False,
    global_defaults: Optional[dict] = None,
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Coupon Request Tenant", is_active=True)
    session.add(tenant)
    session.flush()
    dash: Dict[str, Any] = {
        "levels": levels or _levels(),
        "ai_policy": ai_policy
        or {
            "enabled": True,
            "allowed_levels": ["bronze", "silver"],
            "min_remaining_hours": 3,
            "pool_mode": "pool_first",
        },
        "rules": [{"id": "first_purchase", "enabled": first_purchase}],
        "global_defaults": global_defaults or {"min_order_amount": 0},
    }
    session.add(
        TenantSettings(
            tenant_id=tenant.id,
            ai_settings={"allowed_discount_levels": 40},
            extra_metadata={"coupons_dashboard": dash},
        )
    )
    session.commit()
    return session, tenant.id, engine


def _add_customer(db, tenant_id: int, phone: str, *, name: str = "أحمد سالم") -> Customer:
    row = Customer(
        tenant_id=tenant_id,
        name=name,
        phone=phone,
        normalized_phone=normalize_phone(phone) or phone,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _add_orders(
    db,
    tenant_id: int,
    phone: str,
    *,
    countable: int,
    excluded: int = 0,
    status: str = "delivered",
) -> None:
    for i in range(countable):
        db.add(
            Order(
                tenant_id=tenant_id,
                status=status,
                total="50",
                customer_info={"mobile": phone, "name": "أحمد سالم"},
                is_abandoned=False,
            )
        )
    for i in range(excluded):
        db.add(
            Order(
                tenant_id=tenant_id,
                status="cancelled",
                total="50",
                customer_info={"mobile": phone},
                is_abandoned=False,
            )
        )
    db.commit()


def _pool_meta(*, level: str, used: str = "false", extra: Optional[dict] = None) -> dict:
    segment = {"bronze": "new", "silver": "active", "gold": "vip", "vip": "at_risk"}[level]
    meta = {
        "source": "auto",
        "target_segment": segment,
        "used": used,
        "salla_synced": "true",
        "category": "auto",
        "active": True,
        "coupon_level": level,
    }
    if extra:
        meta.update(extra)
    return meta


def _add_pool_coupon(
    db,
    tenant_id: int,
    code: str,
    level: str,
    *,
    used: str = "false",
    expires_at: Optional[datetime] = None,
    extra: Optional[dict] = None,
) -> Coupon:
    row = Coupon(
        tenant_id=tenant_id,
        code=code,
        discount_type="percentage",
        discount_value="20",
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(days=3)),
        extra_metadata=_pool_meta(level=level, used=used, extra=extra),
        coupon_level=level,
        source_type="system",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _add_native_coupon(
    db,
    tenant_id: int,
    code: str,
    level: str,
    *,
    used: str = "false",
    expires_at: Optional[datetime] = None,
    extra: Optional[dict] = None,
    allocation_channel: str = "ai",
    usage_limit: int = 1,
    ai_allocatable: Any = True,
    source_type: str = "manual",
) -> Coupon:
    meta = {
        "source": "dashboard",
        "used": used,
        "active": True,
        "ai_allocatable": ai_allocatable,
        "coupon_level": level,
        "allocation_channel": allocation_channel,
        "usage_limit": usage_limit,
        "usage_count": 0,
    }
    if extra:
        meta.update(extra)
    row = Coupon(
        tenant_id=tenant_id,
        code=code,
        discount_type="percentage",
        discount_value="6",
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(days=3)),
        extra_metadata=meta,
        coupon_level=level,
        allocation_channel=allocation_channel,
        source_type=source_type,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _stamp_owned(
    db,
    coupon: Coupon,
    customer_id: int,
    *,
    reason: str = ISSUED_REASON_CUSTOMER_REQUEST,
    channel: str = "ai",
) -> Coupon:
    meta = dict(coupon.extra_metadata or {})
    meta["customer_id"] = int(customer_id)
    meta["issued_reason"] = reason
    meta["issued_channel"] = channel
    coupon.extra_metadata = meta
    coupon.allocation_channel = channel
    flag_modified(coupon, "extra_metadata")
    db.commit()
    db.refresh(coupon)
    return coupon


def _issue(db, tenant_id, customer_id, **kwargs):
    kwargs.setdefault("allow_issuance", True)
    return asyncio.run(issue_customer_coupon(db, tenant_id, customer_id, **kwargs))


def test_live_flags_remain_off() -> None:
    assert CUSTOMER_COUPON_LIVE_ROUTING is False
    assert CUSTOMER_COUPON_LIVE_ISSUANCE is False


def test_identity_unavailable() -> None:
    db, tenant_id, _engine = _make_db()
    result = _issue(db, tenant_id, 99999)
    assert result.issued is False
    assert result.reason_code == REASON_IDENTITY_UNAVAILABLE


def test_count_uses_phone_index_not_order_customer_id_link() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=4, excluded=2)
    # Orders remain unlinked (customer_id None) — CI phone index still counts.
    count = count_customer_orders(db, tenant_id, customer.id)
    assert count is not None
    assert count.raw_orders == 6
    assert count.countable_orders == 4
    assert count.excluded_orders == 2
    assert count.count_source == COUNT_SOURCE_CI_PHONE_INDEX
    assert all(row.customer_id is None for row in db.query(Order).all())


def test_zero_orders_no_level_when_first_purchase_disabled() -> None:
    db, tenant_id, _engine = _make_db(first_purchase=False)
    customer = _add_customer(db, tenant_id, PHONE_A)
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.reason_code == REASON_NO_LEVEL
    assert result.countable_orders == 0


def test_live_issuance_disabled_does_not_consume_pool() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    coupon = _add_pool_coupon(db, tenant_id, "NHAAA", "bronze")
    result = asyncio.run(
        issue_customer_coupon(db, tenant_id, customer.id, allow_issuance=False)
    )
    assert result.issued is False
    assert result.reason_code == REASON_LIVE_ISSUANCE_DISABLED
    db.refresh(coupon)
    assert (coupon.extra_metadata or {}).get("used") == "false"
    assert (coupon.extra_metadata or {}).get("customer_id") is None


def test_no_downgrade_when_ai_blocks_gold() -> None:
    db, tenant_id, _engine = _make_db(
        ai_policy={
            "enabled": True,
            "allowed_levels": ["bronze", "silver"],
            "min_remaining_hours": 3,
            "pool_mode": "pool_first",
        }
    )
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=7)
    silver = _add_pool_coupon(db, tenant_id, "NHSLV", "silver")
    gold = _add_pool_coupon(db, tenant_id, "NHGLD", "gold")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.resolved_level == "gold"
    assert result.reason_code == REASON_LEVEL_NOT_ALLOWED_FOR_AI
    db.refresh(silver)
    db.refresh(gold)
    assert (silver.extra_metadata or {}).get("customer_id") is None
    assert (gold.extra_metadata or {}).get("customer_id") is None


def test_ai_policy_disabled() -> None:
    db, tenant_id, _engine = _make_db(
        ai_policy={
            "enabled": False,
            "allowed_levels": ["bronze", "silver", "gold", "vip"],
            "min_remaining_hours": 3,
            "pool_mode": "pool_first",
        },
        levels=_levels(include_ai_for=("bronze", "silver", "gold", "vip")),
    )
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    _add_pool_coupon(db, tenant_id, "NHAAA", "bronze")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.reason_code == REASON_AI_POLICY_DISABLED


def test_gold_channel_block_is_not_silent_silver() -> None:
    levels = _levels()
    db, tenant_id, _engine = _make_db(
        levels=levels,
        ai_policy={
            "enabled": True,
            "allowed_levels": ["bronze", "silver", "gold"],
            "min_remaining_hours": 3,
            "pool_mode": "pool_first",
        },
    )
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=7)
    _add_pool_coupon(db, tenant_id, "NHSLV", "silver")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.resolved_level == "gold"
    assert result.reason_code == REASON_LEVEL_NOT_ALLOWED_FOR_AI


def test_issues_assigned_gold_when_ai_allows() -> None:
    db, tenant_id, _engine = _make_db(
        levels=_levels(include_ai_for=("gold",)),
        ai_policy={
            "enabled": True,
            "allowed_levels": ["bronze", "silver", "gold"],
            "min_remaining_hours": 3,
            "pool_mode": "pool_first",
        },
    )
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=7)
    gold = _add_pool_coupon(db, tenant_id, "NHGLD", "gold")
    silver = _add_pool_coupon(db, tenant_id, "NHSLV", "silver")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is True
    assert result.reason_code == "issued"
    assert result.resolved_level == "gold"
    assert result.coupon_id == gold.id
    assert result.code == "NHGLD"
    db.refresh(gold)
    db.refresh(silver)
    assert int((gold.extra_metadata or {}).get("customer_id")) == customer.id
    assert (gold.extra_metadata or {}).get("used") == "true"
    assert (silver.extra_metadata or {}).get("customer_id") is None


def test_repeat_request_reuses_assigned_coupon() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    first = _add_pool_coupon(db, tenant_id, "NHAAA", "bronze")
    second = _add_pool_coupon(db, tenant_id, "NHBBB", "bronze")
    one = _issue(db, tenant_id, customer.id)
    two = _issue(db, tenant_id, customer.id)
    assert one.issued and two.issued
    assert one.coupon_id == two.coupon_id == first.id
    assert two.reason_code == "reused_existing_assignment"
    db.refresh(second)
    assert (second.extra_metadata or {}).get("customer_id") is None


def test_different_customers_are_isolated() -> None:
    db, tenant_id, _engine = _make_db()
    a = _add_customer(db, tenant_id, PHONE_A, name="أحمد سالم")
    b = _add_customer(db, tenant_id, PHONE_B, name="نورة عبدالله")
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    _add_orders(db, tenant_id, PHONE_B, countable=1)
    c1 = _add_pool_coupon(db, tenant_id, "NHAAA", "bronze")
    c2 = _add_pool_coupon(db, tenant_id, "NHBBB", "bronze")
    ra = _issue(db, tenant_id, a.id)
    rb = _issue(db, tenant_id, b.id)
    assert ra.coupon_id != rb.coupon_id
    assert {ra.coupon_id, rb.coupon_id} == {c1.id, c2.id}


def test_tenant_isolation() -> None:
    db, tenant_id, engine = _make_db()
    other = Tenant(name="Other Tenant", is_active=True)
    db.add(other)
    db.flush()
    db.add(TenantSettings(tenant_id=other.id, extra_metadata={"coupons_dashboard": {
        "levels": _levels(),
        "ai_policy": {"enabled": True, "allowed_levels": ["bronze"], "pool_mode": "pool_first"},
    }}))
    db.commit()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    foreign = Coupon(
        tenant_id=other.id,
        code="NHZZZ",
        discount_type="percentage",
        discount_value="5",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        extra_metadata=_pool_meta(level="bronze"),
        coupon_level="bronze",
        source_type="system",
    )
    db.add(foreign)
    db.commit()
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False or result.coupon_id != foreign.id
    db.refresh(foreign)
    assert (foreign.extra_metadata or {}).get("customer_id") is None


def test_expired_pool_coupon_is_not_issued() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    _add_pool_coupon(
        db,
        tenant_id,
        "NHEXP",
        "bronze",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.reason_code in {REASON_POOL_EMPTY, REASON_SALLA_UNAVAILABLE}


def test_used_pool_coupon_is_not_issued() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    used = _add_pool_coupon(db, tenant_id, "NHUSD", "bronze", used="true")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    db.refresh(used)
    assert (used.extra_metadata or {}).get("customer_id") is None


def test_min_remaining_hours_excludes_near_expiry() -> None:
    db, tenant_id, _engine = _make_db(
        ai_policy={
            "enabled": True,
            "allowed_levels": ["bronze", "silver"],
            "min_remaining_hours": 3,
            "pool_mode": "pool_only",
        }
    )
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    _add_pool_coupon(
        db,
        tenant_id,
        "NHTTL",
        "bronze",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.reason_code == REASON_POOL_EMPTY


def test_consumed_assigned_coupon_is_not_reused() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    consumed = _add_pool_coupon(
        db,
        tenant_id,
        "NHOLD",
        "bronze",
        used="true",
        extra={"customer_id": None},
    )
    meta = dict(consumed.extra_metadata or {})
    meta["customer_id"] = customer.id
    meta["issued_reason"] = ISSUED_REASON_CUSTOMER_REQUEST
    meta["issued_channel"] = "ai"
    meta["redeemed"] = True
    consumed.extra_metadata = meta
    consumed.allocation_channel = "ai"
    flag_modified(consumed, "extra_metadata")
    db.commit()
    fresh = _add_pool_coupon(db, tenant_id, "NHNEW", "bronze")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is True
    assert result.coupon_id == fresh.id
    assert result.reason_code == "issued"


def test_pool_only_does_not_create() -> None:
    db, tenant_id, _engine = _make_db(
        ai_policy={
            "enabled": True,
            "allowed_levels": ["bronze", "silver"],
            "min_remaining_hours": 0,
            "pool_mode": "pool_only",
        }
    )
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.reason_code == REASON_POOL_EMPTY
    assert db.query(Coupon).count() == 0


def test_on_demand_only_with_patched_adapter() -> None:
    db, tenant_id, _engine = _make_db(
        ai_policy={
            "enabled": True,
            "allowed_levels": ["bronze", "silver"],
            "min_remaining_hours": 0,
            "pool_mode": "on_demand_only",
        }
    )
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    pool = _add_pool_coupon(db, tenant_id, "NHPL1", "bronze")
    with patch.object(
        coupon_request_mod.CouponGeneratorService, "_get_adapter", lambda self: _fake_adapter()
    ):
        result = _issue(db, tenant_id, customer.id)
    assert result.issued is True
    assert result.code != pool.code
    db.refresh(pool)
    assert (pool.extra_metadata or {}).get("customer_id") is None
    created = db.query(Coupon).filter(Coupon.code == result.code).one()
    assert int((created.extra_metadata or {}).get("customer_id")) == customer.id


def test_pool_first_falls_back_to_on_demand() -> None:
    db, tenant_id, _engine = _make_db(
        ai_policy={
            "enabled": True,
            "allowed_levels": ["bronze", "silver"],
            "min_remaining_hours": 0,
            "pool_mode": "pool_first",
        }
    )
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    with patch.object(
        coupon_request_mod.CouponGeneratorService, "_get_adapter", lambda self: _fake_adapter()
    ):
        result = _issue(db, tenant_id, customer.id)
    assert result.issued is True
    assert result.reason_code == "issued"


def test_pick_coupon_for_segment_still_does_not_stamp_customer() -> None:
    db, tenant_id, _engine = _make_db()
    _add_pool_coupon(db, tenant_id, "NHSEG", "silver")
    svc = CouponGeneratorService(db, tenant_id)
    picked = svc.pick_coupon_for_segment("active", for_channel="ai")
    assert picked is not None
    assert (picked.extra_metadata or {}).get("customer_id") is None
    assert (picked.extra_metadata or {}).get("used") == "true"


def test_crm_segment_mapping_unchanged() -> None:
    assert _segment_to_level("new") == "bronze"
    assert _segment_to_level("active") == "silver"
    assert _segment_to_level("vip") == "gold"
    assert _segment_to_level("at_risk") == "vip"


def test_count_matrix_through_service_resolver_gate() -> None:
    cases = {
        0: REASON_NO_LEVEL,
        1: "bronze",
        2: "bronze",
        3: "silver",
        6: "silver",
        7: "gold",
        14: "gold",
        15: "vip",
        16: "vip",
    }
    for n, expected in cases.items():
        db, tenant_id, _engine = _make_db(
            levels=_levels(include_ai_for=("bronze", "silver", "gold", "vip")),
            ai_policy={
                "enabled": True,
                "allowed_levels": ["bronze", "silver", "gold", "vip"],
                "min_remaining_hours": 0,
                "pool_mode": "pool_only",
            },
        )
        customer = _add_customer(db, tenant_id, PHONE_A)
        if n:
            _add_orders(db, tenant_id, PHONE_A, countable=n)
        result = _issue(db, tenant_id, customer.id)
        if expected == REASON_NO_LEVEL:
            assert result.resolved_level is None
            assert result.reason_code == REASON_NO_LEVEL
        else:
            assert result.resolved_level == expected
            assert result.countable_orders == n


def test_bronze_assignment_is_not_reused_after_customer_reaches_silver() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    bronze = _add_pool_coupon(db, tenant_id, "NHBRZ", "bronze")
    first = _issue(db, tenant_id, customer.id)
    assert first.issued is True
    assert first.coupon_id == bronze.id
    assert first.resolved_level == "bronze"
    _add_orders(db, tenant_id, PHONE_A, countable=2)
    silver = _add_pool_coupon(db, tenant_id, "NHSLV", "silver")
    second = _issue(db, tenant_id, customer.id)
    assert second.issued is True
    assert second.resolved_level == "silver"
    assert second.coupon_id == silver.id
    assert second.reason_code == "issued"
    db.refresh(bronze)
    assert int((bronze.extra_metadata or {}).get("customer_id")) == customer.id
    reused = find_reusable_assigned_coupon(
        db, tenant_id, customer.id, resolved_level="bronze", for_channel="ai"
    )
    assert reused is not None and reused.id == bronze.id
    current = find_reusable_assigned_coupon(
        db, tenant_id, customer.id, resolved_level="silver", for_channel="ai"
    )
    assert current is not None and current.id == silver.id


def test_current_silver_customer_request_is_reused() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=3)
    first_pool = _add_pool_coupon(db, tenant_id, "NHSL1", "silver")
    spare = _add_pool_coupon(db, tenant_id, "NHSL2", "silver")
    one = _issue(db, tenant_id, customer.id)
    two = _issue(db, tenant_id, customer.id)
    assert one.issued and two.issued
    assert one.resolved_level == two.resolved_level == "silver"
    assert one.coupon_id == two.coupon_id == first_pool.id
    assert two.reason_code == "reused_existing_assignment"
    db.refresh(spare)
    assert (spare.extra_metadata or {}).get("customer_id") is None


def test_campaign_assignment_with_same_customer_is_not_reused() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    campaign = _add_pool_coupon(db, tenant_id, "NHCMP", "bronze")
    _stamp_owned(db, campaign, customer.id, reason="campaign", channel="campaign")
    pool = _add_pool_coupon(db, tenant_id, "NHNEW", "bronze")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is True
    assert result.coupon_id == pool.id
    assert result.reason_code == "issued"
    assert find_reusable_assigned_coupon(
        db, tenant_id, customer.id, resolved_level="bronze", for_channel="ai"
    ).id == pool.id


def test_autopilot_assignment_with_same_customer_is_not_reused() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    auto = _add_pool_coupon(db, tenant_id, "NHAUT", "bronze")
    _stamp_owned(db, auto, customer.id, reason="autopilot", channel="autopilot")
    pool = _add_pool_coupon(db, tenant_id, "NHNEW", "bronze")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is True
    assert result.coupon_id == pool.id
    assert result.reason_code == "issued"


def test_reuse_finds_assignment_older_than_200_unrelated_coupons() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    assigned = _add_pool_coupon(db, tenant_id, "NHOLD", "bronze")
    _stamp_owned(db, assigned, customer.id)
    for i in range(210):
        _add_pool_coupon(db, tenant_id, f"U{i:04d}", "bronze")
    found = find_reusable_assigned_coupon(
        db, tenant_id, customer.id, resolved_level="bronze", for_channel="ai"
    )
    assert found is not None
    assert found.id == assigned.id
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is True
    assert result.coupon_id == assigned.id
    assert result.reason_code == "reused_existing_assignment"


def test_reuse_query_is_tenant_scoped() -> None:
    db, tenant_id, engine = _make_db()
    other = Tenant(name="Other Coupon Tenant", is_active=True)
    db.add(other)
    db.flush()
    db.add(
        TenantSettings(
            tenant_id=other.id,
            extra_metadata={
                "coupons_dashboard": {
                    "levels": _levels(),
                    "ai_policy": {
                        "enabled": True,
                        "allowed_levels": ["bronze"],
                        "pool_mode": "pool_first",
                    },
                }
            },
        )
    )
    db.commit()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    foreign = Coupon(
        tenant_id=other.id,
        code="NHFOR",
        discount_type="percentage",
        discount_value="5",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        extra_metadata=_pool_meta(level="bronze"),
        coupon_level="bronze",
        source_type="system",
    )
    db.add(foreign)
    db.commit()
    db.refresh(foreign)
    _stamp_owned(db, foreign, customer.id)
    found = find_reusable_assigned_coupon(
        db, tenant_id, customer.id, resolved_level="bronze", for_channel="ai"
    )
    assert found is None
    pool = _add_pool_coupon(db, tenant_id, "NHLOC", "bronze")
    result = _issue(db, tenant_id, customer.id)
    assert result.coupon_id == pool.id
    db.refresh(foreign)
    assert int((foreign.extra_metadata or {}).get("customer_id")) == customer.id
    assert foreign.tenant_id == other.id


def test_general_manual_promo_is_not_ai_allocated() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    promo = _add_native_coupon(
        db, tenant_id, "AYRDIS", "bronze", ai_allocatable=False
    )
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.reason_code == REASON_POOL_EMPTY
    db.refresh(promo)
    assert (promo.extra_metadata or {}).get("customer_id") is None
    assert (promo.extra_metadata or {}).get("used") != "true"


def test_legacy_manual_without_marker_is_excluded() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    row = Coupon(
        tenant_id=tenant_id,
        code="AYODIS",
        discount_type="percentage",
        discount_value="6",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        extra_metadata={"source": "dashboard", "active": True, "usage_limit": 1},
        source_type="manual",
    )
    db.add(row)
    db.commit()
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    db.refresh(row)
    assert (row.extra_metadata or {}).get("customer_id") is None


def test_native_ai_coupon_is_issued_without_salla() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    native = _add_native_coupon(db, tenant_id, "NATAI1", "bronze")
    with patch.object(
        coupon_request_mod.CouponGeneratorService, "_get_adapter", lambda self: None
    ):
        result = _issue(db, tenant_id, customer.id)
    assert result.issued is True
    assert result.code == "NATAI1"
    assert result.reason_code == "issued"
    db.refresh(native)
    assert int((native.extra_metadata or {}).get("customer_id")) == customer.id
    assert (native.extra_metadata or {}).get("issued_reason") == ISSUED_REASON_CUSTOMER_REQUEST
    assert (native.extra_metadata or {}).get("issued_channel") == "ai"


def test_native_wrong_level_is_excluded() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    wrong = _add_native_coupon(db, tenant_id, "NASLV1", "silver")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    db.refresh(wrong)
    assert (wrong.extra_metadata or {}).get("customer_id") is None


def test_native_wrong_channel_is_excluded() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    wrong = _add_native_coupon(
        db, tenant_id, "NACMP1", "bronze", allocation_channel="campaign"
    )
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    db.refresh(wrong)
    assert (wrong.extra_metadata or {}).get("customer_id") is None


def test_native_expired_and_used_and_bound_are_excluded() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    other = _add_customer(db, tenant_id, PHONE_B)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    _add_native_coupon(
        db,
        tenant_id,
        "NAEXP1",
        "bronze",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    _add_native_coupon(db, tenant_id, "NAUSD1", "bronze", used="true")
    bound = _add_native_coupon(db, tenant_id, "NABND1", "bronze")
    _stamp_owned(db, bound, other.id)
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.reason_code == REASON_POOL_EMPTY


def test_native_repeat_request_reuses_assignment() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    native = _add_native_coupon(db, tenant_id, "NAREU1", "bronze")
    spare = _add_native_coupon(db, tenant_id, "NAREU2", "bronze")
    first = _issue(db, tenant_id, customer.id)
    second = _issue(db, tenant_id, customer.id)
    assert first.issued and second.issued
    assert first.coupon_id == second.coupon_id == native.id
    db.refresh(spare)
    assert (spare.extra_metadata or {}).get("customer_id") is None


def test_two_customers_cannot_share_native_coupon() -> None:
    db, tenant_id, _engine = _make_db()
    a = _add_customer(db, tenant_id, PHONE_A)
    b = _add_customer(db, tenant_id, PHONE_B)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    _add_orders(db, tenant_id, PHONE_B, countable=1)
    native = _add_native_coupon(db, tenant_id, "NASHR1", "bronze")
    first = _issue(db, tenant_id, a.id)
    second = _issue(db, tenant_id, b.id)
    assert first.issued is True
    assert first.coupon_id == native.id
    assert second.issued is False
    assert second.reason_code == REASON_POOL_EMPTY


def test_salla_pool_is_selected_ahead_of_native() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    salla = _add_pool_coupon(db, tenant_id, "NHAAA", "bronze")
    native = _add_native_coupon(db, tenant_id, "NATAI9", "bronze")
    result = _issue(db, tenant_id, customer.id)
    assert result.coupon_id == salla.id
    db.refresh(native)
    assert (native.extra_metadata or {}).get("customer_id") is None


def test_salla_pool_pick_ignores_native_manual() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    native = _add_native_coupon(db, tenant_id, "NATAI8", "bronze")
    svc = CouponGeneratorService(db, tenant_id)
    picked = svc.pick_coupon_for_level("bronze", customer.id, for_channel="ai")
    assert picked is None
    native_picked = svc.pick_native_ai_coupon_for_level(
        "bronze", customer.id, for_channel="ai"
    )
    assert native_picked is not None
    assert native_picked.id == native.id


def test_no_salla_empty_native_is_pool_empty_not_salla_unavailable() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    with patch.object(
        coupon_request_mod.CouponGeneratorService, "_get_adapter", lambda self: None
    ):
        result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    assert result.reason_code == REASON_POOL_EMPTY
    assert db.query(Coupon).filter(Coupon.tenant_id == tenant_id).count() == 0


def test_unlimited_manual_usage_is_not_one_customer_safe() -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    promo = _add_native_coupon(
        db, tenant_id, "NAUNL1", "bronze", usage_limit=0, extra={"usage_limit": 0}
    )
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is False
    db.refresh(promo)
    assert (promo.extra_metadata or {}).get("customer_id") is None


def test_zero_orders_first_purchase_enabled_authorizes_bronze() -> None:
    db, tenant_id, _engine = _make_db(first_purchase=True)
    customer = _add_customer(db, tenant_id, PHONE_A)
    native = _add_native_coupon(db, tenant_id, "NAFP01", "bronze")
    result = _issue(db, tenant_id, customer.id)
    assert result.issued is True
    assert result.resolved_level == "bronze"
    assert result.coupon_id == native.id
    assert result.countable_orders == 0
