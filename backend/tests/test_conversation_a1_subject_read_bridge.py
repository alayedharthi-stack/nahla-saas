"""Unit coverage for the tenant-safe conversation → A1 subject read bridge."""
from __future__ import annotations

import logging
import copy
import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from models import (  # noqa: E402
    Base,
    Conversation,
    ConversationA1SubjectBinding,
    Customer,
    ExternalCustomerProfile,
    Integration,
    Tenant,
)
from services.conversation_a1_subject_binding_contract import (  # noqa: E402
    BINDING_SOURCE_PROVIDER_OAUTH_SESSION,
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    BINDING_STATE_ACTIVE,
    BINDING_STATE_REVOKED,
    BINDING_STATE_SUPERSEDED,
    SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
)
from services.conversation_a1_subject_read_contract import (  # noqa: E402
    AuthoritativeA1SubjectHandle,
    BoundAuthoritativeA1PolicyProofSnapshot,
    BoundAuthoritativeA1SubjectScope,
    READ_STATUS_RESOLVED,
    READ_STATUS_UNRESOLVED,
    TrustedConversationA1SubjectReadRequest,
    UNRESOLVED_REASONS,
    UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT,
    UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE,
    UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE,
    UNRESOLVED_REASON_BINDING_NOT_AUTHORITATIVE,
    UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID,
    UNRESOLVED_REASON_BINDING_SOURCE_INVALID,
    UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID,
    UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE,
    UNRESOLVED_REASON_CONVERSATION_ABSENT,
    UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH,
    UNRESOLVED_REASON_INVALID_CONVERSATION,
    UNRESOLVED_REASON_INVALID_REQUEST,
    UNRESOLVED_REASON_INVALID_TENANT,
    UNRESOLVED_REASON_READ_UNAVAILABLE,
    UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH,
    _issue_authoritative_a1_subject_pair,
)
from services.conversation_a1_subject_read_service import (  # noqa: E402
    resolve_authoritative_a1_subject_for_conversation,
)
from services.order_customer_identity_contract import (  # noqa: E402
    EVIDENCE_AUTHORITATIVE,
    EVIDENCE_INFERRED,
    EXTERNAL_PROVIDER_SALLA_V1,
    NAHLA_INTERNAL_ORDER_V1,
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_HEALTHY,
    SYNC_HEALTH_STALE,
)


def _ready_proof(*, binding):
    return SimpleNamespace(
        subject_kind=binding.subject_kind,
        identity_namespace=binding.identity_namespace,
        policy_eligibility_ready=True,
        authoritative_source_history_completeness=SOURCE_HISTORY_COMPLETE,
        forward_sync_health=SYNC_HEALTH_HEALTHY,
    )


def _patch_ready_proof(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._canonical_policy_proof",
        lambda db, *, binding, tenant_id: _ready_proof(binding=binding),
    )


def _patch_unready_proof(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._canonical_policy_proof",
        lambda db, *, binding, tenant_id: SimpleNamespace(
            subject_kind=binding.subject_kind,
            identity_namespace=binding.identity_namespace,
            policy_eligibility_ready=False,
            authoritative_source_history_completeness=SOURCE_HISTORY_INCOMPLETE,
            forward_sync_health=SYNC_HEALTH_STALE,
        ),
    )


