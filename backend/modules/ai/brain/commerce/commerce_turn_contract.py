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
)
from modules.ai.brain.types import BrainContext, Decision

logger = logging.getLogger("nahla.brain.commerce_turn_contract")

_CATALOG_ORDER_FORBIDDEN = (
    "do_not_browse",
    "do_not_search_products",
    "do_not_ask_product",
    "do_not_show_top_products",
)

_BROWSE_FORBIDDEN_TOKENS = frozenset(_CATALOG_ORDER_FORBIDDEN)

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

_SAME_ORDER_CONFIRM_RE = re.compile(
    r"(?:^|\s)(?:نفس\s*(?:ال)?(?:طلب|طلبي|طلبيتي)|نفسه|نفسها|زي\s*قبل)(?:\s*[\?؟!.]*)?$",
    re.UNICODE | re.IGNORECASE,
)

_ADDRESS_ON_FILE_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:ال)?(?:مدين(?:ة|ه)|العنوان|الموقع|بيانات(?:ك)?).{0,40}(?:عندكم|مسجل|محفوظ|موجود|عندك)"
    r"|(?:عندكم|مسجل|محفوظ|موجود).{0,40}(?:ال)?(?:مدين(?:ة|ه)|العنوان|الموقع)"
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


def _catalog_order_line_item_facts(meta: Dict[str, Any]) -> Dict[str, Any]:
    facts: Dict[str, Any] = {}
    items = meta.get("product_items") or []
    if not isinstance(items, list):
        items = []
    order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
    if not items and isinstance(order.get("product_items"), list):
        items = order["product_items"]
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
        known_facts.update(_catalog_order_line_item_facts(meta))
        missing_fields = [
            m for m in missing_fields
            if m not in {"product", "quantity", "variant"}
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
                if m not in {"product", "quantity", "variant"}
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
        contract.known_facts.get("name")
        or contract.known_facts.get("customer_first_name")
    )
    name_fields = {"customer_first_name", "customer_last_name", "name"}
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
    if not _contract_checkout_enforced(contract):
        return decision

    from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
        catalog_order_continue_checkout_enabled,
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

    catalog_decision = try_active_catalog_checkout_continue_decision(ctx)
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


__all__ = [
    "CommerceTurnContract",
    "attach_commerce_turn_contract",
    "build_commerce_turn_contract",
    "is_address_on_file_claim",
    "is_same_order_confirmation",
    "log_commerce_turn_contract_divergence",
    "maybe_enforce_commerce_turn_contract_decision",
]
