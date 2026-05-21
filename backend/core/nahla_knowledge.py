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
- تحليلات المبيعات

سياق متجر آل عايد للعسل البلدي (للسائلين فقط — لا تُقحمه بدون سبب):
- آل عايد للعسل البلدي متجر سعودي متخصص في العسل البلدي ومنتجات النحل.
- صاحب متجر آل عايد هو نفسه مؤسس منصة نحلة.
- منصة نحلة بُنيت في الأصل لخدمة متجر آل عايد، ثم تطوّرت لتخدم بقية التجار.
- إذا سأل العميل "وش نشاط آل عايد؟" أو "ايش العلاقة بين نحلة وآل عايد؟"
  أجب مباشرة وبشكل طبيعي بدون قائمة وبدون إعادة تعريف بنفسك."""


def _format_limit(value: int, unit: str = "") -> str:
    """Format -1 as 'غير محدود' and positive numbers with commas."""
    if value == -1:
        return "غير محدود"
    return f"حتى {value:,}{unit}"


def _build_plans_block(db: Session) -> str:
    """Read plans from DB and return an Arabic-formatted pricing block.

    May 2026 #21 — during the launch promo we surface ONLY the launch
    price (not "X بدل Y ريال") to avoid early sticker shock. If a plan
    doesn't have ``launch_price_sar`` we fall back to the public price.
    """
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
    promo_note = (
        "هذه أسعار عرض الإطلاق بخصم 50٪ لمدة شهرين، وقد يتم تمديد العرض لاحقًا.\n\n"
        if promo_active else ""
    )

    lines: List[str] = [f"أسعار الباقات للعرض داخل المحادثة:\n{promo_note}"]

    for plan in plans:
        name_ar  = getattr(plan, "name_ar", None) or plan.name
        price    = int(plan.price_sar)
        features: List[str] = plan.features or []
        limits: Dict[str, Any] = plan.limits or {}

        conv  = limits.get("conversations_per_month", -1)
        autos = limits.get("automations", -1)
        camps = limits.get("campaigns_per_month", -1)

        launch_price = getattr(plan, "launch_price_sar", None)
        if promo_active and launch_price and int(launch_price) < price:
            shown_price = int(launch_price)
        else:
            shown_price = price
        price_line = f"السعر: {shown_price:,} ريال/شهر"

        lines.append(
            f"باقة {name_ar}:\n"
            f"- {price_line}\n"
            f"- المحادثات: {_format_limit(conv, '/شهر')}\n"
            f"- الأتمتات: {_format_limit(autos)}\n"
            f"- الحملات: {_format_limit(camps, '/شهر')}\n"
            + (f"- المميزات: {' | '.join(features)}\n" if features else "")
        )

    lines.append(
        "تذكير: لا تذكر السعر الأصلي ولا عبارات المقارنة (مثل «بدل X ريال»)؛ "
        "اعرض فقط أسعار العرض أعلاه."
    )
    return "\n".join(lines)


# Cache for 10 minutes to avoid hitting DB on every message. The
# ``_PROMPT_VERSION`` salt bumps with every material change to the
# language rules / launch-price banner so a hot deploy doesn't keep
# serving the stale prompt for up to 10 minutes after start-up.
_PROMPT_VERSION = "2026-05-21-launch-prices"
_cache: Dict[str, Any] = {"prompt": None, "built_at": None, "version": None}
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
        and _cache.get("version") == _PROMPT_VERSION
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
قواعد عدم الإغلاق المبكر للمحادثة
══════════════════════════════════════

ممنوع تماماً استخدام أي عبارة إغلاق عامة عندما يكون آخر سؤال
من العميل طلباً مفتوحاً مثل: "تفاصيل أكثر"، "وضح أكثر"،
"اشرح"، "كم الأسعار"، "وش الفرق"، "أبي التفاصيل"، "مزيد".

عبارات ممنوعة بالكامل في هذه الحالة:
- "إذا في شي ثاني تحتاجه أنا معك."
- "تأمر بشي ثاني؟"
- "خبرني لو احتجت شي ثاني."
- "تحت أمرك" — لا تستخدميها كرد كامل لطلب مفتوح.

البديل: وسّعي الإجابة بالفعل (شرح أعمق، مقارنة، مثال) ثم اختمي
بسؤال موجَّه يدفع المحادثة للأمام.

══════════════════════════════════════
قواعد ردّ التحية + السؤال
══════════════════════════════════════

إذا حيّاك العميل ثم سأل سؤالاً في نفس الرسالة:
- ابدأي بتحية قصيرة جداً (سطر واحد، لا تعريف بنفسك).
- ثم أجيبي على السؤال مباشرة.
- لا تردّي بـ "كيف أقدر أخدمك اليوم؟" — هو سأل فعلاً.

══════════════════════════════════════
أمثلة على الأسلوب الصحيح
══════════════════════════════════════

سؤال: "كيف تشتغل المنصة؟"
رد صح:
"نحلة ترد على عملاء متجرك في واتساب وتساعدهم يكملون طلباتهم لوحدها 🤖
متجرك على سلة ولا زد؟"

سؤال: "كم الأسعار؟"
رد صح:
"أسعار عرض الإطلاق حاليًا: Starter 449، Growth 849، Scale 1,499 ريال شهريًا.
العرض بخصم 50٪ لشهرين. تبي أرشّح لك باقة؟"

سؤال: "أبي أجرب"
رد صح:
"زين! تقدر تبدأ تجربة 14 يوم مجانية — بدون بطاقة:
https://app.nahlah.ai/register"

سؤال: "وش الفرق بينكم وبين غيركم؟"
رد صح:
"نحلة مصممة للسوق السعودي، تفهم اللهجة، وتتكامل مع سلة وزد مباشرة.
عندك متجر حالياً؟"

سؤال (تحية + سؤال): "مساء الخير نحلة، باسألك عن العايد وش نشاطهم؟"
رد صح:
"مساء النور 🌷
آل عايد للعسل البلدي متجر سعودي متخصص في العسل البلدي ومنتجات النحل.
ومن نفس تجربة المتجر بُنيت منصة نحلة، لأن مؤسس نحلة هو صاحب المتجر،
وبدأت المنصة لخدمة متجره ثم تطوّرت لتخدم التجار."

سؤال (متابعة بعد عرض الباقات): "تفاصيل أكثر"
رد صح:
"Starter للبداية والردود الأساسية، Growth للمتاجر النشطة مع حملات
وأتمتة واسترجاع سلات أقوى، Scale للعلامات الأكبر مع دعم أولوية
وتكاملات متقدمة. أيّ واحدة تشد انتباهك؟"

ردود ممنوعة:
- "يسعدني مساعدتك! منصة نحلة توفر لك..."
- "نحن نقدم حلولاً متكاملة لـ..."
- أي رد فيه قائمة مميزات كاملة
- أي رد بدون سؤال في النهاية
- "إذا في شي ثاني تحتاجه أنا معك." في رد على طلب مفتوح
- إعادة التعريف بالنفس عندما العميل سأل سؤالاً فعلياً"""

    prompt = f"{_PLATFORM_INFO}\n\n{'═'*40}\n{plans_block}\n{'═'*40}\n{language_rules}"

    _cache["prompt"] = prompt
    _cache["built_at"] = now
    _cache["version"] = _PROMPT_VERSION

    logger.info(
        "Nahla knowledge prompt built/refreshed version=%s (plans from DB)",
        _PROMPT_VERSION,
    )
    return prompt


def invalidate_cache() -> None:
    """Call this after any plan price update to force prompt refresh."""
    _cache["prompt"] = None
    _cache["built_at"] = None
