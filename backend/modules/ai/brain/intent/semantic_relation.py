"""
Canonical intent semantic-relation registry.

Platform owner for structured relationships between existing intent labels:
  * semantic domain / family
  * optional direct broader (parent/coarser) label

This module does not parse customer language, does not consult Layer 2
vocabulary, does not map execution actions, and does not decide classifier
precedence. Absence of a relation is UNKNOWN — never inferred as compatible.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Mapping, Optional

from ..types import (
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_PRODUCT_VISUAL_REQUEST,
)


class IntentSemanticDomain(str, Enum):
    PRODUCT_INQUIRY = "product_inquiry"


@dataclass(frozen=True)
class IntentSemanticRelation:
    """Declared semantic placement of one existing intent label."""

    domain: IntentSemanticDomain
    broader_label: Optional[str] = None


class IntentSemanticRegistryError(ValueError):
    """Raised when a proposed registry violates structural invariants."""


def _normalize_intent_label(name: object) -> str:
    return str(name or "").strip()


def validate_intent_semantic_registry(
    registry: Mapping[str, IntentSemanticRelation],
) -> None:
    """Fail closed on self-parents, cycles, dangling parents, and domain mismatch."""
    for label, relation in registry.items():
        key = _normalize_intent_label(label)
        if not key:
            raise IntentSemanticRegistryError("registry keys must be non-empty intent labels")
        if not isinstance(relation, IntentSemanticRelation):
            raise IntentSemanticRegistryError(f"{key!r} is not an IntentSemanticRelation")
        parent = _normalize_intent_label(relation.broader_label)
        if not parent:
            continue
        if parent == key:
            raise IntentSemanticRegistryError(f"{key!r} cannot be its own broader_label")
        if parent not in registry:
            raise IntentSemanticRegistryError(
                f"{key!r} broader_label {parent!r} is not registered"
            )
        parent_relation = registry[parent]
        if parent_relation.domain != relation.domain:
            raise IntentSemanticRegistryError(
                f"{key!r} domain {relation.domain.value!r} does not match "
                f"broader_label {parent!r} domain {parent_relation.domain.value!r}"
            )
        seen = {key}
        walk = parent
        while walk:
            if walk in seen:
                raise IntentSemanticRegistryError(
                    f"broader_label cycle involving {key!r}"
                )
            seen.add(walk)
            nxt = registry.get(walk)
            walk = _normalize_intent_label(nxt.broader_label) if nxt is not None else ""


def _build_registry() -> Dict[str, IntentSemanticRelation]:
    """Deliberately small, evidence-backed first registry.

    Location/store-info and payment/contact comments in types.py describe
    execution carve-outs (Maps URL vs storefront URL; media attach vs FAQ
    template). They are not encoded here.
    """
    registry: Dict[str, IntentSemanticRelation] = {
        INTENT_ASK_PRODUCT: IntentSemanticRelation(
            domain=IntentSemanticDomain.PRODUCT_INQUIRY,
            broader_label=None,
        ),
        INTENT_ASK_PRICE: IntentSemanticRelation(
            domain=IntentSemanticDomain.PRODUCT_INQUIRY,
            broader_label=None,
        ),
        INTENT_PRODUCT_VISUAL_REQUEST: IntentSemanticRelation(
            domain=IntentSemanticDomain.PRODUCT_INQUIRY,
            broader_label=INTENT_ASK_PRODUCT,
        ),
    }
    validate_intent_semantic_registry(registry)
    return registry


_REGISTRY: Dict[str, IntentSemanticRelation] = _build_registry()


def get_intent_semantic_relation(intent_name: object) -> Optional[IntentSemanticRelation]:
    """Return the declared relation, or None when the relationship is UNKNOWN."""
    key = _normalize_intent_label(intent_name)
    if not key:
        return None
    return _REGISTRY.get(key)


def is_direct_broader_relation(specific_intent: object, broader_intent: object) -> bool:
    """True only when the registry proves same domain and a direct parent pointer.

    Same domain alone is not enough. Unregistered labels fail closed (False).
    """
    specific = _normalize_intent_label(specific_intent)
    broader = _normalize_intent_label(broader_intent)
    if not specific or not broader or specific == broader:
        return False
    child = _REGISTRY.get(specific)
    parent = _REGISTRY.get(broader)
    if child is None or parent is None:
        return False
    if child.domain != parent.domain:
        return False
    return _normalize_intent_label(child.broader_label) == broader


__all__ = [
    "IntentSemanticDomain",
    "IntentSemanticRegistryError",
    "IntentSemanticRelation",
    "get_intent_semantic_relation",
    "is_direct_broader_relation",
    "validate_intent_semantic_registry",
]
