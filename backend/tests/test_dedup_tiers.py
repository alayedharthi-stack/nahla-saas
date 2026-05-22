"""
backend/tests/test_dedup_tiers.py
─────────────────────────────────
Regression suite for the tiered outbound-dedup guard (May 2026 #34).

Philosophy
──────────
The v1 guard treated ANY ≥60% lexical overlap as a "duplicate" and
replaced the reply with a canned fallback. In production that fired
on perfectly natural re-asks — voice transcripts, delayed
re-engagement, the customer asking the same question twice — and
the canned replacement made the bot feel cold and robotic.

v2 (this commit) splits the detection into two tiers:

  * SOFT  (60% ≤ overlap < 85%): the LLM is repeating a TOPIC with
          its own wording. Pass through, log for telemetry.
  * HARD  (overlap ≥ 85%): near-verbatim. Replace with a fallback
          ONLY when the reply does NOT carry a URL / phone / asset
          marker. Asset-bearing replies always pass through because
          the asset itself is the new content (the customer asking
          "ابي الباركود" twice still deserves the barcode).

These tests pin the new contract: the helpers return the right
classification for each tier, and ``_reply_carries_new_signal``
correctly bypasses the replacement for URL / phone / marker
replies.
"""
from __future__ import annotations

import os
import sys
from typing import List, Dict

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _h(*outs: str) -> List[Dict[str, str]]:
    """Build a history list with N outbound turns (newest last)."""
    return [{"direction": "outbound", "body": body} for body in outs]


# ── _max_outbound_overlap — the numeric foundation ─────────────────────────


def test_overlap_zero_when_no_history() -> None:
    """Bootstrap turn: no prior outbound, no overlap to compute."""
    from routers.whatsapp_webhook import _max_outbound_overlap
    assert _max_outbound_overlap(
        "أهلاً بك في متجرنا — كيف نقدر نخدمك اليوم؟", [],
    ) == 0.0


def test_overlap_zero_when_reply_below_min_tokens() -> None:
    """Replies shorter than _DEDUP_MIN_TOKENS=6 always overlap with
    something; we deliberately skip the detector for them so a quick
    "تفضل" or "أبشر" doesn't trip the guard."""
    from routers.whatsapp_webhook import _max_outbound_overlap
    history = _h("هذي عبارة طويلة جدا حتى تتجاوز عتبة الحد الادنى للكلمات")
    assert _max_outbound_overlap("تفضل", history) == 0.0


def test_overlap_picks_max_across_lookback() -> None:
    """When two recent outbounds exist, we take the MAX overlap so a
    matching mid-window reply isn't masked by an unrelated newest one."""
    from routers.whatsapp_webhook import _max_outbound_overlap
    history = _h(
        "هذا متجرنا الالكتروني وفيه عسل سدر وطلح وسمر تفضل بالاطلاع",
        "كلام مختلف تماما لا يطابق الرد الجديد بأي كلمة من كلماته",  # newest
    )
    new_reply = "هذا متجرنا الالكتروني وفيه عسل سدر وطلح وسمر تفضل بالاطلاع"
    overlap = _max_outbound_overlap(new_reply, history)
    assert overlap >= 0.9, (
        f"max-overlap should match the older outbound: got {overlap:.2f}"
    )


# ── Tier classification via _is_repeat_reply ───────────────────────────────


def test_is_repeat_reply_hard_default_threshold() -> None:
    """Default threshold is HARD (0.85) — the function says
    ``True`` ONLY for near-verbatim repetition. Old "any 60%" callers
    automatically migrate to the safer conservative behaviour."""
    from routers.whatsapp_webhook import _is_repeat_reply
    prev = "السلام عليكم تفضل بزيارة متجرنا للاطلاع على المنتجات الجديدة"
    history = _h(prev)
    # Exact same reply → ~1.0 overlap → HARD repeat.
    assert _is_repeat_reply(prev, history) is True