def _patch_missing_proof(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._canonical_policy_proof",
        lambda db, *, binding, tenant_id: None,
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


def _assert_resolved_snapshot_matches_scope_proof(
    scope: BoundAuthoritativeA1SubjectScope,
    *,
    authoritative_source_history_completeness: str,
    forward_sync_health: str,
) -> None:
    """Every snapshot accessor must mirror legacy scope proof accessors."""
    snapshot = scope.proof_snapshot()
    assert snapshot.subject_kind() == scope.proof_subject_kind()
    assert snapshot.identity_namespace() == scope.proof_identity_namespace()
    assert snapshot.policy_eligibility_ready() == scope.proof_policy_eligibility_ready()
    assert (
        snapshot.authoritative_source_history_completeness()
        == authoritative_source_history_completeness
    )
    assert snapshot.forward_sync_health() == forward_sync_health


@pytest.mark.parametrize(
    ("seed", "completeness", "sync_health"),
    [
        pytest.param("internal", SOURCE_HISTORY_COMPLETE, SYNC_HEALTH_HEALTHY, id="internal"),
        pytest.param("external", SOURCE_HISTORY_INCOMPLETE, SYNC_HEALTH_DEGRADED, id="external"),
    ],
)
def test_resolved_snapshot_accessors_match_scope_proof(
    db, monkeypatch, seed, completeness, sync_health,
) -> None:
    if seed == "internal":
        conversation, _, binding = _seed_binding(db)
    else:
        conversation, _, binding = _seed_external_binding(db)

    def evaluated_proof(db_arg, *, binding, tenant_id):
        return SimpleNamespace(
            subject_kind=binding.subject_kind,
            identity_namespace=binding.identity_namespace,
            policy_eligibility_ready=True,
            authoritative_source_history_completeness=completeness,
            forward_sync_health=sync_health,
        )

    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._canonical_policy_proof",
        evaluated_proof,
    )

    result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))

    assert result.status == READ_STATUS_RESOLVED
    assert result.bound_scope is not None
    _assert_resolved_snapshot_matches_scope_proof(
        result.bound_scope,
        authoritative_source_history_completeness=completeness,
        forward_sync_health=sync_health,
    )
    assert result.bound_scope.proof_snapshot().is_bound_to(result.bound_scope)


def test_active_authoritative_binding_returns_opaque_handle(db, monkeypatch) -> None:
    conversation, _, binding = _seed_binding(db)
    _patch_ready_proof(monkeypatch)

    result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))

    assert result.status == READ_STATUS_RESOLVED
    assert result.handle is not None
    assert result.bound_scope is not None
    assert result.reason is None
    assert result.evidence_class == "authoritative_a1_policy_eligible"
    assert repr(result.handle) == "AuthoritativeA1SubjectHandle()"
    assert repr(result.bound_scope) == "BoundAuthoritativeA1SubjectScope()"
    assert result.handle.is_bound_to(result.bound_scope)
    assert result.bound_scope.is_bound_to(result.handle)
    assert result.bound_scope.tenant_id() == 71
    assert result.bound_scope.conversation_id() == conversation.id
    assert result.bound_scope.subject_kind() == SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER
    assert result.bound_scope.identity_namespace() == NAHLA_INTERNAL_ORDER_V1
    assert result.bound_scope.internal_customer_id() == binding.internal_customer_id
    assert result.bound_scope.external_customer_profile_id() is None
    assert result.bound_scope.proof_policy_eligibility_ready() is True
    snapshot = result.bound_scope.proof_snapshot()
    assert isinstance(snapshot, BoundAuthoritativeA1PolicyProofSnapshot)
    assert snapshot.is_bound_to(result.handle)
    assert snapshot.is_bound_to(result.bound_scope)
    assert snapshot.subject_kind() == SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER
    assert snapshot.identity_namespace() == NAHLA_INTERNAL_ORDER_V1
    assert snapshot.policy_eligibility_ready() is True
    assert snapshot.authoritative_source_history_completeness() == SOURCE_HISTORY_COMPLETE
    assert snapshot.forward_sync_health() == SYNC_HEALTH_HEALTHY
    assert repr(snapshot) == "BoundAuthoritativeA1PolicyProofSnapshot()"
    assert not hasattr(snapshot, "__dict__")
    assert str(binding.id) not in repr(result)


def test_absent_and_non_active_bindings_fail_closed(db, monkeypatch) -> None:
    customer = Customer(tenant_id=71, name="عميل تجريبي")
    db.add(customer)
    db.flush()
    absent = Conversation(tenant_id=71, status="open", customer_id=customer.id)
    db.add(absent)
    db.commit()
    _patch_ready_proof(monkeypatch)

    no_binding = resolve_authoritative_a1_subject_for_conversation(db, request=_request(absent))
    assert no_binding.status == READ_STATUS_UNRESOLVED
    assert no_binding.handle is None
    assert no_binding.bound_scope is None
    assert no_binding.reason == UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT

    for state in (BINDING_STATE_REVOKED, BINDING_STATE_SUPERSEDED):
        conversation, _, _ = _seed_binding(db, state=state)
        result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
        assert result.reason == UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT
        assert result.handle is None
        assert result.bound_scope is None


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
    _patch_ready_proof(monkeypatch)

    result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
    assert result.status == READ_STATUS_UNRESOLVED
    assert result.handle is None
    assert result.bound_scope is None
    assert result.reason == UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE


