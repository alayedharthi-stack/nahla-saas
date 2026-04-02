"""
CustomerMemoryUpdater
──────────────────────
Updates customer intelligence records after each orchestrated conversation turn.
Designed to run as an asyncio background task — never blocks the response.

Update strategy:
  - CustomerProfile:             increment counters, recalculate segment
  - CustomerPreferences:         merge AI-inferred preferences from latest turn
  - PriceSensitivityScore:       recalculate from order history if we have enough data
  - ConversationHistorySummary:  append new turn summary, update sentiment + intent
  - ProductAffinity:             bump recommendation_count for each suggested product
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import (
    AIActionLog,
    ConversationHistorySummary,
    Customer,
    CustomerPreferences,
    CustomerProfile,
    ProductAffinity,
    PriceSensitivityScore,
)
from database.session import SessionLocal

logger = logging.getLogger("ai-orchestrator.updater")


def update_customer_memory(
    tenant_id: int,
    customer_id: Optional[int],
    customer_phone: str,
    turn_data: Dict[str, Any],
) -> None:
    """
    Called after the orchestrator sends a response.
    `turn_data` contains:
      - intent: str
      - sentiment: str
      - inferred_preferences: dict
      - suggested_product_ids: list[int]
      - approved_actions: list[dict]
      - reply: str
    """
    if not customer_id:
        return  # new customer with no DB record yet — nothing to update

    db = SessionLocal()
    try:
        _update_conversation_summary(db, tenant_id, customer_id, turn_data)
        _update_product_affinities(db, tenant_id, customer_id, turn_data)
        _update_preferences(db, tenant_id, customer_id, turn_data)
        _update_profile_last_seen(db, tenant_id, customer_id)
        _log_actions(db, tenant_id, customer_id, turn_data)
        db.commit()
    except Exception as exc:
        logger.warning(f"Memory update failed for customer {customer_id}: {exc}")
        db.rollback()
    finally:
        db.close()


def _update_conversation_summary(db, tenant_id: int, customer_id: int, turn: Dict) -> None:
    summary = db.query(ConversationHistorySummary).filter(
        ConversationHistorySummary.customer_id == customer_id,
        ConversationHistorySummary.tenant_id == tenant_id,
    ).first()

    intent = turn.get("intent", "")
    sentiment = turn.get("sentiment", "neutral")

    if not summary:
        summary = ConversationHistorySummary(
            customer_id=customer_id,
            tenant_id=tenant_id,
            total_conversations=1,
            sentiment=sentiment,
            last_intent=intent,
            topics_discussed=[intent] if intent else [],
            products_mentioned=turn.get("suggested_product_ids", []),
            updated_at=datetime.utcnow(),
        )
        db.add(summary)
    else:
        summary.total_conversations = (summary.total_conversations or 0) + 1
        summary.sentiment = sentiment
        summary.last_intent = intent
        summary.updated_at = datetime.utcnow()

        # Append new topics
        existing_topics = summary.topics_discussed or []
        if intent and intent not in existing_topics:
            summary.topics_discussed = (existing_topics + [intent])[-20:]  # keep last 20

        # Append newly mentioned products
        existing_products = summary.products_mentioned or []
        new_ids = [pid for pid in turn.get("suggested_product_ids", []) if pid not in existing_products]
        if new_ids:
            summary.products_mentioned = (existing_products + new_ids)[-50:]  # keep last 50

        # Append new conversation summary text
        new_snippet = turn.get("summary_snippet", "")
        if new_snippet:
            existing = summary.summary_text or ""
            summary.summary_text = (existing + "\n" + new_snippet).strip()[-3000:]  # ~3k chars


def _update_product_affinities(db, tenant_id: int, customer_id: int, turn: Dict) -> None:
    for product_id in turn.get("suggested_product_ids", []):
        row = db.query(ProductAffinity).filter(
            ProductAffinity.customer_id == customer_id,
            ProductAffinity.product_id == product_id,
            ProductAffinity.tenant_id == tenant_id,
        ).first()
        now = datetime.utcnow()
        if not row:
            row = ProductAffinity(
                customer_id=customer_id,
                product_id=product_id,
                tenant_id=tenant_id,
                recommendation_count=1,
                last_recommended_at=now,
                affinity_score=0.1,
                updated_at=now,
            )
            db.add(row)
        else:
            row.recommendation_count = (row.recommendation_count or 0) + 1
            row.last_recommended_at = now
            # Nudge affinity score upward (capped at 1.0)
            row.affinity_score = min(1.0, (row.affinity_score or 0.0) + 0.05)
            row.updated_at = now


def _update_preferences(db, tenant_id: int, customer_id: int, turn: Dict) -> None:
    inferred = turn.get("inferred_preferences", {})
    if not inferred:
        return

    prefs = db.query(CustomerPreferences).filter(
        CustomerPreferences.customer_id == customer_id,
        CustomerPreferences.tenant_id == tenant_id,
    ).first()

    if not prefs:
        prefs = CustomerPreferences(
            customer_id=customer_id,
            tenant_id=tenant_id,
            communication_style=inferred.get("communication_style", "neutral"),
            language=inferred.get("language", "ar"),
            inferred_notes=inferred.get("notes", {}),
            updated_at=datetime.utcnow(),
        )
        db.add(prefs)
    else:
        if "communication_style" in inferred:
            prefs.communication_style = inferred["communication_style"]
        if "language" in inferred:
            prefs.language = inferred["language"]
        if "notes" in inferred:
            existing_notes = prefs.inferred_notes or {}
            existing_notes.update(inferred["notes"])
            prefs.inferred_notes = existing_notes
        prefs.updated_at = datetime.utcnow()


def _update_profile_last_seen(db, tenant_id: int, customer_id: int) -> None:
    profile = db.query(CustomerProfile).filter(
        CustomerProfile.customer_id == customer_id,
        CustomerProfile.tenant_id == tenant_id,
    ).first()
    now = datetime.utcnow()
    if not profile:
        profile = CustomerProfile(
            customer_id=customer_id,
            tenant_id=tenant_id,
            is_returning=True,
            first_seen_at=now,
            last_seen_at=now,
            updated_at=now,
        )
        db.add(profile)
    else:
        profile.last_seen_at = now
        profile.is_returning = True
        profile.updated_at = now


def _log_actions(db, tenant_id: int, customer_id: int, turn: Dict) -> None:
    for action in turn.get("approved_actions", []):
        log = AIActionLog(
            tenant_id=tenant_id,
            customer_id=customer_id,
            action_type=action.get("type", "unknown"),
            proposed_payload=action.get("proposed_payload"),
            policy_result=action.get("policy_result", "approved"),
            policy_notes=action.get("policy_notes"),
            final_payload=action.get("final_payload"),
            applied=False,   # actions are suggestions until the customer responds
            created_at=datetime.utcnow(),
        )
        db.add(log)
