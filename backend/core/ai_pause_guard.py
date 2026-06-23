"""
core/ai_pause_guard
───────────────────
AI loop guard. Two-stage pre-LLM + pre-send protection that lets long
natural sales conversations (20–50+ turns about products / prices /
shipping / sizes) keep flowing, but trips on the hallmark of a real
loop: the assistant repeating itself.

Core policy
-----------
* Message COUNT alone never pauses AI. A customer asking 30 product
  questions is encouraged.
* The guard uses a `loop_score` driven by:
    - similarity between consecutive assistant replies (primary signal)
    - automated-looking inbound from the customer side (likely a bot)
    - generic short replies ("شكراً", "طيب", "اوكي") with no progress
    - repeated handoff-notice text
  Sales-intent keywords ("سعر", "كم", "شحن", "مقاس", "buy", "price"…)
  decay the score, so genuine progress always relieves pressure.
* When the score crosses the recovery threshold we send ONE recovery
  message ("أخشى أنني أكرر نفس الإجابة…") and keep the AI active.
* Only if the score crosses the pause threshold AFTER recovery does
  the guard pause the AI (`reason=bot_loop_detected`). Handoff is the
  last resort, not the first.

Public surface
--------------
* `should_skip_ai(...)`        — top-of-funnel gate (pre-LLM).
* `evaluate_loop_pre_send(...)` — pre-send gate; returns
  `LoopDecision(action ∈ {continue, recovery, pause}, score, reason,
   recovery_text)`.
* `note_recovery_sent(...)`     — call after the merchant handler
                                   actually emits the recovery line.
* `after_ai_reply(...)`         — counts an emitted reply but never
                                   pauses on counts alone.
* `pause_ai / resume_ai`        — durable Conversation flag.

Design constraints
------------------
* Pure helper — no FastAPI imports.
* Per-process in-memory state for similarity windows; the durable
  truth (`Conversation.ai_paused`) survives restarts so any pause
  decision the guard reaches is persistent.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from models import Conversation, MessageEvent, Tenant

logger = logging.getLogger("nahla-backend")


# ────────────────────────────────────────────────────────────────────────────
# Reasons (mirror the values documented in the migration & UI)
# ────────────────────────────────────────────────────────────────────────────
REASON_MANUAL = "manual"  # legacy alias, kept for backward compat
REASON_MANUAL_PAUSE = "manual_pause"  # NEW — pure "stop AI" without human takeover
REASON_HUMAN_HANDOFF = "human_handoff"
REASON_BOT_LOOP = "bot_loop_detected"
REASON_RATE_LIMIT = "rate_limit"
REASON_INTERNAL_NUMBER = "internal_number"
REASON_MANUAL_TAKEOVER = "manual_takeover"
REASON_SUPPORT_ESCALATION = "support_escalation"

# Synthetic "skip reason" returned by ``should_skip_ai`` when the AI is
# allowed to keep replying in a RESTRICTED capacity — i.e. the customer
# asked for a human, no staff has actually picked up the conversation
# yet, and ``NAHLA_AI_HUMAN_PRIORITY_MODE`` is enabled. This is NOT a
# valid value for ``Conversation.ai_paused_reason`` (it never gets
# persisted); it's only used as a turn-local signal forwarded to the
# Brain pipeline so the policy gate can clamp aggressive actions.
REASON_HUMAN_PRIORITY = "human_priority"

# Feature flag for the new "human-priority" mode. Default OFF so any
# rollout has to be explicit per environment. Set via
# ``NAHLA_AI_HUMAN_PRIORITY_MODE=1`` for the internal tenant first,
# then ramp once observability shows AI stops nudging sales after a
# handoff request.
HUMAN_PRIORITY_MODE_ENABLED = (
    os.environ.get("NAHLA_AI_HUMAN_PRIORITY_MODE", "0").strip().lower()
    in ("1", "true", "yes", "on")
)

# How recently a manual outbound counts as "the human is actively
# typing on this conversation right now". Used by
# ``_is_human_actually_active`` to flip from human_priority (AI replies
# with restrictions) back to the legacy hard-stop (AI fully silent).
_HUMAN_ACTIVE_RECENT_MANUAL_SEC = int(
    os.environ.get("NAHLA_HUMAN_ACTIVE_RECENT_MANUAL_SEC", "60")
)

VALID_REASONS = frozenset(
    {
        REASON_MANUAL,
        REASON_MANUAL_PAUSE,
        REASON_HUMAN_HANDOFF,
        REASON_BOT_LOOP,
        REASON_RATE_LIMIT,
        REASON_INTERNAL_NUMBER,
        REASON_MANUAL_TAKEOVER,
        REASON_SUPPORT_ESCALATION,
    }
)

# Reasons that imply "a human is on this conversation now". Used by the
# inbox to populate the unified "بشري" filter regardless of how the
# pause was set (dashboard takeover button, escalation flow, etc.).
#
# IMPORTANT: ``REASON_MANUAL`` and ``REASON_MANUAL_PAUSE`` are deliberately
# NOT in this set. Pausing AI alone is *not* a human takeover — the
# merchant is just silencing the bot temporarily. The human-filter is
# now driven by the explicit ``needs_human`` / ``handoff_active`` /
# ``taken_over_at`` columns on Conversation, NOT by the pause reason.
HUMAN_PRESENCE_REASONS = frozenset(
    {REASON_HUMAN_HANDOFF, REASON_MANUAL_TAKEOVER, REASON_SUPPORT_ESCALATION}
)


# ────────────────────────────────────────────────────────────────────────────
# Loop-score thresholds. Tuned so a noisy customer with mixed messages
# never trips by chance — only sustained repetition does.
# ────────────────────────────────────────────────────────────────────────────
LOOP_SCORE_RECOVERY = int(os.environ.get("NAHLA_AI_LOOP_RECOVERY", "4"))
LOOP_SCORE_PAUSE    = int(os.environ.get("NAHLA_AI_LOOP_PAUSE",    "6"))

# Soft daily ceiling — kept ONLY as a circuit-breaker against a
# pathological runaway. Exceeding it does not pause AI by itself; it
# just adds 1 to the loop score so a conversation already showing other
# signs of being a loop crosses the pause threshold sooner.
SOFT_DAILY_REPLY_HINT = int(os.environ.get("NAHLA_AI_SOFT_DAILY_HINT", "100"))

# Similarity threshold for "the assistant just said almost the same
# thing again". 0.0 = different, 1.0 = identical.
SIMILARITY_HIGH = float(os.environ.get("NAHLA_AI_SIMILARITY_HIGH", "0.85"))

# How long the loop-state cache for a (tenant, convo) survives without
# new traffic before being trimmed.
LOOP_STATE_TTL_SEC = int(os.environ.get("NAHLA_AI_LOOP_STATE_TTL_SEC", "1800"))

# Recovery cooldown — once we sent a recovery line, don't send another
# one for this many seconds. Prevents the recovery itself from looping.
LOOP_RECOVERY_COOLDOWN_SEC = int(os.environ.get("NAHLA_AI_LOOP_RECOVERY_COOLDOWN_SEC", "900"))


# ────────────────────────────────────────────────────────────────────────────
# Bot-phrase detector (Arabic + English).
# ────────────────────────────────────────────────────────────────────────────
_BOT_PHRASES_AR = [
    "تم تحويل المحادثة",
    "سيرد عليك أحد الموظفين",
    "هل يمكنك توضيح طلبك",
    "ما الذي تود مساعدتي فيه",
    "ما الذي تريد مساعدتي فيه",
    "أنا مساعد ذكي",
    "انا مساعد ذكي",
    "وصلت رسالتك",
    "لا تتردد في إخباري",
    "كيف يمكنني مساعدتك",
    "كيف أستطيع مساعدتك",
]
_BOT_PHRASES_EN = [
    "i am an ai",
    "i'm an ai",
    "i am a chatbot",
    "how can i help you",
    "your message has been received",
    "we will get back to you",
    "please clarify",
]
_BOT_PHRASE_RE = re.compile(
    "|".join(re.escape(p) for p in _BOT_PHRASES_AR + _BOT_PHRASES_EN),
    re.IGNORECASE,
)

# Sales-intent signals — when present in inbound text the guard
# DECREASES the loop score. A customer asking "كم سعرها؟" / "وش الفرق
# بينهم؟" / "كم الشحن؟" is the opposite of a loop.
_INTENT_KEYWORDS_AR = [
    "سعر", "كم", "ثمن", "اطلب", "أطلب", "اشتري", "أشتري", "اشترِ",
    "توصيل", "شحن", "متوفر", "متى", "خصم", "كوبون", "متجر", "منتج",
    "حجم", "لون", "مقاس", "مقاسات", "طلبي", "رقم الطلب", "تتبع",
    "مواصفات", "فوائد", "الفرق", "افضل", "أفضل", "ضمان", "ضمانة",
    "تجربة", "وش الفرق", "وش الفايدة", "ايش الفرق", "كم سعرها",
    "هل يوجد", "متوفرة", "موجود", "كيف اطلب", "كيف أطلب",
]
_INTENT_KEYWORDS_EN = [
    "price", "how much", "buy", "order", "shipping", "delivery", "available",
    "discount", "coupon", "size", "color", "tracking", "specs", "benefits",
    "difference", "compare", "warranty", "review", "review",
]
_INTENT_RE = re.compile(
    "|".join(re.escape(k) for k in _INTENT_KEYWORDS_AR + _INTENT_KEYWORDS_EN),
    re.IGNORECASE,
)

# Generic short / non-substantive customer replies. By themselves they
# never pause AI, but a sequence of them with no other signal hints at
# a bot exchanging pleasantries.
_GENERIC_SHORT_RE = re.compile(
    r"^\s*(شكر[اًا]?|طيب|اوكي|تمام|اوك|ok|okay|thanks?|thank\s+you|"
    r"كويس|زين|ممتاز|جميل|tamam|nice)\b",
    re.IGNORECASE,
)


# ────────────────────────────────────────────────────────────────────────────
# Internal numbers (env-driven). Comma-separated, digits-only after
# normalization. Anything in this list is treated as `internal_number`.
# ────────────────────────────────────────────────────────────────────────────
def _digits(value: Any) -> str:
    """Return only the digits in *value* (so '+966 50…' → '96650…')."""
    if value is None:
        return ""
    return re.sub(r"\D+", "", str(value))


def _internal_numbers_from_env() -> set[str]:
    raw = os.environ.get("NAHLA_INTERNAL_NUMBERS", "") or ""
    out: set[str] = set()
    for chunk in raw.replace(";", ",").split(","):
        digits = _digits(chunk)
        if digits:
            out.add(digits)
    return out


_INTERNAL_NUMBERS_CACHE = _internal_numbers_from_env()


def _phone_matches(haystack: Iterable[str], needle: str) -> bool:
    """Match by full digits or by 9-digit suffix (Saudi mobile suffix)."""
    if not needle:
        return False
    suffix = needle[-9:] if len(needle) >= 9 else needle
    for entry in haystack:
        d = _digits(entry)
        if not d:
            continue
        if d == needle or d.endswith(suffix):
            return True
    return False


def _tenant_blocked_numbers(db: Session, tenant_id: int) -> list[str]:
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    except Exception:
        return []
    if not tenant:
        return []
    raw = getattr(tenant, "ai_blocked_numbers", None) or []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


def is_internal_or_blocked(db: Session, tenant_id: int, phone: str) -> tuple[bool, str | None]:
    """Returns ``(matched, reason)`` where reason is the block source."""
    norm = _digits(phone)
    if not norm:
        return False, None
    if _phone_matches(_INTERNAL_NUMBERS_CACHE, norm):
        return True, REASON_INTERNAL_NUMBER
    blocked = _tenant_blocked_numbers(db, tenant_id)
    if _phone_matches(blocked, norm):
        return True, REASON_INTERNAL_NUMBER
    return False, None


# ────────────────────────────────────────────────────────────────────────────
# Pause / resume helpers
# ────────────────────────────────────────────────────────────────────────────
def pause_ai(
    db: Session,
    convo: Conversation,
    *,
    reason: str,
    by: str = "system",
    commit: bool = True,
) -> None:
    if reason not in VALID_REASONS:
        reason = REASON_MANUAL
    if convo.ai_paused and convo.ai_paused_reason == reason:
        return
    convo.ai_paused = True
    convo.ai_paused_reason = reason
    convo.ai_paused_at = datetime.now(timezone.utc)
    convo.ai_paused_by = by
    db.add(convo)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception as exc:
        logger.warning("[ai_pause] failed to persist pause state: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    logger.info(
        "[ai_pause] PAUSED convo=%s tenant=%s reason=%s by=%s",
        convo.id, convo.tenant_id, reason, by,
    )


def resume_ai(
    db: Session,
    convo: Conversation,
    *,
    by: str = "manual",
    commit: bool = True,
) -> None:
    if not convo.ai_paused and not convo.ai_paused_reason:
        return
    convo.ai_paused = False
    convo.ai_paused_reason = None
    convo.ai_paused_at = None
    convo.ai_paused_by = by
    db.add(convo)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception as exc:
        logger.warning("[ai_pause] failed to persist resume state: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    # Reset in-memory loop / counter state too.
    _reset_loop_state(convo.tenant_id, convo.id)
    _reset_rate_state(convo.tenant_id, convo.id)
    logger.info("[ai_pause] RESUMED convo=%s tenant=%s by=%s", convo.id, convo.tenant_id, by)


# ────────────────────────────────────────────────────────────────────────────
# Reply-rate tracking (used for soft signals + recovery cooldown only).
# Exists so we can log how busy a conversation is, but never auto-pauses
# based on counts alone.
# ────────────────────────────────────────────────────────────────────────────
_LOCK = threading.Lock()
_REPLY_TIMES: dict[tuple[int, int], list[float]] = {}
_INBOUND_TIMES: dict[tuple[int, int], list[float]] = {}

_DAY_SEC = 86400.0
_TEN_MIN_SEC = 600.0


def _reset_rate_state(tenant_id: int | None, convo_id: int | None) -> None:
    if tenant_id is None or convo_id is None:
        return
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        _REPLY_TIMES.pop(key, None)
        _INBOUND_TIMES.pop(key, None)


def _trim(buf: list[float], horizon_sec: float, now: float) -> list[float]:
    cutoff = now - horizon_sec
    return [t for t in buf if t >= cutoff]


def _record_inbound(tenant_id: int, convo_id: int) -> None:
    now = time.monotonic()
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        buf = _trim(_INBOUND_TIMES.get(key, []), _DAY_SEC, now)
        buf.append(now)
        _INBOUND_TIMES[key] = buf


def _record_reply(tenant_id: int, convo_id: int) -> None:
    now = time.monotonic()
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        buf = _trim(_REPLY_TIMES.get(key, []), _DAY_SEC, now)
        buf.append(now)
        _REPLY_TIMES[key] = buf


def _reply_counts(tenant_id: int, convo_id: int) -> tuple[int, int]:
    now = time.monotonic()
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        buf = _trim(_REPLY_TIMES.get(key, []), _DAY_SEC, now)
        _REPLY_TIMES[key] = buf
        ten_min = sum(1 for t in buf if t >= now - _TEN_MIN_SEC)
        day = len(buf)
    return ten_min, day


# ────────────────────────────────────────────────────────────────────────────
# Loop-score state. Per-process per-(tenant, convo).
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class _LoopMemory:
    score: int = 0
    last_assistant_replies: deque = field(default_factory=lambda: deque(maxlen=4))
    last_inbound_short_streak: int = 0
    recovery_sent_at_monotonic: float = 0.0
    last_touch: float = field(default_factory=time.monotonic)
    last_intent_seen_at: float = 0.0
    repeat_strikes: int = 0  # consecutive turns where similarity stayed high


_LOOP_STATE: dict[tuple[int, int], _LoopMemory] = {}


def _reset_loop_state(tenant_id: int | None, convo_id: int | None) -> None:
    if tenant_id is None or convo_id is None:
        return
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        _LOOP_STATE.pop(key, None)


def _gc_loop_state(now: float | None = None) -> None:
    """Drop loop-state entries that have been idle past LOOP_STATE_TTL_SEC."""
    cutoff = (now or time.monotonic()) - LOOP_STATE_TTL_SEC
    with _LOCK:
        stale = [k for k, v in _LOOP_STATE.items() if v.last_touch < cutoff]
        for k in stale:
            _LOOP_STATE.pop(k, None)


def _get_loop_memory(tenant_id: int, convo_id: int) -> _LoopMemory:
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        mem = _LOOP_STATE.get(key)
        if mem is None:
            mem = _LoopMemory()
            _LOOP_STATE[key] = mem
        mem.last_touch = time.monotonic()
        return mem


# ────────────────────────────────────────────────────────────────────────────
# Helpers for similarity / classification
# ────────────────────────────────────────────────────────────────────────────
def _looks_automated(text: str | None) -> bool:
    if not text:
        return False
    return bool(_BOT_PHRASE_RE.search(text))


def _has_sales_intent(text: str | None) -> bool:
    if not text:
        return False
    return bool(_INTENT_RE.search(text))


def _is_generic_short(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) > 30:
        return False
    return bool(_GENERIC_SHORT_RE.match(stripped))


def _similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()


def _max_similarity(candidate: str, prev_replies: Iterable[str]) -> float:
    best = 0.0
    for p in prev_replies:
        ratio = _similarity(candidate, p)
        if ratio > best:
            best = ratio
    return best


# ────────────────────────────────────────────────────────────────────────────
# LoopDecision — returned by `evaluate_loop_pre_send`
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class LoopDecision:
    action: str          # 'continue' | 'recovery' | 'pause'
    score: int
    reason: str          # short tag for logs
    similarity: float    # max similarity vs recent assistant replies
    recovery_text: str | None = None


# Recovery message — kept warm and store-agnostic so we don't need
# tenant-specific context to render it. Sent ONCE per recovery window.
RECOVERY_TEXT_AR = (
    "أحس أني كرّرت نفس الإجابة 🐝\n"
    "خلني أحاول أوضح بطريقة مختلفة — وش بالضبط تحب أعرفك عليه أو "
    "أساعدك فيه الحين؟"
)


def _build_recovery_text() -> str:
    return RECOVERY_TEXT_AR


# ────────────────────────────────────────────────────────────────────────────
# Resume marker (persisted in convo.extra_metadata.ai_resumed_at)
# ────────────────────────────────────────────────────────────────────────────
def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _convo_resumed_at(convo: Conversation | None) -> datetime | None:
    if convo is None:
        return None
    meta = getattr(convo, "extra_metadata", None) or {}
    raw = meta.get("ai_resumed_at") if isinstance(meta, dict) else None
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        return _to_utc(dt)
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────────────
# Public: should_skip_ai (top-of-funnel) — minimal, never count-based.
# ────────────────────────────────────────────────────────────────────────────
def should_skip_ai(
    db: Session,
    convo: Conversation,
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str | None,
) -> tuple[bool, str | None]:
    """
    Pre-LLM gate. Returns ``(skip, reason)``.

    Skips ONLY when:
      1. The conversation is already paused (any reason).
      2. The phone is internal / on the merchant blocklist.
      3. The customer side itself is sending automated-looking
         messages multiple times in a row (real AI-to-AI scenario).

    NEVER skips based on message counts, conversation length, or burst
    rate — those signals are noisy and would kill long natural sales
    conversations. Repetition / progress checks happen pre-send via
    `evaluate_loop_pre_send`.
    """
    if convo and convo.id is not None:
        _record_inbound(int(tenant_id), int(convo.id))
        # Track whether a sales intent appeared recently for the
        # pre-send loop scorer.
        mem = _get_loop_memory(int(tenant_id), int(convo.id))
        if _has_sales_intent(inbound_text):
            mem.last_intent_seen_at = time.monotonic()
            mem.last_inbound_short_streak = 0
        elif _is_generic_short(inbound_text):
            mem.last_inbound_short_streak += 1
        else:
            # Substantive question that isn't on the keyword list still
            # counts as progress for streak purposes.
            mem.last_inbound_short_streak = 0

    # 1) Conversation already paused.
    if convo is not None and getattr(convo, "ai_paused", False):
        reason = getattr(convo, "ai_paused_reason", None) or REASON_MANUAL

        # ── Human-Priority Mode (env-flagged) ────────────────────────────
        # When the pause is a "customer asked for human" handoff AND
        # no staff has actually engaged yet AND the feature flag is on,
        # we DON'T fully silence the AI. Instead we return the synthetic
        # ``"human_priority"`` reason so the webhook can keep the brain
        # active in a clamped capacity (no sales push, no payment link,
        # no upsell — just answer questions + reassure that the team
        # is on the way). The moment a human starts replying or hits
        # the takeover button, ``_is_human_actually_active`` returns
        # True and we fall through to the hard-stop below — exactly the
        # legacy behaviour from that point on.
        if (
            HUMAN_PRIORITY_MODE_ENABLED
            and reason in HUMAN_PRESENCE_REASONS
            and not _is_human_actually_active(db, convo)
        ):
            logger.info(
                "[HUMAN_PRIORITY] gate=allow convo=%s tenant=%s phone=%s "
                "pause_reason=%s — letting brain reply in restricted mode",
                convo.id, tenant_id, customer_phone, reason,
            )
            return False, REASON_HUMAN_PRIORITY

        logger.info(
            "[AI_GUARD] skip reason=%s source=already_paused convo=%s tenant=%s phone=%s",
            reason, convo.id, tenant_id, customer_phone,
        )
        return True, reason

    # 2) Internal / merchant blocked numbers.
    blocked, b_reason = is_internal_or_blocked(db, tenant_id, customer_phone)
    if blocked:
        if convo is not None:
            pause_ai(db, convo, reason=b_reason or REASON_INTERNAL_NUMBER, by="system:internal_number")
        logger.info(
            "[AI_GUARD] skip reason=%s source=blocklist convo=%s tenant=%s phone=%s",
            b_reason or REASON_INTERNAL_NUMBER,
            getattr(convo, "id", None), tenant_id, customer_phone,
        )
        return True, b_reason or REASON_INTERNAL_NUMBER

    # 3) Customer side appears automated — only trip if 2+ inbound
    # messages in a row look automated AND none of them carries sales
    # intent. Honours the manual-resume marker.
    resumed_at = _convo_resumed_at(convo)
    if convo is not None and convo.id is not None and _looks_automated(inbound_text):
        prev_count_automated = _count_recent_automated_inbound(
            db, int(tenant_id), int(convo.id), since=resumed_at,
        )
        if prev_count_automated >= 1:
            pause_ai(db, convo, reason=REASON_BOT_LOOP, by="system:customer_side_bot")
            logger.info(
                "[AI_GUARD] skip reason=%s source=customer_side_bot convo=%s tenant=%s phone=%s "
                "automated_inbound_streak=%d",
                REASON_BOT_LOOP, convo.id, tenant_id, customer_phone, prev_count_automated + 1,
            )
            return True, REASON_BOT_LOOP

    return False, None


def _is_human_actually_active(
    db: Session,
    convo: Conversation,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True when a human is unmistakably engaged on this convo NOW.

    Two evidence sources, ORed:

      1. ``Conversation.taken_over_at`` is set — the merchant clicked
         "استلام" or hit ``/handoff`` so we already know they own this
         thread regardless of whether they've typed yet.
      2. The most recent outbound MessageEvent within the last
         :data:`_HUMAN_ACTIVE_RECENT_MANUAL_SEC` seconds is a manual
         reply (``event_type='manual_reply'`` or ``extra_metadata.is_ai
         IS False``). This catches the natural flow where staff just
         starts typing without clicking the takeover button first.

    Falling back: any DB error returns False so the caller treats the
    conversation as "human still pending, AI may keep helping" — that's
    the safer mistake direction for the human-priority mode (worst case
    AI sends one polite line during the few seconds before we re-classify).
    """
    if convo is None:
        return False
    if getattr(convo, "taken_over_at", None) is not None:
        return True

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=_HUMAN_ACTIVE_RECENT_MANUAL_SEC)
    try:
        last_out = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id       == convo.tenant_id,
                MessageEvent.conversation_id == convo.id,
                MessageEvent.direction       == "outbound",
            )
            .order_by(MessageEvent.id.desc())
            .first()
        )
    except Exception:
        return False
    if not last_out:
        return False
    ts = getattr(last_out, "created_at", None)
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts < cutoff:
        return False
    # A recent outbound — is it manual? Two independent hints because
    # different code paths populate the metadata differently:
    #   * ``event_type='manual_reply'`` is the canonical marker emitted
    #     by ``/conversations/reply``.
    #   * Older / non-router send paths may only flip
    #     ``extra_metadata.is_ai=False`` without changing event_type.
    if (last_out.event_type or "").startswith("manual"):
        return True
    meta = last_out.extra_metadata or {}
    if isinstance(meta, dict) and meta.get("is_ai") is False:
        return True
    return False


