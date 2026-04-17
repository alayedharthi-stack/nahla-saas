"""
tests/test_offer_decisions_router.py
─────────────────────────────────────
Coverage for the read-only telemetry surface that powers the Analytics
"Smart Offer Performance" widget. We assert two invariants:

  1. **Aggregations are tenant-scoped.** Decisions for tenant A must
     never leak into tenant B's rollup.
  2. **Headline maths is correct.** redemption_rate_pct, attributed
     revenue and the surface×source matrix are all derived from the
     same ledger and must agree with the underlying rows.

We exercise the router functions directly (not through TestClient) so
we don't have to spin up the full FastAPI app: the `routers` module
relies on `core.tenant.resolve_tenant_id`, which we monkeypatch to
inject a known tenant id.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Tuple

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, OfferDecisionLedger, Tenant  # noqa: E402
from routers import offer_decisions as router_module  # noqa: E402


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    _saved: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig_type in _saved:
        col.type = orig_type
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db, name: str = "T") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _add_decision(
    db,
    *,
    tenant_id: int,
    surface: str = "automation",
    chosen_source: str = "coupon",
    discount_value: float | None = 10.0,
    attributed: bool = False,
    revenue: float | None = None,
    reason_codes: list[str] | None = None,
    age_days: int = 0,
    decision_id: str | None = None,
) -> OfferDecisionLedger:
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    row = OfferDecisionLedger(
        tenant_id=tenant_id,
        decision_id=decision_id or f"d-{datetime.now().timestamp()}-{tenant_id}-{surface}",
        surface=surface,
        chosen_source=chosen_source,
        discount_type="percentage" if discount_value is not None else None,
        discount_value=Decimal(str(discount_value)) if discount_value is not None else None,
        reason_codes=reason_codes or ["test"],
        policy_version="v1.0-deterministic",
        attributed=attributed,
        revenue_amount=Decimal(str(revenue)) if revenue is not None else None,
        created_at=created,
    )
    db.add(row); db.commit(); db.refresh(row)
    return row


class _FakeRequest:
    """Minimal stand-in for fastapi.Request — only `state` and `url` are
    read by `resolve_tenant_id`, but we monkeypatch that anyway."""
    def __init__(self) -> None:
        class _S: pass
        self.state = _S()


def _call(coro):
    return asyncio.run(coro)


# ── 1. summary endpoint ─────────────────────────────────────────────────

class TestSummary:
    def test_empty_window_is_all_zeros(self, monkeypatch) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            monkeypatch.setattr(router_module, "resolve_tenant_id", lambda _r: t.id)

            out = _call(router_module.decisions_summary(_FakeRequest(), days=30, db=db))
            assert out["decisions_total"] == 0
            assert out["offers_issued"] == 0
            assert out["offers_attributed"] == 0
            assert out["redemption_rate_pct"] == 0.0
            assert out["attributed_revenue"] == 0.0
            assert out["by_surface"] == {}
            assert out["by_source"] == {}
            # Even with no rows we surface the *default* policy version so
            # the widget doesn't render an empty audit field.
            assert out["policy_version"] == "v1.0-deterministic"
        finally:
            db.close(); engine.dispose()

    def test_counts_redemption_and_revenue(self, monkeypatch) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            # 4 issued (3 coupons + 1 promotion), 1 "none", 2 attributed @ 100/200.
            _add_decision(db, tenant_id=t.id, surface="automation", chosen_source="coupon", attributed=True,  revenue=100)
            _add_decision(db, tenant_id=t.id, surface="automation", chosen_source="coupon", attributed=True,  revenue=200)
            _add_decision(db, tenant_id=t.id, surface="chat",       chosen_source="coupon", attributed=False)
            _add_decision(db, tenant_id=t.id, surface="automation", chosen_source="promotion")
            _add_decision(db, tenant_id=t.id, surface="segment_change", chosen_source="none", discount_value=None)

            monkeypatch.setattr(router_module, "resolve_tenant_id", lambda _r: t.id)

            out = _call(router_module.decisions_summary(_FakeRequest(), days=30, db=db))
            assert out["decisions_total"] == 5
            assert out["offers_issued"] == 4
            assert out["offers_attributed"] == 2
            assert out["redemption_rate_pct"] == 50.0
            assert out["attributed_revenue"] == 300.0
            assert out["by_surface"] == {
                "automation": 3,
                "chat": 1,
                "segment_change": 1,
            }
            assert out["by_source"] == {
                "coupon": 3,
                "promotion": 1,
                "none": 1,
            }
        finally:
            db.close(); engine.dispose()

    def test_other_tenants_are_excluded(self, monkeypatch) -> None:
        db, engine = _make_db()
        try:
            ours = _seed_tenant(db, "ours")
            other = _seed_tenant(db, "other")
            _add_decision(db, tenant_id=ours.id,  attributed=True, revenue=50)
            _add_decision(db, tenant_id=other.id, attributed=True, revenue=999)

            monkeypatch.setattr(router_module, "resolve_tenant_id", lambda _r: ours.id)
            out = _call(router_module.decisions_summary(_FakeRequest(), days=30, db=db))
            assert out["decisions_total"] == 1
            assert out["attributed_revenue"] == 50.0
        finally:
            db.close(); engine.dispose()

    def test_window_filter_excludes_old_rows(self, monkeypatch) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _add_decision(db, tenant_id=t.id, attributed=True, revenue=10)
            _add_decision(db, tenant_id=t.id, attributed=True, revenue=10, age_days=120)

            monkeypatch.setattr(router_module, "resolve_tenant_id", lambda _r: t.id)
            out = _call(router_module.decisions_summary(_FakeRequest(), days=30, db=db))
            assert out["decisions_total"] == 1
            assert out["attributed_revenue"] == 10.0
        finally:
            db.close(); engine.dispose()


# ── 2. breakdown endpoint ───────────────────────────────────────────────

class TestBreakdown:
    def test_reason_codes_sorted_descending(self, monkeypatch) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            for _ in range(3):
                _add_decision(db, tenant_id=t.id, reason_codes=["explicit_promotion_id"])
            for _ in range(5):
                _add_decision(db, tenant_id=t.id, reason_codes=["segment_default_pct"])
            _add_decision(db, tenant_id=t.id, reason_codes=["cap_max_discount", "cap_frequency"])

            monkeypatch.setattr(router_module, "resolve_tenant_id", lambda _r: t.id)
            out = _call(router_module.decisions_breakdown(_FakeRequest(), days=30, db=db))
            codes = [r["code"] for r in out["reason_codes"]]
            counts = {r["code"]: r["count"] for r in out["reason_codes"]}
            assert codes[0] == "segment_default_pct"
            assert counts["segment_default_pct"] == 5
            assert counts["explicit_promotion_id"] == 3
            assert counts["cap_max_discount"] == 1
            assert counts["cap_frequency"] == 1

        finally:
            db.close(); engine.dispose()

    def test_matrix_groups_surface_and_source(self, monkeypatch) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _add_decision(db, tenant_id=t.id, surface="automation", chosen_source="coupon")
            _add_decision(db, tenant_id=t.id, surface="automation", chosen_source="coupon")
            _add_decision(db, tenant_id=t.id, surface="automation", chosen_source="promotion")
            _add_decision(db, tenant_id=t.id, surface="chat",       chosen_source="coupon")

            monkeypatch.setattr(router_module, "resolve_tenant_id", lambda _r: t.id)
            out = _call(router_module.decisions_breakdown(_FakeRequest(), days=30, db=db))
            assert out["matrix"]["automation"] == {"coupon": 2, "promotion": 1}
            assert out["matrix"]["chat"]       == {"coupon": 1}
        finally:
            db.close(); engine.dispose()

    def test_discount_buckets_track_attribution(self, monkeypatch) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            # 10% bucket: 2 issued, 1 attributed @ 80 SAR
            _add_decision(db, tenant_id=t.id, discount_value=10, attributed=True,  revenue=80)
            _add_decision(db, tenant_id=t.id, discount_value=12, attributed=False)
            # 20% bucket: 1 issued, 1 attributed @ 150 SAR
            _add_decision(db, tenant_id=t.id, discount_value=22, attributed=True,  revenue=150)

            monkeypatch.setattr(router_module, "resolve_tenant_id", lambda _r: t.id)
            out = _call(router_module.decisions_breakdown(_FakeRequest(), days=30, db=db))
            buckets = out["by_discount_bucket"]
            # 10..14 → "10-14%", 20..24 → "20-24%"
            assert buckets["10-14%"]["issued"]     == 2
            assert buckets["10-14%"]["attributed"] == 1
            assert buckets["10-14%"]["revenue"]    == 80.0
            assert buckets["20-24%"]["issued"]     == 1
            assert buckets["20-24%"]["attributed"] == 1
            assert buckets["20-24%"]["revenue"]    == 150.0
        finally:
            db.close(); engine.dispose()
