"""
tests/test_merchant_kb_scope.py
────────────────────────────────
Regression tests for the May 2026 #20 merchant-mode KB scoping fix.

Nahla is built on top of آل عايد للعسل البلدي and merchants are encouraged
to keep a short Nahla-platform brief inside their KB so platform-curious
customers / peers / merchants-of-the-owner can be answered. That info is
**intentional** (per the platform owner) and must NOT be deleted from
storage. But the same KB is consumed by two audiences:

    * merchant customers (honey buyers) → must NOT see Nahla SaaS plans
    * platform inquirers                → must see them

These tests lock in the *one-sided filter*:

    * ``extract_merchant_kb_excerpt`` strips paragraphs that ONLY talk
      about Nahla (brand name, SaaS, plan tiers) and KEEPS merchant
      paragraphs and mixed paragraphs.
    * ``build_tenant_overlay_split`` wires the filter into the ``facts``
      bucket so both the merchant brain prompt builder AND the legacy
      merchant fallback path are protected by a single layer.

The platform-intent path (``extract_platform_kb_excerpt``) reads the raw
``manual_knowledge_base`` directly from ``merchant_context.ai_settings``
— it is unaffected by the new filter (a separate test below pins this).
"""
from __future__ import annotations

import pytest

from modules.ai.brain.knowledge_platform_slice import (
    extract_merchant_kb_excerpt,
    extract_platform_kb_excerpt,
    PLATFORM_SUBSCRIPTION,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. extract_merchant_kb_excerpt — drops PURE platform paragraphs only.
# ─────────────────────────────────────────────────────────────────────────────


def test_drops_pure_nahla_brand_paragraph() -> None:
    kb = (
        "كيلو السدر بسعر 200 ريال.\n\n"
        "نحلة منصة سعودية تحوّل واتساب الأعمال إلى موظف مبيعات ذكي 24/7."
    )
    filtered, dropped = extract_merchant_kb_excerpt(kb)
    assert dropped == 1
    assert "كيلو السدر" in filtered
    assert "نحلة منصة" not in filtered


def test_drops_plan_tier_paragraph() -> None:
    kb = (
        "نحضّر العسل من مناحلنا الخاصة في الجنوب.\n\n"
        "باقات الاشتراك: Starter 899 ريال شهرياً، Pro 1499 ريال، "
        "Business 2999 ريال. تجربة مجانية 14 يوم."
    )
    filtered, dropped = extract_merchant_kb_excerpt(kb)
    assert dropped == 1
    assert "مناحلنا" in filtered
    assert "Starter" not in filtered
    assert "899" not in filtered


def test_keeps_merchant_paragraph_that_mentions_bee_imagery() -> None:
    # Merchant copy that uses "نحلتنا" as their own bee imagery —
    # mixed signal (brand-ish word + catalog token) must survive.
    kb = "نحلتنا الذهبية تنتج عسل سدر من جبال السراة، الكيلو 320 ريال."
    filtered, dropped = extract_merchant_kb_excerpt(kb)
    assert dropped == 0
    assert "نحلتنا" in filtered
    assert "السراة" in filtered


def test_keeps_pure_merchant_paragraph() -> None:
    kb = (
        "نقدم خدمة التوصيل لجميع مدن المملكة خلال 2-4 أيام عمل.\n"
        "الشحن مجاني للطلبات فوق 300 ريال."
    )
    filtered, dropped = extract_merchant_kb_excerpt(kb)
    assert dropped == 0
    assert "التوصيل" in filtered
    assert "الشحن مجاني" in filtered


def test_does_not_drop_paragraph_with_generic_words_only() -> None:
    # "تطبيق" / "خدمه" / "نظام" / "رقم" / "ربط" are NOT in the
    # hard-anchor set on purpose — they appear in normal merchant copy
    # ("رقم التواصل" / "خدمة العملاء" / "ربط الطلب"…).
    kb = (
        "خدمة العملاء متاحة على رقم 920000000 من 9 صباحاً إلى 9 مساءً.\n"
        "تطبيق المتجر يدعم الدفع الإلكتروني."
    )
    filtered, dropped = extract_merchant_kb_excerpt(kb)
    assert dropped == 0
    assert "خدمة العملاء" in filtered
    assert "تطبيق" in filtered


def test_real_world_mixed_kb_keeps_merchant_drops_platform() -> None:
    """The exact shape آل عايد's KB has — merchant honey copy plus a
    short Nahla brief at the bottom. The filter must keep all honey
    paragraphs and drop both Nahla paragraphs."""
    kb = (
        "### عسل السدر الجبلي\n"
        "عسل سدر معتّق من جبال الباحة. الكيلو 350 ريال، نصف الكيلو 200.\n\n"
        "### عسل الطلح\n"
        "عسل طلح غامق ذو نكهة قوية. الكيلو 220 ريال.\n\n"
        "### الشحن\n"
        "نشحن لجميع مدن المملكة عبر سمسا. التوصيل 2-4 أيام عمل.\n\n"
        "### عن منصة نحلة\n"
        "نحلة منصة SaaS سعودية تحوّل واتساب إلى موظف مبيعات ذكي.\n\n"
        "### باقات نحلة\n"
        "Starter 899 ريال شهرياً، Pro 1499، Business 2999. "
        "تجربة مجانية 14 يوم بدون بطاقة."
    )
    filtered, dropped = extract_merchant_kb_excerpt(kb)
    assert dropped == 2, f"expected to drop 2 pure-platform chunks, dropped={dropped}"
    assert "عسل السدر" in filtered
    assert "عسل الطلح" in filtered
    assert "سمسا" in filtered
    assert "Starter" not in filtered
    assert "تجربة مجانية" not in filtered
    assert "SaaS" not in filtered


def test_empty_kb_returns_empty() -> None:
    assert extract_merchant_kb_excerpt("") == ("", 0)
    assert extract_merchant_kb_excerpt("   \n\n  ") == ("", 0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Symmetry — platform-intent path still reads the FULL KB.
# ─────────────────────────────────────────────────────────────────────────────


def test_platform_path_still_sees_nahla_paragraphs() -> None:
    """The platform-intent slicer reads the raw KB (passed in
    explicitly from ``merchant_context.ai_settings.manual_knowledge_base``).
    It is unaffected by the merchant-side filter, so a customer who
    explicitly asks about Nahla still gets the right answer."""
    kb = (
        "### عسل السدر الجبلي\n"
        "الكيلو 350 ريال.\n\n"
        "### باقات نحلة\n"
        "Starter 899، Pro 1499، Business 2999. تجربة 14 يوم مجاناً."
    )
    excerpt = extract_platform_kb_excerpt(
        kb, PLATFORM_SUBSCRIPTION, "كم باقات نحلة؟",
    )
    assert "Starter" in excerpt or "899" in excerpt


# ─────────────────────────────────────────────────────────────────────────────
# 3. tenant_overlay wiring — buckets["facts"] is filtered.
# ─────────────────────────────────────────────────────────────────────────────


def test_tenant_overlay_facts_bucket_filters_platform_chunks() -> None:
    from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split

    kb = (
        "### عسل السدر\n"
        "الكيلو 350 ريال.\n\n"
        "### عن منصة نحلة\n"
        "نحلة منصة SaaS سعودية. Starter 899 ريال، تجربة مجانية 14 يوم."
    )
    buckets = build_tenant_overlay_split({"manual_knowledge_base": kb})
    facts = buckets["facts"]
    assert "عسل السدر" in facts
    # Pure-platform paragraph must be gone from the prompt-bound bucket.
    assert "SaaS" not in facts
    assert "Starter" not in facts
    assert "899" not in facts


def test_tenant_overlay_facts_unchanged_when_no_platform_text() -> None:
    from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split

    kb = (
        "### عسل السدر\n"
        "الكيلو 350 ريال.\n\n"
        "### الشحن\n"
        "2-4 أيام عمل لجميع المدن."
    )
    buckets = build_tenant_overlay_split({"manual_knowledge_base": kb})
    facts = buckets["facts"]
    assert "عسل السدر" in facts
    assert "الشحن" in facts


# ─────────────────────────────────────────────────────────────────────────────
# 4. Persona scope rule — instructs the LLM about WHEN to discuss Nahla.
# ─────────────────────────────────────────────────────────────────────────────


def test_persona_carries_platform_scope_rule() -> None:
    """Defense-in-depth: even if a paragraph slipped through the filter,
    the persona explicitly tells the LLM NOT to introduce Nahla SaaS in
    a product/shipping/price turn."""
    from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt

    text = nahla_persona_system_prompt(store_name="آل عايد")
    assert "نطاق الحديث" in text
    assert "منصّة نحلة" in text
    # Must positively allow the right turns…
    assert "كيف يعمل" in text or "كيف تم بناء" in text
    # …and explicitly forbid the wrong turns.
    assert "العسل" in text and "المنتجات" in text
