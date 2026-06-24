"""
Temporary allowlist for outbound text audit — Phase 1.

Only technical bodies, CTA labels, sanitizer fallbacks, and internal
detection constants belong here. Everything else that reaches the
customer is tracked as ``customer_facing_text_debt``.

See ``outbound_text_policy.py`` and ``tests/test_outbound_text_debt_audit.py``.
"""
from __future__ import annotations

import re
from typing import FrozenSet, Iterable, Tuple

# ── Allowed technical strings (may reach customer) ───────────────────────────

ALLOWED_TECHNICAL_STRINGS: FrozenSet[str] = frozenset({
    ".",  # Meta catalog interactive body minimum (catalog_body_policy)
    "فتح الرابط",
    "افتح المتجر",
    "عرض المنتج",
    "إتمام الدفع",
    "تتبع الطلب",
    "موقع المتجر",
    "فتح المتجر الإلكتروني",
    "أعتذر، حصل خلل بسيط في الرد. لو تكرر معك، أعد السؤال وأنا معك 🌷",
})

# Prefixes / patterns for technical-only copy (CTA titles, truncated labels).
ALLOWED_TECHNICAL_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^فتح الرابط…?$"),
    re.compile(r"^\.{1,3}$"),
)

# Detection-only constants — must NOT be sent to customers as outbound prose.
LEGACY_DETECTION_MARKERS: FrozenSet[str] = frozenset({
    "تفضّل، اختر من الكتالوج",
    "تفضل، اختر من الكتالوج",
    "تفضّل المنتج",
    "تفضل المنتج",
})

# Scan roots for debt audit (relative to backend/).
AUDIT_SCAN_ROOTS: Tuple[str, ...] = (
    "modules/ai",
    "core",
    "routers",
    "services",
)

# File suffixes to scan.
AUDIT_FILE_SUFFIXES: Tuple[str, ...] = (".py",)

# Heuristic: Arabic customer-facing string in source (3+ Arabic letters).
_ARABIC_PROSE_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]{3,}")

# Lines that are clearly internal (regex, comments patterns, log keys).
_INTERNAL_LINE_HINTS: Tuple[str, ...] = (
    "re.compile",
    "regex",
    "pattern",
    "frozenset",
    "LEGACY_DETECTION",
    "FORBIDDEN_",
    "_RE =",
    "normalize_arabic",
    "logger.",
    "# ",
    "assert ",
    "pytest",
    "test_",
)


def is_allowed_technical_string(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if s in ALLOWED_TECHNICAL_STRINGS:
        return True
    if s in LEGACY_DETECTION_MARKERS:
        return False
    return any(p.match(s) for p in ALLOWED_TECHNICAL_PATTERNS)


def classify_string_literal(value: str, *, filepath: str = "") -> str:
    """Classify a hardcoded Arabic string found in source.

    Returns one of:
      ``allowed_technical`` | ``legacy_detection_constant`` |
      ``internal_only`` | ``deterministic_customer_facing_debt`` |
      ``forbidden_new_prose``
    """
    s = str(value or "").strip()
    if not s:
        return "internal_only"
    if is_allowed_technical_string(s):
        return "allowed_technical"
    if not _ARABIC_PROSE_RE.search(s):
        return "internal_only"
    if s in LEGACY_DETECTION_MARKERS:
        return "legacy_detection_constant"
    if "allowlist" in filepath.replace("\\", "/").lower():
        return "internal_only"
    if any(h in s for h in ("tenant=", "err=", "[", "def ", "import ")):
        return "internal_only"
    return "deterministic_customer_facing_debt"


def extract_arabic_string_literals(source: str) -> Iterable[str]:
    """Yield double-quoted string literals containing Arabic."""
    for m in re.finditer(r'(?:"|\')((?:[^"\'\\]|\\.)*)(?:"|\')', source):
        lit = m.group(1)
        if _ARABIC_PROSE_RE.search(lit):
            yield lit


def is_likely_internal_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("#"):
        return True
    lower = stripped.lower()
    return any(h.lower() in lower for h in _INTERNAL_LINE_HINTS)


__all__ = [
    "ALLOWED_TECHNICAL_STRINGS",
    "AUDIT_FILE_SUFFIXES",
    "AUDIT_SCAN_ROOTS",
    "LEGACY_DETECTION_MARKERS",
    "classify_string_literal",
    "extract_arabic_string_literals",
    "is_allowed_technical_string",
    "is_likely_internal_line",
]
