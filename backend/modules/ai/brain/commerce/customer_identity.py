"""
brain/commerce/customer_identity.py
───────────────────────────────────
Customer identity extraction + persistence during active commerce/order flows.

Bridges LLM semantic understanding and durable state: when the customer
volunteers name / phone / city / location / recipient during checkout,
extract from the raw message, score confidence, and persist into
``order_prep`` + ``Customer`` profile before the assistant replies.

Tenant-agnostic — driven by ``has_active_order_context``, not tenant id.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.customer_identity")


def customer_identity_bridge_enabled() -> bool:
    """Kill-switch for B-WIRE-01 pipeline wiring (default on)."""
    return os.getenv("CUSTOMER_IDENTITY_BRIDGE_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

# ── Extended Arabic name / recipient anchors (commerce order flow) ─────
_RECIPIENT_RE = re.compile(
    r"^\s*(?:المستلم|المستلمة|اسم\s+المستلم|اسم\s+المستلمة)"
    r"\s+(?P<name>[\u0600-\u06FF\u0750-\u077Fa-zA-Z][\u0600-\u06FF\u0750-\u077Fa-zA-Z\s]{1,58})\s*$",
    re.UNICODE,
)
_REGISTER_RECIPIENT_RE = re.compile(
    r"^\s*(?:"
    r"سجل(?:\s+ب)?(?:اسم)?|سج(?:ل|لي)(?:\s+ب)?(?:اسم)?|"
    r"اسجل(?:\s+ب)?(?:اسم)?|"
    r"خل(?:ي|يه|ها)(?:\s+(?:الطلب(?:ية)?|الطلب))?(?:\s+ب)?(?:اسم)?|"
    r"اكتب(?:\s+(?:الطلب(?:ية)?|الطلب))?(?:\s+ب)?(?:اسم)?"
    r")\s+(?P<name>[\u0600-\u06FF\u0750-\u077Fa-zA-Z][\u0600-\u06FF\u0750-\u077Fa-zA-Z\s]{1,58})\s*$",
    re.UNICODE,
)
_BARE_NAME_LABEL_RE = re.compile(
    r"^\s*(?:الاسم|اسم|الإسم|إسم)\s+(?P<name>[\u0600-\u06FF\u0750-\u077Fa-zA-Z][\u0600-\u06FF\u0750-\u077Fa-zA-Z\s]{1,58})\s*$",
    re.UNICODE,
)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?966|0)?5\d{8}(?!\d)|"
    r"(?<!\d)\+?\d{10,15}(?!\d)",
)

_UPDATE_WORDING_RE = re.compile(
    r"(?:"
    r"غ(?:ي|ي)ر|عد(?:ل|ل)|ص(?:ح|ح)ح|بد(?:ل|ال)|"
    r"مو\s|مش\s|خط(?:أ|ا)|تصحيح|"
    r"update|change|correct|wrong|fix"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_CONFIRM_YES_RE = re.compile(
    r"^\s*(?:نعم|ايوه|أيوه|ايوة|أيوة|تمام|صح|صحيح|ok|yes|yep)\s*$",
    re.IGNORECASE | re.UNICODE,
)
_CONFIRM_NO_RE = re.compile(
    r"^\s*(?:لا|لأ|no|cancel|الغ(?:ي|اء))\s*$",
    re.IGNORECASE | re.UNICODE,
)

_IDENTITY_OVERWRITE_COOLDOWN_TURNS = 3

# Greeting / vocative / emotional — never treat as customer self-ID.
_GREETING_VOCATIVE_RE = re.compile(
    r"(?:"
    r"^\s*(?:هلا|مرحبا|أهلا|اهلا|السلام|مساء|صباح)|"
    r"\b(?:يا|ياا)\s+|"
    r"بعدي|ريحاني|بالخدمة|الله\s+يسعد|رسول\s+الله|"
    r"جزاك|جزيت|تسلم|بارك\s+الله"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_COMMERCE_NOUN_TOKENS = frozenset({
    "طلب", "طلبية", "منتج", "سعر", "توصيل", "شحن", "فاتورة", "دفع",
    "تحويل", "حساب", "متجر", "سلة",
})
_EXPLICIT_SELF_ID_RE = re.compile(
    r"(?:"
    r"^\s*(?:اسمي|إسمي|اسمى|إسمى|انا|أنا)\s+|"
    r"^\s*(?:الاسم|اسم|الإسم|إسم)\s+|"
    r"^\s*(?:المستلم|اسم\s+المستلم)|"
    r"^\s*(?:سجل|سج(?:ل|لي)|اسجل|خل(?:ي|يه)|اكتب)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


@dataclass
class IdentityField:
    """One extracted identity signal from the inbound message."""
    field: str          # name | recipient_name | phone | city | location
    value: str          # cleaned value used for persistence
    raw_value: str      # exact customer-provided substring
    confidence: str     # high | medium | low
    source: str = "message"


@dataclass
class CustomerIdentityResult:
    """Outcome of one apply pass."""
    applied: List[IdentityField] = field(default_factory=list)
    skipped: List[tuple[str, str]] = field(default_factory=list)  # (field, reason)
    needs_confirmation: Optional[IdentityField] = None


def _normalize_name_key(name: str) -> str:
    t = (name or "").strip()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )
    return re.sub(r"\s+", " ", t).strip().lower()


def _full_prep_name(prep: Any) -> str:
    first = str(getattr(prep, "customer_first_name", "") or "").strip()
    last = str(getattr(prep, "customer_last_name", "") or "").strip()
    return " ".join(p for p in (first, last) if p).strip()


def _is_update_wording(message: str) -> bool:
    return bool(_UPDATE_WORDING_RE.search(message or ""))


def _split_name(full_name: str) -> tuple[str, str]:
    parts = [p.strip() for p in (full_name or "").split() if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _norm_token(tok: str) -> str:
    t = (tok or "").strip()
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return t.lower()


def _standalone_name_likelihood(message: str, full_name: str) -> bool:
    """Strict Arabic personal-name likelihood for standalone checkout replies."""
    text = (message or "").strip()
    if not text or "\n" in text:
        return False
    if _GREETING_VOCATIVE_RE.search(text):
        return False
    if _EXPLICIT_SELF_ID_RE.search(text):
        return True
    tokens = [t for t in full_name.split() if t.strip()]
    if not tokens or len(tokens) > 4 or len(tokens) < 2:
        return False
    for tok in tokens:
        nt = _norm_token(tok)
        if nt in _COMMERCE_NOUN_TOKENS:
            return False
        if nt in {"بالخدمة", "خدمة", "الخدمة", "يسعد", "رسول"}:
            return False
    if re.search(r"(?:بالخدمة|الله|رسول|يسعدك|مرحبا|هلا\s)", text, re.I):
        return False
    return _validate_name_tokens(full_name)


def _is_assistant_echo_only(
    message: str,
    name: str,
    history: Optional[List[Dict[str, Any]]],
) -> bool:
    """True when name likely comes from assistant greeting, not customer ID."""
    text = (message or "").strip()
    if _EXPLICIT_SELF_ID_RE.search(text):
        return False
    if _GREETING_VOCATIVE_RE.search(text):
        return True
    norm_name = _normalize_name_key(name)
    if not norm_name or not history:
        return False
    seen_out = False
    seen_customer_intro = False
    for turn in reversed(history[-12:]):
        body = str(turn.get("body") or turn.get("text") or "")
        direction = str(turn.get("direction") or "")
        if norm_name not in _normalize_name_key(body):
            continue
        if direction in {"out", "outbound"}:
            seen_out = True
        elif direction in {"in", "inbound"} and _EXPLICIT_SELF_ID_RE.search(body):
            seen_customer_intro = True
    return seen_out and not seen_customer_intro and not _EXPLICIT_SELF_ID_RE.search(text)


def _validate_name_tokens(raw: str) -> bool:
    """Light validation — letters-only tokens, 1–4 tokens, not blocklisted."""
    from modules.ai.brain.intent.ordering_extractor import (  # noqa: PLC0415
        _clean_name_candidate,
    )

    return bool(_clean_name_candidate(raw))


def _is_standalone_name_message(message: str, ordering: Dict[str, Any]) -> bool:
    """True when the message is essentially just a personal name."""
    text = (message or "").strip()
    if not text or "\n" in text:
        return False
    if ordering.get("city") or ordering.get("short_address_code"):
        return False
    if ordering.get("google_maps_url") or _PHONE_RE.search(text):
        return False
    first = str(ordering.get("customer_first_name") or "").strip()
    last = str(ordering.get("customer_last_name") or "").strip()
    full = " ".join(p for p in (first, last) if p).strip()
    if not full or not _validate_name_tokens(full):
        return False
    return _standalone_name_likelihood(text, full)


def _extract_phone(message: str) -> Optional[IdentityField]:
    m = _PHONE_RE.search(message or "")
    if not m:
        return None
    raw = m.group(0).strip()
    return IdentityField(
        field="phone",
        value=raw,
        raw_value=raw,
        confidence="high",
        source="message",
    )


def _extract_pattern_name(message: str) -> Optional[IdentityField]:
    text = (message or "").strip()
    if not text:
        return None

    for pattern, field_name, confidence in (
        (_RECIPIENT_RE, "recipient_name", "high"),
        (_REGISTER_RECIPIENT_RE, "recipient_name", "high"),
        (_BARE_NAME_LABEL_RE, "name", "high"),
    ):
        m = pattern.match(text)
        if not m:
            continue
        raw = re.sub(r"\s+", " ", (m.group("name") or "").strip())
        if not raw or not _validate_name_tokens(raw):
            continue
        return IdentityField(
            field=field_name,
            value=raw,
            raw_value=raw,
            confidence=confidence,
            source="message",
        )

    try:
        from core.customer_name_extractor import extract_high_confidence_name  # noqa: PLC0415

        hit = extract_high_confidence_name(text)
        if hit:
            return IdentityField(
                field="name",
                value=hit.value,
                raw_value=hit.value,
                confidence="high",
                source=f"pattern_{hit.pattern}",
            )
    except Exception:  # noqa: BLE001
        pass

    return None


def _extract_ordering_slots(message: str) -> Dict[str, Any]:
    from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots  # noqa: PLC0415

    return dict(extract_ordering_slots(message or "") or {})


def extract_customer_identity_fields(
    message: str,
    *,
    intent_slots: Optional[Dict[str, Any]] = None,
    in_order_flow: bool = False,
    history: Optional[List[Dict[str, Any]]] = None,
) -> List[IdentityField]:
    """
    Extract identity fields from RAW customer message only.

    Never uses semantic-repaired / canonicalized text or LLM name slots.
    """
    if not in_order_flow:
        return []

    text = (message or "").strip()
    if not text:
        return []

    logger.info("[CUSTOMER_IDENTITY_SOURCE] source=raw_message preview=%r", text[:80])

    found: List[IdentityField] = []
    seen_fields: set[str] = set()

    def _add(item: Optional[IdentityField]) -> None:
        if not item or item.field in seen_fields:
            return
        if item.confidence == "low":
            return
        if item.field in ("name", "recipient_name"):
            if _is_assistant_echo_only(text, item.raw_value, history):
                logger.info(
                    "[CUSTOMER_IDENTITY_SUPPRESSED] field=%s reason=assistant_echo_only",
                    item.field,
                )
                return
            if item.field == "name" and not _EXPLICIT_SELF_ID_RE.search(text):
                if not _standalone_name_likelihood(text, item.raw_value):
                    if item.source == "standalone_name":
                        logger.info(
                            "[CUSTOMER_IDENTITY_SUPPRESSED] field=name "
                            "reason=low_name_likelihood",
                        )
                        return
        seen_fields.add(item.field)
        logger.info(
            "[CUSTOMER_IDENTITY_EXTRACTED] field=%s confidence=%s source=%s value=%r",
            item.field,
            item.confidence,
            item.source,
            item.raw_value[:80],
        )
        found.append(item)

    _add(_extract_pattern_name(text))

    ordering = _extract_ordering_slots(text)
    standalone_name_like = _is_standalone_name_message(text, ordering)

    for key, target_field, conf in (
        ("city", "city", "high"),
        ("google_maps_url", "location", "high"),
        ("short_address_code", "location", "high"),
        ("address_line", "location", "medium"),
    ):
        val = str(ordering.get(key) or "").strip()
        if not val:
            continue
        if target_field == "location":
            _add(IdentityField(
                field="location",
                value=val,
                raw_value=val,
                confidence=conf,
                source="ordering_extractor",
            ))
        elif target_field == "city":
            _add(IdentityField(
                field="city",
                value=val,
                raw_value=val,
                confidence=conf,
                source="ordering_extractor",
            ))

    _add(_extract_phone(text))

    # Standalone Arabic personal-name-like message (active order only).
    if (
        "name" not in seen_fields
        and "recipient_name" not in seen_fields
        and not _REGISTER_RECIPIENT_RE.match(text)
        and not _RECIPIENT_RE.match(text)
        and not ordering.get("city")
        and not ordering.get("short_address_code")
    ):
        name_first = str(ordering.get("customer_first_name") or "").strip()
        name_last = str(ordering.get("customer_last_name") or "").strip()
        if name_first:
            full = " ".join(p for p in (name_first, name_last) if p).strip()
            if standalone_name_like:
                _add(IdentityField(
                    field="name",
                    value=full,
                    raw_value=full,
                    confidence="high",
                    source="standalone_name",
                ))

    return found


def _stamp_provenance(prep: Any, field: str, source: str) -> None:
    prov = dict(getattr(prep, "identity_provenance", None) or {})
    prov[field] = source
    prep.identity_provenance = prov


def _in_overwrite_cooldown(prep: Any, state: Any, *, force_update: bool) -> bool:
    if force_update:
        return False
    last_turn = int(getattr(prep, "identity_name_updated_turn", 0) or 0)
    current = int(getattr(state, "turn", 0) or 0)
    if last_turn <= 0:
        return False
    return (current - last_turn) < _IDENTITY_OVERWRITE_COOLDOWN_TURNS


def _ensure_order_prep(state: Any) -> Any:
    from modules.ai.brain.types import OrderPreparationState  # noqa: PLC0415

    prep = getattr(state, "order_prep", None)
    if prep is None:
        prep = OrderPreparationState()
        state.order_prep = prep
    return prep


def _log_skip(field: str, reason: str) -> None:
    logger.info(
        "[CUSTOMER_IDENTITY_SKIPPED] field=%s reason=%s",
        field,
        reason,
    )


def _persist_customer_profile(
    db: Any,
    *,
    tenant_id: Any,
    phone: str,
    name: str,
) -> None:
    if not db or not phone or not name:
        return
    try:
        from services.customer_intelligence import CustomerIntelligenceService  # noqa: PLC0415

        svc = CustomerIntelligenceService(db, tenant_id)
        svc.upsert_customer_identity(
            phone=phone,
            name=name,
            source="ai_detected_name",
        )
        logger.info(
            "[CUSTOMER_PROFILE_UPDATED] field=name tenant=%s phone=%s value=%r",
            tenant_id,
            phone,
            name[:80],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CUSTOMER_PROFILE_UPDATED] failed tenant=%s err=%s",
            tenant_id,
            exc,
        )


def _persist_customer_contact_phone(
    db: Any,
    *,
    tenant_id: Any,
    channel_phone: str,
    contact_phone: str,
) -> None:
    if not db or not channel_phone or not contact_phone:
        return
    try:
        from services.customer_intelligence import CustomerIntelligenceService  # noqa: PLC0415

        svc = CustomerIntelligenceService(db, tenant_id)
        svc.persist_order_flow_contact_phone(
            channel_phone=channel_phone,
            contact_phone_raw=contact_phone,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CUSTOMER_PROFILE_UPDATED] contact_phone failed tenant=%s err=%s",
            tenant_id,
            exc,
        )


def _apply_name_to_prep(
    prep: Any,
    item: IdentityField,
    *,
    force: bool = False,
) -> bool:
    from modules.ai.brain.execution.orders import (  # noqa: PLC0415
        _merge_message_details,
        _prep_has_real_name,
    )

    if item.field == "recipient_name":
        existing = str(getattr(prep, "recipient_name", "") or "").strip()
        if existing and not force and _normalize_name_key(existing) != _normalize_name_key(item.value):
            return False
        prep.recipient_name = item.raw_value
        _stamp_provenance(prep, "recipient_name", "explicit_customer_statement")
        logger.info(
            "[ORDER_PREP_UPDATED] field=recipient_name value=%r",
            item.raw_value[:80],
        )
        return True

    first, last = _split_name(item.value)
    slots = {
        "customer_name": item.raw_value,
        "customer_first_name": first,
        "customer_last_name": last,
    }
    before = _full_prep_name(prep)
    if force:
        if first:
            prep.customer_first_name = first
        if last:
            prep.customer_last_name = last
    else:
        _merge_message_details(prep, slots, item.raw_value)
    after = _full_prep_name(prep)
    if after and after != before:
        _stamp_provenance(prep, "customer_name", "explicit_customer_statement")
        logger.info(
            "[ORDER_PREP_UPDATED] field=customer_name before=%r after=%r",
            before,
            after,
        )
        return True
    if after and _prep_has_real_name(prep):
        return True
    return False


def _apply_non_name_to_prep(prep: Any, item: IdentityField) -> bool:
    from modules.ai.brain.execution.orders import _merge_message_details  # noqa: PLC0415

    slots: Dict[str, Any] = {}
    if item.field == "city":
        slots["city"] = item.value
    elif item.field == "phone":
        existing_phone = str(getattr(prep, "customer_phone", "") or "").strip()
        if existing_phone and existing_phone != item.value:
            return False
        prep.customer_phone = item.value
        _stamp_provenance(prep, "customer_phone", "explicit_customer_statement")
        logger.info(
            "[ORDER_PREP_UPDATED] field=customer_phone value=%r",
            item.raw_value[:80],
        )
        return True
    elif item.field == "location":
        if item.value.upper().startswith("HTTP") or "maps" in item.value.lower():
            slots["google_maps_url"] = item.value
        elif re.match(r"^[A-Za-z]{4}\d{4}$", item.value.upper()):
            slots["short_address_code"] = item.value.upper()
        else:
            slots["address_line"] = item.value

    if not slots:
        return False
    before_city = str(getattr(prep, "city", "") or "")
    _merge_message_details(prep, slots, item.raw_value)
    if item.field == "city" and str(getattr(prep, "city", "") or "") != before_city:
        logger.info(
            "[ORDER_PREP_UPDATED] field=city value=%r",
            item.raw_value[:80],
        )
        return True
    if item.field == "location":
        logger.info(
            "[ORDER_PREP_UPDATED] field=location value=%r",
            item.raw_value[:80],
        )
        return True
    return False


def _handle_pending_confirmation(
    ctx: Any,
    message: str,
) -> Optional[CustomerIdentityResult]:
    prep = getattr(ctx.state, "order_prep", None)
    if not prep:
        return None
    pending_field = str(getattr(prep, "pending_identity_field", "") or "").strip()
    pending_value = str(getattr(prep, "pending_identity_value", "") or "").strip()
    if not pending_field or not pending_value:
        return None

    result = CustomerIdentityResult()
    if _CONFIRM_YES_RE.match(message or ""):
        item = IdentityField(
            field=pending_field,
            value=pending_value,
            raw_value=pending_value,
            confidence="high",
            source="confirmation_yes",
        )
        if pending_field in ("name", "recipient_name"):
            _apply_name_to_prep(prep, item, force=True)
        else:
            _apply_non_name_to_prep(prep, item)
        if pending_field == "name":
            _persist_customer_profile(
                getattr(ctx, "_db", None),
                tenant_id=ctx.tenant_id,
                phone=ctx.customer_phone,
                name=pending_value,
            )
        elif pending_field == "phone":
            _persist_customer_contact_phone(
                getattr(ctx, "_db", None),
                tenant_id=ctx.tenant_id,
                channel_phone=ctx.customer_phone,
                contact_phone=pending_value,
            )
        prep.pending_identity_field = ""
        prep.pending_identity_value = ""
        result.applied.append(item)
        return result

    if _CONFIRM_NO_RE.match(message or ""):
        _log_skip(pending_field, "confirmation_declined")
        prep.pending_identity_field = ""
        prep.pending_identity_value = ""
        result.skipped.append((pending_field, "confirmation_declined"))
        return result

    return None


def apply_customer_identity_during_order_flow(
    ctx: Any,
    db: Any = None,
) -> CustomerIdentityResult:
    """
    Extract + persist customer identity when checkout context is active.

    Mutates ``ctx.state.order_prep`` in place. Safe to call before the
    decision engine so the assistant's reply reflects stored data.
    """
    from modules.ai.brain.execution.orders import _prep_has_real_name  # noqa: PLC0415
    from modules.ai.brain.order_context_gate import has_active_order_context  # noqa: PLC0415

    result = CustomerIdentityResult()
    raw = getattr(ctx, "raw_message", None)
    if raw is None or str(raw).strip() == "":
        raw = getattr(ctx, "message", "")
    message = str(raw or "").strip()
    if getattr(ctx, "message", "") and message != str(getattr(ctx, "message", "") or "").strip():
        logger.info(
            "[CUSTOMER_IDENTITY_SOURCE] source=raw_message "
            "(semantic repair ignored for identity)",
        )

    pending = _handle_pending_confirmation(ctx, message)
    if pending is not None:
        return pending

    if not has_active_order_context(ctx):
        return result

    prep = _ensure_order_prep(ctx.state)
    history = list(getattr(ctx, "history", None) or [])

    extractions = extract_customer_identity_fields(
        message,
        in_order_flow=True,
        history=history,
    )

    for item in extractions:
        if item.confidence == "low":
            _log_skip(item.field, "low_confidence")
            result.skipped.append((item.field, "low_confidence"))
            continue

        if item.field in ("name", "recipient_name"):
            if _in_overwrite_cooldown(
                prep, ctx.state, force_update=_is_update_wording(message),
            ):
                _log_skip(item.field, "overwrite_cooldown")
                result.skipped.append((item.field, "overwrite_cooldown"))
                continue

            existing = (
                _full_prep_name(prep)
                if item.field == "name"
                else str(getattr(prep, "recipient_name", "") or "").strip()
            )
            has_verified = (
                _prep_has_real_name(prep)
                if item.field == "name"
                else bool(existing)
            )
            if existing and has_verified:
                if _normalize_name_key(existing) != _normalize_name_key(item.value):
                    if not _is_update_wording(message):
                        _log_skip(item.field, "existing_verified_name")
                        result.skipped.append((item.field, "existing_verified_name"))
                        if item.confidence in ("high", "medium"):
                            prep.pending_identity_field = item.field
                            prep.pending_identity_value = item.raw_value
                            result.needs_confirmation = item
                        continue

            if item.confidence == "high":
                if _apply_name_to_prep(prep, item):
                    prep.identity_name_updated_turn = int(getattr(ctx.state, "turn", 0) or 0)
                    result.applied.append(item)
                    if item.field == "name":
                        _persist_customer_profile(
                            db or getattr(ctx, "_db", None),
                            tenant_id=ctx.tenant_id,
                            phone=ctx.customer_phone,
                            name=item.raw_value,
                        )
            elif item.confidence == "medium":
                prep.pending_identity_field = item.field
                prep.pending_identity_value = item.raw_value
                result.needs_confirmation = item
                _log_skip(item.field, "ambiguous")
            continue

        if item.confidence == "high":
            if _apply_non_name_to_prep(prep, item):
                result.applied.append(item)
                if item.field == "phone":
                    _persist_customer_contact_phone(
                        db or getattr(ctx, "_db", None),
                        tenant_id=ctx.tenant_id,
                        channel_phone=ctx.customer_phone,
                        contact_phone=item.raw_value,
                    )
        elif item.confidence == "medium":
            prep.pending_identity_field = item.field
            prep.pending_identity_value = item.raw_value
            result.needs_confirmation = item
            _log_skip(item.field, "ambiguous")

    return result


__all__ = [
    "CustomerIdentityResult",
    "IdentityField",
    "apply_customer_identity_during_order_flow",
    "customer_identity_bridge_enabled",
    "extract_customer_identity_fields",
]
