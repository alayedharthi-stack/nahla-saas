"""
core/wa_order_extraction_model.py
─────────────────────────────────
Feature-flag routing for order extraction / cart-delta LLM calls.

Default remains the existing cheap path; set ``NAHLA_ORDER_EXTRACTION_MODEL``
to override. When confidence is low, ``NAHLA_ORDER_EXTRACTION_MODEL_FALLBACK``
(``gpt-4.1-mini`` by default) may be selected.
"""
from __future__ import annotations

import os
from typing import Optional

_DEFAULT_FALLBACK = "gpt-4.1-mini"


def resolve_order_extraction_model(
    *,
    confidence: float = 1.0,
    low_confidence_threshold: float = 0.65,
) -> Optional[str]:
    explicit = (os.environ.get("NAHLA_ORDER_EXTRACTION_MODEL") or "").strip()
    if explicit:
        return explicit
    fallback = (
        os.environ.get("NAHLA_ORDER_EXTRACTION_MODEL_FALLBACK") or _DEFAULT_FALLBACK
    ).strip()
    if confidence < low_confidence_threshold:
        return fallback or None
    return None


__all__ = ["resolve_order_extraction_model"]
