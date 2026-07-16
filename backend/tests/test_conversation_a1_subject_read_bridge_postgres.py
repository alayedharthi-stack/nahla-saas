"""PostgreSQL integration coverage for the conversation A1 subject read bridge."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from models import Conversation, ConversationA1SubjectBinding  # noqa: E402
from services.conversation_a1_subject_binding_contract import (  # noqa: E402
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    BINDING_STATE_ACTIVE,
    BINDING_STATE_REVOKED,
    BINDING_STATE_SUPERSEDED,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
)
from services.conversation_a1_subject_read_contract import (  # noqa: E402
    READ_STATUS_RESOLVED,
    TrustedConversationA1SubjectReadRequest,
    UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID,
    UNRESOLVED_REASON_BINDING_SOURCE_INVALID,
    UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID,
    UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE,
    UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH,
    UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT,
    UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH,
)
from services.conversation_a1_subject_read_service import (  # noqa: E402
    resolve_authoritative_a1_subject_for_conversation,
)
from services.order_customer_identity_contract import (  # noqa: E402
    CAPABILITY_STATE_EXPAND,
    CAPABILITY_STATE_VALIDATED,
    EVIDENCE_AUTHORITATIVE,
    NAHLA_INTERNAL_ORDER_V1,
)
from services.order_customer_identity_service import reconcile_internal_customer_coverage  # noqa: E402
from tests.order_customer_identity_postgres_fixtures import (  # noqa: E402
    TEST_TENANT_A,
    TEST_TENANT_B,
    pg_session,
    postgres_engine,
    seed_capability_state,
    seed_customer,
    seed_internal_order,
    seed_tenant,
)

pytestmark = pytest.mark.usefixtures("postgres_engine")


def _seed_policy_ready_binding(pg_session):
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A, name="متجر تجريبي عام")
    customer = seed_customer(pg_session, tenant_id=TEST_TENANT_A, name="عميل تجريبي")
    conversation = Conversation(tenant_id=TEST_TENANT_A, status="open", customer_id=customer.id)
    pg_session.add(conversation)
    pg_session.flush()
    seed_internal_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id=f"generic-order-{conversation.id}",
        customer_id=customer.id,
    )
    seed_capability_state(
        pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088",
    )
    reconcile_internal_customer_coverage(
        pg_session, tenant_id=TEST_TENANT_A, customer_id=customer.id,
    )
    now = datetime.now(timezone.utc)
    pg_session.add(ConversationA1SubjectBinding(
        tenant_id=TEST_TENANT_A,
        conversation_id=conversation.id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        internal_customer_id=customer.id,
        binding_state=BINDING_STATE_ACTIVE,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        provenance_kind="order",
        provenance_id="opaque-test-provenance",
        bound_at=now,
        created_at=now,
        updated_at=now,
    ))
    pg_session.flush()
    return conversation


def test_postgres_resolves_only_policy_ready_authoritative_binding(pg_session) -> None:
    conversation = _seed_policy_ready_binding(pg_session)

    result = resolve_authoritative_a1_subject_for_conversation(
        pg_session,
        request=TrustedConversationA1SubjectReadRequest(
            tenant_id=TEST_TENANT_A, conversation_id=conversation.id,
        ),
    )

    assert result.status == READ_STATUS_RESOLVED
    assert result.handle is not None


def test_postgres_capability_expand_fails_closed(pg_session) -> None:
    conversation = _seed_policy_ready_binding(pg_session)
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)

    result = resolve_authoritative_a1_subject_for_conversation(
        pg_session,
        request=TrustedConversationA1SubjectReadRequest(
            tenant_id=TEST_TENANT_A, conversation_id=conversation.id,
        ),
    )

    assert result.reason == UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE

    # A capability transition is evaluated at read time; the same persisted
    # canonical proof becomes eligible only after validated state is restored.
    seed_capability_state(
        pg_session, state=CAPABILITY_STATE_VALIDATED, validation_revision="0088",
    )
    resolved = resolve_authoritative_a1_subject_for_conversation(
        pg_session,
        request=TrustedConversationA1SubjectReadRequest(
            tenant_id=TEST_TENANT_A, conversation_id=conversation.id,
        ),
    )
    assert resolved.status == READ_STATUS_RESOLVED


@pytest.mark.parametrize(
    ("attribute", "value", "reason"),
    [
        ("subject_kind", "invalid_kind", UNRESOLVED_REASON_BINDING_SUBJECT_KIND_INVALID),
        ("identity_namespace", "invalid_namespace", UNRESOLVED_REASON_BINDING_NAMESPACE_INVALID),
        ("binding_source", "invalid_source", UNRESOLVED_REASON_BINDING_SOURCE_INVALID),
    ],
)
def test_postgres_invalid_active_binding_values_fail_closed(
    pg_session, attribute, value, reason,
) -> None:
    conversation = _seed_policy_ready_binding(pg_session)
    binding = (
        pg_session.query(ConversationA1SubjectBinding)
        .filter_by(tenant_id=TEST_TENANT_A, conversation_id=conversation.id)
        .one()
    )
    setattr(binding, attribute, value)

    result = resolve_authoritative_a1_subject_for_conversation(
        pg_session,
        request=TrustedConversationA1SubjectReadRequest(
            tenant_id=TEST_TENANT_A, conversation_id=conversation.id,
        ),
    )

    assert result.reason == reason


def test_postgres_subject_and_conversation_tenant_isolation_fail_closed(pg_session) -> None:
    conversation = _seed_policy_ready_binding(pg_session)
    seed_tenant(pg_session, tenant_id=TEST_TENANT_B, name="متجر تجريبي آخر")
    other_customer = seed_customer(pg_session, tenant_id=TEST_TENANT_B, name="عميل آخر")
    binding = (
        pg_session.query(ConversationA1SubjectBinding)
        .filter_by(tenant_id=TEST_TENANT_A, conversation_id=conversation.id)
        .one()
    )
    binding.internal_customer_id = other_customer.id

    wrong_subject = resolve_authoritative_a1_subject_for_conversation(
        pg_session,
        request=TrustedConversationA1SubjectReadRequest(
            tenant_id=TEST_TENANT_A, conversation_id=conversation.id,
        ),
    )
    assert wrong_subject.reason == UNRESOLVED_REASON_SUBJECT_ABSENT_OR_TENANT_MISMATCH

    wrong_conversation_tenant = resolve_authoritative_a1_subject_for_conversation(
        pg_session,
        request=TrustedConversationA1SubjectReadRequest(
            tenant_id=TEST_TENANT_B, conversation_id=conversation.id,
        ),
    )
    assert wrong_conversation_tenant.reason == UNRESOLVED_REASON_CONVERSATION_TENANT_MISMATCH


@pytest.mark.parametrize("state", (BINDING_STATE_REVOKED, BINDING_STATE_SUPERSEDED))
def test_postgres_non_active_bindings_are_not_readable(pg_session, state) -> None:
    conversation = _seed_policy_ready_binding(pg_session)
    binding = (
        pg_session.query(ConversationA1SubjectBinding)
        .filter_by(tenant_id=TEST_TENANT_A, conversation_id=conversation.id)
        .one()
    )
    binding.binding_state = state
    binding.revoked_at = datetime.now(timezone.utc)
    pg_session.flush()

    result = resolve_authoritative_a1_subject_for_conversation(
        pg_session,
        request=TrustedConversationA1SubjectReadRequest(
            tenant_id=TEST_TENANT_A, conversation_id=conversation.id,
        ),
    )

    assert result.reason == UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT
