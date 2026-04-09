"""
core/conversation_engine.py  v2
────────────────────────────────
Nahla Platform Brain — Production-Grade Stateful Conversation Engine

Implements all 7 architectural requirements:
  1. Rule-first execution   — Claude called only when needed; reason logged
  2. Semantic deduplication — by intent key, not text
  3. Idempotency            — message_id tracking, double-process prevention
  4. Structured context     — state block + history both passed to Claude
  5. Stage transitions      — explicit exit criteria per stage
  6. FactGuard              — Claude cannot hallucinate Nahla platform facts
  7. Observability          — full turn logged to ConversationTrace

Design principle:
  The AI generates language — the system controls logic.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.engine")

HISTORY_WINDOW     = 15   # messages sent to Claude
PLATFORM_TENANT_ID = 1    # Platform Brain lives on tenant 1

# ═══════════════════════════════════════════════════════════════════════════════
# 1. FACT GUARD — ground truth for Nahla platform (Claude never invents these)
# ═══════════════════════════════════════════════════════════════════════════════

class FactGuard:
    """
    Single source of truth for Nahla platform facts.
    Injected verbatim into Claude's system prompt so it can ONLY
    quote these values — never invent pricing, features, or integrations.
    """

    # These are injected from DB at startup via build_nahla_system_prompt,
    # but this block acts as a hard fallback that ALWAYS appears in the prompt.
    STATIC_FACTS = {
        "trial_days":           14,
        "trial_requires_card":  False,
        "plans": {
            "Starter":  {"price_sar": 899,  "monthly": True},
            "Pro":      {"price_sar": 1499, "monthly": True},
            "Business": {"price_sar": 2499, "monthly": True},
        },
        "integrations":    ["سلة", "زد"],
        "register_url":    "https://app.nahlah.ai/register",
        "billing_url":     "https://app.nahlah.ai/billing",
        "support_email":   "support@nahlah.ai",
        "founder_wa":      "https://wa.me/966555906901",
        "features": [
            "ردود واتساب ذكية 24/7",
            "استرجاع السلات المتروكة",
            "الطيار الآلي — إكمال الطلبات تلقائياً",
            "إعادة الطلب التنبؤي",
            "تكامل مع سلة وزد مباشرة",
            "تحليلات المبيعات",
        ],
    }

    @classmethod
    def build_fact_block(cls) -> str:
        """
        Returns a formatted fact block to prepend to Claude's system prompt.
        Claude is explicitly forbidden from contradicting these values.
        """
        p = cls.STATIC_FACTS["plans"]
        f = cls.STATIC_FACTS["features"]
        features_ar = "\n".join(f"  • {feat}" for feat in f)

        return f"""
══════════════════════════════════════════════════════
حقائق نحلة الرسمية — لا تخترع أرقاماً أو معلومات خارج هذا الإطار
══════════════════════════════════════════════════════
التجربة المجانية: {cls.STATIC_FACTS['trial_days']} يوم — بدون بطاقة ائتمان.

الباقات والأسعار:
  • Starter  — {p['Starter']['price_sar']} ريال/شهر
  • Pro      — {p['Pro']['price_sar']} ريال/شهر
  • Business — {p['Business']['price_sar']} ريال/شهر

التكاملات المدعومة: سلة وزد فقط (الآن).

المميزات:
{features_ar}

روابط:
  • التسجيل: {cls.STATIC_FACTS['register_url']}
  • الباقات: {cls.STATIC_FACTS['billing_url']}
  • الدعم:   {cls.STATIC_FACTS['support_email']}

