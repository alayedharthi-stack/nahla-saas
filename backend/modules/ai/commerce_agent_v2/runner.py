"""Official Agents SDK runner boundary for Commerce Agent V2."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from agents import (
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelTimeoutError,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
    ToolTimeoutError,
)

from core.config import (
    COMMERCE_AGENT_V2_MAX_MODEL_RETRIES,
    COMMERCE_AGENT_V2_MODEL,
    COMMERCE_AGENT_V2_MODEL_TIMEOUT_SECONDS,
    COMMERCE_AGENT_V2_REASONING_EFFORT,
    COMMERCE_AGENT_V2_RUN_DEADLINE_SECONDS,
    COMMERCE_AGENT_V2_SERVICE_TIER,
)
from modules.ai.commerce_agent_v2.agent import build_commerce_agent
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import CommerceReply
from modules.ai.commerce_agent_v2.runtime import (
    ExecutionMode,
    build_model_retry_settings,
    classify_model_exception,
    safe_reason_code,
)
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
    cached_input_tokens: int = 0
    requested_service_tier: str = "auto"
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    guardrail_results: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str = ""


def safe_fallback_reply(reason: str) -> CommerceReply:
    return CommerceReply(
        text="لا تتوفر لدي معلومة موثوقة كافية للإجابة الآن.",
        safe_fallback_reason=reason[:240],
    )


def _guardrail_record(item: Any) -> dict[str, Any]:
    return {
        "name": item.guardrail.get_name(),
        "tripwire_triggered": bool(item.output.tripwire_triggered),
        "output_info": item.output.output_info,
    }


def _guardrail_code(item: Any) -> str:
    info = item.output.output_info
    if isinstance(info, dict):
        errors = info.get("errors")
        if isinstance(errors, list) and errors:
            return safe_reason_code(errors[0], default="blocked")
        reason = info.get("reason")
        if reason:
            return safe_reason_code(reason, default="blocked")
    return "blocked"


def _is_retryable_evidence_free_output(item: Any) -> bool:
    """Retry only no-tool outputs that assert an unverified factual value."""
    info = item.output.output_info
    if not isinstance(info, dict):
        return False
    errors = info.get("errors")
    if not isinstance(errors, list):
        return False
    factual_errors = {
        "evidence_free_commercial_or_factual_claim",
        "availability_in_text_without_verified_claim",
        "price_in_text_without_verified_claim",
        "stock_quantity_in_text_without_verified_claim",
        "url_in_text_without_verified_claim",
    }
    return bool(factual_errors.intersection(str(error) for error in errors))


def _grounding_retry_input(user_input: str) -> str:
    """Attach trusted, run-local remediation after a no-tool factual rejection."""
    return (
        f"{str(user_input or '').strip()}\n\n"
        "تعليمة تصحيح داخلية: المحاولة السابقة ذكرت حقيقة بلا دليل. "
        "استخرج اسم المنتج أو ترتيبه من سياق المحادثة المعزول، ثم استخدم "
        "أداة القراءة المناسبة بهذا الاسم في التشغيل الحالي. لا تستخدم بحثًا "
        "عامًا فارغًا إلا إذا طلب العميل تصفحًا عامًا. إذا ظل المرجع ملتبسًا "
        "بعد الأداة فاطلب توضيحًا ولا تذكر أي حقيقة تجارية."
    )


def _cached_tokens(usage: Any) -> int:
    details = getattr(usage, "input_tokens_details", None)
    return int(getattr(details, "cached_tokens", 0) or 0)


async def run_commerce_agent(
    *,
    context: CommerceAgentContext,
    user_input: str,
    model: str | Any | None = None,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
    model_timeout_seconds: float | None = None,
    run_deadline_seconds: float | None = None,
    timeout_seconds: float | None = None,
    execution_mode: ExecutionMode = "shadow",
    service_tier: str | None = None,
    retry_model_timeouts: bool = False,
) -> CommerceAgentRunResult:
    """Run one read-only turn with the official SDK and structured output."""
    configured_model = model if model is not None else COMMERCE_AGENT_V2_MODEL
    observable_model_name = model_name or (
        configured_model if isinstance(configured_model, str) else type(configured_model).__name__
    )
    session = ConversationMessageSession(context)
    requested_service_tier = str(service_tier or COMMERCE_AGENT_V2_SERVICE_TIER).lower()
    if requested_service_tier not in {"auto", "fast"}:
        requested_service_tier = "auto"
    hooks = CommerceTracingHooks(
        model=str(observable_model_name),
        requested_service_tier=requested_service_tier,
    )
    started = time.monotonic()
    trace_id = sdk_trace_id(context.inbound_trace_id)
    context.bind_run_user_input(user_input)
    try:
        # ``timeout_seconds`` remains as a compatibility alias for older evals;
        # it controls only the run-wide deadline, never a provider attempt.
        deadline = float(
            run_deadline_seconds
            or timeout_seconds
            or COMMERCE_AGENT_V2_RUN_DEADLINE_SECONDS
        )
        async with asyncio.timeout(deadline):
            grounding_retry_used = False
            while True:
                agent = build_commerce_agent(
                    model=configured_model,
                    reasoning_effort=(
                        reasoning_effort or COMMERCE_AGENT_V2_REASONING_EFFORT
                    ),
                    model_timeout_seconds=float(
                        model_timeout_seconds or COMMERCE_AGENT_V2_MODEL_TIMEOUT_SECONDS
                    ),
                    retry_settings=build_model_retry_settings(
                        execution_mode=execution_mode,
                        max_retries=COMMERCE_AGENT_V2_MAX_MODEL_RETRIES,
                        observer=hooks.record_retry_decision,
                        retry_model_timeouts=retry_model_timeouts,
                    ),
                    service_tier=requested_service_tier,
                    require_tool_call=grounding_retry_used,
                )
                attempt_event_offset = len(hooks.events)
                try:
                    run = await Runner.run(
                        agent,
                        (
                            _grounding_retry_input(user_input)
                            if grounding_retry_used
                            else str(user_input or "")
                        ),
                        context=context,
                        session=session,
                        hooks=hooks,
                        max_turns=6,
                        run_config=RunConfig(
                            workflow_name=(
                                "Nahlah Commerce Agent V2 Outbound"
                                if execution_mode == "outbound"
                                else "Nahlah Commerce Agent V2 Shadow"
                            ),
                            trace_id=trace_id,
                            group_id=(
                                f"tenant:{context.tenant_id}:conversation:{context.conversation_id}"
                            ),
                            trace_metadata={
                                "tenant_id": str(context.tenant_id),
                                "conversation_id": str(context.conversation_id),
                                "inbound_trace_hash": trace_id.removeprefix("trace_"),
                                "mode": f"{execution_mode}_read_only",
                                "requested_service_tier": requested_service_tier,
                            },
                            trace_include_sensitive_data=False,
                        ),
                    )
                    break
                except OutputGuardrailTripwireTriggered as exc:
                    attempt_events = hooks.events[attempt_event_offset:]
                    tool_started = any(
                        event.get("kind") == "tool_start" for event in attempt_events
                    )
                    if (
                        not grounding_retry_used
                        and not tool_started
                        and _is_retryable_evidence_free_output(exc.guardrail_result)
                    ):
                        grounding_retry_used = True
                        session = ConversationMessageSession(context)
                        hooks.events.append(
                            {
                                "kind": "grounding_retry",
                                "reason": "evidence_free_commercial_or_factual_claim",
                                "tool_choice": "required",
                            }
                        )
                        continue
                    raise
        output = run.final_output_as(CommerceReply, raise_if_incorrect_type=True)
        usage = run.context_wrapper.usage
        guardrails = [
            _guardrail_record(item)
            for item in [*run.input_guardrail_results, *run.output_guardrail_results]
        ]
        cached_input_tokens = _cached_tokens(usage)
        hooks.events.append(
            {
                "kind": "usage_summary",
                "input_tokens": int(usage.input_tokens or 0),
                "cached_input_tokens": cached_input_tokens,
                "cache_percentage": round(
                    (cached_input_tokens / int(usage.input_tokens or 0)) * 100,
                    2,
                )
                if int(usage.input_tokens or 0) > 0
                else 0.0,
                "requested_service_tier": requested_service_tier,
            }
        )
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
            cached_input_tokens=cached_input_tokens,
            requested_service_tier=requested_service_tier,
            tool_trace=list(hooks.events),
            guardrail_results=guardrails,
        )
    except ModelTimeoutError:
        reason = f"model_timeout:attempt_{hooks.model_attempt}"
        hooks.record_runtime_failure(reason)
        guardrails: list[dict[str, Any]] = []
    except MaxTurnsExceeded:
        reason = "max_turns_exceeded"
        hooks.record_runtime_failure(reason)
        guardrails = []
    except ToolTimeoutError as exc:
        reason = f"tool_timeout:{safe_reason_code(exc.tool_name, default='unknown')}"
        hooks.record_runtime_failure(reason)
        guardrails: list[dict[str, Any]] = []
    except InputGuardrailTripwireTriggered as exc:
        item = exc.guardrail_result
        guardrails = [_guardrail_record(item)]
        reason = "input_guardrail_tripwire"
        hooks.record_runtime_failure(reason)
    except OutputGuardrailTripwireTriggered as exc:
        item = exc.guardrail_result
        guardrails = [_guardrail_record(item)]
        reason = f"output_guardrail_tripwire:{_guardrail_code(item)}"
        hooks.record_runtime_failure(reason)
    except ModelBehaviorError:
        guardrails = []
        reason = "structured_output_invalid:model_behavior"
        hooks.record_runtime_failure(reason)
    except TimeoutError:
        reason = "run_deadline_exceeded"
        hooks.record_runtime_failure(reason)
        guardrails = []
    except Exception as exc:  # noqa: BLE001 — shadow must never affect V1
        guardrails = []
        if hooks.last_retry_was_model_timeout:
            reason = f"model_timeout:attempt_{hooks.model_attempt}"
        else:
            reason = classify_model_exception(
                exc,
                attempted_retry=hooks.attempted_retry,
                retry_reason=hooks.last_retry_reason,
            )
        hooks.record_runtime_failure(reason)
    hooks.events.append(
        {
            "kind": "usage_summary",
            **hooks.usage,
            "cache_percentage": round(
                (hooks.usage["cached_input_tokens"] / hooks.usage["input_tokens"]) * 100,
                2,
            )
            if hooks.usage["input_tokens"] > 0
            else 0.0,
            "requested_service_tier": requested_service_tier,
        }
    )
    return CommerceAgentRunResult(
        status="failed",
        reply=safe_fallback_reply(reason),
        model=str(observable_model_name),
        session_id=session.session_id,
        sdk_trace_id=trace_id,
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=hooks.usage["input_tokens"],
        output_tokens=hooks.usage["output_tokens"],
        total_tokens=hooks.usage["total_tokens"],
        cached_input_tokens=hooks.usage["cached_input_tokens"],
        requested_service_tier=requested_service_tier,
        tool_trace=list(hooks.events),
        guardrail_results=guardrails,
        failure_reason=reason,
    )
