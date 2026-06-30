"""
services/ai_playground_dry_run.py
──────────────────────────────────
Stateless AI Playground preview — decision + FakeFacts compose only.

No webhook, no MerchantBrain.process, no DB writes, no WhatsApp send.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from core.ai_disabled_gate import REASON_STORE_AI_DISABLED, is_store_ai_enabled
from core.billing import has_billing_access
from core.payment_intent import looks_like_delivery_confirmation
from modules.ai.brain.commerce.non_catalog_availability_kb_route import (
    TOPIC_KB_AVAILABILITY_FACTS,
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.commerce.order_tracking_intent_guard import (
    is_explicit_order_tracking_request,
)
from modules.ai.brain.commerce.product_knowledge_or_comparison import (
    TOPIC_PRODUCT_KNOWLEDGE_FACTS,
    try_product_knowledge_decision,
)
from modules.ai.brain.decision.actions import (
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.facts.commerce_facts import DefaultFactsLoader
from modules.ai.brain.intent import rules
from modules.ai.brain.turn_owner_contract import build_turn_owner_contract
from modules.ai.brain.types import (
    BrainContext,
    Decision,
    Intent,
    MerchantConversationState,
)

BLOCKED_BILLING_DENIED = "billing_denied"
OUTBOUND_NONE = "none"
OUTBOUND_SESSION_TEXT = "session_text"

PLAYGROUND_PHONE = "+966500000099"
PLAYGROUND_CUSTOMER_ID = 0
PLAYGROUND_CONVERSATION_ID = 0


@dataclass(frozen=True)
class PlaygroundOrderContext:
    order_status: str = "shipped"
    order_reference: str = ""
    tracking_number: str = ""
    shipping_provider: str = ""

    @classmethod
    def from_payload(cls, raw: Optional[Mapping[str, Any]]) -> Optional["PlaygroundOrderContext"]:
        if not raw:
            return None
        return cls(
            order_status=str(raw.get("order_status") or "shipped").strip() or "shipped",
            order_reference=str(
                raw.get("order_reference") or raw.get("order_id") or ""
            ).strip(),
            tracking_number=str(raw.get("tracking_number") or "").strip(),
            shipping_provider=str(raw.get("shipping_provider") or "").strip(),
        )


@dataclass
class PlaygroundDryRunResult:
    ok: bool = True
    dry_run: bool = True
    would_send: bool = False
    outbound_kind: str = OUTBOUND_NONE
    reply_text: Optional[str] = None
    blocked_reason: Optional[str] = None
    used_llm: bool = False
    decision_topic: Optional[str] = None
    decision_action: Optional[str] = None
    owner: Optional[str] = None
    facts: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    needs_context: bool = False
    side_effects: Dict[str, bool] = field(default_factory=lambda: {
        "whatsapp_sent": False,
        "order_created": False,
        "customer_updated": False,
        "automation_triggered": False,
    })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "would_send": self.would_send,
            "outbound_kind": self.outbound_kind,
            "reply_text": self.reply_text,
            "blocked_reason": self.blocked_reason,
            "used_llm": self.used_llm,
            "decision_topic": self.decision_topic,
            "decision_action": self.decision_action,
            "owner": self.owner,
            "facts": self.facts,
            "warnings": self.warnings,
            "needs_context": self.needs_context,
            "side_effects": dict(self.side_effects),
        }


def _empty_side_effects() -> Dict[str, bool]:
    return {
        "whatsapp_sent": False,
        "order_created": False,
        "customer_updated": False,
        "automation_triggered": False,
    }


def build_playground_commerce_bundle(
    context: Optional[PlaygroundOrderContext],
) -> Dict[str, Any]:
    """In-memory order context for tracking preview — never persisted."""
    if context is None:
        return {}
    if not any(
        (
            context.order_reference,
            context.tracking_number,
            context.shipping_provider,
        )
    ):
        return {}

    active: Dict[str, Any] = {
        "order_id": context.order_reference or None,
        "order_status": context.order_status,
        "raw_order_status": context.order_status,
        "shipping_status": context.order_status,
        "tracking_number": context.tracking_number or None,
        "shipping_provider": context.shipping_provider or None,
    }
    if context.tracking_number:
        active["tracking_url"] = (
            f"https://playground.nahlah.ai/track/{context.tracking_number}"
        )
    return {
        "active_order_id": context.order_reference or "playground",
        "active_order_context": active,
        "recent_order_ids": [context.order_reference] if context.order_reference else [],
    }


def _enrich_facts_from_tenant_settings(
    db: Session,
    tenant_id: int,
    facts: Any,
) -> None:
    """Read-only enrichment from tenant settings when snapshot policy is empty."""
    from models import TenantSettings  # noqa: PLC0415

    settings = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    if settings is None:
        return
    store = dict(settings.store_settings or {})
    if not str(getattr(facts, "shipping_policy", "") or "").strip():
        policy = str(store.get("shipping_policy") or "").strip()
        if policy:
            facts.shipping_policy = policy
    if not str(getattr(facts, "store_name", "") or "").strip():
        name = str(store.get("store_name") or "").strip()
        if name:
            facts.store_name = name


def _build_brain_context(
    db: Session,
    *,
    tenant_id: int,
    message: str,
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> BrainContext:
    intent = rules.match(message) or Intent(
        name="general",
        confidence=0.5,
        raw_message=message,
    )
    facts = DefaultFactsLoader().load(db, tenant_id)
    _enrich_facts_from_tenant_settings(db, tenant_id, facts)
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone=PLAYGROUND_PHONE,
        customer_id=PLAYGROUND_CUSTOMER_ID,
        conversation_id=PLAYGROUND_CONVERSATION_ID,
        message=message,
        intent=intent,
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=facts,
        history=[],
        commerce_bundle=dict(commerce_bundle or {}),
    )
    ctx._db = db  # noqa: SLF001 — KB route owners read tenant sections
    return ctx


def _tracking_decision(ctx: BrainContext) -> Optional[Decision]:
    bundle = dict(getattr(ctx, "commerce_bundle", None) or {})
    if not is_explicit_order_tracking_request(ctx.message or "", commerce_bundle=bundle):
        return None
    from core.active_order_context import prepare_tracking_follow_up_decision  # noqa: PLC0415

    return Decision(
        action=ACTION_LLM_REPLY,
        args=prepare_tracking_follow_up_decision(ctx),
        reason="playground_tracking_follow_up",
        confidence=0.93,
    )


def _resolve_decision(ctx: BrainContext) -> Decision:
    tracking = _tracking_decision(ctx)
    if tracking is not None:
        return tracking
    for resolver in (
        try_non_catalog_availability_kb_decision,
        try_product_knowledge_decision,
    ):
        decision = resolver(ctx)
        if decision is not None:
            return decision
    return DefaultDecisionEngine().decide(ctx)


def _kb_sections_from_db(ctx: BrainContext, *, hint: str = "") -> List[str]:
    from models import MerchantKnowledgeSection  # noqa: PLC0415

    rows = (
        ctx._db.query(MerchantKnowledgeSection)  # noqa: SLF001
        .filter_by(tenant_id=ctx.tenant_id, is_active=True)
        .all()
    )
    hint_tokens = [
        tok for tok in re.findall(r"[\w\u0600-\u06FF]+", hint or "")
        if len(tok) >= 3
    ]
    scored: List[Tuple[int, str]] = []
    for row in rows:
        body = str(getattr(row, "body", "") or "").strip()
        title = str(getattr(row, "title", "") or "").strip()
        if not body:
            continue
        score = sum(1 for tok in hint_tokens if tok in title or tok in body)
        scored.append((score, body))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        return [scored[0][1]]
    return [body for _, body in scored if body]


def synthesize_facts_reply(decision: Any, ctx: BrainContext) -> str:
    """FakeFacts compose — wording from allowed_facts / KB / bundle only."""
    args = dict(getattr(decision, "args", None) or {})
    allowed = dict(args.get("allowed_facts") or {})

    body = str(allowed.get("kb_section_body") or "").strip()
    if body:
        return body

    kb_sections = allowed.get("kb_sections") or []
    if isinstance(kb_sections, list) and kb_sections:
        chunks = [
            str(item.get("body") or item.get("text") or "").strip()
            for item in kb_sections
            if isinstance(item, dict) and (item.get("body") or item.get("text"))
        ]
        if chunks:
            return " ".join(chunks)

    topic = str(args.get("topic") or args.get("topic_hint") or "")
    if topic == "tracking_link_follow_up":
        bundle = dict(getattr(ctx, "commerce_bundle", None) or {})
        active = dict(bundle.get("active_order_context") or {})
        ref = str(args.get("order_reference") or active.get("order_id") or "").strip()
        tracking = str(active.get("tracking_number") or "").strip()
        provider = str(
            active.get("shipping_provider") or active.get("provider") or ""
        ).strip()
        parts = [p for p in (ref, tracking, provider) if p]
        return " | ".join(parts)

    if topic in {TOPIC_KB_AVAILABILITY_FACTS, TOPIC_PRODUCT_KNOWLEDGE_FACTS}:
        hint = str(allowed.get("inquiry_subject") or ctx.message or "")
        bodies = _kb_sections_from_db(ctx, hint=hint)
        if bodies:
            return bodies[0]

    if "shipping" in topic or str(args.get("topic_hint") or "") == "shipping":
        policy = str(getattr(ctx.facts, "shipping_policy", "") or "").strip()
        if policy:
            return policy

    bodies = _kb_sections_from_db(ctx, hint=str(ctx.message or ""))
    if bodies:
        return bodies[0]
    return ""


def _extract_preview_facts(decision: Decision, ctx: BrainContext) -> Dict[str, Any]:
    args = dict(getattr(decision, "args", None) or {})
    allowed = dict(args.get("allowed_facts") or {})
    bundle = dict(getattr(ctx, "commerce_bundle", None) or {})
    active = dict(bundle.get("active_order_context") or {})
    facts: Dict[str, Any] = {}
    if allowed:
        facts["allowed_facts"] = allowed
    if args.get("topic"):
        facts["topic"] = args.get("topic")
    if args.get("order_reference"):
        facts["order_reference"] = args.get("order_reference")
    if args.get("tracking_available") is not None:
        facts["tracking_available"] = bool(args.get("tracking_available"))
    if active:
        facts["active_order_context"] = {
            k: active.get(k)
            for k in (
                "order_id",
                "order_status",
                "tracking_number",
                "shipping_provider",
                "tracking_url",
            )
            if active.get(k)
        }
    policy = str(getattr(ctx.facts, "shipping_policy", "") or "").strip()
    if policy:
        facts["shipping_policy"] = policy
    return facts


def _tracking_needs_context(decision: Decision, ctx: BrainContext) -> bool:
    args = dict(getattr(decision, "args", None) or {})
    if str(args.get("topic") or "") != "tracking_link_follow_up":
        return False
    if not is_explicit_order_tracking_request(
        ctx.message or "",
        commerce_bundle=dict(getattr(ctx, "commerce_bundle", None) or {}),
    ):
        return False
    if bool(args.get("tracking_available")):
        return False
    bundle = dict(getattr(ctx, "commerce_bundle", None) or {})
    active = dict(bundle.get("active_order_context") or {})
    tracking = str(active.get("tracking_number") or "").strip()
    return not tracking


def run_playground_dry_run(
    db: Session,
    *,
    tenant_id: int,
    message: str,
    mode: str = "stateless",
    context: Optional[Mapping[str, Any]] = None,
) -> PlaygroundDryRunResult:
    """
    Preview what AI would reply to an inbound message without side effects.

    Read-only DB access for tenant KB/facts; no commits.
    """
    _ = mode  # v1: stateless only
    text = (message or "").strip()
    base = PlaygroundDryRunResult(side_effects=_empty_side_effects())

    if not text:
        base.warnings.append("Message is empty.")
        return base

    if not is_store_ai_enabled(db, tenant_id):
        base.blocked_reason = REASON_STORE_AI_DISABLED
        base.warnings.append(
            "Store AI is disabled; no customer would receive a reply."
        )
        return base

    if not has_billing_access(db, tenant_id):
        base.blocked_reason = BLOCKED_BILLING_DENIED
        base.warnings.append(
            "Billing access denied; outbound AI replies would be suppressed."
        )
        return base

    order_ctx = PlaygroundOrderContext.from_payload(context)
    commerce_bundle = build_playground_commerce_bundle(order_ctx)
    ctx = _build_brain_context(
        db,
        tenant_id=tenant_id,
        message=text,
        commerce_bundle=commerce_bundle,
    )
    decision = _resolve_decision(ctx)
    contract = build_turn_owner_contract(decision, ctx)

    topic = str((decision.args or {}).get("topic") or "")
    action = str(getattr(decision, "action", "") or "")
    base.decision_topic = topic or str((decision.args or {}).get("topic_hint") or "") or None
    base.decision_action = action or None
    base.owner = contract.owner
    base.facts = _extract_preview_facts(decision, ctx)

    if _tracking_needs_context(decision, ctx):
        base.needs_context = True
        base.would_send = False
        base.outbound_kind = OUTBOUND_NONE
        base.warnings.append(
            "Tracking reply requires order context (order reference and tracking data)."
        )
        return base

    if action == ACTION_PROPOSE_DRAFT_ORDER and looks_like_delivery_confirmation(text):
        base.warnings.append(
            "Delivery confirmation detected — playground will not create orders or emit review automation."
        )

    reply = synthesize_facts_reply(decision, ctx).strip()
    base.reply_text = reply or None
    base.used_llm = False

    if reply and action in {ACTION_LLM_REPLY, ACTION_PROPOSE_DRAFT_ORDER}:
        base.would_send = True
        base.outbound_kind = OUTBOUND_SESSION_TEXT
    elif reply:
        base.would_send = True
        base.outbound_kind = OUTBOUND_SESSION_TEXT
    else:
        base.would_send = False
        base.outbound_kind = OUTBOUND_NONE
        if not base.warnings:
            base.warnings.append("No grounded preview text available for this message.")

    return base
