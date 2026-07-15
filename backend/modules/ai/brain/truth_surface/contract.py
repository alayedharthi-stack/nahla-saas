"""
truth_surface/contract.py
─────────────────────────
Unified Truth Surface (UTS) — contract definitions for Phase 1 inventory and Phase 2 UTS v1.

Aligned with AGENTS.md:
  • Operational correctness may be deterministic.
  • Personality must never be deterministic.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class OperationalFactKind(str, Enum):
    """Categories of facts that affect operational claims to the customer."""

    PRICE = "price"
    AVAILABILITY = "availability"
    PRODUCT_LINK = "product_link"
    PRODUCT_TITLE = "product_title"
    SHIPPING = "shipping"
    POLICY = "policy"
    COUPON = "coupon"
    ORDER_STATUS = "order_status"
    PAYMENT_STATE = "payment_state"
    STORE_IDENTITY = "store_identity"
    CONTACT = "contact"
    PLATFORM_SUBSCRIPTION = "platform_subscription"
    MEDIA_KEY = "media_key"
    FAQ = "faq"
    USAGE_GUIDANCE = "usage_guidance"
    OTHER_OPERATIONAL = "other_operational"


class TruthSource(str, Enum):
    """Origin datastore or system — where raw truth is read from."""

    MERCHANT_KNOWLEDGE_SECTIONS = "merchant_knowledge_sections"
    MANUAL_KNOWLEDGE_BASE = "manual_knowledge_base"
    PRODUCTS_TABLE = "products_table"
    PLATFORM_FEED = "platform_feed"
    STORE_SNAPSHOT = "store_snapshot"
    ORDER_PREPARATION_STATE = "order_preparation_state"
    TENANT_SETTINGS = "tenant_settings"
    COUPON_TABLE = "coupon_table"
    PROMOTION_TABLE = "promotion_table"
    MEDIA_REGISTRY = "media_registry"
    CONVERSATION_HISTORY = "conversation_history"
    GOAL_KB_RETRIEVAL = "goal_kb_retrieval"
    AI_SETTINGS = "ai_settings"
    DETERMINISTIC_TEMPLATE = "deterministic_template"
    UNKNOWN = "unknown"


class TruthSurface(str, Enum):
    """Channel through which operational content reaches (or could reach) the LLM."""

    # Prompt block 3 — facts
    STRUCTURED_FACTS_BLOCK = "structured_facts_block"
    OVERLAY_FACTS_FALLBACK = "overlay_facts_fallback"
    PLATFORM_KB_EXCERPT = "platform_kb_excerpt"
    CLARIFICATION_EVIDENCE = "clarification_evidence"

    # BrainStateJSON fields
    BRAIN_STATE_JSON = "brain_state_json"
    KNOWN_FACTS = "known_facts"
    MERCHANT_CONTEXT_PRODUCTS = "merchant_context.products"
    MERCHANT_CONTEXT_POLICIES = "merchant_context.policies"
    MERCHANT_CONTEXT_FAQ = "merchant_context.faq_approved"
    MERCHANT_CONTEXT_AI_SETTINGS = "merchant_context.ai_settings"
    MERCHANT_CONTEXT_CONVERSATION = "merchant_context.conversation"
    SELECTED_PRODUCT = "selected_product"
    LAST_RECOMMENDED_PRODUCTS = "last_recommended_products"
    STORE_KNOWLEDGE = "store_knowledge"
    COUPON_POLICY = "coupon_policy"
    CHECKOUT_PREPARATION = "known_facts.checkout_preparation"

    # Decision / goal layers
    RESPONSE_GOAL = "response_goal"
    GOAL_REGIMEN_BUNDLE = "goal_regimen_bundle"

    # Tools / resolver
    TOOLS_LIBRARIES = "tools.libraries"
    RESOLVER_OVERLAY = "resolver_overlay"

    # High-priority operational guidance (precedence rules — not merchant facts)
    HIGH_PRIORITY_PRECEDENCE = "high_priority.precedence"

    # Provider message array — echo of prior operational claims
    CHAT_HISTORY = "chat_history"

    # Loaded but intentionally excluded from primary brain prompt (dead / latent)
    TENANT_OVERLAY_LEGACY = "tenant_overlay_legacy"
    SALES_CONTEXT_METADATA = "sales_context_metadata"
    FULL_MERCHANT_CONTEXT_LATENT = "full_merchant_context_latent"

    # Legacy WhatsApp path (parallel stack — not brain thin path)
    LEGACY_BUILD_AI_CONTEXT = "legacy.build_ai_context"
    LEGACY_TENANT_OVERLAY = "legacy.tenant_overlay"


UTS_V1_INGEST_SURFACES: frozenset[TruthSurface] = frozenset({
    TruthSurface.STRUCTURED_FACTS_BLOCK,
    TruthSurface.MERCHANT_CONTEXT_PRODUCTS,
    TruthSurface.SELECTED_PRODUCT,
    TruthSurface.LAST_RECOMMENDED_PRODUCTS,
    TruthSurface.CHECKOUT_PREPARATION,
    TruthSurface.MERCHANT_CONTEXT_POLICIES,
    TruthSurface.KNOWN_FACTS,
    TruthSurface.PLATFORM_KB_EXCERPT,
    TruthSurface.GOAL_REGIMEN_BUNDLE,
})

# Surfaces that are personality-only — excluded from operational inventory.
PERSONALITY_EXCLUDED_SURFACES: frozenset[TruthSurface] = frozenset()


class FactDomain(str, Enum):
    CATALOG = "catalog"
    KNOWLEDGE = "knowledge"
    ORDER = "order"
    POLICY = "policy"
    PLATFORM = "platform"
    GOAL = "goal"
    STORE = "store"


class TrustedDomain(str, Enum):
    """Core trusted-context domains for pre-decide snapshot."""

    CUSTOMER = "customer"
    ORDER = "order"
    PAYMENT = "payment"
    SHIPMENT = "shipment"
    CATALOG = "catalog"
    CAPABILITIES = "capabilities"
    MERCHANT_POLICY = "merchant_policy"
    COUPONS = "coupons"
    PROMOTIONS = "promotions"
    CUSTOMER_CONDITIONAL_COUPON = "customer_conditional_coupon"


@dataclass(frozen=True)
class TrustedFact:
    """One atomic trusted fact with provenance — no customer-facing prose."""

    domain: TrustedDomain
    key: str
    value: Any
    source: TruthSource
    path: str = ""
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain.value,
            "key": self.key,
            "value": self.value,
            "source": self.source.value,
            "path": self.path,
            "confidence": self.confidence,
        }


@dataclass
class TrustedContextSnapshot:
    """
    Single authoritative pre-decide context snapshot for one turn.

    Does not contain LLM prose. Consumers request domain projections only.
    """

    snapshot_id: str = ""
    tenant_id: int = 0
    customer_phone: str = ""
    conversation_id: Optional[int] = None
    facts: List[TrustedFact] = field(default_factory=list)
    loaded_domains: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    built_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    shadow_observability: Dict[str, Any] = field(default_factory=dict)

    def ensure_snapshot_id(self) -> str:
        if not self.snapshot_id:
            self.snapshot_id = uuid.uuid4().hex
        return self.snapshot_id

    def lookup(self, domain: TrustedDomain, key: str) -> Optional[TrustedFact]:
        for fact in self.facts:
            if fact.domain == domain and fact.key == key:
                return fact
        return None

    def facts_for_domain(self, domain: TrustedDomain) -> Tuple[TrustedFact, ...]:
        return tuple(f for f in self.facts if f.domain == domain)

    def projection(
        self,
        domains: Optional[List[TrustedDomain]] = None,
    ) -> Dict[str, Any]:
        """Structured facts projection — safe for DecisionPlan / compose hints."""
        allowed = {d.value for d in domains} if domains else None
        out: Dict[str, Dict[str, Any]] = {}
        for fact in self.facts:
            if allowed is not None and fact.domain.value not in allowed:
                continue
            bucket = out.setdefault(fact.domain.value, {})
            bucket[fact.key] = fact.value
        return {
            "snapshot_id": self.ensure_snapshot_id(),
            "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id,
            "loaded_domains": list(self.loaded_domains),
            "sources": list(self.sources),
            "facts": out,
        }

    def to_log_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "snapshot_id": self.ensure_snapshot_id(),
            "tenant_id": self.tenant_id,
            "customer_phone_tail": (self.customer_phone or "")[-4:],
            "conversation_id": self.conversation_id,
            "loaded_domains": self.loaded_domains,
            "sources": self.sources,
            "fact_count": len(self.facts),
            "built_at_ms": self.built_at_ms,
        }
        if self.shadow_observability:
            out["shadow_observability"] = dict(self.shadow_observability)
        return out

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.ensure_snapshot_id(),
            "loaded_domains": list(self.loaded_domains),
            "sources": list(self.sources),
            "fact_count": len(self.facts),
        }


class EffectiveFactStatus(str, Enum):
    ACTIVE = "active"
    DEDUPED = "deduped"
    CONFLICT = "conflict"
    SHADOW = "shadow"


@dataclass(frozen=True)
class OperationalFact:
    """One atomic operational datum observed on a truth surface."""

    kind: OperationalFactKind
    key: str
    value: str
    surface: TruthSurface
    source: TruthSource
    path: str = ""


@dataclass(frozen=True)
class EffectiveFact:
    """One operational fact in the UTS v1 manifest."""

    fact_key: str
    fact_domain: FactDomain
    value: str
    source_surface: TruthSurface
    source: TruthSource
    confidence: float = 1.0
    status: EffectiveFactStatus = EffectiveFactStatus.ACTIVE
    reason: str = ""
    path: str = ""
    kind: OperationalFactKind = OperationalFactKind.OTHER_OPERATIONAL


@dataclass(frozen=True)
class OperationalFactsBlock:
    """Single proposed operational egress block (Phase 2 shadow)."""

    text: str
    fact_count: int = 0
    active_fact_count: int = 0


@dataclass
class TruthSurfaceReport:
    """UTS v1 manifest for one LLM-bound turn."""

    tenant_id: Optional[int] = None
    intent: str = ""
    stage: str = ""
    effective_facts: List[EffectiveFact] = field(default_factory=list)
    operational_facts_block: Optional[OperationalFactsBlock] = None
    ingested_surfaces: List[str] = field(default_factory=list)
    raw_fact_count: int = 0
    deduped_count: int = 0
    active_fact_count: int = 0

    def to_log_dict(self) -> Dict[str, Any]:
        block = self.operational_facts_block
        return {
            "tenant_id": self.tenant_id,
            "intent": self.intent or None,
            "stage": self.stage or None,
            "ingested_surfaces": self.ingested_surfaces,
            "raw_fact_count": self.raw_fact_count,
            "effective_facts_count": len(self.effective_facts),
            "active_fact_count": self.active_fact_count,
            "deduped_count": self.deduped_count,
            "operational_facts_block_chars": len(block.text) if block else 0,
            "operational_facts_block_preview": (
                (block.text[:400] + "…")
                if block and len(block.text) > 400
                else (block.text if block else "")
            ),
        }


@dataclass
class IntegrityGateReport:
    """Shadow-only prompt integrity measurements."""

    duplicate_fact_keys: int = 0
    conflicting_fact_values: int = 0
    external_operational_surfaces_count: int = 0
    external_operational_facts_count: int = 0
    duplicate_keys: List[str] = field(default_factory=list)
    conflict_keys: List[str] = field(default_factory=list)
    external_surfaces: List[str] = field(default_factory=list)
    leakage_chat_history: int = 0
    leakage_brain_state_json: int = 0
    leakage_coupon_policy: int = 0
    leakage_store_knowledge: int = 0

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "duplicate_fact_keys": self.duplicate_fact_keys,
            "conflicting_fact_values": self.conflicting_fact_values,
            "external_operational_surfaces_count": self.external_operational_surfaces_count,
            "external_operational_facts_count": self.external_operational_facts_count,
            "duplicate_keys": self.duplicate_keys[:20],
            "conflict_keys": self.conflict_keys[:20],
            "external_surfaces": self.external_surfaces,
            "leakage": {
                "chat_history": self.leakage_chat_history,
                "brain_state_json": self.leakage_brain_state_json,
                "coupon_policy": self.leakage_coupon_policy,
                "store_knowledge": self.leakage_store_knowledge,
            },
        }


@dataclass
class DuplicateGroup:
    """Same fact key repeated across multiple surfaces."""

    key: str
    kind: OperationalFactKind
    surfaces: List[str] = field(default_factory=list)
    values: List[str] = field(default_factory=list)


@dataclass
class ConflictGroup:
    """Same fact key with incompatible values across surfaces."""

    key: str
    kind: OperationalFactKind
    entries: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SurfacePresence:
    """Whether a truth surface carried content on this turn."""

    surface: TruthSurface
    active: bool
    char_count: int = 0
    fact_count: int = 0
    source: TruthSource = TruthSource.UNKNOWN


@dataclass
class TruthSurfaceInventory:
    """Full shadow inventory for one LLM-bound turn."""

    tenant_id: Optional[int] = None
    intent: str = ""
    stage: str = ""
    surfaces_active: List[SurfacePresence] = field(default_factory=list)
    facts: List[OperationalFact] = field(default_factory=list)
    duplicates: List[DuplicateGroup] = field(default_factory=list)
    conflicts: List[ConflictGroup] = field(default_factory=list)
    latent_surfaces: List[str] = field(default_factory=list)

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "intent": self.intent or None,
            "stage": self.stage or None,
            "surfaces_active_count": sum(1 for s in self.surfaces_active if s.active),
            "surfaces_total": len(self.surfaces_active),
            "surfaces_active": [
                {
                    "surface": s.surface.value,
                    "active": s.active,
                    "char_count": s.char_count,
                    "fact_count": s.fact_count,
                }
                for s in self.surfaces_active
                if s.active
            ],
            "fact_count": len(self.facts),
            "duplicate_count": len(self.duplicates),
            "conflict_count": len(self.conflicts),
            "duplicates": [
                {
                    "key": d.key,
                    "kind": d.kind.value,
                    "surfaces": d.surfaces,
                    "values": d.values,
                }
                for d in self.duplicates
            ],
            "conflicts": [
                {
                    "key": c.key,
                    "kind": c.kind.value,
                    "entries": c.entries,
                }
                for c in self.conflicts
            ],
            "latent_surfaces": self.latent_surfaces,
        }


__all__ = [
    "ConflictGroup",
    "DuplicateGroup",
    "EffectiveFact",
    "EffectiveFactStatus",
    "FactDomain",
    "IntegrityGateReport",
    "OperationalFact",
    "OperationalFactKind",
    "OperationalFactsBlock",
    "PERSONALITY_EXCLUDED_SURFACES",
    "SurfacePresence",
    "TrustedContextSnapshot",
    "TrustedDomain",
    "TrustedFact",
    "TruthSource",
    "TruthSurface",
    "TruthSurfaceInventory",
    "TruthSurfaceReport",
    "UTS_V1_INGEST_SURFACES",
]
