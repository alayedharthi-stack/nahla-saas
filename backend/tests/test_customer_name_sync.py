"""P0 regression: customer name sync + manual_admin override protection."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_MANUAL_ADMIN,
    STATUS_CUSTOMER_ENTERED,
    STATUS_PROPOSED,
    apply_customer_name,
    display_name_for_customer,
    merge_identity_metadata,
    read_customer_identity,
)
from services.customer_intelligence import CustomerIntelligenceService  # noqa: E402
from utils.phone_utils import normalize_to_e164  # noqa: E402


class _CustomerStub:
    """Tracks attribute writes for upsert_customer_identity tests."""

    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id", 615)
        self.tenant_id = kwargs.get("tenant_id", 1)
        self.phone = kwargs.get("phone", "966536867615")
        self.normalized_phone = kwargs.get("normalized_phone", "+966536867615")
        self.name = kwargs.get("name")
        self.email = kwargs.get("email")
        self.extra_metadata: Dict[str, Any] = dict(kwargs.get("extra_metadata") or {})
        self.salla_customer_id = kwargs.get("salla_customer_id")
        self.acquisition_channel = kwargs.get("acquisition_channel", "whatsapp_inbound")
        self.first_seen_at = kwargs.get("first_seen_at")
        self.last_interaction_at = kwargs.get("last_interaction_at")
        self._writes: List[str] = []

    def __setattr__(self, key: str, value: Any) -> None:
        if "_writes" in self.__dict__ and not key.startswith("_"):
            self._writes.append(key)
        object.__setattr__(self, key, value)


class _StubDB:
    def __init__(self) -> None:
        self.added: List[Any] = []
        self.flushed = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushed += 1

    def query(self, *_a: Any, **_k: Any) -> "_StubDB":
        return self


def _customer(**meta: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id=meta.pop("id", 1),
        name=meta.pop("name", None),
        phone=meta.pop("phone", "966536867615"),
        email=meta.pop("email", ""),
        acquisition_channel=meta.pop("acquisition_channel", None),
        extra_metadata=meta or None,
    )


class TestManualAdminOverrideProtection:
    def test_whatsapp_profile_blocked_after_manual_admin(self):
        c = _customer()
        apply_customer_name(c, "Rahaf By Sirdab", source="manual_admin", force_merchant=True)
        assert c.name == "Rahaf By Sirdab"
        assert (c.extra_metadata or {}).get("manual_name_override") is True
        assert read_customer_identity(c).customer_name_source == SOURCE_MANUAL_ADMIN

        blocked = apply_customer_name(c, "Other Profile", source="whatsapp_inbound")
        assert blocked is False
        assert c.name == "Rahaf By Sirdab"
        snap = read_customer_identity(c)
        assert snap.proposed_name != "Other Profile"
        assert snap.customer_name_source == SOURCE_MANUAL_ADMIN

    def test_whatsapp_proposed_blocked_when_manual_override_without_official_name(self):
        """Legacy rows: override set but name still empty — inbound must not mutate."""
        c = _customer(
            manual_name_override=True,
            manual_name_cleared=True,
            proposed_name="Old Proposed",
            customer_name_status=STATUS_PROPOSED,
        )
        blocked = apply_customer_name(c, "Rahaf By Sirdab", source="whatsapp_inbound")
        assert blocked is False
        assert (c.extra_metadata or {}).get("proposed_name") == "Old Proposed"


class TestDisplayNameSync:
    def test_empty_official_name_uses_proposed_for_display(self):
        c = _customer(
            proposed_name="Rahaf By Sirdab",
            customer_name_status=STATUS_PROPOSED,
            customer_name_source="whatsapp_profile",
        )
        assert display_name_for_customer(c) == "Rahaf By Sirdab"

    def test_customers_and_conversations_share_display_name(self):
        from routers.customers import _serialize_customer  # noqa: PLC0415

        c = _customer(
            proposed_name="Rahaf By Sirdab",
            customer_name_status=STATUS_PROPOSED,
            customer_name_source="whatsapp_profile",
        )
        convo_name = display_name_for_customer(c, phone_fallback=c.phone)
        serialized = _serialize_customer(c, profile=None)
        assert serialized["display_name"] == convo_name == "Rahaf By Sirdab"
        assert serialized["name"] == ""


class TestPhoneNormalizationDuplicates:
    @pytest.mark.parametrize(
        "variant",
        ["966536867615", "+966536867615", "0536867615"],
    )
    def test_phone_variants_normalize_to_same_e164(self, variant: str):
        assert normalize_to_e164(variant) == "+966536867615"

    def test_upsert_finds_same_customer_for_phone_variants(self, monkeypatch: pytest.MonkeyPatch):
        db = _StubDB()
        svc = CustomerIntelligenceService(db, tenant_id=1)
        existing = _CustomerStub(
            phone="+966536867615",
            normalized_phone="+966536867615",
            name="Rahaf By Sirdab",
            extra_metadata={
                "manual_name_override": True,
                "customer_name_source": SOURCE_MANUAL_ADMIN,
                "customer_name_status": STATUS_CUSTOMER_ENTERED,
            },
        )
        seen_phones: List[str] = []

        def _find(raw: Any, **_kw: Any):
            seen_phones.append(str(raw))
            return existing

        monkeypatch.setattr(svc, "_find_customer_by_external_id", lambda _eid: None)
        monkeypatch.setattr(svc, "find_customer_by_phone", _find)
        monkeypatch.setattr(svc, "_query_customers", lambda: [])
        monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)
        monkeypatch.setattr(
            svc,
            "recompute_profile_for_customer",
            lambda *_a, **_k: None,
        )

        svc.upsert_lead_customer(
            phone="0536867615",
            name="WhatsApp Alias",
            source="whatsapp_inbound",
        )
        assert existing.name == "Rahaf By Sirdab"
        assert len(db.added) == 0
        assert seen_phones, "expected phone lookup"
        assert normalize_to_e164(seen_phones[0]) == "+966536867615"


class TestUpsertMetadataMerge:
    def test_identity_metadata_survives_inbound_metadata_merge(self):
        customer = _CustomerStub(
            extra_metadata={
                "manual_name_override": True,
                "customer_name_source": SOURCE_MANUAL_ADMIN,
                "customer_name_status": STATUS_CUSTOMER_ENTERED,
                "proposed_name": "Should Stay",
            }
        )
        customer.name = "Merchant Curated"
        inbound = {"source": "whatsapp_inbound", "lead_source": "whatsapp_inbound"}
        merged = merge_identity_metadata(dict(inbound), customer)
        assert merged["manual_name_override"] is True
        assert merged["customer_name_source"] == SOURCE_MANUAL_ADMIN
        assert merged["proposed_name"] == "Should Stay"
        assert merged["source"] == "whatsapp_inbound"

    def test_upsert_lead_customer_preserves_manual_admin_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        db = _StubDB()
        svc = CustomerIntelligenceService(db, tenant_id=1)
        customer = _CustomerStub(
            name="Merchant Curated",
            extra_metadata={
                "manual_name_override": True,
                "manual_name_source": "manual_admin",
                "customer_name_source": SOURCE_MANUAL_ADMIN,
                "customer_name_status": STATUS_CUSTOMER_ENTERED,
            },
        )
        monkeypatch.setattr(svc, "_find_customer_by_external_id", lambda _eid: None)
        monkeypatch.setattr(svc, "find_customer_by_phone", lambda _e164: customer)
        monkeypatch.setattr(svc, "_query_customers", lambda: [])
        monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)
        monkeypatch.setattr(
            svc,
            "recompute_profile_for_customer",
            lambda *_a, **_k: None,
        )

        svc.upsert_lead_customer(
            phone="966536867615",
            name="Rahaf By Sirdab",
            source="whatsapp_inbound",
        )

        assert customer.name == "Merchant Curated"
        meta = customer.extra_metadata or {}
        assert meta.get("manual_name_override") is True
        assert meta.get("customer_name_source") == SOURCE_MANUAL_ADMIN
        assert meta.get("proposed_name") != "Rahaf By Sirdab"
