"""
commerce_turn_contract.py
─────────────────────────
Unified pre-decide commerce turn contract.

Phase 1: build + attach + divergence telemetry before DecisionEngine.decide().
Phase 2: enforce catalog_order_current_turn at decide-time (browse → checkout).
Phase 2.5: extend to active catalog checkout continuity on follow-up turns.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from modules.ai.brain.decision.actions import (
    ACTION_CATALOG_NAVIGATE,
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.types import BrainContext, Decision

logger = logging.getLogger("nahla.brain.commerce_turn_contract")

_CATALOG_ORDER_FORBIDDEN = (
    "do_not_browse",
    "do_not_search_products",
    "do_not_ask_product",
    "do_not_ask_quantity",
    "do_not_show_top_products",
)

_BROWSE_FORBIDDEN_TOKENS = frozenset(_CATALOG_ORDER_FORBIDDEN)
_PRODUCT_QUANTITY_FIELDS = frozenset({
    "product",
    "products",
    "product_id",
    "variant",
    "quantity",
    "qty",
})

_BROWSE_OR_SEARCH_ACTIONS = frozenset({
    ACTION_SEARCH_PRODUCTS,
    ACTION_CATALOG_NAVIGATE,
    ACTION_NARROW,
    ACTION_CLARIFY,
})

# Matches catalog_order_checkout._BROWSE_OR_LISTING_ACTIONS — override to checkout.
_CATALOG_ORDER_OVERRIDE_ACTIONS = frozenset({
    ACTION_CATALOG_NAVIGATE,
    ACTION_SEARCH_PRODUCTS,
    ACTION_CLARIFY,
    ACTION_NARROW,
    ACTION_LLM_REPLY,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
})

_PHONE_FIELD_NAMES = frozenset({
    "phone",
    "customer_phone",
    "customer_phone_number",
    "mobile",
})

_NAME_FIELD_NAMES = frozenset({
    "name",
    "full_name",
    "customer_name",
    "customer_first_name",
    "customer_last_name",
})

# Goals that mean "ask the customer for identity" — stale after known identity
# is projected into missing_fields.
_IDENTITY_COLLECT_GOALS = frozenset({
    "collect_customer_name_for_whatsapp_order",
    "collect_customer_name_only",
    "confirm_customer_name_once",
    "collect_phone_for_whatsapp_order",
    "collect_customer_phone",
})

_PRESERVE_NEXT_GOALS = frozenset({
    "existing_order_support",
    "summarize_active_draft_order",
    "confirm_known_address",
    "continue_checkout_from_catalog_order",
})

_ADDRESS_COLLECT_GOALS = frozenset({
    "collect_missing_city",
    "collect_city_for_whatsapp_order",
    "collect_city_only",
    "collect_missing_address",
    "collect_delivery_address_for_whatsapp_order",
    "collect_delivery_address_only",
    "collect_or_confirm_delivery_address",
    "collect_next_whatsapp_order_field",
})

_SAME_ORDER_CONFIRM_RE = re.compile(
    r"(?:^|\s)(?:نفس\s*(?:ال)?(?:طلب|طلبي|طلبيتي)|نفسه|نفسها|زي\s*قبل)(?:\s*[\?؟!.]*)?$",
    re.UNICODE | re.IGNORECASE,
)

_PLACED_ORDER_STATEMENT_RE = re.compile(
    r"(?:"
    r"خلاص\s*طلبت|انا\s*طلبت|أنا\s*طلبت|تم\s*الطلب|"
    r"الطلب\s*موجود|هذا\s*رقم\s*طلبي|سبق\s*(?:و)?طلبت|"
    r"طلبت\s*بالفعل|already\s*ordered|order\s*already\s*placed"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_BARE_ORDER_REF_RE = re.compile(r"^\d{6,12}$")
_LABELED_ORDER_REF_RE = re.compile(
    r"(?:طلب(?:ك|كم)?\s*رقم|رقم\s*(?:ال)?طلب(?:ك|كم)?|order\s*(?:#|number)?)\s*[:#]?\s*(\d{6,12})",
    re.IGNORECASE | re.UNICODE,
)

_BLOCK_NEW_ORDER_ACTIONS = frozenset({
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_CATALOG_NAVIGATE,
    ACTION_NARROW,
    ACTION_CLARIFY,
})

_ADDRESS_ON_FILE_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:ال)?(?:مدين(?:ة|ه)|العنوان|الموقع|بيانات(?:ك)?).{0,40}(?:عندكم|مسجل|محفوظ|موجود|عندك)"
    r"|(?:عندكم|مسجل|محفوظ|موجود|عندك).{0,40}(?:ال)?(?:مدين(?:ة|ه)|العنوان|الموقع)"
    r"|عنوان(?:ي|نا)?\s*(?:ال)?(?:سابق|قديم|اول|الأول)"
    r"|عنوان(?:ي|نا)?\s*(?:عندكم|عندك|مسجل|محفوظ)"
    r"|(?:عندكم|عندك)\s*مسجل(?:ة)?"
    r"|مسجل(?:ة|ه)?\s*عند(?:كم|ك)"
    r"|(?:كل|جميع)\s*بيانات(?:ي|نا)?\s*عند(?:كم|ك)"
    r"|(?:اسم(?:ي|نا)?|عنوان(?:ي|نا)?).{0,30}عند(?:كم|ك)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _norm_msg(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    return re.sub(r"\s+", " ", t).strip()


def _contract_checkout_enforced(contract: CommerceTurnContract) -> bool:
    facts = contract.known_facts
    return bool(
        facts.get("catalog_order_current_turn")
        or facts.get("active_catalog_checkout")
    )


def _contract_existing_order_support_enforced(contract: CommerceTurnContract) -> bool:
    facts = contract.known_facts
    return bool(
        facts.get("placed_order_support_only")
        or facts.get("existing_order_support_only")
    )


def _recent_customer_order_reference(history: Optional[List[Any]]) -> str:
    if not history:
        return ""
    try:
        for turn in reversed(history):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("in", "inbound", ""):
                continue
            body = str((turn or {}).get("body") or "").strip()
            if not body:
                continue
            compact = re.sub(r"\s+", "", body)
            if _BARE_ORDER_REF_RE.match(compact):
                return compact
            labeled = _LABELED_ORDER_REF_RE.search(body)
            if labeled:
                return labeled.group(1)
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _has_existing_order_support_context(ctx: BrainContext) -> bool:
    """True when order evidence exists — availability, not current-turn ownership."""
    state = getattr(ctx, "state", None)
    commerce_bundle = getattr(ctx, "commerce_bundle", None) or {}
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            has_existing_order_evidence,
        )

        if has_existing_order_evidence(
            state=state,
            history=getattr(ctx, "history", None),
            commerce_bundle=commerce_bundle,
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — order evidence probe is best-effort
        pass
    return bool(_recent_customer_order_reference(getattr(ctx, "history", None)))


def _current_turn_existing_order_support_owns(ctx: BrainContext) -> bool:
    """Order evidence may be available without owning catalog/discovery turns.

    Ownership requires current-turn support intent + continuity
    (``should_yield_to_existing_order_support``). Historical orders must not
    block ``search_products`` on an unrelated browse/recommendation turn.
    """
    try:
        from modules.ai.order_flow_v2.explicit_intent_checkout_suppression import (  # noqa: PLC0415
            should_yield_to_existing_order_support,
        )

        profile = getattr(ctx, "profile", None) or {}
        inbound_metadata = (
            profile.get("inbound_metadata")
            if isinstance(profile, dict)
            else {}
        )
        ownership = should_yield_to_existing_order_support(
            getattr(ctx, "message", "") or "",
            inbound_metadata=inbound_metadata if isinstance(inbound_metadata, dict) else {},
            brain_state=getattr(ctx, "state", None),
            history=getattr(ctx, "history", None),
            commerce_bundle=getattr(ctx, "commerce_bundle", None) or {},
        )
        return bool(getattr(ownership, "should_yield", False))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — ownership probe must not block decide
        return False


def decision_owned_by_existing_order_support(decision: Decision) -> bool:
    if str(getattr(decision, "action", "") or "") != ACTION_LLM_REPLY:
        return False
    args = getattr(decision, "args", None) or {}
    return str(args.get("topic") or "").strip() == "existing_order_support"


def order_support_reply_protected(
    *,
    decision_action: str = "",
    decision_args: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when post-decision layers must not hijack into checkout continuation."""
    action = str(decision_action or "")
    args = decision_args if isinstance(decision_args, dict) else {}
    if decision_owned_by_existing_order_support(
        Decision(action=action, args=args, reason="probe"),
    ):
        return True
    return action == ACTION_TRACK_ORDER


