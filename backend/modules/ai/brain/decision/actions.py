"""
brain/decision/actions.py
──────────────────────────
Action type constants — the vocabulary that DecisionEngine speaks and
ActionExecutor understands.

Phase 1 actions (all implemented):
  greet                 — send a greeting reply
  search_products       — look up products matching a query and return results
  propose_draft_order   — create a Salla/Zid draft order and return a checkout URL
  send_payment_link     — resend an existing payment / checkout link
  suggest_coupon        — offer a discount code (only when products available)
  show_order_status     — retrieve and show customer's order status
  handoff_to_human      — trigger the human-handoff flow
  llm_reply             — fall back to full LLM response (catch-all)

Adding a new action in Phase 2+:
  1. Add an ACTION_* constant here.
  2. Create a handler in execution/.
  3. Register it in execution/executor.py.
  4. Add a decision rule in decision/engine.py.
"""

# ── Phase 1 action constants ──────────────────────────────────────────────────
ACTION_GREET               = "greet"
ACTION_SEARCH_PRODUCTS     = "search_products"
ACTION_PROPOSE_DRAFT_ORDER = "propose_draft_order"
ACTION_SEND_PAYMENT_LINK   = "send_payment_link"
ACTION_SUGGEST_COUPON      = "suggest_coupon"
ACTION_TRACK_ORDER         = "track_order"
ACTION_HANDOFF             = "handoff_to_human"
ACTION_LLM_REPLY           = "llm_reply"    # catch-all — routes to orchestrator

# ── Phase 2 action constants ──────────────────────────────────────────────────
# Ask the customer one clarifying question (e.g. "ما المنتج الذي تود طلبه؟")
ACTION_CLARIFY             = "clarify"
# Present 2-3 product choices when search returns too many similar results
ACTION_NARROW              = "narrow_choices"

ALL_ACTIONS = [
    ACTION_GREET,
    ACTION_SEARCH_PRODUCTS,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_CLARIFY,
    ACTION_NARROW,
]
