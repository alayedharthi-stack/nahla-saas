"""
brain/compose/templates.py
───────────────────────────
Arabic reply templates for every deterministic action.

Templates use Python .format() style placeholders.
All templates are written in Gulf-dialect Arabic appropriate for a
professional Saudi e-commerce assistant.

State → template binding (single source of truth)
────────────────────────────────────────────────
Each template here is bound to ONE decision branch in the Composer
(`compose/responder.py`). Adding a template is forbidden unless the
DecisionEngine actually produces an action that maps to it — this keeps
the count minimal and prevents the "templates sprout, LLM and templates
fight" pattern that triggered the simplification request.

  Stage discovery (greeted=False) → greeting
  Stage discovery/exploring       → product_results, narrow_choices,
                                    no_products, faq_*, web_search_summary,
                                    addon_recommendations, coupon_offer
  Stage deciding                  → coupon_offer, narrow_choices, clarify
  Stage ordering                  → collect_order_details,
                                    order_intent_captured, draft_order_created
  Stage checkout                  → payment_link
  Stage support                   → handoff
  Any stage                       → order_status (track), clarify

generic_fallback() exists ONLY as the last-resort safety net inside
compose() and `_legacy_llm_compose`. It must never be selected by the
DecisionEngine directly.

Rules:
  - Every template MUST be complete and polite.
  - No placeholders that can render as blank (use .get() with defaults).
  - Emoji are intentionally minimal — one or two per message max.
  - Templates do not greet or self-introduce mid-conversation; the
    greeting template is the only one that says "أهلاً، أنا مساعد ...".

Variant system
──────────────
Six high-frequency templates (greeting, product_results, narrow_choices,
no_products, handoff, generic_fallback) accept an optional `variant: int`
parameter (0, 1, or 2).  The Composer passes `len(ctx.history) % 3` so
wording rotates naturally across turns without randomness or stored state.
Checkout-flow templates (collect_order_details, draft_order_created, etc.)
are data-driven and always unique — they need no variants.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── Greeting ─────────────────────────────────────────────────────────────────
#
# Two persona modes, both wired through the same `greeting()` entry point:
#
#   * Named persona (assistant_name set, e.g. "نحلة" — the default in
#     core.tenant.DEFAULT_AI): the bot introduces itself by name —
#     "أنا نحلة، مساعدة متجرك". Feels human, matches the merchant's
#     branding choice, and is what the dashboard onboarding configures.
#   * Generic (assistant_name empty): falls back to the original
#     "مساعد {store_name}" phrasing. Kept so an explicit empty
#     setting still produces a polite greeting.

_GREETING_NAMED_VARIANTS = [
    # variant 0
    lambda persona, store: (
        f"أهلاً! أنا {persona} 🤖 مساعدتك من *{store}*.\n"
        "أقدر أساعدك في:\n"
        "• البحث عن منتج معيّن أو معرفة الأسعار\n"
        "• إنشاء طلب مباشرة من هنا\n"
        "• متابعة الشحن والاستفسارات\n\n"
        "وش تحتاج اليوم؟"
    ),
    # variant 1
    lambda persona, store: (
        f"مرحباً بك في *{store}*! 👋\n"
        f"أنا {persona}، أقدر أساعدك في:\n"
        "• اقتراح المنتج المناسب لك\n"
        "• إنشاء طلبك مباشرة\n"
        "• الإجابة عن الشحن والدفع\n\n"
        "بماذا أخدمك؟"
    ),
    # variant 2
    lambda persona, store: (
        f"أهلاً وسهلاً! 🌟 معك *{persona}* من *{store}*.\n"
        "قولي وش تحتاج — منتج، سعر، طلب، أو أي استفسار — وأنا هنا."
    ),
]

_GREETING_GENERIC_VARIANTS = [
    # variant 0
    lambda store: (
        f"أهلاً! أنا مساعد {store} الذكي 🤖\n"
        "هنا أساعدك في أي شي تحتاجه:\n"
        "• استفسارات عن المنتجات والأسعار\n"
        "• إنشاء طلب مباشرة من هنا\n"
        "• متابعة الشحن\n\n"
        "كيف أقدر أساعدك اليوم؟"
    ),
    # variant 1
    lambda store: (
        f"مرحباً بك في {store}! 👋\n"
        "أنا المساعد الذكي وأقدر أساعدك في:\n"
        "• البحث عن المنتج المناسب\n"
        "• إنشاء طلبك مباشرة\n"
        "• الاستفسار عن الشحن والدفع\n\n"
        "بماذا أخدمك؟"
    ),
    # variant 2
    lambda store: (
        f"أهلاً وسهلاً! 🌟 معك مساعد {store}.\n"
        "قولي وش تحتاج — منتج، سعر، طلب، أو أي استفسار — وأنا هنا."
    ),
]


def greeting(
    store_name: str = "",
    assistant_name: str = "",
    variant: int = 0,
    **_: Any,
) -> str:
    store = store_name or "متجرنا"
    persona = (assistant_name or "").strip()
    v = variant % 3
    if persona:
        return _GREETING_NAMED_VARIANTS[v](persona, store)
    return _GREETING_GENERIC_VARIANTS[v](store)


# Re-greeting templates intentionally OMIT the persona name and any
# "أنا مساعدتك / مستشارة المتجر" framing — once the customer has been
# greeted, repeating the identity ("معك نحلة من المتجر") makes the
# conversation feel robotic. The merchant flagged this in production
# ("الذكاء يعيد تعريف نفسه بشكل متكرر"). After ``assistant_identity_
# introduced=True`` only the explicit identity FAQ (faq_identity) is
# allowed to re-state who the bot is.
_REGREET_VARIANTS = [
    "ياهلا 🌷 وش أقدر أخدمك فيه؟",
    "حياك الله 💛 تحت أمرك.",
    "أهلاً 🌷 قول وش تحتاج وأكمل معك.",
]


def re_greeting(
    store_name: str = "",
    assistant_name: str = "",
    variant: int = 0,
    **_: Any,
) -> str:
    """Short, warm re-greeting for explicit "السلام عليكم" / "هلا" /
    "مرحبا" arriving AFTER the customer has already been greeted.

    Three rules baked in:
      1. NEVER mention the assistant's name (e.g. "نحلة") — that's a
         re-introduction the customer didn't ask for.
      2. NEVER mention "ذكاء اصطناعي / مساعدة / مستشارة" — those
         belong to the identity FAQ which the brain routes to only
         when the customer EXPLICITLY asks (``INTENT_WHO_ARE_YOU``).
      3. Stay one line so WhatsApp keeps the texture of a normal chat.

    Parameters are kept for backwards-compat with the responder call
    site (``T.re_greeting(store_name=..., assistant_name=...)``) but
    are no longer interpolated — we deliberately drop them here to
    enforce the discipline at the template boundary, not just in the
    LLM prompt.
    """
    del store_name, assistant_name  # intentionally unused — see docstring
    return _REGREET_VARIANTS[variant % 3]


# ── Product search ────────────────────────────────────────────────────────────

_PRODUCT_RESULTS_INTROS = [
    # variant 0
    lambda count, query: (
        (f"وجدت {count} منتج" if count else "إليك المنتجات المتاحة")
        + (f' مناسب لـ "{query}"' if query else "")
        + ":"
    ),
    # variant 1
    lambda count, query: (
        "إليك" + (f' أبرز خيارات "{query}":' if query else " ما يتوفر حالياً:")
    ),
    # variant 2
    lambda count, query: (
        "هذه المنتجات المتوفرة" + (f' بحثاً عن "{query}":' if query else ":")
    ),
]

_PRODUCT_RESULTS_CLOSINGS = [
    "هل تودّ معرفة تفاصيل أكثر عن أي منتج، أو تريد الطلب مباشرة؟",
    "اختر منتجاً وأساعدك بالتفاصيل أو نبدأ الطلب مباشرة.",
    "أرسل رقم أو اسم المنتج اللي يهمك وأكمل معك.",
]


def product_results(
    product_lines: str,
    query: str = "",
    count: int = 0,
    variant: int = 0,
    **_: Any,
) -> str:
    v = variant % 3
    intro   = _PRODUCT_RESULTS_INTROS[v](count, query)
    closing = _PRODUCT_RESULTS_CLOSINGS[v]
    return f"{intro}\n\n{product_lines}\n\n{closing}"


_NO_PRODUCTS_VARIANTS = [
    # variant 0
    "عذراً، لم أتمكن من العثور على منتجات متاحة في المتجر حالياً.\n"
    "سيتواصل معك فريق المتجر قريباً للمساعدة. 🙏",
    # variant 1
    "ما وجدت منتجات متوفرة في الوقت الحالي.\n"
    "جرّب البحث بكلمة أخرى أو تواصل مع المتجر مباشرة.",
    # variant 2
    "لا توجد منتجات مزامنة الآن. 🙏\n"
    "حاول مرة أخرى لاحقاً أو أخبرنا وش تبحث عنه بشكل أدق.",
]


def no_products(variant: int = 0, **_: Any) -> str:
    return _NO_PRODUCTS_VARIANTS[variant % 3]


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
    ref_part  = f"رقم الطلب: *{reference}*" if reference else ""
    total_str = f"المبلغ: *{total:.2f} {currency}*" if total else ""

    header = f"✅ تم إنشاء طلبك بنجاح!"
    details_lines = [f"المنتج: *{title}*"]
    if ref_part:
        details_lines.append(ref_part)
    if total_str:
        details_lines.append(total_str)
    details = "\n".join(details_lines)

    if checkout_url:
        return (
            f"{header}\n\n"
            f"{details}\n\n"
            f"💳 *ادفع الآن:*\n{checkout_url}"
        )
    # No payment URL — order exists but payment link unavailable
    return (
        f"{header}\n\n"
        f"{details}\n\n"
        "سيتواصل معك فريق المتجر لإتمام الدفع. 🙏"
    )


def collect_order_details(
    product: Dict[str, Any],
    question: str = "",
    missing_fields: List[str] | None = None,
    is_first_ask: bool = True,
    **_: Any,
) -> str:
    title = product.get("title", "المنتج المحدد")
    ask = question or "أرسل لي البيانات الناقصة لإكمال الطلب."
    if is_first_ask:
        # First time asking: show the intro + the specific question
        lead = f"ممتاز، سأجهز طلب *{title}* لك."
        if missing_fields:
            lead += " بقيت عليّ بعض التفاصيل فقط."
        return f"{lead}\n{ask}"
    # Subsequent turns: just ask the specific question without the repeated intro
    return ask


def ask_product_options(
    product: Dict[str, Any],
    missing_option_groups: List[Dict[str, Any]] | None = None,
    selected_options: Dict[str, Any] | None = None,
    **_: Any,
) -> str:
    """Ask the customer to pick missing option groups with smart UX.

    Adapts the message style to how many groups remain:
      - 1 group remaining (e.g. only colour left): short, focused prompt
      - multiple groups remaining: lists all groups at once
      - all options selected by caller: confirmation summary only

    Already-selected options are summarised so the customer knows their
    earlier picks were recorded.
    """
    title = product.get("title", "المنتج المحدد")
    groups = list(missing_option_groups or [])

    # Build confirmation summary of already-picked options.
    picked_lines: List[str] = []
    for sel in (selected_options or {}).values():
        if not isinstance(sel, dict):
            continue
        gname = (sel.get("option_name") or "").strip()
        vname = (sel.get("value_name") or "").strip()
        if gname and vname:
            picked_lines.append(f"• {gname}: {vname}")

    # ── All options already collected → confirmation only ─────────────────
    if not groups:
        if picked_lines:
            return (
                f"تمام، اخترت لك *{title}*:\n"
                + "\n".join(picked_lines)
                + "\n✅"
            )
        return f"تمام، سأجهز طلب *{title}*."

    lines: List[str] = []

    # ── Single group remaining → focused short prompt ─────────────────────
    if len(groups) == 1:
        g = groups[0]
        gname = (g.get("name") or "").strip() or "الخيار"
        values = g.get("values") or []
        if picked_lines:
            lines.append(f"تمام 👍")
            lines.extend(picked_lines)
            lines.append("")
            lines.append(f"باقي *{gname}* فقط:")
        else:
            lines.append(f"تمام 👍 اختر *{gname}* المناسب:")
        for idx, val in enumerate(values, 1):
            vname = (val.get("name") or "").strip()
            if vname:
                lines.append(f"{idx}. {vname}")
        return "\n".join(lines)

    # ── Multiple groups remaining → list all at once ──────────────────────
    lines.append(f"تمام، *{title}* متوفر بعدة خيارات.")
    if picked_lines:
        lines.append("اختياراتك حتى الآن:")
        lines.extend(picked_lines)

    group_names = [(g.get("name") or "").strip() or "الخيار" for g in groups]
    if len(group_names) == 2:
        header = f"اختر {group_names[0]} و{group_names[1]}:"
    else:
        header = "اختر " + "، ".join(group_names[:-1]) + f" و{group_names[-1]}:"
    lines.append("")
    lines.append(header)

    name_examples: List[str] = []
    numeric_example: List[str] = []
    for g in groups:
        gname = (g.get("name") or "").strip() or "الخيار"
        values = g.get("values") or []
        lines.append("")
        lines.append(f"*{gname}:*")
        for idx, val in enumerate(values, 1):
            vname = (val.get("name") or "").strip()
            if vname:
                lines.append(f"{idx}. {vname}")
        first_named = next(
            ((v.get("name") or "").strip() for v in values if (v.get("name") or "").strip()),
            "",
        )
        if first_named:
            name_examples.append(first_named)
        if values:
            numeric_example.append("1")

    lines.append("")
    if name_examples and numeric_example:
        lines.append(
            f"يمكنك الرد بالاسم مثل: *{' '.join(name_examples)}* "
            f"أو بالأرقام: *{' '.join(numeric_example)}*"
        )
    else:
        lines.append("(يمكنك الرد بالاسم أو رقم الخيار)")

    return "\n".join(lines)


_PREDICTION_SOURCE_LABELS = {
    "last_customer_choice": "اختيارك السابق",
    "top_variant":          "الأكثر طلباً",
    "stock_heavy":          "المتوفر حالياً",
}


def confirm_predicted_options(
    product: Dict[str, Any],
    predicted_options: Dict[str, Any] | None = None,
    selected_options: Dict[str, Any] | None = None,
    prediction_source: str = "",
    **_: Any,
) -> str:
    """Present predicted options to the customer for confirmation.

    Shows already-selected options, then predicted ones with a source
    label, and asks the customer to confirm or change.
    """
    title = product.get("title", "المنتج المحدد")
    source_label = _PREDICTION_SOURCE_LABELS.get(prediction_source, "")

    lines: List[str] = [f"تمام، اخترت لك *{title}*:"]

    # Already-selected options (customer picked these explicitly)
    for sel in (selected_options or {}).values():
        if not isinstance(sel, dict):
            continue
        gname = (sel.get("option_name") or "").strip()
        vname = (sel.get("value_name") or "").strip()
        if gname and vname:
            lines.append(f"• {gname}: {vname}")

    # Predicted options
    for psel in (predicted_options or {}).values():
        if not isinstance(psel, dict):
            continue
        gname = (psel.get("option_name") or "").strip()
        vname = (psel.get("value_name") or "").strip()
        if gname and vname:
            tag = f" ({source_label})" if source_label else ""
            lines.append(f"• {gname}: *{vname}*{tag}")

    lines.append("")
    lines.append("نكمّل عليه؟ أو تبغى تغيّره؟")

    return "\n".join(lines)


def salla_retry_message(product: Dict[str, Any], code: str = "", **_: Any) -> str:
    """Soft retry message — never blame "خطأ تقني"; just say we're trying again."""
    title = product.get("title", "المنتج المحدد")
    code_ref = f"الرمز *{code}*" if code else "بياناتك"
    return (
        f"وصلني {code_ref} ✅\n"
        f"جارٍ إنشاء طلب *{title}* — لحظة من فضلك. 🔄\n"
        "أرسل أي رسالة وسأتابع معك الطلب."
    )


