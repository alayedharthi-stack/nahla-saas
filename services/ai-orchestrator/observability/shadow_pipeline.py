"""
services/ai-orchestrator/observability/shadow_pipeline.py
──────────────────────────────────────────────────────────
Shadow observability helpers for the AI orchestrator service.

Public surface (imported by api/routes.py):
  observe_shadow_pipeline(adapter, req, ctx, final_reply)

Internal helpers (called only within this module):
  _run_shadow_pipeline(adapter, req, ctx)
  _build_shadow_comparison_data(final_reply, shadow_payload)

Adapter loading responsibility:
  This module does NOT load or import the canonical adapter.
  The adapter is passed in by the caller (api/routes.py) which is
  the service bootstrap layer that owns all runtime dependency loading.
  If adapter is None, shadow execution is safely skipped.

Invariants:
  - observe_shadow_pipeline() never raises.
  - No route, schema, webhook, ai-engine, or DB state is modified here.
  - The legacy OrchestrateResponse is never affected by anything in this file.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from modules.ai.orchestrator.invoker import invoke_adapter
from modules.ai.orchestrator.request_mapper import build_adapter_kwargs

logger = logging.getLogger("ai-orchestrator.shadow")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _run_shadow_pipeline(
    adapter: Optional[Any],
    req: Any,
    ctx: Dict[str, Any],
) -> Optional[Any]:
    """
    Run the canonical backend/modules/ai pipeline in shadow mode.

    Parameters:
      adapter : the pre-loaded modules.ai.orchestrator.adapter module, or None.
                Passed in from the service bootstrap layer (routes.py).

    Responsibilities:
    - Call: invoke_adapter() handles the guard, call, and exception safety.
    - Log:  emit [shadow-pipeline] INFO/DEBUG based on the returned payload.
    - Safe: invoke_adapter() never raises; this function also never raises.

    Returns the AIReplyPayload on success, or None on failure/unavailability.
    """
    payload = invoke_adapter(
        adapter,
        build_adapter_kwargs(
            tenant_id=req.tenant_id,
            customer_phone=req.customer_phone,
            message=req.message,
            store_name=ctx.get("store_name", ""),
            locale=ctx.get("preferred_language", "ar"),
            context_metadata=ctx,
        ),
        caller_tag="shadow-pipeline",
    )
    if payload is None:
        return None
    if payload.reply_text:
        logger.info(
            "[shadow-pipeline] reply_text=%s provider=%s",
            payload.reply_text[:120],
            payload.provider_used,
        )
    else:
        logger.debug(
            "[shadow-pipeline] reply_text empty — new pipeline not active yet "
            "(scaffold mode or no API key configured)"
        )
    return payload


def _build_shadow_comparison_data(
    final_reply: Optional[str],
    shadow_payload: Optional[Any],
) -> Dict[str, Any]:
    """
    Safely extract comparison fields between the legacy reply and the shadow
    pipeline reply for use in [shadow-compare] log entries.

    Always returns a plain dict — never raises.

    Keys:
      skip          : bool  — True when comparison should be skipped because
                              shadow_payload is None or has no reply_text
      legacy_snip   : str   — first 120 chars of legacy reply after strip()
      shadow_snip   : str   — first 120 chars of shadow reply after strip()
      both_present  : bool  — True when both snippets are non-empty
      equal         : bool  — True when stripped replies are exactly equal
    """
    if shadow_payload is None or not shadow_payload.reply_text:
        return {"skip": True, "legacy_snip": "", "shadow_snip": "",
                "both_present": False, "equal": False}
    legacy_stripped = (final_reply or "").strip()
    shadow_stripped = shadow_payload.reply_text.strip()
    return {
        "skip":         False,
        "legacy_snip":  legacy_stripped[:120],
        "shadow_snip":  shadow_stripped[:120],
        "both_present": bool(legacy_stripped) and bool(shadow_stripped),
        "equal":        legacy_stripped == shadow_stripped,
    }


# ── Public entry point ─────────────────────────────────────────────────────────

def observe_shadow_pipeline(
    adapter: Optional[Any],
    req: Any,
    ctx: Dict[str, Any],
    final_reply: Optional[str],
) -> None:
    """
    Full shadow observability flow — run and compare in one call.

    Parameters:
      adapter    : the pre-loaded modules.ai.orchestrator.adapter module, or
                   None. Sourced from the service bootstrap layer (routes.py).
                   When None, shadow execution is silently skipped.
      req        : OrchestrateRequest (typed as Any to avoid circular import).
      ctx        : customer-memory context dict from load_customer_memory().
      final_reply: the fully-built legacy reply string (may be empty or None).

    Responsibilities (in order):
    1. Call _run_shadow_pipeline(adapter, req, ctx) — execute canonical pipeline.
    2. Call _build_shadow_comparison_data(final_reply, shadow_payload) — extract
       safe, truncated comparison fields.
    3. Emit [shadow-compare] INFO or DEBUG log based on the result.

    Invariants:
    - Never raises — all errors are caught and logged as WARNING.
    - Never modifies any argument or external state.
    - The legacy OrchestrateResponse is never affected by this function.
    """
    try:
        shadow_payload = _run_shadow_pipeline(adapter, req, ctx)
        cmp = _build_shadow_comparison_data(final_reply, shadow_payload)
        if cmp["skip"]:
            logger.debug(
                "[shadow-compare] skipped — shadow pipeline inactive or returned empty reply"
            )
        else:
            logger.info(
                "[shadow-compare] legacy=%r shadow=%r both_present=%s equal=%s",
                cmp["legacy_snip"],
                cmp["shadow_snip"],
                cmp["both_present"],
                cmp["equal"],
            )
    except Exception as exc:
        logger.warning(
            "[shadow-compare] comparison logging failed %r — ignored", exc
        )
