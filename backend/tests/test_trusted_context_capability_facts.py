"""CAPABILITIES domain loader resolves from platform truth sources.

``modules.ai.brain.truth_surface.trusted_context._load_capability_facts``
imported ``modules.ai.commerce_agent.capability_resolver`` — a package that
was never committed (referenced since #1019). In production every WhatsApp
brain turn logged ``ModuleNotFoundError: No module named
'modules.ai.commerce_agent'`` with a full traceback and the CAPABILITIES
domain was silently absent from every trusted-context snapshot.

The loader now composes the existing resolvers (sales channels, merchant
capabilities, commerce permissions). These tests fail on the previous code:
the loader returned ``[]`` and logged the exception.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface import trusted_context  # noqa: E402
from modules.ai.brain.truth_surface.contract import TrustedDomain, TruthSource  # noqa: E402
from modules.ai.commerce.permission_loader import PermissionLoadResult  # noqa: E402
from modules.ai.commerce.permissions import CommercePermissionSet  # noqa: E402

_PHANTOM_IMPORTS = ("from modules.ai.commerce_agent", "import modules.ai.commerce_agent")


def _slot(enabled: bool, available: bool) -> SimpleNamespace:
    return SimpleNamespace(enabled=enabled, available=available, evidence="test")


def _channels(**overrides):
    values = dict(
        store_url="https://example.test",
        online_store=_slot(True, True),
        whatsapp_quick_order=_slot(True, True),
        showroom_visit=_slot(True, False),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _merchant(**overrides):
    values = dict(has_whatsapp_catalog=True, supports_cod=True)
    values.update(overrides)
    return SimpleNamespace(**values)


def _permissions(**flags) -> PermissionLoadResult:
    return PermissionLoadResult(
        permissions=CommercePermissionSet(tenant_id=1, **flags), source="db_row",
    )


def _facts_by_key(facts):
    return {fact.key: fact for fact in facts}


def test_capability_loader_no_longer_references_the_uncommitted_package() -> None:
    source = inspect.getsource(trusted_context._load_capability_facts) + inspect.getsource(
        trusted_context._resolve_tenant_capabilities
    )
    assert not any(marker in source for marker in _PHANTOM_IMPORTS)


def test_capability_facts_come_from_platform_resolvers() -> None:
    db = MagicMock()
    with patch(
        "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
        return_value=_channels(),
    ) as channels, patch(
        "core.merchant_capabilities.resolve_merchant_capabilities",
        return_value=_merchant(),
    ) as merchant, patch(
        "modules.ai.commerce.permission_loader.load_tenant_commerce_permissions",
        return_value=_permissions(can_cancel_orders=False, can_apply_coupons=False),
    ) as perms, patch.object(trusted_context.logger, "exception") as logged:
        facts = trusted_context._load_capability_facts(db, 7)

    channels.assert_called_once_with(db, 7)
    merchant.assert_called_once_with(db, 7)
    perms.assert_called_once_with(db, 7)
    logged.assert_not_called()

    by_key = _facts_by_key(facts)
    assert set(by_key) == {
        "whatsapp_order", "online_store", "pickup", "native_catalog",
        "showroom_enabled", "cod_enabled", "store_url", "available_tools",
    }
    for fact in facts:
        assert fact.domain == TrustedDomain.CAPABILITIES
        assert fact.source == TruthSource.TENANT_SETTINGS
        assert fact.path == f"capabilities.{fact.key}"
    assert by_key["whatsapp_order"].value is True
    assert by_key["online_store"].value is True
    assert by_key["pickup"].value is False          # showroom enabled but not available
    assert by_key["showroom_enabled"].value is True
    assert by_key["native_catalog"].value is True
    assert by_key["cod_enabled"].value is True
    assert by_key["store_url"].value == "https://example.test"
    tools = by_key["available_tools"].value
    assert "search_products" in tools and "track_order" in tools
    assert "create_draft_order" in tools
    assert "apply_coupon" not in tools and "cancel_order" not in tools
    assert "delete_customer" not in tools


def test_channel_counts_only_when_enabled_and_available() -> None:
    with patch(
        "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
        return_value=_channels(
            online_store=_slot(True, False),
            whatsapp_quick_order=_slot(False, True),
            showroom_visit=_slot(True, True),
            store_url="",
        ),
    ), patch(
        "core.merchant_capabilities.resolve_merchant_capabilities",
        return_value=_merchant(has_whatsapp_catalog=False, supports_cod=False),
    ), patch(
        "modules.ai.commerce.permission_loader.load_tenant_commerce_permissions",
        return_value=_permissions(),
    ):
        by_key = _facts_by_key(trusted_context._load_capability_facts(MagicMock(), 7))

    assert by_key["online_store"].value is False
    assert by_key["whatsapp_order"].value is False
    assert by_key["pickup"].value is True
    assert by_key["native_catalog"].value is False
    assert by_key["cod_enabled"].value is False
    assert "store_url" not in by_key  # blank values are never emitted as facts


def test_capability_domain_reaches_the_snapshot() -> None:
    with patch.object(trusted_context, "_load_customer_order_facts", return_value=[]), patch.object(
        trusted_context, "_load_state_order_facts", return_value=[]
    ), patch.object(trusted_context, "_load_payment_shipment_facts", return_value=[]), patch.object(
        trusted_context, "_load_merchant_capability_facts", return_value=[]
    ), patch.object(trusted_context, "_load_merchant_profile_facts", return_value=[]), patch.object(
        trusted_context, "_load_merchant_policy_facts", return_value=[]
    ), patch(
        "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
        return_value=_channels(),
    ), patch(
        "core.merchant_capabilities.resolve_merchant_capabilities",
        return_value=_merchant(),
    ), patch(
        "modules.ai.commerce.permission_loader.load_tenant_commerce_permissions",
        return_value=_permissions(),
    ):
        snap = trusted_context.build_trusted_context_snapshot(
            db=MagicMock(), tenant_id=7, customer_phone="966500000001", message="السلام عليكم",
        )
    assert TrustedDomain.CAPABILITIES.value in snap.loaded_domains
    assert "capability_resolver" in snap.sources


def test_resolver_failure_still_fails_open() -> None:
    with patch.object(
        trusted_context, "_resolve_tenant_capabilities", side_effect=RuntimeError("boom"),
    ), patch.object(trusted_context.logger, "exception") as logged:
        facts = trusted_context._load_capability_facts(MagicMock(), 7)
    assert facts == []
    logged.assert_called_once()
    assert "capabilities failed" in str(logged.call_args.args[0])