def test_is_repeat_reply_soft_overlap_not_hard() -> None:
    """A topic-similar reply with its own wording must NOT trip the
    default (hard) detector — that's the whole point of v2."""
    from routers.whatsapp_webhook import _is_repeat_reply
    history = _h(
        "نوفر العسل بأنواعه: السدر والطلح والسمر — كل نوع له ميزته"
    )
    paraphrase = (
        "بالنسبة لأنواع العسل عندنا السدر والطلح والسمر و"
        "تقدر تشوف الميزات لكل نوع"
    )
    # Paraphrase shares topic words ("العسل", "السدر", "الطلح",
    # "السمر") but rewrites the structure. Hard threshold should
    # consider this NOT a repeat.
    assert _is_repeat_reply(paraphrase, history) is False


def test_is_repeat_reply_respects_explicit_threshold() -> None:
    """The webhook passes the SOFT threshold explicitly when it wants
    to log a topic-overlap turn. The function must honour that.

    Pair calibrated to ~0.80 overlap (the SOFT band): same opening
    + product list, different CTA. Mirrors the real "two answers
    about the same products with different framing" pattern that
    used to trip the v1 guard incorrectly.
    """
    from routers.whatsapp_webhook import (
        _is_repeat_reply, _DEDUP_OVERLAP_THRESHOLD,
    )
    history = _h(
        "تفضل المتجر الالكتروني فيه العسل السدر الطلح السمر للزيارة"
    )
    near_dup = (
        "تفضل المتجر الالكتروني فيه العسل السدر الطلح السمر للبيع الآن"
    )
    # ~80% overlap: SOFT band (≥0.60) should fire, HARD (≥0.85) shouldn't.
    assert _is_repeat_reply(
        near_dup, history, threshold=_DEDUP_OVERLAP_THRESHOLD,
    ) is True
    assert _is_repeat_reply(near_dup, history) is False


def test_is_repeat_reply_empty_inputs_safe() -> None:
    """Defensive: empty / None inputs must not raise."""
    from routers.whatsapp_webhook import _is_repeat_reply
    assert _is_repeat_reply("", []) is False
    assert _is_repeat_reply("anything", []) is False
    assert _is_repeat_reply("", _h("non-empty outbound")) is False


# ── _reply_carries_new_signal — the asset-bypass guard ─────────────────────


def test_carries_signal_detects_https_url() -> None:
    """Any URL → bypass dedup. Customer asked twice and we have the
    link — let the link land both times."""
    from routers.whatsapp_webhook import _reply_carries_new_signal
    assert _reply_carries_new_signal(
        "تفضل رابط متجرنا 🌷\nhttps://mystore.example.sa"
    ) is True
    # http (no s) also counts — some merchants still run plain HTTP.
    assert _reply_carries_new_signal(
        "اضغط هنا http://example.sa/cart"
    ) is True


def test_carries_signal_detects_saudi_mobile() -> None:
    """Bare Saudi mobile (05XXXXXXXX) and +966 variants must trigger
    the bypass so a "ابي رقم أبو هشام" re-ask delivers the number."""
    from routers.whatsapp_webhook import _reply_carries_new_signal
    assert _reply_carries_new_signal("للتواصل: 0501234567") is True
    assert _reply_carries_new_signal("للتواصل: +966501234567") is True


def test_carries_signal_detects_asset_markers() -> None:
    """[MEDIA:], [MEDIA_KEY:], [PRODUCT:], [CALL:] — every asset
    marker the LLM emits must flip the signal so the downstream
    resolver gets a chance to attach the actual asset."""
    from routers.whatsapp_webhook import _reply_carries_new_signal
    assert _reply_carries_new_signal(
        "تفضل [MEDIA:42] هذا باركود الراجحي"
    ) is True
    assert _reply_carries_new_signal(
        "[MEDIA_KEY:payment_rajhi_barcode]"
    ) is True
    assert _reply_carries_new_signal(
        "هذا [PRODUCT:عسل السمر] للبيع"
    ) is True
    assert _reply_carries_new_signal(
        "[CALL:+966500000000|أبو هشام]"
    ) is True


