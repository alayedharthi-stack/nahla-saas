"""
Risk-bucket classification for outbound text debt audit.

Separates raw Arabic string literal counts from customer-facing outbound risk.
See ``scripts/audit_outbound_text_debt.py``.
"""
from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from core.outbound_text_allowlist import (
    LEGACY_DETECTION_MARKERS,
    classify_string_literal,
    is_allowed_technical_string,
)

# ── Audit buckets ────────────────────────────────────────────────────────────

BUCKET_OUTBOUND_CUSTOMER_FACING_RISK = "outbound_customer_facing_risk"
BUCKET_DETERMINISTIC_TEMPLATE = "deterministic_customer_facing_template"
BUCKET_SAFETY_NET = "safety_net_customer_text"
BUCKET_ORDER_FLOW = "order_flow_customer_prompt"
BUCKET_FALLBACK_HANDOFF = "fallback_or_handoff_claim"
BUCKET_META_TEMPLATE = "meta_whatsapp_template"
BUCKET_TECHNICAL_ALLOWLIST = "technical_allowlist"
BUCKET_REGEX_INTENT = "regex_or_intent_pattern"
BUCKET_PROMPT_ONLY = "prompt_only"
BUCKET_INTERNAL_TOOLING = "internal_tooling"
BUCKET_TEST_FIXTURE = "test_or_fixture"
BUCKET_LEGACY_DETECTION = "legacy_detection_constant"
BUCKET_UNKNOWN = "unknown_needs_review"

CUSTOMER_FACING_RISK_BUCKETS: FrozenSet[str] = frozenset({
    BUCKET_OUTBOUND_CUSTOMER_FACING_RISK,
    BUCKET_DETERMINISTIC_TEMPLATE,
    BUCKET_SAFETY_NET,
    BUCKET_ORDER_FLOW,
    BUCKET_FALLBACK_HANDOFF,
})

NOISE_BUCKETS: FrozenSet[str] = frozenset({
    BUCKET_REGEX_INTENT,
    BUCKET_PROMPT_ONLY,
    BUCKET_INTERNAL_TOOLING,
    BUCKET_TEST_FIXTURE,
    BUCKET_LEGACY_DETECTION,
    BUCKET_TECHNICAL_ALLOWLIST,
})

ALL_BUCKETS: Tuple[str, ...] = (
    BUCKET_OUTBOUND_CUSTOMER_FACING_RISK,
    BUCKET_DETERMINISTIC_TEMPLATE,
    BUCKET_SAFETY_NET,
    BUCKET_ORDER_FLOW,
    BUCKET_FALLBACK_HANDOFF,
    BUCKET_META_TEMPLATE,
    BUCKET_TECHNICAL_ALLOWLIST,
    BUCKET_REGEX_INTENT,
    BUCKET_PROMPT_ONLY,
    BUCKET_INTERNAL_TOOLING,
    BUCKET_TEST_FIXTURE,
    BUCKET_LEGACY_DETECTION,
    BUCKET_UNKNOWN,
)

# Paths excluded from scan (documented in report; not walked by audit).
AUDIT_EXCLUDED_PATH_PARTS: Tuple[str, ...] = (
    "__pycache__",
    "migrations",
    "dashboard",
    "billing",
    ".venv",
)

AUDIT_EXCLUDED_PATHS_DOC: Tuple[str, ...] = (
    "database KB (MerchantKnowledgeSection, tenant knowledge tables)",
    "manual_knowledge_base runtime (prompt injection, not .py literals)",
    "JSON / SQL / YAML configuration and fixtures",
    "backend/tests/ (outside scan roots)",
    "migrations/",
    "dashboard/dist/",
    "billing/",
)

# Known KB literal-reply paths (classification only — no runtime changes).
KB_RISK_FUNCTIONS: Dict[str, Tuple[str, str]] = {
    "faq_store_info": (
        "templates.faq_store_info",
        "merchant_structured_field_in_reply",
    ),
    "faq_owner_contact": (
        "templates.faq_owner_contact",
        "kb_text_in_reply",
    ),
    "faq_working_hours": (
        "templates.faq_working_hours",
        "kb_text_in_reply",
    ),
    "build_cod_policy_reply": (
        "cod_policy_evidence.build_cod_policy_reply",
        "kb_text_in_reply",
    ),
    "apply_outbound_artifact_guard": (
        "safety_nets.apply_outbound_artifact_guard",
        "merchant_structured_field_in_reply",
    ),
    "_build_location_reply": (
        "location_safety_net._build_location_reply",
        "kb_text_in_reply",
    ),
}

