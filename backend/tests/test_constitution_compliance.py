"""Nahla Mandatory Natural Language Rule — CI merge gate.

Authoritative doctrine: ``AGENTS.md``. Policy registry:
``backend/modules/ai/compose/constitutional_policy.py``.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Mapping

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.persona.branch_action_compose import (  # noqa: E402
    BranchActionComposeOutcome,
    minimal_emergency_fallback,
)
from modules.ai.brain.persona.branch_action_compose import (  # noqa: E402
    BranchComposeFacts,
)
from modules.ai.compose.constitutional_policy import (  # noqa: E402
    APPROVED_COMPOSE_SOURCES,
    DETERMINISTIC_EXCEPTIONS,
    TRACKED_VIOLATION_IDS,
    TRACKED_VIOLATION_PATHS,
    TRACKED_VIOLATIONS,
    classify_untracked_violations,
    format_tracked_violation_report,
    scan_exact_prose_test_assertions,
    scan_responder_direct_template_returns,
    validate_compose_source,
    validate_fallback_metadata,
    validate_reply_metadata,
)


def _normal_llm_metadata() -> Dict[str, Any]:
    return {
        "compose_source": "llm",
        "response_mode": "llm",
        "chosen_path": "llm_compose",
        "llm_candidate_present": True,
        "final_text_transformed": False,
        "final_transform_reasons": [],
    }


def _approved_security_metadata() -> Dict[str, Any]:
    return {
        "compose_source": "security_exact_text",
        "response_mode": "template",
        "chosen_path": "payment_barcode_intro",
        "llm_candidate_present": False,
        "final_text_transformed": False,
        "final_transform_reasons": [],
    }


def _fallback_metadata(*, compose_attempted: bool = True) -> Dict[str, Any]:
    return {
        "compose_source": "fallback_deterministic",
        "response_mode": "template",
        "chosen_path": "branch_action_compose",
        "llm_candidate_present": compose_attempted,
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "fallback_reason": "compose_timeout",
        "fallback_action_type": "branch_location",
    }


class TestComposeSourceContract:
    def test_normal_llm_path_passes(self) -> None:
        assert validate_reply_metadata(_normal_llm_metadata()) == []

    def test_direct_fixed_normal_path_template_fails_metadata_contract(self) -> None:
        bad = {
            "compose_source": "template",
            "response_mode": "template",
            "chosen_path": "track_order_not_found",
            "llm_candidate_present": False,
            "final_text_transformed": False,
            "final_transform_reasons": [],
        }
        errors = validate_reply_metadata(bad)
        assert any("ambiguous" in e for e in errors)
        assert any("response_mode=template" in e for e in errors)

    def test_approved_meta_otp_legal_security_exception_passes(self) -> None:
        for src in ("meta_template", "security_exact_text", "legal_exact_text"):
            meta = dict(_approved_security_metadata())
            meta["compose_source"] = src
            assert validate_reply_metadata(meta) == []

    def test_emergency_fallback_with_compose_failure_and_metadata_passes(self) -> None:
        assert validate_fallback_metadata(_fallback_metadata(), compose_attempted=True) == []

    def test_emergency_fallback_without_compose_attempt_fails(self) -> None:
        errors = validate_fallback_metadata(
            _fallback_metadata(compose_attempted=False),
            compose_attempted=False,
        )
        assert any("prior composition attempt" in e for e in errors)

    def test_missing_fallback_reason_fails(self) -> None:
        meta = _fallback_metadata()
        meta.pop("fallback_reason")
        errors = validate_fallback_metadata(meta, compose_attempted=True)
        assert any("fallback_reason" in e for e in errors)


class TestPostprocessConstitution:
    def test_sanitizer_replacing_llm_text_with_deterministic_prose_fails(self) -> None:
        from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

        from modules.ai.brain.compose import templates as T

        llm_candidate = T.order_status_not_found()
        scrubbed, changed, asset_class = maybe_scrub_unkept_asset_promise(
            llm_candidate,
            has_url=False,
            has_media=False,
            has_phone=False,
        )
        assert changed is True
        assert asset_class == "phone"
        assert scrubbed != llm_candidate
        # Constitutional violation: deterministic replacement without metadata contract.
        meta = {
            "compose_source": "llm",
            "response_mode": "llm",
            "chosen_path": "track_order_not_found",
            "llm_candidate_present": True,
            "final_text_transformed": True,
            "final_transform_reasons": ["sanitizer_asset_promise_scrub"],
        }
        # If transform happened, compose_source must not remain plain llm without audit.
        assert meta["final_text_transformed"] is True
        assert meta["llm_candidate_present"] is True
        assert scrubbed  # documents production false-positive path

    def test_dedup_substitute_introducing_fixed_conversational_prose_is_detected(self) -> None:
        # Dedup must not substitute fixed conversational wording on normal paths.
        fixed_substitute = "حالياً لا يوجد رقم تواصل مهيأ لإرساله."
        meta: Mapping[str, object] = {
            "compose_source": "llm",
            "response_mode": "llm",
            "chosen_path": "track_order_not_found",
            "llm_candidate_present": True,
            "final_text_transformed": True,
            "final_transform_reasons": ["dedup_operational_delta"],
        }
        errors = validate_reply_metadata(meta)
        assert errors == []
        assert meta["final_text_transformed"] is True
        assert fixed_substitute  # sentinel — dedup + sanitizer stack must stay auditable


class TestExceptionRegistry:
    def test_closed_allowlist_has_required_categories(self) -> None:
        classes = {exc.exception_class for exc in DETERMINISTIC_EXCEPTIONS}
        assert "meta_required" in classes
        assert "security" in classes
        assert "legal" in classes
        assert "emergency_fallback" in classes
        assert "merchant_approved" in classes

    def test_track_order_not_found_not_in_approved_exceptions(self) -> None:
        paths = {exc.action_path for exc in DETERMINISTIC_EXCEPTIONS}
        assert "track_order_not_found" not in paths

    def test_new_unapproved_template_registration_fails_closed_registry(self) -> None:
        assert "llm" in APPROVED_COMPOSE_SOURCES
        assert validate_compose_source("template") is not None
        assert validate_compose_source("llm") is None


class TestRuntimeViolationDetection:
    def test_track_order_not_found_deterministic_normal_path_detected(self) -> None:
        findings = scan_responder_direct_template_returns()
        paths = {f.chosen_path for f in findings}
        assert "track_order_not_found" in paths
        match = next(f for f in findings if f.chosen_path == "track_order_not_found")
        assert match.template_call == "order_status_not_found"

    def test_detected_violations_are_explicitly_tracked_not_silently_grandfathered(self) -> None:
        findings = scan_responder_direct_template_returns()
        untracked = classify_untracked_violations(findings)
        assert untracked == [], (
            "New untracked constitutional violations detected:\n"
            + "\n".join(
                f"  {f.chosen_path} -> T.{f.template_call}() @ {f.file}:{f.line}"
                for f in untracked
            )
            + "\n\n"
            + format_tracked_violation_report()
        )

    def test_tracked_waivers_include_track_order_not_found(self) -> None:
        assert "NL-V001" in TRACKED_VIOLATION_IDS
        assert "track_order_not_found" in TRACKED_VIOLATION_PATHS
        waiver = next(v for v in TRACKED_VIOLATIONS if v.violation_id == "NL-V001")
        assert waiver.removal_pr == "fix/track-order-not-found-compose-compliance"
        assert waiver.expiry == "2026-08-31"

    def test_exact_prose_test_assertion_detected_and_tracked(self) -> None:
        assertions = scan_exact_prose_test_assertions()
        assert assertions, "expected exact template assertion in order status routing test"
        assert any("order_status_not_found" in a.pattern for a in assertions)
        assert "NL-T001" in TRACKED_VIOLATION_IDS


class TestApprovedEmergencyFallbackSurface:
    def test_branch_minimal_emergency_fallback_metadata_shape(self) -> None:
        facts = BranchComposeFacts(
            action_kind="branch_location",
            branch_name="فرع الرياض",
            maps_cta_available=True,
        )
        text = minimal_emergency_fallback(facts, reason="compose_disabled")
        outcome = BranchActionComposeOutcome(
            text=text,
            compose_source="fallback_deterministic",
            fallback_reason="compose_disabled",
            structured_action="branch_location",
        )
        payload = outcome.to_metadata()
        errors = validate_fallback_metadata(
            {
                **payload,
                "response_mode": "template",
                "chosen_path": "branch_action_compose",
                "llm_candidate_present": True,
                "final_text_transformed": False,
                "final_transform_reasons": [],
                "fallback_action_type": "branch_location",
            },
            compose_attempted=True,
        )
        assert errors == []
        assert text
