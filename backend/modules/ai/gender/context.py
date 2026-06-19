"""
modules/ai/gender/context.py
────────────────────────────
Resolve customer gender context for outbound Arabic agreement.

Priority (strongest → weakest):
  explicit (verb) > profile > trusted_history > context > inferred_name

Never overwrite a stronger stored source with a weaker inference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .detector import (
    APPLY_CONFIDENCE_THRESHOLD,
    GENDER_FEMALE,
    GENDER_MALE,
    GENDER_UNKNOWN,
    GenderHint,
    detect_gender,
)

SOURCE_EXPLICIT = "explicit"
SOURCE_PROFILE = "profile"
SOURCE_TRUSTED_HISTORY = "trusted_history"
SOURCE_CONTEXT = "context"
SOURCE_INFERRED_NAME = "inferred_name"
SOURCE_UNKNOWN = "unknown"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

REPLY_STYLE_MASCULINE = "masculine"
REPLY_STYLE_FEMININE = "feminine"
REPLY_STYLE_NEUTRAL = "neutral"

_TRUSTED_HISTORY_THRESHOLD = 0.85

_SOURCE_PRIORITY = {
    SOURCE_EXPLICIT: 100,
    SOURCE_PROFILE: 90,
    SOURCE_TRUSTED_HISTORY: 80,
    SOURCE_CONTEXT: 70,
    SOURCE_INFERRED_NAME: 60,
    SOURCE_UNKNOWN: 0,
    "conflict": 0,
    "none": 0,
}


@dataclass(frozen=True)
class CustomerGenderContext:
    """Resolved gender context for one outbound turn."""

    gender: str = GENDER_UNKNOWN
    confidence: str = CONFIDENCE_LOW
    confidence_score: float = 0.0
    source: str = SOURCE_UNKNOWN
    reply_style: str = REPLY_STYLE_NEUTRAL

    @property
    def hint(self) -> GenderHint:
        return GenderHint(
            value=self.gender,
            confidence=float(self.confidence_score),
            source=self._hint_source(),
        )

    def _hint_source(self) -> str:
        if self.source == SOURCE_EXPLICIT:
            return "verb"
        if self.source == SOURCE_INFERRED_NAME:
            return "name"
        if self.source in {SOURCE_TRUSTED_HISTORY, SOURCE_CONTEXT}:
            return "context"
        if self.source == SOURCE_PROFILE:
            return "profile"
        return "none"


def confidence_tier(score: float) -> str:
    if score >= APPLY_CONFIDENCE_THRESHOLD:
        return CONFIDENCE_HIGH
    if score >= 0.50:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def normalize_gender_source(source: str, *, confidence: float = 0.0) -> str:
    src = str(source or "").strip().lower()
    if src in {"verb", "explicit", "self_identification"}:
        return SOURCE_EXPLICIT
    if src == "profile":
        return SOURCE_PROFILE
    if src in {"trusted_history", "history"}:
        return SOURCE_TRUSTED_HISTORY
    if src == "name":
        return SOURCE_INFERRED_NAME
    if src == "context":
        if confidence >= _TRUSTED_HISTORY_THRESHOLD:
            return SOURCE_TRUSTED_HISTORY
        return SOURCE_CONTEXT
    if src in {"conflict", "none", ""}:
        return SOURCE_UNKNOWN
    return SOURCE_UNKNOWN


def source_priority(source: str, *, confidence: float = 0.0) -> int:
    normalized = normalize_gender_source(source, confidence=confidence)
    return int(_SOURCE_PRIORITY.get(normalized, 0))


def reply_style_for(gender: str, confidence_score: float) -> str:
    if confidence_score < APPLY_CONFIDENCE_THRESHOLD:
        return REPLY_STYLE_NEUTRAL
    if gender == GENDER_FEMALE:
        return REPLY_STYLE_FEMININE
    if gender == GENDER_MALE:
        return REPLY_STYLE_MASCULINE
    return REPLY_STYLE_NEUTRAL


def _profile_gender_signal(profile: Optional[Mapping[str, Any]]) -> Optional[GenderHint]:
    if not isinstance(profile, Mapping):
        return None
    raw = str(profile.get("gender") or profile.get("customer_gender") or "").strip().lower()
    if raw not in {GENDER_MALE, GENDER_FEMALE}:
        return None
    try:
        conf = float(profile.get("gender_confidence") or 0.95)
    except (TypeError, ValueError):
        conf = 0.95
    src = str(profile.get("gender_source") or SOURCE_PROFILE).strip().lower()
    if src not in {SOURCE_PROFILE, SOURCE_EXPLICIT}:
        src = SOURCE_PROFILE
    return GenderHint(value=raw, confidence=conf, source=src)


def _prior_hint_from_state(state: Any) -> GenderHint:
    if state is None:
        return GenderHint()
    value = str(getattr(state, "customer_gender_hint", "") or GENDER_UNKNOWN).strip().lower()
    if value not in {GENDER_MALE, GENDER_FEMALE}:
        value = GENDER_UNKNOWN
    try:
        confidence = float(getattr(state, "customer_gender_confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    stored_source = str(getattr(state, "customer_gender_source", "") or "context")
    return GenderHint(value=value, confidence=confidence, source=stored_source)


def _pick_resolved_hint(
    *,
    detected: GenderHint,
    profile_hint: Optional[GenderHint],
    prior: GenderHint,
) -> GenderHint:
    """Choose the winning hint using doctrine priority."""
    candidates: list[GenderHint] = []
    if detected.source == "verb" and detected.value in {GENDER_MALE, GENDER_FEMALE}:
        candidates.append(
            GenderHint(value=detected.value, confidence=detected.confidence, source="verb"),
        )
    if profile_hint and profile_hint.value in {GENDER_MALE, GENDER_FEMALE}:
        candidates.append(profile_hint)
    if prior.value in {GENDER_MALE, GENDER_FEMALE} and prior.confidence > 0:
        candidates.append(prior)
    if detected.source == "name" and detected.value in {GENDER_MALE, GENDER_FEMALE}:
        candidates.append(detected)
    if (
        detected.source == "context"
        and detected.value in {GENDER_MALE, GENDER_FEMALE}
        and detected.confidence > 0
    ):
        candidates.append(detected)

    if not candidates:
        if detected.value == GENDER_UNKNOWN and detected.source == "conflict":
            return detected
        return GenderHint()

    def _sort_key(h: GenderHint) -> tuple[int, float]:
        return (
            source_priority(h.source, confidence=h.confidence),
            float(h.confidence),
        )

    return max(candidates, key=_sort_key)


def resolve_customer_gender_context(
    *,
    message: str = "",
    customer_name: str = "",
    state: Any = None,
    profile: Optional[Mapping[str, Any]] = None,
) -> CustomerGenderContext:
    """Resolve gender context for compose/postprocess agreement."""
    prior = _prior_hint_from_state(state)
    profile_hint = _profile_gender_signal(profile)
    detected = detect_gender(
        message or "",
        customer_name or None,
        prior_hint=GenderHint(
            value=prior.value,
            confidence=prior.confidence,
            source="context",
        ) if prior.value in {GENDER_MALE, GENDER_FEMALE} else None,
    )

    if detected.source == "conflict":
        return CustomerGenderContext(
            gender=GENDER_UNKNOWN,
            confidence=CONFIDENCE_LOW,
            confidence_score=0.0,
            source=SOURCE_UNKNOWN,
            reply_style=REPLY_STYLE_NEUTRAL,
        )

    winning = _pick_resolved_hint(
        detected=detected,
        profile_hint=profile_hint,
        prior=prior,
    )
    if winning.value not in {GENDER_MALE, GENDER_FEMALE}:
        return CustomerGenderContext(
            gender=GENDER_UNKNOWN,
            confidence=CONFIDENCE_LOW,
            confidence_score=float(winning.confidence or 0.0),
            source=SOURCE_UNKNOWN,
            reply_style=REPLY_STYLE_NEUTRAL,
        )

    normalized_source = normalize_gender_source(
        winning.source,
        confidence=float(winning.confidence),
    )
    score = float(winning.confidence)
    return CustomerGenderContext(
        gender=winning.value,
        confidence=confidence_tier(score),
        confidence_score=score,
        source=normalized_source,
        reply_style=reply_style_for(winning.value, score),
    )


def should_persist_gender_hint(
    *,
    stored_source: str,
    stored_confidence: float,
    new_hint: GenderHint,
) -> bool:
    """True when *new_hint* may replace stored gender evidence."""
    if new_hint.value not in {GENDER_MALE, GENDER_FEMALE}:
        return False
    new_src = normalize_gender_source(new_hint.source, confidence=new_hint.confidence)
    cur_src = normalize_gender_source(stored_source, confidence=stored_confidence)
    new_pri = source_priority(new_src, confidence=new_hint.confidence)
    cur_pri = source_priority(cur_src, confidence=stored_confidence)
    if new_pri > cur_pri:
        return True
    if new_pri < cur_pri:
        return False
    return float(new_hint.confidence) >= float(stored_confidence)


def persist_gender_hint(
    state: Any,
    *,
    hint: GenderHint,
) -> bool:
    """Persist gender on conversation state when priority allows."""
    if state is None or hint.value not in {GENDER_MALE, GENDER_FEMALE}:
        return False
    stored_source = str(getattr(state, "customer_gender_source", "") or "")
    try:
        stored_conf = float(getattr(state, "customer_gender_confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        stored_conf = 0.0
    if not should_persist_gender_hint(
        stored_source=stored_source,
        stored_confidence=stored_conf,
        new_hint=hint,
    ):
        return False
    state.customer_gender_hint = hint.value
    state.customer_gender_confidence = float(hint.confidence)
    state.customer_gender_source = normalize_gender_source(
        hint.source,
        confidence=hint.confidence,
    )
    return True


__all__ = [
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MEDIUM",
    "CustomerGenderContext",
    "REPLY_STYLE_FEMININE",
    "REPLY_STYLE_MASCULINE",
    "REPLY_STYLE_NEUTRAL",
    "SOURCE_EXPLICIT",
    "SOURCE_INFERRED_NAME",
    "SOURCE_PROFILE",
    "SOURCE_TRUSTED_HISTORY",
    "SOURCE_UNKNOWN",
    "confidence_tier",
    "normalize_gender_source",
    "persist_gender_hint",
    "reply_style_for",
    "resolve_customer_gender_context",
    "should_persist_gender_hint",
    "source_priority",
]
