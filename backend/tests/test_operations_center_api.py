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

from models import Base, BranchContact, BranchEscalationStep, MerchantBranch, Tenant  # noqa: E402
from routers.operations_center import (  # noqa: E402
    EscalationReorderIn,
    _normalize_phone_field,
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
