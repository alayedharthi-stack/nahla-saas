"""
constrained_operational_compose.py
────────────────────────────────────
Resolve customer-facing copy for pre-Brain operational decisions via
constrained LLM compose, with legacy fixed copy as fail-closed fallback.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from core.reply_instruction import (
    ReplyInstruction,
    is_operational_constrained_compose_enabled,
    stamp_reply_metadata,
)
from modules.ai.brain.compose.operational_expression import (
    compose_operational_expression_goal,
)
from modules.ai.brain.postprocess.operational_reply_validator import (
    validate_operational_reply,
)

logger = logging.getLogger("nahla.constrained_operational_compose")

_COMPOSE_TIMEOUT_S = 20


def _load_recent_history(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    try:
        from core.conversation_engine import StateManager  # noqa: PLC0415

        rows = StateManager.load_history(
            db, phone=phone, limit=limit, tenant_id=tenant_id,
        ) or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            body = str(row.get("body") or "").strip()
            if not body:
                continue
            direction = str(row.get("direction") or "inbound")
            role = "assistant" if direction in ("out", "outbound") else "user"
            out.append({"role": role, "content": body})
        return out[-limit:]
    except Exception:
        return []


def _load_store_name(db: Any, tenant_id: int) -> str:
    try:
        from database.models import StoreKnowledgeSnapshot  # noqa: PLC0415
        from core.store_display import clean_store_name  # noqa: PLC0415

        snap = (
            db.query(StoreKnowledgeSnapshot)
            .filter(StoreKnowledgeSnapshot.tenant_id == tenant_id)
            .first()
        )
        if snap and snap.store_profile:
            return clean_store_name(str(snap.store_profile.get("name") or ""))
    except Exception:  # noqa: silent-ok — store name is optional compose context
        pass
    return ""


async def compose_constrained_operational_reply(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    instruction: ReplyInstruction,
    inbound_text: str = "",
    history: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Return (reply_text, metadata). Falls back to legacy_copy on any failure."""
    legacy = (instruction.legacy_copy or "").strip()
    base_meta = stamp_reply_metadata(
        instruction,
        copy_source="legacy_fixed",
        expression_owner="legacy_fixed",
    )

    if not is_operational_constrained_compose_enabled():
        return legacy, base_meta

    if not legacy:
        return legacy, base_meta

    goal = compose_operational_expression_goal(instruction)
    message = (inbound_text or instruction.inbound_text or "").strip()
    if not message:
        message = "[media attachment]"

    system_prompt = (
        "You are Nahla, a warm Saudi Arabic WhatsApp store assistant.\n"
        "Reply ONLY in natural Saudi Arabic.\n\n"
        f"## Response goal\n{goal}\n"
    )

    history_rows = history if history is not None else _load_recent_history(
        db, tenant_id=tenant_id, phone=phone,
    )
    store_name = _load_store_name(db, tenant_id) if db is not None else ""

    try:
        from modules.ai.orchestrator.adapter import generate_ai_reply  # noqa: PLC0415

        payload = await asyncio.wait_for(
            asyncio.to_thread(
                generate_ai_reply,
                tenant_id=tenant_id,
                customer_phone=phone,
                message=message,
                store_name=store_name,
                channel="whatsapp",
                locale="ar",
                history=history_rows,
                context_metadata={
                    "operational_compose": True,
                    "reply_instruction_path": instruction.path,
                    "reply_instruction_kind": instruction.decision_kind,
                    "operational_facts": dict(instruction.facts or {}),
                },
                prompt_overrides={"__full_system_prompt": system_prompt},
                provider_hint="anthropic",
            ),
            timeout=_COMPOSE_TIMEOUT_S,
        )
        candidate = (payload.reply_text or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CONSTRAINED_COMPOSE] llm_failed path=%s tenant=%s phone=*%s err=%s",
            instruction.path,
            tenant_id,
            (phone or "")[-4:],
            exc,
        )
        return legacy, base_meta

    validation = validate_operational_reply(candidate, instruction)
    if not validation.ok:
        logger.info(
            "[CONSTRAINED_COMPOSE] validation_failed path=%s tenant=%s "
            "reason=%s preview=%r",
            instruction.path,
            tenant_id,
            validation.reason,
            candidate[:80],
        )
        return legacy, {
            **base_meta,
            "constrained_compose_failed": validation.reason,
        }

    meta = stamp_reply_metadata(
        instruction,
        copy_source="constrained_compose",
        expression_owner="constrained_compose",
    )
    meta["brain_called"] = True
    meta["reply_source"] = "constrained_compose"
    logger.info(
        "[CONSTRAINED_COMPOSE] ok path=%s tenant=%s phone=*%s len=%d",
        instruction.path,
        tenant_id,
        (phone or "")[-4:],
        len(candidate),
    )
    return candidate, meta


async def resolve_prebrain_reply_text(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    decision: Dict[str, Any],
    inbound_text: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """Resolve reply for a pre-Brain short-circuit decision dict."""
    legacy = str(decision.get("reply_text") or "").strip()
    raw_instr = decision.get("reply_instruction")
    instruction = ReplyInstruction.from_dict(raw_instr)
    if instruction is None:
        instruction = ReplyInstruction(
            path=str(decision.get("deterministic_path") or "pre_brain"),
            decision_kind="unknown",
            legacy_copy=legacy,
            inbound_text=inbound_text,
        )
    elif not instruction.legacy_copy:
        instruction = ReplyInstruction(
            path=instruction.path,
            decision_kind=instruction.decision_kind,
            facts=instruction.facts,
            constraints=instruction.constraints,
            forbidden_claims=instruction.forbidden_claims,
            legacy_copy=legacy,
            decision_owner=instruction.decision_owner,
            expression_owner=instruction.expression_owner,
            inbound_text=inbound_text or instruction.inbound_text,
        )
    return await compose_constrained_operational_reply(
        db=db,
        tenant_id=tenant_id,
        phone=phone,
        instruction=instruction,
        inbound_text=inbound_text,
    )


__all__ = [
    "compose_constrained_operational_reply",
    "resolve_prebrain_reply_text",
]
