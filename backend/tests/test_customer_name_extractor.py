"""P0 regression: conservative inbound customer-name extraction."""
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
    read_customer_identity,
)
from core.customer_name_extractor import extract_high_confidence_name  # noqa: E402
from core.customer_name_validator import validate_customer_name  # noqa: E402
from services.customer_intelligence import CustomerIntelligenceService  # noqa: E402


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


class _StubDB:
    def add(self, _obj: Any) -> None:
        return None

    def flush(self) -> None:
        return None


def _simulate_webhook_name_adoption(
    svc: CustomerIntelligenceService,
    *,
    phone: str,
    text: str,
) -> bool:
    """Mirror the whatsapp_webhook inbound name-adoption guard."""
    hit = extract_high_confidence_name(text)
    if not hit:
        return False
    if not validate_customer_name(hit.value).valid:
        return False
    svc.upsert_customer_identity(
        phone=phone,
        name=hit.value,
        source="ai_detected_name",
    )
    return True


@pytest.mark.parametrize(
    "message",
    [
        "انا انضحك علي",
        "انا اشتريت من القاهرة 5 كيلو عسل",
        "أنا وصلت",
        "أنا محمد",
    ],
)
def test_complaint_and_bare_ana_messages_not_extracted(message: str) -> None:
    assert extract_high_confidence_name(message) is None


@pytest.mark.parametrize(
    "message, expected",
    [
        ("أنا اسمي عبدالله", "عبدالله"),
        ("معك محمد الحارثي", "محمد الحارثي"),
        ("اسمي فهد", "فهد"),
    ],
)
def test_explicit_self_id_still_extracted(message: str, expected: str) -> None:
    hit = extract_high_confidence_name(message)
    assert hit is not None
    assert hit.value == expected


@pytest.mark.parametrize(
    "message",
    [
        "أنا وصلت",
        "انا وصلت",
        "أنا جاي",
        "انا جايه",
        "أنا هنا",
        "أنا موجود",
        "وصلت",
        "وصلنا",
        "جاي الحين",
        "جايه الحين",
    ],
)
def test_arrival_verbs_are_not_extracted_as_names(message: str) -> None:
    assert extract_high_confidence_name(message) is None


def test_complaint_inbound_does_not_overwrite_existing_customer_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook-style adoption must no-op when extractor returns None."""
    db = _StubDB()
    svc = CustomerIntelligenceService(db, tenant_id=1)
    customer = _CustomerStub(
        name="عبدالله علي",
        extra_metadata={
            "customer_name_source": SOURCE_CUSTOMER_MESSAGE,
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
        },
    )

    monkeypatch.setattr(svc, "_find_customer_by_external_id", lambda _eid: None)
    monkeypatch.setattr(svc, "find_customer_by_phone", lambda _e164: customer)
    monkeypatch.setattr(svc, "_query_customers", lambda: [customer])
    monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)
    monkeypatch.setattr(
        svc,
        "recompute_profile_for_customer",
        lambda *_a, **_k: None,
    )

    upsert_mock = MagicMock(wraps=svc.upsert_customer_identity)
    monkeypatch.setattr(svc, "upsert_customer_identity", upsert_mock)

    adopted = _simulate_webhook_name_adoption(
        svc,
        phone="966536867615",
        text="انا انضحك علي",
    )

    assert adopted is False
    assert customer.name == "عبدالله علي"
    upsert_mock.assert_not_called()
    snap = read_customer_identity(customer)
    assert snap.customer_name == "عبدالله علي"