def test_cross_tenant_request_and_unready_capability_fail_closed(db, monkeypatch) -> None:
    conversation, _, _ = _seed_binding(db)
    cross_tenant = resolve_authoritative_a1_subject_for_conversation(
        db, request=_request(conversation, tenant_id=72),
    )
    assert cross_tenant.reason == UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH
    assert cross_tenant.handle is None
    assert cross_tenant.bound_scope is None

    _patch_unready_proof(monkeypatch)
    unavailable = resolve_authoritative_a1_subject_for_conversation(
        db, request=_request(conversation),
    )
    assert unavailable.reason == UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE
    assert unavailable.handle is None
    assert unavailable.bound_scope is None


def test_invalid_active_binding_fails_closed(db, monkeypatch) -> None:
    conversation, _, binding = _seed_binding(db)
    request = _request(conversation)
    binding.evidence_class = EVIDENCE_INFERRED
    db.commit()
    _patch_ready_proof(monkeypatch)

    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)
    assert result.status == READ_STATUS_UNRESOLVED
    assert result.handle is None
    assert result.bound_scope is None
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
    _patch_ready_proof(monkeypatch)

    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)

    assert result.status == READ_STATUS_UNRESOLVED
    assert result.handle is None
    assert result.bound_scope is None
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
    _patch_ready_proof(monkeypatch)

    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)

    assert result.bound_scope is None
    assert result.handle is None
    assert result.reason == UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH
    db.rollback()


def test_missing_canonical_proof_and_proof_errors_fail_closed(db, monkeypatch) -> None:
    conversation, _, _ = _seed_binding(db)
    _patch_missing_proof(monkeypatch)
    absent = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
    assert absent.reason == UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE
    assert absent.handle is None
    assert absent.bound_scope is None

    def broken_proof(*args, **kwargs):
        raise RuntimeError("private database detail")

    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._canonical_policy_proof", broken_proof,
    )
    failed = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
    assert failed.reason == UNRESOLVED_REASON_READ_UNAVAILABLE
    assert failed.handle is None
    assert failed.bound_scope is None
    assert "private database detail" not in repr(failed)


@pytest.mark.parametrize("malformed_request", (None, object(), {"tenant_id": 71, "conversation_id": 1}))
def test_malformed_request_fails_closed_without_dereference(db, malformed_request) -> None:
    result = resolve_authoritative_a1_subject_for_conversation(
        db, request=malformed_request,
    )

    assert result.status == READ_STATUS_UNRESOLVED
    assert result.handle is None
    assert result.bound_scope is None
    assert result.reason == UNRESOLVED_REASON_INVALID_REQUEST


def test_resolve_never_flushes_caller_pending_invalid_writes(db, monkeypatch) -> None:
    conversation, _, _ = _seed_binding(db)
    request = _request(conversation)
    pending_invalid = ConversationA1SubjectBinding()
    db.add(pending_invalid)
    _patch_ready_proof(monkeypatch)

    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)

    assert result.status == READ_STATUS_RESOLVED
    assert result.bound_scope is not None
    assert pending_invalid in db.new
    db.rollback()


