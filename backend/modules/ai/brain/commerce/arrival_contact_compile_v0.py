"""
Compiler v0 — ``arrival_contact`` operational policy only.

Aggregates KB signals across sections (metadata, split text policy,
showroom staff contact) into one runtime-consumable artifact. Not a
general policy engine.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from modules.ai.brain.commerce.arrival_contact_policy import (
    _ARRIVAL_SIGNAL_RE,
    _POLICY_SCAN_KINDS,
    _STAFF_CONTACT_ACTION_RE,
    _legacy_settings_text,
    _metadata_allows_arrival_contact,
    _norm,
    _section_fields,
    _text_allows_arrival_contact,
)

logger = logging.getLogger("nahla.brain.arrival_contact_compile_v0")

COMPILE_VERSION = 0
POLICY_ID = "arrival_contact"
CONTACT_REF = "primary_showroom_seller"
DEFAULT_TRIGGERS: Tuple[str, ...] = (
    "customer_arrival",
    "branch_access_failure",
)
DEFAULT_ACTION = "send_staff_contact"

# Kinds that may carry showroom staff phone directories.
_CONTACT_SCAN_KINDS: frozenset[str] = frozenset({
    "branches",
    "custom",
    "store_story",
    "owner_identity",
    "quick_update",
    "faq",
    "escalation_rules",
})

_SHOWROOM_ROLE_RE = re.compile(
    r"(?:"
    r"بائع\s*المعرض"
    r"|(?:^|\s)البائع(?:\s|[،,.:]|$)"
    r"|(?:^|\s)بائع(?:\s|[،,.:]|$)"
    r"|موظف\s*المعرض"
    r"|(?:^|\s)المحاسب(?:\s|[،,.:]|$)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_OWNER_ONLY_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:المالك|مالك\s*المتجر|صاحب\s*المتجر)"
    r"|(?:^|\s)owner(?:\s|[،,.]|$)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_PHONE_REGEXES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\+?\s*9665\d{8}\b"),
    re.compile(r"\b00\s*9665\d{8}\b"),
    re.compile(r"\b05\d{8}\b"),
    re.compile(r"\b5\d{8}\b"),
)


@dataclass(frozen=True)
class ArrivalContactCompiledArtifact:
    """Runtime-consumable v0 artifact for ``arrival_contact``."""

    policy_id: str = POLICY_ID
    enabled: bool = False
    triggers: Tuple[str, ...] = DEFAULT_TRIGGERS
    action: str = DEFAULT_ACTION
    contact_ref: str = CONTACT_REF
    source_sections: Tuple[int, ...] = ()
    contact_lookup_name: str = ""
    contact_section_id: Optional[int] = None
    compile_reason: str = ""
    version: int = COMPILE_VERSION


def _extract_phones(text: str) -> List[str]:
    if not text:
        return []
    seen: List[str] = []
    for pat in _PHONE_REGEXES:
        for m in pat.finditer(text):
            cand = m.group(0).strip()
            if cand and cand not in seen:
                seen.append(cand)
    return seen


def _section_has_arrival_signal(title: str, body: str) -> bool:
    combined = f"{title}\n{body}".strip()
    norm = _norm(combined)
    if not norm:
        return False
    return bool(_ARRIVAL_SIGNAL_RE.search(norm))


def _section_has_staff_action(title: str, body: str) -> bool:
    combined = f"{title}\n{body}".strip()
    norm = _norm(combined)
    if not norm:
        return False
    return bool(_STAFF_CONTACT_ACTION_RE.search(norm))


def _is_owner_only_contact_section(kind: str, title: str, body: str) -> bool:
    """True when a section looks like owner/admin contact, not showroom staff."""
    combined = f"{title}\n{body}".strip()
    norm = _norm(combined)
    if not norm:
        return False
    if _SHOWROOM_ROLE_RE.search(norm):
        return False
    if kind.strip().lower() == "owner_identity":
        return True
    if _OWNER_ONLY_RE.search(norm):
        return True
    if re.search(
        r"رقم\s*(?:ال)?(?:مالك|ادارة|إدارة|owner)",
        norm,
        re.IGNORECASE | re.UNICODE,
    ):
        return True
    return False


def _contact_lookup_label(title: str, body: str) -> str:
    """Derive a KB-resolver lookup label from role prose (no hardcoded names)."""
    combined = f"{title}\n{body}".strip()
    for pat in (
        r"بائع\s*المعرض",
        r"البائع",
        r"بائع",
        r"المحاسب",
        r"موظف\s*المعرض",
    ):
        m = re.search(pat, combined, re.IGNORECASE | re.UNICODE)
        if m:
            return m.group(0).strip()
    return "بائع المعرض"


@dataclass
class _ContactCandidate:
    section_id: int
    kind: str
    phone: str
    lookup_name: str
    score: int


def _score_contact_candidate(
    *,
    kind: str,
    title: str,
    body: str,
    phones: Sequence[str],
) -> Optional[_ContactCandidate]:
    if not phones:
        return None
    if _is_owner_only_contact_section(kind, title, body):
        return None

    norm = _norm(f"{title}\n{body}")
    score = 0
    if _SHOWROOM_ROLE_RE.search(norm):
        score += 30
    elif _STAFF_CONTACT_ACTION_RE.search(norm):
        score += 10
    if kind in {"branches", "custom"}:
        score += 5
    if kind == "owner_identity":
        score -= 50

    return _ContactCandidate(
        section_id=0,  # filled by caller
        kind=kind,
        phone=phones[0],
        lookup_name=_contact_lookup_label(title, body),
        score=score,
    )


def _collect_policy_signals(
    sections: Sequence[Any],
    *,
    settings: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, Tuple[int, ...], str]:
    """Return ``(has_policy, source_section_ids, reason_snippet)``."""
    source_ids: List[int] = []
    metadata_ids: List[int] = []
    arrival_ids: List[int] = []
    action_ids: List[int] = []
    dual_ids: List[int] = []

    for section in sections or ():
        kind, title, body, meta, section_id = _section_fields(section)
        sid = section_id or 0

        meta_reason = _metadata_allows_arrival_contact(meta)
        if meta_reason:
            if sid:
                metadata_ids.append(sid)
            continue

        if kind and kind not in _POLICY_SCAN_KINDS:
            continue

        combined = f"{title}\n{body}".strip()
        if _text_allows_arrival_contact(combined):
            if sid:
                dual_ids.append(sid)
            continue
        if _section_has_arrival_signal(title, body):
            if sid:
                arrival_ids.append(sid)
        if _section_has_staff_action(title, body):
            if sid:
                action_ids.append(sid)

    legacy = _legacy_settings_text(settings)
    legacy_dual = bool(legacy and _text_allows_arrival_contact(legacy))

    if metadata_ids:
        source_ids.extend(metadata_ids)
        return True, tuple(dict.fromkeys(source_ids)), "metadata_opt_in"

    if dual_ids:
        source_ids.extend(dual_ids)
        return True, tuple(dict.fromkeys(source_ids)), "text_dual_match"

    if legacy_dual:
        return True, tuple(source_ids), "legacy_dual_match"

    if arrival_ids and action_ids:
        source_ids.extend(arrival_ids)
        source_ids.extend(action_ids)
        return True, tuple(dict.fromkeys(source_ids)), "cross_section_text"

    return False, tuple(), "no_policy_signal"


def _resolve_showroom_contact(
    sections: Sequence[Any],
    *,
    preferred_section_ids: Sequence[int] = (),
) -> Optional[_ContactCandidate]:
    preferred = {int(x) for x in preferred_section_ids if x}
    best: Optional[_ContactCandidate] = None

    for section in sections or ():
        kind, title, body, _meta, section_id = _section_fields(section)
        if kind and kind not in _CONTACT_SCAN_KINDS:
            continue
        phones = _extract_phones(f"{title}\n{body}")
        cand = _score_contact_candidate(
            kind=kind or "",
            title=title,
            body=body,
            phones=phones,
        )
        if cand is None or section_id is None:
            continue
        cand = _ContactCandidate(
            section_id=int(section_id),
            kind=cand.kind,
            phone=cand.phone,
            lookup_name=cand.lookup_name,
            score=cand.score + (15 if int(section_id) in preferred else 0),
        )
        if best is None or cand.score > best.score:
            best = cand
    return best


@dataclass(frozen=True)
class ShowroomContactEvidence:
    """Resolved showroom contact from KB sections."""

    lookup_name: str
    phone: str
    section_id: Optional[int] = None


def resolve_showroom_contact_for_delivery(
    sections: Sequence[Any],
    *,
    preferred_section_ids: Sequence[int] = (),
) -> Optional[ShowroomContactEvidence]:
    """Public wrapper for arrival delivery — returns phone + lookup label."""
    contact = _resolve_showroom_contact(
        sections,
        preferred_section_ids=preferred_section_ids,
    )
    if contact is None or not contact.phone:
        return None
    return ShowroomContactEvidence(
        lookup_name=contact.lookup_name,
        phone=contact.phone,
        section_id=contact.section_id,
    )


def compile_arrival_contact_policy_v0(
    sections: Optional[Sequence[Any]] = None,
    *,
    settings: Optional[Mapping[str, Any]] = None,
) -> ArrivalContactCompiledArtifact:
    """Compile the v0 ``arrival_contact`` artifact from KB sections."""
    has_policy, policy_section_ids, policy_reason = _collect_policy_signals(
        sections or (),
        settings=settings,
    )

    if not has_policy:
        return ArrivalContactCompiledArtifact(
            enabled=False,
            source_sections=(),
            compile_reason="no_policy_signal",
        )

    contact = _resolve_showroom_contact(
        sections or (),
        preferred_section_ids=policy_section_ids,
    )
    if contact is None or contact.score < 10:
        return ArrivalContactCompiledArtifact(
            enabled=False,
            source_sections=policy_section_ids,
            compile_reason="unresolved_contact",
        )

    source_sections = tuple(
        dict.fromkeys(
            list(policy_section_ids) + [contact.section_id],
        )
    )
    return ArrivalContactCompiledArtifact(
        enabled=True,
        source_sections=source_sections,
        contact_lookup_name=contact.lookup_name,
        contact_section_id=contact.section_id,
        compile_reason=policy_reason,
    )


def log_operational_policy_compile(
    *,
    tenant_id: Any = None,
    artifact: Optional[ArrivalContactCompiledArtifact] = None,
) -> None:
    """Emit ``[OPERATIONAL_POLICY_COMPILE]`` telemetry."""
    if artifact is None:
        return
    try:
        sections_str = ",".join(str(s) for s in artifact.source_sections) or "-"
        logger.info(
            "[OPERATIONAL_POLICY_COMPILE] tenant=%s policy=%s enabled=%s "
            "reason=%r source_sections=%s contact_ref=%s contact_section_id=%s",
            tenant_id if tenant_id is not None else "-",
            artifact.policy_id,
            "true" if artifact.enabled else "false",
            (artifact.compile_reason or "")[:96],
            sections_str,
            artifact.contact_ref or "-",
            artifact.contact_section_id if artifact.contact_section_id else "-",
        )
    except Exception:  # noqa: BLE001
        pass


def verdict_from_compiled_artifact(
    artifact: ArrivalContactCompiledArtifact,
) -> "ArrivalContactPolicyVerdict":
    from modules.ai.brain.commerce.arrival_contact_policy import (  # noqa: PLC0415
        ArrivalContactPolicyVerdict,
    )

    if artifact.enabled:
        return ArrivalContactPolicyVerdict(
            allowed=True,
            reason=f"compiled_v0:{artifact.compile_reason}",
            source_kind="compiled_v0",
            section_id=artifact.contact_section_id,
            policy_source="compiled_v0",
            contact_ref=artifact.contact_ref,
            contact_lookup_name=artifact.contact_lookup_name,
            contact_section_id=artifact.contact_section_id,
            source_sections=artifact.source_sections,
        )

    reason = artifact.compile_reason or "compiled_v0:disabled"
    return ArrivalContactPolicyVerdict(
        allowed=False,
        reason=reason,
        source_kind="compiled_v0",
        section_id=None,
        policy_source="compiled_v0",
        contact_ref=artifact.contact_ref,
        source_sections=artifact.source_sections,
    )


__all__ = [
    "ArrivalContactCompiledArtifact",
    "ShowroomContactEvidence",
    "compile_arrival_contact_policy_v0",
    "log_operational_policy_compile",
    "resolve_showroom_contact_for_delivery",
    "verdict_from_compiled_artifact",
]
