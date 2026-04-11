"""
backend/modules/ai/orchestrator/invoker.py
───────────────────────────────────────────
Shared safe invocation helper for the canonical AI orchestrator adapter.

Public surface:
  invoke_adapter(adapter, kwargs, caller_tag)  — returns payload or None

Used by:
  ai-engine/main.py
  services/ai-orchestrator/observability/shadow_pipeline.py

Purpose:
  Both callers previously called adapter.generate_ai_reply(**kwargs) inside
  their own try/except blocks. This module centralises the call + exception
  guard so the invocation contract is expressed and maintained in one place.

Invariants:
  - Never raises — all failures are caught and logged as WARNING.
  - Returns the AIReplyPayload on success, or None on any failure.
  - Does not inspect payload content — callers handle success/empty logic.
  - No route, schema, webhook, DB, or runtime state is modified here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

_logger = logging.getLogger("nahla.ai.orchestrator.invoker")


def invoke_adapter(
    adapter: Optional[Any],
    kwargs: Dict[str, Any],
    caller_tag: str = "nahla.ai",
) -> Optional[Any]:
    """
    Safely call adapter.generate_ai_reply(**kwargs).

    Parameters
    ----------
    adapter    : the pre-loaded adapter module, or None.
                 When None, returns None immediately (no call is made).
    kwargs     : dict built by build_adapter_kwargs() — passed as **kwargs.
    caller_tag : short identifier for the calling service used in WARNING
                 logs to preserve per-caller log context (e.g. "ai-engine",
                 "shadow-pipeline").

    Returns
    -------
    AIReplyPayload on success, or None if adapter is None or the call raised.

    Logging
    -------
    On failure : WARNING "[{caller_tag}] adapter call raised {exc} — skipped"
    Success / empty-reply logging is left to the caller.
    """
    if adapter is None:
        return None
    try:
        return adapter.generate_ai_reply(**kwargs)
    except Exception as exc:
        _logger.warning(
            "[%s] adapter call raised %r — skipped", caller_tag, exc
        )
        return None
