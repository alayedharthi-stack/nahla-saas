"""
ai_usage_pricing.py
───────────────────
Pricing v2 for AI usage ledger — per provider/model with cache token rates.

All costs are USD, excluding VAT and billing/subscription logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional, Tuple

PRICING_VERSION = "2026-06-v1"

# cost_source distinguishes ledger estimate origin (routing unchanged by this field):
#   "provisional" — placeholder rates until billing confirms list pricing
#   "invoice"     — reconciled against provider invoice (not yet wired)
COST_SOURCE_PROVISIONAL = "provisional"
COST_SOURCE_INVOICE = "invoice"

_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class ModelPricingV2:
    input_per_1m: Decimal
    output_per_1m: Decimal
    cache_read_per_1m: Decimal
    cache_write_per_1m: Decimal


# Longest-prefix match on model name (lowercase).
_MODEL_PRICING_V2: Dict[str, ModelPricingV2] = {
    # Anthropic Claude — Opus
    "claude-opus-4-8": ModelPricingV2(
        Decimal("15"), Decimal("75"), Decimal("1.50"), Decimal("18.75"),
    ),
    "claude-opus-4-6": ModelPricingV2(
        Decimal("15"), Decimal("75"), Decimal("1.50"), Decimal("18.75"),
    ),
    "claude-opus-4": ModelPricingV2(
        Decimal("15"), Decimal("75"), Decimal("1.50"), Decimal("18.75"),
    ),
    "claude-3-opus": ModelPricingV2(
        Decimal("15"), Decimal("75"), Decimal("1.50"), Decimal("18.75"),
    ),
    # Sonnet
    "claude-sonnet-4-6": ModelPricingV2(
        Decimal("3"), Decimal("15"), Decimal("0.30"), Decimal("3.75"),
    ),
    "claude-3-5-sonnet": ModelPricingV2(
        Decimal("3"), Decimal("15"), Decimal("0.30"), Decimal("3.75"),
    ),
    "claude-3-sonnet": ModelPricingV2(
        Decimal("3"), Decimal("15"), Decimal("0.30"), Decimal("3.75"),
    ),
    # Haiku
    "claude-haiku-4-5": ModelPricingV2(
        Decimal("0.80"), Decimal("4"), Decimal("0.08"), Decimal("1.00"),
    ),
    "claude-3-5-haiku": ModelPricingV2(
        Decimal("0.80"), Decimal("4"), Decimal("0.08"), Decimal("1.00"),
    ),
    "claude-3-haiku": ModelPricingV2(
        Decimal("0.25"), Decimal("1.25"), Decimal("0.025"), Decimal("0.3125"),
    ),
    "claude": ModelPricingV2(
        Decimal("3"), Decimal("15"), Decimal("0.30"), Decimal("3.75"),
    ),
    # OpenAI customer-chat models (provisional pricing — confirm with billing).
    "gpt-5.6-luna": ModelPricingV2(
        Decimal("0.015"), Decimal("0.060"), Decimal("0.0075"), Decimal("0.01875"),
    ),
    "gpt-5.6-terra": ModelPricingV2(
        Decimal("0.150"), Decimal("0.600"), Decimal("0.075"), Decimal("0.1875"),
    ),
    "gpt-5.6-sol": ModelPricingV2(
        Decimal("0.375"), Decimal("1.500"), Decimal("0.1875"), Decimal("0.46875"),
    ),
    "gpt-4o-mini": ModelPricingV2(
        Decimal("0.15"), Decimal("0.60"), Decimal("0.075"), Decimal("0.1875"),
    ),
    "gpt-4o": ModelPricingV2(
        Decimal("2.50"), Decimal("10"), Decimal("1.25"), Decimal("3.125"),
    ),
    "gpt-4-turbo": ModelPricingV2(
        Decimal("10"), Decimal("30"), Decimal("5"), Decimal("12.5"),
    ),
    "gpt-4": ModelPricingV2(
        Decimal("30"), Decimal("60"), Decimal("15"), Decimal("37.5"),
    ),
    "gpt-3.5-turbo": ModelPricingV2(
        Decimal("0.50"), Decimal("1.50"), Decimal("0.25"), Decimal("0.625"),
    ),
    # Gemini (future-ready)
    "gemini-2.0-flash-lite": ModelPricingV2(
        Decimal("0.075"), Decimal("0.30"), Decimal("0.01875"), Decimal("0.09375"),
    ),
    "gemini-2.0-flash": ModelPricingV2(
        Decimal("0.10"), Decimal("0.40"), Decimal("0.025"), Decimal("0.125"),
    ),
    "gemini-2.0-pro": ModelPricingV2(
        Decimal("3.50"), Decimal("10.50"), Decimal("0.875"), Decimal("4.375"),
    ),
    "gemini-1.5-flash": ModelPricingV2(
        Decimal("0.075"), Decimal("0.30"), Decimal("0.01875"), Decimal("0.09375"),
    ),
    "gemini-1.5-pro": ModelPricingV2(
        Decimal("3.50"), Decimal("10.50"), Decimal("0.875"), Decimal("4.375"),
    ),
    "gemini": ModelPricingV2(
        Decimal("0.10"), Decimal("0.40"), Decimal("0.025"), Decimal("0.125"),
    ),
}

_PROVIDER_FALLBACK_V2: Dict[str, ModelPricingV2] = {
    "anthropic": ModelPricingV2(
        Decimal("3"), Decimal("15"), Decimal("0.30"), Decimal("3.75"),
    ),
    "openai_compatible": ModelPricingV2(
        Decimal("0.50"), Decimal("1.50"), Decimal("0.25"), Decimal("0.625"),
    ),
    "gemini": ModelPricingV2(
        Decimal("0.10"), Decimal("0.40"), Decimal("0.025"), Decimal("0.125"),
    ),
}

_DEFAULT_PRICING_V2 = ModelPricingV2(
    Decimal("1"), Decimal("3"), Decimal("0.10"), Decimal("0.25"),
)


def lookup_model_pricing_v2(provider: str, model: str) -> ModelPricingV2:
    model_lower = (model or "").lower().strip()
    best_prefix = ""
    best: Optional[ModelPricingV2] = None
    for prefix, pricing in _MODEL_PRICING_V2.items():
        if model_lower.startswith(prefix.lower()) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best = pricing
    if best is not None:
        return best

    provider_lower = (provider or "").lower().strip()
    if provider_lower in _PROVIDER_FALLBACK_V2:
        return _PROVIDER_FALLBACK_V2[provider_lower]
    return _DEFAULT_PRICING_V2


def compute_usage_cost_usd(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Dict[str, Decimal]:
    """Return precise Decimal costs for ledger storage."""
    pricing = lookup_model_pricing_v2(provider, model)
    input_cost = Decimal(input_tokens) * pricing.input_per_1m / _MILLION
    output_cost = Decimal(output_tokens) * pricing.output_per_1m / _MILLION
    cache_read_cost = Decimal(cache_read_tokens) * pricing.cache_read_per_1m / _MILLION
    cache_write_cost = Decimal(cache_write_tokens) * pricing.cache_write_per_1m / _MILLION
    cache_cost = cache_read_cost + cache_write_cost
    total = input_cost + output_cost + cache_cost
    return {
        "input_cost_usd": input_cost,
        "output_cost_usd": output_cost,
        "cache_cost_usd": cache_cost,
        "total_cost_usd": total,
        "pricing_version": PRICING_VERSION,
    }


def pricing_tier_for_model(model: str) -> str:
    """Human-readable tier label for tests and admin grouping."""
    model_lower = (model or "").lower()
    if "opus" in model_lower:
        return "opus"
    if "sonnet" in model_lower:
        return "sonnet"
    if "haiku" in model_lower:
        return "haiku"
    if model_lower.startswith("gpt-5.6-luna") or model_lower.endswith("-luna"):
        return "luna"
    if model_lower.startswith("gpt-5.6-terra") or model_lower.endswith("-terra"):
        return "terra"
    if model_lower.startswith("gpt-5.6-sol") or model_lower.endswith("-sol"):
        return "sol"
    if model_lower.startswith("gpt"):
        return "openai_compatible"
    if model_lower.startswith("gemini"):
        return "gemini"
    return "unknown"
