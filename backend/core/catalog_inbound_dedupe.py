"""
core/catalog_inbound_dedupe.py
──────────────────────────────
Dedupe helpers for WhatsApp catalog_order inbound rows and timelines.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from modules.ai.media.normalizer import CATALOG_FRAME_MARKER


def inbound_wa_message_id(meta: Optional[Dict[str, Any]]) -> str:
    """Resolve the external WhatsApp message id from MessageEvent metadata."""
    data = dict(meta or {})
    wid = str(data.get("wa_message_id") or "").strip()
    if wid:
        return wid
    norm = data.get("normalized_inbound") or {}
    if isinstance(norm, dict):
        return str(norm.get("wa_message_id") or "").strip()
    return ""


def is_catalog_order_body(body: str) -> bool:
    return bool(body and CATALOG_FRAME_MARKER in body)


def find_duplicate_inbound_by_wa_id(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: int,
    wa_message_id: str,
    scan_limit: int = 30,
) -> Any:
    """Return an existing inbound MessageEvent for the same WhatsApp wamid."""
    wid = str(wa_message_id or "").strip()
    if not wid or conversation_id is None:
        return None
    try:
        from models import MessageEvent  # noqa: PLC0415

        rows = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.conversation_id == conversation_id,
                MessageEvent.direction.in_(("inbound", "in")),
            )
            .order_by(MessageEvent.id.desc())
            .limit(max(1, scan_limit))
            .all()
        )
        for row in rows:
            if inbound_wa_message_id(row.extra_metadata) == wid:
                return row
    except Exception:  # noqa: silent-ok - best-effort dedupe lookup must not break save path
        return None
    return None


def dedupe_timeline_by_wa_message_id(
    messages: List[Dict[str, Any]],
    *,
    me_rows: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    """Collapse duplicate inbound timeline rows that share a WhatsApp wamid."""
    if not messages:
        return messages

    row_meta: Dict[str, Dict[str, Any]] = {}
    if me_rows:
        for row in me_rows:
            row_meta[str(getattr(row, "id", ""))] = dict(getattr(row, "extra_metadata", None) or {})

    seen: Set[str] = set()
    seen_catalog_bodies: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("direction") != "in":
            out.append(msg)
            continue
        body = str(msg.get("body") or "")
        meta = row_meta.get(str(msg.get("id") or ""), {})
        wid = inbound_wa_message_id(meta)
        catalog_body_key = ""
        if is_catalog_order_body(body):
            catalog_body_key = f"catalog-body:{hash(body.strip())}"
            if catalog_body_key in seen_catalog_bodies:
                continue
        if wid:
            key = f"wamid:{wid}"
            if key in seen:
                continue
            seen.add(key)
            if catalog_body_key:
                seen_catalog_bodies.add(catalog_body_key)
            out.append(msg)
            continue
        if catalog_body_key:
            seen_catalog_bodies.add(catalog_body_key)
            out.append(msg)
            continue
        out.append(msg)
    return out


__all__ = [
    "dedupe_timeline_by_wa_message_id",
    "find_duplicate_inbound_by_wa_id",
    "inbound_wa_message_id",
    "is_catalog_order_body",
]
