"""Audit actual provider attempts against one explicitly bound outbound row.

No customer prose or transport decision is changed here. Unbound callers and
child tasks are excluded; a missing binding never falls back to a recent row.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("nahla.outbound_wire_audit")


def _task() -> Any:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _phone(value: Any) -> str:
    return "".join(c for c in str(value or "") if c.isdigit())


@dataclass
class WireAudit:
    db: Any
    row_id: int | None
    tenant_id: int
    recipient: str
    source_body: str
    metadata: dict
    task: Any
    pending: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    previous_body: str = ""
    active: bool = True


_current: ContextVar[WireAudit | None] = ContextVar("outbound_wire_audit", default=None)


def bind_wire_audit(db: Any, row_id: Any, tenant_id: int, recipient: str,
                    body: str, metadata: dict) -> Token:
    bound_id = row_id if type(row_id) is int and row_id > 0 else None
    if bound_id is None:
        logger.warning("[OUTBOUND_WIRE_AUDIT] unavailable: no persisted row tenant=%s", tenant_id)
    ctx = WireAudit(db, bound_id, tenant_id, _phone(recipient), body,
                    dict(metadata), _task(), previous_body=body)
    return _current.set(ctx)


def refresh_wire_audit(tenant_id: Any, recipient: str, body: str, metadata: dict) -> None:
    ctx = current_wire_audit(tenant_id, recipient)
    if ctx is not None:
        ctx.source_body = body
        ctx.previous_body = body
        ctx.metadata = dict(metadata)
        ctx.pending = []


def reset_wire_audit(token: Token) -> None:
    ctx = _current.get()
    if ctx is not None:
        ctx.active = False
    _current.reset(token)


def current_wire_audit(tenant_id: Any, recipient: str) -> WireAudit | None:
    ctx = _current.get()
    if (ctx is None or not ctx.active or ctx.task is not _task()
            or ctx.tenant_id != tenant_id or ctx.recipient != _phone(recipient)):
        return None
    return ctx


def wire_row_id(tenant_id: Any, recipient: str) -> int | None:
    ctx = current_wire_audit(tenant_id, recipient)
    return ctx.row_id if ctx else None


def payload_text_fields(payload: dict) -> dict[str, str]:
    fields: dict[str, str] = {}
    for kind in ("text", "image", "video", "document", "audio"):
        item = payload.get(kind)
        if isinstance(item, dict):
            for key in ("body", "caption"):
                if isinstance(item.get(key), str):
                    fields[f"{kind}.{key}"] = item[key]
    interactive = payload.get("interactive")
    if isinstance(interactive, dict):
        for kind in ("header", "body", "footer"):
            item = interactive.get(kind)
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                fields[f"interactive.{kind}.text"] = item["text"]
    return fields


def _body(payload: dict) -> str:
    fields = payload_text_fields(payload)
    for key in ("text.body", "interactive.body.text", "image.caption", "video.caption", "document.caption"):
        if key in fields:
            return fields[key]
    return ""


def observe_wire_payload(tenant_id: Any, payload: dict, layer: str) -> None:
    ctx = current_wire_audit(tenant_id, payload.get("to", ""))
    if ctx is None:
        return
    body = _body(payload)
    if body != ctx.previous_body:
        ctx.pending.append(layer)
    ctx.previous_body = body


def set_wire_expression(tenant_id: Any, recipient: str, body: str, owner: str,
                        *, source: str, model_metadata: dict | None = None) -> None:
    """Record a later author explicitly, without attributing its prose to Brain."""
    ctx = current_wire_audit(tenant_id, recipient)
    if ctx is None:
        return
    ctx.metadata.update({
        "final_expression_owner": owner,
        "final_customer_text_source": source,
        "compose_source": None,
        "actual_model": None,
        "requested_model": None,
        "attempted_model": None,
        "model_identity_source": "not_llm" if source == "deterministic" else "unknown",
    })
    ctx.metadata.update(model_metadata or {})
    if body != ctx.source_body:
        ctx.metadata["final_text_transformed"] = True
        ctx.metadata["final_transform_reasons"] = list(dict.fromkeys(
            [*(ctx.metadata.get("final_transform_reasons") or []), owner]
        ))
    ctx.source_body = body
    ctx.previous_body = body
    ctx.pending = []


def record_wire_attempt(*, tenant_id: Any, payload: dict, operation: str,
                        classification: str, wamid: str | None = None) -> None:
    """Persist after the HTTP result; addresses exactly the bound row even if sent."""
    ctx = current_wire_audit(tenant_id, payload.get("to", ""))
    if ctx is None or ctx.row_id is None:
        return
    try:
        observe_wire_payload(tenant_id, payload, "provider_wire_payload")
        meta = dict(ctx.metadata)
        reasons = list(dict.fromkeys([*(meta.get("final_transform_reasons") or []), *ctx.pending]))
        if ctx.pending:
            meta["final_expression_owner"] = ctx.pending[-1]
            if meta.get("final_customer_text_source") in ("llm", "persona_llm"):
                meta["final_customer_text_source"] = "llm_postprocess"
        meta["final_transform_reasons"] = reasons
        meta["final_text_transformed"] = bool(meta.get("final_text_transformed") or ctx.pending)
        body = _body(payload)
        from core.outbound_final_boundary import outbound_body_kind

        meta["final_wire_body_kind"] = outbound_body_kind(body)
        meta["final_wire_structured_delivery"] = payload.get("type") != "text"
        attempt = {
            "sequence": len(ctx.attempts) + 1,
            "operation": operation,
            "classification": classification,
            "wamid": wamid,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "payload_type": payload.get("type"),
            "text_fields": payload_text_fields(payload),
            "payload_sha256": hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "provenance": meta,
        }
        ctx.attempts.append(attempt)
        from models import MessageEvent
        from sqlalchemy.orm.attributes import flag_modified

        # No suffix/status/time-window lookup: concurrent turns cannot be selected.
        with ctx.db.begin_nested():
            row = ctx.db.query(MessageEvent).filter(
                MessageEvent.id == ctx.row_id,
                MessageEvent.tenant_id == ctx.tenant_id,
                MessageEvent.direction.in_(("outbound", "out")),
            ).first()
            if row is None:
                logger.warning("[OUTBOUND_WIRE_AUDIT] bound row missing tenant=%s row=%s", tenant_id, ctx.row_id)
                return
            extra = dict(row.extra_metadata or {})
            extra["wire_attempts"] = list(ctx.attempts)
            extra.update(meta)
            row.extra_metadata = extra
            # The attempt list preserves retries, split sends, captions and failures.
            # For text/interactive replies, keep the transcript aligned with wire text.
            if payload.get("type") in ("text", "interactive"):
                row.body = body
            flag_modified(row, "extra_metadata")
            ctx.db.add(row)
            ctx.db.flush()
        ctx.db.commit()
    except Exception:
        logger.exception("[OUTBOUND_WIRE_AUDIT] persistence failed tenant=%s row=%s", tenant_id, ctx.row_id)
        try:
            ctx.db.rollback()
        except Exception:
            logger.exception("[OUTBOUND_WIRE_AUDIT] rollback failed tenant=%s row=%s", tenant_id, ctx.row_id)
    finally:
        ctx.pending = []
        ctx.previous_body = ctx.source_body
