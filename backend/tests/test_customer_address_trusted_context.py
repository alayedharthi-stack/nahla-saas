"""Trusted customer-address facts for the Commerce Runtime.

These tests deliberately exercise only the platform projection.  They do not
compose customer-facing wording and do not bind a real merchant/customer: the
runtime must later provide the verified tenant/customer scope before it may use
these facts.
"""
from __future__ import annotations

from typing import Any

from core import customer_address_candidates as addresses


def _row(
    address_id: int,
    *,
    source: str,
    selection_state: str = addresses.SELECTION_STATE_CANDIDATE,
    city: str = "Example City",
    address_line: str = "Example Street 1",
    short_address_code: str = "EXMP1234",
) -> addresses.ResolvedAddress:
    return addresses.ResolvedAddress(
        address_id=address_id,
        components=addresses.AddressComponents(
            city=city,
            district="Example District",
            address_line=address_line,
            country="SA",
            short_address_code=short_address_code,
            maps_url="https://maps.example.test/pin",
        ),
        fingerprint=f"internal-fingerprint-{address_id}",
        source=source,
        selection_state=selection_state,
        selection_source=(
            addresses.SELECTION_SOURCE_ORDER_CONFIRMED_SHIPPING
            if selection_state == addresses.SELECTION_STATE_SELECTED
            else ""
        ),
        selection_operation_ref="internal-operation-ref",
        provenance_known=True,
    )


def _project(monkeypatch: Any, resolution: addresses.AddressResolution) -> dict:
    seen: list[dict] = []

    def resolve(db: Any, **scope: Any) -> addresses.AddressResolution:
        seen.append({"db": db, **scope})
        return resolution

    monkeypatch.setattr(addresses, "resolve_customer_address_selection", resolve)
    result = addresses.customer_address_facts_for_trusted_context(
        object(), tenant_id=71, customer_id=811,
    )
    assert seen and seen[0]["tenant_id"] == 71 and seen[0]["customer_id"] == 811
    return result


def test_selected_and_prior_order_facts_stay_distinct_from_a_saved_candidate(monkeypatch):
    imported = _row(10, source=addresses.SOURCE_SALLA_CUSTOMER_PROFILE)
    confirmed = _row(
        20,
        source=addresses.SOURCE_ORDER_CONFIRMED_SHIPPING,
        selection_state=addresses.SELECTION_STATE_SELECTED,
        city="Confirmed City",
        address_line="Confirmed Street 8",
        short_address_code="CNFR5678",
    )
    facts = _project(monkeypatch, addresses.AddressResolution(
        reason=addresses.REASON_SELECTED_ADDRESS,
        selected=confirmed,
        candidates=(imported,),
        addresses=(imported, confirmed),
    ))

    assert facts["address_read_status"] == addresses.ADDRESS_READ_AVAILABLE
    assert facts["address_resolution"] == addresses.REASON_SELECTED_ADDRESS
    assert facts["requires_explicit_selection"] is False
    assert facts["selected_delivery_address"]["address"]["city"] == "Confirmed City"
    assert facts["selected_delivery_address"]["selection_state"] == "selected"
    assert [row["source"] for row in facts["saved_addresses"]] == [
        addresses.SOURCE_ORDER_CONFIRMED_SHIPPING,
        addresses.SOURCE_SALLA_CUSTOMER_PROFILE,
    ]
    assert facts["prior_order_addresses"] == [facts["selected_delivery_address"]]

    # No internal selector can leak from the platform projection into model context.
    for row in facts["saved_addresses"]:
        assert "address_id" not in row
        assert "fingerprint" not in row
        assert "selection_operation_ref" not in row
        assert "lat" not in row["address"] and "lng" not in row["address"]


def test_single_candidate_is_never_reported_as_a_selected_delivery_address(monkeypatch):
    candidate = _row(10, source=addresses.SOURCE_SALLA_CUSTOMER_PROFILE)
    facts = _project(monkeypatch, addresses.AddressResolution(
        reason=addresses.REASON_SINGLE_CANDIDATE,
        candidates=(candidate,),
        addresses=(candidate,),
    ))

    assert facts["selected_delivery_address"] is None
    assert facts["saved_addresses"][0]["selection_state"] == "candidate"
    assert facts["requires_explicit_selection"] is False
    assert facts["prior_order_addresses"] == []


def test_multiple_candidates_carry_the_selection_requirement_without_a_default(monkeypatch):
    first = _row(10, source=addresses.SOURCE_SALLA_CUSTOMER_PROFILE)
    second = _row(20, source=addresses.SOURCE_SALLA_CUSTOMER_PROFILE, city="Other City")
    facts = _project(monkeypatch, addresses.AddressResolution(
        reason=addresses.REASON_MULTIPLE_CANDIDATES,
        candidates=(first, second),
        addresses=(first, second),
    ))

    assert facts["address_resolution"] == addresses.REASON_MULTIPLE_CANDIDATES
    assert facts["requires_explicit_selection"] is True
    assert facts["selected_delivery_address"] is None
    assert len(facts["saved_addresses"]) == 2


def test_no_address_is_an_available_empty_read_not_a_reader_failure(monkeypatch):
    facts = _project(monkeypatch, addresses.AddressResolution(reason=addresses.REASON_NO_ADDRESS))

    assert facts["address_read_status"] == addresses.ADDRESS_READ_AVAILABLE
    assert facts["address_read_reason"] == addresses.ADDRESS_READ_REASON_OK
    assert facts["address_resolution"] == addresses.REASON_NO_ADDRESS
    assert facts["saved_addresses"] == []
    assert facts["selected_delivery_address"] is None


def test_reader_failure_is_not_misreported_as_no_address(monkeypatch):
    def broken(*_args: Any, **_kwargs: Any) -> addresses.AddressResolution:
        raise RuntimeError("database is unavailable")

    monkeypatch.setattr(addresses, "resolve_customer_address_selection", broken)
    facts = addresses.customer_address_facts_for_trusted_context(
        object(), tenant_id=71, customer_id=811,
    )

    assert facts["address_read_status"] == addresses.ADDRESS_READ_UNAVAILABLE
    assert facts["address_read_reason"] == addresses.ADDRESS_READ_REASON_RESOLVER_UNAVAILABLE
    assert facts["address_resolution"] is None
    assert facts["saved_addresses"] == []


def test_missing_customer_binding_never_triggers_an_unscoped_address_read(monkeypatch):
    called = False

    def should_not_run(*_args: Any, **_kwargs: Any) -> addresses.AddressResolution:
        nonlocal called
        called = True
        raise AssertionError("unscoped resolver call")

    monkeypatch.setattr(addresses, "resolve_customer_address_selection", should_not_run)
    facts = addresses.customer_address_facts_for_trusted_context(
        object(), tenant_id=71, customer_id=None,
    )

    assert called is False
    assert facts["address_read_status"] == addresses.ADDRESS_READ_UNAVAILABLE
    assert facts["address_read_reason"] == addresses.ADDRESS_READ_REASON_CUSTOMER_NOT_BOUND
    assert facts["address_resolution"] is None
