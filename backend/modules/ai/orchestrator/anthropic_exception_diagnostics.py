"""
anthropic_exception_diagnostics.py
──────────────────────────────────
PII-safe exception-chain fields for Anthropic runtime logging.

Logs only exception type names and stable errno/category hints — never
messages, URLs, headers, API keys, prompts, or response bodies.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def anthropic_exception_diagnostics(exc: BaseException) -> Dict[str, Any]:
    """Return structured, safe diagnostics for an exception chain."""
    cause = exc.__cause__
    context = exc.__context__
    category = _safe_category(exc)
    if category is None and cause is not None:
        category = _safe_category(cause)
    if category is None and context is not None and context is not cause:
        category = _safe_category(context)
    return {
        "exc_type": type(exc).__name__,
        "cause_type": type(cause).__name__ if cause is not None else None,
        "context_type": type(context).__name__ if context is not None else None,
        "category": category,
    }


def _safe_category(exc: Optional[BaseException]) -> Optional[str]:
    if exc is None:
        return None
    if isinstance(exc, OSError):
        errno = getattr(exc, "errno", None)
        if errno is not None:
            return f"errno_{errno}"
    return None
