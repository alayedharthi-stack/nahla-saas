"""
modules/ai/gender
─────────────────
Light, isolated gender-awareness layer for Gulf-Arabic social
replies. Two pure modules:

* :mod:`detector`   — infer customer gender from current message
                       (verb suffixes), customer name, or sticky prior
                       hint. Returns a structured :class:`GenderHint`
                       with an explicit confidence score; never
                       guesses aggressively.

* :mod:`conjugator` — closed-set token swap that turns a male-default
                       social reply into a female-coded variant, but
                       only when the hint passes the confidence gate.
                       Otherwise returns the input unchanged.

Design constraints (per the May 2026 spec):

* SURGICAL — touches only the ``ACTION_SOCIAL_REPLY`` branch of the
  composer. Sales, KB, catalog, scope_tiers, OUT_OF_SCOPE, and
  personality templates are NOT modified.
* CONSERVATIVE — when confidence is low we return Arabic's natural
  masculine-default. No swap, no emoji, no extra warmth.
* BACKWARD-SAFE — both modules are pure functions; importing them
  has no side effects, and the conjugator is a no-op for any
  non-female / low-confidence input.
"""
from __future__ import annotations

from .detector import GenderHint, detect_gender
from .conjugator import apply_gender_to_social_reply

__all__ = [
    "GenderHint",
    "apply_gender_to_social_reply",
    "detect_gender",
]
