"""
Reset persisted sales / fulfillment brain state for one customer conversation.

Platform-wide ops tool — no tenant-specific logic. Clears commerce funnel
state in ``Conversation.extra_metadata`` while preserving customer row,
profile/preferences, message history, and identity flags.

Usage (production via Railway):
    railway run python scripts/reset_conversation_sales_state.py --tenant 33 --phone 966506569015 --dry-run
    railway run python scripts/reset_conversation_sales_state.py --tenant 33 --phone 966506569015 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
for _p in (str(REPO), str(BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from database.models import Conversation, Customer, CustomerPreferences, CustomerProfile
from modules.ai.brain.state.stages import STAGE_EXPLORING
from modules.ai.brain.state.store import _find_conversation, _find_customer
from modules.ai.brain.types import MerchantConversationState, OrderPreparationState

_METADATA_ORDER_KEYS = (
    "active_order_id",
    "active_order_context",
)


def _db_session() -> Session:
    url = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return sessionmaker(bind=create_engine(url))()


def _snapshot_brain(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not raw:
        return {"present": False}
    op = raw.get("order_prep") or {}
    focus = raw.get("current_product_focus")
    return {
        "present": True,
        "stage": raw.get("stage"),
        "turn": raw.get("turn"),
        "greeted": raw.get("greeted"),
        "assistant_identity_introduced": raw.get("assistant_identity_introduced"),
        "current_product_focus": (
            {k: focus.get(k) for k in ("id", "title", "external_id", "salla_id")}
            if isinstance(focus, dict) else focus
        ),
        "pending_action": raw.get("pending_action"),
        "draft_order_id": raw.get("draft_order_id"),
        "checkout_url": bool(raw.get("checkout_url")),
        "order_prep": {
            "awaiting_payment_receipt": op.get("awaiting_payment_receipt"),
            "order_status": op.get("order_status"),
            "product_id": op.get("product_id"),
            "missing_fields": op.get("missing_fields"),
        },
        "conversation_summary_len": len(str(raw.get("conversation_summary") or "")),
    }


def _snapshot_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    ctx = meta.get("active_order_context")
    return {
        "active_order_id": meta.get("active_order_id"),
        "active_order_context": (
            {
                "order_id": ctx.get("order_id"),
                "order_status": ctx.get("order_status"),
                "product_summary": ctx.get("product_summary"),
            }
            if isinstance(ctx, dict) else None
        ),
        "recent_order_ids_count": len(meta.get("recent_order_ids") or []),
    }


def _reset_brain_state(existing: MerchantConversationState) -> Tuple[MerchantConversationState, Dict[str, Any]]:
    """Return new state + list of cleared fields."""
    cleared: Dict[str, Any] = {}

    new = MerchantConversationState(
        stage=STAGE_EXPLORING,
        greeted=existing.greeted,
        assistant_identity_introduced=existing.assistant_identity_introduced,
        last_intent="general",
        current_product_focus=None,
        draft_order_id=None,
        checkout_url=None,
        customer_goal="",
        recent_topic="",
        recent_topic_turn=0,
        last_fallback_fingerprint="",
        last_fallback_turn=0,
        last_salam_return_turn=existing.last_salam_return_turn,
        last_salam_return_level=existing.last_salam_return_level,
        product_focus_turn=0,
        visual_focus_turn=0,
        last_inbound_canonical="",
        last_inbound_canonical_turn=0,
        last_question_asked="",
        last_question_answered=True,
        recommended_next_step="",
        order_prep=OrderPreparationState(),
        turn=existing.turn,
        updated_at=datetime.now(timezone.utc).isoformat(),
        last_search_candidates=[],
        catalog_browse_pool=[],
        catalog_browse_offset=0,
        last_browse_query="",
        recent_messages=list(existing.recent_messages or []),
        conversation_summary=str(existing.conversation_summary or ""),
        cart_items=[],
        selected_variant=None,
        payment_method="",
        pending_action="",
        last_recommended_products=[],
        pending_short_address_code="",
        pending_google_maps_url="",
        pending_city="",
        last_action="",
        general_streak=0,
        current_selected_options={},
        pending_option_groups=[],
        awaiting_option_confirmation=False,
        last_platform_topic="",
        pending_confirmation="",
        last_link_sent="",
        last_link_sent_turn=0,
        customer_gender_hint=str(getattr(existing, "customer_gender_hint", "") or ""),
        customer_gender_confidence=float(getattr(existing, "customer_gender_confidence", 0) or 0),
        staff_contacts_sent=list(getattr(existing, "staff_contacts_sent", None) or []),
    )

    if existing.stage != STAGE_EXPLORING:
        cleared["stage"] = f"{existing.stage} -> {STAGE_EXPLORING}"
    if existing.current_product_focus:
        cleared["current_product_focus"] = existing.current_product_focus
    if existing.pending_action:
        cleared["pending_action"] = existing.pending_action
    if existing.draft_order_id:
        cleared["draft_order_id"] = existing.draft_order_id
    if existing.checkout_url:
        cleared["checkout_url"] = True
    op = existing.order_prep
    if (
        op.awaiting_payment_receipt
        or op.product_id
        or op.order_status
        or op.missing_fields
    ):
        cleared["order_prep"] = {
            "awaiting_payment_receipt": op.awaiting_payment_receipt,
            "order_status": op.order_status,
            "product_id": op.product_id,
            "missing_fields": list(op.missing_fields or []),
        }

    return new, cleared


def _customer_memory_snapshot(db: Session, customer_id: int) -> Dict[str, Any]:
    cust = db.query(Customer).filter(Customer.id == customer_id).one()
    prof = db.query(CustomerProfile).filter(CustomerProfile.customer_id == customer_id).first()
    prefs = db.query(CustomerPreferences).filter(CustomerPreferences.customer_id == customer_id).first()
    return {
        "customer_id": customer_id,
        "name": cust.name,
        "phone": cust.phone,
        "normalized_phone": cust.normalized_phone,
        "profile_segment": getattr(prof, "segment", None) if prof else None,
        "preferences_present": prefs is not None,
    }


def run(tenant_id: int, phone: str, *, apply: bool) -> Dict[str, Any]:
    db = _db_session()
    try:
        customer, matched, matched_col, tried = _find_customer(db, tenant_id, phone)
        if not customer:
            raise SystemExit(
                f"No customer for tenant={tenant_id} phone={phone!r} "
                f"(tried {len(tried)} candidates)"
            )

        conv = _find_conversation(db, tenant_id, customer.id)
        if not conv:
            raise SystemExit(
                f"No conversation for tenant={tenant_id} customer_id={customer.id}"
            )

        meta = dict(conv.extra_metadata or {})
        raw_brain = meta.get("brain_state")
        before_brain = _snapshot_brain(raw_brain if isinstance(raw_brain, dict) else None)
        before_meta = _snapshot_metadata(meta)
        memory = _customer_memory_snapshot(db, customer.id)

        existing = (
            MerchantConversationState.from_dict(raw_brain)
            if isinstance(raw_brain, dict)
            else MerchantConversationState()
        )
        new_state, cleared_brain = _reset_brain_state(existing)

        cleared_meta: Dict[str, Any] = {}
        for key in _METADATA_ORDER_KEYS:
            if key in meta and meta.get(key):
                cleared_meta[key] = meta.get(key)
                if apply:
                    meta.pop(key, None)

        report: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "phone_input": phone,
            "matched_phone": matched,
            "matched_column": matched_col,
            "customer_id": customer.id,
            "conversation_id": conv.id,
            "apply": apply,
            "before": {
                "brain_state": before_brain,
                "metadata_order": before_meta,
                "conversation_flags": {
                    "is_human_handoff": conv.is_human_handoff,
                    "handoff_active": conv.handoff_active,
                    "needs_human": conv.needs_human,
                    "ai_paused": conv.ai_paused,
                    "ai_paused_reason": conv.ai_paused_reason,
                },
            },
            "cleared": {
                "brain_fields": cleared_brain,
                "metadata_keys": cleared_meta,
            },
            "preserved": {
                "customer_memory": memory,
                "greeted": new_state.greeted,
                "assistant_identity_introduced": new_state.assistant_identity_introduced,
                "conversation_summary_len": len(new_state.conversation_summary or ""),
                "recent_messages_count": len(new_state.recent_messages or []),
                "turn": new_state.turn,
            },
            "after": {},
        }

        if apply:
            meta["brain_state"] = new_state.to_dict()
            conv.extra_metadata = meta
            flag_modified(conv, "extra_metadata")

            if conv.is_human_handoff or conv.handoff_active or conv.needs_human:
                conv.is_human_handoff = False
                conv.handoff_active = False
                conv.needs_human = False
                conv.taken_over_at = None
                conv.taken_over_by = None
                report["cleared"]["conversation_handoff"] = True

            if conv.ai_paused:
                conv.ai_paused = False
                conv.ai_paused_reason = None
                conv.ai_paused_at = None
                conv.ai_paused_by = None
                report["cleared"]["ai_paused"] = True

            db.commit()
            db.refresh(conv)

        after_meta = dict(conv.extra_metadata or {})
        after_raw = after_meta.get("brain_state")
        report["after"] = {
            "brain_state": _snapshot_brain(after_raw if isinstance(after_raw, dict) else None),
            "metadata_order": _snapshot_metadata(after_meta),
            "slim_eligible_hint": {
                "stage": (after_raw or {}).get("stage") if isinstance(after_raw, dict) else None,
                "no_product_focus": not (after_raw or {}).get("current_product_focus")
                if isinstance(after_raw, dict) else True,
                "order_prep_idle": not any(
                    [
                        ((after_raw or {}).get("order_prep") or {}).get("awaiting_payment_receipt"),
                        ((after_raw or {}).get("order_prep") or {}).get("product_id"),
                        str(((after_raw or {}).get("order_prep") or {}).get("order_status") or "").strip(),
                    ]
                )
                if isinstance(after_raw, dict) else True,
            },
        }

        return report
    finally:
        db.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Reset sales/fulfillment conversation state")
    p.add_argument("--tenant", type=int, required=True)
    p.add_argument("--phone", required=True, help="Any phone shape; E.164 preferred")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()
    report = run(args.tenant, args.phone, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
