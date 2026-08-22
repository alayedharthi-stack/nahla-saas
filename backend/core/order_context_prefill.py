"""
core/order_context_prefill.py
──────────────────────────────
Phase C — identity/shipping missing modes, merchant-lock respect, and
optional safe order_prep prefill behind feature flags.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

from core.customer_identity_resolver import (
    SOURCE_WHATSAPP_PROFILE,
    STATUS_PROPOSED,
)
from core.wa_order_lifecycle import has_accepted_delivery_address

logger = logging.getLogger("nahla.order_context_prefill")

MODE_SKIP = "skip"
MODE_CONFIRM = "confirm"
MODE_ASK = "ask"
MODE_EDIT_REQUESTED = "edit_requested"

_NAME_EDIT_RE = re.compile(
    r"(?:"
    r"غير\s*(?:ال)?اسم|اسم\s*غلط|الاسم\s*غلط|مو\s*اسمي|تصحيح\s*اسم|"
    r"change\s*name|wrong\s*name|correct\s*name|not\s*my\s*name"
    r")",
    re.I | re.UNICODE,
)
_SHIPPING_EDIT_RE = re.compile(
    r"(?:"
    r"أ?غير\s*(?:ال)?(?:عنوان|موقع|التوصيل|المدينة|المدينه)|"
    r"(?:ال)?عنوان\s*غ(?:ل|ي)ط|مو\s*هذا\s*(?:ال)?(?:عنوان|موقع)|"
    r"موقع\s*ث(?:اني|اني)|تغي(?:ر|ير)\s*(?:ال)?(?:عنوان|المدينة|المدينه)|"
    r"change\s*(?:address|location)|wrong\s*address|different\s*address"
    r")",
    re.I | re.UNICODE,
)
_PREVIOUS_ADDRESS_CONFIRM_RE = re.compile(
    r"(?:"
    r"نفس\s*(?:ال)?(?:عنوان|موقع)|العنوان\s*(?:ال)?(?:سابق|قديم|اول|الأول)|"
    r"عنوان(?:ي|نا)?\s*(?:ال)?(?:سابق|قديم|اول|الأول)|"
    r"استخدم\s*(?:ال)?(?:عنوان|موقع)\s*(?:ال)?(?:سابق|قديم)|"
    r"(?:عندكم|عندك|مسجل|محفوظ).{0,30}(?:عنوان(?:ي|نا)?|الموقع|المدينة)|"
    r"عنوان(?:ي|نا)?\s*(?:عندكم|عندك|مسجل|محفوظ)|"
    r"(?:عندكم|عندك)\s*مسجل(?:ة)?|"
    r"مسجل(?:ة|ه)?\s*عند(?:كم|ك)|"
    r"confirm\s*(?:previous|old)\s*address|same\s*address"
    r")",
    re.I | re.UNICODE,
)


@dataclass(frozen=True)
class EditIntentFacts:
    name_edit_requested: bool = False
    shipping_edit_requested: bool = False
    previous_address_confirmed: bool = False


@dataclass(frozen=True)
class OrderPrefillState:
    shadow_missing_modes: Dict[str, str]
    identity_missing_mode: str
    shipping_city_mode: str
    shipping_delivery_mode: str
    customer_requested_edit: bool = False
    locked_field_edit_requested: bool = False
    requires_merchant_review: bool = False
    suggested_shipping_snapshot: Optional[Any] = None


def detect_edit_intent_facts(
    message: str = "",
    prep: Optional[Dict[str, Any]] = None,
) -> EditIntentFacts:
    prep = dict(prep or {})
    text = str(message or "").strip()
    name_edit = bool(_NAME_EDIT_RE.search(text)) or bool(prep.get("customer_requested_name_edit"))
    shipping_edit = (
        bool(_SHIPPING_EDIT_RE.search(text))
        or bool(prep.get("customer_requested_shipping_edit"))
        or bool(prep.get("order_flow_v2_address_refused"))
    )
    prev_confirmed = (
        bool(_PREVIOUS_ADDRESS_CONFIRM_RE.search(text))
        or bool(prep.get("previous_address_confirmed"))
        or bool(prep.get("customer_confirmed_previous_address"))
    )
    return EditIntentFacts(
        name_edit_requested=name_edit,
        shipping_edit_requested=shipping_edit,
        previous_address_confirmed=prev_confirmed,
    )


def _prep_str(prep: Dict[str, Any], key: str) -> str:
    return str(prep.get(key) or "").strip()


def _shipping_locked(*, prep: Dict[str, Any], draft_meta: Dict[str, Any]) -> bool:
    if bool(draft_meta.get("merchant_edit_locked")):
        return True
    return bool(prep.get("merchant_shipping_locked"))


def resolve_identity_missing_mode(
    *,
    operational_name: str,
    first_name: str,
    last_name: str,
    has_verified_name: bool,
    has_proposed_name: bool,
    locked_by_merchant: bool,
    edit_facts: EditIntentFacts,
) -> str:
    if edit_facts.name_edit_requested:
        return MODE_EDIT_REQUESTED
    if locked_by_merchant and operational_name:
        return MODE_SKIP
    if has_verified_name and operational_name:
        return MODE_SKIP
    if first_name and last_name:
        return MODE_SKIP
    if has_proposed_name and not has_verified_name:
        return MODE_CONFIRM
    if operational_name and not has_verified_name:
        return MODE_CONFIRM
    return MODE_ASK


def resolve_shipping_city_mode(
    *,
    city: str,
    locked_by_merchant: bool,
    known_previous: bool,
    edit_facts: EditIntentFacts,
) -> str:
    if edit_facts.shipping_edit_requested:
        return MODE_EDIT_REQUESTED
    if city:
        return MODE_SKIP if locked_by_merchant else MODE_SKIP
    if known_previous:
        return MODE_CONFIRM
    return MODE_ASK


def resolve_shipping_delivery_mode(
    *,
    accepted_delivery_address: bool,
    locked_by_merchant: bool,
    known_previous: bool,
    edit_facts: EditIntentFacts,
) -> str:
    if edit_facts.shipping_edit_requested:
        return MODE_EDIT_REQUESTED
    if accepted_delivery_address:
        return MODE_SKIP
    if known_previous and not accepted_delivery_address:
        return MODE_CONFIRM
    return MODE_ASK


def compute_shadow_missing_modes(
    *,
    identity_mode: str,
    city_mode: str,
    delivery_mode: str,
    has_product: bool,
    has_total: bool,
) -> Dict[str, str]:
    modes: Dict[str, str] = {}
    modes["product"] = MODE_SKIP if has_product else MODE_ASK
    modes["name"] = identity_mode
    modes["city"] = city_mode
    modes["delivery_address"] = delivery_mode
    modes["total"] = MODE_SKIP if has_total else MODE_ASK
    return modes


def shadow_missing_fields_from_modes(modes: Dict[str, str]) -> List[str]:
    missing: List[str] = []
    for field, mode in modes.items():
        if mode in {MODE_ASK, MODE_CONFIRM, MODE_EDIT_REQUESTED}:
            missing.append(field)
    return missing


def build_prefill_state(
    *,
    identity: Any,
    shipping: Any,
    known_previous: Optional[Any],
    prep: Dict[str, Any],
    active_draft: Optional[Any],
    message: str = "",
    has_product: bool,
    has_total: bool,
) -> OrderPrefillState:
    draft_meta = {}
    if active_draft is not None and getattr(active_draft, "merchant_edit_locked", False):
        draft_meta["merchant_edit_locked"] = True

    edit_facts = detect_edit_intent_facts(message, prep)
    shipping_locked = _shipping_locked(prep=prep, draft_meta=draft_meta)
    has_previous = known_previous is not None and bool(
        getattr(known_previous, "city", None)
        or getattr(known_previous, "maps_url", None)
        or getattr(known_previous, "short_address", None)
    )

    identity_mode = resolve_identity_missing_mode(
        operational_name=getattr(identity, "operational_name", "") or "",
        first_name=getattr(identity, "first_name", "") or "",
        last_name=getattr(identity, "last_name", "") or "",
        has_verified_name=bool(getattr(identity, "has_verified_name", False)),
        has_proposed_name=bool(getattr(identity, "has_proposed_name", False)),
        locked_by_merchant=bool(getattr(identity, "locked_by_merchant", False)),
        edit_facts=edit_facts,
    )
    city_mode = resolve_shipping_city_mode(
        city=getattr(shipping, "city", "") or "",
        locked_by_merchant=shipping_locked,
        known_previous=has_previous,
        edit_facts=edit_facts,
    )
    delivery_mode = resolve_shipping_delivery_mode(
        accepted_delivery_address=bool(getattr(shipping, "accepted_delivery_address", False)),
        locked_by_merchant=shipping_locked,
        known_previous=has_previous,
        edit_facts=edit_facts,
    )

    modes = compute_shadow_missing_modes(
        identity_mode=identity_mode,
        city_mode=city_mode,
        delivery_mode=delivery_mode,
        has_product=has_product,
        has_total=has_total,
    )

    customer_requested_edit = edit_facts.name_edit_requested or edit_facts.shipping_edit_requested
    locked_field_edit_requested = customer_requested_edit and (
        bool(getattr(identity, "locked_by_merchant", False)) or shipping_locked
    )
    requires_review = locked_field_edit_requested

    suggested = known_previous if (
        has_previous
        and not getattr(shipping, "accepted_delivery_address", False)
        and delivery_mode == MODE_CONFIRM
    ) else None

    return OrderPrefillState(
        shadow_missing_modes=modes,
        identity_missing_mode=identity_mode,
        shipping_city_mode=city_mode,
        shipping_delivery_mode=delivery_mode,
        customer_requested_edit=customer_requested_edit,
        locked_field_edit_requested=locked_field_edit_requested,
        requires_merchant_review=requires_review,
        suggested_shipping_snapshot=suggested,
    )


def enrich_identity_context(identity: Any, *, missing_mode: str) -> Any:
    first = str(getattr(identity, "first_name", "") or "").strip()
    last = str(getattr(identity, "last_name", "") or "").strip()
    can_label = bool(getattr(identity, "operational_name", "")) and (
        getattr(identity, "has_verified_name", False)
        or getattr(identity, "locked_by_merchant", False)
        or bool(first and last)
    )
    return replace(
        identity,
        missing_mode=missing_mode,
        can_use_for_shipping_label=can_label,
    )


def enrich_shipping_context(
    shipping: Any,
    *,
    city_mode: str,
    delivery_mode: str,
    locked_by_merchant: bool,
    requires_merchant_review: bool,
) -> Any:
    missing_mode = delivery_mode if delivery_mode != MODE_SKIP else city_mode
    return replace(
        shipping,
        locked_by_merchant=locked_by_merchant,
        missing_mode=missing_mode,
        requires_merchant_review=requires_merchant_review,
    )


def _shipping_context_to_prep_patch(source: Any) -> Dict[str, Any]:
    return {
        "city": getattr(source, "city", "") or "",
        "district": getattr(source, "district", "") or "",
        "street": getattr(source, "street", "") or "",
        "address_line": getattr(source, "address_line", "") or "",
        "google_maps_url": getattr(source, "maps_url", "") or "",
        "short_address_code": getattr(source, "short_address", "") or "",
        "latitude": getattr(source, "latitude", None),
        "longitude": getattr(source, "longitude", None),
        "shipping_source": getattr(source, "source", "") or "customer_confirmed_previous_address",
        "shipping_confidence": getattr(source, "confidence", 0.95),
    }


def _operational_prefill_enabled() -> bool:
    import os

    return os.environ.get("ORDER_CONTEXT_OPERATIONAL_PREFILL_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _shipping_confirm_enabled() -> bool:
    import os

    return os.environ.get("ORDER_CONTEXT_SHIPPING_CONFIRM_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def build_order_prep_prefill_patch(
    ctx: Any,
    *,
    prep: Dict[str, Any],
) -> Dict[str, Any]:
    """Non-mutating patch proposal from OrderContext prefill rules."""
    if not _operational_prefill_enabled():
        return {}

    patch: Dict[str, Any] = {}
    identity = ctx.identity
    prefill = ctx.prefill
    locked_name = bool(getattr(identity, "locked_by_merchant", False))

    first_name = getattr(identity, "first_name", "") or ""
    last_name = getattr(identity, "last_name", "") or ""
    if not first_name and not last_name and getattr(identity, "operational_name", ""):
        from core.order_context_builder import _split_name  # noqa: PLC0415

        first_name, last_name = _split_name(identity.operational_name)

    if (
        not locked_name
        and getattr(identity, "has_verified_name", False)
        and getattr(identity, "operational_name", "")
        and prefill.identity_missing_mode == MODE_SKIP
    ):
        if not _prep_str(prep, "customer_first_name") and first_name:
            patch["customer_first_name"] = first_name
        if not _prep_str(prep, "customer_last_name") and last_name:
            patch["customer_last_name"] = last_name
        if patch:
            patch["identity_prefill_source"] = identity.name_source or "verified_customer"
            patch["identity_prefill_confidence"] = identity.confidence

    edit_facts = detect_edit_intent_facts("", prep)
    if (
        _shipping_confirm_enabled()
        and edit_facts.previous_address_confirmed
        and ctx.known_previous_address is not None
    ):
        if not bool(getattr(ctx.shipping, "locked_by_merchant", False)):
            if not has_accepted_delivery_address(prep):
                patch.update(_shipping_context_to_prep_patch(ctx.known_previous_address))
                patch["customer_confirmed_previous_address"] = True
                patch["shipping_source"] = "customer_confirmed_previous_address"

    if prefill.customer_requested_edit:
        patch["customer_requested_edit"] = True
    if prefill.locked_field_edit_requested:
        patch["locked_field_edit_requested"] = True
    if prefill.requires_merchant_review:
        patch["requires_merchant_review"] = True

    return patch


def apply_order_prep_prefill_patch(prep: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a prefill patch in-place on a copy of order_prep."""
    merged = dict(prep or {})
    for key, value in patch.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        current = merged.get(key)
        if isinstance(current, str) and current.strip():
            continue
        if key in {"customer_first_name", "customer_last_name"}:
            if bool(merged.get("merchant_name_locked")) or bool(merged.get("identity_locked_by_merchant")):
                continue
        if key in {
            "city",
            "google_maps_url",
            "short_address_code",
            "address_line",
            "district",
            "street",
            "latitude",
            "longitude",
        }:
            if bool(merged.get("merchant_shipping_locked")):
                continue
        merged[key] = value
    return merged