def _annotate_contract_ownership_preserved(decision: Decision) -> Decision:
    args = dict(getattr(decision, "args", None) or {})
    action = str(getattr(decision, "action", "") or "")
    args["pre_contract_action"] = action
    args["post_contract_action"] = action
    args["contract_override_applied"] = False
    args["override_skipped_reason"] = "existing_order_support_owned"
    return Decision(
        action=decision.action,
        args=args,
        reason=decision.reason,
        confidence=getattr(decision, "confidence", 1.0),
    )


def _build_existing_order_support_decision(
    ctx: BrainContext,
    *,
    reason: str,
) -> Decision:
    ref = _recent_customer_order_reference(getattr(ctx, "history", None))
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "existing_order_support",
            "order_reference": ref or None,
            "order_verified": False,
            "response_goal": (
                "existing_order_support — customer is discussing an order they "
                "already placed or a prior order reference. Stay in order-support "
                "context. Do NOT collect new products, quantities, or checkout "
                "fields. Do NOT create a duplicate order or open catalog browsing."
            ),
        },
        reason=reason,
        confidence=0.94,
    )


def _derive_active_checkout_next_goal(
    message: str,
    missing_fields: Sequence[str],
) -> str:
    from modules.ai.brain.commerce.current_order_amount import is_current_order_inquiry  # noqa: PLC0415

    msg = str(message or "")
    if is_current_order_inquiry(msg):
        return "summarize_active_draft_order"
    if _SAME_ORDER_CONFIRM_RE.search(_norm_msg(msg)):
        return "continue_checkout"
    if _ADDRESS_ON_FILE_CLAIM_RE.search(msg):
        return "confirm_known_address"
    if missing_fields:
        first = missing_fields[0]
        if first in {"city"}:
            return "collect_missing_city"
        if first in {"delivery_address", "address", "address_line", "short_address_code"}:
            return "collect_missing_address"
        if first in {"customer_first_name", "customer_last_name", "name"}:
            return "collect_customer_name_for_whatsapp_order"
        if first == "payment_method":
            return "collect_payment_method_for_whatsapp_order"
    return "continue_checkout"


