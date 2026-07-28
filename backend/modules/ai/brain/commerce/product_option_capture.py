"""Deterministic detection of a pending product-option value in the customer message.

Live acceptance run 2026-07-29 (tenant 1): after product pick the customer sent
«42 - L» while «المقاس» was still pending. No intent rule matched, section 0b
was blocked by ``facts.orderable=False``, section 3.7 by ``last_search_candidates``,
and the turn fell through to ``ACTION_LLM_REPLY`` — ``_merge_message_options`` in
``DraftOrderHandler`` never ran.

This module is **detection-only** for the decision gate. The writer remains
``_merge_message_options`` inside ``orders.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .variant_pricing import normalize_text

_SEPARATOR_RE = re.compile(r"[\-–—_/\\|،,:؛;.]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_option_text(text: Any) -> str:
    """Fold case, Arabic forms and value separators into one comparable shape."""
    norm = normalize_text(str(text or "")).lower()
    norm = _SEPARATOR_RE.sub(" ", norm)
    return _WHITESPACE_RE.sub(" ", norm).strip()


@dataclass(frozen=True)
class OptionValueCandidate:
    group_id: Any
    group_name: str
    value_id: Any
    value_name: str

    @property
    def group_key(self) -> str:
        return str(self.group_name or "").strip().lower()


@dataclass(frozen=True)
class OptionCaptureResult:
    kind: str  # "matched" | "ambiguous" | "none"
    match: Optional[OptionValueCandidate] = None
    candidates: Tuple[OptionValueCandidate, ...] = ()

    @property
    def matched(self) -> bool:
        return self.kind == "matched" and self.match is not None

    @property
    def ambiguous(self) -> bool:
        return self.kind == "ambiguous"


_NO_MATCH = OptionCaptureResult(kind="none")


def _contains_token_run(tokens: Sequence[str], run: Sequence[str]) -> bool:
    if not run or len(run) > len(tokens):
        return False
    for start in range(len(tokens) - len(run) + 1):
        if list(tokens[start : start + len(run)]) == list(run):
            return True
    return False


def _message_states_value(text: str, value: str, *, group_name: str) -> bool:
    if not text or not value:
        return False
    stripped = text
    group_norm = normalize_option_text(group_name)
    if group_norm and stripped.startswith(f"{group_norm} "):
        stripped = stripped[len(group_norm) + 1 :].strip()
    if value in (text, stripped):
        return True
    value_tokens = value.split()
    if len(value_tokens) < 2:
        return False
    return _contains_token_run(text.split(), value_tokens)


def capture_pending_option_value(
    groups: Sequence[Dict[str, Any]],
    message: str,
) -> OptionCaptureResult:
    """Resolve the customer's message against pending option-group values."""
    text = normalize_option_text(message)
    if not text or text.isdigit():
        return _NO_MATCH

    hits: List[OptionValueCandidate] = []
    seen: set[tuple[str, str]] = set()
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "").strip()
        group_norm = normalize_option_text(group_name)
        for value in group.get("values") or []:
            if not isinstance(value, dict):
                continue
            value_name = str(value.get("name") or "").strip()
            value_norm = normalize_option_text(value_name)
            if not value_norm:
                continue
            if not _message_states_value(text, value_norm, group_name=group_norm):
                continue
            key = (group_norm, value_norm)
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                OptionValueCandidate(
                    group_id=group.get("id"),
                    group_name=group_name,
                    value_id=value.get("id"),
                    value_name=value_name,
                )
            )

    if not hits:
        return _NO_MATCH
    if len(hits) == 1:
        return OptionCaptureResult(kind="matched", match=hits[0], candidates=tuple(hits))
    return OptionCaptureResult(kind="ambiguous", candidates=tuple(hits))


def pending_option_groups_from_prep(prep: Any) -> List[Dict[str, Any]]:
    """Option groups with values that still have no customer pick."""
    meta = list(getattr(prep, "product_options_meta", None) or [])
    selected = dict(getattr(prep, "product_options", None) or {})
    out: List[Dict[str, Any]] = []
    for group in meta:
        if not isinstance(group, dict) or not (group.get("values") or []):
            continue
        group_key = str(group.get("name") or "").strip().lower()
        if not group_key or group_key in selected:
            continue
        out.append(group)
    return out


__all__ = [
    "OptionCaptureResult",
    "OptionValueCandidate",
    "capture_pending_option_value",
    "normalize_option_text",
    "pending_option_groups_from_prep",
]
