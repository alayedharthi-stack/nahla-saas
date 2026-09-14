"""Privacy-safe bridge between Agents SDK tracing and Nahla observability."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from typing import Any

from agents import RunContextWrapper, RunHooks, custom_span, trace

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import CommerceReply


def sdk_trace_id(inbound_trace_id: str) -> str:
    digest = hashlib.sha256(str(inbound_trace_id).encode("utf-8")).hexdigest()[:32]
    return f"trace_{digest}"


@contextmanager
def whatsapp_turn_trace(*, tenant_id: int, conversation_id: int, inbound_wamid: str):
    """Add only Nahlah channel correlation around SDK-owned runtime spans."""
    trace_id = sdk_trace_id(inbound_wamid)
    with trace(
        "Nahlah Commerce Agent V2 WhatsApp Turn",
        trace_id=trace_id,
        group_id=f"tenant:{tenant_id}:conversation:{conversation_id}",
        metadata={
            "tenant_id": str(tenant_id),
            "conversation_id": str(conversation_id),
            "inbound_trace_hash": trace_id.removeprefix("trace_"),
            "channel": "whatsapp",
        },
    ):
        with custom_span(
            "whatsapp.turn",
            data={
                "tenant_id": int(tenant_id),
                "conversation_id": int(conversation_id),
                "trace_id": trace_id,
            },
        ):
            yield trace_id


def commerce_delivery_span(*, tenant_id: int, conversation_id: int, action_kind: str):
    return custom_span(
        "commerce.delivery",
        data={
            "tenant_id": int(tenant_id),
            "conversation_id": int(conversation_id),
            "action_kind": str(action_kind)[:32],
        },
    )


def _safe_arguments(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:  # noqa: BLE001
        return {"malformed": True}
    safe: dict[str, Any] = {}
    for key, value in parsed.items():
        if key == "query":
            text = str(value or "")
            safe["query_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            safe["query_length"] = len(text)
        elif key in {"product_id", "limit"}:
            safe[key] = value
        elif key in {"order_id", "order_number"}:
            text = str(value or "")
            safe[f"{key}_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            safe[f"{key}_length"] = len(text)
        elif key == "purpose":
            safe[key] = str(value or "")
        else:
            safe[key] = "[redacted]"
    return safe


def _safe_tool_result(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        data = result.model_dump(mode="json")
    elif isinstance(result, str):
        try:
            parsed = json.loads(result)
            data = parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        data = {}

    def safe_evidence(item: dict[str, Any]) -> dict[str, Any]:
        source = str(item.get("source") or "")
        if source.startswith("order_"):
            source_id = str(item.get("source_id") or "")
            return {
                "ref_sha256": hashlib.sha256(
                    str(item.get("ref") or "").encode("utf-8")
                ).hexdigest(),
                "source": source,
                "source_id_sha256": hashlib.sha256(source_id.encode("utf-8")).hexdigest(),
                "provenance": item.get("provenance", {}),
            }
        return {
            "ref": item.get("ref"),
            "source": source,
            "source_id": item.get("source_id"),
            "provenance": item.get("provenance", {}),
        }

    order = data.get("order") if isinstance(data.get("order"), dict) else None
    shipment = data.get("shipment") if isinstance(data.get("shipment"), dict) else None
    return {
        "status": data.get("status", "unknown"),
        "failure_reason": data.get("failure_reason"),
        "product_ids": [
            item.get("product_id")
            for item in data.get("products", [])
            if isinstance(item, dict) and item.get("product_id")
        ]
        + ([data["product"]["product_id"]] if isinstance(data.get("product"), dict) else []),
        "authorized_order_present": bool(order or shipment),
        "evidence": [
            safe_evidence(item)
            for item in data.get("evidence", [])
            if isinstance(item, dict)
        ],
        "result_count": len(data.get("products", []) or data.get("sections", []) or [])
        or int(bool(order or shipment)),
    }


class CommerceTracingHooks(RunHooks[CommerceAgentContext]):
    """Persist only safe summaries alongside the SDK's authoritative spans."""

    def __init__(self, *, model: str, requested_service_tier: str = "auto") -> None:
        self.model = model
        self.requested_service_tier = requested_service_tier
        self.events: list[dict[str, Any]] = []
        self._started: dict[str, float] = {}
        self.usage: dict[str, int] = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        self._usage_requests = 0
        self._model_turn = 0
        self._model_attempt = 0

    @property
    def model_attempt(self) -> int:
        return max(1, self._model_attempt)

    @property
    def attempted_retry(self) -> bool:
        return any(
            event.get("kind") == "model_retry_decision" and event.get("retry") is True
            for event in self.events
        )

    @property
    def last_retry_reason(self) -> str:
        return next(
            (
                str(event.get("reason") or "")
                for event in reversed(self.events)
                if event.get("kind") == "model_retry_decision"
            ),
            "",
        )

    @property
    def last_retry_was_model_timeout(self) -> bool:
        return next(
            (
                bool(event.get("model_timeout"))
                for event in reversed(self.events)
                if event.get("kind") == "model_retry_decision"
            ),
            False,
        )

    def record_retry_decision(self, event: dict[str, Any]) -> None:
        recorded = dict(event)
        now = time.monotonic()
        started = self._started.get("llm", now)
        recorded["model_turn"] = self._model_turn
        recorded["model_attempt"] = self._model_attempt
        recorded["latency_ms"] = max(0, int((now - started) * 1000))
        self.events.append(recorded)
        if recorded.get("retry") is True:
            self._model_attempt += 1
            retry_delay = float(recorded.get("delay_ms") or 0) / 1000.0
            self._started["llm"] = now + retry_delay

    def record_runtime_failure(self, reason: str) -> None:
        self.events.append({"kind": "runtime_failure", "reason": str(reason)[:120]})

    async def on_llm_start(self, _context, _agent, _system_prompt, _input_items) -> None:
        self._model_turn += 1
        self._model_attempt = 1
        self._started["llm"] = time.monotonic()
        self.events.append(
            {
                "kind": "model_start",
                "model": self.model,
                "model_turn": self._model_turn,
                "model_attempt": self._model_attempt,
                "requested_service_tier": self.requested_service_tier,
            }
        )

    async def on_llm_end(self, context, _agent, response) -> None:
        started = self._started.pop("llm", time.monotonic())
        input_details = getattr(context.usage, "input_tokens_details", None)
        cumulative_usage = {
            "input_tokens": int(context.usage.input_tokens or 0),
            "cached_input_tokens": int(
                getattr(input_details, "cached_tokens", 0) or 0
            ),
            "output_tokens": int(context.usage.output_tokens or 0),
            "total_tokens": int(context.usage.total_tokens or 0),
        }
        cumulative_requests = int(context.usage.requests or 0)
        call_usage = {
            key: max(0, value - self.usage[key])
            for key, value in cumulative_usage.items()
        }
        call_requests = max(0, cumulative_requests - self._usage_requests)
        self.usage = cumulative_usage
        self._usage_requests = cumulative_requests
        self.events.append(
            {
                "kind": "model_end",
                "model": self.model,
                "model_turn": self._model_turn,
                "requested_service_tier": self.requested_service_tier,
                "provider_response_received": True,
                "response_id_present": bool(getattr(response, "response_id", None)),
                "request_id_present": bool(getattr(response, "request_id", None)),
                "model_attempt": self._model_attempt,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                "usage": {
                    "requests": call_requests,
                    **call_usage,
                },
            }
        )

    async def on_tool_start(self, context, _agent, tool) -> None:
        name = str(getattr(tool, "name", "unknown"))
        self._started[f"tool:{name}"] = time.monotonic()
        self.events.append(
            {
                "kind": "tool_start",
                "tool": name,
                "arguments": _safe_arguments(getattr(context, "tool_arguments", {})),
            }
        )

    async def on_tool_end(self, _context, _agent, tool, result) -> None:
        name = str(getattr(tool, "name", "unknown"))
        started = self._started.pop(f"tool:{name}", time.monotonic())
        safe_result = _safe_tool_result(result)
        latency_ms = int((time.monotonic() - started) * 1000)
        failure_reason = str(safe_result.get("failure_reason") or "")
        if failure_reason.startswith("tool_timeout:"):
            self.events.append(
                {
                    "kind": "tool_timeout",
                    "tool": name,
                    "latency_ms": latency_ms,
                    "failure_reason": failure_reason,
                }
            )
        elif failure_reason.startswith("tool_error:"):
            self.events.append(
                {
                    "kind": "tool_error",
                    "tool": name,
                    "latency_ms": latency_ms,
                    "failure_reason": failure_reason,
                }
            )
        self.events.append(
            {
                "kind": "tool_end",
                "tool": name,
                "latency_ms": latency_ms,
                "result": safe_result,
            }
        )

    async def on_agent_end(self, _context, _agent, output) -> None:
        self.events.append(
            {
                "kind": "agent_end",
                "structured_output": isinstance(output, CommerceReply),
                "evidence_refs": list(getattr(output, "evidence_refs", []) or []),
                "safe_fallback_reason": getattr(output, "safe_fallback_reason", None),
            }
        )
