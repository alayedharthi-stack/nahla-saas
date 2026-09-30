"""A hard spend cap for one evaluation run (eval harness only).

The evaluation key shares its organization's monthly spend limit with
production: when an evaluation exhausted that limit, production WhatsApp turns
failed too. Until the key sits in a workspace with its own spend limit, every
run carries its own ceiling. Prices are Anthropic's list prices per million
tokens; a model not listed here is refused, because its spend cannot be bounded.
Overshoot is at most the one call or turn in flight when the cap is reached.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

BUDGET_ENV = "EVAL_BUDGET_USD"

# (input, output) USD per million tokens, list prices.
PRICES: Dict[str, Tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (2.0, 10.0),
    # EVAL_PROVIDER=scripted makes no API call at all.
    "scripted": (0.0, 0.0),
}


def price_for(model: str) -> Optional[Tuple[float, float]]:
    for prefix, price in PRICES.items():
        if str(model or "").startswith(prefix):
            return price
    return None


class Budget:
    def __init__(self, limit_usd: float) -> None:
        self.limit_usd = float(limit_usd)
        self.spent_usd = 0.0

    @classmethod
    def from_env(cls) -> "Budget":
        raw = os.environ.get(BUDGET_ENV, "").strip()
        value = float(raw)  # raises on a missing or malformed cap: no cap, no run
        if value <= 0:
            raise ValueError(f"{BUDGET_ENV} must be positive")
        return cls(value)

    def add(self, model: str, input_tokens: Optional[int], output_tokens: Optional[int]) -> float:
        price = price_for(model)
        if price is None:
            raise ValueError(f"no list price for model {model!r}; refusing to spend unbounded")
        cost = (int(input_tokens or 0) * price[0] + int(output_tokens or 0) * price[1]) / 1e6
        self.spent_usd += cost
        return cost

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd
