"""
services/billing_formatter.py
──────────────────────────────
Nahla-specific billing context resolver and message formatters.

This module is the single source of truth for how billing information
is described to Nahla merchants (tenants).  It deliberately does NOT
import or reference anything from shawahid-service so the two codebases
remain fully isolated.

Public API
──────────
  resolve_billing_context(db, *, tenant_id, sub, plan_obj, payment_id,
                          payment_amount_sar, paid_at)
      → BillingContext   (TypedDict — plain dict in practice)

  build_nahla_payment_link_message(ctx)  → str   (WhatsApp text)
  build_nahla_payment_receipt_message(ctx) → str (WhatsApp text)
  build_nahla_email_invoice_html(ctx)    → str   (HTML for Resend)

Context fields
──────────────
  merchant_name   – owner's display name (falls back to store_name / "عزيزنا التاجر")
  store_name      – tenant store label   (falls back to merchant_name / f"متجر #{tenant_id}")
  tenant_id       – integer tenant PK
  plan_name       – human plan label (Arabic preferred)
  plan_slug       – machine key, e.g. "starter" / "growth" / "scale"
  billing_period  – "شهري" | "سنوي" (derived from BillingPlan.billing_cycle)
  amount_sar      – integer SAR amount
  payment_id      – gateway payment ID (Moyasar / HyperPay)
  invoice_id      – display invoice ref (short hash of payment_id or BillingPayment.id)
  paid_at         – ISO date string of payment "2026-05-04"
  ends_at         – subscription end date string "2026-06-04"
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.billing_formatter")

# ── Billing-period label map ────────────────────────────────────────────────

_CYCLE_LABELS: Dict[str, str] = {
    "monthly":   "شهري",
    "yearly":    "سنوي",
    "annual":    "سنوي",
    "quarterly": "ربع سنوي",
    "weekly":    "أسبوعي",
}


def _cycle_label(cycle: Optional[str]) -> str:
    return _CYCLE_LABELS.get((cycle or "").lower(), "شهري")


# ── Context resolver ────────────────────────────────────────────────────────

def resolve_billing_context(
    db,
    *,
    tenant_id: int,
    sub,                            # BillingSubscription ORM object
    plan_obj=None,                  # BillingPlan ORM object (optional; resolved if None)
    payment_id: str = "",
    payment_amount_sar: Optional[int] = None,
    paid_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Build a billing context dict from DB objects.

    Always call this **inside** a try/except at the call site so a
    missing settings row can never blow up the webhook handler.
    """
    from models import BillingPlan, Tenant, User  # noqa: PLC0415

    # ── Tenant / merchant info ──────────────────────────────────────────────
    tenant_obj: Optional[Any] = (
        db.query(Tenant).filter(Tenant.id == tenant_id).first()
    )
    merchant: Optional[Any] = (
        db.query(User).filter(
            User.tenant_id == tenant_id,
            User.role == "merchant",
            User.is_active == True,  # noqa: E712
        )
        .order_by(User.id.asc())
        .first()
    )

    # Prefer the merchant's display name; fall back to store name / generic
    merchant_name: str = ""
    if merchant:
        merchant_name = (
            getattr(merchant, "full_name", "")
            or getattr(merchant, "name", "")
            or ""
        ).strip()

    # Try to pull store_name from TenantSettings JSON first
    store_name: str = ""
    try:
        from core.settings_utils import get_or_create_settings, merge_defaults, DEFAULT_STORE  # noqa: PLC0415
        _s = get_or_create_settings(db, tenant_id)
        _st = merge_defaults(_s.store_settings, DEFAULT_STORE)
        store_name = (_st.get("store_name") or "").strip()
    except Exception:
        pass

    if not store_name and tenant_obj:
        store_name = (tenant_obj.name or "").strip()
    if not store_name:
        store_name = f"متجر #{tenant_id}"

    # Use store_name as fallback for merchant_name and vice-versa
    if not merchant_name:
        merchant_name = store_name
    if store_name == f"متجر #{tenant_id}" and merchant_name != store_name:
        store_name = merchant_name

    # ── Plan info ───────────────────────────────────────────────────────────
    if plan_obj is None and sub is not None and getattr(sub, "plan_id", None):
        plan_obj = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()

    plan_name: str = "الباقة المختارة"
    plan_slug: str = ""
    billing_period: str = "شهري"
    if plan_obj:
        meta = plan_obj.extra_metadata or {}
        plan_name = meta.get("name_ar") or plan_obj.name or plan_name
        plan_slug = plan_obj.slug or ""
        billing_period = _cycle_label(plan_obj.billing_cycle)

    # ── Amount / payment ────────────────────────────────────────────────────
    if payment_amount_sar is None and sub is not None:
        sub_meta = sub.extra_metadata or {}
        payment_amount_sar = int(sub_meta.get("price_charged_sar", 0))
    amount_sar: int = payment_amount_sar or 0

    # ── Dates ───────────────────────────────────────────────────────────────
    paid_at_dt = paid_at or datetime.now(timezone.utc)
    paid_at_str = paid_at_dt.strftime("%Y/%m/%d")

    ends_at_str = "—"
    if sub is not None and getattr(sub, "ends_at", None):
        ends_at_str = sub.ends_at.strftime("%Y/%m/%d")

    # ── Invoice ID ──────────────────────────────────────────────────────────
    # Use first 12 chars of the gateway payment_id, or the DB subscription id
    if payment_id:
        invoice_id = payment_id[:12].upper()
    elif sub is not None:
        invoice_id = f"SUB-{sub.id}"
    else:
        invoice_id = "—"

    # ── Merchant contact ────────────────────────────────────────────────────
    merchant_email: str = ""
    merchant_phone: str = ""
    if merchant:
        merchant_email = (getattr(merchant, "email", "") or "").strip()
        merchant_phone = (getattr(merchant, "username", "") or "").strip()

    ctx: Dict[str, Any] = {
        "tenant_id":      tenant_id,
        "merchant_name":  merchant_name,
        "store_name":     store_name,
        "plan_name":      plan_name,
        "plan_slug":      plan_slug,
        "billing_period": billing_period,
        "amount_sar":     amount_sar,
        "payment_id":     payment_id or "—",
        "invoice_id":     invoice_id,
        "paid_at":        paid_at_str,
        "ends_at":        ends_at_str,
        "merchant_email": merchant_email,
        "merchant_phone": merchant_phone,
    }
    logger.debug(
        "[BillingFormatter] resolved context tenant=%s plan=%s amount=%s SAR invoice=%s",
        tenant_id, plan_slug, amount_sar, invoice_id,
    )
    return ctx


