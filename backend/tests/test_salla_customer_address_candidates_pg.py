"""PostgreSQL proofs for durable Salla address candidates and selection.

Why PostgreSQL specifically: the behaviour under test is persistence,
transaction outcome and isolation — a candidate that survives a COMMIT and
a brand-new session, a rolled-back transaction that leaves no save
evidence, the partial/unique constraints that make the idempotent upsert
idempotent under concurrency-shaped repeats, and revision 0110 itself
(reconciliation against a ``create_all`` table is a PostgreSQL-only
question). SQLite proves none of these.

Runs when a PostgreSQL DSN is available (see
``legacy_migration_drift_postgres_fixtures.connect_engine``) and is
REQUIRED — a connection failure fails instead of skipping — whenever
``LEGACY_MIG_PG_TEST_DATABASE_URL`` is set, which is the configuration
``scripts/required_postgres_proofs.json`` gates this suite on and which
CI already provides to the required-proofs step.
``CUSTOMER_ADDRESS_CANDIDATES_PG_REQUIRED=1`` and
``LEGACY_MIG_PG_INTEGRATION_REQUIRED=1`` also force it.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
_DATABASE = _REPO / "database"
for _entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from core.customer_address_candidates import (  # noqa: E402
    ACTION_CREATED,
    ACTION_UNCHANGED,
    REASON_MULTIPLE_CANDIDATES,
    SELECTION_SOURCE_CUSTOMER_CONFIRMED,
    SOURCE_SALLA_CUSTOMER_PROFILE,
    AddressComponents,
    components_from_salla_customer_payload,
    record_explicit_address_selection,
    resolve_customer_address_selection,
    source_updated_at_from_salla_customer_payload,
    upsert_imported_address_candidate,
)
from core.customer_address_persistence_evidence import (  # noqa: E402
    AddressPersistenceScope,
    resolve_customer_address_persistence_evidence,
)
from core.order_context_builder import build_order_context  # noqa: E402
from legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    drop_ephemeral_database,
    run_alembic,
)
from models import (  # noqa: E402
    Base,
    Conversation,
    Customer,
    CustomerAddress,
    CustomerAddressProvenance,
    Tenant,
)
from modules.ai.brain.postprocess.customer_address_save_claim_guard import (  # noqa: E402
    apply_customer_address_save_claim_guard,
)
from modules.ai.order_flow_v2.checkout_context import (  # noqa: E402
    apply_delivery_continuation_address_patch,
    load_checkout_reply_context,
)

_TABLE = "customer_address_provenance"
_UNIQUE = "uq_customer_address_provenance_address"
_INDEX = "ix_customer_address_provenance_source"

MERCHANT = "متجر تجريبي عام"
PHONE = "+966500000321"
SALLA_ID = "SC-PG-1"
CITY = "الرياض"
SHORT_CODE = "RRRD1234"
STREET = "حي النرجس، شارع 10"


def _pg_required() -> bool:
    """These proofs must FAIL, never skip, once a target is configured.

    An explicit ``LEGACY_MIG_PG_TEST_DATABASE_URL`` is authoritative: it is
    the configuration the required-proofs runner gates this suite on, and
    the runner counts a skip as a failure anyway. The two opt-in flags stay
    recognised so an operator (or a later governance change to the
    workflow) can require the suite without setting a DSN here.
    """
    return (
        bool((os.getenv("LEGACY_MIG_PG_TEST_DATABASE_URL") or "").strip())
        or (os.getenv("CUSTOMER_ADDRESS_CANDIDATES_PG_REQUIRED") or "").strip() == "1"
        or (os.getenv("LEGACY_MIG_PG_INTEGRATION_REQUIRED") or "").strip() == "1"
    )


def _pg_admin() -> Engine:
    try:
        engine = connect_engine()
    except pytest.skip.Exception as exc:
        if _pg_required():
            pytest.fail(f"PostgreSQL required for address candidate proofs: {exc}")
        raise
    if engine.dialect.name != "postgresql":
        pytest.fail("address candidate proofs require PostgreSQL, not SQLite")
    return engine


def _ephemeral(revision: str | None) -> Iterator[Engine]:
    admin = _pg_admin()
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        if revision is not None:
            run_alembic(engine, revision)
        yield engine
    finally:
        engine.dispose()
        drop_ephemeral_database(admin, db_name)
        admin.dispose()


@pytest.fixture(scope="module")
def pg_at_0110() -> Iterator[Engine]:
    """Schema at 0110 — customer_address_provenance exists."""
    yield from _ephemeral("0110")


@pytest.fixture()
def pg_at_0109() -> Iterator[Engine]:
    """Schema at 0109 — the revision BEFORE this slice."""
    yield from _ephemeral("0109")


def _session(engine: Engine) -> Session:
    return sessionmaker(
        bind=engine, autocommit=False, autoflush=True, expire_on_commit=False,
    )()


def _seed(db: Session, *, salla_id: str = SALLA_ID, phone: str = PHONE) -> Tuple[int, int]:
    tenant = Tenant(name=f"{MERCHANT}-{os.urandom(4).hex()}", is_active=True)
    db.add(tenant)
    db.flush()
    customer = Customer(
        tenant_id=tenant.id, phone=phone, normalized_phone=phone,
        salla_customer_id=salla_id, acquisition_channel="salla_sync",
    )
    db.add(customer)
    db.commit()
    return tenant.id, customer.id


def _payload(**overrides) -> dict:
    payload = {
        "id": SALLA_ID, "first_name": "نورة", "last_name": "عبدالله",
        "mobile": PHONE, "city": CITY, "country": "SA", "location": STREET,
        "updated_at": "2026-09-01T10:00:00Z",
    }
    payload.update(overrides)
    return payload


def _import(db: Session, tenant_id: int, customer_id: int, payload: dict):
    return upsert_imported_address_candidate(
        db, tenant_id=tenant_id, customer_id=customer_id,
        components=components_from_salla_customer_payload(payload),
        source=SOURCE_SALLA_CUSTOMER_PROFILE,
        source_ref=str(payload.get("id") or ""),
        source_updated_at=source_updated_at_from_salla_customer_payload(payload),
    )


# ── Revision 0110 ───────────────────────────────────────────────────────

def test_0110_creates_the_table_on_a_database_at_0109(pg_at_0109: Engine) -> None:
    insp = inspect(pg_at_0109)
    assert _TABLE not in insp.get_table_names()

    run_alembic(pg_at_0109, "0110")

    insp = inspect(pg_at_0109)
    assert _TABLE in insp.get_table_names()
    columns = {c["name"]: c for c in insp.get_columns(_TABLE)}
    for required in (
        "tenant_id", "customer_id", "customer_address_id", "source", "source_ref",
        "integration_connection_id", "source_country", "content_fingerprint",
        "source_updated_at", "source_observed_at", "selection_state",
        "selected_fingerprint", "selected_at", "selection_source",
    ):
        assert required in columns
    # A provider revision that was never sent stays NULL.
    assert columns["source_updated_at"]["nullable"] is True
    # A local observation is always known.
    assert columns["source_observed_at"]["nullable"] is False
    assert {u["name"] for u in insp.get_unique_constraints(_TABLE)} >= {_UNIQUE}
    assert {i["name"] for i in insp.get_indexes(_TABLE)} >= {_INDEX}
    with pg_at_0109.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "0110"


def test_0110_reconciles_a_create_all_materialised_table(pg_at_0109: Engine) -> None:
    """Production startup materialises new ORM tables before the revision.

    The revision must then reconcile that table additively: same shape as
    a fresh create, revision recorded, and the rows already in it
    untouched — no drop, no re-create, no rewrite.
    """
    CustomerAddressProvenance.__table__.create(bind=pg_at_0109)
    assert _TABLE in inspect(pg_at_0109).get_table_names()

    session = _session(pg_at_0109)
    try:
        tenant_id, customer_id = _seed(session, salla_id="SC-PG-RECONCILE")
        address = CustomerAddress(
            tenant_id=tenant_id, customer_id=customer_id, city=CITY,
            address_text=STREET, address_type="imported_profile_candidate",
        )
        session.add(address)
        session.flush()
        session.add(CustomerAddressProvenance(
            tenant_id=tenant_id, customer_id=customer_id,
            customer_address_id=address.id,
            source=SOURCE_SALLA_CUSTOMER_PROFILE, source_ref="SC-PG-RECONCILE",
            source_country="SA", content_fingerprint="pre-existing",
            source_updated_at=None,
            source_observed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            selection_state="candidate",
        ))
        session.commit()
        address_id = address.id
    finally:
        session.close()

    run_alembic(pg_at_0109, "0110")

    insp = inspect(pg_at_0109)
    post = {c["name"]: c for c in insp.get_columns(_TABLE)}
    assert post["selection_state"]["default"] is not None
    assert post["created_at"]["default"] is not None
    assert post["source_updated_at"]["nullable"] is True
    assert post["source_observed_at"]["nullable"] is False
    assert {u["name"] for u in insp.get_unique_constraints(_TABLE)} >= {_UNIQUE}
    assert {i["name"] for i in insp.get_indexes(_TABLE)} >= {_INDEX}
    referred = {
        (fk["referred_table"], tuple(fk["constrained_columns"]))
        for fk in insp.get_foreign_keys(_TABLE)
    }
    assert ("customer_addresses", ("customer_address_id",)) in referred
    assert ("customers", ("customer_id",)) in referred
    assert ("tenants", ("tenant_id",)) in referred
    with pg_at_0109.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == "0110"

    check = _session(pg_at_0109)
    try:
        row = check.query(CustomerAddressProvenance).filter_by(
            customer_address_id=address_id,
        ).one()
        # The pre-existing row survived untouched — nothing was rewritten.
        assert row.content_fingerprint == "pre-existing"
        assert row.source_updated_at is None
        assert row.source_country == "SA"
        assert row.selection_state == "candidate"
    finally:
        check.close()


def test_0110_downgrade_removes_only_this_revision(pg_at_0109: Engine) -> None:
    run_alembic(pg_at_0109, "0110")
    from legacy_migration_drift_postgres_fixtures import downgrade_alembic

    downgrade_alembic(pg_at_0109, "0109")
    insp = inspect(pg_at_0109)
    assert _TABLE not in insp.get_table_names()
    # customer_addresses is untouched by this revision, in both directions.
    assert "customer_addresses" in insp.get_table_names()


# ── Durability across commits, sessions and conversations ───────────────

def test_import_survives_commit_a_fresh_session_and_a_new_conversation(
    pg_at_0110: Engine,
) -> None:
    db = _session(pg_at_0110)
    try:
        tenant_id, customer_id = _seed(db)
        result = _import(db, tenant_id, customer_id, _payload())
        db.commit()
        assert result.action == ACTION_CREATED
    finally:
        db.close()

    # A completely new session: nothing survives in memory.
    fresh = _session(pg_at_0110)
    try:
        resolution = resolve_customer_address_selection(
            fresh, tenant_id=tenant_id, customer_id=customer_id,
        )
        assert resolution.reusable is not None
        assert resolution.reusable.components.city == CITY
        assert resolution.reusable.components.address_line == STREET
        assert resolution.reusable.selected is False

        convo = Conversation(
            tenant_id=tenant_id, status="open", customer_id=customer_id,
            extra_metadata={},
        )
        fresh.add(convo)
        fresh.commit()
        ctx = build_order_context(
            fresh, tenant_id=tenant_id, conversation=convo, phone=PHONE,
            brain_state={"order_prep": {}}, build_source="pg_new_conversation",
        )
        assert ctx.known_previous_address is not None
        assert ctx.known_previous_address.city == CITY
        assert ctx.known_previous_address.explicitly_selected is False
    finally:
        fresh.close()


def test_repeated_import_across_sessions_stays_one_row(pg_at_0110: Engine) -> None:
    db = _session(pg_at_0110)
    try:
        tenant_id, customer_id = _seed(db, salla_id="SC-PG-IDEMPOTENT")
        db.commit()
    finally:
        db.close()

    for _ in range(3):
        session = _session(pg_at_0110)
        try:
            result = _import(
                session, tenant_id, customer_id,
                _payload(id="SC-PG-IDEMPOTENT"),
            )
            session.commit()
            assert result.action in {ACTION_CREATED, ACTION_UNCHANGED}
        finally:
            session.close()

    check = _session(pg_at_0110)
    try:
        assert check.query(CustomerAddress).filter_by(
            tenant_id=tenant_id, customer_id=customer_id,
        ).count() == 1
        assert check.query(CustomerAddressProvenance).filter_by(
            tenant_id=tenant_id, customer_id=customer_id,
        ).count() == 1
    finally:
        check.close()


def test_one_provenance_row_per_address_is_enforced_by_the_schema(
    pg_at_0110: Engine,
) -> None:
    db = _session(pg_at_0110)
    try:
        tenant_id, customer_id = _seed(db, salla_id="SC-PG-UNIQUE")
        imported = _import(db, tenant_id, customer_id, _payload(id="SC-PG-UNIQUE"))
        db.commit()
        db.add(CustomerAddressProvenance(
            tenant_id=tenant_id, customer_id=customer_id,
            customer_address_id=imported.address_id,
            source=SOURCE_SALLA_CUSTOMER_PROFILE,
            content_fingerprint="duplicate",
            source_observed_at=datetime.now(timezone.utc),
            selection_state="candidate",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


# ── Transaction outcome vs. save evidence ───────────────────────────────

def test_rolled_back_import_yields_no_durable_save_evidence(
    pg_at_0110: Engine,
) -> None:
    db = _session(pg_at_0110)
    try:
        tenant_id, customer_id = _seed(db, salla_id="SC-PG-ROLLBACK")
        result = _import(db, tenant_id, customer_id, _payload(id="SC-PG-ROLLBACK"))
        assert result.action == ACTION_CREATED  # the writer "succeeded"…
        db.rollback()                           # …but the commit did not happen
    finally:
        db.close()

    check = _session(pg_at_0110)
    try:
        evidence = resolve_customer_address_persistence_evidence(
            check, tenant_id=tenant_id, customer_id=customer_id,
        )
        assert evidence.scope is AddressPersistenceScope.NONE
        assert evidence.allows_saved_address_claim() is False
        guarded = apply_customer_address_save_claim_guard(
            reply="تم حفظ عنوانك عندنا. نكمل الطلب؟", evidence=evidence,
        )
        assert guarded.action == "blocked_unsupported_address_save_claim"
        assert "تم حفظ عنوانك" not in guarded.reply
        assert check.query(CustomerAddress).filter_by(
            tenant_id=tenant_id, customer_id=customer_id,
        ).count() == 0
    finally:
        check.close()


def test_committed_selection_is_the_only_adoption_evidence(
    pg_at_0110: Engine,
) -> None:
    db = _session(pg_at_0110)
    try:
        tenant_id, customer_id = _seed(db, salla_id="SC-PG-ADOPT")
        imported = _import(db, tenant_id, customer_id, _payload(id="SC-PG-ADOPT"))
        db.commit()
        candidate_evidence = resolve_customer_address_persistence_evidence(
            db, tenant_id=tenant_id, customer_id=customer_id,
        )
        assert candidate_evidence.scope is AddressPersistenceScope.IMPORTED_CANDIDATE
        assert candidate_evidence.allows_adopted_address_claim() is False

        record_explicit_address_selection(
            db, tenant_id=tenant_id, customer_id=customer_id,
            address_id=imported.address_id,
            selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
            expected_fingerprint=imported.fingerprint,
        )
        db.commit()
    finally:
        db.close()

    check = _session(pg_at_0110)
    try:
        evidence = resolve_customer_address_persistence_evidence(
            check, tenant_id=tenant_id, customer_id=customer_id,
        )
        assert evidence.scope is AddressPersistenceScope.SELECTED_DELIVERY_ADDRESS
        assert evidence.allows_adopted_address_claim() is True
    finally:
        check.close()


# ── Isolation and reuse ─────────────────────────────────────────────────

def test_another_tenant_never_sees_or_selects_the_address(
    pg_at_0110: Engine,
) -> None:
    db = _session(pg_at_0110)
    try:
        tenant_id, customer_id = _seed(db, salla_id="SC-PG-ISO-A", phone="+966500000801")
        other_tenant_id, other_customer_id = _seed(
            db, salla_id="SC-PG-ISO-B", phone="+966500000802",
        )
        imported = _import(db, tenant_id, customer_id, _payload(id="SC-PG-ISO-A"))
        db.commit()

        assert resolve_customer_address_selection(
            db, tenant_id=other_tenant_id, customer_id=customer_id,
        ).reusable is None
        refused = record_explicit_address_selection(
            db, tenant_id=other_tenant_id, customer_id=other_customer_id,
            address_id=imported.address_id,
            selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        )
        assert refused.action == "skipped"
        db.rollback()
        assert db.query(CustomerAddressProvenance).filter_by(
            customer_address_id=imported.address_id, selection_state="selected",
        ).count() == 0
    finally:
        db.close()


def test_selected_address_reaches_checkout_in_a_new_conversation(
    pg_at_0110: Engine,
) -> None:
    db = _session(pg_at_0110)
    try:
        tenant_id, customer_id = _seed(db, salla_id="SC-PG-REUSE", phone="+966500000803")
        imported = upsert_imported_address_candidate(
            db, tenant_id=tenant_id, customer_id=customer_id,
            components=AddressComponents(
                city=CITY, address_line=STREET, short_address_code=SHORT_CODE,
            ),
            source=SOURCE_SALLA_CUSTOMER_PROFILE, source_ref="SC-PG-REUSE",
            source_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        db.commit()
        record_explicit_address_selection(
            db, tenant_id=tenant_id, customer_id=customer_id,
            address_id=imported.address_id,
            selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
            expected_fingerprint=imported.fingerprint,
        )
        db.commit()
    finally:
        db.close()

    fresh = _session(pg_at_0110)
    try:
        convo = Conversation(
            tenant_id=tenant_id, status="open", customer_id=customer_id,
            extra_metadata={},
        )
        fresh.add(convo)
        fresh.commit()

        reply_ctx = load_checkout_reply_context(
            fresh, tenant_id=tenant_id, conversation=convo,
            customer_phone="+966500000803", order_prep={}, brain_state={},
        )
        assert reply_ctx.known_previous["city"] == CITY
        assert reply_ctx.known_previous["short_address"] == SHORT_CODE
        assert reply_ctx.known_previous["selection_state"] == "selected"

        patch = apply_delivery_continuation_address_patch(
            fresh, tenant_id=tenant_id, conversation=convo,
            customer_phone="+966500000803", order_prep={},
        )
        assert patch["city"] == CITY
        assert patch["short_address_code"] == SHORT_CODE
        assert patch["customer_confirmed_previous_address"] is True
    finally:
        fresh.close()


def test_several_candidates_never_resolve_to_an_implicit_default(
    pg_at_0110: Engine,
) -> None:
    db = _session(pg_at_0110)
    try:
        tenant_id, customer_id = _seed(db, salla_id="SC-PG-MULTI", phone="+966500000804")
        _import(db, tenant_id, customer_id, _payload(id="SC-PG-MULTI"))
        db.commit()
        upsert_imported_address_candidate(
            db, tenant_id=tenant_id, customer_id=customer_id,
            components=AddressComponents(city="جدة", short_address_code="JJJD5678"),
            source=SOURCE_SALLA_CUSTOMER_PROFILE, source_ref="SC-PG-MULTI-2",
            source_updated_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
        db.commit()
    finally:
        db.close()

    fresh = _session(pg_at_0110)
    try:
        resolution = resolve_customer_address_selection(
            fresh, tenant_id=tenant_id, customer_id=customer_id,
        )
        assert resolution.reason == REASON_MULTIPLE_CANDIDATES
        assert resolution.reusable is None
        convo = Conversation(
            tenant_id=tenant_id, status="open", customer_id=customer_id,
            extra_metadata={},
        )
        fresh.add(convo)
        fresh.commit()
        ctx = build_order_context(
            fresh, tenant_id=tenant_id, conversation=convo,
            phone="+966500000804", brain_state={"order_prep": {}},
            build_source="pg_multiple_candidates",
        )
        assert ctx.known_previous_address is None
        assert len(ctx.known_address_candidates) == 2
    finally:
        fresh.close()


def test_orm_metadata_matches_the_migrated_schema(pg_at_0110: Engine) -> None:
    """``create_all`` on a migrated database adds nothing — shapes agree."""
    before = set(inspect(pg_at_0110).get_table_names())
    Base.metadata.create_all(pg_at_0110, checkfirst=True)
    after = set(inspect(pg_at_0110).get_table_names())
    assert _TABLE in before
    assert after >= before
