"""
backend/modules/ai/orchestrator/provider_router.py
────────────────────────────────────────────────────
Canonical provider-routing policy for the Nahla AI orchestrator.

STATUS: Active routing metadata.
  - This module defines the provider priority order used by the canonical
    orchestrator pipeline.
  - The engine now consumes provider_chain and attempts providers in order.
  - No route, schema, webhook, or adapter contract is modified here.

Purpose:
  Define the canonical provider priority order and a helper for building
  provider chains so the orchestrator can route and fall through providers
  deterministically inside modules.ai.orchestrator.

Current active flow:
  anthropic
    → openai_compatible
    → gemini
    → safe empty-reply fallback in the caller legacy path

Architecture rule preserved:
  No route or service may call providers directly.
  Provider selection belongs exclusively to modules.ai.orchestrator.

Used by:
  modules.ai.orchestrator.pipeline
  modules.ai.orchestrator.engine
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from modules.ai.orchestrator.types import AIProvider


# ── Canonical provider order ───────────────────────────────────────────────────
# This is the active priority sequence when Nahla routes between providers.
# Lower index = higher priority.
# Order rationale:
#   1. anthropic          — primary provider for current production behavior
#   2. openai_compatible  — secondary compatible fallback
#   3. gemini             — tertiary Google fallback
#   4. mock               — deterministic fallback for dev/testing environments
#
# "unknown" is intentionally absent — it signals a misconfiguration, not a
# valid routing target.

DEFAULT_PROVIDER_CHAIN: List[AIProvider] = [
    "anthropic",
    "openai_compatible",
    "gemini",
    "mock",
]


# ── Provider chain config ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProviderChainConfig:
    """
    Immutable specification of the provider priority chain for one request.

    Attributes
    ----------
    providers   : ordered list of providers to attempt, highest priority first
    hint        : optional caller-supplied preference that may be promoted to
                  the front of the chain at selection time
    allow_mock  : whether the mock provider is permitted as a final fallback
                  (disabled in production by default)
    """
    providers:  List[AIProvider] = field(default_factory=lambda: list(DEFAULT_PROVIDER_CHAIN))
    hint:       Optional[AIProvider] = None
    allow_mock: bool = False


# ── Provider chain builder ─────────────────────────────────────────────────────

def get_provider_chain(
    hint: Optional[AIProvider] = None,
    *,
    allow_mock: bool = False,
    override_chain: Optional[Sequence[AIProvider]] = None,
) -> ProviderChainConfig:
    """
    Build the canonical provider chain config for one orchestration request.

    This function is the single place where provider ordering decisions live.
    Callers (engine, pipeline) query it to understand which providers to try
    and in what order.

    Parameters
    ----------
    hint           : optional caller-supplied provider preference.
                     When provided, it is recorded in the config for
                     observability and future per-request prioritisation.
                     Current behavior does NOT reorder the active chain.
    allow_mock     : if True, the mock provider is retained in the chain.
                     Should only be True in dev / test environments.
    override_chain : if provided, replaces the DEFAULT_PROVIDER_CHAIN entirely.
                     For testing or special deployment overrides only.

    Returns
    -------
    ProviderChainConfig — immutable, safe to pass between layers.

    Current behavior
    ----------------
    This function is called from the canonical orchestration pipeline for
    live adapter-driven requests.  It builds provider-chain metadata that the
    engine now consumes to attempt providers in order.
    """
    if override_chain is not None:
        base: List[AIProvider] = list(override_chain)
    else:
        base = list(DEFAULT_PROVIDER_CHAIN)

    if not allow_mock and "mock" in base:
        base = [p for p in base if p != "mock"]

    return ProviderChainConfig(
        providers=base,
        hint=hint,
        allow_mock=allow_mock,
    )


# ── Provider availability check (stub) ────────────────────────────────────────

def is_provider_configured(provider: AIProvider) -> bool:
    """
    Return True if the given provider appears to be configured in the current
    environment (API key present, SDK available, etc.).

    STATUS: Stub only.
      Always returns False for gemini and mock.
      For anthropic and openai_compatible, checks environment variables.
      Does NOT attempt any network call.

    Intended to be used by the engine/pipeline selection loop in a future step.
    """
    import os
    if provider == "anthropic":
        return bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
        )
    if provider == "openai_compatible":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if provider == "gemini":
        return False  # not yet integrated
    if provider == "mock":
        return False  # only usable when allow_mock=True in non-production
    return False
