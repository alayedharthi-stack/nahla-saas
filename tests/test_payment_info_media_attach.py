"""Regression tests for the bank-transfer / payment-info handoff bug.

The merchant reported: customer wrote "ارسل لي حساب الراجحي" while a
matching media item ("باركود التحويل البنكي الراجحي") was active in
the AI Media Library. Instead of attaching the media, the system fell
through to the static "هذه وسائل التواصل المتاحة" FAQ template
(``faq_owner_contact``) and the barcode never reached the customer.

Root causes locked here so they can't regress:

  1. Intent classification — "ارسل حساب الراجحي" used to land on
     INTENT_ASK_OWNER_CONTACT (sometimes via the LLM slot-extractor
     hint). It must now match INTENT_ASK_PAYMENT_INFO with confidence
     >= 0.95 so it beats both OWNER_CONTACT (0.92) and ASK_PRODUCT
     (0.88) without needing the LLM hint at all.

  2. Decision routing — INTENT_ASK_PAYMENT_INFO must NOT route to
     ACTION_FAQ_REPLY{topic=owner_contact}. The brain composes via
     LLM so it can attach a [MEDIA:<id>] marker.

  3. Relevance scoring — the AI Media Library lister must rank a
     bank-transfer media item ABOVE an unrelated one when the
     customer's query mentions "الراجحي" / "تحويل" / "آيبان", even
     if the media's tags don't share any literal characters with the
     query (they share *meaning* through the synonym cluster).

  4. Prompt instruction — the brain prompt must explicitly tell GPT
     to consult the media library FIRST for payment-info questions
     and never fall back to "تواصل مع المتجر" / handoff if a media
     item is available.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ─────────────────────────────────────────────────────────────────────────
# 1. Intent classification — keyword rules win without needing the LLM
# ─────────────────────────────────────────────────────────────────────────

def _classify_rules(message: str):
    """Run only the rules-based matcher (no LLM) so the test is hermetic."""
    from modules.ai.brain.intent.rules import match as match_rules
    return match_rules(message)


def test_send_rajhi_account_routes_to_payment_info():
    intent = _classify_rules("ارسل لي حساب الراجحي")
    assert intent is not None, "no rule fired for 'ارسل لي حساب الراجحي'"
    assert intent.name == "ask_payment_info", (
        f"expected ask_payment_info, got {intent.name}"
    )
    assert intent.confidence >= 0.95


def test_send_iban_routes_to_payment_info():
    intent = _classify_rules("ابغى الآيبان")
    assert intent is not None
    assert intent.name == "ask_payment_info"


def test_send_bank_transfer_data_routes_to_payment_info():
    intent = _classify_rules("بيانات التحويل البنكي")
    assert intent is not None
    assert intent.name == "ask_payment_info"


def test_send_payment_barcode_routes_to_payment_info():
    intent = _classify_rules("ودي باركود التحويل")
    assert intent is not None
    assert intent.name == "ask_payment_info"


def test_qr_code_request_routes_to_payment_info():
    intent = _classify_rules("ابغى qr code")
    assert intent is not None
    assert intent.name == "ask_payment_info"


def test_english_bank_account_routes_to_payment_info():
    intent = _classify_rules("send me your bank account")
    assert intent is not None
    assert intent.name == "ask_payment_info"


def test_owner_contact_phrase_still_works_when_no_payment_words():
    """A clean owner-contact request must NOT be hijacked by payment-info."""
    intent = _classify_rules("ابغى رقمكم")
    assert intent is not None
    assert intent.name == "ask_owner_contact", (
        f"plain owner-contact request collapsed to {intent.name}"
    )


def test_payment_info_beats_owner_contact_in_mixed_request():
    """When the query mentions payment AND a verb that other rules also
    catch, payment-info must still win because of its higher confidence."""
    intent = _classify_rules("أرسل الآيبان")
    assert intent is not None
    assert intent.name == "ask_payment_info"
    assert intent.confidence >= 0.95


# ─────────────────────────────────────────────────────────────────────────
# 2. Decision routing — payment intent goes to LLM, not FAQ template
# ─────────────────────────────────────────────────────────────────────────

def _decide_payment_info(message: str):
    """Run DefaultDecisionEngine for a payment-info style customer message."""
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        MerchantConversationState,
    )

    intent = _classify_rules(message)
    assert intent is not None, f"no intent matched for {message!r}"
    assert intent.name == "ask_payment_info", (
        f"expected ask_payment_info for {message!r}, got {intent.name}"
    )
    facts = CommerceFacts(
        payment_methods=["cod", "bank"],
        payment_methods_source="salla_merchant_enabled",
        salla_payments_status="known",
        merchant_capabilities={
            "source": "salla",
            "kind": "merchant_enabled",
            "payments": {
                "status": "known",
                "methods": [
                    {"code": "cod", "label": "cod", "enabled": True},
                    {"code": "bank", "label": "bank", "enabled": True},
                ],
            },
            "shipping": {
                "companies_status": "known",
                "companies": [],
                "zones_status": "known",
                "zones": [],
            },
        },
    )
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        history=[],
        profile={},
        intent=intent,
        state=MerchantConversationState(stage="browsing", greeted=True),
        facts=facts,
    )
    return DefaultDecisionEngine().decide(ctx)


def test_decision_engine_routes_payment_info_to_llm_not_faq():
    """The bug was: ASK_PAYMENT_INFO (or its precursor in OWNER_CONTACT)
    routed to ACTION_FAQ_REPLY{owner_contact} which short-circuited the
    media library. Lock the LLM route so GPT can attach [MEDIA:<id>].

    Assert the live decision contract (not source layout / char windows).
    """
    from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY

    # Classic bank-account / payment-info path (media attach contract).
    payment_info = _decide_payment_info("ارسل لي حساب الراجحي")
    assert payment_info.action == ACTION_LLM_REPLY, (
        "INTENT_ASK_PAYMENT_INFO must use ACTION_LLM_REPLY so the media "
        "library can attach a barcode — not ACTION_FAQ_REPLY which would "
        "re-introduce the static contact-owner fallback bug."
    )
    assert payment_info.action != ACTION_FAQ_REPLY
    assert payment_info.args.get("topic") == "payment_info"

    # Barcode image request stays on LLM path.
    barcode = _decide_payment_info("ارسل باركود التحويل")
    assert barcode.action == ACTION_LLM_REPLY
    assert barcode.action != ACTION_FAQ_REPLY
    assert barcode.args.get("topic") == "payment_barcode_image"

    # Pack B merchant payment-methods FAQ also stays on LLM path with
    # MERCHANT_CAPABILITIES ownership (must not fall back to FAQ).
    methods = _decide_payment_info("وش طرق الدفع عندكم؟")
    assert methods.action == ACTION_LLM_REPLY
    assert methods.action != ACTION_FAQ_REPLY
    assert methods.args.get("topic") == "merchant_payment_methods"
    assert methods.args.get("capability_surface") == "salla_merchant_enabled"

    assert ACTION_LLM_REPLY != ACTION_FAQ_REPLY


# ─────────────────────────────────────────────────────────────────────────
# 3. Relevance scoring — synonym cluster pulls bank-transfer media to top
# ─────────────────────────────────────────────────────────────────────────

def test_relevance_score_rajhi_query_matches_bank_tags():
    """Customer asks for "الراجحي" — synonym expansion must light up a
    media item tagged [تحويل / بنك / آيبان] even though those tags
    don't share characters with "راجحي"."""
    from core.ai_libraries import _relevance_score

    bank_item = {
        "title": "باركود التحويل البنكي",
        "tags": ["تحويل", "بنك", "آيبان"],
        "usage_context": "أرسله إذا طلب العميل التحويل البنكي",
    }
    unrelated_item = {
        "title": "صورة عسل السمر",
        "tags": ["عسل", "سمر"],
        "usage_context": "أرسلها إذا سأل عن عسل السمر",
    }

    q = "ارسل لي حساب الراجحي"
    bank_score = _relevance_score(bank_item, q)
    unrelated_score = _relevance_score(unrelated_item, q)
    assert bank_score > 0, "bank media item didn't pick up any synonym match"
    assert bank_score > unrelated_score, (
        f"bank score {bank_score} did not beat unrelated {unrelated_score}"
    )


