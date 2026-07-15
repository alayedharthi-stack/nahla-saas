"""PostgreSQL migration/constraint tests for conversation A1-subject bindings (0089)."""
from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
_DATABASE = _REPO / "database"
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests.order_customer_identity_postgres_fixtures import _connect_engine  # noqa: E402
from models import (  # noqa: E402
    Conversation,
    ConversationA1SubjectBinding,
    Customer,
    Order,
    Tenant,
)
from services.conversation_a1_subject_binding_contract import (  # noqa: E402
    BINDING_STATE_ACTIVE,
    BINDING_STATE_SUPERSEDED,
    BINDING_WRITE_OUTCOME_CREATED,
    BINDING_WRITE_OUTCOME_NO_OP,
    BINDING_WRITE_OUTCOME_SUPERSEDED,
)
from services.conversation_a1_subject_binding_service import (  # noqa: E402
    write_authoritative_internal_binding_from_verified_order,
)
from services.order_customer_identity_contract import (  # noqa: E402
    EVIDENCE_AUTHORITATIVE,
    LINK_STATE_VERIFIED,
    NAHLA_INTERNAL_ORDER_V1,
    ORDER_SOURCE_NAHL_INTERNAL,
)

MIGRATION_TENANT_A = 991_001
MIGRATION_TENANT_B = 991_002

_0089_OBJECTS = (
    "conversation_a1_subject_bindings",
    "uq_conversations_tenant_id",
    "uq_casb_tenant_conversation_active",
    "fk_casb_tenant_conversation",
    "fk_casb_tenant_internal_customer",
    "chk_casb_binding_state",
    "chk_casb_subject_kind",
    "chk_casb_subject_xor",
    "chk_casb_state_revocation_timestamp",
)


def _alembic_config(engine: Engine) -> Config:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        cfg = Config("alembic.ini")
        url = str(engine.url.render_as_string(hide_password=False))
        cfg.set_main_option("sqlalchemy.url", url)
        os.environ["DATABASE_URL"] = url
        return cfg
    finally:
        os.chdir(prev_cwd)


def _upgrade(engine: Engine, revision: str) -> None:
    cfg = _alembic_config(engine)
    prev_cwd = os.getcwd()
    try:
        os.chdir(_DATABASE)
        command.upgrade(cfg, revision)
    finally:
        os.chdir(prev_cwd)


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Engine]:
    engine = _connect_engine()
    _upgrade(engine, "0089")
    yield engine
    engine.dispose()


def test_0089_objects_present(pg_engine: Engine) -> None:
    insp = inspect(pg_engine)
    tables = set(insp.get_table_names())
    assert "conversation_a1_subject_bindings" in tables
    assert "uq_conversations_tenant_id" in {
        idx.get("name") for idx in insp.get_indexes("conversations")
    }
    casb_indexes = {idx.get("name") for idx in insp.get_indexes("conversation_a1_subject_bindings")}
    assert "uq_casb_tenant_conversation_active" in casb_indexes
    assert "ix_casb_tenant_conversation_state" in casb_indexes
    fks = {fk.get("name") for fk in insp.get_foreign_keys("conversation_a1_subject_bindings")}
    assert "fk_casb_tenant_conversation" in fks
    assert "fk_casb_tenant_internal_customer" in fks
    checks = {
        c.get("name")
        for c in insp.get_check_constraints("conversation_a1_subject_bindings")
    }
    assert "chk_casb_subject_xor" in checks
    assert "chk_casb_state_revocation_timestamp" in checks
    from models import ConversationA1SubjectBinding  # noqa: PLC0415

    active_index = next(
        index
        for index in ConversationA1SubjectBinding.__table__.indexes
        if index.name == "uq_casb_tenant_conversation_active"
    )
    assert active_index.unique is True
    assert str(active_index.dialect_options["postgresql"]["where"]) == "binding_state = 'active'"


