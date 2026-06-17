"""
tests/test_dua_greeting_misclassification.py
──────────────────────────────────────────────
P1 — Social reply misclassification: dua/thanks must not receive
opening-greeting replies («هلا وغلا»).
"""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose.persona_template_engine import (  # noqa: E402
    enforce_social_context_reply_guard,
    inbound_is_explicit_opening_greeting,
    inbound_is_religious_dua_exchange,
    pick_persona_social_reply,
    reply_is_opening_greeting_style,
)
from modules.ai.brain.intent.social_classifier import (  # noqa: E402
    SOCIAL_BLESSING,
    SOCIAL_THANKS,
    classify_social,
    is_explicit_opening_greeting,
)
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.postprocess.social_phrase_quality_guard import (  # noqa: E402
    apply_social_phrase_quality_guard,
)
from modules.ai.brain.postprocess.social_reply_context_guard import (  # noqa: E402
    apply_social_reply_context_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_GREETING,
    INTENT_SOCIAL,
    Intent,
    MerchantConversationState,
)


def _ctx(message: str, *, category: str = "blessing") -> BrainContext:
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        message=message,
        intent=Intent(
            name=INTENT_SOCIAL,
            confidence=0.94,
            slots={"social_category": category},
            raw_message=message,
        ),
        state=MerchantConversationState(greeted=True),
        facts=CommerceFacts(has_products=True, orderable=True),
        history=[],
    )


class TestDuaBlessingClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "الله يبارك فيك ويعمرك",
            "إن شاء الله يبارك فيك ويعمرك وكل حياتك وصحتك يارب",
            "جزاك الله خير",
            "الله يسعدك",
        ],
    )
    def test_dua_phrases_classify_as_social_not_greeting(self, message: str) -> None:
        social = classify_social(message)
        assert social is not None, f"expected social match for {message!r}"
        assert social.category in {SOCIAL_THANKS, SOCIAL_BLESSING}, (
            f"expected thanks/blessing for {message!r}, got {social.category}"
        )
        top = intent_rules.match_top_k(message, k=1)
        assert top, f"expected intent for {message!r}"
        assert top[0][1].name == INTENT_SOCIAL
        assert top[0][1].name != INTENT_GREETING

    @pytest.mark.parametrize(
        "message",
        [
            "صباح الخير",
            "هلا",
            "السلام عليكم",
            "مرحبا",
        ],
    )
    def test_explicit_greetings_are_allowed(self, message: str) -> None:
        assert is_explicit_opening_greeting(message)
        assert inbound_is_explicit_opening_greeting(message)
        assert not inbound_is_religious_dua_exchange(message)

    def test_sabah_alkhair_greeting_allowed(self) -> None:
        assert is_explicit_opening_greeting("صباح الخير")

    def test_allah_yesedak_is_dua_not_greeting(self) -> None:
        msg = "الله يسعدك"
        match = classify_social(msg)
        assert match is not None
        assert match.category == SOCIAL_BLESSING
        assert inbound_is_religious_dua_exchange(msg)
        assert not is_explicit_opening_greeting(msg)


class TestNoHlaWghlaOnDuaInbound:
    @pytest.mark.parametrize(
        "inbound",
        [
            "الله يبارك فيك ويعمرك",
            "إن شاء الله يبارك فيك ويعمرك وكل حياتك وصحتك يارب",
            "جزاك الله خير",
            "الله يسعدك",
        ],
    )
    def test_dua_inbound_never_gets_hla_wghla_template(self, inbound: str) -> None:
        reply = pick_persona_social_reply(_ctx(inbound), "blessing", inbound_text=inbound)
        assert "هلا وغلا" not in reply, f"opening greeting on dua inbound: {reply!r}"

    @pytest.mark.parametrize(
        "inbound",
        [
            "الله يبارك فيك ويعمرك",
            "جزاك الله خير",
            "الله يسعدك",
        ],
    )
    def test_context_guard_replaces_hla_wghla(self, inbound: str) -> None:
        guarded = enforce_social_context_reply_guard("هلا وغلا 😊", inbound_text=inbound)
        assert "هلا وغلا" not in guarded
        assert guarded.strip()

    def test_explicit_greeting_allows_hla_wghla(self) -> None:
        guarded = enforce_social_context_reply_guard("هلا وغلا 😊", inbound_text="هلا")
        assert guarded == "هلا وغلا 😊"

    def test_final_guard_in_social_phrase_quality_pipeline(self) -> None:
        result = apply_social_phrase_quality_guard(
            "هلا وغلا 😊",
            inbound_text="الله يبارك فيك ويعمرك",
            tenant_id=1,
        )
        assert "هلا وغلا" not in result.reply

    def test_long_dua_still_classifies_social(self) -> None:
        msg = (
            "إن شاء الله يبارك فيك ويعمرك وكل حياتك وصحتك يارب "
            + " ".join(["كل"] * 10)
        )
        match = classify_social(msg)
        assert match is not None
        assert match.category in {SOCIAL_BLESSING, SOCIAL_THANKS}


class TestGreetingStyleDetection:
    def test_reply_is_opening_greeting_style(self) -> None:
        assert reply_is_opening_greeting_style("هلا وغلا 😊")
        assert not reply_is_opening_greeting_style("آمين وياك، الله يجزاك خير 🌷")

    def test_apply_social_reply_context_guard_noop_on_dua_reply(self) -> None:
        dua_reply = "آمين وياك، الله يجزاك خير 🌷"
        result = apply_social_reply_context_guard(
            dua_reply,
            inbound_text="جزاك الله خير",
        )
        assert result.replaced is False
        assert result.reply == dua_reply
