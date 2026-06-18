"""Locks GET /admin/debug/conversation-list-visibility handler contract.

The admin route uses ?tenant_id= instead of merchant JWT tenant scope.
It must call list_conversations without AttributeError on request.url.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from starlette.requests import Request

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from models import Base, Conversation, Customer, MessageEvent, Tenant  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


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
    return sessionmaker(bind=engine)(), engine


def _fake_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/debug/conversation-list-visibility",
            "headers": [],
            "query_string": b"",
        }
    )


def _call_visibility(*, db, tenant_id: int, phone: str):
    from routers.admin_debug import admin_debug_conversation_list_visibility

    return _run(
        admin_debug_conversation_list_visibility(
            request=_fake_request(),
            tenant_id=tenant_id,
            phone=phone,
            filter="all",
            limit=80,
            offset=0,
            db=db,
            _admin={"sub": "admin@nahla", "role": "admin"},
        )
    )


class TestAdminDebugListVisibility:
    def test_returns_verdict_without_request_url_attribute_error(self):
        db, _ = _make_db()
        t = Tenant(name="T", is_active=True)
        db.add(t)
        db.commit()
        db.refresh(t)

        cust = Customer(
            tenant_id=t.id,
            phone="+966505263377",
            normalized_phone="+966505263377",
            name="Buyer",
        )
        db.add(cust)
        db.commit()
        db.refresh(cust)

        convo = Conversation(
            tenant_id=t.id,
            customer_id=cust.id,
            status="active",
            extra_metadata={"customer_phone": "+966505263377"},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)

        db.add(
            MessageEvent(
                tenant_id=t.id,
                conversation_id=convo.id,
                direction="inbound",
                body="387",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={"phone": "+966505263377"},
            )
        )
        db.commit()

        resp = _call_visibility(db=db, tenant_id=t.id, phone="966505263377")

        assert "verdict" in resp
        assert resp["tenant_id"] == t.id
        assert resp["list_api"]["matched_row"] is not None
