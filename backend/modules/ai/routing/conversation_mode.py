"""
modules/ai/routing/conversation_mode.py
───────────────────────────────────────
Top-level Conversation Mode Controller.

This module sits ABOVE the Merchant Brain / legacy-AI split. For every
incoming customer message we ask a single question first:

    "Who owns this conversation right now?"

The answer is one of a small, well-defined set of MODES:

    - automation_recovery   → cart-recovery / abandoned-cart automation
                               is currently driving the thread
    - live_chat             → the customer is conversing freely with the
                               store assistant (Brain or legacy)
    - identity_reply        → the customer just asked "who are you?" /
                               "السلام عليكم" / "من أنت" — answer
                               deterministically before anything else
    - support_escalation    → human handoff / explicit complaint
    - checkout_assist       → mid-checkout (open draft order, payment)
    - post_purchase         → tracking / status / after-sale follow-up

Why a controller, not just better prompts
─────────────────────────────────────────
The bug we are fixing is architectural, not stylistic: the system kept
behaving like an automation even after the customer switched to a
free-form conversational message inside the open 24-hour session.

That happened because mode was implicit, scattered across:
  * automation event lineage (recovery_event_id / step_idx)
  * brain_state.stage         (sales-funnel substate)
  * conversation flags        (is_human_handoff, paused_by_human)
  * ad-hoc prompt wording     in two separate AI paths

There was no shared decision layer. This controller IS that layer.

Design contract
───────────────
- READ-ONLY w.r.t. the engine (`brain/decision/engine.py`),
  provider selection, fallback, and rule-first decision flow.
- Persists its lease on `Conversation.extra_metadata['conversation_mode']`
  using the merge-safe metadata write path that already preserves
  `brain_state` (see core.conversation_engine.StateManager.save).
- Sticky LIVE_CHAT lease: when a free-form message overrides a prior
  `automation_recovery`, we acquire a short-lived lease so subsequent
  turns in the same activity window stay in live chat instead of
  bouncing back to automation behavior.
- Safe fallback: if the stored lease is missing/malformed, we rebuild
  it from current signals and continue without ever raising.
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.routing.mode")


# ── Mode constants ────────────────────────────────────────────────────────────

MODE_AUTOMATION_RECOVERY = "automation_recovery"
MODE_LIVE_CHAT           = "live_chat"
MODE_IDENTITY_REPLY      = "identity_reply"
MODE_SUPPORT_ESCALATION  = "support_escalation"
MODE_CHECKOUT_ASSIST     = "checkout_assist"
MODE_POST_PURCHASE       = "post_purchase"

ALL_MODES: Tuple[str, ...] = (
    MODE_AUTOMATION_RECOVERY,
    MODE_LIVE_CHAT,
    MODE_IDENTITY_REPLY,
    MODE_SUPPORT_ESCALATION,
    MODE_CHECKOUT_ASSIST,
    MODE_POST_PURCHASE,
)

# Stored on Conversation.extra_metadata. Kept namespaced so we never
# collide with brain_state, phone, customer_phone, etc.
META_KEY = "conversation_mode"

# Default sticky LIVE_CHAT lease in minutes. Aligned with the
# scheduler-side "active conversation" guard so an automation already
# scheduled for delivery is naturally deferred while the lease is held.
# See: services.conversion_layer.ACTIVE_CONVERSATION_WINDOW_MINUTES.
DEFAULT_LEASE_MINUTES_LIVE_CHAT      = 10
DEFAULT_LEASE_MINUTES_CHECKOUT       = 15
DEFAULT_LEASE_MINUTES_SUPPORT        = 30
DEFAULT_LEASE_MINUTES_POST_PURCHASE  = 15

# Sources used by `reason` / observability. Kept as constants so logs
# and tests can match exact strings.
SOURCE_OVERRIDE_FREEFORM   = "override_from_recovery_freeform"
SOURCE_IDENTITY_DETECTED   = "identity_question_detected"
SOURCE_HANDOFF_FLAG        = "human_handoff_flag"
SOURCE_RECOVERY_ACTIVE     = "recovery_lineage_active"
SOURCE_CHECKOUT_OPEN       = "checkout_open"
SOURCE_POST_PURCHASE_HINT  = "post_purchase_signal"
SOURCE_LEASE_HELD          = "live_chat_lease_held"
SOURCE_DEFAULT_FALLBACK    = "default_live_chat"


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class RecoverySnapshot:
    """Lightweight summary of the customer's recovery lineage. Built
    from existing `Order.extra_metadata.recovery_event_id` + automation
    event tree so the controller never needs to re-implement that
    logic. All fields are best-effort and may be empty."""
    has_recovery: bool = False
    recovery_active: bool = False     # event tree is not converted/cancelled
    last_step_idx: int = 0
    last_step_at: Optional[str] = None  # ISO UTC of latest sent step
    converted_at: Optional[str] = None
    cancel_reason: Optional[str] = None
    order_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ModeLease:
    """The persisted, per-conversation lease record. Stored under
    `Conversation.extra_metadata['conversation_mode']`."""
    mode: str = MODE_LIVE_CHAT
    previous_mode: str = ""
    reason: str = ""
    source: str = ""
    changed_at: str = ""           # ISO UTC of last transition
    locked_until: str = ""         # ISO UTC; "" == no active lease

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: Optional[Dict[str, Any]]) -> "ModeLease":
        if not isinstance(raw, dict):
            return ModeLease()
        return ModeLease(
            mode          = str(raw.get("mode") or MODE_LIVE_CHAT),
            previous_mode = str(raw.get("previous_mode") or ""),
            reason        = str(raw.get("reason") or ""),
            source        = str(raw.get("source") or ""),
            changed_at    = str(raw.get("changed_at") or ""),
            locked_until  = str(raw.get("locked_until") or ""),
        )

    def is_lease_active(self, now: Optional[datetime] = None) -> bool:
        if not self.locked_until:
            return False
        try:
            until = _parse_iso(self.locked_until)
        except Exception:
            return False
        return _now(now) < until


@dataclass
class ModeDecision:
    """Result returned by the controller to the webhook. The webhook
    routes by `mode` and persists `lease` back to the conversation."""
    mode: str
    lease: ModeLease
    previous_mode: str = ""
    reason: str = ""
    source: str = ""
    transitioned: bool = False
    recovery: RecoverySnapshot = field(default_factory=RecoverySnapshot)
    identity_topic: str = ""        # set when mode==identity_reply
    free_form_override: bool = False

    def is_identity(self) -> bool:
        return self.mode == MODE_IDENTITY_REPLY

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "mode":             self.mode,
            "previous_mode":    self.previous_mode,
            "reason":           self.reason,
            "source":           self.source,
            "transitioned":     self.transitioned,
            "lease_until":      self.lease.locked_until,
            "free_form_override": self.free_form_override,
            "recovery_active":  self.recovery.recovery_active,
        }


# ── Time helpers ─────────────────────────────────────────────────────────────

def _now(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _parse_iso(value: str) -> datetime:
    """Parse an ISO8601 timestamp; assume UTC if naive."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ── Inbound signal detectors ─────────────────────────────────────────────────

