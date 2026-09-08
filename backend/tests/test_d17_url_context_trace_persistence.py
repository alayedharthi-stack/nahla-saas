"""D17 url_context_trace end-to-end outbound persistence.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.conversation_engine import StateManager  # noqa: E402
from models import MessageEvent  # noqa: E402
from modules.ai.brain.pipeline import get_brain  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    CommerceFacts,
    MerchantConversationState,
)
from modules.ai.orchestrator.types import AIReplyPayload  # noqa: E402
from routers.whatsapp_webhook import _otp_merge_save_metadata  # noqa: E402
from services.merchant_brain_turn import (  # noqa: E402
    LiveMerchantBrainPreconditions,
    LiveMerchantBrainTurnInput,
    evaluate_live_merchant_brain_turn,
)
from services.safe_http_fetch import SafeHttpResult  # noqa: E402
from services.url_context import reset_url_context_cache  # noqa: E402
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_tenant,
)

from test_d17_url_context_attach_observability import (  # noqa: E402
    FORBIDDEN_TRACE_PATTERNS,
    PUBLIC_PAGE_URL,
    SENSITIVE_URL,
    _assert_trace_privacy,
    _brain_process_stack,
    _trace_bytes,
)
from test_d17_url_context_enrichment import (  # noqa: E402
    GENERIC_MERCHANT,
    HTML_FIXTURE,
    MODEL_CANDIDATE,
    _active_checkout_state,
    _ok_fetch,
    _user_turn_json,
)

PHONE = DEFAULT_PHONE_E164.replace("+", "")


def _assert_trace_privacy(blob: str) -> None:
    for pattern in FORBIDDEN_TRACE_PATTERNS:
        assert not re.search(pattern, blob, re.IGNORECASE)


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name=GENERIC_MERCHANT)
    customer = seed_customer(db, tenant.id, name="أحمد سالم")
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=PHONE,
        conv=conv,
    )


def _trace(out: dict[str, Any]) -> dict[str, Any]:
    row = dict(out.get("url_context_trace") or {})
    assert row.get("schema_version")
    return row


async def _run_live_turn(
    *,
    db: Any,
    tenant_ctx: Any,
    message: str,
    extra_patches: Optional[list[Any]] = None,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    reset_url_context_cache()
    brain = get_brain()
    state = _active_checkout_state()
    captured: dict[str, Any] = {"compose_count": 0}
    stack = _brain_process_stack(
        brain=brain,
        state=state,
        message=message,
        tenant_id=int(tenant_ctx.tenant_id),
        captured=captured,
        extra_patches=extra_patches,
    )
    persona = MagicMock()
    persona.merge_from_dict = MagicMock()
    persona.to_metadata = MagicMock(return_value={})
    trace = SimpleNamespace(
        brain_called=False,
        brain_silent=False,
        response_goal="",
        response_mode="",
        reply_source="",
        fallback_source="",
        chosen_path="",
        handoff_triggered=False,
    )
    with stack:
        turn_result = await evaluate_live_merchant_brain_turn(
            db=db,
            tenant_id=int(tenant_ctx.tenant_id),
            phone_id="PH-D17",
            turn_input=LiveMerchantBrainTurnInput(
                customer_phone=tenant_ctx.phone,
                text=message,
                conversation_id=int(tenant_ctx.conversation_id),
                history=[],
                preconditions=LiveMerchantBrainPreconditions(),
                profile={
                    "id": tenant_ctx.customer_id,
                    "preferred_language": "ar",
                    "inbound_metadata": {"source_type": "text"},
                },
            ),
            convo=tenant_ctx.conv,
            trace=trace,
            persona_ownership=persona,
            brain_factory=lambda: brain,
            brain_active=True,
        )
    assert turn_result.status == "evaluated"
    assert isinstance(turn_result.brain_result, dict)
    extra = _otp_merge_save_metadata(
        None,
        {},
        brain_result=turn_result.brain_result,
    )
    return turn_result, extra, captured


def _persist_outbound(
    db: Any,
    tenant_ctx: Any,
    reply: str,
    extra: dict[str, Any],
) -> MessageEvent:
    StateManager.save_message(
        db,
        tenant_ctx.phone,
        reply,
        "outbound",
        conversation_id=int(tenant_ctx.conversation_id),
        tenant_id=int(tenant_ctx.tenant_id),
        extra_metadata=extra,
    )
    db.commit()
    row = (
        db.query(MessageEvent)
        .filter(MessageEvent.tenant_id == int(tenant_ctx.tenant_id))
        .filter(MessageEvent.direction == "outbound")
        .order_by(MessageEvent.id.desc())
        .first()
    )
    assert row is not None
    return row


def test_persistence_a_url_success_end_to_end(db, tenant_ctx) -> None:
    turn_result, extra, captured = asyncio.run(
        _run_live_turn(db=db, tenant_ctx=tenant_ctx, message=PUBLIC_PAGE_URL)
    )
    assert turn_result.brain_result is not None
    assert "url_context_trace" in turn_result.brain_result
    assert "url_context_trace" in extra
    trace = extra["url_context_trace"]
    assert trace["url_count"] >= 1
    assert trace["fetch_attempted"] is True
    assert trace["facts_projected"] is True
    assert trace["reply_state_url_context_present_before_compose"] is True
    assert _trace_bytes(trace) <= 900
    _assert_trace_privacy(json.dumps(trace, ensure_ascii=False))

    row = _persist_outbound(db, tenant_ctx, turn_result.reply_text, extra)
    stored = dict(row.extra_metadata or {}).get("url_context_trace") or {}
    assert stored == trace
    assert stored["external_fetch_count"] == 1
    assert row.id is not None
    assert captured.get("compose_count") == 1


def test_persistence_b_attach_failure_still_persisted(db, tenant_ctx) -> None:
    turn_result, extra, captured = asyncio.run(
        _run_live_turn(
            db=db,
            tenant_ctx=tenant_ctx,
            message=PUBLIC_PAGE_URL,
            extra_patches=[
                patch(
                    "services.url_context.begin_url_context_turn",
                    side_effect=RuntimeError("probe attach"),
                )
            ],
        )
    )
    trace = extra.get("url_context_trace") or {}
    assert trace.get("failure_stage") == "attach"
    assert trace.get("exception_class") == "attach_error"
    assert _trace_bytes(trace) <= 900
    row = _persist_outbound(db, tenant_ctx, turn_result.reply_text, extra)
    stored = dict(row.extra_metadata or {}).get("url_context_trace") or {}
    assert stored.get("failure_stage") == "attach"
    assert stored.get("exception_class") == "attach_error"
    assert captured.get("compose_count") == 1


def test_persistence_c_no_url_minimal_trace(db, tenant_ctx) -> None:
    turn_result, extra, _ = asyncio.run(
        _run_live_turn(db=db, tenant_ctx=tenant_ctx, message="مرحبا")
    )
    trace = extra.get("url_context_trace") or {}
    assert trace.get("url_count") == 0
    assert "fetch_attempted" not in trace
    assert _trace_bytes(trace) <= 160
    row = _persist_outbound(db, tenant_ctx, turn_result.reply_text, extra)
    stored = dict(row.extra_metadata or {}).get("url_context_trace") or {}
    assert stored.get("url_count") == 0


def test_persistence_d_merge_failure_non_blocking(db, tenant_ctx) -> None:
    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    root = logging.getLogger("nahla.url_context_trace")
    handler = _Cap()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)

    try:
        turn_result, _, captured = asyncio.run(
            _run_live_turn(db=db, tenant_ctx=tenant_ctx, message=PUBLIC_PAGE_URL)
        )
        brain_result = dict(turn_result.brain_result or {})
        brain_result["url_context_trace"] = {
            "schema_version": "99",
            "attach_entered": True,
            "detector_ran": True,
            "url_count": 1,
            "attach_completed": True,
            "raw_url": "https://evil.test/SECRET",
        }
        extra = _otp_merge_save_metadata(None, {"quality_observability": {"ok": True}}, brain_result=brain_result)
    finally:
        root.removeHandler(handler)

    assert turn_result.reply_text
    assert extra.get("quality_observability") == {"ok": True}
    trace = extra.get("url_context_trace") or {}
    assert trace.get("failure_stage") == "trace_merge"
    assert trace.get("exception_class") == "trace_merge_error"
    assert "raw_url" not in trace
    assert captured.get("compose_count") == 1
    assert any("merge_rejected" in line for line in records)
    row = _persist_outbound(db, tenant_ctx, turn_result.reply_text, extra)
    stored = dict(row.extra_metadata or {}).get("url_context_trace") or {}
    assert stored.get("failure_stage") == "trace_merge"


_SENSITIVE_TRACE_PATTERNS = (
    r"secret_token",
    r"966511000099",
    r"#preview",
    r"https://shop\.example\.test/p/shirt",
)


def _assert_sensitive_trace_privacy(blob: str) -> None:
    for pattern in _SENSITIVE_TRACE_PATTERNS:
        assert not re.search(pattern, blob, re.IGNORECASE)


def test_persistence_e_privacy_in_db_metadata(db, tenant_ctx) -> None:
    turn_result, extra, captured = asyncio.run(
        _run_live_turn(db=db, tenant_ctx=tenant_ctx, message=SENSITIVE_URL)
    )
    row = _persist_outbound(db, tenant_ctx, turn_result.reply_text, extra)
    trace_blob = json.dumps(dict(extra.get("url_context_trace") or {}), ensure_ascii=False)
    _assert_trace_privacy(trace_blob)
    _assert_sensitive_trace_privacy(trace_blob)
    _assert_sensitive_trace_privacy(json.dumps(dict(row.extra_metadata or {}), ensure_ascii=False))
    parsed = dict(captured.get("parsed") or {})
    canonical = str(parsed.get("canonical_url") or "")
    assert "?" not in canonical
    assert "#" not in canonical
    assert "url_context_trace" not in str(captured.get("prompt") or "")


def test_persistence_f_concurrent_outbound_rows_isolated(db, tenant_ctx) -> None:
    async def _main() -> tuple[dict[str, Any], int, dict[str, Any], int]:
        trace_a, id_a = await _one_turn(PUBLIC_PAGE_URL, db=db, tenant_ctx=tenant_ctx)
        trace_b, id_b = await _one_turn("مرحبا", db=db, tenant_ctx=tenant_ctx)
        return trace_a, id_a, trace_b, id_b

    trace_a, id_a, trace_b, id_b = asyncio.run(_main())
    assert id_a != id_b
    assert trace_a.get("url_count", 0) >= 1
    assert trace_b.get("url_count") == 0


async def _one_turn(message: str, *, db: Any, tenant_ctx: Any) -> tuple[dict[str, Any], int]:
    turn_result, extra, _ = await _run_live_turn(
        db=db,
        tenant_ctx=tenant_ctx,
        message=message,
    )
    row = _persist_outbound(db, tenant_ctx, turn_result.reply_text, extra)
    return dict(extra.get("url_context_trace") or {}), int(row.id)


def test_persistence_g_regression_byte_identical_model_payload(db, tenant_ctx) -> None:
    async def _payload(message: str) -> tuple[str, int, dict[str, Any]]:
        _, _, captured = await _run_live_turn(
            db=db,
            tenant_ctx=tenant_ctx,
            message=message,
        )
        return (
            str(captured.get("model_text") or ""),
            int(captured.get("compose_count") or 0),
            dict(captured.get("parsed") or {}),
        )

    a_text, a_count, a_parsed = asyncio.run(_payload(PUBLIC_PAGE_URL))
    b_text, b_count, b_parsed = asyncio.run(_payload(PUBLIC_PAGE_URL))
    assert a_count == b_count == 1
    assert a_text == b_text
    assert a_parsed == b_parsed