# ── WhatsApp message builders ───────────────────────────────────────────────

def build_nahla_payment_link_message(
    ctx: Dict[str, Any],
    payment_url: str,
) -> str:
    """
    WhatsApp message sent to the merchant when a payment link is created.

    Example output:
        مرحبًا أحمد العمري 👋

        هذا رابط سداد اشتراك نحلة AI لمتجر:
        متجر الريادة

        الباقة: باقة النمو
        المدة: شهري
        المبلغ: 849 ريال
        رقم التاجر: 42

        رابط الدفع:
        https://api.moyasar.com/...

        ⏳ الرابط صالح لمدة 24 ساعة
        الدفع آمن 🔒
    """
    return (
        f"مرحبًا {ctx['merchant_name']} 👋\n\n"
        f"هذا رابط سداد اشتراك نحلة AI لمتجر:\n"
        f"*{ctx['store_name']}*\n\n"
        f"الباقة: *{ctx['plan_name']}*\n"
        f"المدة: {ctx['billing_period']}\n"
        f"المبلغ: *{ctx['amount_sar']:,} ريال*\n"
        f"رقم التاجر: #{ctx['tenant_id']}\n\n"
        f"رابط الدفع:\n"
        f"{payment_url}\n\n"
        f"⏳ الرابط صالح لمدة 24 ساعة\n"
        f"الدفع آمن عبر Moyasar 🔒"
    )


def build_nahla_payment_receipt_message(ctx: Dict[str, Any]) -> str:
    """
    WhatsApp receipt sent to the merchant after successful payment.

    Example output:
        تم استلام دفعتك بنجاح ✅

        🧾 فاتورة اشتراك نحلة AI
        ─────────────────────────
        اسم التاجر: أحمد العمري
        اسم المتجر: متجر الريادة
        رقم التاجر: #42
        الباقة: باقة النمو
        المدة: شهري
        المبلغ: 849 ريال
        رقم العملية: PAY_ABC123...
        تاريخ الدفع: 2026/05/04
        الاشتراك حتى: 2026/06/04
        ─────────────────────────

        تم تفعيل الاشتراك بنجاح 🍯
        لوحة التحكم: https://app.nahlah.ai
    """
    divider = "─" * 25
    return (
        f"تم استلام دفعتك بنجاح ✅\n\n"
        f"🧾 *فاتورة اشتراك نحلة AI*\n"
        f"{divider}\n"
        f"اسم التاجر: {ctx['merchant_name']}\n"
        f"اسم المتجر: {ctx['store_name']}\n"
        f"رقم التاجر: #{ctx['tenant_id']}\n"
        f"الباقة: *{ctx['plan_name']}*\n"
        f"المدة: {ctx['billing_period']}\n"
        f"المبلغ: *{ctx['amount_sar']:,} ريال*\n"
        f"رقم العملية: {ctx['invoice_id']}\n"
        f"تاريخ الدفع: {ctx['paid_at']}\n"
        f"الاشتراك حتى: {ctx['ends_at']}\n"
        f"{divider}\n\n"
        f"تم تفعيل الاشتراك بنجاح 🍯\n"
        f"لوحة التحكم: https://app.nahlah.ai"
    )


