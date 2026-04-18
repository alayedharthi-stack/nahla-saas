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

from typing import Any, Dict

# ── Greeting ─────────────────────────────────────────────────────────────────

def greeting(store_name: str = "", **_: Any) -> str:
    name = store_name or "متجرنا"
    return (
        f"أهلاً بك في {name}! 👋\n"
        "كيف أقدر أساعدك اليوم؟\n"
        "يمكنني مساعدتك في:\n"
        "• البحث عن المنتجات\n"
        "• معرفة الأسعار والتوفر\n"
        "• إنشاء طلب بشكل مباشر"
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


def order_intent_captured(product: Dict[str, Any], **_: Any) -> str:
    title = product.get("title", "المنتج المحدد")
    return (
        f"رائع! سجّلت اهتمامك بـ *{title}*.\n"
        "سيتواصل معك فريق المتجر لإتمام الطلب قريباً. 🤝"
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
