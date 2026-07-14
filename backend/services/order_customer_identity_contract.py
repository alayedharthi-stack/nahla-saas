"""
Closed enums and namespaces for A1-v3.7 order-customer identity.

Dual link semantics:
- customer_link_* — nahla_internal canonical path only
- external_identity_* — external_provider profile path only
"""
from __future__ import annotations

# Identity namespaces
NAHLA_INTERNAL_ORDER_V1 = "nahla_internal_order_v1"
EXTERNAL_PROVIDER_SALLA_V1 = "external_provider_salla_v1"

# order_source_kind
ORDER_SOURCE_EXTERNAL_PROVIDER = "external_provider"
ORDER_SOURCE_NAHL_INTERNAL = "nahla_internal"
ORDER_SOURCE_WHATSAPP = "whatsapp"
ORDER_SOURCE_MANUAL = "manual"
ORDER_SOURCE_OTHER = "other"

ORDER_SOURCE_KINDS = frozenset({
    ORDER_SOURCE_EXTERNAL_PROVIDER,
    ORDER_SOURCE_NAHL_INTERNAL,
    ORDER_SOURCE_WHATSAPP,
    ORDER_SOURCE_MANUAL,
    ORDER_SOURCE_OTHER,
})

UNTRUSTED_ORDER_SOURCE_KINDS = frozenset({
    ORDER_SOURCE_WHATSAPP,
    ORDER_SOURCE_MANUAL,
    ORDER_SOURCE_OTHER,
})

# Link states (shared value set — separate columns on orders)
LINK_STATE_VERIFIED = "verified"
LINK_STATE_UNLINKED = "unlinked"
LINK_STATE_AMBIGUOUS = "ambiguous"
LINK_STATE_REJECTED = "rejected"

LINK_STATES = frozenset({
    LINK_STATE_VERIFIED,
    LINK_STATE_UNLINKED,
    LINK_STATE_AMBIGUOUS,
    LINK_STATE_REJECTED,
})

EVIDENCE_AUTHORITATIVE = "authoritative"
EVIDENCE_INFERRED = "inferred"

EVIDENCE_CLASSES = frozenset({EVIDENCE_AUTHORITATIVE, EVIDENCE_INFERRED})

# Internal canonical link sources
CUSTOMER_LINK_SOURCE_NAHL_BRIDGE = "nahla_order_bridge_conversation_customer"

# External profile sources
PROFILE_SOURCE_SALLA_CUSTOMER_SYNC = "salla_customer_sync"
PROFILE_SOURCE_SALLA_ORDER_REF = "salla_order_ref_upsert"
EXTERNAL_LINK_SOURCE_SALLA_PROFILE = "salla_external_profile"

# Coverage
SOURCE_HISTORY_COMPLETE = "complete"
SOURCE_HISTORY_INCOMPLETE = "incomplete"

SYNC_HEALTH_HEALTHY = "healthy"
SYNC_HEALTH_DEGRADED = "degraded"
SYNC_HEALTH_STALE = "stale"

EXTERNAL_COVERAGE_SCOPE_CLAIM = "external_identity_tuple_bound_orders_only"
INTERNAL_COVERAGE_SCOPE_CLAIM = "nahla_internal_customer_orders_only"

POLICY_ELIGIBILITY_READY = False

# Webhook provider channels
WEBHOOK_CHANNEL_SALLA = "salla"
WEBHOOK_CHANNEL_SALLA_OAUTH = "salla_oauth"

__all__ = [
    "CUSTOMER_LINK_SOURCE_NAHL_BRIDGE",
    "EVIDENCE_AUTHORITATIVE",
    "EVIDENCE_CLASSES",
    "EVIDENCE_INFERRED",
    "EXTERNAL_COVERAGE_SCOPE_CLAIM",
    "EXTERNAL_LINK_SOURCE_SALLA_PROFILE",
    "EXTERNAL_PROVIDER_SALLA_V1",
    "INTERNAL_COVERAGE_SCOPE_CLAIM",
    "LINK_STATE_AMBIGUOUS",
    "LINK_STATE_REJECTED",
    "LINK_STATE_UNLINKED",
    "LINK_STATE_VERIFIED",
    "LINK_STATES",
    "NAHLA_INTERNAL_ORDER_V1",
    "ORDER_SOURCE_EXTERNAL_PROVIDER",
    "ORDER_SOURCE_KINDS",
    "ORDER_SOURCE_MANUAL",
    "ORDER_SOURCE_NAHL_INTERNAL",
    "ORDER_SOURCE_OTHER",
    "ORDER_SOURCE_WHATSAPP",
    "POLICY_ELIGIBILITY_READY",
    "PROFILE_SOURCE_SALLA_CUSTOMER_SYNC",
    "PROFILE_SOURCE_SALLA_ORDER_REF",
    "SOURCE_HISTORY_COMPLETE",
    "SOURCE_HISTORY_INCOMPLETE",
    "SYNC_HEALTH_DEGRADED",
    "SYNC_HEALTH_HEALTHY",
    "SYNC_HEALTH_STALE",
    "UNTRUSTED_ORDER_SOURCE_KINDS",
    "WEBHOOK_CHANNEL_SALLA",
    "WEBHOOK_CHANNEL_SALLA_OAUTH",
]
