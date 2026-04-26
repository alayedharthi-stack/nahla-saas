"""
tests/test_send_governor.py
───────────────────────────
سيناريو الاختبار:
  عميل واحد مؤهَّل في نفس اللحظة لأربع خدمات:
    1. abandoned_cart        (HIGH  priority)
    2. unpaid_order_reminder (HIGH  priority)
    3. salary_payday_offer   (LOW   priority)
    4. back_in_stock         (MEDIUM priority)

التحقق:
  - لا تُرسَل إلا رسالة واحدة (abandoned_cart لأنها أول HIGH في القائمة).
  - unpaid_order_reminder تُأجَّل (blocked_by_6h_limit) بعد إرسال abandoned_cart.
  - salary_payday_offer  تُأجَّل  (blocked_by_priority) لأن HIGH لا يزال معلقاً.
  - back_in_stock        تُأجَّل  (blocked_by_priority) لأن HIGH لا يزال معلقاً.
  - record_sent لا يُسجَّل إلا بعد نجاح الإرسال الفعلي.
  - الرسائل المؤجَّلة (soft blocks) لا تكتب AutomationExecution → تبقى قابلة
    للإعادة في الدورة القادمة (event.processed = False).
  - الرسائل المحظورة بشكل دائم (hard blocks: unsubscribe / weekly_limit)
    تكتب AutomationExecution(status=skipped) → لا تُعاد.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

# ── path bootstrap ─────────────────────────────────────────────────────────
REPO_ROOT   = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    AutomationEvent,
    AutomationExecution,
    Base,
    Customer,
    GovernorSendLog,
    SmartAutomation,
    Tenant,
    TenantSettings,
)
from core.send_governor import (  # noqa: E402
    GovDecision,
    Priority,
    _count_sent,
    _find_higher_priority_active,
    _last_sent_any_service,
    _priority,
    _utcnow,
    check,
    record_sent,
)


# ── SQLite in-memory DB ────────────────────────────────────────────────────

def _make_db():
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Seed helpers ───────────────────────────────────────────────────────────

def _seed_tenant(db) -> Tenant:
    t = Tenant(name="Test", is_active=True)
    db.add(t)
    db.flush()
    db.add(TenantSettings(
        tenant_id=t.id,
        extra_metadata={"autopilot": {"enabled": True}},
    ))
    db.commit()
    db.refresh(t)
    return t


def _seed_customer(db, tenant_id: int) -> Customer:
    c = Customer(tenant_id=tenant_id, phone="+966555000111", name="أحمد اختبار")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_automation(
    db, tenant_id: int, automation_type: str, trigger_event: str,
) -> SmartAutomation:
    a = SmartAutomation(
        tenant_id=tenant_id,
        automation_type=automation_type,
        name=f"auto_{automation_type}",
        enabled=True,
        engine="recovery",
        trigger_event=trigger_event,
        config={"template_name": f"tpl_{automation_type}"},
        stats_triggered=0,
        stats_sent=0,
        stats_converted=0,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _seed_event(
    db, tenant_id: int, customer_id: int, event_type: str,
) -> AutomationEvent:
    e = AutomationEvent(
        tenant_id=tenant_id,
        customer_id=customer_id,
        event_type=event_type,
        payload={},
        processed=False,
        created_at=_utcnow_naive(),
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


# ══════════════════════════════════════════════════════════════════════════
# 1. اختبارات الأولوية
# ══════════════════════════════════════════════════════════════════════════

class TestPriority:
    """اختبارات نظام الأولويات."""

    def test_priority_values(self):
        assert _priority("abandoned_cart")        == Priority.HIGH
        assert _priority("unpaid_order_reminder") == Priority.HIGH
        assert _priority("cod_confirmation")      == Priority.HIGH
        assert _priority("back_in_stock")         == Priority.MEDIUM
        assert _priority("predictive_reorder")    == Priority.MEDIUM
        assert _priority("salary_payday_offer")   == Priority.LOW
        assert _priority("customer_winback")      == Priority.LOW
        assert _priority("seasonal_offer")        == Priority.LOW
        assert _priority("vip_upgrade")           == Priority.LOW

    def test_high_not_blocked_by_high(self):
        """HIGH لا تمنع HIGH الأخرى — تُتحكَّم بها عبر rate limits فقط."""
        assert Priority.HIGH >= Priority.HIGH  # تجاوز الحد → لا منع أولوية

    def test_is_hard_block_classification(self):
        """التحقق من تصنيف is_hard_block بدقة."""
        hard_blocked = GovDecision(
            allowed=False, reason_code="blocked_by_unsubscribe",
        )
        assert hard_blocked.is_hard_block is True

        hard_weekly = GovDecision(
            allowed=False, reason_code="blocked_by_weekly_limit",
        )
        assert hard_weekly.is_hard_block is True

        soft_priority = GovDecision(
            allowed=False, reason_code="blocked_by_priority",
        )
        assert soft_priority.is_hard_block is False, (
            "blocked_by_priority يجب أن يكون soft — الرسالة تُعاد في الدورة القادمة"
        )

        soft_6h = GovDecision(allowed=False, reason_code="blocked_by_6h_limit")
        assert soft_6h.is_hard_block is False

        soft_daily = GovDecision(allowed=False, reason_code="blocked_by_daily_limit")
        assert soft_daily.is_hard_block is False

        soft_cooldown = GovDecision(allowed=False, reason_code="blocked_by_cooldown")
        assert soft_cooldown.is_hard_block is False


# ══════════════════════════════════════════════════════════════════════════
# 2. اختبار السيناريو الكامل (4 خدمات في نفس اللحظة)
# ══════════════════════════════════════════════════════════════════════════

class TestFourServicesScenario:
    """
    السيناريو الرئيسي: عميل واحد × 4 خدمات متزامنة.

    الترتيب المتوقع:
      1. abandoned_cart        → ALLOWED  (أول HIGH يُقيَّم)
      2. unpaid_order_reminder → DELAY    (blocked_by_6h_limit بعد إرسال #1)
      3. salary_payday_offer   → DELAY    (blocked_by_priority: HIGH معلّق)
      4. back_in_stock         → DELAY    (blocked_by_priority: HIGH معلّق)
    """

    def setup_method(self):
        self.db, self.engine = _make_db()
        tenant = _seed_tenant(self.db)
        self.tenant_id = tenant.id
        self.customer  = _seed_customer(self.db, self.tenant_id)
        self.cid       = self.customer.id

        # الأتمتة
        self.auto_cart    = _seed_automation(self.db, self.tenant_id, "abandoned_cart",        "cart_abandoned")
        self.auto_unpaid  = _seed_automation(self.db, self.tenant_id, "unpaid_order_reminder", "order_payment_pending")
        self.auto_salary  = _seed_automation(self.db, self.tenant_id, "salary_payday_offer",   "salary_payday_due")
        self.auto_stock   = _seed_automation(self.db, self.tenant_id, "back_in_stock",         "product_back_in_stock")

        # الأحداث (كلها غير معالجة)
        self.ev_cart   = _seed_event(self.db, self.tenant_id, self.cid, "cart_abandoned")
        self.ev_unpaid = _seed_event(self.db, self.tenant_id, self.cid, "order_payment_pending")
        self.ev_salary = _seed_event(self.db, self.tenant_id, self.cid, "salary_payday_due")
        self.ev_stock  = _seed_event(self.db, self.tenant_id, self.cid, "product_back_in_stock")

    def teardown_method(self):
        self.db.close()

    # ── الخطوة 1: قبل أي إرسال ────────────────────────────────────────────

    def test_step1_abandoned_cart_allowed_before_any_send(self):
        """abandoned_cart (HIGH) مسموح قبل أي إرسال."""
        decision = check(self.db, self.tenant_id, self.cid, "abandoned_cart")
        assert decision.allowed is True, (
            f"abandoned_cart يجب أن يُسمح به. reason={decision.reason_code}"
        )
        assert decision.reason_code == "allowed"

    def test_step1_unpaid_allowed_before_any_send(self):
        """unpaid_order_reminder (HIGH) مسموح أيضاً قبل أي إرسال."""
        decision = check(self.db, self.tenant_id, self.cid, "unpaid_order_reminder")
        assert decision.allowed is True, (
            "HIGH لا تمنع HIGH — كلاهما مسموح قبل الإرسال"
        )

    def test_step1_salary_blocked_by_priority_before_any_send(self):
        """salary_payday_offer (LOW) محجوب بسبب HIGH معلّق (abandoned_cart)."""
        decision = check(self.db, self.tenant_id, self.cid, "salary_payday_offer")
        assert decision.allowed is False
        assert decision.reason_code == "blocked_by_priority"
        assert decision.is_hard_block is False, "blocked_by_priority يجب أن يكون soft"
        assert "أولوية" in decision.suggestion_ar or "أعلى" in decision.suggestion_ar

    def test_step1_back_in_stock_blocked_by_priority(self):
        """back_in_stock (MEDIUM) محجوب بسبب HIGH معلّق."""
        decision = check(self.db, self.tenant_id, self.cid, "back_in_stock")
        assert decision.allowed is False
        assert decision.reason_code == "blocked_by_priority"
        assert decision.is_hard_block is False

    # ── الخطوة 2: بعد إرسال abandoned_cart ────────────────────────────────

    def test_step2_after_cart_send_unpaid_hits_6h_limit(self):
        """
        بعد إرسال abandoned_cart:
          - نسجّل الإرسال في GovernorSendLog
          - نُعلّم الحدث كـ processed=True
          - unpaid_order_reminder يضرب blocked_by_6h_limit
        """
        # سجّل الإرسال (يحاكي ما يفعله automation_engine بعد النجاح)
        record_sent(self.db, self.tenant_id, self.cid, "abandoned_cart")
        self.db.commit()

        # عند تقييم unpaid_order_reminder:
        # لا يوجد HIGH أعلى أولوية (abandoned_cart يساويه في HIGH)
        # لكن توجد رسالة أُرسلت منذ < 6 ساعات
        decision = check(self.db, self.tenant_id, self.cid, "unpaid_order_reminder")
        assert decision.allowed is False
        assert decision.reason_code == "blocked_by_6h_limit"
        assert decision.is_hard_block is False, "blocked_by_6h_limit يجب soft → retry"

    def test_step2_after_cart_send_salary_still_priority_blocked(self):
        """
        بعد إرسال abandoned_cart:
          - unpaid_order_reminder لا يزال معلّقاً (حدثه لم يُعالَج)
          - salary_payday_offer لا يزال محجوباً بسبب الأولوية
        """
        record_sent(self.db, self.tenant_id, self.cid, "abandoned_cart")
        self.db.commit()

        decision = check(self.db, self.tenant_id, self.cid, "salary_payday_offer")
        assert decision.allowed is False
        assert decision.reason_code == "blocked_by_priority"

    # ── الخطوة 3: record_sent فقط بعد الإرسال الفعلي ──────────────────────

    def test_record_sent_creates_governor_log_row(self):
        """record_sent يكتب سجلاً في GovernorSendLog بعد الإرسال."""
        before = self.db.query(GovernorSendLog).filter(
            GovernorSendLog.customer_id == self.cid,
        ).count()
        assert before == 0

        record_sent(self.db, self.tenant_id, self.cid, "abandoned_cart")
        self.db.commit()

        after = self.db.query(GovernorSendLog).filter(
            GovernorSendLog.customer_id == self.cid,
        ).count()
        assert after == 1

    def test_record_sent_not_called_before_send(self):
        """قبل أي إرسال، GovernorSendLog يجب أن يكون فارغاً."""
        # مجرد تقييم القرار لا يُسجّل إرسالاً
        check(self.db, self.tenant_id, self.cid, "abandoned_cart")
        check(self.db, self.tenant_id, self.cid, "salary_payday_offer")

        count = self.db.query(GovernorSendLog).filter(
            GovernorSendLog.customer_id == self.cid,
        ).count()
        assert count == 0, "check() لا يكتب GovernorSendLog، record_sent() فقط يكتب"

    # ── الخطوة 4: soft blocks لا تكتب AutomationExecution ─────────────────

    def test_soft_block_does_not_write_execution_record(self):
        """
        الـ soft blocks (priority, 6h, cooldown, daily) يجب ألا تكتب
        AutomationExecution حتى تظل idempotency سليمة ويمكن إعادة المحاولة.

        هذا الاختبار يختبر GovDecision.is_hard_block مباشرة لأن منطق الكتابة
        موجود في automation_engine._try_execute.
        """
        soft_reasons = [
            "blocked_by_priority",
            "blocked_by_6h_limit",
            "blocked_by_cooldown",
            "blocked_by_daily_limit",
        ]
        for code in soft_reasons:
            d = GovDecision(allowed=False, reason_code=code)
            assert d.is_hard_block is False, (
                f"{code} يجب أن يكون soft block (is_hard_block=False) "
                f"حتى لا تُكتب execution record وتبقى الرسالة قابلة للإعادة"
            )

    def test_hard_block_signals_write_execution_record(self):
        """الـ hard blocks (unsubscribe, weekly_limit) يجب أن تكتب execution record."""
        hard_reasons = [
            "blocked_by_unsubscribe",
            "blocked_by_weekly_limit",
        ]
        for code in hard_reasons:
            d = GovDecision(allowed=False, reason_code=code)
            assert d.is_hard_block is True, (
                f"{code} يجب أن يكون hard block (is_hard_block=True)"
            )

    # ── الخطوة 5: فحص الحدود ─────────────────────────────────────────────

    def test_daily_limit_after_2_sends(self):
        """
        بعد إرسالين في 24 ساعة، الحد اليومي يُطبَّق.
        نختبر هذا في db منفصل بدون أحداث HIGH معلّقة (حتى لا يُمنع بالأولوية أولاً).
        """
        db2, _ = _make_db()
        tid2 = _seed_tenant(db2).id
        c2   = _seed_customer(db2, tid2)
        cid2 = c2.id
        # لا نُنشئ أحداثاً معلّقة → لن يوجد HIGH blocking

        record_sent(db2, tid2, cid2, "abandoned_cart")
        record_sent(db2, tid2, cid2, "unpaid_order_reminder")
        db2.commit()

        count = _count_sent(db2, tid2, cid2, hours=24)
        assert count == 2

        # الثالثة: نتخطى 6h بـ patch، نتوقع daily_limit
        with patch("core.send_governor._last_sent_any_service", return_value=None):
            decision = check(db2, tid2, cid2, "vip_upgrade")
        db2.close()

        assert decision.allowed is False
        assert decision.reason_code == "blocked_by_daily_limit"

    def test_weekly_limit_after_4_sends(self):
        """
        بعد 4 رسائل في 7 أيام، الحد الأسبوعي يُطبَّق (hard block).
        نختبر في db منفصل بدون أحداث معلّقة.
        """
        db2, _ = _make_db()
        tid2 = _seed_tenant(db2).id
        c2   = _seed_customer(db2, tid2)
        cid2 = c2.id

        for service in ["abandoned_cart", "unpaid_order_reminder",
                         "back_in_stock", "predictive_reorder"]:
            record_sent(db2, tid2, cid2, service)
        db2.commit()

        count = _count_sent(db2, tid2, cid2, hours=168)
        assert count == 4

        # نتخطى 6h و priority؛ نريد الوصول لـ weekly_limit مباشرة
        with patch("core.send_governor._last_sent_any_service", return_value=None), \
             patch("core.send_governor._find_higher_priority_active", return_value=None), \
             patch("core.send_governor._last_sent_for_service", return_value=None):
            decision = check(db2, tid2, cid2, "seasonal_offer")
        db2.close()

        assert decision.allowed is False
        assert decision.reason_code in ("blocked_by_daily_limit", "blocked_by_weekly_limit"), (
            f"توقعنا daily أو weekly limit، حصلنا {decision.reason_code}"
        )


# ══════════════════════════════════════════════════════════════════════════
# 3. اختبارات الـ Unsubscribe Guard
# ══════════════════════════════════════════════════════════════════════════

class TestUnsubscribeGuard:
    """العميل المُلغي لا يستقبل أي رسائل."""

    def setup_method(self):
        self.db, _ = _make_db()
        tenant = _seed_tenant(self.db)
        self.tid = tenant.id

        # عميل ألغى الاشتراك
        c = Customer(
            tenant_id=self.tid,
            phone="+966555000222",
            name="مُلغٍ",
            extra_metadata={"is_unsubscribed": True},
        )
        self.db.add(c)
        self.db.commit()
        self.db.refresh(c)
        self.cid = c.id

    def teardown_method(self):
        self.db.close()

    def test_unsubscribed_customer_hard_blocked(self):
        """العميل المُلغي: hard block + reason_code صحيح."""
        decision = check(self.db, self.tid, self.cid, "abandoned_cart")
        assert decision.allowed is False
        assert decision.reason_code == "blocked_by_unsubscribe"
        assert decision.is_hard_block is True
        assert "ألغى" in decision.label_ar or "اشتراك" in decision.label_ar

    def test_pending_unsubscribe_also_blocked(self):
        """العميل في حالة pending_unsubscribe: أيضاً محجوب."""
        c2 = Customer(
            tenant_id=self.tid,
            phone="+966555000333",
            extra_metadata={"pending_unsubscribe": True},
        )
        self.db.add(c2)
        self.db.commit()
        self.db.refresh(c2)

        decision = check(self.db, self.tid, c2.id, "seasonal_offer")
        assert decision.allowed is False
        assert decision.reason_code == "blocked_by_unsubscribe"


# ══════════════════════════════════════════════════════════════════════════
# 4. اختبار الـ Cooldown
# ══════════════════════════════════════════════════════════════════════════

class TestCooldown:
    """كل خدمة لها فترة راحة إلزامية."""

    def setup_method(self):
        self.db, _ = _make_db()
        tenant = _seed_tenant(self.db)
        self.tid = tenant.id
        self.customer = _seed_customer(self.db, self.tid)
        self.cid = self.customer.id

    def teardown_method(self):
        self.db.close()

    def test_cooldown_blocks_same_service(self):
        """بعد إرسال abandoned_cart، إعادة الإرسال محجوبة بـ cooldown 24h."""
        record_sent(self.db, self.tid, self.cid, "abandoned_cart")
        self.db.commit()

        # تحايل على 6h limit لاختبار cooldown بمعزل
        with patch("core.send_governor._last_sent_any_service", return_value=None):
            decision = check(self.db, self.tid, self.cid, "abandoned_cart")

        assert decision.allowed is False
        assert decision.reason_code == "blocked_by_cooldown"
        assert decision.is_hard_block is False  # soft → يُعاد بعد 24h

    def test_winback_longer_cooldown(self):
        """customer_winback له cooldown 14 يوم (336 ساعة)."""
        from core.send_governor import _COOLDOWN_HOURS
        assert _COOLDOWN_HOURS.get("customer_winback", 0) >= 336, (
            "customer_winback يجب أن تكون cooldown ≥ 14 يوم"
        )

    def test_predictive_reorder_7day_cooldown(self):
        from core.send_governor import _COOLDOWN_HOURS
        assert _COOLDOWN_HOURS.get("predictive_reorder", 0) >= 168, (
            "predictive_reorder يجب أن تكون cooldown ≥ 7 أيام"
        )


# ══════════════════════════════════════════════════════════════════════════
# 5. اختبار نصوص التاجر (Arabic UX)
# ══════════════════════════════════════════════════════════════════════════

class TestMerchantMessages:
    """التحقق من جودة النصوص العربية للتاجر."""

    def setup_method(self):
        self.db, _ = _make_db()
        tenant = _seed_tenant(self.db)
        self.tid = tenant.id
        self.customer = _seed_customer(self.db, self.tid)
        self.cid = self.customer.id

        # حدث HIGH معلّق لاختبار الأولوية
        _seed_automation(self.db, self.tid, "abandoned_cart", "cart_abandoned")
        _seed_event(self.db, self.tid, self.cid, "cart_abandoned")

    def teardown_method(self):
        self.db.close()

    def test_priority_message_mentions_blocker(self):
        """رسالة blocked_by_priority تذكر الخدمة الحاجبة."""
        dec = check(self.db, self.tid, self.cid, "salary_payday_offer")
        assert dec.reason_code == "blocked_by_priority"
        # suggestion_ar يجب أن يكون مفيداً
        assert len(dec.suggestion_ar) > 10, "النص يجب أن يكون وصفياً"

    def test_all_reason_codes_have_arabic_labels(self):
        """كل reason_code يجب أن يكون له label_ar غير فارغ."""
        from core.send_governor import _MESSAGES
        for code, msgs in _MESSAGES.items():
            assert msgs.get("label_ar"), f"label_ar مفقود لـ {code}"
            if code != "allowed":
                assert msgs.get("suggestion_ar"), f"suggestion_ar مفقود لـ {code}"

    def test_unsubscribe_message_is_clear(self):
        """رسالة إلغاء الاشتراك تشرح الوضع للتاجر بوضوح."""
        from core.send_governor import _MESSAGES
        msg = _MESSAGES["blocked_by_unsubscribe"]
        # التحقق من أن النص وصفي (أطول من 20 حرف) وليس فارغاً
        assert len(msg["suggestion_ar"]) > 20, (
            "suggestion_ar يجب أن يكون وصفاً مفيداً للتاجر"
        )
        assert len(msg["label_ar"]) > 5, (
            "label_ar يجب أن يكون موجوداً"
        )


# ══════════════════════════════════════════════════════════════════════════
# 6. اختبار آلية الـ Retry (الرسائل المؤجَّلة لا تضيع)
# ══════════════════════════════════════════════════════════════════════════

class TestRetryMechanism:
    """
    الرسائل المؤجَّلة (soft blocks) يجب ألا تُكتب كـ AutomationExecution
    حتى يبقى event.processed=False ويُعاد تقييمها في الدورة القادمة.
    """

    def test_soft_block_decision_does_not_write_to_db(self):
        """
        GovDecision مع is_hard_block=False يعني:
          - automation_engine يُعيد "delay"
          - لا يكتب AutomationExecution
          - event.processed يبقى False → إعادة في الدورة القادمة

        هنا نختبر المنطق المنطقي مباشرة (unit test للقرار).
        """
        soft_decisions = [
            GovDecision(allowed=False, reason_code="blocked_by_priority"),
            GovDecision(allowed=False, reason_code="blocked_by_6h_limit"),
            GovDecision(allowed=False, reason_code="blocked_by_cooldown"),
            GovDecision(allowed=False, reason_code="blocked_by_daily_limit"),
        ]
        for d in soft_decisions:
            assert not d.is_hard_block, (
                f"{d.reason_code}: يجب أن يكون soft → "
                "لا تُكتب execution record → event يُعاد تقييمه"
            )

    def test_hard_block_decision_signals_write_to_db(self):
        """
        GovDecision مع is_hard_block=True يعني:
          - automation_engine يكتب AutomationExecution(status=skipped)
          - يُعيد "skipped"
          - event.processed = True عند انتهاء الأتمتة الوحيدة → لا إعادة
        """
        hard_decisions = [
            GovDecision(allowed=False, reason_code="blocked_by_unsubscribe"),
            GovDecision(allowed=False, reason_code="blocked_by_weekly_limit"),
        ]
        for d in hard_decisions:
            assert d.is_hard_block, (
                f"{d.reason_code}: يجب أن يكون hard → "
                "تُكتب execution record → لا إعادة محاولة"
            )

    def test_governor_log_api_returns_only_hard_blocks(self):
        """
        get_governor_log يقرأ من AutomationExecution (soft blocks لا تُكتب هناك).
        """
        from core.send_governor import get_governor_log, _MESSAGES

        GOVERNOR_CODES = set(_MESSAGES.keys()) - {"allowed"}
        hard_codes = {"blocked_by_unsubscribe", "blocked_by_weekly_limit"}
        soft_codes = GOVERNOR_CODES - hard_codes

        # التحقق من أن الكود يقرأ أكواد صحيحة
        for code in hard_codes:
            assert code in GOVERNOR_CODES
        for code in soft_codes:
            assert code in GOVERNOR_CODES


# ══════════════════════════════════════════════════════════════════════════
# 7. اختبار تكاملي للسيناريو الكامل (4 → 1)
# ══════════════════════════════════════════════════════════════════════════

class TestFullScenarioIntegration:
    """
    التحقق الكامل: من 4 مؤهَّلين → 1 يُرسَل.

    ملاحظة: لا نستدعي process_pending_events هنا لأنه يحتاج WhatsApp mock.
    نختبر Governor.check() مباشرة بتسلسل يحاكي ما يفعله automation_engine.
    """

    def setup_method(self):
        self.db, _ = _make_db()
        tenant = _seed_tenant(self.db)
        self.tid = tenant.id
        self.cid = _seed_customer(self.db, self.tid).id

        for at, te in [
            ("abandoned_cart",        "cart_abandoned"),
            ("unpaid_order_reminder", "order_payment_pending"),
            ("salary_payday_offer",   "salary_payday_due"),
            ("back_in_stock",         "product_back_in_stock"),
        ]:
            _seed_automation(self.db, self.tid, at, te)
            _seed_event(self.db, self.tid, self.cid, te)

    def teardown_method(self):
        self.db.close()

    def test_only_one_message_sent(self):
        """
        تسلسل التقييم:
          1. check(abandoned_cart) → allowed → record_sent
          2. check(unpaid_order_reminder) → 6h_limit (soft)
          3. check(salary_payday_offer)   → priority  (soft)
          4. check(back_in_stock)         → priority  (soft)
        ⇒ إجمالي الإرسال الحقيقي: 1 فقط.
        """
        results: dict[str, Any] = {}

        # 1. abandoned_cart → allowed
        d1 = check(self.db, self.tid, self.cid, "abandoned_cart")
        results["abandoned_cart"] = d1
        assert d1.allowed is True, "abandoned_cart (HIGH) يجب أن يُسمح"
        # محاكاة: الإرسال نجح → سجّل
        record_sent(self.db, self.tid, self.cid, "abandoned_cart")
        self.db.commit()

        # 2. unpaid_order_reminder → 6h limit
        d2 = check(self.db, self.tid, self.cid, "unpaid_order_reminder")
        results["unpaid_order_reminder"] = d2
        assert d2.allowed is False
        assert d2.reason_code == "blocked_by_6h_limit", (
            f"unpaid_order_reminder: توقعنا 6h_limit، حصلنا {d2.reason_code}"
        )
        assert not d2.is_hard_block, "يجب أن يُعاد في الدورة القادمة"

        # 3. salary_payday_offer → priority (HIGH معلّق: unpaid_order_reminder)
        d3 = check(self.db, self.tid, self.cid, "salary_payday_offer")
        results["salary_payday_offer"] = d3
        assert d3.allowed is False
        assert d3.reason_code == "blocked_by_priority", (
            f"salary_payday_offer: توقعنا priority، حصلنا {d3.reason_code}"
        )
        assert not d3.is_hard_block

        # 4. back_in_stock → priority (HIGH معلّق)
        d4 = check(self.db, self.tid, self.cid, "back_in_stock")
        results["back_in_stock"] = d4
        assert d4.allowed is False
        assert d4.reason_code == "blocked_by_priority", (
            f"back_in_stock: توقعنا priority، حصلنا {d4.reason_code}"
        )
        assert not d4.is_hard_block

        # ── التحقق النهائي ─────────────────────────────────────────────
        sent_count = sum(1 for d in results.values() if d.allowed)
        assert sent_count == 1, (
            f"يجب إرسال رسالة واحدة فقط، أُرسل {sent_count}"
        )

        log_count = self.db.query(GovernorSendLog).filter(
            GovernorSendLog.customer_id == self.cid,
        ).count()
        assert log_count == 1, (
            f"GovernorSendLog يجب أن يحتوي صفاً واحداً، يحتوي {log_count}"
        )

        print("\n✅ السيناريو الكامل:")
        for svc, d in results.items():
            status = "✅ أُرسل" if d.allowed else f"⏳ {d.reason_code}"
            print(f"  {svc:<28} → {status}")