def address_stashed_pre_product(
    short_code: str = "",
    google_maps_url: str = "",
    city: str = "",
    **_: Any,
) -> str:
    """Customer dropped address info before picking a product. Confirm we
    saved it so they don't repeat themselves, and ask them to choose a
    product. The order flow consumes the stash on the next turn, so the
    bot will NOT ask for the address again."""
    bits: List[str] = []
    if short_code:
        bits.append(f"الرمز الوطني *{short_code}*")
    if google_maps_url:
        bits.append("موقع Google Maps")
    if city:
        bits.append(f"المدينة *{city}*")
    saved = " و ".join(bits) if bits else "بيانات العنوان"
    return (
        f"وصلني {saved} ✅ — محفوظ ولن أعيد سؤالك عنه.\n\n"
        "قبل ما نكمّل، اختر المنتج اللي تبغاه من القائمة (أرسل رقمه أو اسمه)."
    )


def product_unsyncable(product: Dict[str, Any], **_: Any) -> str:
    """Shown when the product the customer picked is not available on the
    store (wrong / stale identifier, deleted, not synced). Asks the customer
    to choose another item instead of attempting a doomed order."""
    title = product.get("title", "هذا المنتج")
    return (
        f"للأسف *{title}* غير متوفر حالياً أو لم يتم مزامنته مع المتجر بعد 😔\n"
        "يمكنك البحث عن منتج آخر أو قول \"أكثر مبيعاً\" وأعرض لك ما هو متاح.\n"
        "إذا استمرت المشكلة فقد يحتاج المتجر إلى مزامنة المنتجات من لوحة التحكم."
    )