def maybe_apply_operational_prefill_to_state(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int],
    customer: Any,
    phone: str,
    message: str,
    state: Any,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    if not _operational_prefill_enabled():
        return None
    if state is None or conversation_id is None:
        return None

    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415
        from models import Conversation  # noqa: PLC0415

        conversation = (
            db.query(Conversation).filter_by(id=int(conversation_id), tenant_id=tenant_id).first()
        )
        if conversation is None:
            return None

        if customer is None and getattr(conversation, "customer_id", None):
            from models import Customer  # noqa: PLC0415

            customer = db.query(Customer).filter_by(id=int(conversation.customer_id)).first()
        if customer is None and phone:
            try:
                from services.customer_intelligence import CustomerIntelligenceService  # noqa: PLC0415

                customer = CustomerIntelligenceService(db, tenant_id).upsert_lead_customer(
                    phone=phone,
                    source="whatsapp_inbound",
                    commit=False,
                )
            except Exception:  # noqa: BLE001
                customer = None

        brain_state = {}
        prep_obj = getattr(state, "order_prep", None)
        if prep_obj is not None:
            try:
                from dataclasses import asdict  # noqa: PLC0415

                brain_state["order_prep"] = asdict(prep_obj)
            except Exception:  # noqa: BLE001
                brain_state["order_prep"] = dict(getattr(prep_obj, "__dict__", {}) or {})
        else:
            brain_state["order_prep"] = {}

        ctx = build_order_context(
            db,
            tenant_id=tenant_id,
            conversation=conversation,
            customer=customer,
            phone=phone,
            brain_state=brain_state,
            inbound_metadata=inbound_metadata,
            build_source="operational_prefill",
            message=message,
        )
        patch = build_order_prep_prefill_patch(ctx, prep=brain_state.get("order_prep") or {})
        if not patch:
            return ctx

        merged = apply_order_prep_prefill_patch(brain_state.get("order_prep") or {}, patch)
        prep_obj = getattr(state, "order_prep", None)
        if prep_obj is not None:
            for key, value in merged.items():
                if hasattr(prep_obj, key):
                    setattr(prep_obj, key, value)
        logger.info(
            "[ORDER_CONTEXT_PREFILL] applied tenant=%s conv=%s keys=%s",
            tenant_id,
            conversation_id,
            sorted(patch.keys()),
        )
        return ctx
    except Exception:  # noqa: BLE001
        logger.exception(
            "[ORDER_CONTEXT_PREFILL] apply failed tenant=%s conv=%s",
            tenant_id,
            conversation_id,
        )
        return None


