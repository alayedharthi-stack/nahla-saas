"""
brain/commerce/product_visual.py
────────────────────────────────
Product image / catalog-card visual requests — tenant-agnostic.

Detects when the customer wants to *see* a product (photo, card,
catalog entry) rather than browse or ask price. Used by intent rules,
decision engine, dispatch guard, and webhook visual enforcement.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")

FOCUS_FRESH_TURNS = 8
FOCUS_GAP_STALE_TURNS = 16
# Image-only referents decay faster than textual product focus.
VISUAL_FOCUS_FRESH_TURNS = 4

_PRODUCT_MARKER_RE = re.compile(r"\[PRODUCT:([^\]]+)\]", re.IGNORECASE)

# Deictic — refers to "the image" without naming a SKU.
_DEICTIC_VISUAL_RE = re.compile(
    r"(?:"
    r"(?:ال)?صور(?:ه|ة)?\s*(?:وين|فين|وينها|فينها|م[\s]?و|مو\s*موجود)"
    r"|(?:وين|فين)\s*(?:ال)?صور(?:ه|ة)?"
    r"|(?:ارسل|أرسل|ابعث|أبعث|ور(?:ي|)ني|ور(?:ي|)ن(?:ي|a))\s*(?:ال)?صور(?:ه|ة)?"
    r"|(?:اشوف|أشوف)\s*(?:ال)?صور(?:ه|ة)?"
    r"|(?:ور(?:ي|)ني|ور(?:ي|)ن(?:ي|a))\s+شكل(?:ه|ها)"
    r"|صور(?:ه|ة)?\s*(?:المنتج|منتج)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_COMPARE_RE = re.compile(
    r"(?:قارن|مقارنه|مقارنة|فرق|الفرق|ايه\s*احسن|أيه\s*أحسن|ايش\s*احسن|أيش\s*أحسن)",
    re.UNICODE | re.IGNORECASE,
)

# Named visual ask — product token may follow.
_NAMED_VISUAL_RE = re.compile(
    r"(?:"
    r"(?:اب(?:ي|غ(?:ى|a)?)|أ(?:بي|ب(?:غ(?:ى|a)?)?)|ودي|ود(?:ي|ه)|"
    r"ار(?:سل|سل)|أ(?:رسل|رس(?:ل)?)|ابعث|أبعث|ور(?:ي|)ني|ور(?:ي|)ن(?:ي|a)|"
    r"اعرض(?:\s*(?:لي|علي|لي)?)?|"
    r"اشوف|أشوف|اب(?:ي|غ(?:ى|a)?)\s*اشوف|أ(?:بي|ب(?:غ(?:ى|a)?)?)\s*أ?شوف)"
    r"\s*(?:"
    r"(?:ال)?صور(?:ه|ة)?|صور|شكل(?:ه|ها)?|المنتج|منتج|"
    r"(?:صور(?:ه|ة)?\s*(?:ل|لـ|ال)?)"
    r")"
    r"(?:\s*(?:ل|لـ|ال|بتاع|حق|حق\s*)?\s*)?"
    r"(?P<product>[\w\u0600-\u06FF][\w\u0600-\u06FF\s\-]{1,40})?"
    r"|"
    r"صور(?:ه|ة)?\s*(?:ل|لـ|ال)\s*(?P<product2>[\w\u0600-\u06FF][\w\u0600-\u06FF\s\-]{1,40})"
    r"|"
    r"(?P<product3>[\w\u0600-\u06FF][\w\u0600-\u06FF\s\-]{0,20})\s+صور(?:ه|ة)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_GENERIC_VISUAL_RE = re.compile(
    r"(?:"
    r"صور\s+(?:ال)?(?:عسل|منتج|طلح|سدر|سمر)"
    r"|(?:اب(?:ي|غ(?:ى|a)?)|أ(?:بي|ب(?:غ(?:ى|a)?)?))\s*(?:أ?شوف\s*)?(?:صور|صوره|صورة)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Common STT / dialect variants collapsed before detection.
_STT_REPLACEMENTS = (
    (r"\bابغا\b", "ابغى"),
    (r"\bابي\b", "ابي"),
    (r"\bورني\b", "ورني"),
    (r"\bوين\b", "وين"),
    (r"\bفين\b", "فين"),
)

_STOP_PRODUCT_TOKENS = frozenset({
    "صور", "صورة", "الصورة", "الصور", "صوره", "منتج", "المنتج", "شكل", "شكلها",
    "شكله", "لي", "علي", "ل", "لـ", "ال", "و", "في", "من", "على", "فوق",
    "ابي", "ابغى", "ابغا", "ودي", "ارسل", "ابعث", "ورني", "اشوف", "هذا", "هذي",
    "اللي", "لي", "وين", "فين", "سعر", "كم", "بكم", "ثمن", "ريال",
})

# Vision-template tokens — never valid catalog queries (ARCH-MEDIA-001 Wave 0).
_VISION_QUERY_STOPLIST_RAW = (
    "المحتوى",
    "نوع المحتوى",
    "صورة",
    "صورة عامة",
    "عام",
    "المحتوى العام",
    "الوصف",
    "وصف الصورة",
)

_BOT_FRAMING_LINE_PREFIXES = (
    "[وصف الصورة",
    "[وصف الفيديو",
    "[تصنيف الصورة",
    "[تصنيف الوسائط",
    "[فيديو من العميل",
    "[تفريغ التسجيل",
    "[طلب كتالوج",
)

_BOT_FRAMING_SKIP_LINE_MARKERS = (
    "استنتاج خفيف من النص",
    "اقرأ السياق ورد على العميل",
    "ملاحظة: الفيديو",
    "ملاحظة: تعذّر استخراج",
)


@dataclass(frozen=True)
class TrustedFocusResult:
    title: str = ""
    product_id: str = ""
    origin: str = ""
    fresh: bool = False
    reason: str = ""
    freshness_score: float = 0.0
    continuity_score: float = 0.0

    @property
    def trusted(self) -> bool:
        return bool(self.title)


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    s = re.sub(r"[^\w\s\u0600-\u06FF]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


_VISION_QUERY_STOPLIST = frozenset(
    {_norm(v) for v in _VISION_QUERY_STOPLIST_RAW if v}
)


def _strip_voice_framing(text: str) -> str:
    """Remove ``[تفريغ التسجيل]`` and similar media prefixes."""
    s = (text or "").strip()
    s = re.sub(r"^\[\s*[^\]]+\]\s*", "", s)
    return s.strip()


def customer_authored_caption(message: str) -> str:
    """Customer lines before the first bot media/vision framing block."""
    lines: List[str] = []
    for line in (message or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if any(s.startswith(prefix) for prefix in _BOT_FRAMING_LINE_PREFIXES):
            break
        if s.startswith("[تصنيف") or s.startswith("[طلب كتالوج"):
            break
        lines.append(s)
    return "\n".join(lines).strip()


def strip_bot_media_framing(text: str) -> str:
    """Strip normalizer / brain framing; keep customer caption + vision body.

    Removes bot-generated lines such as ``[وصف الصورة المرسلة]``,
    ``[تصنيف الصورة: …]``, and video instruction blocks.  When a
    vision line carries a ``]`` suffix, only the OCR/vision body after
    the closing bracket is kept.
    """
    if not text:
        return ""
    lines: List[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if any(s.startswith(prefix) for prefix in _BOT_FRAMING_LINE_PREFIXES):
            if (
                s.startswith("[وصف الصورة")
                or s.startswith("[وصف الفيديو")
                or s.startswith("[تفريغ التسجيل")
            ):
                if "]" in s:
                    remainder = s.split("]", 1)[-1].strip()
                    if remainder:
                        lines.append(remainder)
                else:
                    remainder = _strip_voice_framing(s)
                    if remainder:
                        lines.append(remainder)
            continue
        if s.startswith("[تصنيف") or s.startswith("[طلب كتالوج"):
            continue
        if any(marker in s for marker in _BOT_FRAMING_SKIP_LINE_MARKERS):
            continue
        lines.append(s)
    return "\n".join(lines).strip()


def _is_vision_stoplist_query(candidate: str) -> bool:
    """True when *candidate* is a vision-template token, not a SKU."""
    core = _norm((candidate or "").strip())
    if not core:
        return True
    if core in _VISION_QUERY_STOPLIST:
        return True
    for stop in _VISION_QUERY_STOPLIST_RAW:
        if core == _norm(stop):
            return True
    return False


def normalize_for_visual_detection(text: str) -> str:
    """Normalize inbound (incl. STT quirks) before visual intent detection."""
    raw = strip_bot_media_framing(text or "")
    norm = _norm(raw)
    for pat, repl in _STT_REPLACEMENTS:
        norm = re.sub(pat, repl, norm)
    return norm


def prepare_inbound_for_commerce(
    raw_message: str,
    brain_state: Optional[Dict[str, Any]] = None,
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Best inbound text for commerce/visual detection at dispatch time.

    Prefers the brain's semantically repaired canonical message from the
    same turn, then applies lightweight STT normalization.
    """
    bs = brain_state or {}
    raw = strip_bot_media_framing(_strip_voice_framing(raw_message or ""))
    canonical = str(bs.get("last_inbound_canonical") or "").strip()
    canon_turn = int(bs.get("last_inbound_canonical_turn") or 0)
    current_turn = int(bs.get("turn") or 0)
    if canonical and canon_turn >= max(0, current_turn - 1):
        base = canonical
    else:
        base = raw

    meta = inbound_metadata or {}
    if str(meta.get("normalized_type") or "").lower() == "audio" and raw:
        base = raw

    try:
        from modules.ai.brain.interpret.semantic_turn_interpreter import (  # noqa: PLC0415
            interpret_semantic_turn,
            should_run_semantic_interpreter,
        )

        if base and should_run_semantic_interpreter(base, None, []):
            interp = interpret_semantic_turn(
                raw_text=base,
                state=None,
                history=list(bs.get("recent_messages") or [])[-8:],
            )
            if interp and str(getattr(interp, "canonical_text", "") or "").strip():
                base = str(interp.canonical_text).strip()
    except Exception:  # noqa: BLE001
        pass

    return base.strip() or raw