def product_unavailable_alternatives(
    rejected_title: str = "",
    alternatives: Optional[List[Dict[str, Any]]] = None,
    **_: Any,
) -> str:
    """Smart fallback when a customer picks a product that turned out to
    be out-of-stock / not orderable. Shows alternatives when available."""
    name = rejected_title or "المنتج اللي اخترته"
    header = f"للأسف *{name}* غير متوفر حالياً 😔"

    if alternatives:
        lines = [header, "لكن عندنا خيارات قريبة:\n"]
        for i, p in enumerate(alternatives, 1):
            price_str = f" — {p['price']} ريال" if p.get("price") else ""
            lines.append(f"{i}. {p.get('title', '؟')}{price_str}")
        lines.append("\nأرسل رقم المنتج اللي يعجبك 👆")
        return "\n".join(lines)

    return f"{header}\nاكتب اسم منتج ثاني أو قول \"أكثر مبيعاً\" وأعرض لك المتاح."


def salla_escalate_message(product: Dict[str, Any], **_: Any) -> str:
    """Sent after 2+ consecutive Salla creation failures — escalate to human."""
    title = product.get("title", "المنتج المحدد")
    return (
        f"بياناتك لطلب *{title}* محفوظة لدينا بالكامل ✅\n\n"
        "سيتواصل معك فريق المتجر خلال دقائق لإتمام الطلب يدوياً. "
        "شكراً لصبرك! 🤝"
    )


