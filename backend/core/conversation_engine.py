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


# ─────────────────────────────────────────────────────────────────────────────
# Temporary SQL-error diagnostic helper (May 2026 #19)
# ─────────────────────────────────────────────────────────────────────────────
#
# Production logs show
#
#   [SQL: INSERT INTO message_events ...]
#   [SQL: INSERT INTO message_delivery_events ...]
#   brain_exc=ProgrammingError
#
# for nearly every customer message, but the original psycopg2 details
# (SQLSTATE, the actual failing column / value, the underlying error
# class) are dropped because every persistence path catches
# ``Exception`` generically and logs ``logger.warning(..., exc)`` — no
# traceback, no ``exc.orig.pgcode``.
#
# This helper pulls the structured fields we need to root-cause the
# next crash from logs alone:
#
#   * exc.__class__.__name__       — e.g. ``ProgrammingError``
#   * exc.code                     — SQLAlchemy short code
#   * getattr(exc.orig, "pgcode")  — Postgres SQLSTATE
#                                    (``42703`` = UndefinedColumn,
#                                     ``23502`` = NotNullViolation,
#                                     ``22P02`` = InvalidTextRepr.,
#                                     ``25P02`` = InFailedSqlTxn, …)
#   * exc.statement (trimmed)      — the failing SQL text
#   * exc.params keys              — parameter NAMES only, no values
#                                    (values can contain PII)
#   * len(db.new) / len(db.dirty)  — session state at failure
#
# All extraction is wrapped in defensive ``getattr`` calls so the
# diagnostic itself can NEVER raise — the worst case is missing
# fields, never a secondary crash.
def _diag_sql_error(exc: BaseException, *, db: Any = None) -> str:
    """Return a single-line key=value summary of a SQL error.

    Designed for ``logger.exception(..., extra={...})`` or appending
    to a normal exception log line. Output is intentionally compact
    so Railway / Datadog log dumps stay greppable.

    May 2026 #19 update — expanded fields:

      * ``orig_msg``           : ``str(exc.orig)``
      * ``pgerror``            : ``exc.orig.pgerror`` (full PG message)
      * ``msg_primary``        : ``exc.orig.diag.message_primary``
                                 (Postgres primary error text — most
                                 useful: contains the OFFENDING COLUMN
                                 name verbatim, e.g.
                                 ``column "X" of relation "products"
                                 does not exist``)
      * ``msg_detail``         : ``exc.orig.diag.message_detail``
      * ``msg_hint``           : ``exc.orig.diag.message_hint``
      * ``stmt_position``      : ``exc.orig.diag.statement_position``
      * ``column_name``        : ``exc.orig.diag.column_name``
                                 (set for some PG errors)
      * ``constraint_name``    : ``exc.orig.diag.constraint_name``
      * ``table_name``         : ``exc.orig.diag.table_name``
      * ``schema_name``        : ``exc.orig.diag.schema_name``

    Stmt cap raised from 240 → 2000 chars (TEMP) so the full failing
    SQL fits — the merchant needs to see the actual columns/relations
    in the INSERT to root-cause a column drift, not a truncated head.
    Once root cause is found, lower the cap back to 240 to keep
    log lines tight.

    All extraction is wrapped in defensive ``getattr`` calls so the
    diagnostic itself can NEVER raise — the worst case is missing
    fields, never a secondary crash.
    """
    try:
        cls_name = exc.__class__.__name__
        sa_code  = getattr(exc, "code", None) or "-"

        orig     = getattr(exc, "orig", None)
        orig_cls = orig.__class__.__name__ if orig is not None else "-"
        pgcode   = getattr(orig, "pgcode", None) or "-"
        pgerror  = getattr(orig, "pgerror", None) or "-"
        # ``str(orig)`` is the human-readable psycopg2 message line —
        # different from ``pgerror`` (which is the FULL PG server
        # output). Keep both: ``orig_msg`` is the short line,
        # ``pgerror`` is the canonical PG dump.
        try:
            orig_msg = str(orig) if orig is not None else "-"
        except Exception:  # noqa: BLE001
            orig_msg = "-"

        # psycopg2 ``Diagnostic`` object — only present on psycopg2
        # errors. Holds the structured PG fields including the
        # column / table name the error is about.
        diag = getattr(orig, "diag", None)
        def _diag_field(name: str) -> str:
            try:
                v = getattr(diag, name, None) if diag is not None else None
                return str(v) if v is not None else "-"
            except Exception:  # noqa: BLE001
                return "-"

        msg_primary     = _diag_field("message_primary")
        msg_detail      = _diag_field("message_detail")
        msg_hint        = _diag_field("message_hint")
        stmt_position   = _diag_field("statement_position")
        column_name     = _diag_field("column_name")
        constraint_name = _diag_field("constraint_name")
        table_name      = _diag_field("table_name")
        schema_name     = _diag_field("schema_name")

        stmt_raw = getattr(exc, "statement", None) or ""
        # TEMP (May 2026 #19): bumped to 2000 to surface the full
        # failing INSERT/SELECT — the merchant needs to see the
        # actual column list to root-cause a products-table column
        # drift. Drop back to 240 after the bug is fixed.
        STMT_CAP = 2000
        stmt = (stmt_raw[:STMT_CAP] + ("…" if len(stmt_raw) > STMT_CAP else "")).replace("\n", " ")

        params   = getattr(exc, "params", None)
        if isinstance(params, dict):
            param_keys = ",".join(list(params.keys())[:24])
        elif isinstance(params, (list, tuple)) and params and isinstance(params[0], dict):
            param_keys = ",".join(list(params[0].keys())[:24])
        else:
            param_keys = "-"

        if db is not None:
            new_n   = len(getattr(db, "new", ()) or ())
            dirty_n = len(getattr(db, "dirty", ()) or ())
            deleted_n = len(getattr(db, "deleted", ()) or ())
            session_state = f"new={new_n} dirty={dirty_n} deleted={deleted_n}"
        else:
            session_state = "-"

        return (
            f"sql_err={cls_name} sa_code={sa_code} "
            f"orig_cls={orig_cls} pgcode={pgcode} "
            f"msg_primary={msg_primary!r} "
            f"column_name={column_name!r} table_name={table_name!r} "
            f"schema_name={schema_name!r} constraint_name={constraint_name!r} "
            f"stmt_position={stmt_position} "
            f"msg_detail={msg_detail!r} msg_hint={msg_hint!r} "
            f"orig_msg={orig_msg!r} pgerror={pgerror!r} "
            f"params=[{param_keys}] session=[{session_state}] "
            f"stmt={stmt!r}"
        )
    except Exception:  # noqa: BLE001 — diagnostic must never raise
        return f"sql_err_diag_failed exc_type={type(exc).__name__}"

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
    # but this block acts as a hard fallback that ALWAYS appears in the
    # prompt. May 2026 #21 — prices are the *launch promo* (50% off for
    # two months). Original prices are intentionally NOT shown to the
    # customer; only the launch line is surfaced so the bot doesn't
    # parade the steady-state price early in the funnel.
    STATIC_FACTS = {
        "trial_days":           14,
        "trial_requires_card":  False,
        "plans": {
            "Starter": {"price_sar": 449,  "monthly": True},
            "Growth":  {"price_sar": 849,  "monthly": True},
            "Scale":   {"price_sar": 1499, "monthly": True},
        },
        "launch_promo_note":
            "هذه أسعار عرض الإطلاق بخصم 50٪ لمدة شهرين، وقد يتم تمديد "
            "العرض لاحقًا.",
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

أسعار عرض الإطلاق (هذه التي تذكرها للعميل — لا تذكر الأسعار الأصلية):
  • Starter — {p['Starter']['price_sar']} ريال شهريًا
  • Growth  — {p['Growth']['price_sar']} ريال شهريًا
  • Scale   — {p['Scale']['price_sar']:,} ريال شهريًا

{cls.STATIC_FACTS['launch_promo_note']}

التكاملات المدعومة: سلة وزد فقط (الآن).

المميزات:
{features_ar}

روابط:
  • التسجيل: {cls.STATIC_FACTS['register_url']}
  • الباقات: {cls.STATIC_FACTS['billing_url']}
  • الدعم:   {cls.STATIC_FACTS['support_email']}

قاعدة صارمة: لا تذكر أرقاماً أو مميزات أو تواريخ غير مذكورة أعلاه.
ممنوع ذكر أي سعر آخر غير أسعار العرض المذكورة في هذا الجدول، وممنوع
عبارات المقارنة من نوع «بدل كذا ريال» — نعرض فقط أسعار العرض.
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
        # Strip commas/spaces inside numbers so "1,499" lands as "1499"
        # (matching the canonical STATIC_FACTS value) and we don't end
        # up with spurious "499" fragments to whitelist.
        normalised = re.sub(r"(?<=\d)[,\s](?=\d{3}\b)", "", reply)
        prices_in_reply = set(re.findall(r"\b(\d{3,5})\b", normalised))
        allowed = valid_prices | {
            "14", "24", "30", "60", "90", "1", "7", "3", "2", "50",
        }
        suspicious = prices_in_reply - allowed
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
    # ── Greeting lock — same contract as MerchantBrain ──────────────────────
    # Set True after the first welcome menu is sent (or inferred from a prior
    # outbound in history). Flips the platform DecisionEngine away from
    # `SHOW_WELCOME_MENU` for every subsequent "هلا" so the bot never
    # re-introduces itself in the same conversation.
    greeted:          bool             = False
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

    # 11. Elaborate / follow-up request — fires when the customer asks for
    # more detail on whatever the bot just said. Without this rule a turn
    # like "تفاصيل أكثر" or "اشرح" falls through to "general"/conf=0.3 and
    # the LLM (lacking explicit prior-topic context) often emits a generic
    # closing line. With this rule the DecisionEngine inspects the prior
    # action (e.g. SHOW_PLANS) and continues that topic instead of closing.
    _ELABORATE = (
        "تفاصيل اكثر", "تفاصيل أكثر", "مزيد من التفاصيل", "مزيد",
        "وضح اكثر", "وضح أكثر", "وضح", "وضّح", "اشرح", "اشرح اكثر",
        "اشرح أكثر", "اشرح لي", "ابي التفاصيل", "أبي التفاصيل",
        "ابغى التفاصيل", "أبغى التفاصيل", "ابغى اعرف اكثر",
        "أبغى أعرف أكثر", "اعرف اكثر", "أعرف أكثر", "اكثر", "أكثر",
        "وش الفرق", "ايش الفرق", "الفرق بينهم", "الفرق بينها",
        "more details", "more info", "elaborate", "tell me more",
        "explain more", "explain", "details please", "more",
    )

    # Tokens that are "greeting residue" — stripped while testing whether
    # a message that *also* contains a salaam carries a real question on
    # top. Mirrors ``brain.intent.rules._GREETING_RESIDUE_LEAD_TOKENS``
    # but lives here so the platform classifier is self-contained.
    _GREET_RESIDUE = (
        "السلام", "عليكم", "وعليكم", "سلام", "هلا", "هلو", "هاي",
        "مرحبا", "مرحباً", "مرحبتين", "صباح", "مساء", "الخير",
        "الخيرات", "النور", "أهلا", "أهلاً", "أهلين", "حياك",
        "حياكم", "الله", "كيف", "حالك", "حالكم", "اخبارك", "أخبارك",
        "شخبارك", "شلونك", "نحلة", "نحله", "بوت", "البوت",
        "hi", "hello", "hey", "good", "morning", "evening", "afternoon",
        "ya", "يا",
    )

    # Single-word punctuation/connector tokens we collapse before checking
    # whether the message has substantive content left.
    _GREET_NOISE_CHARS = ".,!؟?:؛ـ-—…"

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

        # Elaborate / follow-up MUST be checked before ``_HOW`` so a
        # generic "tell me more" / "explain" / "اشرح" doesn't accidentally
        # collide with the "how does it work?" rule. We also keep it
        # ahead of the greeting branch so a short "تفاصيل أكثر" never
        # gets eaten by greeting matching on a longer history line.
        if cls._m(t, cls._ELABORATE):
            return "ask_elaborate", 0.95

        if cls._m(t, cls._HOW):      return "ask_how_it_works", 0.9
        if cls._m(t, cls._FEATURES): return "ask_features",     0.9

        # Platform — only if platform slot is not filled yet OR explicitly mentioned
        if cls._m(t, cls._SALLA): return "platform_salla", 1.0
        if cls._m(t, cls._ZID):   return "platform_zid",   1.0

        if cls._m(t, cls._SMALL): return "store_small", 0.9
        if cls._m(t, cls._LARGE): return "store_large",  0.9

        if cls._m(t, cls._FOUNDER): return "contact_founder",  1.0
        if cls._m(t, cls._SUPPORT): return "request_support",  0.9

        # Greeting gate: a message qualifies as a *pure* greeting only when
        # there is nothing actionable left after stripping the salaam
        # tokens. Mixed turns ("مساء الخير نحلة باسألك عن العايد وش
        # نشاطهم") fall through to "general" so the brain answers the
        # actual question instead of replaying the welcome card.
        if len(text) <= 80 and cls._m(t, cls._GREET):
            if not cls._has_substantive_residue(t):
                return "greeting", 0.9
            # Mixed greeting + actionable content — let the LLM handle it
            # with full context. Confidence 0.7 lets downstream logging
            # distinguish "real question" turns from low-conf fallbacks.
            return "general", 0.7

        return "general", 0.3

    @staticmethod
    def _m(text: str, kws: tuple) -> bool:
        return any(kw in text for kw in kws)

    @classmethod
    def _has_substantive_residue(cls, normalised_text: str) -> bool:
        """True iff stripping greeting / courtesy / bot-name tokens leaves
        a chunk of real characters (≥ 3 Arabic/Latin word chars). Used to
        demote 'greeting' to 'general' on mixed turns so the welcome card
        never overrides a real question."""
        if not normalised_text:
            return False
        cleaned = normalised_text
        for tok in cls._GREET_RESIDUE:
            cleaned = cleaned.replace(tok, " ")
        for ch in cls._GREET_NOISE_CHARS:
            cleaned = cleaned.replace(ch, " ")
        residue = "".join(c for c in cleaned if c.isalnum() or c == " ").strip()
        if not residue:
            return False
        # Require at least one token of length >= 3 to count as substantive.
        return any(len(tok) >= 3 for tok in residue.split())


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
SHOW_PLAN_DETAILS  = "SHOW_PLAN_DETAILS"
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
    SHOW_PLAN_DETAILS,
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

        if intent == "ask_elaborate":
            # Follow-up "تفاصيل أكثر / اشرح / مزيد" — anchor to the prior
            # topic instead of asking the LLM cold. When the immediately
            # previous action was SHOW_PLANS we hand off to the dedicated
            # SHOW_PLAN_DETAILS template (deterministic, on-brand prices).
            # Otherwise let the brain elaborate with an explicit hint so
            # it does NOT emit a generic closing line.
            if state.last_action == SHOW_PLANS:
                return SHOW_PLAN_DETAILS, "elaborate_after_show_plans"
            if state.last_action == SHOW_PLAN_DETAILS:
                # Already showed the long form once — escalate to founder
                # contact so the customer doesn't get a third repeat.
                return SEND_FOUNDER_LINK, "elaborate_after_plan_details"
            return GENERATE_AI_REPLY, (
                f"elaborate_after:{state.last_action or 'none'}"
            )

        if intent == "contact_founder":
            return SEND_FOUNDER_LINK, "founder_contact_rule"

        if intent == "request_support":
            return ESCALATE_SUPPORT, "support_escalation_rule"

        if intent == "greeting":
            # State-driven greeting: only fire the welcome menu the FIRST
            # time we see this customer. Re-greetings (state.greeted=True)
            # OR greetings received mid-funnel (any stage past discovery)
            # are routed to the LLM with full context so the bot acknowledges
            # without restarting the conversation. Mirrors the MerchantBrain
            # composer's defense-in-depth guard.
            #
            # NOTE: The classifier itself already demotes "greeting +
            # actionable question" turns to ``intent="general"`` (conf 0.7)
            # via ``_has_substantive_residue``. By the time we reach this
            # branch the message is a *pure* salaam.
            if state.greeted or state.stage != S_DISCOVERY:
                return GENERATE_AI_REPLY, (
                    f"greeting_after_first_turn:greeted={state.greeted}:stage={state.stage}"
                )
            return SHOW_WELCOME_MENU, "greeting_rule_first_turn"

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

    # Human-readable response goals per action — handed to Claude verbatim
    # so it understands WHY the rule layer punted to it.
    _ACTION_GOAL = {
        GENERATE_AI_REPLY: "أجب عن سؤال التاجر بناءً على حالة المحادثة، بدون تكرار الترحيب.",
        SHOW_WELCOME_MENU: "رحّب بالتاجر للمرة الأولى واعرض قائمة البداية.",
        SHOW_PLANS: "اعرض الباقات والأسعار الرسمية فقط.",
        SHOW_PLAN_DETAILS: "وسّع شرح الباقات الثلاث بدون تكرار جدول الأسعار، وبدون إغلاق المحادثة.",
        SEND_CHECKOUT_LINK: "وجّه التاجر مباشرة إلى رابط الاشتراك بدون أسئلة إضافية.",
        SEND_TRIAL_LINK: "أرسل رابط التجربة المجانية وشجّع التاجر على البدء.",
        SEND_FOUNDER_LINK: "زوّد التاجر برابط التواصل المباشر مع المؤسس.",
        ESCALATE_SUPPORT: "حوّل التاجر للدعم الفني واترك الرد قصيراً.",
        FILL_SLOT_PLATFORM: "اسأل عن حجم المتجر بعد تأكيد المنصة.",
        FILL_SLOT_SIZE: "اقترح الباقة الأنسب بناءً على حجم المتجر.",
    }

    @classmethod
    def build_system_injection(
        cls,
        state: ConversationState,
        next_action: str,
        decision_reason: str,
        intent: Optional[str] = None,
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

        response_goal = cls._ACTION_GOAL.get(
            next_action, "أجب بشكل مختصر وواضح بناءً على الحالة الحالية."
        )
        greeted_label = "نعم — ممنوع تكرار الترحيب" if state.greeted else "لا"
        intent_label = intent or "غير محدد"

        block = f"""
══════════════════════════════════════════════
حالة المحادثة الحالية (لا تتجاهلها)
══════════════════════════════════════════════
المرحلة: {state.stage} — {stage_guidance}
Turn رقم: {state.turn}
نقاط الشراء: {state.purchase_score}/10
سبق الترحيب بالتاجر: {greeted_label}

معلومات التاجر المعروفة:
{state.slots.as_context_block()}

أسئلة طُرحت بالفعل (لا تكررها أبداً):
{', '.join(asked_labels) if asked_labels else 'لا شيء حتى الآن'}

الباقة المقترحة: {state.recommended_plan or 'لم تُحدَّد بعد'}

══════════════════════════════════════════════
سياق القرار لهذه الجولة (Decision Context)
══════════════════════════════════════════════
نية التاجر (intent): {intent_label}
سبب التوجيه للذكاء (decision_reason): {decision_reason}
الإجراء المحدد (action): {next_action}
هدف الرد (response_goal): {response_goal}

قاعدة صارمة: التزم بهدف الرد أعلاه. لا ترحّب من جديد إذا كان "سبق الترحيب = نعم".
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

    # Keys owned by ConversationState.to_dict(); everything else in
    # Conversation.extra_metadata (e.g. ``brain_state`` written by the
    # MerchantBrain state store, "customer_phone" / "phone" written by
    # ``_get_or_create_conversation``) MUST be preserved on save.
    _OWNED_META_KEYS = frozenset({
        "phone", "stage", "greeted", "turn", "intent_history", "slots",
        "last_action", "last_message_id", "processed_message_ids",
        "updated_at", "tenant_id",
    })

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
                # ── CRITICAL: merge with existing metadata ──────────────────
                # Direct ``conv.extra_metadata = meta`` would wipe keys this
                # class does not own — most importantly ``brain_state`` from
                # the MerchantBrain. That bug caused every inbound webhook
                # to silently reset the brain to a fresh greeting state.
                existing = dict(conv.extra_metadata or {})
                merged = dict(existing)
                merged.update(meta)
                # Re-apply any unrelated keys that ``meta`` did not explicitly
                # set (covers future additions to extra_metadata).
                for key, value in existing.items():
                    if key not in cls._OWNED_META_KEYS and key not in meta:
                        merged[key] = value
                conv.extra_metadata = merged

                # JSONB needs an explicit dirty flag for SQLAlchemy to emit an
                # UPDATE when the column object identity does not change in
                # some replacement strategies; safe to call regardless.
                try:
                    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                    flag_modified(conv, "extra_metadata")
                except Exception:
                    pass
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
                     tenant_id: Optional[int] = None,
                     *,
                     event_type: Optional[str] = None,
                     created_at: Optional[datetime] = None,
                     extra_metadata: Optional[Dict[str, Any]] = None) -> None:
        _tid = tenant_id if tenant_id is not None else PLATFORM_TENANT_ID

        # ── Marker scrub on outbound persistence ──────────────────
        #
        # OUTBOUND ONLY: strip any `[TEMPLATE:foo]` / `[TRANSFER]` /
        # `[DEBUG]` / `[ACTION]` / `[INTERNAL]` / `[MEDIA:N]` token
        # that survived past the brain boundary scrub
        # (pipeline.py). The DB row written here is what the
        # dashboard renders as the merchant-visible message — we
        # do NOT want that copy to show GPT-hallucinated
        # placeholders even though the wire-layer scrub stripped
        # them from the WhatsApp send.
        #
        # INBOUND is left untouched on purpose:
        #   * The customer may have legitimately typed something
        #     bracketed (e.g. ``[طلبية]`` or even Latin uppercase
        #     stuff like ``[A1]``). Scrubbing inbound text would
        #     mangle merchant evidence in the audit log.
        #   * The marker leak is an OUTBOUND-only failure mode —
        #     only the model emits the kind of pattern we strip.
        #
        # Fail-open: if the scrub itself errors, persist the
        # original body. Better to write a slightly ugly row than
        # to lose the entire message event (which downstream
        # observability + analytics depend on).
        safe_body = body
        if isinstance(body, str) and body and direction in ("outbound", "out"):
            try:
                from core.ai_libraries import scrub_internal_markers  # noqa: PLC0415
                safe_body = scrub_internal_markers(body)
                if safe_body != body:
                    logger.info(
                        "[PERSIST_SCRUB] outbound MessageEvent "
                        "tenant=%s phone=%s len_before=%d len_after=%d",
                        _tid, phone, len(body), len(safe_body or ""),
                    )
            except Exception as _scrub_exc:  # noqa: BLE001
                logger.warning(
                    "[PERSIST_SCRUB] failed tenant=%s err=%s — "
                    "writing original body",
                    _tid, _scrub_exc,
                )
                safe_body = body

        try:
            from models import MessageEvent  # noqa: PLC0415
            meta: Dict[str, Any] = {"phone": phone}
            if extra_metadata:
                meta.update(extra_metadata)

            # ── Pre-stamp outbound rows with ``provider_send.status='queued'`` ──
            # The dashboard reads MessageEvent rows verbatim. Without
            # this marker the inbox renders every brand-new outbound
            # bubble as fully delivered (✔✔) before the WhatsApp send
            # has even fired. We pre-stamp ``queued`` here so the UI
            # can render a clock icon; the wire layer
            # (``_post_wa`` → ``stamp_outbound_send_status``) flips
            # this to ``sent`` / ``failed`` after the provider POST
            # returns. Skipped for inbound / historical-import rows
            # (those are never "sends") and for rows that already
            # carry a final ``provider_send`` block (e.g. campaign
            # dispatcher writing the row AFTER the send).
            if direction in ("outbound", "out"):
                is_historical = bool(meta.get("historical_import")) or (
                    meta.get("message_origin") == "historical_sync"
                )
                existing = meta.get("provider_send")
                if not is_historical and not (
                    isinstance(existing, dict)
                    and existing.get("status") in ("sent", "failed")
                ):
                    try:
                        from core.outbound_send_status import build_queued_block  # noqa: PLC0415
                        meta.setdefault(
                            "provider_send",
                            build_queued_block(operation=event_type or "whatsapp"),
                        )
                    except Exception as _q_exc:  # noqa: BLE001
                        logger.debug(
                            "[StateManager] queued pre-stamp skipped tenant=%s: %s",
                            _tid, _q_exc,
                        )

            ts = created_at if created_at is not None else datetime.utcnow()
            db.add(MessageEvent(
                tenant_id=_tid,
                conversation_id=conversation_id,
                direction=direction,
                body=safe_body,
                event_type=event_type or "whatsapp",
                created_at=ts,
                extra_metadata=meta,
            ))
            db.commit()
            # ── W2.0.1 (May 2026): Inbound-lifecycle telemetry.
            # We record the persistence outcome on the active trace
            # so the summary line knows whether a MessageEvent was
            # written, and whether it was orphaned (no
            # ``conversation_id``). This is the single most important
            # signal the operator needs when a merchant reports
            # "the conversation never appeared in Nahla".
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_MESSAGE_SAVED,
                    EVENT_MESSAGE_SAVED_ORPHAN,
                    record_lifecycle,
                )
                if direction in ("inbound", "in"):
                    if conversation_id is None:
                        record_lifecycle(
                            EVENT_MESSAGE_SAVED_ORPHAN,
                            detail=(
                                f"direction={direction} "
                                f"event_type={event_type or 'whatsapp'} "
                                f"body_len={len(safe_body or '')}"
                            ),
                        )
                    else:
                        record_lifecycle(
                            EVENT_MESSAGE_SAVED,
                            detail=(
                                f"direction={direction} "
                                f"event_type={event_type or 'whatsapp'} "
                                f"body_len={len(safe_body or '')}"
                            ),
                            conversation_id=int(conversation_id),
                        )
            except Exception:
                pass
        except Exception as exc:
            # ── Surface psycopg2 details (May 2026 #19) ─────────────
            # The original ``logger.warning("...: %s", exc)`` dropped
            # the traceback AND the psycopg2 fields (SQLSTATE, failing
            # column, underlying error class). With ``logger.exception``
            # plus ``_diag_sql_error`` we now log:
            #   * full traceback
            #   * SQLAlchemy code + Postgres SQLSTATE
            #   * trimmed failing SQL
            #   * parameter NAMES (values omitted for PII)
            #   * session counts (new / dirty / deleted)
            # That single log line is enough to root-cause an
            # ``INSERT INTO message_events`` ProgrammingError without
            # turning on PG slow-query logs in Railway.
            #
            # CRITICAL: rollback BEFORE the diagnostic. If the session
            # is in failed-transaction state, ANY further attribute
            # access on bound objects can trigger lazy loads that
            # re-raise ``InFailedSqlTransaction`` and mask the
            # original error. Roll back first, then diagnose.
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.exception(
                "[StateManager] save_message error tenant=%s phone=%s "
                "direction=%s event_type=%s | %s",
                _tid, phone, direction, event_type or "whatsapp",
                _diag_sql_error(exc, db=db),
            )
            # ── W2.0.1 (May 2026): Inbound-lifecycle telemetry.
            # The rollback above ALSO unwinds any uncommitted
            # ``Conversation`` flush from the same session — so this
            # event tells the summary line "the convo creation that
            # looked successful upstream was just rolled back". The
            # exception itself is swallowed (legacy contract); only
            # the trace and structured log surface it.
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_MESSAGE_SAVE_ROLLBACK,
                    record_lifecycle,
                )
                record_lifecycle(
                    EVENT_MESSAGE_SAVE_ROLLBACK,
                    detail=(
                        f"direction={direction} "
                        f"event_type={event_type or 'whatsapp'} "
                        f"exc={type(exc).__name__}"
                    ),
                )
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
    """Map the resolved store size onto the current public plan ladder.

    Plan names (May 2026): Starter / Growth / Scale. ``Pro`` / ``Business``
    were the May 2025 names and have been retired — old states that still
    carry them are migrated lazily on next load.
    """
    if state.slots.store_size == "large":
        return "Scale"
    if state.slots.store_size == "medium":
        return "Growth"
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
