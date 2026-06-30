"""
commerce_confidence_helpers.py
──────────────────────────────
Shared helpers for AI Commerce Confidence Gate suites.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

from commerce_scenario_fixtures import list_orders
from commerce_scenario_runner import AIScenarioRunner
from routers.ai_playground import PlaygroundDryRunBody, playground_dry_run

INTERNAL_KB_MARKERS = (
    "قواعد علينا يجب أن يلتزم بها الذكاء",
    "قاعدة المعرفة الرسمية",
    "تعليمات للنظام",
    "guardrail",
    "system prompt",
)

PHONE_OR_ADDRESS_MARKERS = (
    "رقم الجوال",
    "رقم الهاتف",
    "ما هو رقم",
    "google maps",
)

ADDRESS_MARKERS = (
    "العنوان",
    "العنوان الوطني",
    "رابط الموقع",
)


def run_async(coro):
    return asyncio.run(coro)


def format_confidence_failure(
    *,
    scenario: str,
    message: str,
    reason: str = "",
    payload: Optional[Dict[str, Any]] = None,
    runner_result: Any = None,
    orders_before: Optional[int] = None,
    orders_after: Optional[int] = None,
) -> str:
    lines = [
        f"AI Commerce Confidence failed: {scenario}",
        f"  reason: {reason or 'assertion failed'}",
        f"  message: {message!r}",
    ]
    if payload is not None:
        lines.extend([
            f"  reply_text: {payload.get('reply_text')!r}",
            f"  decision_topic: {payload.get('decision_topic')!r}",
            f"  decision_action: {payload.get('decision_action')!r}",
            f"  owner: {payload.get('owner')!r}",
            f"  blocked_reason: {payload.get('blocked_reason')!r}",
            f"  would_send: {payload.get('would_send')!r}",
            f"  outbound_kind: {payload.get('outbound_kind')!r}",
            f"  warnings: {payload.get('warnings')!r}",
            f"  side_effects: {payload.get('side_effects')!r}",
        ])
    if runner_result is not None:
        customer = getattr(runner_result, "customer", None)
        convo = getattr(runner_result, "conversation", None)
        lines.extend([
            f"  customer_name: {getattr(customer, 'name', None)!r}",
            f"  customer_phone: {getattr(customer, 'normalized_phone', None)!r}",
            f"  conversation_ai_paused: {getattr(convo, 'ai_paused', None)!r}",
            f"  fake_outbound_count: {getattr(runner_result, 'fake_outbound_count', None)!r}",
            f"  errors: {getattr(runner_result, 'errors', None)!r}",
        ])
    if orders_before is not None or orders_after is not None:
        lines.append(f"  order_count: before={orders_before} after={orders_after}")
    return "\n".join(lines)


def assert_no_side_effects(payload: Dict[str, Any]) -> None:
    effects = dict(payload.get("side_effects") or {})
    assert effects.get("whatsapp_sent") is False
    assert effects.get("order_created") is False
    assert effects.get("customer_updated") is False
    assert effects.get("automation_triggered") is False


def assert_no_internal_kb(reply_text: Optional[str]) -> None:
    text = str(reply_text or "")
    for marker in INTERNAL_KB_MARKERS:
        assert marker not in text, f"internal KB leaked: {marker!r}"
    assert not re.search(r"^#+\s", text, flags=re.MULTILINE), "markdown KB heading leaked"


def assert_no_phone_request(reply_text: Optional[str]) -> None:
    lowered = str(reply_text or "").lower()
    for marker in PHONE_OR_ADDRESS_MARKERS:
        assert marker.lower() not in lowered, f"phone request leaked: {marker!r}"


def assert_no_address_request(reply_text: Optional[str]) -> None:
    lowered = str(reply_text or "").lower()
    for marker in ADDRESS_MARKERS:
        if marker == "رابط" and ("تتبع" in lowered or "trk" in lowered):
            continue
        assert marker not in lowered, f"address request leaked: {marker!r}"


def call_playground_endpoint(
    db,
    tenant_id: int,
    *,
    message: str,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    request = MagicMock()
    body = (
        PlaygroundDryRunBody(message=message, context=context)
        if context is not None
        else PlaygroundDryRunBody(message=message)
    )
    with patch(
        "routers.ai_playground.resolve_tenant_id",
        return_value=tenant_id,
    ), patch(
        "routers.ai_playground.get_or_create_tenant",
        return_value=MagicMock(id=tenant_id),
    ), patch(
        "services.ai_playground_dry_run.has_billing_access",
        return_value=True,
    ):
        return run_async(playground_dry_run(request, body, db))


def scenario_order_count(db, tenant_id: int) -> int:
    return len(list_orders(db, tenant_id))


def make_runner(world) -> AIScenarioRunner:
    return AIScenarioRunner(world)
