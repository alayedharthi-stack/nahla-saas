"""Operational order-reference slots must unlock CHAT_DEDUP hard-tier correctly."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: E402
    extract_operational_slots,
    has_operational_delta_since_last_reply,
    operational_order_reference_slot,
    should_restore_brain_reply_after_dedup_silence,
)
from routers.whatsapp_webhook import (  # noqa: E402
    _DEDUP_HARD_OVERLAP_THRESHOLD,
    _max_outbound_overlap,
    _reply_carries_new_signal,
)

GENERIC_REF_A = "527184639"
GENERIC_REF_B = "519384726"
NOT_FOUND_REPLY = (
    "ما قدرت ألقى طلب بهذا الرقم. تأكد من رقم الطلب وأرسله لي مرة ثانية."
)
PRIOR_NOT_FOUND_OUTBOUND = (
    "ما قدرت ألقى طلب بحالياً لا يوجد رقم تواصل مهيأ لإرساله.. "
    "تأكد من رقم الطلب وأرسله لي مرة ثانية."
)
_SOCIAL_REPLY = "صباح النور! 👋 🌿"


def _hist(*turns):
    return list(turns)


def _would_hard_dedup_silence(
    *,
    inbound: str,
    candidate: str,
    history: list,
) -> bool:
    """Mirror webhook hard-tier silence when no bypass fires."""
    overlap = _max_outbound_overlap(candidate, history)
    is_hard = overlap >= _DEDUP_HARD_OVERLAP_THRESHOLD
    carries_signal = _reply_carries_new_signal(candidate)
    if not (is_hard and not carries_signal):
        return False
    prev_out = ""
    for turn in reversed(history):
        if str(turn.get("direction", "")).lower() in {"out", "outbound"}:
            prev_out = str(turn.get("body") or "")
            break
    if has_operational_delta_since_last_reply(
        inbound,
        candidate,
        prev_out,
        history=history,
    ):
        return False
    if should_restore_brain_reply_after_dedup_silence(
        current_inbound=inbound,
        candidate_reply=candidate,
        previous_outbound=prev_out,
    ):
        return False
    return True


# ── A. Slot extraction ───────────────────────────────────────────────────


def test_bare_order_reference_produces_order_ref_slot():
    slot = operational_order_reference_slot(GENERIC_REF_A)
    assert slot == f"order_ref:{GENERIC_REF_A}"
    assert slot in extract_operational_slots(GENERIC_REF_A)


def test_labeled_order_reference_produces_same_normalized_slot():
    labeled = f"رقم الطلب {GENERIC_REF_B}"
    slot = operational_order_reference_slot(labeled)
    assert slot == f"order_ref:{GENERIC_REF_B}"
    assert f"order_ref:{GENERIC_REF_B}" in extract_operational_slots(labeled)


def test_short_or_non_order_number_not_misclassified():
    assert operational_order_reference_slot("12345") == ""
    assert operational_order_reference_slot("ابي اطلب حذاء") == ""
    assert "order_ref:" not in extract_operational_slots("عندي 3 منتجات فقط")


# ── B. Different reference ───────────────────────────────────────────────


def test_different_order_reference_is_operational_delta():
    history = _hist(
        {"direction": "inbound", "body": GENERIC_REF_A},
        {"direction": "outbound", "body": PRIOR_NOT_FOUND_OUTBOUND},
    )
    assert has_operational_delta_since_last_reply(
        GENERIC_REF_B,
        NOT_FOUND_REPLY,
        PRIOR_NOT_FOUND_OUTBOUND,
        history=history,
    )


def test_different_reference_hard_dedup_does_not_silence():
    history = _hist(
        {"direction": "inbound", "body": GENERIC_REF_A},
        {"direction": "outbound", "body": PRIOR_NOT_FOUND_OUTBOUND},
    )
    assert not _would_hard_dedup_silence(
        inbound=GENERIC_REF_B,
        candidate=NOT_FOUND_REPLY,
        history=history,
    )


# ── C. Same reference ────────────────────────────────────────────────────


def test_same_order_reference_repeat_restores_brain_candidate():
    history = _hist(
        {"direction": "inbound", "body": GENERIC_REF_A},
        {"direction": "outbound", "body": PRIOR_NOT_FOUND_OUTBOUND},
    )
    assert should_restore_brain_reply_after_dedup_silence(
        current_inbound=GENERIC_REF_A,
        candidate_reply=NOT_FOUND_REPLY,
        previous_outbound=PRIOR_NOT_FOUND_OUTBOUND,
    )


def test_same_reference_repeat_not_silenced():
    history = _hist(
        {"direction": "inbound", "body": GENERIC_REF_A},
        {"direction": "outbound", "body": PRIOR_NOT_FOUND_OUTBOUND},
        {"direction": "inbound", "body": GENERIC_REF_A},
    )
    assert not _would_hard_dedup_silence(
        inbound=GENERIC_REF_A,
        candidate=NOT_FOUND_REPLY,
        history=history,
    )


def test_same_reference_no_operational_delta_but_restore_still_wins():
    history = _hist(
        {"direction": "inbound", "body": GENERIC_REF_A},
        {"direction": "outbound", "body": PRIOR_NOT_FOUND_OUTBOUND},
        {"direction": "inbound", "body": GENERIC_REF_A},
    )
    assert not has_operational_delta_since_last_reply(
        GENERIC_REF_A,
        NOT_FOUND_REPLY,
        PRIOR_NOT_FOUND_OUTBOUND,
        history=history,
    )
    assert should_restore_brain_reply_after_dedup_silence(
        current_inbound=GENERIC_REF_A,
        candidate_reply=NOT_FOUND_REPLY,
        previous_outbound=PRIOR_NOT_FOUND_OUTBOUND,
    )


# ── D. Safety controls ───────────────────────────────────────────────────


def test_social_near_duplicate_still_deduped_without_restore():
    history = _hist(
        {"direction": "inbound", "body": "صباح الخير"},
        {"direction": "outbound", "body": _SOCIAL_REPLY},
        {"direction": "inbound", "body": "صباح الخير"},
    )
    assert not should_restore_brain_reply_after_dedup_silence(
        current_inbound="صباح الخير",
        candidate_reply=_SOCIAL_REPLY,
        previous_outbound=_SOCIAL_REPLY,
    )


def test_commerce_inquiry_restore_unchanged():
    price_ask = "كم سعر القميص؟"
    candidate = "القميص متوفر بسعر 120 ريال."
    assert should_restore_brain_reply_after_dedup_silence(
        current_inbound=price_ask,
        candidate_reply=candidate,
        previous_outbound="صباح النور! 👋",
    )


@pytest.mark.parametrize(
    "module_path,attr",
    [
        ("core.inbound_dedup", "is_duplicate_inbound"),
        ("routers.whatsapp_webhook", "_max_outbound_overlap"),
    ],
)
def test_safety_helpers_still_importable(module_path, attr):
    mod = __import__(module_path, fromlist=[attr])
    assert callable(getattr(mod, attr))
