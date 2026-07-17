"""AI consumer tests for Platform conversation → A1 subject read bridge."""
from __future__ import annotations

import logging
import os
import pickle
import sys
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for entry in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from models import (  # noqa: E402
    Base,
    Conversation,
    ConversationA1SubjectBinding,
    Customer,
    Tenant,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    REASON_CUSTOMER_UNVERIFIED,
    REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE,
    REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED,
    REASON_ORDER_HISTORY_SYNC_DEGRADED,
    REASON_ORDER_HISTORY_SYNC_STALE,
    REASON_PROOF_ABSENT,
    REASON_SUBJECT_AMBIGUOUS,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_loader import (  # noqa: E402
    clear_customer_conditional_coupon_turn_cache,
    load_customer_conditional_coupon_facts,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_subject import (  # noqa: E402
    REASON_HANDLE_UNAVAILABLE,
    REASON_MISSING_TENANT,
    resolve_conditional_coupon_subject_handle,
)
from services.conversation_a1_subject_binding_contract import (  # noqa: E402
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    BINDING_STATE_ACTIVE,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
)
from services.conversation_a1_subject_read_contract import (  # noqa: E402
    ConversationA1SubjectReadResult,
    READ_STATUS_RESOLVED,
    READ_STATUS_UNRESOLVED,
    UNRESOLVED_REASONS,
    UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT,
    UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE,
    UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE,
    UNRESOLVED_REASON_CONVERSATION_ABSENT,
    UNRESOLVED_REASON_INVALID_CONVERSATION,
    UNRESOLVED_REASON_INVALID_REQUEST,
    UNRESOLVED_REASON_INVALID_TENANT,
    UNRESOLVED_REASON_READ_UNAVAILABLE,
    _issue_authoritative_a1_subject_pair,
)
from services.order_customer_identity_contract import (  # noqa: E402
    EVIDENCE_AUTHORITATIVE,
    NAHLA_INTERNAL_ORDER_V1,
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_HEALTHY,
    SYNC_HEALTH_STALE,
)


@event.listens_for(Base.metadata, "before_create")
def _sqlite_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()


@pytest.fixture(autouse=True)
def _clear_turn_cache() -> None:
    clear_customer_conditional_coupon_turn_cache()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id=81, name="متجر تجريبي عام"))
    session.commit()
    yield session
    session.close()