def build_order_context_api_payload(ctx: Any) -> Dict[str, Any]:
    identity = ctx.identity
    shipping = ctx.shipping
    prefill = ctx.prefill
    previous = ctx.known_previous_address
    return {
        "identity": {
            "operational_name": identity.operational_name,
            "display_name": identity.display_name,
            "name_source": identity.name_source,
            "name_status": identity.name_status,
            "confidence": identity.confidence,
            "locked_by_merchant": identity.locked_by_merchant,
            "missing_mode": identity.missing_mode,
            "can_use_for_shipping_label": identity.can_use_for_shipping_label,
        },
        "shipping": {
            "city": shipping.city,
            "source": shipping.source,
            "confidence": shipping.confidence,
            "accepted_delivery_address": shipping.accepted_delivery_address,
            "locked_by_merchant": shipping.locked_by_merchant,
            "missing_mode": shipping.missing_mode,
            "requires_merchant_review": shipping.requires_merchant_review,
        },
        "known_previous_address": (
            {
                "city": previous.city,
                "maps_url": previous.maps_url,
                "short_address": previous.short_address,
                "source": previous.source,
            }
            if previous is not None
            else None
        ),
        "suggested_shipping_snapshot": (
            {
                "city": prefill.suggested_shipping_snapshot.city,
                "maps_url": prefill.suggested_shipping_snapshot.maps_url,
                "short_address": prefill.suggested_shipping_snapshot.short_address,
                "source": prefill.suggested_shipping_snapshot.source,
            }
            if prefill.suggested_shipping_snapshot is not None
            else None
        ),
        "shadow_missing_modes": dict(prefill.shadow_missing_modes),
        "customer_requested_edit": prefill.customer_requested_edit,
        "locked_field_edit_requested": prefill.locked_field_edit_requested,
        "requires_merchant_review": prefill.requires_merchant_review,
    }


