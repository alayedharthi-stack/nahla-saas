"""
brain/compose/templates.py
───────────────────────────
Arabic reply templates for every deterministic action.

Templates use Python .format() style placeholders.
All templates are written in Gulf-dialect Arabic appropriate for a
professional Saudi e-commerce assistant.

Rules:
  - Every template MUST be complete and polite.
  - No placeholders that can render as blank (use .get() with defaults).
  - Emoji are intentionally minimal — one or two per message max.
"""
from __future__ import annotations

from typing import Any, Dict, List

# ── Greeting ─────────────────────────────────────────────────────────────────

def greeting(store_name: str = "", **_: Any) -> str:
    name = store_name or "متجرنا"
    return (
        f"أهلاً! أنا مساعد {name} الذكي 🤖\n"
        "هنا أساعدك في أي شي تحتاجه:\n"
        "• استفسارات عن المنتجات والأسعار\n"
        "• إنشاء طلب مباشرة من هنا\n"
        "• متابعة الشحن\n\n"
        "كيف أقدر أساعدك اليوم؟"
    )


# ── Product search ────────────────────────────────────────────────────────────

def product_results(product_lines: str, query: str = "", count: int = 0, **_: Any) -> str:
    intro = f"وجدت {count} منتج" if count else "إليك المنتجات المتاحة"
    if query:
        intro += f" مناسب لـ \"{query}\""
    return (
        f"{intro}:\n\n"
        f"{product_lines}\n\n"
        "هل تودّ معرفة تفاصيل أكثر عن أي منتج، أو تريد الطلب مباشرة؟"
    )


def no_products(**_: Any) -> str:
    return (
        "عذراً، لم أتمكن من العثور على منتجات متاحة في المتجر حالياً.\n"
        "سيتواصل معك فريق المتجر قريباً للمساعدة. 🙏"
    )


# ── Draft order ───────────────────────────────────────────────────────────────

def draft_order_created(
    product: Dict[str, Any],
    reference: str = "",
    checkout_url: str = "",
    total: float = 0.0,
    currency: str = "SAR",
    **_: Any,
) -> str:
    title     = product.get("title", "المنتج المحدد")
    ref_part  = f" (رقم الطلب: {reference})" if reference else ""
    total_str = f"\nالإجمالي: {total:.2f} {currency}" if total else ""
    url_part  = f"\n\nرابط الدفع:\n{checkout_url}" if checkout_url else ""
    return (
        f"تم إنشاء طلبك لـ *{title}*{ref_part}! 🎉"
        f"{total_str}"
        f"{url_part}\n\n"
        "هل تريد تأكيد الطلب أو تعديله؟"
    )


def collect_order_details(
    product: Dict[str, Any],
    question: str = "",
    missing_fields: List[str] | None = None,
    **_: Any,
) -> str:
    title = product.get("title", "المنتج المحدد")
    lead = f"ممتاز، سأجهز طلب *{title}* لك."
    if missing_fields:
        lead += " بقيت عليّ بعض التفاصيل فقط."
    ask = question or "أرسل لي البيانات الناقصة لإكمال الطلب."
    return f"{lead}\n{ask}"


def order_intent_captured(product: Dict[str, Any], **_: Any) -> str:
    title = product.get("title", "المنتج المحدد")
    return (
        f"رائع! سجّلت اهتمامك بـ *{title}*.\n"
        "تم حفظ بيانات الطلب الحالية، وإذا لزم الأمر سيتابعك فريق المتجر لإكماله. 🤝"
    )


# ── Payment link ──────────────────────────────────────────────────────────────

def payment_link(checkout_url: str = "", **_: Any) -> str:
    if checkout_url:
        return f"هذا رابط الدفع لطلبك:\n{checkout_url}\n\nيمكنك إتمام الدفع بشكل آمن من خلاله. 🔒"
    return "لا يوجد رابط دفع نشط حالياً. هل تريد إنشاء طلب جديد؟"


# ── Order tracking ────────────────────────────────────────────────────────────

def order_status(reference: str = "", status: str = "", total: float = 0, currency: str = "SAR", **_: Any) -> str:
    ref_part = f"رقم الطلب {reference}" if reference else "آخر طلب"
    return (
        f"حالة {ref_part}: *{status}*\n"
        f"الإجمالي: {total:.2f} {currency}"
    )


