"""
core/wa_notify.py
──────────────────
Platform-level WhatsApp notifications sent from Nahla's own number
to merchants (registration, billing, subscription events, expiry warnings).

All messages are sent via Meta Cloud API using the platform-level token.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import httpx

from core.config import WA_PHONE_ID, WA_TOKEN

logger = logging.getLogger("nahla-wa-notify")

_GRAPH_URL = "https://graph.facebook.com/v20.0"


def _normalize_phone(phone: str) -> str:
    """Strip spaces, dashes, plus signs and ensure starts with country code."""
    digits = re.sub(r"[^\d]", "", phone)
    # Saudi numbers: 05xxxxxxxx → 9665xxxxxxxx
    if digits.startswith("05") and len(digits) == 10:
        digits = "966" + digits[1:]
    elif digits.startswith("5") and len(digits) == 9:
        digits = "966" + digits
    return digits


async def _send(to: str, text: str) -> bool:
    """Send a plain text WhatsApp message from Nahla's platform number."""
    if not WA_TOKEN or not WA_PHONE_ID:
        logger.warning("[wa_notify] Platform WA_TOKEN or PHONE_ID not set — skipping notification")
        return False

    phone = _normalize_phone(to)
    if not phone:
        logger.warning("[wa_notify] Invalid phone number: %s", to)
        return False

    url = f"{_GRAPH_URL}/{WA_PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text, "preview_url": False},
    }
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (200, 201):
                logger.info("[wa_notify] Sent to=%s", phone)
                return True
            else:
                logger.error("[wa_notify] Failed to=%s status=%s body=%s", phone, resp.status_code, resp.text[:200])
                return False
    except Exception as exc:
        logger.error("[wa_notify] Error sending to=%s: %s", phone, exc)
        return False


# ── Platform notification messages ─────────────────────────────────────────────

async def notify_welcome(phone: str, store_name: str) -> bool:
    """Sent immediately after a new merchant registers."""
    if not phone:
        return False
    msg = (
        f"🍯 أهلاً بك في نحلة AI!\n\n"
        f"تم إنشاء حسابك لمتجر *{store_name}* بنجاح.\n\n"
        f"✅ تجربتك المجانية (14 يوم) بدأت الآن\n\n"
        f"🚀 ابدأ الإعداد الآن:\n"
        f"👉 https://app.nahlah.ai\n\n"
        f"📌 الخطوات التالية:\n"
        f"1️⃣ اربط متجرك (سلة / زد)\n"
        f"2️⃣ اربط واتساب\n"
        f"3️⃣ شغّل الطيار الآلي\n\n"
        f"فريق الدعم جاهز لمساعدتك هنا على واتساب 💬"
    )
    return await _send(phone, msg)


async def notify_trial_ending(phone: str, store_name: str, days_left: int) -> bool:
    """Warning sent 7 days and 3 days before trial ends."""
    if not phone:
        return False
    urgency = "⚠️" if days_left <= 3 else "📅"
    msg = (
        f"{urgency} *تنبيه: تجربتك المجانية تنتهي خلال {days_left} أيام*\n\n"
        f"متجر: *{store_name}*\n\n"
        f"للاستمرار في استخدام نحلة وعدم توقف ردودك الذكية، اشترك الآن:\n\n"
        f"💎 باقة المبتدئ: *449 ريال/شهر* (الأصلي 899)\n"
        f"🚀 باقة النمو: *849 ريال/شهر* (الأكثر اختياراً)\n"
        f"🏢 باقة التوسع: *1,499 ريال/شهر*\n\n"
        f"👉 اشترك الآن: https://app.nahlah.ai/billing\n\n"
        f"تحتاج مساعدة؟ أرسل *اشتراك* وسنساعدك."
    )
    return await _send(phone, msg)


async def notify_subscription_confirmed(
    phone: str,
    store_name: str,
    plan_name: str,
    amount_sar: int,
    next_billing: Optional[datetime] = None,
) -> bool:
    """Sent after successful subscription payment."""
    if not phone:
        return False
    next_date = next_billing.strftime("%-d %B %Y") if next_billing else "شهر من الآن"
    msg = (
        f"✅ *تم تفعيل اشتراكك في نحلة AI*\n\n"
        f"متجر: *{store_name}*\n"
        f"الباقة: *{plan_name}*\n"
        f"المبلغ: *{amount_sar:,} ريال سعودي*\n"
        f"تاريخ التجديد: {next_date}\n\n"
        f"🍯 نحلة تعمل الآن لصالح متجرك!\n"
        f"لوحة التحكم: https://app.nahlah.ai"
    )
    return await _send(phone, msg)