def order_intent_captured(product: Dict[str, Any], **_: Any) -> str:
    title = product.get("title", "المنتج المحدد")
    return (
        f"تم تسجيل طلبك لـ *{title}* ✅\n"
        "واجهنا مشكلة تقنية في إنشاء الطلب تلقائياً الآن.\n"
        "يُرجى المحاولة مرة أخرى، أو تواصل مع المتجر مباشرة لإتمام الطلب. 🙏"
    )


# ── Payment link ──────────────────────────────────────────────────────────────

def payment_link(checkout_url: str = "", **_: Any) -> str:
    if checkout_url:
        return (
            f"💳 *رابط الدفع لطلبك:*\n{checkout_url}\n\n"
            "يمكنك إتمام الدفع بشكل آمن من خلال الرابط أعلاه. 🔒"
        )
    return "لا يوجد رابط دفع نشط حالياً. هل تريد إنشاء طلب جديد؟"


# ── Order tracking ────────────────────────────────────────────────────────────

def order_status(
    reference: str = "",
    status: str = "",
    status_label_ar: str = "",
    total: float = 0,
    currency: str = "SAR",
    item_titles: list | None = None,
    **_: Any,
) -> str:
    ref_part = f"رقم الطلب {reference}" if reference else "آخر طلب"
    label = status_label_ar or status or "—"
    lines = [
        f"حالة {ref_part}: *{label}*",
        f"الإجمالي: {total:.2f} {currency}",
    ]
    if item_titles:
        items_str = " | ".join(item_titles)
        lines.append(f"المنتجات: {items_str}")
    return "\n".join(lines)