def _count_recent_automated_inbound(
    db: Session,
    tenant_id: int,
    convo_id: int,
    *,
    since: datetime | None = None,
    limit: int = 6,
) -> int:
    """How many of the most recent inbound messages look automated."""
    try:
        rows = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.conversation_id == convo_id,
                MessageEvent.direction == "inbound",
            )
            .order_by(MessageEvent.id.desc())
            .limit(limit)
            .all()
        )
    except Exception as exc:
        logger.debug("[ai_pause] history fetch failed: %s", exc)
        return 0
    if since is not None:
        rows = [r for r in rows if r.created_at and _to_utc(r.created_at) > since]
    n = 0
    for r in rows:
        if _looks_automated((r.body or "").strip()):
            n += 1
        else:
            break  # streak only — first non-automated breaks the chain
    return n


# ────────────────────────────────────────────────────────────────────────────
# Public: evaluate_loop_pre_send — call AFTER the Brain produced a
# candidate reply but BEFORE we hand it to the WhatsApp send.
# ────────────────────────────────────────────────────────────────────────────
def evaluate_loop_pre_send(
    db: Session,
    convo: Conversation | None,
    *,
    tenant_id: int,
    candidate_reply: str,
    inbound_text: str | None,
    checkout_active: bool = False,
    checkout_recovery_reply: str | None = None,
) -> LoopDecision:
    """Score the (history, inbound, candidate) triple for repetition.

    This is the ONLY place where the assistant gets paused for
    "loop / repetition" reasons. It always lets the conversation
    continue on the first signs of repetition (replaces the reply with
    a recovery line) and only escalates to a hard pause if repetition
    persists despite the recovery.
    """
    _gc_loop_state()
    candidate = (candidate_reply or "").strip()
    if not candidate or convo is None or convo.id is None:
        return LoopDecision(action="continue", score=0, reason="no_candidate", similarity=0.0)

    mem = _get_loop_memory(int(tenant_id), int(convo.id))

    # Pull last 3 outbound bodies from cached deque, falling back to DB
    # on cold cache so a process restart doesn't reset the window.
    history_replies: list[str] = list(mem.last_assistant_replies)
    if not history_replies:
        try:
            rows = (
                db.query(MessageEvent)
                .filter(
                    MessageEvent.tenant_id == tenant_id,
                    MessageEvent.conversation_id == convo.id,
                    MessageEvent.direction == "outbound",
                )
                .order_by(MessageEvent.id.desc())
                .limit(3)
                .all()
            )
            history_replies = [(r.body or "").strip() for r in rows if (r.body or "").strip()]
        except Exception as exc:
            logger.debug("[ai_pause] outbound history fetch failed: %s", exc)
            history_replies = []
        for r in reversed(history_replies):
            mem.last_assistant_replies.append(r)

    similarity = _max_similarity(candidate, history_replies[:3])

    # ── Score adjustment ────────────────────────────────────────────────
    score = mem.score

    progressed = False
    progress_reasons: list[str] = []
    decay = 0
    if checkout_active:
        try:
            from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: PLC0415
                is_checkout_continue_inbound,
            )

            if is_checkout_continue_inbound(inbound_text or ""):
                decay += 2
                progressed = True
                progress_reasons.append("checkout_continue")
        except Exception:  # noqa: BLE001
            pass
    if _has_sales_intent(inbound_text):
        decay += 2
        progressed = True
        progress_reasons.append("sales_intent")
    elif inbound_text and not _is_generic_short(inbound_text) and len(inbound_text.strip()) > 12:
        # Substantive non-generic message — treat as progress even if it
        # didn't match the keyword list (e.g. brand names, free-form
        # comparisons).
        decay += 1
        progressed = True
        progress_reasons.append("substantive_inbound")

    score = max(0, score - decay)

    increments: list[str] = []
    # Per spec: similarity-driven score increases ONLY when the
    # customer side is NOT making progress. A customer asking new
    # product / price / shipping questions earns the AI another shot
    # even if the brain happens to phrase the answer similarly.
    if similarity >= SIMILARITY_HIGH and not progressed:
        score += 3
        mem.repeat_strikes += 1
        increments.append(f"similarity={similarity:.2f}")
    elif similarity >= SIMILARITY_HIGH and progressed:
        # Keep the strike counter accurate but don't bump the score —
        # the customer is moving the conversation forward.
        mem.repeat_strikes = max(0, mem.repeat_strikes)
        increments.append(f"similarity={similarity:.2f}_progressed")
    else:
        mem.repeat_strikes = 0

    if _looks_automated(candidate) and not progressed:
        # Only count repeated automated patterns as a problem if the
        # previous outbound was also automated-looking AND the
        # customer is not driving the conversation forward.
        if any(_looks_automated(p) for p in history_replies[:2]):
            score += 2
            increments.append("automated_outbound_repeat")

    if mem.last_inbound_short_streak >= 3 and not progressed:
        score += 1
        increments.append(f"generic_short_streak={mem.last_inbound_short_streak}")

    # Soft daily-volume hint: only adds to score if other signals are
    # already firing. Never causes a pause on its own.
    _, day = _reply_counts(int(tenant_id), int(convo.id))
    if day >= SOFT_DAILY_REPLY_HINT and score >= 2:
        score += 1
        increments.append(f"soft_daily={day}")

    mem.score = score
    log_score = score

    # Slot-progress observability (best-effort proxy: a sales intent or
    # substantive message is treated as forward progress for the turn).
    logger.info(
        "[SLOT_PROGRESS] tenant=%s convo=%s progressed=%s reasons=%s",
        tenant_id, convo.id, str(progressed).lower(), progress_reasons or ["none"],
    )

    # ── Decision ────────────────────────────────────────────────────────
    now = time.monotonic()
    cooldown_active = (
        mem.recovery_sent_at_monotonic > 0
        and (now - mem.recovery_sent_at_monotonic) < LOOP_RECOVERY_COOLDOWN_SEC
    )

    # Hard pause only if score crossed the pause threshold AND we
    # already attempted recovery in this episode.
    if log_score >= LOOP_SCORE_PAUSE and cooldown_active:
        reason_tag = "+".join(increments) or "score_threshold"
        logger.info(
            "[LOOP_GUARD] tenant=%s convo=%s score=%d similarity=%.2f reason=%s "
            "action=pause repeat_strikes=%d",
            tenant_id, convo.id, log_score, similarity, reason_tag, mem.repeat_strikes,
        )
        logger.info(
            "[DEDUP_GUARD] tenant=%s convo=%s similarity=%.2f action=block",
            tenant_id, convo.id, similarity,
        )
        return LoopDecision(
            action="pause",
            score=log_score,
            reason=reason_tag,
            similarity=similarity,
        )

    # Recovery: score crossed the recovery threshold OR similarity is
    # very high (and customer is NOT making progress). Only one
    # recovery message per cooldown window.
    similarity_trigger = similarity >= SIMILARITY_HIGH and not progressed
    if (log_score >= LOOP_SCORE_RECOVERY or similarity_trigger) and not cooldown_active:
        reason_tag = "+".join(increments) or ("similarity_high" if similarity >= SIMILARITY_HIGH else "score")
        recovery_text = _build_recovery_text()
        if checkout_active:
            slot_reply = (checkout_recovery_reply or "").strip()
            if slot_reply:
                recovery_text = slot_reply
            elif candidate:
                recovery_text = candidate
        logger.info(
            "[LOOP_GUARD] tenant=%s convo=%s score=%d similarity=%.2f reason=%s "
            "action=recovery repeat_strikes=%d",
            tenant_id, convo.id, log_score, similarity, reason_tag, mem.repeat_strikes,
        )
        logger.info(
            "[DEDUP_GUARD] tenant=%s convo=%s similarity=%.2f action=replace_with_recovery",
            tenant_id, convo.id, similarity,
        )
        return LoopDecision(
            action="recovery",
            score=log_score,
            reason=reason_tag,
            similarity=similarity,
            recovery_text=recovery_text,
        )

    # Continue — log compactly so we can see the score evolve.
    logger.info(
        "[LOOP_GUARD] tenant=%s convo=%s score=%d similarity=%.2f reason=%s action=continue "
        "repeat_strikes=%d cooldown=%s",
        tenant_id, convo.id, log_score, similarity,
        "+".join(increments) or "ok",
        mem.repeat_strikes,
        "active" if cooldown_active else "none",
    )
    if similarity > 0:
        logger.info(
            "[DEDUP_GUARD] tenant=%s convo=%s similarity=%.2f action=allow",
            tenant_id, convo.id, similarity,
        )
    return LoopDecision(
        action="continue",
        score=log_score,
        reason="ok",
        similarity=similarity,
    )


