"""
brain/decision/actions.py
──────────────────────────
Action type constants — the vocabulary that DecisionEngine speaks and
ActionExecutor understands.

Phase 1 actions (all implemented):
  greet                 — send a greeting reply
  faq_reply             — answer a simple store / shipping / identity question
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
ACTION_FAQ_REPLY           = "faq_reply"
ACTION_SEARCH_PRODUCTS     = "search_products"
ACTION_PROPOSE_DRAFT_ORDER = "propose_draft_order"
ACTION_SEND_PAYMENT_LINK   = "send_payment_link"
ACTION_SUGGEST_COUPON      = "suggest_coupon"
ACTION_CUSTOMER_COUPON_REQUEST = "customer_coupon_request"
ACTION_TRACK_ORDER         = "track_order"
ACTION_CUSTOMER_LEDGER_REPLY = "customer_ledger_reply"
ACTION_PAYMENT_CONTINUATION_REPLY = "payment_continuation_reply"
ACTION_HANDOFF             = "handoff_to_human"
ACTION_LLM_REPLY           = "llm_reply"    # catch-all — routes to orchestrator
ACTION_RECOMMEND_ADDON     = "recommend_addon"
ACTION_WEB_SEARCH          = "web_search"

# ── Phase 2 action constants ──────────────────────────────────────────────────
# Ask the customer one clarifying question (e.g. "ما المنتج الذي تود طلبه؟")
ACTION_CLARIFY             = "clarify"
# Present 2-3 product choices when search returns too many similar results
ACTION_NARROW              = "narrow_choices"
# Customer dropped a TAPA short-code / Maps URL / city BEFORE picking a
# product. We stash the address on `state.pending_*` and reply asking
# them to choose a product first; the order flow consumes the stash on
# the next turn so we never re-ask for the address.
ACTION_STASH_ADDRESS_PRE_PRODUCT = "stash_address_pre_product"

# ── Out-of-scope hard guard (May 2026) ────────────────────────────────────────
# Returned by the decision engine when a customer asks something that
# clearly has nothing to do with the merchant's catalogue / orders /
# shipping / payment / store knowledge — e.g. "ايهما حساب كهرباء
# الشقة". The responder emits a fixed, short Arabic deflection and
# the executor never reaches the LLM or any web tool. This is the
# replacement for the old "INTENT_GENERAL → ACTION_WEB_SEARCH" path
# that used to leak DuckDuckGo dumps into customer threads.
ACTION_OUT_OF_SCOPE        = "out_of_scope_reply"

# ── Social / platform actions (May 2026 #4 — context routing) ─────────────────
#
# Two new actions emitted by the new ``INTENT_SOCIAL`` and
# ``INTENT_PLATFORM_INQUIRY`` intents. They short-circuit the rule
# chain with deterministic, culturally-appropriate canned replies and
# DO NOT trigger:
#   * product search / catalog flow
#   * KB / sales context retrieval
#   * LLM expansion
#   * upsell / recommendation
#   * order or payment flow
#
# Why they're separate actions (not just two ACTION_LLM_REPLY branches
# with different system prompts): putting them at the executor level
# means the entire orchestrator skips its sales-oriented machinery
# for these turns. The decision is purely "category → template" so
# behaviour is auditable and provably can't drift into a sales pitch.
ACTION_SOCIAL_REPLY        = "social_reply"
ACTION_PLATFORM_REPLY      = "platform_reply"

# Customer updates delivery location / address while an order is active.
ACTION_ORDER_CONTEXT_UPDATE = "order_context_update"

# Deterministic variant-bound price / budget-quantity reply (generic catalog).
ACTION_VARIANT_PRICING = "variant_pricing"

# Customer promised a future bank transfer while awaiting receipt proof.
ACTION_PAYMENT_TRANSFER_PROMISE = "payment_transfer_promise"
ACTION_PRODUCT_MEDIA_IDENTITY = "product_media_identity"
ACTION_CATALOG_NAVIGATE = "catalog_navigate"
ACTION_SELECT_PURCHASE_CHANNEL = "select_purchase_channel"

ALL_ACTIONS = [
    ACTION_GREET,
    ACTION_FAQ_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SUGGEST_COUPON,
    ACTION_CUSTOMER_COUPON_REQUEST,
    ACTION_TRACK_ORDER,
    ACTION_CUSTOMER_LEDGER_REPLY,
    ACTION_PAYMENT_CONTINUATION_REPLY,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_RECOMMEND_ADDON,
    ACTION_WEB_SEARCH,
    ACTION_OUT_OF_SCOPE,
    ACTION_CLARIFY,
    ACTION_NARROW,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
    ACTION_SOCIAL_REPLY,
    ACTION_PLATFORM_REPLY,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_VARIANT_PRICING,
    ACTION_PAYMENT_TRANSFER_PROMISE,
    ACTION_PRODUCT_MEDIA_IDENTITY,
    ACTION_CATALOG_NAVIGATE,
    ACTION_SELECT_PURCHASE_CHANNEL,
]
