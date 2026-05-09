"""
core/ai_pause_guard
───────────────────
Loop / cost guard that runs BEFORE any LLM call.

Responsibilities
----------------
1. Maintain `Conversation.ai_paused*` state (set / clear / reasons).
2. Detect AI-to-AI loops:
   * inbound from internal/blocked numbers (Nahla / Shawahid / staff / merchant blocklist)
   * tell-tale automated phrases in recent history
   * rapid back-and-forth without a clear sales intent
3. Enforce per-contact rate limits on AI replies (10-min and per-day).
4. Provide a single `should_skip_ai(...)` function used at the top of the
   merchant message handler, so we save the inbound message but never
   spend tokens / send replies on a paused conversation.

Design constraints
------------------
* Pure helper — no FastAPI imports.
* Memory-only sliding windows for the rate limiter (per-process); the
  `ai_paused` flag persisted on the conversation row is the durable
  enforcement mechanism. Once a contact trips the rate limit we set
  `ai_paused=true`, so even after a process restart the cap holds until
  a human resumes the AI.
* All checks are cheap and exception-safe — they must never break a
  legitimate conversation.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from models import Conversation, MessageEvent, Tenant

logger = logging.getLogger("nahla-backend")


# ────────────────────────────────────────────────────────────────────────────
# Reasons (mirror the values documented in the migration & UI)
# ────────────────────────────────────────────────────────────────────────────
REASON_MANUAL = "manual"
REASON_HUMAN_HANDOFF = "human_handoff"
REASON_BOT_LOOP = "bot_loop_detected"
REASON_RATE_LIMIT = "rate_limit"
REASON_INTERNAL_NUMBER = "internal_number"

VALID_REASONS = frozenset(
    {REASON_MANUAL, REASON_HUMAN_HANDOFF, REASON_BOT_LOOP, REASON_RATE_LIMIT, REASON_INTERNAL_NUMBER}
)


# ────────────────────────────────────────────────────────────────────────────
# Rate-limit settings (overridable via env)
# ────────────────────────────────────────────────────────────────────────────
MAX_AI_REPLIES_PER_CONTACT_10MIN = int(
    os.environ.get("NAHLA_MAX_AI_REPLIES_PER_CONTACT_10MIN", "5")
)
MAX_AI_REPLIES_PER_CONTACT_DAY = int(
    os.environ.get("NAHLA_MAX_AI_REPLIES_PER_CONTACT_DAY", "20")
)

# Quick-burst detector: if the same contact sends >N inbounds within
# WINDOW seconds AND the system has already replied >M times without a
# clear sales intent, pause AI.
RAPID_BURST_INBOUND_LIMIT = int(os.environ.get("NAHLA_AI_BURST_INBOUND", "3"))
RAPID_BURST_REPLY_LIMIT = int(os.environ.get("NAHLA_AI_BURST_REPLY", "3"))
RAPID_BURST_WINDOW_SEC = int(os.environ.get("NAHLA_AI_BURST_WINDOW_SEC", "60"))


# ────────────────────────────────────────────────────────────────────────────
# Bot-loop phrase detector (Arabic + English).
#
# These are tell-tale phrases produced by *automated* assistants — both
# ours and other vendors'. If the *recent inbound history* from the
# customer side contains them (i.e., the "customer" is in fact another
# bot), or our own outbound side keeps producing the same generic
# fallback over and over, we trip the loop guard.
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

# Sales-intent signals — if the inbound history contains these we keep
# the AI active even when other heuristics would fire. Customers asking
# about price / shipping / order status are NOT a loop.
_INTENT_KEYWORDS_AR = [
    "سعر", "كم", "ثمن", "اطلب", "أطلب", "اشتري", "أشتري", "اشترِ",
    "توصيل", "شحن", "متوفر", "متى", "خصم", "كوبون", "متجر", "منتج",
    "حجم", "لون", "مقاس", "طلبي", "رقم الطلب",
]
_INTENT_KEYWORDS_EN = [
    "price", "how much", "buy", "order", "shipping", "delivery", "available",
    "discount", "coupon", "size", "color", "tracking",
]
_INTENT_RE = re.compile(
    "|".join(re.escape(k) for k in _INTENT_KEYWORDS_AR + _INTENT_KEYWORDS_EN),
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
    # Reset in-memory rate / burst counters too.
    _reset_rate_state(convo.tenant_id, convo.id)
    logger.info("[ai_pause] RESUMED convo=%s tenant=%s by=%s", convo.id, convo.tenant_id, by)


# ────────────────────────────────────────────────────────────────────────────
# Rate-limit & burst tracking (per-process, thread-safe sliding windows)
# Keyed by (tenant_id, conversation_id).
# ────────────────────────────────────────────────────────────────────────────
_LOCK = threading.Lock()
# Per-key list of monotonic seconds when an AI reply was emitted.
_REPLY_TIMES: dict[tuple[int, int], list[float]] = {}
# Per-key list of monotonic seconds for inbound messages.
_INBOUND_TIMES: dict[tuple[int, int], list[float]] = {}
# Per-key bool: did we observe a clear sales intent in this window?
_INTENT_SEEN: dict[tuple[int, int], float] = {}

_DAY_SEC = 86400.0
_TEN_MIN_SEC = 600.0


def _reset_rate_state(tenant_id: int | None, convo_id: int | None) -> None:
    if tenant_id is None or convo_id is None:
        return
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        _REPLY_TIMES.pop(key, None)
        _INBOUND_TIMES.pop(key, None)
        _INTENT_SEEN.pop(key, None)


def _trim(buf: list[float], horizon_sec: float, now: float) -> list[float]:
    cutoff = now - horizon_sec
    return [t for t in buf if t >= cutoff]


def _record_inbound(tenant_id: int, convo_id: int, text: str | None) -> None:
    now = time.monotonic()
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        buf = _trim(_INBOUND_TIMES.get(key, []), _DAY_SEC, now)
        buf.append(now)
        _INBOUND_TIMES[key] = buf
        if text and _INTENT_RE.search(text):
            _INTENT_SEEN[key] = now


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


def _inbound_burst(tenant_id: int, convo_id: int) -> int:
    now = time.monotonic()
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        buf = _trim(_INBOUND_TIMES.get(key, []), _DAY_SEC, now)
        _INBOUND_TIMES[key] = buf
        return sum(1 for t in buf if t >= now - RAPID_BURST_WINDOW_SEC)


def _has_recent_intent(tenant_id: int, convo_id: int) -> bool:
    now = time.monotonic()
    key = (int(tenant_id), int(convo_id))
    with _LOCK:
        last = _INTENT_SEEN.get(key, 0.0)
    return last > 0 and (now - last) <= RAPID_BURST_WINDOW_SEC


# ────────────────────────────────────────────────────────────────────────────
# Bot-loop detection from recent message history
# ────────────────────────────────────────────────────────────────────────────
def _looks_automated(text: str | None) -> bool:
    if not text:
        return False
    return bool(_BOT_PHRASE_RE.search(text))


def _recent_messages(
    db: Session,
    tenant_id: int,
    convo_id: int,
    *,
    limit: int = 8,
) -> list[MessageEvent]:
    try:
        return list(
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.conversation_id == convo_id,
            )
            .order_by(MessageEvent.id.desc())
            .limit(limit)
            .all()
        )
    except Exception as exc:
        logger.debug("[ai_pause] history fetch failed: %s", exc)
        return []


def detect_bot_loop(
    db: Session,
    tenant_id: int,
    convo_id: int,
    inbound_text: str | None,
) -> bool:
    """True if recent traffic looks like an automated assistant on the
    customer side OR our own replies keep repeating the same canned line.
    """
    if _looks_automated(inbound_text):
        return True

    msgs = _recent_messages(db, tenant_id, convo_id, limit=8)
    automated_inbound = 0
    repeated_outbound: dict[str, int] = {}
    for m in msgs:
        body = (m.body or "").strip()
        if not body:
            continue
        if (m.direction or "").lower() == "inbound" and _looks_automated(body):
            automated_inbound += 1
        elif (m.direction or "").lower() == "outbound":
            short = body[:120]
            repeated_outbound[short] = repeated_outbound.get(short, 0) + 1

    # Two automated-looking inbound messages in the recent window OR the
    # same outbound bubble repeated 3+ times = loop.
    if automated_inbound >= 2:
        return True
    for n in repeated_outbound.values():
        if n >= 3:
            return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# Public: should_skip_ai (top-of-funnel) + after_ai_reply (post-funnel)
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
    Run BEFORE any LLM/Brain call. Returns ``(skip, reason)``.

    When ``skip`` is True the caller MUST:
      * still persist the inbound message
      * NOT call the LLM
      * NOT send any outbound reply (handoff is a separate, deterministic path).
    """
    # Track the inbound for burst detection regardless of outcome.
    if convo and convo.id is not None:
        _record_inbound(int(tenant_id), int(convo.id), inbound_text)

    # 1) Conversation already paused → respect it.
    if convo is not None and getattr(convo, "ai_paused", False):
        reason = getattr(convo, "ai_paused_reason", None) or REASON_MANUAL
        return True, reason

    # 2) Internal / merchant blocked numbers.
    blocked, reason = is_internal_or_blocked(db, tenant_id, customer_phone)
    if blocked:
        # Persist the pause so subsequent messages don't even hit this branch.
        if convo is not None:
            pause_ai(db, convo, reason=reason or REASON_INTERNAL_NUMBER, by="system:internal_number")
        return True, reason or REASON_INTERNAL_NUMBER

    # 3) Bot-loop detection (recent automated patterns).
    if convo is not None and convo.id is not None:
        if detect_bot_loop(db, tenant_id, int(convo.id), inbound_text):
            pause_ai(db, convo, reason=REASON_BOT_LOOP, by="system:bot_loop_detected")
            return True, REASON_BOT_LOOP

    # 4) Rapid-burst guard: many inbound messages but no clear intent and
    #    we've already replied a few times → likely a ping-pong.
    if convo is not None and convo.id is not None:
        burst = _inbound_burst(int(tenant_id), int(convo.id))
        if burst > RAPID_BURST_INBOUND_LIMIT:
            ten_min_replies, _ = _reply_counts(int(tenant_id), int(convo.id))
            if ten_min_replies > RAPID_BURST_REPLY_LIMIT and not _has_recent_intent(int(tenant_id), int(convo.id)):
                pause_ai(db, convo, reason=REASON_BOT_LOOP, by="system:rapid_burst_no_intent")
                return True, REASON_BOT_LOOP

    # 5) Hard rate-limit enforcement (10-min / day caps).
    if convo is not None and convo.id is not None:
        ten_min, day = _reply_counts(int(tenant_id), int(convo.id))
        if ten_min >= MAX_AI_REPLIES_PER_CONTACT_10MIN or day >= MAX_AI_REPLIES_PER_CONTACT_DAY:
            pause_ai(db, convo, reason=REASON_RATE_LIMIT, by="system:rate_limit")
            return True, REASON_RATE_LIMIT

    return False, None