def _resolve_active_checkout_known_facts(
    ctx: BrainContext,
    *,
    order_context: Any = None,
) -> Dict[str, Any]:
    from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
        is_active_catalog_checkout,
    )

    if not is_active_catalog_checkout(ctx):
        return {}

    facts: Dict[str, Any] = {
        "active_catalog_checkout": True,
        "checkout_owner_active": True,
    }
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None) if state else None
    prep_d: Dict[str, Any] = {}
    if prep is not None:
        if hasattr(prep, "to_dict"):
            try:
                prep_d = dict(prep.to_dict())
            except Exception:  # noqa: BLE001
                prep_d = {}
        elif isinstance(prep, dict):
            prep_d = dict(prep)
        else:
            prep_d = dict(getattr(prep, "__dict__", {}) or {})

    line_items = list(prep_d.get("line_items") or [])
    if line_items:
        facts["line_items_known"] = True
        first_item = next((li for li in line_items if isinstance(li, dict)), None)
        if first_item:
            selected_id = str(
                first_item.get("product_id")
                or first_item.get("product_retailer_id")
                or ""
            ).strip()
            if selected_id:
                facts["selected_product_id"] = selected_id
            source = str(first_item.get("source") or "").strip()
            if prep_d.get("catalog_line_items_authoritative"):
                facts["selected_product_source"] = source or "whatsapp_catalog"
            elif source:
                facts["selected_product_source"] = source
        titles = [
            str(li.get("product_name") or li.get("title") or "").strip()
            for li in line_items
            if isinstance(li, dict)
        ]
        titles = [t for t in titles if t]
        if titles:
            facts["product_titles"] = titles
    if prep_d.get("catalog_checkout_total") is not None:
        facts["catalog_total"] = prep_d.get("catalog_checkout_total")
    if order_context is not None and getattr(order_context, "active_draft", None) is not None:
        facts["active_draft_exists"] = True
    return facts


@dataclass
class CommerceTurnContract:
    """Pre-decide commerce projection — facts and constraints, never reply text."""

    commerce_state: str
    known_facts: Dict[str, Any] = field(default_factory=dict)
    missing_fields: List[str] = field(default_factory=list)
    next_goal: Optional[str] = None
    allowed_actions: List[str] = field(default_factory=list)
    forbidden_actions: List[str] = field(default_factory=list)
    action_to_execute: Optional[str] = None
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commerce_state": self.commerce_state,
            "known_facts": dict(self.known_facts),
            "missing_fields": list(self.missing_fields),
            "next_goal": self.next_goal,
            "allowed_actions": list(self.allowed_actions),
            "forbidden_actions": list(self.forbidden_actions),
            "action_to_execute": self.action_to_execute,
            "reasons": list(self.reasons),
        }


def attach_commerce_turn_contract(ctx: BrainContext, contract: CommerceTurnContract) -> None:
    """Store contract on BrainContext for downstream compose/guards (read-only)."""
    ctx.commerce_turn_contract = contract
    profile = getattr(ctx, "profile", None)
    if isinstance(profile, dict):
        profile["commerce_turn_contract"] = contract.to_dict()


def canonical_checkout_next_slot(ctx: BrainContext) -> tuple[List[str], str]:
    """Return ``(missing_fields, next_missing_field)`` from the pre-decide contract.

    Falls back to empty/none when no checkout contract is attached.
    """
    contract = getattr(ctx, "commerce_turn_contract", None)
    if contract is None:
        return [], "none"
    missing = list(getattr(contract, "missing_fields", None) or [])
    facts = dict(getattr(contract, "known_facts", None) or {})
    nxt = str(facts.get("next_missing_field") or "").strip() or "none"
    if nxt == "none":
        try:
            from modules.ai.order_flow_v2.missing_fields import next_missing_field  # noqa: PLC0415

            nxt = next_missing_field(missing) or "none"
        except Exception:  # noqa: BLE001
            nxt = "none"
    return missing, nxt


def _inbound_metadata(ctx: BrainContext) -> Dict[str, Any]:
    profile = getattr(ctx, "profile", None)
    if not isinstance(profile, dict):
        return {}
    meta = profile.get("inbound_metadata") or {}
    return dict(meta) if isinstance(meta, dict) else {}


