"""
service_closer_guard.py
───────────────────────
Post-compose guard: strip customer-service / sales closers from outbound text.

Personality must not be deterministic canned CS language. This guard removes
matching trailing segments without substituting another canned phrase.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from core.fallback_policy import (
    contains_sales_closer,
    contains_service_closer,
    strip_closer_segments,
)

logger = logging.getLogger("nahla.brain.postprocess.service_closer_guard")

# Longest-first so «ولكن» wins over the «لكن» suffix.
_DANGLING_CONNECTORS = ("ولكن", "لكن", "لذلك", "ثم", "أو", "و")
_TRAILING_CONNECTOR_RE = re.compile(
    r"(?:(?<=[.!؟…])|\s+|^)(?:"
    + "|".join(re.escape(c) for c in _DANGLING_CONNECTORS)
    + r")[\s،,.!؟…]*$"
)
_SENTENCE_BOUNDARY_CHARS = ".!؟…"


_MAX_DANGLING_STRIPS = 5


def _strip_dangling_connector_pass(text: str) -> str:
    """Remove one trailing dangling-connector clause from *text*."""
    raw = (text or "").rstrip()
    if not raw:
        return raw

    match = _TRAILING_CONNECTOR_RE.search(raw)
    if not match:
        return raw

    prefix = raw[: match.start()].rstrip()
    if not prefix:
        return _TRAILING_CONNECTOR_RE.sub("", raw).strip()

    last_boundary = max(
        (i for i, ch in enumerate(prefix) if ch in _SENTENCE_BOUNDARY_CHARS),
        default=-1,
    )
    if last_boundary >= 0:
        return prefix[: last_boundary + 1].rstrip()

    return prefix.rstrip(" ،,.")


def _strip_dangling_connector_clause(text: str) -> str:
    """Drop trailing clauses left bare after service-closer stripping.

    Production regression 2026-07-27 (tenant 1): ``strip_closer_segments``
    removed the forbidden closer but left «... السابقة. لكن»; emoji injection
    then shipped «لكن ✨🛒» to the customer.

    EM review 2026-07-27: one pass is insufficient — e.g. «... عندنا و لكن»
    becomes «... عندنا و» after removing «لكن»; repeat until clean (bounded).
    """
    current = (text or "").rstrip()
    if not current:
        return current

    for _ in range(_MAX_DANGLING_STRIPS):
        nxt = _strip_dangling_connector_pass(current)
        if nxt == current:
            break
        current = nxt
        if not current:
            break

    return current


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
    block_commerce_escalation: bool = False,
    inbound_metadata: Optional[dict[str, Any]] = None,
    tenant_id: Optional[int] = None,
) -> ServiceCloserGuardResult:
    text = (reply or "").strip()
    if not text:
        return ServiceCloserGuardResult(reply="", stripped=False, non_commerce=False)

    meta = inbound_metadata if isinstance(inbound_metadata, dict) else {}
    non_commerce = bool(
        non_commerce_block_mode
        or block_commerce_escalation
        or meta.get("block_commerce_escalation")
        or str(meta.get("non_commerce_category") or "").strip()
    )

    if not (
        contains_service_closer(text)
        or (non_commerce and contains_sales_closer(text))
    ):
        return ServiceCloserGuardResult(reply=text, stripped=False, non_commerce=non_commerce)

    cleaned, stripped = strip_closer_segments(text, non_commerce=non_commerce)
    if stripped:
        cleaned = _strip_dangling_connector_clause(cleaned)
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
