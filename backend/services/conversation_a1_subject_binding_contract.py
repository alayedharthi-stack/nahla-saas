"""
Closed enums for conversation → A1-subject binding (platform write path).

PR1: authoritative internal bindings only. External binding sources are reserved
for future writers; the schema accepts both subject kinds for namespace alignment.
"""
from __future__ import annotations

from services.order_customer_identity_contract import (
    EVIDENCE_AUTHORITATIVE,
    EVIDENCE_CLASSES,
    EXTERNAL_PROVIDER_SALLA_V1,
    LINK_STATE_VERIFIED,
    NAHLA_INTERNAL_ORDER_V1,
    ORDER_SOURCE_NAHL_INTERNAL,
)

# Binding lifecycle
BINDING_STATE_ACTIVE = "active"
BINDING_STATE_REVOKED = "revoked"
BINDING_STATE_SUPERSEDED = "superseded"

BINDING_STATES = frozenset({
    BINDING_STATE_ACTIVE,
    BINDING_STATE_REVOKED,
    BINDING_STATE_SUPERSEDED,
})

# Subject kinds (align with conditional-coupon handle vocabulary)
SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER = "nahla_internal_customer"
SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE = "external_customer_profile"

SUBJECT_KINDS = frozenset({
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
    SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE,
})

# Closed binding sources (writers)
BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL = (
    "wa_order_bridge_authoritative_internal"
)
BINDING_SOURCE_SALLA_ORDER_CONVERSATION_ATTESTATION = (
    "salla_order_conversation_attestation"
)
BINDING_SOURCE_PROVIDER_OAUTH_SESSION = "provider_oauth_session"

BINDING_SOURCES = frozenset({
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    BINDING_SOURCE_SALLA_ORDER_CONVERSATION_ATTESTATION,
    BINDING_SOURCE_PROVIDER_OAUTH_SESSION,
})

# Opaque platform provenance (never exposed to AI facts/telemetry)
PROVENANCE_KIND_ORDER = "order"
PROVENANCE_KIND_WEBHOOK_EVENT = "webhook_event"
PROVENANCE_KIND_OPERATOR = "operator"

PROVENANCE_KINDS = frozenset({
    PROVENANCE_KIND_ORDER,
    PROVENANCE_KIND_WEBHOOK_EVENT,
    PROVENANCE_KIND_OPERATOR,
})

# Write outcomes (logging / tests)
BINDING_WRITE_OUTCOME_CREATED = "created"
BINDING_WRITE_OUTCOME_NO_OP = "no_op"
BINDING_WRITE_OUTCOME_SUPERSEDED = "superseded"
BINDING_WRITE_OUTCOME_SKIPPED = "skipped"

BINDING_WRITE_OUTCOMES = frozenset({
    BINDING_WRITE_OUTCOME_CREATED,
    BINDING_WRITE_OUTCOME_NO_OP,
    BINDING_WRITE_OUTCOME_SUPERSEDED,
    BINDING_WRITE_OUTCOME_SKIPPED,
})

# Closed skip reasons (no PII)
SKIP_REASON_MISSING_TENANT = "missing_tenant"
SKIP_REASON_MISSING_CONVERSATION_ID = "missing_conversation_id"
SKIP_REASON_ORDER_LINK_NOT_VERIFIED = "order_link_not_verified"
SKIP_REASON_CONVERSATION_NOT_FOUND = "conversation_not_found"
SKIP_REASON_CONVERSATION_TENANT_MISMATCH = "conversation_tenant_mismatch"
SKIP_REASON_CUSTOMER_TENANT_MISMATCH = "customer_tenant_mismatch"
SKIP_REASON_SUBJECT_ROW_MISSING = "subject_row_missing"


def order_has_verified_authoritative_internal_link(order: object) -> bool:
    """True when an order row carries a Nahla-internal A1 link ready for binding."""
    if str(getattr(order, "order_source_kind", "") or "") != ORDER_SOURCE_NAHL_INTERNAL:
        return False
    if str(getattr(order, "customer_link_state", "") or "") != LINK_STATE_VERIFIED:
        return False
    if (
        str(getattr(order, "customer_link_evidence_class", "") or "")
        != EVIDENCE_AUTHORITATIVE
    ):
        return False
    if getattr(order, "customer_id", None) is None:
        return False
    return (
        str(getattr(order, "identity_namespace", "") or "")
        == NAHLA_INTERNAL_ORDER_V1
    )


def namespace_for_subject_kind(subject_kind: str) -> str | None:
    if subject_kind == SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER:
        return NAHLA_INTERNAL_ORDER_V1
    if subject_kind == SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE:
        return EXTERNAL_PROVIDER_SALLA_V1
    return None


__all__ = [
    "BINDING_SOURCES",
    "BINDING_SOURCE_PROVIDER_OAUTH_SESSION",
    "BINDING_SOURCE_SALLA_ORDER_CONVERSATION_ATTESTATION",
    "BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL",
    "BINDING_STATE_ACTIVE",
    "BINDING_STATE_REVOKED",
    "BINDING_STATE_SUPERSEDED",
    "BINDING_STATES",
    "BINDING_WRITE_OUTCOME_CREATED",
    "BINDING_WRITE_OUTCOME_NO_OP",
    "BINDING_WRITE_OUTCOME_SKIPPED",
    "BINDING_WRITE_OUTCOME_SUPERSEDED",
    "BINDING_WRITE_OUTCOMES",
    "EVIDENCE_AUTHORITATIVE",
    "EVIDENCE_CLASSES",
    "PROVENANCE_KIND_OPERATOR",
    "PROVENANCE_KIND_ORDER",
    "PROVENANCE_KIND_WEBHOOK_EVENT",
    "PROVENANCE_KINDS",
    "SKIP_REASON_CONVERSATION_NOT_FOUND",
    "SKIP_REASON_CONVERSATION_TENANT_MISMATCH",
    "SKIP_REASON_CUSTOMER_TENANT_MISMATCH",
    "SKIP_REASON_MISSING_CONVERSATION_ID",
    "SKIP_REASON_MISSING_TENANT",
    "SKIP_REASON_ORDER_LINK_NOT_VERIFIED",
    "SKIP_REASON_SUBJECT_ROW_MISSING",
    "SUBJECT_KIND_EXTERNAL_CUSTOMER_PROFILE",
    "SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER",
    "SUBJECT_KINDS",
    "namespace_for_subject_kind",
    "order_has_verified_authoritative_internal_link",
]
