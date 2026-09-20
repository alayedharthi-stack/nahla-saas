"""Salla profile address candidates — persistence, selection and reuse policy.

Platform-wide behaviour, exercised with a neutral generic merchant and a
synthetic customer (AGENTS.md generic commerce regression policy). Truth is
asserted from persisted state and structured evidence, never from reply
wording.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.customer_address_candidates import (  # noqa: E402
    ACTION_CREATED,
    ACTION_SKIPPED,
    ACTION_UNCHANGED,
    ACTION_UPDATED,
    REASON_MULTIPLE_CANDIDATES,
    REASON_SELECTED_ADDRESS,
    REASON_SINGLE_CANDIDATE,
    SELECTION_SOURCE_CUSTOMER_CONFIRMED,
    SOURCE_SALLA_CUSTOMER_PROFILE,
    AddressComponents,
    address_content_fingerprint,
    components_from_salla_customer_payload,
    is_sufficient_delivery_address,
    missing_address_requirements,
    record_explicit_address_selection,
    resolve_customer_address_selection,
    source_updated_at_from_salla_customer_payload,
    upsert_imported_address_candidate,
)
from core.customer_address_persistence_evidence import (  # noqa: E402
    NO_OPERATION,
    AddressOperation,
    AddressOperationAttempt,
    AddressPersistenceScope,
    no_evidence,
    resolve_customer_address_persistence_evidence,
    state_only_evidence,
)
from core.order_context_builder import build_order_context  # noqa: E402
from models import (  # noqa: E402
    Base,
    Conversation,
    Customer,
    CustomerAddress,
    CustomerAddressProvenance,
    Tenant,
)
from modules.ai.brain.postprocess.customer_address_save_claim_guard import (  # noqa: E402
    CLAIM_KIND_ADOPTED,
    CLAIM_KIND_SAVED,
    apply_customer_address_save_claim_guard,
    detect_address_save_claim_kinds,
)
from modules.ai.order_flow_v2.checkout_context import (  # noqa: E402
    _offered_revisions,
    address_choice_actions,
    address_more_action_id,
    apply_delivery_continuation_address_patch,
    apply_explicit_address_selection,
    apply_previous_address_confirmation,
    apply_structured_address_consent,
    consent_action_id,
    load_checkout_reply_context,
    read_offered_address,
    record_presented_address_offer,
    delivered_address_action_ids,
    structured_consent_action,
)

# Neutral generic commerce fixture — no merchant-specific assumptions.
MERCHANT = "متجر تجريبي عام"
CUSTOMER_PHONE = "+966500000123"
SALLA_CUSTOMER_ID = "SC-77"
CITY = "الرياض"
SHORT_CODE = "RRRD1234"
STREET = "حي النرجس، شارع 10"


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)(), engine


def _seed(db, *, name: str = MERCHANT, phone: str = CUSTOMER_PHONE,
          salla_id: str = SALLA_CUSTOMER_ID) -> Tuple[Tenant, Customer]:
    tenant = Tenant(name=f"{name}-{phone}", is_active=True)
    db.add(tenant)
    db.flush()
    customer = Customer(
        tenant_id=tenant.id,
        phone=phone,
        normalized_phone=phone,
        salla_customer_id=salla_id,
        acquisition_channel="salla_sync",
    )
    db.add(customer)
    db.commit()
    return tenant, customer


def _payload(**overrides) -> dict:
    base = {
        "id": SALLA_CUSTOMER_ID,
        "first_name": "نورة",
        "last_name": "عبدالله",
        "mobile": CUSTOMER_PHONE,
        "city": CITY,
        "country": "SA",
        "location": STREET,
        "updated_at": "2026-09-01T10:00:00Z",
    }
    base.update(overrides)
    return base


def _import(db, tenant, customer, payload) -> Any:
    return upsert_imported_address_candidate(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        components=components_from_salla_customer_payload(payload),
        source=SOURCE_SALLA_CUSTOMER_PROFILE,
        source_ref=str(payload.get("id") or ""),
        source_updated_at=source_updated_at_from_salla_customer_payload(payload),
    )


# ── Source projection & policy ──────────────────────────────────────────

def test_supported_profile_surface_is_location_city_country_only():
    components = components_from_salla_customer_payload(
        _payload(gender="female", email="a@b.c", avatar="https://x/y.png")
    )
    assert components.city == CITY
    assert components.country == "SA"
    assert components.address_line == STREET
    # Nothing else on the profile becomes an address component.
    assert components.short_address_code == ""
    assert components.maps_url == ""
    assert components.lat == "" and components.lng == ""


def test_free_text_location_never_becomes_a_national_short_address(): 
    """``location`` is not a short address and not a postal code."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    result = _import(db, tenant, customer, _payload())
    db.commit()
    row = db.query(CustomerAddress).filter_by(id=result.address_id).one()
    assert row.address_text == STREET
    assert row.saudi_national_address is None


def test_city_alone_is_not_a_complete_delivery_address():
    components = components_from_salla_customer_payload(
        _payload(location=None, city=CITY)
    )
    assert components.city == CITY
    assert is_sufficient_delivery_address(components) is False
    # Only the missing requirement is asked for — the city is not re-asked.
    assert missing_address_requirements(components) == ("delivery_address",)


def test_city_plus_locating_component_is_sufficient():
    assert is_sufficient_delivery_address(
        AddressComponents(city=CITY, short_address_code=SHORT_CODE)
    )
    assert missing_address_requirements(
        AddressComponents(city=CITY, short_address_code=SHORT_CODE)
    ) == ()


def test_short_address_and_postal_code_are_not_conflated_on_round_trip():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    components = AddressComponents(
        city=CITY, district="النرجس", address_line=STREET,
        country="SA", short_address_code=SHORT_CODE,
    )
    result = upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=components, source_ref=SALLA_CUSTOMER_ID,
        source_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    db.commit()
    row = db.query(CustomerAddress).filter_by(id=result.address_id).one()
    assert row.saudi_national_address == SHORT_CODE
    assert row.address_text == STREET
    resolved = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).reusable
    assert resolved.components == components


# ── Import durability & idempotency ─────────────────────────────────────

def test_initial_import_persists_as_candidate_not_as_default():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    result = _import(db, tenant, customer, _payload())
    db.commit()
    assert result.action == ACTION_CREATED

    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    assert resolution.reason == REASON_SINGLE_CANDIDATE
    assert resolution.selected is None
    assert resolution.candidates[0].selected is False


def test_repeated_import_is_idempotent():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    first = _import(db, tenant, customer, _payload())
    db.commit()
    for _ in range(3):
        again = _import(db, tenant, customer, _payload())
        db.commit()
        assert again.action == ACTION_UNCHANGED
        assert again.address_id == first.address_id
    assert db.query(CustomerAddress).count() == 1
    assert db.query(CustomerAddressProvenance).count() == 1


def test_older_event_cannot_overwrite_current_information():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload(updated_at="2026-09-05T10:00:00Z"))
    db.commit()
    stale = _import(
        db, tenant, customer,
        _payload(location="عنوان قديم", updated_at="2026-09-01T10:00:00Z"),
    )
    db.commit()
    assert stale.action == ACTION_SKIPPED
    assert stale.reason == "stale_source_event"
    resolved = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).reusable
    assert resolved.components.address_line == STREET


def test_incomplete_payload_never_clears_known_fields():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload(updated_at="2026-09-01T10:00:00Z"))
    db.commit()
    # Missing, empty and the literal "null" all mean ABSENT, never deletion.
    for missing_value in (None, "", "null", "NULL"):
        _import(
            db, tenant, customer,
            _payload(location=missing_value, city=missing_value,
                     updated_at="2026-09-09T10:00:00Z"),
        )
        db.commit()
        resolved = resolve_customer_address_selection(
            db, tenant_id=tenant.id, customer_id=customer.id,
        ).reusable
        assert resolved.components.city == CITY
        assert resolved.components.address_line == STREET


def test_event_without_provider_revision_only_fills_gaps():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload(updated_at="2026-09-01T10:00:00Z"))
    db.commit()
    # No provider timestamp: not provably newer, so it may not overwrite.
    result = _import(
        db, tenant, customer,
        _payload(location="عنوان مختلف", city="", updated_at=None),
    )
    db.commit()
    assert result.action == ACTION_UNCHANGED
    resolved = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).reusable
    assert resolved.components.address_line == STREET

    prov = db.query(CustomerAddressProvenance).one()
    # A missing provider revision is never replaced by an invented one.
    assert prov.source_updated_at is not None
    assert prov.source_updated_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 9, 1, 10, 0, tzinfo=timezone.utc,
    )


def test_missing_provider_timestamp_is_stored_as_null():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload(updated_at=None))
    db.commit()
    prov = db.query(CustomerAddressProvenance).one()
    assert prov.source_updated_at is None
    assert prov.source_observed_at is not None


def test_empty_payload_writes_nothing_and_deletes_nothing():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    result = _import(
        db, tenant, customer,
        {"id": SALLA_CUSTOMER_ID, "city": None, "country": "null", "location": ""},
    )
    db.commit()
    assert result.action == ACTION_SKIPPED
    assert result.reason == "no_supported_address_components"
    assert db.query(CustomerAddress).count() == 1
    resolved = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).reusable
    assert resolved.components.city == CITY


def test_new_information_updates_the_same_unselected_candidate():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    first = _import(db, tenant, customer, _payload(updated_at="2026-09-01T10:00:00Z"))
    db.commit()
    result = _import(
        db, tenant, customer,
        _payload(location="حي الياسمين، شارع 5", updated_at="2026-09-05T10:00:00Z"),
    )
    db.commit()
    assert result.action == ACTION_UPDATED
    assert result.address_id == first.address_id
    assert db.query(CustomerAddress).count() == 1


# ── Identity binding ────────────────────────────────────────────────────

def test_missing_scope_fails_safe():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    components = components_from_salla_customer_payload(_payload())
    for tid, cid in ((0, customer.id), (tenant.id, None), (None, None)):
        result = upsert_imported_address_candidate(
            db, tenant_id=tid, customer_id=cid, components=components,
        )
        assert result.action == ACTION_SKIPPED
        assert result.reason == "missing_scope"
    assert db.query(CustomerAddress).count() == 0


def test_selection_refuses_another_customers_address():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    other = Customer(tenant_id=tenant.id, phone="+966500000999",
                     normalized_phone="+966500000999", salla_customer_id="SC-99")
    db.add(other)
    db.commit()
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    result = record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=other.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
    )
    assert result.action == ACTION_SKIPPED
    assert result.reason == "customer_mismatch"


def test_selection_refuses_another_tenants_address():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    other_tenant, other_customer = _seed(db, phone="+966500000888", salla_id="SC-88")
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    result = record_explicit_address_selection(
        db, tenant_id=other_tenant.id, customer_id=other_customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
    )
    assert result.action == ACTION_SKIPPED
    assert result.reason == "address_not_found"


def test_resolution_is_tenant_scoped():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    other_tenant, _ = _seed(db, phone="+966500000888", salla_id="SC-88")
    _import(db, tenant, customer, _payload())
    db.commit()
    assert resolve_customer_address_selection(
        db, tenant_id=other_tenant.id, customer_id=customer.id,
    ).reusable is None


# ── Selection, revision binding and multiple candidates ─────────────────

def test_selection_is_bound_to_the_reviewed_revision():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    result = record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint="a-revision-that-no-longer-matches",
    )
    assert result.action == ACTION_SKIPPED
    assert result.reason == "address_revision_changed"
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None


def test_repeated_selection_of_the_same_revision_writes_once():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    first = record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=imported.fingerprint,
    )
    db.commit()
    selected_at = db.query(CustomerAddressProvenance).one().selected_at
    again = record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=imported.fingerprint,
    )
    db.commit()
    assert first.action == ACTION_UPDATED
    assert again.action == ACTION_UNCHANGED
    assert db.query(CustomerAddressProvenance).count() == 1
    assert db.query(CustomerAddressProvenance).one().selected_at == selected_at


def test_refresh_cannot_silently_alter_an_approved_revision():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload(updated_at="2026-09-01T10:00:00Z"))
    db.commit()
    record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=imported.fingerprint,
    )
    db.commit()

    refreshed = _import(
        db, tenant, customer,
        _payload(location="حي الياسمين، شارع 5", updated_at="2026-09-10T10:00:00Z"),
    )
    db.commit()
    assert refreshed.action == ACTION_CREATED
    assert refreshed.reason == "selected_revision_preserved"
    assert refreshed.address_id != imported.address_id

    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    # The approved revision is untouched and still the reusable address.
    assert resolution.selected.address_id == imported.address_id
    assert resolution.selected.components.address_line == STREET
    assert resolution.selected.fingerprint == imported.fingerprint


def test_multiple_candidates_create_no_implicit_default():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city="جدة", short_address_code="JJJD5678"),
        source_ref="SC-OTHER",
        source_updated_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    db.commit()

    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    assert resolution.reason == REASON_MULTIPLE_CANDIDATES
    assert resolution.requires_explicit_selection is True
    # No highest-row-id / latest-import default.
    assert resolution.reusable is None
    assert len(resolution.candidates) == 2


def test_explicit_selection_resolves_ambiguity():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    first = _import(db, tenant, customer, _payload())
    db.commit()
    upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city="جدة", short_address_code="JJJD5678"),
        source_ref="SC-OTHER",
        source_updated_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    db.commit()
    record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=first.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=first.fingerprint,
    )
    db.commit()
    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    assert resolution.reason == REASON_SELECTED_ADDRESS
    assert resolution.selected.address_id == first.address_id
    assert resolution.requires_explicit_selection is False


def test_legacy_confirmed_shipping_row_without_provenance_stays_reusable():
    """Rows that predate this slice keep main's reuse behaviour."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    db.add(CustomerAddress(
        tenant_id=tenant.id, customer_id=customer.id, city=CITY,
        saudi_national_address=SHORT_CODE, address_type="confirmed_shipping",
    ))
    db.commit()
    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    assert resolution.reason == REASON_SELECTED_ADDRESS
    assert resolution.selected.legacy is True
    assert resolution.selected.selected is True


# ── Persistence evidence ────────────────────────────────────────────────

def test_state_only_storage_is_not_durable_save_evidence():
    evidence = state_only_evidence("order_prep_only")
    assert evidence.scope is AddressPersistenceScope.CONVERSATION_STATE
    assert evidence.committed is False
    assert evidence.allows_saved_address_claim() is False
    assert evidence.allows_adopted_address_claim() is False


def test_skipped_writer_and_unavailable_capability_yield_no_evidence():
    for reason in ("draft_bridge_disabled", "capability_unavailable",
                   "ambiguous_customer_identity"):
        evidence = no_evidence(reason)
        assert evidence.scope is AddressPersistenceScope.NONE
        assert evidence.allows_saved_address_claim() is False
        assert evidence.allows_adopted_address_claim() is False


OPERATION_REF = "op-test-1"


def _save_attempt(tenant, customer, result, operation=AddressOperation.SAVE_CANDIDATE,
                  operation_ref=OPERATION_REF):
    return AddressOperationAttempt(
        operation=operation, tenant_id=tenant.id, customer_id=customer.id,
        address_id=result.address_id, fingerprint=result.fingerprint,
        operation_ref=operation_ref,
    )


def test_no_operation_in_the_turn_supports_no_save_claim():
    """An address that was already on file proves nothing about a new save."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id, attempt=NO_OPERATION,
    )
    assert evidence.scope is AddressPersistenceScope.NONE
    assert evidence.reason == "no_address_operation_in_turn"
    assert evidence.allows_saved_address_claim() is False