def no_orders(**_: Any) -> str:
    return "لم أجد أي طلبات مسجّلة لرقمك. هل تريد إنشاء طلب جديد؟"


# ── FAQ ───────────────────────────────────────────────────────────────────────

def faq_identity(
    store_name: str = "",
    assistant_name: str = "",
    **_: Any,
) -> str:
    """Reply for "من أنت؟" / "أنت بوت؟" / "هل أنت ذكاء اصطناعي؟" / "مين معي؟".

    Kept deliberately short (one or two sentences, no bullet list)
    per merchant UX spec:

      > "نعم 🌷 أنا نظام ذكي بالذكاء الاصطناعي يساعد في خدمة العملاء
      >  والطلبات."

    Long identity replies ("أنا نحلة، مساعدتك الذكية، أقدر أساعدك في
    المنتجات والأسعار والطلبات والشحن…") made the conversation feel
    robotic; the merchant flagged this in production. The template
    now obeys WhatsApp shape — short, natural, one emoji max.
    """
    store = store_name or "متجرنا"
    persona = (assistant_name or "").strip()
    if persona:
        return f"نعم 🌷 أنا *{persona}*، مساعدة {store} الذكية. تحت أمرك."
    return f"نعم 🌷 أنا مساعد {store} الذكي. تحت أمرك."


