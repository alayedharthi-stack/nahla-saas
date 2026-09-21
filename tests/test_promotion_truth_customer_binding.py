"""A personal coupon — one issued to a single customer — is shareable only in a
conversation with that customer.

The promotion engine and the coupon generator write personal codes into the
same ``coupons`` table as store-wide ones, stamped with the customer's id in
the row's metadata and with no campaign-only channel. Before this, the
shareable list treated them as store-wide. Offline, with the same fake session
the resolver's other tests use.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce import promotion_truth as pt  # noqa: E402


class Row:
    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id", 1)
        self.tenant_id = kwargs.get("tenant_id", 1)
        self.code = kwargs.get("code", "SAVE10")
        self.description = kwargs.get("description", "خصم")
        self.discount_type = kwargs.get("discount_type", "percentage")
        self.discount_value = kwargs.get("discount_value", "10")
        self.expires_at = kwargs.get("expires_at", datetime.now(timezone.utc) + timedelta(days=7))
        self.source_type = kwargs.get("source_type", "manual")
        self.allocation_channel = kwargs.get("allocation_channel", "")
        self.coupon_level = kwargs.get("coupon_level")
        self.extra_metadata = kwargs.get("extra_metadata") or {}
        self.rules: List[Any] = []
        self.starts_at = None


def session(coupons: List[Row]) -> MagicMock:
    db = MagicMock()

    def query(model: Any) -> MagicMock:
        q = MagicMock()
        for chain in ("filter", "order_by", "limit", "join"):
            getattr(q, chain).return_value = q
        name = str(getattr(model, "__name__", "") or model)
        q.all.return_value = [] if ("CouponRule" in name or "Promotion" in name) else list(coupons)
        q.first.return_value = None
        return q

    db.query.side_effect = query
    return db


def codes(result: Any) -> List[str]:
    return [fact["code"] for fact in result.shareable]


def personal(customer_id: int, **overrides: Any) -> Row:
    """A personal code exactly as the promotion engine writes it: customer id
    and a single-use limit in the metadata, no allocation channel."""
    meta: Dict[str, Any] = {"customer_id": customer_id, "usage_limit": 1, "usage_count": 0, "active": True}
    return Row(code=f"PERSONAL{customer_id}", extra_metadata=meta, **overrides)


def test_a_store_wide_code_is_shareable_with_anyone() -> None:
    db = session([Row(code="WELCOME10")])
    assert codes(pt.resolve_shareable_promotions(db, 1)) == ["WELCOME10"]
    assert codes(pt.resolve_shareable_promotions(db, 1, customer_id=55)) == ["WELCOME10"]


def test_a_personal_code_is_never_shareable_without_a_customer() -> None:
    db = session([personal(41), Row(id=2, code="WELCOME10")])
    assert codes(pt.resolve_shareable_promotions(db, 1)) == ["WELCOME10"]


def test_a_personal_code_is_shareable_only_with_its_own_customer() -> None:
    db = session([personal(41), Row(id=2, code="WELCOME10")])
    assert codes(pt.resolve_shareable_promotions(db, 1, customer_id=41)) == ["PERSONAL41", "WELCOME10"]
    assert codes(pt.resolve_shareable_promotions(db, 1, customer_id=42)) == ["WELCOME10"]


def test_the_generator_s_ai_channel_stamp_does_not_make_a_personal_code_store_wide() -> None:
    row = personal(41, allocation_channel="ai")
    row.extra_metadata["issued_channel"] = "ai"
    db = session([row])
    assert codes(pt.resolve_shareable_promotions(db, 1)) == []
    assert codes(pt.resolve_shareable_promotions(db, 1, customer_id=41)) == ["PERSONAL41"]


def test_an_unreadable_binding_is_bound_to_nobody_shareable() -> None:
    db = session([Row(code="ODD", extra_metadata={"customer_id": "not-a-number"})])
    assert codes(pt.resolve_shareable_promotions(db, 1)) == []
    assert codes(pt.resolve_shareable_promotions(db, 1, customer_id=41)) == []


def test_the_fact_says_whose_code_it_is_and_which_level() -> None:
    db = session([personal(41, coupon_level="Silver")])
    fact = pt.resolve_shareable_promotions(db, 1, customer_id=41).shareable[0]
    assert fact["customer_bound"] is True and fact["bound_customer_id"] == 41
    assert fact["coupon_level"] == "silver"
    store_wide = pt.resolve_shareable_promotions(session([Row(code="WELCOME10")]), 1).shareable[0]
    assert store_wide["customer_bound"] is False and store_wide["bound_customer_id"] is None
    assert store_wide["coupon_level"] == ""


def _binding(meta: Optional[Dict[str, Any]]) -> Optional[int]:
    return pt._row_customer_binding(Row(extra_metadata=meta or {}))


def test_every_binding_key_the_platform_writes_is_recognised() -> None:
    assert _binding({"customer_id": 7}) == 7
    assert _binding({"assigned_customer_id": "8"}) == 8
    assert _binding({"owner_customer_id": 9}) == 9
    assert _binding({"customer_id": 0}) is None and _binding({}) is None
