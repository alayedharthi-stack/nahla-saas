"""
modules/ai/gender/neutral.py
────────────────────────────
Backward-compatible re-exports for address marker detection.
"""
from __future__ import annotations

from .address_guard import (
    contains_feminine_address_markers as contains_feminine_markers,
    contains_masculine_address_markers as contains_masculine_imperative_markers,
)

__all__ = [
    "contains_feminine_markers",
    "contains_masculine_imperative_markers",
]