def test_operation_for_another_customer_or_tenant_is_refused():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    attempt = AddressOperationAttempt(
        operation=AddressOperation.SAVE_CANDIDATE, tenant_id=tenant.id,
        customer_id=customer.id + 999, address_id=imported.address_id,
        fingerprint=imported.fingerprint, operation_ref=OPERATION_REF,
    )
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id, attempt=attempt,
    )
    assert evidence.scope is AddressPersistenceScope.NONE
    assert evidence.reason == "operation_scope_mismatch"


def test_operation_on_an_unknown_address_supports_nothing():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    attempt = AddressOperationAttempt(
        operation=AddressOperation.SAVE_CANDIDATE, tenant_id=tenant.id,
        customer_id=customer.id, address_id=999_999, fingerprint="whatever",
        operation_ref=OPERATION_REF,
    )
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id, attempt=attempt,
    )
    assert evidence.scope is AddressPersistenceScope.NONE
    assert evidence.allows_saved_address_claim() is False


def test_mismatched_revision_supports_nothing():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    attempt = AddressOperationAttempt(
        operation=AddressOperation.SAVE_CANDIDATE, tenant_id=tenant.id,
        customer_id=customer.id, address_id=imported.address_id,
        fingerprint="a-revision-that-was-never-committed",
        operation_ref=OPERATION_REF,
    )
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id, attempt=attempt,
    )
    assert evidence.scope is AddressPersistenceScope.NONE
    assert evidence.reason == "committed_revision_mismatch"


def test_committed_candidate_supports_saved_but_not_adopted_claim():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id,
        attempt=_save_attempt(tenant, customer, imported),
    )
    assert evidence.scope is AddressPersistenceScope.IMPORTED_CANDIDATE
    assert evidence.allows_saved_address_claim() is True
    assert evidence.allows_adopted_address_claim() is False


def test_adoption_that_did_not_commit_cannot_claim_adoption():
    """The row is there, but it is not the selected delivery address."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id,
        attempt=_save_attempt(tenant, customer, imported,
                              operation=AddressOperation.ADOPT_SELECTION),
    )
    assert evidence.scope is AddressPersistenceScope.IMPORTED_CANDIDATE
    assert evidence.reason == "adoption_not_committed"
    assert evidence.allows_adopted_address_claim() is False
    assert evidence.allows_saved_address_claim() is False


def test_committed_selection_supports_adopted_claim():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=imported.fingerprint,
        operation_ref=OPERATION_REF,
    )
    db.commit()
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id,
        attempt=_save_attempt(tenant, customer, imported,
                              operation=AddressOperation.ADOPT_SELECTION),
    )
    assert evidence.scope is AddressPersistenceScope.SELECTED_DELIVERY_ADDRESS
    assert evidence.allows_adopted_address_claim() is True


# ── Response validation ─────────────────────────────────────────────────

SAVE_CLAIM = "تم حفظ عنوانك عندنا. كم كمية الطلب؟"
ADOPT_CLAIM = "اعتمدنا الرياض كعنوانك الافتراضي. نكمل الطلب؟"


@pytest.mark.parametrize("evidence", [
    None,
    no_evidence("draft_bridge_disabled"),
    state_only_evidence("order_prep_only"),
])
def test_save_claim_is_removed_without_committed_evidence(evidence):
    result = apply_customer_address_save_claim_guard(
        reply=SAVE_CLAIM, evidence=evidence,
    )
    assert result.action == "blocked_unsupported_address_save_claim"
    assert "تم حفظ عنوانك" not in result.reply
    # Only the unsupported claim is removed; the rest of the reply stands.
    assert "كم كمية الطلب؟" in result.reply


def test_adopted_claim_is_removed_when_only_a_candidate_is_committed():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    result = apply_customer_address_save_claim_guard(
        reply=ADOPT_CLAIM, evidence=evidence,
    )
    assert result.action == "blocked_unsupported_address_save_claim"
    assert "الافتراضي" not in result.reply


def test_committed_selection_lets_the_claim_through_unchanged():
    """A legitimate, committed adoption is still permitted."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=imported.fingerprint,
        operation_ref=OPERATION_REF,
    )
    db.commit()
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id,
        attempt=_save_attempt(tenant, customer, imported,
                              operation=AddressOperation.ADOPT_SELECTION),
    )
    result = apply_customer_address_save_claim_guard(
        reply=ADOPT_CLAIM, evidence=evidence,
    )
    assert result.action == "allowed"
    assert result.reply == ADOPT_CLAIM


def test_an_old_address_never_authorizes_a_claim_about_a_new_one():
    """Riyadh on file does not make "saved your new Jeddah address" true."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=imported.fingerprint,
    )
    db.commit()
    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id, attempt=NO_OPERATION,
    )
    result = apply_customer_address_save_claim_guard(
        reply="تم حفظ عنوانك الجديد في جدة واعتماده كعنوان افتراضي.",
        evidence=evidence,
    )
    assert result.action == "blocked_unsupported_address_save_claim"
    assert "جدة" not in result.reply


def test_truthful_negative_is_never_deleted():
    """"Your address was NOT saved" is honest text, not a false claim."""
    for reply in (
        "لم يتم حفظ عنوانك بعد.",
        "ما حفظنا عنوانك. ارسل لي الرمز المختصر.",
        "عنوانك غير محفوظ عندنا حالياً.",
    ):
        result = apply_customer_address_save_claim_guard(reply=reply, evidence=None)
        assert result.action == "allowed", reply
        assert result.reply == reply


def test_a_question_is_never_treated_as_a_completed_action():
    for reply in (
        "هل تريد اعتماد عنوانك كعنوان افتراضي؟",
        "هل نحفظ عنوانك للطلبات الجاية؟",
    ):
        result = apply_customer_address_save_claim_guard(reply=reply, evidence=None)
        assert result.action == "allowed", reply
        assert result.reply == reply


def test_combined_save_and_adoption_claim_is_detected_as_both():
    """The adoption rides an attached pronoun rather than repeating the noun."""
    reply = "تم حفظ عنوانك واعتماده للتوصيل يا تركي"
    kinds = detect_address_save_claim_kinds(reply)
    assert CLAIM_KIND_SAVED in kinds
    assert CLAIM_KIND_ADOPTED in kinds


@pytest.mark.parametrize("reply", [
    "عنوانك محفوظ عندنا",
    "عنوانك مسجل لدينا",
    "اعتمدنا عنوانك الافتراضي",
    "سجلنا لك العنوان",
])
def test_unseen_phrasings_of_the_same_two_claims_are_detected(reply):
    assert detect_address_save_claim_kinds(reply)
    result = apply_customer_address_save_claim_guard(reply=reply, evidence=None)
    assert result.action == "blocked_unsupported_address_save_claim"


def test_only_the_unsupported_sentence_is_removed():
    reply = "تم حفظ عنوانك عندنا. وش الكمية اللي تبيها؟"
    result = apply_customer_address_save_claim_guard(reply=reply, evidence=None)
    assert result.action == "blocked_unsupported_address_save_claim"
    assert "تم حفظ عنوانك" not in result.reply
    assert "وش الكمية اللي تبيها؟" in result.reply
    assert result.scrubbed_empty is False


def test_full_removal_is_reported_so_the_send_can_be_suppressed():
    """Neither the false claim nor an empty message may be delivered."""
    reply = "تم حفظ عنوانك واعتماده للتوصيل يا تركي"
    result = apply_customer_address_save_claim_guard(reply=reply, evidence=None)
    assert result.action == "blocked_unsupported_address_save_claim"
    assert result.scrubbed_empty is True
    assert result.reply.strip() == ""


def test_pipeline_recovers_truthfully_when_nothing_is_left_to_send():
    """Whole removal must not become silence — a guard corrects, never mutes."""
    from core.fallback_policy import (  # noqa: PLC0415
        is_compose_failure_fallback,
    )
    from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: PLC0415
        run_post_compose_truth_guards,
    )

    db, _ = _make_db()
    tenant, customer = _seed(db)
    convo = _conversation(db, tenant, customer)
    unsupported = "تم حفظ عنوانك واعتماده للتوصيل يا تركي"
    result = run_post_compose_truth_guards(
        db=db, tenant_id=tenant.id, to=CUSTOMER_PHONE, text="وين توصلون؟",
        reply=unsupported, convo=convo,
        inbound_metadata={}, brain_handoff=False, brain_nc_block=False,
        brain_nc_category="", br_action="", brain_persona_compose_event=None,
        mode="primary", conversation_id=convo.id,
    )
    event = next(
        e for e in result.events if e.guard == "customer_address_save_claim_guard"
    )
    assert event.modified is True
    assert "scrubbed_empty" in (event.reason or "")
    # Something truthful is still delivered…
    assert result.reply.strip()
    assert is_compose_failure_fallback(result.reply)
    assert event.suppressed_send is False
    # …and the removed claim is not restored anywhere in it.
    assert "تم حفظ عنوانك" not in result.reply
    assert "اعتماده" not in result.reply


def test_pipeline_lets_a_committed_acknowledgement_through():
    """The positive runtime case: a real, committed selection may be stated.

    This goes through the shared pipeline, not the guard helper, because
    that is where the evidence used to be lost.
    """
    from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: PLC0415
        run_post_compose_truth_guards,
    )

    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    consent = _consent_meta(showing, imported.address_id, turn_ref="wamid.in-ack")
    patch = apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=consent,
    )
    db.commit()
    assert patch.get("customer_confirmed_previous_address") is True

    reply = "تم حفظ عنوانك واعتماده للتوصيل."
    result = run_post_compose_truth_guards(
        db=db, tenant_id=tenant.id, to=CUSTOMER_PHONE, text="نعم",
        reply=reply, convo=convo, inbound_metadata=consent,
        brain_handoff=False, brain_nc_block=False, brain_nc_category="",
        br_action="", brain_persona_compose_event=None, mode="primary",
        conversation_id=convo.id,
    )
    assert result.reply == reply
    event = next(
        e for e in result.events if e.guard == "customer_address_save_claim_guard"
    )
    assert event.modified is False


def test_pipeline_refuses_an_acknowledgement_from_an_earlier_turn():
    """Last turn's operation is not this turn's; the claim goes."""
    from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: PLC0415
        run_post_compose_truth_guards,
    )

    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))
    apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(showing, imported.address_id, turn_ref="wamid.in-1"),
    )
    db.commit()

    result = run_post_compose_truth_guards(
        db=db, tenant_id=tenant.id, to=CUSTOMER_PHONE, text="كم السعر؟",
        reply="تم حفظ عنوانك واعتماده للتوصيل. كم كمية الطلب؟", convo=convo,
        inbound_metadata={"wa_message_id": "wamid.in-2"},
        brain_handoff=False, brain_nc_block=False, brain_nc_category="",
        br_action="", brain_persona_compose_event=None, mode="primary",
        conversation_id=convo.id,
    )
    assert "تم حفظ عنوانك" not in result.reply
    assert "كم كمية الطلب؟" in result.reply


def test_pipeline_leaves_a_truthful_negative_intact():
    from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: PLC0415
        run_post_compose_truth_guards,
    )

    db, _ = _make_db()
    tenant, customer = _seed(db)
    convo = _conversation(db, tenant, customer)
    reply = "لم يتم حفظ عنوانك"
    result = run_post_compose_truth_guards(
        db=db, tenant_id=tenant.id, to=CUSTOMER_PHONE, text="هل حفظتم عنواني؟",
        reply=reply, convo=convo, inbound_metadata={}, brain_handoff=False,
        brain_nc_block=False, brain_nc_category="", br_action="",
        brain_persona_compose_event=None, mode="primary", conversation_id=convo.id,
    )
    assert result.reply == reply


def test_guard_leaves_replies_without_save_claims_alone():
    reply = "وش المدينة اللي توصل لها الطلب؟"
    result = apply_customer_address_save_claim_guard(reply=reply, evidence=None)
    assert result.action == "allowed"
    assert result.reply == reply


# ── Agent context & checkout reuse ──────────────────────────────────────

def _conversation(db, tenant, customer) -> Conversation:
    convo = Conversation(
        tenant_id=tenant.id, status="open", customer_id=customer.id,
        extra_metadata={},
    )
    db.add(convo)
    db.commit()
    return convo


def _select(db, tenant, customer, imported) -> None:
    record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=imported.fingerprint,
    )
    db.commit()


def test_selected_address_reaches_agent_context_in_a_new_conversation():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(
            city=CITY, address_line=STREET, short_address_code=SHORT_CODE,
        ),
        source_ref=SALLA_CUSTOMER_ID,
        source_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    db.commit()
    _select(db, tenant, customer, imported)

    # A brand-new conversation with empty state — nothing carried over.
    fresh = _conversation(db, tenant, customer)
    ctx = build_order_context(
        db, tenant_id=tenant.id, conversation=fresh, phone=CUSTOMER_PHONE,
        brain_state={"order_prep": {}}, build_source="test_reset",
    )
    assert ctx.known_previous_address is not None
    assert ctx.known_previous_address.explicitly_selected is True
    assert ctx.known_previous_address.city == CITY
    assert ctx.known_previous_address.short_address == SHORT_CODE
    assert ctx.known_previous_address.sufficient is True
    assert ctx.known_address_candidates


def test_selected_address_reaches_checkout_after_reset():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city=CITY, short_address_code=SHORT_CODE),
        source_ref=SALLA_CUSTOMER_ID,
        source_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    db.commit()
    _select(db, tenant, customer, imported)

    fresh = _conversation(db, tenant, customer)
    reply_ctx = load_checkout_reply_context(
        db, tenant_id=tenant.id, conversation=fresh,
        customer_phone=CUSTOMER_PHONE, order_prep={}, brain_state={},
    )
    assert reply_ctx.known_previous["city"] == CITY
    assert reply_ctx.known_previous["short_address"] == SHORT_CODE
    assert reply_ctx.known_previous["selection_state"] == "selected"

    patch = apply_delivery_continuation_address_patch(
        db, tenant_id=tenant.id, conversation=fresh,
        customer_phone=CUSTOMER_PHONE, order_prep={},
    )
    assert patch["city"] == CITY
    assert patch["short_address_code"] == SHORT_CODE
    assert patch["customer_confirmed_previous_address"] is True


def test_unselected_candidate_is_offered_never_auto_adopted():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city=CITY, short_address_code=SHORT_CODE),
        source_ref=SALLA_CUSTOMER_ID,
        source_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    db.commit()
    convo = _conversation(db, tenant, customer)

    patch = apply_delivery_continuation_address_patch(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={},
    )
    # Known fields are carried so nothing known is asked again…
    assert patch["city"] == CITY
    # …but the candidate is not claimed as confirmed.
    assert "customer_confirmed_previous_address" not in patch
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None


def test_partial_candidate_keeps_known_fields_and_asks_only_what_is_missing():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    result = _import(db, tenant, customer, _payload(location=None))  # city only
    db.commit()
    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    candidate = resolution.reusable
    assert candidate.components.city == CITY
    assert candidate.sufficient is False
    assert candidate.missing_requirements == ("delivery_address",)

    convo = _conversation(db, tenant, customer)
    reply_ctx = load_checkout_reply_context(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={}, brain_state={},
    )
    assert reply_ctx.known_previous["city"] == CITY
    assert reply_ctx.field_modes.get("city") == "confirm"
    # The delivery address is still genuinely missing, so it is still asked.
    assert "delivery_address" in reply_ctx.missing_fields
    assert result.address_id is not None


def _load_ctx(db, tenant, convo, order_prep=None):
    ctx = load_checkout_reply_context(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep=dict(order_prep or {}),
        brain_state={},
    )
    db.commit()
    return ctx


def _wire_payload(presentation):
    """The payload this presentation would actually leave as.

    Built through the REAL surface choice and the REAL wire sanitizers, so
    a test never records a showing the provider would have thinned out.
    """
    from core.wa_link_buttons import whatsapp_reply_buttons_payload
    from modules.ai.order_flow_v2.checkout_context import (
        address_choice_actions,
        address_choice_rows,
        address_choice_surface,
    )

    surface = address_choice_surface(presentation)
    if surface == "list":
        rows = []
        seen_ids, seen_titles = set(), set()
        for row in address_choice_rows(presentation):
            if row["id"] in seen_ids or row["title"] in seen_titles:
                continue
            seen_ids.add(row["id"])
            seen_titles.add(row["title"])
            rows.append(row)
            if len(rows) >= 10:
                break
        return {"interactive": {"type": "list", "action": {"sections": [{"rows": rows}]}}}
    if surface == "buttons":
        buttons = whatsapp_reply_buttons_payload(address_choice_actions(presentation))
        return {"interactive": {"type": "button", "action": {"buttons": buttons}}}
    return {}


def _present(db, tenant, convo, reply_ctx, delivery_ref="wamid.out-1",
             duplicate_suppressed=False, sent_payload=None):
    """The SUCCESSFUL-SEND boundary, with the outbound message identity."""
    from modules.ai.order_flow_v2.checkout_context import delivered_address_action_ids

    presentation = getattr(reply_ctx, "presentation", reply_ctx)
    payload = _wire_payload(presentation) if sent_payload is None else sent_payload
    recorded = record_presented_address_offer(
        db, tenant_id=tenant.id, conversation=convo,
        presentation=presentation, delivery_ref=delivery_ref,
        duplicate_suppressed=duplicate_suppressed,
        delivered_action_ids=delivered_address_action_ids(payload),
    )
    db.commit()
    # The showing, so a test can build the action the customer would tap.
    return presentation if recorded else None


def _consent_meta(presentation, address_id, turn_ref="wamid.in-1"):
    """A structured customer action naming one address FROM one showing."""
    offer_id = getattr(presentation, "offer_id", presentation)
    return {
        "button_id": consent_action_id(offer_id, address_id),
        "wa_message_id": turn_ref,
    }


def test_a_context_read_alone_never_records_an_offer():
    """Reading the customer's addresses is not showing them to anyone."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    reply_ctx = _load_ctx(db, tenant, convo)
    # The revision is PREPARED for a reply that may or may not be sent…
    assert reply_ctx.presentation.address_id == imported.address_id
    # …but nothing was recorded, so a structured consent has nothing to
    # bind to yet.
    assert read_offered_address(tenant_id=tenant.id, conversation=convo) is None
    # Even the action id this unsent showing would have carried binds to
    # nothing, because the showing was never recorded.
    assert apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(reply_ctx.presentation, imported.address_id),
    ) == {}
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None