def test_active_unique_per_tenant_conversation(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name) VALUES (:tid, 'T-A')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"tid": MIGRATION_TENANT_A},
        )
        cust = conn.execute(
            text(
                """
                INSERT INTO customers (tenant_id, name)
                VALUES (:tid, 'أحمد سالم') RETURNING id
                """
            ),
            {"tid": MIGRATION_TENANT_A},
        ).scalar_one()
        conv = conn.execute(
            text(
                """
                INSERT INTO conversations (tenant_id, status, customer_id)
                VALUES (:tid, 'open', :cid) RETURNING id
                """
            ),
            {"tid": MIGRATION_TENANT_A, "cid": cust},
        ).scalar_one()
        bind_id = uuid.uuid4()
        conn.execute(
            text(
                """
                INSERT INTO conversation_a1_subject_bindings (
                    id, tenant_id, conversation_id, subject_kind, identity_namespace,
                    internal_customer_id, binding_state, evidence_class, binding_source,
                    provenance_kind, provenance_id, bound_at, created_at, updated_at
                ) VALUES (
                    :id, :tid, :conv, 'nahla_internal_customer', 'nahla_internal_order_v1',
                    :cust, 'active', 'authoritative', 'wa_order_bridge_authoritative_internal',
                    'order', 'opaque-ref', now(), now(), now()
                )
                """
            ),
            {
                "id": bind_id,
                "tid": MIGRATION_TENANT_A,
                "conv": conv,
                "cust": cust,
            },
        )
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text(
                    """
                    INSERT INTO conversation_a1_subject_bindings (
                        id, tenant_id, conversation_id, subject_kind, identity_namespace,
                        internal_customer_id, binding_state, evidence_class, binding_source,
                        provenance_kind, provenance_id, bound_at, created_at, updated_at
                    ) VALUES (
                        gen_random_uuid(), :tid, :conv, 'nahla_internal_customer',
                        'nahla_internal_order_v1', :cust, 'active', 'authoritative',
                        'wa_order_bridge_authoritative_internal', 'order', 'opaque-ref-2',
                        now(), now(), now()
                    )
                    """
                ),
                {"tid": MIGRATION_TENANT_A, "conv": conv, "cust": cust},
            )


def test_partial_active_index_allows_multiple_non_active_rows(pg_engine: Engine) -> None:
    tenant_id = 992_000_000 + (uuid.uuid4().int % 900_000)
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:tid, 'partial-index-test')"),
            {"tid": tenant_id},
        )
        customer_id = conn.execute(
            text("INSERT INTO customers (tenant_id, name) VALUES (:tid, 'عميل') RETURNING id"),
            {"tid": tenant_id},
        ).scalar_one()
        conversation_id = conn.execute(
            text(
                """
                INSERT INTO conversations (tenant_id, status, customer_id)
                VALUES (:tid, 'open', :cid) RETURNING id
                """
            ),
            {"tid": tenant_id, "cid": customer_id},
        ).scalar_one()
        for state in ("revoked", "superseded"):
            conn.execute(
                text(
                    """
                    INSERT INTO conversation_a1_subject_bindings (
                        id, tenant_id, conversation_id, subject_kind, identity_namespace,
                        internal_customer_id, binding_state, evidence_class, binding_source,
                        provenance_kind, provenance_id, bound_at, revoked_at, created_at, updated_at
                    ) VALUES (
                        gen_random_uuid(), :tid, :conv, 'nahla_internal_customer',
                        'nahla_internal_order_v1', :cust, :state, 'authoritative',
                        'wa_order_bridge_authoritative_internal', 'order', :provenance,
                        now(), now(), now(), now()
                    )
                    """
                ),
                {
                    "tid": tenant_id,
                    "conv": conversation_id,
                    "cust": customer_id,
                    "state": state,
                    "provenance": f"opaque-{state}",
                },
            )
        count = conn.execute(
            text(
                """
                SELECT count(*) FROM conversation_a1_subject_bindings
                WHERE tenant_id = :tid AND conversation_id = :conv
                """
            ),
            {"tid": tenant_id, "conv": conversation_id},
        ).scalar_one()
        assert count == 2


