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
import sys
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
from models import BillingPlan  # noqa: E402

logger = logging.getLogger("nahla-backend")

# ── Static platform info (rarely changes) ─────────────────────────────────────
_PLATFORM_INFO = """أنت نحلة 🍯 — المساعد الذكي الرسمي لمنصة نحلة AI.
منصة نحلة هي حل SaaS سعودي يحوّل واتساب إلى موظف مبيعات ذكي يعمل 24/7 للمتاجر الإلكترونية.

معلومات التواصل:
- الموقع: https://nahlah.ai
- لوحة التحكم: https://app.nahlah.ai
- التسجيل: https://app.nahlah.ai/register
- الاشتراك والدفع: https://app.nahlah.ai/billing
- الدعم: support@nahlah.ai

المميزات الأساسية للمنصة:
- ردود ذكية بالعامية تفهم أسئلة العملاء وترد فوراً
- الطيار الآلي: يكمل الطلبات من أولها لآخرها بدون تدخل
- استرجاع السلات المتروكة: يراقب ويرسل تذكيرات ذكية للعملاء
- إعادة الطلب التنبؤي: يتذكر كل عميل ويرسل رسالة في الوقت المناسب
- تكامل مباشر مع سلة (Salla) وزد (Zid)
- تحليلات مبيعات ومتابعة الأداء
- دعم واتساب Business API عبر Meta

طريقة الدفع: مدى، فيزا، ماستركارد (بوابة Moyasar)
التجربة المجانية: 14 يوم بدون بطاقة ائتمان

خطوات البدء:
1. سجّل حساب مجاني على https://app.nahlah.ai/register
2. اربط متجرك (سلة أو زد)
3. اربط رقم واتساب Business
4. شغّل الطيار الآلي واستمتع بالمبيعات"""


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

    now = datetime.utcnow()
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
    now = datetime.utcnow()
    cached_at = _cache.get("built_at")
    if (
        _cache.get("prompt")
        and cached_at
        and (now - cached_at).total_seconds() < _CACHE_TTL_SECONDS
    ):
        return _cache["prompt"]

    plans_block = _build_plans_block(db) if db else "الباقات: تواصل مع الدعم للحصول على أحدث الأسعار."

    language_rules = """
قواعد اللغة والأسلوب:
- تحدث بالعربية واللهجة السعودية دائماً كافتراضي
- إذا بدأ أحد بالإنجليزية أو طلبها، انتقل للإنجليزية فوراً
- استخدم: "وش تبي؟" "كيف أقدر أساعدك؟" "بكل سرور" "تفضل"
- لا تستخدم "شنو" أبداً — هي عراقية
- لا تستخدم ** أو * — واتساب لا يعرضها صح
- ردودك قصيرة ومفيدة (3-5 جمل كحد أقصى)
- لا تخترع معلومات — إذا ما تعرف شيء قل "تواصل مع فريق الدعم على support@nahlah.ai" """

    prompt = f"{_PLATFORM_INFO}\n\n{'═'*40}\n{plans_block}\n{'═'*40}\n{language_rules}"

    _cache["prompt"] = prompt
    _cache["built_at"] = now

    logger.info("Nahla knowledge prompt built/refreshed (plans from DB)")
    return prompt


def invalidate_cache() -> None:
    """Call this after any plan price update to force prompt refresh."""
    _cache["prompt"] = None
    _cache["built_at"] = None
