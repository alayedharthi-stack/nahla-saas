"""
Order-update WhatsApp template inventory, clustering, and merchant-visible hygiene.

Keeps one official active APPROVED template per (service_key, language) for
lifecycle sends. Archives Nahla-managed duplicates without deleting rows or
touching Meta-approved templates on the provider.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.commerce_lifecycle.order_updates import (
    ORDER_UPDATE_SERVICE_KEYS,
    is_order_update_service_key,
    promote_approved_revision,
)

VAR_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")

ARCHIVE_SUPERSEDED = "superseded"
ARCHIVE_DUPLICATE = "duplicate"
ARCHIVE_EXPERIMENTAL = "experimental"
ARCHIVE_MISBOUND = "misbound"


def _meta_dict(tpl: Any) -> Dict[str, Any]:
    raw = getattr(tpl, "ai_generation_metadata", None) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _set_meta(tpl: Any, **updates: Any) -> None:
    meta = _meta_dict(tpl)
    meta.update(updates)
    tpl.ai_generation_metadata = meta
    flag_modified(tpl, "ai_generation_metadata")


def archive_state(tpl: Any) -> Optional[str]:
    return _meta_dict(tpl).get("archive_state")


def superseded_by_template_id(tpl: Any) -> Optional[int]:
    raw = _meta_dict(tpl).get("superseded_by_template_id")
    return int(raw) if raw is not None else None


def body_text(components: Any) -> str:
    for comp in components or []:
        if str((comp or {}).get("type", "")).upper() == "BODY":
            return str((comp or {}).get("text") or "")
    return ""


def button_contract(components: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for comp in components or []:
        if str((comp or {}).get("type", "")).upper() != "BUTTONS":
            continue
        for btn in comp.get("buttons") or []:
            url = str(btn.get("url") or "")
            out.append(
                {
                    "type": str(btn.get("type") or "").upper(),
                    "text": btn.get("text"),
                    "url": url or None,
                    "param_count": len(VAR_RE.findall(url)),
                }
            )
    return out


def param_signature(components: Any) -> Dict[str, Any]:
    text = body_text(components)
    return {
        "body_param_count": len(VAR_RE.findall(text)),
        "body_placeholders": [int(x) for x in VAR_RE.findall(text)],
        "buttons": button_contract(components),
    }


def has_example_com(components: Any) -> bool:
    return "example.com" in json.dumps(components or [], ensure_ascii=False)


def is_nahla_managed_template(tpl: Any) -> bool:
    """Platform-managed lifecycle clone — safe to archive when superseded."""
    source = str(getattr(tpl, "source", "") or "").strip().lower()
    name = str(getattr(tpl, "name", "") or "").strip().lower()
    service_key = str(getattr(tpl, "service_key", "") or "").strip()
    if source == "merchant" and not name.startswith("nahla_"):
        return False
    if service_key and is_order_update_service_key(service_key):
        return True
    if name.startswith("nahla_"):
        return True
    return source in {"nahla_library", ""}


def inventory_row(tpl: Any, *, superseded_by: Optional[Dict[int, int]] = None) -> Dict[str, Any]:
    components = getattr(tpl, "components", None) or []
    tpl_id = int(tpl.id)
    supersedes_id = getattr(tpl, "supersedes_template_id", None)
    replaced_by = superseded_by.get(tpl_id) if superseded_by else None
    if replaced_by is None:
        replaced_by = superseded_by_template_id(tpl)
    return {
        "db_id": tpl_id,
        "meta_name": tpl.name,
        "language": tpl.language or "ar",
        "service_key": tpl.service_key,
        "nahla_source_key": getattr(tpl, "nahla_source_key", None),
        "meta_status": tpl.status,
        "is_active": bool(tpl.is_active),
        "is_hidden": bool(tpl.is_hidden),
        "revision": int(getattr(tpl, "revision", 1) or 1),
        "usage_count": int(getattr(tpl, "usage_count", 0) or 0),
        "supersedes_template_id": int(supersedes_id) if supersedes_id else None,
        "superseded_by_template_id": int(replaced_by) if replaced_by else None,
        "archive_state": archive_state(tpl),
        "meta_template_id": tpl.meta_template_id,
        "source": getattr(tpl, "source", None),
        "param_signature": param_signature(components),
        "has_example_com": has_example_com(components),
        "nahla_managed": is_nahla_managed_template(tpl),
    }


def build_order_update_inventory(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    from models import WhatsAppTemplate  # noqa: PLC0415

    rows = (
        db.query(WhatsAppTemplate)
        .filter(WhatsAppTemplate.tenant_id == int(tenant_id))
        .order_by(WhatsAppTemplate.id.asc())
        .all()
    )
    lifecycle = [
        r
        for r in rows
        if is_order_update_service_key(getattr(r, "service_key", None))
        or str(getattr(r, "name", "") or "").startswith("nahla_order_")
        or str(getattr(r, "nahla_source_key", "") or "") in ORDER_UPDATE_SERVICE_KEYS
    ]
    superseded_by: Dict[int, int] = {}
    for row in lifecycle:
        supersedes_id = getattr(row, "supersedes_template_id", None)
        if supersedes_id:
            superseded_by[int(supersedes_id)] = int(row.id)
    return [inventory_row(r, superseded_by=superseded_by) for r in lifecycle]


def cluster_inventory(inventory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[Tuple[str, str, str], List[int]] = defaultdict(list)
    for item in inventory:
        key = (
            str(item.get("service_key") or "?"),
            str(item.get("language") or "ar"),
            json.dumps(item.get("param_signature") or {}, ensure_ascii=False, sort_keys=True),
        )
        buckets[key].append(int(item["db_id"]))
    return [
        {
            "service_key": key[0],
            "language": key[1],
            "param_signature": json.loads(key[2]),
            "template_ids": ids,
            "count": len(ids),
        }
        for key, ids in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[1][0]))
    ]


def _score_official_candidate(item: Dict[str, Any]) -> Tuple[int, int, int]:
    """Higher is better."""
    score = 0
    if str(item.get("meta_status") or "").upper() == "APPROVED":
        score += 100
    if item.get("is_active"):
        score += 50
    if not item.get("has_example_com"):
        score += 40
    if not item.get("is_hidden"):
        score += 10
    revision = int(item.get("revision") or 1)
    usage = int(item.get("usage_count") or 0)
    return score, revision, usage


def choose_official_template_ids(
    inventory: List[Dict[str, Any]],
    *,
    preferred_ids: Optional[Dict[str, int]] = None,
) -> Dict[Tuple[str, str], int]:
    """
    Pick one official template per (service_key, language).

    ``preferred_ids`` maps service_key → db_id when an operator has already
    designated the successor (e.g. revision pending Meta approval).
    """
    preferred_ids = preferred_ids or {}
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for item in inventory:
        service_key = str(item.get("service_key") or "").strip()
        if not is_order_update_service_key(service_key):
            continue
        grouped[(service_key, str(item.get("language") or "ar"))].append(item)

    official: Dict[Tuple[str, str], int] = {}
    for slot, items in grouped.items():
        service_key, _lang = slot
        preferred = preferred_ids.get(service_key)
        if preferred is not None:
            match = next((i for i in items if int(i["db_id"]) == int(preferred)), None)
            if match and str(match.get("meta_status") or "").upper() == "APPROVED":
                official[slot] = int(preferred)
                continue
            if match and str(match.get("meta_status") or "").upper() == "PENDING":
                # Wait for approval — do not switch slot yet.
                active = next((i for i in items if i.get("is_active")), None)
                if active:
                    official[slot] = int(active["db_id"])
                continue
        ranked = sorted(items, key=_score_official_candidate, reverse=True)
        if ranked:
            official[slot] = int(ranked[0]["db_id"])
    return official


def mark_template_archived(
    tpl: Any,
    *,
    archive_state: str,
    superseded_by_template_id: Optional[int] = None,
    hide: bool = True,
) -> None:
    tpl.is_active = False
    if hide:
        tpl.is_hidden = True
    _set_meta(
        tpl,
        archive_state=archive_state,
        archived_at=datetime.now(timezone.utc).isoformat(),
        superseded_by_template_id=superseded_by_template_id,
    )
    tpl.updated_at = datetime.now(timezone.utc)


def apply_order_update_template_hygiene(
    db: Session,
    tenant_id: int,
    *,
    preferred_official_ids: Optional[Dict[str, int]] = None,
    promote_template_id: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Archive Nahla-managed duplicates and optionally promote an approved revision.

    Never deletes rows or calls Meta delete APIs.
    """
    from models import WhatsAppTemplate  # noqa: PLC0415

    inventory = build_order_update_inventory(db, tenant_id)
    official = choose_official_template_ids(inventory, preferred_ids=preferred_official_ids)

    actions: List[Dict[str, Any]] = []
    visible: List[int] = []
    hidden: List[int] = []
    gaps: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    if promote_template_id is not None:
        candidate = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.id == int(promote_template_id),
                WhatsAppTemplate.tenant_id == int(tenant_id),
            )
            .first()
        )
        if candidate is None:
            actions.append({"action": "promote_skipped", "reason": "template_not_found"})
        elif str(candidate.status or "").upper() != "APPROVED":
            actions.append(
                {
                    "action": "promote_skipped",
                    "reason": "not_approved",
                    "template_id": int(candidate.id),
                    "status": candidate.status,
                }
            )
        elif not dry_run:
            ok = promote_approved_revision(
                db,
                tenant_id=int(tenant_id),
                template_id=int(candidate.id),
                commit=False,
            )
            actions.append({"action": "promoted", "template_id": int(candidate.id), "ok": ok})
            inventory = build_order_update_inventory(db, tenant_id)
            official = choose_official_template_ids(inventory, preferred_ids=preferred_official_ids)

    by_id = {int(i["db_id"]): i for i in inventory}
    official_ids = set(official.values())

    for slot, official_id in official.items():
        service_key, language = slot
        item = by_id.get(official_id)
        if item and item.get("has_example_com"):
            conflicts.append(
                {
                    "service_key": service_key,
                    "language": language,
                    "official_template_id": official_id,
                    "issue": "active_official_has_example_com_url",
                }
            )
        visible.append(official_id)

    for item in inventory:
        tpl_id = int(item["db_id"])
        service_key = str(item.get("service_key") or "")
        if not is_order_update_service_key(service_key):
            if item.get("nahla_managed"):
                actions.append(
                    {
                        "action": "archive_misbound",
                        "template_id": tpl_id,
                        "service_key": service_key,
                    }
                )
                if not dry_run:
                    tpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == tpl_id).first()
                    if tpl:
                        mark_template_archived(tpl, archive_state=ARCHIVE_MISBOUND, hide=True)
                        hidden.append(tpl_id)
            continue

        slot = (service_key, str(item.get("language") or "ar"))
        official_id = official.get(slot)
        if official_id is None:
            gaps.append({"service_key": service_key, "language": slot[1], "issue": "no_official_template"})
            continue
        if tpl_id == official_id:
            continue
        if not item.get("nahla_managed"):
            continue
        if str(item.get("meta_status") or "").upper() in {"PENDING", "DRAFT"}:
            continue

        archive_reason = ARCHIVE_DUPLICATE
        if official_id and tpl_id != official_id:
            archive_reason = ARCHIVE_SUPERSEDED if int(item.get("revision") or 1) < int(
                by_id.get(official_id, {}).get("revision") or 1
            ) else ARCHIVE_DUPLICATE

        actions.append(
            {
                "action": "archive",
                "template_id": tpl_id,
                "archive_state": archive_reason,
                "superseded_by_template_id": official_id,
            }
        )
        hidden.append(tpl_id)
        if not dry_run:
            tpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == tpl_id).first()
            if tpl:
                mark_template_archived(
                    tpl,
                    archive_state=archive_reason,
                    superseded_by_template_id=official_id,
                    hide=True,
                )

    for service_key in ORDER_UPDATE_SERVICE_KEYS:
        for language in sorted({str(i.get("language") or "ar") for i in inventory} or {"ar"}):
            slot = (service_key, language)
            if slot not in official:
                gaps.append({"service_key": service_key, "language": language, "issue": "no_template_row"})

    active_example = [
        i
        for i in inventory
        if i.get("is_active") and i.get("has_example_com") and is_order_update_service_key(str(i.get("service_key") or ""))
    ]
    if active_example:
        conflicts.append(
            {
                "issue": "active_templates_with_example_com",
                "template_ids": [int(x["db_id"]) for x in active_example],
            }
        )

    if not dry_run:
        db.flush()

    return {
        "tenant_id": int(tenant_id),
        "dry_run": dry_run,
        "inventory": inventory,
        "clusters": cluster_inventory(inventory),
        "official_by_slot": {
            f"{service_key}:{language}": template_id for (service_key, language), template_id in official.items()
        },
        "visible_template_ids": sorted(set(visible)),
        "hidden_template_ids": sorted(set(hidden)),
        "gaps": gaps,
        "conflicts": conflicts,
        "actions": actions,
        "ledger_impact": {
            "note": "Historical CommerceLifecycleNotificationLedger rows are unchanged; template_id is not rewritten.",
        },
    }


__all__ = [
    "ARCHIVE_DUPLICATE",
    "ARCHIVE_EXPERIMENTAL",
    "ARCHIVE_MISBOUND",
    "ARCHIVE_SUPERSEDED",
    "apply_order_update_template_hygiene",
    "archive_state",
    "build_order_update_inventory",
    "choose_official_template_ids",
    "cluster_inventory",
    "has_example_com",
    "inventory_row",
    "is_nahla_managed_template",
    "mark_template_archived",
    "param_signature",
    "superseded_by_template_id",
]
