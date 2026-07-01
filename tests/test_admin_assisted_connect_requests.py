"""Admin queue must surface merchant assisted-connect requests."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from models import Base, Tenant, User, WhatsAppConnection  # noqa: E402
from routers import admin as admin_router  # noqa: E402
from routers import whatsapp_connect as wa_router  # noqa: E402


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session()


def _seed_tenant(db, *, tenant_id=None, name="Store 49", email=None):
    tenant = Tenant(name=name, is_active=True)
    if tenant_id is not None:
        tenant.id = tenant_id
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    user = User(
        tenant_id=tenant.id,
        username=f"user-{tenant.id}",
        email=email or f"merchant{tenant.id}@example.com",
        password_hash="x",
        role="merchant",
    )
    db.add(user)
    db.commit()
    return tenant, user


def test_assisted_request_appears_in_admin_coexistence_queue():
    db = _make_db()
    tenant, _user = _seed_tenant(db, name="Tenant Forty Nine", email="merchant49@example.com")

    request = MagicMock()
    request.state = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wa_router, "resolve_tenant_id", lambda _req: tenant.id)
        body = wa_router.AssistedConnectRequestIn(
            contact_phone="0549815590",
            display_name="متجر الاختبار",
            notes="Need help linking WhatsApp",
        )
        result = asyncio.run(wa_router.request_assisted_connect(body, request, db))

    assert result["status"] == "request_submitted"
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant.id).one()
    assert conn.connection_type == "assisted"
    assert conn.status == "request_submitted"

    admin = {"role": "admin", "sub": "admin@test"}
    payload = asyncio.run(
        admin_router.admin_list_coexistence_requests(
            status_filter="request_submitted",
            db=db,
            _admin=admin,
        )
    )
    assert payload["total"] >= 1
    match = next(r for r in payload["requests"] if r["tenant_id"] == tenant.id)
    assert match["request_kind"] == "assisted_connect"
    assert match["request_id"] == conn.id
    assert match["requested_phone"] == "0549815590"
    assert match["display_name"] == "متجر الاختبار"
    assert match["notes"] == "Need help linking WhatsApp"
    assert match["merchant_email"] == "merchant49@example.com"
    assert "access_token" not in match
    assert match["has_api_key"] is False


def test_coexistence_and_assisted_both_listed_without_cross_tenant_bleed():
    db = _make_db()
    t_assisted, _ = _seed_tenant(db, name="Assisted Store", email="assisted@example.com")
    t_coex, _ = _seed_tenant(db, name="Coex Store", email="coex@example.com")

    db.add(WhatsAppConnection(
        tenant_id=t_coex.id,
        status="request_submitted",
        connection_type="coexistence",
        provider="dialog360",
        phone_number="+966500000001",
        last_attempt_at=None,
        extra_metadata={
            "coexistence": {
                "request": {
                    "phone_number": "+966500000001",
                    "display_name": "Coex Biz",
                    "submitted_at": "2026-07-01T12:00:00+00:00",
                },
            },
        },
    ))
    db.add(WhatsAppConnection(
        tenant_id=t_assisted.id,
        status="request_submitted",
        connection_type="assisted",
        provider="meta",
        phone_number="+966500000002",
        extra_metadata={
            "assisted_connect": {
                "status": "request_submitted",
                "request": {
                    "contact_phone": "+966500000002",
                    "display_name": "Assisted Biz",
                    "submitted_at": "2026-07-01T12:30:00+00:00",
                },
            },
        },
    ))
    db.commit()

    payload = asyncio.run(
        admin_router.admin_list_coexistence_requests(
            status_filter="request_submitted",
            db=db,
            _admin={"role": "admin"},
        )
    )
    kinds = {r["tenant_id"]: r["request_kind"] for r in payload["requests"]}
    assert kinds[t_assisted.id] == "assisted_connect"
    assert kinds[t_coex.id] == "coexistence"
    assert len(payload["requests"]) == 2