def _load_order_context_for_contract(ctx: BrainContext, db: Any) -> Any:
    """Best-effort OrderContext — same sources as compose-time loader."""
    if db is None:
        return None
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    if not tenant_id:
        return None
    try:
        from dataclasses import asdict, is_dataclass

        from core.order_context_builder import build_order_context

        state = getattr(ctx, "state", None)
        customer = None
        conversation = None
        customer_id = getattr(ctx, "customer_id", None)
        conversation_id = getattr(ctx, "conversation_id", None)
        if customer_id:
            from models import Conversation, Customer  # noqa: PLC0415

            customer = (
                db.query(Customer)
                .filter_by(id=int(customer_id), tenant_id=tenant_id)
                .first()
            )
            if conversation_id:
                conversation = (
                    db.query(Conversation)
                    .filter_by(id=int(conversation_id), tenant_id=tenant_id)
                    .first()
                )
        prep: Dict[str, Any] = {}
        order_prep = getattr(state, "order_prep", None) if state else None
        if order_prep is not None:
            if is_dataclass(order_prep):
                prep = asdict(order_prep)
            elif isinstance(order_prep, dict):
                prep = dict(order_prep)
            else:
                prep = dict(getattr(order_prep, "__dict__", {}) or {})

        return build_order_context(
            db,
            tenant_id=tenant_id,
            conversation=conversation,
            customer=customer,
            phone=str(getattr(ctx, "customer_phone", "") or ""),
            brain_state={"order_prep": prep},
            inbound_metadata=_inbound_metadata(ctx),
            message=str(getattr(ctx, "message", "") or ""),
            build_source="commerce_turn_contract_pre_decide",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[COMMERCE_TURN_CONTRACT] order_context skipped tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return None


def _catalog_order_line_item_facts(meta: Dict[str, Any], message: str = "") -> Dict[str, Any]:
    facts: Dict[str, Any] = {}
    items = meta.get("product_items") or []
    if not isinstance(items, list):
        items = []
    order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
    if not items and isinstance(order.get("product_items"), list):
        items = order["product_items"]
    try:
        from core.wa_native_catalog_order import extract_catalog_order_text_facts  # noqa: PLC0415

        text_facts = extract_catalog_order_text_facts(message)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — text fact extraction is best-effort
        text_facts = {}
    if text_facts.get("catalog_order_line_count"):
        facts["catalog_order_line_count"] = text_facts.get("catalog_order_line_count")
    if text_facts.get("catalog_total") is not None:
        facts["catalog_total"] = text_facts.get("catalog_total")
    if text_facts.get("catalog_currency"):
        facts["catalog_currency"] = text_facts.get("catalog_currency")
    if text_facts.get("catalog_skus"):
        facts["catalog_skus"] = list(text_facts.get("catalog_skus") or [])
    if text_facts.get("total_quantity"):
        facts["quantity_known"] = True
        facts["quantity"] = int(text_facts.get("total_quantity") or 0)
    if items:
        facts["line_items_known"] = True
        titles: List[str] = []
        names = meta.get("product_names")
        if isinstance(names, list):
            titles = [str(n).strip() for n in names if str(n).strip()]
        if not titles:
            for item in items:
                if isinstance(item, dict):
                    title = str(item.get("product_name") or item.get("name") or "").strip()
                    if title:
                        titles.append(title)
        if titles:
            facts["product_titles"] = titles
        qty = meta.get("total_quantity")
        if qty is None and len(items) == 1 and isinstance(items[0], dict):
            qty = items[0].get("quantity")
        if qty is not None:
            try:
                if int(qty) > 0:
                    facts["quantity_known"] = True
                    facts["quantity"] = int(qty)
            except (TypeError, ValueError):
                pass
        elif items:
            facts["quantity_known"] = True
        total = meta.get("total_price")
        if total is not None:
            facts["catalog_total"] = total
        skus = [
            str(item.get("product_retailer_id") or item.get("sku") or "").strip()
            for item in items
            if isinstance(item, dict)
        ]
        skus = [sku for sku in skus if sku]
        if skus:
            facts["catalog_skus"] = skus
    return facts


def _merge_forbidden(*groups: Sequence[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for group in groups:
        for token in group:
            if token and token not in seen:
                seen.add(token)
                out.append(token)
    return out


def _filter_phone_from_missing(
    missing: Sequence[str],
    *,
    phone_known: bool,
) -> List[str]:
    if not phone_known:
        return list(missing)
    return [m for m in missing if m not in _PHONE_FIELD_NAMES]


def _identity_collect_goal_is_stale(
    next_goal: Optional[str],
    missing_fields: Sequence[str],
) -> bool:
    """True when next_goal still asks for a name/phone slot that is no longer missing."""
    goal = str(next_goal or "").strip()
    if not goal or goal in _PRESERVE_NEXT_GOALS:
        return False
    if goal in _IDENTITY_COLLECT_GOALS:
        if "phone" in goal or "mobile" in goal:
            return not any(m in _PHONE_FIELD_NAMES for m in missing_fields)
        return not any(m in _NAME_FIELD_NAMES for m in missing_fields)
    return False


def _address_collect_goal_is_stale(
    next_goal: Optional[str],
    missing_fields: Sequence[str],
) -> bool:
    goal = str(next_goal or "").strip()
    if not goal or goal in _PRESERVE_NEXT_GOALS:
        return False
    if goal not in _ADDRESS_COLLECT_GOALS:
        return False
    address_slots = {"city"} | {
        "delivery_address",
        "address",
        "address_line",
        "short_address_code",
        "google_maps_url",
        "address_location",
    }
    if goal in {"collect_missing_city", "collect_city_for_whatsapp_order", "collect_city_only"}:
        return "city" not in missing_fields
    if "address" in goal:
        return not any(m in address_slots - {"city"} for m in missing_fields)
    # Generic collect-next: stale only when no address/city slots remain.
    return not any(m in address_slots for m in missing_fields)


def build_commerce_turn_contract(
    ctx: BrainContext,
    *,
    db: Any = None,
) -> CommerceTurnContract:
    """Build a unified pre-decide commerce contract from existing projections."""
    reasons: List[str] = []
    intent = getattr(ctx, "intent", None)
    intent_name = str(getattr(intent, "name", "") or "")
    intent_slots = dict(getattr(intent, "slots", None) or {})
    state = getattr(ctx, "state", None)
    order_prep = getattr(state, "order_prep", None) if state else None
    stage = str(getattr(state, "stage", "") or "")
    meta = _inbound_metadata(ctx)
    phone = str(getattr(ctx, "customer_phone", "") or "").strip()
    phone_known = bool(phone)

    order_context = _load_order_context_for_contract(ctx, db)

    from modules.ai.brain.commerce.commerce_navigator import resolve_commerce_navigator  # noqa: PLC0415

    nav = resolve_commerce_navigator(
        message=str(getattr(ctx, "message", "") or ""),
        intent_name=intent_name,
        intent_slots=intent_slots,
        decision_topic="",
        stage=stage,
        order_prep=order_prep,
        state=state,
        inbound_metadata=meta,
        whatsapp_phone=phone,
        order_context=order_context,
    )
    reasons.append(f"navigator:{nav.reason}")

    known_facts: Dict[str, Any] = dict(nav.known_fields)
    missing_fields = _filter_phone_from_missing(
        list(nav.missing_fields),
        phone_known=phone_known,
    )
    forbidden_actions = list(nav.forbidden_actions)
    commerce_state = str(nav.stage)
    next_goal: Optional[str] = nav.next_goal
    allowed_actions: List[str] = []
    action_to_execute: Optional[str] = None

    if phone_known:
        known_facts["phone_known"] = True
        known_facts["phone_source"] = "whatsapp"

    msg = str(getattr(ctx, "message", "") or "")
    if is_placed_order_statement(msg):
        known_facts["placed_order_support_only"] = True
        commerce_state = "existing_order_support"
        next_goal = "existing_order_support"
        forbidden_actions = _merge_forbidden(
            forbidden_actions,
            _CATALOG_ORDER_FORBIDDEN,
        )
        reasons.append("placed_order_statement")
    elif _current_turn_existing_order_support_owns(ctx):
        known_facts["existing_order_support_only"] = True
        known_facts["existing_order_evidence_available"] = _has_existing_order_support_context(
            ctx,
        )
        commerce_state = "existing_order_support"
        next_goal = "existing_order_support"
        forbidden_actions = _merge_forbidden(
            forbidden_actions,
            _CATALOG_ORDER_FORBIDDEN,
        )
        reasons.append("existing_order_support_current_turn")
    elif _has_existing_order_support_context(ctx):
        known_facts["existing_order_evidence_available"] = True
        reasons.append("existing_order_evidence_available_not_owning")

    from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
        is_current_catalog_order_submitted,
        try_catalog_order_continue_decision,
    )

    catalog_order_current_turn = is_current_catalog_order_submitted(ctx)
    if catalog_order_current_turn:
        known_facts["catalog_order_current_turn"] = True
        reasons.append("current_turn_whatsapp_catalog_order")
        commerce_state = "whatsapp_quick_order"
        next_goal = "continue_checkout_from_catalog_order"
        forbidden_actions = _merge_forbidden(
            forbidden_actions,
            _CATALOG_ORDER_FORBIDDEN,
        )
        known_facts.update(_catalog_order_line_item_facts(
            meta,
            str(getattr(ctx, "message", "") or ""),
        ))
        missing_fields = [
            m for m in missing_fields
            if m not in _PRODUCT_QUANTITY_FIELDS
        ]
        allowed_actions = [ACTION_PROPOSE_DRAFT_ORDER, "llm_compose"]
        catalog_decision = try_catalog_order_continue_decision(ctx)
        if catalog_decision is not None:
            action_to_execute = catalog_decision.action
            reasons.append("catalog_order_continue_checkout_candidate")
    else:
        active_facts = _resolve_active_checkout_known_facts(ctx, order_context=order_context)
        if active_facts.get("active_catalog_checkout"):
            known_facts.update(active_facts)
            reasons.append("active_catalog_checkout_session")
            commerce_state = "whatsapp_quick_order"
            forbidden_actions = _merge_forbidden(
                forbidden_actions,
                _CATALOG_ORDER_FORBIDDEN,
            )
            missing_fields = [
                m for m in missing_fields
                if m not in _PRODUCT_QUANTITY_FIELDS
            ]
            next_goal = _derive_active_checkout_next_goal(
                str(getattr(ctx, "message", "") or ""),
                missing_fields,
            )
            allowed_actions = [ACTION_PROPOSE_DRAFT_ORDER, "llm_compose"]
            from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
                try_active_catalog_checkout_continue_decision,
            )

            active_decision = try_active_catalog_checkout_continue_decision(ctx)
            if active_decision is not None:
                action_to_execute = active_decision.action
                reasons.append("active_catalog_checkout_continue_candidate")
        elif commerce_state == "whatsapp_quick_order":
            allowed_actions = [ACTION_PROPOSE_DRAFT_ORDER, "llm_compose"]
        elif commerce_state == "browse":
            allowed_actions = [
                ACTION_SEARCH_PRODUCTS,
                ACTION_CATALOG_NAVIGATE,
                ACTION_CLARIFY,
                "llm_compose",
            ]

    # Phase 2 TODO: wire MissingFieldsEngine enforce when ORDER_MISSING_FIELDS_ENGINE_ENABLED.
    if order_context is not None:
        shadow_missing = getattr(order_context, "shadow_missing_fields", None)
        if isinstance(shadow_missing, list) and shadow_missing:
            reasons.append("order_context_shadow_missing_fields_available")

    apply_identity = bool(
        known_facts.get("catalog_order_current_turn")
        or known_facts.get("active_catalog_checkout")
        or commerce_state == "whatsapp_quick_order"
    )
    if apply_identity:
        from dataclasses import asdict, is_dataclass  # noqa: PLC0415

        prep_d: Dict[str, Any] = {}
        order_prep_obj = getattr(state, "order_prep", None) if state else None
        if order_prep_obj is not None:
            if is_dataclass(order_prep_obj):
                prep_d = asdict(order_prep_obj)
            elif isinstance(order_prep_obj, dict):
                prep_d = dict(order_prep_obj)
        try:
            from modules.ai.order_flow_v2.missing_fields import (  # noqa: PLC0415
                compute_v2_missing_fields,
            )

            brain_state: Dict[str, Any] = {}
            if state is not None:
                focus = getattr(state, "current_product_focus", None)
                if isinstance(focus, dict) and focus:
                    brain_state["current_product_focus"] = focus
            v2_missing = compute_v2_missing_fields(
                prep_d,
                brain_state=brain_state or None,
                whatsapp_phone=phone or None,
                db=db,
                tenant_id=int(getattr(ctx, "tenant_id", 0) or 0) or None,
                inbound_metadata=meta,
            )
            missing_fields = _filter_phone_from_missing(
                list(v2_missing),
                phone_known=phone_known,
            )
            reasons.append("canonical_v2_missing_fields_owner")
        except Exception:  # noqa: BLE001
            logger.exception(
                "[COMMERCE_TURN_CONTRACT] canonical missing-fields owner failed tenant=%s",
                getattr(ctx, "tenant_id", None),
            )
        customer_row = None
        if order_context is not None and getattr(order_context, "customer_id", None) and db is not None:
            try:
                from models import Customer  # noqa: PLC0415

                customer_row = (
                    db.query(Customer)
                    .filter_by(id=int(order_context.customer_id), tenant_id=int(ctx.tenant_id))
                    .first()
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — contract identity lookup is best-effort
                customer_row = None
        try:
            from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
                apply_catalog_customer_identity_to_contract,
            )

            missing_fields, known_facts = apply_catalog_customer_identity_to_contract(
                missing_fields=list(missing_fields),
                known_facts=dict(known_facts),
                db=db,
                tenant_id=int(getattr(ctx, "tenant_id", 0) or 0) or None,
                phone=phone,
                order_prep=prep_d,
                profile=dict(getattr(ctx, "profile", None) or {}),
                customer=customer_row,
            )
            if known_facts.get("customer_name_known"):
                reasons.append("catalog_checkout_customer_name_known")
            if _identity_collect_goal_is_stale(next_goal, missing_fields):
                next_goal = _derive_active_checkout_next_goal(
                    str(getattr(ctx, "message", "") or ""),
                    missing_fields,
                )
                reasons.append("identity_next_goal_refreshed_after_known_facts")
        except Exception:  # noqa: BLE001
            logger.exception(
                "[COMMERCE_TURN_CONTRACT] catalog customer identity apply failed tenant=%s",
                getattr(ctx, "tenant_id", None),
            )
        try:
            from core.order_context_prefill import (  # noqa: PLC0415
                apply_saved_address_to_checkout_contract,
                checkout_location_evidence_known,
            )

            goal_before = next_goal
            missing_fields, known_facts = apply_saved_address_to_checkout_contract(
                missing_fields=list(missing_fields),
                known_facts=dict(known_facts),
                order_context=order_context,
                order_prep=prep_d,
            )
            known_facts["next_goal_before_hydration"] = goal_before
            from modules.ai.order_flow_v2.missing_fields import (  # noqa: PLC0415
                next_missing_field,
            )

            location_known = checkout_location_evidence_known(prep_d)
            known_facts["checkout_location_evidence_known"] = location_known
            known_facts["next_missing_field"] = next_missing_field(missing_fields) or "none"
            if location_known and (
                next_goal in _ADDRESS_COLLECT_GOALS
                or next_goal == "confirm_known_address"
                or next_goal in {
                    "continue_checkout",
                    "collect_next_whatsapp_order_field",
                }
            ):
                next_goal = _derive_active_checkout_next_goal(
                    str(getattr(ctx, "message", "") or ""),
                    missing_fields,
                )
                reasons.append("accepted_location_outranks_stale_address_goal")
            elif _address_collect_goal_is_stale(next_goal, missing_fields):
                if (
                    known_facts.get("saved_address_complete")
                    and not prep_d.get("customer_confirmed_previous_address")
                    and not location_known
                ):
                    next_goal = "confirm_known_address"
                else:
                    next_goal = _derive_active_checkout_next_goal(
                        str(getattr(ctx, "message", "") or ""),
                        missing_fields,
                    )
                reasons.append("saved_address_next_goal_refreshed_after_hydration")
            known_facts["next_goal_after_hydration"] = next_goal
            known_facts["checkout_missing_fields"] = list(missing_fields)
            known_facts["next_missing_field"] = next_missing_field(missing_fields) or "none"
        except Exception:  # noqa: BLE001
            logger.exception(
                "[COMMERCE_TURN_CONTRACT] saved address apply failed tenant=%s",
                getattr(ctx, "tenant_id", None),
            )

    return CommerceTurnContract(
        commerce_state=commerce_state,
        known_facts=known_facts,
        missing_fields=missing_fields,
        next_goal=next_goal,
        allowed_actions=allowed_actions,
        forbidden_actions=forbidden_actions,
        action_to_execute=action_to_execute,
        reasons=reasons,
    )


def log_commerce_turn_contract_divergence(
    contract: CommerceTurnContract,
    decision: Decision,
    *,
    ctx: Optional[BrainContext] = None,
    phase: str = "post_decide",
) -> List[str]:
    """
    Log contract vs decision mismatches (Phase 1 telemetry only).

    Returns divergence reason keys recorded this call.
    """
    divergences: List[str] = []
    forbidden = set(contract.forbidden_actions)
    action = str(getattr(decision, "action", "") or "")

    if forbidden & _BROWSE_FORBIDDEN_TOKENS and action in _BROWSE_OR_SEARCH_ACTIONS:
        divergences.append("contract_forbids_browse_but_decision_is_browse_or_search")
        logger.warning(
            "[COMMERCE_TURN_CONTRACT/divergence] phase=%s kind=browse_vs_forbidden "
            "tenant=%s action=%s forbidden=%s state=%s goal=%s",
            phase,
            getattr(ctx, "tenant_id", None) if ctx else None,
            action,
            sorted(forbidden & _BROWSE_FORBIDDEN_TOKENS),
            contract.commerce_state,
            contract.next_goal,
        )

    if contract.known_facts.get("catalog_order_current_turn") and action in _BROWSE_OR_SEARCH_ACTIONS:
        divergences.append("catalog_order_current_turn_but_decision_is_browse_or_search")
        logger.warning(
            "[COMMERCE_TURN_CONTRACT/divergence] phase=%s kind=catalog_order_vs_browse "
            "tenant=%s action=%s goal=%s",
            phase,
            getattr(ctx, "tenant_id", None) if ctx else None,
            action,
            contract.next_goal,
        )

    if contract.known_facts.get("active_catalog_checkout") and action in _BROWSE_OR_SEARCH_ACTIONS:
        divergences.append("active_catalog_checkout_but_decision_is_browse_or_search")
        logger.warning(
            "[COMMERCE_TURN_CONTRACT/divergence] phase=%s kind=active_checkout_vs_browse "
            "tenant=%s action=%s goal=%s",
            phase,
            getattr(ctx, "tenant_id", None) if ctx else None,
            action,
            contract.next_goal,
        )

    if contract.known_facts.get("phone_known"):
        phone_missing = [m for m in contract.missing_fields if m in _PHONE_FIELD_NAMES]
        if phone_missing:
            divergences.append("phone_known_but_missing_fields_contains_phone")
            logger.warning(
                "[COMMERCE_TURN_CONTRACT/divergence] phase=%s kind=phone_known_vs_missing "
                "tenant=%s missing=%s",
                phase,
                getattr(ctx, "tenant_id", None) if ctx else None,
                phone_missing,
            )

    name_known = bool(
        contract.known_facts.get("customer_name_known")
        or contract.known_facts.get("name")
        or contract.known_facts.get("customer_first_name")
    )
    name_fields = {
        "customer_first_name",
        "customer_last_name",
        "name",
        "full_name",
        "customer_name",
    }
    if name_known and any(m in name_fields for m in contract.missing_fields):
        divergences.append("name_known_but_missing_fields_contains_name")
        logger.warning(
            "[COMMERCE_TURN_CONTRACT/divergence] phase=%s kind=name_known_vs_missing "
            "tenant=%s missing=%s known_name=%r",
            phase,
            getattr(ctx, "tenant_id", None) if ctx else None,
            [m for m in contract.missing_fields if m in name_fields],
            contract.known_facts.get("name"),
        )

    if divergences:
        logger.info(
            "[COMMERCE_TURN_CONTRACT/divergence] phase=%s tenant=%s keys=%s "
            "decision_action=%s contract_state=%s",
            phase,
            getattr(ctx, "tenant_id", None) if ctx else None,
            divergences,
            action,
            contract.commerce_state,
        )
    return divergences


def maybe_enforce_commerce_turn_contract_decision(
    ctx: BrainContext,
    contract: CommerceTurnContract,
    decision: Decision,
) -> Decision:
    """
    Phase 2 — when contract marks catalog order or active catalog checkout, override
    browse / discovery / LLM-fallback decisions into checkout continuation.
    """
    if _contract_existing_order_support_enforced(contract):
        raw_action = str(getattr(decision, "action", "") or "")
        if raw_action in _BLOCK_NEW_ORDER_ACTIONS:
            enforced = _build_existing_order_support_decision(
                ctx,
                reason="commerce_turn_contract — block new-order continuation",
            )
            logger.info(
                "[COMMERCE_TURN_CONTRACT/enforce] "
                "event=contract_enforced_existing_order_support "
                "tenant=%s raw_action=%s enforced_action=%s",
                getattr(ctx, "tenant_id", None),
                raw_action,
                enforced.action,
            )
            return enforced

    if decision_owned_by_existing_order_support(decision):
        preserved = _annotate_contract_ownership_preserved(decision)
        logger.info(
            "[COMMERCE_TURN_CONTRACT/enforce] "
            "event=contract_preserve_existing_order_support "
            "tenant=%s pre_contract_action=%s post_contract_action=%s "
            "override_skipped_reason=existing_order_support_owned",
            getattr(ctx, "tenant_id", None),
            preserved.args.get("pre_contract_action"),
            preserved.args.get("post_contract_action"),
        )
        return preserved

    if not _contract_checkout_enforced(contract):
        return decision

    from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
        catalog_order_continue_checkout_enabled,
        try_catalog_order_continue_decision,
        maybe_enforce_catalog_order_continue_checkout,
        try_active_catalog_checkout_continue_decision,
    )

    if not catalog_order_continue_checkout_enabled():
        return decision

    raw_action = str(getattr(decision, "action", "") or "")

    if raw_action == ACTION_PROPOSE_DRAFT_ORDER:
        enforced = maybe_enforce_catalog_order_continue_checkout(ctx, decision)
        if enforced.action != raw_action or enforced.args != decision.args:
            logger.info(
                "[COMMERCE_TURN_CONTRACT/enforce] "
                "event=contract_enforced_catalog_order_over_browse "
                "tenant=%s raw_action=%s enforced_action=%s next_goal=%s",
                getattr(ctx, "tenant_id", None),
                raw_action,
                enforced.action,
                contract.next_goal,
            )
        return enforced

    if raw_action not in _CATALOG_ORDER_OVERRIDE_ACTIONS:
        return decision

    catalog_decision = (
        try_catalog_order_continue_decision(ctx)
        if contract.known_facts.get("catalog_order_current_turn")
        else try_active_catalog_checkout_continue_decision(ctx)
    )
    if catalog_decision is None:
        logger.warning(
            "[COMMERCE_TURN_CONTRACT/enforce] catalog_order override skipped "
            "tenant=%s raw_action=%s reason=no_checkout_candidate",
            getattr(ctx, "tenant_id", None),
            raw_action,
        )
        return decision

    logger.info(
        "[COMMERCE_TURN_CONTRACT/enforce] "
        "event=contract_enforced_catalog_order_over_browse "
        "tenant=%s raw_action=%s enforced_action=%s next_goal=%s",
        getattr(ctx, "tenant_id", None),
        raw_action,
        catalog_decision.action,
        contract.next_goal,
    )
    return catalog_decision


def is_address_on_file_claim(message: str) -> bool:
    return bool(_ADDRESS_ON_FILE_CLAIM_RE.search(str(message or "")))


def is_same_order_confirmation(message: str) -> bool:
    return bool(_SAME_ORDER_CONFIRM_RE.search(_norm_msg(message)))


def is_placed_order_statement(message: str) -> bool:
    """Customer states an order was already placed — block duplicate checkout."""
    return bool(_PLACED_ORDER_STATEMENT_RE.search(_norm_msg(message)))


__all__ = [
    "CommerceTurnContract",
    "attach_commerce_turn_contract",
    "build_commerce_turn_contract",
    "canonical_checkout_next_slot",
    "decision_owned_by_existing_order_support",
    "is_address_on_file_claim",
    "is_placed_order_statement",
    "is_same_order_confirmation",
    "log_commerce_turn_contract_divergence",
    "maybe_enforce_commerce_turn_contract_decision",
    "order_support_reply_protected",
]
