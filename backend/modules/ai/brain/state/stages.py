"""
brain/state/stages.py
──────────────────────
Stage constants for the merchant conversation state machine.

Stages represent the *conversation progress*, not the intent. A customer
can send any message at any stage; the stage tells the brain how to
interpret and prioritise actions.

  discovery  → greeted, browsing, no product selected
  exploring  → customer is asking about one or more specific products
  deciding   → customer has a product focus, showing intent to buy
  ordering   → order creation in progress, awaiting confirmation
  checkout   → draft order created, payment link sent
  complete   → order confirmed / paid
  support    → human handoff requested or triggered
"""

STAGE_DISCOVERY = "discovery"
STAGE_EXPLORING = "exploring"
STAGE_DECIDING  = "deciding"
STAGE_ORDERING  = "ordering"
STAGE_CHECKOUT  = "checkout"
STAGE_COMPLETE  = "complete"
STAGE_SUPPORT   = "support"

ALL_STAGES = [
    STAGE_DISCOVERY,
    STAGE_EXPLORING,
    STAGE_DECIDING,
    STAGE_ORDERING,
    STAGE_CHECKOUT,
    STAGE_COMPLETE,
    STAGE_SUPPORT,
]
