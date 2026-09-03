"""GOV-002 machine-readable intelligence non-interference registry.

The trusted CI scanner is ``scripts/lint_intelligence_non_interference.py``.
That script is stdlib-only and must be loaded from BASE, not HEAD.

This module is the documented class/path contract. It does not authorize
exceptions by itself. Exceptions are JSON loaded from BASE by the scanner.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, FrozenSet, List, Mapping, Optional, Sequence, Tuple

CHANGE_CLASSES: Tuple[str, ...] = (
    "MODEL_CHANGE",
    "PROMPT_CHANGE",
    "PERSONA_CHANGE",
    "PHRASE_MAP_CHANGE",
    "KEYWORD_ROUTER_CHANGE",
    "CUSTOMER_REGEX_CHANGE",
    "CANNED_REPLY_CHANGE",
    "TENANT_SPECIFIC_SEMANTIC_CHANGE",
    "PHONE_SPECIFIC_SEMANTIC_CHANGE",
    "PRODUCT_SPECIFIC_SEMANTIC_CHANGE",
    "SAME_PR_SELF_WAIVER",
    "GOVERNANCE_CORE_CHANGE",
    "PROTECTED_CONTRACT_REMOVAL",
    "PROTECTED_CONTRACT_WEAKENING",
    "UNSAFE_PARTIAL_REPAIR",
    "BASE_NOT_AVAILABLE",
)


class ChangeClass(str, Enum):
    MODEL_CHANGE = "MODEL_CHANGE"
    PROMPT_CHANGE = "PROMPT_CHANGE"
    PERSONA_CHANGE = "PERSONA_CHANGE"
    PHRASE_MAP_CHANGE = "PHRASE_MAP_CHANGE"
    KEYWORD_ROUTER_CHANGE = "KEYWORD_ROUTER_CHANGE"
    CUSTOMER_REGEX_CHANGE = "CUSTOMER_REGEX_CHANGE"
    CANNED_REPLY_CHANGE = "CANNED_REPLY_CHANGE"
    TENANT_SPECIFIC_SEMANTIC_CHANGE = "TENANT_SPECIFIC_SEMANTIC_CHANGE"
    PHONE_SPECIFIC_SEMANTIC_CHANGE = "PHONE_SPECIFIC_SEMANTIC_CHANGE"
    PRODUCT_SPECIFIC_SEMANTIC_CHANGE = "PRODUCT_SPECIFIC_SEMANTIC_CHANGE"
    SAME_PR_SELF_WAIVER = "SAME_PR_SELF_WAIVER"
    GOVERNANCE_CORE_CHANGE = "GOVERNANCE_CORE_CHANGE"
    PROTECTED_CONTRACT_REMOVAL = "PROTECTED_CONTRACT_REMOVAL"
    PROTECTED_CONTRACT_WEAKENING = "PROTECTED_CONTRACT_WEAKENING"
    UNSAFE_PARTIAL_REPAIR = "UNSAFE_PARTIAL_REPAIR"
    BASE_NOT_AVAILABLE = "BASE_NOT_AVAILABLE"


SEMANTIC_SURFACE_PREFIXES: Tuple[str, ...] = (
    "backend/modules/ai/brain/intent/",
    "backend/modules/ai/brain/interpret/",
    "backend/modules/ai/brain/decision/",
    "backend/modules/ai/brain/state/",
    "backend/modules/ai/brain/turn/",
    "backend/modules/ai/brain/commerce/",
    "backend/modules/ai/knowledge/",
)

SEMANTIC_SURFACE_FILES: Tuple[str, ...] = (
    "backend/modules/ai/brain/pipeline.py",
)

MODEL_SELECTION_PREFIXES: Tuple[str, ...] = (
    "backend/modules/ai/orchestrator/providers/",
)

MODEL_SELECTION_FILES: Tuple[str, ...] = (
    "backend/modules/ai/orchestrator/customer_chat_models.py",
    "backend/modules/ai/orchestrator/provider_router.py",
    "backend/modules/ai/orchestrator/llm_cost_audit.py",
)

PROMPT_INSTRUCTION_PREFIXES: Tuple[str, ...] = (
    "backend/modules/ai/prompts/",
    "backend/modules/ai/brain/persona/prompts.py",
    "services/ai-orchestrator/prompt/",
)

PROMPT_INSTRUCTION_FILES: Tuple[str, ...] = (
    "backend/modules/ai/brain/compose/prompt_builder.py",
    "backend/modules/ai/brain/intent/coupon_capability_probe.py",
)

# Structured evidence / fact projection — not automatically PROMPT_CHANGE.
EVIDENCE_PROJECTION_FILES: FrozenSet[str] = frozenset(
    {
        "backend/modules/ai/brain/commerce/customer_order_evidence.py",
        "backend/modules/ai/brain/compose/prompt_state_serializer.py",
        "backend/modules/ai/brain/compose/prompt_payload_slim.py",
    }
)

PERSONA_PREFIXES: Tuple[str, ...] = (
    "backend/modules/ai/brain/persona/",
)

PERSONA_FILES: Tuple[str, ...] = (
    "backend/modules/ai/brain/persona_expression.py",
    "backend/modules/ai/brain/persona_ownership.py",
    "backend/modules/ai/prompts/nahla_persona.py",
)

CANNED_REPLY_FILES: Tuple[str, ...] = (
    "backend/modules/ai/brain/compose/responder.py",
    "backend/modules/ai/brain/compose/templates.py",
)

GOVERNANCE_CORE_PATHS: Tuple[str, ...] = (
    "scripts/lint_intelligence_non_interference.py",
    "backend/modules/ai/governance/intelligence_non_interference.py",
    "backend/modules/ai/governance/intelligence_exceptions.json",
    "backend/modules/ai/governance/__init__.py",
    "backend/tests/test_intelligence_non_interference_guard.py",
    "backend/tests/test_constitution_compliance.py",
    ".github/workflows/ci.yml",
)

GOVERNANCE_DOC_PATHS: FrozenSet[str] = frozenset(
    {
        "AGENTS.md",
        "docs/engineering/intelligence-non-interference-policy.md",
        "docs/engineering/ai-pr-constitution-checklist.md",
        "pytest.ini",
        ".github/CODEOWNERS",
    }
)

PROTECTED_CONTRACT_MODULES: Tuple[str, ...] = (
    "backend/tests/test_order_support_d1_natural_ownership.py",
    "backend/tests/test_order_support_d1b_turn_arbiter_preservation.py",
    "backend/tests/test_product_attribute_questions_order_flow.py",
    "backend/tests/test_product_correction_topic_shift.py",
    "backend/tests/test_order_history_intent_routing.py",
    "backend/tests/test_commerce_contract_preserve_order_support.py",
    "backend/tests/test_post_decision_order_support_preservation.py",
)

EXCEPTIONS_PATH = "backend/modules/ai/governance/intelligence_exceptions.json"

OWNERSHIP_PRODUCTION_PREFIXES: Tuple[str, ...] = (
    "backend/modules/ai/brain/state/",
    "backend/modules/ai/brain/commerce/",
    "backend/modules/ai/brain/decision/",
    "backend/modules/ai/brain/turn/",
    "backend/modules/ai/brain/intent/",
)

HISTORICAL_SCOPED_EXCEPTIONS: Tuple[Mapping[str, Any], ...] = (
    {
        "exception_id": "HIST-GOV001-02aff345-coupon-probe",
        "change_class": "PROMPT_CHANGE",
        "commit": "02aff3455c777b2d7cc6a4d4a234ae1b0b0b3c00",
        "exact_file_scope": (
            "backend/modules/ai/brain/intent/coupon_capability_probe.py",
        ),
        "exact_reason": (
            "Owner-approved GOV-001 exception: tighten "
            "COUPON_CAPABILITY_PROBE_SYSTEM semantic definition only"
        ),
        "created_at": "2026-09-01",
        "expires_at": "2026-09-01",
        "permanent": False,
        "historical_only": True,
    },
)


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    change_class: str
    reason: str
    authorized_exception_id: str = ""
    diff_hunk: str = ""

    def as_dict(self) -> dict:
        return {
            "FILE": self.file,
            "LINE": self.line,
            "CHANGE_CLASS": self.change_class,
            "REASON": self.reason,
            "AUTHORIZED_EXCEPTION_ID": self.authorized_exception_id,
            "DIFF_HUNK": self.diff_hunk,
        }


@dataclass(frozen=True)
class OwnerException:
    exception_id: str
    change_class: str
    exact_file_scope: Tuple[str, ...]
    exact_reason: str
    owner_approval_ref: str
    created_at: str
    expires_at: str


def load_exceptions_from_text(raw: str) -> List[OwnerException]:
    if not (raw or "").strip():
        return []
    payload = json.loads(raw)
    rows = payload.get("exceptions") if isinstance(payload, dict) else payload
    out: List[OwnerException] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        scope = row.get("exact_file_scope") or []
        if isinstance(scope, str):
            scope = [scope]
        out.append(
            OwnerException(
                exception_id=str(row.get("exception_id") or ""),
                change_class=str(row.get("change_class") or ""),
                exact_file_scope=tuple(str(s) for s in scope),
                exact_reason=str(row.get("exact_reason") or ""),
                owner_approval_ref=str(row.get("owner_approval_ref") or ""),
                created_at=str(row.get("created_at") or ""),
                expires_at=str(row.get("expires_at") or ""),
            )
        )
    return out


def exception_is_active(exc: OwnerException, *, as_of: Optional[date] = None) -> bool:
    if not exc.exception_id or not exc.change_class:
        return False
    as_of = as_of or date.today()
    if exc.expires_at:
        try:
            exp = datetime.strptime(exc.expires_at, "%Y-%m-%d").date()
        except ValueError:
            return False
        if as_of > exp:
            return False
    return True


def match_exception(
    exceptions: Sequence[OwnerException],
    *,
    change_class: str,
    file: str,
    as_of: Optional[date] = None,
) -> Optional[OwnerException]:
    posix = file.replace("\\", "/")
    for exc in exceptions:
        if exc.change_class != change_class:
            continue
        if not exception_is_active(exc, as_of=as_of):
            continue
        if posix in exc.exact_file_scope:
            return exc
    return None
