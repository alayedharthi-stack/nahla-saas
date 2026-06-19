"""P0 regression: central customer name adoption guard."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_CUSTOMER_MESSAGE,
    STATUS_CUSTOMER_ENTERED,
    apply_customer_name,
    read_customer_identity,
)
from core.customer_name_adoption_guard import (  # noqa: E402
    filter_name_for_identity_upsert,
    is_trusted_name_adoption_source,
    is_untrusted_message_name_source,
)
from core.customer_name_extractor import extract_high_confidence_name  # noqa: E402
from core.customer_name_validator import validate_customer_name  # noqa: E402
from routers.conversations import _get_or_create_customer  # noqa: E402
from services.customer_intelligence import CustomerIntelligenceService  # noqa: E402


ECHO_BODY = "ايه وقف النحلة شغلتنا"


class _CustomerStub:
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
        self._writes: list[str] = []

    def __setattr__(self, key: str, value: Any) -> None:
        if key != "_writes" and hasattr(self, "_writes"):
            self._writes.append(key)
        super().__setattr__(key, value)


class _QueryStub:
    def __init__(self, customer: _CustomerStub | None) -> None:
        self._customer = customer

    def filter(self, *_a, **_k) -> "_QueryStub":
        return self

    def first(self) -> _CustomerStub | None:
        return self._customer


class _StubDB:
    def __init__(self, customer: _CustomerStub | None = None) -> None:
        self._customer = customer

    def query(self, *_a, **_k) -> _QueryStub:
        return _QueryStub(self._customer)

    def add(self, _obj: Any) -> None:
        return None

    def flush(self) -> None:
        return None


def _svc_with_customer(customer: _CustomerStub) -> CustomerIntelligenceService:
    db = _StubDB()
    svc = CustomerIntelligenceService(db, tenant_id=customer.tenant_id)
    return svc


@pytest.mark.parametrize(
    "source",
    [
        "whatsapp_outbound_echo",
        "outbound",
        "campaign",
        "template",
        "automation",
        "ai_reply",
        "merchant_reply",
        "system",
    ],
)
def test_untrusted_sources_blocked_by_guard(source: str) -> None:
    assert is_untrusted_message_name_source(source)
    assert not is_trusted_name_adoption_source(source)
    name, mode = filter_name_for_identity_upsert(
        "عبدالله",
        source,
        explicit_customer_entry=True,
    )
    assert name is None
    assert mode == "blocked"


def test_outbound_direction_blocks_explicit_inbound_source() -> None:
    assert not is_trusted_name_adoption_source(
        "ai_detected_name",
        direction="outbound",
        explicit_customer_entry=True,
    )
    name, mode = filter_name_for_identity_upsert(
        "عبدالله",
        "ai_detected_name",
        direction="outbound",
        explicit_customer_entry=True,
    )
    assert name is None
    assert mode == "blocked"


def test_echo_body_not_validated_as_name() -> None:
    assert validate_customer_name(ECHO_BODY).valid is False
    assert extract_high_confidence_name(ECHO_BODY) is None


def test_outbound_echo_does_not_adopt_name_or_call_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer = _CustomerStub(name=None)
    svc = _svc_with_customer(customer)
    monkeypatch.setattr(svc, "_find_customer_by_external_id", lambda _eid: None)
    monkeypatch.setattr(svc, "find_customer_by_phone", lambda _e164: customer)
    monkeypatch.setattr(svc, "_query_customers", lambda: [customer])
    monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)

    apply_mock = MagicMock(wraps=apply_customer_name)
    monkeypatch.setattr(
        "core.customer_identity_resolver.apply_customer_name",
        apply_mock,
    )

    svc.upsert_customer_identity(
        phone="966536867615",
        name=ECHO_BODY,
        source="whatsapp_outbound_echo",
    )

    assert customer.name is None
    apply_mock.assert_not_called()


def test_get_or_create_customer_echo_does_not_pass_message_as_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer = _CustomerStub(name=None)
    db = _StubDB(customer)
    upsert_mock = MagicMock()
    monkeypatch.setattr(
        CustomerIntelligenceService,
        "upsert_customer_identity",
        upsert_mock,
    )

    _get_or_create_customer(
        db,
        tenant_id=1,
        customer_phone="966536867615",
        customer_name="",
        source="whatsapp_outbound_echo",
    )

    upsert_mock.assert_called_once()
    assert upsert_mock.call_args.kwargs.get("name") in (None, "")


def test_inbound_explicit_self_id_updates_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer = _CustomerStub(name=None)
    svc = _svc_with_customer(customer)
    monkeypatch.setattr(svc, "_find_customer_by_external_id", lambda _eid: None)
    monkeypatch.setattr(svc, "find_customer_by_phone", lambda _e164: customer)
    monkeypatch.setattr(svc, "_query_customers", lambda: [customer])
    monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)

    svc.upsert_customer_identity(
        phone="966536867615",
        name="عبدالله",
        source="ai_detected_name",
    )

    assert customer.name == "عبدالله"
    snap = read_customer_identity(customer)
    assert snap.customer_name_status == STATUS_CUSTOMER_ENTERED
    assert snap.customer_name_source == SOURCE_CUSTOMER_MESSAGE


def test_inbound_with_you_intro_updates_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = extract_high_confidence_name("معك محمد الحارثي")
    assert hit is not None
    assert hit.value == "محمد الحارثي"

    customer = _CustomerStub(name=None)
    svc = _svc_with_customer(customer)
    monkeypatch.setattr(svc, "_find_customer_by_external_id", lambda _eid: None)
    monkeypatch.setattr(svc, "find_customer_by_phone", lambda _e164: customer)
    monkeypatch.setattr(svc, "_query_customers", lambda: [customer])
    monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)

    svc.upsert_customer_identity(
        phone="966536867615",
        name=hit.value,
        source="ai_detected_name",
    )

    assert customer.name == "محمد الحارثي"


@pytest.mark.parametrize(
    "source",
    ["campaign", "merchant_reply", "automation"],
)
def test_non_inbound_sources_do_not_update_name(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    customer = _CustomerStub(name="عبدالله")
    svc = _svc_with_customer(customer)
    monkeypatch.setattr(svc, "_find_customer_by_external_id", lambda _eid: None)
    monkeypatch.setattr(svc, "find_customer_by_phone", lambda _e164: customer)
    monkeypatch.setattr(svc, "_query_customers", lambda: [customer])
    monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)

    svc.upsert_customer_identity(
        phone="966536867615",
        name="محمد",
        source=source,
    )

    assert customer.name == "عبدالله"


def test_complaint_message_not_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer = _CustomerStub(name=None)
    svc = _svc_with_customer(customer)
    monkeypatch.setattr(svc, "_find_customer_by_external_id", lambda _eid: None)
    monkeypatch.setattr(svc, "find_customer_by_phone", lambda _e164: None)
    monkeypatch.setattr(svc, "_query_customers", lambda: [])
    monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)

    svc.upsert_customer_identity(
        phone="966536867615",
        name="انا انضحك علي",
        source="ai_detected_name",
    )

    assert customer.name is None


def test_manual_admin_edit_updates_name() -> None:
    customer = _CustomerStub(name=None)
    apply_customer_name(
        customer,
        "أبو نايف",
        source="manual_admin",
        force_merchant=True,
    )
    assert customer.name == "أبو نايف"


def test_manual_clear_leaves_name_empty() -> None:
    customer = _CustomerStub(
        name="قديم",
        extra_metadata={"manual_name_override": True},
    )
    apply_customer_name(
        customer,
        None,
        source="manual_admin",
        force_merchant=True,
    )
    # Clearing is handled by PATCH endpoint; guard must not resurrect.
    customer.name = None
    meta = dict(customer.extra_metadata or {})
    meta["manual_name_cleared"] = True
    customer.extra_metadata = meta
    assert customer.name is None
