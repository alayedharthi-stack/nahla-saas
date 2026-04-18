"""
modules/ai/brain
─────────────────
Merchant Brain — Phase 1 Commerce Decision Engine.

Public surface:
    from backend.modules.ai.brain import MerchantBrain, build_default_brain
    from backend.modules.ai.brain.types import (
        Intent, MerchantConversationState, CommerceFacts, BrainContext,
        Decision, ActionResult,
    )
"""
from .pipeline import MerchantBrain, build_default_brain  # noqa: F401
from .types import (  # noqa: F401
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_HESITATION,
    INTENT_PAY_NOW,
    INTENT_START_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