_CUSTOMER_ASKS_NAME_RE = re.compile(
    r"(?:"
    r"وش\s*اسمي|اسمي\s*(?:ايش|إيش|ك(?:م|يف)?)|"
    r"ت(?:عرف|عرفين|عرفون)\s*اسمي|"
    r"انت\s*عارف\s*اسمي|"
    r"what(?:'s|\s+is)\s*my\s*name"
    r")",
    re.I | re.UNICODE,
)
_CUSTOMER_ASKS_PHONE_RE = re.compile(
    r"(?:"
    r"وش\s*(?:ج(?:وال|وال)|رقم(?:ي|)?)|"
    r"ج(?:وال|وال)(?:ي|)\s*(?:عند(?:كم|ك)|عندنا|كم|ك(?:م|يف)?)|"
    r"رقم(?:ي|)\s*(?:عند(?:كم|ك)|عندنا|كم|ك(?:م|يف)?)|"
    r"what(?:'s|\s+is)\s*my\s*(?:phone|number|mobile)"
    r")",
    re.I | re.UNICODE,
)


def detect_customer_identity_inquiry(message: str = "") -> Dict[str, bool]:
    """Detect when the customer asks for their own known name or phone."""
    text = str(message or "").strip()
    if not text:
        return {}
    out: Dict[str, bool] = {}
    if _CUSTOMER_ASKS_NAME_RE.search(text):
        out["customer_asks_known_name"] = True
    if _CUSTOMER_ASKS_PHONE_RE.search(text):
        out["customer_asks_known_phone"] = True
    return out


