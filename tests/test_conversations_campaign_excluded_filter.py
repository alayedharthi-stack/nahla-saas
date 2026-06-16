"""tests/test_conversations_campaign_excluded_filter.py
Coverage for the "مستبعدون من الحملات" inbox filter:

  * ``GET /conversations?filter=campaign_excluded`` returns only
    conversations whose linked customer has
    ``marketing_opt_out_manual=True``.
  * Rows carry ``marketingOptOutManual`` in the list payload.
  * Opt-out does not affect AI pause state on the conversation row.
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

from models import (  # noqa: E402
    Base, Conversation, Customer, MessageEvent, Tenant,
)
from routers import conversations as conv_router  # noqa: E402
from services.manual_segments import (  # noqa: E402
    META_KEY_OPT_OUT,
    set_marketing_opt_out_manual,
)


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
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_conversation(
    db,
    tenant_id,
    phone,
    *,
    marketing_opt_out: bool = False,
    ai_paused: bool = False,
):
    extra = {META_KEY_OPT_OUT: True} if marketing_opt_out else {}
    cust = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=phone,
        name="Cust",
        extra_metadata=extra,
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    convo = Conversation(
        tenant_id=tenant_id,
        customer_id=cust.id,
        status="active",
        ai_paused=ai_paused,
        ai_paused_reason="manual_pause" if ai_paused else None,
        extra_metadata={"customer_phone": phone, "phone": phone},
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)
    db.add(MessageEvent(
        conversation_id=convo.id,
        tenant_id=tenant_id,
        direction="outbound",
        body="hi",
        event_type="whatsapp",
        created_at=datetime.utcnow(),
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


class TestCampaignExcludedFilter:
    def test_filter_returns_only_opted_out_customers(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_conversation(db, t.id, "+966500000001", marketing_opt_out=True)
        _seed_conversation(db, t.id, "+966500000002", marketing_opt_out=False)

        result = _call_list(db, t.id, filter="campaign_excluded")
        phones = [c["phone"] for c in result["conversations"]]
        assert phones == ["+966500000001"]

    def test_payload_includes_marketing_opt_out_flag(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_conversation(db, t.id, "+966500000010", marketing_opt_out=True)
        _seed_conversation(db, t.id, "+966500000011", marketing_opt_out=False)

        result = _call_list(db, t.id, filter="all")
        by_phone = {c["phone"]: c for c in result["conversations"]}
        assert by_phone["+966500000010"]["marketingOptOutManual"] is True
        assert not by_phone["+966500000011"].get("marketingOptOutManual")

    def test_opt_out_does_not_pause_ai(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        cust, convo = _seed_conversation(
            db, t.id, "+966500000020",
            marketing_opt_out=True,
            ai_paused=False,
        )
        set_marketing_opt_out_manual(
            db, tenant_id=t.id, customer_id=cust.id, opted_out=True,
        )
        db.refresh(convo)
        assert convo.ai_paused is False

        result = _call_list(db, t.id, filter="campaign_excluded")
        row = result["conversations"][0]
        assert row["marketingOptOutManual"] is True
        assert row["aiPaused"] is False
