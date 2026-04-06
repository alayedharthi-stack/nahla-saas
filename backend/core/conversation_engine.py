"""
core/conversation_engine.py
────────────────────────────
Nahla Platform Brain — Stateful Conversation Engine

Implements the three-layer intelligence required to fix:
  ❌ repeated questions that were already answered
  ❌ losing conversation context between messages
  ❌ misunderstanding purchase intent
  ❌ over-relying on LLM to control conversation logic

Architecture:
  ┌─────────────────────────────────────────┐
  │  IntentEngine  →  DecisionEngine        │
  │       ↓               ↓                │
  │  SlotFiller    →  ActionRouter          │
  │       ↓               ↓                │
  │  ContextBuilder → Claude (only if needed)│
  └─────────────────────────────────────────┘

State is persisted in PostgreSQL (Conversation.extra_metadata + MessageEvent).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.engine")

# ── How many recent messages to pass to Claude for context ─────────────────────
HISTORY_WINDOW = 15

# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODEL
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConversationSlots:
    """Structured slots collected during the Platform Brain funnel."""
    platform:      Optional[str] = None   # salla | zid | other
    store_size:    Optional[str] = None   # small | medium | large
    has_whatsapp:  Optional[bool] = None  # does merchant have WA Business?
    goals:         List[str]     = field(default_factory=list)
    merchant_name: Optional[str] = None

    def filled(self) -> List[str]:
        """Return list of slot names that have been filled."""
        out = []
        if self.platform:      out.append("platform")
        if self.store_size:    out.append("store_size")
        if self.has_whatsapp is not None: out.append("has_whatsapp")
        if self.goals:         out.append("goals")
        return out

    def missing_critical(self) -> List[str]:
        """Return slot names that are still needed for a recommendation."""
        needed = []
        if not self.platform:   needed.append("platform")
        if not self.store_size: needed.append("store_size")
        return needed


@dataclass
class ConversationState:
    """
    Complete state for a single customer conversation with the Platform Brain.
    Persisted as JSON in Conversation.extra_metadata per phone number.
    """
    phone:            str
    stage:            str              = "discovery"
    slots:            ConversationSlots = field(default_factory=ConversationSlots)
    asked:            List[str]        = field(default_factory=list)   # slot questions asked
    turn:             int              = 0
    last_action:      Optional[str]   = None
    last_question:    Optional[str]   = None   # exact text of last question to avoid repeating
    purchase_score:   int              = 0      # 0-10; ≥7 triggers checkout flow
    recommended_plan: Optional[str]   = None
    lang:             str              = "ar"
    updated_at:       float            = field(default_factory=time.time)

    # STAGES:
    # discovery      → user just arrived, no info collected
    # qualification  → learning about platform / store
    # recommendation → suggesting a specific plan
    # checkout       → user is ready to subscribe
    # onboarded      → already has account

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["slots"] = asdict(self.slots)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationState":
        slots_data = data.pop("slots", {})
        slots = ConversationSlots(**{k: v for k, v in slots_data.items()
                                     if k in ConversationSlots.__dataclass_fields__})
        return cls(slots=slots, **{k: v for k, v in data.items()
                                    if k in cls.__dataclass_fields__})


# ═══════════════════════════════════════════════════════════════════════════════
# INTENT ENGINE  (deterministic, runs before Claude)
# ═══════════════════════════════════════════════════════════════════════════════

class IntentEngine:
    """
    Rule-based intent classifier.
    Runs in <1ms before any AI call — determines what the user WANTS.
    """

    _SUBSCRIBE_KW = (
        "أبي أشترك", "ابي اشترك", "أريد الاشتراك", "اريد الاشتراك",
        "أبي أبدأ", "ابي ابدا", "أبدأ الآن", "ابدا الان",
        "كيف أسجل", "كيف اسجل", "سجّلني", "سجلني",
        "اشتراك الآن", "اشتراك الان", "اشترك الان", "اشترك الآن",
        "أبغى أشترك", "ابغى اشترك", "أبغى أبدأ", "ابغى ابدا",
        "وين أسجل", "وين اسجل", "رابط التسجيل", "رابط الاشتراك",
        "how do i subscribe", "i want to subscribe", "sign me up",
        "register now", "let's start", "how do i start", "start now",
    )

    _PAYMENT_KW = (
        "أرسل رابط الدفع", "ارسل رابط الدفع", "رابط الدفع",
        "أبي أدفع", "ابي ادفع", "أبغى أدفع", "ابغى ادفع",
        "أبي الرابط", "ابي الرابط", "وين الرابط",
        "ارسل الرابط", "أرسل الرابط", "send payment link",
        "payment link", "how to pay", "send the link",
    )

    _TRIAL_KW = (
        "أبي أجرب", "ابي اجرب", "أبغى أجرب", "ابغى اجرب",
        "تجربة مجانية", "تجربة مجانيه", "جرب مجانا", "جرب مجاناً",
        "i want to try", "free trial", "try for free", "start trial",
    )

    _PRICE_KW = (
        "كم الأسعار", "كم الاسعار", "كم السعر", "وش الأسعار",
        "وش الاسعار", "وش الباقات", "أسعار", "اسعار", "الأسعار",
        "الباقات", "باقات", "تكلفة", "سعر", "كم تكلف",
        "كم ثمنها", "كم ثمن", "how much", "pricing", "plans",
        "price", "cost", "how much does it cost",
    )

    _HOW_IT_WORKS_KW = (
        "كيف تشتغل", "كيف يشتغل", "كيف تعمل", "وش تسوي",
        "وش تعمل", "كيف تساعد", "وش المنصة", "عرفني",
        "اشرح لي", "ايش هي نحلة", "وش هي نحلة",
        "how does it work", "what does it do", "explain",
        "tell me more", "what is nahla",
    )

    _FEATURES_KW = (
        "المميزات", "مميزات", "الخصائص", "خصائص", "وش فيها",
        "وش تقدر تسوي", "قدرات", "الخدمات",
        "features", "what can it do", "capabilities",
    )

    _PLATFORM_SALLA_KW = (
        "سلة", "salla", "متجري على سلة", "عندي متجر سلة",
    )

    _PLATFORM_ZID_KW = (
        "زد", "zid", "متجري على زد", "عندي متجر زد",
    )

    _STORE_SMALL_KW = (
        "صغير", "ناشئ", "مبتدئ", "بداية", "طلبات قليلة",
        "مو كبير", "ما عندي طلبات كثير", "small", "starter", "beginner",
    )

    _STORE_BIG_KW = (
        "كبير", "متوسط", "طلبات كثيرة", "طلبات كثير", "طلبات يومية كثيرة",
        "متجر كبير", "large", "medium", "big store",
    )

    _FOUNDER_KW = (
        "المؤسس", "مؤسس", "المدير التنفيذي", "تركي", "تواصل مع",
        "رقم المدير", "رقم المؤسس", "founder", "ceo", "contact founder",
    )

    _SUPPORT_KW = (
        "مشكلة", "خطأ", "لا يشتغل", "معطل", "دعم فني",
        "support", "problem", "error", "not working", "issue",
    )

    _GREETING_KW = (
        "هلا", "هلو", "هاي", "مرحبا", "مرحباً", "السلام عليكم", "سلام",
        "صباح الخير", "مساء الخير", "أهلاً", "أهلا", "وعليكم السلام",
        "hi", "hello", "hey", "good morning", "good evening", "yo", "sup",
    )

    @classmethod
    def classify(cls, text: str, state: ConversationState) -> str:
        """
        Return the best intent label for this message.
        Order matters — more specific intents checked first.
        """
        t  = text.lower().strip()
        tN = _normalize_arabic(t)  # remove diacritics for matching

        # 1. Explicit buy/subscribe signals — highest priority
        if cls._matches(tN, cls._PAYMENT_KW):   return "request_payment_link"
        if cls._matches(tN, cls._SUBSCRIBE_KW): return "subscribe_now"
        if cls._matches(tN, cls._TRIAL_KW):     return "request_trial"

        # 2. Pricing / plans
        if cls._matches(tN, cls._PRICE_KW):     return "ask_price"

        # 3. How it works / features
        if cls._matches(tN, cls._HOW_IT_WORKS_KW): return "ask_how_it_works"
        if cls._matches(tN, cls._FEATURES_KW):     return "ask_features"

        # 4. Slot-filling answers
        if cls._matches(tN, cls._PLATFORM_SALLA_KW): return "platform_salla"
        if cls._matches(tN, cls._PLATFORM_ZID_KW):   return "platform_zid"
        if cls._matches(tN, cls._STORE_SMALL_KW):     return "store_small"
        if cls._matches(tN, cls._STORE_BIG_KW):       return "store_large"

        # 5. Support / founder / handoff
        if cls._matches(tN, cls._FOUNDER_KW):  return "contact_founder"
        if cls._matches(tN, cls._SUPPORT_KW):  return "request_support"

        # 6. Greeting (only for very short messages)
        if len(t) <= 50 and cls._matches(tN, cls._GREETING_KW):
            return "greeting"

        # 7. Default
        return "general"

    @staticmethod
    def _matches(text: str, keywords: tuple) -> bool:
        return any(kw in text for kw in keywords)


# ═══════════════════════════════════════════════════════════════════════════════
# DECISION ENGINE  (maps intent + state → next action)
# ═══════════════════════════════════════════════════════════════════════════════

# Action labels
SEND_CHECKOUT_LINK   = "SEND_CHECKOUT_LINK"
SEND_TRIAL_LINK      = "SEND_TRIAL_LINK"
SHOW_PLANS           = "SHOW_PLANS"
SHOW_WELCOME_MENU    = "SHOW_WELCOME_MENU"
ASK_PLATFORM         = "ASK_PLATFORM"
ASK_STORE_SIZE       = "ASK_STORE_SIZE"
FILL_SLOT_PLATFORM   = "FILL_SLOT_PLATFORM"
FILL_SLOT_SIZE       = "FILL_SLOT_SIZE"
SEND_FOUNDER_LINK    = "SEND_FOUNDER_LINK"
ESCALATE_SUPPORT     = "ESCALATE_SUPPORT"
GENERATE_AI_REPLY    = "GENERATE_AI_REPLY"


class DecisionEngine:
    """
    Deterministic next-best-action selector.
    The AI should generate language, not control the system.
    This engine controls the system.
    """

    @classmethod
    def decide(cls, intent: str, state: ConversationState) -> str:
        """Return the action to execute for this intent + state combination."""

        # ── High-priority overrides (always execute regardless of stage) ────────
        if intent in ("request_payment_link", "subscribe_now"):
            return SEND_CHECKOUT_LINK

        if intent == "request_trial":
            return SEND_TRIAL_LINK

        if intent == "contact_founder":
            return SEND_FOUNDER_LINK

        if intent == "request_support":
            return ESCALATE_SUPPORT

        if intent == "greeting":
            return SHOW_WELCOME_MENU

        # ── Slot-filling actions ────────────────────────────────────────────────
        if intent == "platform_salla":
            return FILL_SLOT_PLATFORM

        if intent == "platform_zid":
            return FILL_SLOT_PLATFORM

        if intent in ("store_small", "store_large"):
            return FILL_SLOT_SIZE

        # ── Info requests ───────────────────────────────────────────────────────
        if intent == "ask_price":
            return SHOW_PLANS

        # ── Contextual routing based on stage ──────────────────────────────────
        if intent in ("ask_how_it_works", "ask_features"):
            # After explaining, check if platform slot is missing
            if not state.slots.platform and "platform" not in state.asked:
                return GENERATE_AI_REPLY  # Claude will explain + ask platform
            return GENERATE_AI_REPLY

        # ── General fallback ────────────────────────────────────────────────────
        return GENERATE_AI_REPLY


# ═══════════════════════════════════════════════════════════════════════════════
# DEDUPLICATION GUARD
# ═══════════════════════════════════════════════════════════════════════════════

class DeduplicationGuard:
    """
    Prevents asking the same question twice.
    Must be checked before any question-generating action.
    """

    @staticmethod
    def should_ask_platform(state: ConversationState) -> bool:
        """True only if platform is unknown AND was not recently asked."""
        return (
            state.slots.platform is None
            and "platform" not in state.asked
        )

    @staticmethod
    def should_ask_store_size(state: ConversationState) -> bool:
        """True only if store_size is unknown AND was not recently asked."""
        return (
            state.slots.store_size is None
            and "store_size" not in state.asked
        )

    @staticmethod
    def mark_asked(state: ConversationState, slot: str) -> None:
        if slot not in state.asked:
            state.asked.append(slot)


# ═══════════════════════════════════════════════════════════════════════════════
# CONTEXT BUILDER  (builds Claude message history)
# ═══════════════════════════════════════════════════════════════════════════════

class ContextBuilder:
    """
    Builds the message history array passed to Claude.
    Ensures Claude always has the last N turns of conversation.
    """

    @staticmethod
    def build_messages(history: List[Dict], current_message: str) -> List[Dict]:
        """
        Returns a list of {role, content} dicts for the Claude API.
        history: list of {direction: inbound/outbound, body: str}
        """
        messages = []
        for turn in history[-HISTORY_WINDOW:]:
            role = "user" if turn.get("direction") == "inbound" else "assistant"
            body = turn.get("body", "").strip()
            if body:
                messages.append({"role": role, "content": body})

        # Ensure last message is from user (Claude requirement)
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": current_message})
        elif messages[-1]["content"] != current_message:
            messages.append({"role": "user", "content": current_message})

        return messages

    @staticmethod
    def build_context_prefix(state: ConversationState) -> str:
        """
        Returns a short context block prepended to the system prompt.
        Tells Claude what is already known about this user.
        """
        lines = []
        if state.slots.platform:
            lines.append(f"- منصة التاجر: {state.slots.platform}")
        if state.slots.store_size:
            size_ar = {"small": "صغير/ناشئ", "medium": "متوسط", "large": "كبير"}.get(
                state.slots.store_size, state.slots.store_size
            )
            lines.append(f"- حجم المتجر: {size_ar}")
        if state.recommended_plan:
            lines.append(f"- الباقة المقترحة: {state.recommended_plan}")
        if state.asked:
            asked_ar = {
                "platform": "المنصة", "store_size": "حجم المتجر",
                "has_whatsapp": "واتساب Business",
            }
            asked_labels = [asked_ar.get(a, a) for a in state.asked]
            lines.append(f"- أسئلة طرحتها بالفعل (لا تكررها): {', '.join(asked_labels)}")
        if state.stage == "checkout":
            lines.append("- التاجر جاهز للاشتراك — لا تسأل أسئلة إضافية.")

        if not lines:
            return ""

        return (
            "\n══════════════════════════════\n"
            "معلومات التاجر الحالية:\n"
            + "\n".join(lines)
            + "\n══════════════════════════════\n"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# STATE PERSISTENCE  (PostgreSQL via Conversation + MessageEvent)
# ═══════════════════════════════════════════════════════════════════════════════

class StateManager:
    """
    Load and save ConversationState from/to the database.
    State is stored in Conversation.extra_metadata keyed by phone number.
    Messages are stored in MessageEvent.
    """

    PLATFORM_TENANT_ID = 1   # Platform Brain always belongs to tenant 1

    @classmethod
    def load(cls, db, phone: str) -> ConversationState:
        """Load existing state or create fresh for this phone number."""
        try:
            from models import Conversation  # noqa: PLC0415
            conv = (
                db.query(Conversation)
                .filter(
                    Conversation.tenant_id == cls.PLATFORM_TENANT_ID,
                    Conversation.extra_metadata["phone"].astext == phone,
                )
                .order_by(Conversation.id.desc())
                .first()
            )
            if conv and conv.extra_metadata and "stage" in conv.extra_metadata:
                data = dict(conv.extra_metadata)
                # Hydrate slots
                slots_data = data.pop("slots", {})
                valid_slot_fields = ConversationSlots.__dataclass_fields__
                slots = ConversationSlots(**{k: v for k, v in slots_data.items()
                                             if k in valid_slot_fields})
                valid_state_fields = ConversationState.__dataclass_fields__
                state = ConversationState(
                    slots=slots,
                    **{k: v for k, v in data.items() if k in valid_state_fields}
                )
                return state
        except Exception as exc:
            logger.warning("StateManager.load error for phone=%s: %s", phone, exc)

        return ConversationState(phone=phone)

    @classmethod
    def save(cls, db, state: ConversationState) -> None:
        """Persist state back to the Conversation record."""
        try:
            from models import Conversation  # noqa: PLC0415
            state.updated_at = time.time()
            meta = state.to_dict()

            conv = (
                db.query(Conversation)
                .filter(
                    Conversation.tenant_id == cls.PLATFORM_TENANT_ID,
                    Conversation.extra_metadata["phone"].astext == state.phone,
                )
                .order_by(Conversation.id.desc())
                .first()
            )

            if conv:
                conv.extra_metadata = meta
            else:
                conv = Conversation(
                    tenant_id=cls.PLATFORM_TENANT_ID,
                    status="active",
                    extra_metadata=meta,
                )
                db.add(conv)

            db.commit()
            db.refresh(conv)
            return conv
        except Exception as exc:
            logger.error("StateManager.save error for phone=%s: %s", state.phone, exc)
            try:
                db.rollback()
            except Exception:
                pass
            return None

    @classmethod
    def save_message(
        cls,
        db,
        phone: str,
        body: str,
        direction: str,  # "inbound" | "outbound"
        conversation_id: Optional[int] = None,
    ) -> None:
        """Persist a message turn to MessageEvent."""
        try:
            from models import MessageEvent  # noqa: PLC0415
            evt = MessageEvent(
                tenant_id=cls.PLATFORM_TENANT_ID,
                conversation_id=conversation_id,
                direction=direction,
                body=body,
                event_type="whatsapp",
                extra_metadata={"phone": phone},
            )
            db.add(evt)
            db.commit()
        except Exception as exc:
            logger.warning("StateManager.save_message error: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass

    @classmethod
    def load_history(cls, db, phone: str, limit: int = HISTORY_WINDOW) -> List[Dict]:
        """Load recent message history for this phone number."""
        try:
            from models import MessageEvent  # noqa: PLC0415
            events = (
                db.query(MessageEvent)
                .filter(
                    MessageEvent.tenant_id == cls.PLATFORM_TENANT_ID,
                    MessageEvent.extra_metadata["phone"].astext == phone,
                )
                .order_by(MessageEvent.id.desc())
                .limit(limit)
                .all()
            )
            return [
                {"direction": e.direction, "body": e.body}
                for e in reversed(events)
            ]
        except Exception as exc:
            logger.warning("StateManager.load_history error: %s", exc)
            return []


# ═══════════════════════════════════════════════════════════════════════════════
# SLOT UPDATER  (extract slot values from recognized intents)
# ═══════════════════════════════════════════════════════════════════════════════

class SlotUpdater:
    """Update state slots based on detected intent."""

    @staticmethod
    def update(state: ConversationState, intent: str) -> bool:
        """
        Fill slot values based on the intent.
        Returns True if any slot was updated.
        """
        updated = False

        if intent == "platform_salla":
            if state.slots.platform != "سلة":
                state.slots.platform = "سلة"
                updated = True

        elif intent == "platform_zid":
            if state.slots.platform != "زد":
                state.slots.platform = "زد"
                updated = True

        elif intent == "store_small":
            if state.slots.store_size != "small":
                state.slots.store_size = "small"
                updated = True

        elif intent == "store_large":
            if state.slots.store_size != "large":
                state.slots.store_size = "large"
                updated = True

        # Advance stage when we have enough info
        if (
            state.stage in ("discovery", "qualification")
            and state.slots.platform
            and state.slots.store_size
        ):
            state.stage = "recommendation"
            updated = True

        if intent in ("subscribe_now", "request_payment_link"):
            if state.stage != "onboarded":
                state.stage = "checkout"
                state.purchase_score = 10
                updated = True

        return updated


# ═══════════════════════════════════════════════════════════════════════════════
# PLAN RECOMMENDER
# ═══════════════════════════════════════════════════════════════════════════════

def recommend_plan(state: ConversationState) -> str:
    """Return the best plan name based on collected slots."""
    size = state.slots.store_size or ""
    if size == "large":
        return "Business"
    elif size == "medium":
        return "Pro"
    else:
        return "Starter"


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize_arabic(text: str) -> str:
    """Remove common Arabic diacritics and normalise alef variants for matching."""
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ة", "ه").replace("ى", "ي")
    # Remove tashkeel
    for ch in "ًٌٍَُِّْ":
        text = text.replace(ch, "")
    return text