def _seed_internal_binding(db):
    customer = Customer(tenant_id=81, name="أحمد سالم", phone="966500033344")
    db.add(customer)
    db.flush()
    conversation = Conversation(tenant_id=81, status="open", customer_id=customer.id)
    db.add(conversation)
    db.flush()
    binding = ConversationA1SubjectBinding(
        tenant_id=81,
        conversation_id=conversation.id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        internal_customer_id=customer.id,
        binding_state=BINDING_STATE_ACTIVE,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        provenance_kind="order",
        provenance_id="scoped-order-ref",
        bound_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    db.add(binding)
    db.commit()
    return conversation, customer, binding


def _resolver_proof_namespace(**overrides: object) -> SimpleNamespace:
    values = {
        "subject_kind": SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        "identity_namespace": NAHLA_INTERNAL_ORDER_V1,
        "policy_eligibility_ready": True,
        "authoritative_source_history_completeness": SOURCE_HISTORY_COMPLETE,
        "forward_sync_health": SYNC_HEALTH_HEALTHY,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _paired_bridge_result(
    *,
    binding_id,
    tenant_id: int,
    conversation_id: int,
    customer_id: int,
    proof_overrides: Optional[dict] = None,
) -> ConversationA1SubjectReadResult:
    handle, bound_scope = _issue_authoritative_a1_subject_pair(
        binding_key=binding_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        binding_evidence_class=EVIDENCE_AUTHORITATIVE,
        proof=_resolver_proof_namespace(**(proof_overrides or {})),
        internal_customer_id=customer_id,
    )
    return ConversationA1SubjectReadResult(
        status=READ_STATUS_RESOLVED,
        handle=handle,
        bound_scope=bound_scope,
        evidence_class="authoritative_a1_policy_eligible",
    )


def test_tenant_mismatch_in_bound_scope_fails_closed(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation, customer, binding = _seed_internal_binding(db)
    bridge_result = _paired_bridge_result(
        binding_id=binding.id,
        tenant_id=82,
        conversation_id=conversation.id,
        customer_id=customer.id,
    )
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        lambda _db, *, request: bridge_result,
    )
    result = resolve_conditional_coupon_subject_handle(
        tenant_id=81,
        db=db,
        conversation=conversation,
    )
    assert result.status == "unresolved"
    assert result.reason_code == REASON_CUSTOMER_UNVERIFIED
    assert result.handle is None


def test_wrong_snapshot_subject_kind_fails_closed(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation, customer, binding = _seed_internal_binding(db)
    bridge_result = _paired_bridge_result(
        binding_id=binding.id,
        tenant_id=81,
        conversation_id=conversation.id,
        customer_id=customer.id,
        proof_overrides={"subject_kind": "external_customer_profile"},
    )
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        lambda _db, *, request: bridge_result,
    )
    result = resolve_conditional_coupon_subject_handle(
        tenant_id=81,
        db=db,
        conversation=conversation,
    )
    assert result.status == "unresolved"
    assert result.reason_code == REASON_CUSTOMER_UNVERIFIED
    assert result.handle is None


def test_loader_uses_resolver_snapshot_without_post_bridge_proof_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    paired = _paired_bridge_result(
        binding_id=uuid4(),
        tenant_id=81,
        conversation_id=5,
        customer_id=42,
    )
    promo = SimpleNamespace(
        conditions={"min_orders_for_eligibility": 2},
        extra_metadata={},
    )
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        return_value=paired,
    ), patch(
        "services.order_customer_identity_read_contract.build_safe_internal_customer_proof",
    ) as internal_proof, patch(
        "services.order_customer_identity_read_contract.build_safe_external_profile_proof",
    ) as external_proof, patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[promo],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
        return_value=2,
    ):
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=81,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(id=5),
        )
    internal_proof.assert_not_called()
    external_proof.assert_not_called()
    assert facts[0].value["order_history_completeness"] == COMPLETENESS_VERIFIED
    assert obs["order_count_query_count"] == 1


def test_mismatched_snapshot_loader_skips_target_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    first = _paired_bridge_result(
        binding_id=uuid4(),
        tenant_id=81,
        conversation_id=3,
        customer_id=9,
        proof_overrides={"subject_kind": "external_customer_profile"},
    )
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        return_value=first,
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[SimpleNamespace()],
    ) as discover:
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=81,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(id=3),
        )
        discover.assert_not_called()
    assert facts[0].value["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED


def test_missing_bound_scope_loader_proof_absent_no_target_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    resolved = _paired_bridge_result(
        binding_id=uuid4(),
        tenant_id=81,
        conversation_id=3,
        customer_id=9,
    )
    broken_handle = resolved.handle
    assert broken_handle is not None
    from modules.ai.brain.truth_surface.customer_conditional_coupon_subject import (  # noqa: PLC0415
        ConditionalCouponSubjectHandle,
        HANDLE_SOURCE_BRIDGE,
        SubjectResolutionResult,
    )

    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
        return_value=SubjectResolutionResult(
            status="resolved",
            handle=ConditionalCouponSubjectHandle(
                subject_kind="nahla_internal_customer",
                tenant_id=81,
                identity_namespace=NAHLA_INTERNAL_ORDER_V1,
                handle_source=HANDLE_SOURCE_BRIDGE,
                customer_id=9,
                authoritative_a1_subject_handle=broken_handle,
                bound_authoritative_a1_subject_scope=None,
            ),
        ),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[SimpleNamespace()],
    ) as discover:
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=81,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(id=3),
        )
        discover.assert_not_called()
    assert facts[0].value["closed_reason_code"] == REASON_PROOF_ABSENT


