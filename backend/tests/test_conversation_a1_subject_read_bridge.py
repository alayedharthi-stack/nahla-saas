"""Unit coverage for the tenant-safe conversation → A1 subject read bridge."""
from __future__ import annotations

import logging
import copy
import dataclasses
import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from models import Base, Conversation, ConversationA1SubjectBinding, Customer, Tenant  # noqa: E402
from services.conversation_a1_subject_binding_contract import (  # noqa: E402
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    BINDING_STATE_ACTIVE,
    BINDING_STATE_REVOKED,
    BINDING_STATE_SUPERSEDED,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
)
from services.conversation_a1_subject_read_contract import (  # noqa: E402
    READ_STATUS_RESOLVED,
    READ_STATUS_UNRESOLVED,
    TrustedConversationA1SubjectReadRequest,
    UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT,
    UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE,
    UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE,
    UNRESOLVED_REASON_BINDING_NOT_AUTHORITATIVE,
    UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID,
    UNRESOLVED_REASON_BINDING_SOURCE_INVALID,
    UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID,
    UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE,
    UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH,
    UNRESOLVED_REASON_INVALID_REQUEST,
    UNRESOLVED_REASON_READ_UNAVAILABLE,
    UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH,
)
from services.conversation_a1_subject_read_service import (  # noqa: E402
    resolve_authoritative_a1_subject_for_conversation,
)
from services.order_customer_identity_contract import (  # noqa: E402
    EVIDENCE_AUTHORITATIVE,
    EVIDENCE_INFERRED,
    NAHLA_INTERNAL_ORDER_V1,
)


@event.listens_for(Base.metadata, "before_create")
def _sqlite_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all((Tenant(id=71, name="متجر تجريبي عام"), Tenant(id=72, name="متجر تجريبي آخر")))
    session.commit()
    yield session
    session.close()


def _seed_binding(db, *, tenant_id: int = 71, state: str = BINDING_STATE_ACTIVE):
    customer = Customer(tenant_id=tenant_id, name="نورة عبدالله", phone="966500011122")
    db.add(customer)
    db.flush()
    conversation = Conversation(tenant_id=tenant_id, status="open", customer_id=customer.id)
    db.add(conversation)
    db.flush()
    now = datetime.now(timezone.utc)
    binding = ConversationA1SubjectBinding(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        internal_customer_id=customer.id,
        binding_state=state,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        provenance_kind="order",
        provenance_id="private-order-reference",
        bound_at=now,
        revoked_at=None if state == BINDING_STATE_ACTIVE else now,
        created_at=now,
        updated_at=now,
    )
    db.add(binding)
    db.commit()
    return conversation, customer, binding


def _request(conversation, tenant_id: int = 71):
    return TrustedConversationA1SubjectReadRequest(
        tenant_id=tenant_id, conversation_id=conversation.id,
    )


def test_active_authoritative_binding_returns_opaque_handle(db, monkeypatch) -> None:
    conversation, _, binding = _seed_binding(db)
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (True, True),
    )

    result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))

    assert result.status == READ_STATUS_RESOLVED
    assert result.handle is not None
    assert result.reason is None
    assert result.evidence_class == "authoritative_a1_policy_eligible"
    assert repr(result.handle) == "AuthoritativeA1SubjectHandle()"
    assert not hasattr(result.handle, "__dict__")
    assert str(binding.id) not in repr(result)


def test_absent_and_non_active_bindings_fail_closed(db, monkeypatch) -> None:
    customer = Customer(tenant_id=71, name="عميل تجريبي")
    db.add(customer)
    db.flush()
    absent = Conversation(tenant_id=71, status="open", customer_id=customer.id)
    db.add(absent)
    db.commit()
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (True, True),
    )

    no_binding = resolve_authoritative_a1_subject_for_conversation(db, request=_request(absent))
    assert no_binding.status == READ_STATUS_UNRESOLVED
    assert no_binding.reason == UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT

    for state in (BINDING_STATE_REVOKED, BINDING_STATE_SUPERSEDED):
        conversation, _, _ = _seed_binding(db, state=state)
        result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
        assert result.reason == UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT


def test_multiple_active_rows_fail_closed_even_if_database_constraint_is_bypassed(db, monkeypatch) -> None:
    conversation, customer, _ = _seed_binding(db)
    db.execute(text("DROP INDEX uq_casb_tenant_conversation_active"))
    now = datetime.now(timezone.utc)
    db.add(ConversationA1SubjectBinding(
        tenant_id=71, conversation_id=conversation.id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        internal_customer_id=customer.id, binding_state=BINDING_STATE_ACTIVE,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        provenance_kind="order", provenance_id="other-private-reference",
        bound_at=now, created_at=now, updated_at=now,
    ))
    db.commit()
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (True, True),
    )

    result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
    assert result.status == READ_STATUS_UNRESOLVED
    assert result.reason == UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE


