"""
backend/modules/ai/orchestrator/costing.py
───────────────────────────────────────────
Lightweight estimated cost metadata for AI provider calls.

Public surface:
  estimate_call_cost(provider, model, prompt_chars, reply_chars)
    → Dict[str, Any]

Design:
  - Static pricing table only. No external pricing API calls.
  - Token count is estimated at 4 chars/token (OpenAI rule of thumb).
    Arabic text is typically 2–3 chars/token; 4 is a conservative over-estimate
    that keeps costs from being under-reported.
  - All values are estimates — is_estimated is always True.
  - No DB writes. No billing logic. Log-based and metadata-only.
  - Pricing is expressed in USD per 1M tokens (input / output separately).

Cost buckets:
  low    — est_cost_usd < $0.001 per call
  medium — est_cost_usd $0.001–$0.01 per call
  high   — est_cost_usd > $0.01 per call

Pricing table notes:
  Values are approximate and based on publicly available pricing at the time
  of writing.  Update _MODEL_PRICING or _PROVIDER_FALLBACK_PRICING as needed.
  The table uses prefix matching on model names for flexibility.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

# ── Token estimation constant ─────────────────────────────────────────────────
_CHARS_PER_TOKEN: int = int(os.environ.get("AI_CHARS_PER_TOKEN", "4"))

# ── Pricing table: model name prefix → (input$/1M, output$/1M) ───────────────
# Keys are matched by prefix (longest match wins) for model name flexibility.
# Override via _PROVIDER_FALLBACK_PRICING if no model prefix matches.

_MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    # Anthropic Claude
    "claude-opus-4":         (15.00,  75.00),
    "claude-opus-3":         (15.00,  75.00),
    "claude-opus":           (15.00,  75.00),
    "claude-3-5-sonnet":     ( 3.00,  15.00),
    "claude-3-5-haiku":      ( 0.80,   4.00),
    "claude-3-haiku":        ( 0.25,   1.25),
    "claude-3-sonnet":       ( 3.00,  15.00),
    "claude-3-opus":         (15.00,  75.00),
    "claude":                ( 3.00,  15.00),   # anthropic generic fallback
    # OpenAI / compatible
    "gpt-4o-mini":           ( 0.15,   0.60),
    "gpt-4o":                ( 2.50,  10.00),
    "gpt-4-turbo":           (10.00,  30.00),
    "gpt-4":                 (30.00,  60.00),
    "gpt-3.5-turbo":         ( 0.50,   1.50),
    # Google Gemini
    "gemini-2.0-flash-lite": ( 0.075,  0.30),
    "gemini-2.0-flash":      ( 0.10,   0.40),
    "gemini-2.0-pro":        ( 3.50,  10.50),
    "gemini-1.5-flash-8b":   ( 0.0375, 0.15),
    "gemini-1.5-flash":      ( 0.075,  0.30),
    "gemini-1.5-pro":        ( 3.50,  10.50),
    "gemini-1.0-pro":        ( 0.50,   1.50),
    "gemini":                ( 0.10,   0.40),   # gemini generic fallback
}

# Provider-level fallback when no model prefix matches
_PROVIDER_FALLBACK_PRICING: Dict[str, Tuple[float, float]] = {
    "anthropic":         ( 3.00,  15.00),
    "openai_compatible": ( 0.50,   1.50),
    "gemini":            ( 0.10,   0.40),
}

# Final fallback when provider is also unknown
_DEFAULT_PRICING: Tuple[float, float] = (1.00, 3.00)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lookup_pricing(provider: str, model: str) -> Tuple[float, float]:
    """
    Find per-1M-token pricing for (provider, model).

    Lookup order:
    1. Match model name by longest prefix in _MODEL_PRICING.
    2. Fall back to provider entry in _PROVIDER_FALLBACK_PRICING.
    3. Fall back to _DEFAULT_PRICING.
    """
    model_lower = (model or "").lower().strip()

    # Try longest-prefix match first
    best_prefix = ""
    best_pricing: Optional[Tuple[float, float]] = None
    for prefix, pricing in _MODEL_PRICING.items():
        if model_lower.startswith(prefix.lower()) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_pricing = pricing

    if best_pricing is not None:
        return best_pricing

    # Provider-level fallback
    provider_lower = (provider or "").lower().strip()
    if provider_lower in _PROVIDER_FALLBACK_PRICING:
        return _PROVIDER_FALLBACK_PRICING[provider_lower]

    return _DEFAULT_PRICING


def _cost_bucket(est_cost_usd: float) -> str:
    if est_cost_usd < 0.001:
        return "low"
    if est_cost_usd < 0.01:
        return "medium"
    return "high"


# ── Public API ────────────────────────────────────────────────────────────────

def estimate_call_cost(
    *,
    provider: str,
    model: str,
    prompt_chars: int,
    reply_chars: int,
) -> Dict[str, Any]:
    """
    Compute lightweight estimated cost metadata for one AI provider call.

    Parameters
    ----------
    provider     : canonical provider name (anthropic | openai_compatible | gemini)
    model        : model identifier string as returned by the provider
    prompt_chars : character count of the built system prompt
    reply_chars  : character count of the reply text

    Returns
    -------
    Dict with the following keys (all values are estimates):
      provider          : str   — provider name
      model             : str   — model name
      prompt_chars      : int   — raw character count of the prompt
      reply_chars       : int   — raw character count of the reply
      est_input_tokens  : int   — prompt_chars // chars_per_token
      est_output_tokens : int   — reply_chars // chars_per_token
      est_total_tokens  : int   — sum of input + output
      est_cost_usd      : float — estimated USD cost (input + output combined)
      cost_bucket       : str   — "low" | "medium" | "high"
      is_estimated      : bool  — always True
    """
    cpt = max(1, _CHARS_PER_TOKEN)
    est_input  = prompt_chars  // cpt
    est_output = reply_chars   // cpt
    est_total  = est_input + est_output

    input_per_1m, output_per_1m = _lookup_pricing(provider, model)
    est_cost = (est_input * input_per_1m + est_output * output_per_1m) / 1_000_000

    return {
        "provider":          provider,
        "model":             model,
        "prompt_chars":      prompt_chars,
        "reply_chars":       reply_chars,
        "est_input_tokens":  est_input,
        "est_output_tokens": est_output,
        "est_total_tokens":  est_total,
        "est_cost_usd":      round(est_cost, 8),
        "cost_bucket":       _cost_bucket(est_cost),
        "is_estimated":      True,
    }
