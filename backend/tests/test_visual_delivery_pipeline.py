"""
tests/test_visual_delivery_pipeline.py
──────────────────────────────────────
Regression tests for the May 2026 #10 catalog / visual product
delivery hardening.

Three things this suite enforces:

  1. ``cta_only`` is now an ACCEPTABLE final mode for visual-product
     intents (was rejected before May #10). The hard-recovery layer
     in the webhook leans on this contract.

  2. ``compute_final_delivery_mode`` still classifies precedence
     correctly — catalog > image_cta > media_only > cta_only >
     text_only > failed.

  3. None of the tone-sensitive layers from prior commits regressed:
     - mirror_reply still mirrors "تسلم" → "الله يسلمك" / "بيض الله
       وجهك" → "وجهك أبيض"
     - the social classifier still routes "بيض الله وجهك" to
       SOCIAL_STRONG_PRAISE
     - mixed deferred turns ("الله يسعدك، ما خلص اللي عندنا") still
       yield to the brain pipeline (social classifier returns None)

We deliberately do NOT test the webhook's HTTP path here — that path
calls the WhatsApp provider, which is out of reach in unit tests.
The new admin debug endpoint
``POST /admin/debug/whatsapp/send-product`` is the operational
counterpart that exercises the same chain against a real recipient.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.observability.delivery_mode import (  # noqa: E402
    DELIVERY_MODE_CATALOG,
    DELIVERY_MODE_CTA_ONLY,
    DELIVERY_MODE_FAILED,
    DELIVERY_MODE_IMAGE_CTA,
    DELIVERY_MODE_MEDIA_ONLY,
    DELIVERY_MODE_TEXT_ONLY,
    compute_final_delivery_mode,
    customer_wants_product_or_image,
    is_acceptable_mode_for_product_intent,
    new_delivery_audit,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Acceptable modes — cta_only is now in the OK set.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode, ok", [
    (DELIVERY_MODE_CATALOG,    True),
    (DELIVERY_MODE_IMAGE_CTA,  True),
    (DELIVERY_MODE_MEDIA_ONLY, True),
    (DELIVERY_MODE_CTA_ONLY,   True),   # NEW — May 2026 #10
    (DELIVERY_MODE_TEXT_ONLY,  False),
    (DELIVERY_MODE_FAILED,     False),
])
def test_acceptable_modes(mode: str, ok: bool) -> None:
    """``text_only`` and ``failed`` are the ONLY modes that should
    flip the [DELIVERY_GUARD_FAIL] alarm. The hard-recovery in the
    webhook downgrades to cta_only on purpose; treating it as
    unacceptable would create alarm fatigue."""
    assert is_acceptable_mode_for_product_intent(mode) is ok


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Final mode precedence — sanity guard for the audit math.
# ─────────────────────────────────────────────────────────────────────────────

def _audit(**overrides) -> dict:
    base = new_delivery_audit()
    base["text_sent"] = True  # default — text body always lands
    base.update(overrides)
    return base


def test_failed_overrides_everything() -> None:
    a = _audit(first_send_failed=True, catalog_card_sent_count=99)
    assert compute_final_delivery_mode(a) == DELIVERY_MODE_FAILED


def test_catalog_wins_over_image() -> None:
    a = _audit(
        catalog_card_sent_count=1,
        legacy_media_sent_count=1,
        cta_url_sent_count=1,
    )
    assert compute_final_delivery_mode(a) == DELIVERY_MODE_CATALOG


def test_image_cta_when_media_and_cta() -> None:
    a = _audit(legacy_media_sent_count=1, cta_url_sent_count=1)
    assert compute_final_delivery_mode(a) == DELIVERY_MODE_IMAGE_CTA


def test_media_only_when_media_but_no_cta() -> None:
    a = _audit(legacy_media_sent_count=1)
    assert compute_final_delivery_mode(a) == DELIVERY_MODE_MEDIA_ONLY


def test_cta_only_when_cta_but_no_media() -> None:
    """This is the exact final mode the visual-fallback rescue
    produces. The recovery path stamps ``cta_url_sent_count`` and
    recomputes — that must land us here, not in text_only."""
    a = _audit(cta_url_sent_count=1)
    assert compute_final_delivery_mode(a) == DELIVERY_MODE_CTA_ONLY


def test_text_only_when_nothing_rich() -> None:
    a = _audit()
    assert compute_final_delivery_mode(a) == DELIVERY_MODE_TEXT_ONLY


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Customer-wants-product classifier — production cases.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "أبغى أشوف صورة لعسل السمر",
    "ورني السمر",
    "أرسل رابط السمر",
    "أبي الكتالوج",
    "عندك صورة للضهيان؟",
    "اعرض لي السمر",
    "شكل المنتج كيف؟",
])
def test_visual_intent_detected(msg: str) -> None:
    """The exact production phrasings the user listed in the bug
    report. The classifier must light up so the visual-enforcer
    appends a product card AND so the fallback guard fires when the
    pipeline still ends up text_only."""
    assert customer_wants_product_or_image(
        inbound_text=msg, brain_action=""
    ) is True


@pytest.mark.parametrize("msg", [
    # Generic links / documents — must NOT trigger.
    "أرسل رابط الموقع",
    "صورة العقد",
    "صورة الفاتورة",
    "رابط الدفع",
    # Tone-sensitive — must NOT trigger.
    "الله يسعدك، ما خلص اللي عندنا",
    "تسلم يا غالي",
    "بيض الله وجهك",
    # Empty / noise.
    "",
    "  ",
    "السلام عليكم",
])
def test_non_visual_messages_not_flagged(msg: str) -> None:
    """The negative-phrase gate + keyword tightness must prevent
    the enforcer from attaching product cards onto tone-sensitive
    or document-style turns. This is the contract that keeps the
    May 2026 #8/#9 tone fixes intact."""
    assert customer_wants_product_or_image(
        inbound_text=msg, brain_action=""
    ) is False


