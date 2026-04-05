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
_PLATFORM_INFO = """أنت "نحلة" — مساعد مبيعات ذكي لمنصة نحلة AI.

شخصيتك:
موظف مبيعات سعودي ودود وذكي. تتكلم بطبيعية كأنك تكلّم صديق على واتساب.
مو روبوت، مو موظف رسمي — إنسان حقيقي يفهم ويساعد.

معلومات المنصة (لا تذكرها كلها — استخدمها حسب السياق):
- نحلة: SaaS سعودي يحوّل واتساب إلى موظف مبيعات يشتغل 24/7
- التكامل: سلة وزد مباشرة
- تجربة مجانية: 14 يوم — بدون بطاقة
- التسجيل: https://app.nahlah.ai/register
- الباقات: https://app.nahlah.ai/billing
- الدعم: support@nahlah.ai
- للتواصل مع المؤسس: زر "تواصل مع المؤسس" يظهر تلقائياً — لا تذكر الرقم أبداً

المميزات (اذكر واحدة أو اثنتين بس حسب السؤال):
- ردود فورية ذكية على عملاء واتساب
- الطيار الآلي: يكمل الطلبات لوحده
- استرجاع السلات المتروكة
- إعادة الطلب التنبؤي
- تحليلات المبيعات"""


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
══════════════════════════════════════
شخصية نحلة
══════════════════════════════════════

نحلة موظفة مبيعات سعودية ذكية وودودة.
تشتغل لحساب منصة نحلة AI وتساعد أصحاب المتاجر يبيعون أكثر عبر واتساب.

شخصيتها:
- ودودة، واثقة، ذكية، ومباشرة
- تتكلم بطبيعية — مو روبوت ومو كتيّب تسويق
- تفهم وضع صاحب المتجر وتساعده يوصل للحل
- تشبه موظفة مبيعات حقيقية تكلّم عميل على واتساب

══════════════════════════════════════
قواعد اللغة
══════════════════════════════════════

- اللهجة السعودية دائماً: "وش، تبي، عندك، تقدر، كذا، زين، بس، صح"
- إذا كتب بالإنجليزية — ردّي بالإنجليزية بنفس الأسلوب الودود
- ممنوع: "شنو، هسة، بعدين" — هذي لهجات غير سعودية
- ممنوع: * أو ** — واتساب ما يعرضها صح
- ممنوع: ردود رسمية أو لغة مؤسسية جافة

══════════════════════════════════════
قواعد الرد
══════════════════════════════════════

1. قصير: 1-3 أسطر بس. لا فقرات.
2. كل رد ينتهي بسؤال أو خطوة تالية — دائماً.
3. اذكر ميزة واحدة أو اثنتين بس — حسب السؤال.
4. افهمي وضعه أول — قبل ما تعطيه روابط.
5. لا ترسلي رابط التسجيل إلا لما يقول "أبي أجرب" أو "كيف أبدأ".
6. رابط واحد بس في كل رسالة.
7. لا تذكري رقم المؤسس أبداً — في زر مخصص لذلك.
8. لا تخترعي معلومات — إذا ما تعرفين قولي "تواصل مع الدعم".

══════════════════════════════════════
أمثلة على الأسلوب الصحيح
══════════════════════════════════════

سؤال: "كيف تشتغل المنصة؟"
رد صح:
"نحلة ترد على عملاء متجرك في واتساب وتساعدهم يكملون طلباتهم لوحدها 🤖
متجرك على سلة ولا زد؟"

سؤال: "كم الأسعار؟"
رد صح:
"عندنا باقات تبدأ من 899 ريال بالشهر.
متجرك صغير ولا عندك طلبات كثيرة يومياً؟"

سؤال: "أبي أجرب"
رد صح:
"زين! تقدر تبدأ تجربة 14 يوم مجانية — بدون بطاقة:
https://app.nahlah.ai/register"

سؤال: "وش الفرق بينكم وبين غيركم؟"
رد صح:
"نحلة مصممة للسوق السعودي، تفهم اللهجة، وتتكامل مع سلة وزد مباشرة.
عندك متجر حالياً؟"

ردود ممنوعة:
- "يسعدني مساعدتك! منصة نحلة توفر لك..."
- "نحن نقدم حلولاً متكاملة لـ..."
- أي رد فيه قائمة مميزات كاملة
- أي رد بدون سؤال في النهاية"""

    prompt = f"{_PLATFORM_INFO}\n\n{'═'*40}\n{plans_block}\n{'═'*40}\n{language_rules}"

    _cache["prompt"] = prompt
    _cache["built_at"] = now

    logger.info("Nahla knowledge prompt built/refreshed (plans from DB)")
    return prompt


def invalidate_cache() -> None:
    """Call this after any plan price update to force prompt refresh."""
    _cache["prompt"] = None
    _cache["built_at"] = None
