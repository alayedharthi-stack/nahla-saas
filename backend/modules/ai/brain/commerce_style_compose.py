"""
commerce_style_compose.py
─────────────────────────
Style-attribute composition for commerce replies (Nahla doctrine).

Operational facts stay separate; personality varies via seeded style
dimensions — not full-sentence template banks.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple

_OPENING_BY_STYLE: Dict[str, Tuple[str, ...]] = {
    "warm": ("أبشر", "تمام", "حاضر", "يا هلا"),
    "direct": ("", "نعم"),
    "helpful": ("أكيد", "تام", "حاضر"),
}

_FOLLOWUP_STYLE_BY_CATEGORY: Dict[str, Tuple[str, ...]] = {
    "dress": ("size_model", "model", "options"),
    "clothes": ("size_model", "model", "options"),
    "abaya": ("size_model", "model", "options"),
    "shoes": ("size_model", "model", "options"),
    "bags": ("size_model", "model", "options"),
    "mobile": ("model", "options", "quantity"),
    "electronics": ("model", "options", "quantity"),
    "computer": ("model", "options", "quantity"),
    "accessories": ("model", "options", "quantity"),
    "stationery": ("options", "quantity", "model"),
    "books": ("options", "quantity", "model"),
    "honey": ("size", "quantity", "options"),
    "food": ("size", "quantity", "options"),
    "coffee": ("size", "quantity", "options"),
    "dates": ("size", "quantity", "options"),
    "general": ("options", "size", "quantity"),
}

# Compositional slots — pick one token per slot via seed (not full-sentence templates).
_FOLLOWUP_SLOTS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "size": (
        ("وش", "أي"),
        ("الحجم", "الوزن"),
        ("يناسبك؟", "تبيه؟", "تفضّل؟"),
    ),
    "size_model": (
        ("وش", "أي"),
        ("المقاس", "الحجم"),
        ("أو", "و"),
        ("الموديل", "النوع"),
        ("يناسبك؟", "تبيه؟"),
    ),
    "model": (
        ("وش", "أي"),
        ("موديل", "نوع"),
        ("تبحث عنه؟", "تبيه؟", "تفضّل؟"),
    ),
    "options": (
        ("وش", "أي"),
        ("خيار", "نوع"),
        ("يناسبك؟", "تبيه؟"),
    ),
    "quantity": (
        ("كم", "وش"),
        ("الكمية", "العدد"),
        ("تحتاج؟", "تبغى؟"),
    ),
    "options_list": (
        ("تحب", "تبغى"),
        ("أرسل", "أعرض"),
        ("الخيارات", "الأنواع", "المتوفر"),
        ("لك؟", "الحين؟"),
    ),
}


@dataclass(frozen=True)
class StyleBundle:
    opening_style: str
    followup_style: str
    emoji_style: str
    sentence_order: str
    seed: int
    style_signature: str


def _style_seed(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    turn_id: Optional[int],
    intent_name: str,
    category: str,
) -> int:
    raw = "|".join(
        str(part)
        for part in (
            tenant_id if tenant_id is not None else 0,
            conversation_id if conversation_id is not None else 0,
            turn_id if turn_id is not None else 0,
            (intent_name or "").strip().lower(),
            (category or "general").strip().lower(),
        )
    )
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest(), 16)


def resolve_style_bundle(
    *,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    turn_id: Optional[int] = None,
    intent_name: str = "",
    category: str = "general",
) -> StyleBundle:
    seed = _style_seed(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        intent_name=intent_name,
        category=category,
    )
    opening_style = ("warm", "direct", "helpful")[seed % 3]
    followup_styles = _FOLLOWUP_STYLE_BY_CATEGORY.get(category) or _FOLLOWUP_STYLE_BY_CATEGORY["general"]
    followup_style = followup_styles[(seed >> 3) % len(followup_styles)]
    emoji_style = ("paired", "single", "none")[(seed >> 5) % 3]
    sentence_order = ("fact_first", "assist_first")[(seed >> 7) % 2]
    signature = (
        f"{opening_style}|{followup_style}|{emoji_style}|{sentence_order}|"
        f"{hashlib.sha256(str(seed).encode()).hexdigest()[:8]}"
    )
    return StyleBundle(
        opening_style=opening_style,
        followup_style=followup_style,
        emoji_style=emoji_style,
        sentence_order=sentence_order,
        seed=seed,
        style_signature=signature,
    )


def pick_emojis_for_style(
    *,
    category: str,
    style: StyleBundle,
    emoji_pools: Mapping[str, Sequence[str]],
) -> Tuple[str, str]:
    """Return (emoji_str, emoji_bucket)."""
    bucket = category or "general"
    pool = tuple(emoji_pools.get(bucket) or emoji_pools.get("general") or ("✨",))
    if style.emoji_style == "none" or not pool:
        return "", bucket
    if style.emoji_style == "single":
        return pool[style.seed % len(pool)], bucket
    first = pool[style.seed % len(pool)]
    second = pool[((style.seed >> 4) + 1) % len(pool)]
    if first == second and len(pool) > 1:
        second = pool[(style.seed >> 4) % len(pool)]
    return (f"{first}{second}" if first != second else first), bucket


def compose_followup_line(style: StyleBundle) -> str:
    slots = _FOLLOWUP_SLOTS.get(style.followup_style) or _FOLLOWUP_SLOTS["options"]
    parts: list[str] = []
    for index, options in enumerate(slots):
        if not options:
            continue
        shift = (style.seed >> (index * 2)) & 0xFFFF
        parts.append(options[shift % len(options)])
    return " ".join(parts).strip()


def compose_personality_overlay(
    *,
    operational_fact: str,
    style: StyleBundle,
    category: str,
    emoji_pools: Mapping[str, Sequence[str]],
    include_followup: bool = True,
    inbound_text: str = "",
) -> str:
    """Layer personality on operational facts — varies by style seed."""
    from modules.ai.brain.commerce.commerce_followup_policy import (  # noqa: PLC0415
        followup_style_for_request,
    )

    fact = (operational_fact or "").strip()
    if not fact:
        return fact

    effective_style = style
    effective_style = style
    if include_followup and inbound_text.strip():
        followup_kind = followup_style_for_request(
            inbound_text=inbound_text,
            category=category,
            seeded_style=style.followup_style,
        )
        if followup_kind != style.followup_style:
            effective_style = StyleBundle(
                opening_style=style.opening_style,
                followup_style=followup_kind,
                emoji_style=style.emoji_style,
                sentence_order=style.sentence_order,
                seed=style.seed,
                style_signature=f"{style.style_signature}|req:{followup_kind}",
            )

    openers = _OPENING_BY_STYLE.get(effective_style.opening_style) or _OPENING_BY_STYLE["warm"]
    opener = openers[effective_style.seed % len(openers)].strip()
    emojis, _bucket = pick_emojis_for_style(
        category=category,
        style=effective_style,
        emoji_pools=emoji_pools,
    )
    followup = compose_followup_line(effective_style) if include_followup else ""

    if effective_style.sentence_order == "assist_first" and followup:
        lead = followup
        if opener:
            lead = f"{opener}، {followup[0].lower() + followup[1:]}" if followup else opener
        body = f"{lead}\n{fact}" if lead else fact
    else:
        line = fact
        if opener and not line.startswith(opener):
            line = f"{opener}، {line.lstrip('،, ')}"
        if emojis and emojis not in line:
            line = f"{line.rstrip('؟. ')} {emojis}".strip()
        body = line
        if followup:
            body = f"{body}\n{followup}"

    return body.strip()


__all__ = [
    "StyleBundle",
    "compose_followup_line",
    "compose_personality_overlay",
    "pick_emojis_for_style",
    "resolve_style_bundle",
]