def note_recovery_sent(
    tenant_id: int,
    convo_id: int,
    *,
    recovery_text: str,
) -> None:
    """Mark that the merchant handler emitted the recovery line so the
    next turn can decide whether to hard-pause if the loop persists."""
    if tenant_id is None or convo_id is None:
        return
    mem = _get_loop_memory(int(tenant_id), int(convo_id))
    mem.recovery_sent_at_monotonic = time.monotonic()
    mem.last_assistant_replies.append(recovery_text.strip())


def note_assistant_reply(
    tenant_id: int,
    convo_id: int,
    *,
    reply_text: str,
) -> None:
    """Cache the just-sent assistant reply for future similarity scoring."""
    if tenant_id is None or convo_id is None:
        return
    if not (reply_text or "").strip():
        return
    mem = _get_loop_memory(int(tenant_id), int(convo_id))
    mem.last_assistant_replies.append(reply_text.strip())


# ────────────────────────────────────────────────────────────────────────────
# Public: after_ai_reply — counts a reply for observability.
# Never auto-pauses on counts alone. The hard pause path runs through
# `evaluate_loop_pre_send` (similarity-based) instead.
# ────────────────────────────────────────────────────────────────────────────
def after_ai_reply(
    db: Session,
    convo: Conversation,
    *,
    tenant_id: int,
    reply_text: str | None = None,
) -> None:
    if not convo or convo.id is None or tenant_id is None:
        return
    _record_reply(int(tenant_id), int(convo.id))
    if reply_text:
        note_assistant_reply(int(tenant_id), int(convo.id), reply_text=reply_text)


