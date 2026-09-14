"""Read-only, exact-turn evidence collector for the real WhatsApp harness."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from modules.ai.commerce_agent_v2.tracing import sdk_trace_id


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _metadata(row: Any) -> dict[str, Any]:
    return dict(getattr(row, "extra_metadata", None) or {})


def _wamid(metadata: dict[str, Any]) -> str:
    provider = metadata.get("provider_send")
    provider = provider if isinstance(provider, dict) else {}
    return str(
        metadata.get("outbound_provider_wamid")
        or metadata.get("wa_message_id")
        or provider.get("wamid")
        or ""
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def observe_persisted_turn(
    db: Any,
    *,
    account_alias: str,
    case_id: str,
    inbound_wamid: str,
    tenant_id: int = 1,
) -> dict[str, Any]:
    """Read only the exact controlled inbound, trace row, and response window."""
    from models import AIUsageEvent, CommerceAgentV2ShadowRun, MessageEvent

    if tenant_id != 1:
        raise ValueError("real_whatsapp_harness_tenant_must_be_1")
    trace_id = sdk_trace_id(inbound_wamid)
    inbound = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == tenant_id,
            MessageEvent.direction.in_(("in", "inbound")),
            MessageEvent.extra_metadata["wa_message_id"].as_string() == inbound_wamid,
        )
        .one_or_none()
    )
    if inbound is None:
        raise LookupError("exact_inbound_wamid_not_found")
    run = (
        db.query(CommerceAgentV2ShadowRun)
        .filter(
            CommerceAgentV2ShadowRun.tenant_id == tenant_id,
            CommerceAgentV2ShadowRun.conversation_id == inbound.conversation_id,
            CommerceAgentV2ShadowRun.sdk_trace_id == trace_id,
        )
        .one_or_none()
    )
    if run is None:
        raise LookupError("exact_commerce_v2_run_not_found")

    start = _aware(inbound.created_at)
    next_inbound = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == tenant_id,
            MessageEvent.conversation_id == inbound.conversation_id,
            MessageEvent.direction.in_(("in", "inbound")),
            MessageEvent.created_at > inbound.created_at,
        )
        .order_by(MessageEvent.created_at.asc(), MessageEvent.id.asc())
        .first()
    )
    # Attribute responses only until the next controlled inbound. The bounded
    # fallback window prevents a missing next turn from collecting later,
    # unrelated conversation history.
    end = (
        _aware(next_inbound.created_at)
        if next_inbound is not None
        else start + timedelta(seconds=240)
    )
    outbound_rows = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == tenant_id,
            MessageEvent.conversation_id == inbound.conversation_id,
            MessageEvent.direction.in_(("out", "outbound")),
            MessageEvent.created_at >= start,
            MessageEvent.created_at < end,
        )
        .order_by(MessageEvent.created_at.asc(), MessageEvent.id.asc())
        .all()
    )
    tool_trace = list(run.tool_trace or [])
    model_end_events = [event for event in tool_trace if event.get("kind") == "model_end"]
    retry_events = [
        event for event in tool_trace if event.get("kind") == "model_retry_decision"
    ]
    provider_attempt_events = [
        event
        for event in tool_trace
        if event.get("kind") in {"model_retry_decision", "model_end"}
    ]
    tool_calls = [
        str(event.get("tool"))
        for event in tool_trace
        if event.get("kind") == "tool_start"
    ]
    tool_end_index = max(
        [index for index, event in enumerate(tool_trace) if event.get("kind") == "tool_end"],
        default=-1,
    )
    post_tool_event = next(
        (
            event
            for index, event in enumerate(tool_trace)
            if index > tool_end_index and event.get("kind") == "model_end"
        ),
        None,
    )
    post_tool_turn = int((post_tool_event or {}).get("model_turn") or 0)
    usage_summary = next(
        (event for event in reversed(tool_trace) if event.get("kind") == "usage_summary"),
        {},
    )
    requested_service_tier = str(
        usage_summary.get("requested_service_tier") or "auto"
    )
    usage_row = (
        db.query(AIUsageEvent)
        .filter(
            AIUsageEvent.tenant_id == tenant_id,
            AIUsageEvent.conversation_id == inbound.conversation_id,
            AIUsageEvent.request_id == trace_id,
        )
        .one_or_none()
    )
    base_cost_usd = float(getattr(usage_row, "total_cost_usd", 0) or 0)
    outbound_metadata = [_metadata(row) for row in outbound_rows]
    owners = [str(metadata.get("reply_owner") or "") for metadata in outbound_metadata]
    correlated = [
        row
        for row, metadata in zip(outbound_rows, outbound_metadata)
        if metadata.get("sdk_trace_id") == trace_id
    ]
    primary = correlated[0] if correlated else (outbound_rows[0] if outbound_rows else None)
    primary_metadata = _metadata(primary) if primary is not None else {}
    provider_duration = primary_metadata.get("outbound_provider_duration_ms")
    e2e_latency = (
        int((_aware(primary.created_at) - start).total_seconds() * 1000)
        if primary is not None
        else None
    )
    fallback_reason = str(run.failure_reason or "") or str(
        (run.structured_output or {}).get("safe_fallback_reason") or ""
    )
    delivery_failure = str(primary_metadata.get("delivery_failure_reason") or "")
    turn_failure_reason = delivery_failure or str(run.failure_reason or "")
    runtime_fallback = bool(
        fallback_reason
        and fallback_reason.startswith(
            (
                "model_",
                "run_",
                "tool_error:",
                "tool_timeout:",
                "structured_output_invalid:",
                "unexpected:",
            )
        )
    )
    guardrails = list(run.guardrail_results or [])
    return {
        "case_id": case_id,
        "account_alias": account_alias,
        "tenant_id": tenant_id,
        "conversation_id": int(inbound.conversation_id),
        "inbound_text": str(inbound.body or ""),
        "outbound_text": str(getattr(primary, "body", "") or ""),
        "inbound_wamid": inbound_wamid,
        "outbound_wamids": [value for value in (_wamid(item) for item in outbound_metadata) if value],
        "trace_id": trace_id,
        "owner": str(primary_metadata.get("reply_owner") or ""),
        "model_attempts": len(
            [event for event in tool_trace if event.get("kind") == "model_start"]
        )
        + sum(event.get("retry") is True for event in retry_events),
        "retry_decisions": retry_events,
        "provider_latencies_ms": [
            int(event.get("latency_ms") or 0) for event in provider_attempt_events
        ],
        "first_model_latency_ms": (
            sum(
                int(event.get("latency_ms") or 0)
                for event in provider_attempt_events
                if int(event.get("model_turn") or 0) == 1
            )
            if model_end_events
            else None
        ),
        "post_tool_model_latency_ms": (
            sum(
                int(event.get("latency_ms") or 0)
                for event in provider_attempt_events
                if int(event.get("model_turn") or 0) == post_tool_turn
            )
            if post_tool_event
            else None
        ),
        "tool_calls": tool_calls,
        "tool_latencies_ms": [
            {"tool": event.get("tool"), "latency_ms": int(event.get("latency_ms") or 0)}
            for event in tool_trace
            if event.get("kind") == "tool_end"
        ],
        "guardrail_result": guardrails,
        "guardrail_passed": all(not item.get("tripwire_triggered") for item in guardrails),
        "status": "failed" if delivery_failure else str(run.status),
        "runner_status": str(run.status),
        "failure_reason": turn_failure_reason,
        "total_runner_latency_ms": int(run.latency_ms or 0),
        "whatsapp_provider_latency_ms": int(provider_duration or 0),
        "whatsapp_e2e_latency_ms": e2e_latency,
        "fallback_type": "unexpected_runtime_fallback" if runtime_fallback else (
            "expected_safe_fallback" if fallback_reason else "none"
        ),
        "v1_bypassed": bool(primary_metadata.get("v1_bypassed")),
        "silent_v1_fallback": int(any(owner and owner != "commerce_agent_v2" for owner in owners)),
        "duplicate_replies": max(0, len(outbound_rows) - 1),
        "requested_service_tier": requested_service_tier,
        "input_tokens": int(run.input_tokens or 0),
        "cached_input_tokens": int(usage_summary.get("cached_input_tokens") or 0),
        "output_tokens": int(run.output_tokens or 0),
        "total_tokens": int(run.total_tokens or 0),
        # These are deliberately unproven until the batch-level evidence and
        # grounded reply inspection enrich the record. The scorer fails closed.
        "unsupported_commercial_claims": None,
        "cross_tenant_leakage": None,
        "cross_customer_leakage": None,
        "write_mutations": None,
        "salla_mutations": None,
        # Fast mode is billed at 2x standard processing. The underlying usage
        # ledger records the model-token cost before this service-tier factor.
        "base_cost_usd": base_cost_usd,
        "cost_usd": base_cost_usd * (2 if requested_service_tier == "fast" else 1),
        "cost_basis": "usage_ledger_with_fast_2x_factor",
    }


def capture_controlled_state(
    db: Any,
    *,
    conversation_ids: Iterable[int],
    tenant_id: int = 1,
) -> dict[str, Any]:
    """Fingerprint only the three controlled conversations' order/shipment state."""
    from models import Conversation, Order, OrderShipment

    ids = sorted({int(value) for value in conversation_ids})
    conversations = (
        db.query(Conversation)
        .filter(Conversation.tenant_id == tenant_id, Conversation.id.in_(ids))
        .all()
    )
    if len(conversations) != len(ids):
        raise ValueError("controlled_conversation_scope_incomplete")
    customer_ids = sorted(
        {int(row.customer_id) for row in conversations if row.customer_id is not None}
    )
    orders = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.customer_id.in_(customer_ids))
        .order_by(Order.id.asc())
        .all()
        if customer_ids
        else []
    )
    order_ids = [int(row.id) for row in orders]
    shipments = (
        db.query(OrderShipment)
        .filter(OrderShipment.tenant_id == tenant_id, OrderShipment.order_id.in_(order_ids))
        .order_by(OrderShipment.id.asc())
        .all()
        if order_ids
        else []
    )
    state = {
        "orders": [
            {
                "id": int(row.id),
                "status": row.status,
                "total": row.total,
                "line_items": row.line_items,
                "metadata": row.extra_metadata,
            }
            for row in orders
        ],
        "shipments": [
            {
                "id": int(row.id),
                "order_id": int(row.order_id),
                "status": row.status,
                "provider": row.provider,
                "tracking_number": row.tracking_number,
                "label_url": row.label_url,
                "metadata": row.extra_metadata,
            }
            for row in shipments
        ],
    }
    return {
        "tenant_id": tenant_id,
        "conversation_count": len(conversations),
        "order_count": len(orders),
        "shipment_count": len(shipments),
        "state_sha256": hashlib.sha256(_canonical(state).encode()).hexdigest(),
    }


def compare_controlled_state(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    changed = before.get("state_sha256") != after.get("state_sha256")
    return {"write_mutations": int(changed), "salla_mutations": int(changed)}


__all__ = ["capture_controlled_state", "compare_controlled_state", "observe_persisted_turn"]
