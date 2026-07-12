"""Nahla Mandatory Natural Language Rule — enforceable policy registry.

Authoritative source: ``AGENTS.md`` (Mandatory Natural Language Rule).

Two strictly separate registries:

A. ``DETERMINISTIC_EXCEPTIONS`` — permanent legitimate exact-text categories.
B. ``TRACKED_VIOLATIONS`` — unconstitutional debt with temporary waiver (never approved).

Tracked waivers load from ``tracked_violations_baseline.json``. New violation IDs
require ``governance_baseline_version`` bump in a dedicated governance PR.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]
BASELINE_JSON = Path(__file__).resolve().parent / "tracked_violations_baseline.json"

COMPOSE_BOUNDARY_FILES: Tuple[str, ...] = (
    "backend/modules/ai/brain/compose/responder.py",
    "backend/modules/ai/brain/compose/templates.py",
)

APPROVED_COMPOSE_SOURCES: FrozenSet[str] = frozenset(
    {
        "llm",
        "persona_llm",
        "merchant_template",
        "meta_template",
        "legal_exact_text",
        "security_exact_text",
        "fallback_deterministic",
    }
)

AMBIGUOUS_COMPOSE_SOURCES: FrozenSet[str] = frozenset({"template"})

REQUIRED_REPLY_METADATA_KEYS: Tuple[str, ...] = (
    "compose_source",
    "response_mode",
    "chosen_path",
    "llm_candidate_present",
    "final_text_transformed",
    "final_transform_reasons",
)

FALLBACK_METADATA_KEYS: Tuple[str, ...] = (
    "fallback_reason",
    "fallback_action_type",
)

TRACKED_VIOLATION_STATUS = "FAILING_POLICY_WITH_TEMPORARY_WAIVER"


class ViolationKind(str, Enum):
    DIRECT_TEMPLATE_RETURN = "direct_template_return"
    ASSIGNED_TEMPLATE_RETURN = "assigned_template_return"
    FIXED_STRING_RETURN = "fixed_string_return"
    BUILDER_CALL_RETURN = "builder_call_return"


@dataclass(frozen=True)
class DeterministicException:
    exception_id: str
    category: str
    action_path: str
    reason_exact_wording: str
    owner: str
    approving_source: str
    exception_class: str


DETERMINISTIC_EXCEPTIONS: Tuple[DeterministicException, ...] = (
    DeterministicException(
        exception_id="EX-OTP-001",
        category="authentication",
        action_path="otp/send",
        reason_exact_wording="OTP codes require exact deterministic delivery",
        owner="platform-auth",
        approving_source="AGENTS.md allowed exception #4",
        exception_class="security",
    ),
    DeterministicException(
        exception_id="EX-META-001",
        category="meta_template",
        action_path="whatsapp/meta_template_send",
        reason_exact_wording="Official WhatsApp/Meta templates require exact approved wording",
        owner="integrations",
        approving_source="AGENTS.md allowed exception #3",
        exception_class="meta_required",
    ),
    DeterministicException(
        exception_id="EX-MERCHANT-TPL-001",
        category="merchant_template",
        action_path="templates/library",
        reason_exact_wording="Merchant-created or merchant-approved Nahla Templates Library entries",
        owner="merchant-success",
        approving_source="AGENTS.md allowed exception #1-2",
        exception_class="merchant_approved",
    ),
    DeterministicException(
        exception_id="EX-LEGAL-001",
        category="legal_notice",
        action_path="legal/exact_notice",
        reason_exact_wording="Legally required notices with mandated exact wording",
        owner="legal",
        approving_source="AGENTS.md allowed exception #5",
        exception_class="legal",
    ),
    DeterministicException(
        exception_id="EX-SEC-PAYMENT-BARCODE-001",
        category="payment_security",
        action_path="payment_barcode_intro",
        reason_exact_wording="Payment barcode/security instructions may require exact wording",
        owner="ai-commerce",
        approving_source="nahla-ai-merchant-assistant-policy.md",
        exception_class="security",
    ),
    DeterministicException(
        exception_id="EX-FALLBACK-GENERIC-001",
        category="emergency_fallback",
        action_path="llm_fallback_failed",
        reason_exact_wording="Minimal generic fallback only after genuine LLM compose failure",
        owner="ai-platform",
        approving_source="AGENTS.md emergency fallback requirements",
        exception_class="emergency_fallback",
    ),
)

APPROVED_EXCEPTION_PATHS: FrozenSet[str] = frozenset(
    exc.action_path for exc in DETERMINISTIC_EXCEPTIONS
)


@dataclass(frozen=True)
class TrackedViolation:
    violation_id: str
    path: str
    file: str
    action: str
    owner: str
    reason: str
    removal_ref: str
    added_at: str
    expiry_date: str
    approved_by: str

    @property
    def status(self) -> str:
        return TRACKED_VIOLATION_STATUS


@dataclass(frozen=True)
class GovernanceBaseline:
    schema_version: int
    governance_baseline_version: int
    governance_pr: str
    approved_by: str
    allowed_violation_ids: Tuple[str, ...]
    violations: Tuple[TrackedViolation, ...]


@dataclass(frozen=True)
class ComposeViolationFinding:
    kind: ViolationKind
    path: str
    file: str
    line: int
    detail: str
    template_call: str = ""


@dataclass(frozen=True)
class ExactProseTestAssertion:
    file: str
    line: int
    pattern: str
    template_call: str


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EXACT_TEMPLATE_ASSERT_RE = re.compile(
    r"assert\s+\w+\s*==\s*T\.(\w+)\(\)"
)
_KNOWN_BUILDER_CALLS: FrozenSet[str] = frozenset(
    {
        "payment_barcode_intro_text",
        "minimal_emergency_fallback",
    }
)


def _parse_iso_date(value: str) -> date:
    if not _DATE_RE.match(value or ""):
        raise ValueError(f"malformed date (expected YYYY-MM-DD): {value!r}")
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_governance_baseline(path: Optional[Path] = None) -> GovernanceBaseline:
    src = path or BASELINE_JSON
    raw = json.loads(src.read_text(encoding="utf-8"))
    violations: List[TrackedViolation] = []
    for item in raw.get("violations", []):
        violations.append(
            TrackedViolation(
                violation_id=str(item["violation_id"]),
                path=str(item["path"]),
                file=str(item["file"]),
                action=str(item["action"]),
                owner=str(item.get("owner", "")),
                reason=str(item.get("reason", "")),
                removal_ref=str(item.get("removal_ref", "")),
                added_at=str(item.get("added_at", "")),
                expiry_date=str(item.get("expiry_date", "")),
                approved_by=str(item.get("approved_by", "")),
            )
        )
    return GovernanceBaseline(
        schema_version=int(raw.get("schema_version", 0)),
        governance_baseline_version=int(raw.get("governance_baseline_version", 0)),
        governance_pr=str(raw.get("governance_pr", "")),
        approved_by=str(raw.get("approved_by", "")),
        allowed_violation_ids=tuple(str(x) for x in raw.get("allowed_violation_ids", [])),
        violations=tuple(violations),
    )


GOVERNANCE_BASELINE = load_governance_baseline()
TRACKED_VIOLATIONS: Tuple[TrackedViolation, ...] = GOVERNANCE_BASELINE.violations
TRACKED_VIOLATION_PATHS: FrozenSet[str] = frozenset(v.path for v in TRACKED_VIOLATIONS)
TRACKED_VIOLATION_IDS: FrozenSet[str] = frozenset(v.violation_id for v in TRACKED_VIOLATIONS)
ALLOWED_BASELINE_VIOLATION_IDS: FrozenSet[str] = frozenset(
    GOVERNANCE_BASELINE.allowed_violation_ids
)


def validate_tracked_violation_entry(
    violation: TrackedViolation,
    *,
    as_of: Optional[date] = None,
) -> List[str]:
    errors: List[str] = []
    today = as_of or date.today()

    if not violation.violation_id.strip():
        errors.append("violation_id is required")
    if not violation.path.strip():
        errors.append(f"{violation.violation_id}: path is required")
    if not violation.file.strip():
        errors.append(f"{violation.violation_id}: file is required")
    if not violation.action.strip():
        errors.append(f"{violation.violation_id}: action is required")
    if not violation.owner.strip():
        errors.append(f"{violation.violation_id}: owner is required")
    if not violation.reason.strip():
        errors.append(f"{violation.violation_id}: reason is required")
    if not violation.removal_ref.strip():
        errors.append(f"{violation.violation_id}: removal_ref is required")
    if not violation.approved_by.strip():
        errors.append(f"{violation.violation_id}: approved_by is required")

    try:
        added = _parse_iso_date(violation.added_at)
    except ValueError as exc:
        errors.append(f"{violation.violation_id}: malformed added_at — {exc}")
        added = None

    try:
        expiry = _parse_iso_date(violation.expiry_date)
    except ValueError as exc:
        errors.append(f"{violation.violation_id}: malformed expiry_date — {exc}")
        expiry = None

    if added and expiry and added > expiry:
        errors.append(f"{violation.violation_id}: added_at is after expiry_date")

    if expiry and today > expiry:
        errors.append(
            f"{violation.violation_id}: waiver expired on {violation.expiry_date}"
        )

    if violation.violation_id not in ALLOWED_BASELINE_VIOLATION_IDS:
        errors.append(
            f"{violation.violation_id}: not in governance baseline allowed_violation_ids; "
            "new waivers require governance_baseline_version bump in a dedicated governance PR"
        )

    return errors


def validate_governance_baseline(
    baseline: Optional[GovernanceBaseline] = None,
    *,
    as_of: Optional[date] = None,
) -> List[str]:
    base = baseline or GOVERNANCE_BASELINE
    errors: List[str] = []

    if base.schema_version < 1:
        errors.append("schema_version must be >= 1")
    if base.governance_baseline_version < 1:
        errors.append("governance_baseline_version must be >= 1")
    if not base.governance_pr.strip():
        errors.append("governance_pr reference is required")
    if not base.approved_by.strip():
        errors.append("baseline approved_by is required")

    live_ids = {v.violation_id for v in base.violations}
    allowed_ids = set(base.allowed_violation_ids)
    if len(base.violations) != len(live_ids):
        errors.append("duplicate violation_id in violations list")
    if live_ids != allowed_ids:
        errors.append(
            "violations list must match allowed_violation_ids exactly "
            f"(live={sorted(live_ids)}, allowed={sorted(allowed_ids)})"
        )

    for violation in base.violations:
        errors.extend(validate_tracked_violation_entry(violation, as_of=as_of))

    return errors


def validate_compose_source(value: object) -> Optional[str]:
    src = str(value or "").strip()
    if not src:
        return "compose_source is required"
    if src in AMBIGUOUS_COMPOSE_SOURCES:
        return (
            f"compose_source={src!r} is ambiguous; use an approved exception class "
            f"({', '.join(sorted(APPROVED_COMPOSE_SOURCES))})"
        )
    if src not in APPROVED_COMPOSE_SOURCES:
        return f"compose_source={src!r} is not in the closed allowlist"
    return None


def validate_reply_metadata(
    metadata: Mapping[str, object],
    *,
    is_fallback: bool = False,
) -> List[str]:
    errors: List[str] = []
    for key in REQUIRED_REPLY_METADATA_KEYS:
        if key not in metadata:
            errors.append(f"missing required metadata key: {key}")

    compose_err = validate_compose_source(metadata.get("compose_source"))
    if compose_err:
        errors.append(compose_err)

    response_mode = str(metadata.get("response_mode") or "").strip()
    compose_source = str(metadata.get("compose_source") or "").strip()
    if response_mode == "template" and compose_source not in {
        "merchant_template",
        "meta_template",
        "legal_exact_text",
        "security_exact_text",
        "fallback_deterministic",
    }:
        errors.append(
            "response_mode=template requires an approved exact-text compose_source"
        )

    if is_fallback or compose_source == "fallback_deterministic":
        for key in FALLBACK_METADATA_KEYS:
            if not str(metadata.get(key) or "").strip():
                errors.append(f"missing fallback metadata key: {key}")
    return errors


def validate_fallback_metadata(
    metadata: Mapping[str, object],
    *,
    compose_attempted: bool,
) -> List[str]:
    errors = validate_reply_metadata(metadata, is_fallback=True)
    if not compose_attempted:
        errors.append("fallback_deterministic requires prior composition attempt")
    if not str(metadata.get("chosen_path") or "").strip():
        errors.append("fallback requires chosen_path")
    return errors


def _is_templates_call(node: ast.AST) -> Optional[str]:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "T":
            return func.attr
    return None


def _is_builder_call(node: ast.AST) -> Optional[str]:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id in _KNOWN_BUILDER_CALLS:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in _KNOWN_BUILDER_CALLS:
        return func.attr
    return None


def _is_arabic_string_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value.strip()
        return bool(text) and any("\u0600" <= ch <= "\u06ff" for ch in text)
    return False


def _extract_chosen_path(stmt: ast.stmt) -> Optional[str]:
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return None
    target = stmt.targets[0]
    if not isinstance(target, ast.Subscript):
        return None
    if (
        isinstance(target.value, ast.Attribute)
        and target.value.attr == "data"
        and isinstance(target.slice, ast.Constant)
        and target.slice.value == "chosen_path"
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    ):
        return stmt.value.value
    return None


def _scan_block_for_findings(
    body: Sequence[ast.stmt],
    *,
    file_rel: str,
    default_path: str = "",
) -> List[ComposeViolationFinding]:
    findings: List[ComposeViolationFinding] = []
    chosen_path = default_path
    assigned_templates: dict[str, str] = {}

    for stmt in body:
        maybe_path = _extract_chosen_path(stmt)
        if maybe_path:
            chosen_path = maybe_path

        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            template_name = _is_templates_call(stmt.value)
            if template_name and isinstance(target, ast.Name):
                assigned_templates[target.id] = template_name

        if isinstance(stmt, ast.Return) and stmt.value is not None:
            template_name = _is_templates_call(stmt.value)
            if template_name:
                if chosen_path and chosen_path not in APPROVED_EXCEPTION_PATHS:
                    findings.append(
                        ComposeViolationFinding(
                            kind=ViolationKind.DIRECT_TEMPLATE_RETURN,
                            path=chosen_path,
                            file=file_rel,
                            line=stmt.lineno,
                            detail=f"return T.{template_name}()",
                            template_call=template_name,
                        )
                    )
                continue

            if isinstance(stmt.value, ast.Name):
                template_name = assigned_templates.get(stmt.value.id)
                if template_name and chosen_path and chosen_path not in APPROVED_EXCEPTION_PATHS:
                    findings.append(
                        ComposeViolationFinding(
                            kind=ViolationKind.ASSIGNED_TEMPLATE_RETURN,
                            path=chosen_path,
                            file=file_rel,
                            line=stmt.lineno,
                            detail=f"{stmt.value.id} = T.{template_name}(); return {stmt.value.id}",
                            template_call=template_name,
                        )
                    )
                continue

            builder_name = _is_builder_call(stmt.value)
            if builder_name and chosen_path and chosen_path not in APPROVED_EXCEPTION_PATHS:
                findings.append(
                    ComposeViolationFinding(
                        kind=ViolationKind.BUILDER_CALL_RETURN,
                        path=chosen_path,
                        file=file_rel,
                        line=stmt.lineno,
                        detail=f"return {builder_name}(...)",
                        template_call=builder_name,
                    )
                )
                continue

            if _is_arabic_string_constant(stmt.value) and chosen_path:
                if chosen_path not in APPROVED_EXCEPTION_PATHS:
                    findings.append(
                        ComposeViolationFinding(
                            kind=ViolationKind.FIXED_STRING_RETURN,
                            path=chosen_path,
                            file=file_rel,
                            line=stmt.lineno,
                            detail="return fixed Arabic string literal",
                        )
                    )
    return findings


def scan_compose_source_snippet(
    source: str,
    *,
    file_rel: str = "synthetic_scan_fixture.py",
) -> List[ComposeViolationFinding]:
    """Parse a Python snippet and return compose-boundary findings (test fixture helper)."""
    tree = ast.parse(source, filename=file_rel)
    findings: List[ComposeViolationFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            findings.extend(_scan_block_for_findings(node.body, file_rel=file_rel))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            findings.extend(_scan_block_for_findings(node.body, file_rel=file_rel))
    return findings


def scan_compose_boundary_violations(
  files: Optional[Sequence[str]] = None,
) -> List[ComposeViolationFinding]:
    """Scan scoped compose/responder modules for deterministic normal-path prose."""
    rel_paths = files or COMPOSE_BOUNDARY_FILES
    findings: List[ComposeViolationFinding] = []
    for rel in rel_paths:
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        file_rel = rel.replace("\\", "/")

        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                findings.extend(_scan_block_for_findings(node.body, file_rel=file_rel))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                findings.extend(
                    _scan_block_for_findings(node.body, file_rel=file_rel)
                )
    return findings


def scan_responder_direct_template_returns(
    file_path: Optional[Path] = None,
) -> List[ComposeViolationFinding]:
    rel = "backend/modules/ai/brain/compose/responder.py"
    if file_path is not None:
        rel = str(file_path.relative_to(REPO_ROOT)).replace("\\", "/")
    return [
        f
        for f in scan_compose_boundary_violations([rel])
        if f.kind in {
            ViolationKind.DIRECT_TEMPLATE_RETURN,
            ViolationKind.ASSIGNED_TEMPLATE_RETURN,
        }
    ]


def scan_exact_prose_test_assertions(
    file_path: Optional[Path] = None,
) -> List[ExactProseTestAssertion]:
    path = file_path or (
        REPO_ROOT / "backend/tests/test_order_status_lookup_routing.py"
    )
    if not path.exists():
        return []
    findings: List[ExactProseTestAssertion] = []
    file_rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = _EXACT_TEMPLATE_ASSERT_RE.search(line)
        if match:
            findings.append(
                ExactProseTestAssertion(
                    file=file_rel,
                    line=lineno,
                    pattern=match.group(0),
                    template_call=match.group(1),
                )
            )
    return findings


def _waiver_matches_finding(violation: TrackedViolation, finding: ComposeViolationFinding) -> bool:
    if violation.path != finding.path:
        return False
    if violation.file != finding.file:
        return False
    return violation.action.endswith(finding.template_call) or violation.action.endswith(
        finding.detail
    )


def _waiver_matches_test_assertion(
    violation: TrackedViolation,
    assertion: ExactProseTestAssertion,
) -> bool:
    if violation.file != assertion.file:
        return False
    return (
        violation.action.endswith(assertion.template_call)
        or assertion.template_call in violation.action
    )


def classify_untracked_violations(
    findings: Sequence[ComposeViolationFinding],
) -> List[ComposeViolationFinding]:
    waived_paths = TRACKED_VIOLATION_PATHS | APPROVED_EXCEPTION_PATHS
    return [f for f in findings if f.path not in waived_paths]


def validate_live_violations_against_waivers(
    findings: Optional[Sequence[ComposeViolationFinding]] = None,
    test_assertions: Optional[Sequence[ExactProseTestAssertion]] = None,
) -> List[str]:
    """Ensure each waiver matches a live finding and each finding has waiver or approved exception."""
    live_findings = list(findings or scan_compose_boundary_violations())
    live_assertions = list(test_assertions or scan_exact_prose_test_assertions())
    errors: List[str] = []

    unmatched_waivers: List[str] = []
    for violation in TRACKED_VIOLATIONS:
        if violation.path in APPROVED_EXCEPTION_PATHS:
            errors.append(
                f"{violation.violation_id}: tracked violation must not use approved exception path"
            )
            continue
        if violation.path.startswith("test_"):
            if not any(_waiver_matches_test_assertion(violation, a) for a in live_assertions):
                unmatched_waivers.append(
                    f"{violation.violation_id}: stale waiver — no matching exact-prose test assertion"
                )
            continue
        if not any(_waiver_matches_finding(violation, f) for f in live_findings):
            unmatched_waivers.append(
                f"{violation.violation_id}: stale waiver — path/action no longer matches code"
            )

    errors.extend(unmatched_waivers)

    untracked = classify_untracked_violations(live_findings)
    for finding in untracked:
        errors.append(
            f"untracked constitutional violation: {finding.path} "
            f"({finding.kind.value}) @ {finding.file}:{finding.line} — {finding.detail}"
        )

    waived_test_paths = {v.path for v in TRACKED_VIOLATIONS if v.path.startswith("test_")}
    for assertion in live_assertions:
        if not any(
            _waiver_matches_test_assertion(v, assertion) for v in TRACKED_VIOLATIONS
        ):
            errors.append(
                f"untracked exact-prose test assertion @ {assertion.file}:{assertion.line}"
            )

    return errors


def validate_new_violation_cannot_self_waive(
    baseline: GovernanceBaseline,
    *,
    proposed_new_ids: Set[str],
) -> List[str]:
    """Simulate/feature-guard: new IDs must not be addable without baseline version bump."""
    errors: List[str] = []
    for vid in proposed_new_ids:
        if vid not in baseline.allowed_violation_ids:
            errors.append(
                f"{vid}: cannot be waived in an ordinary feature PR; "
                "requires governance_baseline_version bump and CODEOWNERS review"
            )
    return errors


def format_tracked_violation_report() -> str:
    lines = [
        "TRACKED VIOLATIONS — FAILING POLICY WITH TEMPORARY WAIVER (not approved exceptions):"
    ]
    for v in TRACKED_VIOLATIONS:
        lines.append(
            f"  [{v.violation_id}] {v.status} | {v.path} @ {v.file} "
            f"(owner={v.owner}, added_at={v.added_at}, expiry={v.expiry_date}, "
            f"removal={v.removal_ref}, approved_by={v.approved_by}) — {v.reason}"
        )
    return "\n".join(lines)


def format_approved_exception_report() -> str:
    lines = ["APPROVED DETERMINISTIC EXCEPTIONS (legitimate exact-text categories):"]
    for exc in DETERMINISTIC_EXCEPTIONS:
        lines.append(
            f"  [{exc.exception_id}] {exc.action_path} ({exc.exception_class}) "
            f"owner={exc.owner} — {exc.reason_exact_wording}"
        )
    return "\n".join(lines)
