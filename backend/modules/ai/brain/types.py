"""
brain/types.py
──────────────
Core data types shared across every Brain layer.

These types form the "contract" between layers. Changing a field here is a
breaking change — add Optional fields for backward compatibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Intent
# ─────────────────────────────────────────────────────────────────────────────

INTENT_GREETING      = "greeting"
INTENT_ASK_PRODUCT   = "ask_product"
INTENT_ASK_PRICE     = "ask_price"
INTENT_START_ORDER   = "start_order"
INTENT_PAY_NOW       = "pay_now"
INTENT_ASK_SHIPPING  = "ask_shipping"
INTENT_HESITATION    = "hesitation"
INTENT_TALK_HUMAN    = "talk_to_human"
INTENT_TRACK_ORDER   = "track_order"
INTENT_GENERAL       = "general"


@dataclass
class Intent:
    """Result of the IntentLayer: what does the customer want?"""
    name: str
    confidence: float               # 0.0 – 1.0
    slots: Dict[str, Any] = field(default_factory=dict)
    # Useful slot keys: product_query, product_id, quantity, price_range, order_id
    raw_message: str = ""
    extraction_method: str = "rules"  # "rules" | "llm" | "hybrid"


# ─────────────────────────────────────────────────────────────────────────────
# Conversation State
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MerchantConversationState:
    """
    Persistent state of a merchant-customer conversation.
    Serialised to / from Conversation.extra_metadata['brain_state'].
    """
    stage: str = "discovery"
    greeted: bool = False
    last_intent: str = INTENT_GENERAL
    current_product_focus: Optional[Dict[str, Any]] = None   # {id, title, price, external_id}
    draft_order_id: Optional[str] = None
    checkout_url: Optional[str] = None
    turn: int = 0
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "greeted": self.greeted,
            "last_intent": self.last_intent,
            "current_product_focus": self.current_product_focus,
            "draft_order_id": self.draft_order_id,
            "checkout_url": self.checkout_url,
            "turn": self.turn,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "MerchantConversationState":
        return MerchantConversationState(
            stage=d.get("stage", "discovery"),
            greeted=bool(d.get("greeted", False)),
            last_intent=d.get("last_intent", INTENT_GENERAL),
            current_product_focus=d.get("current_product_focus"),
            draft_order_id=d.get("draft_order_id"),
            checkout_url=d.get("checkout_url"),
            turn=int(d.get("turn", 0)),
            updated_at=d.get("updated_at", ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Commerce Facts
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CommerceFacts:
    """
    Operational snapshot of the store's real-world state, loaded before every turn.

    Phase 1 fields: basic booleans + counts.
    Phase 2 fields (marked below): richer data for smarter decisions.
    """
    # ── Phase 1 ───────────────────────────────────────────────────────────────
    has_products: bool = False
    product_count: int = 0
    has_active_integration: bool = False
    has_coupons: bool = False
    snapshot_fresh: bool = False
    blocked_categories: List[str] = field(default_factory=list)
    store_name: str = ""
    store_url: str = ""

    # ── Phase 2 ───────────────────────────────────────────────────────────────
    # Number of products actually in stock (not just synced)
    in_stock_count: int = 0
    # True when integration is active AND at least one product is in stock
    orderable: bool = False
    # Best available coupon code for this turn (empty string = none)
    coupon_eligibility: str = ""
    # Top 5 products for greeting / discovery response
    top_products: List[Dict[str, Any]] = field(default_factory=list)
    # Platform driving the store: "salla" | "zid" | "shopify" | "manual" | "unknown"
    integration_platform: str = "unknown"
    # Whether the store is within configured working hours (None = no config = always open)
    within_working_hours: Optional[bool] = None


# ─────────────────────────────────────────────────────────────────────────────
# Brain Context — assembled once per turn, passed through all layers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BrainContext:
    """
    Full context for a single conversation turn.
    Created by Pipeline.process() and passed read-only to Decision / Execution /
    Composer layers (they may add to it via ActionResult, not mutate it).
    """
    tenant_id: int
    customer_phone: str
    message: str
    intent: Intent
    state: MerchantConversationState
    facts: CommerceFacts
    history: List[Dict[str, Any]] = field(default_factory=list)   # [{direction, body, created_at}]
    profile: Dict[str, Any] = field(default_factory=dict)          # from memory loader
    customer_id: Optional[int] = None
    conversation_id: Optional[int] = None


# ─────────────────────────────────────────────────────────────────────────────
# Decision + Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Decision:
    """
    The DecisionEngine's output: what action should be taken next?
    PolicyGate may modify this before execution.
    """
    action: str         # ActionType constant from decision/actions.py
    args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""    # human-readable explainability string
    confidence: float = 1.0


@dataclass
class ActionResult:
    """Return value from the ExecutionLayer."""
    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
