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
from modules.ai.commerce_agent_v2.knowledge_retrieval import detect_catalog_conflicts
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
    # Set when a DELIVERED reply named a sub-detail the merchant has not
    # documented. Never set for a complete safe fallback — see
    # ``split_knowledge_gap_disclosure``.
    knowledge_gap_disclosure: str = ""
    # Every tenant knowledge lookup this turn attempted, including the ones that
    # returned nothing, timed out or failed. An empty list means no lookup ran.
    knowledge_lookups: list[dict[str, Any]] = field(default_factory=list)
    # Merchant knowledge that contradicts live structured catalog facts.
    # Recorded for the operator; structured Salla evidence always wins.
    knowledge_conflicts: list[dict[str, Any]] = field(default_factory=list)


def safe_fallback_reply(reason: str) -> CommerceReply:
    return CommerceReply(
        text="لا تتوفر لدي معلومة موثوقة كافية للإجابة الآن.",
        safe_fallback_reason=reason[:240],
    )


def split_knowledge_gap_disclosure(reply: CommerceReply) -> tuple[CommerceReply, str]:
    """Separate a partial knowledge-gap disclosure from a complete safe fallback.

    ``safe_fallback_reason`` leaves the model carrying two different meanings.
    ``validate_grounded_reply`` accepts it as an acknowledgement that a reply
    could not cover every piece of evidence it referenced, so the model sets it
    on an otherwise grounded answer whenever the merchant documents nothing for
    part of the question. It is also the field ``safe_fallback_reply`` uses when
    a run fails and the customer receives a complete substitute instead of an
    answer.

    Every consumer read the field as the second meaning only, so a grounded
    reply that merely named an absent sub-detail was scored a fallback — this is
    what failed Phase 2.7A turn B4. A delivered reply that still carries
    verified fact claims is not a substitute: its disclosure moves to a value of
    its own and ``safe_fallback_reason`` keeps only its fallback meaning.

    Applied to delivered replies only. A rejected reply never reaches here: the
    runner replaces it with ``safe_fallback_reply`` on the failure path.
    """
    disclosure = str(reply.safe_fallback_reason or "")
    if not disclosure or not reply.fact_claims:
        return reply, ""
    return reply.model_copy(update={"safe_fallback_reason": None}), disclosure


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


# Rejections that earn the single re-grounding retry.  Factual codes: the
# reply asserted a commercial value without a verified claim.  Knowledge-span
# codes: the reply cited the right section (ref, value and span presence all
# verified) but paraphrased it beyond ``_knowledge_span_supported``; Run 2's
# K16 ended there with no retry at all.  Nothing else is retried, and the
# retry itself stays fail-closed.
_FACTUAL_RETRY_ERRORS = frozenset(
    {
        "evidence_free_commercial_or_factual_claim",
        "availability_in_text_without_verified_claim",
        "order_evidence_without_verified_claim",
        "price_in_text_without_verified_claim",
        "stock_quantity_in_text_without_verified_claim",
        "url_in_text_without_verified_claim",
    }
)
_KNOWLEDGE_SPAN_RETRY_ERRORS = frozenset(
    {
        "claim_span_not_equivalent:product_knowledge",
        "claim_span_not_equivalent:merchant_knowledge",
    }
)
GROUNDING_RETRY_FACTUAL = "evidence_free_commercial_or_factual_claim"
GROUNDING_RETRY_KNOWLEDGE_SPAN = "knowledge_span_not_equivalent"


def _guardrail_error_codes(item: Any) -> list[str]:
    info = item.output.output_info
    if not isinstance(info, dict):
        return []
    errors = info.get("errors")
    if not isinstance(errors, list):
        return []
    return [str(error) for error in errors]


def _is_retryable_evidence_free_output(item: Any) -> bool:
    """Retry outputs that assert a factual value without a verified claim."""
    return bool(_FACTUAL_RETRY_ERRORS.intersection(_guardrail_error_codes(item)))


def _grounding_retry_reason(item: Any) -> str:
    """Which re-grounding retry, if any, a rejected output earns; "" for none."""
    errors = set(_guardrail_error_codes(item))
    if errors & _FACTUAL_RETRY_ERRORS:
        return GROUNDING_RETRY_FACTUAL
    if errors & _KNOWLEDGE_SPAN_RETRY_ERRORS:
        return GROUNDING_RETRY_KNOWLEDGE_SPAN
    return ""


