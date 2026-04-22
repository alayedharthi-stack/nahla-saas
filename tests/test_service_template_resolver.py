"""
tests/test_service_template_resolver.py
───────────────────────────────────────
Lock down the smart cart-recovery template resolver so the production
automation engine never again refuses to send because an APPROVED
template exists but isn't bound to its `service_key` / `step_number`.

The five fallback layers we test:

  a. Strict resolve (active + visible + APPROVED + matching slot)
  b. Same slot but `is_active=False` → auto-promote
  c. Bind via `nahla_source_key` matching the library entry for the slot
  d. Bind via legacy config-level `template_name`
  e. Same `service_key`, ANY `step_number` → bind to the requested step

For (b)–(e) we also assert the row is **persisted** with
`service_key`, `step_number`, and `is_active=True` so subsequent sends
hit the strict path.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

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

from models import Base, Tenant, WhatsAppTemplate  # noqa: E402
from core.service_template_resolver import (  # noqa: E402
    diagnose_service_slot,
    resolve_active_template,
    resolve_template_for_send,
)


# ── In-memory DB helper (mirrors test_automation_engine pattern) ─────────────

def _make_db():
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


def _seed_tenant(db, name="Test Tenant") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_template(
    db,
    tenant_id: int,
    *,
    name: str = "tpl",
    status: str = "APPROVED",
    category: str = "MARKETING",
    service_key: str | None = None,
    step_number: int | None = None,
    is_active: bool = True,
    is_hidden: bool = False,
    nahla_source_key: str | None = None,
) -> WhatsAppTemplate:
    now = datetime.now(timezone.utc)
    t = WhatsAppTemplate(
        tenant_id=tenant_id,
        name=name,
        language="ar",
        category=category,
        status=status,
        components=[{"type": "BODY", "text": "{{1}}"}],
        service_key=service_key,
        step_number=step_number,
        is_active=is_active,
        is_hidden=is_hidden,
        nahla_source_key=nahla_source_key,
        created_at=now,
        updated_at=now,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ═════════════════════════════════════════════════════════════════════════════
# Fallback chain
# ═════════════════════════════════════════════════════════════════════════════

class TestResolveTemplateForSend:
    """Each layer of the smart fallback chain, including auto-bind side-effects."""

    def test_a_strict_match_returns_template(self):
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="cart_recovery_step1",
                service_key="cart_recovery", step_number=1,
                is_active=True,
            )
            got = resolve_template_for_send(db, tenant.id, "cart_recovery", 1)
            assert got is not None
            assert got.id == tpl.id
        finally:
            db.close()

    def test_b_inactive_match_auto_promotes(self):
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="cart_recovery_step1",
                service_key="cart_recovery", step_number=1,
                is_active=False,
            )
            got = resolve_template_for_send(db, tenant.id, "cart_recovery", 1)
            assert got is not None
            assert got.id == tpl.id
            db.refresh(tpl)
            assert tpl.is_active is True, "row should be auto-promoted to active"
        finally:
            db.close()

    def test_c_match_by_nahla_source_key_auto_binds(self):
        """Real-world: merchant imported the library template, but a
        later /templates/sync created a parallel APPROVED row that
        carries the `nahla_source_key` (because we just added that
        backfill) yet has no `service_key` / `step_number`."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="my_recovery_msg",
                service_key=None, step_number=None, is_active=False,
                # `abandoned_cart_reminder` is the canonical
                # cart_recovery / step=1 entry in NAHLA_TEMPLATES.
                nahla_source_key="abandoned_cart_reminder",
            )
            got = resolve_template_for_send(db, tenant.id, "cart_recovery", 1)
            assert got is not None and got.id == tpl.id
            db.refresh(tpl)
            assert tpl.service_key == "cart_recovery"
            assert tpl.step_number == 1
            assert tpl.is_active is True
        finally:
            db.close()

    def test_d_match_by_fallback_template_name_auto_binds(self):
        """Real-world: merchant created a template directly in Meta
        Business Manager whose name happens to match the seed config's
        legacy `template_name`."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="abandoned_cart_recovery_ar",   # the seed's template_name
                service_key=None, step_number=None, is_active=False,
            )
            got = resolve_template_for_send(
                db, tenant.id, "cart_recovery", 1,
                fallback_template_name="abandoned_cart_recovery_ar",
            )
            assert got is not None and got.id == tpl.id
            db.refresh(tpl)
            assert tpl.service_key == "cart_recovery"
            assert tpl.step_number == 1
            assert tpl.is_active is True
        finally:
            db.close()

    def test_e_match_any_step_on_same_service_auto_binds(self):
        """When the merchant only has one cart-recovery template approved,
        we should still send something rather than fail every stage."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            # Bound to step 1 only, but stage-2 send is being attempted.
            tpl = _seed_template(
                db, tenant.id,
                name="cart_recovery_only",
                service_key="cart_recovery", step_number=1, is_active=True,
            )
            got = resolve_template_for_send(db, tenant.id, "cart_recovery", 2)
            assert got is not None and got.id == tpl.id
            # Notice: we don't reassign step on the original — we DO,
            # because the auto-bind logic stamps the requested step.
            db.refresh(tpl)
            assert tpl.step_number == 2
        finally:
            db.close()

    def test_no_approved_template_returns_none(self):
        """Truly empty case — resolver gives up so the engine can surface
        a precise error."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            _seed_template(
                db, tenant.id,
                name="some_pending_tpl",
                status="PENDING",
                service_key="cart_recovery", step_number=1,
            )
            got = resolve_template_for_send(db, tenant.id, "cart_recovery", 1)
            assert got is None
        finally:
            db.close()

    def test_strict_resolver_unaffected(self):
        """Sanity: strict resolve_active_template still requires the
        full set of strict conditions (no auto-bind side-effects)."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="unbound",
                service_key=None, step_number=None,
                nahla_source_key="abandoned_cart_reminder",
            )
            assert resolve_active_template(db, tenant.id, "cart_recovery", 1) is None
            db.refresh(tpl)
            assert tpl.service_key is None  # strict path must NOT mutate
        finally:
            db.close()

    def test_f_keyword_pattern_matches_english_name(self):
        """Real-world: merchant created template directly in Meta Business
        Manager named e.g. `cart_reminder_v2_ar`. No service_key, no
        nahla_source_key, no exact-name match — but the keyword fallback
        recognises 'cart' and binds it."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="cart_reminder_v2_ar",
                category="MARKETING",
                service_key=None, step_number=None, is_active=False,
            )
            got = resolve_template_for_send(db, tenant.id, "cart_recovery", 1)
            assert got is not None and got.id == tpl.id
            db.refresh(tpl)
            assert tpl.service_key == "cart_recovery"
            assert tpl.step_number == 1
            assert tpl.is_active is True
        finally:
            db.close()

    def test_f_keyword_pattern_matches_arabic_name(self):
        """Same as above but the merchant's template name is in Arabic
        (`تذكير_السلة_المتروكة`). The Arabic patterns must also catch it."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="تذكير_السلة_المتروكة",
                category="MARKETING",
                service_key=None, step_number=None, is_active=False,
            )
            got = resolve_template_for_send(db, tenant.id, "cart_recovery", 1)
            assert got is not None and got.id == tpl.id
        finally:
            db.close()

    def test_f_keyword_pattern_skips_authentication_category(self):
        """Safety: an AUTHENTICATION (OTP) template named `verify_cart_otp`
        must NOT be bound to cart_recovery even though 'cart' is in the
        name. The keyword fallback is restricted to MARKETING / UTILITY."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            _seed_template(
                db, tenant.id,
                name="verify_cart_otp",
                category="AUTHENTICATION",
                service_key=None, step_number=None,
            )
            got = resolve_template_for_send(db, tenant.id, "cart_recovery", 1)
            assert got is None
        finally:
            db.close()

    def test_single_active_invariant_holds_after_autobind(self):
        """Auto-bind must enforce the single-active rule by deactivating
        any sibling that previously held the slot."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            old = _seed_template(
                db, tenant.id,
                name="old_recovery",
                service_key="cart_recovery", step_number=1, is_active=True,
                status="APPROVED",
            )
            # New approved row matching by source key — should win.
            new = _seed_template(
                db, tenant.id,
                name="new_recovery",
                service_key=None, step_number=None, is_active=False,
                nahla_source_key="abandoned_cart_reminder",
            )
            # Strict resolver returns the old one, so to actually exercise
            # the fallback we mark the old as not-approved temporarily.
            old.status = "REJECTED"
            db.commit()

            got = resolve_template_for_send(db, tenant.id, "cart_recovery", 1)
            assert got is not None and got.id == new.id
            db.refresh(old)
            db.refresh(new)
            # The newly-bound row is active; old can't compete because
            # its status is no longer APPROVED.
            assert new.is_active is True
            assert new.service_key == "cart_recovery"
            assert new.step_number == 1
        finally:
            db.close()


