"""Focused regressions for the internal conversational E2E safety plane."""
from __future__ import annotations

import asyncio
from types import MethodType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.acceptance_execution_context import (
    INTERNAL_E2E_EGRESS_DENIED,
    InternalE2EEgressDenied,
    current_acceptance_context,
    deny_external_egress,
    internal_conversational_e2e_context,
    recorded_egress_denials,
)


TENANT_ID = 48
SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _run(coro):
    return asyncio.run(coro)


def test_no_context_is_a_strict_noop() -> None:
    assert current_acceptance_context() is None
    assert recorded_egress_denials() == ()
    assert deny_external_egress(
        egress_kind="whatsapp_provider",
        operation="send_message",
        tenant_id=TENANT_ID,
    ) is None


def test_context_is_nested_reset_safe_and_immutable() -> None:
    with internal_conversational_e2e_context(
        session_id="22222222-2222-4222-8222-222222222222",
        tenant_id=TENANT_ID,
        allow_llm_inference=True,
    ) as outer:
        assert current_acceptance_context() is outer
        assert outer.allow_llm_inference is True
        with pytest.raises(AttributeError):
            outer.tenant_id = 49  # type: ignore[misc]

        with internal_conversational_e2e_context(
            session_id="33333333-3333-4333-8333-333333333333",
            tenant_id=49,
        ) as inner:
            assert current_acceptance_context() is inner
        assert current_acceptance_context() is outer

    assert current_acceptance_context() is None
    assert recorded_egress_denials() == ()


@pytest.mark.parametrize("tenant_id", [None, 0, -1, True, "48"])
def test_context_requires_positive_explicit_tenant(tenant_id) -> None:
    with pytest.raises(ValueError, match="internal_e2e_tenant_id_invalid"):
        with internal_conversational_e2e_context(
            session_id=SESSION_ID,
            tenant_id=tenant_id,
        ):
            pass


def test_tenant_mismatch_is_typed_and_audited_without_pii() -> None:
    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    ):
        with pytest.raises(InternalE2EEgressDenied) as caught:
            deny_external_egress(
                egress_kind="salla_integration",
                operation="post",
                tenant_id=49,
            )
        audit = caught.value.to_audit_dict()
        assert audit["code"] == INTERNAL_E2E_EGRESS_DENIED
        assert audit["reason"] == "tenant_mismatch"
        assert audit["tenant_id"] == TENANT_ID
        assert audit["requested_tenant_id"] == 49
        assert len(recorded_egress_denials()) == 1
        assert "phone" not in audit
        assert "payload" not in audit
        assert "token" not in audit


def test_invalid_requested_tenant_is_still_typed_denial() -> None:
    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    ), pytest.raises(InternalE2EEgressDenied) as caught:
        deny_external_egress(
            egress_kind="whatsapp_provider",
            operation="send_message",
            tenant_id=None,
        )

    assert caught.value.audit.reason == "requested_tenant_invalid"


def test_whatsapp_denies_before_token_or_provider_work() -> None:
    from services.whatsapp_platform.service import provider_send_message

    with patch(
        "services.whatsapp_platform.service.get_token_for_operation",
        new=AsyncMock(),
    ) as token_lookup, patch(
        "services.whatsapp_platform.service.provider_post_with_context",
        new=AsyncMock(),
    ) as provider_post, internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        allow_llm_inference=True,
    ):
        with pytest.raises(InternalE2EEgressDenied) as caught:
            _run(provider_send_message(
                MagicMock(),
                MagicMock(),
                tenant_id=TENANT_ID,
                operation="send_message",
                phone_id="generic-phone-id",
                payload={"type": "text", "to": "966500000000"},
            ))

    assert caught.value.egress_kind == "whatsapp_provider"
    token_lookup.assert_not_awaited()
    provider_post.assert_not_awaited()
    assert "wamid" not in str(caught.value.to_audit_dict()).lower()


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("_get", ("/products",)),
        ("_post", ("/orders", {"products": []})),
        ("_delete", ("/coupons/1",)),
        ("_refresh_access_token", ()),
    ],
)
def test_salla_transport_denies_before_freshness_or_http(
    method_name: str,
    args: tuple,
) -> None:
    from store_adapters.salla_adapter import SallaAdapter

    adapter = SallaAdapter(
        api_key="unused",
        refresh_token="unused",
        tenant_id=TENANT_ID,
        integration_id=7,
    )
    with patch.object(
        adapter,
        "_ensure_token_fresh",
        new=AsyncMock(),
    ) as freshness, patch(
        "store_adapters.salla_adapter.httpx.AsyncClient",
    ) as http_client, internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    ):
        with pytest.raises(InternalE2EEgressDenied):
            _run(getattr(adapter, method_name)(*args))

    freshness.assert_not_awaited()
    http_client.assert_not_called()


