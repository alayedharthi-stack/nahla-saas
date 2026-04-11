"""
backend/modules/ai/orchestrator/providers/base.py
──────────────────────────────────────────────────
Minimal abstract base for AI provider implementations.

All provider implementations (anthropic, openai_compatible, gemini, mock)
must subclass BaseAIProvider and satisfy this interface.

The engine delegates to a provider through this contract so it never owns
provider-specific execution logic directly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseAIProvider(ABC):
    """
    Minimal interface that all AI provider implementations must satisfy.

    The dict returned by call() must always contain these keys:
      reply_text : str  — the generated text; empty string signals failure
      provider   : str  — canonical provider name (matches AIProvider literal)
      model      : str  — model identifier used
      status     : str  — "ok" | error code string

    Implementations must never raise from call() — return reply_text=""
    on any failure so the engine and upstream callers can fall through safely.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Canonical provider name, matches AIProvider literals in types.py."""
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        """
        Return True if this provider has the required credentials/SDK
        available in the current environment.

        Must not perform any network call.
        """
        ...

    @abstractmethod
    def call(self, message: str, prompt: str) -> Dict[str, Any]:
        """
        Invoke the provider synchronously and return a result dict.

        Parameters
        ----------
        message : inbound customer message (user turn)
        prompt  : pre-built system prompt from the prompt builder

        Must never raise.  Return reply_text="" on any failure.
        """
        ...