def after_ai_reply(
    db: Session,
    convo: Conversation,
    *,
    tenant_id: int,
) -> None:
    """Call AFTER the LLM/Brain successfully sent an outbound reply.

    Increments rate counters and flips ``ai_paused`` to True if either
    cap has now been exceeded — guarantees the next inbound is filtered
    even if the per-process counters reset.
    """
    if not convo or convo.id is None or tenant_id is None:
        return
    _record_reply(int(tenant_id), int(convo.id))
    ten_min, day = _reply_counts(int(tenant_id), int(convo.id))
    if ten_min > MAX_AI_REPLIES_PER_CONTACT_10MIN or day > MAX_AI_REPLIES_PER_CONTACT_DAY:
        pause_ai(db, convo, reason=REASON_RATE_LIMIT, by="system:rate_limit")


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


__all__ = [
    "REASON_MANUAL",
    "REASON_HUMAN_HANDOFF",
    "REASON_BOT_LOOP",
    "REASON_RATE_LIMIT",
    "REASON_INTERNAL_NUMBER",
    "MAX_AI_REPLIES_PER_CONTACT_10MIN",
    "MAX_AI_REPLIES_PER_CONTACT_DAY",
    "should_skip_ai",
    "after_ai_reply",
    "pause_ai",
    "resume_ai",
    "is_internal_or_blocked",
    "detect_bot_loop",
    "add_blocked_number",
    "remove_blocked_number",
    "list_blocked_numbers",
]