قاعدة صارمة: لا تذكر أرقاماً أو مميزات أو تواريخ غير مذكورة أعلاه.
إذا لم تعرف الإجابة اكتب: "تواصل مع الدعم: support@nahlah.ai"
══════════════════════════════════════════════════════
"""

    @classmethod
    def verify_reply(cls, reply: str) -> Tuple[bool, List[str]]:
        """
        Scan Claude's reply for known hallucination patterns.
        Returns (is_clean, list_of_issues).
        Currently detects wrong pricing numbers.
        """
        issues: List[str] = []
        valid_prices = {str(v["price_sar"]) for v in cls.STATIC_FACTS["plans"].values()}
        import re
        prices_in_reply = set(re.findall(r"\b(\d{3,5})\b", reply))
        suspicious = prices_in_reply - valid_prices - {"14", "24", "30", "60", "90", "1", "7", "3"}
        if suspicious:
            issues.append(f"suspicious_numbers:{suspicious}")
        return (len(issues) == 0, issues)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODEL
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConversationSlots:
    """Structured slots collected during the Platform Brain funnel."""
    platform:      Optional[str] = None   # سلة | زد | other
    store_size:    Optional[str] = None   # small | large
    has_whatsapp:  Optional[bool] = None
    goals:         List[str]     = field(default_factory=list)
    merchant_name: Optional[str] = None

    def as_context_block(self) -> str:
        lines = []
        if self.platform:
            lines.append(f"المنصة: {self.platform}")
        if self.store_size:
            label = "صغير/ناشئ" if self.store_size == "small" else "متوسط/كبير"
            lines.append(f"حجم المتجر: {label}")
        if self.goals:
            lines.append(f"الأهداف: {', '.join(self.goals)}")
        if self.merchant_name:
            lines.append(f"اسم التاجر: {self.merchant_name}")
        return "\n".join(lines) if lines else "لا توجد معلومات بعد"


@dataclass
class ConversationState:
    """
    Complete per-user state for the Platform Brain.
    Persisted as JSON in PostgreSQL.
    """
    phone:            str
    # tenant that owns this conversation (set at load time, not persisted in JSON)
    tenant_id:        Optional[int]    = field(default=None, compare=False, repr=False)
    # ── Stage (5. Stage Transition) ──────────────────────────────────────────
    stage:            str              = "discovery"
    # ── Slots ────────────────────────────────────────────────────────────────
    slots:            ConversationSlots = field(default_factory=ConversationSlots)
    # ── 2. Semantic Deduplication — keys asked so far ────────────────────────
    asked_keys:       List[str]        = field(default_factory=list)
    # ── 3. Idempotency — processed WhatsApp message IDs ──────────────────────
    processed_ids:    List[str]        = field(default_factory=list)
    # ── Counters & scores ────────────────────────────────────────────────────
    turn:             int              = 0
    purchase_score:   int              = 0      # 0-10
    # ── Last action tracking ─────────────────────────────────────────────────
    last_action:      Optional[str]   = None
    last_question:    Optional[str]   = None
    recommended_plan: Optional[str]   = None
    lang:             str              = "ar"
    updated_at:       float            = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["slots"] = asdict(self.slots)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationState":
        raw    = dict(data)
        slots_raw = raw.pop("slots", {})
        valid_slot = ConversationSlots.__dataclass_fields__
        slots  = ConversationSlots(**{k: v for k, v in slots_raw.items() if k in valid_slot})
        valid  = cls.__dataclass_fields__
        return cls(slots=slots, **{k: v for k, v in raw.items() if k in valid})


# ═══════════════════════════════════════════════════════════════════════════════
# 3. IDEMPOTENCY GUARD
# ═══════════════════════════════════════════════════════════════════════════════

class IdempotencyGuard:
    """
    Prevents processing the same WhatsApp message twice.
    Meta may deliver duplicate webhooks on retries.
    """
    MAX_STORED_IDS = 50   # rolling window of last N message IDs

    @classmethod
    def is_duplicate(cls, state: ConversationState, message_id: str) -> bool:
        return message_id in state.processed_ids

    @classmethod
    def mark_processed(cls, state: ConversationState, message_id: str) -> None:
        if message_id not in state.processed_ids:
            state.processed_ids.append(message_id)
        # Keep rolling window
        if len(state.processed_ids) > cls.MAX_STORED_IDS:
            state.processed_ids = state.processed_ids[-cls.MAX_STORED_IDS:]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. INTENT ENGINE — rule-based, runs before Claude
# ═══════════════════════════════════════════════════════════════════════════════

class IntentEngine:
    """
    Rule-based intent classifier.
    Runs in <1ms. Determines what the user WANTS.
    Order of patterns matters — most specific first.
    """

    # 1. Subscribe / Checkout — HIGHEST PRIORITY
    _SUBSCRIBE = (
        "أبي أشترك", "ابي اشترك", "أريد الاشتراك", "اريد الاشتراك",
        "أبي أبدأ", "ابي ابدا", "أبدأ الآن", "ابدا الان",
        "كيف أسجل", "كيف اسجل", "سجّلني", "سجلني",
        "اشتراك الآن", "اشتراك الان", "اشترك الان", "اشترك الآن",
        "أبغى أشترك", "ابغى اشترك", "أبغى أبدأ", "ابغى ابدا",
        "وين أسجل", "وين اسجل", "كيف أبدأ", "كيف ابدا",
        "how do i subscribe", "i want to subscribe", "sign me up",
        "register now", "how do i start", "start now",
    )

    # 2. Payment link — explicit request for link
    _PAYMENT = (
        "أرسل رابط الدفع", "ارسل رابط الدفع", "رابط الدفع", "أبي أدفع",
        "ابي ادفع", "أبغى أدفع", "أبي الرابط", "ابي الرابط",
        "وين الرابط", "ارسل الرابط", "أرسل الرابط",
        "send payment link", "payment link", "how to pay", "send the link",
        "payment url", "pay now",
    )

    # 3. Trial
    _TRIAL = (
        "أبي أجرب", "ابي اجرب", "أبغى أجرب", "ابغى اجرب",
        "تجربة مجانية", "تجربة مجانيه", "جرب مجانا", "جرب مجاناً",
        "i want to try", "free trial", "try for free", "start trial",
    )

    # 4. Pricing
    _PRICE = (
        "كم الأسعار", "كم الاسعار", "كم السعر", "وش الأسعار", "وش الاسعار",
        "وش الباقات", "أسعار", "اسعار", "الأسعار", "الباقات", "باقات",
        "تكلفة", "سعر", "كم تكلف", "كم ثمنها", "كم ثمن",
        "how much", "pricing", "plans", "price", "cost",
        "باقة النمو", "باقة بروفيشنال", "starter", "pro", "business",
    )

    # 5. How it works
    _HOW = (
        "كيف تشتغل", "كيف يشتغل", "كيف تعمل", "وش تسوي", "وش تعمل",
        "كيف تساعد", "وش المنصة", "عرفني", "اشرح لي", "ايش هي نحلة",
        "وش هي نحلة", "how does it work", "what does it do", "explain",
        "tell me more", "what is nahla",
    )

    # 6. Features
    _FEATURES = (
        "المميزات", "مميزات", "الخصائص", "خصائص", "وش فيها", "وش تقدر تسوي",
        "قدرات", "الخدمات", "features", "what can it do", "capabilities",
    )

    # 7. Platform answers
    _SALLA = ("سلة", "salla",)
    _ZID   = ("زد", "zid",)

    # 8. Store size
    _SMALL = (
        "صغير", "ناشئ", "مبتدئ", "بداية", "طلبات قليلة",
        "مو كبير", "ما عندي طلبات كثير", "small", "starter", "beginner", "new store",
    )
    _LARGE = (
        "كبير", "متوسط", "طلبات كثيرة", "طلبات كثير", "طلبات يومية",
        "متجر كبير", "large", "medium", "big store", "enterprise",
    )

    # 9. Founder / support
    _FOUNDER = (
        "المؤسس", "مؤسس", "المدير التنفيذي", "تركي",
        "تواصل مع", "رقم المدير", "رقم المؤسس",
        "founder", "ceo", "contact founder",
    )
    _SUPPORT = (
        "مشكلة", "خطأ", "لا يشتغل", "معطل", "دعم فني",
        "support", "problem", "error", "not working", "issue", "help",
    )

    # 10. Greeting (only short messages)
    _GREET = (
        "هلا", "هلو", "هاي", "مرحبا", "مرحباً", "السلام عليكم", "سلام",
        "صباح الخير", "مساء الخير", "أهلاً", "أهلا", "وعليكم السلام",
        "hi", "hello", "hey", "good morning", "good evening",
    )

    @classmethod
    def classify(cls, text: str, state: ConversationState) -> Tuple[str, float]:
        """
        Returns (intent_label, confidence_0_to_1).
        Confidence 1.0 = rule matched, 0.5 = greeting, 0.3 = general fallback.
        """
        t = _normalize(text.lower().strip())

        if cls._m(t, cls._PAYMENT):   return "request_payment_link", 1.0
        if cls._m(t, cls._SUBSCRIBE): return "subscribe_now",         1.0
        if cls._m(t, cls._TRIAL):     return "request_trial",         1.0

        if cls._m(t, cls._PRICE):    return "ask_price",        1.0
        if cls._m(t, cls._HOW):      return "ask_how_it_works", 0.9
        if cls._m(t, cls._FEATURES): return "ask_features",     0.9

        # Platform — only if platform slot is not filled yet OR explicitly mentioned
        if cls._m(t, cls._SALLA): return "platform_salla", 1.0
        if cls._m(t, cls._ZID):   return "platform_zid",   1.0

        if cls._m(t, cls._SMALL): return "store_small", 0.9
        if cls._m(t, cls._LARGE): return "store_large",  0.9

        if cls._m(t, cls._FOUNDER): return "contact_founder",  1.0
        if cls._m(t, cls._SUPPORT): return "request_support",  0.9

        if len(text) <= 60 and cls._m(t, cls._GREET):
            return "greeting", 0.9

        return "general", 0.3

    @staticmethod
    def _m(text: str, kws: tuple) -> bool:
        return any(kw in text for kw in kws)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. STAGE TRANSITION ENGINE — explicit exit criteria
# ═══════════════════════════════════════════════════════════════════════════════

# Stage constants
S_DISCOVERY      = "discovery"
S_QUALIFICATION  = "qualification"
S_RECOMMENDATION = "recommendation"
S_CHECKOUT       = "checkout"
S_ONBOARDED      = "onboarded"

class StageTransitionEngine:
    """
    Explicit stage transition rules.
    Every stage has clear entry AND exit conditions.

    discovery      → any engagement (turn > 0 OR any slot filled)
    qualification  → platform OR store_size known
    recommendation → platform AND store_size both known
    checkout       → purchase_score >= 7 OR explicit buy intent
    onboarded      → checkout link confirmed sent
    """

    @classmethod
    def advance(cls, state: ConversationState, intent: str) -> Optional[str]:
        """
        Evaluate whether the state should advance to the next stage.
        Returns the new stage name, or None if no change.
        """
        current = state.stage

        # Forced transitions regardless of stage
        if intent in ("subscribe_now", "request_payment_link", "request_trial"):
            if current != S_ONBOARDED:
                return S_CHECKOUT

        if state.purchase_score >= 7 and current not in (S_CHECKOUT, S_ONBOARDED):
            return S_CHECKOUT

        # Progressive transitions
        if current == S_DISCOVERY:
            if state.slots.platform or state.slots.store_size or state.turn > 1:
                return S_QUALIFICATION

        if current == S_QUALIFICATION:
            if state.slots.platform and state.slots.store_size:
                return S_RECOMMENDATION

        if current == S_RECOMMENDATION:
            if state.purchase_score >= 5:
                return S_CHECKOUT

        return None  # No transition

    @classmethod
    def apply(cls, state: ConversationState, intent: str) -> Optional[str]:
        """Apply transition if warranted. Returns old→new string for logging."""
        new_stage = cls.advance(state, intent)
        if new_stage and new_stage != state.stage:
            old = state.stage
            state.stage = new_stage
            logger.info("[Stage] %s → %s (intent=%s phone=%s)", old, new_stage, intent, state.phone)
            return f"{old}→{new_stage}"
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 1. DECISION ENGINE — next_best_action with decision_reason
# ═══════════════════════════════════════════════════════════════════════════════

# Action labels (exported for use in webhook)
SEND_CHECKOUT_LINK = "SEND_CHECKOUT_LINK"
SEND_TRIAL_LINK    = "SEND_TRIAL_LINK"
SHOW_PLANS         = "SHOW_PLANS"
SHOW_WELCOME_MENU  = "SHOW_WELCOME_MENU"
FILL_SLOT_PLATFORM = "FILL_SLOT_PLATFORM"
FILL_SLOT_SIZE     = "FILL_SLOT_SIZE"
SEND_FOUNDER_LINK  = "SEND_FOUNDER_LINK"
ESCALATE_SUPPORT   = "ESCALATE_SUPPORT"
GENERATE_AI_REPLY  = "GENERATE_AI_REPLY"

# Actions that NEVER call Claude (rule-based, deterministic)
DETERMINISTIC_ACTIONS = {
    SEND_CHECKOUT_LINK,
    SEND_TRIAL_LINK,
    SHOW_PLANS,
    SHOW_WELCOME_MENU,
    FILL_SLOT_PLATFORM,
    FILL_SLOT_SIZE,
    SEND_FOUNDER_LINK,
    ESCALATE_SUPPORT,
}


class DecisionEngine:
    """
    Deterministic next-best-action selector.
    Returns (action, reason) — reason is logged to ConversationTrace.

    Rule: every non-GENERATE_AI_REPLY action is cheaper, faster, and more reliable.
    Claude is called ONLY for GENERATE_AI_REPLY.
    """

    @classmethod
    def decide(cls, intent: str, state: ConversationState) -> Tuple[str, str]:
        """Returns (action_label, decision_reason)."""

        # ── TIER 1: Explicit buy intent → immediate checkout, NO questions ──────
        if intent == "request_payment_link":
            return SEND_CHECKOUT_LINK, "explicit_payment_link_request"

        if intent == "subscribe_now":
            return SEND_CHECKOUT_LINK, "explicit_subscribe_intent"

        if intent == "request_trial":
            return SEND_TRIAL_LINK, "explicit_trial_request"

        # ── TIER 2: Stage override → already in checkout, send link ─────────────
        if state.stage == S_CHECKOUT:
            return SEND_CHECKOUT_LINK, f"stage=checkout_push"

        # ── TIER 3: Deterministic info responses ─────────────────────────────────
        if intent == "ask_price":
            return SHOW_PLANS, "price_inquiry_rule"

        if intent == "contact_founder":
            return SEND_FOUNDER_LINK, "founder_contact_rule"

        if intent == "request_support":
            return ESCALATE_SUPPORT, "support_escalation_rule"

        if intent == "greeting":
            return SHOW_WELCOME_MENU, "greeting_rule"

        # ── TIER 4: Slot-filling → deterministic follow-up ───────────────────────
        if intent in ("platform_salla", "platform_zid"):
            return FILL_SLOT_PLATFORM, f"slot_fill:{intent}"

        if intent in ("store_small", "store_large"):
            return FILL_SLOT_SIZE, f"slot_fill:{intent}"

        # ── TIER 5: AI reply — only for open-ended questions ─────────────────────
        # (ask_how_it_works, ask_features, general)
        reason = f"no_rule_match:intent={intent}:stage={state.stage}"
        return GENERATE_AI_REPLY, reason


# ═══════════════════════════════════════════════════════════════════════════════
# 2. SEMANTIC DEDUPLICATION GUARD
# ═══════════════════════════════════════════════════════════════════════════════

# Semantic question keys — each KEY represents a unique question concept.
# Regardless of HOW the question is phrased, the KEY must only be asked ONCE.
QUESTION_KEYS = {
    "ask_platform":   "متجرك على أي منصة؟",       # any phrasing of "what platform"
    "ask_store_size": "حجم متجرك صغير أو كبير؟",   # any phrasing of "what size"
    "ask_goal":       "وش هدفك الرئيسي من نحلة؟",   # any phrasing of "what do you want"
    "ask_whatsapp":   "عندك واتساب Business جاهز؟", # any phrasing of "do you have WA"
}


class DeduplicationGuard:
    """
    Semantic deduplication — by KEY, not by text.
    Prevents asking the same CONCEPT twice even if phrased differently.
    """

    @classmethod
    def can_ask(cls, state: ConversationState, key: str) -> bool:
        """True if this semantic question has NOT been asked yet."""
        return key not in state.asked_keys

    @classmethod
    def mark_asked(cls, state: ConversationState, key: str) -> None:
        if key not in state.asked_keys:
            state.asked_keys.append(key)

    @classmethod
    def should_ask_platform(cls, state: ConversationState) -> bool:
        return state.slots.platform is None and cls.can_ask(state, "ask_platform")

    @classmethod
    def should_ask_store_size(cls, state: ConversationState) -> bool:
        return state.slots.store_size is None and cls.can_ask(state, "ask_store_size")


# ═══════════════════════════════════════════════════════════════════════════════
# SLOT UPDATER
# ═══════════════════════════════════════════════════════════════════════════════

class SlotUpdater:

    @staticmethod
    def update(state: ConversationState, intent: str) -> List[str]:
        """Fill slot values from intent. Returns list of updated slot names."""
        updated: List[str] = []

        if intent == "platform_salla":
            state.slots.platform = "سلة"
            DeduplicationGuard.mark_asked(state, "ask_platform")
            updated.append("platform=سلة")

        elif intent == "platform_zid":
            state.slots.platform = "زد"
            DeduplicationGuard.mark_asked(state, "ask_platform")
            updated.append("platform=زد")

        elif intent == "store_small":
            state.slots.store_size = "small"
            DeduplicationGuard.mark_asked(state, "ask_store_size")
            updated.append("store_size=small")

        elif intent == "store_large":
            state.slots.store_size = "large"
            DeduplicationGuard.mark_asked(state, "ask_store_size")
            updated.append("store_size=large")

        # Purchase score adjustments
        if intent in ("ask_price", "ask_features", "ask_how_it_works"):
            state.purchase_score = min(10, state.purchase_score + 1)
        if intent in ("request_trial", "subscribe_now", "request_payment_link"):
            state.purchase_score = 10
            updated.append("purchase_score=10")

        return updated


# ═══════════════════════════════════════════════════════════════════════════════
# 4. CONTEXT BUILDER — structured state + recent history
# ═══════════════════════════════════════════════════════════════════════════════

class ContextBuilder:
    """
    Builds the full input for Claude:
      - Structured state block (deterministic facts about this conversation)
      - Fact guard block (Nahla platform ground truth)
      - Recent message history (last N turns)
    """

    @classmethod
    def build_system_injection(
        cls,
        state: ConversationState,
        next_action: str,
        decision_reason: str,
    ) -> str:
        """
        Returns a block prepended to the system prompt.
        Tells Claude exactly what it knows and what it should do next.
        """
        stage_guidance = {
            S_DISCOVERY:      "أنت في مرحلة التعرف. اكتشف وضع التاجر.",
            S_QUALIFICATION:  "أنت في مرحلة التأهيل. اجمع معلومات المنصة والحجم.",
            S_RECOMMENDATION: "أنت في مرحلة التوصية. اقترح الباقة المناسبة.",
            S_CHECKOUT:       "التاجر جاهز للاشتراك. لا تسأل أسئلة إضافية — أرسل الرابط فقط.",
            S_ONBOARDED:      "التاجر مشترك. ساعده في الإعداد والاستخدام.",
        }.get(state.stage, "")

        asked_labels = [
            {"ask_platform": "المنصة", "ask_store_size": "حجم المتجر",
             "ask_goal": "الهدف", "ask_whatsapp": "واتساب Business"}.get(k, k)
            for k in state.asked_keys
        ]

        block = f"""
