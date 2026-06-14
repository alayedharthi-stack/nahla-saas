"""
Staff contact fallback v0 — next contact in merchant KB escalation chain.

When a customer reports the last suggested staff member is unavailable
(«مايرد», «البائع مايرد», «مقفل» after a prior vCard), advance to the
next showroom staff entry in KB document order. Owner contacts are
excluded unless the customer explicitly asks using a KB-defined alias
for ``role=owner``.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
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

_ROLE_ALIAS_SCAN_KINDS: frozenset[str] = frozenset(
    _CHAIN_SCAN_KINDS | _OWNER_IDENTITY_KINDS
)

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

_LABEL_PHONE_LINE_RE = re.compile(
    r"^(.{2,48}?)\s*[:：\-–—]\s*(\+?\s*966?\s*5\d{8}|05\d{8}|5\d{8})\s*$",
    re.MULTILINE | re.UNICODE,
)

_ROLE_BODY_BLOCK_RE = re.compile(
    r"(?ms)^\s*role\s*:\s*([a-zA-Z_][\w-]*)\s*\n\s*aliases\s*:\s*\n((?:\s*-\s*.+\n?)+)",
)
_ROLE_BODY_INLINE_RE = re.compile(
    r"(?ms)^\s*role\s*:\s*([a-zA-Z_][\w-]*)\s*\n\s*aliases\s*:\s*(.+?)\s*$",
)
_BODY_ALIAS_BULLET_RE = re.compile(r"^\s*-\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class StaffChainEntry:
    lookup_name: str
    phone: str
    section_id: int
    kind: str
    is_owner: bool
    chain_index: int
    role: str = ""


@dataclass(frozen=True)
class StaffRoleAliasGraph:
    """KB-defined contact-role aliases (e.g. owner → customer phrases)."""

    roles: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def aliases_for(self, role: str) -> Tuple[str, ...]:
        key = (role or "").strip().lower()
        return self.roles.get(key, ())


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
    explicit_role: str = ""


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


def _section_fields(section: Any) -> tuple[str, str, str, Dict[str, Any], Optional[int]]:
    if isinstance(section, dict):
        kind = str(section.get("kind") or "").strip().lower()
        title = str(section.get("title") or "").strip()
        body = str(section.get("body") or "").strip()
        meta = _metadata_dict(section.get("metadata"))
        if not meta:
            meta = _metadata_dict(section.get("metadata_json"))
        sid = section.get("id")
    else:
        kind = str(getattr(section, "kind", "") or "").strip().lower()
        title = str(getattr(section, "title", "") or "").strip()
        body = str(getattr(section, "body", "") or "").strip()
        meta = _metadata_dict(getattr(section, "metadata", None))
        if not meta:
            meta = _metadata_dict(getattr(section, "metadata_json", None))
        sid = getattr(section, "id", None)
    try:
        section_id = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        section_id = None
    combined = f"{title}\n{body}".strip() if title else body
    return kind, combined, body, meta, section_id


def _split_alias_tokens(raw: str) -> List[str]:
    parts = re.split(r"[,،\n|]+", raw or "")
    return [p.strip() for p in parts if p and p.strip()]


def _role_aliases_from_metadata(meta: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    if not meta:
        return []
    found: List[Tuple[str, List[str]]] = []

    role = str(meta.get("role") or meta.get("contact_role") or "").strip().lower()
    aliases_raw = meta.get("aliases")
    if role and isinstance(aliases_raw, (list, tuple)):
        found.append((
            role,
            [str(a).strip() for a in aliases_raw if str(a).strip()],
        ))
    elif role and isinstance(aliases_raw, str) and aliases_raw.strip():
        found.append((role, _split_alias_tokens(aliases_raw)))

    for key in ("staff_contact_roles", "contact_roles"):
        block = meta.get(key)
        if not isinstance(block, list):
            continue
        for item in block:
            if not isinstance(item, dict):
                continue
            item_role = str(item.get("role") or "").strip().lower()
            item_aliases = item.get("aliases")
            if not item_role:
                continue
            if isinstance(item_aliases, (list, tuple)):
                found.append((
                    item_role,
                    [str(a).strip() for a in item_aliases if str(a).strip()],
                ))
            elif isinstance(item_aliases, str) and item_aliases.strip():
                found.append((item_role, _split_alias_tokens(item_aliases)))
    return found


def _role_aliases_from_body(body: str) -> List[Tuple[str, List[str]]]:
    if not body:
        return []
    found: List[Tuple[str, List[str]]] = []
    for m in _ROLE_BODY_BLOCK_RE.finditer(body):
        role = m.group(1).strip().lower()
        aliases = [
            a.strip()
            for a in _BODY_ALIAS_BULLET_RE.findall(m.group(2))
            if a.strip()
        ]
        if role and aliases:
            found.append((role, aliases))
    for m in _ROLE_BODY_INLINE_RE.finditer(body):
        role = m.group(1).strip().lower()
        aliases = _split_alias_tokens(m.group(2))
        if role and aliases:
            found.append((role, aliases))
    return found


def extract_staff_role_aliases_from_sections(
    sections: Sequence[Any],
) -> StaffRoleAliasGraph:
    """Compile role → alias mappings declared in merchant KB sections."""
    merged: Dict[str, List[str]] = {}
    for section in sections or ():
        kind, combined, body, meta, _sid = _section_fields(section)
        if kind and kind not in _ROLE_ALIAS_SCAN_KINDS:
            continue
        for role, aliases in (
            *_role_aliases_from_metadata(meta),
            *_role_aliases_from_body(body),
            *_role_aliases_from_body(combined),
        ):
            bucket = merged.setdefault(role, [])
            for alias in aliases:
                if alias not in bucket:
                    bucket.append(alias)
    return StaffRoleAliasGraph(
        roles={role: tuple(aliases) for role, aliases in merged.items()},
    )


def classify_explicit_role_request(
    message: str,
    aliases: Sequence[str],
) -> bool:
    """True when the customer message matches a KB-defined role alias."""
    norm_msg = _norm(_norm_alif(message or ""))
    if not norm_msg or not aliases:
        return False
    for alias in aliases:
        norm_alias = _norm(_norm_alif(alias))
        if not norm_alias or len(norm_alias) < 2:
            continue
        if norm_alias in norm_msg:
            return True
    return False


def _label_matches_role_alias(label: str, aliases: Sequence[str]) -> bool:
    norm_label = _normalize_name_key(label)
    if not norm_label:
        return False
    for alias in aliases:
        norm_alias = _normalize_name_key(alias)
        if not norm_alias:
            continue
        if (
            norm_alias == norm_label
            or norm_alias in norm_label
            or norm_label in norm_alias
        ):
            return True
    return False


def _section_role(meta: Dict[str, Any]) -> str:
    return str(meta.get("role") or meta.get("contact_role") or "").strip().lower()


def _is_owner_entry(
    *,
    kind: str,
    label: str,
    section_meta: Dict[str, Any],
    owner_aliases: Sequence[str],
) -> bool:
    k = (kind or "").strip().lower()
    if k in _OWNER_IDENTITY_KINDS:
        return True
    if _section_role(section_meta) == "owner":
        return True
    return _label_matches_role_alias(label, owner_aliases)


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


def extract_staff_chain_from_sections(
    sections: Sequence[Any],
    *,
    role_graph: Optional[StaffRoleAliasGraph] = None,
) -> List[StaffChainEntry]:
    """Build ordered staff chain from KB sections (document order)."""
    graph = role_graph or extract_staff_role_aliases_from_sections(sections)
    owner_aliases = graph.aliases_for("owner")
    chain: List[StaffChainEntry] = []
    seen_phones: set[str] = set()
    idx = 0

    for section in sections or ():
        kind, combined, _body, meta, section_id = _section_fields(section)
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
                is_owner = _is_owner_entry(
                    kind=kind,
                    label=label,
                    section_meta=meta,
                    owner_aliases=owner_aliases,
                )
                seen_phones.add(phone_key)
                chain.append(
                    StaffChainEntry(
                        lookup_name=label,
                        phone=phone,
                        section_id=int(section_id),
                        kind=kind or "",
                        is_owner=is_owner,
                        chain_index=idx,
                        role="owner" if is_owner else "",
                    )
                )
                idx += 1
    return chain


_GENERIC_ROLE_NAME_KEYS = frozenset({
    _normalize_name_key(label)
    for label in (
        "بائع المعرض",
        "البائع",
        "بائع",
        "موظف المعرض",
        "خدمة العملاء",
        "خدمه العملاء",
        "دعم العملاء",
        "المندوب",
        "المحاسب",
        "الموظف",
    )
})


def _names_match_for_sent(entry_name: str, sent_name: str) -> bool:
    en = _normalize_name_key(entry_name)
    sn = _normalize_name_key(sent_name)
    if not en or not sn:
        return False
    if en in _GENERIC_ROLE_NAME_KEYS or sn in _GENERIC_ROLE_NAME_KEYS:
        return False
    return en == sn or en in sn or sn in en


def _entry_matches_sent(
    entry: StaffChainEntry,
    contacts_sent: Sequence[Dict[str, Any]],
) -> bool:
    ep = _normalize_phone_key(entry.phone)
    for item in contacts_sent or ():
        sent_phone = _normalize_phone_key(str(item.get("phone") or ""))
        if ep and sent_phone and ep == sent_phone:
            return True
        if _names_match_for_sent(entry.lookup_name, str(item.get("name") or "")):
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

    role_graph = extract_staff_role_aliases_from_sections(sections or ())
    owner_aliases = role_graph.aliases_for("owner")
    explicit_owner = classify_explicit_role_request(customer_msg, owner_aliases)
    chain = extract_staff_chain_from_sections(
        sections or (),
        role_graph=role_graph,
    )
    showroom_chain = [e for e in chain if not e.is_owner]

    log_staff_contact_fallback_policy(
        tenant_id=tenant_id,
        trigger=trigger,
        chain_len=len(chain),
        showroom_chain_len=len(showroom_chain),
        contacts_sent_count=len(contacts_sent),
        owner_explicit=explicit_owner,
        owner_alias_count=len(owner_aliases),
    )

    if explicit_owner:
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
                reason="explicit_role_request",
                chain_index=entry.chain_index,
                last_sent_index=_last_sent_chain_index(chain, contacts_sent),
                explicit_role="owner",
            )
            return StaffContactFallbackVerdict(
                enabled=True,
                trigger=trigger,
                reason="explicit_role_request",
                next_lookup_name=entry.lookup_name,
                next_phone=entry.phone,
                section_id=entry.section_id,
                chain_len=len(chain),
                last_sent_index=_last_sent_chain_index(chain, contacts_sent),
                explicit_role="owner",
            )
        log_staff_contact_fallback_resolve(
            tenant_id=tenant_id,
            trigger=trigger,
            selected="",
            phone="",
            section_id=None,
            reason="role_unavailable",
            chain_index=-1,
            last_sent_index=_last_sent_chain_index(chain, contacts_sent),
            explicit_role="owner",
        )
        return StaffContactFallbackVerdict(
            enabled=False,
            trigger=trigger,
            reason="role_unavailable",
            chain_len=len(chain),
            last_sent_index=_last_sent_chain_index(chain, contacts_sent),
            explicit_role="owner",
        )

    if not chain:
        return StaffContactFallbackVerdict(
            enabled=False,
            trigger=trigger,
            reason="empty_chain",
            chain_len=len(chain),
        )

    from modules.ai.brain.commerce.staff_contact_escalation_chain import (  # noqa: PLC0415
        classify_contact_tier,
        find_last_sent_chain_entry,
        log_escalation_chain_resolve,
        resolve_next_tiered_contact,
    )

    last_entry = find_last_sent_chain_entry(chain, contacts_sent)
    last_tier = classify_contact_tier(last_entry) if last_entry else ""
    last_idx = last_entry.chain_index if last_entry else -1

    next_entry = resolve_next_tiered_contact(
        chain,
        contacts_sent,
        allow_admin=True,
    )
    if next_entry is not None:
        log_escalation_chain_resolve(
            tenant_id=tenant_id,
            trigger=trigger,
            last_tier=last_tier,
            selected=next_entry.lookup_name,
            phone=next_entry.phone,
            reason="next_in_tier_chain",
        )
        log_staff_contact_fallback_resolve(
            tenant_id=tenant_id,
            trigger=trigger,
            selected=next_entry.lookup_name,
            phone=next_entry.phone,
            section_id=next_entry.section_id,
            reason="next_in_tier_chain",
            chain_index=next_entry.chain_index,
            last_sent_index=last_idx,
        )
        return StaffContactFallbackVerdict(
            enabled=True,
            trigger=trigger,
            reason="next_in_tier_chain",
            next_lookup_name=next_entry.lookup_name,
            next_phone=next_entry.phone,
            section_id=next_entry.section_id,
            chain_len=len(chain),
            last_sent_index=last_idx,
        )

    log_escalation_chain_resolve(
        tenant_id=tenant_id,
        trigger=trigger,
        last_tier=last_tier,
        selected="",
        phone="",
        reason="chain_exhausted",
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
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415

        kinds = tuple(_CHAIN_SCAN_KINDS | _OWNER_IDENTITY_KINDS)
        return (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
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
    owner_alias_count: int = 0,
) -> None:
    try:
        logger.info(
            "[STAFF_CONTACT_FALLBACK_POLICY] tenant=%s trigger=%s "
            "enabled=%s chain_len=%d showroom_chain_len=%d "
            "contacts_sent_count=%d owner_explicit=%s owner_alias_count=%d",
            tenant_id if tenant_id is not None else "-",
            trigger or "-",
            "true" if contacts_sent_count > 0 and chain_len > 0 else "false",
            chain_len,
            showroom_chain_len,
            contacts_sent_count,
            "true" if owner_explicit else "false",
            owner_alias_count,
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
    explicit_role: str = "",
) -> None:
    try:
        logger.info(
            "[STAFF_CONTACT_FALLBACK_RESOLVE] tenant=%s trigger=%s "
            "reason=%r selected=%r phone_len=%d section_id=%s "
            "chain_index=%d last_sent_index=%d explicit_role=%s",
            tenant_id if tenant_id is not None else "-",
            trigger or "-",
            (reason or "")[:64],
            (selected or "")[:48],
            len(re.sub(r"\D", "", phone or "")),
            section_id if section_id is not None else "-",
            chain_index,
            last_sent_index,
            explicit_role or "-",
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "StaffChainEntry",
    "StaffContactFallbackVerdict",
    "StaffRoleAliasGraph",
    "classify_explicit_role_request",
    "extract_staff_chain_from_sections",
    "extract_staff_role_aliases_from_sections",
    "load_staff_chain_sections",
    "log_staff_contact_fallback_policy",
    "log_staff_contact_fallback_resolve",
    "resolve_staff_contact_fallback_v0",
]
