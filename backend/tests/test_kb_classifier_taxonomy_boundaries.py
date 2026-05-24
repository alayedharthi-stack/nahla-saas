"""
backend/tests/test_kb_classifier_taxonomy_boundaries.py
───────────────────────────────────────────────────────
KB-3 (May 2026 #46) — Tenant 33 reported that text describing the
order-completion / shipping flow ("بعد تأكيد الطلب نطلب العنوان
والجوال ونحدد سمسا أو توصيل") was misclassified by the KB suggestion
classifier as ``assistant_behavior → escalation_rules``. Root cause
was that the Arabic verb "حوّل / تحويل" is shared between two very
different senses:

    1. تحويل لموظف بشري      → escalation (assistant_behavior)
    2. تحويل / توجيه شحنة     → shipping / commerce
    2'. تحويل بنكي             → bank_transfer / commerce

The classifier prompt now carries an explicit anti-confusion rule
(``KB-3 — حدود escalation_rules``) AND a paired contrast set of
few-shot examples so the model uses the surrounding context
(شحن / عنوان / سمسا / Google Maps  vs  موظف / شكوى / غش) instead
of the verb alone.

This test file does NOT call the live LLM — it pins the prompt
content so a future refactor cannot silently drop the rules. It
also exercises the deterministic-fallback path (no API key) for
the four canonical category texts the merchant raised:

    * shipping completion / order continuation
    * escalation
    * generic FAQ
    * product info
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ════════════════════════════════════════════════════════════════════
# Part 1 — Prompt content pins (anti-confusion rule + contrast pair)
# ════════════════════════════════════════════════════════════════════


def test_prompt_carries_kb3_escalation_boundary_rule() -> None:
    """The explicit anti-pattern rule (KB-3) must be present verbatim
    in the system prompt — without it the model has no signal that
    "نحوّل الشحنة" is different from "نحوّل العميل لموظف".
    """
    from modules.ai.knowledge.classifier import PROPOSAL_SCHEMA_NOTE

    # Section header — pins that the rule exists.
    assert "حدود escalation_rules" in PROPOSAL_SCHEMA_NOTE, (
        "KB-3 anti-confusion section header missing from prompt"
    )

    # Negative-list content — texts that must NOT route to escalation.
    for needle in (
        "إكمال الطلب",
        "خرائط Google",
        "العنوان",
        "سمسا",
        "التوصيل المباشر",
        "العنوان الوطني",
        "التحويل البنكي",
    ):
        assert needle in PROPOSAL_SCHEMA_NOTE, (
            f"KB-3 negative-list keyword missing: {needle!r}"
        )

    # The two senses of "حوّل" must be explicitly contrasted.
    assert "تحويل لموظف بشري" in PROPOSAL_SCHEMA_NOTE
    assert "توجيه شحنة" in PROPOSAL_SCHEMA_NOTE


def test_prompt_lists_shipping_alternatives_for_escalation_misroute() -> None:
    """The prompt must enumerate the correct commerce kinds the
    model should pick instead of escalation_rules for shipping/order
    text — otherwise it falls back to ``custom`` or the wrong slot."""
    from modules.ai.knowledge.classifier import PROPOSAL_SCHEMA_NOTE

    for kind in (
        "shipping_zones",
        "shipping_carrier",
        "cold_shipping",
        "bank_transfer",
        "faq",
    ):
        assert kind in PROPOSAL_SCHEMA_NOTE, (
            f"Allowed-alternative kind missing from KB-3 rule: {kind!r}"
        )


def test_prompt_carries_kb3_quick_test_question() -> None:
    """Pins the explicit "quick test" the model should run before
    picking escalation_rules. This is the single highest-leverage
    line in the rule — it forces the model to verify intent before
    committing to the wrong taxonomy."""
    from modules.ai.knowledge.classifier import PROPOSAL_SCHEMA_NOTE

    assert "اختبار سريع قبل اختيار escalation_rules" in PROPOSAL_SCHEMA_NOTE
    assert "ينسحب المساعد ويسلّم الموضوع لموظف" in PROPOSAL_SCHEMA_NOTE


# ════════════════════════════════════════════════════════════════════
# Part 2 — Few-shot example contrast pair (shipping vs escalation)
# ════════════════════════════════════════════════════════════════════


def test_fewshot_pair_includes_shipping_completion_example() -> None:
    """Pin the new shipping-completion example so a future trim
    cannot silently drop it. Without this concrete instance the
    model loses its anchor for "نطلب العنوان والجوال ونحدد سمسا"
    type text."""
    from modules.ai.knowledge.classifier import _FEW_SHOT_EXAMPLES

    shipping_example = next(
        (ex for ex in _FEW_SHOT_EXAMPLES
         if "Google Maps" in (ex.get("input") or "")),
        None,
    )
    assert shipping_example is not None, (
        "KB-3 shipping-completion few-shot example is missing"
    )
    expected_kind = (
        shipping_example["expected"]["proposed_ops"][0]["kind"]
    )
    assert expected_kind == "shipping_zones", (
        f"shipping-completion example must map to shipping_zones, "
        f"got {expected_kind!r}"
    )
    # Critical signal words in the body — these are what the model
    # will use as positive evidence on similar inputs.
    body = shipping_example["expected"]["proposed_ops"][0]["body"]
    for needle in ("الجوال", "العنوان", "Google Maps", "سمسا"):
        assert needle in body, (
            f"shipping-completion example body missing: {needle!r}"
        )


def test_fewshot_pair_includes_escalation_complaint_example() -> None:
    """Pin the contrasting escalation example. It uses the SAME
    verb stem ("حوّله") as the shipping example but unambiguously
    in the handoff sense (شكوى / غش / فلوسي). The pair is what
    teaches the model to disambiguate by context."""
    from modules.ai.knowledge.classifier import _FEW_SHOT_EXAMPLES

    escalation_example = next(
        (ex for ex in _FEW_SHOT_EXAMPLES
         if "غش" in (ex.get("input") or "")
         and "شكوى" in (ex.get("input") or "")),
        None,
    )
    assert escalation_example is not None, (
        "KB-3 escalation contrast few-shot example is missing"
    )
    expected_kind = (
        escalation_example["expected"]["proposed_ops"][0]["kind"]
    )
    assert expected_kind == "escalation_rules"
    rationale = (
        escalation_example["expected"]["proposed_ops"][0]["rationale"]
    )
    # Rationale should explicitly say "this is NOT shipping" so
    # the model internalises the boundary.
    assert "الشحن" in rationale or "التوصيل" in rationale, (
        "Escalation example rationale should disambiguate from "
        "shipping/delivery context"
    )


def test_fewshot_count_minimum_includes_kb3_pair() -> None:
    """KB-3 added two new examples on top of the original five
    (commerce/specific, behavioral/forbidden, commerce/cold-shipping,
    platform-conflict, behavioral/tone). Pin >= 7 so the pair can't
    be removed by accident."""
    from modules.ai.knowledge.classifier import _FEW_SHOT_EXAMPLES

    assert len(_FEW_SHOT_EXAMPLES) >= 7, (
        f"Expected >= 7 few-shot examples after KB-3, "
        f"got {len(_FEW_SHOT_EXAMPLES)}. The shipping/escalation "
        "contrast pair must remain in the registry."
    )


def test_built_prompt_includes_both_kb3_examples() -> None:
    """End-to-end check: when ``_build_system_prompt`` renders the
    full prompt, both new KB-3 examples must appear. This catches
    the case where someone adds them to the registry but breaks the
    rendering helper."""
    from modules.ai.knowledge.classifier import (
        PlatformSignal,
        _build_system_prompt,
    )

    prompt = _build_system_prompt(
        existing_sections=[],
        attached_media=[],
        platform_signal=PlatformSignal(connected=True, platform="salla",
                                       warning="موصولة بسلة"),
        available_kinds=[
            "shipping_zones", "shipping_carrier", "escalation_rules",
            "bank_transfer", "faq", "quick_update",
        ],
    )

    # Shipping-completion example fingerprint.
    assert "نطلب الاسم والجوال والعنوان" in prompt
    assert "سمسا أو التوصيل المباشر" in prompt

    # Escalation contrast example fingerprint.
    assert "غش أو شكوى أو أبي فلوسي" in prompt


# ════════════════════════════════════════════════════════════════════
# Part 3 — Deterministic fallback shape on canonical category texts
# ════════════════════════════════════════════════════════════════════
#
# The classifier degrades to the deterministic fallback when no
# OPENAI_API_KEY is set. We use that path here to assert the shape
# the merchant sees (and that the original text survives intact)
# for the four categories the user named: shipping completion,
# escalation, FAQ, product info. We deliberately don't assert the
# CLASSIFIED kind here — the live LLM is what classifies; what we
# pin is "the user's text is preserved + nothing crashes".


@pytest.fixture(autouse=True)
def _clear_openai_key(monkeypatch: pytest.MonkeyPatch):
    """Force the deterministic-fallback branch by removing the API
    key. Tests that need the live LLM path mock at a higher level."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield


