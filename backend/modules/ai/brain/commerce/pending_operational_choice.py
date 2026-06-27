"""
Pending operational choice — deterministic follow-up for pre-brain questions.

When the system asks an operational disambiguation (e.g. maps vs contact),
persist the pending choice in ``order_prep`` and consume short affirmatives
(نعم / ارسل) on the next turn without LLM compose.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.brain.pending_operational_choice")

PENDING_PICKUP_MAPS_OR_CONTACT = "pickup_maps_or_contact"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_PENDING_CONFIRM_RE = re.compile(
    r"^(?:"
    r"نعم|نعم\s+ارسل|نعم\s+أرسل|"
    r"ايه|أيه|اي|أي|ايوه|أيوه|"
    r"تمام|اوك|اوكي|ok|okay|yes|"
    r"ارسل|أرسل|ارسلي|أرسلي|"
    r"ماشي|زين|حاضر"
    r")$",
    re.UNICODE | re.IGNORECASE,
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


def is_pending_choice_confirmation(message: str) -> bool:
    """True for short yes/send replies that should consume a pending operational choice."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if not norm:
        return False
    if len(norm.split()) > 3:
        return False
    return bool(_PENDING_CONFIRM_RE.match(norm))


def load_pending_operational_context(
    order_prep: Dict[str, Any],
) -> Tuple[str, int]:
    prep = dict(order_prep or {})
    choice = str(prep.get("pending_operational_choice") or "").strip()
    try:
        branch_id = int(prep.get("pending_operational_branch_id") or 0)
    except (TypeError, ValueError):
        branch_id = 0
    return choice, branch_id


def persist_pending_operational_choice(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    choice: str,
    branch_id: int = 0,
) -> bool:
    if not db or not tenant_id or not phone or not choice:
        return False
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        if conv is None:
            return False
        bs = dict(bs or {})
        op = dict(bs.get("order_prep") or {})
        op["pending_operational_choice"] = str(choice)
        op["pending_operational_branch_id"] = int(branch_id or 0)
        bs["order_prep"] = op
        meta = dict(getattr(conv, "extra_metadata", None) or {})
        meta["brain_state"] = bs
        conv.extra_metadata = meta
        flag_modified(conv, "extra_metadata")
        db.add(conv)
        db.flush()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[PENDING_OPERATIONAL] persist failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return False


def clear_pending_operational_choice(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
) -> bool:
    if not db or not tenant_id or not phone:
        return False
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        if conv is None:
            return False
        bs = dict(bs or {})
        op = dict(bs.get("order_prep") or {})
        op.pop("pending_operational_choice", None)
        op.pop("pending_operational_branch_id", None)
        bs["order_prep"] = op
        meta = dict(getattr(conv, "extra_metadata", None) or {})
        meta["brain_state"] = bs
        conv.extra_metadata = meta
        flag_modified(conv, "extra_metadata")
        db.add(conv)
        db.flush()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[PENDING_OPERATIONAL] clear failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return False


def _load_order_prep_from_db(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
) -> Dict[str, Any]:
    if not db or not tenant_id or not customer_phone:
        return {}
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415

        _conv, bs = _load_brain_state(
            db, tenant_id=int(tenant_id), phone=customer_phone,
        )
        return dict((bs or {}).get("order_prep") or {})
    except Exception:  # noqa: BLE001
        return {}


def evaluate_pending_operational_choice_routing(
    db: Any,
    *,
    tenant_id: int,
    message: str,
    customer_phone: str = "",
) -> Optional[Any]:
    """Consume pending operational choice before other branch routing."""
    from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: PLC0415
        BranchTriggerDecision,
    )

    raw = (message or "").strip()
    if not raw or not is_pending_choice_confirmation(raw):
        return None

    order_prep = _load_order_prep_from_db(
        db, tenant_id=int(tenant_id or 0), customer_phone=customer_phone,
    )
    choice, branch_id = load_pending_operational_context(order_prep)
    if choice != PENDING_PICKUP_MAPS_OR_CONTACT or branch_id <= 0:
        return None

    decision = _build_pickup_maps_or_contact_delivery(
        db,
        int(tenant_id or 0),
        branch_id=branch_id,
    )
    if decision is None:
        return None

    clear_pending_operational_choice(
        db,
        tenant_id=int(tenant_id or 0),
        phone=customer_phone or "",
    )
    logger.info(
        "[PENDING_OPERATIONAL] consumed choice=%s branch_id=%s tenant=%s",
        choice,
        branch_id,
        tenant_id,
    )
    return decision


def _build_pickup_maps_or_contact_delivery(
    db: Any,
    tenant_id: int,
    *,
    branch_id: int,
) -> Optional[Any]:
    from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: PLC0415
        BranchTriggerDecision,
        _build_reception_targets,
        _cta_label_for_url,
    )
    from modules.operations.branch_arrival_keyword_evidence import (  # noqa: PLC0415
        TRIGGER_LOCATION_REQUEST,
        load_branch_action_config,
    )

    config = load_branch_action_config(db, int(branch_id))
    if config is None:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            MSG_LOCATION_NOT_CONFIGURED,
        )

        return BranchTriggerDecision(
            trigger_type=TRIGGER_LOCATION_REQUEST,
            matched_phrase="pending_pickup_confirm",
            branch_id=int(branch_id),
            reason="pending_pickup_no_config",
            reply_text=MSG_LOCATION_NOT_CONFIGURED,
        )

    maps_url = str(getattr(config, "maps_url", "") or "").strip()
    reply_parts = []
    if maps_url:
        reply_parts.append("موقعنا 📍")
        instructions = str(getattr(config, "location_instructions_text", "") or "").strip()
        if instructions:
            reply_parts.append(instructions)

    reception_target, reception_reply = _build_reception_targets(
        db, tenant_id, message="",
    )
    deliver_reception = reception_target is not None

    if not maps_url and not deliver_reception:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            MSG_LOCATION_NOT_CONFIGURED,
        )

        return BranchTriggerDecision(
            trigger_type=TRIGGER_LOCATION_REQUEST,
            matched_phrase="pending_pickup_confirm",
            branch_id=int(branch_id),
            reason="pending_pickup_nothing_configured",
            reply_text=MSG_LOCATION_NOT_CONFIGURED,
        )

    return BranchTriggerDecision(
        trigger_type=TRIGGER_LOCATION_REQUEST,
        matched_phrase="pending_pickup_confirm",
        branch_id=int(branch_id),
        reason="pending_pickup_confirmed",
        reply_text="\n".join(reply_parts) if reply_parts else (reception_reply or "موقعنا 📍"),
        maps_url=maps_url,
        cta_button_label=_cta_label_for_url(maps_url) if maps_url else "",
        use_cta=bool(maps_url),
        deliver_reception_after_maps=deliver_reception,
        reception_call_target=reception_target,
        reception_reply_text=reception_reply or "",
        persist_contact=deliver_reception,
    )


__all__ = [
    "PENDING_PICKUP_MAPS_OR_CONTACT",
    "clear_pending_operational_choice",
    "evaluate_pending_operational_choice_routing",
    "is_pending_choice_confirmation",
    "load_pending_operational_context",
    "persist_pending_operational_choice",
]
