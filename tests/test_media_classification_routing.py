"""
tests/test_media_classification_routing.py
──────────────────────────────────────────
Integration coverage for the May 2026 media-classification hotfixes.

The production complaint these tests lock down:

  1) A Kaaba/Hajj greeting card image with OCR text
     "أهنئكم بقدوم عشر ذي الحجة ... ويبلغكم يوم النحر ..." was being
     mis-classified as ``INTENT_ASK_SHIPPING`` because the shipping
     rule pattern ``كم يوم`` matched the SUBSTRING "كم يوم" inside
     the word boundary "ويبلغكم يوم". The bot then replied with the
     ``faq_shipping()`` template ("بالنسبة للشحن: ... بعد اختيار
     المنتج المناسب") instead of letting the image flow to vision/
     brain for a natural greeting reply.

  2) A real ``Transaction-Receipt.pdf`` from a Saudi bank was being
     classified as ``pre_transfer_review`` because its body
     contained the section header "تأكيد التحويل" (which is ALSO
     the imperative on the pre-transfer button screen). The fix
     uses the filename as a tie-breaker: a clearly-named receipt
     artifact + body context = CONFIRMED unless the body contains
     an EXPLICIT imperative ("اضغط تحويل" / "Tap to transfer").

Every test in this module asserts the actual **route** the system
would take — not just a lexicon hit. The "route" is one of:

  * ``vision_brain``                — generic image, brain decides
  * ``map_short_circuit``           — map_screenshot in active order
  * ``receipt_short_circuit``       — confirmed receipt
  * ``payment_evidence_short_circuit`` — soft pre-review / pending
  * ``handoff_short_circuit``       — pre-brain handoff guard

These names mirror the ``final_route`` field emitted by the
``[MEDIA_CLASSIFY_TRACE]`` log line so a failing test can be
diagnosed by grepping production logs for the same key.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ──────────────────────────────────────────────────────────────────────
# 1. Shipping intent rule — must NOT fire on greeting card text
# ──────────────────────────────────────────────────────────────────────


class TestShippingIntentBoundaries:
    """Reproducer for the production regression where a Hajj greeting
    card image routed to the shipping FAQ."""

    GREETING_OCR = (
        "أهنئكم بقدوم عشر ذي الحجة، خير الأيام عند الله، "
        "تقبل الله منا ومنكم صالح الأعمال، وكتب لكم الأجر، "
        "ويبلغكم يوم النحر، وأسعدكم طول الدهر، كل عام وأنتم "
        "إلى الله أقرب، وعلى الطاعة أدوم، وعن النار أبعد، "
        "وإلى الجنة أسبق"
    )

    def test_hajj_greeting_does_not_match_shipping_intent(self) -> None:
        from modules.ai.brain.intent import rules as _rules

        result = _rules.match(self.GREETING_OCR)
        # The exact assertion: greeting card produces NO rule hit.
        # Brain LLM extractor takes over and replies naturally.
        assert result is None, (
            f"Greeting card MUST NOT match any rule, got "
            f"name={getattr(result, 'name', None)!r}"
        )

    def test_kam_yom_inside_word_does_not_fire_shipping(self) -> None:
        """Lock-in: the standalone substring 'كم يوم' inside any
        Arabic word (e.g. 'ويبلغكم يوم') no longer triggers the
        shipping duration rule."""
        from modules.ai.brain.intent import rules as _rules

        # Each of these contains "كم يوم" as a SUBSTRING inside
        # another Arabic word — must not match ASK_SHIPPING.
        false_positive_inputs = (
            "ويبلغكم يوم النحر",
            "حضرتكم يوم الجمعة",
            "نراكم يوم الأحد",
            "بلغكم يوم البشارة",
        )
        for text in false_positive_inputs:
            result = _rules.match(text)
            name = getattr(result, "name", None) if result else None
            assert name != "ask_shipping", (
                f"Greeting/social text {text!r} must NOT match "
                f"ask_shipping, got {name!r}"
            )

    def test_real_shipping_duration_questions_still_match(self) -> None:
        """Regression-safety: the tightening must NOT block real
        shipping-duration questions from matching."""
        from modules.ai.brain.intent import rules as _rules

        real_shipping_inputs = (
            "كم يوم يستغرق الشحن",
            "كم يوم للتوصيل",
            "كم يوم؟",
            "كم يوم تأخذ الشحنة",
            "مدة الشحن كم",
            "متى يوصل الطلب",
            "وشلون التوصيل عندكم",
            "طريقة التوصيل عندكم",
        )
        for text in real_shipping_inputs:
            result = _rules.match(text)
            name = getattr(result, "name", None) if result else None
            assert name == "ask_shipping", (
                f"Real shipping question {text!r} must match "
                f"ask_shipping, got {name!r}"
            )


# ──────────────────────────────────────────────────────────────────────
# 2. Payment-evidence classifier — filename + body interactions
# ──────────────────────────────────────────────────────────────────────


class TestTransactionReceiptPdfClassification:
    """Reproducer for the production regression where a bank-generated
    ``Transaction-Receipt.pdf`` was demoted from confirmed to
    pre_transfer_review because the body contained the passive header
    "تأكيد التحويل" — which is also the pre-transfer button label
    on the review screen."""

    def test_transaction_receipt_pdf_with_passive_confirmation_header_is_confirmed(self) -> None:
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_CONFIRMED,
        )
        # Body lifted from a real Al-Rajhi PDF receipt: header is
        # "تأكيد التحويل" (passive), followed by the completed-
        # transfer details. No imperative.
        body = (
            "تأكيد التحويل\n"
            "اسم المستفيد: متجر نهلة\n"
            "المبلغ: 360 ريال\n"
            "الراجحي\n"
        )
        v = classify_payment_evidence(
            body, filename="Transaction-Receipt.pdf",
        )
        assert v["status"] == PAYMENT_EVIDENCE_CONFIRMED
        assert v["reason"] in (
            "strong_success_phrase",
            "receipt_filename_with_payment_context",
            "weak_success_with_context",
            "reference_number_with_context",
        )

    def test_pre_review_body_with_non_receipt_filename_is_pre_review(self) -> None:
        """Genuine pre-transfer-review screen → still demoted."""
        from core.payment_evidence import (
            classify_payment_evidence,
            PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        body = "مراجعة بيانات التحويل\nالمستفيد: ...\nاضغط تحويل لإتمام العملية"
        v = classify_payment_evidence(body, filename="screenshot.jpg")
        assert v["status"] == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW

    def test_receipt_filename_but_explicit_imperative_stays_pre_review(self) -> None:
        """Edge case: filename says Receipt but body contains the
        explicit imperative "اضغط تحويل" — the imperative wins
        because no completed-transfer receipt ever prints that
        button label."""
        from core.payment_evidence import (
            classify_payment_evidence,
            PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        body = (
            "مراجعة قبل التحويل\n"
            "اسم المستفيد: متجر نهلة\n"
            "اضغط تحويل لإتمام العملية"
        )
        v = classify_payment_evidence(
            body, filename="Transaction-Receipt.pdf",
        )
        assert v["status"] == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW
        assert v["reason"] in (
            "pre_transfer_imperative_with_receipt_filename",
            "pre_transfer_review_phrase",
        )

    def test_receipt_filename_with_strong_success_marker_is_confirmed(self) -> None:
        """Smoke: filename + body that explicitly confirms the
        transfer → confirmed. This is the "Rajhi happy path"."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_CONFIRMED,
        )
        body = (
            "تم التحويل بنجاح\n"
            "المبلغ: 360 ريال\n"
            "رقم العملية: 9981234"
        )
        v = classify_payment_evidence(body, filename="Receipt-9981234.pdf")
        assert v["status"] == PAYMENT_EVIDENCE_CONFIRMED

    def test_unknown_filename_without_signals_is_not_payment(self) -> None:
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence("صورة عشوائية", filename="IMG_9981.jpg")
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT


