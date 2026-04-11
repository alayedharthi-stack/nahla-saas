"""
backend/modules/ai/orchestrator/loader.py
──────────────────────────────────────────
Shared adapter loader for the canonical Nahla AI orchestrator.

Public surface:
  load_orchestrator_adapter(caller_tag)  — returns adapter module or None

Used by:
  ai-engine/main.py
  services/ai-orchestrator/api/routes.py

Purpose:
  Both callers previously contained identical inline dynamic-import blocks
  that load modules.ai.orchestrator.adapter and verify generate_ai_reply.
  This module centralises that logic so it is defined and maintained once.

Invariants:
  - Never raises — all failures are caught and logged.
  - Returns the adapter module on success, or None on any failure.
  - A None return means the caller falls back to its own legacy path.
  - No runtime state is modified; no DB access; no side effects.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Optional

_logger = logging.getLogger("nahla.ai.orchestrator.loader")

_ADAPTER_MODULE = "modules.ai.orchestrator.adapter"
_REQUIRED_CALLABLE = "generate_ai_reply"


def load_orchestrator_adapter(caller_tag: str = "nahla.ai") -> Optional[Any]:
    """
    Attempt to import the canonical AI orchestrator adapter.

    Parameters
    ----------
    caller_tag : str
        Short identifier for the calling service, used in log messages to
        preserve the original per-caller log context (e.g. "ai-engine",
        "shadow-pipeline").

    Returns
    -------
    The imported adapter module if loading succeeds and generate_ai_reply
    is callable, otherwise None.

    Logging
    -------
    On success : INFO  "[{caller_tag}] Orchestrator adapter loaded — active"
    On failure : WARNING "[{caller_tag}] Orchestrator adapter unavailable ({exc})"
    """
    try:
        adapter = importlib.import_module(_ADAPTER_MODULE)
        assert callable(getattr(adapter, _REQUIRED_CALLABLE, None)), (
            f"{_REQUIRED_CALLABLE} not found in {_ADAPTER_MODULE}"
        )
        _logger.info("[%s] Orchestrator adapter loaded — active", caller_tag)
        return adapter
    except Exception as exc:
        _logger.warning(
            "[%s] Orchestrator adapter unavailable (%s)", caller_tag, exc
        )
        return None
