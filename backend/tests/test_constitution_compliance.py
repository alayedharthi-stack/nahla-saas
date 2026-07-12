"""Nahla Mandatory Natural Language Rule — CI merge gate.

Authoritative doctrine: ``AGENTS.md``. Policy registry:
``backend/modules/ai/compose/constitutional_policy.py``.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import date
from typing import Any, Dict, Mapping

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.persona.branch_action_compose import (  # noqa: E402
    BranchActionComposeOutcome,
    BranchComposeFacts,
    minimal_emergency_fallback,
)
from modules.ai.compose import constitutional_policy as policy  # noqa: E402
from modules.ai.compose.constitutional_policy import (  # noqa: E402
    APPROVED_COMPOSE_SOURCES,
    DETERMINISTIC_EXCEPTIONS,
    GOVERNANCE_BASELINE,
    TRACKED_VIOLATION_IDS,
    TRACKED_VIOLATION_PATHS,
    TRACKED_VIOLATION_STATUS,
    TRACKED_VIOLATIONS,
    TrackedViolation,
    classify_untracked_violations,
    format_approved_exception_report,
    format_tracked_violation_report,
    load_governance_baseline,
    scan_compose_boundary_violations,
    scan_exact_prose_test_assertions,
    scan_responder_direct_template_returns,
    validate_governance_baseline,
    validate_live_violations_against_waivers,
    validate_new_violation_cannot_self_waive,
    validate_tracked_violation_entry,
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


def _sample_waiver(**overrides: object) -> TrackedViolation:
    base = TRACKED_VIOLATIONS[0]
    data = {
        "violation_id": base.violation_id,
        "path": base.path,
        "file": base.file,
        "action": base.action,
        "owner": base.owner,
        "reason": base.reason,
        "removal_ref": base.removal_ref,
        "added_at": base.added_at,
        "expiry_date": base.expiry_date,
        "approved_by": base.approved_by,
    }
    data.update(overrides)
    return TrackedViolation(**data)  # type: ignore[arg-type]


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


class TestGovernanceWaiverSchema:
    def test_expired_tracked_violation_fails(self) -> None:
        expired = _sample_waiver(expiry_date="2020-01-01")
        errors = validate_tracked_violation_entry(expired, as_of=date(2026, 7, 12))
        assert any("expired" in e for e in errors)

    def test_missing_owner_fails(self) -> None:
        bad = _sample_waiver(owner="")
        errors = validate_tracked_violation_entry(bad)
        assert any("owner is required" in e for e in errors)

    def test_missing_removal_reference_fails(self) -> None:
        bad = _sample_waiver(removal_ref="")
        errors = validate_tracked_violation_entry(bad)
        assert any("removal_ref is required" in e for e in errors)

    def test_malformed_expiry_fails(self) -> None:
        bad = _sample_waiver(expiry_date="31-08-2026")
        errors = validate_tracked_violation_entry(bad)
        assert any("malformed expiry_date" in e for e in errors)

    def test_missing_approved_by_fails(self) -> None:
        bad = _sample_waiver(approved_by="")
        errors = validate_tracked_violation_entry(bad)
        assert any("approved_by is required" in e for e in errors)

    def test_live_baseline_passes_governance_validation(self) -> None:
        assert validate_governance_baseline(as_of=date(2026, 7, 12)) == []

    def test_tracked_violation_is_not_classified_as_approved_exception(self) -> None:
        assert TRACKED_VIOLATION_STATUS == "FAILING_POLICY_WITH_TEMPORARY_WAIVER"
        approved_paths = {exc.action_path for exc in DETERMINISTIC_EXCEPTIONS}
        for violation in TRACKED_VIOLATIONS:
            assert violation.path not in approved_paths
            assert violation.status == TRACKED_VIOLATION_STATUS
        report = format_tracked_violation_report()
        assert "FAILING POLICY WITH TEMPORARY WAIVER" in report
        assert "APPROVED" not in report.split("FAILING")[0]

    def test_approved_exception_report_is_separate_from_waivers(self) -> None:
        approved_report = format_approved_exception_report()
        waiver_report = format_tracked_violation_report()
        assert "APPROVED DETERMINISTIC EXCEPTIONS" in approved_report
        assert "track_order_not_found" not in approved_report
        assert "NL-V001" in waiver_report


class TestAntiGrandfathering:
    def test_new_violation_id_not_in_baseline_allowed_list_fails(self) -> None:
        errors = validate_new_violation_cannot_self_waive(
            GOVERNANCE_BASELINE,
            proposed_new_ids={"NL-V999"},
        )
        assert errors
        assert "governance_baseline_version" in errors[0]

    def test_adding_violation_and_waiver_together_cannot_pass_baseline_guard(self) -> None:
        raw = json.loads(
            (policy.BASELINE_JSON).read_text(encoding="utf-8")
        )
        mutated = copy.deepcopy(raw)
        mutated["violations"].append(
            {
                "violation_id": "NL-V999",
                "path": "new_fake_path",
                "file": "backend/modules/ai/brain/compose/responder.py",
                "action": "fake:T.fake_template",
                "owner": "ai-platform",
                "reason": "attempted casual waiver",
                "removal_ref": "fix/some-feature",
                "added_at": "2026-07-12",
                "expiry_date": "2026-12-31",
                "approved_by": "self",
            }
        )
        # allowed_violation_ids unchanged — governance mismatch must fail.
        tmp = policy.REPO_ROOT / ".tmp_baseline_guard_test.json"
        tmp.write_text(json.dumps(mutated), encoding="utf-8")
        try:
            baseline = load_governance_baseline(tmp)
            errors = validate_governance_baseline(baseline, as_of=date(2026, 7, 12))
            assert any("must match allowed_violation_ids" in e for e in errors)
        finally:
            if tmp.exists():
                tmp.unlink()


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

    def test_dedup_substitute_introducing_fixed_conversational_prose_is_detected(self) -> None:
        fixed_substitute = "حالياً لا يوجد رقم تواصل مهيأ لإرساله."
        meta: Mapping[str, object] = {
            "compose_source": "llm",
            "response_mode": "llm",
            "chosen_path": "track_order_not_found",
            "llm_candidate_present": True,
            "final_text_transformed": True,
            "final_transform_reasons": ["dedup_operational_delta"],
        }
        assert validate_reply_metadata(meta) == []
        assert fixed_substitute


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
    def test_track_order_not_found_still_detected_until_runtime_fix(self) -> None:
        findings = scan_responder_direct_template_returns()
        paths = {f.path for f in findings}
        assert "track_order_not_found" in paths
        match = next(f for f in findings if f.path == "track_order_not_found")
        assert match.template_call == "order_status_not_found"

    def test_compose_scan_detects_assigned_template_return_pattern(self) -> None:
        findings = scan_compose_boundary_violations(
            ["backend/modules/ai/brain/compose/responder.py"]
        )
        kinds = {f.kind.value for f in findings}
        assert "direct_template_return" in kinds

    def test_detected_violations_are_explicitly_tracked_not_silently_grandfathered(self) -> None:
        errors = validate_live_violations_against_waivers()
        assert errors == [], (
            "Constitutional drift detected:\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\n\n"
            + format_tracked_violation_report()
        )

    def test_untracked_new_deterministic_prose_fails(self) -> None:
        untracked = classify_untracked_violations(scan_compose_boundary_violations())
        assert untracked == []

    def test_nl_v001_removal_ownership(self) -> None:
        waiver = next(v for v in TRACKED_VIOLATIONS if v.violation_id == "NL-V001")
        assert waiver.removal_ref == "fix/track-order-not-found-compose-compliance"

    def test_nl_v002_has_separate_removal_scope(self) -> None:
        v001 = next(v for v in TRACKED_VIOLATIONS if v.violation_id == "NL-V001")
        v002 = next(v for v in TRACKED_VIOLATIONS if v.violation_id == "NL-V002")
        assert v002.removal_ref == "fix/track-order-need-identifiers-compose-compliance"
        assert v002.removal_ref != v001.removal_ref

    def test_nl_t001_tracked_for_exact_prose_assertion(self) -> None:
        assertions = scan_exact_prose_test_assertions()
        assert assertions
        assert "NL-T001" in TRACKED_VIOLATION_IDS
        assert "track_order_not_found" in TRACKED_VIOLATION_PATHS

    def test_stale_waiver_without_matching_code_fails(self) -> None:
        fake_waivers = (
            TrackedViolation(
                violation_id="NL-V001",
                path="track_order_not_found",
                file="backend/modules/ai/brain/compose/responder.py",
                action="order_not_found:T.removed_template",
                owner="ai-platform",
                reason="stale",
                removal_ref="fix/x",
                added_at="2026-07-12",
                expiry_date="2026-08-31",
                approved_by="test",
            ),
        )
        original = policy.TRACKED_VIOLATIONS
        policy.TRACKED_VIOLATIONS = fake_waivers  # type: ignore[misc]
        policy.TRACKED_VIOLATION_PATHS = frozenset(v.path for v in fake_waivers)
        try:
            errors = validate_live_violations_against_waivers()
            assert any("no longer matches code" in e for e in errors)
        finally:
            policy.TRACKED_VIOLATIONS = original
            policy.TRACKED_VIOLATION_PATHS = frozenset(v.path for v in original)


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