# ──────────────────────────────────────────────────────────────────────
# 3. End-to-end image classification (NOT_PAYMENT + NOT_MAP → general)
# ──────────────────────────────────────────────────────────────────────


class TestImageRoutingToVisionBrain:
    """When the classifier returns ``not_payment`` AND no map markers
    are present, the image must NOT enter the order/payment/shipping
    short-circuits — those handlers must return ``None`` so the
    webhook proceeds with the vision/brain pipeline."""

    def test_greeting_image_payment_short_circuit_returns_none(self, monkeypatch) -> None:
        from core import order_flow

        monkeypatch.setattr(
            "core.order_flow._load_brain_state",
            lambda *_a, **_k: (
                None,
                {
                    "current_product_focus": {
                        "title": "عسل سدر", "price": 360, "currency": "SAR",
                    },
                    "order_prep": {"awaiting_payment_receipt": True},
                },
            ),
        )
        decision = order_flow.maybe_handle_payment_evidence_inbound(
            db=None,
            tenant_id=1,
            phone="+966500000050",
            inbound_normalized_type="image",
            inbound_metadata={
                "image_kind": None,
                "payment_evidence_status": "not_payment",
                "payment_evidence_reason": "greeting_or_social_content",
            },
        )
        assert decision is None

    def test_greeting_image_map_short_circuit_returns_none(self, monkeypatch) -> None:
        from core import order_flow

        monkeypatch.setattr(
            "core.order_flow._load_brain_state",
            lambda *_a, **_k: (
                None,
                {
                    "current_product_focus": {
                        "title": "عسل سدر", "price": 360, "currency": "SAR",
                    },
                    "order_prep": {"awaiting_payment_receipt": True},
                },
            ),
        )
        decision = order_flow.maybe_handle_map_image_inbound(
            db=None,
            tenant_id=1,
            phone="+966500000051",
            inbound_normalized_type="image",
            inbound_metadata={
                "image_kind": None,
            },
        )
        assert decision is None