def test_tenant_isolation_on_conversation_fk(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        for tid, label in (
            (MIGRATION_TENANT_A, "T-A"),
            (MIGRATION_TENANT_B, "T-B"),
        ):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, name) VALUES (:tid, :name) ON CONFLICT (id) DO NOTHING"
                ),
                {"tid": tid, "name": label},
            )
        cust_b = conn.execute(
            text(
                "INSERT INTO customers (tenant_id, name) VALUES (:tid, 'نورة') RETURNING id"
            ),
            {"tid": MIGRATION_TENANT_B},
        ).scalar_one()
        conv_b = conn.execute(
            text(
                """
                INSERT INTO conversations (tenant_id, status, customer_id)
                VALUES (:tid, 'open', :cid) RETURNING id
                """
            ),
            {"tid": MIGRATION_TENANT_B, "cid": cust_b},
        ).scalar_one()
        cust_a = conn.execute(
            text(
                "INSERT INTO customers (tenant_id, name) VALUES (:tid, 'أحمد') RETURNING id"
            ),
            {"tid": MIGRATION_TENANT_A},
        ).scalar_one()
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text(
                    """
                    INSERT INTO conversation_a1_subject_bindings (
                        id, tenant_id, conversation_id, subject_kind, identity_namespace,
                        internal_customer_id, binding_state, evidence_class, binding_source,
                        provenance_kind, provenance_id, bound_at, created_at, updated_at
                    ) VALUES (
                        gen_random_uuid(), :tid_a, :conv_b, 'nahla_internal_customer',
                        'nahla_internal_order_v1', :cust_a, 'active', 'authoritative',
                        'wa_order_bridge_authoritative_internal', 'order', 'opaque',
                        now(), now(), now()
                    )
                    """
                ),
                {"tid_a": MIGRATION_TENANT_A, "conv_b": conv_b, "cust_a": cust_a},
            )


def test_tenant_isolation_on_internal_customer_fk(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        for tid, label in (
            (MIGRATION_TENANT_A, "T-A"),
            (MIGRATION_TENANT_B, "T-B"),
        ):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, name) VALUES (:tid, :name) ON CONFLICT (id) DO NOTHING"
                ),
                {"tid": tid, "name": label},
            )
        customer_a = conn.execute(
            text("INSERT INTO customers (tenant_id, name) VALUES (:tid, 'عميل أ') RETURNING id"),
            {"tid": MIGRATION_TENANT_A},
        ).scalar_one()
        conversation_a = conn.execute(
            text(
                """
                INSERT INTO conversations (tenant_id, status, customer_id)
                VALUES (:tid, 'open', :cid) RETURNING id
                """
            ),
            {"tid": MIGRATION_TENANT_A, "cid": customer_a},
        ).scalar_one()
        customer_b = conn.execute(
            text("INSERT INTO customers (tenant_id, name) VALUES (:tid, 'عميل ب') RETURNING id"),
            {"tid": MIGRATION_TENANT_B},
        ).scalar_one()
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text(
                    """
                    INSERT INTO conversation_a1_subject_bindings (
                        id, tenant_id, conversation_id, subject_kind, identity_namespace,
                        internal_customer_id, binding_state, evidence_class, binding_source,
                        provenance_kind, provenance_id, bound_at, created_at, updated_at
                    ) VALUES (
                        gen_random_uuid(), :tid, :conv, 'nahla_internal_customer',
                        'nahla_internal_order_v1', :foreign_customer, 'active',
                        'authoritative', 'wa_order_bridge_authoritative_internal',
                        'order', 'opaque', now(), now(), now()
                    )
                    """
                ),
                {
                    "tid": MIGRATION_TENANT_A,
                    "conv": conversation_a,
                    "foreign_customer": customer_b,
                },
            )


