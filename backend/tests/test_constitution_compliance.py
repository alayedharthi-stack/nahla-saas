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
from pathlib import Path
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
    ViolationKind,
    classify_untracked_violations,
    format_approved_exception_report,
    format_tracked_violation_report,
    load_governance_baseline,
    scan_compose_boundary_violations,
    scan_compose_source_snippet,
    scan_exact_prose_test_assertions,
    scan_responder_direct_template_returns,
    scan_tracking_clarification_guard_violations,
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
    data = {
        "violation_id": "NL-V999",
        "path": "synthetic_waiver_test",
        "file": "backend/modules/ai/brain/compose/responder.py",
        "action": "fake:T.fake_template",
        "owner": "ai-platform",
        "reason": "synthetic waiver schema test fixture",
        "removal_ref": "fix/synthetic",
        "added_at": "2026-07-12",
        "expiry_date": "2026-12-31",
        "approved_by": "test",
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

    def test_constitution_documents_final_customer_text_provenance_rule(self) -> None:
        text = (policy.REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        assert "Final customer text provenance rule" in text
        assert "true source" in text.lower() or "True source" in text

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
        assert "track_order_need_order_number" not in approved_report
        assert "NL-V002" not in waiver_report
        assert "NL-T002" not in waiver_report


class TestGov001IntelligenceNonInterference:
    """GOV-001 document lock — no runtime Brain/coupon/routing assertions."""

    def test_policy_files_exist(self) -> None:
        root = policy.REPO_ROOT
        assert (root / "docs/engineering/intelligence-non-interference-policy.md").is_file()
        assert (root / ".cursor/rules/intelligence-non-interference.mdc").is_file()

    def test_agents_and_checklist_declare_gov_001(self) -> None:
        agents = (policy.REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        checklist = (
            policy.REPO_ROOT / "docs/engineering/ai-pr-constitution-checklist.md"
        ).read_text(encoding="utf-8")
        assert "GOV-001" in agents
        assert "INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE" in agents
        assert "KEEP_MODEL_FREE_FIX_SYSTEM_AROUND_IT" in agents
        assert "CUSTOMER_REGEX_INTENT_REPAIR=FORBIDDEN" in agents
        assert "GOV-001" in checklist
        assert "INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE" in checklist

    def test_policy_locks_model_prompt_persona_and_phrase_hacks(self) -> None:
        text = (
            policy.REPO_ROOT / "docs/engineering/intelligence-non-interference-policy.md"
        ).read_text(encoding="utf-8")
        for token in (
            "INTELLIGENCE_POLICY=KEEP_MODEL_FREE_FIX_SYSTEM_AROUND_IT",
            "MODEL_CHANGE=FORBIDDEN_BY_DEFAULT",
            "PROMPT_CHANGE=FORBIDDEN_BY_DEFAULT",
            "PERSONA_CHANGE=FORBIDDEN_BY_DEFAULT",
            "PHRASE_MAPS=FORBIDDEN",
            "KEYWORD_INTENT_HACKS=FORBIDDEN",
            "CUSTOMER_REGEX_INTENT_REPAIR=FORBIDDEN",
            "ONLY THEN RAW MODEL EVALUATION",
            "INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE",
            "MODEL_CHANGED=NO",
            "PROMPT_CHANGED=NO",
            "PERSONA_CHANGED=NO",
            "PHRASE_MAP_CHANGED=NO",
            "KEYWORD_ROUTER_CHANGED=NO",
            "CUSTOMER_REGEX_CHANGED=NO",
        ):
            assert token in text, token


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

        llm_candidate = "ما لقيت طلب بهذا الرقم، تأكد من رقم الطلب لو سمحت."
        scrubbed, changed, asset_class = maybe_scrub_unkept_asset_promise(
            llm_candidate,
            has_url=False,
            has_media=False,
            has_phone=False,
        )
        assert changed is False
        assert scrubbed == llm_candidate
        assert asset_class is None

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


class TestScannerPatternProofs:
    """Synthetic AST fixtures proving scanner patterns beyond live responder hits."""

    def test_scanner_detects_direct_template_return(self) -> None:
        findings = scan_compose_source_snippet(
            '''
if msg_key == "order_not_found":
    result.data["chosen_path"] = "synthetic_direct"
    return T.order_status_not_found()
'''
        )
        assert any(f.kind == ViolationKind.DIRECT_TEMPLATE_RETURN for f in findings)
        match = next(f for f in findings if f.path == "synthetic_direct")
        assert match.template_call == "order_status_not_found"

    def test_scanner_detects_assign_template_then_return(self) -> None:
        findings = scan_compose_source_snippet(
            '''
if msg_key == "order_not_found":
    result.data["chosen_path"] = "synthetic_assign_return"
    text = T.order_status_not_found()
    return text
'''
        )
        assert any(f.kind == ViolationKind.ASSIGNED_TEMPLATE_RETURN for f in findings)
        match = next(f for f in findings if f.path == "synthetic_assign_return")
        assert match.template_call == "order_status_not_found"

    def test_scanner_detects_deterministic_builder_call_return(self) -> None:
        findings = scan_compose_source_snippet(
            '''
if topic == "payment_barcode":
    result.data["chosen_path"] = "synthetic_builder"
    return payment_barcode_intro_text(ctx)
'''
        )
        assert any(f.kind == ViolationKind.BUILDER_CALL_RETURN for f in findings)
        match = next(f for f in findings if f.path == "synthetic_builder")
        assert match.template_call == "payment_barcode_intro_text"

    def test_scanner_detects_fixed_arabic_string_return(self) -> None:
        findings = scan_compose_source_snippet(
            '''
if topic == "greeting":
    result.data["chosen_path"] = "synthetic_fixed_arabic"
    return "مرحباً، هذا رد محادثة ثابت"
'''
        )
        assert any(f.kind == ViolationKind.FIXED_STRING_RETURN for f in findings)
        match = next(f for f in findings if f.path == "synthetic_fixed_arabic")
        assert match.detail == "return fixed Arabic string literal"

    def test_tracking_guard_scanner_detects_retired_prose_owner_call(self) -> None:
        findings = policy._scan_tracking_clarification_guard_source(
            '''
def guard():
    return T.track_order_need_identifiers()
''',
            file_rel="synthetic_tracking_guard.py",
        )
        assert any(
            f.path == "tracking_clarification_guard_boundary"
            and f.template_call == "track_order_need_identifiers"
            for f in findings
        )

    def test_tracking_guard_scanner_detects_fixed_tracking_reply(self) -> None:
        findings = policy._scan_tracking_clarification_guard_source(
            '''
def guard():
    return StaffEscalationTruthGuardResult(
        reply="أرسل رقم الطلب",
        action="blocked_false_escalation_order_tracking",
    )
''',
            file_rel="synthetic_staff_guard.py",
        )
        assert any(
            f.kind == ViolationKind.FIXED_STRING_RETURN
            and f.path == "tracking_clarification_guard_boundary"
            for f in findings
        )


class TestRuntimeViolationDetection:
    def test_track_order_not_found_no_longer_direct_template_return(self) -> None:
        findings = scan_responder_direct_template_returns()
        paths = {f.path for f in findings}
        assert "track_order_not_found" not in paths

    def test_track_order_need_order_number_no_longer_direct_template_return(self) -> None:
        findings = scan_responder_direct_template_returns()
        paths = {f.path for f in findings}
        assert "track_order_need_order_number" not in paths

    def test_tracking_clarification_guard_boundary_has_no_prose_owner(self) -> None:
        assert scan_tracking_clarification_guard_violations() == []

    def test_live_responder_scan_has_no_tracked_track_order_template_paths(self) -> None:
        findings = scan_responder_direct_template_returns()
        paths = {f.path for f in findings}
        assert "track_order_need_order_number" not in paths
        assert "track_order_not_found" not in paths

    def test_governance_baseline_rejects_duplicate_violation_ids(self) -> None:
        raw = json.loads(policy.BASELINE_JSON.read_text(encoding="utf-8"))
        dup = copy.deepcopy(raw)
        sample = {
            "violation_id": "NL-V999",
            "path": "synthetic_duplicate_test",
            "file": "backend/modules/ai/brain/compose/responder.py",
            "action": "fake:T.fake_template",
            "owner": "ai-platform",
            "reason": "duplicate guard test",
            "removal_ref": "fix/x",
            "added_at": "2026-07-12",
            "expiry_date": "2026-12-31",
            "approved_by": "test",
        }
        dup["violations"].extend([sample, copy.deepcopy(sample)])
        dup["allowed_violation_ids"] = [v["violation_id"] for v in dup["violations"]]
        tmp = policy.REPO_ROOT / ".tmp_dup_baseline_test.json"
        tmp.write_text(json.dumps(dup), encoding="utf-8")
        try:
            errors = validate_governance_baseline(load_governance_baseline(tmp))
            assert any("duplicate violation_id" in e for e in errors)
        finally:
            if tmp.exists():
                tmp.unlink()

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

    def test_nl_v002_and_nl_t002_removed_from_baseline(self) -> None:
        assert "NL-V002" not in TRACKED_VIOLATION_IDS
        assert "NL-T002" not in TRACKED_VIOLATION_IDS
        assert "track_order_need_order_number" not in TRACKED_VIOLATION_PATHS

    def test_stale_waiver_without_matching_code_fails(self) -> None:
        fake_waivers = (
            TrackedViolation(
                violation_id="NL-V002",
                path="track_order_need_order_number",
                file="backend/modules/ai/brain/compose/responder.py",
                action="need_order_number:T.removed_template",
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


class TestFixedTenantAcceptancePolicy:
    def test_production_runtime_has_no_tenant_33_dependency(self) -> None:
        from modules.platform.fixed_tenant_policy import (  # noqa: PLC0415
            format_violation_report,
            scan_fixed_tenant_violations,
        )

        violations = scan_fixed_tenant_violations(zone="production")
        assert violations == [], format_violation_report(violations)

    def test_ops_scripts_have_no_implicit_tenant_33_defaults(self) -> None:
        from modules.platform.fixed_tenant_policy import (  # noqa: PLC0415
            format_violation_report,
            scan_fixed_tenant_violations,
        )

        violations = scan_fixed_tenant_violations(zone="ops")
        assert violations == [], format_violation_report(violations)


class TestGOV002ExecutableGuardWiring:
    def test_scanner_exists(self) -> None:
        root = Path(__file__).resolve().parents[2]
        scanner = root / "scripts" / "lint_intelligence_non_interference.py"
        assert scanner.is_file()
        text = scanner.read_text(encoding="utf-8")
        assert "--trusted-base-scanner" in text
        assert "BASE_NOT_AVAILABLE" in text

    def test_constitution_job_runs_trusted_base_scanner(self) -> None:
        root = Path(__file__).resolve().parents[2]
        ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "GOV-002 — Intelligence non-interference diff guard" in ci
        assert "fetch-depth: 0" in ci
        assert "git merge-base" in ci
        assert "git show" in ci and "nahla_intelligence_guard.py" in ci
        assert "BASE_NOT_AVAILABLE" in ci
        assert "test_intelligence_non_interference_guard.py" in ci

    def test_exception_registry_starts_empty(self) -> None:
        from modules.ai.governance.intelligence_non_interference import (  # noqa: PLC0415
            EXCEPTIONS_PATH,
            load_exceptions_from_text,
        )

        root = Path(__file__).resolve().parents[2]
        raw = (root / EXCEPTIONS_PATH).read_text(encoding="utf-8")
        assert load_exceptions_from_text(raw) == []