══════════════════════════════════════════════
حالة المحادثة الحالية (لا تتجاهلها)
══════════════════════════════════════════════
المرحلة: {state.stage} — {stage_guidance}
Turn رقم: {state.turn}
نقاط الشراء: {state.purchase_score}/10

معلومات التاجر المعروفة:
{state.slots.as_context_block()}

أسئلة طُرحت بالفعل (لا تكررها أبداً):
{', '.join(asked_labels) if asked_labels else 'لا شيء حتى الآن'}

الباقة المقترحة: {state.recommended_plan or 'لم تُحدَّد بعد'}
══════════════════════════════════════════════
"""
        return block

    @classmethod
    def build_messages(
        cls,
        history: List[Dict],
        current_message: str,
    ) -> List[Dict]:
        """
        Returns Claude messages array with proper role alternation.
        history: [{direction: inbound|outbound, body: str}]
        """
        messages: List[Dict] = []

        for turn in history[-HISTORY_WINDOW:]:
            role = "user" if turn.get("direction") == "inbound" else "assistant"
            body = (turn.get("body") or "").strip()
            if not body or body.startswith("[button:"):
                continue
            # Ensure no consecutive same-role (Claude requirement)
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n{body}"
            else:
                messages.append({"role": role, "content": body})

        # Ensure current message is the final user turn
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": current_message})
        elif messages[-1]["content"] != current_message:
            messages.append({"role": "user", "content": current_message})

        return messages


# ═══════════════════════════════════════════════════════════════════════════════
# 7. OBSERVABILITY — turn logging to ConversationTrace
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TurnLog:
    """Everything that happened during one message processing cycle."""
    phone:              str
    turn:               int
    raw_message:        str
    detected_intent:    str
    confidence:         float
    extracted_slots:    List[str]
    stage_before:       str
    stage_after:        str
    stage_transition:   Optional[str]
    decision:           str
    decision_reason:    str
    ai_called:          bool
    duplicate_blocked:  bool        = False
    idempotency_skip:   bool        = False
    fact_guard_issues:  List[str]   = field(default_factory=list)
    response_text:      Optional[str] = None
    latency_ms:         int          = 0


class ObservabilityLogger:
    """
    Writes a TurnLog to the ConversationTrace table.
    Silently fails — observability must never crash the main flow.
    """

    @staticmethod
    def log(db, log: TurnLog, tenant_id: Optional[int] = None) -> None:
        if not db:
            return
        _tid = tenant_id if tenant_id is not None else PLATFORM_TENANT_ID
        try:
            from models import ConversationTrace  # noqa: PLC0415
            trace = ConversationTrace(
                tenant_id=_tid,
                customer_phone=log.phone,
                session_id=log.phone,
                turn=log.turn,
                message=log.raw_message[:1000],
                detected_intent=log.detected_intent,
                confidence=log.confidence,
                orchestrator_used=log.ai_called,
                fact_guard_modified=bool(log.fact_guard_issues),
                fact_guard_claims={"issues": log.fact_guard_issues} if log.fact_guard_issues else None,
                actions_triggered={
                    "decision":           log.decision,
                    "decision_reason":    log.decision_reason,
                    "stage_before":       log.stage_before,
                    "stage_after":        log.stage_after,
                    "stage_transition":   log.stage_transition,
                    "extracted_slots":    log.extracted_slots,
                    "duplicate_blocked":  log.duplicate_blocked,
                    "idempotency_skip":   log.idempotency_skip,
                },
                response_text=(log.response_text or "")[:2000],
                latency_ms=log.latency_ms,
            )
            db.add(trace)
            db.commit()
        except Exception as exc:
            logger.warning("[Observability] Failed to write trace: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════

class StateManager:
    """
    Load and save ConversationState from/to PostgreSQL.
    State: Conversation.extra_metadata (JSONB keyed by phone).
    Messages: MessageEvent table.
    """

    @classmethod
    def load(cls, db, phone: str, tenant_id: Optional[int] = None) -> "ConversationState":
        _tid = tenant_id if tenant_id is not None else PLATFORM_TENANT_ID
        try:
            from models import Conversation  # noqa: PLC0415
            conv = (
                db.query(Conversation)
                .filter(
                    Conversation.tenant_id == _tid,
                    Conversation.extra_metadata["phone"].astext == phone,
                )
                .order_by(Conversation.id.desc())
                .first()
            )
            if conv and conv.extra_metadata and "stage" in conv.extra_metadata:
                return ConversationState.from_dict(dict(conv.extra_metadata))
        except Exception as exc:
            logger.warning("[StateManager] load error phone=%s tenant=%s: %s", phone, _tid, exc)
        state = ConversationState(phone=phone)
        state.tenant_id = _tid   # carry it for downstream save
        return state

    @classmethod
    def save(cls, db, state: "ConversationState", tenant_id: Optional[int] = None) -> Optional[Any]:
        # Prefer explicit tenant_id arg, then the one attached to the state, then platform default
        _tid = tenant_id if tenant_id is not None else getattr(state, "tenant_id", None) or PLATFORM_TENANT_ID
        try:
            from models import Conversation  # noqa: PLC0415
            state.updated_at = time.time()
            meta = state.to_dict()
            conv = (
                db.query(Conversation)
                .filter(
                    Conversation.tenant_id == _tid,
                    Conversation.extra_metadata["phone"].astext == state.phone,
                )
                .order_by(Conversation.id.desc())
                .first()
            )
            if conv:
                conv.extra_metadata = meta
            else:
                conv = Conversation(
                    tenant_id=_tid,
                    status="active",
                    extra_metadata=meta,
                )
                db.add(conv)
            db.commit()
            return conv
        except Exception as exc:
            logger.error("[StateManager] save error phone=%s tenant=%s: %s", state.phone, _tid, exc)
            try:
                db.rollback()
            except Exception:
                pass
            return None

    @classmethod
    def save_message(cls, db, phone: str, body: str, direction: str,
                     conversation_id: Optional[int] = None,
                     tenant_id: Optional[int] = None) -> None:
        _tid = tenant_id if tenant_id is not None else PLATFORM_TENANT_ID
        try:
            from models import MessageEvent  # noqa: PLC0415
            db.add(MessageEvent(
                tenant_id=_tid,
                conversation_id=conversation_id,
                direction=direction,
                body=body,
                event_type="whatsapp",
                extra_metadata={"phone": phone},
            ))
            db.commit()
        except Exception as exc:
            logger.warning("[StateManager] save_message error: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass

    @classmethod
    def load_history(cls, db, phone: str, limit: int = HISTORY_WINDOW,
                     tenant_id: Optional[int] = None) -> List[Dict]:
        _tid = tenant_id if tenant_id is not None else PLATFORM_TENANT_ID
        try:
            from models import MessageEvent  # noqa: PLC0415
            events = (
                db.query(MessageEvent)
                .filter(
                    MessageEvent.tenant_id == _tid,
                    MessageEvent.extra_metadata["phone"].astext == phone,
                )
                .order_by(MessageEvent.id.desc())
                .limit(limit)
                .all()
            )
            return [{"direction": e.direction, "body": e.body} for e in reversed(events)]
        except Exception as exc:
            logger.warning("[StateManager] load_history error: %s", exc)
            return []


# ═══════════════════════════════════════════════════════════════════════════════
# PLAN RECOMMENDER
# ═══════════════════════════════════════════════════════════════════════════════

def recommend_plan(state: ConversationState) -> str:
    if state.slots.store_size == "large":
        return "Business"
    elif state.slots.store_size == "medium":
        return "Pro"
    else:
        return "Starter"


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize(text: str) -> str:
    """Normalize Arabic text for keyword matching."""
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ة", "ه").replace("ى", "ي")
    for ch in "ًٌٍَُِّْ":
        text = text.replace(ch, "")
    return text