# ──────────────────────────────────────────────────────────────────────
# 4. MEDIA_CLASSIFY_TRACE log line — required-field smoke
# ──────────────────────────────────────────────────────────────────────


class TestMediaClassifyTraceEmission:
    """The ``_emit_media_classify_trace`` helper must emit a single
    grep-able log line with EVERY required field. We exercise it
    twice — once for an image-side call, once for a document-side
    call — and parse the resulting log message to confirm the
    audit contract."""

    def _run(self, **kwargs):
        from modules.ai.media import normalizer as _nrm
        import logging

        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = _Handler(); h.setLevel(logging.DEBUG)
        prev_level = _nrm.logger.level
        prev_propagate = _nrm.logger.propagate
        _nrm.logger.setLevel(logging.DEBUG)
        _nrm.logger.propagate = True
        _nrm.logger.addHandler(h)
        try:
            _nrm._emit_media_classify_trace(**kwargs)
        finally:
            _nrm.logger.removeHandler(h)
            _nrm.logger.setLevel(prev_level)
            _nrm.logger.propagate = prev_propagate
        assert records, "expected one [MEDIA_CLASSIFY_TRACE] line"
        return records[0]

    def test_image_trace_carries_every_required_field(self) -> None:
        msg = self._run(
            tenant_id=42,
            media_id="wamid.ABC",
            media_type="image",
            filename=None,
            mime_type="image/jpeg",
            caption="هذي صورة",
            extracted_text_preview="تقبل الله منا ومنكم",
            image_kind=None,
            image_kind_confidence=None,
            image_kind_reasons=None,
            payment_evidence_status="not_payment",
            payment_evidence_reason="greeting_or_social_content",
            payment_evidence_signals={"greeting_hit": "تقبل الله منا ومنكم"},
            order_context={
                "conversation_id": 17,
                "awaiting_payment_receipt": False,
                "has_active_order": True,
            },
        )
        assert "[MEDIA_CLASSIFY_TRACE]" in msg
        for fragment in (
            "tenant=42",
            "conv=17",
            "media_id=wamid.ABC",
            "media_type=image",
            "image_kind=None",
            "payment_evidence_status=not_payment",
            "payment_evidence_reason=greeting_or_social_content",
            "map_status=not_map",
            "hard_negative_matched=True",
            "final_route=vision_brain",
        ):
            assert fragment in msg, f"missing required field {fragment!r}"

    def test_pdf_receipt_trace_carries_route_and_filename(self) -> None:
        msg = self._run(
            tenant_id=7,
            media_id="wamid.PDF",
            media_type="document",
            filename="Transaction-Receipt.pdf",
            mime_type="application/pdf",
            caption=None,
            extracted_text_preview="تم التحويل بنجاح المبلغ 360 ريال",
            pdf_kind="payment_receipt",
            pdf_kind_confidence="high",
            pdf_kind_reasons=["filename/caption/text matches receipt keyword"],
            payment_evidence_status="confirmed",
            payment_evidence_reason="strong_success_phrase",
            payment_evidence_signals={
                "success_hits": ["تم التحويل"], "context_hits": ["المبلغ"],
            },
            order_context={
                "conversation_id": 99,
                "awaiting_payment_receipt": True,
                "has_active_order": True,
            },
        )
        for fragment in (
            "tenant=7",
            "conv=99",
            "media_type=document",
            "filename='Transaction-Receipt.pdf'",
            "pdf_kind=payment_receipt",
            "payment_evidence_status=confirmed",
            "final_route=receipt_short_circuit",
        ):
            assert fragment in msg, f"missing required field {fragment!r}"

    def test_map_screenshot_trace_marks_route_as_map(self) -> None:
        msg = self._run(
            tenant_id=3,
            media_id="wamid.MAP",
            media_type="image",
            filename=None,
            mime_type="image/jpeg",
            caption="موقعي",
            extracted_text_preview="apple maps drop a pin",
            image_kind="map_screenshot",
            image_kind_confidence="high",
            image_kind_reasons=["map_marker:apple maps"],
            payment_evidence_status="not_payment",
            payment_evidence_reason="no_payment_signals",
            payment_evidence_signals={},
            order_context={"conversation_id": 1},
        )
        assert "map_status=map_screenshot" in msg
        assert "final_route=map_short_circuit" in msg

    def test_trace_never_raises_on_malformed_input(self) -> None:
        """Defensive: corrupted ``order_context`` (e.g. SQLAlchemy
        Mock returning unsubscriptable proxies) must not crash the
        normaliser. The trace is a passive observer."""
        from modules.ai.media import normalizer as _nrm

        # Should NOT raise:
        _nrm._emit_media_classify_trace(
            tenant_id=None,
            media_id=None,
            media_type="image",
            filename=None,
            mime_type=None,
            caption=None,
            extracted_text_preview=None,
            payment_evidence_signals={
                # Deliberately not-a-dict items inside the list.
                "success_hits": object(),
                "iban_present": object(),
            },
            order_context=object(),  # not even a dict
        )


