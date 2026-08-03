"""
Layer 3 LLM provider gate — OpenAI Luna path only.

Layer 3 requires real ``DefaultComposer._llm_compose`` → ``generate_ai_reply``
→ ``OpenAICompatibleProvider`` (gpt-5.6-luna). Anthropic is not the merchant
chat default and must not be substituted when OpenAI is unavailable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from modules.ai.orchestrator.customer_chat_models import MODEL_LUNA


@dataclass(frozen=True)
class Layer3LLMConfig:
    provider: str
    model: str
    router_enabled: bool
    premium_allowed: bool

    def to_report_dict(self) -> Dict[str, Any]:
        return {
            "llm_provider_and_model": f"{self.provider}/{self.model}",
            "router_enabled": self.router_enabled,
            "premium_allowed": self.premium_allowed,
        }


def openai_key_present() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def anthropic_key_present() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("CLAUDE_API_KEY", "").strip()
    )


def resolve_layer3_llm_config() -> Optional[Layer3LLMConfig]:
    """Return config when Layer 3 can run; None when blocked."""
    if not openai_key_present():
        return None
    model = (
        os.environ.get("NAHLA_MODEL_CHEAP", "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or MODEL_LUNA
    )
    router = os.getenv("NAHLA_MODEL_ROUTER_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    premium = os.getenv("ALLOW_PREMIUM_MODEL", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    return Layer3LLMConfig(
        provider="openai_compatible",
        model=model,
        router_enabled=router,
        premium_allowed=premium,
    )


def layer3_blocker_reason() -> str:
    if openai_key_present():
        return ""
    anthropic = "present" if anthropic_key_present() else "absent"
    return (
        "OPENAI_API_KEY absent — Layer 3 requires live OpenAI Luna compose "
        f"(DefaultComposer._llm_compose -> OpenAICompatibleProvider). "
        f"ANTHROPIC_API_KEY={anthropic} cannot substitute merchant chat path."
    )


def apply_layer3_process_env() -> None:
    """Test-process env only — does not change production defaults."""
    os.environ.setdefault("NAHLA_MODEL_ROUTER_ENABLED", "true")
    os.environ.setdefault("ALLOW_PREMIUM_MODEL", "false")
    os.environ.setdefault("NAHLA_MODEL_CHEAP", MODEL_LUNA)
    os.environ.setdefault("ORDER_FLOW_V2_ENABLED", "false")
    os.environ.setdefault("ORDER_FLOW_V2_SHADOW_ENABLED", "true")


__all__ = [
    "Layer3LLMConfig",
    "anthropic_key_present",
    "apply_layer3_process_env",
    "layer3_blocker_reason",
    "openai_key_present",
    "resolve_layer3_llm_config",
]
