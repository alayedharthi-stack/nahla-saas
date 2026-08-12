"""
Pack A3 — customer-ready MKS completeness (runtime eligibility).

Pure, IO-free verdict shared by:
  - merchant_policy_existence (KNOWN_PRESENT requires ready evidence)
  - merchant_document_retrieval (customer grounding excludes incomplete)

Incomplete authoring/template content must not become customer policy truth.
Brackets alone are never sufficient — authoring/template intent is required.
A non-empty substantive body is required; title alone never establishes readiness.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

ReadinessStatus = Literal[
    "READY",
    "INCOMPLETE_AUTHORING_TEMPLATE",
    "EMPTY_CONTENT",
]

READY: ReadinessStatus = "READY"
INCOMPLETE_AUTHORING_TEMPLATE: ReadinessStatus = "INCOMPLETE_AUTHORING_TEMPLATE"
EMPTY_CONTENT: ReadinessStatus = "EMPTY_CONTENT"

# Explicit unfinished markers (Latin / common generator vocabulary).
_EXPLICIT_MARKER_RE = re.compile(
    r"(?:"
    r"\bTODO\b|"
    r"\bFIXME\b|"
    r"\bTBD\b|"
    r"\bPLACEHOLDER\b|"
    r"\bTEMPLATE\b|"
    r"\[\s*TODO\s*\]|"
    r"\[\s*PLACEHOLDER\s*\]|"
    r"\[\s*TEMPLATE\s*\]|"
    r"\{\{\s*TODO\s*\}\}|"
    r"__PLACEHOLDER__|"
    r"<placeholder>"
    r")",
    re.IGNORECASE,
)

# Bracket chunks only — inspected for authoring intent (not rejected alone).
_BRACKET_CHUNK_RE = re.compile(r"\[[^\]]{1,240}\]")

# Authoring / fill-in instruction vocabulary inside brackets.
# Ordinary prose using أضف / مثلاً outside this context must remain READY.
_BRACKET_AUTHORING_RE = re.compile(
    r"(?:"
    # Arabic instructional verbs / fill-in cues
    r"أضف|"
    r"أدخل|"
    r"أدرج|"
    r"ضع|"
    r"اكتب|"
    r"أكمل|"
    r"اكمل|"
    r"املأ|"
    r"إملأ|"
    r"عبّئ|"
    r"عبئ|"
    r"عدّل|"
    r"عدل|"
    r"استبدل|"
    r"هنا\s*(?:النص|الرابط|القيمة|المدة)?|"
    r"اكتب\s*هنا|"
    # English instructional / generator cues inside brackets
    r"\binsert\b|"
    r"\badd\s+(?:here|your|the)\b|"
    r"\bwrite\s+(?:here|your)\b|"
    r"\byour\s+(?:text|link|url|value|duration)\b|"
    r"\benter\s+(?:here|your|the)\b|"
    r"\bfill\s+in\b|"
    r"\bTODO\b|"
    r"\bPLACEHOLDER\b|"
    r"\bTEMPLATE\b|"
    r"\bTBD\b|"
    r"\bFIXME\b"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MksCustomerReadinessVerdict:
    """Runtime-only readiness; never persisted as a DB status."""

    status: ReadinessStatus
    reason_code: Optional[str] = None

    @property
    def is_ready(self) -> bool:
        return self.status == READY

    @property
    def is_incomplete(self) -> bool:
        return self.status == INCOMPLETE_AUTHORING_TEMPLATE

    @property
    def is_empty(self) -> bool:
        return self.status == EMPTY_CONTENT


def _authoring_template_verdict(text: str) -> Optional[MksCustomerReadinessVerdict]:
    """Return incomplete verdict when authoring/template markers are present."""
    if not text:
        return None
    if _EXPLICIT_MARKER_RE.search(text):
        return MksCustomerReadinessVerdict(
            INCOMPLETE_AUTHORING_TEMPLATE,
            reason_code="template_marker",
        )
    for match in _BRACKET_CHUNK_RE.finditer(text):
        chunk = match.group(0)
        inner = chunk[1:-1].strip()
        if not inner:
            continue
        if _BRACKET_AUTHORING_RE.search(inner):
            if re.search(r"أضف", inner) and re.search(r"مثلاً|مثلا", inner):
                return MksCustomerReadinessVerdict(
                    INCOMPLETE_AUTHORING_TEMPLATE,
                    reason_code="bracket_add_instruction",
                )
            if re.search(r"أضف|أدخل|ضع|اكتب|أدرج|أكمل|اكمل|املأ|إملأ", inner):
                return MksCustomerReadinessVerdict(
                    INCOMPLETE_AUTHORING_TEMPLATE,
                    reason_code="unfinished_authoring_instruction",
                )
            return MksCustomerReadinessVerdict(
                INCOMPLETE_AUTHORING_TEMPLATE,
                reason_code="example_placeholder",
            )
    return None


def assess_mks_customer_readiness(
    body: Optional[str],
    *,
    title: Optional[str] = None,
) -> MksCustomerReadinessVerdict:
    """Return customer-readiness verdict for MKS text.

    Source-agnostic and IO-free.
    READY requires a non-empty substantive body. Title alone never rescues.
    Title may still contribute template-marker detection, but never eligibility.
    """
    title_text = str(title or "").strip()
    body_text = str(body or "").strip()

    # Title may expose unfinished authoring markers even when body is empty,
    # but empty/whitespace body is never customer-groundable.
    scan_text = f"{title_text}\n{body_text}".strip()
    incomplete = _authoring_template_verdict(scan_text)
    if incomplete is not None:
        return incomplete

    if not body_text:
        reason = "empty_body"
        if title_text:
            reason = "title_only_empty_body"
        elif body is not None and str(body) != "" and not str(body).strip():
            reason = "whitespace_body"
        return MksCustomerReadinessVerdict(
            EMPTY_CONTENT,
            reason_code=reason,
        )

    return MksCustomerReadinessVerdict(READY)


def mks_section_customer_ready(section: Any) -> MksCustomerReadinessVerdict:
    """Assess readiness from a MerchantKnowledgeSection-like object."""
    return assess_mks_customer_readiness(
        getattr(section, "body", None),
        title=getattr(section, "title", None),
    )


def is_mks_customer_ready(section: Any) -> bool:
    """True when the section may contribute PRESENT / customer retrieval."""
    try:
        return mks_section_customer_ready(section).is_ready
    except Exception:  # noqa: BLE001 — fail closed: incomplete must not become eligible
        return False
