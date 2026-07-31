"""Slice B — order updates revisions, enable flags, promote-on-approved."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Tuple

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.order_updates import (  # noqa: E402
    create_revision_from_active,
    get_order_update_flags,
    is_order_update_enabled,
    promote_approved_revision,
    resolve_active_and_pending,
    set_order_update_flags,
)
from models import TenantSettings, WhatsAppTemplate  # noqa: E402


def _make_db(*models) -> Tuple[Any, Any]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for model in models:
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine, checkfirst=True)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)(), engine


def _seed_approved(db, *, tenant_id: int = 1, service_key: str = "order_confirmation") -> WhatsAppTemplate:
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name=f"nahla_{service_key}_live",
        language="ar",
        category="UTILITY",
        status="APPROVED",
        components=[{"type": "BODY", "text": "مرحبا {{1}} طلب {{2}}"}],
        service_key=service_key,
        is_active=True,
        is_hidden=False,
        step_number=None,
        revision=1,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


class TestOrderUpdateFlags:
    def test_default_enabled_and_toggle(self):
        db, _ = _make_db(TenantSettings)
        assert is_order_update_enabled(db, 1, "order_confirmation") is True
        set_order_update_flags(db, 1, {"order_confirmation": False}, commit=True)
        assert is_order_update_enabled(db, 1, "order_confirmation") is False
        assert is_order_update_enabled(db, 1, "shipping_tracking") is True
        flags = get_order_update_flags(db, 1)
        assert flags["order_confirmation"] is False


class TestRevisionChain:
    def test_draft_keeps_prior_approved_active(self):
        db, _ = _make_db(WhatsAppTemplate, TenantSettings)
        active = _seed_approved(db)
        draft = create_revision_from_active(
            db,
            1,
            "order_confirmation",
            "نص جديد {{1}} {{2}}",
            commit=True,
        )
        db.refresh(active)
        assert draft.status == "DRAFT"
        assert draft.is_active is False
        assert draft.supersedes_template_id == active.id
        assert int(draft.revision) == 2
        assert active.is_active is True
        assert active.status == "APPROVED"
        snap = resolve_active_and_pending(db, 1, "order_confirmation")
        assert snap["active"]["id"] == active.id
        assert snap["pending"]["id"] == draft.id

    def test_promote_switches_atomically(self):
        db, _ = _make_db(WhatsAppTemplate, TenantSettings)
        active = _seed_approved(db)
        draft = create_revision_from_active(
            db,
            1,
            "order_confirmation",
            "نسخة معتمدة لاحقاً {{1}} {{2}}",
            commit=True,
        )
        draft.status = "APPROVED"
        db.commit()
        assert promote_approved_revision(db, tenant_id=1, template_id=draft.id, commit=True) is True
        db.refresh(active)
        db.refresh(draft)
        assert draft.is_active is True
        assert active.is_active is False
        snap = resolve_active_and_pending(db, 1, "order_confirmation")
        assert snap["active"]["id"] == draft.id
