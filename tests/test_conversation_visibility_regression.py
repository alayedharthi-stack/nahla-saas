"""Post-P0 regression: legitimate conversations must stay visible.

After tightening message isolation, some real WhatsApp threads appeared
empty or vanished from the inbox when:
- Customer.normalized_phone was set but raw phone was blank
- Message metadata used Saudi local 05XXXXXXXX while the dashboard
  requested E.164 / digits-only forms
- Legacy rows had empty metadata but valid conversation_id linkage

These tests pin visibility without restoring the tenant-wide metadata.phone OR.
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

PHONE_E164 = "+966505263377"
PHONE_LOCAL = "0505263377"
PHONE_DIGITS = "966505263377"
PHONE_OTHER = "+966551786669"


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


def _call_list(db, tenant_id: int):
    original = conv_router.resolve_tenant_id
    conv_router.resolve_tenant_id = lambda request: tenant_id  # type: ignore
    try:
        return asyncio.run(
            conv_router.list_conversations(
                request=_FakeReq(),
                db=db,
                limit=80,
                offset=0,
                filter="all",
            )
        )
    finally:
        conv_router.resolve_tenant_id = original


class TestConversationVisibilityRegression:
    def test_list_includes_customer_when_only_normalized_phone_is_set(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        cust = Customer(
            tenant_id=t.id,
            phone=None,
            normalized_phone=PHONE_E164,
            name="Buyer",
        )
        db.add(cust)
        db.commit()
        db.refresh(cust)
        convo = Conversation(
            tenant_id=t.id,
            customer_id=cust.id,
            status="active",
            extra_metadata={"customer_phone": PHONE_E164},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)
        db.add(
            MessageEvent(
                tenant_id=t.id,
                conversation_id=convo.id,
                direction="inbound",
                body="السعر. 387 ريال الكيلو",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={"phone": PHONE_E164},
            )
        )
        db.commit()

        result = _call_list(db, t.id)
        phones = [row["phone"] for row in result["conversations"]]
        assert PHONE_E164 in phones, phones

    def test_list_shows_normalized_phone_with_local_metadata_preview(self):
        """Production shape: normalized_phone-only customer + local metadata stamp."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        cust = Customer(
            tenant_id=t.id,
            phone=None,
            normalized_phone=PHONE_E164,
            name="Buyer",
        )
        db.add(cust)
        db.commit()
        db.refresh(cust)
        convo = Conversation(
            tenant_id=t.id,
            customer_id=cust.id,
            status="active",
            extra_metadata={},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)
        db.add(
            MessageEvent(
                tenant_id=t.id,
                conversation_id=convo.id,
                direction="inbound",
                body="السعر. 387 ريال الكيلو",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={"phone": PHONE_LOCAL, "customer_phone": PHONE_LOCAL},
            )
        )
        db.commit()

        result = _call_list(db, t.id)
        row = next(
            (r for r in result["conversations"] if r["phone"] == PHONE_E164),
            None,
        )
        assert row is not None, [r["phone"] for r in result["conversations"]]
        assert row["lastMsg"] == "السعر. 387 ريال الكيلو"

    def test_list_resolves_phone_from_message_when_customer_link_stale(self):
        """Stale customer row with no phone still surfaces via message metadata."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        stale = Customer(
            tenant_id=t.id,
            phone=None,
            normalized_phone=None,
            name="Legacy",
        )
        db.add(stale)
        db.commit()
        db.refresh(stale)
        convo = Conversation(
            tenant_id=t.id,
            customer_id=stale.id,
            status="active",
            extra_metadata={},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)
        db.add(
            MessageEvent(
                tenant_id=t.id,
                conversation_id=convo.id,
                direction="inbound",
                body="السعر. 387 ريال الكيلو",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={"phone": PHONE_LOCAL},
            )
        )
        db.commit()

        result = _call_list(db, t.id)
        row = next(
            (r for r in result["conversations"] if r["phone"] == PHONE_E164),
            None,
        )
        assert row is not None, [r["phone"] for r in result["conversations"]]
        assert "387" in row["lastMsg"]

    def test_list_preview_falls_back_when_latest_row_is_historical_only(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        cust = Customer(
            tenant_id=t.id,
            phone=None,
            normalized_phone=PHONE_E164,
            name="Buyer",
        )
        db.add(cust)
        db.commit()
        db.refresh(cust)
        convo = Conversation(
            tenant_id=t.id,
            customer_id=cust.id,
            status="active",
            extra_metadata={"customer_phone": PHONE_E164},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)
        db.add(
            MessageEvent(
                tenant_id=t.id,
                conversation_id=convo.id,
                direction="inbound",
                body="historical-only",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={
                    "phone": PHONE_E164,
                    "historical_import": True,
                    "message_origin": "historical_sync",
                },
            )
        )
        db.commit()

        result = _call_list(db, t.id)
        row = next(
            (r for r in result["conversations"] if r["phone"] == PHONE_E164),
            None,
        )
        assert row is not None
        assert row["lastMsg"] == "historical-only"

    def test_local_metadata_phone_visible_for_e164_request(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        cust = Customer(
            tenant_id=t.id,
            phone=PHONE_LOCAL,
            normalized_phone=PHONE_E164,
            name="Buyer",
        )
        db.add(cust)
        db.commit()
        db.refresh(cust)
        convo = Conversation(
            tenant_id=t.id,
            customer_id=cust.id,
            status="active",
            extra_metadata={"phone": PHONE_LOCAL, "customer_phone": PHONE_LOCAL},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)
        db.add(
            MessageEvent(
                tenant_id=t.id,
                conversation_id=convo.id,
                direction="inbound",
                body="السعر. 387 ريال الكيلو",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={"phone": PHONE_LOCAL, "customer_phone": PHONE_LOCAL},
            )
        )
        db.commit()

        for requested in (PHONE_E164, PHONE_DIGITS, PHONE_LOCAL):
            result = _call_messages(db, t.id, requested)
            bodies = [m["body"] for m in result["messages"]]
            assert "السعر. 387 ريال الكيلو" in bodies, requested

    def test_legacy_empty_metadata_visible_inside_resolved_conversation(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        cust = Customer(
            tenant_id=t.id,
            phone=PHONE_E164,
            normalized_phone=PHONE_E164,
            name="Buyer",
        )
        db.add(cust)
        db.commit()
        db.refresh(cust)
        convo = Conversation(
            tenant_id=t.id,
            customer_id=cust.id,
            status="active",
            extra_metadata={"phone": PHONE_E164},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)
        db.add(
            MessageEvent(
                tenant_id=t.id,
                conversation_id=convo.id,
                direction="inbound",
                body="legacy-row-no-metadata",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={},
            )
        )
        db.commit()

        result = _call_messages(db, t.id, PHONE_E164)
        bodies = [m["body"] for m in result["messages"]]
        assert "legacy-row-no-metadata" in bodies

    def test_isolation_preserved_when_other_phone_stamped_in_same_conversation(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        cust = Customer(
            tenant_id=t.id,
            phone=PHONE_E164,
            normalized_phone=PHONE_E164,
            name="Buyer",
        )
        db.add(cust)
        db.commit()
        db.refresh(cust)
        convo = Conversation(
            tenant_id=t.id,
            customer_id=cust.id,
            status="active",
            extra_metadata={"phone": PHONE_E164},
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)
        db.add(
            MessageEvent(
                tenant_id=t.id,
                conversation_id=convo.id,
                direction="inbound",
                body="own-message",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={"phone": PHONE_E164},
            )
        )
        db.add(
            MessageEvent(
                tenant_id=t.id,
                conversation_id=convo.id,
                direction="inbound",
                body="other-customer-stamp",
                event_type="whatsapp",
                created_at=datetime.utcnow(),
                extra_metadata={"phone": PHONE_OTHER},
            )
        )
        db.commit()

        result = _call_messages(db, t.id, PHONE_E164)
        bodies = [m["body"] for m in result["messages"]]
        assert "own-message" in bodies
        assert "other-customer-stamp" not in bodies
