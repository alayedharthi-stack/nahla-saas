"""
tests/test_platform_vs_merchant_routing.py
─────────────────────────────────────────
Locks down the platform-vs-merchant routing decision in the WhatsApp
inbound webhook.

Background
──────────
Production was sending Nahla's own platform sales-bot replies
("ممتاز! سجّل الحين وابدأ تجربتك المجانية…") to a real merchant's
customers, because the router used to gate the decision on a hard-
coded `PLATFORM_TENANT_ID = 1`. The merchant had connected its
WhatsApp number on tenant_id=1 (the first tenant ever created in the
DB), so its inbound messages were misclassified as platform traffic.

The new contract is:

  * The platform-vs-merchant decision is data-driven, controlled by
    `tenants.is_platform_tenant`.
  * `_is_platform_tenant(db, tenant_id)` returns True ONLY for the
    tenant that has the flag set to True.
  * If no tenant has the flag set, the resolver returns False for
    every input — meaning every store routes to the merchant AI, which
    is the safe default for any production environment that hasn't
    explicitly opted into the platform sales-bot workspace.
  * The resolver is cached per process, but the cache can be reset for
    tests (`_reset_platform_tenant_cache()`).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from models import Base, Tenant  # noqa: E402
from routers import whatsapp_webhook as wh  # noqa: E402


def _make_db() -> tuple[Any, Any]:
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


def _seed(db, *, tid: int, is_platform: bool = False) -> Tenant:
    t = Tenant(id=tid, name=f"T{tid}", is_active=True,
               is_platform_tenant=is_platform)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ─── 1. The default — nobody flagged → everyone is a merchant ────────────────

class TestDefaultRoutesEverythingToMerchant:
    def test_no_platform_tenant_means_tenant_1_is_merchant(self) -> None:
        wh._reset_platform_tenant_cache()
        db, engine = _make_db()
        try:
            _seed(db, tid=1, is_platform=False)
            _seed(db, tid=2, is_platform=False)
            _seed(db, tid=3, is_platform=False)
            for tid in (1, 2, 3, 99, None):
                assert wh._is_platform_tenant(db, tid) is False, (
                    f"tenant_id={tid} was wrongly classified as platform "
                    "even though no tenant has the flag set"
                )
        finally:
            db.close()
            engine.dispose()


# ─── 2. Exactly one tenant flagged → only that one is platform ───────────────

class TestExplicitPlatformFlag:
    def test_only_flagged_tenant_is_platform(self) -> None:
        wh._reset_platform_tenant_cache()
        db, engine = _make_db()
        try:
            _seed(db, tid=1, is_platform=False)         # real merchant on id=1
            _seed(db, tid=42, is_platform=True)         # explicit platform tenant
            _seed(db, tid=7,  is_platform=False)
            assert wh._is_platform_tenant(db, 42) is True
            assert wh._is_platform_tenant(db, 1) is False, (
                "tenant_id=1 must NOT be classified as platform when the "
                "platform flag lives on a different tenant"
            )
            assert wh._is_platform_tenant(db, 7) is False
            assert wh._is_platform_tenant(db, 999) is False
            assert wh._is_platform_tenant(db, None) is False
        finally:
            db.close()
            engine.dispose()


# ─── 3. Resolver is cached per process ───────────────────────────────────────

class TestResolverIsCached:
    def test_lookup_runs_once_then_uses_cache(self, monkeypatch) -> None:
        wh._reset_platform_tenant_cache()
        db, engine = _make_db()
        try:
            _seed(db, tid=42, is_platform=True)

            calls = {"n": 0}
            real_query = db.query
            def counting_query(*a, **kw):
                calls["n"] += 1
                return real_query(*a, **kw)
            db.query = counting_query  # type: ignore[assignment]

            for _ in range(50):
                wh._is_platform_tenant(db, 1)
                wh._is_platform_tenant(db, 42)
                wh._is_platform_tenant(db, 7)

            assert calls["n"] <= 1, (
                f"resolver hit the DB {calls['n']} times — should be cached "
                "after first call"
            )
        finally:
            db.close()
            engine.dispose()


# ─── 4. Pre-migration safety net — column missing → everyone is merchant ─────

class TestFailsSafeWhenColumnMissing:
    def test_db_error_during_lookup_does_not_classify_as_platform(self) -> None:
        """If the migration hasn't run yet (or the query throws for any
        reason) we must NOT default-promote tenant_id=1 to platform —
        the merchant routing is the safe path."""
        wh._reset_platform_tenant_cache()

        class _BoomSession:
            def query(self, *_a, **_kw):
                raise RuntimeError("simulated: column does not exist yet")

        for tid in (1, 2, 99, None):
            assert wh._is_platform_tenant(_BoomSession(), tid) is False