def test_carries_signal_negative_cases() -> None:
    """Plain conversational text without any asset → no signal,
    dedup replacement is allowed to proceed (when HARD)."""
    from routers.whatsapp_webhook import _reply_carries_new_signal
    assert _reply_carries_new_signal(
        "أهلاً بك 🌷 كيف نقدر نخدمك اليوم؟"
    ) is False
    assert _reply_carries_new_signal("") is False
    # Numbers that aren't phones — prices, quantities — don't trip.
    assert _reply_carries_new_signal("السعر 150 ريال") is False


# ── End-to-end tier behaviour: tier × signal matrix ────────────────────────


@pytest.mark.parametrize("reply, history_body, expected_overlap_band", [
    # ── HARD tier (≥85%) ────────────────────────────────────────────
    (
        "السلام عليكم تفضل بزيارة متجرنا للاطلاع على المنتجات الجديدة",
        "السلام عليكم تفضل بزيارة متجرنا للاطلاع على المنتجات الجديدة",
        "hard",
    ),
    # ── SOFT tier (60–85%) ──────────────────────────────────────────
    (
        "تفضل المتجر الالكتروني فيه العسل السدر الطلح السمر للبيع الآن",
        "تفضل المتجر الالكتروني فيه العسل السدر الطلح السمر للزيارة",
        "soft",
    ),
    # ── NO repeat (< 60%) ───────────────────────────────────────────
    (
        "تفضل رابط التتبع لطلبك من شركة الشحن",
        "نوفر العسل بأنواعه السدر الطلح السمر",
        "none",
    ),
])
def test_tier_classification_end_to_end(
    reply: str, history_body: str, expected_overlap_band: str,
) -> None:
    """The webhook gate uses ``_max_outbound_overlap`` and the two
    thresholds to classify a reply. This test pins the band each
    fixture should land in so future tweaks to either threshold are
    intentional."""
    from routers.whatsapp_webhook import (
        _max_outbound_overlap,
        _DEDUP_OVERLAP_THRESHOLD,
        _DEDUP_HARD_OVERLAP_THRESHOLD,
    )
    overlap = _max_outbound_overlap(reply, _h(history_body))
    if expected_overlap_band == "hard":
        assert overlap >= _DEDUP_HARD_OVERLAP_THRESHOLD, (
            f"expected HARD tier, got overlap={overlap:.2f}"
        )
    elif expected_overlap_band == "soft":
        assert _DEDUP_OVERLAP_THRESHOLD <= overlap < _DEDUP_HARD_OVERLAP_THRESHOLD, (
            f"expected SOFT tier, got overlap={overlap:.2f}"
        )
    else:
        assert overlap < _DEDUP_OVERLAP_THRESHOLD, (
            f"expected NO repeat, got overlap={overlap:.2f}"
        )


def test_thresholds_are_calibrated_correctly() -> None:
    """Sanity: the two thresholds satisfy the documented contract
    (soft < hard, both in [0, 1]) so the tier logic in the webhook
    gate can't silently invert."""
    from routers.whatsapp_webhook import (
        _DEDUP_OVERLAP_THRESHOLD as SOFT,
        _DEDUP_HARD_OVERLAP_THRESHOLD as HARD,
    )
    assert 0.0 < SOFT < HARD < 1.0
    assert SOFT == 0.60
    assert HARD == 0.85


def test_short_reply_under_min_tokens_never_repeats() -> None:
    """Replies under ``_DEDUP_MIN_TOKENS`` (6 distinct tokens) must
    never be considered a repeat — token-set Jaccard is unstable at
    that size, and the merchant's brain often produces legitimate
    short responses ("أبشر 🌷", "تمام", "تفضل") after an outbound."""
    from routers.whatsapp_webhook import _max_outbound_overlap
    history = _h("هذي عبارة كاملة فيها تكرار محتمل لاحقا للحماية")
    # 4 tokens — under the min.
    assert _max_outbound_overlap("تفضل بزيارة المتجر الكريم", history) == 0.0
