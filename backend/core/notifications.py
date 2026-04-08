"""
core/notifications.py
─────────────────────
Email (Resend API) and WhatsApp (Meta Cloud API) notification helpers.
Also contains HTML email templates.
"""
from __future__ import annotations

import logging

import httpx

from core.config import RESEND_API_KEY, EMAIL_FROM, WA_TOKEN, WA_PHONE_ID

logger = logging.getLogger("nahla.notifications")


# ── Transports ─────────────────────────────────────────────────────────────────

async def send_email(to: str, subject: str, html: str) -> bool:
    """Send a transactional email via Resend API. Returns True on success."""
    if not RESEND_API_KEY:
        logger.warning("send_email: RESEND_API_KEY not set — skipping email to %s", to)
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html},
            )
            if resp.status_code in (200, 201):
                logger.info("Email sent: to=%s subject=%s", to, subject)
                return True
            logger.error(
                "Email failed: to=%s status=%s body=%s",
                to, resp.status_code, resp.text[:200],
            )
            return False
    except Exception as exc:
        logger.exception("Email error: to=%s exc=%s", to, exc)
        return False


async def send_whatsapp(to: str, text: str) -> bool:
    """Send a WhatsApp text message via Meta Cloud API. Returns True on success."""
    if not WA_TOKEN or not WA_PHONE_ID:
        logger.warning("send_whatsapp: WHATSAPP_TOKEN or PHONE_NUMBER_ID not set")
        return False
    phone = to.strip().replace(" ", "").replace("-", "").lstrip("0")
    if not phone.startswith("+"):
        phone = "+" + phone
    phone = phone.lstrip("+")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages",
                headers={
                    "Authorization": f"Bearer {WA_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            if resp.status_code == 200:
                logger.info("WhatsApp sent: to=%s", phone)
                return True
            logger.error(
                "WhatsApp failed: to=%s status=%s body=%s",
                phone, resp.status_code, resp.text[:200],
            )
            return False
    except Exception as exc:
        logger.exception("WhatsApp error: to=%s exc=%s", phone, exc)
        return False


# ── HTML email templates ───────────────────────────────────────────────────────

def email_verify(store_name: str, verify_url: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3>مرحباً بك في نحلة، {store_name}!</h3>
  <p>أنشأت حسابك بنجاح. أكّد بريدك الإلكتروني للبدء:</p>
  <a href="{verify_url}"
     style="display:inline-block;background:#f59e0b;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    تأكيد البريد الإلكتروني
  </a>
  <p style="color:#64748b;font-size:13px">الرابط صالح لمدة 24 ساعة.</p>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI</p>
</div>"""


def email_welcome(store_name: str, dashboard_url: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3>تم تفعيل حسابك بنجاح 🎉</h3>
  <p>مرحباً بك في <strong>{store_name}</strong>! يمكنك الآن الدخول للوحة التحكم وبدء ربط متجرك.</p>
  <a href="{dashboard_url}"
     style="display:inline-block;background:#f59e0b;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    الدخول إلى لوحة التحكم
  </a>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI</p>
</div>"""


def email_reset(reset_url: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3>إعادة تعيين كلمة المرور</h3>
  <p>استلمنا طلباً لإعادة تعيين كلمة مرور حسابك. انقر على الزر أدناه:</p>
  <a href="{reset_url}"
     style="display:inline-block;background:#ef4444;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    إعادة تعيين كلمة المرور
  </a>
  <p style="color:#64748b;font-size:13px">الرابط صالح لمدة ساعة واحدة فقط.</p>
  <p style="color:#64748b;font-size:13px">إذا لم تطلب هذا، تجاهل الرسالة.</p>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI</p>
</div>"""


def email_subscription(store_name: str, plan_name: str, ends_at: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3>تم تفعيل اشتراكك ✅</h3>
  <p>مرحباً <strong>{store_name}</strong>،</p>
  <p>تم تفعيل خطة <strong>{plan_name}</strong> بنجاح.</p>
  <p style="color:#64748b">ينتهي الاشتراك في: <strong>{ends_at}</strong></p>
  <a href="https://app.nahlah.ai/billing"
     style="display:inline-block;background:#f59e0b;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    إدارة اشتراكي
  </a>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI · <a href="https://nahlah.ai" style="color:#f59e0b">nahlah.ai</a></p>
</div>"""


def email_payment_failed(store_name: str, plan_name: str, amount_sar: float) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3 style="color:#ef4444">فشل عملية الدفع ❌</h3>
  <p>مرحباً <strong>{store_name}</strong>،</p>
  <p>لم تتم عملية الدفع بنجاح لخطة <strong>{plan_name}</strong> بقيمة <strong>{amount_sar:.0f} ريال</strong>.</p>
  <p>يرجى التحقق من بيانات البطاقة أو طريقة الدفع والمحاولة مجدداً.</p>
  <a href="https://app.nahlah.ai/billing"
     style="display:inline-block;background:#ef4444;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    تجديد الاشتراك الآن
  </a>
  <p style="color:#64748b;font-size:13px">إذا استمرت المشكلة، تواصل معنا على support@nahlah.ai</p>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI · <a href="https://nahlah.ai" style="color:#f59e0b">nahlah.ai</a></p>
</div>"""


def email_subscription_expiring(store_name: str, plan_name: str, days_left: int, ends_at: str) -> str:
    urgency_color = "#ef4444" if days_left <= 3 else "#f59e0b"
    urgency_icon  = "🔴" if days_left <= 3 else "🟡"
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3 style="color:{urgency_color}">{urgency_icon} اشتراكك على وشك الانتهاء</h3>
  <p>مرحباً <strong>{store_name}</strong>،</p>
  <p>اشتراكك في خطة <strong>{plan_name}</strong> سينتهي خلال <strong>{days_left} {'يوم' if days_left > 1 else 'يوم واحد'}</strong> بتاريخ {ends_at}.</p>
  <p>جدّد الآن لتجنب انقطاع الخدمة وفقدان تقدمك.</p>
  <a href="https://app.nahlah.ai/billing"
     style="display:inline-block;background:{urgency_color};color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    تجديد الاشتراك الآن
  </a>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI · <a href="https://nahlah.ai" style="color:#f59e0b">nahlah.ai</a></p>
</div>"""


def email_subscription_expired(store_name: str, plan_name: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3 style="color:#ef4444">انتهى اشتراكك 😔</h3>
  <p>مرحباً <strong>{store_name}</strong>،</p>
  <p>انتهى اشتراكك في خطة <strong>{plan_name}</strong>. تم إيقاف الردود الذكية مؤقتاً على متجرك.</p>
  <p>جدّد اشتراكك لاستعادة كل المميزات وإعادة تشغيل الطيار الآلي.</p>
  <a href="https://app.nahlah.ai/billing"
     style="display:inline-block;background:#f59e0b;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    تجديد الاشتراك الآن
  </a>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI · <a href="https://nahlah.ai" style="color:#f59e0b">nahlah.ai</a></p>
</div>"""


def email_invoice(store_name: str, plan_name: str, amount_sar: float,
                  invoice_id: str, payment_date: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3>فاتورة دفع #{{invoice_id}} 🧾</h3>
  <p>مرحباً <strong>{store_name}</strong>،</p>
  <table style="width:100%;border-collapse:collapse;margin:16px 0">
    <tr style="background:#f8fafc"><td style="padding:10px;border:1px solid #e2e8f0">الخطة</td><td style="padding:10px;border:1px solid #e2e8f0"><strong>{plan_name}</strong></td></tr>
    <tr><td style="padding:10px;border:1px solid #e2e8f0">المبلغ</td><td style="padding:10px;border:1px solid #e2e8f0"><strong>{amount_sar:.0f} ريال</strong></td></tr>
    <tr style="background:#f8fafc"><td style="padding:10px;border:1px solid #e2e8f0">تاريخ الدفع</td><td style="padding:10px;border:1px solid #e2e8f0">{payment_date}</td></tr>
    <tr><td style="padding:10px;border:1px solid #e2e8f0">رقم الفاتورة</td><td style="padding:10px;border:1px solid #e2e8f0">#{invoice_id}</td></tr>
  </table>
  <a href="https://app.nahlah.ai/billing"
     style="display:inline-block;background:#f59e0b;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    عرض تفاصيل الاشتراك
  </a>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI · <a href="https://nahlah.ai" style="color:#f59e0b">nahlah.ai</a></p>
</div>"""
