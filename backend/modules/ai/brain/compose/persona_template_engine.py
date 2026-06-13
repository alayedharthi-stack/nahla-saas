"""
brain/compose/persona_template_engine.py
────────────────────────────────────────
Persona-safe local template engine for routine greeting / social turns.

PR2B no-LLM path: warm Saudi WhatsApp tone with light emoji and rotated
variants — not cold deterministic one-liners.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, List, Optional, Sequence

from ..types import BrainContext

# Saudi-market light emoji — at most one per reply in our pools.
PERSONA_ALLOWED_EMOJI: frozenset[str] = frozenset({"😊", "🌷", "🤍"})

_EMOJI_RE = re.compile(
    "[" + "".join(PERSONA_ALLOWED_EMOJI) + "]",
)

# ── Warm greeting pools ─────────────────────────────────────────────────────

PERSONA_GREETING_COLD: tuple[str, ...] = (
    "ياهلا ومرحبا! كيف الحال؟ 😊",
    "يا هلا وسهلًا، حياك الله 🌷",
    "أهلًا وسهلًا، نورتنا 😊",
    "حياك الله، تفضل 🌷",
    "ياهلا فيك، أبشر",
    "هلا وغلا، حياك 😊",
    "نورتنا، ياهلا 🌷",
)

PERSONA_GREETING_REGREET: tuple[str, ...] = (
    "ياهلا ومرحبا! 😊",
    "يا هلا وسهلًا، حياك الله 🌷",
    "حياك الله 🌷",
    "أهلًا فيك 😊",
    "ياهلا، أبشر",
    "هلا وغلا 🤍",
)

# Mid-order / checkout — warm phatic + resume order context (no generic cold greet).
PERSONA_GREETING_ORDER_AWARE: tuple[str, ...] = (
    "يا هلا، نكمل طلبك؟ 😊",
    "حياك الله، نتابع طلبك 🌷",
    "أهلًا فيك، نكمل بيانات الطلب؟",
    "ياهلا، نكمل وإياك 🌷",
    "هلا، نتابع طلبك؟ 🤍",
)

PERSONA_GREETING_CHECKOUT_AWARE: tuple[str, ...] = (
    "يا هلا، نكمل الدفع؟ 😊",
    "حياك الله، نتابع طلبك 🌷",
    "أهلًا، جاهزين نكمل طلبك؟ 🤍",
    "ياهلا، نكمل خطوة الدفع؟ 😊",
)

# ── Warm social / thanks pools ───────────────────────────────────────────────

PERSONA_SOCIAL_WARM_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "thanks": (
        "الله يعافيك 🌷",
        "العفو، بالخدمة 😊",
        "وياك يارب 🤍",
        "تسلم، حاضرين",
        "الله يجزاك خير 🤍",
        "وإياك، تشرفنا 😊",
    ),
    "blessing": (
        "الله يعافيك 🌷",
        "وإياك يارب 🤍",
        "آمين، الله يسعدك 😊",
        "الله يكرمك 🌷",
        "الله يسعدك 🤍",
    ),
    "general_courtesy": (
        "هلا وغلا 😊",
        "حياك الله 🌷",
        "أهلًا فيك 🤍",
        "نورتنا 😊",
        "يامرحبا 🌷",
    ),
    "compliment": (
        "تسلم 🤍",
        "الله يسعدك 😊",
        "الله يبارك فيك 🌷",
    ),
    "strong_praise": (
        "تسلم، الله يعافيك 🌷",
        "الله يبارك فيك 😊",
        "ما قصّرت 🤍",
    ),
    "morning_greeting": (
        "صباح الخير 😊",
        "صباح النور 🌷",
        "صباحك سعيد 🤍",
    ),
    "celebration": (
        "الله يبارك فيك 😊",
        "مبارك 🌷",
        "الف مبروك 🤍",
    ),
    "informational_only": (
        "تمام 😊",
        "حاضر 🌷",
        "أبشر 🤍",
    ),
    "social_forward": (
        "تسلم 🤍",
        "حاضر 😊",
    ),
    "emotional_personal": (
        "الله يعافيك 🌷",
        "تسلم 🤍",
        "حياك 😊",
    ),
}


def _recent_outbound_bodies(ctx: BrainContext, *, limit: int = 5) -> List[str]:
    bodies: List[str] = []
    for turn in reversed(ctx.history or []):
        if turn.get("direction") not in ("out", "outbound"):
            continue
        body = str(turn.get("body") or "").strip()
        if body:
            bodies.append(body)
        if len(bodies) >= limit:
            break
    return bodies


def _norm_phrase(text: str) -> str:
    """Normalize for repeat detection — strip emoji and punctuation noise."""
    t = _EMOJI_RE.sub("", text or "")
    t = re.sub(r"[^\w\u0600-\u06FF\s]", " ", t)
    return " ".join(t.split()).strip().lower()


def _emojis_in(text: str) -> frozenset[str]:
    return frozenset(ch for ch in (text or "") if ch in PERSONA_ALLOWED_EMOJI)


def _rotation_seed(ctx: BrainContext) -> int:
    phone = str(getattr(ctx, "customer_phone", "") or "")
    tenant = str(getattr(ctx, "tenant_id", "") or "")
    turn = getattr(getattr(ctx, "state", None), "turn", None)
    hist_len = len(ctx.history or [])
    if turn is None:
        turn = hist_len
    raw = f"{tenant}|{phone}|{turn}|{hist_len}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest(), 16)


def _ordered_indices(n: int, seed: int) -> List[int]:
    start = seed % n if n else 0
    return [(start + i) % n for i in range(n)]


def pick_persona_variant(
    pool: Sequence[str],
    ctx: BrainContext,
    *,
    lookback: int = 5,
) -> str:
    """Pick a warm variant; avoid immediate repeats of phrase or emoji."""
    if not pool:
        return ""
    if len(pool) == 1:
        return pool[0]

    recent = _recent_outbound_bodies(ctx, limit=lookback)
    recent_norm = {_norm_phrase(t) for t in recent}
    recent_emojis: set[str] = set()
    for t in recent:
        recent_emojis.update(_emojis_in(t))

    seed = _rotation_seed(ctx)
    for idx in _ordered_indices(len(pool), seed):
        candidate = pool[idx]
        norm = _norm_phrase(candidate)
        if norm and norm in recent_norm:
            continue
        em = _emojis_in(candidate)
        if em and em & recent_emojis:
            continue
        if candidate.strip() in recent:
            continue
        return candidate

    return pool[seed % len(pool)]


def _active_commerce_greeting_stage(ctx: BrainContext) -> Optional[str]:
    """Return ``ordering`` | ``checkout`` when greet must stay order-aware."""
    from ..state.stages import STAGE_CHECKOUT, STAGE_DECIDING, STAGE_ORDERING  # noqa: PLC0415

    stage = str(getattr(getattr(ctx, "state", None), "stage", "") or "")
    if stage == STAGE_CHECKOUT:
        return "checkout"
    if stage in {STAGE_ORDERING, STAGE_DECIDING}:
        return "ordering"
    return None


def pick_persona_greeting(ctx: BrainContext, *, re_greet: bool = False) -> str:
    commerce_ctx = _active_commerce_greeting_stage(ctx)
    if commerce_ctx == "checkout":
        return pick_persona_variant(PERSONA_GREETING_CHECKOUT_AWARE, ctx)
    if commerce_ctx == "ordering":
        return pick_persona_variant(PERSONA_GREETING_ORDER_AWARE, ctx)
    pool = PERSONA_GREETING_REGREET if re_greet else PERSONA_GREETING_COLD
    return pick_persona_variant(pool, ctx)


def pick_persona_social_reply(
    ctx: BrainContext,
    category: str,
    *,
    inbound_text: str = "",
) -> str:
    """Warm social ack for routine no-LLM turns."""
    cat = (category or "general_courtesy").strip().lower() or "general_courtesy"

    warm = PERSONA_SOCIAL_WARM_BY_CATEGORY.get(cat)
    if warm:
        return pick_persona_variant(warm, ctx)

    # Occasion / safety categories — reuse audited template pools.
    from .templates import _SOCIAL_REPLIES_BY_CATEGORY  # noqa: PLC0415
    from .templates import _OCCASION_GATED_SOCIAL_CATEGORIES  # noqa: PLC0415

    if cat in _OCCASION_GATED_SOCIAL_CATEGORIES:
        from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: PLC0415
            inbound_has_occasion_signal,
        )
        if not inbound_has_occasion_signal(inbound_text):
            return ""

    bucket = _SOCIAL_REPLIES_BY_CATEGORY.get(cat) or _SOCIAL_REPLIES_BY_CATEGORY.get(
        "general_courtesy",
        (),
    )
    if not bucket:
        return ""
    return pick_persona_variant(bucket, ctx)


def persona_reply_has_light_emoji(text: str) -> bool:
    """True when reply uses at most one allowed persona emoji."""
    found = [ch for ch in (text or "") if ch in PERSONA_ALLOWED_EMOJI]
    return len(found) <= 1


def persona_reply_is_warm_greeting(text: str) -> bool:
    """Heuristic: phatic warmth markers without help-desk closers."""
    if not (text or "").strip():
        return False
    warm_markers = (
        "هلا", "اهلا", "أهل", "حياك", "مرحب", "نورت", "أبشر", "غلا",
    )
    lowered = text.lower()
    if not any(m in lowered for m in warm_markers):
        return False
    banned = ("كيف أقدر أخدمك", "كيف أقدر أساعدك", "بماذا أخدمك")
    return not any(b in text for b in banned)


def persona_reply_is_order_aware_greeting(text: str) -> bool:
    """True when reply acknowledges an in-progress order/checkout flow."""
    if not (text or "").strip():
        return False
    order_markers = ("طلب", "نكمل", "نتابع", "بيانات", "دفع")
    return any(m in text for m in order_markers)
