"""Silent recovery must honor tenant purchase-channel authority.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: E402
    build_bare_start_order_guard_reply,
)
from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: E402
    MerchantSalesChannels,
    SalesChannelSlot,
)
from modules.ai.brain.postprocess.conversation_recovery import (  # noqa: E402
    try_guard_recovery_reply,
)

_LIVE_BUY = "ابي اشتري"
_TENANT_A = 11
_TENANT_B = 44
_STORE = "https://shop.example.sa"
_MAPS = "https://maps.google.com/?q=showroom"
_CANNED = build_bare_start_order_guard_reply(_LIVE_BUY)


def _slot(*, enabled: bool, available: bool, evidence: str) -> SalesChannelSlot:
    return SalesChannelSlot(enabled=enabled, available=available, evidence=evidence)


def _sales(
    *,
    store: bool = False,
    whatsapp: bool = False,
    showroom: bool = False,
) -> MerchantSalesChannels:
    return MerchantSalesChannels(
        store_url=_STORE if store else "",
        store_url_source="structured_settings" if store else "none",
        maps_url=_MAPS if showroom else "",
        online_store=_slot(
            enabled=store, available=store, evidence="store_url" if store else "none"
        ),
        whatsapp_quick_order=_slot(
            enabled=whatsapp,
            available=whatsapp,
            evidence="whatsapp_order_processing" if whatsapp else "whatsapp_connection_unavailable",
        ),
        showroom_visit=_slot(
            enabled=showroom,
            available=showroom,
            evidence="maps_url" if showroom else "none",
        ),
    )


def _three() -> MerchantSalesChannels:
    return _sales(store=True, whatsapp=True, showroom=True)


class TestSilentRecoveryHonorsTenantChannels:
    def test_multi_channel_silent_recovery_does_not_use_canned_whatsapp(self) -> None:
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            return_value=_three(),
        ) as mock_resolve:
            recovery = try_guard_recovery_reply(
                inbound_text=_LIVE_BUY,
                db=MagicMock(),
                tenant_id=_TENANT_A,
            )
        assert mock_resolve.call_count == 1
        assert recovery.needs_persona_compose is True
        assert recovery.source == "purchase_channel_selection_pending"
        assert not recovery.reply
        assert recovery.reply != _CANNED
        assert "كتالوج واتساب" not in (recovery.reply or "")

    def test_awaiting_persist_failure_silent_recovery_does_not_use_canned_whatsapp(
        self,
    ) -> None:
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            return_value=_three(),
        ) as mock_resolve:
            recovery = try_guard_recovery_reply(
                inbound_text=_LIVE_BUY,
                state={
                    "order_prep": {
                        "awaiting_checkout_channel": True,
                        "checkout_channel": "",
                        "offered_purchase_channel_ids": [
                            "online_store",
                            "whatsapp_quick_order",
                            "showroom_visit",
                        ],
                    }
                },
                db=MagicMock(),
                tenant_id=_TENANT_A,
            )
        assert mock_resolve.call_count == 1
        assert recovery.needs_persona_compose is True
        assert recovery.source == "purchase_channel_selection_pending"
        assert recovery.reply != _CANNED
        assert "كتالوج واتساب" not in (recovery.reply or "")

    def test_should_block_receives_resolved_sales_object_once(self) -> None:
        sales = _three()
        captured: dict[str, object] = {}

        def _fake_block(**kwargs):
            captured.update(kwargs)
            return True

        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            return_value=sales,
        ) as mock_resolve:
            with patch(
                "modules.ai.brain.commerce.checkout_route_owner.should_block_bare_start_product_prompt",
                side_effect=_fake_block,
            ):
                recovery = try_guard_recovery_reply(
                    inbound_text=_LIVE_BUY,
                    db=MagicMock(),
                    tenant_id=_TENANT_A,
                )
        assert mock_resolve.call_count == 1
        assert captured.get("merchant_sales_channels") is sales
        assert "db" not in captured
        assert "tenant_id" not in captured
        assert recovery.needs_persona_compose is True
        assert recovery.source == "purchase_channel_selection_pending"

    def test_should_block_typeerror_fails_closed_no_canned_reply(self) -> None:
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            return_value=_three(),
        ) as mock_resolve:
            with patch(
                "modules.ai.brain.commerce.checkout_route_owner.should_block_bare_start_product_prompt",
                side_effect=TypeError("unexpected kwargs"),
            ):
                recovery = try_guard_recovery_reply(
                    inbound_text=_LIVE_BUY,
                    db=MagicMock(),
                    tenant_id=_TENANT_A,
                )
        assert mock_resolve.call_count == 1
        assert recovery.needs_persona_compose is True
        assert recovery.source == "purchase_channel_selection_pending"
        assert recovery.source != "bare_start_order"
        assert recovery.reply != _CANNED
        assert "كتالوج واتساب" not in (recovery.reply or "")

    def test_resolver_failure_does_not_use_canned_whatsapp_reply(self) -> None:
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            side_effect=RuntimeError("capability lookup failed"),
        ) as mock_resolve:
            recovery = try_guard_recovery_reply(
                inbound_text=_LIVE_BUY,
                db=MagicMock(),
                tenant_id=_TENANT_A,
            )
        assert mock_resolve.call_count == 1
        assert recovery.source != "bare_start_order"
        assert recovery.needs_persona_compose is True
        assert recovery.reply != _CANNED
        assert "كتالوج واتساب" not in (recovery.reply or "")

    def test_recovery_tenant_isolation_uses_requested_tenant(self) -> None:
        seen: list[int] = []

        def _fake_resolve(db, tenant_id, **kwargs):
            seen.append(int(tenant_id))
            return _three() if int(tenant_id) == _TENANT_A else _sales(whatsapp=True)

        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            side_effect=_fake_resolve,
        ) as mock_resolve:
            rec_a = try_guard_recovery_reply(
                inbound_text=_LIVE_BUY,
                db=MagicMock(),
                tenant_id=_TENANT_A,
            )
            rec_b = try_guard_recovery_reply(
                inbound_text=_LIVE_BUY,
                db=MagicMock(),
                tenant_id=_TENANT_B,
            )
        assert mock_resolve.call_count == 2
        assert set(seen) == {_TENANT_A, _TENANT_B}
        assert rec_a.source == "purchase_channel_selection_pending"
        assert rec_a.needs_persona_compose is True
        assert rec_b.source == "bare_start_order"
        assert rec_b.reply == _CANNED
