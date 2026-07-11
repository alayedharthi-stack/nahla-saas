"""
Branch arrival keyword evidence — Operations Center (PR-C).

Deterministic phrase matching for location / arrival / no-response triggers.
No LLM, no KB parsing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence, Tuple

from modules.operations.branch_contact_evidence import (
    _norm,
    load_active_branches,
    resolve_branch_for_message,
    structured_branch_contacts_enabled,
    tenant_has_structured_branch_data,
)

logger = logging.getLogger("nahla.operations.branch_arrival_keyword_evidence")

TRIGGER_LOCATION_REQUEST = "location_request"
TRIGGER_ARRIVAL_SOFT = "arrival_soft"
TRIGGER_ARRIVAL_CONFIRMED = "arrival_confirmed"
TRIGGER_NO_RESPONSE = "no_response"

VALID_TRIGGER_TYPES = frozenset({
    TRIGGER_LOCATION_REQUEST,
    TRIGGER_ARRIVAL_SOFT,
    TRIGGER_ARRIVAL_CONFIRMED,
    TRIGGER_NO_RESPONSE,
})

LOCATION_MODE_ONLY = "location_only"
LOCATION_MODE_PLUS_RECEPTION = "location_plus_reception"
LOCATION_MODE_PLUS_INSTRUCTIONS = "location_plus_instructions"

ARRIVAL_MODE_RECEPTION_ONLY = "reception_only"
ARRIVAL_MODE_LOCATION_AND_RECEPTION = "location_and_reception"
ARRIVAL_MODE_ASK_BRANCH_FIRST = "ask_branch_first"

PLATFORM_DEFAULT_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("وين موقعكم", TRIGGER_LOCATION_REQUEST),
    ("وين موقع", TRIGGER_LOCATION_REQUEST),
    ("ارسل الموقع", TRIGGER_LOCATION_REQUEST),
    ("أرسل الموقع", TRIGGER_LOCATION_REQUEST),
    ("اللوكيشن", TRIGGER_LOCATION_REQUEST),
    ("لوكيشن", TRIGGER_LOCATION_REQUEST),
    ("وين المعرض", TRIGGER_LOCATION_REQUEST),
    ("الموقع", TRIGGER_LOCATION_REQUEST),
    ("أنا في الطريق", TRIGGER_ARRIVAL_SOFT),
    ("انا في الطريق", TRIGGER_ARRIVAL_SOFT),
    ("أنا جاي", TRIGGER_ARRIVAL_SOFT),
    ("انا جاي", TRIGGER_ARRIVAL_SOFT),
    ("قريب منكم", TRIGGER_ARRIVAL_SOFT),
    ("وصلت", TRIGGER_ARRIVAL_CONFIRMED),
    ("عند الباب", TRIGGER_ARRIVAL_CONFIRMED),
    ("عند البوابة", TRIGGER_ARRIVAL_CONFIRMED),
    ("في الحوش", TRIGGER_ARRIVAL_CONFIRMED),
    ("واقف عند المعرض", TRIGGER_ARRIVAL_CONFIRMED),
    ("وصلت الموقع", TRIGGER_ARRIVAL_CONFIRMED),
    ("ما لقيت أحد", TRIGGER_NO_RESPONSE),
    ("مالقيت احد", TRIGGER_NO_RESPONSE),
    ("مالقيت أحد", TRIGGER_NO_RESPONSE),
    ("ما لقيت احد", TRIGGER_NO_RESPONSE),
    ("مافي احد", TRIGGER_NO_RESPONSE),
    ("مافيه احد", TRIGGER_NO_RESPONSE),
    ("ما فيه احد", TRIGGER_NO_RESPONSE),
    ("ما أحد يرد", TRIGGER_NO_RESPONSE),
    ("وينكم", TRIGGER_NO_RESPONSE),
    ("وصلت وما لقيت احد", TRIGGER_NO_RESPONSE),
    ("ما يرد", TRIGGER_NO_RESPONSE),
    ("محد رد", TRIGGER_NO_RESPONSE),
    ("ما يجاوب", TRIGGER_NO_RESPONSE),
    ("المعرض مقفل", TRIGGER_NO_RESPONSE),
    ("ما أحد موجود", TRIGGER_NO_RESPONSE),
)


@dataclass(frozen=True)
class KeywordRecord:
    id: int
    branch_id: int
    phrase: str
    trigger_type: str
    sort_order: int
    source: str = "db"


@dataclass(frozen=True)
class BranchActionConfig:
    branch_id: int
    tenant_id: int
    name: str
    maps_url: str
    location_response_mode: str
    arrival_response_mode: str
    location_instructions_text: str


@dataclass(frozen=True)
class BranchTriggerMatch:
    branch_id: int
    trigger_type: str
    matched_phrase: str
    source: str
    keyword_id: Optional[int] = None


def _keyword_from_row(row: Any) -> KeywordRecord:
    return KeywordRecord(
        id=int(row.id),
        branch_id=int(row.branch_id),
        phrase=str(row.phrase or "").strip(),
        trigger_type=str(row.trigger_type or "").strip(),
        sort_order=int(row.sort_order or 0),
        source="db",
    )


def load_branch_keywords(db: Any, branch_id: int) -> Tuple[KeywordRecord, ...]:
    if db is None or not branch_id:
        return ()
    try:
        from database.models import BranchArrivalKeyword  # noqa: PLC0415

        rows = (
            db.query(BranchArrivalKeyword)
            .filter(
                BranchArrivalKeyword.branch_id == int(branch_id),
                BranchArrivalKeyword.is_active.is_(True),
            )
            .order_by(
                BranchArrivalKeyword.sort_order.asc(),
                BranchArrivalKeyword.id.asc(),
            )
            .all()
        )
        return tuple(_keyword_from_row(r) for r in rows)
    except Exception as exc:  # noqa: silent-ok - keyword query failure degrades to platform defaults
        logger.debug(
            "branch_arrival_keyword_evidence.load_branch_keywords failed branch=%s err=%s",
            branch_id,
            exc,
        )
        return ()


def load_branch_action_config(db: Any, branch_id: int) -> Optional[BranchActionConfig]:
    if db is None or not branch_id:
        return None
    try:
        from database.models import MerchantBranch  # noqa: PLC0415

        row = (
            db.query(MerchantBranch)
            .filter(MerchantBranch.id == int(branch_id))
            .first()
        )
        if row is None:
            return None
        return BranchActionConfig(
            branch_id=int(row.id),
            tenant_id=int(row.tenant_id),
            name=str(row.name or "").strip(),
            maps_url=str(row.maps_url or "").strip(),
            location_response_mode=str(
                getattr(row, "location_response_mode", "") or LOCATION_MODE_ONLY,
            ).strip() or LOCATION_MODE_ONLY,
            arrival_response_mode=str(
                getattr(row, "arrival_response_mode", "") or ARRIVAL_MODE_RECEPTION_ONLY,
            ).strip() or ARRIVAL_MODE_RECEPTION_ONLY,
            location_instructions_text=str(
                getattr(row, "location_instructions_text", "") or "",
            ).strip(),
        )
    except Exception as exc:  # noqa: silent-ok - config load failure degrades to None
        logger.debug(
            "branch_arrival_keyword_evidence.load_branch_action_config failed branch=%s err=%s",
            branch_id,
            exc,
        )
        return None


def _match_phrase_in_message(message: str, phrase: str) -> bool:
    msg_norm = _norm(message or "")
    phrase_norm = _norm(phrase or "")
    if not msg_norm or not phrase_norm:
        return False
    return phrase_norm in msg_norm


def _best_match(
    message: str,
    candidates: Sequence[KeywordRecord],
) -> Optional[KeywordRecord]:
    best: Optional[KeywordRecord] = None
    best_len = 0
    for kw in candidates:
        if kw.trigger_type not in VALID_TRIGGER_TYPES:
            continue
        phrase_norm = _norm(kw.phrase)
        if not phrase_norm:
            continue
        if not _match_phrase_in_message(message, kw.phrase):
            continue
        plen = len(phrase_norm)
        if plen > best_len:
            best = kw
            best_len = plen
        elif plen == best_len and best is not None:
            if kw.sort_order < best.sort_order:
                best = kw
            elif kw.sort_order == best.sort_order and kw.id < best.id:
                best = kw
    return best


def _platform_defaults_for_branch(branch_id: int) -> Tuple[KeywordRecord, ...]:
    out: List[KeywordRecord] = []
    for idx, (phrase, trigger_type) in enumerate(PLATFORM_DEFAULT_KEYWORDS):
        out.append(
            KeywordRecord(
                id=-(idx + 1),
                branch_id=int(branch_id),
                phrase=phrase,
                trigger_type=trigger_type,
                sort_order=idx,
                source="platform_default",
            )
        )
    return tuple(out)


def match_branch_trigger(
    db: Any,
    tenant_id: int,
    message: str = "",
) -> Optional[BranchTriggerMatch]:
    """Match customer message to branch trigger type when structured mode is active."""
    if not structured_branch_contacts_enabled():
        return None
    if not tenant_has_structured_branch_data(db, int(tenant_id or 0)):
        return None

    branch = resolve_branch_for_message(db, int(tenant_id or 0), message or "")
    if branch is None:
        return None

    db_keywords = load_branch_keywords(db, branch.id)
    best = _best_match(message or "", db_keywords)
    if best is None:
        best = _best_match(message or "", _platform_defaults_for_branch(branch.id))
    if best is None:
        return None

    logger.info(
        "[BRANCH_ARRIVAL_KEYWORD] tenant=%s branch_id=%s trigger=%s phrase=%r source=%s",
        tenant_id,
        branch.id,
        best.trigger_type,
        (best.phrase or "")[:48],
        best.source,
    )
    return BranchTriggerMatch(
        branch_id=branch.id,
        trigger_type=best.trigger_type,
        matched_phrase=best.phrase,
        source=best.source,
        keyword_id=best.id if best.id > 0 else None,
    )


def seed_default_keywords_for_branch(db: Any, branch_id: int) -> int:
    """Insert platform default keywords for a branch if none exist."""
    if db is None or not branch_id:
        return 0
    existing = load_branch_keywords(db, branch_id)
    if existing:
        return 0
    try:
        from database.models import BranchArrivalKeyword  # noqa: PLC0415

        now = datetime.now(timezone.utc)
        count = 0
        for idx, (phrase, trigger_type) in enumerate(PLATFORM_DEFAULT_KEYWORDS):
            row = BranchArrivalKeyword(
                branch_id=int(branch_id),
                phrase=phrase,
                trigger_type=trigger_type,
                is_active=True,
                sort_order=idx,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            count += 1
        db.flush()
        return count
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "branch_arrival_keyword_evidence.seed_default_keywords failed branch=%s err=%s",
            branch_id,
            exc,
        )
        return 0


def needs_branch_clarification(
    db: Any,
    tenant_id: int,
    message: str,
    *,
    config: BranchActionConfig,
) -> Optional[Tuple[str, ...]]:
    """Return branch names when ask_branch_first and multiple branches without hint."""
    if config.arrival_response_mode != ARRIVAL_MODE_ASK_BRANCH_FIRST:
        return None
    branches = load_active_branches(db, int(tenant_id or 0))
    if len(branches) <= 1:
        return None
    msg_norm = _norm(message or "")
    for branch in branches:
        for hint in (branch.city, branch.district, branch.name):
            hint_norm = _norm(hint)
            if hint_norm and len(hint_norm) >= 2 and hint_norm in msg_norm:
                return None
    return tuple(b.name for b in branches if b.name)


def preview_trigger_actions(
    db: Any,
    tenant_id: int,
    branch_id: int,
    message: str,
) -> dict:
    """Build preview payload for dashboard — mirrors runtime matching."""
    config = load_branch_action_config(db, branch_id)
    if config is None:
        return {"matched": False, "actions": []}

    db_keywords = load_branch_keywords(db, branch_id)
    best = _best_match(message or "", db_keywords)
    if best is None:
        best = _best_match(message or "", _platform_defaults_for_branch(branch_id))
    if best is None:
        return {"matched": False, "actions": [], "branch_id": branch_id}

    actions: List[dict] = []
    from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
        resolve_reception_for_branch_id,
    )

    reception = resolve_reception_for_branch_id(db, branch_id)

    if best.trigger_type == TRIGGER_LOCATION_REQUEST:
        if config.maps_url:
            actions.append({"type": "maps_cta", "maps_url": config.maps_url})
        if config.location_response_mode == LOCATION_MODE_PLUS_INSTRUCTIONS:
            if config.location_instructions_text:
                actions.append({
                    "type": "text",
                    "body": config.location_instructions_text,
                })
        if config.location_response_mode == LOCATION_MODE_PLUS_RECEPTION and reception:
            actions.append({
                "type": "reception_vcard",
                "display_name": reception.display_name,
                "phone_e164": reception.phone_e164,
            })
    elif best.trigger_type == TRIGGER_ARRIVAL_SOFT:
        actions.append({"type": "text", "body": "أهلاً بك، في انتظارك 🌷"})
        if (
            config.arrival_response_mode == ARRIVAL_MODE_LOCATION_AND_RECEPTION
            and config.maps_url
        ):
            actions.append({"type": "maps_cta", "maps_url": config.maps_url})
    elif best.trigger_type == TRIGGER_ARRIVAL_CONFIRMED:
        if reception:
            actions.append({
                "type": "reception_vcard",
                "display_name": reception.display_name,
                "phone_e164": reception.phone_e164,
            })
        else:
            actions.append({"type": "text", "body": "جهة الاستقبال غير مهيّأة"})
    elif best.trigger_type == TRIGGER_NO_RESPONSE:
        actions.append({"type": "escalation_advance", "note": "next_level_in_chain"})

    return {
        "matched": True,
        "branch_id": branch_id,
        "trigger_type": best.trigger_type,
        "matched_phrase": best.phrase,
        "source": best.source,
        "actions": actions,
    }


__all__ = [
    "ARRIVAL_MODE_ASK_BRANCH_FIRST",
    "ARRIVAL_MODE_LOCATION_AND_RECEPTION",
    "ARRIVAL_MODE_RECEPTION_ONLY",
    "BranchActionConfig",
    "BranchTriggerMatch",
    "KeywordRecord",
    "LOCATION_MODE_ONLY",
    "LOCATION_MODE_PLUS_INSTRUCTIONS",
    "LOCATION_MODE_PLUS_RECEPTION",
    "TRIGGER_ARRIVAL_CONFIRMED",
    "TRIGGER_ARRIVAL_SOFT",
    "TRIGGER_LOCATION_REQUEST",
    "TRIGGER_NO_RESPONSE",
    "VALID_TRIGGER_TYPES",
    "load_branch_action_config",
    "load_branch_keywords",
    "match_branch_trigger",
    "needs_branch_clarification",
    "preview_trigger_actions",
    "seed_default_keywords_for_branch",
]
