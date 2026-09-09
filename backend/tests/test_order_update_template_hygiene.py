"""Hygiene for order-update WhatsApp template inventory."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Tuple

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.order_update_template_hygiene import (  # noqa: E402
    ARCHIVE_SUPERSEDED,
    apply_order_update_template_hygiene,
    archive_state,
    build_order_update_inventory,
    choose_official_template_ids,
    cluster_inventory,
    is_nahla_managed_template,
)
from core.commerce_lifecycle.order_updates import promote_approved_revision  # noqa: E402
from models import WhatsAppTemplate  # noqa: E402


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


def _tpl(
    db,
    *,
    tpl_id: int,
    name: str,
    service_key: str,
    status: str = "APPROVED",
    is_active: bool = False,
    revision: int = 1,
    supersedes_template_id: int | None = None,
    url: str = "https://example.com/{{1}}",
    source: str = "nahla_library",
) -> WhatsAppTemplate:
    row = WhatsAppTemplate(
        id=tpl_id,
        tenant_id=1,
        name=name,
        language="ar",
        category="UTILITY",
        status=status,
        components=[
            {
                "type": "BODY",
                "text": "مرحبا {{1}} طلب {{2}} مبلغ {{3}}",
            },
            {
                "type": "BUTTONS",
                "buttons": [{"type": "URL", "text": "تفاصيل", "url": url}],
            },
        ],
        service_key=service_key,
        nahla_source_key=service_key,
        source=source,
        is_active=is_active,
        is_hidden=False,
        revision=revision,
        supersedes_template_id=supersedes_template_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestOrderUpdateTemplateHygiene:
    def test_inventory_and_cluster_duplicates(self):
        db, _ = _make_db(WhatsAppTemplate)
        _tpl(db, tpl_id=198, name="nahla_order_summary_8d9f", service_key="order_confirmation", is_active=True)
        _tpl(db, tpl_id=233, name="nahla_order_summary_ca72", service_key="order_confirmation")
        inventory = build_order_update_inventory(db, 1)
        assert len(inventory) == 2
        clusters = cluster_inventory(inventory)
        assert clusters[0]["count"] == 2

    def test_hygiene_archives_duplicates_and_keeps_official(self):
        db, _ = _make_db(WhatsAppTemplate)
        old = _tpl(
            db,
            tpl_id=198,
            name="nahla_order_summary_8d9f",
            service_key="order_confirmation",
            is_active=True,
        )
        new = _tpl(
            db,
            tpl_id=432,
            name="nahla_order_confirmation_r2",
            service_key="order_confirmation",
            status="APPROVED",
            revision=2,
            supersedes_template_id=198,
            url="https://mtjr.at/{{1}}",
        )
        promote_approved_revision(db, tenant_id=1, template_id=new.id, commit=True)
        result = apply_order_update_template_hygiene(
            db,
            1,
            preferred_official_ids={"order_confirmation": 432},
            dry_run=False,
        )
        db.commit()
        db.refresh(old)
        db.refresh(new)
        assert new.is_active is True
        assert new.is_hidden is False
        assert old.is_active is False
        assert old.is_hidden is True
        assert archive_state(old) == ARCHIVE_SUPERSEDED
        assert 432 in result["visible_template_ids"]
        assert 198 in result["hidden_template_ids"]

    def test_pending_preferred_does_not_replace_active_until_approved(self):
        db, _ = _make_db(WhatsAppTemplate)
        _tpl(db, tpl_id=198, name="nahla_order_summary_8d9f", service_key="order_confirmation", is_active=True)
        _tpl(
            db,
            tpl_id=432,
            name="nahla_order_confirmation_r2",
            service_key="order_confirmation",
            status="PENDING",
            revision=2,
            supersedes_template_id=198,
            url="https://mtjr.at/{{1}}",
        )
        official = choose_official_template_ids(
            build_order_update_inventory(db, 1),
            preferred_ids={"order_confirmation": 432},
        )
        assert official[("order_confirmation", "ar")] == 198

    def test_pending_revision_not_archived(self):
        db, _ = _make_db(WhatsAppTemplate)
        _tpl(db, tpl_id=198, name="nahla_order_summary_8d9f", service_key="order_confirmation", is_active=True)
        pending = _tpl(
            db,
            tpl_id=432,
            name="nahla_order_confirmation_r2",
            service_key="order_confirmation",
            status="PENDING",
            revision=2,
            supersedes_template_id=198,
            url="https://mtjr.at/{{1}}",
        )
        result = apply_order_update_template_hygiene(db, 1, dry_run=False)
        db.commit()
        db.refresh(pending)
        assert pending.is_hidden is False
        assert 432 not in result["hidden_template_ids"]

        db, _ = _make_db(WhatsAppTemplate)
        custom = _tpl(
            db,
            tpl_id=501,
            name="merchant_custom_confirmation",
            service_key="order_confirmation",
            is_active=False,
            source="merchant",
        )
        _tpl(db, tpl_id=198, name="nahla_order_summary_8d9f", service_key="order_confirmation", is_active=True)
        result = apply_order_update_template_hygiene(db, 1, dry_run=False)
        db.commit()
        db.refresh(custom)
        assert custom.is_hidden is False
        assert 501 not in result["hidden_template_ids"]

    def test_merchant_custom_detection(self):
        db, _ = _make_db(WhatsAppTemplate)
        custom = _tpl(
            db,
            tpl_id=501,
            name="merchant_custom_confirmation",
            service_key="order_confirmation",
            source="merchant",
        )
        nahla = _tpl(db, tpl_id=198, name="nahla_order_summary_8d9f", service_key="order_confirmation")
        assert is_nahla_managed_template(custom) is False
        assert is_nahla_managed_template(nahla) is True