@pytest.mark.parametrize(
    ("binding_state", "revoked_at_sql"),
    (
        ("active", "now()"),
        ("revoked", "NULL"),
        ("superseded", "NULL"),
    ),
)
def test_state_revocation_timestamp_invariant(
    pg_engine: Engine,
    binding_state: str,
    revoked_at_sql: str,
) -> None:
    tenant_id = 993_000_000 + (uuid.uuid4().int % 900_000)
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:tid, 'state-test')"),
            {"tid": tenant_id},
        )
        customer_id = conn.execute(
            text("INSERT INTO customers (tenant_id, name) VALUES (:tid, 'عميل') RETURNING id"),
            {"tid": tenant_id},
        ).scalar_one()
        conversation_id = conn.execute(
            text(
                """
                INSERT INTO conversations (tenant_id, status, customer_id)
                VALUES (:tid, 'open', :cid) RETURNING id
                """
            ),
            {"tid": tenant_id, "cid": customer_id},
        ).scalar_one()
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text(
                    f"""
                    INSERT INTO conversation_a1_subject_bindings (
                        id, tenant_id, conversation_id, subject_kind, identity_namespace,
                        internal_customer_id, binding_state, evidence_class, binding_source,
                        provenance_kind, provenance_id, bound_at, revoked_at, created_at, updated_at
                    ) VALUES (
                        gen_random_uuid(), :tid, :conv, 'nahla_internal_customer',
                        'nahla_internal_order_v1', :cust, :state, 'authoritative',
                        'wa_order_bridge_authoritative_internal', 'order', 'opaque',
                        now(), {revoked_at_sql}, now(), now()
                    )
                    """
                ),
                {
                    "tid": tenant_id,
                    "conv": conversation_id,
                    "cust": customer_id,
                    "state": binding_state,
                },
            )