# ────────────────────────────────────────────────────────────────────────────
# Tenant blocklist mutation helpers (used by the API endpoints)
# ────────────────────────────────────────────────────────────────────────────
def add_blocked_number(db: Session, tenant_id: int, phone: str) -> list[str]:
    norm = _digits(phone)
    if not norm:
        return _tenant_blocked_numbers(db, tenant_id)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return []
    current = list(getattr(tenant, "ai_blocked_numbers", None) or [])
    current_norm = {_digits(x) for x in current}
    if norm not in current_norm:
        current.append(norm)
        tenant.ai_blocked_numbers = current
        db.add(tenant)
        db.commit()
    return list(getattr(tenant, "ai_blocked_numbers", None) or [])


def remove_blocked_number(db: Session, tenant_id: int, phone: str) -> list[str]:
    norm = _digits(phone)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return []
    current = list(getattr(tenant, "ai_blocked_numbers", None) or [])
    new = [x for x in current if _digits(x) != norm]
    if len(new) != len(current):
        tenant.ai_blocked_numbers = new
        db.add(tenant)
        db.commit()
    return list(getattr(tenant, "ai_blocked_numbers", None) or [])


def list_blocked_numbers(db: Session, tenant_id: int) -> list[str]:
    return _tenant_blocked_numbers(db, tenant_id)