def test_structured_consent_after_a_real_presentation_selects_once():
    """The positive outcome: a customer who taps the choice gets it saved."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))
    assert showing is not None

    for turn in ("wamid.in-1", "wamid.in-2"):
        patch = apply_structured_address_consent(
            db, tenant_id=tenant.id, conversation=convo, order_prep={},
            inbound_metadata=_consent_meta(showing, imported.address_id, turn_ref=turn),
        )
        db.commit()
        assert patch["customer_confirmed_previous_address"] is True
        assert patch["shipping_source"] == "customer_selected_address"

    rows = db.query(CustomerAddressProvenance).all()
    assert len(rows) == 1
    assert rows[0].selection_state == "selected"
    assert db.query(CustomerAddress).count() == 1
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected.address_id == imported.address_id


@pytest.mark.parametrize("message", [
    "هل عنواني محفوظ عندكم؟",
    "نفس العنوان السابق",
])
def test_free_text_never_writes_a_durable_selection(message):
    """A question and a confirmation phrase are indistinguishable to words.

    Both read as ``previous_address_confirmed`` by the platform's intent
    detector, so neither may write the durable act. The turn's checkout
    still continues — only the durable selection needs a durable signal.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    patch = apply_previous_address_confirmation(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={}, message=message,
    )
    db.commit()
    assert patch.get("address_selection_durable") is False
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None


def test_consent_refuses_a_revision_the_customer_never_saw():
    """A refresh between the presentation and the tap invalidates consent."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city=CITY, address_line=STREET,
                                     short_address_code=SHORT_CODE),
        source_ref=SALLA_CUSTOMER_ID,
        source_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    row = db.query(CustomerAddress).filter_by(id=imported.address_id).one()
    row.address_text = "شارع لم يره العميل"
    db.add(row)
    db.commit()

    assert apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(showing, imported.address_id),
    ) == {}
    db.commit()
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None


def test_a_second_context_read_never_advances_the_recorded_offer():
    """A refreshed, unpresented revision must not become the live offer."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    first = _load_ctx(db, tenant, convo)
    showing = _present(db, tenant, convo, first)
    offered = read_offered_address(tenant_id=tenant.id, conversation=convo)

    # The source refreshes the row, then context is read again — without
    # anything being sent to the customer.
    row = db.query(CustomerAddress).filter_by(id=imported.address_id).one()
    row.address_text = "شارع لم يره العميل"
    db.add(row)
    db.commit()
    _load_ctx(db, tenant, convo)

    still = read_offered_address(tenant_id=tenant.id, conversation=convo)
    assert still["fingerprint"] == offered["fingerprint"]
    # And the consent now refuses, because the live row is not what was shown.
    assert apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(showing, imported.address_id),
    ) == {}


def test_consent_from_another_customers_conversation_is_refused():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    other = Customer(tenant_id=tenant.id, phone="+966500000777",
                     normalized_phone="+966500000777")
    db.add(other)
    db.commit()
    convo.customer_id = other.id
    db.add(convo)
    db.commit()

    assert apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(showing, imported.address_id),
    ) == {}
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None


def test_reselecting_a_previously_approved_address_makes_it_current_again():
    """A historical approval is not the current selection."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a = upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city=CITY, short_address_code=SHORT_CODE),
        source_ref="SC-A", source_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    b = upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city="جدة", short_address_code="JJJD5678"),
        source_ref="SC-B", source_updated_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    db.commit()
    for target in (a, b, a):
        record_explicit_address_selection(
            db, tenant_id=tenant.id, customer_id=customer.id,
            address_id=target.address_id,
            selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
            expected_fingerprint=target.fingerprint,
        )
        db.commit()
    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    assert resolution.selected.address_id == a.address_id
    # B remains durable history, not a candidate and not lost.
    assert [x.address_id for x in resolution.superseded_selections] == [b.address_id]
    assert {x.address_id for x in resolution.addresses} == {a.address_id, b.address_id}


def test_one_selection_operation_delivered_twice_is_idempotent():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    for _ in range(2):
        record_explicit_address_selection(
            db, tenant_id=tenant.id, customer_id=customer.id,
            address_id=imported.address_id,
            selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
            expected_fingerprint=imported.fingerprint,
            operation_ref="offer-2026-09-01T10:00:00Z",
        )
        db.commit()
    row = db.query(CustomerAddressProvenance).one()
    first_selected_at = row.selected_at
    record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=imported.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=imported.fingerprint,
        operation_ref="offer-2026-09-01T10:00:00Z",
    )
    db.commit()
    assert db.query(CustomerAddressProvenance).one().selected_at == first_selected_at


def test_unselected_candidate_never_makes_checkout_address_accepted():
    from core.wa_order_lifecycle import has_accepted_delivery_address  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city=CITY, short_address_code=SHORT_CODE),
        source_ref=SALLA_CUSTOMER_ID,
        source_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    db.commit()
    convo = _conversation(db, tenant, customer)
    patch = apply_delivery_continuation_address_patch(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={},
    )
    assert patch.get("city") == CITY
    assert "customer_confirmed_previous_address" not in patch
    # The locating artefact is withheld, so nothing downstream reads the
    # order as already having an accepted delivery address.
    assert has_accepted_delivery_address(patch) is False


def test_stale_source_event_is_refused_even_after_selection():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    fresh = _import(db, tenant, customer, _payload(updated_at="2026-09-09T10:00:00Z"))
    db.commit()
    record_explicit_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
        address_id=fresh.address_id,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=fresh.fingerprint,
    )
    db.commit()
    stale = _import(
        db, tenant, customer,
        _payload(location="عنوان قديم", updated_at="2026-09-01T10:00:00Z"),
    )
    db.commit()
    assert stale.action == ACTION_SKIPPED
    assert stale.reason == "stale_source_event"
    assert db.query(CustomerAddress).count() == 1


def test_provenance_read_failure_never_promotes_a_candidate():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()

    real_query = db.query

    def _broken(*entities, **kwargs):
        if entities and getattr(entities[0], "__name__", "") == "CustomerAddressProvenance":
            raise RuntimeError("provenance unavailable")
        return real_query(*entities, **kwargs)

    db.query = _broken
    try:
        resolution = resolve_customer_address_selection(
            db, tenant_id=tenant.id, customer_id=customer.id,
        )
    finally:
        db.query = real_query
    # A failed read says provenance is unknown — never that the row was
    # a pre-slice confirmed address.
    assert resolution.selected is None
    assert len(resolution.candidates) == 1
    assert resolution.candidates[0].provenance_known is False


# ── Guard wiring ────────────────────────────────────────────────────────
#
# The full pipeline-ordering contract lives in
# backend/tests/test_p1b_post_compose_guard_consolidation.py, which the
# repository's CI does not collect. This keeps the one fact this slice
# introduces — that the address save-claim guard is actually registered,
# and where — inside a module CI does run.

def test_address_save_claim_guard_is_registered_in_the_post_compose_pipeline():
    source = (
        BACKEND_DIR
        / "modules" / "ai" / "brain" / "postprocess" / "post_compose_guard_pipeline.py"
    ).read_text(encoding="utf-8")

    # Scope to the pipeline function: the staff guard is also named inside
    # the earlier handoff-only helper.
    body = source[source.index("def run_post_compose_truth_guards") :]
    positions = {
        name: body.index(f'guard_name = "{name}"')
        for name in (
            "shipment_truth_guard",
            "customer_address_save_claim_guard",
            "staff_escalation_truth_guard",
        )
    }
    assert (
        positions["shipment_truth_guard"]
        < positions["customer_address_save_claim_guard"]
        < positions["staff_escalation_truth_guard"]
    )
    # The guard resolves its evidence from the database, never from the
    # turn's own state.
    assert "resolve_and_apply_customer_address_save_claim_guard" in source
    assert "customer_id=getattr(convo, \"customer_id\", None)" in source


def test_address_save_claim_guard_resolver_reads_committed_evidence():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.rollback()

    from modules.ai.brain.postprocess.customer_address_save_claim_guard import (  # noqa: PLC0415
        resolve_and_apply_customer_address_save_claim_guard,
    )

    result = resolve_and_apply_customer_address_save_claim_guard(
        db=db,
        reply=SAVE_CLAIM,
        tenant_id=tenant.id,
        customer_id=customer.id,
    )
    assert result.action == "blocked_unsupported_address_save_claim"
    assert "تم حفظ عنوانك" not in result.reply
    assert result.evidence_scope == "none"


# ── R5: several candidates stay visible and explicitly selectable ────────

def _two_candidates(db, tenant, customer):
    a = upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city=CITY, short_address_code=SHORT_CODE),
        source_ref="SC-A", source_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    b = upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city="جدة", short_address_code="JJJD5678"),
        source_ref="SC-B", source_updated_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    db.commit()
    return a, b


def test_several_candidates_are_offered_for_explicit_selection():
    """No implicit default, but no dead end either."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)

    reply_ctx = _load_ctx(db, tenant, convo)
    assert reply_ctx.known_previous == {}
    assert {c["address_id"] for c in reply_ctx.address_choices} == {
        a.address_id, b.address_id
    }
    showing = _present(db, tenant, convo, reply_ctx)

    patch = apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(showing, b.address_id),
    )
    db.commit()
    assert patch["city"] == "جدة"
    assert patch["customer_confirmed_previous_address"] is True
    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    assert resolution.selected.address_id == b.address_id