def _product_tokens(text: str) -> Set[str]:
    norm = _norm(text)
    if not norm:
        return set()
    return {
        t for t in norm.split()
        if len(t) >= 2 and t not in _STOP_PRODUCT_TOKENS
    }


def _fuzzy_title_match(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    ta, tb = _product_tokens(na), _product_tokens(nb)
    if not ta or not tb:
        return False
    return bool(ta & tb)


def _state_dict(state: Any) -> Dict[str, Any]:
    if isinstance(state, dict):
        return state
    if state is None:
        return {}
    if hasattr(state, "to_dict"):
        try:
            return dict(state.to_dict())
        except Exception:  # noqa: BLE001
            pass
    return {}


def focus_age_turns(state: Any) -> int:
    bs = _state_dict(state)
    turn = int(bs.get("turn") or 0)
    focus_turn = int(bs.get("product_focus_turn") or 0)
    if focus_turn <= 0:
        return 999
    return max(0, turn - focus_turn)


def visual_focus_age_turns(state: Any) -> int:
    bs = _state_dict(state)
    turn = int(bs.get("turn") or 0)
    visual_turn = int(bs.get("visual_focus_turn") or 0)
    if visual_turn <= 0:
        return 999
    return max(0, turn - visual_turn)


def _freshness_score(age: int, max_turns: int) -> float:
    if age >= 999:
        return 0.0
    return round(max(0.0, 1.0 - (age / max(max_turns, 1))), 2)


def _continuity_score(focus: str, recent: str) -> float:
    if not focus or not recent:
        return 0.0
    if _fuzzy_title_match(focus, recent):
        return 1.0
    focus_tokens = _product_tokens(focus)
    recent_tokens = _product_tokens(recent)
    if not focus_tokens or not recent_tokens:
        return 0.0
    return round(len(focus_tokens & recent_tokens) / max(len(focus_tokens), len(recent_tokens)), 2)


def _combined_freshness_score(state: Any) -> float:
    prod_score = _freshness_score(focus_age_turns(state), FOCUS_FRESH_TURNS)
    vis_turn = int(_state_dict(state).get("visual_focus_turn") or 0)
    if vis_turn <= 0:
        return prod_score
    vis_score = _freshness_score(visual_focus_age_turns(state), VISUAL_FOCUS_FRESH_TURNS)
    if is_visual_deictic_focus_trusted(state):
        return max(prod_score, vis_score)
    return min(prod_score, vis_score)


def is_focus_fresh(state: Any, *, max_turns: int = FOCUS_FRESH_TURNS) -> bool:
    return focus_age_turns(state) <= int(max_turns)


def is_visual_focus_fresh(state: Any, *, max_turns: int = VISUAL_FOCUS_FRESH_TURNS) -> bool:
    return visual_focus_age_turns(state) <= int(max_turns)


def is_visual_deictic_focus_trusted(state: Any) -> bool:
    """
    Stricter freshness for image-only deictic asks.

    Visual referents decay faster than textual product focus unless the
    customer recently reinforced the same SKU in text.
    """
    bs = _state_dict(state)
    vis_turn = int(bs.get("visual_focus_turn") or 0)
    if vis_turn <= 0:
        return is_focus_fresh(state)
    if is_visual_focus_fresh(state):
        return True
    if is_focus_fresh(state):
        recent = _latest_customer_product_mention(state)
        focus = _focus_title(state)
        return bool(focus and recent and _fuzzy_title_match(focus, recent))
    return False


def log_focus_resolution(
    *,
    focus: str,
    freshness: float,
    continuity: float,
    trusted: bool,
    reason: str = "",
) -> None:
    if trusted:
        logger.info(
            '[FOCUS_RESOLUTION] focus=%r freshness=%.2f continuity=%.2f trusted=true',
            focus,
            freshness,
            continuity,
        )
        return
    logger.info(
        '[FOCUS_RESOLUTION] focus=%r freshness=%.2f continuity=%.2f trusted=false reason=%s',
        focus,
        freshness,
        continuity,
        reason or "unknown",
    )


def _finish_focus_resolution(
    *,
    evaluated_focus: str,
    recent: str,
    state: Any,
    title: str = "",
    product_id: str = "",
    origin: str = "",
    fresh: bool = False,
    reason: str = "",
) -> TrustedFocusResult:
    trusted = bool(title)
    freshness = _combined_freshness_score(state) if evaluated_focus else 0.0
    continuity = _continuity_score(evaluated_focus, recent)
    log_focus_resolution(
        focus=evaluated_focus or title,
        freshness=freshness,
        continuity=continuity,
        trusted=trusted,
        reason=reason if not trusted else "",
    )
    return TrustedFocusResult(
        title=title,
        product_id=product_id,
        origin=origin,
        fresh=fresh,
        reason=reason,
        freshness_score=freshness,
        continuity_score=continuity,
    )


def stamp_product_focus_metadata(state: Any, product: Optional[Dict[str, Any]]) -> None:
    """Call when ``current_product_focus`` changes."""
    if state is None or not isinstance(product, dict):
        return
    title = str(product.get("title") or "").strip()
    if not title:
        return
    old = getattr(state, "current_product_focus", None) or {}
    old_title = str(old.get("title") or "").strip() if isinstance(old, dict) else ""
    turn = int(getattr(state, "turn", 0) or 0)
    if title != old_title:
        state.product_focus_turn = turn


def _set_turn_field(state: Any, field: str, turn: int) -> None:
    if state is None:
        return
    if hasattr(state, field):
        setattr(state, field, turn)
    elif isinstance(state, dict):
        state[field] = turn


def stamp_visual_focus_metadata(
    state: Any,
    product: Optional[Dict[str, Any]] = None,
) -> None:
    """Call when a product card/image is sent or composed for outbound."""
    if state is None:
        return
    turn = int(getattr(state, "turn", 0) or _state_dict(state).get("turn") or 0)
    _set_turn_field(state, "visual_focus_turn", turn)


def stamp_visual_focus_from_outbound_reply(state: Any, reply: str) -> None:
    """Stamp visual focus when the composed reply includes a product card marker."""
    if not reply or state is None:
        return
    if _PRODUCT_MARKER_RE.search(reply or ""):
        stamp_visual_focus_metadata(state)


def _focus_title(state: Any) -> str:
    bs = _state_dict(state)
    focus = bs.get("current_product_focus")
    if isinstance(focus, dict):
        return str(focus.get("title") or "").strip()
    return ""


def _focus_id(state: Any) -> str:
    bs = _state_dict(state)
    focus = bs.get("current_product_focus")
    if isinstance(focus, dict):
        return str(focus.get("id") or focus.get("external_id") or "").strip()
    return ""


def _latest_customer_product_mention(state: Any, *, limit: int = 10) -> str:
    """Most recent explicit product mention from customer turns."""
    bs = _state_dict(state)
    msgs = list(bs.get("recent_messages") or [])[-limit:]

    def _mention_from_content(content: str) -> str:
        explicit = extract_visual_product_query(content)
        if explicit:
            return explicit
        if _COMPARE_RE.search(_norm(content)):
            return ""
        tokens = _product_tokens(content)
        if tokens:
            return max(tokens, key=len)
        return ""

    for turn in reversed(msgs):
        role = str(turn.get("role") or turn.get("direction") or "").lower()
        content = str(turn.get("content") or turn.get("text") or "").strip()
        if not content:
            continue
        if role in {"user", "customer", "inbound"}:
            hit = _mention_from_content(content)
            if hit:
                return hit

    for turn in reversed(msgs):
        role = str(turn.get("role") or turn.get("direction") or "").lower()
        content = str(turn.get("content") or turn.get("text") or "").strip()
        if not content:
            continue
        if role in {"assistant", "outbound"}:
            hit = _mention_from_content(content)
            if hit:
                return hit
    browse = str(bs.get("last_browse_query") or "").strip()
    if browse:
        return browse
    prep = bs.get("order_prep") or {}
    if isinstance(prep, dict):
        pname = str(prep.get("product_name") or "").strip()
        if pname:
            return pname
    return ""


def resolve_trusted_focus_for_deictic(
    state: Any,
    inbound_message: str = "",
) -> TrustedFocusResult:
    """
    Resolve which product a deictic visual ask refers to.

    Prefers fresh, topic-aligned focus; falls back to recent customer
    mentions on topic shift; otherwise returns empty → clarify.
    """
    bs = _state_dict(state)
    focus = _focus_title(bs)
    focus_id = _focus_id(bs)
    age = focus_age_turns(bs)
    recent = _latest_customer_product_mention(bs)

    if focus and recent and not _fuzzy_title_match(focus, recent):
        if age > FOCUS_FRESH_TURNS or not is_visual_deictic_focus_trusted(state):
            return _finish_focus_resolution(
                evaluated_focus=focus,
                recent=recent,
                state=state,
                title=recent,
                origin="recent_customer_mention",
                fresh=True,
                reason="topic_shift_recent_mention",
            )
        return _finish_focus_resolution(
            evaluated_focus=focus,
            recent=recent,
            state=state,
            title=recent,
            origin="recent_customer_mention",
            fresh=True,
            reason="topic_shift_over_stale_focus",
        )

    if focus and is_visual_deictic_focus_trusted(state):
        return _finish_focus_resolution(
            evaluated_focus=focus,
            recent=recent,
            state=state,
            title=focus,
            product_id=focus_id,
            origin="current_product_focus",
            fresh=True,
            reason="focus_fresh",
        )

    if focus and age <= FOCUS_GAP_STALE_TURNS and recent and _fuzzy_title_match(focus, recent):
        return _finish_focus_resolution(
            evaluated_focus=focus,
            recent=recent,
            state=state,
            title=focus,
            product_id=focus_id,
            origin="current_product_focus",
            fresh=True,
            reason="focus_reinforced_by_recent_mention",
        )

    if recent and not focus:
        return _finish_focus_resolution(
            evaluated_focus=focus,
            recent=recent,
            state=state,
            title=recent,
            origin="recent_customer_mention",
            fresh=True,
            reason="recent_mention_no_focus",
        )

    if focus and age > FOCUS_GAP_STALE_TURNS:
        return _finish_focus_resolution(
            evaluated_focus=focus,
            recent=recent,
            state=state,
            reason="focus_too_stale",
        )

    if focus and not is_visual_deictic_focus_trusted(state):
        return _finish_focus_resolution(
            evaluated_focus=focus,
            recent=recent,
            state=state,
            reason="visual_focus_stale",
        )

    return _finish_focus_resolution(
        evaluated_focus=focus,
        recent=recent,
        state=state,
        reason="no_trusted_focus",
    )


def is_deictic_visual_request(message: str) -> bool:
    """True when customer asks for *the* image without naming a SKU."""
    norm = normalize_for_visual_detection(message or "")
    if not norm:
        return False
    if _COMPARE_RE.search(norm):
        return False
    if extract_visual_product_query(message or ""):
        return False
    if re.search(r"صور(?:ه|ة)?\s*(?:ل|ل|ال)\s+\S", norm):
        return False
    if _DEICTIC_VISUAL_RE.search(norm):
        return True
    if re.search(r"^(?:ال)?صور(?:ه|ة)?\s*(?:وين|فين|وينها|فينها)\s*[\?؟]?$", norm):
        return True
    if re.search(r"^(?:اب(?:ي|غ(?:ى|a)?)|ودي|ابغا)\s+(?:ال)?صور(?:ه|ة)?\s*[\?؟]?$", norm):
        return True
    return False


def _customer_visual_ask_present(norm: str) -> bool:
    return bool(
        re.search(
            r"(?:اب(?:ي|غ(?:ى|a)?)|أ(?:بي|ب(?:غ(?:ى|a)?)?)|ودي|ور(?:ي|)ني|"
            r"ار(?:سل|سل)|ابعث|اشوف|أشوف|اعرض|وين|فين)",
            norm,
        )
    )


def _is_customer_named_visual_request(norm: str) -> bool:
    """Named visual asks only — reject vision-OCR ``في الصورة`` descriptions."""
    if _GENERIC_VISUAL_RE.search(norm):
        return True
    m = _NAMED_VISUAL_RE.search(norm)
    if not m:
        return False
    gd = m.groupdict()
    product = (gd.get("product") or "").strip()
    product2 = (gd.get("product2") or "").strip()
    product3 = (gd.get("product3") or "").strip()
    if product or product2:
        return True
    if product3:
        if _norm(product3) in _STOP_PRODUCT_TOKENS:
            return False
        if _is_vision_stoplist_query(product3):
            return False
        return _customer_visual_ask_present(norm)
    return _customer_visual_ask_present(norm)


def is_product_visual_request(message: str) -> bool:
    """True for any product image / catalog-card visual ask."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = normalize_for_visual_detection(raw)
    if _COMPARE_RE.search(norm):
        return False
    if is_deictic_visual_request(raw):
        return True
    if _is_customer_named_visual_request(norm):
        return True
    if re.search(
        r"^(?:ور(?:ي|)ني|ور(?:ي|)ن(?:ي|a)|اعرض(?:\s*لي)?)\s+[\w\u0600-\u06FF]{2,40}\s*$",
        norm,
    ):
        return True
    return False


def extract_visual_product_query(message: str) -> str:
    """Extract explicit product name from a visual request, if any."""
    raw = customer_authored_caption(message) or (message or "").strip()
    if not raw:
        return ""
    norm = normalize_for_visual_detection(raw)
    m2 = re.search(
        r"صور(?:ه|ة)?\s*(?:ل|ل|ال)\s*([\w\u0600-\u06FF][\w\u0600-\u06FF\s\-]{1,40})",
        norm,
    )
    if m2:
        cand = (m2.group(1) or "").strip()
        if not _is_vision_stoplist_query(cand):
            return cand
    m_carousel = re.search(
        r"(?:ال)?(?:عسل|منتج|طلح|سدر|سمر)\s+(?:اللي\s+)?(?:فوق|هذا|هذي)\b",
        norm,
    )
    if m_carousel:
        return m_carousel.group(0).strip()
    m3 = re.search(r"([\w\u0600-\u06FF]{2,20})\s+صور(?:ه|ة)?", norm)
    if m3:
        cand = (m3.group(1) or "").strip()
        if (
            cand not in _STOP_PRODUCT_TOKENS
            and not _is_vision_stoplist_query(cand)
        ):
            return cand
    m4 = re.search(
        r"^(?:ور(?:ي|)ني|ور(?:ي|)ن(?:ي|a)|اعرض(?:\s*لي)?)\s+([\w\u0600-\u06FF]{2,40})\s*$",
        norm,
    )
    if m4:
        cand = (m4.group(1) or "").strip()
        if (
            cand not in _STOP_PRODUCT_TOKENS
            and cand not in {"شكل", "شكلها", "شكله"}
            and not _is_vision_stoplist_query(cand)
        ):
            return cand
    m = _NAMED_VISUAL_RE.search(norm)
    if m:
        for g in ("product", "product2", "product3"):
            val = (m.group(g) or "").strip()
            if (
                val
                and val not in _STOP_PRODUCT_TOKENS
                and not _is_vision_stoplist_query(val)
            ):
                return val.strip()
    return ""


def attachment_matches_turn_request(
    *,
    inbound_message: str,
    attachment_title: str,
    brain_state: Optional[Dict[str, Any]] = None,
    intent_name: str = "",
    brain_action: str = "",
) -> Tuple[bool, str]:
    """
    Per-card invariant: title must match current-turn customer context.

    Returns ``(allow, reason)``.
    """
    title = (attachment_title or "").strip()
    if not title:
        return False, "empty_title"

    inbound = prepare_inbound_for_commerce(
        inbound_message or "",
        brain_state,
    )
    focus = _focus_title(brain_state)
    intent = (intent_name or "").strip().lower()
    action = (brain_action or "").strip().lower()

    explicit = extract_visual_product_query(inbound)
    if explicit:
        if _fuzzy_title_match(explicit, title):
            return True, "explicit_query_match"
        return False, "explicit_query_mismatch"

    if is_deictic_visual_request(inbound):
        trusted = resolve_trusted_focus_for_deictic(brain_state, inbound)
        if trusted.title and _fuzzy_title_match(trusted.title, title):
            return True, f"deictic_{trusted.reason}"
        if trusted.reason in {"focus_too_stale", "no_trusted_focus", "visual_focus_stale"}:
            return False, "deictic_stale_focus"
        return False, "deictic_focus_mismatch"

    if intent == "product_visual_request" and is_product_visual_request(inbound):
        if focus and is_focus_fresh(brain_state) and _fuzzy_title_match(focus, title):
            return True, "visual_intent_fresh_focus"
        trusted = resolve_trusted_focus_for_deictic(brain_state, inbound)
        if trusted.title and _fuzzy_title_match(trusted.title, title):
            return True, f"visual_intent_{trusted.reason}"

    if is_product_visual_request(inbound):
        if focus and _fuzzy_title_match(focus, title):
            return True, "visual_focus_match"
        inbound_tokens = _product_tokens(inbound)
        title_tokens = _product_tokens(title)
        if inbound_tokens & title_tokens:
            return True, "visual_token_overlap"

    if action in {"search_products", "narrow", "recommend_addon"}:
        if focus and _fuzzy_title_match(focus, title):
            return True, "commerce_action_focus"
        inbound_tokens = _product_tokens(inbound)
        title_tokens = _product_tokens(title)
        if inbound_tokens & title_tokens:
            return True, "commerce_action_token_overlap"

    if intent in {"ask_product", "ask_price", "pick_list_item", "product_visual_request"}:
        if focus and is_focus_fresh(brain_state) and _fuzzy_title_match(focus, title):
            return True, "intent_fresh_focus_match"
        inbound_tokens = _product_tokens(inbound)
        title_tokens = _product_tokens(title)
        if inbound_tokens & title_tokens:
            return True, "intent_token_overlap"

    if focus and is_focus_fresh(brain_state) and _fuzzy_title_match(focus, title):
        return True, "focus_fresh_fallback"

    if not inbound and not focus:
        return False, "no_turn_context"

    return False, "stale_or_unrelated_product"


__all__ = [
    "FOCUS_FRESH_TURNS",
    "FOCUS_GAP_STALE_TURNS",
    "VISUAL_FOCUS_FRESH_TURNS",
    "TrustedFocusResult",
    "attachment_matches_turn_request",
    "extract_visual_product_query",
    "focus_age_turns",
    "is_deictic_visual_request",
    "is_focus_fresh",
    "is_product_visual_request",
    "is_visual_deictic_focus_trusted",
    "is_visual_focus_fresh",
    "log_focus_resolution",
    "normalize_for_visual_detection",
    "prepare_inbound_for_commerce",
    "resolve_trusted_focus_for_deictic",
    "stamp_product_focus_metadata",
    "stamp_visual_focus_from_outbound_reply",
    "stamp_visual_focus_metadata",
    "strip_bot_media_framing",
    "visual_focus_age_turns",
]