def faq_store_info(
    store_name: str = "",
    store_url: str = "",
    store_description: str = "",
    **_: Any,
) -> str:
    # Direct CTA: short header line then the URL alone so the WhatsApp
    # CTA-button normaliser in the webhook lifts it into "افتح المتجر".
    # We deliberately DROP the trailing "أرسل اسم المنتج" follow-up
    # the merchant flagged as bad UX — when the customer asked for the
    # store link they want the link, not a sales nudge.
    name = store_name or "متجرنا"
    if store_url:
        return f"هذا {name} 🌷\n{store_url}"
    if store_description:
        return f"هذا {name} 🌷\n{store_description}"
    return f"هذا {name} 🌷"


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


def addon_recommendations(products: List[Dict[str, Any]], **_: Any) -> str:
    if not products:
        return generic_fallback()
    lines = ["قد يناسبك أيضاً مع هذا المنتج:\n"]
    for idx, product in enumerate(products[:3], 1):
        title = str(product.get("title") or f"منتج {idx}")
        price = product.get("price")
        if price:
            lines.append(f"{idx}. *{title}* — {price} ريال")
        else:
            lines.append(f"{idx}. *{title}*")
    lines.append("\nإذا رغبت أضيفه لك مع الطلب أو أشرح لك الفرق بين الخيارات.")
    return "\n".join(lines)


def web_search_summary(summary: str = "", citations: List[str] | None = None, **_: Any) -> str:
    text = summary.strip() or "وجدت بعض المعلومات العامة من مصادر خارجية لكن أحتاج سؤالك بشكل أدق."
    refs = [url for url in (citations or [])[:3] if url]
    if refs:
        text += "\n\nالمصادر:\n" + "\n".join(f"- {url}" for url in refs)
    return text


