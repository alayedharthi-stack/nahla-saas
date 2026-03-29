"""
PromptBuilder
─────────────
Assembles the Claude system prompt from the customer memory context.

Sections (in order):
  1. Role & persona
  2. Store info
  3. Customer profile (personalisation layer)
  4. Available products (affinity-sorted)
  5. Active coupons
  6. Conversation history summary
  7. Policy constraints
  8. Response instructions
"""

from __future__ import annotations

from typing import Any, Dict, List


def build_system_prompt(ctx: Dict[str, Any]) -> str:
    sections: List[str] = []

    # ── 1. Role ───────────────────────────────────────────────────────────────
    store_name = ctx.get("store_name", "our store")
    sections.append(
        f'You are an intelligent WhatsApp sales agent for "{store_name}".\n'
        "Your goal is to help customers discover products, answer questions, "
        "and guide them toward completing a purchase.\n"
        "You have access to the customer's full history with this store — use it "
        "to make every response feel personal and relevant."
    )

    # ── 2. Store ──────────────────────────────────────────────────────────────
    addr = ctx.get("store_address", "")
    store_section = f"STORE: {store_name}"
    if addr:
        store_section += f"\nAddress: {addr}"
    sections.append(store_section)

    # ── 3. Customer profile ───────────────────────────────────────────────────
    customer_name     = ctx.get("customer_name", "")
    segment           = ctx.get("segment", "new")
    is_returning      = ctx.get("is_returning", False)
    total_orders      = ctx.get("total_orders", 0)
    total_spend       = ctx.get("total_spend_sar", 0.0)
    avg_order         = ctx.get("avg_order_value_sar", 0.0)
    last_order        = ctx.get("last_order_at", None)
    lang              = ctx.get("preferred_language", "ar")
    comm_style        = ctx.get("communication_style", "neutral")
    sensitivity_score = ctx.get("price_sensitivity_score", 0.5)
    rec_discount      = ctx.get("recommended_discount_pct", 0)
    sentiment         = ctx.get("sentiment", "neutral")

    profile_lines = ["CUSTOMER PROFILE:"]
    if customer_name:
        profile_lines.append(f"  Name: {customer_name}")
    profile_lines.append(f"  Status: {'Returning customer' if is_returning else 'New customer'} (segment: {segment})")
    if total_orders > 0:
        profile_lines.append(f"  Orders: {total_orders} total | Avg order: SAR {avg_order:.0f} | Total spent: SAR {total_spend:.0f}")
    if last_order:
        profile_lines.append(f"  Last order: {last_order[:10]}")
    profile_lines.append(f"  Preferred language: {lang} | Communication style: {comm_style}")
    profile_lines.append(f"  Price sensitivity: {_sensitivity_label(sensitivity_score)}")
    if rec_discount > 0:
        profile_lines.append(f"  Effective discount to offer if needed: {rec_discount}%")
    if sentiment in ("negative", "frustrated"):
        profile_lines.append(f"  ⚠️  Customer sentiment: {sentiment} — be extra empathetic and solution-focused.")
    sections.append("\n".join(profile_lines))

    # ── 4. Customer preferences ───────────────────────────────────────────────
    pref_cats     = ctx.get("preferred_categories", [])
    pref_brands   = ctx.get("preferred_brands", [])
    price_range   = ctx.get("price_range", {})
    pref_payment  = ctx.get("preferred_payment", None)
    pref_delivery = ctx.get("preferred_delivery", None)
    if any([pref_cats, pref_brands, price_range.get("max"), pref_payment, pref_delivery]):
        pref_lines = ["CUSTOMER PREFERENCES:"]
        if pref_cats:
            pref_lines.append(f"  Interested in: {', '.join(pref_cats)}")
        if pref_brands:
            pref_lines.append(f"  Preferred brands: {', '.join(pref_brands)}")
        if price_range.get("max"):
            pref_lines.append(f"  Price range: SAR {price_range.get('min', 0):.0f} – {price_range['max']:.0f}")
        if pref_payment:
            pref_lines.append(f"  Preferred payment: {pref_payment}")
        if pref_delivery:
            pref_lines.append(f"  Preferred delivery: {pref_delivery}")
        sections.append("\n".join(pref_lines))

    # ── 5. Recent orders ──────────────────────────────────────────────────────
    recent_orders = ctx.get("recent_orders", [])
    if recent_orders:
        order_lines = ["RECENT ORDERS:"]
        for o in recent_orders[:3]:
            items_str = ""
            if o.get("items"):
                titles = [i.get("title", "?") for i in (o["items"] if isinstance(o["items"], list) else [])]
                items_str = f" ({', '.join(titles[:3])})"
            order_lines.append(f"  • {o.get('status', '?')} — SAR {o.get('total', '?')}{items_str}")
        sections.append("\n".join(order_lines))

    # ── 6. Conversation history ───────────────────────────────────────────────
    history = ctx.get("history_summary", "")
    last_intent = ctx.get("last_intent", None)
    past_topics = ctx.get("past_topics", [])
    escalations = ctx.get("escalation_count", 0)
    if history or past_topics or last_intent:
        hist_lines = ["INTERACTION HISTORY:"]
        if history:
            hist_lines.append(f"  Summary: {history[:500]}")
        if past_topics:
            hist_lines.append(f"  Topics discussed before: {', '.join(past_topics[-10:])}")
        if last_intent:
            hist_lines.append(f"  Last known intent: {last_intent}")
        if escalations > 0:
            hist_lines.append(f"  ⚠️  Escalated {escalations} time(s) before — avoid repeating the same unhelpful answers.")
        sections.append("\n".join(hist_lines))

    # ── 7. Products (affinity-sorted) ─────────────────────────────────────────
    products = ctx.get("products", [])
    if products:
        prod_lines = ["AVAILABLE PRODUCTS (★ = high affinity for this customer):"]
        for p in products[:25]:
            star = " ★" if p.get("affinity_score", 0) > 0.4 else ""
            prod_lines.append(f"  [{p['id']}] {p['title']} | SAR {p['price_sar']}{star}")
        sections.append("\n".join(prod_lines))
    else:
        sections.append("AVAILABLE PRODUCTS:\n  (No products loaded yet.)")

    # ── 8. Coupons ────────────────────────────────────────────────────────────
    coupons = ctx.get("coupons", [])
    if coupons:
        coupon_lines = ["ACTIVE COUPONS:"]
        for c in coupons:
            coupon_lines.append(
                f"  {c['code']} — {c['discount_type']} {c['discount_value']}"
                + (f" ({c['description']})" if c.get("description") else "")
            )
        sections.append("\n".join(coupon_lines))

    # ── 9. Policy constraints ─────────────────────────────────────────────────
    blocked = ctx.get("blocked_categories", [])
    coupon_policy = ctx.get("coupon_policy", {})
    policy_lines = ["POLICY RULES (enforce strictly):"]
    if blocked:
        policy_lines.append(f"  • Never discuss or recommend: {', '.join(blocked)}")
    max_disc = coupon_policy.get("max_discount")
    if max_disc:
        policy_lines.append(f"  • Maximum discount you may offer: {max_disc}%")
    min_disc = coupon_policy.get("min_discount")
    if min_disc:
        policy_lines.append(f"  • Minimum discount threshold: {min_disc}%")
    policy_lines.append("  • Never fabricate product names, prices, or availability.")
    policy_lines.append("  • If a customer escalates, acknowledge and offer to connect with a human agent.")
    sections.append("\n".join(policy_lines))

    # ── 10. Response instructions ──────────────────────────────────────────────
    lang_instruction = (
        "Reply in Arabic." if lang == "ar"
        else "Reply in English." if lang == "en"
        else "Reply in the same language the customer uses."
    )
    style_map = {
        "formal": "Use formal, respectful language.",
        "casual": "Keep it conversational and friendly.",
        "brief": "Be very concise — short messages only.",
        "neutral": "Be warm, professional, and clear.",
    }
    style_instruction = style_map.get(comm_style, style_map["neutral"])

    sections.append(
        "RESPONSE INSTRUCTIONS:\n"
        f"  • {lang_instruction}\n"
        f"  • {style_instruction}\n"
        "  • WhatsApp messages must be short — avoid long paragraphs.\n"
        "  • When suggesting a product, use its product ID in brackets, e.g. [42].\n"
        "  • When proposing a coupon, state the code clearly.\n"
        "  • Do not add any AI disclosure or branding footer — that is added separately.\n"
        "  • Use action tools to propose products, coupons, bundles, or draft orders."
    )

    return "\n\n".join(sections)


def _sensitivity_label(score: float) -> str:
    if score < 0.25:
        return "low (buys at full price)"
    if score < 0.5:
        return "moderate"
    if score < 0.75:
        return "high (responds well to discounts)"
    return "very high (rarely buys without discount)"
