"""Pin GET /conversations/messages/{phone} filter scope.

Global contamination symptom: every phone returns the same thread.
These tests verify the query never widens to tenant-wide when
conv_ids is empty or customer lookup misses.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Conversation, Customer, MessageEvent, Tenant  # noqa: E402
from routers import conversations as conv_router  # noqa: E402
from core.conversation_engine import PLATFORM_TENANT_ID  # noqa: E402


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


def _seed_tenant(db):
    t = Tenant(name="T", is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_pair(db, tenant_id: int, phone: str, body: str) -> tuple[Customer, Conversation, MessageEvent]:
    cust = Customer(tenant_id=tenant_id, phone=phone, normalized_phone=phone, name="X")
    db.add(cust)
    db.commit()
    db.refresh(cust)
    convo = Conversation(
        tenant_id=tenant_id,
        customer_id=cust.id,
        status="active",
        extra_metadata={"phone": phone, "customer_phone": phone},
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)
    msg = MessageEvent(
        tenant_id=tenant_id,
        conversation_id=convo.id,
        direction="inbound",
        body=body,
        event_type="whatsapp",
        created_at=datetime.utcnow(),
        extra_metadata={"phone": phone, "customer_phone": phone},
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return cust, convo, msg


class _FakeReq:
    headers: dict = {}
    cookies: dict = {}
    state = type("S", (), {})()


def _call(db, tenant_id: int, phone: str):
    original = conv_router.resolve_tenant_id
    conv_router.resolve_tenant_id = lambda request: tenant_id  # type: ignore
    try:
        return asyncio.run(
            conv_router.get_conversation_messages(
                customer_phone=phone,
                request=_FakeReq(),
                db=db,
                limit=50,
            )
        )
    finally:
        conv_router.resolve_tenant_id = original


class TestMessageFilterScope:
    def test_unknown_phone_returns_empty_not_tenant_wide(self):
        """Brand-new phone with no Customer row must not inherit other threads."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_pair(db, t.id, "+966500000001", "thread-one")
        _seed_pair(db, t.id, "+966500000002", "thread-two")

        result = _call(db, t.id, "+966509999999")
        bodies = [m["body"] for m in result["messages"]]
        assert bodies == [], f"expected empty thread, got {bodies}"

    def test_three_unrelated_phones_return_distinct_threads(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        phones = ["+966505263377", "+966551786669", "+966500000099"]
        bodies_by_phone = {
            p: f"unique-body-{p[-4:]}" for p in phones
        }
        for phone, body in bodies_by_phone.items():
            _seed_pair(db, t.id, phone, body)

        seen: list[list[str]] = []
        for phone in phones:
            result = _call(db, t.id, phone)
            seen.append([m["body"] for m in result["messages"]])

        assert len({tuple(s) for s in seen}) == 3, seen

    def test_platform_tenant_rows_do_not_bleed_into_merchant_phone(self):
        """Merchant message fetch must stay tenant-scoped (no platform bleed)."""
        db, _ = _make_db()
        merchant = _seed_tenant(db)
        merchant_phone = "+966505263377"
        platform_phone = "+966509999888"

        _seed_pair(db, merchant.id, merchant_phone, "merchant-customer")

        plat_cust = Customer(
            tenant_id=PLATFORM_TENANT_ID,
            phone=platform_phone,
            normalized_phone=platform_phone,
            name="PlatformOnly",
        )
        db.add(plat_cust)
        db.commit()
        db.refresh(plat_cust)
        plat_convo = Conversation(
            tenant_id=PLATFORM_TENANT_ID,
            customer_id=plat_cust.id,
            status="active",
            extra_metadata={"phone": platform_phone},
        )
        db.add(plat_convo)
        db.commit()
        db.refresh(plat_convo)
        db.add(
            MessageEvent(
                tenant_id=PLATFORM_TENANT_ID,
                conversation_id=plat_convo.id,
                direction="inbound",
                body="platform-tenant-leak",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={"phone": platform_phone},
            )
        )
        db.commit()

        result = _call(db, merchant.id, merchant_phone)
        bodies = [m["body"] for m in result["messages"]]
        assert "platform-tenant-leak" not in bodies, bodies
        assert "merchant-customer" in bodies
