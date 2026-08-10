"""Pack B — Salla merchant-enabled shipping/payment capability truth."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from core.salla_merchant_capabilities import (
    STATUS_EMPTY,
    STATUS_FORBIDDEN,
    STATUS_KNOWN,
    STATUS_UNKNOWN,
    assert_no_fabricated_cod,
    find_zone_ids_for_city,
    merge_checkout_profile_into_config,
    normalize_payment_method_entry,
    payment_codes,
    project_merchant_capabilities,
    resource_block,
    shipping_company_names,
)
from modules.ai.brain.truth_surface.contract import (
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.trusted_context import (
    _load_merchant_capability_facts,
)
from modules.ai.brain.truth_surface.trusted_context_brain_projection import (
    project_trusted_context_brain_facts,
)


def _profile(
    *,
    payments_status: str,
    payment_items: Optional[List[Dict[str, Any]]] = None,
    company_status: str = STATUS_KNOWN,
    companies: Optional[List[Dict[str, Any]]] = None,
    zone_status: str = STATUS_KNOWN,
    zones: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    pay_items = list(payment_items or [])
    company_items = list(companies or [])
    zone_items = list(zones or [])
    return {
        "schema_version": 1,
        "source": "salla",
        "surface": "salla_storefront",
        "last_synced_at": "2026-08-10T00:00:00+00:00",
        "payment_methods": [i["code"] for i in pay_items if i.get("code")],
        "shipping_companies": company_items,
        "shipping_zones": zone_items,
        "payments": resource_block(
            status=payments_status,
            endpoint="/payment/methods",
            scope="payments.read",
            items=pay_items,
            extra={"query": {"status": "enabled"}},
        ),
        "shipping": {
            "companies": resource_block(
                status=company_status,
                endpoint="/shipping/companies/",
                scope="shipping.read",
                items=company_items,
            ),
            "zones": resource_block(
                status=zone_status,
                endpoint="/shipping/zones",
                scope="shipping.read",
                items=zone_items,
            ),
        },
    }


def test_normalize_payment_method_uses_slug_not_hardcoded_list() -> None:
    assert normalize_payment_method_entry(
        {"id": 1, "slug": "tamara_installment", "name": "TamaraInstallment"}
    ) == {
        "code": "tamara_installment",
        "label": "TamaraInstallment",
        "id": 1,
        "enabled": True,
    }


def test_unknown_payments_never_fabricate_cod() -> None:
    profile = _profile(payments_status=STATUS_UNKNOWN, payment_items=[])
    # Simulate old buggy default being absent
    profile["payment_methods"] = []
    assert payment_codes(profile) == []
    assert_no_fabricated_cod(profile)

    forbidden = _profile(payments_status=STATUS_FORBIDDEN, payment_items=[])
    assert payment_codes(forbidden) == []
    assert_no_fabricated_cod(forbidden)


def test_known_empty_payments_is_not_unknown() -> None:
    profile = _profile(payments_status=STATUS_EMPTY, payment_items=[])
    proj = project_merchant_capabilities(profile)
    assert proj.payments_status == STATUS_EMPTY
    assert proj.payment_methods == []


def test_project_merchant_capabilities_compact_no_zone_fees() -> None:
    profile = _profile(
        payments_status=STATUS_KNOWN,
        payment_items=[
            {"code": "mada", "label": "mada", "id": 1, "enabled": True},
            {"code": "apple_pay", "label": "ApplePay", "id": 2, "enabled": True},
        ],
        companies=[
            {"id": 10, "name": "SMSA", "slug": "smsa", "active": True, "enabled": True},
        ],
        zones=[{"id": 99, "name": "الرياض"}],
    )
    # Contaminate with fee fields that must never be projected from cache list
    profile["shipping"]["zones"]["items"][0]["fees"] = {"amount": "15.00"}
    proj = project_merchant_capabilities(profile)
    assert [m["code"] for m in proj.payment_methods] == ["mada", "apple_pay"]
    assert [c["name"] for c in proj.shipping_companies] == ["SMSA"]
    assert proj.shipping_zones == [{"id": 99, "name": "الرياض"}]
    assert "fees" not in proj.shipping_zones[0]


def test_city_zone_match_returns_ids_for_on_demand_detail() -> None:
    profile = _profile(
        payments_status=STATUS_KNOWN,
        payment_items=[{"code": "cod", "label": "COD", "id": 3, "enabled": True}],
        zones=[
            {"id": 1, "name": "الرياض"},
            {"id": 2, "name": "جدة"},
        ],
    )
    assert find_zone_ids_for_city(profile, "الرياض") == [1]
    assert find_zone_ids_for_city(profile, "الدمام") == []


def test_merge_checkout_profile_preserves_other_config_keys() -> None:
    merged = merge_checkout_profile_into_config(
        {"api_key": "secret", "refresh_token": "r", "other": 1},
        {"payment_methods": ["mada"]},
    )
    assert merged["api_key"] == "secret"
    assert merged["refresh_token"] == "r"
    assert merged["other"] == 1
    assert merged["checkout_profile"]["payment_methods"] == ["mada"]


def test_dual_tenant_capability_isolation() -> None:
    profile_a = _profile(
        payments_status=STATUS_KNOWN,
        payment_items=[
            {"code": "mada", "label": "mada", "id": 1, "enabled": True},
            {"code": "apple_pay", "label": "ApplePay", "id": 2, "enabled": True},
        ],
        companies=[
            {"id": 101, "name": "SMSA", "slug": "smsa", "active": True, "enabled": True},
        ],
    )
    profile_b = _profile(
        payments_status=STATUS_KNOWN,
        payment_items=[
            {"code": "cod", "label": "COD", "id": 3, "enabled": True},
            {"code": "tabby_installment", "label": "Tabby", "id": 4, "enabled": True},
        ],
        companies=[
            {
                "id": 101,  # overlapping external id across tenants
                "name": "SPL",
                "slug": "sbl",
                "active": True,
                "enabled": True,
            },
        ],
    )

    assert payment_codes(profile_a) == ["mada", "apple_pay"]
    assert payment_codes(profile_b) == ["cod", "tabby_installment"]
    assert shipping_company_names(profile_a) == ["SMSA"]
    assert shipping_company_names(profile_b) == ["SPL"]

    # Same company id, different tenant truth — projections stay separate.
    assert project_merchant_capabilities(profile_a).shipping_companies[0]["id"] == 101
    assert project_merchant_capabilities(profile_b).shipping_companies[0]["id"] == 101
    assert project_merchant_capabilities(profile_a).shipping_companies[0]["name"] == "SMSA"
    assert project_merchant_capabilities(profile_b).shipping_companies[0]["name"] == "SPL"


def test_trusted_context_loader_is_tenant_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = {
        11: _profile(
            payments_status=STATUS_KNOWN,
            payment_items=[
                {"code": "mada", "label": "mada", "id": 1, "enabled": True},
            ],
            companies=[
                {
                    "id": 7,
                    "name": "SMSA",
                    "slug": "smsa",
                    "active": True,
                    "enabled": True,
                },
            ],
        ),
        22: _profile(
            payments_status=STATUS_KNOWN,
            payment_items=[
                {"code": "cod", "label": "COD", "id": 2, "enabled": True},
            ],
            companies=[
                {
                    "id": 7,
                    "name": "SPL",
                    "slug": "sbl",
                    "active": True,
                    "enabled": True,
                },
            ],
        ),
    }

    def _load(_db: Any, tenant_id: int) -> Optional[Dict[str, Any]]:
        return profiles.get(tenant_id)

    monkeypatch.setattr(
        "core.salla_merchant_capabilities.load_checkout_profile_for_tenant",
        _load,
    )
    # Patch the import path used inside the loader.
    import core.salla_merchant_capabilities as caps_mod

    monkeypatch.setattr(
        caps_mod,
        "load_checkout_profile_for_tenant",
        _load,
    )

    facts_a = _load_merchant_capability_facts(MagicMock(), 11)
    facts_b = _load_merchant_capability_facts(MagicMock(), 22)
    pay_a = next(f.value for f in facts_a if f.key == "payments")
    pay_b = next(f.value for f in facts_b if f.key == "payments")
    ship_a = next(f.value for f in facts_a if f.key == "shipping")
    ship_b = next(f.value for f in facts_b if f.key == "shipping")
    assert [m["code"] for m in pay_a["methods"]] == ["mada"]
    assert [m["code"] for m in pay_b["methods"]] == ["cod"]
    assert [c["name"] for c in ship_a["companies"]] == ["SMSA"]
    assert [c["name"] for c in ship_b["companies"]] == ["SPL"]


def test_brain_projection_includes_merchant_capabilities() -> None:
    facts = [
        TrustedFact(
            domain=TrustedDomain.MERCHANT_CAPABILITIES,
            key="payments",
            value={
                "status": STATUS_KNOWN,
                "methods": [{"code": "mada", "label": "mada", "enabled": True}],
            },
            source=TruthSource.INTEGRATION_CONFIG,
            path="checkout_profile.payments",
        ),
        TrustedFact(
            domain=TrustedDomain.MERCHANT_CAPABILITIES,
            key="shipping",
            value={
                "companies_status": STATUS_KNOWN,
                "companies": [{"id": 1, "name": "SMSA", "enabled": True}],
                "zones_status": STATUS_EMPTY,
                "zones": [],
            },
            source=TruthSource.INTEGRATION_CONFIG,
            path="checkout_profile.shipping",
        ),
        TrustedFact(
            domain=TrustedDomain.MERCHANT_CAPABILITIES,
            key="kind",
            value="merchant_enabled",
            source=TruthSource.INTEGRATION_CONFIG,
            path="checkout_profile.kind",
        ),
    ]
    snapshot = TrustedContextSnapshot(
        tenant_id=11,
        customer_phone="966500000001",
        conversation_id=1,
        facts=facts,
        loaded_domains=[TrustedDomain.MERCHANT_CAPABILITIES.value],
    )
    projection = project_trusted_context_brain_facts(
        snapshot=snapshot,
        tenant_id=11,
        customer_phone="966500000001",
        conversation_id=1,
    )
    assert projection["merchant_capabilities"]["payments"]["methods"][0]["code"] == "mada"
    assert projection["merchant_capabilities"]["shipping"]["companies"][0]["name"] == "SMSA"


def test_sync_store_checkout_profile_uses_official_payment_endpoint() -> None:
    import asyncio
    import httpx
    from store_adapters import salla_adapter as sa

    calls: List[tuple] = []

    def _http_error(code: int) -> httpx.HTTPStatusError:
        req = httpx.Request("GET", "https://api.salla.dev/admin/v2/x")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError(f"{code}", request=req, response=resp)

    class _Adapter(sa.SallaAdapter):
        def __init__(self) -> None:
            self.api_key = "t"
            self.store_id = "1"
            self.refresh_token = ""
            self._tenant_id = 55
            self._integration_id = 9

        async def _get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
            calls.append((path, params or {}))
            if path == "/payment/methods":
                return {
                    "data": [
                        {"id": 1, "slug": "mada", "name": "mada"},
                        {"id": 2, "slug": "apple_pay", "name": "ApplePay"},
                    ],
                }
            if path == "/shipping/companies/":
                return {
                    "data": [
                        {
                            "id": 10,
                            "name": "SMSA",
                            "slug": "smsa",
                            "activation_type": "manual",
                        },
                    ],
                }
            if path == "/shipping/zones":
                return {"data": [{"id": 3, "name": "الرياض"}]}
            if path in ("/shipping/methods", "/delivery-methods"):
                raise _http_error(404)
            return {"data": []}

    profile = asyncio.run(_Adapter().sync_store_checkout_profile())
    assert ("/payment/methods", {"status": "enabled", "per_page": 60}) in calls
    assert not any(path == "/store/settings" for path, _ in calls)
    assert profile["payments"]["status"] == STATUS_KNOWN
    assert profile["payment_methods"] == ["mada", "apple_pay"]
    assert profile["shipping"]["companies"]["status"] == STATUS_KNOWN
    assert_no_fabricated_cod(profile)


def test_sync_payment_failure_stays_unknown_without_cod() -> None:
    import asyncio
    import httpx
    from store_adapters import salla_adapter as sa

    def _http_error(code: int) -> httpx.HTTPStatusError:
        req = httpx.Request("GET", "https://api.salla.dev/admin/v2/x")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError(f"{code}", request=req, response=resp)

    class _Adapter(sa.SallaAdapter):
        def __init__(self) -> None:
            self.api_key = "t"
            self.store_id = "1"
            self.refresh_token = ""
            self._tenant_id = 77
            self._integration_id = 8

        async def _get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
            if path == "/payment/methods":
                raise _http_error(403)
            if path == "/shipping/companies/":
                return {"data": []}
            if path == "/shipping/zones":
                return {"data": []}
            if path in ("/shipping/methods", "/delivery-methods"):
                raise _http_error(404)
            return {"data": []}

    profile = asyncio.run(_Adapter().sync_store_checkout_profile())
    assert profile["payments"]["status"] == STATUS_FORBIDDEN
    assert profile["payment_methods"] == []
    assert_no_fabricated_cod(profile)
