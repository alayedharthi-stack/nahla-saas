"""
tests/test_campaign_launch_idempotency.py
─────────────────────────────────────────
Locks the ``idempotency_key`` replay guard on ``POST /campaigns``.

Production scenario
───────────────────
1. Merchant opens the wizard, picks a segment of ~8000 customers,
   clicks "إطلاق الحملة الآن".
2. The frontend's 25 s ``AbortSignal.timeout`` fires before the
   backend's response is flushed back through nginx (rare since
   dispatch moved to a background thread, but possible on slow
   DB / entitlement checks).
3. The wizard surfaces "signal timed out". The merchant — not
   knowing the campaign was actually created and is happily
   dispatching — clicks "إطلاق الحملة الآن" again.

Without the replay guard:
  → A second Campaign row is created.
  → A second background dispatch is spawned.
  → The 14-day frequency cap protects MOST customers, but any
    customer whose first row is still in ``queued`` / ``sending``
    (i.e. not yet marked ``sent`` when the second dispatch
    snapshots) can receive the message twice.

With the replay guard:
  → The second POST short-circuits to the existing campaign
    via the ``idempotency_key`` (UUID per wizard session).
  → No second campaign row, no second dispatch, zero risk of
    duplicate sends, regardless of dispatch state.

We test the dedup logic at the router-handler level with a
real-ish FastAPI request stack so we cover the actual replay path
including the bypass of dispatch spawning.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Campaign, Tenant, WhatsAppTemplate  # noqa: E402


def _make_db():
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


def _seed_tenant_and_template(db):
    t = Tenant(name="T", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    tpl = WhatsAppTemplate(
        tenant_id=t.id,
        name="tpl_idem",
        language="ar",
        category="MARKETING",
        status="APPROVED",
        components=[{"type": "BODY", "text": "hi {{1}}"}],
    )
    db.add(tpl); db.commit(); db.refresh(tpl)
    return t, tpl


def _mk_request(tenant_id: int):
    """Minimal Request stub that satisfies ``resolve_tenant_id``."""
    req = MagicMock()
    req.headers = {"X-Tenant-ID": str(tenant_id)}
    req.state = MagicMock()
    req.state.tenant_id = tenant_id
    req.scope = {"headers": [(b"x-tenant-id", str(tenant_id).encode())]}
    return req


def _call_create(db, body, tenant_id):
    """Drive the router handler directly so we exercise the real
    idempotency-replay branch — including the ``_spawn_dispatch_in_background``
    short-circuit when a replay is detected."""
    import asyncio

    from routers import campaigns as campaigns_mod

    req = _mk_request(tenant_id)
    # Stub out every external dependency the handler reaches for
    # (auth, billing, entitlements, dispatch). We only care about
    # the dedup branch.
    with patch.object(
        campaigns_mod, "resolve_tenant_id", return_value=tenant_id
    ), patch.object(
        campaigns_mod, "get_or_create_tenant"
    ), patch(
        "core.billing.require_outbound_access"
    ), patch(
        "core.plan_entitlements.get_entitlements", return_value={}
    ), patch(
        "core.plan_entitlements.require_feature"
    ), patch(
        "core.plan_entitlements.require_limit_not_exceeded"
    ), patch.object(
        campaigns_mod, "_spawn_dispatch_in_background"
    ) as spawn_mock:
        result = asyncio.run(
            campaigns_mod.create_campaign(body=body, request=req, db=db)
        )
    return result, spawn_mock


# ──────────────────────────────────────────────────────────────────────


class TestIdempotencyReplay:
    """A second POST with the SAME idempotency_key in the dedup
    window must return the existing campaign and NOT spawn a
    second dispatch."""

    def _payload(self, tpl_id: int, key: str | None):
        from routers.campaigns import CreateCampaignIn
        return CreateCampaignIn(
            name="Campaign A",
            campaign_type="broadcast",
            template_id=str(tpl_id),
            template_name="tpl_idem",
            template_language="ar",
            template_category="MARKETING",
            template_body="hi {{1}}",
            template_variables={},
            audience_type="all",
            audience_count=8000,
            schedule_type="immediate",
            coupon_code="",
            idempotency_key=key,
        )

    def test_second_post_with_same_key_returns_existing_campaign(self):
        db = _make_db()
        t, tpl = _seed_tenant_and_template(db)
        key = "11111111-2222-3333-4444-555555555555"

        first, spawn_first = _call_create(db, self._payload(tpl.id, key), t.id)
        first_id = first["id"]
        assert spawn_first.call_count == 1, (
            "first POST must spawn exactly one dispatch"
        )

        second, spawn_second = _call_create(db, self._payload(tpl.id, key), t.id)
        assert second["id"] == first_id, (
            f"replay must return the same campaign_id; got {second['id']} "
            f"vs {first_id} — the second click would have created a new "
            f"campaign + a second dispatch (production duplicate-send risk)"
        )
        assert spawn_second.call_count == 0, (
            "replay MUST NOT spawn a second dispatch — duplicate sends "
            "to the same audience are the exact bug this guard prevents"
        )

        # Only one row in the DB.
        rows = db.query(Campaign).filter(Campaign.tenant_id == t.id).all()
        assert len(rows) == 1, (
            f"expected 1 Campaign row after the replay, found {len(rows)}: "
            f"{[(r.id, r.name) for r in rows]}"
        )

    def test_different_key_creates_separate_campaign(self):
        """Two different wizard sessions (two different UUIDs) must
        produce two campaigns — the guard MUST NOT cross-dedupe."""
        db = _make_db()
        t, tpl = _seed_tenant_and_template(db)

        first, _  = _call_create(db, self._payload(tpl.id, "key-A"), t.id)
        second, _ = _call_create(db, self._payload(tpl.id, "key-B"), t.id)
        assert second["id"] != first["id"], (
            "different idempotency keys must yield different campaign_ids"
        )
        assert (
            db.query(Campaign).filter(Campaign.tenant_id == t.id).count() == 2
        )

    def test_missing_key_does_not_dedupe(self):
        """Legacy clients that don't send a key must keep working —
        every POST creates a fresh campaign (existing behaviour)."""
        db = _make_db()
        t, tpl = _seed_tenant_and_template(db)

        first, _  = _call_create(db, self._payload(tpl.id, None), t.id)
        second, _ = _call_create(db, self._payload(tpl.id, None), t.id)
        assert second["id"] != first["id"], (
            "no idempotency_key → no dedup; got the same id back which "
            "would silently break legacy retries that *want* a fresh row"
        )

    def test_key_is_tenant_scoped(self):
        """Two different tenants reusing the same UUID must never
        collide — the dedup is per-tenant, not global."""
        db = _make_db()
        t1, tpl1 = _seed_tenant_and_template(db)
        # second tenant + their own template
        t2 = Tenant(name="T2", is_active=True)
        db.add(t2); db.commit(); db.refresh(t2)
        tpl2 = WhatsAppTemplate(
            tenant_id=t2.id, name="tpl_idem", language="ar",
            category="MARKETING", status="APPROVED",
            components=[{"type": "BODY", "text": "hi"}],
        )
        db.add(tpl2); db.commit(); db.refresh(tpl2)

        key = "shared-uuid"
        c1, _ = _call_create(db, self._payload(tpl1.id, key), t1.id)
        c2, _ = _call_create(db, self._payload(tpl2.id, key), t2.id)
        assert c1["id"] != c2["id"], (
            "tenants must never share an idempotency namespace — "
            "tenant B's retry must NOT return tenant A's campaign"
        )

    def test_stamped_key_persists_on_campaign(self):
        """Support needs to correlate a frontend UUID with a
        server-side campaign_id from logs — the key must be on
        the row itself, not just in transit."""
        db = _make_db()
        t, tpl = _seed_tenant_and_template(db)
        key = "audit-trail-key"
        c, _ = _call_create(db, self._payload(tpl.id, key), t.id)
        row = db.query(Campaign).filter(Campaign.id == c["id"]).first()
        assert row is not None
        assert isinstance(row.template_variables, dict)
        assert row.template_variables.get("_idempotency_key") == key, (
            "idempotency_key must be persisted on the row so support "
            "can audit a 'merchant clicked twice' report"
        )