def test_read_logs_and_results_do_not_serialize_private_identifiers(db, monkeypatch, caplog) -> None:
    conversation, customer, binding = _seed_binding(db)
    _patch_ready_proof(monkeypatch)
    with caplog.at_level(logging.INFO, logger="nahla.conversation_a1_subject_read"):
        result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))

    serialized = repr(result)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    # Numeric primary-key substrings are not meaningful privacy checks (for
    # example, the closed A1 evidence label contains "1"). Check the opaque
    # UUID and deliberately private source values instead.
    forbidden = (
        str(binding.id),
        customer.phone,
        "private-order-reference",
    )
    for value in forbidden:
        assert value not in serialized
        assert value not in logged
    with pytest.raises(TypeError):
        pickle.dumps(result)
    with pytest.raises(TypeError):
        pickle.dumps(result.bound_scope)
    with pytest.raises(TypeError):
        pickle.dumps(result.bound_scope.proof_snapshot())
    with pytest.raises(TypeError):
        copy.copy(result.handle)
    with pytest.raises(TypeError):
        copy.copy(result.bound_scope)
    with pytest.raises(TypeError):
        json.dumps(result)


def _seed_external_binding(db, *, tenant_id: int = 71):
    integration = Integration(
        id=501,
        tenant_id=tenant_id,
        provider="salla",
        external_store_id="generic-store",
        enabled=True,
    )
    db.add(integration)
    db.flush()
    profile = ExternalCustomerProfile(
        tenant_id=tenant_id,
        identity_namespace=EXTERNAL_PROVIDER_SALLA_V1,
        integration_connection_id=integration.id,
        external_customer_ref="opaque-external-ref",
    )
    db.add(profile)
    db.flush()
    customer = Customer(tenant_id=tenant_id, name="عميل خارجي", phone="966500055566")
    db.add(customer)
    db.flush()
    conversation = Conversation(tenant_id=tenant_id, status="open", customer_id=customer.id)
    db.add(conversation)
    db.flush()
    now = datetime.now(timezone.utc)
    binding = ConversationA1SubjectBinding(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        subject_kind=SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE,
        identity_namespace=EXTERNAL_PROVIDER_SALLA_V1,
        external_customer_profile_id=profile.id,
        binding_state=BINDING_STATE_ACTIVE,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        binding_source=BINDING_SOURCE_PROVIDER_OAUTH_SESSION,
        provenance_kind="webhook_event",
        provenance_id="private-external-provenance",
        bound_at=now,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(binding)
    db.commit()
    return conversation, profile, binding


def test_external_customer_binding_returns_bound_scope(db, monkeypatch) -> None:
    conversation, profile, binding = _seed_external_binding(db)
    _patch_ready_proof(monkeypatch)

    result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))

    assert result.status == READ_STATUS_RESOLVED
    assert result.bound_scope is not None
    assert result.bound_scope.subject_kind() == SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE
    assert result.bound_scope.identity_namespace() == EXTERNAL_PROVIDER_SALLA_V1
    assert result.bound_scope.external_customer_profile_id() == profile.id
    assert result.bound_scope.internal_customer_id() is None
    assert result.bound_scope.binding_source() == BINDING_SOURCE_PROVIDER_OAUTH_SESSION
    assert result.handle.is_bound_to(result.bound_scope)


def test_handle_scope_pair_rejects_forgery_and_cross_resolution(db, monkeypatch) -> None:
    conversation, _, binding = _seed_binding(db)
    _patch_ready_proof(monkeypatch)
    first = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
    second = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
    assert first.handle is not None and first.bound_scope is not None
    assert second.handle is not None and second.bound_scope is not None

    assert first.handle.is_bound_to(first.bound_scope)
    assert second.handle.is_bound_to(second.bound_scope)
    assert not first.handle.is_bound_to(second.bound_scope)
    assert not second.handle.is_bound_to(first.bound_scope)
    assert not first.bound_scope.proof_snapshot().is_bound_to(second.bound_scope)
    assert not second.bound_scope.proof_snapshot().is_bound_to(first.bound_scope)

    forged_proof = SimpleNamespace(
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        policy_eligibility_ready=True,
        authoritative_source_history_completeness=SOURCE_HISTORY_COMPLETE,
        forward_sync_health=SYNC_HEALTH_HEALTHY,
    )
    _, forged_scope = _issue_authoritative_a1_subject_pair(
        binding_key=binding.id,
        tenant_id=72,
        conversation_id=conversation.id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        binding_evidence_class=EVIDENCE_AUTHORITATIVE,
        proof=forged_proof,
        internal_customer_id=binding.internal_customer_id,
    )
    assert not first.handle.is_bound_to(forged_scope)
    assert not forged_scope.proof_snapshot().is_bound_to(first.bound_scope)