# Identity / "who are you" — borrowed from brain.intent.rules so behavior
# stays consistent whether the controller fires before Brain or alongside
# legacy. Patterns are narrow on purpose: false positives here would
# pull a real conversational turn into the deterministic identity reply.
_IDENTITY_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE) for p in (
        r"^\s*(من\s*أنت|من\s*انت|من\s*أنتِ|انت\s*مين|انتي\s*مين|مين\s*أنت|"
        r"وش\s*أنت|وش\s*انت|ايش\s*انت|ايش\s*أنت)\b",
        r"\b(عرفني\s+بنفسك|عرفني\s+عليك|who\s+are\s+you|what\s+are\s+you)\b",
        # "Are you AI / a bot / a robot / human?" — covers the most
        # frequent way customers test the assistant. Matches "هل أنت AI"
        # / "هل أنتم AI" / "انت روبوت" / "أنت بوت" / "هل أنت برنامج" /
        # "ai you" / "are u a bot" with diacritic-tolerant spacing.
        r"(هل\s*)?(انتم|أنتم|انت|أنت|انتي|أنتي|انتو|أنتو)\s*"
        r"(ا\s*ي|آي|ai|بوت|بوتات|روبوت|رو\s*بوت|"
        r"برنامج|ذكاء\s*اصطناعي|ذكاء|chat\s*bot|chatbot)\b",
        r"\b(are\s+(?:you|u|yall|ya'll)\s+(?:an?\s+)?(ai|bot|robot|chatbot|human|real|machine))\b",
        r"\b(this\s+is\s+(?:an?\s+)?(ai|bot|chatbot))\b",
        # Generic suspicion phrases — short Arabic forms.
        r"^\s*(انت\s*انسان|أنت\s*إنسان|انت\s*حقيقي|أنت\s*حقيقي|انت\s*برنامج|أنت\s*برنامج)\b",
    )
)

_GREETING_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE) for p in (
        r"^\s*(السلام\s*عليكم|وعليكم\s*السلام|مرحبا?ً?|أهلاً?|هلا+|"
        r"صباح\s+الخير|مساء\s+الخير|كيف\s+حالك|هاي|هلو|hello|hi|hey)\b",
        r"^\s*(أهلين|يا\s*هلا|أهلا\s+وسهلا)\b",
    )
)