_AVAILABLE_KINDS = [
    "shipping_zones", "shipping_carrier", "cold_shipping",
    "bank_transfer", "cod", "payment_method",
    "escalation_rules", "forbidden_phrases", "response_tone",
    "store_story", "branches", "working_hours",
    "product_usage", "product_benefit", "product_storage",
    "faq", "quick_update", "custom",
]


def _classify(text: str):
    from modules.ai.knowledge.classifier import (
        PlatformSignal,
        classify_quick_update,
    )
    return classify_quick_update(
        raw_text=text,
        attached_media=[],
        existing_sections=[],
        platform_signal=PlatformSignal(
            connected=False, platform=None, warning="",
        ),
        available_kinds=_AVAILABLE_KINDS,
        tenant_id=33,
    )


def test_shipping_completion_text_survives_fallback_intact() -> None:
    """The exact Tenant 33 #46 text — must reach the merchant as
    a quick_update fallback (since no API key) with the body
    preserved verbatim. The TEST does NOT assert the LLM kind; it
    only confirms the pipeline doesn't lose the text or crash."""
    text = (
        "بعد ما يأكد العميل طلبه نطلب الاسم الكامل ورقم الجوال "
        "والعنوان التفصيلي أو رابط Google Maps. "
        "ثم نحدد طريقة الشحن: سمسا أو التوصيل المباشر للعنوان."
    )
    result = _classify(text)
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "no_api_key"
    assert len(result["proposed_ops"]) == 1
    assert text in result["proposed_ops"][0]["body"]


