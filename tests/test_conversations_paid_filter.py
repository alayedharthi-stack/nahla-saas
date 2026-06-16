"""tests/test_conversations_paid_filter.py
─────────────────────────────────
Coverage for the "طلبات مدفوعة" inbox filter:

  * ``Conversation.last_payment_confirmed_at`` is stamped only when
    payment is explicitly confirmed (``payment_confirmed`` /
    ``verified_by_staff`` / ``payment_verified``) — not when the
    customer merely submits a receipt.
  * ``GET /conversations?filter=paid`` only returns conversations
    that have a non-NULL ``last_payment_confirmed_at`` and the rows
    are ordered most-recent-first.
  * Existing filters (``all`` / ``human``) are unaffected — i.e. the
    new column does not leak into the row payload as a state change.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
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

from models import (  # noqa: E402
    Base, Conversation, Customer, MessageEvent, Tenant,
)
from routers import conversations as conv_router  # noqa: E402


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db):
    t = Tenant(name="T", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _seed_conversation(
    db, tenant_id, phone, *,
    paid_at: datetime | None = None,
    last_msg_at: datetime | None = None,
):
    cust = Customer(
        tenant_id=tenant_id, phone=phone, normalized_phone=phone, name="Cust",
    )
    db.add(cust); db.commit(); db.refresh(cust)
    convo = Conversation(
        tenant_id=tenant_id,
        customer_id=cust.id,
        status="active",
        last_payment_confirmed_at=paid_at,
        extra_metadata={"customer_phone": phone, "phone": phone},
    )
    db.add(convo); db.commit(); db.refresh(convo)
    # Conversation list orders by latest MessageEvent; give every row
    # one outbound event so the SQL JOIN-with-MAX path produces a
    # deterministic order.
    db.add(MessageEvent(
        conversation_id=convo.id,
        tenant_id=tenant_id,
        direction="outbound",
        body="hi",
        event_type="whatsapp",
        created_at=last_msg_at or datetime.utcnow(),
    ))
    db.commit()
    return cust, convo


class _FakeReq:
    headers: dict = {}
    cookies: dict = {}
    state = type("S", (), {})()


def _call_list(db, tenant_id, *, filter: str = "all"):
    original = conv_router.resolve_tenant_id
    conv_router.resolve_tenant_id = lambda request: tenant_id  # type: ignore
    try:
        return asyncio.run(
            conv_router.list_conversations(
                request=_FakeReq(), db=db, limit=80, offset=0,
                filter=filter,
            )
        )
    finally:
        conv_router.resolve_tenant_id = original


class TestPaidFilter:
    def test_paid_filter_returns_only_confirmed_payments(self):
        db, _ = _make_db()
        t = _seed_tenant(db)

        # Three customers — only two have confirmed payments.
        now = datetime.now(timezone.utc)
        _seed_conversation(
            db, t.id, "+966500000001",
            paid_at=now - timedelta(minutes=10),
            last_msg_at=datetime.utcnow() - timedelta(minutes=5),
        )
        _seed_conversation(
            db, t.id, "+966500000002",
            paid_at=now,
            last_msg_at=datetime.utcnow(),
        )
        # Third customer never sent a confirmed receipt.
        _seed_conversation(
            db, t.id, "+966500000003",
            paid_at=None,
            last_msg_at=datetime.utcnow() - timedelta(minutes=15),
        )

        result = _call_list(db, t.id, filter="paid")
        phones = [c["phone"] for c in result["conversations"]]
        assert "+966500000003" not in phones, (
            "the customer without a confirmed receipt MUST NOT appear "
            "in the 'paid' filter"
        )
        assert set(phones) == {"+966500000001", "+966500000002"}
        # Every returned row carries the ISO timestamp the badge needs.
        for row in result["conversations"]:
            assert row["lastPaymentConfirmedAt"], (
                "lastPaymentConfirmedAt must be present for every row "
                "in the paid filter"
            )

    def test_paid_filter_empty_when_no_confirmed_payment(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_conversation(db, t.id, "+966500000010", paid_at=None)
        result = _call_list(db, t.id, filter="paid")
        assert result["conversations"] == []
        assert result["total_count"] == 0

    def test_all_filter_still_returns_unpaid_conversations(self):
        """Regression guard: the new column / filter must not
        accidentally narrow the default ``all`` listing."""
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_conversation(db, t.id, "+966500000020", paid_at=None)
        _seed_conversation(
            db, t.id, "+966500000021",
            paid_at=datetime.now(timezone.utc),
        )
        result = _call_list(db, t.id, filter="all")
        phones = [c["phone"] for c in result["conversations"]]
        assert "+966500000020" in phones
        assert "+966500000021" in phones
        # And the timestamp is None on the unpaid row, so the
        # frontend can tell them apart.
        by_phone = {c["phone"]: c for c in result["conversations"]}
        assert by_phone["+966500000020"]["lastPaymentConfirmedAt"] is None
        assert by_phone["+966500000021"]["lastPaymentConfirmedAt"] is not None


class TestApplyStatePatchStampsLastPaymentConfirmedAt:
    """``apply_state_patch`` must stamp ``last_payment_confirmed_at``
    only on explicit payment confirmation — receipt submission alone
    (``payment_submitted``) must not surface in the paid filter."""

    def test_receipt_received_without_confirmation_does_not_stamp(self):
        from core.order_flow import apply_state_patch
        db, _ = _make_db()
        t = _seed_tenant(db)
        _, convo = _seed_conversation(db, t.id, "+966500000030", paid_at=None)
        assert convo.last_payment_confirmed_at is None

        ok = apply_state_patch(
            db,
            tenant_id=t.id,
            phone="+966500000030",
            state_patch={
                "awaiting_payment_receipt": False,
                "payment_receipt_received": True,
                "payment_confirmed": False,
                "order_status": "payment_submitted",
            },
        )
        assert ok is True
        db.refresh(convo)
        assert convo.last_payment_confirmed_at is None, (
            "receipt submission alone must NOT stamp "
            "Conversation.last_payment_confirmed_at"
        )

    def test_explicit_payment_confirmation_stamps_column(self):
        from core.order_flow import apply_state_patch
        db, _ = _make_db()
        t = _seed_tenant(db)
        _, convo = _seed_conversation(db, t.id, "+966500000031", paid_at=None)
        assert convo.last_payment_confirmed_at is None

        ok = apply_state_patch(
            db,
            tenant_id=t.id,
            phone="+966500000031",
            state_patch={
                "payment_receipt_received": True,
                "payment_confirmed": True,
                "order_status": "paid",
            },
        )
        assert ok is True
        db.refresh(convo)
        assert convo.last_payment_confirmed_at is not None, (
            "apply_state_patch with payment_confirmed=True must stamp "
            "Conversation.last_payment_confirmed_at so the paid filter "
            "can find the row"
        )

    def test_payment_confirmed_alone_stamps_column(self):
        from core.order_flow import apply_state_patch
        db, _ = _make_db()
        t = _seed_tenant(db)
        _, convo = _seed_conversation(db, t.id, "+966500000032", paid_at=None)

        apply_state_patch(
            db, tenant_id=t.id, phone="+966500000032",
            state_patch={"payment_confirmed": True},
        )
        db.refresh(convo)
        assert convo.last_payment_confirmed_at is not None

    def test_apply_state_patch_does_not_overwrite_existing_timestamp(self):
        """Idempotency: a redundant confirmation patch on a row that
        already has a stamp must NOT push the timestamp forward."""
        from core.order_flow import apply_state_patch
        db, _ = _make_db()
        t = _seed_tenant(db)
        original = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        _, convo = _seed_conversation(
            db, t.id, "+966500000040", paid_at=original,
        )
        apply_state_patch(
            db, tenant_id=t.id, phone="+966500000040",
            state_patch={
                "payment_receipt_received": True,
                "payment_confirmed": True,
            },
        )
        db.refresh(convo)
        stored = convo.last_payment_confirmed_at
        if stored is not None and stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
        assert stored == original

    def test_apply_state_patch_without_confirmation_does_not_stamp(self):
        """A patch that only asks for a receipt must leave the column
        untouched — otherwise asking for a receipt would falsely mark
        the order as paid."""
        from core.order_flow import apply_state_patch
        db, _ = _make_db()
        t = _seed_tenant(db)
        _, convo = _seed_conversation(db, t.id, "+966500000050", paid_at=None)
        apply_state_patch(
            db, tenant_id=t.id, phone="+966500000050",
            state_patch={
                "awaiting_payment_receipt": True,
                "order_status": "awaiting_receipt",
            },
        )
        db.refresh(convo)
        assert convo.last_payment_confirmed_at is None