def test_checkout_level_a_then_b_then_a_selection_round_trip():
    """C3/R5: the customer may go back to an address they selected before.

    The order's state is RETAINED across the three turns, the way a real
    conversation retains it. Passing a fresh ``order_prep={}`` each time
    was the test erasing the very condition its own first step created:
    once address A is accepted, the second tap hit an early bail and
    returned an empty patch, so the order kept A while the customer was
    being asked to choose. Every step goes through the public checkout
    helpers, so a projection that quietly drops superseded selections
    fails here rather than only in the core resolver.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)

    from core.wa_order_lifecycle import has_accepted_delivery_address  # noqa: PLC0415

    chosen = []
    order_prep = {}
    for target in (a, b, a):
        reply_ctx = _load_ctx(db, tenant, convo, order_prep=order_prep)
        # The full inventory stays offerable, whatever is selected now.
        assert {c["address_id"] for c in reply_ctx.address_choices} == {
            a.address_id, b.address_id
        }
        showing = _present(db, tenant, convo, reply_ctx)
        patch = apply_structured_address_consent(
            db, tenant_id=tenant.id, conversation=convo, order_prep=order_prep,
            inbound_metadata=_consent_meta(showing, target.address_id,
                                           turn_ref=f"wamid.in-{target.address_id}-{len(chosen)}"),
        )
        db.commit()
        assert patch.get("customer_confirmed_previous_address") is True
        # The order state the next turn inherits, exactly as the owner
        # merges it.
        order_prep = {**order_prep, **patch}
        assert has_accepted_delivery_address(order_prep) is True
        assert order_prep["short_address_code"] == target.components.short_address_code
        assert order_prep["city"] == target.components.city
        resolution = resolve_customer_address_selection(
            db, tenant_id=tenant.id, customer_id=customer.id,
        )
        assert resolution.selected.address_id == target.address_id
        chosen.append(resolution.selected.address_id)

    assert chosen == [a.address_id, b.address_id, a.address_id]
    assert db.query(CustomerAddress).filter_by(tenant_id=tenant.id).count() == 2


def test_selecting_an_address_that_was_never_offered_is_refused():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, _b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    # No offer was recorded for this conversation.
    assert apply_explicit_address_selection(
        db, tenant_id=tenant.id, conversation=convo, address_id=a.address_id,
        order_prep={},
    ) == {}
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None


def test_explicit_selection_refuses_a_revision_changed_since_the_offer():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, _b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    load_checkout_reply_context(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={}, brain_state={},
    )
    db.commit()
    row = db.query(CustomerAddress).filter_by(id=a.address_id).one()
    row.city = "مدينة أخرى"
    db.add(row)
    db.commit()
    assert apply_explicit_address_selection(
        db, tenant_id=tenant.id, conversation=convo, address_id=a.address_id,
        order_prep={},
    ) == {}


def test_another_conversation_offer_cannot_select_for_this_customer():
    db, _ = _make_db()
    tenant, customer = _seed(db)
    other_tenant, other_customer = _seed(db, phone="+966500000777", salla_id="SC-OTHER")
    a, _b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    load_checkout_reply_context(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={}, brain_state={},
    )
    db.commit()
    # The same conversation object, judged for a different tenant.
    assert apply_explicit_address_selection(
        db, tenant_id=other_tenant.id, conversation=convo, address_id=a.address_id,
        order_prep={},
    ) == {}


# ── R2: grounded recovery, at the boundary that still has a composer ─────

def test_whole_removal_asks_for_one_natural_recomposition():
    """Composition is attempted BEFORE any deterministic line.

    The composer is stubbed; no provider is called. What is under test is
    the contract: one more natural candidate is requested, revalidated,
    and used when it is truthful.
    """
    import asyncio  # noqa: PLC0415

    from modules.ai.brain.postprocess.customer_address_save_claim_guard import (  # noqa: PLC0415
        apply_customer_address_save_claim_guard,
        finalize_address_claim_after_authorized_recompose,
        invoke_authorized_address_claim_recompose,
    )

    first = apply_customer_address_save_claim_guard(
        reply="تم حفظ عنوانك واعتماده للتوصيل", evidence=None,
    )
    assert first.scrubbed_empty is True
    assert first.requires_grounded_recompose is True

    class _Composer:
        def __init__(self):
            self.calls = 0

        async def compose(self, decision, result, ctx):
            self.calls += 1
            return "وش تحب نكمل فيه؟"

    composer = _Composer()
    text, failed, calls = asyncio.run(
        invoke_authorized_address_claim_recompose(composer, None, None, None)
    )
    assert calls == 1 and composer.calls == 1 and failed is False

    second = apply_customer_address_save_claim_guard(
        reply=text, evidence=None, allow_recompose=False,
    )
    data = {}
    final = finalize_address_claim_after_authorized_recompose(
        second_pass=second, recomposed_reply=text, result_data=data,
        compose_failed=failed,
    )
    # The LLM's own words are delivered — no template, no fallback.
    assert final == "وش تحب نكمل فيه؟"
    assert data.get("compose_source") != "fallback_deterministic"


def test_a_failed_recomposition_falls_back_with_auditable_metadata():
    """Only after a genuine compose failure, and it must be measurable."""
    import asyncio  # noqa: PLC0415

    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415
    from modules.ai.brain.postprocess.customer_address_save_claim_guard import (  # noqa: PLC0415
        ADDRESS_CLAIM_FALLBACK_ACTION,
        ADDRESS_CLAIM_FALLBACK_REASON,
        apply_customer_address_save_claim_guard,
        finalize_address_claim_after_authorized_recompose,
        invoke_authorized_address_claim_recompose,
    )

    class _BrokenComposer:
        async def compose(self, decision, result, ctx):
            raise RuntimeError("provider unavailable")

    text, failed, _calls = asyncio.run(
        invoke_authorized_address_claim_recompose(_BrokenComposer(), None, None, None)
    )
    assert failed is True and not text.strip()

    second = apply_customer_address_save_claim_guard(
        reply=text, evidence=None, allow_recompose=False,
    )
    data = {}
    final = finalize_address_claim_after_authorized_recompose(
        second_pass=second, recomposed_reply=text, result_data=data,
        compose_failed=failed,
    )
    assert final.strip()
    assert is_compose_failure_fallback(final)
    assert data["compose_source"] == "fallback_deterministic"
    assert data["fallback_reason"] == ADDRESS_CLAIM_FALLBACK_REASON
    assert data["fallback_action_type"] == ADDRESS_CLAIM_FALLBACK_ACTION
    assert data["chosen_path"] == ADDRESS_CLAIM_FALLBACK_ACTION
    assert data["llm_candidate_present"] is True
    assert data["final_text_transformed"] is True
    assert "customer_address_save_claim_guard" in data["final_transform_reasons"]


def test_a_recomposition_that_repeats_the_claim_is_not_restored():
    """A second candidate carrying the same false claim never ships."""
    from modules.ai.brain.postprocess.customer_address_save_claim_guard import (  # noqa: PLC0415
        apply_customer_address_save_claim_guard,
        finalize_address_claim_after_authorized_recompose,
    )

    repeated = "تم حفظ عنوانك واعتماده للتوصيل"
    second = apply_customer_address_save_claim_guard(
        reply=repeated, evidence=None, allow_recompose=False,
    )
    data = {}
    final = finalize_address_claim_after_authorized_recompose(
        second_pass=second, recomposed_reply=repeated, result_data=data,
        compose_failed=False,
    )
    assert "تم حفظ عنوانك" not in final
    assert final.strip()
    assert data["compose_source"] == "fallback_deterministic"


def test_the_brain_pipeline_runs_the_address_claim_stage_with_a_composer():
    """The stage lives where a composer is still reachable."""
    from pathlib import Path  # noqa: PLC0415

    source = Path("backend/modules/ai/brain/pipeline.py").read_text(encoding="utf-8")
    assert "invoke_authorized_address_claim_recompose" in source
    assert "read_turn_address_operation_for_conversation" in source
    assert "finalize_address_claim_after_authorized_recompose" in source


@pytest.mark.parametrize("reply,blocked", [
    # A question about something else never exempts a completed assertion.
    ("تم حفظ عنوانك هل تريد إكمال الطلب؟", True),
    ("تم حفظ عنوانك. هل تريد إكمال الطلب؟", True),
    # "without any problem" is not a denial that saving happened.
    ("عنوانك محفوظ بدون أي مشكلة", True),
    # A negation that governs a DIFFERENT statement exempts nothing. The
    # attached "و" marks where that other statement ends…
    ("لا يوجد أي مشكلة وتم حفظ عنوانك", True),
    # …and an adversative connective starts a new clause outright.
    ("لم نغير طلبك لكن تم حفظ عنوانك", True),
    ("لم نغير طلبك بس تم حفظ عنوانك", True),
    # Genuine negatives and questions stay untouched.
    ("لم يتم حفظ عنوانك", False),
    ("لم يسبق أن تم حفظ عنوانك", False),
    ("ما تم حفظ عنوانك بعد، هل ترغب بإرساله؟", False),
    ("هل تريد اعتماد عنوانك؟", False),
    ("عنوانك غير محفوظ عندنا", False),
    ("لا يمكننا حفظ عنوانك حالياً", False),
])
def test_clause_level_truth_without_a_phrase_blacklist(reply, blocked):
    result = apply_customer_address_save_claim_guard(reply=reply, evidence=None)
    if blocked:
        assert result.action == "blocked_unsupported_address_save_claim"
        assert "تم حفظ عنوانك" not in result.reply
        assert "عنوانك محفوظ" not in result.reply
    else:
        assert result.action == "allowed"
        assert result.reply == reply


def test_the_honest_half_of_a_mixed_reply_survives():
    """Clause granularity: only the unsupported assertion is removed."""
    result = apply_customer_address_save_claim_guard(
        reply="تم حفظ عنوانك هل تريد إكمال الطلب؟", evidence=None,
    )
    assert result.reply.strip() == "هل تريد إكمال الطلب؟"
    assert result.scrubbed_empty is False


# ── I1 / I2 / I3: the presentation the customer actually received ───────

def test_a_showing_is_recorded_only_with_a_proven_outbound_identity():
    """I1: producing a reply is not showing it.

    ``_finalize`` runs before permission gating, before the outbound lock
    and before any send. A presentation recorded there described a message
    that may never have left — and a later tap would then be checked
    against something nobody saw.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    reply_ctx = _load_ctx(db, tenant, convo)

    delivered = delivered_address_action_ids(_wire_payload(reply_ctx.presentation))
    assert delivered, "the showing must reach the wire at all"

    # No outbound identity — a blocked, suppressed or failed send.
    assert record_presented_address_offer(
        db, tenant_id=tenant.id, conversation=convo,
        presentation=reply_ctx.presentation, delivery_ref="",
        delivered_action_ids=delivered,
    ) is False
    assert read_offered_address(tenant_id=tenant.id, conversation=convo) is None

    # An outbound identity, but nothing observed on the wire — a caller
    # that cannot say what left proves no showing either.
    assert record_presented_address_offer(
        db, tenant_id=tenant.id, conversation=convo,
        presentation=reply_ctx.presentation, delivery_ref="wamid.out-real",
        delivered_action_ids=[],
    ) is False
    assert read_offered_address(tenant_id=tenant.id, conversation=convo) is None

    # A real outbound message id AND the actions that were on it.
    assert record_presented_address_offer(
        db, tenant_id=tenant.id, conversation=convo,
        presentation=reply_ctx.presentation, delivery_ref="wamid.out-real",
        delivered_action_ids=delivered,
    ) is True
    db.commit()
    offer = read_offered_address(tenant_id=tenant.id, conversation=convo)
    assert offer["delivery_ref"] == "wamid.out-real"
    assert offer["offer_id"] == reply_ctx.presentation.offer_id


def test_a_deduplicated_send_never_claims_a_new_showing():
    """I1: the dedup answered with an EARLIER message's id.

    That proves the earlier message, not a new one. It may reaffirm the
    offer already recorded; it may not create a different one.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    first = _load_ctx(db, tenant, convo)
    assert record_presented_address_offer(
        db, tenant_id=tenant.id, conversation=convo, presentation=first.presentation,
        delivery_ref="wamid.out-1",
        delivered_action_ids=delivered_address_action_ids(
            _wire_payload(first.presentation)),
    ) is True
    db.commit()

    # A different showing, answered by the dedup with the old id.
    second = _load_ctx(db, tenant, convo)
    assert second.presentation.offer_id != first.presentation.offer_id
    assert record_presented_address_offer(
        db, tenant_id=tenant.id, conversation=convo, presentation=second.presentation,
        delivery_ref="wamid.out-1", duplicate_suppressed=True,
        delivered_action_ids=delivered_address_action_ids(
            _wire_payload(second.presentation)),
    ) is False
    db.commit()
    assert read_offered_address(
        tenant_id=tenant.id, conversation=convo,
    )["offer_id"] == first.presentation.offer_id

    # Re-sending the SAME showing is a reaffirmation, which is allowed.
    assert record_presented_address_offer(
        db, tenant_id=tenant.id, conversation=convo, presentation=first.presentation,
        delivery_ref="wamid.out-1", duplicate_suppressed=True,
        delivered_action_ids=delivered_address_action_ids(
            _wire_payload(first.presentation)),
    ) is True


def test_the_outbound_payload_carries_the_choices_the_customer_taps():
    """I2/I5: the action ids have a real producer, not only a definition."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    reply_ctx = _load_ctx(db, tenant, convo)

    actions = address_choice_actions(reply_ctx.presentation)
    assert {a.address_id, b.address_id} == {
        structured_consent_action({"button_id": action["reply"]["id"]})[1]
        for action in actions
    }
    # Every action names THIS showing, and carries a title built from the
    # address's own stored facts — no composed prose.
    for action in actions:
        assert action["type"] == "reply"
        offer_id, _address_id = structured_consent_action(
            {"button_id": action["reply"]["id"]})
        assert offer_id == reply_ctx.presentation.offer_id
        assert action["reply"]["title"] and len(action["reply"]["title"]) <= 20
    assert {"الرياض", "جدة"} & {a["reply"]["title"].split()[0] for a in actions}

    # The wire sanitizer must keep them intact — a flat {id, title} would
    # survive it as an empty, untappable button.
    from core.wa_link_buttons import whatsapp_reply_buttons_payload  # noqa: PLC0415

    wire = whatsapp_reply_buttons_payload(actions)
    assert len(wire) == len(actions)
    assert all(w["reply"]["id"] and w["reply"]["title"] for w in wire)
    assert {w["reply"]["id"] for w in wire} == {a["reply"]["id"] for a in actions}