def _grounding_retry_input(user_input: str) -> str:
    """Attach trusted, run-local remediation after a no-tool factual rejection."""
    return (
        f"{str(user_input or '').strip()}\n\n"
        "تعليمة تصحيح داخلية: المحاولة السابقة ذكرت حقيقة بلا دليل. "
        "استخرج اسم المنتج أو ترتيبه من سياق المحادثة المعزول، ثم استخدم "
        "أداة القراءة المناسبة بهذا الاسم في التشغيل الحالي. لا تستخدم بحثًا "
        "عامًا فارغًا إلا إذا طلب العميل تصفحًا عامًا. إذا ظل المرجع ملتبسًا "
        "بعد الأداة فاطلب توضيحًا ولا تذكر أي حقيقة تجارية. "
        "إذا كان السؤال عن طلب أو شحنة، أعد resolve_customer_order ثم أداة الطلب "
        "أو الشحنة المناسبة، واربط كل evidence_ref مذكور بـ FactClaim موثق لنفس "
        "subject_order_id. لا تذكر evidence_ref استُخدم للتفويض فقط ولا يدعم حقيقة "
        "أو إجراءً ظاهرًا في الرد."
    )


def _knowledge_regrounding_input(user_input: str) -> str:
    """Attach trusted, run-local remediation after a paraphrased knowledge span.

    PROMPT_CHANGED=YES — limited to this grounding-retry instruction.  The
    retry must quote or faithfully reproduce the supported merchant knowledge,
    preserve its meaning and scope, bind the claim to the registered evidence
    ref, and otherwise drop the claim and disclose that the detail is
    unavailable.  The system instructions, persona, model and routing are
    untouched; the span check itself is not relaxed.
    """
    return (
        f"{str(user_input or '').strip()}\n\n"
        "تعليمة تصحيح داخلية: المحاولة السابقة نقلت معلومة من معرفة التاجر بصياغة "
        "لا تطابق النص المعتمد. أعد استخدام أداة القراءة المناسبة في التشغيل الحالي، "
        "ثم انقل معلومة التاجر المدعومة بالاقتباس الحرفي أو بإعادة إنتاج أمينة تحفظ "
        "معناها ونطاقها دون إضافة أو تعميم أو حذف قيد، واربط الادعاء بـ evidence_ref "
        "المسجل لنفس القسم مع text_span مطابق لما ورد في الرد. إذا تعذّر نقلها بأمانة "
        "فاحذف هذا الادعاء واذكر صراحةً أن هذه التفصيلة غير متوفرة لديك، ولا تذكر أي "
        "حقيقة تجارية بلا دليل."
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
            retry_reason = ""
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
                if not grounding_retry_used:
                    run_input = str(user_input or "")
                elif retry_reason == GROUNDING_RETRY_KNOWLEDGE_SPAN:
                    run_input = _knowledge_regrounding_input(user_input)
                else:
                    run_input = _grounding_retry_input(user_input)
                try:
                    run = await Runner.run(
                        agent,
                        run_input,
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
                    reason = _grounding_retry_reason(exc.guardrail_result)
                    if not grounding_retry_used and reason:
                        grounding_retry_used = True
                        retry_reason = reason
                        context.activate_grounding_retry()
                        session = ConversationMessageSession(context)
                        hooks.events.append(
                            {
                                "kind": "grounding_retry",
                                "reason": reason,
                                "tool_choice": "required",
                                # The first pass's rejection, kept on the
                                # trace so a retried turn still shows what it
                                # recovered from — never merged into
                                # ``guardrail_results``, which describe the
                                # delivered reply only.
                                "guardrail": exc.guardrail_result.guardrail.get_name(),
                                "errors": _guardrail_error_codes(exc.guardrail_result),
                            }
                        )
                        continue
                    raise
        output = run.final_output_as(CommerceReply, raise_if_incorrect_type=True)
        output, knowledge_gap_disclosure = split_knowledge_gap_disclosure(output)
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
            knowledge_gap_disclosure=knowledge_gap_disclosure,
            knowledge_lookups=context.knowledge_lookups,
            knowledge_conflicts=detect_catalog_conflicts(context),
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
        knowledge_lookups=context.knowledge_lookups,
        knowledge_conflicts=detect_catalog_conflicts(context),
    )