def test_brain_action_signal_alone_is_enough() -> None:
    """Brain action ``search_products`` MUST trigger visual intent
    regardless of inbound text — that's the most reliable signal we
    have because the decision engine already ran the full intent
    pipeline."""
    assert customer_wants_product_or_image(
        inbound_text="", brain_action="search_products"
    ) is True


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Tone regressions — May 2026 #8/#9 contracts intact.
# ─────────────────────────────────────────────────────────────────────────────

def test_mirror_replies_unchanged() -> None:
    """The mirror layer must still return the exact strings May
    2026 #9 specified. The catalog work above MUST NOT touch
    social tone."""
    from modules.ai.brain.compose.mirror_replies import (  # noqa: PLC0415
        mirror_reply,
    )
    assert "الله يسلمك" in (mirror_reply("تسلم يا غالي") or "")
    assert "وجهك أبيض" in (mirror_reply("بيض الله وجهك") or "")
    assert "وإياك" in (mirror_reply("جزاك الله خير") or "")


def test_social_classifier_unchanged_on_strong_praise() -> None:
    """Strong praise still routes to SOCIAL_STRONG_PRAISE."""
    from modules.ai.brain.intent.social_classifier import (  # noqa: PLC0415
        SOCIAL_STRONG_PRAISE,
        classify_social,
    )
    r = classify_social("بيض الله وجهك")
    assert r is not None and r.category == SOCIAL_STRONG_PRAISE


def test_social_classifier_yields_on_deferred_courtesy() -> None:
    """Mixed deferred courtesy must STILL drop out of the social
    classifier so the brain pipeline + stance detector handle the
    turn (May 2026 #8 contract)."""
    from modules.ai.brain.intent.social_classifier import (  # noqa: PLC0415
        classify_social,
    )
    assert classify_social("الله يسعدك، ما خلص اللي عندنا") is None
    assert classify_social("شكرًا، لسه عندي من المرة اللي قبل") is None


def test_deferred_directive_unchanged() -> None:
    """The May 2026 #9 deferred directive must still forbid generic
    closers — catalog work must NOT regress the tone rules."""
    from modules.ai.brain.intent.stance_detector import (  # noqa: PLC0415
        STANCE_DEFERRED, STANCE_DIRECTIVES,
    )
    d = STANCE_DIRECTIVES[STANCE_DEFERRED]
    assert "دوم بخير" in d  # explicit forbid
    assert "ممنوع" in d
    assert "pitch" in d or "بيعي" in d


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Hard-fallback contract — text_only must be unacceptable.
# ─────────────────────────────────────────────────────────────────────────────

def test_fallback_contract_text_only_unacceptable() -> None:
    """When the customer asked for a product and we ended up at
    text_only, the guard MUST fire. This is the entire reason for
    the [VISUAL_FALLBACK_RECOVERED] rescue layer added in the
    webhook — if this contract regresses we lose the alarm."""
    a = _audit(text_sent=True)
    mode = compute_final_delivery_mode(a)
    assert mode == DELIVERY_MODE_TEXT_ONLY
    assert is_acceptable_mode_for_product_intent(mode) is False


def test_fallback_contract_cta_only_acceptable() -> None:
    """Symmetric to the above — after the rescue stamps a CTA-URL
    we MUST land in an acceptable mode so the guard doesn't fire
    on a successfully-recovered turn."""
    a = _audit(text_sent=True, cta_url_sent_count=1)
    mode = compute_final_delivery_mode(a)
    assert mode == DELIVERY_MODE_CTA_ONLY
    assert is_acceptable_mode_for_product_intent(mode) is True
