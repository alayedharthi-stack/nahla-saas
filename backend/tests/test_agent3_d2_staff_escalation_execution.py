"""AGENT3-D2 — real staff escalation execution and truth.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Generic commerce fixtures. Live false-promise Arabic is a regression
fixture only — not a runtime routing rule.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_HANDOFF  # noqa: E402
from modules.ai.brain.execution.executor import DefaultActionExecutor, _HandoffHandler  # noqa: E402
from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: E402
    STATUS_FAILED,
    STATUS_NOTIFIED,
    STATUS_QUEUED,
    STATUS_UNAVAILABLE,
    execute_staff_escalation,
    format_staff_escalation_facts_overlay,
)
from modules.ai.brain.postprocess.staff_escalation_evidence import (  # noqa: E402
    evaluate_staff_escalation_evidence,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from models import HandoffSession, Tenant  # noqa: E402

_LIVE_FALSE_PROMISE = (
    "سأنبّه فريق المتجر للتواصل معك في أقرب وقت ممكن. "
    "سيرد عليك أحد أعضاء الفريق"
)


def _run(coro):
    return asyncio.run(coro)


def _sqlite_db():
    engine = create_engine("sqlite:///:memory:")
    for table in (Tenant.__table__, HandoffSession.__table__):
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    Tenant.__table__.create(engine, checkfirst=True)
    HandoffSession.__table__.create(engine, checkfirst=True)
    return sessionmaker(bind=engine)(), engine


def _seed_tenant(db, name: str) -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _ctx(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    message: str = "ابي موظف",
    profile: dict | None = None,
    verified_phone: str | None = None,
) -> BrainContext:
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=message,
        intent=Intent(name="talk_to_human", confidence=0.95, raw_message=message),
        state=MerchantConversationState(),
        facts=CommerceFacts(store_name="متجر تجريبي عام"),
        history=[],
        profile=profile if profile is not None else {"name": "أحمد سالم"},
    )
    ctx._db = db  # type: ignore[attr-defined]
    if verified_phone is not None:
        ctx.verified_staff_contact_phone = verified_phone  # type: ignore[attr-defined]
    return ctx


def _decision(**args: Any) -> Decision:
    return Decision(action=ACTION_HANDOFF, args=dict(args), reason="customer_request")


def _count_sessions(db, tenant_id: int, phone: str) -> int:
    return (
        db.query(HandoffSession)
        .filter(
            HandoffSession.tenant_id == tenant_id,
            HandoffSession.customer_phone == phone,
            HandoffSession.status == "active",
        )
        .count()
    )


class TestDurableQueue:
    def test_staff_request_creates_queued_session(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Queue Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000101")
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        db.commit()
        assert result.success is True
        assert result.data["escalation_status"] == STATUS_QUEUED
        assert result.data["handoff_session_created"] is True
        assert result.data["notification_accepted"] is False
        session = db.query(HandoffSession).filter_by(id=result.data["handoff_session_id"]).one()
        assert session.tenant_id == tenant.id
        assert session.customer_phone == "966500000101"
        assert session.status == "active"

    def test_duplicate_request_reuses_active_session(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Idempotent Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000102")
        settings = {"notification_method": "none", "webhook_url": ""}
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value=settings,
        ):
            first = _run(execute_staff_escalation(_decision(), ctx))
            db.commit()
            second = _run(execute_staff_escalation(_decision(), ctx))
            db.commit()
        assert first.data["handoff_session_id"] == second.data["handoff_session_id"]
        assert second.data["handoff_session_reused"] is True
        assert second.data["handoff_session_created"] is False
        assert _count_sessions(db, tenant.id, "966500000102") == 1

    def test_queue_succeeds_when_notification_unavailable(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Notify None Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000103")
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "whatsapp", "webhook_url": "", "staff_whatsapp": "966500009999"},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        db.commit()
        assert result.success is True
        assert result.data["escalation_status"] == STATUS_QUEUED
        assert result.data["notification_attempted"] is False
        assert result.data["notification_accepted"] is False
        assert db.query(HandoffSession).count() == 1


class TestNotification:
    def test_webhook_accept_marks_notified(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Webhook Ok Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000104")
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={
                "notification_method": "webhook",
                "webhook_url": "https://staff.example.test/handoff",
            },
        ), patch(
            "handoff.notifier.notify_handoff",
            new=AsyncMock(return_value=True),
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        db.commit()
        assert result.data["notification_attempted"] is True
        assert result.data["notification_accepted"] is True
        assert result.data["escalation_status"] == STATUS_NOTIFIED
        session = db.query(HandoffSession).one()
        assert session.notification_sent is True

    def test_webhook_failure_keeps_queue_without_notified_claim(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Webhook Fail Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000105")
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={
                "notification_method": "webhook",
                "webhook_url": "https://staff.example.test/handoff",
            },
        ), patch(
            "handoff.notifier.notify_handoff",
            new=AsyncMock(return_value=False),
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        db.commit()
        assert result.success is True
        assert result.data["escalation_status"] == STATUS_QUEUED
        assert result.data["notification_attempted"] is True
        assert result.data["notification_accepted"] is False
        overlay = result.data["compose_facts_overlay"]
        assert "status=queued" in overlay
        assert "status=notified" not in overlay
        assert db.query(HandoffSession).count() == 1


class TestFailureModes:
    def test_persistence_failure_status_failed(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Persist Fail Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000106")
        with patch(
            "handoff.manager.create_handoff_session",
            side_effect=RuntimeError("flush failed"),
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        assert result.success is False
        assert result.data["escalation_status"] == STATUS_FAILED
        assert result.data["failure_code"] == "persistence_failed"
        assert "status=failed" in result.data["compose_facts_overlay"]

    def test_missing_db_status_unavailable(self) -> None:
        ctx = BrainContext(
            tenant_id=9,
            customer_phone="966500000107",
            message="ابي موظف",
            intent=Intent(name="talk_to_human", confidence=0.9),
            state=MerchantConversationState(),
            facts=CommerceFacts(),
        )
        result = _run(execute_staff_escalation(_decision(), ctx))
        assert result.success is False
        assert result.data["escalation_status"] == STATUS_UNAVAILABLE
        assert result.data["handoff_session_id"] is None
        assert "status=unavailable" in result.data["compose_facts_overlay"]


class TestVerifiedContact:
    def test_no_verified_phone_is_not_invented(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 No Phone Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000108")
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        assert result.data["verified_contact_available"] is False
        assert result.data["verified_contact_phone"] == ""
        assert "verified_contact_phone=" not in result.data["compose_facts_overlay"]

    def test_trusted_verified_contact_propagated_exactly(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Verified Phone Merchant")
        trusted = "966511112222"
        ctx = _ctx(
            db=db,
            tenant_id=tenant.id,
            phone="966500000109",
            verified_phone=trusted,
        )
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        assert result.data["verified_contact_available"] is True
        assert result.data["verified_contact_phone"] == trusted


class TestTenantIsolation:
    def test_sessions_do_not_cross_tenants(self) -> None:
        db, _engine = _sqlite_db()
        tenant_a = _seed_tenant(db, "D2 Tenant A")
        tenant_b = _seed_tenant(db, "D2 Tenant B")
        phone = "966500000110"
        settings = {"notification_method": "none", "webhook_url": ""}
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value=settings,
        ):
            a = _run(execute_staff_escalation(_decision(), _ctx(db=db, tenant_id=tenant_a.id, phone=phone)))
            b = _run(execute_staff_escalation(_decision(), _ctx(db=db, tenant_id=tenant_b.id, phone=phone)))
        db.commit()
        assert a.data["handoff_session_id"] != b.data["handoff_session_id"]
        row_a = db.query(HandoffSession).filter_by(id=a.data["handoff_session_id"]).one()
        row_b = db.query(HandoffSession).filter_by(id=b.data["handoff_session_id"]).one()
        assert row_a.tenant_id == tenant_a.id
        assert row_b.tenant_id == tenant_b.id
        assert row_a.customer_phone == phone
        assert row_b.customer_phone == phone


class TestAiControl:
    def test_customer_staff_request_does_not_pause_ai(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 AI Control Merchant")
        convo = SimpleNamespace(ai_paused=False, paused_by_human=False, taken_over_at=None)
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000111")
        ctx.conversation = convo  # type: ignore[attr-defined]
        source = inspect.getsource(execute_staff_escalation)
        assert "pause_ai" not in source
        assert "ai_paused = True" not in source
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        assert result.data["ai_paused"] is False
        assert convo.ai_paused is False
        assert convo.paused_by_human is False
        assert convo.taken_over_at is None


class TestEvidence:
    def test_action_name_alone_is_not_evidence(self) -> None:
        evidence = evaluate_staff_escalation_evidence(
            chosen_path="ACTION_HANDOFF",
            brain_handoff=True,
        )
        assert evidence.evidence_ok is False
        assert evidence.handoff_session_present is False
        assert evidence.notification_present is False

    def test_real_session_is_queue_evidence(self) -> None:
        evidence = evaluate_staff_escalation_evidence(
            inbound_metadata={"handoff_session_id": 88, "escalation_status": "queued"},
        )
        assert evidence.evidence_ok is True
        assert evidence.handoff_session_present is True
        assert evidence.notification_present is False

    def test_notification_accepted_is_notify_evidence(self) -> None:
        evidence = evaluate_staff_escalation_evidence(
            inbound_metadata={
                "handoff_session_id": 89,
                "notification_accepted": True,
            },
        )
        assert evidence.evidence_ok is True
        assert evidence.notification_present is True

    def test_chosen_path_handoff_never_counts(self) -> None:
        for path in ("ACTION_HANDOFF", "handoff", "action_handoff"):
            evidence = evaluate_staff_escalation_evidence(chosen_path=path)
            assert evidence.evidence_ok is False


class TestComposeFacts:
    def test_overlay_exposes_closed_status_vocabulary(self) -> None:
        overlay = format_staff_escalation_facts_overlay(
            {
                "escalation_requested": True,
                "escalation_status": "queued",
                "handoff_session_created": True,
                "handoff_session_reused": False,
                "notification_attempted": False,
                "notification_accepted": False,
                "verified_contact_available": False,
            }
        )
        assert "[STAFF_ESCALATION_EXECUTION_FACTS]" in overlay
        assert "requested=true" in overlay
        assert "status=queued" in overlay
        assert "notification_accepted=false" in overlay

    def test_compose_receives_overlay_and_does_not_use_template(self) -> None:
        composer = DefaultComposer()
        result = ActionResult(
            success=True,
            data={
                "type": "handoff",
                "escalation_requested": True,
                "escalation_status": "queued",
                "compose_facts_overlay": format_staff_escalation_facts_overlay(
                    {
                        "escalation_requested": True,
                        "escalation_status": "queued",
                        "handoff_session_created": True,
                        "notification_attempted": False,
                        "notification_accepted": False,
                        "verified_contact_available": False,
                    }
                ),
            },
        )
        captured: dict[str, str] = {}

        async def _fake_llm(ctx, action_result, *, decision=None):
            captured["overlay"] = str(action_result.data.get("compose_facts_overlay") or "")
            return "llm-owned wording"

        ctx = BrainContext(
            tenant_id=3,
            customer_phone="966500000112",
            message="ابي موظف",
            intent=Intent(name="talk_to_human", confidence=0.9),
            state=MerchantConversationState(),
            facts=CommerceFacts(),
        )
        with patch.object(composer, "_llm_compose", new=_fake_llm):
            text = _run(composer.compose(_decision(), result, ctx))
        assert text == "llm-owned wording"
        assert "status=queued" in captured["overlay"]
        source = inspect.getsource(DefaultComposer._compose_impl)
        assert "T.handoff(" not in source
        assert "T.handoff_after_hours(" not in source

    def test_old_false_live_promise_is_not_runtime_owner(self) -> None:
        source = inspect.getsource(DefaultComposer._compose_impl)
        handler_src = inspect.getsource(_HandoffHandler.handle)
        executor_src = inspect.getsource(DefaultActionExecutor)
        assert _LIVE_FALSE_PROMISE not in source
        assert "سأنبّه" not in source
        assert "سيرد عليك" not in source
        assert "سأنبّه" not in handler_src
        assert "سأنبّه" not in executor_src


class TestExecutorDispatch:
    def test_default_executor_uses_real_handoff_handler(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Executor Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000113")
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(DefaultActionExecutor().execute(_decision(), ctx))
        db.commit()
        assert result.data["type"] == "handoff"
        assert result.data["escalation_status"] == STATUS_QUEUED
        assert db.query(HandoffSession).count() == 1


class TestPostgresPersistence:
    def test_session_persists_idempotent_and_isolated(self) -> None:
        url = (
            os.environ.get("DATABASE_PUBLIC_URL")
            or os.environ.get("DATABASE_URL")
            or ""
        ).strip()
        if not url or "sqlite" in url.lower() or "railway.internal" in url:
            pytest.skip("no reachable postgres url for D2 persistence proof")
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.engine import create_engine as pg_engine  # noqa: PLC0415

        engine = pg_engine(url)
        Session = sessionmaker(bind=engine)
        db = Session()
        suffix = os.getpid()
        try:
            tenant_a = Tenant(name=f"d2-pg-a-{suffix}", is_active=True)
            tenant_b = Tenant(name=f"d2-pg-b-{suffix}", is_active=True)
            db.add_all([tenant_a, tenant_b])
            db.commit()
            db.refresh(tenant_a)
            db.refresh(tenant_b)
            phone = f"96650007{suffix % 10000:04d}"
            settings = {"notification_method": "none", "webhook_url": ""}
            with patch(
                "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
                return_value=settings,
            ):
                first = _run(
                    execute_staff_escalation(
                        _decision(),
                        _ctx(db=db, tenant_id=tenant_a.id, phone=phone),
                    )
                )
                db.commit()
                second = _run(
                    execute_staff_escalation(
                        _decision(),
                        _ctx(db=db, tenant_id=tenant_a.id, phone=phone),
                    )
                )
                db.commit()
                other = _run(
                    execute_staff_escalation(
                        _decision(),
                        _ctx(db=db, tenant_id=tenant_b.id, phone=phone),
                    )
                )
                db.commit()
            assert first.data["handoff_session_id"] == second.data["handoff_session_id"]
            assert other.data["handoff_session_id"] != first.data["handoff_session_id"]
            persisted = db.execute(
                text(
                    "SELECT id, tenant_id FROM handoff_sessions "
                    "WHERE tenant_id = :tid AND customer_phone = :phone AND status = 'active'"
                ),
                {"tid": tenant_a.id, "phone": phone},
            ).fetchall()
            assert len(persisted) == 1
            assert persisted[0][0] == first.data["handoff_session_id"]
        finally:
            try:
                db.execute(
                    text(
                        "DELETE FROM handoff_sessions WHERE tenant_id IN "
                        "(SELECT id FROM tenants WHERE name LIKE :prefix)"
                    ),
                    {"prefix": f"d2-pg-%-{suffix}"},
                )
                db.execute(
                    text("DELETE FROM tenants WHERE name LIKE :prefix"),
                    {"prefix": f"d2-pg-%-{suffix}"},
                )
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
            db.close()
            engine.dispose()
