"""Catalog intelligence layer — provider-agnostic product discovery."""

from .catalog_intelligence import (
    CatalogGroup,
    CatalogIntelligence,
    DiscoveryPlan,
    compute_discovery_score,
)
from .catalog_provider import (
    CatalogProvider,
    get_catalog_provider,
)
from .presentation_contract import (
    discovery_has_catalog_evidence,
    validate_discovery_products,
)

__all__ = [
    "CatalogGroup",
    "CatalogIntelligence",
    "CatalogProvider",
    "DiscoveryPlan",
    "compute_discovery_score",
    "discovery_has_catalog_evidence",
    "get_catalog_provider",
    "validate_discovery_products",
]
