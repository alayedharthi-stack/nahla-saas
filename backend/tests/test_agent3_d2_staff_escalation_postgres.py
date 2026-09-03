"""AGENT3-D2 PostgreSQL persistence proof.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO

Requires a reachable Postgres URL. In CI A1 this is required
(A1_PG_INTEGRATION_REQUIRED=1) and must not skip.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import ACTION_HANDOFF  # noqa: E402
from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: E402
    STATUS_NOTIFIED,
    STATUS_QUEUED,
    execute_staff_escalation,
    execute_staff_escalation_for_safety_signal,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from models import HandoffSession, Tenant  # noqa: E402
from tests.order_customer_identity_postgres_fixtures import (  # noqa: E402
    _connect_engine,
    _ensure_a1_schema,
    _integration_required,
)

if not _integration_required():
    pytest.skip(
        "PostgreSQL integration tests require A1_PG_INTEGRATION_REQUIRED=1",
        allow_module_level=True,
    )

pytestmark = pytest.mark.usefixtures("postgres_engine")


@pytest.fixture(scope="module")
def postgres_engine():
    engine = _connect_engine()
    _ensure_a1_schema(engine)
    yield engine
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


def _ctx(*, db: Any, tenant_id: int, phone: str) -> BrainContext:
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message="ابي موظف",
        intent=Intent(name="talk_to_human", confidence=0.95),
        state=MerchantConversationState(),
        facts=CommerceFacts(store_name="متجر تجريبي عام"),
        profile={"name": "أحمد سالم"},
    )
    ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _decision() -> Decision:
    return Decision(action=ACTION_HANDOFF, args={}, reason="customer_request")


def test_handoff_session_persists_idempotent_isolated_and_notification_sent(
    postgres_engine,
) -> None:
    Session = sessionmaker(bind=postgres_engine)
    db = Session()
    suffix = uuid.uuid4().hex[:10]
    name_a = f"d2-pg-a-{suffix}"
    name_b = f"d2-pg-b-{suffix}"
    phone = f"96650008{suffix[:4]}"
    try:
        tenant_a = Tenant(name=name_a, is_active=True)
        tenant_b = Tenant(name=name_b, is_active=True)
        db.add_all([tenant_a, tenant_b])
        db.commit()
        db.refresh(tenant_a)
        db.refresh(tenant_b)

        notify = AsyncMock(return_value=True)
        settings = {
            "notification_method": "webhook",
            "webhook_url": "https://staff.example.test/handoff",
        }
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value=settings,
        ), patch("handoff.notifier.notify_handoff", new=notify):
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

        other_settings = {"notification_method": "none", "webhook_url": ""}
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value=other_settings,
        ):
            other = _run(
                execute_staff_escalation(
                    _decision(),
                    _ctx(db=db, tenant_id=tenant_b.id, phone=phone),
                )
            )
            db.commit()

        assert first.success is True
        assert first.data["escalation_status"] == STATUS_NOTIFIED
        assert first.data["notification_accepted"] is True
        assert first.data["handoff_session_id"] == second.data["handoff_session_id"]
        assert second.data["handoff_session_reused"] is True
        assert second.data["notification_attempted"] is False
        assert second.data["reused_previous_notification"] is True
        assert notify.await_count == 1
        assert other.data["handoff_session_id"] != first.data["handoff_session_id"]
        assert other.data["escalation_status"] == STATUS_QUEUED

        row_a = db.query(HandoffSession).filter_by(id=first.data["handoff_session_id"]).one()
        row_b = db.query(HandoffSession).filter_by(id=other.data["handoff_session_id"]).one()
        assert row_a.tenant_id == tenant_a.id
        assert row_b.tenant_id == tenant_b.id
        assert row_a.notification_sent is True
        assert row_a.status == "active"
        from core.handoff_truth import resolve_handoff_truth_active  # noqa: PLC0415

        truth_a = resolve_handoff_truth_active(
            db,
            tenant_id=tenant_a.id,
            customer_phone=phone,
        )
        assert truth_a.queue_truth is True
        assert truth_a.notification_truth is True
        truth_b = resolve_handoff_truth_active(
            db,
            tenant_id=tenant_b.id,
            customer_phone=phone,
        )
        assert truth_b.queue_truth is True
        assert truth_b.notification_truth is False
        assert (
            db.query(HandoffSession)
            .filter_by(tenant_id=tenant_a.id, customer_phone=phone, status="active")
            .count()
            == 1
        )
    finally:
        from sqlalchemy import text  # noqa: PLC0415

        try:
            db.execute(
                text(
                    "DELETE FROM handoff_sessions WHERE tenant_id IN "
                    "(SELECT id FROM tenants WHERE name IN (:a, :b))"
                ),
                {"a": name_a, "b": name_b},
            )
            db.execute(
                text("DELETE FROM tenants WHERE name IN (:a, :b)"),
                {"a": name_a, "b": name_b},
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        db.close()


def test_safety_signal_wrapper_reuses_same_d2_session(postgres_engine) -> None:
    Session = sessionmaker(bind=postgres_engine)
    db = Session()
    suffix = uuid.uuid4().hex[:10]
    name = f"d2-pg-safety-{suffix}"
    phone = f"96650009{suffix[:4]}"
    try:
        tenant = Tenant(name=name, is_active=True)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        convo = SimpleNamespace(id=None, needs_human=False)
        settings = {"notification_method": "none", "webhook_url": ""}
        with patch(
            "modules.ai.brain.execution.staff_escalation_execution._load_tenant_handoff_settings",
            return_value=settings,
        ):
            first = _run(
                execute_staff_escalation_for_safety_signal(
                    db=db,
                    tenant_id=tenant.id,
                    customer_phone=phone,
                    message="أريد التحدث مع موظف من المتجر",
                    convo=convo,
                )
            )
            second = _run(
                execute_staff_escalation_for_safety_signal(
                    db=db,
                    tenant_id=tenant.id,
                    customer_phone=phone,
                    message="نعم أريد موظف يساعدني",
                    convo=convo,
                )
            )
        db.commit()
        assert first.success is True
        assert second.data["handoff_session_id"] == first.data["handoff_session_id"]
        assert second.data["handoff_session_reused"] is True
        assert convo.needs_human is True
        assert (
            db.query(HandoffSession)
            .filter_by(tenant_id=tenant.id, customer_phone=phone, status="active")
            .count()
            == 1
        )
    finally:
        from sqlalchemy import text  # noqa: PLC0415

        try:
            db.execute(
                text(
                    "DELETE FROM handoff_sessions WHERE tenant_id IN "
                    "(SELECT id FROM tenants WHERE name = :n)"
                ),
                {"n": name},
            )
            db.execute(text("DELETE FROM tenants WHERE name = :n"), {"n": name})
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        db.close()
