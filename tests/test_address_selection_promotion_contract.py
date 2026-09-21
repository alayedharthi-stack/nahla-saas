"""The explicit-selection contract for promoting an address into an order.

A stored address is not a chosen one. Only an address the customer
EXPLICITLY selected may be promoted into the shipping snapshot; an
imported or order-derived candidate stays unaccepted, because promoting it
would put an address into the order on the strength of a phrase with no
recorded decision behind it (AGENTS.md Claim Rule).

These live under ``tests/`` deliberately. ``pytest.ini`` pins
``testpaths = tests``, so this is the tree CI's unit-test job collects;
the same contract asserted only under ``backend/tests`` was checked by no
job at all.

Platform-wide and merchant-agnostic: a generic merchant and a neutral
customer, asserting persisted state rather than reply wording.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_MERCHANT,
    STATUS_CUSTOMER_ENTERED,
)
from core.order_context_builder import build_order_context  # noqa: E402
from core.order_context_prefill import (  # noqa: E402
    apply_order_prep_prefill_patch,
    build_order_prep_prefill_patch,
)
from models import (  # noqa: E402
    Base,
    Customer,
    CustomerAddress,
    CustomerAddressProvenance,
    Tenant,
)

_PHONE = "+966500000123"


def _db() -> Any:
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)()


def _tenant(db: Any) -> Any:
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _customer(db: Any, tenant_id: int) -> Any:
    customer = Customer(
        tenant_id=tenant_id,
        phone=_PHONE,
        normalized_phone=_PHONE.lstrip("+"),
        name="نورة عبدالله",
        extra_metadata={
            "customer_name_source": SOURCE_MERCHANT,
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
            "customer_name_confidence": 0.95,
        },
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _address(db: Any, tenant_id: int, customer_id: int) -> Any:
    address = CustomerAddress(
        tenant_id=tenant_id,
        customer_id=customer_id,
        city="الرياض",
        saudi_national_address="RRRD1234",
    )
    db.add(address)
    db.commit()
    db.refresh(address)
    return address


def _record_selection(db: Any, *, tenant_id: int, customer_id: int, address_id: int) -> None:
    """Record a real selection, as the platform records one."""
    from core.customer_address_candidates import (  # noqa: PLC0415
        resolve_customer_address_selection,
    )

    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant_id, customer_id=customer_id
    )
    fingerprint = next(
        a.fingerprint for a in resolution.addresses if a.address_id == address_id
    )
    now = datetime.now(timezone.utc)
    db.add(
        CustomerAddressProvenance(
            tenant_id=tenant_id,
            customer_id=customer_id,
            customer_address_id=address_id,
            source="customer_confirmed",
            content_fingerprint=fingerprint,
            source_observed_at=now,
            selection_state="selected",
            selected_fingerprint=fingerprint,
            selected_at=now,
            selection_source="customer_confirmed",
            selection_operation_ref="offer-under-test:",
        )
    )
    db.commit()


def _patch_for(db: Any, tenant: Any, customer: Any) -> dict:
    prep = {"customer_confirmed_previous_address": True}
    ctx = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone=_PHONE,
        brain_state={"order_prep": prep},
        message="نفس العنوان السابق",
    )
    patch = build_order_prep_prefill_patch(ctx, prep=prep)
    return {
        "ctx": ctx,
        "merged": apply_order_prep_prefill_patch(prep, patch),
    }


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_CONTEXT_SHIPPING_CONFIRM_ENABLED", "true")
    monkeypatch.setenv("ORDER_CONTEXT_OPERATIONAL_PREFILL_ENABLED", "true")


def test_an_unselected_previous_address_is_not_promoted() -> None:
    """No recorded decision, so nothing reaches the order."""
    db = _db()
    tenant = _tenant(db)
    customer = _customer(db, tenant.id)
    _address(db, tenant.id, customer.id)

    out = _patch_for(db, tenant, customer)
    # Known and offerable — simply not selected.
    assert out["ctx"].known_previous_address is not None
    assert out["ctx"].known_previous_address.explicitly_selected is False
    assert "city" not in out["merged"]
    assert "short_address_code" not in out["merged"]
    assert out["merged"].get("shipping_source") != "customer_confirmed_previous_address"


def test_an_explicitly_selected_previous_address_is_promoted() -> None:
    """A recorded selection does carry into the shipping snapshot."""
    db = _db()
    tenant = _tenant(db)
    customer = _customer(db, tenant.id)
    address = _address(db, tenant.id, customer.id)
    _record_selection(
        db, tenant_id=tenant.id, customer_id=customer.id, address_id=address.id
    )

    out = _patch_for(db, tenant, customer)
    assert out["ctx"].known_previous_address.explicitly_selected is True
    assert out["merged"]["city"] == "الرياض"
    assert out["merged"]["short_address_code"] == "RRRD1234"
    assert out["merged"]["shipping_source"] == "customer_confirmed_previous_address"
    assert out["merged"]["customer_confirmed_previous_address"] is True
