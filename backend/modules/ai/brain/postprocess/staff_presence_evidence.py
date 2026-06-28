"""
staff_presence_evidence.py
──────────────────────────
Structured staff/contact facts for grounded replies — never infer
presence, owner role, or live availability from LLM wording alone.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from modules.ai.brain.commerce.staff_contact_evidence import (
    StaffContactRecord,
    StaffContactRegistry,
    _role_display_label,
    build_deliver_reply_text,
    build_staff_identity_reply_text,
    classify_staff_contact_request,
    load_staff_contact_registry,
    resolve_contact_display_name,
    resolve_staff_contact,
)

logger = logging.getLogger("nahla.brain.postprocess.staff_presence_evidence")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")


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
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


@dataclass(frozen=True)
class StaffPresenceEvidence:
    matched_record: Optional[StaffContactRecord] = None
    registry_records: Tuple[StaffContactRecord, ...] = ()
    availability_status: str = ""
    availability_evidence_source: str = ""
    evidence_source: str = ""
    staff_context_active: bool = False

    @property
    def has_availability_evidence(self) -> bool:
        return bool(
            (self.availability_status or "").strip()
            and (self.availability_evidence_source or "").strip()
        )


def _record_from_state(state: Any) -> Tuple[str, str]:
    if state is None:
        return "", ""
    status = str(getattr(state, "staff_availability_status", "") or "").strip()
    source = str(getattr(state, "staff_availability_evidence_source", "") or "").strip()
    if not status and isinstance(state, dict):
        status = str(state.get("staff_availability_status") or "").strip()
        source = str(state.get("staff_availability_evidence_source") or "").strip()
    return status, source


def _resolve_matched_record(
    raw: str,
    registry: StaffContactRegistry,
) -> Optional[StaffContactRecord]:
    matched = registry.match_record_in_message(raw)
    if matched is not None:
        return matched

    norm = _norm(raw)
    identity = re.match(
        r"^(?:من|مين|من\s+هو|مين\s+هو)\s+(\S+)",
        norm,
        re.UNICODE | re.IGNORECASE,
    )
    if identity:
        candidate = str(identity.group(1) or "").strip("?.؟")
        for rec in registry.records:
            if rec.is_owner:
                continue
            for token in rec.all_match_tokens():
                if candidate and (candidate in token or token in candidate):
                    return rec

    try:
        from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: PLC0415
            is_pronoun_staff_contact_followup,
        )

        if is_pronoun_staff_contact_followup(raw):
            showroom = registry.non_owner_records()
            if len(showroom) == 1:
                return showroom[0]
    except Exception:  # noqa: BLE001
        logger.exception("[STAFF_PRESENCE_EVIDENCE] pronoun_followup_probe_failed")

    request = classify_staff_contact_request(raw, registry=registry)
    resolution = resolve_staff_contact(registry, request, message=raw)
    if resolution.found and resolution.record is not None:
        return resolution.record
    return None


def evaluate_staff_presence_evidence(
    *,
    message: str = "",
    db: Any = None,
    tenant_id: Optional[int] = None,
    store_contact_phone: str = "",
    state: Any = None,
    registry: Optional[StaffContactRegistry] = None,
) -> StaffPresenceEvidence:
    raw = (message or "").strip()
    staff_context_active = False
    try:
        from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
            is_staff_or_contact_context,
        )

        staff_context_active = bool(raw) and is_staff_or_contact_context(raw)
    except Exception:  # noqa: BLE001
        staff_context_active = False

    reg = registry
    if reg is None and db is not None and tenant_id:
        try:
            reg = load_staff_contact_registry(
                db,
                int(tenant_id),
                store_contact_phone=store_contact_phone,
            )
        except Exception:  # noqa: BLE001
            reg = StaffContactRegistry()

    records = tuple(getattr(reg, "records", None) or ()) if reg is not None else ()
    matched: Optional[StaffContactRecord] = None
    if reg is not None and raw:
        matched = _resolve_matched_record(raw, reg)
        if matched is not None:
            staff_context_active = True

    availability_status, availability_source = _record_from_state(state)
    return StaffPresenceEvidence(
        matched_record=matched,
        registry_records=records,
        availability_status=availability_status,
        availability_evidence_source=availability_source,
        evidence_source="staff_contact_registry" if records else "",
        staff_context_active=staff_context_active or matched is not None,
    )


def derive_allowed_staff_facts(evidence: StaffPresenceEvidence) -> List[str]:
    record = evidence.matched_record
    if record is None:
        return []
    facts: List[str] = []
    label = resolve_contact_display_name(record.lookup_name, role=record.role)
    role_label = _role_display_label(record.role)
    facts.append(f"configured_contact_name={label}")
    if role_label:
        facts.append(f"configured_role={role_label}")
    if (record.phone or "").strip():
        facts.append("configured_phone_present=true")
    if record.is_owner:
        facts.append("configured_owner=true")
    if evidence.has_availability_evidence:
        facts.append(
            f"availability_status={evidence.availability_status} "
            f"(source={evidence.availability_evidence_source})"
        )
    return facts


def derive_forbidden_staff_claims(evidence: StaffPresenceEvidence) -> List[str]:
    forbidden = [
        "staff_live_presence_without_evidence",
        "staff_busy_or_available_now_without_evidence",
        "staff_will_meet_you_in_showroom_without_evidence",
        "staff_will_reply_now_without_evidence",
        "invented_staff_phone_or_contact",
    ]
    record = evidence.matched_record
    if record is None or not record.is_owner:
        forbidden.append("staff_owner_or_store_owner_role_without_evidence")
    if not evidence.has_availability_evidence:
        forbidden.extend([
            "موجود / غير موجود / مشغول / متاح الآن",
            "بتلاقيه / بتقابله / في المعرض الآن",
            "بيرد عليك / يستناك / مستنيك",
        ])
    return forbidden


def build_grounded_staff_reply(
    evidence: StaffPresenceEvidence,
    *,
    inbound_text: str = "",
) -> str:
    record = evidence.matched_record
    if record is None:
        return "حالياً ما عندي معلومات تواصل مهيأة لهذا الطلب."

    norm = _norm(inbound_text or "")
    try:
        from modules.ai.brain.commerce.staff_contact_evidence import _CONTACT_ASK_RE  # noqa: PLC0415

        if _CONTACT_ASK_RE.search(norm) and (record.phone or "").strip():
            return build_deliver_reply_text(record)
    except Exception:  # noqa: BLE001
        logger.exception("[STAFF_PRESENCE_EVIDENCE] contact_ask_probe_failed")

    return build_staff_identity_reply_text(record)


def staff_presence_compose_overlay(evidence: StaffPresenceEvidence) -> str:
    if not evidence.staff_context_active:
        return ""
    allowed = derive_allowed_staff_facts(evidence)
    forbidden = derive_forbidden_staff_claims(evidence)
    if not allowed and not forbidden:
        return ""
    parts = [
        "staff_presence_guard — reply using configured staff facts only.",
        "Allowed staff facts:",
        *(f"- {item}" for item in allowed),
        "Forbidden staff claims:",
        *(f"- {item}" for item in forbidden),
        "Do NOT invent owner role, live presence, showroom location, or availability.",
    ]
    return " ".join(parts)


__all__ = [
    "StaffPresenceEvidence",
    "build_grounded_staff_reply",
    "derive_allowed_staff_facts",
    "derive_forbidden_staff_claims",
    "evaluate_staff_presence_evidence",
    "staff_presence_compose_overlay",
]
