"""
Staff contact evidence — compile configured contacts from KB + store profile.

Phase A (guard + evidence): deterministic registry only. No LLM, no
hardcoded staff names, no owner fallback for generic / CS paths.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modules.ai.brain.commerce.staff_contact_fallback_v0 import (
    StaffChainEntry,
    StaffRoleAliasGraph,
    extract_staff_chain_from_sections,
    extract_staff_role_aliases_from_sections,
    load_staff_chain_sections,
)

logger = logging.getLogger("nahla.brain.staff_contact_evidence")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_CS_ROLE_TOKENS = frozenset({
    "customer_service",
    "cs",
    "support",
    "خدمة_العملاء",
    "خدمة العملاء",
    "دعم العملاء",
    "الدعم",
})

_CS_LABEL_HINTS = (
    "خدمة العملاء",
    "خدمه العملاء",
    "دعم العملاء",
    "الدعم",
    "customer service",
)

_CONTACT_ASK_RE = re.compile(
    r"(?:"
    r"رقم|ارسل|أرسل|ارسلي|أرسلي|ابي|ابغى|أبي|أبغى|"
    r"كلم|أكلم|اتصل|اتواصل|تواصل|وصلني|وصلوني"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_CS_REQUEST_RE = re.compile(
    r"(?:"
    r"رقم\s*(?:ال)?(?:خدمة|خدمه)\s*(?:ال)?(?:عملاء|العملاء)"
    r"|(?:خدمة|خدمه)\s*(?:ال)?(?:عملاء|العملاء)"
    r"|customer\s*service"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_GENERIC_STAFF_RE = re.compile(
    r"(?:"
    r"ابي\s*(?:اكلم|اتكلم|اتواصل)\s*(?:موظف|شخص|بشر|انسان|إنسان|أحد|احد)"
    r"|ابغى\s*(?:اكلم|اتكلم|اتواصل)\s*(?:موظف|شخص|بشر|انسان|إنسان|أحد|احد)"
    r"|وصلني\s*(?:ب|مع)?\s*(?:موظف|شخص|بشر|انسان|إنسان|أحد|احد|فريق)"
    r"|حولني\s*(?:ل|الى|إلى)?\s*(?:موظف|شخص|بشر|انسان|إنسان|أحد|احد)"
    r")",
    re.IGNORECASE | re.UNICODE,
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


@dataclass(frozen=True)
class StaffContactRecord:
    lookup_name: str
    phone: str
    section_id: Optional[int]
    role: str
    aliases: Tuple[str, ...]
    is_owner: bool
    chain_index: int
    source: str

    def all_match_tokens(self) -> Tuple[str, ...]:
        tokens: List[str] = []
        for raw in (self.lookup_name, *self.aliases):
            n = _norm(raw)
            if n and len(n) >= 2 and n not in tokens:
                tokens.append(n)
        return tuple(tokens)


@dataclass
class StaffContactRegistry:
    records: Tuple[StaffContactRecord, ...] = ()
    store_contact_phone: str = ""

    def non_owner_records(self) -> Tuple[StaffContactRecord, ...]:
        return tuple(r for r in self.records if not r.is_owner)

    def first_general_contact(self) -> Optional[StaffContactRecord]:
        showroom = self.non_owner_records()
        if showroom:
            return showroom[0]
        phone = (self.store_contact_phone or "").strip()
        if phone:
            return StaffContactRecord(
                lookup_name="خدمة العملاء",
                phone=phone,
                section_id=None,
                role="customer_service",
                aliases=("خدمة العملاء",),
                is_owner=False,
                chain_index=-1,
                source="store_profile",
            )
        return None

    def customer_service_contact(self) -> Optional[StaffContactRecord]:
        for rec in self.non_owner_records():
            role = (rec.role or "").strip().lower()
            if role in _CS_ROLE_TOKENS:
                return rec
            label_norm = _norm(rec.lookup_name)
            if any(_norm(h) in label_norm for h in _CS_LABEL_HINTS):
                return rec
            if any(_norm(h) in _norm(a) for h in _CS_LABEL_HINTS for a in rec.aliases):
                return rec
        return self.first_general_contact()

    def match_record_in_message(self, message: str) -> Optional[StaffContactRecord]:
        norm = _norm(message or "")
        if not norm:
            return None
        best: Optional[Tuple[int, StaffContactRecord]] = None
        for rec in self.records:
            if rec.is_owner:
                continue
            for token in rec.all_match_tokens():
                if len(token) < 2:
                    continue
                if _token_present_in_text(norm, token):
                    score = len(token)
                    if best is None or score > best[0]:
                        best = (score, rec)
        return best[1] if best else None


def _token_present_in_text(text_norm: str, token_norm: str) -> bool:
    """True when token appears as its own word, not embedded in another."""
    if not text_norm or not token_norm or token_norm not in text_norm:
        return False
    idx = 0
    while True:
        pos = text_norm.find(token_norm, idx)
        if pos < 0:
            return False
        before = text_norm[pos - 1] if pos > 0 else " "
        after_pos = pos + len(token_norm)
        after = text_norm[after_pos] if after_pos < len(text_norm) else " "
        if before.isspace() and after.isspace():
            return True
        idx = pos + 1


def find_next_unsent_registry_contact(
    registry: StaffContactRegistry,
    contacts_sent: Sequence[Any],
) -> Optional[StaffContactRecord]:
    """Return the first registry contact not yet marked sent."""
    from modules.ai.brain.commerce.staff_contact_fallback_v0 import (  # noqa: PLC0415
        StaffChainEntry,
        _entry_matches_sent,
    )

    ordered = sorted(
        registry.non_owner_records(),
        key=lambda r: (r.chain_index if r.chain_index >= 0 else 9999, r.lookup_name),
    )
    for rec in ordered:
        entry = StaffChainEntry(
            lookup_name=rec.lookup_name,
            phone=rec.phone,
            section_id=int(rec.section_id or 0),
            kind="registry",
            is_owner=rec.is_owner,
            chain_index=rec.chain_index,
            role=rec.role,
        )
        if not _entry_matches_sent(entry, contacts_sent):
            return rec
    return None


def _chain_entry_to_record(
    entry: StaffChainEntry,
    role_graph: StaffRoleAliasGraph,
) -> StaffContactRecord:
    role = (entry.role or "").strip().lower()
    aliases: List[str] = list(role_graph.aliases_for(role)) if role else []
    return StaffContactRecord(
        lookup_name=entry.lookup_name,
        phone=entry.phone,
        section_id=entry.section_id,
        role=role,
        aliases=tuple(aliases),
        is_owner=entry.is_owner,
        chain_index=entry.chain_index,
        source=f"kb:{entry.kind or 'section'}",
    )


def compile_staff_contact_registry(
    sections: Optional[Sequence[Any]] = None,
    *,
    store_contact_phone: str = "",
) -> StaffContactRegistry:
    """Build a tenant-scoped contact registry from KB chain + store phone."""
    role_graph = extract_staff_role_aliases_from_sections(sections or ())
    chain = extract_staff_chain_from_sections(sections or (), role_graph=role_graph)
    records = [_chain_entry_to_record(e, role_graph) for e in chain]
    return StaffContactRegistry(
        records=tuple(records),
        store_contact_phone=(store_contact_phone or "").strip(),
    )


def load_staff_contact_registry(
    db: Any,
    tenant_id: int,
    *,
    store_contact_phone: str = "",
) -> StaffContactRegistry:
    sections = load_staff_chain_sections(db, int(tenant_id or 0))
    phone = store_contact_phone
    if not phone and db is not None and tenant_id:
        try:
            from database.models import StoreKnowledgeSnapshot  # noqa: PLC0415

            snap = (
                db.query(StoreKnowledgeSnapshot)
                .filter(StoreKnowledgeSnapshot.tenant_id == tenant_id)
                .first()
            )
            if snap and snap.store_profile:
                phone = str(snap.store_profile.get("contact_phone") or "").strip()
        except Exception:  # noqa: BLE001
            phone = phone or ""
    return compile_staff_contact_registry(sections, store_contact_phone=phone)


@dataclass(frozen=True)
class StaffContactRequest:
    kind: str
    matched_alias: str = ""


_PAYMENT_OR_NON_STAFF_RE = re.compile(
    r"(?:"
    r"حساب|باركود|ايبان|آيبان|iban|"
    r"راجحي|الراجحي|تحويل|"
    r"تتبع|شحن(?:ة|ه)?|طلب(?:ي|يتي)?|"
    r"منتج|عسل|سعر"
    r")",
    re.IGNORECASE | re.UNICODE,
)


def classify_staff_contact_request(message: str) -> StaffContactRequest:
    """Classify inbound staff-contact intent (deterministic)."""
    raw = (message or "").strip()
    if not raw:
        return StaffContactRequest(kind="none")

    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            has_explicit_contact_intent,
            should_defer_contact_policies_for_commerce,
            should_defer_staff_contact_policy,
        )

        if should_defer_staff_contact_policy(raw):
            return StaffContactRequest(kind="none")
        if should_defer_contact_policies_for_commerce(raw):
            return StaffContactRequest(kind="none")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[STAFF_CONTACT_EVIDENCE] contact_route_policy_failed err=%s",
            exc,
        )

    norm = _norm(raw)
    if _PAYMENT_OR_NON_STAFF_RE.search(norm):
        return StaffContactRequest(kind="none")

    try:
        from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
            classify_employee_not_responding,
            classify_store_arrival,
        )

        if classify_employee_not_responding(raw) is not None:
            return StaffContactRequest(kind="not_responding")
        if classify_store_arrival(raw) is not None:
            return StaffContactRequest(kind="arrival")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[STAFF_CONTACT_EVIDENCE] classify_arrival_or_not_responding_failed err=%s",
            exc,
        )

    norm = _norm(raw)
    if _CS_REQUEST_RE.search(norm):
        return StaffContactRequest(kind="customer_service")

    if _GENERIC_STAFF_RE.search(norm):
        return StaffContactRequest(kind="generic_staff")

    from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
        has_explicit_contact_intent,
    )

    if _CONTACT_ASK_RE.search(norm):
        if has_explicit_contact_intent(raw):
            return StaffContactRequest(kind="named")
        return StaffContactRequest(kind="none")

    # Single-token bare configured name only ("هشام") — not generic phrases.
    words = norm.split()
    if len(words) == 1 and len(norm) >= 2:
        return StaffContactRequest(kind="named")

    return StaffContactRequest(kind="none")


@dataclass(frozen=True)
class StaffContactResolution:
    found: bool
    record: Optional[StaffContactRecord] = None
    reason: str = ""
    unknown_name: bool = False


def resolve_staff_contact(
    registry: StaffContactRegistry,
    request: StaffContactRequest,
    *,
    message: str = "",
) -> StaffContactResolution:
    """Resolve configured contact evidence for a classified request."""
    kind = request.kind
    if kind in {"none", "arrival", "not_responding"}:
        return StaffContactResolution(found=False, reason=f"defer_{kind}")

    if kind == "customer_service":
        rec = registry.customer_service_contact()
        if rec:
            return StaffContactResolution(found=True, record=rec, reason="customer_service")
        return StaffContactResolution(found=False, reason="cs_not_configured")

    if kind == "generic_staff":
        rec = registry.first_general_contact()
        if rec:
            return StaffContactResolution(found=True, record=rec, reason="general_staff")
        return StaffContactResolution(found=False, reason="escalation_not_configured")

    if kind == "named":
        rec = registry.match_record_in_message(message)
        if rec:
            return StaffContactResolution(found=True, record=rec, reason="named_match")
        if _CONTACT_ASK_RE.search(_norm(message)):
            return StaffContactResolution(
                found=False,
                reason="name_not_configured",
                unknown_name=True,
            )
        return StaffContactResolution(found=False, reason="no_named_intent")

    return StaffContactResolution(found=False, reason="unknown_kind")


# Operational copy — deterministic facts, not persona templates.
MSG_CS_NOT_CONFIGURED = (
    "حالياً أرقام خدمة العملاء غير مهيأة لهذا المتجر."
)
MSG_NAME_NOT_CONFIGURED = (
    "هذا الاسم غير مهيأ للتواصل حالياً."
)
MSG_ESCALATION_NOT_CONFIGURED = (
    "أرقام التصعيد غير مهيأة لهذا المتجر حالياً."
)
MSG_NO_NEXT_ESCALATION = (
    "حالياً لا يوجد رقم تصعيد إضافي مهيّأ لهذا المتجر."
)
MSG_CONTACT_CARD_FAILED = (
    "تعذّر إرسال بطاقة التواصل حالياً. حاول مرة أخرى."
)

_ROLE_DISPLAY_LABELS: Dict[str, str] = {
    "customer_service": "خدمة العملاء",
    "cs": "خدمة العملاء",
    "support": "خدمة العملاء",
    "owner": "الإدارة",
    "showroom": "بائع المعرض",
    "seller": "البائع",
}

# Arabic particles / prepositions that must never ship as vCard names.
_INVALID_DISPLAY_FRAGMENTS = frozenset({
    "لم", "لو", "في", "من", "على", "مع", "ان", "ان", "ما", "لا",
    "هذا", "هذي", "ذلك", "رقم", "التواصل", "رقم التواصل",
})


def _role_display_label(role: str) -> str:
    key = (role or "").strip().lower()
    return _ROLE_DISPLAY_LABELS.get(key, "")


def is_usable_display_name(name: str) -> bool:
    """True when *name* is safe to show on a WhatsApp contact card."""
    label = (name or "").strip()
    if not label:
        return False
    norm = _norm(label)
    if not norm or norm in _INVALID_DISPLAY_FRAGMENTS:
        return False
    if len(norm) <= 2 and " " not in norm:
        return False
    if norm in {"رقم", "التواصل", "رقم التواصل"}:
        return False
    return True


def resolve_contact_display_name(
    lookup_name: str,
    *,
    role: str = "",
    fallback: str = "خدمة العملاء",
) -> str:
    """Return a vCard-safe display name with role/fallback when needed."""
    label = (lookup_name or "").strip()
    if is_usable_display_name(label):
        return label
    role_label = _role_display_label(role)
    if role_label:
        return role_label
    return fallback


def build_staff_call_target(
    *,
    lookup_name: str,
    phone: str,
    role: str = "",
) -> Optional[Any]:
    """Build a normalized ``CallTarget`` when phone evidence is valid."""
    try:
        from services.call_resolver import (  # noqa: PLC0415
            CallTarget,
            _normalize_saudi_phone,
            _pretty_phone,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("staff_contact_evidence | call_resolver import failed: %s", exc)
        return None
    wa_id = _normalize_saudi_phone(phone)
    if not wa_id:
        return None
    display = resolve_contact_display_name(lookup_name, role=role)
    return CallTarget(
        name=display,
        wa_id=wa_id,
        phone_display=_pretty_phone(wa_id),
        raw_phone=phone,
    )


def build_staff_call_target_from_record(record: StaffContactRecord) -> Optional[Any]:
    return build_staff_call_target(
        lookup_name=record.lookup_name,
        phone=record.phone,
        role=record.role,
    )


def build_deliver_reply_text(record: StaffContactRecord) -> str:
    label = resolve_contact_display_name(
        record.lookup_name,
        role=record.role,
    )
    return f"تقدر تتواصل مع {label}."


def build_not_configured_reply(resolution: StaffContactResolution) -> str:
    if resolution.unknown_name or resolution.reason == "name_not_configured":
        return MSG_NAME_NOT_CONFIGURED
    if resolution.reason == "cs_not_configured":
        return MSG_CS_NOT_CONFIGURED
    return MSG_ESCALATION_NOT_CONFIGURED


__all__ = [
    "MSG_CS_NOT_CONFIGURED",
    "MSG_ESCALATION_NOT_CONFIGURED",
    "MSG_NAME_NOT_CONFIGURED",
    "MSG_NO_NEXT_ESCALATION",
    "MSG_CONTACT_CARD_FAILED",
    "StaffContactRecord",
    "StaffContactRegistry",
    "StaffContactRequest",
    "StaffContactResolution",
    "build_deliver_reply_text",
    "build_not_configured_reply",
    "build_staff_call_target",
    "build_staff_call_target_from_record",
    "classify_staff_contact_request",
    "compile_staff_contact_registry",
    "find_next_unsent_registry_contact",
    "is_usable_display_name",
    "load_staff_contact_registry",
    "resolve_contact_display_name",
    "resolve_staff_contact",
]
