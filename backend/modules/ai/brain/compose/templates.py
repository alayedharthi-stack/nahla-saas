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

from .mirror_replies import mirror_reply as _mirror_reply

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

# ARCH-KB-001: phatic greetings only — no self-intro, no capability bullets,
# no help-desk closers. Used only on rollback paths when persona compose is OFF.
_GREETING_NAMED_VARIANTS = [
    lambda persona, store: "يا هلا",
    lambda persona, store: "حياك الله 💛",
    lambda persona, store: f"وعليكم السلام — نورت *{store}*",
]

_GREETING_GENERIC_VARIANTS = [
    lambda store: "يا هلا",
    lambda store: "حياك الله 💛",
    lambda store: f"وعليكم السلام — نورت *{store}*",
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
    "ياهلا",
    "حياك الله 💛",
    "أهلاً",
]


def re_greeting(
    store_name: str = "",
    assistant_name: str = "",
    variant: int = 0,
    **_: Any,
) -> str:
    """Legacy fallback re-greeting (deterministic).

    Happy path (``ESTABLISHED_GREET_PERSONA_COMPOSE_ENABLED`` default ON):
    established pure greetings use ``persona_social`` + ``persona_kind=greeting``
    LLM compose — not this template.

    This function remains for rollback when the env flag is OFF or for
    emergency paths that still emit ``ACTION_GREET`` + ``re_greet=True``.

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


def ask_product_variants(
    product: Dict[str, Any],
    variants: List[Dict[str, Any]] | None = None,
    **_: Any,
) -> str:
    """Ask the customer to pick a sellable variant before we ship a card.

    Used by the responder when the resolver returns a parent with
    ``needs_variant_choice=True`` (Phase 3 of the catalog refactor —
    migration 0064). Different from :func:`ask_product_options`:

      * ``ask_product_options`` walks Salla *option groups* (one
        question per group: size, then colour, then material …) —
        used when a product has multiple option dimensions.
      * ``ask_product_variants`` walks pre-rolled *variant SKUs*
        (each row = a concrete combination already in stock) — used
        when the catalog layer has variant rows ready to ship and we
        just need the customer to pin one.

    The two paths can both fire in a single conversation; the brain's
    decision engine picks the right one based on which artefact the
    resolver loaded.
    """
    title = (product.get("title") or "المنتج المحدد").strip()
    sellable = [
        v for v in (variants or [])
        if v and v.get("in_stock", True) and not v.get("is_default")
    ]
    if not sellable:
        return f"تمام، سأجهز طلب *{title}*."

    lines: List[str] = [
        f"تمام، *{title}* متوفر بعدة خيارات. اختر الأنسب:",
        "",
    ]
    for idx, v in enumerate(sellable, 1):
        label = (
            (v.get("option_summary") or "").strip()
            or (v.get("sku") or "").strip()
            or (v.get("salla_variant_id") or "").strip()
            or f"الخيار {idx}"
        )
        price = (v.get("price") or "").strip()
        if price:
            lines.append(f"{idx}. {label} — {price}")
        else:
            lines.append(f"{idx}. {label}")
    lines.append("")
    lines.append("(اكتب رقم الخيار أو اسمه)")
    return "\n".join(lines)


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
    """Customer dropped address info before picking a product. Confirm
    receipt softly and nudge them to choose a product. The order flow
    consumes the stash on the next turn so the bot will NOT ask for
    the address again — the previous wording ("محفوظ ولن أعيد سؤالك
    عنه") read robotic and made the bot sound like it was negotiating
    with the customer; merchants asked us to drop it.

    Two minor wording cleanups (June 2026):
      * "موقع Google Maps" → "موقعك" — the source URL may be Apple
        Maps / Waze / Google; the label is now provider-agnostic.
      * The "محفوظ ولن أعيد سؤالك عنه" line is removed entirely.
        Confirmation is a single warm sentence.
    """
    bits: List[str] = []
    if short_code:
        bits.append(f"الرمز الوطني *{short_code}*")
    if google_maps_url:
        bits.append("موقعك")
    if city:
        bits.append(f"المدينة *{city}*")
    saved = " و ".join(bits) if bits else "بيانات عنوانك"
    return (
        f"وصلني {saved} 🌷\n\n"
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


def order_creation_in_progress(
    product: Dict[str, Any] | None = None,
    reference: str = "",
    **_: Any,
) -> str:
    title = (product or {}).get("title") or "طلبك"
    if reference:
        return (
            f"طلب *{title}* قيد الإنشاء — رقم الطلب: *{reference}*."
        )
    return f"طلب *{title}* قيد الإنشاء الآن — لحظات ويظهر رقم الطلب."


def order_creation_failed(product: Dict[str, Any] | None = None, **_: Any) -> str:
    title = (product or {}).get("title") or "المنتج المحدد"
    return (
        f"تعذّر إنشاء طلب *{title}* تلقائياً الآن.\n"
        "بياناتك محفوظة — جرّب مرة أخرى أو تواصل مع المتجر لإتمام الطلب."
    )


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


def faq_location(
    store_name: str = "",
    maps_url: str = "",
    **_: Any,
) -> str:
    """Physical-shop / Google-Maps reply.

    Mirrors :func:`faq_store_info` in shape — short header, then the
    URL on its own line so the CTA-button normaliser can lift it
    into a "موقعنا على الخرايط" button. When no maps URL is
    available we DON'T silently substitute the e-commerce
    ``store_url`` (the bug May 2026 #36 fixed); we also DON'T ask
    the customer for "اسم الفرع" — the safety net's KB scan still
    has a chance to inject a URL it finds in a branches section,
    and the awkward "أخبرنا بالفرع" prose left over after the
    injection was the production complaint May 2026 #38 closed.
    The fallback is now a single honest line; the artifact guard
    rewrites it cleanly when the safety net also fails.
    """
    name = store_name or "متجرنا"
    if maps_url:
        return f"موقعنا 📍\n{maps_url}"
    # Honest fallback — no ask-for-branch prose, no "we'll send".
    # Short enough that ``_looks_like_bare_location_intro`` flags
    # it as a generic stub, which lets the location safety net
    # REPLACE rather than append when it later resolves a URL.
    return f"موقع {name} 📍"


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
    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        MSG_CS_NOT_CONFIGURED,
    )

    if contact_phone:
        return f"تقدر تتواصل معنا على: {contact_phone}"
    lines = []
    if contact_email:
        lines.append(f"البريد: {contact_email}")
    if store_url:
        lines.append(f"رابط المتجر: {store_url}")
    if lines:
        return "\n".join(lines)
    return MSG_CS_NOT_CONFIGURED


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


# ── Out-of-scope deflection (May 2026 #3 — hard-only) ────────────────────────
#
# Three revisions later, the final design is simple:
#
#   * The decision engine only emits ACTION_OUT_OF_SCOPE for the HARD
#     tier (clearly off-domain — electricity bills, real estate,
#     programming, legal cases, financial investing, drug dosages,
#     war). All other INTENT_GENERAL messages — including honey-
#     adjacent KB questions, casual chitchat, weather, safe factoids
#     — fall through to ACTION_LLM_REPLY where the merchant brain
#     composes a natural reply with full KB + catalogue + sales
#     context.
#
#   * This file therefore only needs ONE template family: a polite,
#     short, honey-redirecting line for the HARD case. No clown
#     emoji, no rotating jokes, no "ههه" filler. Calm.
#
#   * ``chitchat_reply`` / ``safe_fact_dodge`` / ``out_of_scope_reply``
#     dispatcher functions are kept as no-op pass-throughs to
#     ``hard_out_of_scope_reply`` so downstream imports / tests that
#     still call them keep working. They are NOT used by the
#     responder pipeline anymore.
#
# BANNED phrasing (per merchant feedback May 2026):
#   - "هذا خارج نطاق متجرنا"
#   - "أنا هنا — قول وش تحتاج"
#   - "وأكمل معك"
#   - any URL / "see also" / "you can search…"
#
# These wording-bans are enforced by code review; the outbound
# sanitiser handles URL leakage as a defence in depth.

# Hard-tier replies — clearly off-domain topics (electricity, real
# estate, programming, legal cases, financial investing, drug
# dosages, war). Calm, short, no clown emoji, no rotating jokes.
# A single soft redirect to honey/orders. The customer can take it
# from there or move on.
_HARD_OUT_OF_SCOPE_VARIANTS = [
    "أعتذر، هذا خارج تخصصي. لو تحب أساعدك في شي يخص العسل أو الطلب، أنا جاهزة",
    "ما أقدر أساعدك في هذا الموضوع، لكني معك في كل ما يخص العسل والطلبات",
    "هالأمر يحتاج جهة مختصة، أما من ناحية العسل والطلبات فأنا في الخدمة",
]


def hard_out_of_scope_reply(variant: int = 0, **_: Any) -> str:
    """Polite, calm redirect for clearly off-domain topics."""
    return _HARD_OUT_OF_SCOPE_VARIANTS[variant % len(_HARD_OUT_OF_SCOPE_VARIANTS)]


# ── Social / courtesy / religious replies (May 2026 #4, tone pass #2) ─────
#
# Hybrid approach: **small weighted template pools** — not a second LLM call.
# Reasons: zero extra latency/cost, predictable safety (no sales leakage),
# culturally auditable copy. Two rotation indices (``variant`` ×
# ``sub_variant``) widen variety without spamming the same line every hour.
#
# Tone: خليجية دافئة، سطر أو سطرين، إيموجي خفيف عند الحاجة — بدون ضحك مفرط
# وبلا طرح بيعي.

# May 2026 #8 — REMOVED the "وهذا من ذوقك … الله يبيض وجهك" entry.
# Routine "شكرًا" / "تسلم" turns must NOT trigger the heavy reciprocal
# blessing — it felt over-the-top when the customer hadn't praised us
# in kind. The heavy phrase now lives ONLY in
# ``_SOCIAL_STRONG_PRAISE_VARIANTS`` below and is reached only when
# the social classifier matches the explicit ``_STRONG_PRAISE_KEYWORDS``
# triggers (بيض الله وجهك / ما قصرت / كفو / رفعت رأسنا / ...).
_SOCIAL_THANKS_VARIANTS = [
    "وياك يا غالي 🌹\nالله يجزاك خير.",
    "الله يبارك فيك 🌹",
    "مو عليك يا الغالي 🤍\nالله يعافيك.",
    "الله يخليك 🤍\nتشرفنا فيك.",
    "العفو يا الغالي 🌹",
]

# May 2026 #8 — same surgery here. The reciprocal "الله يبيض وجهك مثل
# ما بيضت وجهنا" was firing on any blessing keyword ("الله يسعدك" /
# "الله يحفظك") and landing on customers who were just being polite,
# not praising us. Replaced with neutral warm closures that fit ANY
# blessing turn. Heavy reciprocal stays in the strong-praise pool.
_SOCIAL_BLESSING_VARIANTS = [
    "آمين وإياك 🌹\nالله يسعدك.",
    "الله يكرمك 🤍\nشكراً لذوقك.",
    "الله يعافيك ويسعدك\nأي وقت.",
]

# Strong-praise reciprocal pool — reached ONLY when the social
# classifier matches a trigger from ``_STRONG_PRAISE_KEYWORDS``. This
# is the single place in the codebase where the heavy "الله يبيض
# وجهك" reciprocal is allowed; keeping it isolated makes the
# regression test trivial (see test_strong_praise_phrasing.py).
_SOCIAL_STRONG_PRAISE_VARIANTS = [
    "الله يبيض وجهك مثل ما بيضت وجهنا 🌹\nويحفظك.",
    "تسلم يا الغالي 🌹\nالله يبيض وجهك ويرفع قدرك.",
    "ما قصّرت 🤍\nالله يبيض وجهك ويعطيك العافية.",
    "كفو والله\nالله يبيض وجهك وقدّرك خير.",
]

_SOCIAL_PROPHET_INVOCATION_VARIANTS = [
    "صلى الله عليه وسلم 🌹\nجزاك الله خير.",
    "صلى الله عليه وسلم 🌹\nجزاك الله خير وكتب أجرك.",
    "عليه أفضل الصلاة وأزكى السلام 🤍\nالله يجزاك الخير الجميل.",
    "صلى الله عليه وسلم\nويبارك الله فيك.",
    "اللهم صل وسلم على نبينا محمد 🤍\nوشكراً لذوقك الطيب.",
    "صلى الله عليه وسلم عدد خلق الله 🤍\nوما أحسنت.",
]

_SOCIAL_BASMALA_VARIANTS = [
    "بسم الله يا الغالي 🤍\nتفضل…",
    "بسم الله الرحمن الرحيم 🌹\nوعليك السلام والبركة.",
    "بسم الله\nتفضل…",
]

_SOCIAL_COMPLIMENT_VARIANTS = [
    "تسلم 🤍\nالله يسعدك.",
    "الله يبارك فيك 🌹",
    "ما قصّرت 🤍\nالله يعافيك.",
]

_SOCIAL_GENERAL_COURTESY_VARIANTS = [
    "الله يحييك 🌹",
    "حياك الله 🤍",
    "هلا وسهلا 🌹\nتشرفنا.",
    "أهلًا فيك 🤍",
    "يامرحبا 🌹",
]

_SOCIAL_WARM_ACK_VARIANTS = [
    "هلا وغلا 🌹\nأهلاً فيك.",
    "الله يحييك 🌹",
    "تسلم 🤍",
]

_SOCIAL_EID_GREETING_VARIANTS = [
    "كل عام وأنتم بخير 🤍\nتقبل الله طاعتكم.",
    "عيدكم مبارك 🌹\nالله يبارك فيكم.",
    "تقبل الله منا ومنكم\nعساكم من عواده.",
    "كل عام وأنت بخير 🌹\nالله يجعل أيامكم مباركة.",
]

_SOCIAL_DUA_VARIANTS = [
    "آمين يارب 🤍\nجزاك الله خير.",
    "آمين وإياك 🌹\nتقبل الله.",
    "الله يتقبل 🤍\nبارك الله فيك.",
]

_SOCIAL_CONDOLENCE_VARIANTS = [
    "الله يرحمه ويغفر له 🤍",
    "إنا لله وإنا إليه راجعون",
    "الله يصبركم ويعظم أجركم 🤍",
]

_SOCIAL_REPLIES_BY_CATEGORY: Dict[str, List[str]] = {
    "thanks":             _SOCIAL_THANKS_VARIANTS,
    "blessing":           _SOCIAL_BLESSING_VARIANTS,
    "prophet_invocation": _SOCIAL_PROPHET_INVOCATION_VARIANTS,
    "basmala":            _SOCIAL_BASMALA_VARIANTS,
    "compliment":         _SOCIAL_COMPLIMENT_VARIANTS,
    "general_courtesy":   _SOCIAL_GENERAL_COURTESY_VARIANTS,
    # May 2026 #8 — reserved pool for explicit heavy praise only.
    "strong_praise":      _SOCIAL_STRONG_PRAISE_VARIANTS,
    # May 2026 — non-commerce media safety layer.
    "eid_greeting":       _SOCIAL_EID_GREETING_VARIANTS,
    "dua":                _SOCIAL_DUA_VARIANTS,
    "condolence":         _SOCIAL_CONDOLENCE_VARIANTS,
    "religious_media":    _SOCIAL_DUA_VARIANTS,
    "social_forward":     _SOCIAL_WARM_ACK_VARIANTS,
    "morning_greeting":   _SOCIAL_WARM_ACK_VARIANTS,
    "emotional_personal": _SOCIAL_BLESSING_VARIANTS,
    "informational_only": _SOCIAL_WARM_ACK_VARIANTS,
}


_OCCASION_GATED_SOCIAL_CATEGORIES = frozenset({
    "eid_greeting",
    "dua",
    "religious_media",
})


def social_mirror_fallback_reply(inbound_text: str = "") -> str:
    """Emergency fallback only — exact mirror reciprocal, no template pool.

    Used when ``social_persona_ack`` LLM compose is empty or hard-fails.
    Normal healthy social personality turns must not call this before LLM.
    """
    if not inbound_text:
        return ""
    mirrored = _mirror_reply(inbound_text)
    return mirrored or ""


def social_reply(
    category: str = "general_courtesy",
    variant: int = 0,
    sub_variant: int = 0,
    *,
    inbound_text: str = "",
    **_: Any,
) -> str:
    """Deterministic social reply for occasion/safety categories only (P1-F).

    Normal personality social turns (thanks, blessing, courtesy, warmth)
    route to ``ACTION_LLM_REPLY`` + ``social_persona_ack`` — they must not
    reach this function on the healthy path.

    No mirror layer here; mirror is emergency-only via
    ``social_mirror_fallback_reply()``.
    """
    cat = (category or "").strip().lower() or "general_courtesy"
    if cat in _OCCASION_GATED_SOCIAL_CATEGORIES:
        from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: PLC0415
            inbound_has_occasion_signal,
        )
        if not inbound_has_occasion_signal(inbound_text):
            return ""

    bucket = _SOCIAL_REPLIES_BY_CATEGORY.get(
        (category or "").strip().lower() or "general_courtesy",
        _SOCIAL_GENERAL_COURTESY_VARIANTS,
    )
    if not bucket:
        return ""
    idx = (int(variant) + int(sub_variant) * 7 + len(bucket) * 5) % len(bucket)
    return bucket[idx]


# ── Platform inquiry replies (May 2026 #4) ───────────────────────────────────
#
# Replies for the new ``INTENT_PLATFORM_INQUIRY`` intent. The customer
# is asking about Nahla (the SaaS platform), not the merchant's
# products. We do NOT invent platform facts here (pricing, package
# names, etc.) — the safe-and-honest behaviour is to scope the
# conversation back to the MERCHANT'S CONTEXT and tell the customer
# that platform questions are best handled by Nahla support.
#
# Topic-aware so the reply mentions what was asked, but each variant
# is short and ends with a graceful pivot back to "هل أساعدك في شي
# يخص المتجر؟" — which is honest scoping, not a sales push.

_PLATFORM_GENERIC_VARIANTS = [
    "هذا استفسار يخص منصة نحلة وفريق الدعم. أنا هنا لما يخص هذا المتجر "
    "ومنتجاته — لو عندك سؤال عن منتج أو طلب، تفضل 🌹",
    "هذا سؤال عن منصة نحلة وليس عن المتجر. تواصل مع دعم نحلة لو احتجت "
    "تفاصيل عن المنصة، وأنا في خدمتك لكل ما يخص المتجر.",
]

_PLATFORM_SUBSCRIPTION_VARIANTS = [
    "تفاصيل الاشتراك والباقات تخص منصة نحلة، وفريق نحلة هم الأقدر "
    "على شرحها. أنا متخصصة بمنتجات هذا المتجر فقط 🌹",
    "أسعار وباقات نحلة يجيب عليها فريق دعم نحلة مباشرة. لو في شي "
    "يخص المتجر أو منتج معين، خبرني وأنا معك.",
]

_PLATFORM_INTEGRATION_VARIANTS = [
    "موضوع ربط واتساب الأعمال يحتاج فريق دعم نحلة. أنا هنا لخدمة "
    "عملاء هذا المتجر فقط — لو تحتاج شي يخصه، تفضل 🌹",
    "الربط مع واتساب الأعمال يجاوب عنه فريق نحلة. أما ما يخص المتجر "
    "فأنا في الخدمة.",
]

_PLATFORM_API_VARIANTS = [
    "أسئلة الـ API والـ Webhook تخص فريق نحلة التقني. أما ما يخص "
    "هذا المتجر فأنا معك 🌹",
    "تكامل الـ API يتولاه فريق نحلة. لو احتجت شي يخص منتجات المتجر، "
    "خبرني.",
]

_PLATFORM_AI_VARIANTS = [
    "ميزات الذكاء في منصة نحلة يشرحها فريق دعم نحلة. أنا هنا "
    "لمساعدتك في منتجات وطلبات هذا المتجر 🌹",
    "تفاصيل قدرات الذكاء الاصطناعي في نحلة يجاوب عنها فريق نحلة. "
    "أما ما يخص هذا المتجر فأنا معك.",
]

_PLATFORM_CAMPAIGNS_VARIANTS = [
    "الحملات التسويقية في نحلة يشرحها فريق نحلة. أنا في خدمتك "
    "لمنتجات وطلبات هذا المتجر 🌹",
    "إعداد الحملات يتولاه فريق نحلة، وأنا متخصصة في خدمة عملاء "
    "المتجر فقط.",
]

_PLATFORM_DASHBOARD_VARIANTS = [
    "أسئلة لوحة التحكم تخص فريق نحلة. أنا هنا لخدمتك في كل ما "
    "يخص منتجات وطلبات هذا المتجر 🌹",
]

_PLATFORM_META_VARIANTS = [
    "إعداد الربط مع Meta يتولاه فريق نحلة. أنا في خدمتك لما يخص "
    "هذا المتجر — منتجات أو طلبات 🌹",
    "ربط Meta وبيانات WABA يجيب عنها فريق نحلة. أما ما يخص "
    "المتجر فأنا معك.",
]

_PLATFORM_REPLIES_BY_TOPIC: Dict[str, List[str]] = {
    "subscription":     _PLATFORM_SUBSCRIPTION_VARIANTS,
    "integration":      _PLATFORM_INTEGRATION_VARIANTS,
    "api":              _PLATFORM_API_VARIANTS,
    "ai_capabilities":  _PLATFORM_AI_VARIANTS,
    "campaigns":        _PLATFORM_CAMPAIGNS_VARIANTS,
    "dashboard":        _PLATFORM_DASHBOARD_VARIANTS,
    "meta_connection":  _PLATFORM_META_VARIANTS,
    "general_platform": _PLATFORM_GENERIC_VARIANTS,
}


def platform_reply(topic: str = "general_platform", variant: int = 0, **_: Any) -> str:
    """Pick a short reply that scopes the conversation back to the
    merchant's store WITHOUT inventing platform facts.

    ``topic`` is one of the keys in ``_PLATFORM_REPLIES_BY_TOPIC``.
    Unknown topics fall back to ``general_platform``.
    """
    bucket = _PLATFORM_REPLIES_BY_TOPIC.get(
        (topic or "").strip().lower() or "general_platform",
        _PLATFORM_GENERIC_VARIANTS,
    )
    return bucket[variant % len(bucket)]


# ── Legacy no-op shims ───────────────────────────────────────────────────────
# These three functions are no longer reached by the responder
# pipeline (the engine no longer emits chitchat / safe_fact tiers),
# but downstream tests and a couple of older callers still import
# them. We collapse all three onto ``hard_out_of_scope_reply`` so the
# imports keep working and any stray call site lands on the calm
# hard reply instead of the noisy old playful templates.

def chitchat_reply(topic: str = "", variant: int = 0, **_: Any) -> str:  # noqa: ARG001
    """Deprecated — kept for import compatibility. Returns the calm
    hard-tier redirect."""
    return hard_out_of_scope_reply(variant=variant)


def safe_fact_dodge(variant: int = 0, **_: Any) -> str:
    """Deprecated — kept for import compatibility."""
    return hard_out_of_scope_reply(variant=variant)


def out_of_scope_reply(
    tier: str = "hard",
    topic: str = "",          # noqa: ARG001
    variant: int = 0,
    **_: Any,
) -> str:
    """Deprecated dispatcher — kept for import compatibility. The
    engine now only emits the ``hard`` tier; everything else falls
    through to the merchant brain (ACTION_LLM_REPLY) instead of
    landing here. Any tier value passes through to the calm hard
    redirect.
    """
    return hard_out_of_scope_reply(variant=variant)


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


def handoff_after_hours(**_: Any) -> str:
    """Polite "we received your request, the team will reply during
    work hours" copy. Used by the responder when
    ``PolicyGate._working_hours`` flagged the handoff as off-hours so
    we keep the request registered (HandoffSession + needs_human) but
    don't promise an immediate reply."""
    return (
        "وصلتني رغبتك بالتحدث مع موظف 🤝\n"
        "حالياً خارج أوقات الدوام، وسجّلت طلبك للفريق.\n"
        "سيتواصل معك أحد أعضاء الفريق فور بداية الدوام بإذن الله 🙏"
    )


# ── Fallback ──────────────────────────────────────────────────────────────────

def clarify(question: str = "", **_: Any) -> str:
    q = question or "ما الذي تبحث عنه بالضبط؟"
    return q


_NARROW_CHOICES_HEADERS = [
    "لقيت أكثر من خيار — أي واحد يناسبك؟",
    "عندنا عدة خيارات — اختر اللي يهمك",
    "هذي أقرب الخيارات لطلبك:",
]

_NARROW_FOCUSED_HEADERS: Dict[int, List[str]] = {
    1: [
        "هذا أقرب خيار لطلبك",
        "لقيت منتج واحد يناسب بحثك 👌",
    ],
    2: [
        "عندنا خيارين قد يناسبونك 👌",
        "لقيت خيارين — أي واحد تفضل؟",
    ],
    3: [
        "لقيت 3 خيارات — اختر الأنسب",
        "عندنا 3 خيارات مركّزة 👌",
    ],
}

_NARROW_SHOW_MORE_HINTS = [
    "\n\nتبغى أرسل لك باقي الخيارات؟",
    "\n\nتحب أعرض لك خيارات أكثر؟",
]
_NARROW_CHOICES_CLOSINGS = [
    "اختر رقم الخيار أو اسم المنتج وأكمل معك.",
    "أرسل الرقم أو الاسم اللي يناسبك.",
    "وش الخيار اللي يهمك؟",
]


def narrow_choices(
    products: List[Dict[str, Any]],
    variant: int = 0,
    *,
    show_more_hint: bool = False,
    **_: Any,
) -> str:
    """Show a numbered product list.

    CRITICAL: the index shown here (1, 2, 3 …) MUST match the index stored
    in last_search_candidates. Never slice this list differently from the
    candidates stored in state — that mismatch is the root cause of the
    "listed then immediately rejected" bug (e.g. customer sees "1. بنطلون"
    but system rejects "بلوزة" because candidates were stored in a different
    order or were truncated).

    When ``show_more_hint`` is True, a single-line continuation prompt is
    appended so the customer can ask for more options without a wall of SKUs.
    """
    if not products:
        return generic_fallback()
    v = variant % 3
    count = len(products)
    if count <= 3:
        bucket = _NARROW_FOCUSED_HEADERS.get(count) or _NARROW_FOCUSED_HEADERS[3]
        header = bucket[v % len(bucket)]
    else:
        header = _NARROW_CHOICES_HEADERS[v]
    lines = [header + "\n"]
    for i, p in enumerate(products, 1):
        price_str = f"{p['price']} ريال" if p.get("price") else ""
        line = f"{i}. *{p['title']}*"
        if price_str:
            line += f" — {price_str}"
        lines.append(line)
    closing = _NARROW_CHOICES_CLOSINGS[v]
    out = "\n".join(lines) + "\n\n" + closing
    if show_more_hint:
        out += _NARROW_SHOW_MORE_HINTS[v % len(_NARROW_SHOW_MORE_HINTS)]
    return out


_GENERIC_FALLBACK_VARIANTS = [
    # P1-D-1: retired CS/sales rotation pool — single operational honesty line.
]


def generic_fallback(variant: int = 0, **_: Any) -> str:
    from core.fallback_policy import operational_compose_error_fallback  # noqa: PLC0415

    return operational_compose_error_fallback(variant=variant)
