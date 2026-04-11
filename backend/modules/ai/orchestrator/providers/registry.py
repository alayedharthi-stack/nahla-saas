"""
backend/modules/ai/orchestrator/providers/registry.py
──────────────────────────────────────────────────────
Provider registry for the Nahla AI orchestration engine.

Public surface:
  get_provider(name)      — returns a BaseAIProvider instance or None
  registered_names()      — returns list of registered canonical names

Currently registered:
  "anthropic"         →  AnthropicProvider
  "openai_compatible" →  OpenAICompatibleProvider
  "gemini"            →  GeminiProvider

Architecture rule:
  The engine resolves providers through this registry, not by importing
  concrete implementations directly. This keeps provider coupling at the
  registry boundary and makes future provider additions a registry-only change.

To add a new provider:
  1. Create its implementation in providers/<name>_provider.py
  2. Register it here: _REGISTRY["<name>"] = NewProvider()
  The engine and pipeline require no changes beyond that.

Status:
  All registered providers are available to provider-chain execution.
  Runtime order is owned by modules.ai.orchestrator.provider_router and
  consumed by modules.ai.orchestrator.engine.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from modules.ai.orchestrator.providers.base import BaseAIProvider
from modules.ai.orchestrator.providers.anthropic_provider import AnthropicProvider
from modules.ai.orchestrator.providers.gemini_provider import GeminiProvider
from modules.ai.orchestrator.providers.openai_compatible_provider import (
    OpenAICompatibleProvider,
)

# ── Registry ───────────────────────────────────────────────────────────────────
# Keyed by canonical provider name (matches AIProvider literals in types.py).
# Each value is a single shared provider instance — providers must be stateless.

_REGISTRY: Dict[str, BaseAIProvider] = {
    "anthropic":         AnthropicProvider(),
    "openai_compatible": OpenAICompatibleProvider(),
    "gemini":            GeminiProvider(),
}


def get_provider(name: str) -> Optional[BaseAIProvider]:
    """
    Resolve a registered provider by canonical name.

    Returns the provider instance, or None if the name is not registered.
    A None return signals the caller to fall back to its own safe behavior.
    """
    return _REGISTRY.get(name)


def registered_names() -> List[str]:
    """Return the list of currently registered canonical provider names."""
    return list(_REGISTRY.keys())