def test_public_handle_scope_and_snapshot_construction_is_blocked() -> None:
    binding_id = uuid4()
    forged_proof = SimpleNamespace(
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        policy_eligibility_ready=True,
        authoritative_source_history_completeness=SOURCE_HISTORY_COMPLETE,
        forward_sync_health=SYNC_HEALTH_HEALTHY,
    )
    with pytest.raises(TypeError, match="cannot be constructed outside Platform resolution"):
        AuthoritativeA1SubjectHandle(binding_id)
    with pytest.raises(TypeError, match="cannot be constructed outside Platform resolution"):
        BoundAuthoritativeA1PolicyProofSnapshot(
            binding_key=binding_id,
            subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
            policy_eligibility_ready=True,
            authoritative_source_history_completeness=SOURCE_HISTORY_COMPLETE,
            forward_sync_health=SYNC_HEALTH_HEALTHY,
        )
    with pytest.raises(TypeError, match="cannot be constructed outside Platform resolution"):
        BoundAuthoritativeA1SubjectScope(
            binding_key=binding_id,
            tenant_id=71,
            conversation_id=1,
            subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
            binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
            binding_evidence_class=EVIDENCE_AUTHORITATIVE,
            proof_subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
            proof_identity_namespace=NAHLA_INTERNAL_ORDER_V1,
            proof_policy_eligibility_ready=True,
            proof_snapshot=object(),
            internal_customer_id=1,
        )


@pytest.mark.parametrize("reason", sorted(UNRESOLVED_REASONS))
def test_unresolved_states_never_return_bound_scope(db, monkeypatch, reason) -> None:
    conversation, _, binding = _seed_binding(db)
    request = _request(conversation)

    if reason == UNRESOLVED_REASON_INVALID_REQUEST:
        request = object()
    elif reason == UNRESOLVED_REASON_INVALID_TENANT:
        request = TrustedConversationA1SubjectReadRequest(tenant_id=0, conversation_id=conversation.id)
    elif reason == UNRESOLVED_REASON_INVALID_CONVERSATION:
        request = TrustedConversationA1SubjectReadRequest(tenant_id=71, conversation_id=0)
    elif reason == UNRESOLVED_REASON_CONVERSATION_ABSENT:
        request = TrustedConversationA1SubjectReadRequest(tenant_id=71, conversation_id=999_999)
    elif reason == UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH:
        request = _request(conversation, tenant_id=72)
    elif reason == UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT:
        binding.binding_state = BINDING_STATE_REVOKED
        binding.revoked_at = datetime.now(timezone.utc)
        db.commit()
    elif reason == UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE:
        now = datetime.now(timezone.utc)
        db.add(ConversationA1SubjectBinding(
            tenant_id=71,
            conversation_id=conversation.id,
            subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
            internal_customer_id=binding.internal_customer_id,
            binding_state=BINDING_STATE_ACTIVE,
            evidence_class=EVIDENCE_AUTHORITATIVE,
            binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
            provenance_kind="order",
            provenance_id="duplicate-private-reference",
            bound_at=now,
            created_at=now,
            updated_at=now,
        ))
        db.execute(text("DROP INDEX uq_casb_tenant_conversation_active"))
        db.commit()
    elif reason == UNRESOLVED_REASON_BINDING_NOT_AUTHORITATIVE:
        binding.evidence_class = EVIDENCE_INFERRED
        db.commit()
    elif reason == UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID:
        binding.subject_kind = "unknown_subject"
    elif reason == UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID:
        binding.identity_namespace = "unknown_namespace"
    elif reason == UNRESOLVED_REASON_BINDING_SOURCE_INVALID:
        binding.binding_source = "untrusted_source"
    elif reason == UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH:
        binding.internal_customer_id = 999_999
    elif reason == UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE:
        _patch_missing_proof(monkeypatch)
    elif reason == UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE:
        _patch_unready_proof(monkeypatch)
    elif reason == UNRESOLVED_REASON_READ_UNAVAILABLE:
        def broken_proof(*args, **kwargs):
            raise RuntimeError("private database detail")

        monkeypatch.setattr(
            "services.conversation_a1_subject_read_service._canonical_policy_proof",
            broken_proof,
        )
    else:
        pytest.fail(f"unhandled unresolved reason fixture: {reason}")

    if reason not in {
        UNRESOLVED_REASON_CANONICAL_PROOF_UNAVAILABLE,
        UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE,
        UNRESOLVED_REASON_READ_UNAVAILABLE,
    }:
        _patch_ready_proof(monkeypatch)

    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)

    assert result.status == READ_STATUS_UNRESOLVED
    assert result.handle is None
    assert result.bound_scope is None
    assert result.reason == reason
    db.rollback()


