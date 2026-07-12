"""Nahla Mandatory Natural Language Rule — enforceable policy registry.

Authoritative source: ``AGENTS.md`` (Mandatory Natural Language Rule).
This module is the closed allowlist + metadata contract used by CI.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]

# Closed compose_source values for normal AI customer replies.
APPROVED_COMPOSE_SOURCES: FrozenSet[str] = frozenset(
    {
        "llm",
        "persona_llm",  # legacy alias — LLM-owned wording
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


@dataclass(frozen=True)
class DeterministicException:
    exception_id: str
    category: str
    action_path: str
    reason_exact_wording: str
    owner: str
    approving_source: str
    exception_class: str


# Closed registry — changes require explicit review (diff-visible).
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
    line_hint: str
    owner: str
    expiry: str
    removal_pr: str
    reason: str


# Pre-existing violations — visible waivers; CI stays green only while tracked.
TRACKED_VIOLATIONS: Tuple[TrackedViolation, ...] = (
    TrackedViolation(
        violation_id="NL-V001",
        path="track_order_not_found",
        file="backend/modules/ai/brain/compose/responder.py",
        line_hint="order_not_found",
        owner="ai-platform",
        expiry="2026-08-31",
        removal_pr="fix/track-order-not-found-compose-compliance",
        reason=(
            "Normal AI path directly returns T.order_status_not_found() "
            "without LLM compose or fallback metadata"
        ),
    ),
    TrackedViolation(
        violation_id="NL-V002",
        path="track_order_need_order_number",
        file="backend/modules/ai/brain/compose/responder.py",
        line_hint="need_order_number",
        owner="ai-platform",
        expiry="2026-08-31",
        removal_pr="fix/track-order-not-found-compose-compliance",
        reason=(
            "Normal AI path directly returns T.track_order_need_identifiers() "
            "without LLM compose"
        ),
    ),
    TrackedViolation(
        violation_id="NL-T001",
        path="test_order_status_lookup_routing",
        file="backend/tests/test_order_status_lookup_routing.py",
        line_hint="assert reply == T.order_status_not_found()",
        owner="ai-platform",
        expiry="2026-08-31",
        removal_pr="fix/track-order-not-found-compose-compliance",
        reason="Test blesses exact normal-path Arabic instead of behavior/metadata",
    ),
)

TRACKED_VIOLATION_PATHS: FrozenSet[str] = frozenset(v.path for v in TRACKED_VIOLATIONS)
TRACKED_VIOLATION_IDS: FrozenSet[str] = frozenset(v.violation_id for v in TRACKED_VIOLATIONS)


@dataclass(frozen=True)
class DirectTemplateReturn:
    chosen_path: str
    template_call: str
    file: str
    line: int


@dataclass(frozen=True)
class ExactProseTestAssertion:
    file: str
    line: int
    pattern: str


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


def is_approved_exception_path(action_path: str) -> bool:
    return str(action_path or "").strip() in APPROVED_EXCEPTION_PATHS


def _is_templates_call(node: ast.AST) -> Optional[str]:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "T":
            return func.attr
    return None


def scan_responder_direct_template_returns(
    file_path: Optional[Path] = None,
) -> List[DirectTemplateReturn]:
    """Detect normal-path direct ``return T.<template>()`` after chosen_path assignment."""
    path = file_path or (REPO_ROOT / "backend/modules/ai/brain/compose/responder.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    findings: List[DirectTemplateReturn] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        chosen_path: Optional[str] = None
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Subscript)
            ):
                target = stmt.targets[0]
                if (
                    isinstance(target.value, ast.Attribute)
                    and target.value.attr == "data"
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "chosen_path"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    chosen_path = stmt.value.value
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                template_name = _is_templates_call(stmt.value)
                if template_name and chosen_path:
                    findings.append(
                        DirectTemplateReturn(
                            chosen_path=chosen_path,
                            template_call=template_name,
                            file=str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
                            line=stmt.lineno,
                        )
                    )
    return findings


_EXACT_TEMPLATE_ASSERT_RE = re.compile(
    r"assert\s+\w+\s*==\s*T\.(\w+)\(\)"
)


def scan_exact_prose_test_assertions(
    file_path: Optional[Path] = None,
) -> List[ExactProseTestAssertion]:
    path = file_path or (
        REPO_ROOT / "backend/tests/test_order_status_lookup_routing.py"
    )
    if not path.exists():
        return []
    findings: List[ExactProseTestAssertion] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = _EXACT_TEMPLATE_ASSERT_RE.search(line)
        if match:
            findings.append(
                ExactProseTestAssertion(
                    file=str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
                    line=lineno,
                    pattern=match.group(0),
                )
            )
    return findings


def classify_untracked_violations(
    findings: Sequence[DirectTemplateReturn],
) -> List[DirectTemplateReturn]:
    approved_paths = APPROVED_EXCEPTION_PATHS | TRACKED_VIOLATION_PATHS
    return [f for f in findings if f.chosen_path not in approved_paths]


def format_tracked_violation_report() -> str:
    lines = ["Tracked constitutional violations (waived until removal PR lands):"]
    for v in TRACKED_VIOLATIONS:
        lines.append(
            f"  [{v.violation_id}] {v.path} @ {v.file} "
            f"(owner={v.owner}, expiry={v.expiry}, removal={v.removal_pr}) — {v.reason}"
        )
    return "\n".join(lines)
