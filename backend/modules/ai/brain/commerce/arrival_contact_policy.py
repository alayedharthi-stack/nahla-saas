"""
KB-policy gate for arrival / on-the-way / at-the-door staff contact.

Determines whether a merchant has explicitly opted in — via structured KB
metadata or natural-language policy — to send staff contact when a customer
signals they are arriving, on the way, at the gate, or visiting a branch.

Wire-layer dispatch (vCard, safety nets, decision routing) is intentionally
NOT here. Callers use :func:`merchant_allows_arrival_staff_contact` as a
read-only policy probe before any future action.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence

logger = logging.getLogger("nahla.brain.arrival_contact_policy")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Kinds that may carry arrival-contact *policy* (not shipping/catalog facts).
_POLICY_SCAN_KINDS: frozenset[str] = frozenset({
    "escalation_rules",
    "branches",
    "custom",
    "store_story",
    "faq",
    "quick_update",
})

# Customer-arrival / visit condition phrases as written in merchant policy.
_ARRIVAL_SIGNAL_RE = re.compile(
    r"(?:"
    r"عند\s*الوصول"
    r"|عند\s*زيارة(?:\s*ال)?(?:معرض|فرع|محل|متجر)?"
    r"|زيارة\s*(?:ال)?(?:معرض|فرع)"
    r"|عند\s*الب(?:و)?اب(?:ة)?"
    r"|عند\s*الباب"
    r"|(?:^|\s)الحوش(?:\s|[،,.]|$)"
    r"|(?:^|\s)في\s*الطريق"
    r"|(?:^|\s)جا(?:ي|يك)(?:كم|ك|ين)?"
    r"|(?:^|\s)وصل(?:ت|نا|وا)?(?:\s|[،,.]|$)"
    r"|(?:^|\s)انا\s*جا(?:ي|يك)"
    r"|(?:^|\s)أنا\s*جا(?:ي|يك)"
    r"|(?:^|\s)اذا\s*قال\s*العميل"
    r"|(?:^|\s)إذا\s*قال\s*العميل"
    r"|(?:^|\s)عند\s*الوصول\s*ل(?:ل)?(?:معرض|فرع|محل|متجر)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Merchant action: share staff / seller contact (not generic «تواصل معنا» alone).
_STAFF_CONTACT_ACTION_RE = re.compile(
    r"(?:"
    r"تواصل\s*مع"
    r"|(?:^|\s)تواصل\s*(?:مع|ب)"
    r"|(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي)(?:\s*(?:رقم|ال)?)?"
    r"|(?:^|\s)(?:اعط|أعط|اعطي|أعطي)(?:\s*(?:رقم|ال)?)?"
    r"|(?:^|\s)(?:اتصل|اتصلي|كلم|كلمي|اكلم|أكلم)"
    r"|(?:^|\s)رقم\s*(?:ال)?(?:بائع|موظف|مندوب|البائع|الموظف|المسؤول|الادارة|الإدارة)"
    r"|(?:^|\s)(?:بائع\s*المعرض|البائع|الموظف|المندوب|المسؤول)"
    r"|ل(?:ل)?تواصل\s*عند"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_METADATA_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class ArrivalContactPolicyVerdict:
    """Result of :func:`merchant_allows_arrival_staff_contact`."""

    allowed: bool
    reason: str = ""
    source_kind: str = ""
    section_id: Optional[int] = None
    policy_source: str = "heuristic"
    contact_ref: str = ""
    contact_lookup_name: str = ""
    contact_section_id: Optional[int] = None
    source_sections: tuple[int, ...] = ()


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    return _WS_RE.sub(" ", t).strip()


def _metadata_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _metadata_bool_true(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in _METADATA_BOOL_TRUE


def _section_fields(section: Any) -> tuple[str, str, str, Dict[str, Any], Optional[int]]:
    if isinstance(section, dict):
        kind = str(section.get("kind") or "").strip().lower()
        title = str(section.get("title") or "").strip()
        body = str(section.get("body") or "").strip()
        meta = _metadata_dict(section.get("metadata"))
        sid = section.get("id")
    else:
        kind = str(getattr(section, "kind", "") or "").strip().lower()
        title = str(getattr(section, "title", "") or "").strip()
        body = str(getattr(section, "body", "") or "").strip()
        meta = _metadata_dict(getattr(section, "metadata", None))
        sid = getattr(section, "id", None)
    try:
        section_id = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        section_id = None
    return kind, title, body, meta, section_id


def _metadata_allows_arrival_contact(meta: Dict[str, Any]) -> Optional[str]:
    if not meta:
        return None
    if _metadata_bool_true(meta.get("arrival_contact")):
        return "metadata:arrival_contact"
    if _metadata_bool_true(meta.get("arriving_customer_contact_policy")):
        return "metadata:arriving_customer_contact_policy"
    intent = str(meta.get("intent") or "").strip().lower()
    artifact = str(meta.get("artifact_target") or "").strip().lower()
    if (
        intent == "ask_location_or_arrival_help"
        and artifact == "maps_link_or_staff_contact"
    ):
        return "metadata:intent=ask_location_or_arrival_help"
    return None


def _text_allows_arrival_contact(text: str) -> bool:
    """True when policy text contains both an arrival signal and staff action."""
    norm = _norm(text)
    if not norm:
        return False
    return bool(
        _ARRIVAL_SIGNAL_RE.search(norm)
        and _STAFF_CONTACT_ACTION_RE.search(norm)
    )


def _legacy_settings_text(settings: Optional[Mapping[str, Any]]) -> str:
    if not settings:
        return ""
    direct = str(settings.get("escalation_rules") or "").strip()
    if direct:
        return direct
    ai = settings.get("ai_settings")
    if isinstance(ai, dict):
        return str(ai.get("escalation_rules") or "").strip()
    return ""


def merchant_allows_arrival_staff_contact(
    sections: Optional[Sequence[Any]] = None,
    *,
    settings: Optional[Mapping[str, Any]] = None,
) -> ArrivalContactPolicyVerdict:
    """Return whether the merchant KB explicitly allows arrival staff contact.

    Opt-in signals (any one is sufficient):

    * Section metadata ``arrival_contact=true`` or
      ``arriving_customer_contact_policy=true``
    * Metadata ``intent=ask_location_or_arrival_help`` with
      ``artifact_target=maps_link_or_staff_contact``
    * Natural-language policy in scanned KB kinds that mentions BOTH a
      customer-arrival/visit condition AND a staff-contact action
    * Legacy ``settings.escalation_rules`` free text with the same dual match

    A staff phone alone (e.g. in ``branches`` without arrival policy prose)
    does **not** opt in.
    """
    for section in sections or ():
        kind, title, body, meta, section_id = _section_fields(section)

        meta_reason = _metadata_allows_arrival_contact(meta)
        if meta_reason:
            return ArrivalContactPolicyVerdict(
                allowed=True,
                reason=meta_reason,
                source_kind=kind or "metadata",
                section_id=section_id,
            )

        if kind and kind not in _POLICY_SCAN_KINDS:
            continue

        combined = f"{title}\n{body}".strip()
        if _text_allows_arrival_contact(combined):
            return ArrivalContactPolicyVerdict(
                allowed=True,
                reason=f"text:{kind or 'section'}",
                source_kind=kind,
                section_id=section_id,
            )

    legacy = _legacy_settings_text(settings)
    if legacy and _text_allows_arrival_contact(legacy):
        return ArrivalContactPolicyVerdict(
            allowed=True,
            reason="text:legacy_escalation_rules",
            source_kind="escalation_rules",
            section_id=None,
        )

    return ArrivalContactPolicyVerdict(
        allowed=False,
        reason="no_arrival_contact_policy",
    )


def resolve_arrival_contact_policy(
    db: Any,
    tenant_id: int,
) -> ArrivalContactPolicyVerdict:
    """Load KB + legacy settings, evaluate policy, emit telemetry."""
    sections: Sequence[Any] = ()
    settings: Optional[Mapping[str, Any]] = None
    if db is not None and tenant_id:
        try:
            from models import MerchantKnowledgeSection  # noqa: PLC0415
            from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415

            sections = (
                apply_ai_visible_kb_query_filters(
                    db.query(MerchantKnowledgeSection)
                )
                .filter(MerchantKnowledgeSection.tenant_id == tenant_id)
                .order_by(
                    MerchantKnowledgeSection.priority.asc(),
                    MerchantKnowledgeSection.updated_at.desc(),
                )
                .limit(120)
                .all()
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "arrival_contact_policy | sections load failed tenant=%s err=%s",
                tenant_id, exc,
            )
        try:
            from models import TenantSettings  # noqa: PLC0415

            ts = (
                db.query(TenantSettings)
                .filter(TenantSettings.tenant_id == tenant_id)
                .first()
            )
            if ts is not None:
                ai = dict(getattr(ts, "ai_settings", None) or {})
                meta = dict(getattr(ts, "extra_metadata", None) or {})
                settings = {
                    "escalation_rules": (
                        ai.get("escalation_rules")
                        or meta.get("escalation_rules")
                        or ""
                    ),
                    "ai_settings": ai,
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "arrival_contact_policy | settings load failed tenant=%s err=%s",
                tenant_id, exc,
            )
    try:
        from modules.ai.brain.commerce.arrival_contact_compile_v0 import (  # noqa: PLC0415
            compile_arrival_contact_policy_v0,
            log_operational_policy_compile,
            verdict_from_compiled_artifact,
        )

        artifact = compile_arrival_contact_policy_v0(
            sections, settings=settings,
        )
        log_operational_policy_compile(tenant_id=tenant_id, artifact=artifact)
        verdict = verdict_from_compiled_artifact(artifact)
        log_arrival_contact_policy(tenant_id=tenant_id, verdict=verdict)
        return verdict
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "arrival_contact_policy | compile_v0 failed tenant=%s err=%s",
            tenant_id, exc,
        )

    verdict = merchant_allows_arrival_staff_contact(sections, settings=settings)
    log_arrival_contact_policy(tenant_id=tenant_id, verdict=verdict)
    return verdict


def log_arrival_contact_policy(
    *,
    tenant_id: Any = None,
    verdict: Optional[ArrivalContactPolicyVerdict] = None,
    allowed: Optional[bool] = None,
    reason: str = "",
    source_kind: str = "",
    section_id: Optional[int] = None,
    policy_source: str = "",
    source_sections: Optional[Sequence[int]] = None,
) -> None:
    """Emit one grep-friendly ``[ARRIVAL_CONTACT_POLICY]`` line."""
    if verdict is not None:
        allowed = verdict.allowed
        reason = verdict.reason or ""
        source_kind = verdict.source_kind or ""
        section_id = verdict.section_id
        policy_source = verdict.policy_source or policy_source
        source_sections = verdict.source_sections or source_sections
    sections_str = "-"
    if source_sections:
        sections_str = ",".join(str(s) for s in source_sections)
    try:
        logger.info(
            "[ARRIVAL_CONTACT_POLICY] tenant=%s source=%s allow=%s reason=%r "
            "source_kind=%s section_id=%s source_sections=%s",
            tenant_id if tenant_id is not None else "-",
            policy_source or "heuristic",
            "true" if allowed else "false",
            (reason or "")[:96],
            source_kind or "-",
            section_id if section_id is not None else "-",
            sections_str,
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "ArrivalContactPolicyVerdict",
    "log_arrival_contact_policy",
    "merchant_allows_arrival_staff_contact",
    "resolve_arrival_contact_policy",
]
