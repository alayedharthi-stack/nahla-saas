"""
tests/test_payment_relevance_gate.py
────────────────────────────────────
Platform invariant: unrelated multimodal inbound must never resurrect
stale payment workflows or dispatch payment artifacts.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.order_flow import context_aware_dedup_fallback
from core.payment_relevance_gate import (
    DISPATCH_EVIDENCE_PROMPT,
    evaluate_payment_relevance,
    is_visual_batch_context,
    outbound_text_is_payment_artifact,
    validate_payment_evidence_prompt,
    validate_payment_outbound_artifact,
    validate_payment_workflow_resume,
)


class TestVisualBatchDetection:
    def test_unrelated_product_image_is_visual_batch(self):
        msg = "[وصف الصورة المرسلة] canned black olives on purple label"
        meta = {
            "media_semantic_category": "product_image",
            "media_semantic_confidence": "medium",
            "source_type": "image",
        }
        assert is_visual_batch_context(
            message=msg,
            inbound_metadata=meta,
            normalized_type="image",
        )

    def test_explicit_payment_ask_not_visual_batch(self):
        assert not is_visual_batch_context(
            message="أبي باركود الراجحي",
            inbound_metadata={"source_type": "text"},
            normalized_type="text",
        )


class TestWorkflowResumeGate:
    def test_blocks_unrelated_image_while_awaiting_receipt(self):
        verdict = validate_payment_workflow_resume(
            message="[وصف الصورة المرسلة] crate of oranges",
            inbound_metadata={
                "media_semantic_category": "unknown_media",
                "media_semantic_confidence": "low",
                "source_type": "image",
            },
            normalized_type="image",
            state_summary={
                "awaiting_payment_receipt": True,
                "selected_product": "عسل سدر",
            },
            route="test",
        )
        assert not verdict.allowed
        assert verdict.visual_batch

    def test_allows_transfer_claim_text(self):
        verdict = validate_payment_workflow_resume(
            message="حولت الآن",
            state_summary={
                "awaiting_payment_receipt": True,
                "selected_product": "عسل",
            },
            route="test",
        )
        assert verdict.allowed

    def test_blocks_stale_receipt_received_state(self):
        verdict = validate_payment_workflow_resume(
            message="حولت",
            state_summary={
                "awaiting_payment_receipt": True,
                "payment_receipt_received": True,
                "selected_product": "عسل",
            },
            route="test",
        )
        assert not verdict.allowed
        assert not verdict.workflow_fresh


class TestOutboundArtifactGate:
    def test_blocks_barcode_without_payment_ask(self):
        verdict = validate_payment_outbound_artifact(
            message="[وصف الصورة المرسلة] loaf of bread packaging",
            inbound_metadata={
                "media_semantic_category": "product_image",
                "source_type": "image",
            },
            normalized_type="image",
            route="test",
        )
        assert not verdict.allowed

    def test_allows_explicit_barcode_request(self):
        verdict = validate_payment_outbound_artifact(
            message="أرسل لي باركود الراجحي",
            route="test",
        )
        assert verdict.allowed


class TestDedupFallbackIntegration:
    def test_dedup_does_not_resurrect_payment_for_unrelated_image(self):
        class _FakeDB:
            pass

        summary_state = {
            "awaiting_payment_receipt": True,
            "selected_product": "عسل سدر",
            "price": 120,
        }

        import core.order_flow as of

        def _fake_load(db, tenant_id, phone):
            return None, {"order_prep": summary_state}

        def _fake_focus(bs):
            return dict(summary_state)

        orig_load = of._load_brain_state
        orig_focus = of._focus_summary
        try:
            of._load_brain_state = _fake_load
            of._focus_summary = lambda bs: _fake_focus(bs)
            reply = context_aware_dedup_fallback(
                _FakeDB(),
                tenant_id=99,
                phone="966500000001",
                history=[],
                default_fallback="",
                inbound_text="[وصف الصورة المرسلة] black olives can",
                inbound_metadata={
                    "media_semantic_category": "unknown_media",
                    "media_semantic_confidence": "low",
                    "source_type": "image",
                },
                normalized_type="image",
            )
        finally:
            of._load_brain_state = orig_load
            of._focus_summary = orig_focus

        assert "بانتظار إيصال التحويل" not in reply
        assert "كيف أقدر أساعدك؟" not in reply

    def test_dedup_still_blocks_commerce_query_via_gate(self):
        class _FakeDB:
            pass

        summary_state = {
            "awaiting_payment_receipt": True,
            "selected_product": "عسل سدر",
            "price": 120,
        }

        import core.order_flow as of

        def _fake_load(db, tenant_id, phone):
            return None, {"order_prep": summary_state}

        orig_load = of._load_brain_state
        orig_focus = of._focus_summary
        try:
            of._load_brain_state = _fake_load
            of._focus_summary = lambda bs: dict(summary_state)
            reply = context_aware_dedup_fallback(
                _FakeDB(),
                tenant_id=99,
                phone="966500000001",
                history=[],
                default_fallback="كيف أقدر أساعدك؟",
                inbound_text="كل الحجام",
            )
        finally:
            of._load_brain_state = orig_load
            of._focus_summary = orig_focus

        assert "بانتظار إيصال التحويل" not in reply


class TestOutboundTextDetection:
    def test_receipt_reminder_marker(self):
        assert outbound_text_is_payment_artifact(
            "أنا بانتظار إيصال التحويل بإذنك — أرسله هنا (صورة أو PDF)"
        )


class TestEvidencePromptGate:
    def test_blocks_unrelated_multimodal_evidence_prompt(self):
        verdict = evaluate_payment_relevance(
            message="",
            inbound_metadata={
                "media_semantic_category": "product_image",
                "payment_evidence_status": "needs_confirmation",
                "source_type": "image",
            },
            normalized_type="image",
            state_summary={
                "awaiting_payment_receipt": True,
                "selected_product": "عسل",
            },
            dispatch_kind="evidence_prompt",
            route="test",
        )
        assert not verdict.allowed

    def test_allows_deterministic_pre_review_document_evidence_prompt(self):
        meta = {
            "pdf_kind": "payment_pre_review",
            "payment_evidence_status": "pre_transfer_review",
        }
        assert not is_visual_batch_context(
            message="",
            inbound_metadata=meta,
            normalized_type="document",
        )
        verdict = validate_payment_evidence_prompt(
            message="",
            inbound_metadata=meta,
            normalized_type="document",
            state_summary={
                "awaiting_payment_receipt": False,
                "selected_product": "عسل",
            },
            route="payment_evidence_inbound",
        )
        assert verdict.allowed
        assert verdict.dispatch_kind == DISPATCH_EVIDENCE_PROMPT


class TestShortContinuationPaymentFlow:
    def test_short_payment_ack_allowed_by_gate_during_awaiting(self):
        verdict = validate_payment_workflow_resume(
            message="تمام",
            state_summary={
                "awaiting_payment_receipt": True,
                "selected_product": "عسل",
            },
            route="short_ack",
        )
        assert verdict.allowed


class TestPaymentRelevanceGateLogging:
    def test_unrelated_image_logs_deny_with_dedup_context(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="nahla.payment_relevance_gate")

        from core.payment_relevance_gate import (
            PaymentRelevanceLogContext,
            validate_payment_workflow_resume,
        )

        validate_payment_workflow_resume(
            message="[وصف الصورة المرسلة] crate of oranges",
            inbound_metadata={
                "media_semantic_category": "unknown_media",
                "media_semantic_confidence": "low",
                "source_type": "image",
            },
            normalized_type="image",
            state_summary={
                "awaiting_payment_receipt": True,
                "selected_product": "SKU-1",
            },
            tenant_id=33,
            route="dedup_fallback",
            log_context=PaymentRelevanceLogContext(
                tenant_id=33,
                phone_tail="0001",
                dedup=True,
                fallback_source="dedup_fallback",
                artifact=False,
                final_action="dedup_payment_resume_check",
            ),
        )

        lines = [r.message for r in caplog.records if "[PAYMENT_RELEVANCE_GATE]" in r.message]
        assert len(lines) == 1
        line = lines[0]
        assert "tenant=33" in line
        assert "kind=workflow_resume" in line
        assert "allow=false" in line
        assert "visual_batch=true" in line
        assert "reason=" in line
        assert "media=unknown_media" in line
        assert "receipt_confidence=low" in line
        assert "payment_semantics=false" in line
        assert "dedup=true" in line
        assert "fallback=dedup_fallback" in line
        assert "artifact=false" in line
        assert "preview=" in line

    def test_explicit_payment_request_logs_allow(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="nahla.payment_relevance_gate")

        from core.payment_relevance_gate import (
            PaymentRelevanceLogContext,
            validate_payment_outbound_artifact,
        )

        validate_payment_outbound_artifact(
            message="أرسل لي باركود الراجحي",
            tenant_id=33,
            route="outbound_consent",
            log_context=PaymentRelevanceLogContext(
                tenant_id=33,
                fallback_source="outbound_consent",
                artifact=True,
                final_action="dispatch_payment_artifact",
            ),
        )

        line = next(
            r.message for r in caplog.records if "[PAYMENT_RELEVANCE_GATE]" in r.message
        )
        assert "allow=true" in line
        assert "kind=outbound_artifact" in line
        assert "payment_semantics=true" in line
        assert "artifact=true" in line

    def test_receipt_inbound_blocks_artifact_and_logs_reason(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="nahla.payment_relevance_gate")

        from core.payment_relevance_gate import (
            PaymentRelevanceLogContext,
            validate_payment_outbound_artifact,
        )

        validate_payment_outbound_artifact(
            message="",
            inbound_metadata={
                "image_kind": "payment_receipt",
                "media_semantic_category": "payment_receipt",
                "payment_evidence_status": "confirmed",
                "source_type": "image",
            },
            normalized_type="image",
            tenant_id=33,
            route="post_compose_barcode",
            log_context=PaymentRelevanceLogContext(
                tenant_id=33,
                fallback_source="post_compose_barcode",
                artifact=True,
                final_action="dispatch_barcode",
            ),
        )

        line = next(
            r.message for r in caplog.records if "[PAYMENT_RELEVANCE_GATE]" in r.message
        )
        assert "allow=false" in line
        assert "reason=receipt_inbound_no_outbound" in line
        assert "artifact=false" in line