def test_automation_event_denied_before_construction_or_db_mutation() -> None:
    from core.automation_engine import emit_automation_event

    db = MagicMock()
    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    ), pytest.raises(InternalE2EEgressDenied):
        emit_automation_event(
            db,
            TENANT_ID,
            "generic_order_created",
            customer_id=100,
            payload={"source": "synthetic"},
        )

    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


def test_pending_automation_denied_before_query_or_mutation() -> None:
    from core.automation_engine import process_pending_events

    db = MagicMock()
    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    ), pytest.raises(InternalE2EEgressDenied):
        _run(process_pending_events(db, TENANT_ID))

    db.query.assert_not_called()
    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


def test_campaign_denied_after_tenant_lookup_before_mutation() -> None:
    from services.campaign_dispatcher import dispatch_campaign

    campaign_id = 301
    db = MagicMock()
    db.query.return_value.filter.return_value.scalar.return_value = TENANT_ID

    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    ), pytest.raises(InternalE2EEgressDenied):
        _run(dispatch_campaign(db, campaign_id))

    db.query.assert_called_once()
    db.query.return_value.filter.return_value.first.assert_not_called()
    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


def test_payment_service_denies_before_lookup_and_placeholder_fallback() -> None:
    from store_integration.payment_service import generate_payment_link

    with patch(
        "store_integration.payment_service._get_moyasar_settings",
    ) as settings, patch(
        "store_integration.payment_service.get_adapter",
    ) as adapter, internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    ):
        with pytest.raises(InternalE2EEgressDenied) as caught:
            _run(generate_payment_link(
                TENANT_ID,
                "synthetic-order-1",
                125.0,
                description="",
            ))

    settings.assert_not_called()
    adapter.assert_not_called()
    assert "pay.nahlah.ai" not in str(caught.value)


def test_payment_service_no_context_preserves_placeholder_fallback() -> None:
    from store_integration.payment_service import generate_payment_link

    with patch(
        "store_integration.payment_service._get_moyasar_settings",
        return_value={},
    ), patch(
        "store_integration.payment_service.get_adapter",
        return_value=None,
    ):
        link = _run(generate_payment_link(
            TENANT_ID,
            "synthetic-order-2",
            50.0,
        ))

    assert link.startswith(f"https://pay.nahlah.ai/checkout/{TENANT_ID}-")


def test_moyasar_transport_denies_before_http() -> None:
    from payment_gateways.moyasar import MoyasarClient

    client = MoyasarClient(secret_key="unused")
    with patch(
        "payment_gateways.moyasar.httpx.AsyncClient",
    ) as http_client, internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    ):
        with pytest.raises(InternalE2EEgressDenied):
            _run(client.create_invoice(
                amount_sar=10,
                description="",
                callback_url="https://example.invalid/callback",
                metadata={"tenant_id": str(TENANT_ID)},
            ))

    http_client.assert_not_called()


def test_commerce_runtime_preserves_typed_web_search_denial() -> None:
    from modules.ai.commerce.runtime import CommerceToolRuntime
    from modules.ai.security import TenantIsolationLayer

    runtime = CommerceToolRuntime.__new__(CommerceToolRuntime)
    runtime.tenant_id = TENANT_ID
    runtime.tenant_context = TenantIsolationLayer.make_context(TENANT_ID)

    with patch(
        "modules.ai.tools.web_search.search_web",
        new=AsyncMock(),
    ) as search_web, internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        allow_llm_inference=True,
    ):
        result = _run(runtime.execute("web_search", {"query": "generic product"}))

    assert result.ok is False
    assert result.error == INTERNAL_E2E_EGRESS_DENIED
    assert result.payload == {}
    assert result.audit["egress_kind"] == "external_tool"
    assert result.audit["operation"] == "web_search"
    assert result.audit["denial_id"]
    assert "query" not in result.audit
    search_web.assert_not_awaited()


def test_commerce_runtime_discards_success_when_wrapper_swallows_denial() -> None:
    from modules.ai.commerce.runtime import CommerceToolRuntime, ToolExecutionResult
    from modules.ai.security import TenantIsolationLayer

    runtime = CommerceToolRuntime.__new__(CommerceToolRuntime)
    runtime.tenant_id = TENANT_ID
    runtime.tenant_context = TenantIsolationLayer.make_context(TENANT_ID)

    async def _swallowing_tool(self, payload):
        try:
            deny_external_egress(
                egress_kind="salla_integration",
                operation="post",
                tenant_id=self.tenant_id,
            )
        except InternalE2EEgressDenied:
            return ToolExecutionResult(
                ok=True,
                tool_name="swallowing_probe",
                payload={"checkout_url": "https://synthetic.invalid/success"},
            )

    runtime._tool_swallowing_probe = MethodType(_swallowing_tool, runtime)
    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
    ):
        result = _run(runtime.execute("swallowing_probe", {}))

    assert result.ok is False
    assert result.error == INTERNAL_E2E_EGRESS_DENIED
    assert result.payload == {}
    assert result.audit["egress_kind"] == "salla_integration"
    assert "checkout_url" not in result.audit