KB_DISCLAIMER_LINES: Tuple[str, ...] = (
    "Database KB is NOT included in this scan (only .py source under scan roots).",
    "manual_knowledge_base runtime is NOT included in this scan.",
    "Arabic text stored in KB is not debt by itself.",
    "Debt = sending KB or merchant field text literally to the customer via "
    "template/fallback without LLM compose.",
    "Distinguish: kb_text_in_prompt | kb_text_in_reply | "
    "merchant_structured_field_in_reply.",
)

_TEST_PATH_MARKERS: Tuple[str, ...] = (
    "/tests/",
    "/test_",
    "_test.py",
    "/conftest.py",
)

_META_TEMPLATE_MARKERS: Tuple[str, ...] = (
    "services/whatsapp_templates/",
)

_INTERNAL_TOOLING_MARKERS: Tuple[str, ...] = (
    "modules/ai/improvement_advisor",
    "modules/ai/observability",
    "modules/ai/truth_surface",
    "scripts/",
)

_PROMPT_ONLY_MARKERS: Tuple[str, ...] = (
    "prompt_state_serializer",
    "prompt_builder",
    "compose_goal",
    "knowledge_platform_slice",
    "modules/ai/brain/prompt/",
    "commerce_agent/context_builder",
    "manual_knowledge_base",
)

_REGEX_INTENT_MARKERS: Tuple[str, ...] = (
    "intent/rules.py",
    "handoff_detector",
    "link_intent",
    "social_classifier",
    "ordering_extractor",
    "intent_matcher",
    "text_normalize",
    "modules/ai/brain/decision/intent",
)

_TEMPLATE_FILE_SUFFIX = "modules/ai/brain/compose/templates.py"
_SAFETY_NETS_SUFFIX = "modules/ai/postprocess/safety_nets.py"
_ORDER_FLOW_MARKERS: Tuple[str, ...] = (
    "order_flow_v2/replies",
    "order_flow_v2/messages",
    "order_flow_v2/prompts",
)
_FALLBACK_MARKERS: Tuple[str, ...] = (
    "fallback_policy",
    "handoff_detector",
    "escalation_evidence",
    "clear_intent_fallback",
)

_OUTBOUND_SEND_MARKERS: Tuple[str, ...] = (
    "routers/whatsapp_webhook",
    "brain/compose/responder",
    "brain/execution/",
    "postprocess/",
    "routing/layer0_router",
    "brain/commerce/",
    "core/outbound_sanitizer",
    "core/outbound_text",
    "services/whatsapp",
)

_FUNC_DEF_RE = re.compile(r"^\s*def\s+(\w+)\s*\(")
_REGEX_LINE_HINTS: Tuple[str, ...] = (
    "re.compile",
    "regex",
    "pattern",
    "frozenset({",
    "frozenset(",
    "_RE =",
    "_PATTERN",
    "PATTERNS =",
    "MARKERS =",
    "FORBIDDEN_",
    "LEGACY_DETECTION",
)


def _norm_path(filepath: str) -> str:
    return filepath.replace("\\", "/").lower()


def _path_contains(path: str, markers: Iterable[str]) -> bool:
    return any(m.lower() in path for m in markers)


def is_test_path(filepath: str) -> bool:
    path = _norm_path(filepath)
    name = path.rsplit("/", 1)[-1]
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return _path_contains(path, _TEST_PATH_MARKERS)


def is_production_path(filepath: str) -> bool:
    return not is_test_path(filepath)


def should_skip_line(line: str) -> bool:
    """Skip comment-only and empty lines (not regex/pattern lines)."""
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith("#")