def derive_checkout_next_goal(result: Any, prefill: Any) -> str:
    """Map missing-fields engine readiness to a single compose goal token."""
    from core.order_missing_fields_engine import (  # noqa: PLC0415
        READINESS_CONFIRMING_SHIPPING,
        READINESS_READY_FOR_CONFIRMATION,
        READINESS_READY_FOR_PAYMENT,
    )

    if result is None:
        return "collect_next_whatsapp_order_field"

    readiness = str(getattr(result, "readiness_state", "") or "")
    if readiness in {
        READINESS_CONFIRMING_SHIPPING,
        READINESS_READY_FOR_CONFIRMATION,
        READINESS_READY_FOR_PAYMENT,
    }:
        return "confirm_customer_and_shipping_details_once"

    engine_missing = [
        f for f in list(getattr(result, "missing_fields", None) or [])
        if f in {"name", "city", "delivery_address"}
    ]
    if len(engine_missing) == 1:
        field = engine_missing[0]
        if field == "name":
            if prefill.identity_missing_mode == MODE_CONFIRM:
                return "confirm_customer_name_once"
            return "collect_customer_name_only"
        if field == "city":
            if prefill.shipping_city_mode == MODE_CONFIRM:
                return "confirm_city_once"
            return "collect_city_only"
        if field == "delivery_address":
            if prefill.shipping_delivery_mode == MODE_CONFIRM:
                return "confirm_delivery_address_once"
            return "collect_delivery_address_only"

    if not engine_missing:
        return "confirm_customer_and_shipping_details_once"
    return "collect_next_whatsapp_order_field"


