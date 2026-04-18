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
INTENT_WHO_ARE_YOU   = "who_are_you"
INTENT_ASK_PRODUCT   = "ask_product"
INTENT_ASK_PRICE     = "ask_price"
INTENT_START_ORDER   = "start_order"
INTENT_PAY_NOW       = "pay_now"
INTENT_ASK_SHIPPING  = "ask_shipping"
INTENT_ASK_STORE_INFO = "ask_store_info"
INTENT_ASK_OWNER_CONTACT = "ask_owner_contact"
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
class OrderPreparationState:
    """Structured checkout-preparation state persisted inside the conversation."""
    quantity: int = 1
    customer_first_name: str = ""
    customer_last_name: str = ""
    customer_email: str = ""
    city: str = ""
    short_address_code: str = ""
    google_maps_url: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    building_number: str = ""
    additional_number: str = ""
    street: str = ""
    district: str = ""
    postal_code: str = ""
    address_line: str = ""
    resolution_source: str = ""
    missing_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quantity": self.quantity,
            "customer_first_name": self.customer_first_name,
            "customer_last_name": self.customer_last_name,
            "customer_email": self.customer_email,
            "city": self.city,
            "short_address_code": self.short_address_code,
            "google_maps_url": self.google_maps_url,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "building_number": self.building_number,
            "additional_number": self.additional_number,
            "street": self.street,
            "district": self.district,
            "postal_code": self.postal_code,
            "address_line": self.address_line,
            "resolution_source": self.resolution_source,
            "missing_fields": list(self.missing_fields or []),
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "OrderPreparationState":
        raw = d or {}
        return OrderPreparationState(
            quantity=_as_positive_int(raw.get("quantity"), default=1),
            customer_first_name=str(raw.get("customer_first_name", "") or ""),
            customer_last_name=str(raw.get("customer_last_name", "") or ""),
            customer_email=str(raw.get("customer_email", "") or ""),
            city=str(raw.get("city", "") or ""),
            short_address_code=str(raw.get("short_address_code", "") or ""),
            google_maps_url=str(raw.get("google_maps_url", "") or ""),
            latitude=_as_optional_float(raw.get("latitude")),
            longitude=_as_optional_float(raw.get("longitude")),
            building_number=str(raw.get("building_number", "") or ""),
            additional_number=str(raw.get("additional_number", "") or ""),
            street=str(raw.get("street", "") or ""),
            district=str(raw.get("district", "") or ""),
            postal_code=str(raw.get("postal_code", "") or ""),
            address_line=str(raw.get("address_line", "") or ""),
            resolution_source=str(raw.get("resolution_source", "") or ""),
            missing_fields=[
                str(item).strip()
                for item in (raw.get("missing_fields") or [])
                if str(item).strip()
            ],
        )


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
    customer_goal: str = ""
    last_question_asked: str = ""
    last_question_answered: bool = True
    recommended_next_step: str = ""
    order_prep: OrderPreparationState = field(default_factory=OrderPreparationState)
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
            "customer_goal": self.customer_goal,
            "last_question_asked": self.last_question_asked,
            "last_question_answered": self.last_question_answered,
            "recommended_next_step": self.recommended_next_step,
            "order_prep": self.order_prep.to_dict(),
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
            customer_goal=d.get("customer_goal", ""),
            last_question_asked=d.get("last_question_asked", ""),
            last_question_answered=bool(d.get("last_question_answered", True)),
            recommended_next_step=d.get("recommended_next_step", ""),
            order_prep=OrderPreparationState.from_dict(d.get("order_prep")),
            turn=int(d.get("turn", 0)),
            updated_at=d.get("updated_at", ""),
        )


def _as_optional_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _as_positive_int(value: Any, default: int = 1) -> int:
    try:
        parsed = int(value or default)
        return max(parsed, 1)
    except Exception:
        return default


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
    store_description: str = ""
    store_contact_phone: str = ""
    store_contact_email: str = ""

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
    shipping_methods: List[str] = field(default_factory=list)
    shipping_notes: str = ""
    shipping_policy: str = ""
    support_hours: str = ""
    payment_methods: List[str] = field(default_factory=list)


@dataclass
class SuggestionSnapshot:
    """
    Lightweight post-decision recommendation for the next best conversational move.

    It is computed by the SuggestionEngine and used by:
      - Composer: to attach a natural CTA when useful
      - Logs / traces: to explain why the brain moved the customer forward
      - LLM fallback: to inject `recommended_next_step` without patch prompts
    """
    suggested_next_step: str = ""
    close_to_purchase: bool = False
    needs_follow_up_question: bool = False
    follow_up_question: str = ""
    coupon_logic_considered: bool = False
    discount_ok_now: bool = False
    route_to_checkout: bool = False


@dataclass
class BrainReplyState:
    """
    Explicit structured state injected into every MerchantBrain LLM call.

    The LLM sees this as the current world model for the conversation instead of
    inferring it from a long system prompt full of exceptions.
    """
    store_name: str = ""
    tone: str = "neutral"
    stage: str = "discovery"
    customer_goal: str = ""
    selected_product: Optional[Dict[str, Any]] = None
    price_sensitivity: str = "moderate"
    known_facts: Dict[str, Any] = field(default_factory=dict)
    last_question_asked: str = ""
    last_question_answered: bool = True
    recommended_next_step: str = ""
    coupon_policy: Dict[str, Any] = field(default_factory=dict)
    recent_turns: List[str] = field(default_factory=list)
    policy_reason: str = ""


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
    suggestion: Optional[SuggestionSnapshot] = None
    reply_state: Optional[BrainReplyState] = None


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