def test_an_action_from_a_superseded_showing_approves_nothing():
    """I2: an old button must never approve a revision presented later."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    old_showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))
    kept = _consent_meta(old_showing, imported.address_id, turn_ref="wamid.in-old")

    # The row changes and a NEW showing is delivered.
    row = db.query(CustomerAddress).filter_by(id=imported.address_id).one()
    row.city = "جدة"
    db.add(row)
    db.commit()
    new_showing = _present(
        db, tenant, convo, _load_ctx(db, tenant, convo), delivery_ref="wamid.out-2")
    assert new_showing.offer_id != old_showing.offer_id

    # The customer taps the button from the OLD message.
    assert apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=kept,
    ) == {}
    db.commit()
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None

    # The button from the message that is actually live still works.
    patch = apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(new_showing, imported.address_id,
                                       turn_ref="wamid.in-new"),
    )
    db.commit()
    assert patch["city"] == "جدة"


def test_one_action_redelivered_is_one_selection_but_a_new_tap_is_new():
    """I2: idempotency identity comes from the action, not from a timestamp."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    for _ in range(2):
        # The same inbound message delivered twice.
        assert apply_structured_address_consent(
            db, tenant_id=tenant.id, conversation=convo, order_prep={},
            inbound_metadata=_consent_meta(showing, a.address_id, turn_ref="wamid.dup"),
        )
        db.commit()
    assert db.query(CustomerAddressProvenance).filter_by(
        customer_address_id=a.address_id, selection_state="selected").count() == 1

    # A different tap, from the same showing, is a different selection.
    assert apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(showing, b.address_id, turn_ref="wamid.second"),
    )
    db.commit()
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected.address_id == b.address_id


def test_an_action_arriving_in_another_conversation_is_refused():
    """I2: an offer authorizes an action in the conversation that got it."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    # A second conversation for the same customer, carrying a copy of the
    # first one's offer metadata.
    other = _conversation(db, tenant, customer)
    other.extra_metadata = dict(convo.extra_metadata or {})
    db.add(other)
    db.commit()

    assert apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=other, order_prep={},
        inbound_metadata=_consent_meta(showing, imported.address_id),
    ) == {}


def test_free_text_about_an_address_never_accepts_the_delivery_address():
    """I2: an inquiry must not reach checkout as an accepted address."""
    from core.wa_order_lifecycle import has_accepted_delivery_address  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    for message in ("هل عنواني محفوظ عندكم؟", "نفس العنوان السابق"):
        patch = apply_previous_address_confirmation(
            db, tenant_id=tenant.id, conversation=convo,
            customer_phone=CUSTOMER_PHONE, order_prep={}, message=message)
        db.commit()
        assert has_accepted_delivery_address(patch) is False, message
        assert patch.get("customer_confirmed_previous_address") is not True
        # The known fields still travel, so the reply can ask about them.
        assert patch.get("city") == CITY


# ── I1 / I3 through the ACTUAL owner, not the helpers ───────────────────

def _owner_turn(db, tenant, convo, *, live, message="متابعة", inbound_metadata=None,
                missing=("customer_name",)):
    """Run the real OrderFlowV2 owner with its surrounding services stubbed.

    Capability, routing and draft lookups are stubbed because they are not
    what is under test; the address lifecycle, the ORM and the owner's own
    finalization are real.
    """
    from contextlib import ExitStack  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    from modules.ai.order_flow_v2 import owner as owner_module  # noqa: PLC0415

    prep = {
        "order_flow_v2_active": True,
        "line_items": [{"product_id": "neutral", "product_name": "قميص قطني",
                        "quantity": 1, "catalog_price": 80}],
        "order_flow_v2_trusted_price": True,
        "order_flow_v2_catalog_total": 80,
    }
    brain_state = {"order_prep": prep, "cart_items": prep["line_items"]}
    with ExitStack() as stack:
        for name, value in (
            ("is_order_flow_v2_enabled", live),
            ("is_order_flow_v2_shadow_enabled", not live),
            ("_load_brain_state", (convo, brain_state)),
            ("operational_tuple", (live, not live, "probe")),
            ("load_local_draft_evidence", None),
            ("rehydrate_order_prep_patch", {}),
            ("compute_v2_missing_fields", list(missing)),
        ):
            stack.enter_context(patch.object(owner_module, name, return_value=value))
        return owner_module.try_handle_order_flow_v2(
            db, tenant_id=tenant.id, customer_phone=CUSTOMER_PHONE, message=message,
            inbound_metadata=dict(inbound_metadata or {
                "wa_message_id": "wamid.in-owner",
                # A structured continue tap, so the owner may own the turn
                # pre-brain; it names no address, so it grants no consent.
                "button_id": "checkout_continue",
            }),
        )


def test_a_reply_that_asks_for_a_name_never_registers_the_inventory_as_shown():
    """I1: the owner must not mark a presentation it is not making."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    result = _owner_turn(db, tenant, convo, live=True)
    db.commit()

    assert "اسمك" in (result.reply or ""), "the probe must exercise the name question"
    assert not result.address_choice_actions
    assert result.address_presentation is None
    assert read_offered_address(tenant_id=tenant.id, conversation=convo) is None


def test_a_reply_that_confirms_the_address_carries_the_choices():
    """I1: the address turn DOES produce an outbound action payload."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)

    result = _owner_turn(db, tenant, convo, live=True, missing=("delivery_address",))
    db.commit()

    assert result.address_presentation is not None
    ids = {
        structured_consent_action({"button_id": action["reply"]["id"]})[1]
        for action in result.address_choice_actions
    }
    assert ids == {a.address_id, b.address_id}
    # Still not recorded — that waits for a proven send.
    assert read_offered_address(tenant_id=tenant.id, conversation=convo) is None


def test_shadow_evaluation_performs_no_durable_address_write():
    """I3: observe, never write — including before the result is discarded."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))
    before = dict(convo.extra_metadata or {})

    result = _owner_turn(
        db, tenant, convo, live=False,
        inbound_metadata=_consent_meta(showing, imported.address_id,
                                       turn_ref="wamid.in-shadow"),
    )
    db.commit()

    assert result.handled is False and result.shadow_only is True
    assert result.address_presentation is None
    assert not result.address_choice_actions
    # Nothing durable moved: no selection, no operation, no new offer.
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None
    assert (convo.extra_metadata or {}).get("address_operation") is None
    assert (convo.extra_metadata or {}).get("address_offer") == before.get("address_offer")


def test_the_live_owner_records_the_selection_the_shadow_one_refused():
    """I3's positive half: the gate blocks shadow, not the capability."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    _owner_turn(db, tenant, convo, live=True,
                inbound_metadata=_consent_meta(showing, imported.address_id,
                                               turn_ref="wamid.in-live"))
    db.commit()

    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id)
    assert resolution.selected is not None
    assert resolution.selected.address_id == imported.address_id
    assert (convo.extra_metadata or {}).get("address_operation") is not None


def test_the_webhook_sends_address_choices_on_the_interactive_surface():
    """I1: the delivery seam that makes the actions reachable at all."""
    from pathlib import Path  # noqa: PLC0415

    source = Path("backend/routers/whatsapp_webhook.py").read_text(encoding="utf-8")
    assert "address_choice_actions" in source
    assert "_send_interactive_reply(" in source
    assert "record_presented_address_offer(" in source
    # Recorded from the SEND result, never from the inbound turn.
    assert 'delivery_ref=str(_of2_sink.get("wamid") or "")' in source
    assert 'duplicate_suppressed=bool(' in source


# ── I6 / I7: identity of the committed operation, provenance of the text ─

def test_an_unidentified_committed_selection_supports_no_new_claim():
    """I6: a real selection that cannot say WHICH operation made it.

    The confirmed-shipping writer records a genuine, reusable selection
    with no operation reference. That row is not evidence that this turn
    adopted anything — an arbitrary attempt naming the same row and
    revision used to receive both claim permissions.
    """
    from core.customer_shipping_address_writer import (  # noqa: PLC0415
        persist_customer_shipping_address_if_confirmed,
    )

    db, _ = _make_db()
    tenant, customer = _seed(db)
    persisted, row = persist_customer_shipping_address_if_confirmed(
        db, tenant_id=tenant.id, customer_id=customer.id, order_id=None,
        snapshot={"city": "جدة", "short_address_code": "JJJD5678"},
        order_prep={"customer_confirmed_previous_address": True},
        confirmed_reason="test")
    db.commit()
    assert persisted is True

    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id)
    # It IS a selection, and reuse of it is unchanged.
    assert resolution.selected is not None
    assert resolution.selected.selection_operation_ref == ""

    evidence = resolve_customer_address_persistence_evidence(
        db, tenant_id=tenant.id, customer_id=customer.id,
        attempt=AddressOperationAttempt(
            operation=AddressOperation.ADOPT_SELECTION, tenant_id=tenant.id,
            customer_id=customer.id, address_id=row.id,
            fingerprint=resolution.selected.fingerprint,
            operation_ref="an-operation-that-never-ran"),
    )
    assert evidence.reason == "committed_operation_unknown"
    assert evidence.allows_adopted_address_claim() is False
    assert evidence.allows_saved_address_claim() is False


def test_the_last_line_fallback_declares_its_own_provenance():
    """I7: the audit trail must not say the customer read the model's words.

    This boundary runs after composition and substitutes the platform's
    line directly. Leaving ``compose_source=llm`` on the tracker was the
    same class of untruth the guard exists to remove, one layer down.
    """
    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415
    from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: PLC0415
        run_post_compose_truth_guards,
    )

    db, _ = _make_db()
    tenant, customer = _seed(db)
    convo = _conversation(db, tenant, customer)
    tracker = {"compose_source": "llm", "response_mode": "llm",
               "chosen_path": "probe_llm", "llm_candidate_present": True}
    compose_event = dict(tracker)

    result = run_post_compose_truth_guards(
        db=db, tenant_id=tenant.id, to=CUSTOMER_PHONE, text="وين توصلون؟",
        reply="تم حفظ عنوانك واعتماده للتوصيل.", convo=convo, inbound_metadata={},
        brain_handoff=False, brain_nc_block=False, brain_nc_category="", br_action="",
        brain_persona_compose_event=compose_event, mode="primary",
        conversation_id=convo.id, live_provenance_tracker=tracker)

    assert result.reply.strip()
    assert is_compose_failure_fallback(result.reply)
    for sink in (tracker, compose_event):
        assert sink["compose_source"] == "fallback_deterministic"
        assert sink["response_mode"] == "fallback_deterministic"
        assert sink["final_customer_text_source"] == "fallback_deterministic"
        assert sink["chosen_path"] == "address_save_claim_failed_compose"
        assert sink["fallback_action_type"] == "address_save_claim_failed_compose"
        assert "scrubbed_empty" in sink["fallback_reason"]
        assert sink["llm_candidate_present"] is True
        assert sink["final_text_transformed"] is True


def test_an_allowed_reply_leaves_the_provenance_untouched():
    """The counterpart: nothing is restamped when nothing is substituted."""
    from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: PLC0415
        run_post_compose_truth_guards,
    )

    db, _ = _make_db()
    tenant, customer = _seed(db)
    convo = _conversation(db, tenant, customer)
    tracker = {"compose_source": "llm", "response_mode": "llm",
               "chosen_path": "probe_llm", "llm_candidate_present": True}

    reply = "لم يتم حفظ عنوانك"
    result = run_post_compose_truth_guards(
        db=db, tenant_id=tenant.id, to=CUSTOMER_PHONE, text="هل حفظتم عنواني؟",
        reply=reply, convo=convo, inbound_metadata={}, brain_handoff=False,
        brain_nc_block=False, brain_nc_category="", br_action="",
        brain_persona_compose_event=None, mode="primary", conversation_id=convo.id,
        live_provenance_tracker=tracker)

    assert result.reply == reply
    assert tracker["compose_source"] == "llm"
    assert "fallback_reason" not in tracker


# ── C1–C4: the closure review's reproductions, as regressions ────────


def _many_candidates(db, tenant, customer, count, city="الرياض"):
    """Several addresses in ONE city, distinguished only by their street."""
    rows = []
    for index in range(count):
        rows.append(
            upsert_imported_address_candidate(
                db, tenant_id=tenant.id, customer_id=customer.id,
                components=AddressComponents(
                    city=city, address_line=f"شارع مستقل {index}",
                ),
                source_ref=f"ADDR-{index}",
            )
        )
    db.commit()
    return rows


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7, 10])
def test_every_offered_address_survives_the_wire(count):
    """C1: what is recorded as shown is what the payload actually carried.

    Four addresses in one city all titled "الرياض" met the wire
    sanitizer's duplicate-title rule and arrived as ONE tappable choice,
    while all four were recorded as offered. A tap the customer could not
    make was authorized, and three addresses were unreachable.
    """
    from core.wa_link_buttons import whatsapp_reply_buttons_payload  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    rows = _many_candidates(db, tenant, customer, count)
    convo = _conversation(db, tenant, customer)

    result = _owner_turn(db, tenant, convo, live=True, missing=("delivery_address",))
    db.commit()
    presentation = result.address_presentation
    assert presentation is not None

    payload = _wire_payload(presentation)
    delivered = delivered_address_action_ids(payload)
    # Every address the customer owns is selectable on the message sent.
    assert len(delivered) == count
    assert {structured_consent_action({"button_id": i})[1] for i in delivered} == {
        row.address_id for row in rows
    }
    # Distinguishable, and never thinned by the real button sanitizer.
    if result.address_choice_surface == "buttons":
        assert len(whatsapp_reply_buttons_payload(result.address_choice_actions)) == count
    titles = [
        row["title"] for section in payload["interactive"]["action"].get("sections", [])
        for row in section["rows"]
    ] or [b["reply"]["title"] for b in payload["interactive"]["action"].get("buttons", [])]
    assert len(set(titles)) == len(titles)
    assert all(titles)

    _present(db, tenant, convo, result.address_presentation, sent_payload=payload)
    offered, _ = _offered_revisions(tenant_id=tenant.id, conversation=convo,
                                    offer_id=presentation.offer_id)
    assert set(offered) == {row.address_id for row in rows}


def test_a_choice_the_wire_dropped_is_never_authorized():
    """C1: the receipt follows the payload, not the intention."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    reply_ctx = _load_ctx(db, tenant, convo)
    presentation = reply_ctx.presentation

    # A transport that carried only the FIRST choice.
    full = _wire_payload(presentation)
    thinned = {
        "interactive": {
            "type": "list",
            "action": {"sections": [{"rows": [
                {"id": consent_action_id(presentation.offer_id, a.address_id),
                 "title": "الرياض"},
            ]}]},
        }
    }
    assert len(delivered_address_action_ids(full)) == 2

    _present(db, tenant, convo, presentation, sent_payload=thinned)
    offered, _ = _offered_revisions(tenant_id=tenant.id, conversation=convo,
                                    offer_id=presentation.offer_id)
    assert set(offered) == {a.address_id}

    # The address that never reached the customer authorizes nothing.
    assert apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(presentation, b.address_id,
                                       turn_ref="wamid.in-unshown"),
    ) == {}
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None


