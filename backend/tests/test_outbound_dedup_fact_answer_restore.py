"""Distinct customer turns must not be silenced by assistant-text similarity.

CHAT_DEDUP hard overlap may still fire. Restore must keep the already-composed
FactAnswer / UNKNOWN candidate. Provider send dedup must bind to inbound
message identity when present. Same inbound id replay stays single-execution.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.inbound_dedup import is_duplicate_inbound, reset_cache  # noqa: E402
from core.outbound_dedup import (  # noqa: E402
    _payload_signature,
    check_outbound_send,
    clear_outbound_dedup,
    record_outbound_result,
)
from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: E402
    _inbound_is_availability_or_commerce_inquiry,
    should_restore_brain_reply_after_dedup_silence,
)
from modules.ai.brain.commerce.fact_answer import (  # noqa: E402
    KIND_SHIPPING_ETA,
    classify_fact_answer,
    fact_answer_owns_non_catalog_turn,
)
from routers.whatsapp_webhook import (  # noqa: E402
    _DEDUP_HARD_OVERLAP_THRESHOLD,
    _max_outbound_overlap,
)
from services.merchant_brain_turn import _apply_outbound_dedup  # noqa: E402

_ETA_INBOUND = "كم يستغرق الشحن؟"
_ETA_PARAPHRASE = "كم مدة الشحن؟"
_FOLLOW_INBOUND = "طيب كم ياخذ عادة؟"
_CARRIER_INBOUND = "أي شركة توصلون معها؟"
_FOLLOW_OUTBOUND = "عذرًا، ما عندي معلومات مؤكدة عن مدة التوصيل حاليًا ✨🚚"
_ETA_OUTBOUND = "عذرًا، ما عندي معلومات مؤكدة عن مدة الشحن حاليًا"
_FEE_OUTBOUND = (
    "عذرًا، ما عندي معلومات مؤكدة عن تكلفة الشحن حاليًا. "
    "إذا تحب، أقدر أساعدك في أي شيء ثاني! 😊"
)
_CARRIER_OUTBOUND = "نحن نوصل مع شركة **Dev Company** 🛒✨"
_UNKNOWN_SHOE = "عذرًا، ما عندي معلومات مؤكدة عن مدة الشحن حاليًا"


def _text_payload(body: str, inbound_id: str | None = None) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "to": "966555906901",
        "type": "text",
        "text": {"body": body},
    }
    if inbound_id:
        payload["_nahla_inbound_id"] = inbound_id
    return payload


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    clear_outbound_dedup()
    reset_cache()
    yield
    clear_outbound_dedup()
    reset_cache()


class TestFactAnswerEtaRestoreOwnership:
    def test_standalone_eta_is_fact_answer_not_commerce_inquiry(self) -> None:
        req = classify_fact_answer(_ETA_INBOUND)
        assert req is not None
        assert req.fact_kind == KIND_SHIPPING_ETA
        assert fact_answer_owns_non_catalog_turn(_ETA_INBOUND)
        assert not _inbound_is_availability_or_commerce_inquiry(_ETA_INBOUND)

    def test_restore_keeps_composed_unknown_for_eta_turn(self) -> None:
        assert should_restore_brain_reply_after_dedup_silence(
            current_inbound=_ETA_INBOUND,
            candidate_reply=_ETA_OUTBOUND,
            previous_outbound=_FOLLOW_OUTBOUND,
        )
        assert should_restore_brain_reply_after_dedup_silence(
            current_inbound=_ETA_PARAPHRASE,
            candidate_reply=_ETA_OUTBOUND,
            previous_outbound=_FEE_OUTBOUND,
        )

    def test_generic_commerce_unknown_also_restores(self) -> None:
        assert should_restore_brain_reply_after_dedup_silence(
            current_inbound="كم يستغرق توصيل الحذاء الرياضي الأبيض؟",
            candidate_reply=_UNKNOWN_SHOE,
            previous_outbound=_FOLLOW_OUTBOUND,
        )

    def test_pure_greeting_still_does_not_restore(self) -> None:
        assert not should_restore_brain_reply_after_dedup_silence(
            current_inbound="صباح الخير",
            candidate_reply="صباح النور! 👋",
            previous_outbound="صباح النور! 👋 🌿",
        )


class TestHardOverlapDoesNotAuthorizeSilence:
    def test_followup_then_standalone_hard_overlaps(self) -> None:
        history = [
            {"direction": "inbound", "body": _CARRIER_INBOUND},
            {"direction": "outbound", "body": _CARRIER_OUTBOUND},
            {"direction": "inbound", "body": _FOLLOW_INBOUND},
            {"direction": "outbound", "body": _FOLLOW_OUTBOUND},
        ]
        overlap = _max_outbound_overlap(_ETA_OUTBOUND, history)
        assert overlap >= _DEDUP_HARD_OVERLAP_THRESHOLD

    def test_same_question_twice_hard_overlaps(self) -> None:
        history = [
            {"direction": "inbound", "body": _ETA_INBOUND},
            {"direction": "outbound", "body": _ETA_OUTBOUND},
        ]
        overlap = _max_outbound_overlap(_ETA_OUTBOUND, history)
        assert overlap >= _DEDUP_HARD_OVERLAP_THRESHOLD

    def test_paraphrase_pair_hard_overlaps(self) -> None:
        history = [
            {"direction": "inbound", "body": _ETA_INBOUND},
            {"direction": "outbound", "body": _ETA_OUTBOUND},
        ]
        paraphrase_reply = "عذرًا، ما عندي معلومات مؤكدة عن مدة الشحن حاليًا"
        overlap = _max_outbound_overlap(paraphrase_reply, history)
        assert overlap >= _DEDUP_HARD_OVERLAP_THRESHOLD

    def test_live_batch_fee_then_eta_hard_overlaps(self) -> None:
        history = [
            {"direction": "outbound", "body": _FEE_OUTBOUND},
        ]
        overlap = _max_outbound_overlap(_ETA_OUTBOUND, history)
        assert overlap >= _DEDUP_HARD_OVERLAP_THRESHOLD


def _dedup_apply(inbound: str, candidate: str, history: list) -> tuple[str, str]:
    with patch(
        "services.merchant_brain_turn._dedup_operational_substitute",
        return_value="",
    ):
        return _apply_outbound_dedup(
            db=None,
            tenant_id=1,
            to="966555906901",
            text=inbound,
            reply=candidate,
            history=history,
            inbound_metadata={"normalized_type": "text"},
            brain_handoff=False,
            brain_active=True,
            relational_moment="",
            convo=SimpleNamespace(id=9),
            persona_ownership=SimpleNamespace(),
            brain_persona_compose_event=None,
        )


class TestApplyOutboundDedupRestoresFactAnswer:
    def test_same_eta_question_twice_both_keep_candidate(self) -> None:
        first_history = [
            {"direction": "inbound", "body": _ETA_INBOUND},
        ]
        reply1, suppressor1 = _dedup_apply(_ETA_INBOUND, _ETA_OUTBOUND, first_history)
        assert reply1 == _ETA_OUTBOUND
        assert suppressor1 == ""

        second_history = first_history + [
            {"direction": "outbound", "body": _ETA_OUTBOUND},
            {"direction": "inbound", "body": _ETA_INBOUND},
        ]
        reply2, suppressor2 = _dedup_apply(_ETA_INBOUND, _ETA_OUTBOUND, second_history)
        assert reply2 == _ETA_OUTBOUND
        assert suppressor2 == ""

    def test_paraphrase_eta_keeps_candidate(self) -> None:
        history = [
            {"direction": "inbound", "body": _ETA_INBOUND},
            {"direction": "outbound", "body": _ETA_OUTBOUND},
            {"direction": "inbound", "body": _ETA_PARAPHRASE},
        ]
        reply, suppressor = _dedup_apply(
            _ETA_PARAPHRASE, _ETA_OUTBOUND, history,
        )
        assert reply == _ETA_OUTBOUND
        assert suppressor == ""

    def test_followup_then_standalone_keeps_candidate(self) -> None:
        history = [
            {"direction": "inbound", "body": _CARRIER_INBOUND},
            {"direction": "outbound", "body": _CARRIER_OUTBOUND},
            {"direction": "inbound", "body": _FOLLOW_INBOUND},
            {"direction": "outbound", "body": _FOLLOW_OUTBOUND},
            {"direction": "inbound", "body": _ETA_INBOUND},
        ]
        reply, suppressor = _dedup_apply(_ETA_INBOUND, _ETA_OUTBOUND, history)
        assert reply == _ETA_OUTBOUND
        assert suppressor == ""

    def test_social_duplicate_still_silenced(self) -> None:
        social = "صباح النور يا هلا والله يسعدك نورتنا اليوم كيف أقدر أساعدك"
        history = [
            {"direction": "inbound", "body": "صباح الخير"},
            {"direction": "outbound", "body": social},
            {"direction": "inbound", "body": "صباح الخير"},
        ]
        reply, suppressor = _dedup_apply("صباح الخير", social, history)
        assert reply == ""
        assert suppressor == "chat_dedup_hard"


class TestSendDedupBindsInboundIdentity:
    def test_distinct_inbound_ids_same_body_both_send(self) -> None:
        body = _ETA_OUTBOUND
        first = check_outbound_send(
            tenant_id=1,
            recipient="966555906901",
            payload=_text_payload(body, "wamid.eta.turn1"),
        )
        assert first.skip is False
        record_outbound_result(
            tenant_id=1,
            recipient="966555906901",
            payload=_text_payload(body, "wamid.eta.turn1"),
            wamid="wamid.out.1",
            succeeded=True,
        )
        second = check_outbound_send(
            tenant_id=1,
            recipient="966555906901",
            payload=_text_payload(body, "wamid.eta.turn2"),
        )
        assert second.skip is False
        assert _payload_signature(_text_payload(body, "wamid.eta.turn1")) != (
            _payload_signature(_text_payload(body, "wamid.eta.turn2"))
        )

    def test_same_inbound_id_replay_still_skipped(self) -> None:
        body = _ETA_OUTBOUND
        payload = _text_payload(body, "wamid.eta.replay")
        first = check_outbound_send(
            tenant_id=1, recipient="966555906901", payload=payload,
        )
        assert first.skip is False
        record_outbound_result(
            tenant_id=1,
            recipient="966555906901",
            payload=payload,
            wamid="wamid.out.replay",
            succeeded=True,
        )
        second = check_outbound_send(
            tenant_id=1, recipient="966555906901", payload=payload,
        )
        assert second.skip is True
        assert second.reason == "already_sent"

    def test_missing_inbound_id_still_body_hashes(self) -> None:
        body = _ETA_OUTBOUND
        first = check_outbound_send(
            tenant_id=1,
            recipient="966555906901",
            payload=_text_payload(body),
        )
        assert first.skip is False
        record_outbound_result(
            tenant_id=1,
            recipient="966555906901",
            payload=_text_payload(body),
            wamid="wamid.out.body",
            succeeded=True,
        )
        second = check_outbound_send(
            tenant_id=1,
            recipient="966555906901",
            payload=_text_payload(body),
        )
        assert second.skip is True


class TestInboundWebhookIdempotency:
    def test_same_provider_message_id_is_duplicate(self) -> None:
        assert is_duplicate_inbound(
            phone_number_id="PNID1", msg_id="wamid.eta.same",
        ) is False
        assert is_duplicate_inbound(
            phone_number_id="PNID1", msg_id="wamid.eta.same",
        ) is True

    def test_distinct_provider_message_ids_are_not_duplicates(self) -> None:
        assert is_duplicate_inbound(
            phone_number_id="PNID1", msg_id="wamid.eta.a",
        ) is False
        assert is_duplicate_inbound(
            phone_number_id="PNID1", msg_id="wamid.eta.b",
        ) is False