@pytest.mark.parametrize("binding_state", ("revoked", "superseded"))
def test_non_active_state_accepts_revocation_timestamp(
    pg_engine: Engine,
    binding_state: str,
) -> None:
    tenant_id = 995_000_000 + (uuid.uuid4().int % 900_000)
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:tid, 'lifecycle-success')"),
            {"tid": tenant_id},
        )
        customer_id = conn.execute(
            text("INSERT INTO customers (tenant_id, name) VALUES (:tid, 'عميل') RETURNING id"),
            {"tid": tenant_id},
        ).scalar_one()
        conversation_id = conn.execute(
            text(
                """
                INSERT INTO conversations (tenant_id, status, customer_id)
                VALUES (:tid, 'open', :cid) RETURNING id
                """
            ),
            {"tid": tenant_id, "cid": customer_id},
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO conversation_a1_subject_bindings (
                    id, tenant_id, conversation_id, subject_kind, identity_namespace,
                    internal_customer_id, binding_state, evidence_class, binding_source,
                    provenance_kind, provenance_id, bound_at, revoked_at, created_at, updated_at
                ) VALUES (
                    gen_random_uuid(), :tid, :conv, 'nahla_internal_customer',
                    'nahla_internal_order_v1', :cust, :state, 'authoritative',
                    'wa_order_bridge_authoritative_internal', 'order', 'opaque',
                    now(), now(), now(), now()
                )
                """
            ),
            {
                "tid": tenant_id,
                "conv": conversation_id,
                "cust": customer_id,
                "state": binding_state,
            },
        )
        stored = conn.execute(
            text(
                """
                SELECT binding_state, revoked_at IS NOT NULL
                FROM conversation_a1_subject_bindings
                WHERE tenant_id = :tid AND conversation_id = :conv
                """
            ),
            {"tid": tenant_id, "conv": conversation_id},
        ).one()
        assert stored == (binding_state, True)


def _seed_concurrency_fixture(engine: Engine) -> tuple[int, int, int, int, int]:
    tenant_id = 994_000_000 + (uuid.uuid4().int % 900_000)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session.begin() as session:
        session.add(Tenant(id=tenant_id, name="متجر تجريبي عام"))
        customer_a = Customer(tenant_id=tenant_id, name="أحمد سالم")
        customer_b = Customer(tenant_id=tenant_id, name="نورة عبدالله")
        session.add_all((customer_a, customer_b))
        session.flush()
        conversation = Conversation(
            tenant_id=tenant_id,
            status="open",
            customer_id=customer_a.id,
        )
        session.add(conversation)
        session.flush()
        orders = []
        for customer, external_id in (
            (customer_a, f"concurrent-a-{tenant_id}"),
            (customer_b, f"concurrent-b-{tenant_id}"),
        ):
            order = Order(
                tenant_id=tenant_id,
                external_id=external_id,
                status="pending",
                total="1",
                source="whatsapp",
                order_source_kind=ORDER_SOURCE_NAHL_INTERNAL,
                identity_namespace=NAHLA_INTERNAL_ORDER_V1,
                customer_id=customer.id,
                customer_link_state=LINK_STATE_VERIFIED,
                customer_link_evidence_class=EVIDENCE_AUTHORITATIVE,
                customer_link_source="nahla_order_bridge_conversation_customer",
            )
            session.add(order)
            orders.append(order)
        session.flush()
        return tenant_id, conversation.id, orders[0].id, orders[1].id, customer_b.id


def _concurrent_write(engine: Engine, *, tenant_id: int, conversation_id: int, order_id: int) -> str:
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        order = session.get(Order, order_id)
        result = write_authoritative_internal_binding_from_verified_order(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            order=order,
        )
        # A conflict handled by a nested savepoint must leave this transaction usable.
        assert session.execute(text("SELECT 1")).scalar_one() == 1
        session.commit()
        return result.outcome
    finally:
        session.close()


def test_concurrent_same_subject_write_is_idempotent_and_transaction_usable(pg_engine: Engine) -> None:
    tenant_id, conversation_id, order_a, _, _ = _seed_concurrency_fixture(pg_engine)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda _: _concurrent_write(
                    pg_engine,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    order_id=order_a,
                ),
                range(2),
            )
        )
    assert sorted(outcomes) == sorted((
        BINDING_WRITE_OUTCOME_CREATED,
        BINDING_WRITE_OUTCOME_NO_OP,
    ))
    with sessionmaker(bind=pg_engine)() as session:
        assert (
            session.query(ConversationA1SubjectBinding)
            .filter_by(tenant_id=tenant_id, conversation_id=conversation_id,
                       binding_state=BINDING_STATE_ACTIVE)
            .count()
            == 1
        )


def test_concurrent_conflicting_rebind_supersedes_safely(pg_engine: Engine) -> None:
    tenant_id, conversation_id, order_a, order_b, customer_b_id = _seed_concurrency_fixture(pg_engine)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda order_id: _concurrent_write(
                    pg_engine,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    order_id=order_id,
                ),
                (order_a, order_b),
            )
        )
    assert BINDING_WRITE_OUTCOME_CREATED in outcomes
    assert BINDING_WRITE_OUTCOME_SUPERSEDED in outcomes
    with sessionmaker(bind=pg_engine)() as session:
        active = (
            session.query(ConversationA1SubjectBinding)
            .filter_by(tenant_id=tenant_id, conversation_id=conversation_id,
                       binding_state=BINDING_STATE_ACTIVE)
            .one()
        )
        assert active.internal_customer_id in {
            session.get(Order, order_a).customer_id,
            customer_b_id,
        }
        assert (
            session.query(ConversationA1SubjectBinding)
            .filter_by(tenant_id=tenant_id, conversation_id=conversation_id,
                       binding_state=BINDING_STATE_SUPERSEDED)
            .count()
            == 1
        )