# ── Actionable / relational signal detector (welcome-gate consistency) ──────
#
# A real production bug (May 2026): a customer wrote
#     "السلام عليكم أبي سعر العسل"
# and got a canned greeting card instead of a price answer. The greeting
# pattern above matched at the start of the message, MODE_IDENTITY_REPLY
# fired, and the brain pipeline (with the welcome-gate fix that demotes
# the greeting to ASK_PRICE+embedded_greeting) never even ran.
#
# Fix v1: a greeting at the start is only allowed to route to the canned
# identity card when the REST of the message carries no actionable
# commerce/platform signal.
#
# Fix v2 (May 2026, Eid season — Tenant 33): production traffic revealed
# the v1 regex was commerce-only, so customers sending heartfelt Eid /
# religious / wellbeing / self-introduction greetings *with no commerce
# token* still collapsed into the canned identity card (cold sales
# fallback on top of a deeply social message). Extending the same regex
# with relational axes lets those messages yield to the Brain so the
# natural social reply runs. We keep MODE_IDENTITY_REPLY for pure short
# greetings ("السلام عليكم", "هلا", "مرحبا") only.
#
# Cost of a false positive (yield to Brain when canned would also work)
# is low — Brain handles any inbound gracefully. Cost of a false
# negative is the cold-card regression we are fixing.
#
# Adding a new actionable intent? Add its discriminating tokens here too.
_ACTIONABLE_OR_RELATIONAL_AFTER_GREETING_RE = re.compile(
    "|".join((
        # ── Commerce / platform (legacy v1) ──────────────────────────────
        # Price / cost asks (avoid bare "كم" because "كيف حالك" would
        # false-positive — require concrete price tokens).
        r"سعر", r"اسعار", r"أسعار",
        r"كم\s*ثمن", r"كم\s*يساوي", r"كم\s*تمنه", r"كم\s*ثمنه",
        r"بكم", r"ثمنه",
        r"price", r"cost", r"how\s*much",
        # Product / catalog asks
        r"عسل", r"سدر", r"طلح", r"ضهيان", r"قسط",
        r"شمع", r"قرص\s*العسل",
        r"منتج", r"بضاعة", r"سلعة", r"صنف", r"موديل",
        r"عندك", r"عندكم", r"لديك", r"لديكم",
        r"ابي", r"أبي", r"ابغى", r"أبغى", r"ابغي", r"أبغي",
        r"اريد", r"أريد", r"بدي", r"ودي", r"بغيت",
        r"اشتري", r"أشتري", r"اطلب", r"أطلب",
        r"تفاصيل",
        # Payment / shipping / address asks
        r"رابط", r"تحويل", r"ايبان", r"آيبان", r"iban",
        r"راجحي", r"الراجحي", r"باركود", r"qr",
        r"شحن", r"توصيل", r"عنوان",
        # Platform asks
        r"اشتراك", r"باقات", r"\bapi\b", r"نحلة",
        r"واتساب\s*الاعمال", r"واتساب\s*الأعمال",
        r"meta", r"embedded\s*signup",
        # Track order asks
        r"طلبي", r"طلبية", r"شحنتي", r"تتبع",

        # ── Relational / seasonal / religious / self-intro (v2) ──────────
        # Eid / seasonal markers. ``\b`` on the short tokens keeps
        # "سعيد" (a common Saudi name) from false-matching "عيد",
        # and "مبارك" from false-matching inside an unrelated word.
        r"\bعيد\b", r"كل\s*عام", r"كل\s*سنة",
        r"عساكم", r"عساك",
        r"العايدين", r"العائدين", r"عواده",
        r"\bمبارك\b",
        # Religious / wellbeing.
        # ``الله\s+ي`` catches "الله يحفظكم / يجزاك / يبارك / يشفي / ..."
        # without matching bare "والله" / "إن شاء الله بخير" alone,
        # which the legacy "هلا والله" pure-greeting test exercises.
        r"اللهم", r"الحمد\s*لله",
        r"الله\s+ي",
        r"إن\s*شاء\s*الله", r"ما\s*شاء\s*الله",
        r"سلامتك", r"سلامتكم", r"سلامة",
        r"يحفظ",
        r"يجزا", r"جزا",
        r"بارك",
        r"يشفي", r"شفا", r"اشف",
        r"عافى", r"عافاك", r"العافية",
        r"يصبر", r"يعوض", r"يرحم",
        # Self-introduction. ``\b`` on the short tokens reduces lax matches.
        r"\bمعك\b", r"\bأنا\b",
        r"اسمي",
        r"رقمي", r"رقم\s*جديد",
        # Relational inquiry.
        r"كيفكم",
        r"كيف\s*الأهل", r"كيف\s*الاهل",
        r"طمنا", r"طمني", r"اطمئن",
        r"أخبارك", r"اخبارك", r"أخباركم", r"اخباركم",
        r"أبشرك", r"ابشرك",
    )),
    re.IGNORECASE | re.UNICODE,
)

# Backward-compat alias — internal callers / tests that still reference the
# legacy "actionable-only" name keep working. Both names point at the same
# extended regex.
_ACTIONABLE_AFTER_GREETING_RE = _ACTIONABLE_OR_RELATIONAL_AFTER_GREETING_RE

# Greeting prefix tokens we strip off before testing for actionable content.
# Kept separate from the matching patterns so we can compute the "what's
# LEFT after the salaam?" remainder without re-grepping.
_GREETING_PREFIX_RE = re.compile(
    r"^\s*("
    r"السلام\s*عليكم(?:\s*ورحمة\s*الله(?:\s*وبركاته)?)?|"
    r"وعليكم\s*السلام(?:\s*ورحمة\s*الله(?:\s*وبركاته)?)?|"
    r"مرحبا?ً?|أهلاً?|أهلين|هلا+|يا\s*هلا|أهلا\s+وسهلا|"
    r"صباح\s+الخير(?:ات)?|مساء\s+الخير(?:ات)?|"
    r"كيف\s+حالك|هاي|هلو|hello|hi|hey"
    r")[\s،,.!؟?]*",
    re.IGNORECASE | re.UNICODE,
)


def _message_has_actionable_or_relational_after_greeting(text: str) -> bool:
    """True when the customer combined a greeting with a real ask OR with
    relational / religious / seasonal / self-introduction content.

    Examples that yield to Brain (return True):

      * "السلام عليكم أبي سعر العسل"             — commerce
      * "السلام عليكم كل عام وأنتم بخير"          — Eid / seasonal
      * "السلام عليكم الله يشفي الشباب"          — wellbeing prayer
      * "السلام عليكم معك سعيد رقمي الجديد"      — self-introduction

    Examples that stay on the canned identity card (return False):

      * "السلام عليكم"                            — pure salaam
      * "وعليكم السلام ورحمة الله وبركاته"        — pure return salaam
      * "هلا والله"                               — short greeting + intensifier
      * "صباح الخير، كيف حالك؟"                   — pure courtesy

    The brain's welcome-gate handles every "True" case below.
    """
    if not isinstance(text, str):
        return False
    remainder = _GREETING_PREFIX_RE.sub("", text, count=1).strip()
    if not remainder:
        return False
    return bool(_ACTIONABLE_OR_RELATIONAL_AFTER_GREETING_RE.search(remainder))


# Backward-compat alias — internal callers / tests that still reference the
# legacy "actionable-only" function name keep working. Both names route
# through the same v2 detector.
_message_has_actionable_after_greeting = (
    _message_has_actionable_or_relational_after_greeting
)

_SUPPORT_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE) for p in (
        r"\b(تحدث\s+مع\s+(إنسان|بشر|موظف)|موظف|خدمة\s+العملاء|تواصل\s+مع\s+شخص|"
        r"إنسان\s+حقيقي|مو\s+روبوت|مو\s+بوت)\b",
        r"\b(human\s+agent|real\s+person|customer\s+service|speak\s+to\s+someone|"
        r"talk\s+to\s+agent)\b",
        r"\b(شكوى|أشتكي|اشتكي|مشكلة\s+كبيرة|ما\s+ينفع|تعب|سيء|سيئة|سيئ|"
        r"مزعج|مزعجة|complaint|terrible|awful|disappointed)\b",
    )
)