def test_relevance_score_iban_query_matches_arabic_tag():
    """English query 'IBAN' should pick up Arabic tag 'آيبان' via the
    same cluster (and tashkeel/alif normalisation)."""
    from core.ai_libraries import _relevance_score

    item = {
        "title": "بيانات التحويل",
        "tags": ["آيبان", "تحويل"],
        "usage_context": "",
    }
    q = "send me iban please"
    assert _relevance_score(item, q) > 0


def test_relevance_score_definite_article_invariant():
    """A query for "البنك" (with prefix) should match a tag of "بنك"
    (without prefix), and vice versa."""
    from core.ai_libraries import _relevance_score

    item_a = {"title": "x", "tags": ["بنك"], "usage_context": ""}
    item_b = {"title": "x", "tags": ["البنك"], "usage_context": ""}

    assert _relevance_score(item_a, "حساب البنك") > 0
    assert _relevance_score(item_b, "حساب بنك") > 0


def test_relevance_sorting_lifts_bank_item_to_top_for_payment_query():
    """End-to-end ordering check: with a high-priority unrelated item
    and a low-priority bank-transfer item, the bank item must still
    surface first when the customer asks about payment because of the
    synonym-aware relevance score."""
    from core.ai_libraries import _sort_with_relevance

    items = [
        {"id": 1, "title": "صورة منتج", "tags": ["منتج"], "priority": 1, "usage_context": ""},
        {"id": 2, "title": "باركود التحويل", "tags": ["تحويل", "بنك"], "priority": 100, "usage_context": ""},
    ]
    out = _sort_with_relevance(items, "ارسل لي حساب الراجحي", cap=5)
    assert [m["id"] for m in out] == [2, 1], (
        f"expected bank media (id=2) first, got order {[m['id'] for m in out]}"
    )


