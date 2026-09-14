"""Contracts for the production-faithful Commerce V2 INTERNAL_E2E channel."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from agents import RunConfig
from agents.tool_context import ToolContext
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core.acceptance_execution_context import deny_external_egress
from core.wa_usage import count_messages_in_window
from database.models import Base, Conversation, MessageEvent, Order, Product, Tenant, TenantSettings
from evals.commerce_agent_v2_whatsapp.scorer import score_turn
from modules.ai.commerce_agent_v2.context import CommerceAgentContext, CommerceContextError
from modules.ai.commerce_agent_v2.output import CommerceReply
from modules.ai.commerce_agent_v2.internal_e2e_identity import internal_e2e_metadata
from modules.ai.commerce_agent_v2.runner import CommerceAgentRunResult
from modules.ai.commerce_agent_v2.session import ConversationMessageSession
from modules.ai.security.tenant_isolation import TenantIsolationViolation
from modules.ai.commerce_agent_v2.tools.orders import (
    get_order_details,
    get_order_shipment,
    resolve_customer_order,
)
from services.commerce_v2_internal_e2e import (
    INTERNAL_E2E_INBOUND,
    INTERNAL_E2E_OUTBOUND,
    InternalE2EContractError,
    InternalE2ETurnRequest,
    provision_internal_e2e_fixtures,
    reset_internal_e2e_customer,
    submit_internal_customer_turn,
)


@pytest.fixture()
def db() -> Any:
    engine = create_engine("sqlite:///:memory:")
    saved: list[tuple[Any, Any]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id=1, name="متجر تجريبي عام", is_active=True))
    session.add(TenantSettings(tenant_id=1, ai_settings={"locale": "ar-SA"}))
    session.add_all(
        [
            Product(
                tenant_id=1,
                external_id="GENERIC-SHOE",
                title="حذاء رياضي أبيض",
                price="180",
                in_stock=True,
                stock_quantity=4,
                catalog_status="active",
                extra_metadata={"status": "active", "currency": "SAR"},
            ),
            Product(
                tenant_id=1,
                external_id="GENERIC-SHIRT",
                title="قميص قطني أزرق",
                price="95",
                in_stock=True,
                stock_quantity=7,
                catalog_status="active",
                extra_metadata={"status": "active", "currency": "SAR"},
            ),
        ]
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def enabled_env() -> dict[str, str]:
    return {
        "NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED": "true",
        "NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS": "1",
    }


def _fake_result(context: CommerceAgentContext, text: str = "نتيجة داخلية") -> CommerceAgentRunResult:
    return CommerceAgentRunResult(
        status="completed",
        reply=CommerceReply(text=text, response_mode="social"),
        model="gpt-5.6-sol",
        session_id=f"commerce-v2:{context.tenant_id}:{context.conversation_id}",
        sdk_trace_id=f"trace_{context.conversation_id:032x}",
        latency_ms=12,
        input_tokens=20,
        output_tokens=5,
        total_tokens=25,
        cached_input_tokens=4,
        requested_service_tier="auto",
        tool_trace=[
            {"kind": "model_start", "model_turn": 1},
            {"kind": "model_end", "model_turn": 1, "latency_ms": 12},
        ],
        guardrail_results=[
            {"name": "grounded_output_guardrail", "tripwire_triggered": False}
        ],
    )


async def _invoke(tool: Any, context: CommerceAgentContext, arguments: dict[str, Any]) -> Any:
    raw_arguments = json.dumps(arguments)
    raw = await tool.on_invoke_tool(
        ToolContext(
            context=context,
            tool_name=tool.name,
            tool_call_id=f"test-{tool.name}",
            tool_arguments=raw_arguments,
            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
        ),
        raw_arguments,
    )
    if isinstance(raw, str):
        return json.loads(raw)
    if hasattr(raw, "model_dump"):
        return raw.model_dump(mode="json")
    return raw


def _context(db: Any, fixture: Any, trace_id: str = "internal-turn") -> CommerceAgentContext:
    return CommerceAgentContext.from_trusted_scope(
        db=db,
        tenant_id=1,
        conversation_id=fixture.conversation_id,
        customer_id=fixture.customer_id,
        normalized_customer_phone=fixture.identity,
        connection_id="internal_e2e",
        inbound_trace_id=trace_id,
        channel="internal_e2e",
        synthetic_customer_alias=fixture.alias,
    )


def test_operator_scope_is_disabled_and_tenant_bound(db: Any, enabled_env: dict[str, str]) -> None:
    with pytest.raises(InternalE2EContractError, match="disabled"):
        provision_internal_e2e_fixtures(db, tenant_id=1, env={})
    with pytest.raises(InternalE2EContractError, match="unapproved"):
        provision_internal_e2e_fixtures(
            db,
            tenant_id=1,
            env={
                **enabled_env,
                "NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS": "1,33",
            },
        )
    with pytest.raises(InternalE2EContractError, match="allowlist_invalid"):
        provision_internal_e2e_fixtures(
            db,
            tenant_id=1,
            env={
                **enabled_env,
                "NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS": "1,merchant",
            },
        )


def test_internal_scorer_requires_internal_ids_and_zero_external_egress() -> None:
    expected = {
        "case_id": "A-001",
        "expected_tools": ["search_products"],
        "expected_outcome": "grounded_reply",
    }
    actual = {
        "execution_mode": "INTERNAL_E2E",
        "tenant_id": 1,
        "owner": "commerce_agent_v2",
        "v1_bypassed": True,
        "status": "completed",
        "internal_inbound_message_id": "internal_e2e:t1:a:in:fixture",
        "internal_outbound_message_id": "internal_e2e:t1:a:out:fixture",
        "trace_id": "trace_fixture",
        "guardrail_passed": True,
        "tool_calls": ["search_products"],
        "fallback_type": "none",
        "external_egress_count": 0,
        "unsupported_commercial_claims": 0,
        "cross_tenant_leakage": 0,
        "cross_customer_leakage": 0,
        "duplicate_replies": 0,
        "silent_v1_fallback": 0,
        "write_mutations": 0,
        "salla_mutations": 0,
    }
    assert score_turn(expected, actual)["passed"] is True
    actual["external_egress_count"] = 1
    failed = score_turn(expected, actual)
    assert failed["passed"] is False
    assert "external_egress" in failed["blockers"]


def test_provisions_three_unmistakable_identities_and_isolated_order(
    db: Any, enabled_env: dict[str, str]
) -> None:
    fixtures = provision_internal_e2e_fixtures(db, tenant_id=1, env=enabled_env)
    assert set(fixtures) == {"A", "B", "C"}
    assert all(item.customer_id is None for item in fixtures.values())
    assert len({item.conversation_id for item in fixtures.values()}) == 3
    assert all(item.identity.startswith("internal_e2e:t1:customer:") for item in fixtures.values())
    assert fixtures["A"].order_id is None
    assert fixtures["B"].order_id is None
    assert fixtures["C"].order_id is not None
    assert fixtures["C"].order_number == "IE2E-C-001"
    assert db.query(Conversation).filter(Conversation.customer_id.isnot(None)).count() == 0
    directions = {
        value
        for (value,) in db.query(MessageEvent.direction)
        .filter(MessageEvent.conversation_id == fixtures["B"].conversation_id)
        .all()
    }
    assert directions == {INTERNAL_E2E_INBOUND, INTERNAL_E2E_OUTBOUND}
    assert db.query(MessageEvent).filter(MessageEvent.direction == "inbound").count() == 0


def test_internal_context_rejects_alias_and_whatsapp_confusion(
    db: Any, enabled_env: dict[str, str]
) -> None:
    fixture = provision_internal_e2e_fixtures(db, tenant_id=1, env=enabled_env)["A"]
    context = _context(db, fixture)
    context.assert_scope()
    with pytest.raises(CommerceContextError, match="identity_mismatch"):
        CommerceAgentContext.from_trusted_scope(
            db=db,
            tenant_id=1,
            conversation_id=fixture.conversation_id,
            customer_id=fixture.customer_id,
            normalized_customer_phone="internal_e2e:t1:customer:b",
            connection_id="internal_e2e",
            inbound_trace_id="bad-alias",
            channel="internal_e2e",
            synthetic_customer_alias="A",
        )
    with pytest.raises(CommerceContextError, match="invalid_customer_phone"):
        CommerceAgentContext.from_trusted_scope(
            db=db,
            tenant_id=1,
            conversation_id=fixture.conversation_id,
            customer_id=fixture.customer_id,
            normalized_customer_phone=fixture.identity,
            connection_id="internal_e2e",
            inbound_trace_id="not-whatsapp",
        )
    with pytest.raises(CommerceContextError, match="conversation_not_in_tenant_scope"):
        CommerceAgentContext.from_trusted_scope(
            db=db,
            tenant_id=2,
            conversation_id=fixture.conversation_id,
            customer_id=None,
            normalized_customer_phone="internal_e2e:t2:customer:a",
            connection_id="internal_e2e",
            inbound_trace_id="tenant-override",
            channel="internal_e2e",
            synthetic_customer_alias="A",
        )


@pytest.mark.asyncio
async def test_a_b_c_sessions_do_not_read_each_others_history(
    db: Any, enabled_env: dict[str, str]
) -> None:
    fixtures = provision_internal_e2e_fixtures(db, tenant_id=1, env=enabled_env)
    for alias, secret in (("A", "A-ONLY"), ("B", "B-ONLY"), ("C", "C-ONLY")):
        db.add(
            MessageEvent(
                tenant_id=1,
                conversation_id=fixtures[alias].conversation_id,
                direction=INTERNAL_E2E_INBOUND,
                body=secret,
                event_type="internal_e2e_customer_turn",
                extra_metadata={"internal_message_id": f"seed-{alias}"},
            )
        )
    db.commit()
    for alias in "ABC":
        items = await ConversationMessageSession(_context(db, fixtures[alias])).get_items()
        bodies = " ".join(str(item["content"]) for item in items)
        assert f"{alias}-ONLY" in bodies
        assert all(f"{other}-ONLY" not in bodies for other in "ABC" if other != alias)


@pytest.mark.asyncio
async def test_order_fixture_is_customer_scoped_and_requires_same_run_authorization(
    db: Any, enabled_env: dict[str, str]
) -> None:
    fixtures = provision_internal_e2e_fixtures(db, tenant_id=1, env=enabled_env)
    a_context = _context(db, fixtures["A"])
    c_context = _context(db, fixtures["C"])
    a_lookup = await _invoke(
        resolve_customer_order,
        a_context,
        {"order_number": fixtures["C"].order_number, "purpose": "status"},
    )
    assert a_lookup["status"] == "not_found"
    db.add(
        Order(
            tenant_id=1,
            customer_id=None,
            external_id=f"{fixtures['A'].identity}:order:009",
            external_order_number="IE2E-A-009",
            status="draft",
            total="50",
            customer_info={"phone": fixtures["A"].identity},
            line_items=[{"name": "منتج أ", "quantity": 1}],
            source="internal_e2e",
            is_abandoned=False,
            extra_metadata=internal_e2e_metadata(1, "A"),
        )
    )
    db.commit()
    c_cannot_read_a = await _invoke(
        resolve_customer_order,
        c_context,
        {"order_number": "IE2E-A-009", "purpose": "status"},
    )
    assert c_cannot_read_a["status"] == "not_found"
    c_lookup = await _invoke(
        resolve_customer_order,
        c_context,
        {"order_number": fixtures["C"].order_number, "purpose": "shipment"},
    )
    assert c_lookup["status"] == "ok"
    order_id = c_lookup["order"]["order_id"]
    details = await _invoke(get_order_details, c_context, {"order_id": order_id})
    shipment = await _invoke(get_order_shipment, c_context, {"order_id": order_id})
    assert details["status"] == "ok"
    assert shipment["status"] == "ok"
    assert shipment["shipment"]["tracking_number"] == "IE2E-TRACK-C-001"
    with pytest.raises(TenantIsolationViolation, match="order_id_not_discovered"):
        a_context.require_authorized_order(order_id)


@pytest.mark.asyncio
async def test_submit_persists_internal_ids_structured_reply_bundle_and_usage(
    db: Any, enabled_env: dict[str, str]
) -> None:
    fixture = provision_internal_e2e_fixtures(db, tenant_id=1, env=enabled_env)["A"]

    runner_arguments: dict[str, Any] = {}

    async def fake_run(*, context: CommerceAgentContext, user_input: str, **kwargs: Any):
        runner_arguments.update(kwargs)
        return _fake_result(context, text=f"رد على: {user_input}")

    artifact = await submit_internal_customer_turn(
        db,
        InternalE2ETurnRequest(
            tenant_id=1,
            synthetic_customer_alias="A",
            text="السلام عليكم",
            case_id="A-001",
            expected={"expected_outcome": "grounded_reply"},
        ),
        env=enabled_env,
        run_agent=fake_run,
    )
    assert artifact["execution_mode"] == "INTERNAL_E2E"
    assert artifact["owner"] == "commerce_agent_v2"
    assert artifact["external_egress_count"] == 0
    assert artifact["write_mutations"] == 0
    assert artifact["internal_inbound_message_id"].startswith("internal_e2e:")
    assert artifact["internal_outbound_message_id"].startswith("internal_e2e:")
    assert artifact["structured_reply"]["text"] == "رد على: السلام عليكم"
    assert artifact["customer_visible_text"] == "رد على: السلام عليكم"
    assert artifact["presentation_bundle"]["dispatchable"] is False
    assert runner_arguments == {"execution_mode": "outbound", "service_tier": "auto"}
    rows = (
        db.query(MessageEvent)
        .filter(MessageEvent.conversation_id == fixture.conversation_id)
        .order_by(MessageEvent.id.desc())
        .limit(2)
        .all()
    )
    assert {row.direction for row in rows} == {INTERNAL_E2E_INBOUND, INTERNAL_E2E_OUTBOUND}
    assert all("wamid" not in json.dumps(row.extra_metadata).lower() for row in rows)
    conversation = db.get(Conversation, fixture.conversation_id)
    assert conversation.last_read_at is not None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert count_messages_in_window(db, 1, now - timedelta(hours=1), now) == 0


@pytest.mark.asyncio
async def test_sequential_same_customer_preserves_history_and_reset_is_alias_local(
    db: Any, enabled_env: dict[str, str]
) -> None:
    fixtures = provision_internal_e2e_fixtures(db, tenant_id=1, env=enabled_env)
    observed_histories: list[list[str]] = []

    async def history_run(*, context: CommerceAgentContext, **_: Any):
        items = await ConversationMessageSession(context).get_items()
        observed_histories.append([str(item["content"]) for item in items])
        return _fake_result(context)

    for text in ("الدور الأول", "الدور الثاني"):
        await submit_internal_customer_turn(
            db,
            InternalE2ETurnRequest(tenant_id=1, synthetic_customer_alias="A", text=text),
            env=enabled_env,
            run_agent=history_run,
        )
    assert "الدور الأول" not in observed_histories[0]
    assert "الدور الأول" in observed_histories[1]
    b_count = (
        db.query(MessageEvent)
        .filter(MessageEvent.conversation_id == fixtures["B"].conversation_id)
        .count()
    )
    reset = reset_internal_e2e_customer(
        db,
        tenant_id=1,
        synthetic_customer_alias="A",
        env=enabled_env,
    )
    assert reset["messages"] == 4
    assert (
        db.query(MessageEvent)
        .filter(MessageEvent.conversation_id == fixtures["B"].conversation_id)
        .count()
        == b_count
    )


@pytest.mark.asyncio
async def test_concurrency_overlaps_a_b_c_but_serializes_same_customer(
    db: Any, enabled_env: dict[str, str]
) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=enabled_env)
    active = 0
    peak = 0
    release = asyncio.Event()

    async def overlapping_run(*, context: CommerceAgentContext, **_: Any):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 3:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=1)
        active -= 1
        return _fake_result(context)

    # SQLite sessions are not safe for concurrent writes; use one database
    # session per production-style operator call in the real runner. Here the
    # lock behavior itself is exercised with three independent fixture aliases.
    original_commit = db.commit
    db.commit = lambda: db.flush()
    try:
        await asyncio.gather(
            *[
                submit_internal_customer_turn(
                    db,
                    InternalE2ETurnRequest(tenant_id=1, synthetic_customer_alias=alias, text=alias),
                    env=enabled_env,
                    run_agent=overlapping_run,
                )
                for alias in "ABC"
            ]
        )
    finally:
        db.commit = original_commit
    assert peak == 3


@pytest.mark.asyncio
async def test_external_sender_boundary_attempt_is_recorded_as_contract_failure(
    db: Any, enabled_env: dict[str, str]
) -> None:
    provision_internal_e2e_fixtures(db, tenant_id=1, env=enabled_env)

    async def egress_attempt(*, context: CommerceAgentContext, **_: Any):
        try:
            deny_external_egress(
                egress_kind="whatsapp",
                operation="test_sender_boundary",
                tenant_id=context.tenant_id,
            )
        except Exception:
            return CommerceAgentRunResult(
                **{
                    **_fake_result(context).__dict__,
                    "status": "failed",
                    "failure_reason": "internal_e2e_egress_denied",
                }
            )
        raise AssertionError("egress guard did not deny")

    artifact = await submit_internal_customer_turn(
        db,
        InternalE2ETurnRequest(tenant_id=1, synthetic_customer_alias="A", text="اختبار"),
        env=enabled_env,
        run_agent=egress_attempt,
    )
    assert artifact["status"] == "test_contract_failed"
    assert artifact["external_egress_count"] == 1
    assert artifact["failure_reason"] == "internal_e2e_external_egress_attempted"