async def notify_payment_invoice(
    phone: str,
    store_name: str,
    plan_name: str,
    amount_sar: int,
    invoice_id: str,
    payment_date: Optional[datetime] = None,
) -> bool:
    """Send invoice details after payment."""
    if not phone:
        return False
    date_str = (payment_date or datetime.utcnow()).strftime("%Y/%m/%d")
    msg = (
        f"🧾 *فاتورة نحلة AI*\n"
        f"{'─' * 25}\n"
        f"رقم الفاتورة: #{invoice_id}\n"
        f"التاريخ: {date_str}\n"
        f"المتجر: {store_name}\n"
        f"الباقة: {plan_name}\n"
        f"المبلغ: *{amount_sar:,} ريال سعودي*\n"
        f"الحالة: ✅ مدفوعة\n"
        f"{'─' * 25}\n\n"
        f"شكراً لثقتك بنحلة 🍯\n"
        f"nahlah.ai"
    )
    return await _send(phone, msg)


async def notify_payment_link(
    phone: str,
    store_name: str,
    plan_name: str,
    amount_sar: int,
    payment_url: str,
) -> bool:
    """Send a payment link to the merchant."""
    if not phone:
        return False
    msg = (
        f"💳 *رابط الدفع — نحلة AI*\n\n"
        f"متجر: *{store_name}*\n"
        f"الباقة: *{plan_name}*\n"
        f"المبلغ: *{amount_sar:,} ريال سعودي*\n\n"
        f"👇 أكمل الدفع عبر الرابط:\n"
        f"{payment_url}\n\n"
        f"⏳ الرابط صالح لمدة 24 ساعة\n"
        f"الدفع آمن عبر Moyasar 🔒"
    )
    return await _send(phone, msg)


async def notify_subscription_expiring(
    phone: str,
    store_name: str,
    plan_name: str,
    days_left: int,
) -> bool:
    """Warning sent 7 days and 3 days before subscription expires."""
    if not phone:
        return False
    urgency = "🔴" if days_left <= 3 else "🟡"
    msg = (
        f"{urgency} *تنبيه: اشتراكك ينتهي خلال {days_left} {'يوم' if days_left == 1 else 'أيام'}*\n\n"
        f"متجر: *{store_name}*\n"
        f"الباقة: *{plan_name}*\n\n"
        f"{'⚠️ بعد الانتهاء ستتوقف ردود نحلة على عملائك.' if days_left <= 3 else 'جدّد اشتراكك لاستمرار الخدمة.'}\n\n"
        f"🔄 جدّد الآن: https://app.nahlah.ai/billing\n\n"
        f"أو أرسل *جدّد* وسنرسل لك رابط الدفع مباشرة."
    )
    return await _send(phone, msg)


async def notify_subscription_expired(phone: str, store_name: str) -> bool:
    """Sent when subscription has expired."""
    if not phone:
        return False
    msg = (
        f"❌ *انتهى اشتراكك في نحلة AI*\n\n"
        f"متجر: *{store_name}*\n\n"
        f"تم إيقاف الردود الذكية مؤقتاً على متجرك.\n\n"
        f"♻️ لإعادة التفعيل فوراً:\n"
        f"👉 https://app.nahlah.ai/billing\n\n"
        f"أو أرسل *اشتراك* وسنساعدك في اختيار الباقة المناسبة 🍯"
    )
    return await _send(phone, msg)


async def notify_store_connected(phone: str, store_name: str, platform: str) -> bool:
    """Sent when a merchant successfully connects their store."""
    if not phone:
        return False
    msg = (
        f"🔗 *تم ربط متجرك بنجاح!*\n\n"
        f"المتجر: *{store_name}*\n"
        f"المنصة: *{platform}*\n\n"
        f"✅ نحلة الآن تقرأ منتجاتك وطلباتك\n"
        f"الخطوة التالية: اربط واتساب لتبدأ الردود الذكية\n\n"
        f"👉 https://app.nahlah.ai/settings"
    )
    return await _send(phone, msg)


async def notify_whatsapp_connected(phone: str, store_name: str) -> bool:
    """Sent when WhatsApp is successfully connected."""
    if not phone:
        return False
    msg = (
        f"💬 *واتساب متصل بنحلة!*\n\n"
        f"متجر: *{store_name}*\n\n"
        f"🍯 نحلة جاهزة للرد على عملائك الآن!\n"
        f"شغّل الطيار الآلي من الإعدادات لتبدأ المبيعات:\n\n"
        f"👉 https://app.nahlah.ai/settings"
    )
    return await _send(phone, msg)
