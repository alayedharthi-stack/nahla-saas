"""
backend/modules/ai/orchestrator/request_mapper.py
───────────────────────────────────────────────────
Shared kwargs builder for generate_ai_reply calls.

Public surface:
  build_adapter_kwargs(**fields)  — returns a dict ready for **-unpacking

Used by:
  ai-engine/main.py
  services/ai-orchestrator/observability/shadow_pipeline.py

Purpose:
  Both callers previously assembled the same set of canonical adapter kwargs
  inline in separate places. This module defines the mapping once so the
  adapter contract is expressed and maintained in a single location.

Invariants:
  - Pure function — no side effects, no I/O, no logging.
  - All parameters are keyword-only to prevent positional mistakes.
  - Callers are responsible for extracting scalar values from their own
    request/context objects before calling this helper.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def build_adapter_kwargs(
    *,
    tenant_id: Optional[int] = None,
    customer_phone: str = "",
    message: str = "",
    store_name: str = "",
    channel: str = "whatsapp",
    locale: str = "ar",
    context_metadata: Optional[Dict[str, Any]] = None,
    provider_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the keyword-argument dict for a generate_ai_reply() call.

    All parameters are keyword-only.  Callers extract scalar values from
    their own request/context objects and pass them here; this function does
    not access any request or context object directly.

    Parameters
    ----------
    tenant_id        : numeric DB tenant id, or None
    customer_phone   : customer's phone number string
    message          : inbound customer message text
    store_name       : display name of the merchant's store
    channel          : caller surface — one of whatsapp | campaigns |
                       conversations | widgets | system  (default: whatsapp)
    locale           : preferred reply language  (default: ar)
    context_metadata : optional enrichment dict forwarded as context_metadata
    provider_hint    : optional LLM provider preference string, or None

    Returns
    -------
    Dict ready for **-unpacking into adapter.generate_ai_reply().
    None values for provider_hint are excluded so the adapter uses its own
    default selection logic.
    """
    kwargs: Dict[str, Any] = dict(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        message=message,
        store_name=store_name,
        channel=channel,
        locale=locale,
        context_metadata=context_metadata or {},
    )
    if provider_hint is not None:
        kwargs["provider_hint"] = provider_hint
    return kwargs
