"""FactBoundPersonaComposer — verified-facts phrasing layer (Phase 2)."""
from .fact_bound_composer import FactBoundPersonaComposer
from .facts_bundle import PersonaComposeResult, PersonaConstraints, PersonaFactsBundle

__all__ = [
    "FactBoundPersonaComposer",
    "PersonaComposeResult",
    "PersonaConstraints",
    "PersonaFactsBundle",
]
