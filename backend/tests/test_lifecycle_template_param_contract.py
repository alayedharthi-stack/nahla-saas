"""Regression coverage for lifecycle template parameter contract assembly."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _path in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.automation_engine import send_lifecycle_whatsapp_template  # noqa: E402
from core.commerce_lifecycle.dispatch import _build_dispatch_payload  # noqa: E402
from core.commerce_lifecycle.external_shadow_producer import (  # noqa: E402
    build_order_lifecycle_evidence,
)


def _order_summary_template() -> SimpleNamespace:
    return SimpleNamespace(
        id=198,
        name="nahla_order_summary_8d9f",
        language="ar",
        nahla_source_key="order_summary",
        components=[
            {
                "type": "BODY",
                "text": (
                    "تم استلام طلبك يا {{1}} 📦\n\n"
                    "رقم الطلب: #{{2}}\n"
                    "المبلغ الإجمالي: {{3}} ريال"
                ),
            },
            {"type": "FOOTER", "text": "نحلة — مساعد متجرك"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL",
                        "text": "عرض تفاصيل الطلب",
                        "url": "https://example.com/{{1}}",
                    }
                ],
            },
        ],
    )


def _provider_payload(mock_send: AsyncMock) -> dict:
    mock_send.assert_awaited_once()
    return mock_send.await_args.kwargs["payload"]


@pytest.mark.parametrize(
    ("order_number", "order_total", "checkout_url", "expected_suffix"),
    [
        pytest.param(
            "284264061",
            "174.0",
            "https://mtjr.at/7aa3dvwy5y",
            "7aa3dvwy5y",
            id="observed-order-139-contract",
        ),
        pytest.param(
            "GEN-SHOE-8801",
            "249.0",
            "https://shop.generic-commerce.org/orders/GEN-SHOE-8801",
            "orders/GEN-SHOE-8801",
            id="generic-commerce-contract",
        ),
    ],
)
def test_order_summary_contract_calls_provider_once_with_correct_parameters(
    monkeypatch,
    order_number: str,
    order_total: str,
    checkout_url: str,
    expected_suffix: str,
) -> None:
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", "20")
    monkeypatch.setenv(
        "COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST",
        "+966500111222",
    )

    order = SimpleNamespace(
        id=139,
        external_id="765203672",
        external_order_number=order_number,
        status="in_progress",
        total=order_total,
        checkout_url="",
        customer_name="نورة عبدالله",
        customer_info={
            "name": "نورة عبدالله",
            "phone": "+966500111222",
        },
        extra_metadata={"payment_method": "waiting"},
    )
    evidence = build_order_lifecycle_evidence(
        order=order,
        normalized_order={
            "external_id": order.external_id,
            "external_order_number": order_number,
            "status": "in_progress",
        },
        raw_payload={
            "payment_actions": {
                "remaining_action": {"checkout_url": checkout_url},
            }
        },
        source_event_id="sem:observed-order-created",
        transition_version="observed-order-created",
    )
    assert evidence.checkout_url == checkout_url

    payload = _build_dispatch_payload(evidence, order=order)
    assert payload["total"] == order_total

    wa_connection = SimpleNamespace(
        tenant_id=20,
        status="connected",
        phone_number_id="phone-id-generic",
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = wa_connection

    with (
        patch("core.billing.has_billing_access", return_value=True),
        patch("core.automation_engine._resolve_store_name", return_value="متجر تجريبي عام"),
        patch(
            "services.whatsapp_platform.service.provider_send_message",
            new_callable=AsyncMock,
            return_value=(
                {"messages": [{"id": "wamid.contract.001"}]},
                SimpleNamespace(),
            ),
        ) as mock_provider_send,
        patch(
            "modules.ai.brain.pipeline.MerchantBrain.process",
            new_callable=AsyncMock,
        ) as mock_brain,
        patch("modules.ai.orchestrator.adapter.generate_ai_reply") as mock_ai_reply,
        patch(
            "modules.ai.orchestrator.adapter.generate_orchestrate_response",
            new_callable=AsyncMock,
        ) as mock_orchestrate,
    ):
        outcome, info = asyncio.run(
            send_lifecycle_whatsapp_template(
                db,
                20,
                "+966500111222",
                _order_summary_template(),
                payload,
                customer_name=order.customer_name,
                service_key="order_confirmation",
            )
        )

    assert outcome == "sent"
    assert info["wa_message_id"] == "wamid.contract.001"
    provider_payload = _provider_payload(mock_provider_send)
    components = provider_payload["template"]["components"]
    header = [item for item in components if item["type"] == "header"]
    body = [item for item in components if item["type"] == "body"]
    buttons = [item for item in components if item["type"] == "button"]

    assert len(header) == 0
    assert len(body) == 1
    assert len(body[0]["parameters"]) == 3
    assert [item["text"] for item in body[0]["parameters"]] == [
        order.customer_name,
        order_number,
        order_total,
    ]
    assert len(buttons) == 1
    assert buttons[0]["index"] == "0"
    assert buttons[0]["parameters"] == [
        {"type": "text", "text": expected_suffix},
    ]
    assert mock_provider_send.await_count == 1
    assert mock_brain.await_count + mock_ai_reply.call_count + mock_orchestrate.await_count == 0


def test_missing_dynamic_button_url_blocks_before_provider_call(monkeypatch) -> None:
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", "20")
    monkeypatch.setenv(
        "COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST",
        "+966500111222",
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        tenant_id=20,
        status="connected",
        phone_number_id="phone-id-generic",
    )
    with (
        patch("core.billing.has_billing_access", return_value=True),
        patch("core.automation_engine._resolve_store_name", return_value="متجر تجريبي عام"),
        patch(
            "services.whatsapp_platform.service.provider_send_message",
            new_callable=AsyncMock,
        ) as mock_provider_send,
    ):
        outcome, info = asyncio.run(
            send_lifecycle_whatsapp_template(
                db,
                20,
                "+966500111222",
                _order_summary_template(),
                {
                    "order_number": "GEN-NO-URL-1",
                    "total": "199.0",
                },
                customer_name="أحمد سالم",
                service_key="order_confirmation",
            )
        )

    assert outcome == "failed"
    assert info["error_code"] == "missing_template_evidence"
    assert info["missing_fields"] == ["button[0].url"]
    mock_provider_send.assert_not_awaited()
