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
from typing import Any, Tuple

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
    apply_delivery_continuation_address_patch,
    apply_explicit_address_selection,
    apply_previous_address_confirmation,
    load_checkout_reply_context,
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


def _save_attempt(tenant, customer, result, operation=AddressOperation.SAVE_CANDIDATE):
    return AddressOperationAttempt(
        operation=operation, tenant_id=tenant.id, customer_id=customer.id,
        address_id=result.address_id, fingerprint=result.fingerprint,
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
        fingerprint=imported.fingerprint,
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


def test_pipeline_suppresses_the_send_when_nothing_truthful_remains():
    from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: PLC0415
        run_post_compose_truth_guards,
    )

    db, _ = _make_db()
    tenant, customer = _seed(db)
    convo = _conversation(db, tenant, customer)
    result = run_post_compose_truth_guards(
        db=db, tenant_id=tenant.id, to=CUSTOMER_PHONE, text="وين توصلون؟",
        reply="تم حفظ عنوانك واعتماده للتوصيل يا تركي", convo=convo,
        inbound_metadata={}, brain_handoff=False, brain_nc_block=False,
        brain_nc_category="", br_action="", brain_persona_compose_event=None,
        mode="primary", conversation_id=convo.id,
    )
    event = next(
        e for e in result.events if e.guard == "customer_address_save_claim_guard"
    )
    assert event.modified is True
    # Audited, not silent: the send is suppressed rather than delivering an
    # empty string or restoring the unsupported claim.
    assert event.suppressed_send is True
    assert "scrubbed_empty" in (event.reason or "")
    assert result.reply.strip() == ""


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


def _offer_then_confirm(db, tenant, customer, convo, message="نفس العنوان السابق"):
    """The real lifecycle: the address is OFFERED, then confirmed."""
    load_checkout_reply_context(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={}, brain_state={},
    )
    db.commit()
    patch = apply_previous_address_confirmation(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={}, message=message,
    )
    db.commit()
    return patch


def test_confirmation_path_records_the_selection_exactly_once():
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

    for _ in range(2):
        patch = _offer_then_confirm(db, tenant, customer, convo)
        assert patch.get("customer_confirmed_previous_address") is True

    rows = db.query(CustomerAddressProvenance).all()
    assert len(rows) == 1
    assert rows[0].selection_state == "selected"
    assert db.query(CustomerAddress).count() == 1


def test_confirmation_without_a_prior_offer_selects_nothing():
    """Consent is consent about an offer. An inquiry is not an offer."""
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

    patch = apply_previous_address_confirmation(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={},
        message="هل عنواني محفوظ عندكم؟",
    )
    db.commit()
    assert patch == {}
    assert resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    ).selected is None


def test_confirmation_refuses_a_revision_the_customer_never_saw():
    """A refresh between the offer and the reply invalidates the consent."""
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
    load_checkout_reply_context(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={}, brain_state={},
    )
    db.commit()

    row = db.query(CustomerAddress).filter_by(id=imported.address_id).one()
    row.address_text = "شارع لم يره العميل"
    db.add(row)
    db.commit()

    patch = apply_previous_address_confirmation(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={},
        message="نفس العنوان السابق",
    )
    db.commit()
    assert patch == {}
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

    reply_ctx = load_checkout_reply_context(
        db, tenant_id=tenant.id, conversation=convo,
        customer_phone=CUSTOMER_PHONE, order_prep={}, brain_state={},
    )
    db.commit()
    assert reply_ctx.known_previous == {}
    assert {c["address_id"] for c in reply_ctx.address_choices} == {
        a.address_id, b.address_id
    }

    patch = apply_explicit_address_selection(
        db, tenant_id=tenant.id, conversation=convo, address_id=b.address_id,
        order_prep={},
    )
    db.commit()
    assert patch["city"] == "جدة"
    assert patch["customer_confirmed_previous_address"] is True
    resolution = resolve_customer_address_selection(
        db, tenant_id=tenant.id, customer_id=customer.id,
    )
    assert resolution.selected.address_id == b.address_id


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