def test_an_inventory_larger_than_one_message_stays_reachable():
    """C1: paging, not silent truncation, for a long address book."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    rows = _many_candidates(db, tenant, customer, 12)
    convo = _conversation(db, tenant, customer)

    seen = set()
    page_action = None
    for _turn in range(2):
        meta = {"wa_message_id": f"wamid.page-{_turn}",
                "button_id": page_action or "checkout_continue"}
        result = _owner_turn(db, tenant, convo, live=True,
                             missing=("delivery_address",), inbound_metadata=meta)
        db.commit()
        presentation = result.address_presentation
        assert presentation is not None
        assert result.address_choice_surface == "list"
        delivered = delivered_address_action_ids(_wire_payload(presentation))
        seen |= {structured_consent_action({"button_id": i})[1] for i in delivered}
        page_action = address_more_action_id(presentation)
        assert page_action, "a longer inventory must offer a way to the rest"

    assert seen == {row.address_id for row in rows}


def test_a_denied_commerce_permission_writes_no_selection():
    """C2: authorization comes BEFORE the mutation, not after it.

    The owner returned ``handled=False`` with a denial reason while the
    durable selection and the turn operation were already written — and
    an unhandled result cannot undo them, because the caller commits the
    transaction either way.
    """
    from unittest.mock import patch as _patch  # noqa: PLC0415

    from modules.ai.commerce.permission_loader import PermissionLoadResult  # noqa: PLC0415
    from modules.ai.commerce.permissions import CommercePermissionSet  # noqa: PLC0415
    from modules.ai.order_flow_v2 import owner as owner_module  # noqa: PLC0415

    for load_ok, expected_reason in (
        (True, "commerce_permission_denied:create_orders"),
        (False, "commerce_permissions_load_failed"),
    ):
        db, _ = _make_db()
        tenant, customer = _seed(db)
        imported = _import(db, tenant, customer, _payload())
        db.commit()
        convo = _conversation(db, tenant, customer)
        showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

        denied = PermissionLoadResult(
            permissions=CommercePermissionSet(tenant_id=tenant.id, can_create_orders=False),
            source="db_row" if load_ok else "load_failed",
            ok=load_ok,
        )
        with _patch.object(owner_module, "load_tenant_commerce_permissions",
                           return_value=denied):
            result = _owner_turn(
                db, tenant, convo, live=True,
                inbound_metadata=_consent_meta(showing, imported.address_id,
                                               turn_ref="wamid.in-denied"),
            )
        # The caller commits whatever the owner left behind.
        db.commit()

        assert result.handled is False
        assert result.reason == expected_reason
        assert resolve_customer_address_selection(
            db, tenant_id=tenant.id, customer_id=customer.id,
        ).selected is None
        assert (convo.extra_metadata or {}).get("address_operation") is None


def test_an_authorized_commerce_permission_still_writes_the_selection():
    """C2's positive half: the gate blocks denial, not the capability."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    _owner_turn(
        db, tenant, convo, live=True,
        inbound_metadata=_consent_meta(showing, imported.address_id,
                                       turn_ref="wamid.in-allowed"),
    )
    db.commit()

    selected = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected
    assert selected is not None and selected.address_id == imported.address_id


# ── Section 5: the address truth contract at the OrderFlowV2 boundary ──


def _of2_guard(db, tenant, convo, reply, turn_ref="wamid.in-guard"):
    from modules.ai.order_flow_v2.outbound_guards import (  # noqa: PLC0415
        apply_order_flow_v2_outbound_guards,
    )

    sink = {}
    text = apply_order_flow_v2_outbound_guards(
        reply,
        db=db,
        tenant_id=tenant.id,
        conversation_id=convo.id,
        conversation=convo,
        turn_ref=turn_ref,
        provenance_sink=sink,
        order_prep={},
    )
    return text, sink


def test_the_orderflow_boundary_removes_an_unsupported_save_claim():
    """Section 5: this branch answers and returns before the Brain guards.

    A reply produced here claiming the address was saved met no address
    guard at all — the shared post-compose boundary never ran for it.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    text, sink = _of2_guard(db, tenant, convo, "تم حفظ عنوانك. نكمل الطلب؟")

    assert "حفظ عنوانك" not in text
    assert text.strip(), "the guard removes the claim; it must not silence the turn"
    assert sink["address_save_claim_guard_action"] == (
        "blocked_unsupported_address_save_claim"
    )
    assert sink["final_text_transformed"] is True
    assert "customer_address_save_claim_guard" in sink["final_transform_reasons"]


def test_the_orderflow_boundary_keeps_a_claim_its_own_turn_committed():
    """Section 5's positive half: committed evidence carries the claim."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    _owner_turn(
        db, tenant, convo, live=True,
        inbound_metadata=_consent_meta(showing, imported.address_id,
                                       turn_ref="wamid.in-committed"),
    )
    db.commit()

    text, sink = _of2_guard(db, tenant, convo, "تم حفظ عنوانك. نكمل الطلب؟",
                            turn_ref="wamid.in-committed")
    assert "تم حفظ عنوانك" in text
    assert sink["address_save_claim_guard_action"] == "allowed"


def test_an_operation_from_another_turn_never_carries_this_reply():
    """Section 5: the evidence is scoped to THIS inbound turn."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))

    _owner_turn(
        db, tenant, convo, live=True,
        inbound_metadata=_consent_meta(showing, imported.address_id,
                                       turn_ref="wamid.in-earlier"),
    )
    db.commit()

    text, sink = _of2_guard(db, tenant, convo, "تم حفظ عنوانك. نكمل الطلب؟",
                            turn_ref="wamid.in-a-later-turn")
    assert "حفظ عنوانك" not in text
    assert "نكمل الطلب؟" in text, "the honest half of the reply survives"
    assert sink["address_save_claim_guard_action"] == (
        "blocked_unsupported_address_save_claim"
    )


def test_an_emptied_reply_is_suppressed_not_falsely_attributed():
    """D4: no compose happened here, so no compose failure may be claimed.

    When removal leaves nothing, the approved generic emergency fallback
    is NOT available: its exception is scoped to a genuine LLM compose
    failure, and nothing on this path composes. Borrowing its wording
    meant recording ``llm_candidate_present`` and a recomposition that
    never occurred — metadata asserting an event that did not happen,
    which is the same class of untruth the guard exists to remove. The
    turn is refused instead, and the gap is recorded so it is measurable.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    # The whole reply is the unsupported claim — removal leaves nothing.
    text, sink = _of2_guard(db, tenant, convo, "تم حفظ عنوانك")

    assert text == ""
    assert sink["address_claim_send_suppressed"] is True
    assert sink["address_save_claim_suppress_reason"] == "removal_left_no_reply"
    assert sink["address_save_claim_guard_action"] == (
        "suppressed_unverifiable_address_save_claim"
    )
    # The truthful part: nothing composed, so nothing may be claimed.
    assert sink["llm_candidate_present"] is False
    assert sink["address_claim_compose_attempted"] is False
    assert "compose_source" not in sink
    assert "fallback_reason" not in sink
    assert sink["final_text_transformed"] is True


def test_an_honest_address_reply_is_left_exactly_as_composed():
    """Section 5: the guard removes claims, it does not rewrite replies."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    honest = "وش العنوان اللي تبي نوصل له؟"
    text, sink = _of2_guard(db, tenant, convo, honest)
    assert text == honest
    assert sink["address_save_claim_guard_action"] == "allowed"
    assert sink.get("final_text_transformed") is not True


def test_re_preparing_one_inbound_turn_is_the_same_showing():
    """Observation 7: a redelivery must not become a second live offer.

    A random identity per preparation made two preparations of the SAME
    inbound turn produce two different payloads, so the outbound dedup
    could not see the redelivery as the same message. The identity is
    derived from the showing — scope, turn, page and the exact revisions
    on it — so re-preparing that turn reproduces it, and a genuinely
    different showing still differs.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    meta = {"wa_message_id": "wamid.in-retry", "button_id": "checkout_continue"}

    first = _owner_turn(db, tenant, convo, live=True,
                        missing=("delivery_address",), inbound_metadata=meta)
    second = _owner_turn(db, tenant, convo, live=True,
                         missing=("delivery_address",), inbound_metadata=dict(meta))
    db.commit()

    assert first.address_presentation.offer_id == second.address_presentation.offer_id
    assert (
        _wire_payload(first.address_presentation)
        == _wire_payload(second.address_presentation)
    )

    # A different inbound turn is a different showing.
    other = _owner_turn(db, tenant, convo, live=True, missing=("delivery_address",),
                        inbound_metadata={"wa_message_id": "wamid.in-other",
                                          "button_id": "checkout_continue"})
    assert other.address_presentation.offer_id != first.address_presentation.offer_id

    # And so is the same turn once the inventory underneath it changes.
    upsert_imported_address_candidate(
        db, tenant_id=tenant.id, customer_id=customer.id,
        components=AddressComponents(city="جدة", address_line="طريق الملك"),
        source_ref="ADDR-NEW",
    )
    db.commit()
    changed = _owner_turn(db, tenant, convo, live=True,
                          missing=("delivery_address",), inbound_metadata=dict(meta))
    assert changed.address_presentation.offer_id != first.address_presentation.offer_id
    assert {a.address_id, b.address_id} < {
        c["address_id"] for c in changed.address_presentation.choices
    }


def test_an_action_from_an_expired_showing_approves_nothing():
    """Observation 7: supersession is not the only way a showing ends.

    An offer nobody superseded stayed answerable forever in a
    conversation that simply went quiet — a button tapped weeks later
    still named a live showing. A showing is a question asked in a
    conversation, and it expires the way a question does.
    """
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
        _OFFER_TTL_SECONDS,
        _OFFER_KEY,
        _OFFER_SET_KEY,
    )

    db, _ = _make_db()
    tenant, customer = _seed(db)
    imported = _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    showing = _present(db, tenant, convo, _load_ctx(db, tenant, convo))
    assert showing is not None

    # Age the recorded showing past the window, nothing else changed.
    stale = (
        datetime.now(timezone.utc)
        - timedelta(seconds=_OFFER_TTL_SECONDS + 60)
    ).isoformat()
    meta = dict(convo.extra_metadata or {})
    for key in (_OFFER_KEY, _OFFER_SET_KEY):
        if isinstance(meta.get(key), dict):
            meta[key] = {**meta[key], "offered_at": stale}
    convo.extra_metadata = meta
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    flag_modified(convo, "extra_metadata")
    db.commit()

    assert apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(showing, imported.address_id,
                                       turn_ref="wamid.in-late"),
    ) == {}
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None

    # A fresh showing of the same address is answerable again.
    fresh = _present(db, tenant, convo, _load_ctx(db, tenant, convo),
                     delivery_ref="wamid.out-fresh")
    assert fresh is not None
    patch = apply_structured_address_consent(
        db, tenant_id=tenant.id, conversation=convo, order_prep={},
        inbound_metadata=_consent_meta(fresh, imported.address_id,
                                       turn_ref="wamid.in-fresh"),
    )
    db.commit()
    assert patch.get("customer_confirmed_previous_address") is True


# ── D1–D4: the second closure review's reproductions, as regressions ───


def _rendered_rows(result):
    """What the customer actually sees, whichever surface carried it."""
    from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
        address_choice_actions,
        address_choice_rows,
    )

    if result.address_choice_surface == "list":
        return [
            {"id": r["id"], "title": r["title"], "description": r.get("description", "")}
            for r in address_choice_rows(result.address_presentation)
            if str(r["id"]).startswith("nahla_addr_select:")
        ]
    return [
        {"id": b["reply"]["id"], "title": b["reply"]["title"], "description": ""}
        for b in address_choice_actions(result.address_presentation)
    ]


_LONG_SHARED_STREET = (
    "شارع الملك عبدالعزيز بالقرب من المركز التجاري والحديقة العامة بوابة المجمع السكني "
)


def _addresses(db, tenant, customer, lines, city="الرياض"):
    rows = [
        upsert_imported_address_candidate(
            db, tenant_id=tenant.id, customer_id=customer.id,
            components=AddressComponents(city=city, address_line=line),
            source_ref=f"D1-{index}",
        )
        for index, line in enumerate(lines)
    ]
    db.commit()
    return rows


@pytest.mark.parametrize(
    "lines",
    [
        # The review's case: identical long street, different building.
        [_LONG_SHARED_STREET + "مبنى 11 شقة 1", _LONG_SHARED_STREET + "مبنى 22 شقة 2"],
        # Three, two of which agree further into the string.
        [_LONG_SHARED_STREET + "مبنى 11 شقة 1",
         _LONG_SHARED_STREET + "مبنى 11 شقة 2",
         _LONG_SHARED_STREET + "مبنى 99 شقة 9"],
        # Short and already distinct.
        ["حي النرجس شارع 1", "حي الياسمين شارع 2"],
    ],
)
def test_the_customer_can_tell_the_destinations_apart(lines):
    """D1: distinct row ids are not evidence of an identifiable destination.

    Two addresses on the same long street were labelled from the FRONT of
    the text, so both rows showed the same street and neither building.
    The rows differed to the provider and were indistinguishable to the
    person choosing between them, and both ids became authorized.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    rows = _addresses(db, tenant, customer, lines)
    convo = _conversation(db, tenant, customer)

    result = _owner_turn(db, tenant, convo, live=True, missing=("delivery_address",))
    db.commit()
    shown = _rendered_rows(result)
    assert len(shown) == len(lines)

    rendered = [f"{r['title']}|{r['description']}" for r in shown]
    assert len(set(rendered)) == len(rendered), rendered

    # Each row shows an excerpt of ITS OWN address, not of the shared
    # street, and that excerpt is not what any other row is showing.
    for index, line in enumerate(lines):
        excerpt = (shown[index]["description"] or shown[index]["title"])
        excerpt = excerpt.replace("…", "").strip()
        assert excerpt, rendered
        assert excerpt in line, (excerpt, line)
        for other in range(len(lines)):
            if other == index or lines[other] == line:
                continue
            other_excerpt = (
                shown[other]["description"] or shown[other]["title"]
            ).replace("…", "").strip()
            assert excerpt != other_excerpt, rendered

    # And what is shown is still exactly what is authorized.
    _present(db, tenant, convo, result.address_presentation)
    offered, _ = _offered_revisions(tenant_id=tenant.id, conversation=convo,
                                    offer_id=result.address_presentation.offer_id)
    assert set(offered) == {row.address_id for row in rows}


def test_one_label_scheme_covers_every_row():
    """D1: a row identified only by elimination is not identified.

    Escalating per row left the first choice holding the bare city while
    the second carried a street — distinct, but the customer could only
    work out the first by ruling out the second.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _addresses(db, tenant, customer,
               [_LONG_SHARED_STREET + "مبنى 11", _LONG_SHARED_STREET + "مبنى 22"])
    convo = _conversation(db, tenant, customer)

    result = _owner_turn(db, tenant, convo, live=True, missing=("delivery_address",))
    shown = _rendered_rows(result)
    bare_city = [r for r in shown if r["title"].strip() == "الرياض"]
    assert not bare_city, shown


