"""
Layer 2 — Domain registry metadata (PROPOSED / SHADOW CONTRACT).

Static metadata only. Loader references are strings, never callables.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Tuple

from ..contract import TrustedDomain, TruthSource

CONTRACT_STATUS = "PROPOSED / SHADOW CONTRACT"
SCHEMA_VERSION = "1"


class FreshnessPolicy(str, Enum):
    LIVE_PER_RELEVANT_TURN = "live_per_relevant_turn"
    SHORT_CACHE = "short_cache"
    VERSIONED_CACHE = "versioned_cache"


class PrivacyClassification(str, Enum):
    PUBLIC_MERCHANT = "public_merchant"
    CUSTOMER_PII_MASKED = "customer_pii_masked"
    SECRET_NEVER_LOG = "secret_never_log"


class OwnerAgent(str, Enum):
    AI_ARCHITECTURE = "ai_architecture"
    LIFECYCLE_EXECUTION = "lifecycle_execution"


@dataclass(frozen=True)
class DomainDefinition:
    domain_id: TrustedDomain
    schema_version: str
    official_source: TruthSource
    loader_id: str
    relevance_entities: Tuple[str, ...] = ()
    relevance_triggers: Tuple[str, ...] = ()
    scope: Tuple[str, ...] = ("tenant",)
    freshness_policy: FreshnessPolicy = FreshnessPolicy.LIVE_PER_RELEVANT_TURN
    privacy_classification: PrivacyClassification = PrivacyClassification.PUBLIC_MERCHANT
    actionability: Tuple[str, ...] = ()
    owner_agent: OwnerAgent = OwnerAgent.AI_ARCHITECTURE
    read_only: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        if not isinstance(self.loader_id, str) or not self.loader_id:
            raise ValueError("loader_id must be a non-empty string reference")
        if callable(self.loader_id):  # pragma: no cover - defensive
            raise ValueError("loader_id must not be callable")

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "domain_id": self.domain_id.value,
            "schema_version": self.schema_version,
            "official_source": self.official_source.value,
            "loader_id": self.loader_id,
            "relevance_entities": list(self.relevance_entities),
            "relevance_triggers": list(self.relevance_triggers),
            "scope": list(self.scope),
            "freshness_policy": self.freshness_policy.value,
            "privacy_classification": self.privacy_classification.value,
            "actionability": list(self.actionability),
            "owner_agent": self.owner_agent.value,
            "read_only": self.read_only,
        }


def _initial_registry() -> Tuple[DomainDefinition, ...]:
    return (
        DomainDefinition(
            domain_id=TrustedDomain.CUSTOMER,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.ORDER_PREPARATION_STATE,
            loader_id="trusted_context._load_customer_order_facts",
            relevance_triggers=("always_base",),
            scope=("tenant", "customer"),
            privacy_classification=PrivacyClassification.CUSTOMER_PII_MASKED,
        ),
        DomainDefinition(
            domain_id=TrustedDomain.ORDER,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.ORDER_PREPARATION_STATE,
            loader_id="trusted_context._load_state_order_facts",
            relevance_triggers=("checkout_active", "order_ref", "order_status"),
            scope=("tenant", "order"),
        ),
        DomainDefinition(
            domain_id=TrustedDomain.PAYMENT,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.ORDER_PREPARATION_STATE,
            loader_id="trusted_context._load_payment_shipment_facts",
            relevance_triggers=("payment_query", "receipt", "checkout_active"),
            scope=("tenant", "order"),
            privacy_classification=PrivacyClassification.SECRET_NEVER_LOG,
        ),
        DomainDefinition(
            domain_id=TrustedDomain.SHIPMENT,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.ORDER_PREPARATION_STATE,
            loader_id="trusted_context._load_payment_shipment_facts",
            relevance_triggers=("tracking_query", "shipping_query", "order_ref"),
            scope=("tenant", "order"),
        ),
        DomainDefinition(
            domain_id=TrustedDomain.CATALOG,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.PRODUCTS_TABLE,
            loader_id="trusted_context._load_capability_facts",
            relevance_triggers=("product_query", "catalog_browse", "price_query"),
            scope=("tenant",),
        ),
        DomainDefinition(
            domain_id=TrustedDomain.CAPABILITIES,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.TENANT_SETTINGS,
            loader_id="trusted_context._load_capability_facts",
            relevance_triggers=("always_base",),
            scope=("tenant",),
            freshness_policy=FreshnessPolicy.VERSIONED_CACHE,
        ),
        DomainDefinition(
            domain_id=TrustedDomain.MERCHANT_CAPABILITIES,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.INTEGRATION_CONFIG,
            loader_id="trusted_context._load_merchant_capability_facts",
            relevance_triggers=(
                "payment_query",
                "shipping_query",
                "checkout_active",
            ),
            scope=("tenant",),
            freshness_policy=FreshnessPolicy.VERSIONED_CACHE,
        ),
        DomainDefinition(
            domain_id=TrustedDomain.MERCHANT_PROFILE,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.STORE_SNAPSHOT,
            loader_id="trusted_context._load_merchant_profile_facts",
            relevance_triggers=(
                "store_info",
                "store_about",
                "store_url",
                "contact",
                "social",
                "currency",
                "store_status",
            ),
            scope=("tenant",),
            freshness_policy=FreshnessPolicy.VERSIONED_CACHE,
        ),
        DomainDefinition(
            domain_id=TrustedDomain.MERCHANT_POLICY,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.MERCHANT_KNOWLEDGE_SECTIONS,
            loader_id="trusted_context._load_merchant_policy_facts",
            relevance_triggers=("policy_query", "shipping_policy", "return_policy"),
            scope=("tenant",),
            freshness_policy=FreshnessPolicy.VERSIONED_CACHE,
        ),
        DomainDefinition(
            domain_id=TrustedDomain.COUPONS,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.COUPON_TABLE,
            loader_id="coupon_offer_loader.load_coupon_promotion_facts",
            relevance_triggers=("coupon_intent", "discount_intent", "cart_discount"),
            relevance_entities=("coupon_code", "cart_total"),
            scope=("tenant",),
            privacy_classification=PrivacyClassification.SECRET_NEVER_LOG,
            actionability=("eligibility_read",),
        ),
        DomainDefinition(
            domain_id=TrustedDomain.PROMOTIONS,
            schema_version=SCHEMA_VERSION,
            official_source=TruthSource.PROMOTION_TABLE,
            loader_id="coupon_offer_loader.load_coupon_promotion_facts",
            relevance_triggers=("offer_intent", "promotion_intent"),
            relevance_entities=("product_id", "cart_total"),
            scope=("tenant",),
            privacy_classification=PrivacyClassification.SECRET_NEVER_LOG,
            actionability=("eligibility_read",),
        ),
    )


_REGISTRY: Dict[TrustedDomain, DomainDefinition] = {
    definition.domain_id: definition for definition in _initial_registry()
}


def get_domain_definition(domain: TrustedDomain) -> DomainDefinition:
    try:
        return _REGISTRY[domain]
    except KeyError as exc:
        raise KeyError(f"unregistered TrustedDomain: {domain.value}") from exc


def list_domain_definitions() -> Tuple[DomainDefinition, ...]:
    return tuple(_REGISTRY[d] for d in sorted(_REGISTRY, key=lambda item: item.value))


def registered_domain_ids() -> FrozenSet[str]:
    return frozenset(definition.domain_id.value for definition in _REGISTRY.values())


def domains_for_triggers(trigger_ids: FrozenSet[str]) -> Tuple[TrustedDomain, ...]:
    if not trigger_ids:
        return ()
    selected: List[TrustedDomain] = []
    for definition in _REGISTRY.values():
        if "always_base" in trigger_ids and definition.domain_id in (
            TrustedDomain.CUSTOMER,
            TrustedDomain.CAPABILITIES,
        ):
            selected.append(definition.domain_id)
            continue
        if any(trigger in trigger_ids for trigger in definition.relevance_triggers):
            selected.append(definition.domain_id)
    seen: set[str] = set()
    ordered: List[TrustedDomain] = []
    for domain in selected:
        if domain.value not in seen:
            seen.add(domain.value)
            ordered.append(domain)
    return tuple(ordered)


__all__ = [
    "CONTRACT_STATUS",
    "DomainDefinition",
    "FreshnessPolicy",
    "OwnerAgent",
    "PrivacyClassification",
    "SCHEMA_VERSION",
    "domains_for_triggers",
    "get_domain_definition",
    "list_domain_definitions",
    "registered_domain_ids",
]
