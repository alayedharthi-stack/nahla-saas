"""
brain/memory/updater.py
────────────────────────
DefaultMemoryUpdater — Phase 2.

Writes after every turn:
  1. ConversationTrace row (always — observability)
  2. ProductAffinity bump (when search or order action)
  3. PriceSensitivity nudge (when hesitation intent)
  4. ConversationHistorySummary (Haiku call every 5 turns)

All writes are fire-and-forget — failures are logged but never
propagate to the reply path.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from ..types import ActionResult, BrainContext, Decision
from ..decision.actions import (
    ACTION_SEARCH_PRODUCTS,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SUGGEST_COUPON,
    ACTION_HANDOFF,
    ACTION_SEND_PAYMENT_LINK,
)
from ..types import INTENT_HESITATION

logger = logging.getLogger("nahla.brain.memory_updater")

# Produce a history summary every N turns
SUMMARISE_EVERY_N = 5


class DefaultMemoryUpdater:
    """Implements MemoryUpdater protocol — Phase 2."""

    def update(
        self,
        db: Any,
        ctx: BrainContext,
        decision: Decision,
        result: ActionResult,
        reply: str,
        stage_before: str,
        latency_ms: int,
    ) -> None:
        self._write_trace(db, ctx, decision, result, reply, stage_before, latency_ms)
        self._bump_affinity(db, ctx, decision, result)
        self._nudge_price_sensitivity(db, ctx)
        if ctx.state.turn % SUMMARISE_EVERY_N == 0:
            self._summarise(db, ctx)

    # ── 1. ConversationTrace ──────────────────────────────────────────────────

    def _write_trace(
        self,
        db: Any,
        ctx: BrainContext,
        decision: Decision,
        result: ActionResult,
        reply: str,
        stage_before: str,
        latency_ms: int,
    ) -> None:
        from database.models import ConversationTrace
        trace = ConversationTrace(
            tenant_id         = ctx.tenant_id,
            customer_phone    = ctx.customer_phone,
            session_id        = None,
            turn              = ctx.state.turn,
            message           = ctx.message,
            detected_intent   = ctx.intent.name,
            confidence        = ctx.intent.confidence,
            response_type     = decision.action,
            orchestrator_used = str(result.data.get("chosen_path", "")).startswith("llm"),
            model_used        = str(result.data.get("model_used") or "brain_v2"),
            fact_guard_modified = bool(result.data.get("fact_guard_modified", False)),
            fact_guard_claims = result.data.get("fact_guard_claims"),
            actions_triggered = {
                "action":        decision.action,
                "chosen_path":   result.data.get("chosen_path"),
                "reason":        decision.reason,
                "args":          decision.args,
                "policy_reason": decision.args.get("policy_reason"),
                "success":       result.success,
                "exec_error":    result.error,
                "stage_before":  stage_before,
                "stage_after":   ctx.state.stage,
                "customer_goal": getattr(ctx.state, "customer_goal", ""),
                "selected_product": (ctx.state.current_product_focus or {}).get("title"),
                "order_preparation": getattr(ctx.state.order_prep, "to_dict", lambda: {})(),
                "suggestion": {
                    "suggested_next_step": getattr(ctx.suggestion, "suggested_next_step", ""),
                    "close_to_purchase": getattr(ctx.suggestion, "close_to_purchase", False),
                    "needs_follow_up_question": getattr(ctx.suggestion, "needs_follow_up_question", False),
                    "coupon_logic_considered": getattr(ctx.suggestion, "coupon_logic_considered", False),
                    "discount_ok_now": getattr(ctx.suggestion, "discount_ok_now", False),
                    "route_to_checkout": getattr(ctx.suggestion, "route_to_checkout", False),
                },
            },
            response_text     = reply,
            order_started     = decision.action == ACTION_PROPOSE_DRAFT_ORDER,
            payment_link_sent = decision.action == ACTION_SEND_PAYMENT_LINK,
            handoff_triggered = decision.action == ACTION_HANDOFF,
            latency_ms        = latency_ms,
        )
        try:
            db.add(trace)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("[MemoryUpdater] trace write failed: %s", exc)

    # ── 2. ProductAffinity ────────────────────────────────────────────────────

    def _bump_affinity(
        self,
        db: Any,
        ctx: BrainContext,
        decision: Decision,
        result: ActionResult,
    ) -> None:
        if decision.action not in (ACTION_SEARCH_PRODUCTS, ACTION_PROPOSE_DRAFT_ORDER):
            return
        if not ctx.customer_id:
            return

        products = result.data.get("products", [])
        if not products and ctx.state.current_product_focus:
            products = [ctx.state.current_product_focus]

        if not products:
            return

        try:
            from database.models import ProductAffinity
            now = datetime.now(timezone.utc)

            for p in products[:5]:
                product_id = p.get("id")
                if not product_id:
                    continue

                row = (
                    db.query(ProductAffinity)
                    .filter(
                        ProductAffinity.tenant_id   == ctx.tenant_id,
                        ProductAffinity.customer_id == ctx.customer_id,
                        ProductAffinity.product_id  == product_id,
                    )
                    .first()
                )
                if row:
                    row.view_count += 1
                    if decision.action == ACTION_PROPOSE_DRAFT_ORDER:
                        row.purchase_count += 1
                    row.affinity_score = min(1.0, row.affinity_score + 0.05)
                    row.updated_at = now
                else:
                    purchase = 1 if decision.action == ACTION_PROPOSE_DRAFT_ORDER else 0
                    row = ProductAffinity(
                        customer_id         = ctx.customer_id,
                        product_id          = product_id,
                        tenant_id           = ctx.tenant_id,
                        view_count          = 1,
                        purchase_count      = purchase,
                        recommendation_count= 0,
                        affinity_score      = 0.1 if not purchase else 0.3,
                        updated_at          = now,
                    )
                    db.add(row)

            db.commit()
        except Exception as exc:
            db.rollback()
            logger.debug("[MemoryUpdater] affinity bump failed: %s", exc)

    # ── 3. PriceSensitivity ───────────────────────────────────────────────────

    def _nudge_price_sensitivity(self, db: Any, ctx: BrainContext) -> None:
        if ctx.intent.name != INTENT_HESITATION:
            return
        if not ctx.customer_id:
            return

        try:
            from database.models import PriceSensitivityScore
            now = datetime.now(timezone.utc)
            row = (
                db.query(PriceSensitivityScore)
                .filter(
                    PriceSensitivityScore.tenant_id   == ctx.tenant_id,
                    PriceSensitivityScore.customer_id == ctx.customer_id,
                )
                .first()
            )
            if row:
                row.score = min(1.0, row.score + 0.05)
                row.updated_at = now
            else:
                row = PriceSensitivityScore(
                    customer_id = ctx.customer_id,
                    tenant_id   = ctx.tenant_id,
                    score       = 0.55,
                    updated_at  = now,
                )
                db.add(row)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.debug("[MemoryUpdater] price_sensitivity nudge failed: %s", exc)

    # ── 4. ConversationHistorySummary ─────────────────────────────────────────

    def _summarise(self, db: Any, ctx: BrainContext) -> None:
        """Call Claude Haiku to write a rolling summary of the conversation."""
        if not ctx.customer_id:
            return

        api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return

        try:
            import anthropic

            # Build history text from last 10 turns
            history_lines = []
            for turn in ctx.history[-10:]:
                direction = turn.get("direction", "in")
                body      = (turn.get("body") or "").strip()
                if not body:
                    continue
                role = "عميل" if direction == "in" else "مساعد"
                history_lines.append(f"{role}: {body}")

            if not history_lines:
                return

            history_text = "\n".join(history_lines)
            prompt = (
                f"لخّص هذه المحادثة بين عميل ومساعد متجر إلكتروني في جملتين أو ثلاث باللغة العربية:\n\n"
                f"{history_text}\n\n"
                f"أيضاً أجب بـ JSON بالحقول التالية فقط:\n"
                f'{{ "summary": "...", "last_intent": "browse|order|complaint|inquiry", "sentiment": "positive|neutral|negative|frustrated" }}'
            )

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

            import json, re
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)

            from database.models import ConversationHistorySummary
            now = datetime.now(timezone.utc)
            row = (
                db.query(ConversationHistorySummary)
                .filter(
                    ConversationHistorySummary.tenant_id   == ctx.tenant_id,
                    ConversationHistorySummary.customer_id == ctx.customer_id,
                )
                .first()
            )
            if row:
                row.summary_text           = parsed.get("summary", row.summary_text)
                row.last_intent            = parsed.get("last_intent", row.last_intent)
                row.sentiment              = parsed.get("sentiment", row.sentiment)
                row.total_conversations    = (row.total_conversations or 0) + 1
                row.updated_at             = now
                if ctx.state.stage == "support":
                    row.escalation_count   = (row.escalation_count or 0) + 1
            else:
                row = ConversationHistorySummary(
                    customer_id          = ctx.customer_id,
                    tenant_id            = ctx.tenant_id,
                    summary_text         = parsed.get("summary", ""),
                    last_intent          = parsed.get("last_intent", "browse"),
                    sentiment            = parsed.get("sentiment", "neutral"),
                    total_conversations  = 1,
                    escalation_count     = 1 if ctx.state.stage == "support" else 0,
                    updated_at           = now,
                )
                db.add(row)

            db.commit()
            logger.info(
                "[MemoryUpdater] summary written for customer=%s turn=%s",
                ctx.customer_id, ctx.state.turn,
            )

        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            logger.debug("[MemoryUpdater] summarise failed: %s", exc)
