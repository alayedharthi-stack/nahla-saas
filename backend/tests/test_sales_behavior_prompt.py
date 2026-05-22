"""
tests/test_sales_behavior_prompt.py
───────────────────────────────────
Regression tests for the Salesperson Behavior layer added to
``backend/modules/ai/prompts/high_priority_layer.py``.

The layer exists to fix a real production gap (May 2026 #9): even with
the existing "no full catalog dump" rule under ``BASELINE_STYLE_RULES``,
the bot kept producing brochure-shaped replies when the customer asked
literally about "الأنواع وأسعارها". The new ``BASELINE_SALES_BEHAVIOR_RULES``
+ ``SALES_BEHAVIOR_EXAMPLES`` block enforces:

* progressive disclosure (names → price → image → link)
* numeric density caps (≤2 prices, ≤3 product names, ≤1 CTA per msg)
* ask-before-price when the customer didn't specify a size
* intent → response-shape map
* adaptive verbosity (first reply is always the shortest)

These tests do NOT call an LLM — they verify the *rendered* prompt
contains the rules and examples, so a future refactor cannot silently
drop them. They are intentionally tolerant of phrasing tweaks: we look
for keyword anchors, not exact strings, so editing the wording for
tone doesn't require updating every assertion.
"""
from __future__ import annotations

import re

import pytest