def is_regex_or_pattern_line(line: str) -> bool:
    lower = line.lower()
    if "re.compile" in lower or "regex" in lower:
        return True
    if any(h.lower() in lower for h in _REGEX_LINE_HINTS):
        return True
    if re.search(r'\br["\'][^"\']*[\u0600-\u06FF]', line):
        return True
    return False


def resolve_kb_risk_hint(
    *,
    filepath: str,
    current_function: Optional[str],
) -> Optional[Dict[str, str]]:
    if not current_function or current_function not in KB_RISK_FUNCTIONS:
        return None
    path_id, kb_category = KB_RISK_FUNCTIONS[current_function]
    return {
        "kb_risk_path": path_id,
        "kb_delivery_mode": kb_category,
    }


def classify_audit_finding(
    *,
    filepath: str,
    line_content: str,
    literal: str,
    current_function: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify one Arabic string literal for audit reporting."""
    path = _norm_path(filepath)
    base_kind = classify_string_literal(literal, filepath=filepath)
    kb_hint = resolve_kb_risk_hint(filepath=filepath, current_function=current_function)

    if is_test_path(filepath):
        bucket = BUCKET_TEST_FIXTURE
    elif base_kind == "legacy_detection_constant" or literal.strip() in LEGACY_DETECTION_MARKERS:
        bucket = BUCKET_LEGACY_DETECTION
    elif base_kind == "allowed_technical" or is_allowed_technical_string(literal):
        bucket = BUCKET_TECHNICAL_ALLOWLIST
    elif _path_contains(path, _META_TEMPLATE_MARKERS):
        bucket = BUCKET_META_TEMPLATE
    elif _path_contains(path, _REGEX_INTENT_MARKERS) or is_regex_or_pattern_line(line_content):
        bucket = BUCKET_REGEX_INTENT
    elif _path_contains(path, _PROMPT_ONLY_MARKERS):
        bucket = BUCKET_PROMPT_ONLY
    elif _path_contains(path, _INTERNAL_TOOLING_MARKERS):
        bucket = BUCKET_INTERNAL_TOOLING
    elif path.endswith(_TEMPLATE_FILE_SUFFIX.lower()) or path.endswith("templates.py"):
        bucket = BUCKET_DETERMINISTIC_TEMPLATE
    elif _SAFETY_NETS_SUFFIX in path:
        bucket = BUCKET_SAFETY_NET if not is_regex_or_pattern_line(line_content) else BUCKET_REGEX_INTENT
    elif _path_contains(path, _ORDER_FLOW_MARKERS):
        bucket = BUCKET_ORDER_FLOW
    elif _path_contains(path, _FALLBACK_MARKERS):
        bucket = BUCKET_FALLBACK_HANDOFF
    elif _path_contains(path, _OUTBOUND_SEND_MARKERS):
        bucket = BUCKET_OUTBOUND_CUSTOMER_FACING_RISK
    elif base_kind == "internal_only":
        bucket = BUCKET_INTERNAL_TOOLING
    elif not _path_contains(path, _OUTBOUND_SEND_MARKERS):
        # Arabic literals in scanned .py that are not on outbound send paths.
        bucket = BUCKET_INTERNAL_TOOLING
    else:
        bucket = BUCKET_UNKNOWN

    # layer0_router: flag-gated at runtime; still outbound risk when present.
    if "routing/layer0_router" in path and bucket == BUCKET_UNKNOWN:
        bucket = BUCKET_OUTBOUND_CUSTOMER_FACING_RISK
        kb_hint = kb_hint or {
            "kb_risk_path": "routing.layer0_router",
            "kb_delivery_mode": "kb_text_in_reply",
            "runtime_note": "active only when layer0 flag ON",
        }

    result: Dict[str, Any] = {
        "bucket": bucket,
        "base_kind": base_kind,
    }
    if kb_hint:
        result.update(kb_hint)
    return result


def parse_current_function(line: str, previous: Optional[str]) -> Optional[str]:
    m = _FUNC_DEF_RE.match(line)
    if m:
        return m.group(1)
    return previous


def build_summary(
    findings: List[Dict[str, Any]],
    *,
    raw_arabic_string_count: int,
    scanned_paths: List[str],
) -> Dict[str, Any]:
    """Aggregate audit metrics from classified findings."""
    by_bucket: Dict[str, int] = {b: 0 for b in ALL_BUCKETS}
    risk_strings: List[str] = []
    all_previews: List[str] = []
    production_count = 0
    tests_count = 0
    by_file_risk: Dict[str, int] = {}
    by_file_noise: Dict[str, int] = {}

    for f in findings:
        bucket = f.get("bucket", BUCKET_UNKNOWN)
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
        preview = f.get("preview", "")
        all_previews.append(preview)
        fp = f.get("file", "")
        if is_test_path(fp):
            tests_count += 1
        else:
            production_count += 1
        if bucket in CUSTOMER_FACING_RISK_BUCKETS:
            risk_strings.append(preview)
            by_file_risk[fp] = by_file_risk.get(fp, 0) + 1
        if bucket in NOISE_BUCKETS:
            by_file_noise[fp] = by_file_noise.get(fp, 0) + 1

    unique_all = len(set(all_previews))
    unique_risk = len(set(risk_strings))
    total = len(findings)

    kb_risk_findings = [
        {
            "file": f.get("file"),
            "line": f.get("line"),
            "kb_risk_path": f.get("kb_risk_path"),
            "kb_delivery_mode": f.get("kb_delivery_mode"),
            "preview": f.get("preview", "")[:80],
        }
        for f in findings
        if f.get("kb_risk_path")
    ]

    return {
        "total_findings": total,
        "raw_arabic_string_count": raw_arabic_string_count,
        "production_code_count": production_count,
        "tests_count": tests_count,
        "actual_customer_facing_risk_count": sum(
            by_bucket.get(b, 0) for b in CUSTOMER_FACING_RISK_BUCKETS
        ),
        "unique_customer_facing_risk_count": unique_risk,
        "regex_or_intent_count": by_bucket.get(BUCKET_REGEX_INTENT, 0),
        "prompt_only_count": by_bucket.get(BUCKET_PROMPT_ONLY, 0),
        "internal_only_count": (
            by_bucket.get(BUCKET_INTERNAL_TOOLING, 0)
            + by_bucket.get(BUCKET_TEST_FIXTURE, 0)
        ),
        "technical_allowlist_count": by_bucket.get(BUCKET_TECHNICAL_ALLOWLIST, 0),
        "meta_template_count": by_bucket.get(BUCKET_META_TEMPLATE, 0),
        "duplicates_count": max(0, total - unique_all),
        "by_bucket": {k: v for k, v in by_bucket.items() if v > 0},
        "scanned_paths": scanned_paths,
        "excluded_paths": list(AUDIT_EXCLUDED_PATHS_DOC),
        "kb_disclaimer": list(KB_DISCLAIMER_LINES),
        "kb_literal_reply_risk_paths": kb_risk_findings[:50],
        "top_risk_files": sorted(by_file_risk.items(), key=lambda x: -x[1])[:10],
        "top_noise_files": sorted(by_file_noise.items(), key=lambda x: -x[1])[:10],
    }


__all__ = [
    "ALL_BUCKETS",
    "AUDIT_EXCLUDED_PATH_PARTS",
    "AUDIT_EXCLUDED_PATHS_DOC",
    "BUCKET_DETERMINISTIC_TEMPLATE",
    "BUCKET_FALLBACK_HANDOFF",
    "BUCKET_INTERNAL_TOOLING",
    "BUCKET_LEGACY_DETECTION",
    "BUCKET_META_TEMPLATE",
    "BUCKET_ORDER_FLOW",
    "BUCKET_OUTBOUND_CUSTOMER_FACING_RISK",
    "BUCKET_PROMPT_ONLY",
    "BUCKET_REGEX_INTENT",
    "BUCKET_SAFETY_NET",
    "BUCKET_TECHNICAL_ALLOWLIST",
    "BUCKET_TEST_FIXTURE",
    "BUCKET_UNKNOWN",
    "CUSTOMER_FACING_RISK_BUCKETS",
    "KB_DISCLAIMER_LINES",
    "KB_RISK_FUNCTIONS",
    "build_summary",
    "classify_audit_finding",
    "is_production_path",
    "is_test_path",
    "parse_current_function",
    "should_skip_line",
]
