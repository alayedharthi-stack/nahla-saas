"""
tests/test_answer_alignment.py
──────────────────────────────
Locks the answer-alignment validator (May 2026 #12).

Validator contract
──────────────────
* Detects four mismatch shapes drawn from real merchant screenshots:
    1. ``question_to_social``   — substantive question answered by a
       purely social ack ("ما تقصر أبداً وياك").
    2. ``closing_to_reopen``    — polite close answered by "وش الخدمة".
    3. ``religious_to_oos``     — dua / blessing answered by the
       out-of-scope template ("ما أقدر أساعدك في هذا الموضوع").
    4. ``delivery_to_receipt``  — package-delivery confirmation
       answered by payment-receipt copy.
* Defaults to LOG-ONLY mode. Regeneration is controlled by the
  ``BRAIN_ALIGNMENT_REGEN`` env flag.
* Never raises into the pipeline.

Closing-context disqualifier (social_classifier)
────────────────────────────────────────────────
Same commit ships the closing-context disqualifier in
``social_classifier`` — covered here for a single PR-aligned suite.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── 1. Validator: question → social ────────────────────────────────────


class TestQuestionToSocialMismatch:
    def test_screenshot_reproducer(self):
        """Customer asks about product benefits; bot replies with a
        pure social ack. Mismatch must fire."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="هو ممتاز لمشاكل البطن والجهاز الهضمي؟",
            reply="ما تقصر أبدًا ❤️ ويّاك.",
        )
        assert result.passed is False
        assert result.mismatch_type == "question_to_social"

    def test_question_with_informational_reply_passes(self):
        """Same inbound but the reply actually answers — no flag."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="هو ممتاز لمشاكل البطن والجهاز الهضمي؟",
            reply=(
                "الله يعافيك 🌷 نعم، يستخدم لتهدئة المعدة وتحسين الهضم. "
                "السعر 79 ريال. تأمر بشيء؟"
            ),
        )
        assert result.passed is True

    def test_pure_social_inbound_with_social_reply_passes(self):
        """No question signal in inbound → no mismatch even if reply
        is purely social. Otherwise we'd block legitimate ACKs."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="الله يعطيك العافية",
            reply="ما تقصر أبدًا ويّاك ❤️",
        )
        assert result.passed is True


# ── 2. Validator: closing → reopen ─────────────────────────────────────