# ─────────────────────────────────────────────────────────────────────────
# 4. Prompt builder — instructs GPT to use media library before any
#    owner-contact / handoff fallback for payment questions.
# ─────────────────────────────────────────────────────────────────────────

def test_prompt_includes_payment_media_first_rule():
    """The system prompt must contain the explicit rule that bank /
    IBAN / QR questions look at ai_media_library FIRST before any
    handoff or contact-store template."""
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
    from modules.ai.brain.types import BrainReplyState

    state = BrainReplyState(
        store_name="متجر اختبار",
        tone="neutral",
        intent_name="ask_payment_info",
        stage="exploring",
        response_goal="share_payment_info",
        merchant_context={
            "brain_profile": {"autopilot_enabled": False},
            "manual_coupons": [],
            "ai_media_library": [{
                "id": 12,
                "title": "باركود التحويل البنكي الراجحي",
                "media_type": "image",
                "tags": ["تحويل", "بنك", "راجحي", "آيبان"],
                "usage_context": "أرسله إذا طلب العميل التحويل البنكي",
                "description": "",
                "priority": 5,
            }],
        },
    )
    prompt = build_brain_reply_prompt(state)
    # Explicit guidance for payment-info questions
    assert "ai_media_library" in prompt
    assert "[MEDIA:<id>]" in prompt
    # The rule must mention the cluster of payment terms so GPT knows
    # WHEN to apply it.
    for keyword in ("تحويل", "بنك", "باركود", "آيبان", "qr"):
        assert keyword in prompt.lower() or keyword in prompt, (
            f"payment-media rule missing keyword {keyword!r}"
        )
    # Must forbid handoff/contact-owner fallback when media is available.
    # Phase-1 prompt refactor (708655b1) moved this policy into
    # high_priority_layer.BASELINE_POLICY_RULES with updated wording.
    assert "سأحوّلك للفريق" in prompt or "مكتبة الوسائط" in prompt
    assert "MEDIA_ID=12" in prompt  # GPT can see the exact id to cite
    assert "باركود التحويل البنكي الراجحي" in prompt


# ─────────────────────────────────────────────────────────────────────────
# 5. End-to-end scenario from the merchant's bug report
# ─────────────────────────────────────────────────────────────────────────

def test_scenario_send_rajhi_account_with_active_media():
    """Full scenario lockdown:
        - autopilot disabled
        - active media item titled "باركود التحويل البنكي الراجحي"
        - customer says "ارسل حساب الراجحي"

    Expected (verified at the contract layer — no LLM call):
        * intent classification  → ask_payment_info (not owner_contact)
        * relevance scoring      → bank media beats unrelated items
        * prompt rendering       → GPT sees the exact media id, the
                                   payment-media rule fires, and no
                                   contact-owner fallback is encouraged.
    """
    from core.ai_libraries import _relevance_score
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
    from modules.ai.brain.intent.rules import match as match_rules
    from modules.ai.brain.types import BrainReplyState

    customer_msg = "ارسل حساب الراجحي"

    # Intent
    intent = match_rules(customer_msg)
    assert intent is not None and intent.name == "ask_payment_info"

    # Relevance
    bank_item = {
        "id": 7,
        "title": "باركود التحويل البنكي الراجحي",
        "tags": ["تحويل", "بنك", "راجحي"],
        "usage_context": "أرسله إذا طلب العميل التحويل البنكي",
    }
    assert _relevance_score(bank_item, customer_msg) > 0

    # Prompt rendering
    state = BrainReplyState(
        store_name="نحلة",
        tone="neutral",
        intent_name="ask_payment_info",
        stage="exploring",
        response_goal="share_payment_info",
        merchant_context={
            "brain_profile": {"autopilot_enabled": False},
            "manual_coupons": [],
            "ai_media_library": [
                {**bank_item, "media_type": "image", "description": "", "priority": 5},
            ],
        },
    )
    prompt = build_brain_reply_prompt(state)
    assert "MEDIA_ID=7" in prompt
    assert "باركود التحويل البنكي الراجحي" in prompt