def test_cross_tenant_request_and_unready_capability_fail_closed(db, monkeypatch) -> None:
    conversation, _, _ = _seed_binding(db)
    cross_tenant = resolve_authoritative_a1_subject_for_conversation(
        db, request=_request(conversation, tenant_id=72),
    )
    assert cross_tenant.reason == UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH

    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (True, False),
    )
    unavailable = resolve_authoritative_a1_subject_for_conversation(
        db, request=_request(conversation),
    )
    assert unavailable.reason == UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE


def test_invalid_active_binding_fails_closed(db, monkeypatch) -> None:
    conversation, _, binding = _seed_binding(db)
    request = _request(conversation)
    binding.evidence_class = EVIDENCE_INFERRED
    db.commit()
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (True, True),
    )

    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)
    assert result.status == READ_STATUS_UNRESOLVED
    assert result.reason == UNRESOLVED_REASON_BINDING_NOT_AUTHORITATIVE


@pytest.mark.parametrize(
    ("attribute", "value", "reason"),
    [
        ("subject_kind", "unknown_subject", UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID),
        ("identity_namespace", "unknown_namespace", UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID),
        ("binding_source", "untrusted_source", UNRESOLVED_REASON_BINDING_SOURCE_INVALID),
    ],
)
def test_invalid_binding_contract_values_fail_closed(db, monkeypatch, attribute, value, reason) -> None:
    conversation, _, binding = _seed_binding(db)
    request = _request(conversation)
    setattr(binding, attribute, value)
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (True, True),
    )

    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)

    assert result.status == READ_STATUS_UNRESOLVED
    assert result.reason == reason
    db.rollback()


@pytest.mark.parametrize("wrong_tenant", (False, True))
def test_missing_or_cross_tenant_subject_fails_closed(db, monkeypatch, wrong_tenant) -> None:
    conversation, _, binding = _seed_binding(db)
    request = _request(conversation)
    if wrong_tenant:
        other = Customer(tenant_id=72, name="عميل مستأجر آخر")
        db.add(other)
        db.flush()
        binding.internal_customer_id = other.id
    else:
        binding.internal_customer_id = 999_999
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (True, True),
    )

    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)

    assert result.reason == UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH
    db.rollback()


def test_missing_canonical_proof_and_proof_errors_fail_closed(db, monkeypatch) -> None:
    conversation, _, _ = _seed_binding(db)
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (False, False),
    )
    absent = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
    assert absent.reason == UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE

    def broken_proof(*args, **kwargs):
        raise RuntimeError("private database detail")

    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status", broken_proof,
    )
    failed = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
    assert failed.reason == UNRESOLVED_REASON_READ_UNAVAILABLE
    assert "private database detail" not in repr(failed)


@pytest.mark.parametrize("malformed_request", (None, object(), {"tenant_id": 71, "conversation_id": 1}))
def test_malformed_request_fails_closed_without_dereference(db, malformed_request) -> None:
    result = resolve_authoritative_a1_subject_for_conversation(
        db, request=malformed_request,
    )

    assert result.status == READ_STATUS_UNRESOLVED
    assert result.reason == UNRESOLVED_REASON_INVALID_REQUEST


def test_resolve_never_flushes_caller_pending_invalid_writes(db, monkeypatch) -> None:
    conversation, _, _ = _seed_binding(db)
    request = _request(conversation)
    pending_invalid = ConversationA1SubjectBinding()
    db.add(pending_invalid)
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (True, True),
    )

    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)

    assert result.status == READ_STATUS_RESOLVED
    assert pending_invalid in db.new
    db.rollback()


def test_read_logs_and_results_do_not_serialize_private_identifiers(db, monkeypatch, caplog) -> None:
    conversation, customer, binding = _seed_binding(db)
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._policy_proof_status",
        lambda *args, **kwargs: (True, True),
    )
    with caplog.at_level(logging.INFO, logger="nahla.conversation_a1_subject_read"):
        result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))

    serialized = repr(result)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    # Numeric primary-key substrings are not meaningful privacy checks (for
    # example, the closed A1 evidence label contains "1"). Check the opaque
    # UUID and deliberately private source values instead.
    forbidden = (str(binding.id), customer.phone, "private-order-reference")
    for value in forbidden:
        assert value not in serialized
        assert value not in logged
    with pytest.raises(TypeError):
        pickle.dumps(result)
    with pytest.raises(TypeError):
        copy.copy(result.handle)
    with pytest.raises(TypeError):
        dataclasses.asdict(result)
    with pytest.raises(TypeError):
        json.dumps(result)
