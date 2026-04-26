"""
core/send_governor.py
─────────────────────
Global Send Governor — طبقة مركزية تُقرر قبل كل إرسال من الطيار الآلي.

تمنع:
  1. تضارب الخدمات   (أولوية HIGH > MEDIUM > LOW)
  2. إزعاج العميل    (حد 1 رسالة/6 ساعات · 2/يوم · 4/أسبوع)
  3. Cooldown لكل خدمة
  4. إرسال لمن ألغى الاشتراك

Public API
──────────
  check(db, tenant_id, customer_id, automation_type) → GovDecision
      استدعِها قبل _try_execute في automation_engine.
      تُعيد GovDecision مع reason_code + label_ar + suggestion_ar.

  record_sent(db, tenant_id, customer_id, automation_type)
      سجّل الإرسال الفعلي حتى تُحسب الـ limits الصحيح.

══════════════════════════════════════════════════════════════════════════════
📜 GOVERNOR DECISION LAW — قانون أساسي في نحلة لا يُكسر
══════════════════════════════════════════════════════════════════════════════

┌─────────────────┬─────────────────────────────────────────────────────────┐
│ GovernorDecision│ السلوك المطلوب                                          │
│ Type            │                                                         │
├─────────────────┼─────────────────────────────────────────────────────────┤
│ SOFT_BLOCK      │ • لا تُنشئ AutomationExecution                          │
│ (مؤقت)          │ • event.processed يبقى False                            │
│                 │ • يُعاد التقييم في الدورة القادمة تلقائياً               │
│                 │ أمثلة:                                                  │
│                 │   blocked_by_priority  → HIGH ينتهي خلال ساعات         │
│                 │   blocked_by_6h_limit  → يتجدد في < 6 ساعات            │
│                 │   blocked_by_daily_limit → يتجدد غداً                  │
│                 │   blocked_by_cooldown  → يتجدد حسب المدة                │
├─────────────────┼─────────────────────────────────────────────────────────┤
│ HARD_BLOCK      │ • أنشئ AutomationExecution(status=skipped)              │
│ (دائم)          │ • event.processed = True — لا إعادة                     │
│                 │ • سجّل السبب بوضوح                                     │
│                 │ أمثلة:                                                  │
│                 │   blocked_by_unsubscribe → العميل ألغى نهائياً          │
│                 │   blocked_by_weekly_limit → 7 أيام، الرسالة ستكون قديمة│
├─────────────────┼─────────────────────────────────────────────────────────┤
│ ALLOW_SEND      │ • نفّذ الإرسال أولاً                                    │
│ (مسموح)         │ • بعد التأكد من النجاح الفعلي فقط:                     │
│                 │   - أنشئ AutomationExecution(status=sent)               │
│                 │   - سجّل في GovernorSendLog عبر record_sent()           │
│                 │ ❌ لا تُسجَّل أي شيء قبل نجاح الإرسال                  │
└─────────────────┴─────────────────────────────────────────────────────────┘

⚠️  أي كود يخالف هذا القانون سيُطلق RuntimeError فورياً عبر:
     _write_execution() → يرفض skip_reason من SOFT_BLOCK_REASONS
══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.send_governor")


# ══════════════════════════════════════════════════════════════════════════
# نوع قرار Governor — يحكم ما يفعله automation_engine بعد كل قرار
# ══════════════════════════════════════════════════════════════════════════

class GovernorDecisionType(str, Enum):
    """
    التصنيف النهائي لقرار Governor.

    automation_engine يقرأ هذا الـ Enum — وليس is_hard_block فقط —
    ليعرف بدقة ما يجب فعله:

    SOFT_BLOCK → delay (لا record، يُعاد التقييم)
    HARD_BLOCK → skipped (اكتب record، أغلق الحدث)
    ALLOW_SEND → نفّذ الإرسال، ثم اكتب sent + record_sent بعد النجاح
    """
    SOFT_BLOCK = "soft_block"
    HARD_BLOCK = "hard_block"
    ALLOW_SEND = "allow_send"


# ── Reason-code sets (مرجع ثابت يُستخدم في GovDecision + _write_execution) ─

SOFT_BLOCK_REASONS: FrozenSet[str] = frozenset({
    # مؤقت — يزول تلقائياً، يُعاد تقييمه في الدورة القادمة
    "blocked_by_priority",    # HIGH ينتهي خلال ساعات
    "blocked_by_6h_limit",    # يتجدد في < 6 ساعات
    "blocked_by_daily_limit", # يتجدد غداً
    "blocked_by_cooldown",    # يتجدد حسب مدة الخدمة
})

HARD_BLOCK_REASONS: FrozenSet[str] = frozenset({
    # دائم أو طويل جداً — الرسالة لن تكون ذات صلة عند التعافي
    "blocked_by_unsubscribe",  # العميل ألغى نهائياً
    "blocked_by_weekly_limit", # 7 أيام، الرسالة ستكون قديمة
})


# ── أولويات الخدمات ────────────────────────────────────────────────────────

class Priority:
    HIGH   = 10
    MEDIUM = 5
    LOW    = 1


# automation_type → priority level
_PRIORITY: Dict[str, int] = {
    # HIGH — رسائل تشغيلية حرجة
    "cod_confirmation":      Priority.HIGH,
    "unpaid_order_reminder": Priority.HIGH,
    "abandoned_cart":        Priority.HIGH,

    # MEDIUM — تنبيهات مفيدة
    "back_in_stock":         Priority.MEDIUM,
    "predictive_reorder":    Priority.MEDIUM,

    # LOW — تسويق واستهداف
    "vip_upgrade":           Priority.LOW,
    "new_product_alert":     Priority.LOW,
    "seasonal_offer":        Priority.LOW,
    "salary_payday_offer":   Priority.LOW,
    "customer_winback":      Priority.LOW,
}


def _priority(automation_type: str) -> int:
    return _PRIORITY.get(automation_type, Priority.LOW)


# ── Cooldown لكل خدمة (ساعات) ─────────────────────────────────────────────

_COOLDOWN_HOURS: Dict[str, float] = {
    "abandoned_cart":        24,
    "unpaid_order_reminder": 24,
    "cod_confirmation":      6,      # يعتمد على طلب بعينه — أقصر
    "back_in_stock":         48,
    "predictive_reorder":    168,    # 7 أيام
    "new_product_alert":     72,
    "seasonal_offer":        72,
    "salary_payday_offer":   72,
    "customer_winback":      336,    # 14 يوم
    "vip_upgrade":           336,    # 14 يوم
}

# حدود الإرسال العالمية للعميل
_LIMIT_PER_6H  = 1   # رسالة واحدة كل 6 ساعات
_LIMIT_PER_24H = 2   # حد أقصى 2/يوم
_LIMIT_PER_7D  = 4   # حد أقصى 4/أسبوع


# ── النتيجة ───────────────────────────────────────────────────────────────

@dataclass
class GovDecision:
    """
    نتيجة استدعاء send_governor.check().

    الخاصية الأهم: decision_type (GovernorDecisionType)
    ─────────────────────────────────────────────────────
    automation_engine يقرأها ليحدد السلوك التالي:

      ALLOW_SEND  → نفّذ الإرسال ثم record_sent بعد النجاح فقط
      SOFT_BLOCK  → أعد "delay" — لا تكتب execution record أبداً
      HARD_BLOCK  → أعد "skipped" — اكتب execution record ثم أغلق الحدث

    is_hard_block → اختصار لـ (decision_type == HARD_BLOCK)
    """
    allowed: bool
    reason_code: str = "allowed"
    label_ar: str = ""
    suggestion_ar: str = ""
    blocked_by_type: Optional[str] = None   # الخدمة ذات الأولوية الأعلى التي منعت

    @property
    def decision_type(self) -> GovernorDecisionType:
        """
        يُشتق من reason_code — يُعبّر عن نوع القرار بشكل صريح.
        استخدم هذا بدل is_hard_block عند كتابة كود جديد.
        """
        if self.allowed:
            return GovernorDecisionType.ALLOW_SEND
        if self.reason_code in HARD_BLOCK_REASONS:
            return GovernorDecisionType.HARD_BLOCK
        return GovernorDecisionType.SOFT_BLOCK

    @property
    def is_hard_block(self) -> bool:
        """اختصار لـ (decision_type == HARD_BLOCK)."""
        return self.decision_type == GovernorDecisionType.HARD_BLOCK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed":         self.allowed,
            "reason_code":     self.reason_code,
            "decision_type":   self.decision_type.value,
            "label_ar":        self.label_ar,
            "suggestion_ar":   self.suggestion_ar,
            "blocked_by_type": self.blocked_by_type,
        }


# نصوص الرسائل العربية لكل reason_code (للتاجر)
_MESSAGES: Dict[str, Dict[str, str]] = {
    "blocked_by_priority": {
        "label_ar":      "تم المنع — خدمة أعلى أولوية",
        "suggestion_ar": "يوجد رسالة تشغيلية أهم للعميل تأخذ الأولوية. ستُرسل هذه الرسالة بعد انتهاء الخدمة الأعلى.",
    },
    "blocked_by_unsubscribe": {
        "label_ar":      "تم التجاهل — العميل ألغى الاشتراك",
        "suggestion_ar": "العميل طلب عدم الاستقبال. إذا أردت استعادته ننصحك بالتواصل معه شخصياً.",
    },
    "blocked_by_daily_limit": {
        "label_ar":      "تم التأجيل — تجاوز الحد اليومي",
        "suggestion_ar": "العميل استلم الحد الأقصى من الرسائل اليوم (رسالتان). ستُرسل تلقائياً غداً.",
    },
    "blocked_by_weekly_limit": {
        "label_ar":      "تم الإلغاء — تجاوز الحد الأسبوعي",
        "suggestion_ar": "العميل استلم 4 رسائل هذا الأسبوع. لن تُرسل هذه الرسالة للحفاظ على تجربته.",
    },
    "blocked_by_6h_limit": {
        "label_ar":      "تم التأجيل — رسالة مؤخراً",
        "suggestion_ar": "العميل استلم رسالة منذ أقل من 6 ساعات. ستُرسل تلقائياً عند انتهاء الفترة.",
    },
    "blocked_by_cooldown": {
        "label_ar":      "تم التأجيل — فترة الراحة",
        "suggestion_ar": "هذه الخدمة لها فترة راحة إلزامية بين الرسائل. ستُرسل تلقائياً عند انتهائها.",
    },
    "allowed": {
        "label_ar":      "تم الإرسال",
        "suggestion_ar": "",
    },
}


def _arabic_label(reason_code: str, automation_type: str, blocked_by_type: Optional[str] = None) -> Dict[str, str]:
    base = _MESSAGES.get(reason_code, {"label_ar": reason_code, "suggestion_ar": ""})
    # نضيف اسم الخدمة الحاجبة في رسالة الأولوية
    if reason_code == "blocked_by_priority" and blocked_by_type:
        _TYPE_NAMES: Dict[str, str] = {
            "abandoned_cart":        "السلة المتروكة 🛒",
            "unpaid_order_reminder": "الطلب غير المدفوع 💳",
            "cod_confirmation":      "تأكيد الدفع عند الاستلام 💰",
            "back_in_stock":         "عودة المنتج للمخزون 📦",
            "predictive_reorder":    "تذكير إعادة الطلب 🔄",
        }
        blocker_name = _TYPE_NAMES.get(blocked_by_type, blocked_by_type)
        return {
            "label_ar":    base["label_ar"],
            "suggestion_ar": (
                f"لم تُرسَل لأن هناك خدمة ذات أولوية أعلى للعميل حالياً: «{blocker_name}». "
                "ستُرسل هذه الرسالة تلقائياً عند انتهاء الخدمة الأعلى."
            ),
        }
    return base


# ── الوظيفة الرئيسية ──────────────────────────────────────────────────────

def check(
    db: Session,
    tenant_id: int,
    customer_id: int,
    automation_type: str,
    *,
    order_id: Optional[int] = None,
) -> GovDecision:
    """
    📜 قرار Governor: هل نرسل لهذا العميل من هذه الخدمة الآن؟

    يُستدعى من automation_engine._try_execute بعد فحص unsubscribe
    وقبل _execute_action.

    ── قواعد السلوك (القانون) ─────────────────────────────────────────────

    decision.decision_type = SOFT_BLOCK  →  إعادة "delay" في engine
      • لا تُنشئ AutomationExecution — المنع مؤقت وسيزول
      • event.processed يبقى False → يُعاد التقييم تلقائياً
      • أسباب: blocked_by_priority, blocked_by_6h_limit,
                blocked_by_daily_limit, blocked_by_cooldown

    decision.decision_type = HARD_BLOCK  →  إعادة "skipped" في engine
      • أنشئ AutomationExecution(status=skipped, skip_reason=...)
      • event.processed = True نهائياً — لا إعادة
      • أسباب: blocked_by_unsubscribe, blocked_by_weekly_limit

    decision.decision_type = ALLOW_SEND  →  نفّذ الإرسال في engine
      • نفّذ _execute_action أولاً
      • بعد التأكد من النجاح: أنشئ AutomationExecution(sent)
                              + record_sent() → GovernorSendLog
      • ❌ لا تُسجَّل شيئاً قبل نجاح الإرسال الفعلي

    ⚠️  _write_execution() يرفض أي skip_reason من SOFT_BLOCK_REASONS
        بـ RuntimeError — هذا هو خط الدفاع الأخير ضد كسر القانون.
    ──────────────────────────────────────────────────────────────────────

    ترتيب الفحوصات:
      1. إلغاء الاشتراك       → HARD_BLOCK
      2. أولوية أعلى نشطة     → SOFT_BLOCK
      3. Cooldown الخدمة       → SOFT_BLOCK
      4. حد 6 ساعات           → SOFT_BLOCK
      5. حد يومي (2/يوم)       → SOFT_BLOCK
      6. حد أسبوعي (4/أسبوع)   → HARD_BLOCK
      7. مسموح                 → ALLOW_SEND
    """
    # ── 1. فحص إلغاء الاشتراك ──────────────────────────────────────────
    try:
        from models import Customer as _Customer  # noqa: PLC0415
        from services.unsubscribe import is_silenced as _is_silenced  # noqa: PLC0415

        cust = db.query(_Customer).filter(
            _Customer.id == customer_id,
            _Customer.tenant_id == tenant_id,
        ).first()
        if cust and _is_silenced(cust):
            msgs = _arabic_label("blocked_by_unsubscribe", automation_type)
            return GovDecision(
                allowed=False,
                reason_code="blocked_by_unsubscribe",
                **msgs,
            )
    except Exception as exc:
        logger.warning("[Governor] unsubscribe check error: %s", exc)

    # ── 2. فحص الأولوية ────────────────────────────────────────────────
    _this_priority = _priority(automation_type)
    blocker = _find_higher_priority_active(db, tenant_id, customer_id, automation_type, _this_priority)
    if blocker:
        msgs = _arabic_label("blocked_by_priority", automation_type, blocked_by_type=blocker)
        logger.info(
            "[Governor] BLOCK priority — tenant=%s customer=%s type=%s blocked_by=%s",
            tenant_id, customer_id, automation_type, blocker,
        )
        return GovDecision(
            allowed=False,
            reason_code="blocked_by_priority",
            blocked_by_type=blocker,
            **msgs,
        )

    now = _utcnow()

    # ── 3. فحص Cooldown الخدمة ─────────────────────────────────────────
    cooldown_h = _COOLDOWN_HOURS.get(automation_type, 24)
    last_sent_this_service = _last_sent_for_service(
        db, tenant_id, customer_id, automation_type, order_id=order_id,
    )
    if last_sent_this_service:
        age_h = (now - last_sent_this_service).total_seconds() / 3600
        if age_h < cooldown_h:
            msgs = _arabic_label("blocked_by_cooldown", automation_type)
            logger.info(
                "[Governor] BLOCK cooldown — tenant=%s customer=%s type=%s "
                "age_h=%.1f cooldown_h=%.1f",
                tenant_id, customer_id, automation_type, age_h, cooldown_h,
            )
            return GovDecision(
                allowed=False,
                reason_code="blocked_by_cooldown",
                **msgs,
            )

    # ── 4. فحص حد 6 ساعات ─────────────────────────────────────────────
    last_any = _last_sent_any_service(db, tenant_id, customer_id)
    if last_any:
        age_6h = (now - last_any).total_seconds() / 3600
        if age_6h < 6:
            msgs = _arabic_label("blocked_by_6h_limit", automation_type)
            logger.info(
                "[Governor] DELAY 6h_limit — tenant=%s customer=%s type=%s age_h=%.1f",
                tenant_id, customer_id, automation_type, age_6h,
            )
            return GovDecision(
                allowed=False,
                reason_code="blocked_by_6h_limit",
                **msgs,
            )

    # ── 5. فحص الحد اليومي ────────────────────────────────────────────
    count_24h = _count_sent(db, tenant_id, customer_id, hours=24)
    if count_24h >= _LIMIT_PER_24H:
        msgs = _arabic_label("blocked_by_daily_limit", automation_type)
        logger.info(
            "[Governor] BLOCK daily_limit — tenant=%s customer=%s count_24h=%d",
            tenant_id, customer_id, count_24h,
        )
        return GovDecision(
            allowed=False,
            reason_code="blocked_by_daily_limit",
            **msgs,
        )

    # ── 6. فحص الحد الأسبوعي ──────────────────────────────────────────
    count_7d = _count_sent(db, tenant_id, customer_id, hours=168)
    if count_7d >= _LIMIT_PER_7D:
        msgs = _arabic_label("blocked_by_weekly_limit", automation_type)
        logger.info(
            "[Governor] BLOCK weekly_limit — tenant=%s customer=%s count_7d=%d",
            tenant_id, customer_id, count_7d,
        )
        return GovDecision(
            allowed=False,
            reason_code="blocked_by_weekly_limit",
            **msgs,
        )

    # ── مسموح ─────────────────────────────────────────────────────────
    return GovDecision(allowed=True, reason_code="allowed", label_ar="مسموح بالإرسال")


def record_sent(
    db: Session,
    tenant_id: int,
    customer_id: int,
    automation_type: str,
    *,
    execution_id: Optional[int] = None,
) -> None:
    """
    سجّل عملية إرسال ناجحة حتى تُحسب الحدود بدقة.
    يُستدعى من automation_engine مباشرة بعد status='sent'.
    """
    try:
        from models import GovernorSendLog  # noqa: PLC0415
        entry = GovernorSendLog(
            tenant_id=tenant_id,
            customer_id=customer_id,
            automation_type=automation_type,
            execution_id=execution_id,
            sent_at=_utcnow(),
        )
        db.add(entry)
        db.flush()
    except Exception as exc:
        logger.warning("[Governor] record_sent failed: %s", exc)


# ── Query helpers ──────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _last_sent_for_service(
    db: Session,
    tenant_id: int,
    customer_id: int,
    automation_type: str,
    *,
    order_id: Optional[int] = None,
) -> Optional[datetime]:
    """
    آخر مرة أُرسلت فيها رسالة من هذه الخدمة لهذا العميل.
    نستخدم GovernorSendLog أولاً، ثم AutomationExecution كـ fallback.
    """
    try:
        from models import GovernorSendLog  # noqa: PLC0415
        row = (
            db.query(GovernorSendLog.sent_at)
            .filter(
                GovernorSendLog.tenant_id == tenant_id,
                GovernorSendLog.customer_id == customer_id,
                GovernorSendLog.automation_type == automation_type,
            )
            .order_by(GovernorSendLog.sent_at.desc())
            .first()
        )
        if row:
            return row[0]
    except Exception:
        pass

    # Fallback: AutomationExecution (قبل إضافة GovernorSendLog)
    try:
        from models import AutomationExecution, SmartAutomation  # noqa: PLC0415
        row = (
            db.query(AutomationExecution.executed_at)
            .join(
                SmartAutomation,
                SmartAutomation.id == AutomationExecution.automation_id,
            )
            .filter(
                AutomationExecution.tenant_id == tenant_id,
                AutomationExecution.customer_id == customer_id,
                AutomationExecution.status == "sent",
                SmartAutomation.automation_type == automation_type,
            )
            .order_by(AutomationExecution.executed_at.desc())
            .first()
        )
        if row:
            return row[0]
    except Exception:
        pass
    return None


def _last_sent_any_service(
    db: Session,
    tenant_id: int,
    customer_id: int,
) -> Optional[datetime]:
    """آخر إرسال لهذا العميل من أي خدمة."""
    try:
        from models import GovernorSendLog  # noqa: PLC0415
        row = (
            db.query(GovernorSendLog.sent_at)
            .filter(
                GovernorSendLog.tenant_id == tenant_id,
                GovernorSendLog.customer_id == customer_id,
            )
            .order_by(GovernorSendLog.sent_at.desc())
            .first()
        )
        if row:
            return row[0]
    except Exception:
        pass

    try:
        from models import AutomationExecution  # noqa: PLC0415
        row = (
            db.query(AutomationExecution.executed_at)
            .filter(
                AutomationExecution.tenant_id == tenant_id,
                AutomationExecution.customer_id == customer_id,
                AutomationExecution.status == "sent",
            )
            .order_by(AutomationExecution.executed_at.desc())
            .first()
        )
        if row:
            return row[0]
    except Exception:
        pass
    return None


def _count_sent(
    db: Session,
    tenant_id: int,
    customer_id: int,
    hours: int,
) -> int:
    """عدد الرسائل المُرسلة لهذا العميل في آخر N ساعة."""
    since = _utcnow() - timedelta(hours=hours)
    try:
        from models import GovernorSendLog  # noqa: PLC0415
        return (
            db.query(GovernorSendLog)
            .filter(
                GovernorSendLog.tenant_id == tenant_id,
                GovernorSendLog.customer_id == customer_id,
                GovernorSendLog.sent_at >= since,
            )
            .count()
        )
    except Exception:
        pass

    try:
        from models import AutomationExecution  # noqa: PLC0415
        return (
            db.query(AutomationExecution)
            .filter(
                AutomationExecution.tenant_id == tenant_id,
                AutomationExecution.customer_id == customer_id,
                AutomationExecution.status == "sent",
                AutomationExecution.executed_at >= since,
            )
            .count()
        )
    except Exception:
        pass
    return 0


def _find_higher_priority_active(
    db: Session,
    tenant_id: int,
    customer_id: int,
    automation_type: str,
    this_priority: int,
) -> Optional[str]:
    """
    هل يوجد حدث نشط لخدمة أعلى أولوية لم يُعالَج بعد؟
    نفحص AutomationEvent غير المعالج خلال آخر 24 ساعة.
    """
    if this_priority >= Priority.HIGH:
        return None  # HIGH لا يمنعها شيء

    from models import AutomationEvent, SmartAutomation  # noqa: PLC0415

    since = _utcnow() - timedelta(hours=24)
    pending_events: List[Any] = (
        db.query(AutomationEvent, SmartAutomation)
        .join(
            SmartAutomation,
            SmartAutomation.trigger_event == AutomationEvent.event_type,
        )
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.customer_id == customer_id,
            AutomationEvent.processed.is_(False),
            AutomationEvent.created_at >= since,
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.enabled.is_(True),
        )
        .all()
    )

    for _ev, auto in pending_events:
        if auto.automation_type == automation_type:
            continue
        if _priority(auto.automation_type) > this_priority:
            return auto.automation_type

    return None


# ── Governor log API (للـ endpoint) ───────────────────────────────────────

def get_governor_log(
    db: Session,
    tenant_id: int,
    *,
    customer_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    يُعيد سجل المنع والتأجيل (AutomationExecution مع skip_reason من Governor)
    مُثراً بالنص العربي ليعرضه الـ dashboard للتاجر.
    """
    from models import AutomationExecution, Customer, SmartAutomation  # noqa: PLC0415

    GOVERNOR_CODES = {
        "blocked_by_priority",
        "blocked_by_unsubscribe",
        "blocked_by_daily_limit",
        "blocked_by_weekly_limit",
        "blocked_by_6h_limit",
        "blocked_by_cooldown",
    }

    q = (
        db.query(AutomationExecution, SmartAutomation, Customer)
        .join(SmartAutomation, SmartAutomation.id == AutomationExecution.automation_id)
        .outerjoin(Customer, Customer.id == AutomationExecution.customer_id)
        .filter(
            AutomationExecution.tenant_id == tenant_id,
            AutomationExecution.status == "skipped",
            AutomationExecution.skip_reason.in_(GOVERNOR_CODES),
        )
    )
    if customer_id:
        q = q.filter(AutomationExecution.customer_id == customer_id)

    rows = (
        q.order_by(AutomationExecution.executed_at.desc())
        .limit(limit)
        .all()
    )

    result = []
    for exe, auto, cust in rows:
        msgs = _MESSAGES.get(exe.skip_reason, {"label_ar": exe.skip_reason, "suggestion_ar": ""})
        result.append({
            "id":               exe.id,
            "executed_at":      exe.executed_at.isoformat() if exe.executed_at else None,
            "automation_type":  auto.automation_type if auto else None,
            "automation_name":  auto.name if auto else None,
            "customer_id":      exe.customer_id,
            "customer_name":    (cust.name if cust else None),
            "customer_phone":   (cust.phone if cust else None),
            "reason_code":      exe.skip_reason,
            "label_ar":         msgs["label_ar"],
            "suggestion_ar":    msgs["suggestion_ar"],
        })
    return result