def no_orders(**_: Any) -> str:
    return "لم أجد أي طلبات مسجّلة لرقمك. هل تريد إنشاء طلب جديد؟"


# ── FAQ ───────────────────────────────────────────────────────────────────────

def faq_identity(store_name: str = "", **_: Any) -> str:
    name = store_name or "متجرنا"
    return (
        f"أنا مساعد {name} الذكي.\n"
        "أساعدك في المنتجات والأسعار والطلبات والشحن بشكل مباشر.\n"
        "وش أقدر أخدمك فيه اليوم؟"
    )


def faq_store_info(
    store_name: str = "",
    store_url: str = "",
    store_description: str = "",
    **_: Any,
) -> str:
    lines = [f"هذا {store_name or 'متجرنا'}."]
    if store_description:
        lines.append(store_description)
    if store_url:
        lines.append(f"رابط المتجر: {store_url}")
    lines.append("إذا تحب أساعدك في منتج معيّن أرسل اسمه أو وصفه.")
    return "\n".join(lines)


def faq_shipping(
    shipping_policy: str = "",
    shipping_methods: List[str] | None = None,
    shipping_notes: str = "",
    support_hours: str = "",
    **_: Any,
) -> str:
    methods = shipping_methods or []
    lines = ["بالنسبة للشحن:"]
    if shipping_policy:
        lines.append(f"- سياسة الشحن: {shipping_policy}")
    if methods:
        lines.append(f"- طرق الشحن: {', '.join(methods)}")
    if shipping_notes:
        lines.append(f"- ملاحظات التوصيل: {shipping_notes}")
    if support_hours:
        lines.append(f"- ساعات الدعم: {support_hours}")
    if len(lines) == 1:
        lines.append("أقدر أتحقق لك من خيارات الشحن المتاحة بعد اختيار المنتج المناسب.")
    else:
        lines.append("إذا اخترت المنتج أقدر أكمل معك للطلب مباشرة.")
    return "\n".join(lines)


def faq_owner_contact(
    contact_phone: str = "",
    contact_email: str = "",
    store_url: str = "",
    **_: Any,
) -> str:
    lines = ["هذه وسائل التواصل المتاحة:"]
    if contact_phone:
        lines.append(f"- واتساب / هاتف: {contact_phone}")
    if contact_email:
        lines.append(f"- البريد: {contact_email}")
    if store_url:
        lines.append(f"- رابط المتجر: {store_url}")
    if len(lines) == 1:
        lines.append("حالياً لا توجد وسيلة تواصل مباشرة محفوظة، لكن يمكنني مساعدتك هنا أو تحويل طلبك للفريق.")
    return "\n".join(lines)


# ── Coupon ────────────────────────────────────────────────────────────────────

def coupon_offer(coupon_block: str = "", product: Dict[str, Any] | None = None, **_: Any) -> str:
    title = (product or {}).get("title", "")
    intro = f"يسعدني تقديم عرض خاص لك على *{title}*:\n\n" if title else "إليك عرض خاص:\n\n"
    return f"{intro}{coupon_block}"


# ── Handoff ───────────────────────────────────────────────────────────────────

def handoff(**_: Any) -> str:
    return (
        "بالتأكيد! سأحوّلك الآن لأحد أعضاء فريق المتجر.\n"
        "سيتواصل معك في أقرب وقت ممكن. 🙏"
    )


# ── Fallback ──────────────────────────────────────────────────────────────────

def clarify(question: str = "", **_: Any) -> str:
    q = question or "ما الذي تبحث عنه بالضبط؟"
    return q


def narrow_choices(products: List[Dict[str, Any]], **_: Any) -> str:
    if not products:
        return generic_fallback()
    lines = ["وجدت عدة خيارات تناسبك، أيها يثير اهتمامك أكثر؟\n"]
    for i, p in enumerate(products[:3], 1):
        price_str = f"{p['price']} ريال" if p.get("price") else ""
        line = f"{i}. *{p['title']}*"
        if price_str:
            line += f" — {price_str}"
        lines.append(line)
    lines.append("\nأخبرني برقم الخيار أو اسم المنتج لأساعدك أكثر.")
    return "\n".join(lines)


def generic_fallback(**_: Any) -> str:
    return (
        "شكراً على تواصلك! هل يمكنك توضيح طلبك أكثر؟\n"
        "يمكنني مساعدتك في البحث عن المنتجات أو إنشاء طلب."
    )