from modules.ai.prompts.high_priority_layer import (
    BASELINE_SALES_BEHAVIOR_RULES,
    SALES_BEHAVIOR_EXAMPLES,
    build_high_priority_block,
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _render(settings=None) -> str:
    """Render the high-priority block with the given (or empty) settings."""
    return build_high_priority_block(settings or {}, store_name="اختبار")


# ── 1. Rules tuple shape ────────────────────────────────────────────────


def test_sales_behavior_rules_are_non_empty_strings():
    """The behavior layer must ship at least 6 distinct rules; each
    rule must be a non-trivial Arabic string. A future PR that
    accidentally clears the tuple would silently strip the new layer
    from the prompt — this test pins the floor."""
    assert isinstance(BASELINE_SALES_BEHAVIOR_RULES, tuple)
    assert len(BASELINE_SALES_BEHAVIOR_RULES) >= 6, (
        f"expected >=6 sales-behavior rules, got {len(BASELINE_SALES_BEHAVIOR_RULES)}"
    )
    seen: set[str] = set()
    for r in BASELINE_SALES_BEHAVIOR_RULES:
        assert isinstance(r, str)
        assert len(r) >= 40, f"rule too short to be meaningful: {r!r}"
        assert r not in seen, f"duplicate rule: {r!r}"
        seen.add(r)


def test_sales_behavior_examples_are_well_formed():
    """Examples must be (customer, bad, good, lesson) tuples with
    distinct ``bad`` and ``good`` replies — otherwise the in-context
    contrast that teaches the LLM the difference is broken."""
    assert isinstance(SALES_BEHAVIOR_EXAMPLES, tuple)
    assert len(SALES_BEHAVIOR_EXAMPLES) >= 3, (
        "expected at least 3 good/bad examples"
    )
    for idx, ex in enumerate(SALES_BEHAVIOR_EXAMPLES):
        assert len(ex) == 4, f"example #{idx} must be a 4-tuple"
        customer, bad, good, lesson = ex
        assert customer.strip(), f"example #{idx} customer message empty"
        assert bad.strip(),     f"example #{idx} bad reply empty"
        assert good.strip(),    f"example #{idx} good reply empty"
        assert lesson.strip(),  f"example #{idx} lesson empty"
        assert bad != good, (
            f"example #{idx} has identical bad/good — the contrast is "
            "the whole point of the example"
        )
        # The good reply must be strictly shorter than the bad one for
        # examples that demonstrate density / brevity (which is all three
        # of the seeded examples). If we add a "compare" example later
        # where the good reply happens to be longer, this assertion will
        # need to relax — for now we pin the invariant that holds.
        assert len(good) < len(bad), (
            f"example #{idx} good reply is not shorter than bad — "
            "this defeats the progressive-disclosure lesson"
        )


# ── 2. Rendered block contains the new layer ────────────────────────────


def test_rendered_block_contains_a1_section_header():
    """The new section must be rendered with a clearly labelled
    ``[A1] SALESPERSON BEHAVIOR`` header so the LLM can attend to it
    distinctly from the generic STYLE rules."""
    block = _render()
    assert "[A1] SALESPERSON BEHAVIOR" in block
    assert "البيع التدريجي" in block
    # Must appear AFTER the STYLE header and BEFORE the POLICY header —
    # the precedence order matters for how the LLM weighs the rules.
    a_pos  = block.index("[A] STYLE")
    a1_pos = block.index("[A1] SALESPERSON BEHAVIOR")
    b_pos  = block.index("[B] POLICY")
    assert a_pos < a1_pos < b_pos, (
        "section order must be [A] → [A1] → [B], "
        f"got positions {a_pos}, {a1_pos}, {b_pos}"
    )


def test_rendered_block_contains_every_sales_behavior_rule():
    """Every rule in the tuple must end up in the rendered prompt.
    A render bug that quietly skipped half the rules would let
    production replies regress without a unit test catching it."""
    block = _render()
    for r in BASELINE_SALES_BEHAVIOR_RULES:
        # Compare on the first 30 chars — long rules can wrap inside
        # the bullet renderer but the prefix is stable.
        head = r[:30]
        assert head in block, f"rule not rendered: {head!r}…"


def test_rendered_block_contains_every_example():
    """Each good/bad example must appear in the rendered prompt with
    both ❌ and ✅ markers — those are the visual anchors the LLM
    keys on when learning the contrast."""
    block = _render()
    assert "أمثلة تعليمية" in block
    for customer, _bad, _good, _lesson in SALES_BEHAVIOR_EXAMPLES:
        assert f"عميل: «{customer}»" in block, (
            f"example customer line missing: {customer!r}"
        )
    # ❌ and ✅ must appear at least once per example.
    assert block.count("❌") >= len(SALES_BEHAVIOR_EXAMPLES)
    assert block.count("✅") >= len(SALES_BEHAVIOR_EXAMPLES)


# ── 3. Key invariants the rules must carry ──────────────────────────────


def test_rules_carry_explicit_density_caps():
    """The numeric density caps are the only part of the layer the
    LLM can self-audit against — they must be present verbatim
    (or close to it). Without them, "don't dump" stays subjective."""
    block = _render()
    # ≤2 prices per message (the literal "سعرين" anchor)
    assert "سعرين" in block, "missing ≤2 prices/message cap"
    # ≤3 named products per message
    assert re.search(r"ثلاث(ة)?\s+أسماء|3\s+أسماء", block), (
        "missing ≤3 product names/message cap"
    )
    # ≤1 clickable link per message
    assert "رابط واحد" in block, "missing ≤1 link/message cap"
    # The cap section must be labelled as "صلب/صلبة/لا تتجاوزها" so the
    # LLM treats it as a hard rule, not a suggestion.
    assert re.search(r"صلب\w*|لا تتجاوزها", block), (
        "density caps must be labelled as hard limits"
    )


def test_rules_pin_ask_before_variant_for_unknown_price():
    """The merchant chose "ask the customer for the variant FIRST"
    (size / model / color / version — depending on category) over
    "default to the middle size" — verify the rule is present so a
    future edit can't silently flip it back to a hard default.

    The assertions deliberately accept BOTH the honey-shaped wording
    ("حجم") and the generic wording ("متغيّر") so the rule can stay
    category-agnostic without breaking when reworded for a non-honey
    catalog.
    """
    block = _render()
    # The honey size example («أي حجم تحب؟») must remain present as
    # ONE worked example — but the rule itself must also list other
    # variant axes (مقاس / لون / موديل) so the LLM doesn't think the
    # layer is honey-only.
    assert "أي حجم" in block, "honey-shaped variant question missing"
    assert re.search(r"مقاس|لون|موديل|سعة|إصدار", block), (
        "rule must list non-size variant axes so it generalises beyond honey"
    )
    # "Ask first" directive — accept either phrasing.
    assert re.search(r"اسأل[هـ]?\s+أولًا|بدون أن يحدّد", block), (
        "ask-before-price directive missing"
    )
    # Anti-default directive — accept either the original "حجم
    # افتراضي" wording or the generic "متغيّر افتراضي" form.
    assert re.search(r"(متغيّر|حجم)\s+افتراضي", block), (
        "anti-default-variant directive missing"
    )


def test_rules_pin_progressive_disclosure_ladder():
    """The 4-rung ladder (names → price → image → link) is the
    canonical sequence. A regression that dropped or re-ordered the
    rungs would let the LLM jump straight to "here's the link" on
    the first turn.

    We scope the search to the [A1] section only because the keywords
    (سعر / رابط / صورة) also appear in [A] STYLE and [B] POLICY —
    a global ``find()`` would lock onto the first occurrence in the
    whole block and report a spurious order failure.
    """
    block = _render()
    # Slice the [A1] section out of the full rendered block.
    a1_start = block.index("[A1] SALESPERSON BEHAVIOR")
    b_start  = block.index("[B] POLICY")
    a1_block = block[a1_start:b_start]

    assert "Disclosure Ladder" in a1_block or "ترتيب الإفصاح" in a1_block
    # Locate the ladder rule explicitly so the ordering check runs on a
    # single rule, not on the whole [A1] section (which mentions the
    # keywords again inside the intent-shape map and the examples).
    ladder_anchor = "Disclosure Ladder" if "Disclosure Ladder" in a1_block else "ترتيب الإفصاح"
    ladder_start = a1_block.index(ladder_anchor)
    # The rule ends at the next bullet point — every rule in this layer
    # is rendered as "• <text>" so the next "• " after the anchor marks
    # the end of the ladder rule.
    next_bullet = a1_block.find("• ", ladder_start + 1)
    ladder_text = a1_block[ladder_start: next_bullet if next_bullet > 0 else len(a1_block)]

    rungs = ["الأسماء", "سعر", "صورة", "رابط"]
    positions = [ladder_text.find(r) for r in rungs]
    assert all(p >= 0 for p in positions), (
        f"missing ladder rung — positions: {dict(zip(rungs, positions))}; "
        f"ladder_text={ladder_text!r}"
    )
    # Names must come before price, price before image, image before
    # link — the order is the rule.
    assert positions == sorted(positions), (
        "disclosure ladder rungs are out of order: "
        f"{dict(zip(rungs, positions))}"
    )


def test_rules_pin_intent_to_shape_map():
    """The intent → shape map covers the six common WhatsApp questions
    that previously produced dump-shaped replies. Verify each anchor
    is present so we don't lose one in a future copy-edit."""
    block = _render()
    anchors = (
        "وش الأنواع",      # catalog ask
        "كم سعر",          # price ask
        "صورة",            # image ask
        "رابط",            # link ask
        "الفرق بين",       # comparison ask
    )
    for a in anchors:
        assert a in block, f"intent-shape map anchor missing: {a!r}"


def test_rules_carry_adaptive_verbosity_directive():
    """First reply about a product must be capped at 3 lines so the
    bot can't open the conversation with a full pitch."""
    block = _render()
    assert "verbosity" in block.lower() or "متكيّفة" in block
    assert "3 أسطر" in block, "first-reply length cap missing"


def test_screenshot_example_is_present_verbatim():
    """Pin the exact "أنواع العسل وأسعاره" example from the screenshot
    that motivated this whole layer. If a future PR removes it, this
    test fails loudly so we don't lose the original training case."""
    block = _render()
    assert "أنواع العسل وأسعاره" in block, (
        "the founding example of this layer must stay in the prompt"
    )
    # The good reply must NOT contain any price digit — that's the
    # whole point of example #1.
    # Find the first example block and check.
    a1_start = block.index("[A1] SALESPERSON BEHAVIOR")
    b_start  = block.index("[B] POLICY")
    a1_block = block[a1_start:b_start]
    # The good reply for example #1 should not contain "126", "193",
    # "387", "79", "139", or "249" — those are the dumped prices in
    # the bad reply.
    # Locate the ✅ block immediately following the screenshot line.
    # We just check the good reply for example #1 by slicing on its
    # known bracket header.
    ex1_pos = a1_block.index("«أنواع العسل وأسعاره»")
    ex2_pos = a1_block.index("«الفرق بين الطلح والسمر»")
    ex1_region = a1_block[ex1_pos:ex2_pos]
    good_marker = ex1_region.index("✅")
    good_region = ex1_region[good_marker:]
    for forbidden_price in ("126", "193", "387", "79", "139", "249"):
        assert forbidden_price not in good_region, (
            f"good reply of example #1 should not contain price {forbidden_price} "
            "— that's exactly the dump pattern we're teaching against"
        )


# ── 4. Backward compatibility with existing baseline rules ──────────────


def test_existing_baseline_rules_still_present():
    """The new layer must be ADDED, not replace existing rules. The
    relational-frame directive, identity discipline, and per-field
    precedence rules are load-bearing — quick sanity check that
    they're still rendered alongside the new behavior layer."""
    block = _render()
    assert "relational_frame" in block, "stance directive dropped"
    assert "identity_already_introduced" in block, "identity rule dropped"
    assert "أولوية مصادر البيانات per-field" in block, (
        "source-of-truth precedence rule dropped"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