def test_escalation_text_survives_fallback_intact() -> None:
    text = (
        "إذا قال العميل غش أو شكوى أو أبي فلوسي، حوّله للموظف فوراً "
        "وعلّق الرد التلقائي."
    )
    result = _classify(text)
    assert result["fallback_used"] is True
    assert text in result["proposed_ops"][0]["body"]


def test_faq_text_survives_fallback_intact() -> None:
    text = "كم مدة التوصيل المتوقعة لمدينة الرياض؟"
    result = _classify(text)
    assert result["fallback_used"] is True
    assert text in result["proposed_ops"][0]["body"]


def test_product_info_text_survives_fallback_intact() -> None:
    text = (
        "عسل السدر الجبلي يخزّن في مكان جاف بعيد عن الشمس، "
        "ولا يحتاج تبريداً عاديًا."
    )
    result = _classify(text)
    assert result["fallback_used"] is True
    assert text in result["proposed_ops"][0]["body"]


# ════════════════════════════════════════════════════════════════════
# Part 4 — Boundary-keyword presence in the prompt
# ════════════════════════════════════════════════════════════════════
#
# The merchant explicitly asked for "إعطاء وزن أعلى لكلمات: عنوان،
# شحن، سمسا، توصيل، Google Maps، طلب، إكمال الطلب". We don't add
# a hardcoded keyword classifier — the merchant also asked for
# "لا hardcoded mapping جامد، فقط تحسين semantic classification".
# Instead we pin that EVERY one of these signal words appears at
# least once in the rendered prompt (either in the rule body or
# the few-shot examples) — i.e. the model is exposed to them in
# context paired with the correct kind.


def test_prompt_exposes_shipping_signal_keywords() -> None:
    from modules.ai.knowledge.classifier import (
        PlatformSignal,
        _build_system_prompt,
    )
    prompt = _build_system_prompt(
        existing_sections=[],
        attached_media=[],
        platform_signal=PlatformSignal(connected=False, platform=None,
                                       warning=""),
        available_kinds=_AVAILABLE_KINDS,
    )

    # Each signal must appear at least once (rule OR few-shot).
    for kw in (
        "العنوان",
        "الشحن",
        "سمسا",
        "التوصيل",
        "Google Maps",
        "الطلب",
        "إكمال الطلب",
        "الجوال",
    ):
        assert kw in prompt, (
            f"shipping/order signal keyword missing from prompt: {kw!r}"
        )
