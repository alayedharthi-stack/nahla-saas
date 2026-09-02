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
    verified_record: Any = None,
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
    if verified_record is not None:
        ctx.verified_staff_contact_record = verified_record  # type: ignore[attr-defined]
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
        assert result.data["notification_status"] == "not_attempted"
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
        assert result.data["notification_status"] == "unavailable"
        overlay = result.data["compose_facts_overlay"]
        assert "notification_status=unavailable" in overlay
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
        assert result.data["notification_status"] == "accepted"
        assert result.data["escalation_status"] == STATUS_NOTIFIED
        overlay = result.data["compose_facts_overlay"]
        assert "notification_status=accepted" in overlay
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
        assert "notification_status=failed" in overlay
        assert "notification_failure_code=provider_rejected" in overlay
        assert db.query(HandoffSession).count() == 1

    def test_duplicate_after_accepted_notify_does_not_resend(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Notify Idempotent Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000114")
        notify = AsyncMock(return_value=True)
        settings = {
            "notification_method": "webhook",
            "webhook_url": "https://staff.example.test/handoff",
        }
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value=settings,
        ), patch("handoff.notifier.notify_handoff", new=notify):
            first = _run(execute_staff_escalation(_decision(), ctx))
            db.commit()
            second = _run(execute_staff_escalation(_decision(), ctx))
            db.commit()
        assert first.data["handoff_session_created"] is True
        assert first.data["notification_accepted"] is True
        assert first.data["escalation_status"] == STATUS_NOTIFIED
        assert notify.await_count == 1
        assert second.data["handoff_session_id"] == first.data["handoff_session_id"]
        assert second.data["handoff_session_reused"] is True
        assert second.data["notification_attempted"] is False
        assert second.data["notification_accepted"] is True
        assert second.data["reused_previous_notification"] is True
        assert second.data["notification_status"] == "accepted"
        assert second.data["escalation_status"] == STATUS_NOTIFIED
        assert notify.await_count == 1
        assert db.query(HandoffSession).one().notification_sent is True
        overlay = second.data["compose_facts_overlay"]
        assert "reused_previous_notification=true" in overlay
        assert "notification_status=accepted" in overlay

    def test_duplicate_after_failed_notify_does_not_retry_or_invent_acceptance(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Notify Fail Idempotent Merchant")
        ctx = _ctx(db=db, tenant_id=tenant.id, phone="966500000118")
        notify = AsyncMock(return_value=False)
        settings = {
            "notification_method": "webhook",
            "webhook_url": "https://staff.example.test/handoff",
        }
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value=settings,
        ), patch("handoff.notifier.notify_handoff", new=notify):
            first = _run(execute_staff_escalation(_decision(), ctx))
            db.commit()
            second = _run(execute_staff_escalation(_decision(), ctx))
            db.commit()
        assert first.data["notification_attempted"] is True
        assert first.data["notification_accepted"] is False
        assert first.data["notification_status"] == "failed"
        assert first.data["escalation_status"] == STATUS_QUEUED
        assert notify.await_count == 1
        assert second.data["handoff_session_reused"] is True
        assert second.data["notification_attempted"] is False
        assert second.data["notification_accepted"] is False
        assert second.data["notification_status"] == "not_attempted"
        assert second.data["escalation_status"] == STATUS_QUEUED
        assert notify.await_count == 1
        assert db.query(HandoffSession).one().notification_sent is not True


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

    def test_inbound_metadata_phone_is_not_verified_staff_contact(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Inbound Phone Merchant")
        inbound = {"verified_contact_phone": "966555551111"}
        ctx = _ctx(
            db=db,
            tenant_id=tenant.id,
            phone="966500000115",
            profile={"name": "نورة عبدالله", "inbound_metadata": dict(inbound)},
        )
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        assert result.data["verified_contact_available"] is False
        assert result.data["verified_contact_phone"] == ""

    def test_untyped_profile_phone_is_not_verified_staff_contact(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Profile Phone Merchant")
        ctx = _ctx(
            db=db,
            tenant_id=tenant.id,
            phone="966500000116",
            profile={"name": "أحمد سالم", "verified_contact_phone": "966555551112"},
        )
        ctx.verified_staff_contact_phone = "966555551113"  # type: ignore[attr-defined]
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        assert result.data["verified_contact_available"] is False
        assert result.data["verified_contact_phone"] == ""

    def test_speculative_staff_contact_object_is_not_merchant_truth(self) -> None:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            StaffContactRecord,
        )

        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Verified Phone Merchant")
        record = StaffContactRecord(
            lookup_name="خدمة العملاء",
            phone="966511112222",
            section_id=1,
            role="customer_service",
            aliases=("خدمة العملاء",),
            is_owner=False,
            chain_index=0,
            source="kb_section",
        )
        ctx = _ctx(
            db=db,
            tenant_id=tenant.id,
            phone="966500000109",
            verified_record=record,
        )
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        assert result.data["verified_contact_available"] is False
        assert result.data["verified_contact_phone"] == ""


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
        from modules.ai.brain.execution import staff_escalation_execution as exe  # noqa: PLC0415

        assert "auto_pause_ai" not in exe._DEFAULT_HANDOFF_SETTINGS
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
        assert evidence.queue_evidence_ok is False
        assert evidence.notification_evidence_ok is False
        assert evidence.contact_delivery_evidence_ok is False

    def test_real_session_is_queue_evidence_not_notify(self) -> None:
        evidence = evaluate_staff_escalation_evidence(
            inbound_metadata={
                "handoff_session_id": 88,
                "escalation_status": "queued",
                "notification_accepted": False,
            },
        )
        assert evidence.queue_evidence_ok is True
        assert evidence.notification_evidence_ok is False
        assert evidence.handoff_session_present is True
        assert evidence.notification_present is False

    def test_notification_accepted_is_notify_evidence(self) -> None:
        evidence = evaluate_staff_escalation_evidence(
            inbound_metadata={
                "handoff_session_id": 89,
                "notification_accepted": True,
            },
        )
        assert evidence.notification_evidence_ok is True
        assert evidence.notification_present is True

    def test_conversation_flags_do_not_fabricate_session(self) -> None:
        evidence = evaluate_staff_escalation_evidence(
            conversation_flags={
                "needs_human": True,
                "handoff_active": True,
                "is_human_handoff": True,
                "status": "human",
            },
        )
        assert evidence.handoff_session_present is False
        assert evidence.queue_evidence_ok is False
        assert evidence.notification_evidence_ok is False
        assert evidence.evidence_ok is False

    def test_chosen_path_handoff_never_counts(self) -> None:
        for path in ("ACTION_HANDOFF", "handoff", "action_handoff"):
            evidence = evaluate_staff_escalation_evidence(chosen_path=path)
            assert evidence.evidence_ok is False
            assert evidence.queue_evidence_ok is False
            assert evidence.notification_evidence_ok is False
            assert evidence.contact_delivery_evidence_ok is False

    def test_boolean_session_created_is_not_queue_authority(self) -> None:
        evidence = evaluate_staff_escalation_evidence(
            inbound_metadata={"handoff_session_created": True},
        )
        assert evidence.handoff_session_present is False
        assert evidence.queue_evidence_ok is False
        assert evidence.evidence_ok is False
        assert evidence.notification_evidence_ok is False


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
        assert "notification_status=not_attempted" in overlay

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


class TestInboundMetadataIsolation:
    def test_execution_does_not_mutate_inbound_metadata(self) -> None:
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Inbound Isolation Merchant")
        inbound = {"wa_message_id": "wamid.d2test", "verified_contact_phone": "966555559999"}
        profile = {"name": "أحمد سالم", "inbound_metadata": dict(inbound)}
        ctx = _ctx(
            db=db,
            tenant_id=tenant.id,
            phone="966500000117",
            profile=profile,
        )
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        assert result.data["handoff_session_id"]
        assert ctx.profile["inbound_metadata"] == inbound
        for key in (
            "handoff_session_id",
            "notification_accepted",
            "escalation_status",
            "verified_contact_available",
        ):
            assert key not in ctx.profile["inbound_metadata"]


class TestWireDefenceInDepth:
    _NOTIFY_PROMISE = "سيتواصل معك الفريق قريباً"

    def _queued_session(self, *, notified: bool = False):
        db, _engine = _sqlite_db()
        tenant = _seed_tenant(db, "D2 Wire Defence Merchant")
        phone = "966500000119"
        ctx = _ctx(db=db, tenant_id=tenant.id, phone=phone)
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value={"notification_method": "none", "webhook_url": ""},
        ):
            result = _run(execute_staff_escalation(_decision(), ctx))
        session = db.query(HandoffSession).filter_by(id=result.data["handoff_session_id"]).one()
        session.notification_sent = notified
        db.commit()
        return db, tenant, phone, session

    def test_queue_only_session_authorizes_queue_truth_not_notify(self) -> None:
        from core.handoff_truth import resolve_handoff_truth_active  # noqa: PLC0415

        db, tenant, phone, session = self._queued_session(notified=False)
        truth = resolve_handoff_truth_active(
            db,
            tenant_id=tenant.id,
            customer_phone=phone,
        )
        assert session.id is not None
        assert session.notification_sent is not True
        assert truth.queue_truth is True
        assert truth.notification_truth is False
        assert truth.active is True

    def test_guard_exception_queue_only_promise_is_scrubbed_on_wire(self) -> None:
        from core.outbound_sanitizer import sanitize_outbound_payload  # noqa: PLC0415
        from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: PLC0415
            run_post_compose_truth_guards,
        )

        db, tenant, phone, session = self._queued_session(notified=False)
        convo = SimpleNamespace(
            id=1,
            needs_human=False,
            handoff_active=False,
            is_human_handoff=False,
            status="active",
        )
        with patch(
            "modules.ai.brain.postprocess.staff_escalation_truth_guard.apply_staff_escalation_truth_guard",
            side_effect=RuntimeError("d2_injected_guard_failure"),
        ), patch(
            "core.order_flow._load_brain_state",
            return_value=(None, None),
        ):
            post = run_post_compose_truth_guards(
                db=db,
                tenant_id=tenant.id,
                to=phone,
                text="ابي موظف",
                reply=self._NOTIFY_PROMISE,
                convo=convo,
                inbound_metadata={},
                brain_handoff=True,
                brain_nc_block=False,
                brain_nc_category="",
                br_action="ACTION_HANDOFF",
                brain_persona_compose_event=None,
                mode="primary",
            )
        assert post.reply == self._NOTIFY_PROMISE
        assert any(event.reason == "guard_exception" for event in post.events)

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": post.reply},
        }
        out, scrubbed = sanitize_outbound_payload(
            payload,
            tenant_id=tenant.id,
            recipient=phone,
            db=db,
        )
        assert session.notification_sent is not True
        assert scrubbed is True
        assert self._NOTIFY_PROMISE not in (out.get("text") or {}).get("body", "")

    def test_persisted_notification_sent_allows_notify_promise_on_wire(self) -> None:
        from core.handoff_truth import resolve_handoff_truth_active  # noqa: PLC0415
        from core.outbound_sanitizer import sanitize_outbound_payload  # noqa: PLC0415

        db, tenant, phone, _session = self._queued_session(notified=True)
        truth = resolve_handoff_truth_active(
            db,
            tenant_id=tenant.id,
            customer_phone=phone,
        )
        assert truth.queue_truth is True
        assert truth.notification_truth is True
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": self._NOTIFY_PROMISE},
        }
        out, scrubbed = sanitize_outbound_payload(
            payload,
            tenant_id=tenant.id,
            recipient=phone,
            db=db,
        )
        assert scrubbed is False
        assert out["text"]["body"] == self._NOTIFY_PROMISE