class TestClosingToReopenMismatch:
    def test_screenshot_reproducer(self):
        """'على خير إن شاء الله' → 'الله يحييك وش الخدمة؟' must be
        flagged."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="على خير إن شاء الله",
            reply="الله يحييك 🌹 وش الخدمة؟",
        )
        assert result.passed is False
        assert result.mismatch_type == "closing_to_reopen"

    def test_closing_with_brief_close_passes(self):
        """A brief mirroring close — no funnel reopen — passes."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="على خير إن شاء الله",
            reply="على خير 🌷 الله يحفظك",
        )
        assert result.passed is True

    def test_greeting_with_reopen_does_not_fire(self):
        """No closing signal in inbound → reopen reply is fine."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="السلام عليكم",
            reply="وعليكم السلام، وش الخدمة؟",
        )
        assert result.passed is True


# ── 3. Validator: religious → OOS ──────────────────────────────────────


class TestReligiousToOOSMismatch:
    def test_dua_image_caption_reproducer(self):
        """Religious blessing inbound + OOS template → flagged."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="اللهم صل وسلم على نبينا محمد",
            reply=(
                "ما أقدر أساعدك في هذا الموضوع، لكني معك في كل ما "
                "يخص العسل والطلبات 🌷"
            ),
        )
        assert result.passed is False
        assert result.mismatch_type == "religious_to_oos"

    def test_religious_with_proper_ack_passes(self):
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="اللهم صل وسلم على نبينا محمد",
            reply="آمين، الله يتقبل منا ومنك 🌷",
        )
        assert result.passed is True

    def test_unrelated_inbound_with_oos_reply_passes(self):
        """OOS reply is appropriate for actually-OOS inbound."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="ابغى مساعدة في برمجة موقع",
            reply=(
                "ما أقدر أساعدك في هذا الموضوع، لكني معك في كل ما "
                "يخص العسل والطلبات 🌷"
            ),
        )
        assert result.passed is True


# ── 4. Validator: delivery → receipt ───────────────────────────────────


class TestDeliveryToReceiptMismatch:
    def test_screenshot_reproducer(self):
        """Customer confirms package arrival; bot replies about a
        money receipt. Must flag."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="وصل الله يوصل في عمرك بوهشام اليوم اخذته",
            reply=(
                "وصل الإيصال، وسيتم متابعة الطلب وتجهيز الشحن "
                "للهفوف بإذن الله 🚚"
            ),
            order_status="awaiting_receipt",
            awaiting_payment_receipt=True,
        )
        assert result.passed is False
        assert result.mismatch_type == "delivery_to_receipt"

    def test_real_transfer_reply_passes(self):
        """Customer explicitly mentions a transfer → receipt reply is
        legitimate, no mismatch."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="تم التحويل اليوم، ارسلت لك الإيصال",
            reply="وصل الإيصال، شكراً لك 🌷 جاري المراجعة الآن.",
            order_status="awaiting_receipt",
            awaiting_payment_receipt=True,
        )
        assert result.passed is True

    def test_delivery_with_natural_reply_passes(self):
        """Same inbound, but reply correctly thanks the customer for
        confirming arrival → no mismatch."""
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        result = check_alignment(
            last_user_message="وصل الله يوصل في عمرك بوهشام اليوم اخذته",
            reply=(
                "الله يسلّمك بومحمد 🌷 يا هلا، عساه مبارك عليك "
                "وبالعافية يا رب"
            ),
        )
        assert result.passed is True


# ── 5. Hard guarantees ────────────────────────────────────────────────


class TestValidatorIsExceptionSafe:
    def test_empty_inputs_pass(self):
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        assert check_alignment(last_user_message="", reply="").passed is True
        assert check_alignment(last_user_message=None, reply=None).passed is True

    def test_invalid_types_pass(self):
        from modules.ai.brain.postprocess.answer_alignment import check_alignment
        # Should never raise even with weird inputs.
        assert check_alignment(last_user_message=123, reply=[1, 2]).passed is True
        assert check_alignment(last_user_message={}, reply=None).passed is True


class TestRegenFlagDefaultsToLogOnly:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("BRAIN_ALIGNMENT_REGEN", raising=False)
        from modules.ai.brain.postprocess.answer_alignment import regen_enabled
        assert regen_enabled() is False

    def test_explicit_off(self, monkeypatch):
        for v in ("", "0", "false", "no", "off"):
            monkeypatch.setenv("BRAIN_ALIGNMENT_REGEN", v)
            from modules.ai.brain.postprocess.answer_alignment import regen_enabled
            assert regen_enabled() is False, f"value {v!r} must be off"

    def test_explicit_on(self, monkeypatch):
        for v in ("1", "true", "yes", "on"):
            monkeypatch.setenv("BRAIN_ALIGNMENT_REGEN", v)
            from modules.ai.brain.postprocess.answer_alignment import regen_enabled
            assert regen_enabled() is True, f"value {v!r} must be on"


class TestEmitMismatchLogIsSafe:
    def test_passed_result_logs_nothing(self, caplog):
        import logging
        from modules.ai.brain.postprocess.answer_alignment import (
            AlignmentResult, emit_mismatch_log,
        )
        with caplog.at_level(logging.WARNING, logger="nahla.brain.postprocess.alignment"):
            emit_mismatch_log(
                tenant_id=1, phone="+966500000001", turn=3,
                last_user_message="x", reply="y",
                result=AlignmentResult(passed=True),
            )
        assert not [r for r in caplog.records if "[ALIGN_MISMATCH]" in r.getMessage()]

    def test_failed_result_emits_single_warning(self, caplog):
        import logging
        from modules.ai.brain.postprocess.answer_alignment import (
            AlignmentResult, emit_mismatch_log,
        )
        with caplog.at_level(logging.WARNING, logger="nahla.brain.postprocess.alignment"):
            emit_mismatch_log(
                tenant_id=1, phone="+966500000001", turn=3,
                last_user_message="هل ممتاز؟", reply="ما تقصر أبداً وياك",
                result=AlignmentResult(
                    passed=False,
                    mismatch_type="question_to_social",
                    reason="test",
                ),
                intent_name="social", action="ACTION_SOCIAL_REPLY",
                order_status="", awaiting_payment_receipt=False,
                regen_will_fire=False,
            )
        msgs = [r.getMessage() for r in caplog.records if "[ALIGN_MISMATCH]" in r.getMessage()]
        assert len(msgs) == 1
        assert "question_to_social" in msgs[0]
        assert "regen=False" in msgs[0]


# ── 6. Closing-context disqualifier in social_classifier ──────────────


class TestSocialClassifierClosingDisqualifier:
    def test_pure_close_yields_to_brain(self):
        """'على خير إن شاء الله' must NOT classify as social — yields
        to the brain pipeline so the stance detector marks it
        ``STANCE_POLITE_CLOSE`` and the LLM mirrors the close
        without the "وش الخدمة" reopener."""
        from modules.ai.brain.intent.social_classifier import classify_social
        for msg in (
            "على خير إن شاء الله",
            "علي خير ان شاء الله",
            "خلاص شكرا",
            "تكفينا الحين",
            "تمام كذا",
            "بس كذا شكراً",
            "في امان الله",
            "بحفظ الله",
            "مع السلامة",
        ):
            assert classify_social(msg) is None, (
                f"closing message must yield to brain pipeline: {msg!r}"
            )

    def test_thanks_blessing_compliment_still_classify(self):
        """Pure social phrases that do NOT contain a closing token
        must still classify as social. The closing disqualifier
        must not over-reach.

        (Greetings — "السلام عليكم", "صباح الخير" etc. — are routed
        via the regex-driven ``INTENT_GREETING`` rule, not through
        ``classify_social`` itself, so we test thanks / blessings /
        compliments here.)"""
        from modules.ai.brain.intent.social_classifier import classify_social
        for msg in (
            "شكراً",
            "شكرا لك",
            "الله يعطيك العافية",
            "ربي يعطيك العافية",
            "جزاك الله خير",
            "بيض الله وجهك",
        ):
            assert classify_social(msg) is not None, (
                f"thanks/blessing/compliment must still classify: {msg!r}"
            )


# ── 7. Pipeline wire-up smoke test ────────────────────────────────────


class TestPipelineWireUp:
    def test_alignment_module_importable_from_pipeline_path(self):
        """Sanity check — the import path used in pipeline.py
        actually resolves so we never break a turn on import error."""
        from modules.ai.brain.postprocess.answer_alignment import (  # noqa: F401
            check_alignment, regen_enabled, emit_mismatch_log,
            AlignmentResult,
        )
        # If we got here the import worked.
        assert True

    def test_pipeline_invokes_alignment(self):
        """Verify pipeline.py actually wires the alignment check.
        We read the source file directly to dodge sys.path pollution
        from other test modules."""
        src = (REPO_ROOT / "backend" / "modules" / "ai" / "brain" / "pipeline.py").read_text(encoding="utf-8")
        assert "answer_alignment" in src, (
            "pipeline.py must import from answer_alignment"
        )
        assert "check_alignment(" in src, (
            "pipeline.py must invoke check_alignment(...)"
        )
        assert "[ALIGN_MISMATCH]" in src, (
            "pipeline.py must emit ALIGN_MISMATCH log"
        )
