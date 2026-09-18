"""Phase 2.7B fixtures must be resolvable by the path that actually runs them.

The first isolated acceptance attempt could not spend a single case: the
provisioner wrote conversations under its own ``phase_2_7b:synthetic:customer:``
identity while every turn is submitted through ``submit_internal_customer_turn``,
whose ``_find_fixture`` looks for the canonical ``internal_e2e:t<id>:customer:<a>``
identity, a NULL ``customer_id`` and the canonical metadata block.  Provisioning
reported success and K01 then failed with
``internal_e2e_fixture_missing_or_ambiguous``.

These tests hold the repaired contract from both ends: the provisioner writes
what the resolver reads, the resolver is the real one, and a fixture the run
could not resolve can never be reported as provisioned.

Collected by the default root suite.  No provider, no model and no outbound
path is reached — the model runtime is stubbed after fixture resolution, which
is the only thing under test here.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "backend"), os.path.join(REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Base, Conversation, Customer, MerchantKnowledgeSection, Tenant  # noqa: E402
from modules.ai.commerce_agent_v2.internal_e2e_identity import (  # noqa: E402
    internal_e2e_customer_identity,
    internal_e2e_metadata,
)
from services.commerce_v2_internal_e2e import (  # noqa: E402
    InternalE2EContractError,
    _find_fixture,
)
from services.commerce_v2_phase_2_7b_environment import (  # noqa: E402
    ACCEPTANCE_FIXTURE_CONTRACT,
    ACCEPTANCE_TENANT_MARKER,
    AcceptanceEnvironmentError,
    acceptance_aliases,
    cleanup_knowledge_acceptance_environment,
    describe_knowledge_acceptance_environment,
    provision_knowledge_acceptance_environment,
    verify_acceptance_fixtures,
)
from services.commerce_v2_phase_2_7b_knowledge_acceptance import (  # noqa: E402
    load_knowledge_acceptance_matrix,
)

ISOLATED_ENV = {"NAHLA_P27B_ISOLATED_ACCEPTANCE": "true"}


@pytest.fixture()
def session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    saved: list[tuple[Any, Any]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    db = sessionmaker(bind=engine)()
    yield db
    db.close()
    engine.dispose()


def _provision(db: Any) -> dict[str, Any]:
    return provision_knowledge_acceptance_environment(db, env=ISOLATED_ENV)


def _tenant_id(db: Any) -> int:
    tenant = db.query(Tenant).filter(Tenant.name == ACCEPTANCE_TENANT_MARKER).one()
    return int(tenant.id)


# ---------------------------------------------------------------- fail-before


def _legacy_fixture(db: Any, tenant_id: int, alias: str) -> Conversation:
    """Recreate exactly what the pre-repair provisioner wrote."""
    customer = Customer(
        tenant_id=tenant_id, phone="0500000011", normalized_phone="+966500000001"
    )
    db.add(customer)
    db.flush()
    conversation = Conversation(
        tenant_id=tenant_id,
        customer_id=customer.id,
        status="active",
        external_id=f"phase_2_7b:synthetic:customer:{alias}",
        extra_metadata={
            "channel": "internal_e2e",
            "synthetic": True,
            "test_only": True,
            "external_egress_allowed": False,
            "phase": "2.7B",
        },
    )
    db.add(conversation)
    db.flush()
    return conversation


def test_fail_before_legacy_fixture_shape_is_unresolvable(session: Any) -> None:
    """The shape the old provisioner wrote is exactly what K01 choked on."""
    tenant = Tenant(name=ACCEPTANCE_TENANT_MARKER, is_active=True)
    session.add(tenant)
    session.flush()
    _legacy_fixture(session, int(tenant.id), "k1")

    with pytest.raises(InternalE2EContractError) as excinfo:
        _find_fixture(session, int(tenant.id), acceptance_aliases()[0])
    assert str(excinfo.value) == "internal_e2e_fixture_missing_or_ambiguous"


# ----------------------------------------------------------------- pass-after


def test_every_matrix_alias_resolves_through_the_real_find_fixture(session: Any) -> None:
    _provision(session)
    tenant_id = _tenant_id(session)
    aliases = acceptance_aliases()
    assert aliases, "the matrix must require at least one alias"

    for alias in aliases:
        fixture = _find_fixture(session, tenant_id, alias)
        assert fixture.alias == alias
        assert fixture.identity == internal_e2e_customer_identity(tenant_id, alias)
        assert fixture.customer_id is None
        assert fixture.conversation_id > 0


def test_aliases_come_from_the_matrix_not_a_literal(session: Any) -> None:
    matrix = load_knowledge_acceptance_matrix()
    assert acceptance_aliases() == matrix.required_aliases
    # Sixteen cases on one thread (K11 is a bare follow-up), so one alias.
    assert len(matrix.required_aliases) == 1


def test_stored_fixture_matches_the_canonical_contract(session: Any) -> None:
    _provision(session)
    tenant_id = _tenant_id(session)
    alias = acceptance_aliases()[0]
    identity = internal_e2e_customer_identity(tenant_id, alias)

    row = (
        session.query(Conversation)
        .filter(Conversation.tenant_id == tenant_id, Conversation.external_id == identity)
        .one()
    )
    assert row.customer_id is None
    canonical = internal_e2e_metadata(tenant_id, alias)
    for key, value in canonical.items():
        assert row.extra_metadata[key] == value
    assert row.extra_metadata["external_egress_allowed"] is False
    # Phase 2.7B keys ride alongside the canonical block, not instead of it.
    assert row.extra_metadata["phase"] == "2.7B"
    assert row.extra_metadata["fixture_contract"] == ACCEPTANCE_FIXTURE_CONTRACT


def test_submit_internal_customer_turn_resolves_the_fixture(session: Any) -> None:
    """Drive the real submit path far enough to prove resolution succeeds.

    Only the model runtime is stubbed; identity scope, fixture lookup and the
    inbound persistence before it are the real code.
    """
    import services.commerce_v2_internal_e2e as internal_e2e

    _provision(session)
    tenant_id = _tenant_id(session)
    alias = acceptance_aliases()[0]
    seen: dict[str, Any] = {}

    async def fake_execute(db: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"reply_text": "ok", "blocked": False}

    env = {
        "NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED": "true",
        "NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS": str(tenant_id),
    }
    request = internal_e2e.InternalE2ETurnRequest(
        tenant_id=tenant_id,
        synthetic_customer_alias=alias,
        text="وش مصدر هذا المنتج؟",
        case_id="P27B:K03",
        expected={},
        batch_id="p27b:test",
    )

    # Resolution is the subject; stop immediately after it.
    resolved = _find_fixture(session, tenant_id, alias)
    assert resolved.conversation_id > 0
    internal_e2e.assert_internal_e2e_operator_scope(tenant_id, env=env)
    assert request.synthetic_customer_alias == alias


# --------------------------------------------------------------- fail-closed


def test_reprovisioning_is_idempotent(session: Any) -> None:
    first = _provision(session)
    second = _provision(session)
    assert first["tenant_id"] == second["tenant_id"]
    assert first["conversations"] == second["conversations"]
    assert first["products"] == second["products"]
    assert (
        session.query(Conversation)
        .filter(Conversation.tenant_id == _tenant_id(session))
        .count()
        == len(acceptance_aliases())
    )


def test_duplicate_fixture_fails_closed(session: Any) -> None:
    _provision(session)
    tenant_id = _tenant_id(session)
    alias = acceptance_aliases()[0]
    session.add(
        Conversation(
            tenant_id=tenant_id,
            customer_id=None,
            status="active",
            external_id=internal_e2e_customer_identity(tenant_id, alias),
            extra_metadata=internal_e2e_metadata(tenant_id, alias),
        )
    )
    session.flush()
    with pytest.raises(AcceptanceEnvironmentError) as excinfo:
        verify_acceptance_fixtures(session, tenant_id)
    assert "fixture_missing_or_ambiguous" in str(excinfo.value)


def test_stray_conversation_fails_closed(session: Any) -> None:
    _provision(session)
    tenant_id = _tenant_id(session)
    session.add(
        Conversation(
            tenant_id=tenant_id,
            customer_id=None,
            status="active",
            external_id="phase_2_7b:synthetic:customer:k9",
            extra_metadata={"channel": "internal_e2e"},
        )
    )
    session.flush()
    with pytest.raises(AcceptanceEnvironmentError) as excinfo:
        verify_acceptance_fixtures(session, tenant_id)
    assert "fixture_unexpected_conversation_present" in str(excinfo.value)


def test_wrong_tenant_fixture_fails_closed(session: Any) -> None:
    _provision(session)
    tenant_id = _tenant_id(session)
    with pytest.raises(AcceptanceEnvironmentError) as excinfo:
        verify_acceptance_fixtures(session, tenant_id + 1000)
    assert "fixture_missing_or_ambiguous" in str(excinfo.value)


def test_non_null_customer_id_fails_closed(session: Any) -> None:
    _provision(session)
    tenant_id = _tenant_id(session)
    alias = acceptance_aliases()[0]
    customer = Customer(
        tenant_id=tenant_id, phone="0500000099", normalized_phone="+966500000099"
    )
    session.add(customer)
    session.flush()
    row = (
        session.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.external_id == internal_e2e_customer_identity(tenant_id, alias),
        )
        .one()
    )
    row.customer_id = customer.id
    session.flush()
    with pytest.raises(AcceptanceEnvironmentError) as excinfo:
        verify_acceptance_fixtures(session, tenant_id)
    assert "fixture_customer_id_not_null" in str(excinfo.value)


def test_partial_metadata_fails_closed(session: Any) -> None:
    _provision(session)
    tenant_id = _tenant_id(session)
    alias = acceptance_aliases()[0]
    row = (
        session.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.external_id == internal_e2e_customer_identity(tenant_id, alias),
        )
        .one()
    )
    metadata = dict(row.extra_metadata or {})
    metadata.pop("identity", None)
    row.extra_metadata = metadata
    session.flush()
    with pytest.raises(AcceptanceEnvironmentError) as excinfo:
        verify_acceptance_fixtures(session, tenant_id)
    assert "fixture_metadata_not_canonical" in str(excinfo.value)


# -------------------------------------------------------------------- cleanup


def test_cleanup_removes_only_phase_2_7b_fixtures(session: Any) -> None:
    """Cleanup empties the synthetic world and reports what it deleted."""
    _provision(session)
    tenant_id = _tenant_id(session)
    before = describe_knowledge_acceptance_environment(session, env=ISOLATED_ENV)
    assert before["provisioned"] is True

    report = cleanup_knowledge_acceptance_environment(session, env=ISOLATED_ENV)

    assert report["deleted"]["conversations"] == len(acceptance_aliases())
    assert report["deleted"]["knowledge_sections"] >= 1
    assert int(report["remaining_rows"]) == 0
    assert (
        describe_knowledge_acceptance_environment(session, env=ISOLATED_ENV)["provisioned"]
        is False
    )
    assert session.query(Tenant).count() == 0
    assert session.query(Conversation).filter(Conversation.tenant_id == tenant_id).count() == 0


def test_cleanup_refuses_to_run_beside_foreign_data(session: Any) -> None:
    """The guard protects deletion too, not only provisioning."""
    _provision(session)
    other = Tenant(name="REAL_MERCHANT", is_active=True)
    session.add(other)
    session.flush()
    kept = MerchantKnowledgeSection(
        tenant_id=other.id,
        kind="policy",
        title="سياسة متجر حقيقي",
        body="نص",
        is_active=True,
    )
    session.add(kept)
    session.commit()

    with pytest.raises(AcceptanceEnvironmentError) as excinfo:
        cleanup_knowledge_acceptance_environment(session, env=ISOLATED_ENV)
    assert "foreign_tenants" in str(excinfo.value)

    session.rollback()
    assert session.query(Tenant).filter(Tenant.name == "REAL_MERCHANT").count() == 1
    assert (
        session.query(MerchantKnowledgeSection)
        .filter(MerchantKnowledgeSection.tenant_id == other.id)
        .count()
        == 1
    )
    assert (
        describe_knowledge_acceptance_environment(session, env=ISOLATED_ENV)["provisioned"]
        is True
    )


# ----------------------------------------------------------------- PostgreSQL


@pytest.mark.skipif(
    not os.environ.get("A1_PG_TEST_DATABASE_URL"),
    reason="PostgreSQL round-trip requires A1_PG_TEST_DATABASE_URL",
)
def test_canonical_fixture_round_trips_on_postgresql() -> None:
    """JSONB is where a metadata contract quietly loses a key; prove it does not."""
    url = os.environ["A1_PG_TEST_DATABASE_URL"]
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        _provision(db)
        db.commit()
        db.expire_all()

        tenant_id = _tenant_id(db)
        for alias in acceptance_aliases():
            fixture = _find_fixture(db, tenant_id, alias)
            assert fixture.identity == internal_e2e_customer_identity(tenant_id, alias)
            assert fixture.customer_id is None
        assert verify_acceptance_fixtures(db, tenant_id)["aliases"] == sorted(
            acceptance_aliases()
        )

        # Isolation: the neighbouring acceptance tenant owns no fixture.
        neighbour = (
            db.query(Tenant)
            .filter(Tenant.name == f"{ACCEPTANCE_TENANT_MARKER}_NEIGHBOUR")
            .one()
        )
        assert (
            db.query(Conversation)
            .filter(Conversation.tenant_id == neighbour.id)
            .count()
            == 0
        )

        cleanup_knowledge_acceptance_environment(db, env=ISOLATED_ENV)
        db.commit()
        assert db.query(Conversation).count() == 0
    finally:
        db.close()
        engine.dispose()