_TRACKING_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE) for p in (
        r"\b(وين\s+طلبي|وين\s+أمري|تتبع\s+الطلب|متى\s+يوصل\s+طلبي|"
        r"رقم\s+التتبع|طلبي\s+وين|شحنتي\s+وين)\b",
        r"\b(track|track\s+my\s+order|where\s+is\s+my\s+order|order\s+status)\b",
    )
)

_CHECKOUT_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE) for p in (
        r"\b(ادفع|أدفع|دفع|سدد|أسدد|إتمام\s+الدفع|الدفع\s+الآن|أكمل\s+الدفع|"
        r"رابط\s+الدفع|رابط\s+الطلب|أتمم\s+الطلب)\b",
        r"\b(pay|payment\s+link|checkout|complete\s+order)\b",
    )
)


def _matches_any(text: str, patterns: Tuple[re.Pattern, ...]) -> bool:
    if not text:
        return False
    for pattern in patterns:
        if pattern.search(text):
            return True
    return False


def detect_identity_topic(text: str) -> str:
    """Return 'identity' for who-are-you, 'greeting' for السلام-عليكم,
    or '' for anything else. Both topics route to MODE_IDENTITY_REPLY
    so the deterministic identity path can introduce the assistant
    once instead of falling through to automation boilerplate.

    IMPORTANT — welcome-gate consistency (May 2026):
    A greeting prefix WITH an actionable commerce/platform signal
    after it ("السلام عليكم أبي سعر العسل") is NOT a pure greeting.
    Routing it to the canned identity card means the customer's real
    question never reaches the brain pipeline. We yield to the brain
    in that case so its welcome-gate can demote the greeting to the
    underlying actionable intent and answer the real question with a
    short embedded salaam on top.
    """
    if not isinstance(text, str):
        return ""
    if _matches_any(text, _IDENTITY_PATTERNS):
        return "identity"
    if _matches_any(text, _GREETING_PATTERNS):
        # Welcome-gate yield: greeting + actionable / relational signal
        # → let the brain handle it. Pure greeting (or greeting + bare
        # courtesy only) stays on the canned identity reply path.
        if _message_has_actionable_or_relational_after_greeting(text):
            return ""
        return "greeting"
    return ""


# ── Order-flow recovery signal ───────────────────────────────────────────────
# Customers in the middle of an order frequently keep typing order data
# (a national short address code, a Google Maps link, a numeric pick from
# a previous list, or an explicit "complete the order" phrase) AFTER a
# transient handoff/escalation flag was set. This helper centralises the
# detection so every layer that might block the customer can yield to
# the order-flow recovery override consistently:
#
#   - human_handoff_flag override         (whatsapp_webhook.py)
#   - live_chat_lease_held override       (whatsapp_webhook.py + this file)
#   - ACTION_HANDOFF prevention           (brain.decision.engine)
#
# Keep the regex side-by-side with the keyword list so it stays the
# single source of truth across the codebase.
_ORDER_RECOVERY_SHORT_CODE_RE = re.compile(r"\b[A-Z]{4}\d{4}\b")
_ORDER_RECOVERY_KEYWORDS: Tuple[str, ...] = (
    "أنشئ الطلب", "انشئ الطلب", "اطلب لي", "أطلب لي",
    "أبغى أطلب", "أبي أطلب", "ابي اطلب", "ابغى اطلب",
    "ادفع", "أدفع", "رابط الدفع", "اكمل الطلب", "أكمل الطلب",
    "كمل الطلب", "كمل طلبي", "أكمل طلبي",
    "https://maps.app.goo.gl", "https://goo.gl/maps", "maps.google.com",
)


def message_has_order_recovery_signal(text: str) -> bool:
    """True when the inbound message looks like the customer is trying
    to continue an order (address code, Maps URL, numeric pick, explicit
    "create the order" phrase). Used by every escalation/lease guard
    that should yield to the order flow."""
    if not isinstance(text, str):
        return False
    cleaned = text.strip()
    if not cleaned:
        return False

    # 1. Saudi national short address code (e.g. TAPA7401).
    if _ORDER_RECOVERY_SHORT_CODE_RE.search(cleaned.upper()):
        return True

    # 2. Explicit order keywords / Maps URLs.
    lowered = cleaned.lower()
    for kw in _ORDER_RECOVERY_KEYWORDS:
        if kw in cleaned or kw.lower() in lowered:
            return True

    # 3. Pure numeric pick from a previous product/options list.
    if cleaned.isdigit() and 1 <= len(cleaned) <= 2:
        return True
    # 3b. Multi-token numeric pick like "2 1" (option-group selection).
    tokens = cleaned.split()
    if 1 <= len(tokens) <= 4 and all(t.isdigit() and 1 <= len(t) <= 2 for t in tokens):
        return True

    return False


def is_free_form_message(text: str) -> bool:
    """True for any non-empty inbound that isn't a button payload or
    pure interactive token. Free-form messages are the trigger for
    overriding a prior automation_recovery owner."""
    if not isinstance(text, str):
        return False
    cleaned = text.strip()
    if not cleaned:
        return False
    # WhatsApp button taps land here as "[button:...]" or as a pick
    # token ("pick_1"); both are interactive payloads, not free-form.
    if cleaned.startswith("[button:") or cleaned.startswith("pick_"):
        return False
    return True


# ── Recovery snapshot loader ─────────────────────────────────────────────────

