"""
social_human_context.py
───────────────────────
Social & Human Context Layer — runs before commerce intent resolution.

Detection order (semantic-first, not keyword-only):
  1. Existing classifiers (``classify_social``, ``classify_non_commerce``)
  2. Intent Priority element analysis (blessing / courtesy spans)
  3. Structural semantic patterns (Allah-verb blessings, ja3lak, mizan…)
  4. Narrow operational patterns (religious reminder verses, job help)

Mixed turns honour ``primary_goal`` over ``secondary_social_signal``.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .intent.non_commerce_classifier import NonCommerceMatch, classify_non_commerce
from .intent.social_classifier import (
    _has_commercial_signal,
    _has_practical_question_signal,
    classify_social,
)
from .types import (
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_HESITATION,
    INTENT_SOCIAL,
    Intent,
    MerchantConversationState,
)

logger = logging.getLogger("nahla.brain.social_human_context")

# ── Intent family: social_human_intent categories ─────────────────────────────
SHC_APPRECIATION = "appreciation"
SHC_GRATITUDE = "gratitude"
SHC_RESPECT = "respect"
SHC_COMPLIMENT = "compliment"
SHC_RELIGIOUS_REMINDER = "religious_reminder"
SHC_DUA = "dua"
SHC_CONGRATULATIONS = "congratulations"
SHC_SYMPATHY = "sympathy"
SHC_HUMOR = "humor"
SHC_SOCIAL_STICKER = "social_sticker"
SHC_ACKNOWLEDGEMENT = "acknowledgement"
SHC_JOB_HELP_REQUEST = "job_help_request"
SHC_WELLBEING_CHECK = "wellbeing_check"

SOCIAL_HUMAN_CATEGORIES = frozenset({
    SHC_APPRECIATION,
    SHC_GRATITUDE,
    SHC_RESPECT,
    SHC_COMPLIMENT,
    SHC_RELIGIOUS_REMINDER,
    SHC_DUA,
    SHC_CONGRATULATIONS,
    SHC_SYMPATHY,
    SHC_HUMOR,
    SHC_SOCIAL_STICKER,
    SHC_ACKNOWLEDGEMENT,
    SHC_JOB_HELP_REQUEST,
    SHC_WELLBEING_CHECK,
})

COMMERCE_TAIL_BLOCK_CATEGORIES = frozenset({
    SHC_APPRECIATION,
    SHC_GRATITUDE,
    SHC_RESPECT,
    SHC_COMPLIMENT,
    SHC_RELIGIOUS_REMINDER,
    SHC_DUA,
    SHC_CONGRATULATIONS,
    SHC_SYMPATHY,
    SHC_HUMOR,
    SHC_SOCIAL_STICKER,
    SHC_ACKNOWLEDGEMENT,
    SHC_WELLBEING_CHECK,
})

# Reply-type hints for commerce tail guard
REPLY_TYPE_SOCIAL = "social"
REPLY_TYPE_PERSONA = "persona_social"
REPLY_TYPE_COMMERCE = "commerce"
REPLY_TYPE_OPERATIONAL = "operational"
REPLY_TYPE_MIXED = "mixed"

_DIA = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_WS = re.compile(r"\s+")

# ── Structural semantic patterns (category families, not literal phrases) ─────
# Allah + blessing/welfare verb stem — covers «الله يبارك لك», «الله يسعدك»,
# «الله يجعلها في ميزان حسناتك», «الله يرزقنا وإياك», etc.
_ALLAH_BLESSING_VERB_RE = re.compile(
    r"الله\s+(?:"
    r"ي?(?:بارك|يسعد|يجعل|يرزق|يجز|يبيض|يعاف|يحفظ|يطول|يرحم|يرض|يوفق|"
    r"يكثر|يكتب|يسلم|يفرح|يبق|يعمر|يجعل)"
    r"|(?:ت|ن)?(?:بارك|يسعد|يجعل|يرزق)"
    r")"
)
_MIZAN_HASSANAT_RE = re.compile(r"ميزان\s+(?:حسن(?:ات)?|اجر|اعمال)")
_JAAALAK_WELLBEING_RE = re.compile(
    r"(?:الله\s+)?جعل(?:ك|ك|كم|كن|كما)\s+(?:الله\s+)?(?:سالم|بخير|عاف(?:يه|ية)|سليم)"
)
_RIZQ_DUA_RE = re.compile(
    r"(?:الله\s+)?(?:يرزق|رزق)(?:نا|ك|كم|كن)?(?:\s+و)?(?:\s*(?:ا?يا)?(?:ك|نا|كم))?"
)
_BIYAD_WAJH_RE = re.compile(r"بي+[ضظ]\s+الله\s+وجه")
_THANKS_SEMANTIC_RE = re.compile(
    r"(?:جزاك|جزيت|جزيتم|مشكور|مشكور(?:ه|ين)|تسلم|يسلمو|thank\s*you|thanks\b)"
)

_RELIGIOUS_REMINDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"قل\s*لا\s*اله\s*الا\s*الله"),
    re.compile(r"لا\s*اله\s*الا\s*الله"),
    re.compile(r"صل(?:ي|وا|و)?\s*عل(?:ي|ى|ه)\s*(?:محمد|النبي|نبينا|ال)"),
    re.compile(r"اللهم\s*صل\s*و?سلم\s*عل"),
    re.compile(r"سبحان\s*الله"),
    re.compile(r"الحمد\s*لله"),
)

_SOCIAL_WELLBEING_PHRASES: tuple[re.Pattern[str], ...] = (
    re.compile(r"الحمد\s*لله\s+ع(?:لى|ل)\s*السلام"),
    re.compile(r"^الحمد\s*لله(?:\s|$|[،.!؟?])"),
)

_JOB_HELP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"وظيف"),
    re.compile(r"توظيف"),
    re.compile(r"يوظف"),
    re.compile(r"تعرف\s+(?:احد|حد)\s+يوظف"),
    re.compile(r"يعرف\s+(?:احد|حد)\s+يوظف"),
    re.compile(r"سير(?:ه|ة)\s*ذات"),
    re.compile(r"cv\b", re.I),
    re.compile(r"اب(?:ي|غى|غي)\s*(?:وظيف|عمل|شغل)"),
    re.compile(r"مساعد(?:ه|ة)\s*(?:في\s*)?(?:ال)?(?:وظيف|عمل|شغل|job)"),
)

_SOCIAL_CATEGORY_MAP: dict[str, str] = {
    "thanks": SHC_GRATITUDE,
    "blessing": SHC_DUA,
    "prophet_invocation": SHC_RELIGIOUS_REMINDER,
    "basmala": SHC_RELIGIOUS_REMINDER,
    "compliment": SHC_COMPLIMENT,
    "strong_praise": SHC_APPRECIATION,
    "general_courtesy": SHC_ACKNOWLEDGEMENT,
    "eid_greeting": SHC_CONGRATULATIONS,
    "dua": SHC_DUA,
    "condolence": SHC_SYMPATHY,
    "religious_media": SHC_RELIGIOUS_REMINDER,
    "social_forward": SHC_ACKNOWLEDGEMENT,
    "morning_greeting": SHC_WELLBEING_CHECK,
    "emotional_personal": SHC_APPRECIATION,
    "social_image": SHC_SOCIAL_STICKER,
}

_COMMERCE_INTENTS = frozenset({
    "ask_product", "ask_price", "start_order", "pay_now",
    "pick_list_item", "ask_shipping", "ask_payment_info",
    "track_order", "product_visual_request", "ask_location",
    "ask_owner_contact", "ask_payment_info", "ask_cod",
})

_SOCIAL_PRIMARY_GOALS = frozenset({
    "social_only", "greeting_only",
})


@dataclass(frozen=True)
class SocialHumanContext:
    active: bool
    category: str
    confidence: float
    source: str
    primary_goal: str = ""
    secondary_social_signal: str = ""
    is_pure_social_turn: bool = False
    reply_type: str = REPLY_TYPE_COMMERCE
    block_commerce_tail: bool = False
    block_commerce_escalation: bool = False
    suppress_greeting_fast_path: bool = False
    suppress_embedded_greeting_prepend: bool = False
    in_commerce_context: bool = False

    @property
    def social_human_intent(self) -> str:
        return self.category if self.active else ""


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _DIA.sub("", s)
    s = (
        s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    )
    return _WS.sub(" ", s.lower()).strip()


def _is_commerce_context(state: Optional[MerchantConversationState]) -> bool:
    if state is None:
        return False
    stage = str(getattr(state, "stage", "") or "").strip().lower()
    if stage in {"deciding", "ordering", "checkout"}:
        return True
    prep = getattr(state, "order_prep", None)
    if prep is not None:
        if bool(getattr(prep, "awaiting_payment_receipt", False)):
            return True
        if bool(getattr(prep, "payment_receipt_received", False)):
            return True
    if getattr(state, "current_product_focus", None):
        return True
    return False


def _has_actionable_substance(message: str) -> bool:
    try:
        from .decision.engine import _first_turn_has_actionable_substance  # noqa: PLC0415

        return _first_turn_has_actionable_substance(message)
    except Exception:
        return len(_norm(message).split()) > 8


def _is_job_help_message(norm: str) -> bool:
    return any(pat.search(norm) for pat in _JOB_HELP_PATTERNS)


def _has_commercial_primary(
    *,
    message: str,
    intent: Intent,
    intent_priority: Any = None,
) -> bool:
    """True when commerce / operational ask dominates the turn."""
    norm = _norm(message)
    if _is_job_help_message(norm):
        return False

    if str(getattr(intent, "name", "") or "") in _COMMERCE_INTENTS:
        return True

    if _has_commercial_signal(norm):
        return True
    if _has_practical_question_signal(norm):
        return True

    if intent_priority is not None:
        primary = str(getattr(intent_priority, "primary_customer_goal", "") or "")
        if primary and primary not in _SOCIAL_PRIMARY_GOALS.union({"general"}):
            return True
        if getattr(intent_priority, "has_commercial_primary", False):
            return True

    return False


def _primary_goal_from_sources(
    *,
    intent: Intent,
    intent_priority: Any = None,
    is_pure_social: bool,
    category: str,
) -> str:
    if intent_priority is not None:
        pg = str(getattr(intent_priority, "primary_customer_goal", "") or "").strip()
        if pg:
            return pg
    if str(getattr(intent, "name", "") or "") in _COMMERCE_INTENTS:
        return str(intent.name)
    if is_pure_social and category:
        return "social_only"
    return "general"


def _detect_semantic_social_signal(norm: str) -> tuple[str, float, str]:
    """Structural social/dua families — not a literal phrase whitelist."""
    for pat in _SOCIAL_WELLBEING_PHRASES:
        if pat.search(norm):
            return SHC_WELLBEING_CHECK, 0.91, "semantic:wellbeing_phrase"
    if _RELIGIOUS_REMINDER_PATTERNS and any(
        p.search(norm) for p in _RELIGIOUS_REMINDER_PATTERNS
    ):
        return SHC_RELIGIOUS_REMINDER, 0.96, "semantic:religious_reminder"
    if _MIZAN_HASSANAT_RE.search(norm):
        return SHC_DUA, 0.92, "semantic:mizan_hassanat"
    if _ALLAH_BLESSING_VERB_RE.search(norm):
        return SHC_DUA, 0.91, "semantic:allah_blessing_verb"
    if _JAAALAK_WELLBEING_RE.search(norm):
        return SHC_DUA, 0.90, "semantic:jaalak_wellbeing"
    if _RIZQ_DUA_RE.search(norm) and ("يرزق" in norm or "رزق" in norm):
        return SHC_DUA, 0.89, "semantic:rizq_dua"
    if _BIYAD_WAJH_RE.search(norm):
        return SHC_APPRECIATION, 0.92, "semantic:biyad_wajh"
    if _THANKS_SEMANTIC_RE.search(norm):
        return SHC_GRATITUDE, 0.90, "semantic:thanks_family"
    return "", 0.0, ""


def _detect_from_intent_priority(intent_priority: Any) -> tuple[str, float, str]:
    if intent_priority is None:
        return "", 0.0, ""
    try:
        from .intent_priority.types import (  # noqa: PLC0415
            ELEMENT_BLESSING,
            ELEMENT_COURTESY,
        )

        for el in getattr(intent_priority, "detected_elements", None) or []:
            et = str(getattr(el, "element_type", "") or "")
            conf = float(getattr(el, "confidence", 0) or 0)
            if et == ELEMENT_BLESSING:
                return SHC_DUA, max(conf, 0.88), f"intent_priority:{et}"
            if et == ELEMENT_COURTESY:
                return SHC_GRATITUDE, max(conf, 0.86), f"intent_priority:{et}"
    except Exception:
        pass
    return "", 0.0, ""


def _detect_category_from_text(
    norm: str,
    *,
    raw: str,
    intent_priority: Any = None,
) -> tuple[str, float, str]:
    # 1. Battle-tested classifiers (respect commercial disqualifiers internally)
    social = classify_social(raw)
    if social is not None:
        mapped = _SOCIAL_CATEGORY_MAP.get(social.category, SHC_ACKNOWLEDGEMENT)
        return mapped, float(social.confidence), f"social_classifier:{social.category}"

    nc = classify_non_commerce(raw)
    if nc is not None:
        mapped = _SOCIAL_CATEGORY_MAP.get(nc.category, SHC_ACKNOWLEDGEMENT)
        return mapped, float(nc.confidence), f"non_commerce:{nc.category}"

    # 2. Intent priority element analysis
    cat, conf, src = _detect_from_intent_priority(intent_priority)
    if cat:
        return cat, conf, src

    # 3. Structural semantic families
    cat, conf, src = _detect_semantic_social_signal(norm)
    if cat:
        return cat, conf, src

    # 4. Narrow operational patterns (job / explicit religious verse)
    for pat in _JOB_HELP_PATTERNS:
        if pat.search(norm):
            return SHC_JOB_HELP_REQUEST, 0.93, "operational:job_help"

    return "", 0.0, ""


def _detect_sticker_category(
    *,
    metadata: Dict[str, Any],
    message: str,
    history: List[Dict[str, Any]],
) -> tuple[str, float, str]:
    meta = metadata or {}
    media_type = str(
        meta.get("normalized_type") or meta.get("source_type") or ""
    ).strip().lower()
    if media_type != "sticker":
        return "", 0.0, ""

    if str(meta.get("attachment_ack_mode") or "").strip().lower() == "social":
        return SHC_SOCIAL_STICKER, 0.94, "sticker:ack_social"

    sticker_kind = str(meta.get("sticker_kind") or "").strip().lower()
    if sticker_kind in {"expressive_only", "text"} or meta.get("non_commerce_category"):
        social = classify_social(message)
        if social:
            mapped = _SOCIAL_CATEGORY_MAP.get(social.category, SHC_GRATITUDE)
            return mapped, float(social.confidence), "sticker:social_classifier"
        recent = _recent_conversation_blob(history, limit=4)
        if recent:
            return SHC_SOCIAL_STICKER, 0.90, "sticker:contextual_social"
        return SHC_SOCIAL_STICKER, 0.88, "sticker:expressive_default"

    return "", 0.0, ""


def _recent_conversation_blob(
    history: List[Dict[str, Any]],
    *,
    limit: int = 4,
) -> str:
    parts: list[str] = []
    for turn in (history or [])[-limit:]:
        body = str(turn.get("body") or turn.get("content") or turn.get("text") or "")
        if body.strip():
            parts.append(body)
    return _norm(" ".join(parts))


def _resolve_reply_type(
    *,
    is_pure_social: bool,
    commercial_primary: bool,
    has_secondary_social: bool,
    intent_name: str,
) -> str:
    if commercial_primary and has_secondary_social:
        return REPLY_TYPE_MIXED
    if commercial_primary and not is_pure_social:
        return REPLY_TYPE_COMMERCE
    if is_pure_social or intent_name == INTENT_SOCIAL:
        return REPLY_TYPE_SOCIAL
    return REPLY_TYPE_COMMERCE


def compute_social_human_context(
    *,
    message: str,
    intent: Intent,
    state: Optional[MerchantConversationState] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    nc_match: Optional[NonCommerceMatch] = None,
    intent_priority: Any = None,
) -> SocialHumanContext:
    """Analyze inbound turn for social/human signals before commerce routing."""
    raw = (message or "").strip()
    inactive = SocialHumanContext(
        active=False,
        category="",
        confidence=0.0,
        source="none",
    )
    if not raw:
        return inactive

    hist = list(history or [])
    meta = dict(inbound_metadata or {})
    slots = dict(getattr(intent, "slots", None) or {})
    in_commerce = _is_commerce_context(state)

    category, confidence, source = _detect_sticker_category(
        metadata=meta,
        message=raw,
        history=hist,
    )
    if not category:
        category, confidence, source = _detect_category_from_text(
            _norm(raw),
            raw=raw,
            intent_priority=intent_priority,
        )

    if not category and nc_match is not None:
        mapped = _SOCIAL_CATEGORY_MAP.get(nc_match.category, "")
        if mapped:
            category = mapped
            confidence = float(nc_match.confidence or 0.9)
            source = f"nc_match:{nc_match.category}"

    if not category:
        social_cat = str(slots.get("social_category") or "").strip()
        if social_cat:
            category = _SOCIAL_CATEGORY_MAP.get(social_cat, SHC_ACKNOWLEDGEMENT)
            confidence = float(getattr(intent, "confidence", 0) or 0.85)
            source = f"intent_slot:{social_cat}"

    if not category:
        return inactive

    commercial_primary = _has_commercial_primary(
        message=raw,
        intent=intent,
        intent_priority=intent_priority,
    )
    is_pure_social = not commercial_primary and not in_commerce
    if commercial_primary:
        is_pure_social = False

    primary_goal = _primary_goal_from_sources(
        intent=intent,
        intent_priority=intent_priority,
        is_pure_social=is_pure_social,
        category=category,
    )
    secondary_social = category if commercial_primary else ""

    embedded = bool(slots.get("embedded_greeting"))
    actionable = _has_actionable_substance(raw)
    suppress_greeting = bool(
        actionable
        or embedded
        or category == SHC_JOB_HELP_REQUEST
        or commercial_primary
    )
    suppress_prepend = bool(
        suppress_greeting
        or (is_pure_social and category in {
            SHC_RELIGIOUS_REMINDER,
            SHC_DUA,
            SHC_GRATITUDE,
            SHC_APPRECIATION,
            SHC_SYMPATHY,
        })
    )

    block_tail = bool(
        is_pure_social
        and category in COMMERCE_TAIL_BLOCK_CATEGORIES
        and category != SHC_JOB_HELP_REQUEST
    )
    block_commerce = bool(
        is_pure_social
        and category not in {SHC_WELLBEING_CHECK}
        and category != SHC_JOB_HELP_REQUEST
    )
    if category == SHC_JOB_HELP_REQUEST and is_pure_social:
        block_commerce = True
        block_tail = True

    reply_type = _resolve_reply_type(
        is_pure_social=is_pure_social,
        commercial_primary=commercial_primary,
        has_secondary_social=bool(secondary_social),
        intent_name=str(getattr(intent, "name", "") or ""),
    )

    return SocialHumanContext(
        active=True,
        category=category if is_pure_social else (secondary_social or category),
        confidence=confidence,
        source=source,
        primary_goal=primary_goal,
        secondary_social_signal=secondary_social,
        is_pure_social_turn=is_pure_social,
        reply_type=reply_type,
        block_commerce_tail=block_tail,
        block_commerce_escalation=block_commerce,
        suppress_greeting_fast_path=suppress_greeting,
        suppress_embedded_greeting_prepend=suppress_prepend,
        in_commerce_context=in_commerce or commercial_primary,
    )


def enrich_intent_with_social_human(
    intent: Intent,
    shc: SocialHumanContext,
) -> Intent:
    """Stamp slots; upgrade to SOCIAL only on pure social turns."""
    if not shc.active:
        return intent

    slots = dict(getattr(intent, "slots", None) or {})
    if shc.is_pure_social_turn:
        slots["social_human_intent"] = shc.category
    else:
        slots["secondary_social_signal"] = shc.secondary_social_signal or shc.category
        slots["primary_goal"] = shc.primary_goal
    slots["social_human_confidence"] = shc.confidence
    slots["social_human_source"] = shc.source
    slots["social_human_reply_type"] = shc.reply_type

    if shc.block_commerce_escalation and shc.is_pure_social_turn:
        slots["block_commerce_escalation"] = True
    if shc.category and not slots.get("social_category") and shc.is_pure_social_turn:
        slots.setdefault("social_category", shc.category)

    if (
        shc.is_pure_social_turn
        and intent.name in {INTENT_GENERAL, INTENT_HESITATION}
        and shc.category in SOCIAL_HUMAN_CATEGORIES - {SHC_JOB_HELP_REQUEST}
        and shc.confidence >= 0.88
    ):
        return Intent(
            name=INTENT_SOCIAL,
            confidence=max(float(intent.confidence or 0), shc.confidence),
            slots=slots,
            raw_message=intent.raw_message,
            extraction_method=getattr(intent, "extraction_method", "") or "social_human_context",
        )

    return Intent(
        name=intent.name,
        confidence=float(getattr(intent, "confidence", 0) or 0),
        slots=slots,
        raw_message=intent.raw_message,
        extraction_method=getattr(intent, "extraction_method", ""),
    )


def try_social_human_context_decision(ctx: Any) -> Any:
    """Route pure social/human turns before commerce branches."""
    shc: Optional[SocialHumanContext] = getattr(ctx, "social_human_context", None)
    if shc is None or not shc.active:
        return None
    if not shc.is_pure_social_turn or shc.in_commerce_context:
        return None

    intent = ctx.intent
    slots = dict(getattr(intent, "slots", None) or {})
    cat = shc.category

    from .decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from .persona_expression import (  # noqa: PLC0415
        PERSONA_TOPIC_SOCIAL,
        build_social_courtesy_decision,
        compose_social_persona_goal,
    )
    from .types import Decision  # noqa: PLC0415

    # Sticker / job turns must win even when upstream classifiers already
    # stamped a social bucket on the intent (e.g. religious_media on sticker text).
    if cat == SHC_JOB_HELP_REQUEST:
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": PERSONA_TOPIC_SOCIAL,
                "persona_kind": "social",
                "social_category": cat,
                "social_human_intent": cat,
                "block_commerce_escalation": True,
                "compose_goal_override": (
                    "job_help_request — The customer is asking about employment. "
                    "Answer naturally in Saudi Arabic without CS redirect openers."
                ),
            },
            reason=f"social human — job help ({shc.source})",
            confidence=shc.confidence,
        )

    if cat == SHC_SOCIAL_STICKER:
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": PERSONA_TOPIC_SOCIAL,
                "social_category": "general_courtesy",
                "social_human_intent": cat,
                "block_commerce_escalation": True,
                "compose_goal_override": (
                    compose_social_persona_goal("general_courtesy")
                    + " Social sticker — respond warmly, not media receipt copy."
                ),
            },
            reason=f"social human — expressive sticker ({shc.source})",
            confidence=shc.confidence,
        )

    if cat == SHC_WELLBEING_CHECK and intent.name in {
        INTENT_GENERAL, INTENT_HESITATION, INTENT_SOCIAL,
    }:
        return build_social_courtesy_decision(
            cat,
            confidence=shc.confidence,
            reason=f"social human — {cat} ({shc.source})",
            block_commerce=True,
            extra_args={"social_human_intent": cat},
        )

    if intent.name == INTENT_SOCIAL and slots.get("social_category"):
        if getattr(intent, "extraction_method", "") == "social_human_context":
            return None
        return None

    if cat in {SHC_RELIGIOUS_REMINDER, SHC_DUA} and intent.name in {
        INTENT_GENERAL, INTENT_HESITATION,
    }:
        return build_social_courtesy_decision(
            "dua" if cat == SHC_DUA else "religious_media",
            confidence=shc.confidence,
            reason=f"social human — {cat} ({shc.source})",
            block_commerce=True,
            extra_args={"social_human_intent": cat},
        )

    if intent.name in {INTENT_GENERAL, INTENT_HESITATION, INTENT_GREETING}:
        if shc.suppress_greeting_fast_path and intent.name == INTENT_GREETING:
            return None
        if cat in COMMERCE_TAIL_BLOCK_CATEGORIES:
            return build_social_courtesy_decision(
                slots.get("social_category") or cat,
                confidence=shc.confidence,
                reason=f"social human — {cat} ({shc.source})",
                block_commerce=True,
                extra_args={"social_human_intent": cat},
            )

    return None


def log_social_human_context(
    *,
    tenant_id: Any,
    shc: SocialHumanContext,
    preview: str = "",
) -> None:
    if not shc.active:
        return
    try:
        logger.info(
            "[SOCIAL_HUMAN_CONTEXT] tenant=%s category=%s conf=%.2f "
            "source=%s primary=%s secondary=%s pure_social=%s reply_type=%s "
            "block_tail=%s block_commerce=%s preview=%r",
            tenant_id,
            shc.category,
            shc.confidence,
            shc.source,
            shc.primary_goal,
            shc.secondary_social_signal,
            shc.is_pure_social_turn,
            shc.reply_type,
            shc.block_commerce_tail,
            shc.block_commerce_escalation,
            (preview or "")[:80],
        )
    except Exception:
        pass


__all__ = [
    "COMMERCE_TAIL_BLOCK_CATEGORIES",
    "REPLY_TYPE_COMMERCE",
    "REPLY_TYPE_MIXED",
    "REPLY_TYPE_OPERATIONAL",
    "REPLY_TYPE_PERSONA",
    "REPLY_TYPE_SOCIAL",
    "SHC_ACKNOWLEDGEMENT",
    "SHC_APPRECIATION",
    "SHC_COMPLIMENT",
    "SHC_CONGRATULATIONS",
    "SHC_DUA",
    "SHC_GRATITUDE",
    "SHC_HUMOR",
    "SHC_JOB_HELP_REQUEST",
    "SHC_RELIGIOUS_REMINDER",
    "SHC_RESPECT",
    "SHC_SOCIAL_STICKER",
    "SHC_SYMPATHY",
    "SHC_WELLBEING_CHECK",
    "SOCIAL_HUMAN_CATEGORIES",
    "SocialHumanContext",
    "compute_social_human_context",
    "enrich_intent_with_social_human",
    "log_social_human_context",
    "try_social_human_context_decision",
]
