"""
Structured branch contact evidence — Operations Center (PR-A).

Read-only DB accessors for branch locations and reception contacts.
No LLM, no KB parsing. Used when USE_STRUCTURED_BRANCH_CONTACTS is ON.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("nahla.operations.branch_contact_evidence")

_FLAG_FALSY = frozenset({"0", "false", "no", "off"})

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_RECEPTION_ROLE_TOKENS = frozenset({
    "showroom",
    "reception",
    "seller",
    "delivery",
    "branch_staff",
    "بائع_المعرض",
    "بائع المعرض",
    "الاستقبال",
    "استقبال",
    "موظف الفرع",
    "مسؤول التسليم",
    "المعرض",
})

_ADMIN_ROLE_TOKENS = frozenset({
    "admin",
    "owner",
    "management",
    "الإدارة",
    "ادارة",
    "مدير",
})


def structured_branch_contacts_enabled() -> bool:
    raw = os.getenv("USE_STRUCTURED_BRANCH_CONTACTS", "0").strip().lower()
    return raw not in _FLAG_FALSY


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
class BranchRecord:
    id: int
    tenant_id: int
    name: str
    city: str
    district: str
    address: str
    maps_url: str
    sort_order: int


@dataclass(frozen=True)
class BranchContactRecord:
    id: int
    branch_id: int
    display_name: str
    role: str
    phone_e164: str
    whatsapp_e164: str
    sort_order: int
    is_default_reception: bool = False


def _branch_from_row(row: Any) -> BranchRecord:
    return BranchRecord(
        id=int(row.id),
        tenant_id=int(row.tenant_id),
        name=str(row.name or "").strip(),
        city=str(row.city or "").strip(),
        district=str(row.district or "").strip(),
        address=str(row.address or "").strip(),
        maps_url=str(row.maps_url or "").strip(),
        sort_order=int(row.sort_order or 0),
    )


def _contact_from_row(row: Any) -> BranchContactRecord:
    return BranchContactRecord(
        id=int(row.id),
        branch_id=int(row.branch_id),
        display_name=str(row.display_name or "").strip(),
        role=str(row.role or "").strip(),
        phone_e164=str(row.phone_e164 or "").strip(),
        whatsapp_e164=str(row.whatsapp_e164 or "").strip(),
        sort_order=int(row.sort_order or 0),
        is_default_reception=bool(getattr(row, "is_default_reception", False)),
    )


def load_active_branches(db: Any, tenant_id: int) -> Tuple[BranchRecord, ...]:
    """Return active branches for tenant ordered by sort_order, id."""
    if db is None or not tenant_id:
        return ()
    try:
        from database.models import MerchantBranch  # noqa: PLC0415

        rows = (
            db.query(MerchantBranch)
            .filter(
                MerchantBranch.tenant_id == int(tenant_id),
                MerchantBranch.is_active.is_(True),
            )
            .order_by(
                MerchantBranch.sort_order.asc(),
                MerchantBranch.id.asc(),
            )
            .all()
        )
        return tuple(_branch_from_row(r) for r in rows)
    except Exception as exc:  # noqa: silent-ok - branch query failure degrades to empty evidence
        logger.debug(
            "branch_contact_evidence.load_active_branches failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return ()


def resolve_branch_for_message(
    db: Any,
    tenant_id: int,
    message: str = "",
) -> Optional[BranchRecord]:
    """Pick branch by city/district/name hint, else default first active."""
    branches = load_active_branches(db, int(tenant_id or 0))
    if not branches:
        return None
    if len(branches) == 1:
        return branches[0]

    msg_norm = _norm(message or "")
    if msg_norm:
        for branch in branches:
            for hint in (branch.city, branch.district, branch.name):
                hint_norm = _norm(hint)
                if hint_norm and len(hint_norm) >= 2 and hint_norm in msg_norm:
                    return branch
    return branches[0]


def lookup_structured_maps_url(
    db: Any,
    tenant_id: int,
    message: str = "",
) -> Tuple[str, str, Optional[int]]:
    """Return ``(maps_url, source, branch_id)`` from structured branches."""
    if not structured_branch_contacts_enabled():
        return "", "none", None

    branch = resolve_branch_for_message(db, int(tenant_id or 0), message)
    if branch is None or not branch.maps_url:
        return "", "none", None

    url = branch.maps_url.strip()
    if not url:
        return "", "none", None

    logger.info(
        "[BRANCH_CONTACT_EVIDENCE] maps_url tenant=%s branch_id=%s url_len=%d",
        tenant_id,
        branch.id,
        len(url),
    )
    return url, "structured_branch", branch.id


def load_branch_contacts(
    db: Any,
    branch_id: int,
) -> Tuple[BranchContactRecord, ...]:
    if db is None or not branch_id:
        return ()
    try:
        from database.models import BranchContact  # noqa: PLC0415

        rows = (
            db.query(BranchContact)
            .filter(
                BranchContact.branch_id == int(branch_id),
                BranchContact.is_active.is_(True),
            )
            .order_by(
                BranchContact.sort_order.asc(),
                BranchContact.id.asc(),
            )
            .all()
        )
        return tuple(_contact_from_row(r) for r in rows)
    except Exception as exc:  # noqa: silent-ok - contact query failure degrades to empty evidence
        logger.debug(
            "branch_contact_evidence.load_branch_contacts failed branch=%s err=%s",
            branch_id,
            exc,
        )
        return ()


def _is_reception_role(role: str) -> bool:
    key = _norm(role).replace(" ", "_")
    role_norm = _norm(role)
    if key in _RECEPTION_ROLE_TOKENS or role_norm in _RECEPTION_ROLE_TOKENS:
        return True
    for token in _RECEPTION_ROLE_TOKENS:
        if token in role_norm:
            return True
    return False


def resolve_reception_contact(
    db: Any,
    tenant_id: int,
    message: str = "",
) -> Optional[BranchContactRecord]:
    """Default reception contact for arrival / showroom delivery."""
    if not structured_branch_contacts_enabled():
        return None

    branch = resolve_branch_for_message(db, int(tenant_id or 0), message)
    if branch is None:
        return None

    contacts = load_branch_contacts(db, branch.id)
    if not contacts:
        return None

    for contact in contacts:
        if contact.is_default_reception and contact.phone_e164:
            return contact

    for contact in contacts:
        if _is_reception_role(contact.role):
            return contact
    return contacts[0]


def resolve_reception_for_branch_id(
    db: Any,
    branch_id: int,
) -> Optional[BranchContactRecord]:
    """Reception contact for a specific branch — dashboard preview (no flag gate)."""
    if db is None or not branch_id:
        return None
    contacts = load_branch_contacts(db, int(branch_id))
    if not contacts:
        return None
    for contact in contacts:
        if contact.is_default_reception and contact.phone_e164:
            return contact
    for contact in contacts:
        if _is_reception_role(contact.role):
            return contact
    return contacts[0]


def tenant_has_structured_branch_data(db: Any, tenant_id: int) -> bool:
    """True when tenant has at least one active branch with contacts or maps."""
    branches = load_active_branches(db, int(tenant_id or 0))
    if not branches:
        return False
    for branch in branches:
        if branch.maps_url:
            return True
        if load_branch_contacts(db, branch.id):
            return True
    return False


def load_structured_staff_contact_registry(
    db: Any,
    tenant_id: int,
) -> Optional[Any]:
    """Build ``StaffContactRegistry`` from all active branch contacts."""
    if not structured_branch_contacts_enabled():
        return None

    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        StaffContactRecord,
        StaffContactRegistry,
    )

    branches = load_active_branches(db, int(tenant_id or 0))
    if not branches:
        return None

    records: List[StaffContactRecord] = []
    chain_index = 0
    for branch in branches:
        for contact in load_branch_contacts(db, branch.id):
            phone = contact.phone_e164 or contact.whatsapp_e164
            if not phone:
                continue
            role = (contact.role or "").strip().lower()
            is_owner = (
                role in _ADMIN_ROLE_TOKENS
                or _norm(contact.role) in _ADMIN_ROLE_TOKENS
            )
            records.append(
                StaffContactRecord(
                    lookup_name=contact.display_name,
                    phone=phone,
                    section_id=contact.id,
                    role=role,
                    aliases=(branch.name,) if branch.name else (),
                    is_owner=is_owner,
                    chain_index=chain_index,
                    source="structured_branch_contact",
                )
            )
            chain_index += 1

    if not records:
        return None

    logger.info(
        "[BRANCH_CONTACT_EVIDENCE] registry tenant=%s records=%d branches=%d",
        tenant_id,
        len(records),
        len(branches),
    )
    return StaffContactRegistry(records=tuple(records), store_contact_phone="")


__all__ = [
    "BranchContactRecord",
    "BranchRecord",
    "load_active_branches",
    "load_branch_contacts",
    "load_structured_staff_contact_registry",
    "lookup_structured_maps_url",
    "resolve_branch_for_message",
    "resolve_reception_contact",
    "resolve_reception_for_branch_id",
    "structured_branch_contacts_enabled",
    "tenant_has_structured_branch_data",
]
