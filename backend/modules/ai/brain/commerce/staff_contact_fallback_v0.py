"""
Staff contact fallback v0 — next contact in merchant KB escalation chain.

When a customer reports the last suggested staff member is unavailable
(«مايرد», «البائع مايرد», «مقفل» after a prior vCard), advance to the
next showroom staff entry in KB document order. Owner contacts are
excluded unless the customer explicitly asks for the owner.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.staff_contact_fallback_v0")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_CHAIN_SCAN_KINDS: frozenset[str] = frozenset({
    "escalation_rules",
    "branches",
    "custom",
    "store_story",
    "quick_update",
    "faq",
})

_OWNER_IDENTITY_KINDS: frozenset[str] = frozenset({"owner_identity"})

_PHONE_REGEXES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\+?\s*9665\d{8}\b"),
    re.compile(r"\b00\s*9665\d{8}\b"),
    re.compile(r"\b05\d{8}\b"),
    re.compile(r"\b5\d{8}\b"),
)

_SHOWROOM_ROLE_RE = re.compile(
    r"(?:"
    r"بائع\s*المعرض"
    r"|(?:^|\s)البائع(?:\s|[،,.:]|$)"
    r"|(?:^|\s)بائع(?:\s|[،,.:]|$)"
    r"|موظف\s*المعرض"
    r"|(?:^|\s)المحاسب(?:\s|[،,.:]|$)"
    r"|(?:^|\s)المندوب(?:\s|[،,.:]|$)"
    r"|(?:^|\s)الموظف(?:\s|[،,.:]|$)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_OWNER_LABEL_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:المالك|مالك\s*المتجر|صاحب\s*الم(?:حل|تجر)|owner)"
    r"|(?:^|\s)أبو\s*هشام"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_EXPLICIT_OWNER_ASK_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ابي|ابغ|اريد|أبي|أبغ|أريد|ابغى|أبغى)\s*(?:رقم\s*)?"
    r"(?:المالك|مالك|صاحب\s*الم(?:حل|تجر)|owner|أبو\s*هشام)"
    r"|(?:^|\s)(?:المالك|مالك\s*المتجر|صاحب\s*الم(?:حل|تجر))"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_LABEL_PHONE_LINE_RE = re.compile(
    r"^(.{2,48}?)\s*[:：\-–—]\s*(\+?\s*966?\s*5\d{8}|05\d{8}|5\d{8})\s*$",
    re.MULTILINE | re.UNICODE,
)


@dataclass(frozen=True)
class StaffChainEntry:
    lookup_name: str
    phone: str
    section_id: int
    kind: str
    is_owner: bool
    chain_index: int


@dataclass(frozen=True)
class StaffContactFallbackVerdict:
    enabled: bool
    trigger: str = ""
    reason: str = ""
    next_lookup_name: str = ""
    next_phone: str = ""
    section_id: Optional[int] = None
    chain_len: int = 0
    last_sent_index: int = -1


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    return _WS_RE.sub(" ", t).strip()


def _norm_alif(text: str) -> str:
    return (
        text
        .replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )


def _normalize_phone_key(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("966") and len(digits) >= 12:
        return digits[-9:]
    if len(digits) >= 9:
        return digits[-9:]
    return digits


def _normalize_name_key(name: str) -> str:
    return _norm(_norm_alif(name or ""))


def classify_explicit_owner_request(message: str) -> bool:
    norm = _norm(_norm_alif(message or ""))
    if not norm:
        return False
    return bool(_EXPLICIT_OWNER_ASK_RE.search(norm))


def _is_owner_label(label: str, kind: str) -> bool:
    k = (kind or "").strip().lower()
    if k in _OWNER_IDENTITY_KINDS:
        return True
    norm = _norm(_norm_alif(label or ""))
    if not norm:
        return False
    if _OWNER_LABEL_RE.search(norm):
        return True
    if "مالك" in norm and not _SHOWROOM_ROLE_RE.search(norm):
        return True
    return False


def _extract_label_near_phone(body: str, phone_start: int) -> str:
    line_start = body.rfind("\n", 0, phone_start) + 1
    line_end = body.find("\n", phone_start)
    if line_end < 0:
        line_end = len(body)
    line = body[line_start:line_end].strip()
    m = _LABEL_PHONE_LINE_RE.match(line)
    if m:
        return m.group(1).strip()
    window = body[max(0, phone_start - 48):phone_start]
    window = re.sub(r"[\d+()\s\-]+$", "", window).strip()
    if window:
        parts = re.split(r"[:：\-–—]", window)
        if parts:
            return parts[-1].strip()
    if _SHOWROOM_ROLE_RE.search(line):
        m2 = _SHOWROOM_ROLE_RE.search(line)
        if m2:
            return m2.group(0).strip()
    return ""


def _section_fields(section: Any) -> tuple[str, str, str, Optional[int]]:
    if isinstance(section, dict):
        kind = str(section.get("kind") or "").strip().lower()
        title = str(section.get("title") or "").strip()
        body = str(section.get("body") or "").strip()
        sid = section.get("id")
    else:
        kind = str(getattr(section, "kind", "") or "").strip().lower()
        title = str(getattr(section, "title", "") or "").strip()
        body = str(getattr(section, "body", "") or "").strip()
        sid = getattr(section, "id", None)
    try:
        section_id = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        section_id = None
    combined = f"{title}\n{body}".strip() if title else body
    return kind, combined, body, section_id


def extract_staff_chain_from_sections(
    sections: Sequence[Any],
) -> List[StaffChainEntry]:
    """Build ordered showroom staff chain from KB sections (document order)."""
    chain: List[StaffChainEntry] = []
    seen_phones: set[str] = set()
    idx = 0

    for section in sections or ():
        kind, combined, _body, section_id = _section_fields(section)
        if kind and kind not in _CHAIN_SCAN_KINDS and kind not in _OWNER_IDENTITY_KINDS:
            continue
        if not combined or section_id is None:
            continue

        for pat in _PHONE_REGEXES:
            for m in pat.finditer(combined):
                phone = m.group(0).strip()
                phone_key = _normalize_phone_key(phone)
                if not phone_key or phone_key in seen_phones:
                    continue
                label = _extract_label_near_phone(combined, m.start())
                if not label:
                    continue
                is_owner = _is_owner_label(label, kind)
                seen_phones.add(phone_key)
                chain.append(
                    StaffChainEntry(
                        lookup_name=label,
                        phone=phone,
                        section_id=int(section_id),
                        kind=kind or "",
                        is_owner=is_owner,
                        chain_index=idx,
                    )
                )
                idx += 1
    return chain


def _entry_matches_sent(
    entry: StaffChainEntry,
    contacts_sent: Sequence[Dict[str, Any]],
) -> bool:
    ep = _normalize_phone_key(entry.phone)
    en = _normalize_name_key(entry.lookup_name)
    for item in contacts_sent or ():
        sent_phone = _normalize_phone_key(str(item.get("phone") or ""))
        sent_name = _normalize_name_key(str(item.get("name") or ""))
        if ep and sent_phone and ep == sent_phone:
            return True
        if en and sent_name and (
            en == sent_name
            or en in sent_name
            or sent_name in en
        ):
            return True
    return False


def _last_sent_chain_index(
    chain: Sequence[StaffChainEntry],
    contacts_sent: Sequence[Dict[str, Any]],
) -> int:
    last = -1
    for entry in chain:
        if _entry_matches_sent(entry, contacts_sent):
            last = max(last, entry.chain_index)
    return last


def resolve_staff_contact_fallback_v0(
    sections: Optional[Sequence[Any]],
    *,
    contacts_sent: Sequence[Dict[str, Any]],
    customer_msg: str,
    trigger: str,
    tenant_id: Any = None,
) -> StaffContactFallbackVerdict:
    """Pick the next KB chain contact after a staff-unavailable follow-up."""
    if not contacts_sent:
        return StaffContactFallbackVerdict(
            enabled=False,
            trigger=trigger,
            reason="no_prior_sent",
        )

    owner_explicit = classify_explicit_owner_request(customer_msg)
    chain = extract_staff_chain_from_sections(sections or ())
    showroom_chain = [e for e in chain if not e.is_owner]

    log_staff_contact_fallback_policy(
        tenant_id=tenant_id,
        trigger=trigger,
        chain_len=len(chain),
        showroom_chain_len=len(showroom_chain),
        contacts_sent_count=len(contacts_sent),
        owner_explicit=owner_explicit,
    )

    if owner_explicit:
        for entry in chain:
            if not entry.is_owner:
                continue
            if _entry_matches_sent(entry, contacts_sent):
                continue
            log_staff_contact_fallback_resolve(
                tenant_id=tenant_id,
                trigger=trigger,
                selected=entry.lookup_name,
                phone=entry.phone,
                section_id=entry.section_id,
                reason="explicit_owner_request",
                chain_index=entry.chain_index,
                last_sent_index=_last_sent_chain_index(chain, contacts_sent),
            )
            return StaffContactFallbackVerdict(
                enabled=True,
                trigger=trigger,
                reason="explicit_owner_request",
                next_lookup_name=entry.lookup_name,
                next_phone=entry.phone,
                section_id=entry.section_id,
                chain_len=len(chain),
                last_sent_index=_last_sent_chain_index(chain, contacts_sent),
            )
        log_staff_contact_fallback_resolve(
            tenant_id=tenant_id,
            trigger=trigger,
            selected="",
            phone="",
            section_id=None,
            reason="owner_unavailable",
            chain_index=-1,
            last_sent_index=_last_sent_chain_index(chain, contacts_sent),
        )
        return StaffContactFallbackVerdict(
            enabled=False,
            trigger=trigger,
            reason="owner_unavailable",
            chain_len=len(chain),
            last_sent_index=_last_sent_chain_index(chain, contacts_sent),
        )

    if not showroom_chain:
        return StaffContactFallbackVerdict(
            enabled=False,
            trigger=trigger,
            reason="empty_chain",
            chain_len=len(chain),
        )

    last_idx = _last_sent_chain_index(showroom_chain, contacts_sent)
    for entry in showroom_chain:
        if entry.chain_index <= last_idx:
            continue
        if _entry_matches_sent(entry, contacts_sent):
            continue
        log_staff_contact_fallback_resolve(
            tenant_id=tenant_id,
            trigger=trigger,
            selected=entry.lookup_name,
            phone=entry.phone,
            section_id=entry.section_id,
            reason="next_in_chain",
            chain_index=entry.chain_index,
            last_sent_index=last_idx,
        )
        return StaffContactFallbackVerdict(
            enabled=True,
            trigger=trigger,
            reason="next_in_chain",
            next_lookup_name=entry.lookup_name,
            next_phone=entry.phone,
            section_id=entry.section_id,
            chain_len=len(chain),
            last_sent_index=last_idx,
        )

    log_staff_contact_fallback_resolve(
        tenant_id=tenant_id,
        trigger=trigger,
        selected="",
        phone="",
        section_id=None,
        reason="chain_exhausted",
        chain_index=-1,
        last_sent_index=last_idx,
    )
    return StaffContactFallbackVerdict(
        enabled=False,
        trigger=trigger,
        reason="chain_exhausted",
        chain_len=len(chain),
        last_sent_index=last_idx,
    )


def load_staff_chain_sections(db: Any, tenant_id: int) -> Sequence[Any]:
    if db is None or not tenant_id:
        return ()
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415

        kinds = tuple(_CHAIN_SCAN_KINDS | _OWNER_IDENTITY_KINDS)
        return (
            db.query(MerchantKnowledgeSection)
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.is_active.is_(True),
                MerchantKnowledgeSection.kind.in_(kinds),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(60)
            .all()
        )
    except Exception:  # noqa: BLE001
        return ()


def log_staff_contact_fallback_policy(
    *,
    tenant_id: Any = None,
    trigger: str = "",
    chain_len: int = 0,
    showroom_chain_len: int = 0,
    contacts_sent_count: int = 0,
    owner_explicit: bool = False,
) -> None:
    try:
        logger.info(
            "[STAFF_CONTACT_FALLBACK_POLICY] tenant=%s trigger=%s "
            "enabled=%s chain_len=%d showroom_chain_len=%d "
            "contacts_sent_count=%d owner_explicit=%s",
            tenant_id if tenant_id is not None else "-",
            trigger or "-",
            "true" if contacts_sent_count > 0 and chain_len > 0 else "false",
            chain_len,
            showroom_chain_len,
            contacts_sent_count,
            "true" if owner_explicit else "false",
        )
    except Exception:  # noqa: BLE001
        pass


def log_staff_contact_fallback_resolve(
    *,
    tenant_id: Any = None,
    trigger: str = "",
    selected: str = "",
    phone: str = "",
    section_id: Optional[int] = None,
    reason: str = "",
    chain_index: int = -1,
    last_sent_index: int = -1,
) -> None:
    try:
        logger.info(
            "[STAFF_CONTACT_FALLBACK_RESOLVE] tenant=%s trigger=%s "
            "reason=%r selected=%r phone_len=%d section_id=%s "
            "chain_index=%d last_sent_index=%d",
            tenant_id if tenant_id is not None else "-",
            trigger or "-",
            (reason or "")[:64],
            (selected or "")[:48],
            len(re.sub(r"\D", "", phone or "")),
            section_id if section_id is not None else "-",
            chain_index,
            last_sent_index,
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "StaffChainEntry",
    "StaffContactFallbackVerdict",
    "classify_explicit_owner_request",
    "extract_staff_chain_from_sections",
    "load_staff_chain_sections",
    "log_staff_contact_fallback_policy",
    "log_staff_contact_fallback_resolve",
    "resolve_staff_contact_fallback_v0",
]
