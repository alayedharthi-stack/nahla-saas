"""
modules/ai/gender/address_guard.py
──────────────────────────────────
Minimal outbound address-word fixes — grammar guard only.

Rules:
* Swap only clearly gender-marked address tokens.
* Never replace a full reply or inject canned response shapes.
* Unknown gender → neutral address words only.
* Known gender → fix mismatches only; leave correct replies untouched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Pattern, Tuple

from .context import (
    REPLY_STYLE_FEMININE,
    REPLY_STYLE_MASCULINE,
    REPLY_STYLE_NEUTRAL,
)

_TRAIL = r"(?=\s|$|[.,،!?؟…])"
_NO_FEM_YA = r"(?!ي)"
_NO_KASRA = r"(?!ِ)"

# Feminine → masculine (minimal address/imperative mismatches only).
_FEMININE_TO_MASCULINE: List[Tuple[str, str]] = [
    (rf"تفضلين{_TRAIL}", "تفضل"),
    (rf"تفضلي{_TRAIL}", "تفضل"),
    (rf"أرسلي{_TRAIL}", "أرسل"),
    (rf"ارسلي{_TRAIL}", "أرسل"),
    (rf"اختاري{_TRAIL}", "اختر"),
    (rf"أبشري{_TRAIL}", "أبشر"),
    (rf"تسلمين{_TRAIL}", "تسلم"),
    (rf"عندكِ{_TRAIL}", "عندك"),
    (rf"لكِ{_TRAIL}", "لك"),
    (rf"عليكِ{_TRAIL}", "عليك"),
    (rf"فيكِ{_TRAIL}", "فيك"),
]

# Masculine → feminine (minimal; only forms with an unambiguous female pair).
_MASCULINE_TO_FEMININE: List[Tuple[str, str]] = [
    (rf"تفضل{_NO_FEM_YA}{_TRAIL}", "تفضلي"),
    (rf"أرسل{_NO_FEM_YA}{_TRAIL}", "أرسلي"),
    (rf"ارسل{_NO_FEM_YA}{_TRAIL}", "ارسلي"),
    (rf"اختر{_NO_FEM_YA}{_TRAIL}", "اختاري"),
    (rf"أبشر{_NO_FEM_YA}{_TRAIL}", "أبشري"),
    (rf"تسلم{_NO_FEM_YA}{_TRAIL}", "تسلمين"),
]

# Gender-marked address words → neutral polite address (word-level only).
_GENDERED_TO_NEUTRAL: List[Tuple[str, str]] = [
    (rf"تفضلين{_TRAIL}", "لو سمحت"),
    (rf"تفضلي{_TRAIL}", "لو سمحت"),
    (rf"تفضل{_TRAIL}", "لو سمحت"),
    (rf"أرسلي{_TRAIL}", "لو سمحت"),
    (rf"ارسلي{_TRAIL}", "لو سمحت"),
    (rf"اختاري{_TRAIL}", "لو سمحت"),
    (rf"أبشري{_TRAIL}", "لو سمحت"),
    (rf"تسلمين{_TRAIL}", "تسلم"),
    (rf"عندكِ{_TRAIL}", "لديك"),
    (rf"لكِ{_TRAIL}", "لك"),
    (rf"عليكِ{_TRAIL}", "عليك"),
    (rf"فيكِ{_TRAIL}", "فيك"),
]

_FEMININE_MARKERS_RE = re.compile(
    r"(?:"
    r"تفضلين|تفضلي|أرسلي|ارسلي|اختاري|"
    r"عندكِ|تسلمين|أبشري|"
    r"كِ\b|لكِ\b|عليكِ\b|فيكِ\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_MASCULINE_ADDRESS_RE = re.compile(
    r"(?<!\w)(?:تفضل|أرسل|ارسل|اختر|أبشر|تسلم)(?!\w)",
    re.UNICODE | re.IGNORECASE,
)

_MAX_SWAPS_BEFORE_NEUTRAL_FALLBACK = 4


def _compile(pairs: List[Tuple[str, str]]) -> List[Tuple[Pattern[str], str]]:
    return [(re.compile(src, re.UNICODE | re.IGNORECASE), dst) for src, dst in pairs]


_COMPILED_F2M = _compile(_FEMININE_TO_MASCULINE)
_COMPILED_M2F = _compile(_MASCULINE_TO_FEMININE)
_COMPILED_NEUTRAL = _compile(_GENDERED_TO_NEUTRAL)


@dataclass(frozen=True)
class AddressGuardResult:
    text: str
    changed: bool
    swaps: int
    mode: str


def contains_feminine_address_markers(text: str) -> bool:
    return bool(text and _FEMININE_MARKERS_RE.search(text))


def contains_masculine_address_markers(text: str) -> bool:
    return bool(text and _MASCULINE_ADDRESS_RE.search(text))


def _apply_swaps(text: str, table: List[Tuple[Pattern[str], str]]) -> tuple[str, int]:
    out = text
    swaps = 0
    for pattern, replacement in table:
        new_out, count = pattern.subn(replacement, out)
        if count:
            swaps += count
            out = new_out
    return out, swaps


def apply_address_gender_guard(text: str, reply_style: str) -> AddressGuardResult:
    """Minimally rewrite gendered address words in *text*."""
    if not text:
        return AddressGuardResult(text=text or "", changed=False, swaps=0, mode=reply_style)

    original = text
    mode = reply_style

    if reply_style == REPLY_STYLE_MASCULINE:
        if not contains_feminine_address_markers(text):
            return AddressGuardResult(text=text, changed=False, swaps=0, mode=mode)
        out, swaps = _apply_swaps(text, _COMPILED_F2M)
        if swaps > _MAX_SWAPS_BEFORE_NEUTRAL_FALLBACK:
            out, swaps = _apply_swaps(text, _COMPILED_NEUTRAL)
            mode = REPLY_STYLE_NEUTRAL
        return AddressGuardResult(
            text=out,
            changed=out != original,
            swaps=swaps,
            mode=mode,
        )

    if reply_style == REPLY_STYLE_FEMININE:
        if not contains_masculine_address_markers(text):
            return AddressGuardResult(text=text, changed=False, swaps=0, mode=mode)
        out, swaps = _apply_swaps(text, _COMPILED_M2F)
        if swaps > _MAX_SWAPS_BEFORE_NEUTRAL_FALLBACK:
            out, swaps = _apply_swaps(text, _COMPILED_NEUTRAL)
            mode = REPLY_STYLE_NEUTRAL
        return AddressGuardResult(
            text=out,
            changed=out != original,
            swaps=swaps,
            mode=mode,
        )

    # Unknown / low confidence — neutralize only gender-marked address words.
    if not (
        contains_feminine_address_markers(text)
        or contains_masculine_address_markers(text)
    ):
        return AddressGuardResult(text=text, changed=False, swaps=0, mode=REPLY_STYLE_NEUTRAL)

    out, swaps = _apply_swaps(text, _COMPILED_NEUTRAL)
    return AddressGuardResult(
        text=out,
        changed=out != original,
        swaps=swaps,
        mode=REPLY_STYLE_NEUTRAL,
    )


__all__ = [
    "AddressGuardResult",
    "apply_address_gender_guard",
    "contains_feminine_address_markers",
    "contains_masculine_address_markers",
]