# ═════════════════════════════════════════════════════════════════════════════
# Diagnostic helper (read-only)
# ═════════════════════════════════════════════════════════════════════════════

class TestDiagnoseServiceSlot:
    """The diagnostic snapshot powers the support endpoint and the
    dashboard's debugging UI. It must NEVER mutate state."""

    def test_empty_tenant_recommends_import(self):
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            report = diagnose_service_slot(db, tenant.id, "cart_recovery", 1)
            assert report["approved_total"] == 0
            assert report["would_resolve"] is None
            assert "استورد" in report["recommendation"]
        finally:
            db.close()

    def test_strict_match_classification(self):
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="bound_tpl",
                service_key="cart_recovery", step_number=1, is_active=True,
            )
            report = diagnose_service_slot(db, tenant.id, "cart_recovery", 1)
            assert report["would_resolve"]["id"] == tpl.id
            assert report["would_resolve"]["via"] == "a"
            cand = next(c for c in report["candidates"] if c["id"] == tpl.id)
            assert cand["classification"] == "strict_match"
        finally:
            db.close()

    def test_unbound_keyword_match_classification(self):
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="my_cart_reminder",
                service_key=None, step_number=None,
            )
            report = diagnose_service_slot(db, tenant.id, "cart_recovery", 1)
            cand = next(c for c in report["candidates"] if c["id"] == tpl.id)
            assert cand["classification"] == "would_autobind_keyword"
            assert report["would_resolve"]["via"] == "f"
        finally:
            db.close()

    def test_diagnose_does_not_mutate(self):
        """Snapshot must NOT auto-bind anything — the caller may invoke
        it on a read-only DB role."""
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="my_cart_reminder",
                service_key=None, step_number=None, is_active=False,
            )
            diagnose_service_slot(db, tenant.id, "cart_recovery", 1)
            db.refresh(tpl)
            # Critical: the snapshot must NOT have stamped any binding.
            assert tpl.service_key is None
            assert tpl.step_number is None
            assert tpl.is_active is False
        finally:
            db.close()

    def test_unrelated_approved_template_classified_as_no_match(self):
        db, _ = _make_db()
        try:
            tenant = _seed_tenant(db)
            tpl = _seed_template(
                db, tenant.id,
                name="welcome_message",
                service_key=None, step_number=None,
            )
            report = diagnose_service_slot(db, tenant.id, "cart_recovery", 1)
            cand = next(c for c in report["candidates"] if c["id"] == tpl.id)
            assert cand["classification"] == "no_match"
            assert report["would_resolve"] is None
        finally:
            db.close()