# ── HTML email builder ──────────────────────────────────────────────────────

def build_nahla_email_invoice_html(ctx: Dict[str, Any]) -> str:
    """
    Full HTML invoice email for the merchant after successful payment.
    Compatible with Resend API (inline styles only).
    """
    gold   = "#f59e0b"
    green  = "#10b981"
    border = "#e2e8f0"
    muted  = "#94a3b8"
    dark   = "#1e293b"

    row_style = f'style="padding:10px 12px;border:1px solid {border};vertical-align:top"'
    alt_style = f'style="padding:10px 12px;border:1px solid {border};vertical-align:top;background:#f8fafc"'

    rows = [
        ("اسم التاجر",  ctx["merchant_name"]),
        ("اسم المتجر",  ctx["store_name"]),
        ("رقم التاجر",  f"#{ctx['tenant_id']}"),
        ("الباقة",       ctx["plan_name"]),
        ("المدة",        ctx["billing_period"]),
        ("المبلغ",       f"<strong>{ctx['amount_sar']:,} ريال سعودي</strong>"),
        ("رقم العملية",  ctx["invoice_id"]),
        ("تاريخ الدفع", ctx["paid_at"]),
        ("الاشتراك حتى", ctx["ends_at"]),
        ("الحالة",       f'<span style="color:{green};font-weight:bold">✅ مدفوعة</span>'),
    ]

    table_rows = ""
    for i, (label, value) in enumerate(rows):
        td = alt_style if i % 2 == 0 else row_style
        table_rows += (
            f"<tr>"
            f"<td {td} width='40%'>{label}</td>"
            f"<td {td}>{value}</td>"
            f"</tr>\n"
        )

    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;padding:28px 24px;color:{dark}">

  <!-- Header -->
  <div style="text-align:center;margin-bottom:24px">
    <h2 style="color:{gold};margin:0 0 4px">🍯 نحلة AI</h2>
    <p style="margin:0;color:{muted};font-size:13px">منصة المبيعات الذكية للمتاجر العربية</p>
  </div>

  <!-- Status banner -->
  <div style="background:{green};border-radius:10px;padding:14px 20px;text-align:center;margin-bottom:24px">
    <p style="margin:0;color:#fff;font-size:17px;font-weight:bold">✅ تم استلام دفعتك بنجاح</p>
  </div>

  <!-- Greeting -->
  <p style="margin:0 0 16px">
    مرحباً <strong>{ctx['merchant_name']}</strong>،<br>
    شكراً لاشتراكك في نحلة AI. فيما يلي تفاصيل فاتورتك:
  </p>

  <!-- Invoice table -->
  <table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:14px">
    {table_rows}
  </table>

  <!-- CTA -->
  <div style="text-align:center;margin-bottom:24px">
    <a href="https://app.nahlah.ai/billing"
       style="display:inline-block;background:{gold};color:#fff;padding:12px 32px;
              border-radius:8px;text-decoration:none;font-weight:bold;font-size:15px">
      إدارة اشتراكي
    </a>
  </div>

  <!-- Note -->
  <p style="color:{muted};font-size:12px;text-align:center;margin:0 0 4px">
    تم تفعيل اشتراكك وهو يعمل الآن لصالح متجرك 🍯
  </p>
  <p style="color:{muted};font-size:12px;text-align:center;margin:0">
    للدعم: <a href="mailto:support@nahlah.ai" style="color:{gold}">support@nahlah.ai</a>
  </p>

  <hr style="border:none;border-top:1px solid {border};margin:20px 0">
  <p style="color:{muted};font-size:11px;text-align:center;margin:0">
    مدعوم بواسطة نحلة AI ·
    <a href="https://nahlah.ai" style="color:{gold}">nahlah.ai</a>
  </p>
</div>"""
