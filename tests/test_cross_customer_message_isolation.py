"""Regression: two different WhatsApp numbers must never share a thread.

Incident (Jun 2026): +966505263377's Nahla thread showed messages that
belonged to +966551786669 on WhatsApp.

The API endpoint ``GET /conversations/messages/{customer_phone}`` must
only return MessageEvents that belong to THAT customer's conversation(s),
never rows whose ``extra_metadata.phone`` belongs to a different customer
when conversation_id is also distinct.

These tests pin the contract BEFORE any production evidence run; they
fail today if contamination vectors are live in the query layer.
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
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Conversation, Customer, MessageEvent, Tenant  # noqa: E402
from routers import conversations as conv_router  # noqa: E402

PHONE_A = "+966505263377"
PHONE_B = "+966551786669"


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
    return Session(), engine


def _seed_tenant(db):
    t = Tenant(name="T", is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_customer_conversation(db, tenant_id: int, phone: str) -> tuple[Customer, Conversation]:
    cust = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=phone,
        name="Cust",
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    convo = Conversation(
        tenant_id=tenant_id,
        customer_id=cust.id,
        status="active",
        extra_metadata={"customer_phone": phone, "phone": phone},
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return cust, convo


def _add_message(
    db,
    *,
    tenant_id: int,
    conversation_id: int,
    phone: str,
    body: str,
    direction: str = "inbound",
) -> MessageEvent:
    row = MessageEvent(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        direction=direction,
        body=body,
        event_type="whatsapp",
        created_at=datetime.utcnow(),
        extra_metadata={"phone": phone, "customer_phone": phone},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class _FakeReq:
    headers: dict = {}
    cookies: dict = {}
    state = type("S", (), {})()


def _call_messages(db, tenant_id: int, phone: str):
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


class TestCrossCustomerMessageIsolation:
    def test_separate_customers_do_not_see_each_others_messages(self):
        """Baseline: distinct customers + distinct conversations → isolated."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        _, conv_a = _seed_customer_conversation(db, t.id, PHONE_A)
        _, conv_b = _seed_customer_conversation(db, t.id, PHONE_B)

        _add_message(
            db, tenant_id=t.id, conversation_id=conv_a.id,
            phone=PHONE_A, body="عسل السمرة من اي منطقة",
        )
        _add_message(
            db, tenant_id=t.id, conversation_id=conv_b.id,
            phone=PHONE_B, body="ذي بنتي ف الاختبار",
        )

        result_a = _call_messages(db, t.id, PHONE_A)
        bodies_a = [m["body"] for m in result_a["messages"]]
        assert "ذي بنتي ف الاختبار" not in bodies_a
        assert "عسل السمرة من اي منطقة" in bodies_a

        result_b = _call_messages(db, t.id, PHONE_B)
        bodies_b = [m["body"] for m in result_b["messages"]]
        assert "عسل السمرة من اي منطقة" not in bodies_b
        assert "ذي بنتي ف الاختبار" in bodies_b

    def test_merged_customer_id_must_not_blend_metadata_phones(self):
        """If two numbers were wrongly merged onto one Customer row, the API
        must still not return the other number's historical rows when the
        merchant opens the thread for one phone.

        This is the suspected production failure mode: customer B's messages
        share conversation_id with customer A after identity overwrite.
        """
        db, _ = _make_db()
        t = _seed_tenant(db)

        # Simulate identity merge: ONE customer row now shows phone A,
        # but old messages were persisted under the same conversation when
        # the channel identity was phone B.
        cust = Customer(
            tenant_id=t.id,
            phone=PHONE_A,
            normalized_phone=PHONE_A,
            name="Merged",
        )
        db.add(cust)
        db.commit()
        db.refresh(cust)

        convo = Conversation(
            tenant_id=t.id,
            customer_id=cust.id,
            status="active",
            extra_metadata={"customer_phone": PHONE_A, "phone": PHONE_A},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)

        _add_message(
            db, tenant_id=t.id, conversation_id=convo.id,
            phone=PHONE_B, body="الله يرزقك",
        )
        _add_message(
            db, tenant_id=t.id, conversation_id=convo.id,
            phone=PHONE_A, body="السلام عليكم",
        )

        result = _call_messages(db, t.id, PHONE_A)
        bodies = [m["body"] for m in result["messages"]]

        # TODAY this assertion FAILS if the OR(conversation_id, phone_meta)
        # query is unchanged — documenting the contamination vector.
        assert "الله يرزقك" not in bodies, (
            "Phone A's thread must not include rows whose metadata.phone "
            "is Phone B, even when they share a conversation_id from a "
            "bad customer merge"
        )
        assert "السلام عليكم" in bodies

    def test_wrong_metadata_phone_must_not_leak_across_conversations(self):
        """A row stored under conversation B but stamped with phone A in
        metadata must not appear in phone A's thread when conversations
        are otherwise separate."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        _, conv_a = _seed_customer_conversation(db, t.id, PHONE_A)
        _, conv_b = _seed_customer_conversation(db, t.id, PHONE_B)

        # Mis-stamped row: belongs to conv B but metadata says phone A.
        _add_message(
            db, tenant_id=t.id, conversation_id=conv_b.id,
            phone=PHONE_A, body="واباك",
        )
        _add_message(
            db, tenant_id=t.id, conversation_id=conv_a.id,
            phone=PHONE_A, body="وكم الاسعار",
        )

        result = _call_messages(db, t.id, PHONE_A)
        bodies = [m["body"] for m in result["messages"]]

        assert "واباك" not in bodies, (
            "Phone-metadata OR filter must not pull rows from another "
            "customer's conversation"
        )
        assert "وكم الاسعار" in bodies
