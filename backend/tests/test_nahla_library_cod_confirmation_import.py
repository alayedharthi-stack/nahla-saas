"""Regression coverage for the image-backed COD confirmation revision."""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.commerce_lifecycle.cod_confirmation_assets import (  # noqa: E402
    COD_CONFIRMATION_HEADER_DEFAULT_URL,
)
from core.commerce_lifecycle.nahla_library_cod_confirmation_import import (  # noqa: E402
    import_cod_confirmation_from_library,
    is_cod_confirmation_image_contract,
)
from models import WhatsAppTemplate  # noqa: E402
from services.whatsapp_templates.nahla_templates import get_template_by_key  # noqa: E402


def _db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for column in WhatsAppTemplate.__table__.columns:
        if isinstance(column.type, JSONB):
            saved.append((column, column.type))
            column.type = JSON()
    WhatsAppTemplate.__table__.create(engine)
    for column, original in saved:
        column.type = original
    return sessionmaker(bind=engine)()


def _active_cod(db):
    row = WhatsAppTemplate(
        tenant_id=1,
        name="nahla_cod_confirmation_b60e",
        language="ar",
        category="UTILITY",
        status="APPROVED",
        components=[{"type": "BODY", "text": "القالب الحالي {{1}} {{2}} {{3}}"}],
        service_key="cod_confirmation",
        nahla_source_key="cod_confirmation",
        is_active=True,
        is_hidden=False,
        step_number=None,
        revision=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_cod_library_definition_carries_approved_image():
    definition = get_template_by_key("cod_confirmation")
    assert definition["name_ar"] == "تأكيد طلب الدفع عند الاستلام"
    assert is_cod_confirmation_image_contract(definition["components"])
    assert definition["components"][0]["example"]["header_url"] == COD_CONFIRMATION_HEADER_DEFAULT_URL


def test_import_preserves_active_approved_cod_and_creates_inactive_revision():
    db = _db()
    active = _active_cod(db)
    outcome = import_cod_confirmation_from_library(
        db,
        1,
        get_template_by_key("cod_confirmation"),
    )
    draft = outcome["template"]
    db.refresh(active)
    assert active.is_active is True
    assert draft.is_active is False
    assert draft.status == "DRAFT"
    assert draft.nahla_source_key == "cod_confirmation"
    assert draft.revision == 2
    assert draft.supersedes_template_id == active.id
    assert is_cod_confirmation_image_contract(draft.components)


def test_second_import_reuses_same_image_draft():
    db = _db()
    _active_cod(db)
    definition = get_template_by_key("cod_confirmation")
    first = import_cod_confirmation_from_library(db, 1, definition)
    second = import_cod_confirmation_from_library(db, 1, definition)
    assert first["created"] is True
    assert second["created"] is False
    assert second["reused_existing_draft"] is True
    assert second["template"].id == first["template"].id
