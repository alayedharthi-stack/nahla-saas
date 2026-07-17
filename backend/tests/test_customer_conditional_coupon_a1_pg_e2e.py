"""PostgreSQL E2E: authoritative A1 chain → conditional coupon Layer 0 facts.

Exercises real models/migrations, the conversation A1 subject read bridge, canonical
proof/capability state, promotion discovery, and countable order history through
``load_customer_conditional_coupon_facts``. Shadow flag stays default-off in runtime;
tests opt in locally via ``shadow_coupon_enabled``.

Ambiguous active binding (multiple ``binding_state='active'`` rows for one
conversation) is not seeded here: migration ``0089`` enforces
``uq_casb_tenant_conversation_active`` (partial unique on active rows). That
corruption class stays in mocked consumer tests where invalid pairs can be
injected without fighting PostgreSQL DDL.

Cross-tenant ``internal_customer_id`` corruption is likewise not persisted in PG:
``fk_casb_tenant_internal_customer`` rejects it at flush. Runtime fail-closed
loader behavior for a hypothetically readable corrupted binding remains in the
mocked consumer tests; this file proves PostgreSQL blocks the invalid state.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from models import Conversation, ConversationA1SubjectBinding, Order, Promotion  # noqa: E402
from modules.ai.brain.truth_surface.contract import TrustedDomain, TruthSource  # noqa: E402
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SATISFIED,
    EVALUATION_CONDITION_SHORTFALL,
    EVALUATION_REQUIRES_CONTEXT,
    MIN_ORDERS_STATE_SATISFIED,
    MIN_ORDERS_STATE_SHORTFALL,
    REASON_CUSTOMER_UNVERIFIED,
    REASON_NO_CONDITIONAL_TARGET,
    REASON_ORDERS_SHORTFALL,
    assert_fact_record_sanitized,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_loader import (  # noqa: E402
    clear_customer_conditional_coupon_turn_cache,
    load_customer_conditional_coupon_facts,
)
from services.conversation_a1_subject_binding_contract import (  # noqa: E402
    BINDING_SOURCE_PROVIDER_OAUTH_SESSION,
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    BINDING_STATE_ACTIVE,
    BINDING_STATE_REVOKED,
    SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
)
from services.order_customer_identity_contract import (  # noqa: E402
    CAPABILITY_STATE_EXPAND,
    CAPABILITY_STATE_VALIDATED,
    CUSTOMER_LINK_SOURCE_NAHL_BRIDGE,
    EVIDENCE_AUTHORITATIVE,
    EXTERNAL_PROVIDER_SALLA_V1,
    LINK_STATE_UNLINKED,
    LINK_STATE_VERIFIED,
    NAHLA_INTERNAL_ORDER_V1,
    ORDER_SOURCE_EXTERNAL_PROVIDER,
    ORDER_SOURCE_NAHL_INTERNAL,
)
from services.order_customer_identity_service import (  # noqa: E402
    reconcile_external_profile_coverage,
    reconcile_internal_customer_coverage,
)
from tests.order_customer_identity_postgres_fixtures import (  # noqa: E402
    TEST_TENANT_A,
    TEST_TENANT_B,
    pg_session,
    postgres_engine,
    seed_capability_state,
    seed_customer,
    seed_external_order,
    seed_external_profile,
    seed_integration,
    seed_internal_order,
    seed_tenant,
)

pytestmark = pytest.mark.usefixtures("postgres_engine")

_CONDITIONAL_INTENT_MESSAGE = "conditional coupon after min orders for loyalty offer"


@pytest.fixture(autouse=True)
def _clear_turn_cache() -> None:
    clear_customer_conditional_coupon_turn_cache()


@pytest.fixture
def shadow_coupon_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        "true",
    )


@contextmanager
def _no_runtime_side_effects() -> Iterator[None]:
    """Sentinels: Layer 0 read must not compose, dispatch, issue coupons, or mutate promos."""
    with (
        patch(
            "services.promotion_engine.materialise_for_customer",
            new_callable=MagicMock,
        ) as materialise,
        patch(
            "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
            "scan_conditional_targets",
            wraps=__import__(
                "modules.ai.brain.truth_surface.customer_conditional_coupon_repository",
                fromlist=["scan_conditional_targets"],
            ).scan_conditional_targets,
        ),
        patch(
            "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
            "count_countable_orders_for_subject",
            wraps=__import__(
                "modules.ai.brain.truth_surface.customer_conditional_coupon_repository",
                fromlist=["count_countable_orders_for_subject"],
            ).count_countable_orders_for_subject,
        ),
    ):
        yield
        materialise.assert_not_called()


def _promotion_count(pg_session) -> int:
    return int(pg_session.query(func.count(Promotion.id)).scalar() or 0)


def _seed_countable_internal_order(
    pg_session,
    *,
    tenant_id: int,
    customer_id: int,
    external_id: str,
    status: str = "completed",
) -> Order:
    row = Order(
        tenant_id=int(tenant_id),
        external_id=str(external_id),
        status=status,
        total="75.00",
        source="whatsapp",
        order_source_kind=ORDER_SOURCE_NAHL_INTERNAL,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        customer_id=int(customer_id),
        customer_link_state=LINK_STATE_VERIFIED,
        customer_link_evidence_class=EVIDENCE_AUTHORITATIVE,
        customer_link_source=CUSTOMER_LINK_SOURCE_NAHL_BRIDGE,
        is_abandoned=False,
    )
    pg_session.add(row)
    pg_session.flush()
    return row


def _seed_conditional_promotion(
    pg_session,
    *,
    tenant_id: int,
    min_orders: int,
    status: str = "active",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    usage_count: int = 0,
    usage_limit: int | None = 100,
) -> Promotion:
    now = datetime.now(timezone.utc)
    promo = Promotion(
        tenant_id=int(tenant_id),
        name="generic-loyalty-threshold",
        promotion_type="percentage",
        discount_value=10,
        conditions={"min_orders_for_eligibility": int(min_orders)},
        status=status,
        starts_at=starts_at if starts_at is not None else now - timedelta(hours=1),
        ends_at=ends_at if ends_at is not None else now + timedelta(days=30),
        usage_count=int(usage_count),
        usage_limit=usage_limit,
    )
    pg_session.add(promo)
    pg_session.flush()
    return promo


def _seed_active_conditional_promotion(
    pg_session,
    *,
    tenant_id: int,
    min_orders: int,
) -> Promotion:
    return _seed_conditional_promotion(
        pg_session,
        tenant_id=tenant_id,
        min_orders=min_orders,
    )


def _seed_countable_external_order(
    pg_session,
    *,
    tenant_id: int,
    integration_connection_id: int,
    external_customer_ref: str,
    external_customer_profile_id,
    external_id: str,
    status: str = "completed",
) -> Order:
    row = Order(
        tenant_id=int(tenant_id),
        external_id=str(external_id),
        status=status,
        total="120.00",
        source="salla",
        order_source_kind=ORDER_SOURCE_EXTERNAL_PROVIDER,
        identity_namespace=EXTERNAL_PROVIDER_SALLA_V1,
        integration_connection_id=int(integration_connection_id),
        external_customer_ref=str(external_customer_ref),
        external_customer_profile_id=external_customer_profile_id,
        external_identity_link_state=LINK_STATE_VERIFIED,
        external_identity_evidence_class=EVIDENCE_AUTHORITATIVE,
        is_abandoned=False,
    )
    pg_session.add(row)
    pg_session.flush()
    return row


def _seed_external_active_binding(
    pg_session,
    *,
    tenant_id: int,
    conversation_id: int,
    external_customer_profile_id,
) -> ConversationA1SubjectBinding:
    now = datetime.now(timezone.utc)
    binding = ConversationA1SubjectBinding(
        tenant_id=int(tenant_id),
        conversation_id=int(conversation_id),
        subject_kind=SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE,
        identity_namespace=EXTERNAL_PROVIDER_SALLA_V1,
        external_customer_profile_id=external_customer_profile_id,
        binding_state=BINDING_STATE_ACTIVE,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        binding_source=BINDING_SOURCE_PROVIDER_OAUTH_SESSION,
        provenance_kind="webhook_event",
        provenance_id="pg-e2e-external-provenance",
        bound_at=now,
        created_at=now,
        updated_at=now,
    )
    pg_session.add(binding)
    pg_session.flush()
    return binding


def _seed_external_policy_ready_chain(
    pg_session,
    *,
    countable_orders: int = 2,
    min_orders_threshold: int = 2,
):
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A, name="متجر تجريبي عام")
    integration = seed_integration(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_store_id="STORE-GENERIC-E2E",
    )
    profile = seed_external_profile(
        pg_session,
        tenant_id=TEST_TENANT_A,
        integration_connection_id=integration.id,
        external_customer_ref="GEN-PERFUME-CUST",
    )
    customer = seed_customer(
        pg_session,
        tenant_id=TEST_TENANT_A,
        name="نورة عبدالله",
    )
    conversation = Conversation(
        tenant_id=TEST_TENANT_A,
        status="open",
        customer_id=customer.id,
    )
    pg_session.add(conversation)
    pg_session.flush()

    for index in range(countable_orders):
        _seed_countable_external_order(
            pg_session,
            tenant_id=TEST_TENANT_A,
            integration_connection_id=integration.id,
            external_customer_ref="GEN-PERFUME-CUST",
            external_customer_profile_id=profile.id,
            external_id=f"generic-perfume-order-{conversation.id}-{index}",
        )

    seed_capability_state(
        pg_session,
        state=CAPABILITY_STATE_VALIDATED,
        validation_revision="0088",
    )
    reconcile_external_profile_coverage(pg_session, profile=profile)
    _seed_external_active_binding(
        pg_session,
        tenant_id=TEST_TENANT_A,
        conversation_id=conversation.id,
        external_customer_profile_id=profile.id,
    )
    promotion = _seed_active_conditional_promotion(
        pg_session,
        tenant_id=TEST_TENANT_A,
        min_orders=min_orders_threshold,
    )
    return conversation, profile, promotion


def _seed_active_binding(
    pg_session,
    *,
    tenant_id: int,
    conversation_id: int,
    customer_id: int,
) -> ConversationA1SubjectBinding:
    now = datetime.now(timezone.utc)
    binding = ConversationA1SubjectBinding(
        tenant_id=int(tenant_id),
        conversation_id=int(conversation_id),
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        internal_customer_id=int(customer_id),
        binding_state=BINDING_STATE_ACTIVE,
        evidence_class=EVIDENCE_AUTHORITATIVE,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        provenance_kind="order",
        provenance_id="pg-e2e-provenance",
        bound_at=now,
        created_at=now,
        updated_at=now,
    )
    pg_session.add(binding)
    pg_session.flush()
    return binding


def _seed_policy_ready_chain(
    pg_session,
    *,
    countable_orders: int = 2,
    min_orders_threshold: int = 2,
    reconcile: bool = True,
    capability_state: str = CAPABILITY_STATE_VALIDATED,
):
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A, name="متجر تجريبي عام")
    customer = seed_customer(
        pg_session,
        tenant_id=TEST_TENANT_A,
        name="أحمد سالم",
    )
    conversation = Conversation(
        tenant_id=TEST_TENANT_A,
        status="open",
        customer_id=customer.id,
    )
    pg_session.add(conversation)
    pg_session.flush()

    for index in range(countable_orders):
        _seed_countable_internal_order(
            pg_session,
            tenant_id=TEST_TENANT_A,
            customer_id=customer.id,
            external_id=f"generic-shoe-order-{conversation.id}-{index}",
            status="completed",
        )

    seed_capability_state(
        pg_session,
        state=capability_state,
        validation_revision="0088",
    )
    if reconcile:
        reconcile_internal_customer_coverage(
            pg_session,
            tenant_id=TEST_TENANT_A,
            customer_id=customer.id,
        )
    _seed_active_binding(
        pg_session,
        tenant_id=TEST_TENANT_A,
        conversation_id=conversation.id,
        customer_id=customer.id,
    )
    promotion = _seed_active_conditional_promotion(
        pg_session,
        tenant_id=TEST_TENANT_A,
        min_orders=min_orders_threshold,
    )
    return conversation, customer, promotion


def _load_facts(pg_session, conversation: Conversation):
    return load_customer_conditional_coupon_facts(
        db=pg_session,
        tenant_id=TEST_TENANT_A,
        message=_CONDITIONAL_INTENT_MESSAGE,
        conversation=conversation,
    )


def test_pg_shadow_flag_default_off_no_facts_or_queries(
    pg_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        raising=False,
    )
    monkeypatch.delenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED",
        raising=False,
    )
    conversation, _, _ = _seed_policy_ready_chain(pg_session)

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    assert facts == []
    assert obs["gate_skipped_reason"] == "layer0_flags_disabled"
    assert obs["order_count_query_count"] == 0
    assert obs["usage_evidence_query_count"] == 0


def test_pg_e2e_generic_merchant_satisfied_min_orders(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    conversation, _customer, promotion = _seed_policy_ready_chain(
        pg_session,
        countable_orders=2,
        min_orders_threshold=2,
    )
    promo_before = _promotion_count(pg_session)

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    assert _promotion_count(pg_session) == promo_before
    assert len(facts) == 1
    fact = facts[0]
    assert fact.domain == TrustedDomain.CUSTOMER_CONDITIONAL_COUPON
    assert fact.source == TruthSource.PROMOTION_TABLE
    assert fact.path == "customer_conditional_coupon_loader.layer0"

    record = fact.value
    assert_fact_record_sanitized(record)
    assert record["identity_status"] == "resolved"
    assert record["customer_scope"] == "nahla_internal_customer"
    assert record["order_history_completeness"] == COMPLETENESS_VERIFIED
    assert record["completed_orders_count"] == 2
    assert record["min_orders_for_eligibility"] == 2
    assert record["min_orders_condition_state"] == MIN_ORDERS_STATE_SATISFIED
    assert record["conditional_coupon_evaluation_state"] == EVALUATION_CONDITION_SATISFIED
    assert record["allow_min_orders_condition_claim"] is True
    assert record["closed_reason_code"] is None
    assert obs["order_count_query_count"] == 1
    assert obs["usage_evidence_query_count"] == 1
    assert obs["conditional_target_count"] >= 1
    assert promotion.conditions["min_orders_for_eligibility"] == 2


def test_pg_e2e_generic_merchant_orders_shortfall(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    conversation, _customer, _promotion = _seed_policy_ready_chain(
        pg_session,
        countable_orders=1,
        min_orders_threshold=3,
    )

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    record = facts[0].value
    assert record["completed_orders_count"] == 1
    assert record["min_orders_for_eligibility"] == 3
    assert record["orders_shortfall"] == 2
    assert record["min_orders_condition_state"] == MIN_ORDERS_STATE_SHORTFALL
    assert record["conditional_coupon_evaluation_state"] == EVALUATION_CONDITION_SHORTFALL
    assert record["closed_reason_code"] == REASON_ORDERS_SHORTFALL
    assert record["allow_min_orders_condition_claim"] is False
    assert obs["order_count_query_count"] == 1
    assert obs["usage_evidence_query_count"] == 1


def test_pg_e2e_external_profile_satisfied_min_orders(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    conversation, _profile, promotion = _seed_external_policy_ready_chain(
        pg_session,
        countable_orders=2,
        min_orders_threshold=2,
    )
    promo_before = _promotion_count(pg_session)

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    assert _promotion_count(pg_session) == promo_before
    record = facts[0].value
    assert_fact_record_sanitized(record)
    assert record["identity_status"] == "resolved"
    assert record["customer_scope"] == "external_customer_profile"
    assert record["order_history_completeness"] == COMPLETENESS_VERIFIED
    assert record["completed_orders_count"] == 2
    assert record["min_orders_for_eligibility"] == 2
    assert record["min_orders_condition_state"] == MIN_ORDERS_STATE_SATISFIED
    assert record["conditional_coupon_evaluation_state"] == EVALUATION_CONDITION_SATISFIED
    assert record["allow_min_orders_condition_claim"] is True
    assert obs["order_count_query_count"] == 1
    assert obs["usage_evidence_query_count"] == 1
    assert promotion.conditions["min_orders_for_eligibility"] == 2


def test_pg_e2e_external_profile_orders_shortfall(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    conversation, _profile, _promotion = _seed_external_policy_ready_chain(
        pg_session,
        countable_orders=1,
        min_orders_threshold=3,
    )

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    record = facts[0].value
    assert record["customer_scope"] == "external_customer_profile"
    assert record["completed_orders_count"] == 1
    assert record["min_orders_for_eligibility"] == 3
    assert record["orders_shortfall"] == 2
    assert record["min_orders_condition_state"] == MIN_ORDERS_STATE_SHORTFALL
    assert record["conditional_coupon_evaluation_state"] == EVALUATION_CONDITION_SHORTFALL
    assert record["closed_reason_code"] == REASON_ORDERS_SHORTFALL
    assert record["allow_min_orders_condition_claim"] is False
    assert obs["order_count_query_count"] == 1
    assert obs["usage_evidence_query_count"] == 1


@pytest.mark.parametrize(
    ("liveness_case", "promo_kwargs"),
    [
        (
            "expired",
            {
                "ends_at": datetime.now(timezone.utc) - timedelta(hours=2),
            },
        ),
        (
            "inactive",
            {
                "status": "paused",
            },
        ),
        (
            "usage_exhausted",
            {
                "usage_count": 5,
                "usage_limit": 5,
            },
        ),
    ],
)
def test_pg_non_live_promotion_excluded_by_jsonb_liveness_predicate(
    pg_session,
    shadow_coupon_enabled: None,
    liveness_case: str,
    promo_kwargs: dict,
) -> None:
    conversation, _customer, live_promotion = _seed_policy_ready_chain(
        pg_session,
        countable_orders=2,
        min_orders_threshold=2,
    )
    _seed_conditional_promotion(
        pg_session,
        tenant_id=TEST_TENANT_A,
        min_orders=1,
        **promo_kwargs,
    )
    promo_before = _promotion_count(pg_session)

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    assert _promotion_count(pg_session) == promo_before
    record = facts[0].value
    assert record["identity_status"] == "resolved"
    assert record["completed_orders_count"] == 2
    assert record["min_orders_for_eligibility"] == 2
    assert record["min_orders_condition_state"] == MIN_ORDERS_STATE_SATISFIED
    assert record["allow_min_orders_condition_claim"] is True
    assert obs["usage_evidence_query_count"] == 1
    assert obs["order_count_query_count"] == 1
    assert obs["conditional_target_count"] == 1, liveness_case
    assert live_promotion.status == "active"


def test_pg_only_non_live_promotions_yield_no_target_and_no_count(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A, name="متجر تجريبي عام")
    customer = seed_customer(pg_session, tenant_id=TEST_TENANT_A, name="أحمد سالم")
    conversation = Conversation(
        tenant_id=TEST_TENANT_A,
        status="open",
        customer_id=customer.id,
    )
    pg_session.add(conversation)
    pg_session.flush()

    _seed_countable_internal_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        customer_id=customer.id,
        external_id=f"orphan-order-{conversation.id}",
    )
    seed_capability_state(
        pg_session,
        state=CAPABILITY_STATE_VALIDATED,
        validation_revision="0088",
    )
    reconcile_internal_customer_coverage(
        pg_session,
        tenant_id=TEST_TENANT_A,
        customer_id=customer.id,
    )
    _seed_active_binding(
        pg_session,
        tenant_id=TEST_TENANT_A,
        conversation_id=conversation.id,
        customer_id=customer.id,
    )
    now = datetime.now(timezone.utc)
    for status in ("paused", "draft"):
        _seed_conditional_promotion(
            pg_session,
            tenant_id=TEST_TENANT_A,
            min_orders=2,
            status=status,
        )
    _seed_conditional_promotion(
        pg_session,
        tenant_id=TEST_TENANT_A,
        min_orders=2,
        ends_at=now - timedelta(days=1),
    )
    _seed_conditional_promotion(
        pg_session,
        tenant_id=TEST_TENANT_A,
        min_orders=2,
        usage_count=10,
        usage_limit=10,
    )
    promo_before = _promotion_count(pg_session)

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    assert _promotion_count(pg_session) == promo_before
    record = facts[0].value
    assert record["identity_status"] == "resolved"
    assert record["closed_reason_code"] == REASON_NO_CONDITIONAL_TARGET
    assert record["allow_min_orders_condition_claim"] is False
    assert record["completed_orders_count"] is None
    assert obs["usage_evidence_query_count"] == 1
    assert obs["order_count_query_count"] == 0
    assert obs["conditional_target_count"] == 0


def test_pg_capability_expand_fails_closed_before_target_scan(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    conversation, _, _ = _seed_policy_ready_chain(
        pg_session,
        capability_state=CAPABILITY_STATE_EXPAND,
    )

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    record = facts[0].value
    assert record["identity_status"] == "unresolved"
    assert record["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED
    assert record["allow_min_orders_condition_claim"] is False
    assert record["conditional_coupon_evaluation_state"] == EVALUATION_REQUIRES_CONTEXT
    assert obs["order_count_query_count"] == 0
    assert obs["usage_evidence_query_count"] == 0


def test_pg_stale_proof_without_reconcile_fails_closed_before_target_scan(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    conversation, _, _ = _seed_policy_ready_chain(pg_session, reconcile=False)

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    record = facts[0].value
    assert record["identity_status"] == "unresolved"
    assert record["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED
    assert obs["order_count_query_count"] == 0
    assert obs["usage_evidence_query_count"] == 0


def test_pg_incomplete_coverage_fails_closed_before_target_scan(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A, name="متجر تجريبي عام")
    customer = seed_customer(pg_session, tenant_id=TEST_TENANT_A, name="نورة عبدالله")
    conversation = Conversation(
        tenant_id=TEST_TENANT_A,
        status="open",
        customer_id=customer.id,
    )
    pg_session.add(conversation)
    pg_session.flush()

    _seed_countable_internal_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        customer_id=customer.id,
        external_id=f"linked-order-{conversation.id}",
    )
    seed_internal_order(
        pg_session,
        tenant_id=TEST_TENANT_A,
        external_id=f"unmapped-order-{conversation.id}",
        customer_id=customer.id,
    )
    unmapped = (
        pg_session.query(Order)
        .filter_by(external_id=f"unmapped-order-{conversation.id}")
        .one()
    )
    unmapped.customer_link_state = LINK_STATE_UNLINKED
    unmapped.customer_link_evidence_class = None

    seed_capability_state(
        pg_session,
        state=CAPABILITY_STATE_VALIDATED,
        validation_revision="0088",
    )
    reconcile_internal_customer_coverage(
        pg_session,
        tenant_id=TEST_TENANT_A,
        customer_id=customer.id,
    )
    _seed_active_binding(
        pg_session,
        tenant_id=TEST_TENANT_A,
        conversation_id=conversation.id,
        customer_id=customer.id,
    )
    _seed_active_conditional_promotion(
        pg_session,
        tenant_id=TEST_TENANT_A,
        min_orders=2,
    )

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    record = facts[0].value
    assert record["identity_status"] == "unresolved"
    assert record["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED
    assert obs["order_count_query_count"] == 0
    assert obs["usage_evidence_query_count"] == 0


def test_pg_missing_binding_fails_closed_before_target_scan(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    conversation, _, _ = _seed_policy_ready_chain(pg_session)
    pg_session.query(ConversationA1SubjectBinding).filter_by(
        tenant_id=TEST_TENANT_A,
        conversation_id=conversation.id,
    ).delete()
    pg_session.flush()

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    record = facts[0].value
    assert record["identity_status"] == "unresolved"
    assert record["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED
    assert obs["order_count_query_count"] == 0
    assert obs["usage_evidence_query_count"] == 0


def test_pg_revoked_binding_fails_closed_before_target_scan(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    conversation, customer, _ = _seed_policy_ready_chain(pg_session)
    binding = (
        pg_session.query(ConversationA1SubjectBinding)
        .filter_by(tenant_id=TEST_TENANT_A, conversation_id=conversation.id)
        .one()
    )
    binding.binding_state = BINDING_STATE_REVOKED
    binding.revoked_at = datetime.now(timezone.utc)
    pg_session.flush()

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    record = facts[0].value
    assert record["identity_status"] == "unresolved"
    assert record["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED
    assert obs["order_count_query_count"] == 0
    assert obs["usage_evidence_query_count"] == 0
    assert customer.tenant_id == TEST_TENANT_A


def test_pg_cross_tenant_binding_corruption_blocked_by_composite_fk(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    """
    PostgreSQL rejects cross-tenant ``internal_customer_id`` at flush.

    Runtime fail-closed loader behavior for a corrupted readable binding is
    covered in mocked consumer tests; this PG slice proves the invalid state
    cannot persist under ``fk_casb_tenant_internal_customer``.
    """
    conversation, customer, _ = _seed_policy_ready_chain(pg_session)
    seed_tenant(pg_session, tenant_id=TEST_TENANT_B, name="متجر تجريبي آخر")
    other_customer = seed_customer(
        pg_session,
        tenant_id=TEST_TENANT_B,
        name="عميل آخر",
    )
    binding = (
        pg_session.query(ConversationA1SubjectBinding)
        .filter_by(tenant_id=TEST_TENANT_A, conversation_id=conversation.id)
        .one()
    )
    with pytest.raises(IntegrityError, match="fk_casb_tenant_internal_customer"):
        with pg_session.begin_nested():
            binding.internal_customer_id = other_customer.id
            pg_session.flush()

    pg_session.expire(binding)
    assert binding.internal_customer_id == customer.id

    with _no_runtime_side_effects():
        facts, obs = _load_facts(pg_session, conversation)

    record = facts[0].value
    assert record["identity_status"] == "resolved"
    assert record["customer_scope"] == "nahla_internal_customer"
    assert record["allow_min_orders_condition_claim"] is True
    assert obs["order_count_query_count"] == 1
    assert obs["usage_evidence_query_count"] == 1