def _wire_text(db, tenant, convo, reply, turn_ref="wamid.in-guard"):
    from modules.ai.order_flow_v2.outbound_guards import (  # noqa: PLC0415
        apply_order_flow_v2_outbound_guards,
    )

    sink = {}
    text = apply_order_flow_v2_outbound_guards(
        reply, db=db, tenant_id=tenant.id, conversation_id=convo.id,
        conversation=convo, turn_ref=turn_ref, provenance_sink=sink, order_prep={},
    )
    # The literal key, not the module constant: this helper has to run
    # against a tree that does not define it, so the pre-fix failure is
    # behavioural rather than an import error.
    return text, sink, sink.get("address_claim_send_suppressed", False)


def test_malformed_persisted_operation_never_carries_a_claim():
    """D3: unreadable persisted state is not evidence, and must not raise.

    ``address_operation.conversation_id`` stored as a string raised inside
    the reader. The boundary caught it and returned the reply untouched,
    so the unverified claim went to the customer with no guard decision
    recorded at all.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    convo.extra_metadata = {
        **(convo.extra_metadata or {}),
        "address_operation": {"conversation_id": "broken", "turn_ref": "wamid.in-guard"},
    }
    flag_modified(convo, "extra_metadata")
    db.commit()

    text, sink, suppressed = _wire_text(db, tenant, convo, "تم حفظ عنوانك. نكمل الطلب؟")
    assert "حفظ عنوانك" not in text
    assert suppressed is False, "the honest half is still sendable"
    assert sink["address_save_claim_guard_action"] == (
        "blocked_unsupported_address_save_claim"
    )


@pytest.mark.parametrize(
    "target",
    [
        "modules.ai.order_flow_v2.checkout_context.read_turn_address_operation",
        "core.customer_address_persistence_evidence."
        "resolve_customer_address_persistence_evidence",
    ],
)
def test_an_evidence_failure_removes_the_claim_rather_than_trusting_it(target):
    """D3: every failure under the guard proves nothing, not "probably fine"."""
    from unittest.mock import patch as _patch  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    with _patch(target, side_effect=RuntimeError("isolated failure")):
        text, sink, _ = _wire_text(db, tenant, convo, "تم حفظ عنوانك. نكمل الطلب؟")

    assert "حفظ عنوانك" not in text
    assert "نكمل الطلب؟" in text
    assert sink["address_save_claim_guard_action"] == (
        "blocked_unsupported_address_save_claim"
    )


def test_a_guard_failure_suppresses_the_turn_rather_than_sending_the_claim():
    """D3: if nothing can judge the claim, the claim does not go out."""
    from unittest.mock import patch as _patch  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    with _patch(
        "modules.ai.brain.postprocess.customer_address_save_claim_guard"
        ".apply_customer_address_save_claim_guard",
        side_effect=RuntimeError("isolated guard failure"),
    ):
        text, sink, suppressed = _wire_text(db, tenant, convo, "تم حفظ عنوانك. نكمل الطلب؟")

    assert text == ""
    assert suppressed is True
    assert sink["address_save_claim_suppress_reason"] == "guard_unavailable"
    assert sink["llm_candidate_present"] is False


def test_a_reply_with_no_claim_survives_every_failure():
    """D3's other half: fail-closed must not mean fail-silent.

    A reply that asserts nothing has nothing to verify, so an unavailable
    database or guard must leave it exactly as composed.
    """
    from unittest.mock import patch as _patch  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    honest = "وش العنوان اللي تبي نوصل له؟"

    with _patch(
        "modules.ai.order_flow_v2.checkout_context.read_turn_address_operation",
        side_effect=RuntimeError("isolated failure"),
    ):
        text, sink, suppressed = _wire_text(db, tenant, convo, honest)

    assert text == honest
    assert suppressed is False
    assert sink["address_save_claim_guard_action"] == "allowed"


# ── E1–E3: the actual webhook send boundary, not the guard in isolation ──


def _import_blocker(blocked: str):
    """Make exactly one module import raise, leaving every other alone."""
    import builtins  # noqa: PLC0415

    real = builtins.__import__

    def _blocking(name, globals=None, locals=None, fromlist=(), level=0):
        if name == blocked:
            raise ImportError(f"isolated import failure: {blocked}")
        return real(name, globals, locals, fromlist, level)

    return _blocking


def _webhook_send_block():
    """The REAL OrderFlowV2 send block, lifted out of the webhook by AST.

    The guard-unit helpers above cannot prove anything about delivery: a
    failure that stops ``apply_order_flow_v2_outbound_guards`` returning
    is invisible to a test that calls it directly. These probes execute
    the branch that decides whether to send, together with the real
    senders and the real wire sanitizer.
    """
    import ast  # noqa: PLC0415
    import logging as _logging  # noqa: PLC0415

    source = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text()
    tree = ast.parse(source)
    branch = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and ast.unparse(node.test)
        == "_of2_result.handled and _of2_result.reply and _trace.outbound_lock_acquired()"
    )
    senders = [
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in ("_send_interactive_reply", "_send_list_reply",
                          "_send_whatsapp_message")
    ]
    runner = ast.parse("async def _exercise():\n pass").body[0]
    runner.body = [branch]
    module = ast.fix_missing_locations(
        ast.Module(body=senders + [runner], type_ignores=[])
    )
    namespace = {
        "Optional": Optional, "Dict": Dict, "Any": Any,
        "logger": _logging.getLogger("address-send-probe"),
    }
    exec(compile(module, "actual_webhook_send_block", "exec"), namespace)  # noqa: S102
    return namespace


def _run_send_block(db, tenant, convo, result, *, turn_ref="wamid.in-send",
                    message="متابعة", patches=()):
    """Run that block with transport faked and storage captured."""
    import asyncio  # noqa: PLC0415
    from contextlib import ExitStack  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415
    from unittest.mock import patch as _patch  # noqa: PLC0415

    from core.outbound_sanitizer import sanitize_outbound_payload  # noqa: PLC0415
    from modules.ai.brain.persona_ownership import (  # noqa: PLC0415
        PersonaBypassReason,
        PersonaOwnershipRecord,
    )

    namespace = _webhook_send_block()
    sent, saved = [], []

    async def _post(phone_id, payload, **kwargs):
        payload, _ = sanitize_outbound_payload(payload, tenant_id=tenant.id)
        sent.append(payload)
        sink = kwargs.get("_result_sink")
        if isinstance(sink, dict):
            sink.update(wamid=f"wamid.out-{len(sent)}", duplicate_suppressed=False,
                        sent_payload=payload)
        return True

    namespace.update({
        "_trace": SimpleNamespace(outbound_lock_acquired=lambda: True),
        "persist_order_flow_v2_result": lambda *a, **k: None,
        "db": db, "tenant_id": tenant.id, "to": CUSTOMER_PHONE,
        "phone_id": "synthetic-phone-id", "wa_msg_id": turn_ref, "convo": convo,
        "text": message, "_post_wa": _post,
        "_persona_ownership": PersonaOwnershipRecord(),
        "_POReason": PersonaBypassReason,
        "StateManager": SimpleNamespace(
            save_message=lambda *a, **k: saved.append(
                {"text": a[2], "metadata": k.get("extra_metadata") or {}}
            )
        ),
        "_sync_persona_observability": lambda: None,
        "_of2_result": result,
    })
    with ExitStack() as stack:
        for target, kwargs in patches:
            stack.enter_context(_patch(target, **kwargs))
        asyncio.run(namespace["_exercise"]())
    db.commit()
    return sent, saved


def _payload_text(payload):
    interactive = payload.get("interactive") or {}
    if interactive:
        return str((interactive.get("body") or {}).get("text") or "")
    return str((payload.get("text") or {}).get("body") or payload.get("text") or "")


# The REAL composer is used throughout; only the provider adapter is
# replaced. A stub that returns a string cannot expose what the composer
# does on its own failure paths — it catches provider errors internally
# and returns the platform's emergency line rather than raising — so a
# stub would hide exactly the attribution defect these tests exist for.
_COMPOSED_ADDRESS_QUESTION = "وين نوصل طلبك؟ اختر عنوان أو أرسل رابط الموقع."


def _fake_provider(reply_text=_COMPOSED_ADDRESS_QUESTION, fail=False, calls=None):
    """Patches for the two orchestration entry points, nothing else."""
    from types import SimpleNamespace  # noqa: PLC0415

    def _generate(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if fail:
            raise RuntimeError("isolated provider failure")
        return SimpleNamespace(reply_text=reply_text, provider_used="fake", metadata={})

    async def _legacy(**kwargs):
        raise RuntimeError("isolated provider failure")

    return [
        ("modules.ai.orchestrator.adapter.generate_ai_reply", {"side_effect": _generate}),
        ("modules.ai.orchestrator.adapter.generate_orchestrate_response",
         {"side_effect": _legacy}),
    ]


def _compose_calls_counter():
    """Count real composer invocations without replacing the composer."""
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

    counter = {"n": 0}
    original = DefaultComposer.compose

    async def _counting(self, decision, result, ctx):
        counter["n"] += 1
        return await original(self, decision, result, ctx)

    return counter, ("modules.ai.brain.compose.responder.DefaultComposer.compose",
                     {"new": _counting})


def _result_with_reply(reply, reason="isolated-address-claim"):
    from modules.ai.order_flow_v2.owner import OrderFlowV2Result  # noqa: PLC0415

    return OrderFlowV2Result(handled=True, reply=reply, reason=reason)


_GUARD_CANNOT_RUN = [
    ([("modules.ai.order_flow_v2.outbound_guards"
       ".apply_order_flow_v2_outbound_guards",
       {"side_effect": RuntimeError("isolated wrapper failure")})], "invocation"),
    ([("builtins.__import__",
       {"side_effect": _import_blocker(
           "modules.ai.order_flow_v2.outbound_guards")})], "import"),
]


@pytest.mark.parametrize("patches,label", _GUARD_CANNOT_RUN)
def test_an_unchecked_claim_never_reaches_the_wire(patches, label):
    """E1, stated so it can fail on the pre-fix tree without new symbols.

    An import error or a raise on the way in stopped the guard returning,
    the belt's ``except`` logged it, and the ORIGINAL unchecked candidate
    — still sitting in the reply variable — went to the customer. Only
    the existing composer symbol is patched here, so this same test runs
    against a tree that has no recovery module at all.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    sent, saved = _run_send_block(
        db, tenant, convo, _result_with_reply("تم حفظ عنوانك. نكمل الطلب؟"),
        patches=list(patches) + _fake_provider(fail=True),
    )

    assert all("حفظ عنوانك" not in _payload_text(p) for p in sent), (label, sent)
    assert all("حفظ عنوانك" not in m["text"] for m in saved), (label, saved)


@pytest.mark.parametrize("patches,label", _GUARD_CANNOT_RUN)
def test_a_guard_that_cannot_run_never_sends_the_unchecked_reply(patches, label):
    """E1: the refusal inside the guard cannot protect its own absence.

    An import error or a raise on the way in stopped the guard returning,
    the belt's ``except`` logged it, and the ORIGINAL unchecked candidate
    — still sitting in the reply variable — went to the customer with no
    guard decision recorded at all.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    counter, counting = _compose_calls_counter()

    sent, saved = _run_send_block(
        db, tenant, convo, _result_with_reply("تم حفظ عنوانك. نكمل الطلب؟"),
        patches=list(patches) + _fake_provider() + [counting],
    )

    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

    bodies = [_payload_text(p) for p in sent]
    assert all("حفظ عنوانك" not in body for body in bodies), (label, bodies)
    assert all("حفظ عنوانك" not in m["text"] for m in saved), (label, saved)
    # Safety is not enough: the customer's turn is still answered. With
    # the guard itself unavailable nothing can verify fresh wording
    # either, so the approved minimal line speaks — composition was
    # attempted first, and the provenance says which step failed.
    assert bodies, label
    assert counter["n"] == 1, label
    assert is_compose_failure_fallback(bodies[0]), (label, bodies)
    meta = saved[0]["metadata"]
    assert meta["compose_source"] == "fallback_deterministic"
    assert meta["address_claim_compose_attempted"] is True
    assert meta["fallback_reason"] == "address_reply_unverifiable_after_compose"


def test_a_refused_turn_is_answered_by_the_authorized_composer():
    """E2: refusing the claim is right; leaving the turn silent is not."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    counter, counting = _compose_calls_counter()

    sent, saved = _run_send_block(
        db, tenant, convo, _result_with_reply("تم حفظ عنوانك"),
        patches=_fake_provider() + [counting],
    )

    assert counter["n"] == 1
    assert [_payload_text(p) for p in sent] == [_COMPOSED_ADDRESS_QUESTION]
    meta = saved[0]["metadata"]
    assert meta["compose_source"] == "llm"
    assert meta["llm_candidate_present"] is True
    assert meta["address_reply_recovered"] is True
    assert "fallback_reason" not in meta


def test_a_genuine_compose_failure_earns_the_approved_fallback():
    """E2: composition attempted FIRST, then the minimal approved line.

    This is the condition EX-FALLBACK-GENERIC-001 actually describes, and
    the reason the earlier "fallback" on this path was untruthful.
    """
    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    counter, counting = _compose_calls_counter()

    sent, saved = _run_send_block(
        db, tenant, convo, _result_with_reply("تم حفظ عنوانك"),
        patches=_fake_provider(fail=True) + [counting],
    )

    assert counter["n"] == 1
    assert len(sent) == 1
    assert is_compose_failure_fallback(_payload_text(sent[0]))
    meta = saved[0]["metadata"]
    assert meta["compose_source"] == "fallback_deterministic"
    assert meta["response_mode"] == "fallback_deterministic"
    assert meta["fallback_reason"] == "address_reply_compose_failed"
    assert meta["fallback_action_type"] == "order_flow_v2_address_reply"
    assert meta["address_claim_compose_attempted"] is True
    assert meta["llm_candidate_present"] is False


def test_a_recovery_that_restates_the_claim_is_refused_too():
    """E2: a recovery repeating the unsupported claim is not a recovery."""
    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    counter, counting = _compose_calls_counter()

    sent, saved = _run_send_block(
        db, tenant, convo, _result_with_reply("تم حفظ عنوانك"),
        patches=_fake_provider(reply_text="تم حفظ عنوانك") + [counting],
    )

    assert counter["n"] == 1
    assert all("حفظ عنوانك" not in _payload_text(p) for p in sent)
    assert is_compose_failure_fallback(_payload_text(sent[0]))
    meta = saved[0]["metadata"]
    assert meta["fallback_reason"] == "address_reply_unsupported_after_compose"
    # A candidate DID exist this time, and the metadata says so.
    assert meta["llm_candidate_present"] is True


