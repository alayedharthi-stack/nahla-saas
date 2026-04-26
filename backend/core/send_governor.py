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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.send_governor")


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
    allowed: bool
    reason_code: str = "allowed"
    label_ar: str = ""
    suggestion_ar: str = ""
    blocked_by_type: Optional[str] = None   # الخدمة ذات الأولوية الأعلى التي منعت

    @property
    def is_hard_block(self) -> bool:
        """
        True  = منع دائم — اكتب AutomationExecution(skipped) وأنهِ الأمر.
                لن يتغيّر السبب في وقت قريب، لا فائدة من إعادة المحاولة.

        False = مؤجَّل — لا تكتب execution record (لتبقى idempotency سليمة).
                أعد المحاولة في الدورة القادمة (event.processed يبقى False).

        سبب استثناء blocked_by_priority من Hard:
          الحدث ذو الأولوية الأعلى ينتهي في ساعات — الرسالة المؤجَّلة يجب
          أن تُرسَل لاحقاً بمجرد زوال الحدث المانع.

        سبب استثناء blocked_by_daily_limit من Hard:
          الحد اليومي يتجدد غداً — الرسالة ستُرسَل في الدورة القادمة.

        blocked_by_weekly_limit يبقى Hard لأن الأسبوع طويل والرسالة ستكون
        قديمة وغير ذات صلة حين يتجدد الحد.
        """
        return self.reason_code in {
            "blocked_by_unsubscribe",   # دائم: العميل ألغى الاشتراك
            "blocked_by_weekly_limit",  # طويل جداً: 7 أيام
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed":         self.allowed,
            "reason_code":     self.reason_code,
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
    قرار: هل نرسل لهذا العميل من هذه الخدمة الآن؟

    الاستدعاء يكون من _try_execute في automation_engine مباشرة بعد
    فحص unsubscribe وقبل _execute_action.
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