def test_bridge_resolved_uses_resolver_bound_scope_only(db, monkeypatch) -> None:
    conversation, customer, binding = _seed_internal_binding(db)
    bridge_result = _paired_bridge_result(
        binding_id=binding.id,
        tenant_id=81,
        conversation_id=conversation.id,
        customer_id=customer.id,
    )
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        lambda _db, *, request: bridge_result,
    )

    result = resolve_conditional_coupon_subject_handle(
        tenant_id=81,
        db=db,
        conversation=conversation,
    )

    assert result.status == "resolved"
    assert result.handle is not None
    assert result.handle.handle_source == "conversation_a1_subject_read_bridge"
    assert result.handle.customer_id == customer.id
    assert result.handle.authoritative_a1_subject_handle is bridge_result.handle
    assert bridge_result.handle.is_bound_to(bridge_result.bound_scope)
    assert repr(result.handle.authoritative_a1_subject_handle) == "AuthoritativeA1SubjectHandle()"
    assert str(binding.id) not in repr(result)
    assert str(binding.id) not in repr(result.handle)


def test_resolver_issued_handle_and_bound_scope_both_required(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation, customer, binding = _seed_internal_binding(db)
    paired = _paired_bridge_result(
        binding_id=binding.id,
        tenant_id=81,
        conversation_id=conversation.id,
        customer_id=customer.id,
    )

    missing_scope = ConversationA1SubjectReadResult(
        status=READ_STATUS_RESOLVED,
        handle=paired.handle,
        bound_scope=None,
        evidence_class="authoritative_a1_policy_eligible",
    )
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        lambda _db, *, request: missing_scope,
    )
    result = resolve_conditional_coupon_subject_handle(
        tenant_id=81,
        db=db,
        conversation=conversation,
    )
    assert result.status == "unresolved"
    assert result.reason_code == REASON_CUSTOMER_UNVERIFIED
    assert result.handle is None


def test_mismatched_cross_resolution_pair_fails_closed(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation, customer, binding = _seed_internal_binding(db)
    first = _paired_bridge_result(
        binding_id=binding.id,
        tenant_id=81,
        conversation_id=conversation.id,
        customer_id=customer.id,
    )
    _, forged_scope = _issue_authoritative_a1_subject_pair(
        binding_key=uuid4(),
        tenant_id=81,
        conversation_id=conversation.id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        binding_evidence_class=EVIDENCE_AUTHORITATIVE,
        proof=_resolver_proof_namespace(),
        internal_customer_id=customer.id,
    )
    mismatched = ConversationA1SubjectReadResult(
        status=READ_STATUS_RESOLVED,
        handle=first.handle,
        bound_scope=forged_scope,
        evidence_class="authoritative_a1_policy_eligible",
    )
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        lambda _db, *, request: mismatched,
    )
    result = resolve_conditional_coupon_subject_handle(
        tenant_id=81,
        db=db,
        conversation=conversation,
    )
    assert result.status == "unresolved"
    assert result.reason_code == REASON_CUSTOMER_UNVERIFIED
    assert result.handle is None
    assert not first.handle.is_bound_to(forged_scope)


def test_no_post_bridge_binding_or_proof_reads() -> None:
    db = MagicMock()
    paired = _paired_bridge_result(
        binding_id=uuid4(),
        tenant_id=81,
        conversation_id=5,
        customer_id=42,
    )
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        return_value=paired,
    ):
        result = resolve_conditional_coupon_subject_handle(
            tenant_id=81,
            db=db,
            conversation=SimpleNamespace(id=5),
        )
    assert result.status == "resolved"
    db.query.assert_not_called()


