"""
tests/test_product_visual_dispatch.py
─────────────────────────────────────
Product visual requests + final dispatch send-boundary guards.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.product_visual import (  # noqa: E402
    attachment_matches_turn_request,
    extract_visual_product_query,
    focus_age_turns,
    is_deictic_visual_request,
    is_focus_fresh,
    is_product_visual_request,
    normalize_for_visual_detection,
    prepare_inbound_for_commerce,
    resolve_trusted_focus_for_deictic,
)
from modules.ai.brain.intent.rules import match as match_intent  # noqa: E402
from modules.ai.brain.types import INTENT_PRODUCT_VISUAL_REQUEST  # noqa: E402
from modules.observability.visual_enforcement import (  # noqa: E402
    SOURCE_FOCUS,
    SOURCE_NONE,
    pick_best_candidate_title,
)
from services.final_dispatch_guard import (  # noqa: E402
    should_allow_product_attachment_dispatch,
    validate_product_attachment_for_send,
)

_BEE_VENOM = "كريم سم النحل بلس"
_TALH = "عسل طلح"


def _state(
    *,
    turn: int = 5,
    focus_title: str = "",
    focus_turn: int = 0,
    visual_focus_turn: int = 0,
    recent: list | None = None,
    canonical: str = "",
    canonical_turn: int = 0,
) -> dict:
    bs: dict = {
        "turn": turn,
        "product_focus_turn": focus_turn,
        "visual_focus_turn": visual_focus_turn,
        "last_inbound_canonical": canonical,
        "last_inbound_canonical_turn": canonical_turn,
        "recent_messages": recent or [],
    }
    if focus_title:
        bs["current_product_focus"] = {"title": focus_title, "id": 101}
    return bs


def test_voice_stt_variants_detect_as_visual():
    for msg in (
        "ابي اشوف الصوره",
        "ابغا الصوره",
        "وين الصوره",
        "ورني شكله",
    ):
        assert is_product_visual_request(msg), msg


def test_focus_poisoning_topic_shift_prefers_talh():
    """Bee Venom focus + recent طلح mention → deictic resolves to طلح."""
    state = _state(
        turn=12,
        focus_title=_BEE_VENOM,
        focus_turn=3,
        recent=[
            {"role": "user", "content": "كم سعر طلح"},
            {"role": "assistant", "content": "عسل طلح 150 ريال"},
        ],
    )
    trusted = resolve_trusted_focus_for_deictic(state, "الصورة وينها")
    assert trusted.title
    assert "طلح" in trusted.title.lower() or trusted.reason.startswith("topic_shift")
    title, source = pick_best_candidate_title(state, "الصورة وينها")
    assert "طلح" in title.lower()
    assert source != SOURCE_NONE


def test_stale_focus_deictic_clarify_not_bee_venom():
    state = _state(
        turn=30,
        focus_title=_BEE_VENOM,
        focus_turn=2,
        recent=[],
    )
    trusted = resolve_trusted_focus_for_deictic(state, "الصورة وينها")
    assert not trusted.title
    assert trusted.reason == "focus_too_stale"
    ok, reason = attachment_matches_turn_request(
        inbound_message="الصورة وينها",
        attachment_title=_BEE_VENOM,
        brain_state=state,
    )
    assert ok is False
    assert "stale" in reason


def test_fresh_talh_focus_deictic_allows_talh_card():
    state = _state(turn=8, focus_title=_TALH, focus_turn=7)
    assert is_focus_fresh(state)
    ok, reason = attachment_matches_turn_request(
        inbound_message="الصورة وينها",
        attachment_title=_TALH,
        brain_state=state,
        intent_name="product_visual_request",
    )
    assert ok is True
    assert "deictic" in reason or "focus" in reason


def test_canonical_inbound_used_at_dispatch():
    raw = "ابغا الصوره"
    bs = _state(canonical="أبي أشوف صورة الطلح", canonical_turn=4, turn=5)
    prepared = prepare_inbound_for_commerce(raw, bs)
    assert "طلح" in prepared


def test_explicit_sku_still_allowed():
    d = should_allow_product_attachment_dispatch(
        brain_action="search_products",
        intent_name="product_visual_request",
        inbound_message="أرسل صورة الطلح",
        reply_text="تفضل",
    )
    assert d.allow is True
    ok, reason = validate_product_attachment_for_send(
        inbound_message="أرسل صورة الطلح",
        attachment={"title": _TALH},
        dispatch_allowed=True,
    )
    assert ok is True
    assert "explicit" in reason


def test_compare_request_not_visual():
    assert not is_product_visual_request("قارن بين الشكلين")
    assert not is_deictic_visual_request("قارن بين الشكلين")


def test_legitimate_variant_shape_request():
    assert is_product_visual_request("ورني شكله")
    state = _state(turn=6, focus_title=_TALH, focus_turn=5)
    ok, _ = attachment_matches_turn_request(
        inbound_message="ورني شكله",
        attachment_title=_TALH,
        brain_state=state,
        intent_name="product_visual_request",
    )
    assert ok is True


def test_carousel_reference_extraction():
    q = extract_visual_product_query("أبي صورة العسل اللي فوق")
    assert q
    assert "فوق" in q or "عسل" in q


def test_voice_turn_without_product_request_blocks_bee_venom():
    d = should_allow_product_attachment_dispatch(
        brain_action="llm_reply",
        intent_name="social",
        inbound_message="[تفريغ التسجيل] جزاك الله خير",
        reply_text="الله يبارك فيك 🌷",
    )
    assert d.allow is False


def test_stale_candidate_suppressed_at_send_boundary():
    d = should_allow_product_attachment_dispatch(
        brain_action="llm_reply",
        intent_name="general",
        inbound_message="تمام",
        reply_text="تمام 🌷",
    )
    assert d.allow is False
    ok, reason = validate_product_attachment_for_send(
        inbound_message="تمام",
        attachment={"title": _BEE_VENOM},
        brain_state={"last_search_candidates": [{"title": _BEE_VENOM}]},
        dispatch_allowed=d.allow,
    )
    assert ok is False


def test_named_talh_visual_request_intent():
    intent = match_intent("أبي أشوف صورة الطلح")
    assert intent is not None
    assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST


def test_deictic_without_focus_does_not_pick_stale_search():
    title, source = pick_best_candidate_title(
        {"last_search_candidates": [{"title": _BEE_VENOM}]},
        "الصورة وينها",
    )
    assert title == ""
    assert source == SOURCE_NONE


def test_bee_venom_blocked_when_customer_asked_talh():
    state = _state(turn=10, focus_title=_TALH, focus_turn=9)
    ok, _ = attachment_matches_turn_request(
        inbound_message="أبي أشوف صور العسل",
        attachment_title=_BEE_VENOM,
        brain_state=state,
        intent_name="product_visual_request",
    )
    assert ok is False


def test_focus_age_turns():
    assert focus_age_turns(_state(turn=10, focus_turn=8)) == 2
    assert focus_age_turns(_state(turn=10, focus_turn=0)) == 999


def test_normalize_strips_voice_framing():
    n = normalize_for_visual_detection("[تفريغ التسجيل] وين الصوره")
    assert "وين" in n and "صور" in n


def test_focus_resolution_logs_confidence_telemetry(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="modules.ai.brain.commerce.product_visual")
    state = _state(turn=8, focus_title=_TALH, focus_turn=7)
    trusted = resolve_trusted_focus_for_deictic(state, "الصورة وينها")
    assert trusted.title == _TALH
    assert trusted.freshness_score > 0
    assert trusted.continuity_score >= 0
    assert any("[FOCUS_RESOLUTION]" in r.message for r in caplog.records)
    assert any("trusted=true" in r.message for r in caplog.records)


def test_stale_visual_focus_rejects_deictic_even_when_text_focus_fresh():
    """Old product card + fresh text focus without reinforcement → clarify."""
    state = _state(
        turn=20,
        focus_title=_BEE_VENOM,
        focus_turn=18,
        visual_focus_turn=10,
        recent=[],
    )
    trusted = resolve_trusted_focus_for_deictic(state, "الصورة وينها")
    assert not trusted.title
    assert trusted.reason == "visual_focus_stale"
    ok, reason = attachment_matches_turn_request(
        inbound_message="الصورة وينها",
        attachment_title=_BEE_VENOM,
        brain_state=state,
    )
    assert ok is False
    assert "stale" in reason


def test_visual_focus_reinforced_by_recent_mention_allows_deictic():
    state = _state(
        turn=20,
        focus_title=_BEE_VENOM,
        focus_turn=18,
        visual_focus_turn=10,
        recent=[{"role": "user", "content": "كم سعر كريم سم النحل"}],
    )
    trusted = resolve_trusted_focus_for_deictic(state, "الصورة وينها")
    assert trusted.title == _BEE_VENOM
    assert trusted.reason == "focus_fresh"
