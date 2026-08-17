"""Surface KB phone numbers that conflict with structured contacts.

Structured contact records win. This module never uses KB phones as
execution source; it only reports conflicts for merchant/admin review.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from utils.phone_utils import normalize_to_e164

_DIGIT_RE = re.compile(r"(?:\+?966|0)?5\d{8}")


def _digits(raw: str) -> str:
    return re.sub(r"\D+", "", raw or "")


def extract_phone_candidates(text: str) -> List[str]:
    found: List[str] = []
    for match in _DIGIT_RE.finditer(text or ""):
        e164 = normalize_to_e164(match.group(0) or "")
        if e164 and e164 not in found:
            found.append(e164)
    return found


def find_kb_contact_conflicts(
    db: Any,
    tenant_id: int,
    contacts: Sequence[Any],
) -> List[Dict[str, Any]]:
    if db is None or not tenant_id:
        return []
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
    except Exception:  # noqa: silent-ok - conflict scan must not break authoring
        return []

    structured_phones = set()
    for contact in contacts:
        for raw in (
            getattr(contact, "phone_e164", ""),
            getattr(contact, "whatsapp_e164", ""),
        ):
            e164 = normalize_to_e164(str(raw or ""))
            if e164:
                structured_phones.add(e164)
                structured_phones.add(_digits(e164))

    rows = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.tenant_id == int(tenant_id),
            MerchantKnowledgeSection.is_active.is_(True),
        )
        .all()
    )
    conflicts: List[Dict[str, Any]] = []
    for row in rows:
        body = str(getattr(row, "body", "") or "")
        title = str(getattr(row, "title", "") or "")
        for phone in extract_phone_candidates(f"{title}\n{body}"):
            digits = _digits(phone)
            if digits in structured_phones or phone in structured_phones:
                continue
            conflicts.append({
                "section_id": int(row.id),
                "title": title,
                "kb_phone": phone,
                "winner": "structured_contact",
                "message": "رقم قاعدة المعرفة لا يطابق جهات التواصل المعتمدة",
            })
    return conflicts
