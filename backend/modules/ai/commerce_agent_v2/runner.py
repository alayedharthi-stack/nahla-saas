"""Official Agents SDK runner boundary for Commerce Agent V2."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from agents import (
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
)

from core.config import (
    COMMERCE_AGENT_V2_MODEL,
    COMMERCE_AGENT_V2_REASONING_EFFORT,
    COMMERCE_AGENT_V2_TIMEOUT_SECONDS,
)
from modules.ai.commerce_agent_v2.agent import build_commerce_agent
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import CommerceReply
from modules.ai.commerce_agent_v2.session import ConversationMessageSession
from modules.ai.commerce_agent_v2.tracing import CommerceTracingHooks, sdk_trace_id


@dataclass(frozen=True)
class CommerceAgentRunResult:
    status: str
    reply: CommerceReply
    model: str
    session_id: str
    sdk_trace_id: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    guardrail_results: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str = ""


def _fallback(reason: str) -> CommerceReply:
    return CommerceReply(
        text="لا تتوفر لدي معلومة موثوقة كافية للإجابة الآن.",
        safe_fallback_reason=reason[:240],
    )


async def run_commerce_agent(
    *,
    context: CommerceAgentContext,
    user_input: str,
    model: str | Any | None = None,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: float | None = None,
) -> CommerceAgentRunResult:
    """Run one read-only turn with the official SDK and structured output."""
    configured_model = model if model is not None else COMMERCE_AGENT_V2_MODEL
    observable_model_name = model_name or (
        configured_model if isinstance(configured_model, str) else type(configured_model).__name__
    )
    session = ConversationMessageSession(context)
    hooks = CommerceTracingHooks(model=str(observable_model_name))
    started = time.monotonic()
    trace_id = sdk_trace_id(context.inbound_trace_id)
    try:
        agent = build_commerce_agent(
            model=configured_model,
            reasoning_effort=reasoning_effort or COMMERCE_AGENT_V2_REASONING_EFFORT,
        )
        run = await asyncio.wait_for(
            Runner.run(
                agent,
                str(user_input or ""),
                context=context,
                session=session,
                hooks=hooks,
                max_turns=6,
                run_config=RunConfig(
                    workflow_name="Nahlah Commerce Agent V2 Shadow",
                    trace_id=trace_id,
                    group_id=f"tenant:{context.tenant_id}:conversation:{context.conversation_id}",
                    trace_metadata={
                        "tenant_id": str(context.tenant_id),
                        "conversation_id": str(context.conversation_id),
                        "inbound_trace_hash": trace_id.removeprefix("trace_"),
                        "mode": "shadow_read_only",
                    },
                    trace_include_sensitive_data=False,
                ),
            ),
            timeout=float(timeout_seconds or COMMERCE_AGENT_V2_TIMEOUT_SECONDS),
        )
        output = run.final_output_as(CommerceReply, raise_if_incorrect_type=True)
        usage = run.context_wrapper.usage
        guardrails = [
            {
                "name": item.guardrail.get_name(),
                "tripwire_triggered": item.output.tripwire_triggered,
                "output_info": item.output.output_info,
            }
            for item in [*run.input_guardrail_results, *run.output_guardrail_results]
        ]
        return CommerceAgentRunResult(
            status="completed",
            reply=output,
            model=str(observable_model_name),
            session_id=session.session_id,
            sdk_trace_id=trace_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            total_tokens=int(usage.total_tokens or 0),
            tool_trace=list(hooks.events),
            guardrail_results=guardrails,
        )
    except asyncio.TimeoutError:
        reason = "provider_or_tool_timeout"
        guardrails: list[dict[str, Any]] = []
    except (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered) as exc:
        item = exc.guardrail_result
        guardrails = [
            {
                "name": item.guardrail.get_name(),
                "tripwire_triggered": item.output.tripwire_triggered,
                "output_info": item.output.output_info,
            }
        ]
        reason = type(exc).__name__
    except Exception as exc:  # noqa: BLE001 — shadow must never affect V1
        guardrails = []
        reason = f"{type(exc).__name__}"
    return CommerceAgentRunResult(
        status="failed",
        reply=_fallback(reason),
        model=str(observable_model_name),
        session_id=session.session_id,
        sdk_trace_id=trace_id,
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=hooks.usage["input_tokens"],
        output_tokens=hooks.usage["output_tokens"],
        total_tokens=hooks.usage["total_tokens"],
        tool_trace=list(hooks.events),
        guardrail_results=guardrails,
        failure_reason=reason,
    )
