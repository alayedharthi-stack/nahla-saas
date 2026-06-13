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
from typing import Any, Dict, Optional

from ..types import ActionResult, BrainContext, Decision
from ..decision.actions import (
    ACTION_SEARCH_PRODUCTS,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_RECOMMEND_ADDON,
    ACTION_SUGGEST_COUPON,
    ACTION_HANDOFF,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_WEB_SEARCH,
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
        self._emit_sales_events(db, ctx, decision, result)
        # Phase 1.6 — best-effort anonymized signal emission.  This MUST be
        # the last side-effect of the turn so that any failure here can never
        # roll back the trace / affinity / sales-event writes above.  The
        # method itself is wrapped in a hard try/except so a misconfigured
        # cross-merchant store (or even a database outage on that table)
        # never breaks the customer reply path.
        self._emit_anonymous_signal(db, ctx, decision, result, stage_before, latency_ms)
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

    # ── 3.5 Automation + recommendation event loop ───────────────────────────

    def _emit_sales_events(
        self,
        db: Any,
        ctx: BrainContext,
        decision: Decision,
        result: ActionResult,
    ) -> None:
        if not ctx.customer_id:
            return
        try:
            from core.automation_engine import emit_automation_event

            if decision.action == ACTION_SEND_PAYMENT_LINK and result.data.get("checkout_url"):
                emit_automation_event(
                    db,
                    tenant_id=ctx.tenant_id,
                    event_type="order_payment_pending",
                    customer_id=ctx.customer_id,
                    payload={
                        "source": "merchant_brain",
                        "checkout_url": result.data.get("checkout_url"),
                        "draft_order_id": ctx.state.draft_order_id,
                    },
                    commit=True,
                )
            elif decision.action == ACTION_RECOMMEND_ADDON and result.data.get("products"):
                emit_automation_event(
                    db,
                    tenant_id=ctx.tenant_id,
                    event_type="product_created",
                    customer_id=ctx.customer_id,
                    payload={
                        "source": "merchant_brain",
                        "kind": "addon_recommendation",
                        "products": result.data.get("products", []),
                    },
                    commit=True,
                )
            elif (
                decision.action in {ACTION_SEARCH_PRODUCTS, ACTION_PROPOSE_DRAFT_ORDER, ACTION_WEB_SEARCH}
                and ctx.sales_context
                and ctx.sales_context.repeat_purchase_candidates
            ):
                emit_automation_event(
                    db,
                    tenant_id=ctx.tenant_id,
                    event_type="predictive_reorder_due",
                    customer_id=ctx.customer_id,
                    payload={
                        "source": "merchant_brain",
                        "candidates": ctx.sales_context.repeat_purchase_candidates[:3],
                    },
                    commit=True,
                )
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            logger.debug("[MemoryUpdater] emit_sales_events failed: %s", exc)

    # ── 3.6 Anonymized cross-merchant signal (Phase 1.6) ─────────────────────

    def _emit_anonymous_signal(
        self,
        db: Any,
        ctx: BrainContext,
        decision: Decision,
        result: ActionResult,
        stage_before: str,
        latency_ms: int,
    ) -> None:
        """Persist a single anonymized ``TraceEvent`` for the current turn.

        Hard rules
        ──────────
        * Best-effort: any exception is swallowed and the turn proceeds.
        * Never writes raw text, raw ids, raw prices, raw product titles or
          raw customer / store data — every field is either categorical,
          bucketed, or a salted hash.
        * Master-switched off → silent no-op (the writer enforces this).
        """
        try:
            from modules.ai.security import (
                CrossMerchantLearningStore,
                LearningTier,
                OutcomeKind,
                TraceEvent,
                UIMode,
                anonymize_tenant,
                industry_of,
                value_bucket,
            )
        except Exception as exc:
            logger.debug("[MemoryUpdater] security module unavailable — skipping signal: %s", exc)
            return

        if not CrossMerchantLearningStore.is_enabled():
            return

        try:
            tenant_hash = anonymize_tenant(int(ctx.tenant_id))
            industry    = _resolve_industry(db, ctx, industry_of)
            ui_mode     = _classify_ui_mode(result, UIMode)
            outcome     = _classify_outcome(decision, result, OutcomeKind)
            order_total = _extract_order_total(result)
            bucket      = value_bucket(order_total) if order_total is not None else "unknown"
            chosen_path = str(result.data.get("chosen_path") or "rule").strip().lower() or "rule"
            tier        = LearningTier.VERTICAL if industry and industry != "unknown" else LearningTier.GLOBAL

            extra = {
                "stage_before":             stage_before,
                "stage_after":              ctx.state.stage,
                "decision_path":            chosen_path,
                "fact_guard_modified":      bool(result.data.get("fact_guard_modified", False)),
                "had_recommendations":      bool(
                    (ctx.sales_context.recommendations if ctx.sales_context else None)
                    or result.data.get("recommended_products")
                    or result.data.get("products")
                ),
                "had_repeat_purchase":      bool(
                    ctx.sales_context.repeat_purchase_candidates
                    if ctx.sales_context else False
                ),
                "had_buttons":              bool(result.data.get("pending_buttons")),
                "intent_confidence_bucket": _bucket_confidence(ctx.intent.confidence),
                "history_length_bucket":    _bucket_history_length(len(ctx.history or [])),
                "tool_count":               int(len(result.data.get("products") or [])),
                "language":                 str(
                    (ctx.profile or {}).get("preferred_language", "ar")
                )[:8],
                "rule_version":             "phase1.9",
            }

            # Phase 1.8 — record policy_hint adoption metadata (no PII;
            # ``_collect_hint_metadata`` returns at most categorical labels
            # and booleans).  All keys are pre-whitelisted in
            # ``ALLOWED_EXTRA_KEYS`` so a stripped/missing whitelist still
            # produces a clean event after ``validate_anonymized``.
            extra.update(_collect_hint_metadata(decision, result, ui_mode))

            event = TraceEvent(
                tenant_hash  = tenant_hash,
                industry     = industry,
                intent       = str(ctx.intent.name or "unknown"),
                action       = str(decision.action or "unknown"),
                ui_mode      = ui_mode,
                outcome      = outcome,
                value_bucket = bucket,
                turn_index   = int(getattr(ctx.state, "turn", 0) or 0),
                model_path   = chosen_path,
                latency_ms   = int(latency_ms or 0),
                tier         = tier,
                extra        = extra,
            )
        except Exception as exc:
            logger.debug("[MemoryUpdater] signal build failed: %s", exc)
            return

        try:
            store = CrossMerchantLearningStore(db)
            store.record(event, commit=True)
        except Exception as exc:
            # validate_anonymized may raise on a programming bug; we swallow
            # at the emission boundary so the customer turn is never broken.
            logger.debug("[MemoryUpdater] cross-merchant signal write failed: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass

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
            _summary_model = "claude-haiku-4-5"
            from modules.ai.orchestrator.llm_cost_audit import emit_llm_cost_audit  # noqa: PLC0415

            emit_llm_cost_audit(
                tenant_id=ctx.tenant_id,
                turn_id=getattr(ctx.state, "turn", None),
                model=_summary_model,
                provider="anthropic",
                messages_count=1,
                messages_chars=len(prompt),
                total_prompt_chars=len(prompt),
                estimated_input_tokens=len(prompt) // 4,
                reason="brain.memory.updater._summarise",
            )
            response = client.messages.create(
                model=_summary_model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            from modules.ai.orchestrator.ai_usage_ledger import record_ai_usage_from_anthropic  # noqa: PLC0415

            record_ai_usage_from_anthropic(
                audit_extra={
                    "tenant_id": ctx.tenant_id,
                    "turn_id": getattr(ctx.state, "turn", None),
                    "reason": "brain.memory.updater._summarise",
                    "estimated_input_tokens": len(prompt) // 4,
                },
                model=_summary_model,
                response=response,
                reply_text=response.content[0].text if response.content else "",
                total_prompt_chars=len(prompt),
                db=db,
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


# ── Phase 1.6 helpers ────────────────────────────────────────────────────────
# These helpers are intentionally module-private and pure functions so they
# can be unit-tested without spinning up the full MemoryUpdater pipeline.
# They MUST NOT touch the database directly.

def _resolve_industry(db: Any, ctx: BrainContext, industry_of_fn: Any) -> str:
    """Best-effort industry derivation from TenantSettings.

    Returns ``"unknown"`` on any failure — never raises so the surrounding
    signal emission cannot be broken by an industry lookup error.
    """
    try:
        from database.models import TenantSettings
        ts = db.query(TenantSettings).filter_by(tenant_id=ctx.tenant_id).first()
        return industry_of_fn(ts) if ts is not None else "unknown"
    except Exception:
        return "unknown"


def _classify_ui_mode(result: ActionResult, ui_mode_cls: Any) -> str:
    """Map an ``ActionResult`` to one of the categorical ``UIMode`` values."""
    data = result.data or {}
    if data.get("pending_buttons"):
        return ui_mode_cls.BUTTONS
    if data.get("products") or data.get("recommended_products"):
        return ui_mode_cls.PRODUCT_CARDS
    rtype = str(data.get("type") or "").lower()
    if rtype in {"product_card", "product_cards", "search"}:
        return ui_mode_cls.PRODUCT_CARDS
    if rtype in {"list", "narrow_choices"}:
        return ui_mode_cls.LIST
    if rtype == "voice":
        return ui_mode_cls.VOICE
    if rtype == "image":
        return ui_mode_cls.IMAGE
    return ui_mode_cls.TEXT


def _classify_outcome(decision: Decision, result: ActionResult, outcome_cls: Any) -> str:
    """Map (decision, result) to an ``OutcomeKind`` label.

    The mapping is intentionally coarse so that downstream learners can
    aggregate across many merchants without reverse-engineering specific
    business flows.
    """
    if not result.success:
        return outcome_cls.ERROR

    action = (decision.action or "").lower()
    data   = result.data or {}

    if action == ACTION_HANDOFF:
        return outcome_cls.HANDOFF
    if action == ACTION_SEND_PAYMENT_LINK:
        return outcome_cls.PAYMENT_SENT
    if action == ACTION_PROPOSE_DRAFT_ORDER:
        if data.get("checkout_url") or data.get("order_id"):
            return outcome_cls.CHECKOUT_STARTED
        return outcome_cls.ADDED_TO_CART
    if action == ACTION_SEARCH_PRODUCTS:
        if data.get("products"):
            return outcome_cls.PRODUCT_PRESENTED
        return outcome_cls.BROWSE
    if action == ACTION_RECOMMEND_ADDON:
        return outcome_cls.PRODUCT_PRESENTED
    if action == ACTION_SUGGEST_COUPON:
        return outcome_cls.OBJECTION
    if action in {"greet"}:
        return outcome_cls.GREET
    if action in {"faq_reply", "narrow_choices", "clarify"}:
        return outcome_cls.SUPPORT
    if action == ACTION_WEB_SEARCH:
        return outcome_cls.SUPPORT
    return outcome_cls.UNKNOWN


def _extract_order_total(result: ActionResult) -> Optional[float]:
    """Pull a ``total`` value from common result shapes.

    Returns ``None`` (not 0.0) when no total is available so the bucket
    helper can mark the value as ``"unknown"`` rather than ``"zero"``.
    """
    data = result.data or {}
    for key in ("total", "order_total", "amount"):
        if key in data and data[key] is not None:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                return None
    order = data.get("order")
    if isinstance(order, dict):
        for key in ("total", "amount"):
            if order.get(key) is not None:
                try:
                    return float(order[key])
                except (TypeError, ValueError):
                    return None
    return None


def _bucket_confidence(value: Any) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "unknown"
    if v < 0.4:
        return "low"
    if v < 0.75:
        return "medium"
    return "high"


def _bucket_history_length(n: int) -> str:
    if n <= 0:
        return "0"
    if n <= 3:
        return "1_3"
    if n <= 10:
        return "4_10"
    if n <= 25:
        return "11_25"
    return "25_plus"


# ── Phase 1.8 helpers ────────────────────────────────────────────────────────
# Adoption-measurement metadata extracted from a ``Decision``.  Returned as a
# plain dict that is merged into ``TraceEvent.extra`` and therefore must only
# contain keys that are pre-whitelisted in ``ALLOWED_EXTRA_KEYS``.

def _collect_hint_metadata(
    decision: Decision,
    result: Optional[ActionResult] = None,
    rendered_ui_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Return categorical / boolean metadata describing the policy hint.

    When ``decision.args["policy_hint"]`` is missing or malformed, the
    function returns a single ``{"hint_present": False}`` flag — never an
    empty dict — so adoption queries can distinguish "no hint produced"
    from "no signal at all".

    Phase 1.9 additions
    ───────────────────
    When the bias layer attached ``bias_applied=True`` to ``decision.args``
    we also record ``bias_type``, ``bias_reason``, ``final_ui_mode``,
    ``final_recommendation_shape`` and ``final_choice_count_bucket``.
    The "final_*" fields describe what the executor *actually* rendered
    so dashboards can compare requested bias vs. realised behavior.

    Strict anti-leak rules
    ──────────────────────
    * No raw merchant data — the input ``policy_hint`` only carries
      labels already validated by the learner.
    * Confidence and sample size are bucketed before being returned so a
      single tenant's hint cannot be reverse-engineered via repeated
      observations.
    * ``bias_type`` / ``bias_reason`` are clipped to 64 chars so a
      pathological reason string cannot smuggle long arbitrary text.
    """
    out: Dict[str, Any] = {"hint_present": False}
    try:
        args = decision.args if isinstance(decision.args, dict) else {}

        # Phase 1.9 ─ surface bias_* and final_* even when there is no hint
        # (executor may set ``preferred_ui_mode`` independently).  We only
        # record bias metadata when ``bias_applied`` is True to avoid
        # polluting traces from non-biased turns.
        bias_applied = bool(args.get("bias_applied"))
        if bias_applied:
            out["bias_applied"] = True
            out["bias_type"]    = _truncate(args.get("bias_type"), 64) or "unknown"
            out["bias_reason"]  = _truncate(args.get("bias_reason"), 64) or "unknown"
            bias_intent = _truncate(args.get("bias_intent"), 64)
            if bias_intent:
                out["bias_intent"] = bias_intent
            bias_industry = _truncate(args.get("bias_industry"), 64)
            if bias_industry:
                out["bias_industry"] = bias_industry

        # Final realised UI / shape / count — useful even when bias was
        # NOT applied (lets us compare baseline distributions).
        if bias_applied:
            final_ui = (
                _truncate(rendered_ui_mode, 32)
                or _truncate(args.get("preferred_ui_mode"), 32)
            )
            if final_ui:
                out["final_ui_mode"] = final_ui
            final_shape = _classify_recommendation_shape(args, result)
            if final_shape:
                out["final_recommendation_shape"] = final_shape
            count_bucket = _bucket_choice_count(args.get("choice_count"))
            if count_bucket:
                out["final_choice_count_bucket"] = count_bucket

        hint = args.get("policy_hint")
        if not isinstance(hint, dict) or not hint:
            return out

        recommended_action = str(hint.get("recommended_action") or "unknown").strip().lower()
        recommended_ui     = str(hint.get("recommended_ui") or "unknown").strip().lower()
        scope              = str(hint.get("scope") or "global").strip().lower()
        aligned            = (
            bool(hint.get("matches_inner"))
            if "matches_inner" in hint
            else recommended_action == str(decision.action or "").strip().lower()
        )

        out.update({
            "hint_present":           True,
            "hint_action":            recommended_action or "unknown",
            "hint_ui":                recommended_ui or "unknown",
            "hint_scope":             scope or "global",
            "hint_aligned":           bool(aligned),
            # Phase 1.9 — set to True when the soft bias layer actually
            # acted on the hint (i.e. mutated decision.args).  Phase 1.8
            # measurement-only paths still see ``hint_used=False``.
            "hint_used":              bias_applied,
            "hint_confidence_bucket": _bucket_hint_confidence(hint.get("confidence")),
            "hint_sample_bucket":     _bucket_hint_sample_size(hint.get("sample_size")),
        })
    except Exception:
        # Best-effort: a malformed hint should never break the trace.
        return {"hint_present": False}
    return out


def _bucket_hint_confidence(value: Any) -> str:
    try:
        v = float(value if value is not None else 0)
    except (TypeError, ValueError):
        return "unknown"
    if v < 0.4:
        return "low"
    if v < 0.7:
        return "medium"
    if v < 0.9:
        return "high"
    return "very_high"


def _bucket_hint_sample_size(value: Any) -> str:
    try:
        n = int(value if value is not None else 0)
    except (TypeError, ValueError):
        return "unknown"
    if n <= 0:
        return "0"
    if n < 30:
        return "lt_30"
    if n < 100:
        return "30_100"
    if n < 500:
        return "100_500"
    if n < 2000:
        return "500_2k"
    return "2k_plus"


# ── Phase 1.9 helpers ────────────────────────────────────────────────────────

def _truncate(value: Any, limit: int) -> str:
    """Coerce ``value`` to a normalised, length-bounded label.

    Used for ``bias_type`` / ``bias_reason`` so a runaway upstream string
    cannot grow the trace ``extra`` blob.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    return s[:max(int(limit), 1)].lower()


def _classify_recommendation_shape(args: Dict[str, Any], result: Optional[ActionResult]) -> str:
    """Return the *realised* recommendation shape label.

    Order of preference:
      1. The bias layer's ``recommendation_style`` if it set one.
      2. A shape inferred from the executor's result (``products`` /
         ``pending_buttons`` / ``narrow_choices`` / etc.).
      3. ``"none"`` when neither path produced a recommendation surface.
    """
    style = _truncate(args.get("recommendation_style"), 32)
    if style:
        return style
    data = (result.data if result is not None else None) or {}
    if data.get("products") or data.get("recommended_products"):
        return "cards"
    if data.get("pending_buttons"):
        return "compact"
    if str(data.get("type") or "").lower() in {"list", "narrow_choices"}:
        return "list"
    return "none"


def _bucket_choice_count(value: Any) -> str:
    """Map a choice count to a coarse bucket label.

    Returns ``""`` (empty string) for missing values so the caller can
    skip writing the field entirely instead of recording a fake "0".
    """
    if value is None:
        return ""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return "0"
    if n <= 2:
        return "1_2"
    if n <= 4:
        return "3_4"
    if n <= 7:
        return "5_7"
    return "8_plus"
