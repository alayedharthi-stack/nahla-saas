"""
staff_contact_target_continuity.py
──────────────────────────────────
Cross-turn staff/contact reference resolution (PR-C2).

When a configured staff member or role was mentioned recently, pronoun
follow-ups («ارسل رقمه», «وينه») resolve to that target instead of
general_channel — platform-wide, evidence-backed, no hardcoded names.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.staff_contact_target_continuity")

PENDING_CONTACT_TARGET_KEY = "pending_contact_target"
DEFAULT_EXPIRES_AFTER_TURNS = 3

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_INVALID_TARGET_NAMES = frozenset({
    "احد", "أحد", "حد", "موظف", "الموظف", "شخص", "بشر",
    "المعرض", "معرض", "الفرع", "فرع", "متجر", "المتجر",
    "خدمة العملاء", "خدمه العملاء", "التواصل", "رقم", "الرقم",
})

_PRONOUN_CONTACT_FOLLOWUP_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي)\s*(?:لي\s+)?(?:رقم(?:ه|ها|هم)?|جوال(?:ه|ها)?|هاتف(?:ه|ها)?|تواصل(?:ه|ها)?|بيانات(?:ه|ها)?|(?:ه|ها|هم)(?:\s|$|[؟?]))"
    r"|(?:^|\s)(?:رقم(?:ه|ها|هم)|جوال(?:ه|ها)|هاتف(?:ه|ها))(?:\s|$|[؟?])"
    r"|(?:^|\s)(?:وينه|وينها|فينه|فينها)(?:\s|[؟?]|$)"
    r"|(?:^|\s)(?:وين|فين|اين|أين)\s*(?:هو|هي|ه)\s*(?:\s|[؟?]|$)"
    r"|(?:^|\s)(?:ابي|أبي|ابغى|أبغى|بدي|اريد|أريد)\s*(?:اكلم|أكلم|اتصل|أتصل|اتواصل|أتواصل|كلم|كلمه)(?:ه|ها|هم)?"
    r"|(?:^|\s)(?:اكلم|أكلم|اتصل|أتصل|اتواصل|أتواصل|كلم|كلمه)(?:ه|ها|هم)?(?:\s|$|[؟?])"
    r"|(?:^|\s)(?:كيف\s*(?:أ|ا)?تواصل|كيف\s*(?:أ|a)?كلم)\s*(?:مع(?:ه|ها|هم))?"
    r"|(?:^|\s)(?:وين|فين|اين|أين)\s*رقم(?:ه|ها|هم)?"
    r")",
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


@dataclass(frozen=True)
class PendingContactTarget:
    lookup_name: str
    display_name: str
    role: str = ""
    source: str = ""
    confidence: float = 0.0
    created_turn: int = 0
    expires_after_turns: int = DEFAULT_EXPIRES_AFTER_TURNS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lookup_name": self.lookup_name,
            "display_name": self.display_name,
            "role": self.role,
            "source": self.source,
            "confidence": float(self.confidence or 0.0),
            "created_turn": int(self.created_turn or 0),
            "expires_after_turns": int(self.expires_after_turns or DEFAULT_EXPIRES_AFTER_TURNS),
        }

    @staticmethod
    def from_dict(raw: Any) -> Optional["PendingContactTarget"]:
        if not isinstance(raw, dict):
            return None
        lookup = str(raw.get("lookup_name") or "").strip()
        if not lookup:
            return None
        display = str(raw.get("display_name") or lookup).strip() or lookup
        return PendingContactTarget(
            lookup_name=lookup,
            display_name=display,
            role=str(raw.get("role") or "").strip(),
            source=str(raw.get("source") or "").strip(),
            confidence=float(raw.get("confidence") or 0.0),
            created_turn=int(raw.get("created_turn") or 0),
            expires_after_turns=int(
                raw.get("expires_after_turns") or DEFAULT_EXPIRES_AFTER_TURNS
            ),
        )


def is_pronoun_staff_contact_followup(message: str) -> bool:
    """True for «ارسل رقمه» / «وينه» — not store-wide «ارسل الأرقام»."""
    raw = (message or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            extract_staff_name_candidate,
        )

        if extract_staff_name_candidate(raw):
            return False
    except Exception:  # noqa: BLE001
        logger.exception(
            "[STAFF_CONTACT_CONTINUITY] staff_name_candidate_check_failed",
        )
    norm = _norm(raw)
    if not _PRONOUN_CONTACT_FOLLOWUP_RE.search(norm):
        return False
    if re.search(
        r"(?:^|\s)(?:ال)?(?:ارقام|أرقام|ارقم)(?:كم|ك|ه|ها|هم)?(?:\s|$)",
        norm,
        flags=re.UNICODE,
    ):
        return False
    return True


is_contact_target_followup = is_pronoun_staff_contact_followup


def should_clear_pending_on_topic_switch(message: str) -> bool:
    """Clear pending target when the customer switches to product/catalog flow."""
    raw = (message or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
            has_explicit_product_commerce_intent,
        )

        if has_explicit_product_commerce_intent(raw):
            return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "[STAFF_CONTACT_CONTINUITY] product_intent_clear_check_failed",
        )
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_commerce_or_product_flow_message,
        )

        if is_commerce_or_product_flow_message(raw):
            return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "[STAFF_CONTACT_CONTINUITY] commerce_flow_clear_check_failed",
        )
    return False


def is_stale_pending_target(
    target: PendingContactTarget,
    *,
    current_turn: int,
) -> bool:
    if not target or not target.lookup_name:
        return True
    created = int(target.created_turn or 0)
    ttl = int(target.expires_after_turns or DEFAULT_EXPIRES_AFTER_TURNS)
    if created <= 0:
        return False
    return int(current_turn or 0) - created > ttl


def _is_valid_target_name(name: str) -> bool:
    norm = _norm(name or "")
    if not norm or len(norm) < 2:
        return False
    if norm in _INVALID_TARGET_NAMES:
        return False
    if norm in {"رقمه", "رقمها", "رقمهم", "جواله", "هاتفه"}:
        return False
    return True


def pending_target_from_record(
    record: Any,
    *,
    source: str = "",
    confidence: float = 0.95,
    created_turn: int = 0,
) -> Optional[PendingContactTarget]:
    lookup = str(getattr(record, "lookup_name", "") or "").strip()
    if not lookup or not _is_valid_target_name(lookup):
        return None
    role = str(getattr(record, "role", "") or "").strip()
    try:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            resolve_contact_display_name,
        )

        display = resolve_contact_display_name(lookup, role=role, fallback=lookup)
    except Exception:  # noqa: BLE001
        display = lookup
    return PendingContactTarget(
        lookup_name=lookup,
        display_name=display or lookup,
        role=role,
        source=source or str(getattr(record, "source", "") or "registry"),
        confidence=float(confidence or 0.0),
        created_turn=int(created_turn or 0),
    )


def pending_target_from_role_label(
    role_label: str,
    *,
    registry: Any,
    source: str = "",
    confidence: float = 0.85,
    created_turn: int = 0,
) -> Optional[PendingContactTarget]:
    label = (role_label or "").strip()
    if not label or not _is_valid_target_name(label):
        return None
    rec = None
    if registry is not None:
        rec = registry.match_record_in_message(label)
        if rec is None:
            norm_label = _norm(label)
            for candidate in getattr(registry, "records", ()) or ():
                if candidate.is_owner:
                    continue
                if _norm(candidate.lookup_name) == norm_label:
                    rec = candidate
                    break
                for alias in getattr(candidate, "aliases", ()) or ():
                    if _norm(alias) == norm_label:
                        rec = candidate
                        break
                if rec is not None:
                    break
    if rec is not None:
        return pending_target_from_record(
            rec,
            source=source or "role_label",
            confidence=confidence,
            created_turn=created_turn,
        )
    return PendingContactTarget(
        lookup_name=label,
        display_name=label,
        role="",
        source=source or "role_label_unconfigured",
        confidence=float(confidence or 0.0),
        created_turn=int(created_turn or 0),
    )


def infer_pending_target_from_text(
    text: str,
    *,
    registry: Any,
    created_turn: int = 0,
    source: str = "outbound_mention",
) -> Optional[PendingContactTarget]:
    """Extract pending target only when text matches configured registry evidence."""
    raw = (text or "").strip()
    if not raw or registry is None:
        return None
    rec = registry.match_record_in_message(raw)
    if rec is not None:
        return pending_target_from_record(
            rec,
            source=source,
            confidence=0.90,
            created_turn=created_turn,
        )
    try:
        from modules.ai.brain.commerce.staff_contact_fallback_v0 import (  # noqa: PLC0415
            extract_staff_role_aliases_from_sections,
            load_staff_chain_sections,
        )
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            _ROLE_STAFF_RE,
        )

        norm = _norm(raw)
        if _ROLE_STAFF_RE.search(norm):
            m = re.search(
                r"(?:بائع(?:\s*المعرض)?|موظف|خدمة\s*العملاء|دعم(?:\s*العملاء)?|مسؤول|مدير)",
                norm,
                flags=re.UNICODE | re.IGNORECASE,
            )
            if m:
                return pending_target_from_role_label(
                    m.group(0),
                    registry=registry,
                    source=source,
                    created_turn=created_turn,
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[STAFF_CONTACT_CONTINUITY] role_label_infer_failed",
        )
    return None


def _recent_message_texts(
    brain_state: Optional[Dict[str, Any]],
    *,
    limit: int = 6,
) -> List[Tuple[str, str, int]]:
    """Return (direction, body, turn) tuples from recent_messages."""
    rows: List[Tuple[str, str, int]] = []
    for item in list((brain_state or {}).get("recent_messages") or [])[-limit:]:
        if not isinstance(item, dict):
            continue
        direction = str(
            item.get("direction")
            or item.get("role")
            or ""
        ).strip().lower()
        if direction in {"assistant", "bot", "outbound"}:
            direction = "outbound"
        elif direction in {"user", "customer", "inbound"}:
            direction = "inbound"
        body = str(item.get("body") or item.get("content") or "").strip()
        turn = int(item.get("turn") or 0)
        if body:
            rows.append((direction, body, turn))
    return rows


def infer_pending_target_from_context(
    *,
    registry: Any,
    brain_state: Optional[Dict[str, Any]] = None,
    current_turn: int = 0,
) -> Optional[PendingContactTarget]:
    """Fallback: infer from staff_contacts_sent or recent outbound/inbound turns."""
    bs = dict(brain_state or {})
    turn = int(current_turn or bs.get("turn") or 0)

    try:
        from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
            parse_staff_contacts_sent,
        )

        sent = parse_staff_contacts_sent(bs.get("staff_contacts_sent"))
        if sent:
            last = sent[-1]
            name = str(last.get("name") or "").strip()
            if name and _is_valid_target_name(name):
                created = int(last.get("turn") or turn)
                target = pending_target_from_role_label(
                    name,
                    registry=registry,
                    source="staff_contacts_sent",
                    confidence=0.98,
                    created_turn=created,
                )
                if target and not is_stale_pending_target(target, current_turn=turn):
                    return target
    except Exception:  # noqa: BLE001
        logger.exception(
            "[STAFF_CONTACT_CONTINUITY] staff_contacts_sent_infer_failed",
        )

    for direction, body, msg_turn in reversed(_recent_message_texts(bs)):
        inferred = infer_pending_target_from_text(
            body,
            registry=registry,
            created_turn=msg_turn or turn,
            source=f"recent_{direction}",
        )
        if inferred and not is_stale_pending_target(inferred, current_turn=turn):
            return inferred
    return None


def load_pending_contact_target(
    order_prep: Dict[str, Any],
    *,
    current_turn: int = 0,
    brain_state: Optional[Dict[str, Any]] = None,
    registry: Any = None,
) -> Optional[PendingContactTarget]:
    prep = dict(order_prep or {})
    raw = prep.get(PENDING_CONTACT_TARGET_KEY)
    target = PendingContactTarget.from_dict(raw)
    if target and not is_stale_pending_target(target, current_turn=current_turn):
        return target
    if registry is not None:
        return infer_pending_target_from_context(
            registry=registry,
            brain_state=brain_state,
            current_turn=current_turn,
        )
    return None


def persist_pending_contact_target(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    target: PendingContactTarget,
) -> bool:
    if not db or not tenant_id or not phone or not target or not target.lookup_name:
        return False
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        if conv is None:
            return False
        bs = dict(bs or {})
        op = dict(bs.get("order_prep") or {})
        op[PENDING_CONTACT_TARGET_KEY] = target.to_dict()
        bs["order_prep"] = op
        meta = dict(getattr(conv, "extra_metadata", None) or {})
        meta["brain_state"] = bs
        conv.extra_metadata = meta
        flag_modified(conv, "extra_metadata")
        db.add(conv)
        db.flush()
        logger.info(
            "[STAFF_CONTACT_CONTINUITY] persisted tenant=%s lookup=%r source=%s turn=%s",
            tenant_id,
            target.lookup_name,
            target.source,
            target.created_turn,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[STAFF_CONTACT_CONTINUITY] persist failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return False


def clear_pending_contact_target(
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
        if PENDING_CONTACT_TARGET_KEY not in op:
            return False
        op.pop(PENDING_CONTACT_TARGET_KEY, None)
        bs["order_prep"] = op
        meta = dict(getattr(conv, "extra_metadata", None) or {})
        meta["brain_state"] = bs
        conv.extra_metadata = meta
        flag_modified(conv, "extra_metadata")
        db.add(conv)
        db.flush()
        return True
    except Exception:  # noqa: BLE001
        return False


def capture_pending_target_from_inbound(
    message: str,
    *,
    registry: Any,
    created_turn: int = 0,
) -> Optional[PendingContactTarget]:
    raw = (message or "").strip()
    if not raw or registry is None:
        return None
    rec = registry.match_record_in_message(raw)
    if rec is not None:
        return pending_target_from_record(
            rec,
            source="inbound_named",
            confidence=0.92,
            created_turn=created_turn,
        )
    try:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            extract_staff_name_candidate,
        )
        from modules.ai.brain.commerce.staff_target_classifier import (  # noqa: PLC0415
            classify_staff_target,
        )

        span = extract_staff_name_candidate(raw)
        if span:
            verdict = classify_staff_target(raw, raw_span=span, registry=registry)
            if verdict.tier == "named_person" and verdict.raw_span:
                rec2 = registry.match_record_in_message(span)
                if rec2 is not None:
                    return pending_target_from_record(
                        rec2,
                        source="inbound_named_span",
                        confidence=float(verdict.confidence or 0.0),
                        created_turn=created_turn,
                    )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[STAFF_CONTACT_CONTINUITY] inbound_named_capture_failed",
        )
    return None


def try_persist_pending_contact_target_from_outbound(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    outbound_text: str,
    store_contact_phone: str = "",
    source: str = "outbound_mention",
) -> bool:
    if not db or not outbound_text.strip():
        return False
    try:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            load_staff_contact_registry,
        )
        from core.order_flow import _load_brain_state  # noqa: PLC0415

        _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        turn = int((bs or {}).get("turn") or 0)
        registry = load_staff_contact_registry(
            db, int(tenant_id or 0), store_contact_phone=store_contact_phone,
        )
        target = infer_pending_target_from_text(
            outbound_text,
            registry=registry,
            created_turn=turn,
            source=source,
        )
        if target is None:
            return False
        return persist_pending_contact_target(
            db,
            tenant_id=tenant_id,
            phone=phone,
            target=target,
        )
    except Exception:  # noqa: BLE001
        return False


def synthetic_message_for_pending_target(target: PendingContactTarget) -> str:
    name = (target.display_name or target.lookup_name or "").strip()
    return f"ابي رقم {name}"


def staff_request_from_pending_target(
    target: PendingContactTarget,
) -> Any:
    from modules.ai.brain.commerce.staff_contact_evidence import StaffContactRequest  # noqa: PLC0415

    tier = "named_person" if _is_valid_target_name(target.lookup_name) else "generic_role"
    return StaffContactRequest(
        kind="named",
        matched_alias=target.lookup_name,
        target_tier=tier,
        target_reason="continuity:pending_contact_target",
        target_confidence=float(target.confidence or 0.90),
        raw_span=target.lookup_name,
    )


def resolve_pending_contact_followup(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
    store_contact_phone: str = "",
) -> Optional[Tuple[Any, Any, str]]:
    """
    Return (StaffContactRequest, PendingContactTarget, synthetic_message) when
    a pronoun follow-up can be resolved; else None.
    """
    if not is_contact_target_followup(message or ""):
        return None
    if should_clear_pending_on_topic_switch(message or ""):
        return None

    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            load_staff_contact_registry,
        )

        _conv, bs = _load_brain_state(
            db,
            tenant_id=int(tenant_id or 0),
            phone=str(customer_phone or ""),
        )
        turn = int((bs or {}).get("turn") or 0)
        order_prep = dict((bs or {}).get("order_prep") or {})
        registry = load_staff_contact_registry(
            db, int(tenant_id or 0), store_contact_phone=store_contact_phone,
        )
        pending = load_pending_contact_target(
            order_prep,
            current_turn=turn,
            brain_state=bs,
            registry=registry,
        )
        if pending is None or is_stale_pending_target(pending, current_turn=turn):
            return None
        request = staff_request_from_pending_target(pending)
        synthetic = synthetic_message_for_pending_target(pending)
        logger.info(
            "[STAFF_CONTACT_CONTINUITY] resolved_followup tenant=%s lookup=%r source=%s",
            tenant_id,
            pending.lookup_name,
            pending.source,
        )
        return request, pending, synthetic
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[STAFF_CONTACT_CONTINUITY] resolve_followup_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return None


__all__ = [
    "DEFAULT_EXPIRES_AFTER_TURNS",
    "PENDING_CONTACT_TARGET_KEY",
    "PendingContactTarget",
    "capture_pending_target_from_inbound",
    "clear_pending_contact_target",
    "infer_pending_target_from_text",
    "is_contact_target_followup",
    "is_pronoun_staff_contact_followup",
    "is_stale_pending_target",
    "load_pending_contact_target",
    "persist_pending_contact_target",
    "pending_target_from_record",
    "pending_target_from_role_label",
    "resolve_pending_contact_followup",
    "should_clear_pending_on_topic_switch",
    "staff_request_from_pending_target",
    "synthetic_message_for_pending_target",
    "try_persist_pending_contact_target_from_outbound",
]