def resolve_checkout_missing_fields_legacy(ctx: Any) -> List[str]:
    """Legacy bridge missing fields from OrderContext engine projection."""
    result = getattr(ctx, "missing_fields_result", None)
    if result is None:
        return list(getattr(ctx, "legacy_missing_fields", None) or [])
    from core.order_missing_fields_engine import to_legacy_missing_fields  # noqa: PLC0415

    return list(to_legacy_missing_fields(result))


def build_checkout_compose_facts(
    ctx: Any,
    *,
    message: str = "",
    phone: str = "",
) -> Dict[str, Any]:
    """Operational checkout facts for LLM compose — not customer reply text."""
    facts: Dict[str, Any] = {
        "do_not_ask_phone": True,
        "do_not_repeat_field_confirmations": True,
        "do_not_ask_delivery_method": True,
        "do_not_invent_shipping_fee": True,
    }

    inquiry = detect_customer_identity_inquiry(message)
    facts.update(inquiry)

    if phone:
        facts["phone_mode"] = MODE_SKIP
        facts["known_phone"] = str(phone).strip()
        facts["phone_source"] = "whatsapp_sender"

    identity = ctx.identity
    prefill = ctx.prefill
    shipping = ctx.shipping
    result = getattr(ctx, "missing_fields_result", None)

    name_mode = str(getattr(identity, "missing_mode", "") or prefill.identity_missing_mode)
    operational_name = str(getattr(identity, "operational_name", "") or "").strip()
    confirmation_candidate = str(
        getattr(identity, "confirmation_candidate", "") or ""
    ).strip()
    name_source = str(getattr(identity, "name_source", "") or "").strip()
    name_status = str(getattr(identity, "name_status", "") or "").strip()
    name_confidence = getattr(identity, "confidence", None)

    if name_mode == MODE_SKIP and operational_name:
        facts["name_mode"] = MODE_SKIP
        facts["known_name"] = operational_name
        facts["name_operational"] = True
        facts["name_reason"] = name_source or "known_customer_name"
        if name_source:
            facts["name_source"] = name_source
        if name_status:
            facts["name_status"] = name_status
    elif name_mode == MODE_CONFIRM:
        facts["name_mode"] = MODE_CONFIRM
        facts["name_operational"] = False
        if confirmation_candidate:
            facts["name_confirmation_candidate"] = confirmation_candidate
            facts["name_source"] = name_source or SOURCE_WHATSAPP_PROFILE
            if name_status:
                facts["name_status"] = name_status
            elif getattr(identity, "has_proposed_name", False):
                facts["name_status"] = STATUS_PROPOSED
            if name_confidence is not None:
                facts["name_confidence"] = float(name_confidence or 0.0)
        elif operational_name:
            facts["name_confirmation_candidate"] = operational_name
            facts["name_source"] = name_source or "inferred"
            if name_status:
                facts["name_status"] = name_status
    elif name_mode == MODE_ASK:
        facts["name_mode"] = MODE_ASK
        facts["name_operational"] = False
    elif name_mode == MODE_EDIT_REQUESTED:
        facts["name_mode"] = MODE_EDIT_REQUESTED
        facts["name_operational"] = False

    city_mode = str(prefill.shipping_city_mode or "")
    if shipping.city:
        facts["known_city"] = shipping.city
        facts["city_mode"] = city_mode or MODE_SKIP
    elif city_mode:
        facts["city_mode"] = city_mode

    delivery_mode = str(prefill.shipping_delivery_mode or "")
    if shipping.short_address:
        facts["known_short_address_code"] = shipping.short_address
    if shipping.maps_url:
        facts["known_google_maps_url"] = shipping.maps_url
    if shipping.address_line:
        facts["known_address_text"] = shipping.address_line

    if delivery_mode == MODE_SKIP or shipping.accepted_delivery_address:
        facts["delivery_address_mode"] = MODE_SKIP
    elif delivery_mode == MODE_CONFIRM:
        facts["delivery_address_mode"] = "confirm"
    elif delivery_mode:
        facts["delivery_address_mode"] = delivery_mode

    previous = getattr(ctx, "known_previous_address", None)
    if previous is not None and delivery_mode == MODE_CONFIRM:
        facts["known_previous_address"] = {
            "city": getattr(previous, "city", "") or "",
            "short_address_code": getattr(previous, "short_address", "") or "",
            "google_maps_url": getattr(previous, "maps_url", "") or "",
        }

    missing = resolve_checkout_missing_fields_legacy(ctx)
    facts["missing_fields"] = missing
    facts["next_goal"] = derive_checkout_next_goal(result, prefill)

    draft = getattr(ctx, "active_draft", None)
    if draft is not None and getattr(draft, "line_items", None):
        facts["line_items_known"] = True
        facts["line_items_count"] = len(draft.line_items)
        draft_total = getattr(draft, "total", None)
        if draft_total is not None:
            try:
                total_val = float(draft_total)
                if total_val > 0:
                    facts["order_total_known"] = True
                    facts["order_total"] = total_val
            except (TypeError, ValueError):
                pass

    if result is not None:
        facts["readiness_state"] = str(getattr(result, "readiness_state", "") or "")
        facts["missing_modes"] = dict(getattr(result, "missing_modes", None) or {})

    if facts.get("order_total_known") and facts.get("line_items_known"):
        from core.order_missing_fields_engine import (  # noqa: PLC0415
            READINESS_CONFIRMING_SHIPPING,
            READINESS_READY_FOR_CONFIRMATION,
            READINESS_READY_FOR_PAYMENT,
        )

        readiness = str(facts.get("readiness_state") or "")
        address_ready = bool(
            facts.get("known_short_address_code")
            or facts.get("known_google_maps_url")
            or facts.get("delivery_address_mode") == MODE_SKIP
        )
        shipping_ready = readiness in {
            READINESS_CONFIRMING_SHIPPING,
            READINESS_READY_FOR_CONFIRMATION,
            READINESS_READY_FOR_PAYMENT,
        }
        identity_only = set(missing) <= {
            "name",
            "customer_first_name",
            "customer_last_name",
        }
        if address_ready and (not missing or shipping_ready or identity_only):
            facts["next_goal"] = "confirm_customer_order_and_shipping_details_once"
            facts["ask_confirmation_once"] = True
    elif facts["next_goal"] == "confirm_customer_and_shipping_details_once":
        facts["ask_confirmation_once"] = True

    return facts


