"""
service_closer_guard.py
───────────────────────
Post-compose guard: strip customer-service / sales closers from outbound text.

Personality must not be deterministic canned CS language. This guard removes
matching trailing segments without substituting another canned phrase.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from core.fallback_policy import (
    contains_sales_closer,
    contains_service_closer,
    strip_closer_segments,
)

logger = logging.getLogger("nahla.brain.postprocess.service_closer_guard")


@dataclass(frozen=True)
class ServiceCloserGuardResult:
    reply: str
    stripped: bool
    non_commerce: bool


def apply_service_closer_guard(
    reply: str,
    *,
    inbound_text: str = "",
    non_commerce_block_mode: bool = False,
    inbound_metadata: Optional[dict[str, Any]] = None,
    tenant_id: Optional[int] = None,
) -> ServiceCloserGuardResult:
    text = (reply or "").strip()
    if not text:
        return ServiceCloserGuardResult(reply="", stripped=False, non_commerce=False)

    meta = inbound_metadata if isinstance(inbound_metadata, dict) else {}
    non_commerce = bool(non_commerce_block_mode) or bool(
        str(meta.get("non_commerce_category") or "").strip()
    )

    if not (
        contains_service_closer(text)
        or (non_commerce and contains_sales_closer(text))
    ):
        return ServiceCloserGuardResult(reply=text, stripped=False, non_commerce=non_commerce)

    cleaned, stripped = strip_closer_segments(text, non_commerce=non_commerce)
    if stripped:
        logger.info(
            "[SERVICE_CLOSER_GUARD] tenant=%s non_commerce=%s "
            "orig_len=%d new_len=%d preview_in=%r",
            tenant_id if tenant_id is not None else "-",
            non_commerce,
            len(text),
            len(cleaned),
            (inbound_text or "")[:60],
        )

    return ServiceCloserGuardResult(
        reply=cleaned,
        stripped=stripped,
        non_commerce=non_commerce,
    )


__all__ = ["ServiceCloserGuardResult", "apply_service_closer_guard"]
