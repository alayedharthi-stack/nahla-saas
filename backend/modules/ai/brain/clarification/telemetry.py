"""
clarification/telemetry.py
──────────────────────────
Structured logging for shadow (Phase 0) and live (Phase 1) clarification routing.

Grep production logs:
  [CLARIFICATION_SHADOW]
  [CLARIFICATION]
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .types import ClarificationSpec

logger = logging.getLogger("nahla.brain.clarification")


def log_clarification_shadow(
    *,
    tenant_id: Any = None,
    spec: ClarificationSpec,
    legacy_action: str = "",
    legacy_reason: str = "",
    would_action: str = "",
    preview: str = "",
    flag_enabled: bool = False,
) -> None:
    """Phase 0 — classify without changing production behavior."""
    try:
        logger.info(
            "[CLARIFICATION_SHADOW] tenant=%s trigger=%s class=%s mode=%s "
            "compose_topic=%s legacy_action=%s would_action=%s flag=%s preview=%r",
            tenant_id if tenant_id is not None else "-",
            spec.trigger or spec.evidence.get("trigger") or "-",
            spec.ambiguity_class,
            spec.recovery_mode,
            spec.compose_topic if spec.is_generative else "-",
            legacy_action or "-",
            would_action or "-",
            int(bool(flag_enabled)),
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def log_clarification_routed(
    *,
    tenant_id: Any = None,
    spec: ClarificationSpec,
    action: str = "",
    reason: str = "",
    preview: str = "",
) -> None:
    """Phase 1 — live contextual clarify routing (flag on)."""
    try:
        logger.info(
            "[CLARIFICATION] tenant=%s trigger=%s class=%s mode=%s "
            "action=%s reason=%s preview=%r",
            tenant_id if tenant_id is not None else "-",
            spec.trigger or spec.evidence.get("trigger") or "-",
            spec.ambiguity_class,
            spec.recovery_mode,
            action or "-",
            (reason or "-")[:120],
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def log_clarification_skipped(
    *,
    tenant_id: Any = None,
    trigger: str = "",
    reason: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[CLARIFICATION] tenant=%s trigger=%s skipped=1 reason=%s preview=%r",
            tenant_id if tenant_id is not None else "-",
            trigger or "-",
            reason or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def log_clarification_leak_event(
    *,
    tenant_id: Any = None,
    source: str = "",
    normalized_subject: str = "",
    resolved_query: str = "",
    preview: str = "",
    blocked_text: str = "",
) -> None:
    """Production validation — grep ``[CLARIFICATION_LEAK]``."""
    from .resolved_product_guard import log_clarification_leak  # noqa: PLC0415

    log_clarification_leak(
        tenant_id=tenant_id,
        source=source,
        normalized_subject=normalized_subject,
        resolved_query=resolved_query,
        preview=preview,
        blocked_text=blocked_text,
    )


__all__ = [
    "log_clarification_leak_event",
    "log_clarification_routed",
    "log_clarification_shadow",
    "log_clarification_skipped",
]
