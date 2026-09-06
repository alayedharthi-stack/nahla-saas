"""Explicit conversation-level AI on/off — Owner contract.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Human/staff activity, advisory queue, and TTL must not control AI
execution. Only explicit Stop AI (ai_paused) turns AI off. Only
explicit Start AI / Return to AI turns it back on.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in [_BACKEND, os.path.join(_BACKEND, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.ai_disabled_gate import (  # noqa: E402
    disabled_reason_for_conversation,
    is_ai_disabled_for_conversation,
)
from core.ai_pause_guard import REASON_MANUAL_PAUSE, resume_ai  # noqa: E402
from core.automation_send_guard import (  # noqa: E402
    REASON_AI_DISABLED,
    REASON_BLOCKED_NUMBER,
    REASON_STORE_AI_DISABLED,
    should_block_automation_for_conversation,
)
from core.ownership_state import (  # noqa: E402
    OWNERSHIP_AI_PRIMARY,
    OWNERSHIP_HUMAN_REQUESTED,
    attempt_implicit_takeover_recovery,
    resolve_ownership_state,
)
from routers.conversations import (  # noqa: E402
    HandoffIn,
    ReplyIn,
    handoff_conversation,
    reply_to_conversation,
)
from models import (  # noqa: E402
    Base,
    Conversation,
    HandoffSession,
    MessageEvent,
    Tenant,
    WaConversationWindow,
    WhatsAppConnection,
)


def _now() -> datetime:
    return datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


class _Msg:
    def __init__(
        self,
        *,
        direction: str,
        created_at: datetime,
        event_type: str = "",
        extra_metadata: dict | None = None,
    ) -> None:
        self.direction = direction
        self.created_at = created_at
        self.event_type = event_type
        self.extra_metadata = extra_metadata or {}


class _FakeQuery:
    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def filter(self, *_args, **_kwargs) -> _FakeQuery:
        return self

    def order_by(self, *_args, **_kwargs) -> _FakeQuery:
        return self

    def limit(self, *_args, **_kwargs) -> _FakeQuery:
        return self

    def first(self):
        inbounds = sorted(
            [r for r in self._rows if r.direction == "inbound"],
            key=lambda r: r.created_at,
            reverse=True,
        )
        return inbounds[0] if inbounds else None

    def all(self):
        return sorted(
            [r for r in self._rows if r.direction == "outbound"],
            key=lambda r: r.created_at,
            reverse=True,
        )


class _FakeDB:
    def __init__(self, rows: list | None = None) -> None:
        self._rows = list(rows or [])
        self.added = []

    def query(self, _model) -> _FakeQuery:
        return _FakeQuery(self._rows)

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        return None

    def flush(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _convo(**kwargs):
    defaults = dict(
        id=42,
        tenant_id=33,
        customer_id=7,
        ai_paused=False,
        ai_paused_reason=None,
        ai_paused_at=None,
        ai_paused_by=None,
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
        paused_by_human=False,
        taken_over_at=None,
        taken_over_by=None,
        status="active",
        extra_metadata={},
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _gate(convo, db=None):
    db = db if db is not None else _FakeDB()
    with patch(
        "core.ai_disabled_gate._find_conversations_for_phone",
        return_value=[convo],
    ), patch(
        "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
        return_value=SimpleNamespace(allowed=True, mode="on"),
    ):
        return is_ai_disabled_for_conversation(
            db,
            tenant_id=33,
            customer_phone="966500000001",
            conversation=convo,
        )


def _send(convo, db=None):
    return should_block_automation_for_conversation(
        db if db is not None else _FakeDB(),
        tenant_id=33,
        customer_phone="966500000001",
        conversation=convo,
    )


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):  # noqa: ARG001
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _sqlite_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), engine


def _request(tenant_id: int, path: str):
    return SimpleNamespace(
        state=SimpleNamespace(tenant_id=tenant_id),
        url=SimpleNamespace(path=path),
    )


def _seed_tenant_world(db, *, name: str = "Explicit AI Tenant"):
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _seed_whatsapp_and_window(db, tenant_id: int, phone: str, *, pid: str):
    db.add(WhatsAppConnection(
        tenant_id=tenant_id,
        status="connected",
        phone_number_id=pid,
        phone_number="+966500000000",
        sending_enabled=True,
        webhook_verified=True,
        connection_type="embedded",
        provider="meta",
    ))
    db.add(WaConversationWindow(
        tenant_id=tenant_id,
        customer_phone=phone,
        window_start=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
        last_customer_inbound_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
        category="service",
    ))
    db.commit()


class TestAHumanRequestedAiStaysOn:
    def test_advisory_queue_does_not_disable(self) -> None:
        convo = _convo(
            needs_human=True,
            handoff_active=True,
            is_human_handoff=True,
            status="human",
        )
        result = resolve_ownership_state(_FakeDB(), convo, now=_now())
        assert result.state == OWNERSHIP_HUMAN_REQUESTED
        assert disabled_reason_for_conversation(convo) == ""
        assert _gate(convo).disabled is False
        assert _send(convo).block is False


class TestBManualReplyDoesNotControlAi:
    def test_reply_source_does_not_stamp_takeover(self) -> None:
        src = inspect.getsource(reply_to_conversation)
        assert "paused_by_human = True" not in src
        assert "create_handoff_session" not in src
        compact = src.replace(" ", "")
        assert 'event_type="manual_reply"' in src
        assert '"is_ai":False' in compact

    def test_dashboard_reply_persists_message_without_pausing_ai(self) -> None:
        db, engine = _sqlite_db()
        try:
            tenant = _seed_tenant_world(db, name="Reply Behavior Tenant")
            window_phone = "+966555000013"
            _seed_whatsapp_and_window(
                db, tenant.id, window_phone, pid="PID_EXPLICIT_REPLY",
            )
            fake_module = ModuleType("routers.whatsapp_webhook")
            fake_module._send_whatsapp_message = AsyncMock(return_value=True)
            body = ReplyIn(customer_phone="0555000013", message="رد يدوي")
            request = _request(tenant.id, "/conversations/reply")
            with patch.dict(sys.modules, {"routers.whatsapp_webhook": fake_module}), patch(
                "core.billing.require_outbound_access",
            ):
                result = asyncio.run(reply_to_conversation(body, request, db))

            assert result["sent"] is True
            event = db.query(MessageEvent).filter(
                MessageEvent.id == result["message_event_id"],
            ).one()
            assert event.event_type == "manual_reply"
            assert (event.extra_metadata or {}).get("is_ai") is False
            convo = db.query(Conversation).filter(
                Conversation.tenant_id == tenant.id,
            ).one()
            assert convo.ai_paused is False
            assert convo.paused_by_human is False
            assert convo.taken_over_at is None
            assert db.query(HandoffSession).filter(
                HandoffSession.tenant_id == tenant.id,
            ).count() == 0
            assert _gate(convo, db=db).disabled is False
            assert _send(convo, db=db).block is False
        finally:
            db.close()
            engine.dispose()

    def test_clean_enabled_conversation_allows_inbound(self) -> None:
        convo = _convo(ai_paused=False)
        assert _gate(convo).disabled is False
        assert _send(convo).block is False


class TestHandoffApiDoesNotControlAi:
    def _call_handoff(self, db, tenant_id: int, phone: str, *, reason: str):
        body = HandoffIn(
            customer_phone=phone,
            customer_name="أحمد سالم",
            last_message="أحتاج مساعدة",
            reason=reason,
        )
        request = _request(tenant_id, "/conversations/handoff")
        return asyncio.run(handoff_conversation(body, request, db))

    def _convo_for_tenant(self, db, tenant_id: int) -> Conversation:
        return db.query(Conversation).filter(Conversation.tenant_id == tenant_id).one()

    def test_generic_customer_handoff_does_not_pause_ai(self) -> None:
        db, engine = _sqlite_db()
        try:
            tenant = _seed_tenant_world(db, name="Handoff Generic Tenant")
            result = self._call_handoff(
                db, tenant.id, "0555000021", reason="customer_request",
            )
            convo = self._convo_for_tenant(db, tenant.id)
            assert result["handoff"] is True
            assert result["needsHuman"] is True
            assert convo.ai_paused is False
            assert convo.needs_human is True
            assert convo.handoff_active is True
            assert db.query(HandoffSession).filter(
                HandoffSession.tenant_id == tenant.id,
                HandoffSession.status == "active",
            ).count() == 1
            assert _gate(convo, db=db).disabled is False
            assert _send(convo, db=db).block is False
        finally:
            db.close()
            engine.dispose()

    def test_support_escalation_handoff_does_not_pause_ai(self) -> None:
        db, engine = _sqlite_db()
        try:
            tenant = _seed_tenant_world(db, name="Handoff Support Tenant")
            self._call_handoff(
                db, tenant.id, "0555000022", reason="support_escalation",
            )
            convo = self._convo_for_tenant(db, tenant.id)
            assert convo.ai_paused is False
            assert convo.needs_human is True
            assert _gate(convo, db=db).disabled is False
        finally:
            db.close()
            engine.dispose()

    def test_manual_and_staff_takeover_handoff_does_not_pause_ai(self) -> None:
        db, engine = _sqlite_db()
        try:
            tenant = _seed_tenant_world(db, name="Handoff Takeover Tenant")
            for reason, phone in (
                ("manual_takeover", "0555000023"),
                ("staff_takeover", "0555000024"),
            ):
                self._call_handoff(db, tenant.id, phone, reason=reason)
            convos = db.query(Conversation).filter(
                Conversation.tenant_id == tenant.id,
            ).all()
            assert len(convos) == 2
            for convo in convos:
                assert convo.ai_paused is False
                assert convo.needs_human is True
                assert _gate(convo, db=db).disabled is False
                assert _send(convo, db=db).block is False
        finally:
            db.close()
            engine.dispose()

    def test_handoff_does_not_resume_explicit_stop(self) -> None:
        db, engine = _sqlite_db()
        try:
            tenant = _seed_tenant_world(db, name="Handoff Stopped Tenant")
            from routers.conversations import _get_or_create_conversation
            from core.ai_pause_guard import pause_ai

            convo = _get_or_create_conversation(db, tenant.id, "0555000025", "نورة عبدالله")
            pause_ai(db, convo, reason=REASON_MANUAL_PAUSE, by="dashboard:manual_pause")
            db.refresh(convo)
            assert convo.ai_paused is True
            self._call_handoff(db, tenant.id, "0555000025", reason="manual_takeover")
            db.refresh(convo)
            assert convo.ai_paused is True
            assert convo.ai_paused_reason == REASON_MANUAL_PAUSE
            assert convo.needs_human is True
            assert _gate(convo, db=db).disabled is True
            assert _send(convo, db=db).block is True
        finally:
            db.close()
            engine.dispose()


class TestCManualReplyDuringHandoff:
    def test_queue_plus_staff_activity_keeps_ai_on(self) -> None:
        staff_at = _now() - timedelta(minutes=2)
        convo = _convo(
            ai_paused=False,
            needs_human=True,
            handoff_active=True,
            is_human_handoff=True,
            status="human",
            paused_by_human=True,
            taken_over_at=staff_at,
            taken_over_by="user:42",
        )
        db = _FakeDB([
            _Msg(direction="outbound", created_at=staff_at, event_type="manual_reply"),
        ])
        result = resolve_ownership_state(db, convo, now=_now(), assume_current_inbound=True)
        assert result.state == OWNERSHIP_HUMAN_REQUESTED
        assert _gate(convo, db=db).disabled is False
        assert _send(convo, db=db).block is False


class TestDExplicitStopAi:
    def test_manual_pause_blocks_gate_and_send(self) -> None:
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        decision = _gate(convo)
        assert decision.disabled is True
        assert decision.reason == REASON_MANUAL_PAUSE
        send = _send(convo)
        assert send.block is True
        assert send.reason == REASON_AI_DISABLED


class TestEManualReplyDoesNotAutoStart:
    def test_reply_while_paused_leaves_ai_off(self) -> None:
        src = inspect.getsource(reply_to_conversation)
        assert "resume_ai" not in src
        convo = _convo(
            ai_paused=True,
            ai_paused_reason=REASON_MANUAL_PAUSE,
            paused_by_human=True,
            taken_over_at=_now(),
            taken_over_by="dashboard:reply",
        )
        assert _gate(convo).disabled is True
        assert _send(convo).block is True


class TestFTtlDoesNotAutoResume:
    def test_idle_ttl_does_not_clear_explicit_pause(self) -> None:
        staff_at = _now() - timedelta(seconds=901)
        convo = _convo(
            ai_paused=True,
            ai_paused_reason=REASON_MANUAL_PAUSE,
            paused_by_human=True,
            taken_over_at=staff_at,
            taken_over_by="user:42",
        )
        db = _FakeDB([
            _Msg(direction="outbound", created_at=staff_at, event_type="manual_reply"),
            _Msg(direction="inbound", created_at=_now() - timedelta(minutes=1)),
        ])
        recovery = attempt_implicit_takeover_recovery(
            db, convo, now=_now(), assume_current_inbound=True,
        )
        assert recovery.released is False
        assert recovery.reason == "ttl_does_not_control_ai"
        assert convo.ai_paused is True
        assert _gate(convo, db=db).disabled is True
        assert _send(convo, db=db).block is True


class TestGExplicitStartAi:
    def test_resume_clears_pause_and_allows(self) -> None:
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        resume_ai(_FakeDB(), convo, by="dashboard:resume", commit=False)
        assert convo.ai_paused is False
        assert _gate(convo).disabled is False
        assert _send(convo).block is False


class TestHLegacyTakeoverResidue:
    def test_residue_alone_does_not_disable(self) -> None:
        staff_at = _now() - timedelta(minutes=5)
        convo = _convo(
            ai_paused=False,
            paused_by_human=True,
            taken_over_at=staff_at,
            taken_over_by="user:42",
        )
        db = _FakeDB([
            _Msg(direction="outbound", created_at=staff_at, event_type="manual_reply"),
        ])
        result = resolve_ownership_state(db, convo, now=_now())
        assert result.state == OWNERSHIP_AI_PRIMARY
        assert disabled_reason_for_conversation(convo) == ""
        assert _gate(convo, db=db).disabled is False
        assert _send(convo, db=db).block is False

    def test_staff_takeover_session_does_not_disable(self) -> None:
        from core.ai_disabled_gate import _handoff_session_disables_ai

        row = SimpleNamespace(status="active", handoff_reason="staff_takeover")
        assert _handoff_session_disables_ai(row) is False
        convo = _convo(ai_paused=False)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = row
        assert _gate(convo, db=db).disabled is False


class TestILegacyResiduePlusStartAi:
    def test_resume_allows_immediately_with_residue(self) -> None:
        staff_at = _now() - timedelta(minutes=3)
        convo = _convo(
            ai_paused=True,
            ai_paused_reason=REASON_MANUAL_PAUSE,
            paused_by_human=True,
            taken_over_at=staff_at,
            taken_over_by="user:42",
            needs_human=True,
            handoff_active=True,
        )
        resume_ai(_FakeDB(), convo, by="dashboard:resume", commit=False)
        assert convo.ai_paused is False
        assert convo.paused_by_human is True
        assert convo.taken_over_at is not None
        assert _gate(convo).disabled is False
        assert _send(convo).block is False


class TestJUnrelatedSafety:
    def test_blocked_number_still_blocks(self) -> None:
        convo = _convo()
        with patch(
            "core.automation_send_guard.is_internal_or_blocked",
            return_value=(True, "internal_number"),
        ):
            decision = _send(convo)
        assert decision.block is True
        assert decision.reason == REASON_BLOCKED_NUMBER

    def test_store_ai_disabled_still_blocks(self) -> None:
        convo = _convo()
        with patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(
                allowed=False, reason=REASON_STORE_AI_DISABLED, mode="off",
            ),
        ):
            decision = _send(convo)
        assert decision.block is True
        assert decision.reason == REASON_STORE_AI_DISABLED

    def test_test_mode_unlisted_still_blocks_gate(self) -> None:
        from core.ai_disabled_gate import REASON_STORE_AI_TEST_MODE_NOT_ALLOWED
        from core.tenant import STORE_AI_MODE_TEST

        convo = _convo()
        db = _FakeDB()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(
                allowed=False,
                reason=REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
                mode=STORE_AI_MODE_TEST,
            ),
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966500000099",
                conversation=convo,
            )
        assert decision.disabled is True
        assert decision.reason == REASON_STORE_AI_TEST_MODE_NOT_ALLOWED