def load_recovery_snapshot(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
) -> RecoverySnapshot:
    """Return a small recovery snapshot for the given customer.

    Reads from the existing recovery lineage (Order.extra_metadata
    .recovery_event_id + AutomationEvent payload) without duplicating
    the timeline summary logic. Always returns a snapshot — never
    raises — so the controller stays safe under DB hiccups."""
    snapshot = RecoverySnapshot()
    if not db or not tenant_id or not customer_phone:
        return snapshot

    try:
        from models import (  # noqa: PLC0415
            AutomationEvent, Customer, Order,
        )
        # Resolve the most-recent customer row for this phone within the
        # tenant. Recovery lineage is anchored on Order.extra_metadata
        # so we need an actual Order row to walk back to the root event.
        cust = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant_id)
            .filter(Customer.phone == customer_phone)
            .order_by(Customer.id.desc())
            .first()
        )
        if cust is None:
            return snapshot

        order = (
            db.query(Order)
            .filter(Order.tenant_id == tenant_id)
            .filter(Order.customer_id == cust.id)
            .order_by(Order.id.desc())
            .first()
        )
        if order is None or not order.extra_metadata:
            return snapshot

        meta = order.extra_metadata or {}
        raw_root = meta.get("recovery_event_id")
        if raw_root is None:
            return snapshot

        try:
            root_event_id = int(raw_root)
        except (TypeError, ValueError):
            return snapshot

        snapshot.has_recovery = True
        snapshot.order_id = int(order.id)

        root = (
            db.query(AutomationEvent)
            .filter(AutomationEvent.id == root_event_id)
            .filter(AutomationEvent.tenant_id == tenant_id)
            .first()
        )
        if root is None:
            return snapshot

        payload = root.payload or {}
        snapshot.converted_at  = payload.get("recovery_converted_at")
        snapshot.cancel_reason = payload.get("recovery_cancel_reason")
        snapshot.recovery_active = not (
            snapshot.converted_at or snapshot.cancel_reason
        )

        # Pull the latest step_idx/timestamp from follow-up events when
        # available; falls back to the root row otherwise. We keep this
        # loose on purpose — the controller only needs a coarse signal.
        followups = (
            db.query(AutomationEvent)
            .filter(AutomationEvent.tenant_id == tenant_id)
            .filter(
                AutomationEvent.payload["parent_event_id"].astext
                == str(root_event_id)
            )
            .order_by(AutomationEvent.id.desc())
            .limit(5)
            .all()
        )
        last_event = followups[0] if followups else root
        last_payload = last_event.payload or {}
        try:
            snapshot.last_step_idx = int(last_payload.get("step_idx") or 0)
        except (TypeError, ValueError):
            snapshot.last_step_idx = 0
        if last_event.created_at:
            snapshot.last_step_at = _iso(
                last_event.created_at if last_event.created_at.tzinfo
                else last_event.created_at.replace(tzinfo=timezone.utc)
            )
        return snapshot
    except Exception as exc:
        logger.debug("[mode] recovery snapshot failed: %s", exc)
        return snapshot


# ── Lease persistence ────────────────────────────────────────────────────────

def load_lease(convo: Any) -> ModeLease:
    """Read the persisted lease from `convo.extra_metadata`. Always
    returns a ModeLease, even when the field is missing or malformed."""
    try:
        meta = getattr(convo, "extra_metadata", None) or {}
        raw = meta.get(META_KEY)
        return ModeLease.from_dict(raw if isinstance(raw, dict) else None)
    except Exception:
        return ModeLease()