# ────────────────────────────────────────────────────────────────────────────
# Backwards-compat: detect_bot_loop is still referenced by older callers.
# Kept intentionally permissive — only flags an obvious customer-side
# automated streak. Never reports based on outbound repetition (that
# moved to evaluate_loop_pre_send).
# ────────────────────────────────────────────────────────────────────────────
def detect_bot_loop(
    db: Session,
    tenant_id: int,
    convo_id: int,
    inbound_text: str | None,
    *,
    since: datetime | None = None,
) -> bool:
    if _looks_automated(inbound_text):
        return _count_recent_automated_inbound(db, tenant_id, convo_id, since=since) >= 1
    return False


__all__ = [
    "REASON_MANUAL",
    "REASON_MANUAL_PAUSE",
    "REASON_HUMAN_HANDOFF",
    "REASON_BOT_LOOP",
    "REASON_RATE_LIMIT",
    "REASON_INTERNAL_NUMBER",
    "REASON_MANUAL_TAKEOVER",
    "REASON_SUPPORT_ESCALATION",
    "REASON_HUMAN_PRIORITY",
    "HUMAN_PRIORITY_MODE_ENABLED",
    "HUMAN_PRESENCE_REASONS",
    "VALID_REASONS",
    "_is_human_actually_active",
    "LOOP_SCORE_RECOVERY",
    "LOOP_SCORE_PAUSE",
    "SIMILARITY_HIGH",
    "RECOVERY_TEXT_AR",
    "LoopDecision",
    "should_skip_ai",
    "evaluate_loop_pre_send",
    "note_recovery_sent",
    "note_assistant_reply",
    "after_ai_reply",
    "pause_ai",
    "resume_ai",
    "is_internal_or_blocked",
    "detect_bot_loop",
    "add_blocked_number",
    "remove_blocked_number",
    "list_blocked_numbers",
]