def test_a_claimless_question_is_delivered_untouched_through_the_send_block():
    """E1/E3: fail-closed must not become fail-silent for honest text."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    honest = "وش العنوان اللي تبي نوصل له؟"

    sent, saved = _run_send_block(
        db, tenant, convo, _result_with_reply(honest),
        patches=[(
            "modules.ai.order_flow_v2.checkout_context.read_turn_address_operation",
            {"side_effect": RuntimeError("isolated failure")},
        )],
    )

    assert [_payload_text(p) for p in sent] == [honest]
    assert saved[0]["text"] == honest
    assert saved[0]["metadata"].get("address_reply_recovered") is not True


def test_a_detector_failure_recovers_rather_than_dropping_the_turn():
    """E2: "cannot decide" must still answer the customer."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    sent, saved = _run_send_block(
        db, tenant, convo, _result_with_reply("وش العنوان اللي تبي نوصل له؟"),
        patches=[
            ("modules.ai.brain.postprocess.customer_address_save_claim_guard"
             ".detect_address_save_claim_kinds",
             {"side_effect": RuntimeError("isolated detector failure")}),
        ] + _fake_provider(),
    )

    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

    assert sent, "a turn that cannot be judged still has to be answered"
    # The detector is broken for the recovery too, so the fresh wording
    # cannot be verified either — the approved line speaks rather than
    # unverified text, and the turn is not dropped.
    assert is_compose_failure_fallback(_payload_text(sent[0]))
    meta = saved[0]["metadata"]
    assert meta["address_reply_recovered"] is True
    assert meta["address_claim_compose_attempted"] is True
    assert meta["fallback_reason"] == "address_reply_unverifiable_after_compose"


def test_a_guarded_address_showing_still_reaches_the_wire_with_its_receipt():
    """E3: the recovery path must not disturb the ordinary showing."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)

    result = _owner_turn(db, tenant, convo, live=True, missing=("delivery_address",),
                         inbound_metadata={"wa_message_id": "wamid.in-show",
                                           "button_id": "checkout_continue"})
    sent, saved = _run_send_block(db, tenant, convo, result, turn_ref="wamid.in-show")

    assert len(sent) == 1
    delivered = delivered_address_action_ids(sent[0])
    offered, _ = _offered_revisions(tenant_id=tenant.id, conversation=convo,
                                    offer_id=result.address_presentation.offer_id)
    assert {structured_consent_action({"button_id": i})[1] for i in delivered} == set(offered)
    assert set(offered) == {a.address_id, b.address_id}
    assert saved[0]["metadata"].get("address_reply_recovered") is not True


# ── F1–F3: the real composer, its provenance, and the ordinary turn ────


def test_the_checkout_facts_actually_reach_the_model():
    """F1: a key in ``result.data`` is not delivery to the model.

    The facts were handed over only as ``ActionResult.data['trusted_facts']``,
    which the composer does not serialize. It logged the missing
    ``reply_state``, built a minimal discovery-stage one, and the city,
    the short address and the customer's name never left this process. A
    reply that happens to ask for an address is not a reply grounded in
    the address state we hold.
    """
    sentinels = {
        "city": "CITY_SENTINEL_782",
        "short_address": "ABCD1234",
        "full_name": "CUSTOMER_SENTINEL_827",
        "product_name": "PRODUCT_SENTINEL_653",
    }
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    convo.extra_metadata = {
        **(convo.extra_metadata or {}),
        "brain_state": {"stage": "checkout", "order_prep": dict(sentinels)},
    }
    from sqlalchemy.orm.attributes import flag_modified as _fm  # noqa: PLC0415

    _fm(convo, "extra_metadata")
    db.commit()

    seen = []
    _run_send_block(
        db, tenant, convo, _result_with_reply("تم حفظ عنوانك"),
        patches=_fake_provider(calls=seen),
    )

    assert seen, "the provider was never reached"
    import json as _json  # noqa: PLC0415

    blob = _json.dumps(seen, ensure_ascii=False, default=str)
    missing = [name for name, value in sentinels.items() if value not in blob]
    assert not missing, missing


def test_the_composers_own_fallback_is_not_recorded_as_a_model_candidate():
    """F2: a non-empty return from ``compose`` is not proof of authorship.

    ``DefaultComposer`` catches orchestration failures itself and returns
    the platform's generic emergency line rather than raising. Treating
    every non-empty string as an LLM candidate stamped that line
    ``compose_source=llm`` with no ``fallback_reason`` — the same false
    provenance class, reintroduced at the newly wired boundary. A stub
    that raises cannot expose this; only the real composer can.
    """
    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    sent, saved = _run_send_block(
        db, tenant, convo, _result_with_reply("تم حفظ عنوانك"),
        patches=_fake_provider(fail=True),
    )

    assert is_compose_failure_fallback(_payload_text(sent[0]))
    meta = saved[0]["metadata"]
    assert meta["compose_source"] == "fallback_deterministic"
    assert meta["response_mode"] == "fallback_deterministic"
    assert meta["final_customer_text_source"] == "fallback_deterministic"
    assert meta["llm_candidate_present"] is False
    assert meta["fallback_reason"] == "address_reply_compose_failed"
    assert meta["fallback_action_type"] == "order_flow_v2_address_reply"


def test_an_empty_model_answer_is_a_compose_failure_not_a_candidate():
    """F2: an empty generation is a failure, and is recorded as one."""
    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)

    sent, saved = _run_send_block(
        db, tenant, convo, _result_with_reply("تم حفظ عنوانك"),
        patches=_fake_provider(reply_text=""),
    )

    assert is_compose_failure_fallback(_payload_text(sent[0]))
    meta = saved[0]["metadata"]
    assert meta["compose_source"] == "fallback_deterministic"
    assert meta["llm_candidate_present"] is False


def test_the_ordinary_address_showing_is_composed_not_written():
    """F3: the normal turn, not only the refused one.

    Asking the customer where to deliver is a clarification, and
    AGENTS.md assigns that wording to the composer. The structured part
    of the turn stays platform-owned: the action ids, the labels, the
    paging and the receipt are unchanged, and the receipt still equals
    what went on the wire.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    counter, counting = _compose_calls_counter()

    result = _owner_turn(db, tenant, convo, live=True, missing=("delivery_address",),
                         inbound_metadata={"wa_message_id": "wamid.in-normal",
                                           "button_id": "checkout_continue"})
    sent, saved = _run_send_block(
        db, tenant, convo, result, turn_ref="wamid.in-normal",
        patches=_fake_provider() + [counting],
    )

    assert counter["n"] == 1, "the ordinary address turn must be composed"
    assert _payload_text(sent[0]) == _COMPOSED_ADDRESS_QUESTION
    meta = saved[0]["metadata"]
    assert meta["compose_source"] == "llm"
    assert meta["llm_candidate_present"] is True
    assert meta["address_reply_composed"] is True

    # Platform-owned structure survives untouched.
    delivered = delivered_address_action_ids(sent[0])
    offered, _ = _offered_revisions(tenant_id=tenant.id, conversation=convo,
                                    offer_id=result.address_presentation.offer_id)
    assert {structured_consent_action({"button_id": i})[1] for i in delivered} == set(offered)
    assert set(offered) == {a.address_id, b.address_id}


def test_a_composed_address_turn_that_claims_a_save_is_still_refused():
    """F3: composition does not exempt the composer from the guard."""
    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)

    result = _owner_turn(db, tenant, convo, live=True, missing=("delivery_address",),
                         inbound_metadata={"wa_message_id": "wamid.in-claim",
                                           "button_id": "checkout_continue"})
    sent, saved = _run_send_block(
        db, tenant, convo, result, turn_ref="wamid.in-claim",
        patches=_fake_provider(reply_text="تم حفظ عنوانك"),
    )

    assert all("حفظ عنوانك" not in _payload_text(p) for p in sent), sent
    assert is_compose_failure_fallback(_payload_text(sent[0]))


def test_a_non_address_turn_is_left_to_its_existing_owner():
    """F3 stays bounded: only the address paths move to composition."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _import(db, tenant, customer, _payload())
    db.commit()
    convo = _conversation(db, tenant, customer)
    counter, counting = _compose_calls_counter()

    result = _owner_turn(db, tenant, convo, live=True, missing=("customer_name",))
    assert "اسمك" in (result.reply or ""), "the probe must exercise the name question"
    sent, saved = _run_send_block(
        db, tenant, convo, result, turn_ref="wamid.in-name",
        patches=_fake_provider() + [counting],
    )

    assert counter["n"] == 0
    # Still the owner's own text (the sanitizer only reflows whitespace).
    assert "اسمك الكامل" in _payload_text(sent[0])
    assert _payload_text(sent[0]) != _COMPOSED_ADDRESS_QUESTION
    assert saved[0]["metadata"].get("address_reply_composed") is not True


# ── G1–G3: the composer's own outcome, the entry boundary, and the field ──


def _model_state(seen):
    """The brain state the composer actually bound into the model call."""
    if not seen:
        return {}
    return dict((seen[0].get("context_metadata") or {}).get("brain_state") or {})


def _address_turn(db, tenant, convo, *, field="delivery_address"):
    """A real owner turn collecting one address field, with facts on file."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    result = _owner_turn(db, tenant, convo, live=True, missing=(field,))
    prep = {"city": "الرياض", "short_address": "RRRD1234", **result.state_patch}
    if field == "city":
        prep.pop("city", None)
        prep["missing_fields"] = ["city"]
        prep["google_maps_url"] = "https://maps.google.com/?q=24.7136,46.6753"
        prep["delivery_address_status"] = "accepted"
    convo.extra_metadata = {
        **(convo.extra_metadata or {}),
        "brain_state": {"stage": "checkout", "order_prep": prep},
    }
    flag_modified(convo, "extra_metadata")
    db.commit()
    return result


def test_an_adapter_timeout_is_never_recorded_as_a_model_candidate():
    """G1: ``text_source`` is INFERRED from entering the LLM path.

    The composer's internal timeout branch returns a fixed sentence and
    records ``chosen_path=llm_timeout`` without ever producing a
    candidate, so an inferred ``llm`` read attributed platform wording to
    the model. Authorship now comes from the candidate the producer
    itself recorded, which that branch never writes.
    """
    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    result = _address_turn(db, tenant, convo)

    seen = []

    def _time_out(**kwargs):
        seen.append(kwargs)
        raise TimeoutError("isolated adapter timeout")

    patches = _fake_provider()
    patches[0] = (patches[0][0], {"side_effect": _time_out})
    sent, saved = _run_send_block(db, tenant, convo, result, patches=patches)

    assert len(seen) == 1, "the adapter must actually have been reached"
    body = _payload_text(sent[0])
    assert "تأخّر الرد" not in body, body
    assert is_compose_failure_fallback(body)
    meta = saved[0]["metadata"]
    assert meta["compose_source"] == "fallback_deterministic"
    assert meta["llm_candidate_present"] is False
    assert meta["fallback_reason"] == "address_reply_compose_failed"
    # Generation WAS attempted here — that is the difference from G2.
    assert meta["address_claim_compose_attempted"] is True


@pytest.mark.parametrize(
    "extra,label",
    [
        ([("modules.ai.order_flow_v2.address_reply_recovery"
           ".compose_address_turn_reply",
           {"side_effect": RuntimeError("isolated entry error")})], "invocation"),
        ([("builtins.__import__",
           {"side_effect": _import_blocker(
               "modules.ai.order_flow_v2.address_reply_recovery")})], "import"),
    ],
)
def test_compose_that_cannot_be_entered_never_revives_the_old_prose(extra, label):
    """G2: the ordinary address body is no longer this path's to write.

    The webhook initialised the reply to the owner's deterministic text
    and left it untouched when the compose block raised, so an import or
    entry failure quietly restored the prose ownership this path had just
    given up — with no compose provenance at all.
    """
    from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

    db, _ = _make_db()
    tenant, customer = _seed(db)
    a, b = _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    result = _address_turn(db, tenant, convo)

    seen = []
    sent, saved = _run_send_block(
        db, tenant, convo, result, patches=_fake_provider(calls=seen) + list(extra),
    )

    assert not seen, (label, "nothing may claim generation was attempted")
    body = _payload_text(sent[0])
    assert "شاركنا عنوان التوصيل" not in body, (label, body)
    assert is_compose_failure_fallback(body), (label, body)
    meta = saved[0]["metadata"]
    assert meta["compose_source"] == "fallback_deterministic"
    assert meta["fallback_reason"] == "address_reply_compose_unavailable"
    assert meta["address_claim_compose_attempted"] is False
    assert meta["llm_candidate_present"] is False

    # Continuity: the customer can still choose from the saved addresses.
    delivered = delivered_address_action_ids(sent[0])
    offered, _ = _offered_revisions(tenant_id=tenant.id, conversation=convo,
                                    offer_id=result.address_presentation.offer_id)
    assert {structured_consent_action({"button_id": i})[1] for i in delivered} == set(offered)
    assert set(offered) == {a.address_id, b.address_id}


def test_a_city_turn_is_not_projected_as_a_missing_delivery_address():
    """G3: the turn's real collection field reaches the composer.

    A city question was handed ``missing_field=delivery_address`` beside
    an accepted address and its map link — two contradictory operational
    facts, supplied before anything was generated.
    """
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    result = _address_turn(db, tenant, convo, field="city")
    assert result.state_patch.get("order_flow_v2_last_field") == "city"

    seen = []
    _run_send_block(db, tenant, convo, result, patches=_fake_provider(calls=seen))

    state = _model_state(seen)
    known = dict(state.get("known_facts") or {})
    assert known.get("missing_field") == "city"
    assert state.get("response_goal") == "collect_delivery_city"
    # The address already on file is still supplied, not erased.
    assert known.get("delivery_address_status") == "accepted"


def test_a_delivery_address_turn_keeps_its_own_field_and_goal():
    """G3's other half: the delivery-address turn is unchanged."""
    db, _ = _make_db()
    tenant, customer = _seed(db)
    _two_candidates(db, tenant, customer)
    convo = _conversation(db, tenant, customer)
    result = _address_turn(db, tenant, convo)

    seen = []
    _run_send_block(db, tenant, convo, result, patches=_fake_provider(calls=seen))

    state = _model_state(seen)
    assert dict(state.get("known_facts") or {}).get("missing_field") == "delivery_address"
    assert state.get("response_goal") == "collect_delivery_address"