def test_single_resolution_uses_one_binding_lookup_and_one_proof_build(db, monkeypatch) -> None:
    conversation, _, _ = _seed_binding(db)
    binding_queries = 0
    proof_calls = 0
    session_cls = type(db)
    original_query = session_cls.query

    def guarded_query(self, *entities, **kwargs):
        nonlocal binding_queries
        if entities and entities[0] is ConversationA1SubjectBinding:
            binding_queries += 1
            if binding_queries > 1:
                raise AssertionError("second ConversationA1SubjectBinding query")
        return original_query(self, *entities, **kwargs)

    def counting_proof(db_arg, *, binding, tenant_id):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls > 1:
            raise AssertionError("second canonical proof build")
        return _ready_proof(binding=binding)

    monkeypatch.setattr(session_cls, "query", guarded_query)
    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._canonical_policy_proof",
        counting_proof,
    )

    result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))

    assert result.status == READ_STATUS_RESOLVED
    assert result.bound_scope is not None
    assert binding_queries == 1
    assert proof_calls == 1


def test_resolve_has_no_binding_write_side_effects(db, monkeypatch) -> None:
    conversation, _, _ = _seed_binding(db)
    _patch_ready_proof(monkeypatch)
    before = db.query(ConversationA1SubjectBinding).count()

    resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))

    assert db.query(ConversationA1SubjectBinding).count() == before
    assert not db.dirty
    assert not db.deleted


def test_proof_snapshot_reflects_canonical_proof_categorical_fields(db, monkeypatch) -> None:
    conversation, _, binding = _seed_binding(db)

    def categorical_proof(db_arg, *, binding, tenant_id):
        return SimpleNamespace(
            subject_kind=binding.subject_kind,
            identity_namespace=binding.identity_namespace,
            policy_eligibility_ready=True,
            authoritative_source_history_completeness=SOURCE_HISTORY_INCOMPLETE,
            forward_sync_health=SYNC_HEALTH_DEGRADED,
        )

    monkeypatch.setattr(
        "services.conversation_a1_subject_read_service._canonical_policy_proof",
        categorical_proof,
    )

    result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))

    assert result.status == READ_STATUS_RESOLVED
    snapshot = result.bound_scope.proof_snapshot()
    assert snapshot.authoritative_source_history_completeness() == SOURCE_HISTORY_INCOMPLETE
    assert snapshot.forward_sync_health() == SYNC_HEALTH_DEGRADED
    assert snapshot.policy_eligibility_ready() is True
    assert snapshot.subject_kind() == str(binding.subject_kind)
    assert snapshot.identity_namespace() == str(binding.identity_namespace)


def test_proof_snapshot_privacy_and_serialization_blocked(db, monkeypatch, caplog) -> None:
    conversation, customer, binding = _seed_binding(db)
    _patch_ready_proof(monkeypatch)
    result = resolve_authoritative_a1_subject_for_conversation(db, request=_request(conversation))
    snapshot = result.bound_scope.proof_snapshot()

    serialized = repr(snapshot)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    forbidden = (
        str(binding.id),
        customer.phone,
        "private-order-reference",
    )
    for value in forbidden:
        assert value not in serialized
        assert value not in logged
    with pytest.raises(TypeError):
        pickle.dumps(snapshot)
    with pytest.raises(TypeError):
        copy.copy(snapshot)
    with pytest.raises(TypeError):
        json.dumps({"snapshot": snapshot})