# ── Handoff ───────────────────────────────────────────────────────────────────

# IMPORTANT: never start a variant with phrases that sound like an
# order confirmation ("وصل طلبك" / "تم استلام طلبك"). Customers read
# them literally and assume their PURCHASE arrived, even when no
# order, draft, checkout or payment link exists. Variant 1 used to
# read "وصل طلبك! سأعيد توجيهك..." and was the single biggest source
# of "I never bought anything, why am I being told my order arrived?"
# complaints — keep this list neutral.
_HANDOFF_VARIANTS = [
    # variant 0 — neutral acknowledgement + next-step
    "بالتأكيد، سأنبّه فريق المتجر للتواصل معك. 🙏\n"
    "سيرد عليك أحد أعضاء الفريق في أقرب وقت ممكن.",
    # variant 1 — explicit "I received your request to talk to a person"
    "وصلتني رغبتك بالتحدث مع موظف. 🤝\n"
    "سأبلّغ الفريق الآن وسيتواصل معك بأسرع وقت.",
    # variant 2 — short, polite, no order-confirmation phrasing
    "حسناً، سأطلب من الفريق التواصل معك مباشرة. 🙏\n"
    "شكراً لصبرك — لن يتأخر الرد.",
]


def handoff(variant: int = 0, **_: Any) -> str:
    return _HANDOFF_VARIANTS[variant % 3]


# ── Fallback ──────────────────────────────────────────────────────────────────

def clarify(question: str = "", **_: Any) -> str:
    q = question or "ما الذي تبحث عنه بالضبط؟"
    return q


_NARROW_CHOICES_HEADERS = [
    "وجدت عدة خيارات تناسبك، أيها يثير اهتمامك أكثر؟",
    "عندي عدة منتجات قد تعجبك — أيها يناسبك؟",
    "لقيت أكثر من خيار، اختر اللي يهمك:",
]
_NARROW_CHOICES_CLOSINGS = [
    "أخبرني برقم الخيار أو اسم المنتج لأساعدك أكثر.",
    "أرسل رقم المنتج أو اكتب اسمه وأكمل معك.",
    "رقم الخيار أو اسمه — وأنا هنا.",
]


def narrow_choices(products: List[Dict[str, Any]], variant: int = 0, **_: Any) -> str:
    """Show a numbered product list.

    CRITICAL: the index shown here (1, 2, 3 …) MUST match the index stored
    in last_search_candidates. Never slice this list differently from the
    candidates stored in state — that mismatch is the root cause of the
    "listed then immediately rejected" bug (e.g. customer sees "1. بنطلون"
    but system rejects "بلوزة" because candidates were stored in a different
    order or were truncated).
    """
    if not products:
        return generic_fallback()
    v = variant % 3
    lines = [_NARROW_CHOICES_HEADERS[v] + "\n"]
    for i, p in enumerate(products, 1):   # show ALL — no [:3] truncation
        price_str = f"{p['price']} ريال" if p.get("price") else ""
        line = f"{i}. *{p['title']}*"
        if price_str:
            line += f" — {price_str}"
        lines.append(line)
    lines.append("\n" + _NARROW_CHOICES_CLOSINGS[v])
    return "\n".join(lines)


_GENERIC_FALLBACK_VARIANTS = [
    # variant 0
    "شكراً على تواصلك! هل يمكنك توضيح طلبك أكثر؟\n"
    "يمكنني مساعدتك في البحث عن المنتجات أو إنشاء طلب.",
    # variant 1
    "وصلني سؤالك! لو تقدر تعطيني تفاصيل أكثر سأكون أقدر على المساعدة.\n"
    "ابحث عن منتج أو أبدأ لك طلباً مباشرة.",
    # variant 2
    "أنا هنا لمساعدتك. 🤝\n"
    "هل تبحث عن منتج معين أو تريد مساعدة في طلب سابق؟",
]


def generic_fallback(variant: int = 0, **_: Any) -> str:
    return _GENERIC_FALLBACK_VARIANTS[variant % 3]
