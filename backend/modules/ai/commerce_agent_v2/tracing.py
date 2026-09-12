"""Privacy-safe bridge between Agents SDK tracing and Nahla observability."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from agents import RunContextWrapper, RunHooks

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import CommerceReply


def sdk_trace_id(inbound_trace_id: str) -> str:
    digest = hashlib.sha256(str(inbound_trace_id).encode("utf-8")).hexdigest()[:32]
    return f"trace_{digest}"


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
    return {
        "status": data.get("status", "unknown"),
        "failure_reason": data.get("failure_reason"),
        "product_ids": [
            item.get("product_id")
            for item in data.get("products", [])
            if isinstance(item, dict) and item.get("product_id")
        ]
        + ([data["product"]["product_id"]] if isinstance(data.get("product"), dict) else []),
        "evidence": [
            {
                "ref": item.get("ref"),
                "source": item.get("source"),
                "source_id": item.get("source_id"),
                "provenance": item.get("provenance", {}),
            }
            for item in data.get("evidence", [])
            if isinstance(item, dict)
        ],
        "result_count": len(data.get("products", []) or data.get("sections", []) or []),
    }


class CommerceTracingHooks(RunHooks[CommerceAgentContext]):
    def __init__(self, *, model: str) -> None:
        self.model = model
        self.events: list[dict[str, Any]] = []
        self._started: dict[str, float] = {}
        self.usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        self._usage_requests = 0

    async def on_llm_start(self, _context, _agent, _system_prompt, _input_items) -> None:
        self._started["llm"] = time.monotonic()
        self.events.append({"kind": "model_start", "model": self.model})

    async def on_llm_end(self, context, _agent, response) -> None:
        started = self._started.pop("llm", time.monotonic())
        cumulative_usage = {
            "input_tokens": int(context.usage.input_tokens or 0),
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
                "provider_response_received": True,
                "response_id_present": bool(getattr(response, "response_id", None)),
                "request_id_present": bool(getattr(response, "request_id", None)),
                "latency_ms": int((time.monotonic() - started) * 1000),
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
        self.events.append(
            {
                "kind": "tool_end",
                "tool": name,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "result": _safe_tool_result(result),
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
