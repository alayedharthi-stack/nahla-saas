"""Operations Center API tests (PR-B)."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from models import (  # noqa: E402
    Base,
    BranchArrivalKeyword,
    BranchContact,
    BranchEscalationStep,
    MerchantBranch,
    Tenant,
)
from routers.operations_center import (  # noqa: E402
    ArrivalKeywordCreateIn,
    EscalationReorderIn,
    TriggerPreviewIn,
    _normalize_phone_field,
    create_arrival_keyword,
    list_arrival_keywords,
    preview_branch_trigger,
    reorder_escalation_steps,
)


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


class _FakeRequest:
    pass


@pytest.fixture()
def db_session():
    db, engine = _make_db()
    tenant = Tenant(id=7, name="T7", is_active=True)
    db.add(tenant)
    db.commit()
    try:
        yield db, tenant.id
    finally:
        db.close()
        engine.dispose()


def test_normalize_phone_accepts_saudi_local() -> None:
    assert _normalize_phone_field("0501234567", field="phone_e164").startswith("+")


def test_normalize_phone_rejects_garbage() -> None:
    with pytest.raises(HTTPException) as exc:
        _normalize_phone_field("abc", field="phone_e164")
    assert exc.value.status_code == 422


def test_reorder_escalation_steps(db_session) -> None:
    db, tenant_id = db_session
    branch = MerchantBranch(tenant_id=tenant_id, name="Main", is_active=True)
    db.add(branch)
    db.commit()
    db.refresh(branch)

    s1 = BranchEscalationStep(
        branch_id=branch.id, escalation_level=1, display_name="A",
        phone_e164="+966511111111", sort_order=0,
    )
    s2 = BranchEscalationStep(
        branch_id=branch.id, escalation_level=2, display_name="B",
        phone_e164="+966522222222", sort_order=1,
    )
    db.add_all([s1, s2])
    db.commit()
    db.refresh(s1)
    db.refresh(s2)

    from unittest.mock import patch

    async def _run():
        with patch("routers.operations_center.resolve_tenant_id", return_value=tenant_id):
            return await reorder_escalation_steps(
                branch.id,
                EscalationReorderIn(step_ids=[s2.id, s1.id]),
                _FakeRequest(),
                db,
            )

    result = asyncio.run(_run())

    levels = [s["escalation_level"] for s in result["steps"]]
    names = [s["display_name"] for s in result["steps"]]
    assert levels == [1, 2]
    assert names == ["B", "A"]


def test_default_reception_column_on_contact(db_session) -> None:
    db, tenant_id = db_session
    branch = MerchantBranch(tenant_id=tenant_id, name="Showroom", is_active=True)
    db.add(branch)
    db.commit()
    db.refresh(branch)

    c1 = BranchContact(
        branch_id=branch.id,
        display_name="Reception",
        phone_e164="+966533333333",
        is_default_reception=True,
    )
    db.add(c1)
    db.commit()
    row = db.query(BranchContact).first()
    assert bool(row.is_default_reception) is True


def test_escalation_level_links_contacts(db_session) -> None:
    db, tenant_id = db_session
    branch = MerchantBranch(tenant_id=tenant_id, name="Main", is_active=True)
    db.add(branch)
    db.commit()
    db.refresh(branch)

    c1 = BranchContact(
        branch_id=branch.id,
        display_name="أمين",
        role="showroom",
        phone_e164="+966511111111",
        is_active=True,
    )
    c2 = BranchContact(
        branch_id=branch.id,
        display_name="هشام",
        role="customer_service",
        phone_e164="+966522222222",
        is_active=True,
    )
    db.add_all([c1, c2])
    db.commit()
    db.refresh(c1)
    db.refresh(c2)

    from unittest.mock import patch

    from routers.operations_center import EscalationLevelUpsertIn, create_escalation_level

    async def _run():
        with patch("routers.operations_center.resolve_tenant_id", return_value=tenant_id):
            return await create_escalation_level(
                branch.id,
                EscalationLevelUpsertIn(contact_ids=[c1.id]),
                _FakeRequest(),
                db,
            )

    level1 = asyncio.run(_run())
    assert level1["escalation_level"] == 1
    assert level1["contact_ids"] == [c1.id]
    assert level1["contacts"][0]["display_name"] == "أمين"

    async def _run_level2():
        with patch("routers.operations_center.resolve_tenant_id", return_value=tenant_id):
            return await create_escalation_level(
                branch.id,
                EscalationLevelUpsertIn(contact_ids=[c1.id, c2.id]),
                _FakeRequest(),
                db,
            )

    level2 = asyncio.run(_run_level2())
    assert level2["escalation_level"] == 2
    assert level2["contact_ids"] == [c1.id, c2.id]

    steps = db.query(BranchEscalationStep).order_by(
        BranchEscalationStep.escalation_level.asc(),
        BranchEscalationStep.sort_order.asc(),
    ).all()
    assert len(steps) == 3
    assert all(step.contact_id for step in steps)
    assert steps[0].display_name == "أمين"


def test_arrival_keyword_crud_and_preview(db_session) -> None:
    db, tenant_id = db_session
    branch = MerchantBranch(
        tenant_id=tenant_id,
        name="Main",
        is_active=True,
        maps_url="https://maps.google.com/?q=test",
        location_response_mode="location_plus_reception",
    )
    db.add(branch)
    db.commit()
    db.refresh(branch)

    c1 = BranchContact(
        branch_id=branch.id,
        display_name="استقبال",
        role="reception",
        phone_e164="+966511111111",
        is_default_reception=True,
        is_active=True,
    )
    db.add(c1)
    db.commit()

    from modules.operations.branch_arrival_keyword_evidence import (  # noqa: E402
        seed_default_keywords_for_branch,
    )
    seed_default_keywords_for_branch(db, branch.id)
    db.commit()

    from unittest.mock import patch

    async def _create_kw():
        with patch("routers.operations_center.resolve_tenant_id", return_value=tenant_id):
            return await create_arrival_keyword(
                branch.id,
                ArrivalKeywordCreateIn(phrase="الحوش", trigger_type="arrival_confirmed"),
                _FakeRequest(),
                db,
            )

    kw = asyncio.run(_create_kw())
    assert kw["phrase"] == "الحوش"
    assert kw["trigger_type"] == "arrival_confirmed"

    async def _list_kw():
        with patch("routers.operations_center.resolve_tenant_id", return_value=tenant_id):
            return await list_arrival_keywords(branch.id, _FakeRequest(), db)

    listed = asyncio.run(_list_kw())
    assert len(listed["keywords"]) >= 2

    async def _preview():
        with patch("routers.operations_center.resolve_tenant_id", return_value=tenant_id):
            return await preview_branch_trigger(
                branch.id,
                TriggerPreviewIn(message="وين موقعكم؟"),
                _FakeRequest(),
                db,
            )

    preview = asyncio.run(_preview())
    assert preview["matched"] is True
    assert preview["trigger_type"] == "location_request"
    action_types = {a["type"] for a in preview["actions"]}
    assert "maps_cta" in action_types
    assert "reception_vcard" in action_types

    row = db.query(BranchArrivalKeyword).filter(
        BranchArrivalKeyword.phrase == "الحوش",
    ).first()
    assert row is not None