_DELIVERY_MISSING_SLOTS = frozenset({
    "delivery_address",
    "address",
    "address_line",
    "short_address_code",
    "google_maps_url",
    "address_location",
})


def _previous_address_as_prep(previous: Any) -> Dict[str, Any]:
    return {
        "city": str(getattr(previous, "city", "") or "").strip(),
        "district": str(getattr(previous, "district", "") or "").strip(),
        "address_line": str(getattr(previous, "address_line", "") or "").strip(),
        "google_maps_url": str(getattr(previous, "maps_url", "") or "").strip(),
        "short_address_code": str(getattr(previous, "short_address", "") or "").strip(),
        "latitude": getattr(previous, "latitude", None),
        "longitude": getattr(previous, "longitude", None),
    }


def checkout_location_evidence_known(order_prep: Optional[Dict[str, Any]] = None) -> bool:
    """True when checkout already has accepted maps/short-code/pin evidence."""
    prep = dict(order_prep or {})
    if has_accepted_delivery_address(prep):
        return True
    if str(prep.get("pending_google_maps_url") or "").strip():
        return True
    pending = prep.get("pending_delivery_location")
    if isinstance(pending, dict):
        if str(
            pending.get("google_maps_url")
            or pending.get("url")
            or pending.get("maps_url")
            or ""
        ).strip():
            return True
        if pending.get("latitude") is not None and pending.get("longitude") is not None:
            return True
    return False


