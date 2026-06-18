"""
staff_target_classifier.py
──────────────────────────
Deterministic boundary between named-person staff asks and generic role /
staff-function references. Platform-wide structural categories — not occupation
keyword lists.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, Optional

from modules.ai.brain.commerce.staff_contact_fallback_v0 import (
    StaffRoleAliasGraph,
    classify_explicit_role_request,
)

StaffTargetTier = Literal["named_person", "generic_role", "ambiguous"]

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Structural categories — not merchant-specific occupation vocabulary.
_COLLECTIVE_PLURAL_RE = re.compile(
    r"\bال[\u0600-\u06FF]{2,12}(?:ين|ون)\b",
    re.UNICODE,
)
_QUANTIFIED_HUMAN_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:واحد|احد|شخص|أحد|ا?حد)\s+(?:من|في)\s+"
    r"|(?:^|\s)(?:اكلم|أكلم|اتواصل|أتواصل|كلم)\s+(?:واحد|احد|شخص|أحد)\s+(?:من|في)\s+"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_INDEFINITE_STAFF_SLOT_RE = re.compile(
    r"^(?:موظف|شخص|بشر|انسان|إنسان|أحد|احد|فريق)$",
    re.UNICODE | re.IGNORECASE,
)
_DEFINITE_FUNCTION_NOUN_RE = re.compile(
    r"^ال[\u0600-\u06FF]{3,14}$",
    re.UNICODE,
)
_PROPER_NAME_TOKEN_RE = re.compile(
    r"^[\u0600-\u06FFa-yA-Y]{2,24}$",
    re.UNICODE,
)
_DESCRIPTIVE_NAMED_PHRASE_RE = re.compile(
    r"(?:غير|مو\s*موجود|مش\s*موجود|ما\s*موجود|unknown|not\s+found)",
    re.UNICODE | re.IGNORECASE,
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    return _WS_RE.sub(" ", t).strip()


def _flat_role_aliases(role_graph: Optional[StaffRoleAliasGraph]) -> tuple[str, ...]:
    if role_graph is None:
        return ()
    seen: list[str] = []
    for aliases in role_graph.roles.values():
        for alias in aliases:
            if alias and alias not in seen:
                seen.append(alias)
    return tuple(seen)


def _registry_names_person(
    registry: Any,
    *,
    message: str,
    raw_span: str,
) -> bool:
    if registry is None:
        return False
    if registry.match_record_in_message(message or ""):
        return True
    span_norm = _norm(raw_span)
    if not span_norm:
        return False
    for rec in getattr(registry, "records", ()) or ():
        for token in rec.all_match_tokens():
            if token == span_norm:
                return True
    return False


def _message_has_collective_structure(text: str) -> bool:
    norm = _norm(text)
    if not norm:
        return False
    if _COLLECTIVE_PLURAL_RE.search(norm):
        return True
    if _QUANTIFIED_HUMAN_RE.search(norm):
        return True
    return False


def _span_has_collective_structure(span: str) -> bool:
    return _message_has_collective_structure(span)


def _message_asks_numbered_role_slot(message: str, span: str) -> bool:
    """
    True when the customer asks for «رقم ال…» — a role slot, not a person name.

    Requires the definite article in the message (``رقم العامل``), not bare
    ``رقم أمين``. The name extractor may strip ``ال`` into its own group.
    """
    msg_norm = _norm(message)
    span_norm = _norm(span)
    if not msg_norm or not span_norm:
        return False
    if " " in span_norm.strip():
        return False
    root = span_norm.lstrip("ال")
    if not root or len(root) < 2:
        return False
    if re.search(rf"رقم\s+ال{re.escape(root)}\b", msg_norm):
        return True
    if span_norm.startswith("ال") and re.search(
        rf"رقم\s+{re.escape(span_norm)}\b",
        msg_norm,
    ):
        return True
    return False


def _is_definite_function_noun(span: str) -> bool:
    norm = _norm(span)
    if not norm:
        return False
    words = norm.split()
    if len(words) != 1:
        return False
    return bool(_DEFINITE_FUNCTION_NOUN_RE.fullmatch(words[0]))


def _is_indefinite_staff_slot(span: str) -> bool:
    norm = _norm(span)
    if not norm:
        return False
    return bool(_INDEFINITE_STAFF_SLOT_RE.fullmatch(norm))


def _is_proper_name_shape(span: str) -> bool:
    raw = (span or "").strip()
    if not raw or len(raw) < 2 or len(raw) > 40:
        return False
    if _DESCRIPTIVE_NAMED_PHRASE_RE.search(raw):
        return True
    words = _WS_RE.sub(" ", raw).strip().split()
    if not words or len(words) > 4:
        return False
    if len(words) >= 2:
        return all(_PROPER_NAME_TOKEN_RE.fullmatch(w) for w in words)
    word = words[0]
    if _is_definite_function_noun(word) or _is_indefinite_staff_slot(word):
        return False
    return bool(_PROPER_NAME_TOKEN_RE.fullmatch(word))


@dataclass(frozen=True)
class StaffTargetVerdict:
    raw_span: str
    tier: StaffTargetTier
    confidence: float
    reason: str


def classify_staff_target(
    message: str,
    *,
    raw_span: str = "",
    registry: Any = None,
    role_graph: Optional[StaffRoleAliasGraph] = None,
) -> StaffTargetVerdict:
    """
    Classify an extracted staff target span (or full message) into named vs generic.

    Pure, deterministic — never raises, never uses LLM.
    """
    span = (raw_span or "").strip()
    msg = (message or "").strip()

    # ── 1. Registry evidence ─────────────────────────────────────────────
    if registry is not None and (span or msg):
        if _registry_names_person(registry, message=msg, raw_span=span):
            return StaffTargetVerdict(
                raw_span=span or msg,
                tier="named_person",
                confidence=0.99,
                reason="evidence:registry_match",
            )

    # ── 2. Tenant KB role-alias graph ────────────────────────────────────
    aliases = _flat_role_aliases(role_graph)
    probe = span or msg
    if aliases and probe and classify_explicit_role_request(probe, aliases):
        return StaffTargetVerdict(
            raw_span=span or probe,
            tier="generic_role",
            confidence=0.94,
            reason="evidence:kb_role_alias",
        )

    # ── 3. Structural collective / quantified human ──────────────────────
    if _message_has_collective_structure(msg) or (span and _span_has_collective_structure(span)):
        return StaffTargetVerdict(
            raw_span=span or msg,
            tier="generic_role",
            confidence=0.92,
            reason="structure:collective_reference",
        )

    if span and _is_indefinite_staff_slot(span):
        return StaffTargetVerdict(
            raw_span=span,
            tier="generic_role",
            confidence=0.90,
            reason="structure:indefinite_staff_slot",
        )

    # ── 4. Structural definite function noun / numbered role slot ─────────
    if span and (
        _is_definite_function_noun(span)
        or _message_asks_numbered_role_slot(msg, span)
    ):
        return StaffTargetVerdict(
            raw_span=span,
            tier="generic_role",
            confidence=0.91,
            reason=(
                "structure:numbered_role_slot"
                if _message_asks_numbered_role_slot(msg, span)
                else "structure:definite_function_noun"
            ),
        )

    # ── 5. Proper-name shape ───────────────────────────────────────────────
    if span and _is_proper_name_shape(span):
        return StaffTargetVerdict(
            raw_span=span,
            tier="named_person",
            confidence=0.88,
            reason="heuristic:proper_name_shape",
        )

    # ── 6. Default ambiguous ───────────────────────────────────────────────
    return StaffTargetVerdict(
        raw_span=span or msg,
        tier="ambiguous",
        confidence=0.55,
        reason="fallback:ambiguous",
    )


__all__ = [
    "StaffTargetTier",
    "StaffTargetVerdict",
    "classify_staff_target",
]
