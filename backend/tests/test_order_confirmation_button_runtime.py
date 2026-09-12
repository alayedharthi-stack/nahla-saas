"""Regression coverage for Meta dynamic URL parameters on order templates."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.automation_engine import (
    _execute_action,
    _resolve_dynamic_url_button_suffix,
)


def _resolve(template_url: str, payload: dict) -> str:
    return _resolve_dynamic_url_button_suffix(
        template_url,
        customer_name="هشام الحارثي",
        store_name="متجر تجريبي",
        payload=payload,
        config={},
        coupon_extras={},
    )


def test_order_summary_builds_approved_mtjr_suffix_without_tracking_url():
    payload = {
        "external_id": "2118127694",
        "order_id": 143,
        "order_number": "2118127694",
        "checkout_url": "",
        "payment_url": "",
    }

    assert _resolve("https://mtjr.at/{{1}}", payload) == "orders/2118127694"


def test_explicit_order_tracking_url_has_priority():
    payload = {
        "order_tracking_url": "https://mtjr.at/orders/public-ABC",
        "order_number": "2118127694",
    }

    assert _resolve("https://mtjr.at/{{1}}", payload) == "orders/public-ABC"


def test_fixed_orders_prefix_uses_encoded_order_reference():
    payload = {"external_order_number": "SLL 42/7", "order_id": 143}

    assert (
        _resolve("https://merchant.example/orders/{{1}}", payload)
        == "SLL%2042%2F7"
    )


def test_unresolvable_dynamic_button_never_returns_whitespace():
    suffix = _resolve("https://merchant.example/custom/{{1}}", {"order_id": 143})

    assert suffix == ""
    assert not suffix.isspace()


def test_production_order_event_sends_non_empty_meta_button_parameter():
    customer = SimpleNamespace(
        id=65,
        tenant_id=1,
        name="هشام الحارثي",
        phone="+966549815590",
    )
    connection = SimpleNamespace(phone_number_id="phone-id", status="connected")
    template = SimpleNamespace(
        id=438,
        tenant_id=1,
        name="nahla_order_confirmation_r3_dc8a88",
        language="ar",
        service_key="order_confirmation",
        status="APPROVED",
        components=[
            {"type": "HEADER", "format": "IMAGE"},
            {"type": "BODY", "text": "{{1}} {{2}} {{3}} {{4}}"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL",
                        "text": "عرض تفاصيل الطلب",
                        "url": "https://mtjr.at/{{1}}",
                    }
                ],
            },
        ],
    )
    event = SimpleNamespace(
        id=25602,
        customer_id=65,
        event_type="order_notifications",
        payload={
            "external_id": "2118127694",
            "order_id": 143,
            "order_internal_id": 143,
            "order_number": "2118127694",
            "external_order_number": None,
            "checkout_url": "",
            "payment_url": "",
            "total": 174,
        },
    )
    automation = SimpleNamespace(
        id=77,
        automation_type="order_notifications",
        template_id=None,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [
        customer,
        connection,
    ]
    provider_send = AsyncMock(
        return_value=({"messages": [{"id": "wamid.order.1"}]}, object())
    )

    with (
        patch("core.acceptance_execution_context.deny_external_egress"),
        patch("core.billing.has_billing_access", return_value=True),
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(
                allowed=True, reason="", used_total=0, limit=100
            ),
        ),
        patch("core.wa_usage.has_open_service_window", return_value=False),
        patch(
            "core.commerce_lifecycle.canary_guard.evaluate_and_audit",
            return_value=SimpleNamespace(allowed=True, reason=""),
        ),
        patch(
            "core.commerce_lifecycle.canary_guard.lifecycle_dispatch_owns_legacy_send",
            return_value=False,
        ),
        patch(
            "core.commerce_lifecycle.order_updates.evaluate_order_update_delivery",
            return_value=(True, ""),
        ),
        patch(
            "core.service_template_resolver.resolve_template_for_send",
            return_value=template,
        ),
        patch(
            "core.automation_engine._resolve_auto_coupon",
            new=AsyncMock(return_value={}),
        ),
        patch("core.automation_engine._resolve_store_name", return_value="متجر تجريبي"),
        patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=provider_send,
        ),
        patch("routers.conversations.record_outbound_message"),
    ):
        ok, info = asyncio.run(_execute_action(db, 1, event, automation, {}))

    assert ok is True
    assert info["wa_message_id"] == "wamid.order.1"
    sent_payload = provider_send.await_args.kwargs["payload"]
    button = next(
        component
        for component in sent_payload["template"]["components"]
        if component.get("sub_type") == "url"
    )
    assert button["parameters"] == [
        {"type": "text", "text": "orders/2118127694"}
    ]
