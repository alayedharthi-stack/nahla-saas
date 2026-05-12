"""
tests/test_offer_policy_registry.py
───────────────────────────────────
Locks down **Phase 6** of the Offer Optimisation plan: opening
``policy_version`` and ``experiment_arm`` to a swappable policy
implementation, without touching any of the three caller surfaces
(automation, chat, segment-change).

Invariants we protect
─────────────────────

1. **Default parity.** A tenant with no ``ai_settings.offer_policy``
   config gets the exact same decision and the same
   ``policy_version='v1.0-deterministic'`` / ``experiment_arm=None``
   in the ledger as Phases 1–5 produced. Phase 6 must be invisible
   when nobody opts in.

2. **Custom policy registration.** A test policy registered under a
   new version string is selectable by name, runs end-to-end, and
   its version is what the ledger records — not the v1 constant.

3. **Unknown version → safe fallback.** Tenant config that points at
   an unregistered ``version`` string degrades to the default policy
   *and* logs a warning. The ledger MUST stamp the version that
   actually ran (``v1.0-deterministic``), not the typo'd string, so
   analytics never lie.

4. **A/B sticky-by-customer.** When an experiment has 50/50 arms
   pointing at two different policies, the same ``customer_id``
   always lands on the same arm. Different customers split
   roughly evenly across the arms.

5. **A/B arm overrides top-level version.** When an arm declares a
   ``policy_version`` the experiment routes traffic to that
   implementation, overriding the top-level ``version`` field.

6. **Admin endpoint round-trip.** GET → PUT → GET yields the
   configuration we wrote, and PUT rejects references to unknown
   policy versions (top-level OR per arm).
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Tuple

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


from models import (  # noqa: E402
    Base,
    OfferDecisionLedger,
    Tenant,
    TenantSettings,
)
from routers import admin as admin_router  # noqa: E402
from services import offer_policies as op  # noqa: E402
from services.offer_decision_service import (  # noqa: E402
    POLICY_VERSION,
    SOURCE_COUPON,
    SOURCE_NONE,
    SURFACE_AUTOMATION,
    OfferDecision,
    OfferDecisionContext,
    OfferDecisionSignals,
    decide,
)


# ── DB harness ─────────────────────────────────────────────────────────────

def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    saved: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig_type in saved:
        col.type = orig_type
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db, *, tid: int = 1) -> Tenant:
    t = Tenant(id=tid, name=f"T{tid}", is_active=True, is_platform_tenant=False)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _set_policy_config(db, tenant_id: int, cfg: dict | None) -> None:
    """Write ``ai_settings.offer_policy`` for a tenant (None wipes it)."""
    ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    if ts is None:
        ts = TenantSettings(tenant_id=tenant_id, ai_settings={})
        db.add(ts)
    ai = dict(ts.ai_settings or {})
    if cfg is None:
        ai.pop("offer_policy", None)
    else:
        ai["offer_policy"] = cfg
    ts.ai_settings = ai
    db.commit()
    db.refresh(ts)


def _make_ctx(*, tenant_id: int = 1, customer_id: int | None = None) -> OfferDecisionContext:
    return OfferDecisionContext(
        tenant_id=tenant_id,
        surface=SURFACE_AUTOMATION,
        customer_id=customer_id,
        signals=OfferDecisionSignals(segment="active"),
    )


@pytest.fixture(autouse=True)
def _clean_registry_overrides():
    """Tests that register custom policies must not leak into siblings."""
    snapshot = set(op._REGISTRY.keys())
    yield
    extras = set(op._REGISTRY.keys()) - snapshot
    for v in extras:
        op.unregister_policy(v)


# ── 1. Default parity ─────────────────────────────────────────────────────

class TestDefaultParity:
    """Phase 6 must be invisible when no tenant opts in."""

    def test_no_config_routes_to_v1_with_null_arm(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            d = decide(db, _make_ctx())
            assert d.policy_version == POLICY_VERSION == "v1.0-deterministic"
            assert d.experiment_arm is None
            row = db.query(OfferDecisionLedger).first()
            assert row is not None
            assert row.policy_version == "v1.0-deterministic"
            assert row.experiment_arm is None
        finally:
            db.close(); engine.dispose()

    def test_explicit_default_version_is_a_noop(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            _set_policy_config(db, 1, {"version": "v1.0-deterministic"})
            d = decide(db, _make_ctx())
            assert d.policy_version == "v1.0-deterministic"
            assert d.experiment_arm is None
        finally:
            db.close(); engine.dispose()


# ── 2. Custom policy registration ─────────────────────────────────────────

class TestCustomPolicyRegistration:

    def _stub_v2(self, db, ctx) -> OfferDecision:
        """Mock 'v2' policy — always picks a fixed coupon for the test."""
        from uuid import uuid4
        return OfferDecision(
            decision_id=uuid4().hex,
            source=SOURCE_COUPON,
            discount_type="percentage",
            discount_value=12.0,
            validity_days=7,
            reason_codes=["stub_v2_executed"],
            segment="active",
        )

    def test_registered_policy_is_selected_by_name(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            op.register_policy("v2.0-stub", self._stub_v2)
            _set_policy_config(db, 1, {"version": "v2.0-stub"})

            d = decide(db, _make_ctx())
            assert "stub_v2_executed" in d.reason_codes
            assert d.policy_version == "v2.0-stub"

            row = db.query(OfferDecisionLedger).first()
            assert row.policy_version == "v2.0-stub", (
                "Ledger must record the policy that actually ran, "
                "so analytics can group decisions by version."
            )
        finally:
            db.close(); engine.dispose()


# ── 3. Unknown version → safe fallback ────────────────────────────────────

class TestUnknownVersionFallback:

    def test_unknown_version_falls_back_to_v1_in_ledger(self, caplog):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            _set_policy_config(db, 1, {"version": "vNonExistent"})

            with caplog.at_level("WARNING"):
                d = decide(db, _make_ctx())

            assert d.policy_version == "v1.0-deterministic", (
                "Unknown configured version must NOT leak into the ledger; "
                "we always stamp the version that actually executed."
            )
            assert any("unknown policy version" in rec.getMessage().lower()
                       for rec in caplog.records), (
                "Ops needs a WARNING to surface the bad tenant config."
            )
        finally:
            db.close(); engine.dispose()


# ── 4. A/B sticky-by-customer ─────────────────────────────────────────────

class TestStickyByCustomer:

    def _stub_arm_a(self, db, ctx) -> OfferDecision:
        from uuid import uuid4
        return OfferDecision(
            decision_id=uuid4().hex, source=SOURCE_NONE,
            reason_codes=["arm_a_ran"],
        )

    def _stub_arm_b(self, db, ctx) -> OfferDecision:
        from uuid import uuid4
        return OfferDecision(
            decision_id=uuid4().hex, source=SOURCE_NONE,
            reason_codes=["arm_b_ran"],
        )

    def test_same_customer_always_lands_on_same_arm(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            op.register_policy("arm-a", self._stub_arm_a)
            op.register_policy("arm-b", self._stub_arm_b)
            _set_policy_config(db, 1, {
                "experiment": {
                    "name": "phase6_split",
                    "sticky_by": "customer_id",
                    "arms": [
                        {"name": "A", "weight": 50, "policy_version": "arm-a"},
                        {"name": "B", "weight": 50, "policy_version": "arm-b"},
                    ],
                },
            })

            arms_seen_per_customer: dict[int, set[str]] = {}
            for cid in (101, 202, 303, 404):
                for _ in range(5):
                    d = decide(db, _make_ctx(customer_id=cid))
                    arms_seen_per_customer.setdefault(cid, set()).add(d.experiment_arm)

            for cid, arms in arms_seen_per_customer.items():
                assert len(arms) == 1, (
                    f"customer={cid} was assigned multiple arms {arms} — "
                    "sticky-by-customer is broken."
                )
        finally:
            db.close(); engine.dispose()

    def test_different_customers_split_across_arms(self):
        """At 50/50 over 200 customers the split must be ~balanced.

        We assert each arm sees at least 25% of the traffic — anything
        less indicates a degenerate hash, not random variance.
        """
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            op.register_policy("arm-a", self._stub_arm_a)
            op.register_policy("arm-b", self._stub_arm_b)
            _set_policy_config(db, 1, {
                "experiment": {
                    "name": "phase6_split",
                    "sticky_by": "customer_id",
                    "arms": [
                        {"name": "A", "weight": 50, "policy_version": "arm-a"},
                        {"name": "B", "weight": 50, "policy_version": "arm-b"},
                    ],
                },
            })
            counter: Counter[str] = Counter()
            for cid in range(1, 201):
                d = decide(db, _make_ctx(customer_id=cid))
                counter[d.experiment_arm] += 1

            total = sum(counter.values())
            assert total == 200
            for name in ("A", "B"):
                assert counter[name] >= 50, (
                    f"Arm {name!r} only got {counter[name]} / {total} "
                    f"customers — hash distribution is broken."
                )
        finally:
            db.close(); engine.dispose()


# ── 5. Arm overrides top-level version ────────────────────────────────────

class TestArmOverridesVersion:

    def test_arm_policy_version_wins_over_top_level(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            captured: list[str] = []

            def stub_x(db, ctx):
                from uuid import uuid4
                captured.append("X")
                return OfferDecision(
                    decision_id=uuid4().hex, source=SOURCE_NONE,
                    reason_codes=["x"],
                )

            op.register_policy("arm-x", stub_x)
            _set_policy_config(db, 1, {
                "version": "v1.0-deterministic",
                "experiment": {
                    "name": "single-arm",
                    "sticky_by": "customer_id",
                    "arms": [
                        {"name": "ONLY", "weight": 100, "policy_version": "arm-x"},
                    ],
                },
            })

            d = decide(db, _make_ctx(customer_id=999))
            assert d.policy_version == "arm-x"
            assert d.experiment_arm == "ONLY"
            assert captured == ["X"]
        finally:
            db.close(); engine.dispose()


# ── 6. Admin endpoint round-trip ──────────────────────────────────────────

class TestAdminOfferPolicyEndpoints:

    def test_get_returns_default_with_registered_versions(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            res = asyncio.run(admin_router.admin_get_offer_policy(tenant_id=1, db=db))
            assert res["tenant_id"] == 1
            assert res["default_policy_version"] == "v1.0-deterministic"
            assert "v1.0-deterministic" in res["registered_versions"]
            assert res["config"] == {}
        finally:
            db.close(); engine.dispose()

    def test_put_then_get_roundtrip(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            op.register_policy("v2.0-test", lambda db, ctx: OfferDecision(
                decision_id="d", source=SOURCE_NONE,
            ))

            body = admin_router._OfferPolicyConfigBody(
                version="v2.0-test",
                experiment=admin_router._OfferPolicyExperimentBody(
                    name="rollout",
                    sticky_by="customer_id",
                    arms=[
                        admin_router._OfferPolicyArmBody(
                            name="A", weight=70, policy_version="v1.0-deterministic",
                        ),
                        admin_router._OfferPolicyArmBody(
                            name="B", weight=30, policy_version="v2.0-test",
                        ),
                    ],
                ),
            )
            asyncio.run(admin_router.admin_put_offer_policy(
                tenant_id=1, body=body, db=db,
            ))

            res = asyncio.run(admin_router.admin_get_offer_policy(tenant_id=1, db=db))
            assert res["config"]["version"] == "v2.0-test"
            assert res["config"]["experiment"]["name"] == "rollout"
            assert len(res["config"]["experiment"]["arms"]) == 2
        finally:
            db.close(); engine.dispose()

    def test_put_rejects_unknown_top_level_version(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            body = admin_router._OfferPolicyConfigBody(version="vGhost")
            with pytest.raises(HTTPException) as ei:
                asyncio.run(admin_router.admin_put_offer_policy(
                    tenant_id=1, body=body, db=db,
                ))
            assert ei.value.status_code == 400
            assert "Unknown policy version" in str(ei.value.detail)
        finally:
            db.close(); engine.dispose()

    def test_put_rejects_unknown_arm_version(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            body = admin_router._OfferPolicyConfigBody(
                experiment=admin_router._OfferPolicyExperimentBody(
                    name="bad-arm",
                    arms=[
                        admin_router._OfferPolicyArmBody(
                            name="A", weight=100, policy_version="vMissing",
                        ),
                    ],
                ),
            )
            with pytest.raises(HTTPException) as ei:
                asyncio.run(admin_router.admin_put_offer_policy(
                    tenant_id=1, body=body, db=db,
                ))
            assert ei.value.status_code == 400
            assert "unknown" in str(ei.value.detail).lower()
        finally:
            db.close(); engine.dispose()

    def test_empty_config_wipes_offer_policy(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            _set_policy_config(db, 1, {"version": "v1.0-deterministic"})

            # Empty body — no version, no experiment → clears the key.
            asyncio.run(admin_router.admin_put_offer_policy(
                tenant_id=1,
                body=admin_router._OfferPolicyConfigBody(),
                db=db,
            ))
            res = asyncio.run(admin_router.admin_get_offer_policy(tenant_id=1, db=db))
            assert res["config"] == {}
        finally:
            db.close(); engine.dispose()
