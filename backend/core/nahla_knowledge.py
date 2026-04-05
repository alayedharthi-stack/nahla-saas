"""
core/nahla_knowledge.py
───────────────────────
Builds a dynamic knowledge block about Nahla AI platform.
Reads live billing plans from the database so the bot always
knows the current prices, features, and limits — no manual updates needed.

Usage:
    from core.nahla_knowledge import build_nahla_system_prompt
    prompt = build_nahla_system_prompt(db)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from models import BillingPlan  # noqa: E402

logger = logging.getLogger("nahla-backend")

# ── Static platform info (rarely changes) ─────────────────────────────────────
_PLATFORM_INFO = """أنت مستشار مبيعات احترافي لمنصة نحلة AI — اسمك "نحلة".
هدفك الوحيد: تفهم وضع العميل أولاً، ثم تقوده خطوة بخطوة نحو التجربة المجانية.

معلومات المنصة (للرجوع إليها فقط — لا تعرضها كلها دفعة واحدة):
- المنصة: SaaS سعودي يحوّل واتساب إلى موظف مبيعات ذكي يعمل 24/7
- التكامل: سلة (Salla) وزد (Zid) مباشرة
- التجربة المجانية: 14 يوم بدون بطاقة ائتمان
- رابط التسجيل: https://app.nahlah.ai/register
- رابط الباقات: https://app.nahlah.ai/billing
- الدعم: support@nahlah.ai
- رقم المؤسس للتواصل المباشر: +966555906901

المميزات (اذكر واحدة أو اثنتين فقط حسب سؤال العميل — لا تعدّدها كلها):
- ردود ذكية فورية على عملاء واتساب
- الطيار الآلي: يكمل الطلبات بدون تدخل
- استرجاع السلات المتروكة بتذكيرات ذكية
- إعادة الطلب التنبؤي في الوقت المناسب
- تحليلات مبيعات ومتابعة الأداء"""


def _format_limit(value: int, unit: str = "") -> str:
    """Format -1 as 'غير محدود' and positive numbers with commas."""
    if value == -1:
        return "غير محدود"
    return f"حتى {value:,}{unit}"


def _build_plans_block(db: Session) -> str:
    """Read plans from DB and return an Arabic-formatted pricing block."""
    try:
        plans: List[BillingPlan] = (
            db.query(BillingPlan)
            .filter(BillingPlan.is_active == True)  # noqa: E712
            .order_by(BillingPlan.price_sar)
            .all()
        )
    except Exception as exc:
        logger.warning("Could not load billing plans from DB: %s", exc)
        plans = []

    if not plans:
        return "الباقات: تواصل مع الدعم للحصول على أحدث الأسعار."

    now = datetime.now(timezone.utc)
    promo_active = now < datetime(2026, 6, 30, 23, 59, 59)
    promo_note = "⚠️ عرض الإطلاق ساري حتى 30 يونيو 2026\n\n" if promo_active else ""

    lines: List[str] = [f"الباقات والأسعار:\n{promo_note}"]

    for plan in plans:
        name_ar  = getattr(plan, "name_ar", None) or plan.name
        price    = int(plan.price_sar)
        features: List[str] = plan.features or []
        limits: Dict[str, Any] = plan.limits or {}

        conv  = limits.get("conversations_per_month", -1)
        autos = limits.get("automations", -1)
        camps = limits.get("campaigns_per_month", -1)

        # Use launch price if promo is active and field exists
        launch_price = getattr(plan, "launch_price_sar", None)
        price_line = (
            f"سعر الإطلاق: {int(launch_price):,} ريال/شهر ✨  (بدل {price:,} ريال)"
            if promo_active and launch_price and int(launch_price) < price
            else f"السعر: {price:,} ريال/شهر"
        )

        lines.append(
            f"باقة {name_ar}:\n"
            f"- {price_line}\n"
            f"- المحادثات: {_format_limit(conv, '/شهر')}\n"
            f"- الأتمتات: {_format_limit(autos)}\n"
            f"- الحملات: {_format_limit(camps, '/شهر')}\n"
            + (f"- المميزات: {' | '.join(features)}\n" if features else "")
        )

    return "\n".join(lines)


# Cache for 10 minutes to avoid hitting DB on every message
_cache: Dict[str, Any] = {"prompt": None, "built_at": None}
_CACHE_TTL_SECONDS = 600


def build_nahla_system_prompt(db: Optional[Session] = None) -> str:
    """
    Build the full Nahla system prompt with live plan data from DB.
    Result is cached for 10 minutes.
    """
    now = datetime.now(timezone.utc)
    cached_at = _cache.get("built_at")
    if (
        _cache.get("prompt")
        and cached_at
        and (now - cached_at).total_seconds() < _CACHE_TTL_SECONDS
    ):
        return _cache["prompt"]

    plans_block = _build_plans_block(db) if db else "الباقات: تواصل مع الدعم للحصول على أحدث الأسعار."

    language_rules = """
قواعد الأسلوب — اتبعها في كل رد:

1. الرد قصير: 2-4 أسطر كحد أقصى. لا فقرات طويلة.

2. رابط واحد فقط لكل رسالة. لا ترسل أكثر من رابط في نفس الرسالة.

3. كل رد ينتهي بسؤال يكمل المحادثة. دائماً.

4. لا تعدد المميزات — اذكر واحدة أو اثنتين فقط حسب سؤال العميل.

5. لا تكتب فقرات تسويقية — تكلم بشكل طبيعي كمستشار حقيقي.

6. افهم وضع العميل قبل ما ترسل أي رابط:
   - اسأل عن متجره أولاً (سلة؟ زد؟ غيرهم؟)
   - اسأل عن حجم الطلبات أو مشكلته الرئيسية
   - اسأل إذا عنده رقم واتساب Business جاهز

7. لا ترسل رابط التسجيل إلا بعد ما يُظهر اهتمام حقيقي.
   إذا سأل بشكل عام — اسأل عن وضعه أولاً.
   إذا قال "أبي أجرب" أو "كيف أبدأ" — أرسل الرابط.

8. اللغة: عربية واللهجة السعودية افتراضياً.
   إذا كتب بالإنجليزية — رد بالإنجليزية.
   لا تستخدم "شنو" — هي عراقية.
   لا تستخدم * أو ** — واتساب لا يعرضها صح.

9. لا تخترع معلومات. إذا ما تعرف — قل "تواصل مع فريق الدعم".

أمثلة على الأسلوب الصحيح:

سؤال: "كيف تعمل المنصة؟"
رد صحيح:
"نحلة ترد على عملاء متجرك في واتساب وتساعدهم يكملون الطلب تلقائياً.
متجرك على سلة أو منصة أخرى؟"

سؤال: "كم السعر؟"
رد صحيح:
"عندنا باقات تبدأ من 899 ريال شهرياً.
متجرك صغير ولا عندك طلبات كثيرة يومياً؟"

سؤال: "أبي أجرب"
رد صحيح:
"تقدر تبدأ تجربة 14 يوم مجانية — بدون بطاقة ائتمان:
https://app.nahlah.ai/register" """

    prompt = f"{_PLATFORM_INFO}\n\n{'═'*40}\n{plans_block}\n{'═'*40}\n{language_rules}"

    _cache["prompt"] = prompt
    _cache["built_at"] = now

    logger.info("Nahla knowledge prompt built/refreshed (plans from DB)")
    return prompt


def invalidate_cache() -> None:
    """Call this after any plan price update to force prompt refresh."""
    _cache["prompt"] = None
    _cache["built_at"] = None
