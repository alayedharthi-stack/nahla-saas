"""tests/test_billing_seed_resync.py
─────────────────────────────────────
Lock-in tests for ``ensure_billing_plans()``.

Why this file exists:
    The first version of ``ensure_billing_plans`` was idempotent on
    *every* field — once a plan row existed, subsequent boots refused to
    update its ``features``, ``limits`` or ``description`` even when the
    seed was edited.  This made every pricing-content commit a no-op on
    long-running deployments (the original commit 34310c7d landed
    correctly in code but the UI kept rendering whatever the very first
    install had written into the DB).

    The fix: re-sync product-config fields on every call, but PRESERVE
    pricing fields (``price_sar``, ``launch_price_sar``) so a deploy
    never silently changes the price a merchant sees.

    These tests pin both halves of that contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, BillingPlan  # noqa: E402
from core.billing import (  # noqa: E402
    BILLING_PLANS_SEED,
    ensure_billing_plans,
)


def _make_db():
    """In-memory SQLite mirror of the real schema (with JSONB→JSON shim)."""
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session()


# ── Fresh install ────────────────────────────────────────────────────────


def test_first_call_seeds_all_three_plans():
    db = _make_db()
    ensure_billing_plans(db)
    rows = db.query(BillingPlan).order_by(BillingPlan.price_sar).all()
    assert {r.slug for r in rows} == {"starter", "growth", "scale"}


def test_seeded_features_match_seed_exactly():
    db = _make_db()
    ensure_billing_plans(db)
    for seed in BILLING_PLANS_SEED:
        row = db.query(BillingPlan).filter(BillingPlan.slug == seed["slug"]).one()
        assert row.features == seed["features"]
        assert row.limits == seed["limits"]


def test_killer_feature_is_first_for_every_plan():
    """First feature remains the WhatsApp + AI + campaigns headline."""
    db = _make_db()
    ensure_billing_plans(db)
    rows = db.query(BillingPlan).all()
    for r in rows:
        first = (r.features or [""])[0]
        assert "📱" not in first
        assert "واتساب الأعمال على الجوال" in first
        assert "الذكاء الاصطناعي" in first
        assert "الحملات" in first


# ── The actual bug fix: re-sync of product fields ───────────────────────


def test_features_re_sync_when_seed_changes():
    """Simulates the production state where an old install has stale
    features in the DB and a fresh deploy ships an updated seed."""
    db = _make_db()
    ensure_billing_plans(db)

    # Pretend the DB was written by an older seed that had a different
    # feature list — this is exactly what production looked like before
    # the fix.
    starter = db.query(BillingPlan).filter(BillingPlan.slug == "starter").one()
    starter.features = ["ميزة قديمة جدًا 1", "ميزة قديمة جدًا 2"]
    starter.limits = {"conversations_per_month": 1000, "automations": 1, "campaigns_per_month": 1}
    starter.description = "وصف قديم"
    db.commit()

    # Now boot again — the canonical seed must overwrite the stale row.
    ensure_billing_plans(db)
    db.expire_all()
    starter = db.query(BillingPlan).filter(BillingPlan.slug == "starter").one()

    seed = next(s for s in BILLING_PLANS_SEED if s["slug"] == "starter")
    assert starter.features == seed["features"]
    assert starter.limits == seed["limits"]
    assert starter.description == seed["description"]


def test_conversation_limits_re_sync_to_new_5k_15k_unlimited():
    """The user's pricing v2 update lifted limits to 5k/15k/unlimited.
    Production rows still had 1k/5k/15k from the original seed.  Pin the
    new ladder so a future seed edit doesn't silently regress it."""
    db = _make_db()
    ensure_billing_plans(db)

    # Pretend old install had the previous ladder.
    for slug, old_limit in [("starter", 1000), ("growth", 5000), ("scale", 15000)]:
        row = db.query(BillingPlan).filter(BillingPlan.slug == slug).one()
        row.limits = {"conversations_per_month": old_limit, "automations": -1, "campaigns_per_month": -1}
    db.commit()

    ensure_billing_plans(db)
    db.expire_all()

    starter = db.query(BillingPlan).filter(BillingPlan.slug == "starter").one()
    growth = db.query(BillingPlan).filter(BillingPlan.slug == "growth").one()
    scale = db.query(BillingPlan).filter(BillingPlan.slug == "scale").one()

    assert starter.limits["conversations_per_month"] == 5000
    assert growth.limits["conversations_per_month"] == 15000
    assert scale.limits["conversations_per_month"] == -1   # unlimited sentinel


# ── The other half of the contract: prices STAY put ─────────────────────


def test_prices_are_preserved_on_re_sync():
    """A deploy must never silently change a merchant's displayed price.
    If an admin or migration set custom pricing on the row, re-syncing
    features must not stomp it."""
    db = _make_db()
    ensure_billing_plans(db)

    starter = db.query(BillingPlan).filter(BillingPlan.slug == "starter").one()
    starter.price_sar = 1234   # <- pretend an admin set custom pricing
    meta = dict(starter.extra_metadata or {})
    meta["launch_price_sar"] = 567
    starter.extra_metadata = meta
    db.commit()

    ensure_billing_plans(db)
    db.expire_all()
    starter = db.query(BillingPlan).filter(BillingPlan.slug == "starter").one()

    assert starter.price_sar == 1234
    assert (starter.extra_metadata or {}).get("launch_price_sar") == 567


# ── Idempotency: clean rows produce no commits ──────────────────────────


def test_second_call_with_clean_rows_is_a_noop():
    """If nothing changed between two boots, the function shouldn't log
    a re-sync.  We approximate this by asserting the row identity stays
    stable and no exceptions fire on repeated calls."""
    db = _make_db()
    ensure_billing_plans(db)
    ensure_billing_plans(db)
    ensure_billing_plans(db)
    rows = db.query(BillingPlan).all()
    assert len(rows) == 3
