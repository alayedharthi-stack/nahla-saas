"""
Immutable BusinessIntentDefinition records and the initial conservative registry set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, FrozenSet, Mapping, Tuple

from core.commerce_lifecycle.intents import BusinessIntent
from core.commerce_lifecycle.strategies import (
    ClosedWindowStrategy,
    MerchantModeConstraint,
    OpenWindowStrategy,
    RetryPolicy,
)

KNOWN_CAPABILITY_FIELDS: FrozenSet[str] = frozenset({
    "has_external_store",
    "supports_external_checkout",
    "supports_external_coupons",
    "supports_whatsapp_orders",
    "supports_nahla_orders",
    "supports_bank_transfer",
    "supports_cod",
    "has_whatsapp_catalog",
    "has_external_tracking",
    "has_nahla_tracking",
    "has_payment_link",
})

KNOWN_EVIDENCE_FIELDS: FrozenSet[str] = frozenset({
    "order_number",
    "checkout_url",
    "payment_url",
    "tracking_url",
    "tracking_number",
    "carrier",
    "delivered_at",
    "payment_method",
    "review_url",
    "coupon_code",
    "customer_phone",
    "customer_name",
    "status",
    "source_event_id",
    "transition_version",
    "missing_fields",
})

_STANDARD_IDEMPOTENCY_KEY_FIELDS: Tuple[str, ...] = (
    "tenant_id",
    "order_id",
    "intent",
    "source_event_id",
)


@dataclass(frozen=True)
class BusinessIntentDefinition:
    intent: BusinessIntent
    required_evidence: Tuple[str, ...] = ()
    optional_evidence: Tuple[str, ...] = ()
    required_evidence_groups: Tuple[Tuple[str, ...], ...] = ()
    required_template_evidence: Tuple[str, ...] = ()
    required_capabilities: Tuple[str, ...] = ()
    forbidden_capabilities: Tuple[str, ...] = ()
    open_window_strategy: OpenWindowStrategy = OpenWindowStrategy.SESSION_HANDOFF
    closed_window_strategy: ClosedWindowStrategy = ClosedWindowStrategy.BLOCKED
    service_key: str | None = None
    template_variable_map: Mapping[str, str] = field(default_factory=dict)
    retry_policy: RetryPolicy = RetryPolicy.NONE
    idempotency_key_fields: Tuple[str, ...] = _STANDARD_IDEMPOTENCY_KEY_FIELDS
    allow_without_autopilot: bool = False
    merchant_mode_constraints: FrozenSet[MerchantModeConstraint] = frozenset({
        MerchantModeConstraint.ANY,
    })

    def __post_init__(self) -> None:
        self._validate_evidence_fields()
        self._validate_capabilities()
        self._validate_template_policy()
        self._validate_idempotency()
        if self.template_variable_map:
            object.__setattr__(
                self,
                "template_variable_map",
                MappingProxyType(dict(self.template_variable_map)),
            )

    def _validate_evidence_fields(self) -> None:
        seen = set(self.required_evidence)
        if len(seen) != len(self.required_evidence):
            raise ValueError(f"duplicate required_evidence for {self.intent.value}")
        template_seen = set(self.required_template_evidence)
        if len(template_seen) != len(self.required_template_evidence):
            raise ValueError(f"duplicate required_template_evidence for {self.intent.value}")
        overlap = seen & frozenset(self.optional_evidence)
        if overlap:
            raise ValueError(
                f"field appears in both required and optional evidence on {self.intent.value}: "
                f"{sorted(overlap)}"
            )
        for name in seen | template_seen | frozenset(self.optional_evidence):
            if name not in KNOWN_EVIDENCE_FIELDS:
                raise ValueError(f"unknown evidence field {name!r} on {self.intent.value}")
        for group in self.required_evidence_groups:
            if not group:
                raise ValueError(f"empty required_evidence_groups entry for {self.intent.value}")
            for name in group:
                if name not in KNOWN_EVIDENCE_FIELDS:
                    raise ValueError(
                        f"unknown evidence field {name!r} in group for {self.intent.value}"
                    )

    def _validate_capabilities(self) -> None:
        for name in self.required_capabilities + self.forbidden_capabilities:
            if name not in KNOWN_CAPABILITY_FIELDS:
                raise ValueError(f"unknown capability field {name!r} on {self.intent.value}")

    def _validate_template_policy(self) -> None:
        if self.closed_window_strategy == ClosedWindowStrategy.APPROVED_TEMPLATE:
            if not self.service_key:
                raise ValueError(
                    f"approved_template requires service_key for {self.intent.value}"
                )
        elif self.service_key:
            raise ValueError(
                f"service_key set without approved_template strategy for {self.intent.value}"
            )
        if self.template_variable_map and self.closed_window_strategy != ClosedWindowStrategy.APPROVED_TEMPLATE:
            raise ValueError(
                f"template_variable_map requires approved_template for {self.intent.value}"
            )
        for slot, evidence_field in self.template_variable_map.items():
            if evidence_field not in KNOWN_EVIDENCE_FIELDS:
                raise ValueError(
                    f"unknown template_variable_map evidence {evidence_field!r} "
                    f"on {self.intent.value}"
                )
            if not str(slot).strip():
                raise ValueError(f"empty template slot on {self.intent.value}")
        if self.closed_window_strategy == ClosedWindowStrategy.APPROVED_TEMPLATE:
            declared = (
                frozenset(self.required_evidence)
                | frozenset(self.required_template_evidence)
                | frozenset(self.optional_evidence)
            )
            for evidence_field in self.template_variable_map.values():
                if evidence_field not in declared:
                    raise ValueError(
                        f"template_variable_map field {evidence_field!r} must be declared in "
                        f"required_evidence or required_template_evidence for {self.intent.value}"
                    )
        if self.required_template_evidence and self.closed_window_strategy != ClosedWindowStrategy.APPROVED_TEMPLATE:
            raise ValueError(
                f"required_template_evidence requires approved_template for {self.intent.value}"
            )

    def _validate_idempotency(self) -> None:
        if not self.idempotency_key_fields:
            raise ValueError(f"idempotency_key_fields must not be empty for {self.intent.value}")
        for key in self.idempotency_key_fields:
            if not str(key).strip():
                raise ValueError(f"empty idempotency key field on {self.intent.value}")


def build_initial_definitions() -> Tuple[BusinessIntentDefinition, ...]:
    """Conservative PR 2A seed definitions — policy metadata only."""
    return (
        BusinessIntentDefinition(
            intent=BusinessIntent.ORDER_CONFIRMED,
            required_evidence=("order_number",),
            optional_evidence=("customer_name", "customer_phone"),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="order_confirmation",
            template_variable_map={
                "customer_name": "customer_name",
                "order_number": "order_number",
            },
            retry_policy=RetryPolicy.ONCE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.PAYMENT_NEEDED,
            required_evidence=(),
            optional_evidence=("payment_url", "payment_method", "customer_name", "order_number"),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="payment_reminder",
            template_variable_map={
                "customer_name": "customer_name",
                "order_number": "order_number",
                "payment_url": "payment_url",
            },
            retry_policy=RetryPolicy.ONCE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.SHIPMENT_AVAILABLE,
            required_evidence=(),
            required_evidence_groups=(
                ("tracking_url",),
                ("tracking_number",),
            ),
            optional_evidence=(
                "carrier",
                "tracking_number",
                "customer_name",
                "order_number",
            ),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="shipping_tracking",
            template_variable_map={
                "customer_name": "customer_name",
                "order_number": "order_number",
                "carrier": "carrier",
                "tracking_number": "tracking_number",
                "tracking_url": "tracking_url",
            },
            required_template_evidence=("tracking_url",),
            retry_policy=RetryPolicy.ONCE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.OUT_FOR_DELIVERY,
            required_evidence=(),
            required_evidence_groups=(
                ("tracking_url",),
                ("tracking_number",),
            ),
            optional_evidence=(
                "carrier",
                "tracking_number",
                "customer_name",
                "order_number",
            ),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="shipping_tracking",
            template_variable_map={
                "customer_name": "customer_name",
                "order_number": "order_number",
                "carrier": "carrier",
                "tracking_number": "tracking_number",
                "tracking_url": "tracking_url",
            },
            required_template_evidence=("tracking_url",),
            retry_policy=RetryPolicy.ONCE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.ORDER_DELIVERED,
            required_evidence=("delivered_at",),
            optional_evidence=("order_number", "customer_name"),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="order_delivered",
            template_variable_map={
                "customer_name": "customer_name",
                "order_number": "order_number",
            },
            retry_policy=RetryPolicy.ONCE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.PAYMENT_CONFIRMED,
            required_evidence=("order_number",),
            optional_evidence=("customer_name", "payment_method"),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="payment_confirmation",
            template_variable_map={
                "customer_name": "customer_name",
                "order_number": "order_number",
            },
            retry_policy=RetryPolicy.ONCE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.ORDER_CANCELLED,
            required_evidence=("order_number",),
            optional_evidence=("customer_name",),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="order_cancelled",
            template_variable_map={
                "customer_name": "customer_name",
                "order_number": "order_number",
            },
            retry_policy=RetryPolicy.ONCE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.ORDER_REFUNDED,
            required_evidence=("order_number",),
            optional_evidence=("customer_name",),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="order_refunded",
            template_variable_map={
                "customer_name": "customer_name",
                "order_number": "order_number",
            },
            retry_policy=RetryPolicy.ONCE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.INCOMPLETE_ORDER,
            required_evidence=(),
            optional_evidence=("checkout_url", "customer_name", "order_number"),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="cart_recovery",
            template_variable_map={
                "customer_name": "customer_name",
                "checkout_url": "checkout_url",
            },
            retry_policy=RetryPolicy.ONCE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.CUSTOMER_ACTION_REQUIRED,
            required_evidence=("missing_fields",),
            optional_evidence=("customer_name", "customer_phone", "order_number"),
            open_window_strategy=OpenWindowStrategy.SESSION_HANDOFF,
            closed_window_strategy=ClosedWindowStrategy.BLOCKED,
            retry_policy=RetryPolicy.NONE,
        ),
        BusinessIntentDefinition(
            intent=BusinessIntent.REVIEW_REQUEST,
            required_evidence=("delivered_at",),
            optional_evidence=("review_url", "order_number", "customer_name"),
            open_window_strategy=OpenWindowStrategy.NO_MESSAGE,
            closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            service_key="post_delivery",
            template_variable_map={
                "customer_name": "customer_name",
                "order_number": "order_number",
            },
            retry_policy=RetryPolicy.ONCE,
        ),
    )


INITIAL_DEFINITIONS: Tuple[BusinessIntentDefinition, ...] = build_initial_definitions()

__all__ = [
    "BusinessIntentDefinition",
    "INITIAL_DEFINITIONS",
    "KNOWN_CAPABILITY_FIELDS",
    "KNOWN_EVIDENCE_FIELDS",
    "build_initial_definitions",
]