def save_lease(db: Any, convo: Any, lease: ModeLease) -> None:
    """Persist the lease on the conversation row using the existing
    metadata-merge contract. We deliberately read-modify-write the dict
    so we never wipe sibling keys (brain_state, phone, customer_phone)
    that other layers own."""
    if convo is None:
        return
    try:
        meta = dict(getattr(convo, "extra_metadata", None) or {})
        meta[META_KEY] = lease.to_dict()
        convo.extra_metadata = meta
        try:
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
            flag_modified(convo, "extra_metadata")
        except Exception:
            pass
        db.add(convo)
        db.flush()
    except Exception as exc:
        logger.warning("[mode] save_lease failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


# ── Lease builders ───────────────────────────────────────────────────────────

def _build_lease(
    *,
    mode: str,
    previous_mode: str,
    reason: str,
    source: str,
    minutes: int,
    now: Optional[datetime] = None,
) -> ModeLease:
    n = _now(now)
    locked_until = ""
    if minutes and minutes > 0:
        locked_until = _iso(n + timedelta(minutes=int(minutes)))
    return ModeLease(
        mode          = mode,
        previous_mode = previous_mode,
        reason        = reason,
        source        = source,
        changed_at    = _iso(n),
        locked_until  = locked_until,
    )


def _conversation_handoff_flag(convo: Any, db: Any = None, *, now: Any = None) -> bool:
    """True when staff genuinely owns the keyboard (Real Handoff Slice 1).

    Uses ``core.ownership_state.conversation_handoff_active`` when a DB
    session is available — implicit takeover expires after staff-idle TTL
    once the customer messages again. Advisory queue flags alone never
    fire this gate (May 2026 #46).

    Without ``db`` (unit tests): falls back to raw takeover booleans.
    """
    if db is not None:
        try:
            from core.ownership_state import conversation_handoff_active  # noqa: PLC0415

            return conversation_handoff_active(
                db, convo, now=now, assume_current_inbound=True,
            )
        except Exception:
            pass
    return bool(
        getattr(convo, "paused_by_human", False)
        or getattr(convo, "taken_over_at", None) is not None
    )


# ── Public API: resolve_conversation_mode ────────────────────────────────────

def resolve_conversation_mode(
    db: Any,
    *,
    tenant_id: int,
    convo: Any,
    customer_phone: str,
    text: str,
    history: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> ModeDecision:
    """Decide who owns this conversation right now.

    Order of resolution (first match wins):

      1. Human handoff flag on the conversation row.
      2. Identity / greeting detection in the inbound text.
      3. Sticky LIVE_CHAT lease that has not expired yet.
      4. Free-form override of an existing automation_recovery owner.
      5. Active recovery lineage (and no overriding signal).
      6. Default to live_chat.

    Returns a ModeDecision. Persistence is the caller's responsibility
    via `save_lease(...)`; this function is pure-ish and never raises.
    """
    text_clean = (text or "").strip()
    history_safe = list(history or [])
    snapshot = load_recovery_snapshot(
        db, tenant_id=tenant_id, customer_phone=customer_phone,
    )
    prior_lease = load_lease(convo)
    prior_mode = prior_lease.mode or MODE_LIVE_CHAT

    # 1) Human handoff trumps everything ────────────────────────────────
    if _conversation_handoff_flag(convo, db, now=now):
        lease = _build_lease(
            mode=MODE_SUPPORT_ESCALATION,
            previous_mode=prior_mode,
            reason="conversation flagged for human handoff",
            source=SOURCE_HANDOFF_FLAG,
            minutes=DEFAULT_LEASE_MINUTES_SUPPORT,
            now=now,
        )
        return ModeDecision(
            mode=MODE_SUPPORT_ESCALATION,
            lease=lease,
            previous_mode=prior_mode,
            reason=lease.reason,
            source=lease.source,
            transitioned=(prior_mode != MODE_SUPPORT_ESCALATION),
            recovery=snapshot,
        )

    # 2) Identity / greeting detection ──────────────────────────────────
    identity_topic = detect_identity_topic(text_clean)
    if identity_topic:
        # Identity replies are deterministic single-turn answers. We
        # acquire a LIVE_CHAT lease right after so the conversation
        # stays in live mode for the next turn instead of falling back
        # into automation behavior.
        lease = _build_lease(
            mode=MODE_LIVE_CHAT,
            previous_mode=prior_mode,
            reason=f"customer asked: {identity_topic}",
            source=SOURCE_IDENTITY_DETECTED,
            minutes=DEFAULT_LEASE_MINUTES_LIVE_CHAT,
            now=now,
        )
        return ModeDecision(
            mode=MODE_IDENTITY_REPLY,
            lease=lease,
            previous_mode=prior_mode,
            reason=lease.reason,
            source=lease.source,
            transitioned=(prior_mode != MODE_LIVE_CHAT),
            recovery=snapshot,
            identity_topic=identity_topic,
            free_form_override=(prior_mode == MODE_AUTOMATION_RECOVERY),
        )

    # 3) Sticky LIVE_CHAT lease still active ────────────────────────────
    # Once we have switched into live chat, we DO NOT bounce back to
    # automation_recovery just because the recovery tree is still open.
    # The lease must expire (or be explicitly released) first.
    if prior_lease.is_lease_active(now) and prior_mode == MODE_LIVE_CHAT:
        # Refresh the sliding window on each turn so a chatty customer
        # keeps the conversation in live mode for the whole exchange.
        lease = _build_lease(
            mode=MODE_LIVE_CHAT,
            previous_mode=MODE_LIVE_CHAT,
            reason="live chat lease still active — refreshing window",
            source=SOURCE_LEASE_HELD,
            minutes=DEFAULT_LEASE_MINUTES_LIVE_CHAT,
            now=now,
        )
        return ModeDecision(
            mode=MODE_LIVE_CHAT,
            lease=lease,
            previous_mode=prior_mode,
            reason=lease.reason,
            source=lease.source,
            transitioned=False,
            recovery=snapshot,
        )

    # Other sticky leases (checkout, support, post-purchase) also win
    # over automation recovery while held — UNLESS the customer is
    # clearly trying to continue an order (short_code / Maps URL /
    # numeric pick / explicit order keyword). In that case the
    # order-flow recovery override breaks the lease and hands back to
    # live_chat so Brain/Order Flow can take over the turn.
    if prior_lease.is_lease_active(now) and prior_mode in (
        MODE_CHECKOUT_ASSIST, MODE_SUPPORT_ESCALATION, MODE_POST_PURCHASE,
    ):
        if message_has_order_recovery_signal(text_clean):
            logger.info(
                "[ORDER FLOW] restoring flow after live chat lease | "
                "prior_mode=%s lease_until=%s",
                prior_mode, prior_lease.locked_until,
            )
            target_mode = MODE_LIVE_CHAT
            lease = _build_lease(
                mode=target_mode,
                previous_mode=prior_mode,
                reason="order recovery signal — releasing non-recovery lease",
                source=SOURCE_OVERRIDE_FREEFORM,
                minutes=_lease_minutes_for(target_mode),
                now=now,
            )
            return ModeDecision(
                mode=target_mode,
                lease=lease,
                previous_mode=prior_mode,
                reason=lease.reason,
                source=lease.source,
                transitioned=True,
                recovery=snapshot,
                free_form_override=True,
            )

        lease = _build_lease(
            mode=prior_mode,
            previous_mode=prior_mode,
            reason="non-recovery lease still active",
            source=SOURCE_LEASE_HELD,
            minutes=_lease_minutes_for(prior_mode),
            now=now,
        )
        return ModeDecision(
            mode=prior_mode,
            lease=lease,
            previous_mode=prior_mode,
            reason=lease.reason,
            source=lease.source,
            transitioned=False,
            recovery=snapshot,
        )

    # 4) Free-form override of an existing automation_recovery owner ────
    if (
        prior_mode == MODE_AUTOMATION_RECOVERY
        and is_free_form_message(text_clean)
    ):
        # The customer broke out of the automation script with a
        # free-form reply inside the open 24h session. Move ownership
        # to live chat and lock it in for the activity window.
        target_mode, target_source = _classify_freeform(text_clean)
        lease = _build_lease(
            mode=target_mode,
            previous_mode=prior_mode,
            reason="free-form reply overrides automation_recovery owner",
            source=SOURCE_OVERRIDE_FREEFORM,
            minutes=_lease_minutes_for(target_mode),
            now=now,
        )
        return ModeDecision(
            mode=target_mode,
            lease=lease,
            previous_mode=prior_mode,
            reason=lease.reason,
            source=target_source or lease.source,
            transitioned=True,
            recovery=snapshot,
            free_form_override=True,
        )

    # 5) Active recovery lineage ─────────────────────────────────────────
    if snapshot.recovery_active and not is_free_form_message(text_clean):
        lease = _build_lease(
            mode=MODE_AUTOMATION_RECOVERY,
            previous_mode=prior_mode,
            reason="recovery lineage active and no overriding signal",
            source=SOURCE_RECOVERY_ACTIVE,
            # No lease for recovery: we want any subsequent free-form
            # message to be able to flip ownership instantly.
            minutes=0,
            now=now,
        )
        return ModeDecision(
            mode=MODE_AUTOMATION_RECOVERY,
            lease=lease,
            previous_mode=prior_mode,
            reason=lease.reason,
            source=lease.source,
            transitioned=(prior_mode != MODE_AUTOMATION_RECOVERY),
            recovery=snapshot,
        )

    # 5b) Active recovery + free-form message → upgrade to live chat
    # immediately even when the prior mode wasn't recovery yet (first
    # contact case where the snapshot says recovery is active).
    if snapshot.recovery_active and is_free_form_message(text_clean):
        target_mode, target_source = _classify_freeform(text_clean)
        lease = _build_lease(
            mode=target_mode,
            previous_mode=prior_mode,
            reason="recovery active but customer messaged free-form",
            source=SOURCE_OVERRIDE_FREEFORM,
            minutes=_lease_minutes_for(target_mode),
            now=now,
        )
        return ModeDecision(
            mode=target_mode,
            lease=lease,
            previous_mode=prior_mode,
            reason=lease.reason,
            source=target_source or lease.source,
            transitioned=(prior_mode != target_mode),
            recovery=snapshot,
            free_form_override=True,
        )

    # 6) Default to live chat (with no lease — natural behavior) ────────
    target_mode, target_source = _classify_freeform(text_clean)
    lease = _build_lease(
        mode=target_mode,
        previous_mode=prior_mode,
        reason="default live chat owner",
        source=SOURCE_DEFAULT_FALLBACK,
        minutes=_lease_minutes_for(target_mode) if target_mode != MODE_LIVE_CHAT else 0,
        now=now,
    )
    return ModeDecision(
        mode=target_mode,
        lease=lease,
        previous_mode=prior_mode,
        reason=lease.reason,
        source=target_source or lease.source,
        transitioned=(prior_mode != target_mode),
        recovery=snapshot,
    )


def _classify_freeform(text: str) -> Tuple[str, str]:
    """Classify a free-form inbound into one of the live owner modes.

    Returns (mode, source). Falls back to live_chat when no specific
    secondary owner is detected — which is exactly what we want: the
    default active conversation owner is the store's normal AI assistant.
    """
    if _matches_any(text, _SUPPORT_PATTERNS):
        return MODE_SUPPORT_ESCALATION, SOURCE_HANDOFF_FLAG
    if _matches_any(text, _CHECKOUT_PATTERNS):
        return MODE_CHECKOUT_ASSIST, SOURCE_CHECKOUT_OPEN
    if _matches_any(text, _TRACKING_PATTERNS):
        return MODE_POST_PURCHASE, SOURCE_POST_PURCHASE_HINT
    return MODE_LIVE_CHAT, SOURCE_DEFAULT_FALLBACK


def _lease_minutes_for(mode: str) -> int:
    if mode == MODE_LIVE_CHAT:
        return DEFAULT_LEASE_MINUTES_LIVE_CHAT
    if mode == MODE_CHECKOUT_ASSIST:
        return DEFAULT_LEASE_MINUTES_CHECKOUT
    if mode == MODE_SUPPORT_ESCALATION:
        return DEFAULT_LEASE_MINUTES_SUPPORT
    if mode == MODE_POST_PURCHASE:
        return DEFAULT_LEASE_MINUTES_POST_PURCHASE
    return 0


# ── Identity reply rendering (deterministic, no AI) ──────────────────────────

def _load_assistant_name(db: Any, tenant_id: int) -> str:
    """Best-effort load of the merchant's configured assistant name.
    Returns "" on any failure so the template can fall back gracefully."""
    try:
        from models import TenantSettings  # noqa: PLC0415
        ts = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if not ts:
            return ""
        ai = getattr(ts, "ai_settings", None) or {}
        if isinstance(ai, dict):
            name = ai.get("assistant_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    return ""


def _load_store_name(db: Any, tenant_id: int) -> str:
    try:
        from core.store_display import clean_store_name  # noqa: PLC0415
        from models import Tenant  # noqa: PLC0415
        t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if t:
            for attr in ("store_name", "name", "display_name"):
                val = getattr(t, attr, None)
                if isinstance(val, str) and val.strip():
                    return clean_store_name(val.strip())
    except Exception:
        pass
    return ""


# ── Identity / greeting variants ──────────────────────────────────────────────
# Multiple short variants are rotated so the bot does not feel scripted.
# Each variant follows the global emoji rules:
#   - 1-2 emojis maximum per message
#   - 🌷 for greeting, 🐝 for the Nahla identity hook (assistant name only),
#     👍 for help / confirmation
#   - emoji at start or end of a sentence — never mid-sentence

def _greeting_variants(assistant_name: str, store_name: str) -> List[str]:
    """Greeting variants used right after السلام عليكم / مرحبا.
    The assistant name carries the bee 🐝 so the persona stays linked
    to the brand mark; the leading 🌷 sets a warm tone."""
    if assistant_name:
        return [
            (
                f"وعليكم السلام 🌷\n"
                f"أنا {assistant_name} 🐝 موجودة أساعدك في المنتجات أو "
                f"الطلبات أو أي استفسار.\n"
                f"وش تحب أعرفك عليه؟"
            ),
            (
                f"حياك الله 🌷\n"
                f"أنا {assistant_name} 🐝 كيف أقدر أخدمك اليوم؟"
            ),
            (
                f"أهلاً وسهلاً 🌷\n"
                f"أنا {assistant_name} 🐝 من {store_name}. "
                f"وش تحب نبدأ فيه؟"
            ),
        ]
    return [
        (
            f"وعليكم السلام 🌷\n"
            f"أهلاً فيك في {store_name}. "
            f"وش تحب أعرفك عليه اليوم؟"
        ),
        (
            f"حياك الله 🌷\n"
            f"أهلاً فيك في {store_name}. كيف أقدر أخدمك؟"
        ),
    ]


def _identity_variants(assistant_name: str, store_name: str) -> List[str]:
    """Variants for «من أنت» / «هل أنت AI».

    Policy: identify CONFIDENTLY as an AI assistant. Never pretend to be
    human. The wording stays warm and store-specific so customers don't
    feel they hit a generic chatbot wall.
    """
    if assistant_name:
        return [
            (
                f"نعم 😊\n"
                f"أنا {assistant_name} 🐝 المساعدة الذكية في متجر {store_name}، "
                f"موجودة على مدار الساعة أساعد فريق المتجر في الرد عليك "
                f"وفي خدمة العملاء.\n"
                f"وش أقدر أساعدك فيه؟"
            ),
            (
                f"أنا {assistant_name} 🐝 المساعدة الذكية لـ {store_name}، "
                f"أرد عليك مباشرة وأساعدك في المنتجات والطلبات.\n"
                f"كيف أقدر أخدمك اليوم؟ 👍"
            ),
            (
                f"نعم 😊 أنا مساعدة ذكية اسمها {assistant_name} 🐝 "
                f"تابعة لمتجر {store_name}.\n"
                f"موجودة عشان أرد عليك بسرعة وأساعدك تكمل طلبك بسهولة."
            ),
        ]
    return [
        (
            f"نعم 😊\n"
            f"أنا المساعدة الذكية لمتجر {store_name} 🐝، "
            f"موجودة على مدار الساعة أساعد الفريق في الرد السريع وخدمة العملاء.\n"
            f"وش أقدر أساعدك فيه؟ 👍"
        ),
        (
            f"أنا مساعدة ذكية تابعة لـ {store_name} 🐝\n"
            f"أرد على استفساراتك عن المنتجات والأسعار وأساعدك تكمل الطلب 👍"
        ),
    ]


def render_identity_reply(
    db: Any,
    *,
    tenant_id: int,
    topic: str,
) -> str:
    """Render a deterministic identity / greeting reply.

    Uses the merchant's configured assistant name when available, picks
    one of several warm variants so the same trigger does not get the
    same line every time, and follows the global emoji rules (1-2 max,
    🌷 for greeting, 🐝 for the Nahla persona hook, 👍 for help)."""
    assistant_name = _load_assistant_name(db, tenant_id)
    store_name = _load_store_name(db, tenant_id) or "متجرنا"

    if topic == "greeting":
        return random.choice(_greeting_variants(assistant_name, store_name))
    return random.choice(_identity_variants(assistant_name, store_name))


# ── Mode-aware prompt augmentation (for legacy fallback) ─────────────────────
#
# Mode overlays are now SHORT per-turn deltas — the canonical Nahla
# persona (tone, emoji rules, anti-repeat, no-tech-talk) is injected
# separately by `modules.ai.prompts.nahla_persona`. We only state what
# is special about THIS turn so the model is not re-told the persona
# on every round.
#
# The one exception is MODE_SUPPORT_ESCALATION: it explicitly REVOKES
# the persona's emoji guidance so apologies stay serious.

def mode_prompt_overlay(decision: ModeDecision) -> str:
    """Per-turn instruction delta for the resolved conversation mode.
    Designed to layer ON TOP of the Nahla persona — never to replace it."""
    mode = decision.mode

    if mode == MODE_LIVE_CHAT:
        if decision.free_form_override:
            return (
                "## وضع هذه الجولة: محادثة حيّة بعد تذكير سلة سابق\n"
                "- لا تكرّري رسائل حفظ الطلب أو استرجاع السلة.\n"
                "- ركّزي على آخر رسالة من العميل وردّي عليها مباشرة "
                "بأسلوب طبيعي."
            )
        return (
            "## وضع هذه الجولة: محادثة حيّة طبيعية\n"
            "- ركّزي على آخر رسالة من العميل وردّي عليها مباشرة "
            "دون تكرار."
        )

    if mode == MODE_CHECKOUT_ASSIST:
        return (
            "## وضع هذه الجولة: مساعدة في إتمام الشراء\n"
            "- ساعدي العميل في الدفع أو إكمال الطلب الحالي بدون تشتيت.\n"
            "- عند إرسال رابط الدفع يكفي 👍 في نهاية الجملة."
        )

    if mode == MODE_POST_PURCHASE:
        return (
            "## وضع هذه الجولة: متابعة ما بعد الشراء\n"
            "- ساعدي العميل في تتبع الطلب أو الاستفسار عن الشحن.\n"
            "- استخدمي 🚚 عند ذكر الشحن، بحد أقصى مرة واحدة في الرسالة."
        )

    if mode == MODE_SUPPORT_ESCALATION:
        # Hard override of the persona's emoji guidance for this turn.
        return (
            "## وضع هذه الجولة: تصعيد لخدمة العملاء أو شكوى\n"
            "- اعتذري بلطف ووضّحي أنك ستحوّلين المحادثة لفريق المتجر.\n"
            "- ⚠️ لا تستخدمي أي إيموجي في هذه الرسالة — النبرة جدّية "
            "ومحترمة، حتى لو كانت قواعد الشخصية تسمح عادةً بالإيموجي.\n"
            "- لا تعدي بحلول لا يمكنك ضمانها؛ اكتفي بأنك ستحوّلين الطلب."
        )

    return ""