def apply_saved_address_to_checkout_contract(
    *,
    missing_fields: List[str],
    known_facts: Dict[str, Any],
    order_context: Any = None,
    order_prep: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], Dict[str, Any]]:
    """Project canonical saved/checkout address into missing_fields.

    Complete saved facts are not collect slots. Current checkout prep and
    explicit shipping-edit requests still win. Does not persist prep.
    """
    facts = dict(known_facts or {})
    missing = list(missing_fields or [])
    prep = dict(order_prep or {})
    prefill = getattr(order_context, "prefill", None) if order_context is not None else None
    if bool(getattr(prefill, "shipping_edit_requested", False)):
        facts["customer_corrections_applied"] = True
        return missing, facts

    previous = getattr(order_context, "known_previous_address", None) if order_context is not None else None
    hydrated: List[str] = []

    checkout_city = str(prep.get("city") or "").strip()
    saved_city = str(getattr(previous, "city", "") or "").strip() if previous is not None else ""
    if checkout_city or saved_city:
        facts["checkout_city"] = checkout_city or saved_city
        if saved_city:
            facts["saved_city"] = saved_city
            facts["saved_address_available"] = True
            facts["saved_address_source"] = str(
                getattr(previous, "source", "") or "customer_addresses"
            )
        missing = [m for m in missing if m != "city"]
        hydrated.append("city")

    checkout_district = str(prep.get("district") or "").strip()
    saved_district = str(getattr(previous, "district", "") or "").strip() if previous is not None else ""
    if checkout_district or saved_district:
        facts["checkout_district"] = checkout_district or saved_district
        if saved_district:
            facts["saved_district"] = saved_district
        hydrated.append("district")

    prev_prep = _previous_address_as_prep(previous) if previous is not None else {}
    saved_delivery = False
    if previous is not None:
        accepted_flag = getattr(previous, "accepted_delivery_address", None)
        if accepted_flag is None:
            saved_delivery = has_accepted_delivery_address(prev_prep)
        else:
            saved_delivery = bool(accepted_flag)
    checkout_delivery = checkout_location_evidence_known(prep)
    if previous is not None:
        facts["saved_address_available"] = True
        facts["saved_address_source"] = str(
            getattr(previous, "source", "") or "customer_addresses"
        )
        facts["saved_address_complete"] = bool(saved_delivery)
        if str(prev_prep.get("short_address_code") or "").strip():
            facts["saved_national_short_address"] = prev_prep["short_address_code"]
        if str(prev_prep.get("google_maps_url") or "").strip():
            facts["saved_location_link"] = prev_prep["google_maps_url"]
    facts["location_link_persisted"] = bool(
        checkout_location_evidence_known(prep)
        or (
            saved_delivery
            and str(prev_prep.get("google_maps_url") or "").strip()
        )
    )
    if saved_delivery or checkout_delivery:
        missing = [m for m in missing if m not in _DELIVERY_MISSING_SLOTS]
        hydrated.append("delivery_address")

    if hydrated:
        facts["hydrated_fields"] = hydrated
    facts["checkout_missing_fields"] = list(missing)
    return missing, facts


__all__ = [
    "EditIntentFacts",
    "MODE_ASK",
    "MODE_CONFIRM",
    "MODE_EDIT_REQUESTED",
    "MODE_SKIP",
    "OrderPrefillState",
    "apply_order_prep_prefill_patch",
    "build_checkout_compose_facts",
    "build_order_context_api_payload",
    "build_order_prep_prefill_patch",
    "build_prefill_state",
    "compute_shadow_missing_modes",
    "derive_checkout_next_goal",
    "detect_customer_identity_inquiry",
    "detect_edit_intent_facts",
    "resolve_checkout_missing_fields_legacy",
    "enrich_identity_context",
    "enrich_shipping_context",
    "maybe_apply_operational_prefill_to_state",
    "resolve_identity_missing_mode",
    "shadow_missing_fields_from_modes",
    "apply_saved_address_to_checkout_contract",
    "checkout_location_evidence_known",
]