@pytest.mark.parametrize("bridge_reason", sorted(UNRESOLVED_REASONS))
def test_bridge_unresolved_maps_to_closed_coupon_reasons(
    db,
    monkeypatch: pytest.MonkeyPatch,
    bridge_reason: str,
) -> None:
    conversation, _, _ = _seed_internal_binding(db)
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        lambda _db, *, request: ConversationA1SubjectReadResult(
            status=READ_STATUS_UNRESOLVED,
            reason=bridge_reason,
        ),
    )

    result = resolve_conditional_coupon_subject_handle(
        tenant_id=81,
        db=db,
        conversation=conversation,
    )

    assert result.handle is None
    assert bridge_reason not in str(result.reason_code)
    if bridge_reason == UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE:
        assert result.status == "ambiguous"
        assert result.reason_code == REASON_SUBJECT_AMBIGUOUS
    else:
        assert result.status == "unresolved"
        assert result.reason_code == REASON_CUSTOMER_UNVERIFIED


@pytest.mark.parametrize(
    "conversation",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(id=0),
        SimpleNamespace(id=-1),
        SimpleNamespace(id="not-int"),
    ],
)
def test_malformed_conversation_context_fails_closed(
    db,
    conversation,
) -> None:
    result = resolve_conditional_coupon_subject_handle(
        tenant_id=81,
        db=db,
        conversation=conversation,
    )
    assert result.status == "unresolved"
    assert result.reason_code == REASON_HANDLE_UNAVAILABLE
    assert result.handle is None


def test_missing_db_or_tenant_fails_closed_without_bridge_call(db) -> None:
    conversation = SimpleNamespace(id=1)
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
    ) as bridge:
        no_db = resolve_conditional_coupon_subject_handle(
            tenant_id=81,
            conversation=conversation,
        )
        assert no_db.reason_code == REASON_HANDLE_UNAVAILABLE

        no_tenant = resolve_conditional_coupon_subject_handle(
            tenant_id=0,
            db=db,
            conversation=conversation,
        )
        assert no_tenant.reason_code == REASON_MISSING_TENANT

        bridge.assert_not_called()


def test_untrusted_conversation_customer_id_is_ignored(db, monkeypatch) -> None:
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        MagicMock(
            return_value=ConversationA1SubjectReadResult(
                status=READ_STATUS_UNRESOLVED,
                reason=UNRESOLVED_REASON_CONVERSATION_ABSENT,
            ),
        ),
    )
    result = resolve_conditional_coupon_subject_handle(
        tenant_id=81,
        db=db,
        conversation=SimpleNamespace(id=99, customer_id=12345),
        inbound_metadata={"customer_id": 77, "external_customer_profile_id": str(uuid4())},
    )
    assert result.status == "unresolved"
    assert result.handle is None


def test_shadow_flag_off_never_invokes_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED",
        raising=False,
    )
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
    ) as bridge, patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
    ) as resolver:
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=81,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(id=1),
        )
        assert facts == []
        assert obs["gate_skipped_reason"] == "layer0_flags_disabled"
        bridge.assert_not_called()
        resolver.assert_not_called()


def test_loader_ambiguous_bridge_maps_to_subject_ambiguous_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        return_value=ConversationA1SubjectReadResult(
            status=READ_STATUS_UNRESOLVED,
            reason=UNRESOLVED_REASON_ACTIVE_BINDING_MULTIPLE,
        ),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[],
    ) as discover:
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=81,
            message="conditional coupon after min orders",
            conversation=SimpleNamespace(id=5),
        )
        discover.assert_not_called()
    assert facts[0].value["identity_status"] == "ambiguous"
    assert facts[0].value["closed_reason_code"] == REASON_SUBJECT_AMBIGUOUS


def test_opaque_handle_not_serializable() -> None:
    handle, _scope = _issue_authoritative_a1_subject_pair(
        binding_key=uuid4(),
        tenant_id=81,
        conversation_id=1,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        binding_evidence_class=EVIDENCE_AUTHORITATIVE,
        proof=_resolver_proof_namespace(),
        internal_customer_id=1,
    )
    with pytest.raises(TypeError):
        pickle.dumps(handle)


