"""Fail-open, no-outbound shadow seam for the production V1 turn."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Coroutine

from core.config import (
    COMMERCE_AGENT_V2_ENABLED,
    COMMERCE_AGENT_V2_KILL_SWITCH,
    COMMERCE_AGENT_V2_SHADOW_ONLY,
    COMMERCE_AGENT_V2_TENANT_IDS,
)
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.runner import CommerceAgentRunResult, run_commerce_agent

logger = logging.getLogger("nahla.commerce_agent_v2.shadow")
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s()-]{7,}\d)(?!\d)")


def shadow_enabled_for_tenant(tenant_id: int) -> bool:
    return bool(
        COMMERCE_AGENT_V2_ENABLED
        and COMMERCE_AGENT_V2_SHADOW_ONLY
        and not COMMERCE_AGENT_V2_KILL_SWITCH
        and int(tenant_id) in COMMERCE_AGENT_V2_TENANT_IDS
    )


def _redact_text(value: str) -> str:
    redacted = _EMAIL_RE.sub("[redacted-email]", str(value or ""))
    return _PHONE_RE.sub("[redacted-phone]", redacted)


def _redact_value(value: Any) -> Any:
    """Redact PII recursively before any model-authored output reaches storage."""
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _persist_shadow_result(db: Any, context: CommerceAgentContext, result: CommerceAgentRunResult) -> None:
    from models import CommerceAgentV2ShadowRun
    from modules.ai.orchestrator.ai_usage_ledger import (
        TOKEN_SOURCE_ACTUAL,
        record_ai_usage_event,
    )

    output = _redact_value(result.reply.model_dump(mode="json"))
    row = CommerceAgentV2ShadowRun(
        tenant_id=context.tenant_id,
        conversation_id=context.conversation_id,
        sdk_trace_id=result.sdk_trace_id,
        model=result.model,
        status=result.status,
        structured_output=output,
        tool_trace=result.tool_trace,
        guardrail_results=result.guardrail_results,
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
        failure_reason=(result.failure_reason or None),
    )
    db.add(row)
    record_ai_usage_event(
        db=db,
        tenant_id=context.tenant_id,
        conversation_id=context.conversation_id,
        provider="openai",
        model=result.model,
        reason="commerce_agent_v2_shadow",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        token_source=TOKEN_SOURCE_ACTUAL,
        request_id=result.sdk_trace_id,
    )
    db.commit()


async def _run_shadow_copy(
    *,
    tenant_id: int,
    conversation_id: int,
    customer_id: int | None,
    normalized_customer_phone: str,
    connection_id: str,
    inbound_trace_id: str,
    user_input: str,
) -> None:
    # Own session: no SQLAlchemy object from the V1 request crosses task bounds.
    from session import SessionLocal

    db = SessionLocal()
    try:
        context = CommerceAgentContext.from_trusted_scope(
            db=db,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            normalized_customer_phone=normalized_customer_phone,
            connection_id=connection_id,
            inbound_trace_id=inbound_trace_id,
        )
        result = await run_commerce_agent(context=context, user_input=user_input)
        _persist_shadow_result(db, context, result)
        logger.info(
            "[COMMERCE_V2_SHADOW] tenant=%s conversation=%s status=%s trace=%s latency_ms=%s",
            tenant_id,
            conversation_id,
            result.status,
            result.sdk_trace_id,
            result.latency_ms,
        )
    except Exception as exc:  # noqa: BLE001 — shadow can never fail the V1 turn
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "[COMMERCE_V2_SHADOW_FAILED] tenant=%s conversation=%s error=%s",
            tenant_id,
            conversation_id,
            type(exc).__name__,
        )
    finally:
        db.close()


def schedule_commerce_agent_v2_shadow(
    *,
    tenant_id: int,
    conversation_id: int,
    customer_id: int | None,
    normalized_customer_phone: str,
    connection_id: str,
    inbound_trace_id: str,
    user_input: str,
    task_factory: Callable[[Coroutine[Any, Any, None]], Any] | None = None,
) -> bool:
    """Schedule a read-only copy and return without delaying V1 outbound."""
    if not shadow_enabled_for_tenant(tenant_id):
        return False
    coroutine = _run_shadow_copy(
            tenant_id=int(tenant_id),
            conversation_id=int(conversation_id),
            customer_id=int(customer_id) if customer_id is not None else None,
            normalized_customer_phone=str(normalized_customer_phone or ""),
            connection_id=str(connection_id),
            inbound_trace_id=str(inbound_trace_id),
            user_input=str(user_input or ""),
        )
    if task_factory is not None:
        task_factory(coroutine)
    else:
        task = asyncio.create_task(coroutine, name="commerce-agent-v2-shadow")
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
    return True
