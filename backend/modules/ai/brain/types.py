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
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # avoid runtime import cycles
    from modules.ai.security import TenantContext


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
INTENT_HESITATION       = "hesitation"
INTENT_TALK_HUMAN       = "talk_to_human"
INTENT_TRACK_ORDER      = "track_order"
INTENT_GENERAL          = "general"
INTENT_PICK_LIST_ITEM   = "pick_list_item"   # customer picks numbered option


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
    country: str = ""
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
    # Tracks which product this prep belongs to (used to detect product change)
    product_id: str = ""
    # Set to True when a Salla order creation attempt failed with this data
    last_order_failed: bool = False
    # Counts consecutive Salla failures for this order (for escalation logic)
    salla_failure_count: int = 0
    # Cached shipping company/zone ID resolved from Salla (avoids re-fetching each turn)
    shipping_company_id: Optional[int] = None
    # ── Product options (variants) ───────────────────────────────────────
    # `product_options_meta` holds the option groups fetched once from the
    # store (id, name, required, values=[{id,name}]). We cache it on the
    # prep so we don't re-fetch every turn while the customer picks values.
    # `product_options` holds the customer's selection so far, keyed by the
    # lowercased option name → {"option_id", "option_name", "value_id",
    # "value_name"}. When all required option groups are selected the
    # adapter receives them in OrderItemInput.options.
    product_options_meta: List[Dict[str, Any]] = field(default_factory=list)
    product_options: Dict[str, Any] = field(default_factory=dict)
    product_has_required_options: bool = False
    # Set when the platform (Salla) returns no product for the given id —
    # i.e. the product identifier we have is wrong / not synced. Order
    # creation MUST be blocked while this is True.
    product_unsyncable: bool = False
    # True once `_ensure_product_options_loaded` has had ONE successful
    # call to adapter.get_product(). Critical: a successful response with
    # an EMPTY options array (a simple product) must still flip this so
    # we do NOT re-hit Salla on every turn. The previous code keyed off
    # `product_options_meta` truthiness, which made simple products
    # re-fetch forever — and a single transient empty Salla response
    # mid-flow falsely flagged the product as unsyncable.
    product_options_loaded: bool = False
    # Raw variant dicts from Salla (with related_options/related_option_values).
    # Cached alongside product_options_meta so variant_id can be resolved
    # locally without an extra API call before order creation.
    product_variants_raw: List[Dict[str, Any]] = field(default_factory=list)
    # ── Predicted options (Intent-Driven Prediction) ─────────────────────
    # When options are missing and the system can predict them with
    # sufficient confidence, the prediction is stored here INSTEAD of
    # directly in product_options. The customer must confirm before
    # predictions are promoted to real selections.
    predicted_options: Dict[str, Any] = field(default_factory=dict)
    prediction_source: str = ""       # last_customer_choice | top_variant | stock_heavy
    prediction_confidence: float = 0.0
    awaiting_option_confirmation: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quantity": self.quantity,
            "customer_first_name": self.customer_first_name,
            "customer_last_name": self.customer_last_name,
            "customer_email": self.customer_email,
            "city": self.city,
            "country": self.country,
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
            "product_id": self.product_id,
            "last_order_failed": self.last_order_failed,
            "salla_failure_count": self.salla_failure_count,
            "shipping_company_id": self.shipping_company_id,
            "product_options_meta": list(self.product_options_meta or []),
            "product_options": dict(self.product_options or {}),
            "product_has_required_options": self.product_has_required_options,
            "product_unsyncable": self.product_unsyncable,
            "product_options_loaded": self.product_options_loaded,
            "product_variants_raw": list(self.product_variants_raw or []),
            "predicted_options": dict(self.predicted_options or {}),
            "prediction_source": self.prediction_source,
            "prediction_confidence": self.prediction_confidence,
            "awaiting_option_confirmation": self.awaiting_option_confirmation,
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "OrderPreparationState":
        raw = d or {}
        _sid = raw.get("shipping_company_id")
        return OrderPreparationState(
            quantity=_as_positive_int(raw.get("quantity"), default=1),
            customer_first_name=str(raw.get("customer_first_name", "") or ""),
            customer_last_name=str(raw.get("customer_last_name", "") or ""),
            customer_email=str(raw.get("customer_email", "") or ""),
            city=str(raw.get("city", "") or ""),
            country=str(raw.get("country", "") or ""),
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
            product_id=str(raw.get("product_id", "") or ""),
            last_order_failed=bool(raw.get("last_order_failed", False)),
            salla_failure_count=int(raw.get("salla_failure_count") or 0),
            shipping_company_id=int(_sid) if _sid is not None else None,
            product_options_meta=list(raw.get("product_options_meta") or []),
            product_options=dict(raw.get("product_options") or {}),
            product_has_required_options=bool(raw.get("product_has_required_options", False)),
            product_unsyncable=bool(raw.get("product_unsyncable", False)),
            product_options_loaded=bool(raw.get("product_options_loaded", False)),
            product_variants_raw=list(raw.get("product_variants_raw") or []),
            predicted_options=dict(raw.get("predicted_options") or {}),
            prediction_source=str(raw.get("prediction_source", "") or ""),
            prediction_confidence=float(raw.get("prediction_confidence") or 0.0),
            awaiting_option_confirmation=bool(raw.get("awaiting_option_confirmation", False)),
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
    # Last ≤3 products shown as numbered list — used to resolve numeric picks
    last_search_candidates: List[Dict[str, Any]] = field(default_factory=list)
    # Last recent turns cached in state so thin-LLM and automations can share
    # the same short-term memory without re-querying unrelated tables.
    recent_messages: List[Dict[str, Any]] = field(default_factory=list)
    # Rolling summary mirrored from ConversationHistorySummary when available.
    conversation_summary: str = ""
    # Commercial state needed to complete the order inside chat.
    cart_items: List[Dict[str, Any]] = field(default_factory=list)
    selected_variant: Optional[Dict[str, Any]] = None
    payment_method: str = ""
    pending_action: str = ""
    last_recommended_products: List[Dict[str, Any]] = field(default_factory=list)
    # Address signals captured BEFORE a product was picked (e.g. customer
    # sent "TAPA7401" while browsing). Stashed here so we don't ask again
    # once the product is selected. Cleared once consumed by the order
    # flow.
    pending_short_address_code: str = ""
    pending_google_maps_url: str = ""
    pending_city: str = ""
    # Most recent brain action (`propose_draft_order`, `search_products`,
    # `stash_address_pre_product`, …) — used for the BRAIN_RESULT trace
    # log and the `/debug/recent-whatsapp-turns` endpoint.
    last_action: str = ""
    # Number of consecutive turns where the customer's intent was GENERAL
    # (unrecognised / off-topic). Reset to 0 whenever a specific intent fires.
    # Used by RealPolicyGate._auto_escalate for a real streak check instead of
    # the crude turn-counter proxy.
    general_streak: int = 0
    # Snapshot of the currently selected product options {group_name: value_name}.
    # Set by the pipeline after each successful option pick so the decision engine
    # can detect "options_pending" without importing orders.py.
    current_selected_options: Dict[str, Any] = field(default_factory=dict)
    # Names of option groups still pending (e.g. ["المقاس"]). Empty once all
    # options have been collected. Set by the pipeline after each turn.
    pending_option_groups: List[str] = field(default_factory=list)
    # True when the system proposed predicted options and is waiting for
    # the customer to confirm or reject them. Synced from order_prep by
    # the pipeline after each turn.
    awaiting_option_confirmation: bool = False

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
            "last_search_candidates": self.last_search_candidates,
            "recent_messages": self.recent_messages,
            "conversation_summary": self.conversation_summary,
            "cart_items": self.cart_items,
            "selected_variant": self.selected_variant,
            "payment_method": self.payment_method,
            "pending_action": self.pending_action,
            "last_recommended_products": self.last_recommended_products,
            "pending_short_address_code": self.pending_short_address_code,
            "pending_google_maps_url": self.pending_google_maps_url,
            "pending_city": self.pending_city,
            "last_action": self.last_action,
            "general_streak": self.general_streak,
            "current_selected_options": self.current_selected_options,
            "pending_option_groups": list(self.pending_option_groups or []),
            "awaiting_option_confirmation": self.awaiting_option_confirmation,
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
            last_search_candidates=list(d.get("last_search_candidates") or []),
            recent_messages=list(d.get("recent_messages") or []),
            conversation_summary=str(d.get("conversation_summary", "") or ""),
            cart_items=list(d.get("cart_items") or []),
            selected_variant=d.get("selected_variant"),
            payment_method=str(d.get("payment_method", "") or ""),
            pending_action=str(d.get("pending_action", "") or ""),
            last_recommended_products=list(d.get("last_recommended_products") or []),
            pending_short_address_code=str(d.get("pending_short_address_code", "") or ""),
            pending_google_maps_url=str(d.get("pending_google_maps_url", "") or ""),
            pending_city=str(d.get("pending_city", "") or ""),
            last_action=str(d.get("last_action", "") or ""),
            general_streak=int(d.get("general_streak", 0) or 0),
            current_selected_options=dict(d.get("current_selected_options") or {}),
            pending_option_groups=[
                str(g) for g in (d.get("pending_option_groups") or []) if g
            ],
            awaiting_option_confirmation=bool(d.get("awaiting_option_confirmation", False)),
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
class SalesContextSnapshot:
    """
    Unified turn-level sales context shared by MerchantBrain and the canonical
    AI orchestration path.

    The goal is to give every layer one stable object instead of rebuilding
    ad-hoc prompt dicts in multiple places.
    """
    store_profile: Dict[str, Any] = field(default_factory=dict)
    store_policies: Dict[str, Any] = field(default_factory=dict)
    customer_profile: Dict[str, Any] = field(default_factory=dict)
    customer_preferences: Dict[str, Any] = field(default_factory=dict)
    conversation_memory: Dict[str, Any] = field(default_factory=dict)
    offer_signals: Dict[str, Any] = field(default_factory=dict)
    recommendations: List[Dict[str, Any]] = field(default_factory=list)
    repeat_purchase_candidates: List[Dict[str, Any]] = field(default_factory=list)
    web_search_policy: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "store_profile": self.store_profile,
            "store_policies": self.store_policies,
            "customer_profile": self.customer_profile,
            "customer_preferences": self.customer_preferences,
            "conversation_memory": self.conversation_memory,
            "offer_signals": self.offer_signals,
            "recommendations": self.recommendations,
            "repeat_purchase_candidates": self.repeat_purchase_candidates,
            "web_search_policy": self.web_search_policy,
        }


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
    conversation_summary: str = ""
    store_knowledge: Dict[str, Any] = field(default_factory=dict)
    customer_memory: Dict[str, Any] = field(default_factory=dict)
    last_recommended_products: List[Dict[str, Any]] = field(default_factory=list)
    explicit_pending_action: str = ""
    tenant_overlay: str = ""
    # ── Merchant context (Phase 2 — Step 2 wire-up) ──────────────────────────
    # Slim, fact-grounded snapshot from `core.store_knowledge.build_merchant_context`.
    # Surfaced to the LLM via `asdict(state)` in compose/prompt_builder.py.
    # Intentionally excludes FAQ and insights at this step.
    merchant_context: Dict[str, Any] = field(default_factory=dict)
    # Decision-engine context surfaced to the LLM so the model never has to
    # guess "why am I being asked to compose now?". These two fields close
    # the loop the user explicitly asked for: the LLM fallback receives
    # intent + state + current product + response goal in one struct.
    intent_name: str = ""
    response_goal: str = ""


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
    sales_context: Optional[SalesContextSnapshot] = None
    # Single source of truth for "which tenant is this turn about?".
    # Built once at the top of the pipeline and forwarded to every layer
    # so no downstream code re-derives or re-validates the tenant id.
    tenant_context: Optional["TenantContext"] = None
    # Full merchant context snapshot from `build_merchant_context(...)`.
    # Empty dict when the call failed (pipeline degrades silently).
    merchant_context: Dict[str, Any] = field(default_factory=dict)


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
