"""Unified Truth Surface — Phase 1 inventory + Phase 2 UTS v1 shadow."""

from .contract import (
    EffectiveFact,
    EffectiveFactStatus,
    FactDomain,
    IntegrityGateReport,
    OperationalFact,
    OperationalFactKind,
    OperationalFactsBlock,
    TruthSource,
    TruthSurface,
    TruthSurfaceInventory,
    TruthSurfaceReport,
    UTS_V1_INGEST_SURFACES,
)
from .flags import (
    is_truth_surface_shadow_enabled,
    is_uts_v1_enforce_enabled,
    is_uts_v1_shadow_enabled,
)
from .inventory import build_truth_surface_inventory
from .shadow_audit import run_truth_surface_shadow_audit
from .uts_v1 import UtsV1ShadowResult, build_uts_v1_manifest, run_uts_v1_shadow

__all__ = [
    "EffectiveFact",
    "EffectiveFactStatus",
    "FactDomain",
    "IntegrityGateReport",
    "OperationalFact",
    "OperationalFactKind",
    "OperationalFactsBlock",
    "TruthSource",
    "TruthSurface",
    "TruthSurfaceInventory",
    "TruthSurfaceReport",
    "UTS_V1_INGEST_SURFACES",
    "UtsV1ShadowResult",
    "build_truth_surface_inventory",
    "build_uts_v1_manifest",
    "is_truth_surface_shadow_enabled",
    "is_uts_v1_enforce_enabled",
    "is_uts_v1_shadow_enabled",
    "run_truth_surface_shadow_audit",
    "run_uts_v1_shadow",
]
