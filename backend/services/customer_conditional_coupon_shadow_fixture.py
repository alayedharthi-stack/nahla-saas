"""
Staging-only conditional-coupon shadow observation fixture harness.

Seeds the minimal valid tuple (authoritative A1 orders, active conditional
promotion, conversation + binding via platform binding service) so operators can
later collect schema-valid Layer 0 shadow observations. Does not enable shadow
flags, compose, canary, or customer messaging.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from services.conversation_a1_subject_binding_service import (
    write_authoritative_internal_binding_from_verified_order,
)
from services.conversation_a1_subject_read_contract import READ_STATUS_RESOLVED
from services.customer_conditional_coupon_shadow_fixture_contract import (
    CAPABILITY_STATE_VALIDATED,
    FIXTURE_CONVERSATION_SUFFIX,
    FIXTURE_COUNTABLE_ORDER_SUFFIX,
    FIXTURE_EXTERNAL_ID_PREFIX,
    FIXTURE_MARKER_FIELD,
    FIXTURE_NAMESPACE,
    FIXTURE_SCHEMA_VERSION,
    FIXTURE_SLOT_CONDITIONAL_PROMOTION,
    FIXTURE_SLOT_CONVERSATION,
    FIXTURE_SLOT_COUNTABLE_ORDER,
    FIXTURE_SLOT_FIELD,
    FIXTURE_SLOT_INTERNAL_CUSTOMER,
    GENERIC_CITY,
    GENERIC_INTERNAL_CUSTOMER_NAME,
    GENERIC_PRODUCT_SHOES,
    GENERIC_SHORT_CODE,
    MAX_CONDITIONAL_PROMOTIONS,
    MAX_CONVERSATIONS,
    MAX_COUNTABLE_ORDERS,
    MAX_INTERNAL_CUSTOMERS,
    MIN_COUNTABLE_ORDERS,
    MIN_ORDERS_THRESHOLD,
    REQUIRED_ALEMBIC_REVISIONS,
    VALIDATION_REVISION_0088,
)
from services.order_customer_identity_logging import log_coupon_shadow_fixture_failure
from services.order_customer_identity_reconciliation_write import (
    read_alembic_revisions,
    read_capability_detail,
)
from services.order_customer_identity_service import (
    apply_nahla_internal_order_identity,
    reconcile_internal_customer_coverage,
)


@dataclass(frozen=True)
class FixtureGateFailure:
    error_class: str
    stage: str


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fixture_external_id(tenant_id: int, suffix: str) -> str:
    return f"{FIXTURE_EXTERNAL_ID_PREFIX}-{int(tenant_id)}-{suffix}"


def _fixture_marker(*, slot: str) -> Dict[str, str]:
    return {
        FIXTURE_MARKER_FIELD: FIXTURE_NAMESPACE,
        FIXTURE_SLOT_FIELD: slot,
    }


def _merge_fixture_metadata(row: Any, *, slot: str) -> None:
    marker = _fixture_marker(slot=slot)
    extra = getattr(row, "extra_metadata", None)
    if extra is not None or hasattr(row, "extra_metadata"):
        meta = dict(extra or {})
        meta.update(marker)
        row.extra_metadata = meta


def _row_has_fixture_namespace(row: Any) -> bool:
    meta = getattr(row, "extra_metadata", None) or {}
    return str(meta.get(FIXTURE_MARKER_FIELD) or "").strip() == FIXTURE_NAMESPACE


def _generic_internal_line_items() -> List[Dict[str, Any]]:
    return [
        {"name": GENERIC_PRODUCT_SHOES, "quantity": 1, "unit_price": "199.00"},
    ]


def validate_fixture_input(*, tenant_id: int) -> FixtureGateFailure | None:
    if int(tenant_id) <= 0:
        return FixtureGateFailure("input_rejected", "invalid_tenant_scope")
    return None


def validate_shadow_fixture_capability_and_revision_gates(
    db: Session,
) -> FixtureGateFailure | None:
    revisions = read_alembic_revisions(db)
    if not revisions:
        return FixtureGateFailure("revision_rejected", "alembic_version_missing")
    if revisions == frozenset({"0088"}):
        return FixtureGateFailure("revision_rejected", "revision_0089_missing")
    if revisions == frozenset({"0089"}):
        return FixtureGateFailure("revision_rejected", "revision_0088_missing")
    if revisions == frozenset({"0087"}):
        return FixtureGateFailure("revision_rejected", "revision_is_0087_not_dual_head")
    if revisions != REQUIRED_ALEMBIC_REVISIONS:
        return FixtureGateFailure("revision_rejected", "revision_not_exactly_0088_and_0089")

    state, validation_revision = read_capability_detail(db)
    if state is None:
        return FixtureGateFailure("capability_rejected", "capability_state_missing")
    if state != CAPABILITY_STATE_VALIDATED:
        return FixtureGateFailure("capability_rejected", "capability_state_not_validated")
    if validation_revision is None:
        return FixtureGateFailure("capability_rejected", "capability_validation_revision_missing")
    if validation_revision != VALIDATION_REVISION_0088:
        return FixtureGateFailure("capability_rejected", "capability_validation_revision_mismatch")
    return None


def _fixture_customers_for_tenant(db: Session, *, tenant_id: int) -> List[Any]:
    from models import Customer  # noqa: PLC0415

    rows = db.query(Customer).filter(Customer.tenant_id == int(tenant_id)).all()
    return [row for row in rows if _row_has_fixture_namespace(row)]


def _fixture_conversations_for_tenant(db: Session, *, tenant_id: int) -> List[Any]:
    from models import Conversation  # noqa: PLC0415

    rows = (
        db.query(Conversation)
        .filter(Conversation.tenant_id == int(tenant_id))
        .all()
    )
    return [row for row in rows if _row_has_fixture_namespace(row)]


def _fixture_orders_for_tenant(db: Session, *, tenant_id: int) -> List[Any]:
    from models import Order  # noqa: PLC0415

    prefix = f"{FIXTURE_EXTERNAL_ID_PREFIX}-{int(tenant_id)}-"
    rows = (
        db.query(Order)
        .filter(Order.tenant_id == int(tenant_id))
        .filter(Order.external_id.like(f"{prefix}%"))
        .all()
    )
    return [row for row in rows if _row_has_fixture_namespace(row)]


def _fixture_promotions_for_tenant(db: Session, *, tenant_id: int) -> List[Any]:
    from models import Promotion  # noqa: PLC0415

    rows = (
        db.query(Promotion)
        .filter(Promotion.tenant_id == int(tenant_id))
        .all()
    )
    return [row for row in rows if _row_has_fixture_namespace(row)]


def _fixture_bindings_for_tenant(db: Session, *, tenant_id: int) -> List[Any]:
    from models import ConversationA1SubjectBinding  # noqa: PLC0415

    conversation_ids = {int(row.id) for row in _fixture_conversations_for_tenant(db, tenant_id=tenant_id)}
    if not conversation_ids:
        return []
    rows = (
        db.query(ConversationA1SubjectBinding)
        .filter(
            ConversationA1SubjectBinding.tenant_id == int(tenant_id),
            ConversationA1SubjectBinding.conversation_id.in_(list(conversation_ids)),
        )
        .all()
    )
    return list(rows)


def _count_fixture_shape(db: Session, *, tenant_id: int) -> Dict[str, int]:
    orders = _fixture_orders_for_tenant(db, tenant_id=tenant_id)
    countable = sum(
        1
        for order in orders
        if str((order.extra_metadata or {}).get(FIXTURE_SLOT_FIELD, "")).startswith(
            FIXTURE_SLOT_COUNTABLE_ORDER
        )
    )
    return {
        "internal_customers": len(_fixture_customers_for_tenant(db, tenant_id=tenant_id)),
        "conversations": len(_fixture_conversations_for_tenant(db, tenant_id=tenant_id)),
        "countable_orders": countable,
        "conditional_promotions": len(_fixture_promotions_for_tenant(db, tenant_id=tenant_id)),
        "subject_bindings": len(_fixture_bindings_for_tenant(db, tenant_id=tenant_id)),
        "total_orders": len(orders),
    }


def _count_authoritative_internal_orders(db: Session, *, tenant_id: int) -> int:
    from services.order_customer_identity_contract import (  # noqa: PLC0415
        EVIDENCE_AUTHORITATIVE,
        ORDER_SOURCE_NAHL_INTERNAL,
    )

    fixture_orders = _fixture_orders_for_tenant(db, tenant_id=tenant_id)
    return sum(
        1
        for order in fixture_orders
        if order.order_source_kind == ORDER_SOURCE_NAHL_INTERNAL
        and order.customer_link_evidence_class == EVIDENCE_AUTHORITATIVE
        and str(order.status or "").lower() == "completed"
    )


def _probe_bridge_resolved(db: Session, *, tenant_id: int, conversation: Any) -> bool:
    from services.conversation_a1_subject_read_contract import (  # noqa: PLC0415
        TrustedConversationA1SubjectReadRequest,
    )
    from services.conversation_a1_subject_read_service import (  # noqa: PLC0415
        resolve_authoritative_a1_subject_for_conversation,
    )

    request = TrustedConversationA1SubjectReadRequest(
        tenant_id=int(tenant_id),
        conversation_id=int(conversation.id),
    )
    result = resolve_authoritative_a1_subject_for_conversation(db, request=request)
    return result.status == READ_STATUS_RESOLVED


@dataclass
class CustomerConditionalCouponShadowFixtureResult:
    fixture_schema_version: str = FIXTURE_SCHEMA_VERSION
    tenant_id: int = 0
    mode: str = "seed"
    dry_run: bool = True
    read_only: bool = True
    outcome: str = "aborted"
    access_status: str = "ok"
    gate_stage: Optional[str] = None
    gate_error_class: Optional[str] = None
    tenant_present: bool = False
    capability_state: Optional[str] = None
    capability_state_readable: bool = False
    capability_validation_revision: Optional[str] = None
    alembic_revisions: List[str] = field(default_factory=list)
    alembic_revision_is_dual_0088_0089: bool = False
    fixture_namespace: str = FIXTURE_NAMESPACE
    existing_shape: Dict[str, int] = field(default_factory=dict)
    would_create: Dict[str, int] = field(default_factory=dict)
    created: Dict[str, int] = field(default_factory=dict)
    skipped_existing: Dict[str, int] = field(default_factory=dict)
    authoritative_internal_orders: int = 0
    bridge_resolved: bool = False
    active_conditional_targets: int = 0
    cleanup_selected: Dict[str, int] = field(default_factory=dict)
    cleanup_deleted: Dict[str, int] = field(default_factory=dict)
    committed: bool = False
    fixture_generated_at_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fixture_schema_version": self.fixture_schema_version,
            "tenant_id": int(self.tenant_id),
            "mode": self.mode,
            "dry_run": bool(self.dry_run),
            "read_only": bool(self.read_only),
            "outcome": self.outcome,
            "access_status": self.access_status,
            "gate_stage": self.gate_stage,
            "gate_error_class": self.gate_error_class,
            "tenant_present": bool(self.tenant_present),
            "capability": {
                "state": self.capability_state,
                "state_readable": bool(self.capability_state_readable),
                "validation_revision": self.capability_validation_revision,
                "alembic_revisions": list(self.alembic_revisions),
                "alembic_revision_is_dual_0088_0089": bool(
                    self.alembic_revision_is_dual_0088_0089
                ),
            },
            "fixture_namespace": self.fixture_namespace,
            "shape": {
                "existing": dict(self.existing_shape),
                "would_create": dict(self.would_create),
                "created": dict(self.created),
                "skipped_existing": dict(self.skipped_existing),
            },
            "observation_readiness": {
                "authoritative_internal_orders": int(self.authoritative_internal_orders),
                "bridge_resolved": bool(self.bridge_resolved),
                "active_conditional_targets": int(self.active_conditional_targets),
            },
            "cleanup": {
                "selected": dict(self.cleanup_selected),
                "deleted": dict(self.cleanup_deleted),
            },
            "committed": bool(self.committed),
            "fixture_generated_at_utc": self.fixture_generated_at_utc,
        }

    def summary_line(self) -> str:
        return (
            f"coupon_shadow_fixture tenant={self.tenant_id} mode={self.mode} "
            f"dry_run={int(self.dry_run)} outcome={self.outcome} "
            f"committed={int(self.committed)}"
        )


def _apply_common_gates(
    db: Session,
    result: CustomerConditionalCouponShadowFixtureResult,
) -> Optional[FixtureGateFailure]:
    input_failure = validate_fixture_input(tenant_id=result.tenant_id)
    if input_failure:
        result.outcome = "failed"
        result.access_status = "gate_rejected"
        result.gate_stage = input_failure.stage
        result.gate_error_class = input_failure.error_class
        return input_failure

    from models import Tenant  # noqa: PLC0415

    tenant = db.query(Tenant.id).filter(Tenant.id == int(result.tenant_id)).first()
    result.tenant_present = tenant is not None
    if not result.tenant_present:
        result.outcome = "failed"
        result.access_status = "tenant_missing"
        return FixtureGateFailure("tenant_rejected", "tenant_missing")

    revisions = read_alembic_revisions(db)
    result.alembic_revisions = sorted(revisions)
    result.alembic_revision_is_dual_0088_0089 = revisions == REQUIRED_ALEMBIC_REVISIONS
    state, validation_revision = read_capability_detail(db)
    result.capability_state = state
    result.capability_state_readable = state is not None
    result.capability_validation_revision = validation_revision

    capability_failure = validate_shadow_fixture_capability_and_revision_gates(db)
    if capability_failure:
        result.outcome = "failed"
        result.access_status = "gate_rejected"
        result.gate_stage = capability_failure.stage
        result.gate_error_class = capability_failure.error_class
        return capability_failure
    return None


def _existing_fixture_order(db: Session, *, tenant_id: int, slot: str) -> Any | None:
    for order in _fixture_orders_for_tenant(db, tenant_id=tenant_id):
        if str((order.extra_metadata or {}).get(FIXTURE_SLOT_FIELD)) == slot:
            return order
    return None


def _ensure_internal_customer(db: Session, *, tenant_id: int, dry_run: bool) -> tuple[Any | None, bool]:
    existing = _fixture_customers_for_tenant(db, tenant_id=tenant_id)
    if existing:
        return existing[0], False
    if dry_run:
        return None, True
    from models import Customer  # noqa: PLC0415

    row = Customer(
        tenant_id=int(tenant_id),
        name=GENERIC_INTERNAL_CUSTOMER_NAME,
    )
    _merge_fixture_metadata(row, slot=FIXTURE_SLOT_INTERNAL_CUSTOMER)
    db.add(row)
    db.flush()
    return row, True


def _ensure_conversation(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    dry_run: bool,
) -> tuple[Any | None, bool]:
    existing = _fixture_conversations_for_tenant(db, tenant_id=tenant_id)
    if existing:
        return existing[0], False
    if dry_run:
        return None, True
    from models import Conversation  # noqa: PLC0415

    row = Conversation(
        tenant_id=int(tenant_id),
        status="open",
        customer_id=int(customer_id),
        external_id=_fixture_external_id(tenant_id, FIXTURE_CONVERSATION_SUFFIX),
    )
    _merge_fixture_metadata(row, slot=FIXTURE_SLOT_CONVERSATION)
    db.add(row)
    db.flush()
    return row, True


def _ensure_internal_order(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    slot: str,
    suffix: str,
    dry_run: bool,
) -> tuple[Any | None, bool]:
    existing = _existing_fixture_order(db, tenant_id=tenant_id, slot=slot)
    if existing:
        return existing, False
    if dry_run:
        return None, True
    from models import Order  # noqa: PLC0415

    row = Order(
        tenant_id=int(tenant_id),
        external_id=_fixture_external_id(tenant_id, suffix),
        status="completed",
        total="199.00",
        source="whatsapp",
        customer_name=GENERIC_INTERNAL_CUSTOMER_NAME,
        line_items=_generic_internal_line_items(),
        customer_info={"city": GENERIC_CITY, "short_address_code": GENERIC_SHORT_CODE},
        is_abandoned=False,
    )
    _merge_fixture_metadata(row, slot=slot)
    db.add(row)
    db.flush()
    apply_nahla_internal_order_identity(
        row,
        db=db,
        tenant_id=int(tenant_id),
        customer_id=int(customer_id),
    )
    _merge_fixture_metadata(row, slot=slot)
    db.flush()
    return row, True


def _ensure_conditional_promotion(
    db: Session,
    *,
    tenant_id: int,
    dry_run: bool,
) -> tuple[Any | None, bool]:
    existing = _fixture_promotions_for_tenant(db, tenant_id=tenant_id)
    if existing:
        return existing[0], False
    if dry_run:
        return None, True
    from models import Promotion  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    row = Promotion(
        tenant_id=int(tenant_id),
        name="generic-loyalty-threshold-fixture",
        promotion_type="percentage",
        discount_value=10,
        conditions={"min_orders_for_eligibility": int(MIN_ORDERS_THRESHOLD)},
        status="active",
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(days=30),
        usage_count=0,
        usage_limit=100,
    )
    _merge_fixture_metadata(row, slot=FIXTURE_SLOT_CONDITIONAL_PROMOTION)
    db.add(row)
    db.flush()
    return row, True


def _populate_observation_readiness(
    db: Session,
    result: CustomerConditionalCouponShadowFixtureResult,
) -> None:
    from modules.ai.brain.truth_surface.customer_conditional_coupon_repository import (  # noqa: PLC0415
        scan_conditional_targets,
    )

    result.authoritative_internal_orders = _count_authoritative_internal_orders(
        db,
        tenant_id=int(result.tenant_id),
    )
    conversations = _fixture_conversations_for_tenant(db, tenant_id=int(result.tenant_id))
    if conversations:
        result.bridge_resolved = _probe_bridge_resolved(
            db,
            tenant_id=int(result.tenant_id),
            conversation=conversations[0],
        )
    try:
        targets, _overflow = scan_conditional_targets(db, tenant_id=int(result.tenant_id))
        result.active_conditional_targets = len(targets)
    except Exception:  # noqa: BLE001
        result.active_conditional_targets = 0


def execute_customer_conditional_coupon_shadow_fixture_seed(
    db: Session,
    tenant_id: int,
    *,
    dry_run: bool = True,
) -> CustomerConditionalCouponShadowFixtureResult:
    result = CustomerConditionalCouponShadowFixtureResult(
        tenant_id=int(tenant_id),
        mode="seed",
        dry_run=bool(dry_run),
        read_only=bool(dry_run),
        fixture_generated_at_utc=_utcnow_iso(),
    )
    try:
        gate = _apply_common_gates(db, result)
        if gate:
            return result

        db.expire_all()
        existing = _count_fixture_shape(db, tenant_id=int(tenant_id))
        result.existing_shape = existing

        would_create = {
            "internal_customers": 0
            if existing["internal_customers"] >= MAX_INTERNAL_CUSTOMERS
            else 1,
            "conversations": 0 if existing["conversations"] >= MAX_CONVERSATIONS else 1,
            "countable_orders": max(
                0,
                min(
                    MAX_COUNTABLE_ORDERS - existing["countable_orders"],
                    MIN_COUNTABLE_ORDERS - existing["countable_orders"],
                ),
            ),
            "conditional_promotions": 0
            if existing["conditional_promotions"] >= MAX_CONDITIONAL_PROMOTIONS
            else 1,
            "subject_bindings": 0 if existing["subject_bindings"] >= 1 else 1,
        }
        result.would_create = would_create

        if sum(would_create.values()) == 0:
            result.outcome = "success"
            result.skipped_existing = dict(existing)
            _populate_observation_readiness(db, result)
            return result

        if dry_run:
            result.outcome = "success"
            _populate_observation_readiness(db, result)
            return result

        created = {
            "internal_customers": 0,
            "conversations": 0,
            "countable_orders": 0,
            "conditional_promotions": 0,
            "subject_bindings": 0,
        }

        customer, created_customer = _ensure_internal_customer(
            db, tenant_id=int(tenant_id), dry_run=False,
        )
        if created_customer:
            created["internal_customers"] = 1
        if customer is None:
            raise ValueError("customer_missing_after_ensure")

        bridge_order = None
        for index in range(MIN_COUNTABLE_ORDERS):
            slot_name = (
                FIXTURE_SLOT_COUNTABLE_ORDER
                if index == 0
                else f"{FIXTURE_SLOT_COUNTABLE_ORDER}_{index}"
            )
            suffix = f"{FIXTURE_COUNTABLE_ORDER_SUFFIX}-{index + 1:02d}"
            order_row, created_order = _ensure_internal_order(
                db,
                tenant_id=int(tenant_id),
                customer_id=int(customer.id),
                slot=slot_name,
                suffix=suffix,
                dry_run=False,
            )
            if created_order:
                created["countable_orders"] += 1
            if index == 0 and order_row is not None:
                bridge_order = order_row

        reconcile_internal_customer_coverage(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(customer.id),
        )

        conversation, created_conversation = _ensure_conversation(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(customer.id),
            dry_run=False,
        )
        if created_conversation:
            created["conversations"] = 1
        if conversation is None:
            raise ValueError("conversation_missing_after_ensure")

        if bridge_order is None:
            bridge_order = _existing_fixture_order(
                db,
                tenant_id=int(tenant_id),
                slot=FIXTURE_SLOT_COUNTABLE_ORDER,
            )
        if bridge_order is not None and existing["subject_bindings"] < 1:
            binding_result = write_authoritative_internal_binding_from_verified_order(
                db,
                tenant_id=int(tenant_id),
                conversation_id=int(conversation.id),
                order=bridge_order,
            )
            if binding_result.outcome in ("created", "superseded"):
                created["subject_bindings"] = 1

        _, created_promo = _ensure_conditional_promotion(
            db, tenant_id=int(tenant_id), dry_run=False,
        )
        if created_promo:
            created["conditional_promotions"] = 1

        db.commit()
        result.committed = True
        result.created = created
        result.outcome = "success"
        _populate_observation_readiness(db, result)
        return result
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log_coupon_shadow_fixture_failure(exception_class=type(exc).__name__)
        result.outcome = "failed"
        result.access_status = "execution_failed"
        result.gate_stage = "execution_exception"
        result.gate_error_class = type(exc).__name__
        return result


def _customer_ids_from_fixture(db: Session, *, tenant_id: int) -> Set[int]:
    orders = _fixture_orders_for_tenant(db, tenant_id=tenant_id)
    ids = {
        int(customer.id) for customer in _fixture_customers_for_tenant(db, tenant_id=tenant_id)
    }
    for order in orders:
        customer_id = getattr(order, "customer_id", None)
        if customer_id is not None:
            ids.add(int(customer_id))
    return ids


def execute_customer_conditional_coupon_shadow_fixture_cleanup(
    db: Session,
    tenant_id: int,
    *,
    dry_run: bool = True,
) -> CustomerConditionalCouponShadowFixtureResult:
    result = CustomerConditionalCouponShadowFixtureResult(
        tenant_id=int(tenant_id),
        mode="cleanup",
        dry_run=bool(dry_run),
        read_only=bool(dry_run),
        fixture_generated_at_utc=_utcnow_iso(),
    )
    try:
        gate = _apply_common_gates(db, result)
        if gate:
            return result

        db.expire_all()
        fixture_bindings = _fixture_bindings_for_tenant(db, tenant_id=int(tenant_id))
        fixture_promotions = _fixture_promotions_for_tenant(db, tenant_id=int(tenant_id))
        fixture_orders = _fixture_orders_for_tenant(db, tenant_id=int(tenant_id))
        fixture_conversations = _fixture_conversations_for_tenant(db, tenant_id=int(tenant_id))
        fixture_customers = _fixture_customers_for_tenant(db, tenant_id=int(tenant_id))
        customer_ids = _customer_ids_from_fixture(db, tenant_id=int(tenant_id))

        from models import NahlaInternalCustomerOrderHistoryCoverage  # noqa: PLC0415

        coverage_count = 0
        if customer_ids:
            coverage_count = (
                db.query(NahlaInternalCustomerOrderHistoryCoverage.id)
                .filter(
                    NahlaInternalCustomerOrderHistoryCoverage.tenant_id == int(tenant_id),
                    NahlaInternalCustomerOrderHistoryCoverage.customer_id.in_(list(customer_ids)),
                )
                .count()
            )

        selected = {
            "subject_bindings": len(fixture_bindings),
            "conditional_promotions": len(fixture_promotions),
            "orders": len(fixture_orders),
            "internal_coverage_rows": int(coverage_count),
            "conversations": len(fixture_conversations),
            "internal_customers": len(fixture_customers),
        }
        result.cleanup_selected = selected
        result.existing_shape = _count_fixture_shape(db, tenant_id=int(tenant_id))

        if dry_run:
            result.outcome = "success"
            return result

        deleted = {
            "subject_bindings": 0,
            "conditional_promotions": 0,
            "orders": 0,
            "internal_coverage_rows": 0,
            "conversations": 0,
            "internal_customers": 0,
        }

        for binding in fixture_bindings:
            db.delete(binding)
            deleted["subject_bindings"] += 1
        db.flush()

        for promotion in fixture_promotions:
            db.delete(promotion)
            deleted["conditional_promotions"] += 1
        db.flush()

        for order in fixture_orders:
            db.delete(order)
            deleted["orders"] += 1
        db.flush()

        if customer_ids:
            deleted["internal_coverage_rows"] = (
                db.query(NahlaInternalCustomerOrderHistoryCoverage)
                .filter(
                    NahlaInternalCustomerOrderHistoryCoverage.tenant_id == int(tenant_id),
                    NahlaInternalCustomerOrderHistoryCoverage.customer_id.in_(list(customer_ids)),
                )
                .delete(synchronize_session=False)
            )

        for conversation in fixture_conversations:
            db.delete(conversation)
            deleted["conversations"] += 1

        for customer in fixture_customers:
            if int(customer.id) in customer_ids:
                db.delete(customer)
                deleted["internal_customers"] += 1

        db.commit()
        result.committed = True
        result.cleanup_deleted = deleted
        result.outcome = "success"
        return result
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log_coupon_shadow_fixture_failure(exception_class=type(exc).__name__)
        result.outcome = "failed"
        result.access_status = "execution_failed"
        result.gate_stage = "execution_exception"
        result.gate_error_class = type(exc).__name__
        return result


__all__ = [
    "CustomerConditionalCouponShadowFixtureResult",
    "FixtureGateFailure",
    "execute_customer_conditional_coupon_shadow_fixture_cleanup",
    "execute_customer_conditional_coupon_shadow_fixture_seed",
    "validate_fixture_input",
    "validate_shadow_fixture_capability_and_revision_gates",
]
