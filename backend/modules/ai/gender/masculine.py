"""
modules/ai/gender/masculine.py
──────────────────────────────
Backward-compatible re-export — use address_guard for outbound fixes.
"""
from __future__ import annotations

from .address_guard import apply_address_gender_guard
from .context import REPLY_STYLE_MASCULINE


def apply_masculine_gender_wording(reply: str) -> str:
    return apply_address_gender_guard(reply, REPLY_STYLE_MASCULINE).text


__all__ = ["apply_masculine_gender_wording"]
