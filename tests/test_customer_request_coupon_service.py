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
    REASON_AI_POLICY_DISABLED,
    REASON_IDENTITY_UNAVAILABLE,
    REASON_LEVEL_NOT_ALLOWED_FOR_AI,
    REASON_LIVE_ISSUANCE_DISABLED,
    REASON_NO_LEVEL,
    REASON_POOL_EMPTY,
    REASON_SALLA_UNAVAILABLE,
    count_customer_orders,
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
    meta["redeemed"] = True
    consumed.extra_metadata = meta
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
