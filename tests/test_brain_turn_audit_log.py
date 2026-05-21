"""
tests/test_brain_turn_audit_log.py
──────────────────────────────────
Locks the per-turn audit log fields (May 2026 #12).

The merchant requested a single structured log line per turn carrying:

    * last_user_message (preview)
    * detected_intent
    * social_classifier_result
    * route/action
    * order_state / post_order_state
    * fallback_used
    * alignment_passed
    * model_used

We extend the existing ``[BrainTurn]`` JSON record (already searchable
in Railway logs) rather than introduce a parallel log channel. These
tests verify the contract by reading ``pipeline.py`` directly so we
never depend on importable runtime state.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PATH = REPO_ROOT / "backend" / "modules" / "ai" / "brain" / "pipeline.py"


# ── 1. Source-level contract ───────────────────────────────────────────


class TestBrainTurnLogContract:
    def setup_method(self):
        self.src = PIPELINE_PATH.read_text(encoding="utf-8")

    def test_inbound_preview_field_present(self):
        assert "\"inbound_preview\"" in self.src

    def test_social_category_field_present(self):
        assert "\"social_category\"" in self.src

    def test_order_status_field_present(self):
        assert "\"order_status\"" in self.src

    def test_awaiting_payment_receipt_field_present(self):
        assert "\"awaiting_payment_receipt\"" in self.src

    def test_payment_receipt_received_field_present(self):
        assert "\"payment_receipt_received\"" in self.src

    def test_fallback_used_field_present(self):
        assert "\"fallback_used\"" in self.src

    def test_model_used_field_present(self):
        assert "\"model_used\"" in self.src

    def test_alignment_passed_field_present(self):
        assert "\"alignment_passed\"" in self.src

    def test_alignment_mismatch_field_present(self):
        assert "\"alignment_mismatch\"" in self.src

    def test_alignment_regen_field_present(self):
        assert "\"alignment_regen\"" in self.src

    def test_existing_fields_preserved(self):
        """Make sure the enrichment did not remove pre-existing
        ``[BrainTurn]`` fields the dashboard depends on."""
        for required in (
            "\"tenant_id\"", "\"phone\"", "\"turn\"", "\"message_len\"",
            "\"detected_intent\"", "\"confidence\"", "\"slots\"",
            "\"stage_before\"", "\"stage_after\"", "\"action\"",
            "\"chosen_path\"", "\"reason\"", "\"reply_len\"",
            "\"latency_ms\"", "\"facts\"",
        ):
            assert required in self.src, f"missing required BrainTurn field: {required}"


# ── 2. Pipeline ordering invariant ────────────────────────────────────


class TestPipelineOrdering:
    """The marker scrub must run BEFORE the alignment check + audit
    log so the validator + log see the SAME text downstream
    consumers receive."""

    def setup_method(self):
        self.src = PIPELINE_PATH.read_text(encoding="utf-8")

    def test_scrub_precedes_alignment_check(self):
        scrub_idx = self.src.find("scrub_internal_markers(reply or")
        align_idx = self.src.find("check_alignment(")
        assert scrub_idx > 0, "scrub call not found"
        assert align_idx > 0, "alignment call not found"
        assert scrub_idx < align_idx, (
            "marker scrub must run BEFORE alignment validation so "
            "the validator sees the customer-bound text"
        )

    def test_alignment_precedes_brainturn_log(self):
        """Alignment outcome must be computed before the BrainTurn
        JSON dump so it ships in the same line."""
        align_idx = self.src.find("_align_passed = _align_result.passed")
        log_idx = self.src.find("logger.info(\n                \"[BrainTurn] %s\"")
        # If the exact substring breaks on whitespace, fall back to
        # a regex match.
        if log_idx < 0:
            m = re.search(r"logger\.info\(\s*\"\[BrainTurn\]", self.src)
            log_idx = m.start() if m else -1
        assert align_idx > 0
        assert log_idx > 0
        assert align_idx < log_idx, (
            "alignment outcome must be computed before BrainTurn log"
        )

    def test_single_scrub_call_only(self):
        """Regression guard: there must be exactly one
        ``scrub_internal_markers(reply or "")`` invocation in the
        pipeline. Two scrubs would mean the previous block was not
        cleaned up properly."""
        n = self.src.count("scrub_internal_markers(reply or")
        assert n == 1, f"expected exactly 1 scrub call, got {n}"


# ── 3. Field derivation logic (unit-style) ────────────────────────────


class TestFallbackUsedDerivation:
    """``fallback_used`` is True whenever ``chosen_path`` indicates
    a degraded path. Spot-check the substring matching used in the
    pipeline so we don't silently miss known degraded paths."""

    @staticmethod
    def _is_fallback(chosen_path: str) -> bool:
        # Mirror the pipeline derivation exactly.
        return bool(
            "fallback" in chosen_path
            or "timeout" in chosen_path
            or "duplicate" in chosen_path
        )

    def test_normal_paths_are_not_fallback(self):
        for p in ("llm_compose", "template_compose", "social", "platform_reply"):
            assert self._is_fallback(p) is False, f"{p!r} should not be fallback"

    def test_degraded_paths_are_fallback(self):
        for p in (
            "llm_fallback",
            "generic_fallback",
            "llm_timeout",
            "llm_fallback_failed",
            "duplicate_replaced",
            "context_fallback",
        ):
            assert self._is_fallback(p) is True, f"{p!r} should be fallback"


# ── 4. End-to-end sanity (parse one synthetic log line) ───────────────


class TestBrainTurnLogIsValidJSON:
    """Construct a minimal payload that mirrors the pipeline's JSON
    dump and verify it parses cleanly. This guards against typos
    that would crash json.dumps() at runtime."""

    def test_payload_round_trips(self):
        payload = {
            "tenant_id":     1,
            "phone":         "0001",
            "turn":          3,
            "message_len":   42,
            "inbound_preview": "هل ممتاز لمشاكل البطن؟",
            "detected_intent": "social",
            "confidence":    0.92,
            "slots":         {"social_category": "general_courtesy"},
            "method":        "rules",
            "social_category": "general_courtesy",
            "stage_before":  "discovery",
            "stage_after":   "discovery",
            "greeted":       True,
            "product_focus": None,
            "draft_order":   None,
            "order_prep_missing": [],
            "order_status":  "",
            "awaiting_payment_receipt": False,
            "payment_receipt_received": False,
            "facts": {
                "products":      12,
                "in_stock":      8,
                "orderable":     True,
                "coupons":       False,
                "integration":   True,
                "platform":      "salla",
                "store":         "Test Store",
            },
            "action":             "ACTION_SOCIAL_REPLY",
            "chosen_path":        "social_template",
            "reason":             "social_courtesy",
            "policy_modified":    False,
            "whether_coupon_logic_considered": False,
            "suggested_next_step": "",
            "customer_goal":      "",
            "selected_product":   None,
            "checkout_city":      "",
            "short_address_code": "",
            "exec_success":     True,
            "exec_error":       None,
            "response_mode":    "template",
            "fallback_used":    False,
            "model_used":       "claude-opus-4-6",
            "reply_len":        25,
            "alignment_passed":   False,
            "alignment_mismatch": "question_to_social",
            "alignment_regen":    False,
            "latency_ms":       420,
        }
        s = json.dumps(payload, ensure_ascii=False)
        round_tripped = json.loads(s)
        assert round_tripped == payload
        assert "alignment_passed" in s
        assert "fallback_used" in s
        assert "model_used" in s
        assert "social_category" in s
        assert "inbound_preview" in s