# ──────────────────────────────────────────────────────────────────────
# 5. Shipping intent after the order exists — must defer to brain
#
# The exact production reproducer from the user's screenshot
# (May 2026): a customer with a paid/processing order asked
# "اي فرع ارسلتو طلبي في سمسا" — the bot replied with the static
# ``faq_shipping()`` template ("بعد اختيار المنتج المناسب…") instead
# of routing the question to the brain with order context.
#
# Resolution policy (per the user's explicit instruction):
#   * DO NOT add a new template / canned reply.
#   * DO NOT add a new "post-order tracking" intent / route.
#   * The decision engine simply REFUSES to use the canned shipping
#     FAQ when the conversation already has a paid / processing /
#     shipped order, and routes to ACTION_LLM_REPLY so the brain
#     writes the reply itself using the existing order context.
# ──────────────────────────────────────────────────────────────────────


class TestShippingIntentDefersToBrainAfterOrder:
    """Lock-in: ASK_SHIPPING + post-order context → brain, not FAQ."""

    @staticmethod
    def _build_post_order_ctx(
        *,
        payment_receipt_received: bool = False,
        order_status: str = "",
        product_focus: bool = False,
        city: str = "",
    ):
        """Build a minimal BrainContext that satisfies the engine
        check. Avoids importing every collaborator the real
        pipeline uses — we only need ``state.order_prep`` and
        ``state.current_product_focus`` for THIS decision branch."""
        from modules.ai.brain.types import (  # noqa: PLC0415
            BrainContext, OrderPreparationState,
            MerchantConversationState,
            Intent, INTENT_ASK_SHIPPING,
            CommerceFacts,
        )
        op = OrderPreparationState()
        op.payment_receipt_received = payment_receipt_received
        op.order_status = order_status
        if city:
            op.city = city
        state = MerchantConversationState()
        state.order_prep = op
        if product_focus:
            state.current_product_focus = {
                "id": "p1", "title": "عسل سدر", "price": 360,
                "currency": "SAR",
            }
        intent = Intent(
            name=INTENT_ASK_SHIPPING,
            confidence=0.90,
            slots={},
            raw_message="اي فرع ارسلتو طلبي في سمسا",
            extraction_method="rules",
        )
        return BrainContext(
            tenant_id=1,
            customer_phone="+966500000099",
            message="اي فرع ارسلتو طلبي في سمسا",
            intent=intent,
            state=state,
            facts=CommerceFacts(),
        )

    def test_paid_order_shipping_question_routes_to_brain_not_faq(self) -> None:
        """The exact production reproducer: payment_receipt_received=True,
        customer asks 'اي فرع ارسلتو طلبي في سمسا' → engine MUST NOT
        route to ACTION_FAQ_REPLY+topic=shipping."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import (
            ACTION_FAQ_REPLY, ACTION_LLM_REPLY,
        )

        ctx = self._build_post_order_ctx(payment_receipt_received=True)
        d = DefaultDecisionEngine().decide(ctx)
        assert d.action == ACTION_LLM_REPLY, (
            f"Expected brain handoff (ACTION_LLM_REPLY); got "
            f"action={d.action!r} args={d.args!r}"
        )
        assert d.args.get("topic") == "shipping_post_order"
        # And, critically, NOT the canned shipping FAQ:
        assert d.action != ACTION_FAQ_REPLY
        assert (d.args.get("topic") or "") != "shipping"

    def test_processing_order_shipping_question_routes_to_brain(self) -> None:
        """``order_status='processing'`` → same guard fires."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import (
            ACTION_FAQ_REPLY, ACTION_LLM_REPLY,
        )

        ctx = self._build_post_order_ctx(order_status="processing")
        d = DefaultDecisionEngine().decide(ctx)
        assert d.action == ACTION_LLM_REPLY
        assert d.action != ACTION_FAQ_REPLY

    def test_shipped_order_shipping_question_routes_to_brain(self) -> None:
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

        ctx = self._build_post_order_ctx(order_status="shipped")
        d = DefaultDecisionEngine().decide(ctx)
        assert d.action == ACTION_LLM_REPLY

    def test_pre_order_shipping_question_still_uses_faq(self) -> None:
        """Regression-safety: a customer with NO active order asking
        'كم سعر الشحن؟' MUST still get the canned shipping FAQ —
        the guard only kicks in AFTER an order exists. Otherwise we'd
        push the customer into a no-context brain reply for the
        legitimate 'do you ship?' question."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import (
            ACTION_FAQ_REPLY, ACTION_LLM_REPLY,
        )

        ctx = self._build_post_order_ctx()  # no signals at all
        d = DefaultDecisionEngine().decide(ctx)
        assert d.action == ACTION_FAQ_REPLY
        assert d.args.get("topic") == "shipping"

    def test_product_focus_plus_city_also_treated_as_post_order(self) -> None:
        """Customers who already gave the bot a product + city are
        effectively mid-order; tracking-style shipping questions
        should still defer to the brain."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

        ctx = self._build_post_order_ctx(
            product_focus=True, city="المدينة",
        )
        d = DefaultDecisionEngine().decide(ctx)
        assert d.action == ACTION_LLM_REPLY