def test_bridge_consumer_logs_no_binding_identifiers(
    db,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversation, customer, binding = _seed_internal_binding(db)
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        lambda _db, *, request: _paired_bridge_result(
            binding_id=binding.id,
            tenant_id=81,
            conversation_id=conversation.id,
            customer_id=customer.id,
        ),
    )

    resolve_conditional_coupon_subject_handle(
        tenant_id=81,
        db=db,
        conversation=conversation,
    )

    blob = caplog.text
    assert str(binding.id) not in blob


@pytest.mark.parametrize(
    "bridge_reason",
    [
        UNRESOLVED_REASON_INVALID_REQUEST,
        UNRESOLVED_REASON_INVALID_TENANT,
        UNRESOLVED_REASON_INVALID_CONVERSATION,
        UNRESOLVED_REASON_CAPABILITY_POLICY_UNAVAILABLE,
        UNRESOLVED_REASON_READ_UNAVAILABLE,
        UNRESOLVED_REASON_ACTIVE_BINDING_ABSENT,
    ],
)
def test_loader_unresolved_bridge_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    bridge_reason: str,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        return_value=ConversationA1SubjectReadResult(
            status=READ_STATUS_UNRESOLVED,
            reason=bridge_reason,
        ),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[SimpleNamespace()],
    ) as discover:
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=81,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(id=3),
        )
        discover.assert_not_called()
    record = facts[0].value
    assert record["identity_status"] == "unresolved"
    assert record["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED
    assert record["allow_min_orders_condition_claim"] is False
    assert bridge_reason not in str(record.values())


def test_mismatched_pair_loader_skips_target_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    first = _paired_bridge_result(
        binding_id=uuid4(),
        tenant_id=81,
        conversation_id=3,
        customer_id=9,
    )
    _, forged_scope = _issue_authoritative_a1_subject_pair(
        binding_key=uuid4(),
        tenant_id=81,
        conversation_id=3,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        binding_evidence_class=EVIDENCE_AUTHORITATIVE,
        proof=_resolver_proof_namespace(),
        internal_customer_id=9,
    )
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        return_value=ConversationA1SubjectReadResult(
            status=READ_STATUS_RESOLVED,
            handle=first.handle,
            bound_scope=forged_scope,
            evidence_class="authoritative_a1_policy_eligible",
        ),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[SimpleNamespace()],
    ) as discover:
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=81,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(id=3),
        )
        discover.assert_not_called()
    assert facts[0].value["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED


@pytest.mark.parametrize(
    ("proof_overrides", "expected_reason"),
    [
        (
            {"policy_eligibility_ready": False},
            REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED,
        ),
        (
            {
                "policy_eligibility_ready": False,
                "authoritative_source_history_completeness": SOURCE_HISTORY_INCOMPLETE,
            },
            REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE,
        ),
        (
            {
                "policy_eligibility_ready": False,
                "forward_sync_health": SYNC_HEALTH_STALE,
            },
            REASON_ORDER_HISTORY_SYNC_STALE,
        ),
        (
            {
                "policy_eligibility_ready": False,
                "forward_sync_health": SYNC_HEALTH_DEGRADED,
            },
            REASON_ORDER_HISTORY_SYNC_DEGRADED,
        ),
        (
            {
                "policy_eligibility_ready": True,
                "authoritative_source_history_completeness": SOURCE_HISTORY_INCOMPLETE,
            },
            REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE,
        ),
    ],
)
def test_snapshot_policy_gates_block_scan_and_count_before_targets(
    monkeypatch: pytest.MonkeyPatch,
    proof_overrides: dict,
    expected_reason: str,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    paired = _paired_bridge_result(
        binding_id=uuid4(),
        tenant_id=81,
        conversation_id=5,
        customer_id=42,
        proof_overrides=proof_overrides,
    )
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_subject."
        "resolve_authoritative_a1_subject_for_conversation",
        return_value=paired,
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
    ) as discover, patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
    ) as count_mock:
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=81,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(id=5),
        )
    discover.assert_not_called()
    count_mock.assert_not_called()
    assert facts[0].value["closed_reason_code"] == expected_reason
    assert obs["usage_evidence_query_count"] == 0
    assert obs["order_count_query_count"] == 0
