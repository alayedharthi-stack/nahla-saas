"""
social_checkout_pressure_guard.py
─────────────────────────────────
Phase A.1 — strip checkout slot pressure from replies to pure phatic turns.

When OrderFlowV2 correctly bypasses social/phatic inbound, Brain replies must not
append address / payment / aggressive checkout resume prompts.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("nahla.brain.postprocess.social_checkout_pressure_guard")

_CHECKOUT_PRESSURE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"أرسل\s*عنوان", re.I | re.UNICODE),
    re.compile(r"شاركنا\s*عنوان", re.I | re.UNICODE),
    re.compile(r"وش\s*طريقة\s*الدفع", re.I | re.UNICODE),
    re.compile(r"أعتمد\s*التوصيل", re.I | re.UNICODE),
    re.compile(r"نكمل\s*طلبك\s*السابق", re.I | re.UNICODE),
    re.compile(r"نكمل\s*طلبك", re.I | re.UNICODE),
    re.compile(r"نتابع\s*طلبك", re.I | re.UNICODE),
    re.compile(r"نكمل\s*الدفع", re.I | re.UNICODE),
    re.compile(r"نكمل\s*بيانات\s*الطلب", re.I | re.UNICODE),
    re.compile(r"جاهزين\s*نكمل\s*طلبك", re.I | re.UNICODE),
)

_GENTLE_OPEN_ORDER_HINT = re.compile(
    r"و?عندك\s+طلب\s+سابق",
    re.I | re.UNICODE,
)


@dataclass(frozen=True)
class SocialCheckoutPressureGuardResult:
    reply: str
    stripped: bool
    reason: str = ""
    empty_fallback: bool = False


def _phatic_no_silence_fallback(inbound: str) -> str:
    """Existing emergency paths only — never leave phatic turns silent after strip."""
    raw = str(inbound or "").strip()
    if not raw:
        return ""
    try:
        from modules.ai.brain.compose.templates import (  # noqa: PLC0415
            social_mirror_fallback_reply,
        )

        mirrored = (social_mirror_fallback_reply(raw) or "").strip()
        if mirrored:
            return mirrored
    except Exception:  # noqa: BLE001  # noqa: silent-ok — mirror fallback optional
        pass
    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            resolve_social_thanks_guard_reply,
        )

        thanks = resolve_social_thanks_guard_reply(raw)
        if thanks and thanks.strip():
            return thanks.strip()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — thanks mirror optional
        pass
    try:
        from modules.ai.brain.intent.social_classifier import (  # noqa: PLC0415
            SOCIAL_BLESSING,
            SOCIAL_THANKS,
            classify_social,
        )
        from modules.ai.brain.compose.persona_template_engine import (  # noqa: PLC0415
            PERSONA_SOCIAL_DUA_FALLBACK,
            PERSONA_SOCIAL_WARM_BY_CATEGORY,
            enforce_persona_dua_reply_guard,
        )

        match = classify_social(raw)
        if match is not None:
            cat = str(getattr(match, "category", "") or "")
            if cat == SOCIAL_THANKS:
                pool = PERSONA_SOCIAL_WARM_BY_CATEGORY.get("thanks") or ()
                if pool:
                    return str(pool[0]).strip()
            if cat == SOCIAL_BLESSING:
                return enforce_persona_dua_reply_guard(
                    PERSONA_SOCIAL_DUA_FALLBACK,
                    inbound_text=raw,
                )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — classifier emergency optional
        pass
    norm = _norm_ar(raw)
    if norm.startswith("السلام") or "سلام عليكم" in norm:
        return "وعليكم السلام ورحمة الله وبركاته"
    if is_pure_phatic_bypass_turn(raw):
        return "الله يعافيك 🌷"
    return ""


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t)


def is_checkout_pressure_line(line: str) -> bool:
    """True when a line/sentence pressures checkout slots after a phatic turn."""
    raw = str(line or "").strip()
    if not raw:
        return False
    if _GENTLE_OPEN_ORDER_HINT.search(raw):
        return False
    return any(p.search(raw) for p in _CHECKOUT_PRESSURE_LINE_PATTERNS)


def strip_checkout_pressure_segments(text: str) -> tuple[str, bool]:
    """Remove checkout-pressure lines while preserving social acknowledgement."""
    raw = str(text or "").strip()
    if not raw:
        return "", False

    stripped_any = False
    kept_paragraphs: list[str] = []

    for paragraph in re.split(r"\n\s*\n", raw):
        p = paragraph.strip()
        if not p:
            continue
        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        kept_lines: list[str] = []
        for ln in lines:
            if is_checkout_pressure_line(ln):
                stripped_any = True
                continue
            kept_lines.append(ln)
        if len(kept_lines) < len(lines):
            stripped_any = True
        if kept_lines:
            kept_paragraphs.append("\n".join(kept_lines))

    return "\n\n".join(kept_paragraphs).strip(), stripped_any


def is_pure_phatic_bypass_turn(inbound_text: str) -> bool:
    """Shared probe — same bypass keys used by OrderFlowV2 stale-checkout suppression."""
    try:
        from modules.ai.order_flow_v2.explicit_intent_checkout_suppression import (  # noqa: PLC0415
            detect_social_phatic_intent,
        )

        return bool(detect_social_phatic_intent(str(inbound_text or "").strip()))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import boundary
        return False


def apply_social_checkout_pressure_guard(
    reply: str,
    *,
    inbound_text: str = "",
    tenant_id: Optional[int] = None,
) -> SocialCheckoutPressureGuardResult:
    text = str(reply or "").strip()
    inbound = str(inbound_text or "").strip()
    if not text or not inbound:
        return SocialCheckoutPressureGuardResult(reply=text, stripped=False)

    if not is_pure_phatic_bypass_turn(inbound):
        return SocialCheckoutPressureGuardResult(reply=text, stripped=False)

    cleaned, stripped = strip_checkout_pressure_segments(text)
    empty_fallback = False
    if stripped and not cleaned.strip():
        cleaned = _phatic_no_silence_fallback(inbound)
        empty_fallback = bool(cleaned.strip())
        if empty_fallback:
            logger.info(
                "[SOCIAL_CHECKOUT_PRESSURE_GUARD] tenant=%s inbound=%r "
                "empty_after_strip — applied no-silence fallback len=%d",
                tenant_id if tenant_id is not None else "-",
                inbound[:80],
                len(cleaned),
            )
    if stripped:
        logger.info(
            "[SOCIAL_CHECKOUT_PRESSURE_GUARD] tenant=%s inbound=%r "
            "orig_len=%d new_len=%d",
            tenant_id if tenant_id is not None else "-",
            inbound[:80],
            len(text),
            len(cleaned),
        )
    reason = ""
    if stripped:
        reason = (
            "phatic_bypass_checkout_pressure_empty_fallback"
            if empty_fallback
            else "phatic_bypass_checkout_pressure"
        )
    return SocialCheckoutPressureGuardResult(
        reply=cleaned,
        stripped=stripped,
        reason=reason,
        empty_fallback=empty_fallback,
    )


__all__ = [
    "SocialCheckoutPressureGuardResult",
    "apply_social_checkout_pressure_guard",
    "is_checkout_pressure_line",
    "is_pure_phatic_bypass_turn",
    "strip_checkout_pressure_segments",
]
