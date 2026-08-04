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
    from modules.ai.commerce.permissions import CommercePermissionSet
    from modules.ai.security import TenantContext


# ─────────────────────────────────────────────────────────────────────────────
# Intent
# ─────────────────────────────────────────────────────────────────────────────

INTENT_GREETING      = "greeting"
INTENT_WHO_ARE_YOU   = "who_are_you"
INTENT_COMPLAINT_REFUND = "complaint_refund"
INTENT_PRODUCT_FEEDBACK = "product_feedback"
INTENT_ASK_PRODUCT   = "ask_product"
INTENT_PRODUCT_VISUAL_REQUEST = "product_visual_request"
INTENT_ASK_PRICE     = "ask_price"
INTENT_START_ORDER   = "start_order"
INTENT_PAY_NOW       = "pay_now"
INTENT_ASK_SHIPPING  = "ask_shipping"
INTENT_ASK_STORE_INFO = "ask_store_info"
INTENT_ONLINE_STORE_INQUIRY = "online_store_inquiry"
# Working-hours / open-closed inquiries — Layer 0 deterministic reply when
# ``support_hours`` / ``working_hours`` is configured on the tenant.
INTENT_ASK_WORKING_HOURS = "ask_working_hours"
# Polite session close — Layer 0 farewell templates (no LLM).
INTENT_FAREWELL = "farewell"
# Physical-location / Google-Maps / branch-address questions. Carved
# out of the broader STORE_INFO bucket so the brain can deliver the
# Maps URL deterministically — instead of falling back to the
# e-commerce ``store_url`` template, which used to silently swap a
# storefront link in for "وين موقعكم؟". Routed to ``ACTION_FAQ_REPLY``
# with ``topic="location"`` and resolved via the new maps URL chain
# (snapshot.maps_url → store_settings.google_maps_location → KB
# section kind=branches body URL). See May 2026 #36 / Phase 1
# diagnosis report.
INTENT_ASK_LOCATION       = "ask_location"
INTENT_ASK_OWNER_CONTACT = "ask_owner_contact"
# Bank-transfer / IBAN / payment-barcode / QR style requests. Carved out
# of the broader OWNER_CONTACT bucket so the brain can attach a matching
# AI Media Library item (e.g. the bank-transfer barcode) instead of
# falling back to the static "contact us" FAQ template — which used to
# silently swallow these messages and leave the customer with a generic
# "هذه وسائل التواصل المتاحة" reply.
INTENT_ASK_PAYMENT_INFO = "ask_payment_info"
# Cash-on-delivery / pay-on-receipt inquiries — answered from tenant
# payment policy evidence, not LLM invention.
INTENT_ASK_COD = "ask_cash_on_delivery"
INTENT_HESITATION       = "hesitation"
INTENT_TALK_HUMAN       = "talk_to_human"
# Follow-up when a previously suggested staff contact did not respond
# ("ما رد" / "اتصلت عليه وما رد"). Distinct from fresh handoff
# (``INTENT_TALK_HUMAN``). Phase 1: detection + memory + telemetry only.
INTENT_EMPLOYEE_NOT_RESPONDING = "employee_not_responding"
# Playful / emotional / social persona probes — affection, appearance
# compliment, tease, mild upset. Routed to persona_social LLM compose;
# NOT deterministic templates.
INTENT_PERSONA_INTERACTION = "persona_interaction"
INTENT_TRACK_ORDER      = "track_order"
INTENT_ORDER_HISTORY_COUNT = "order_history_count"
INTENT_LATEST_ORDER_SUMMARY = "latest_order_summary"
# "وش أرقامها؟" / "أرسل أرقام الطلبات" — the customer asks for the *references*
# of their own previous orders, not a count and not a single latest summary.
# Value doubles as the ``ledger_topic`` passed to the answerer, so the intent
# and the topic must stay literally equal.
INTENT_ORDER_REFERENCE_LIST = "order_reference_list"
INTENT_GENERAL          = "general"
INTENT_PICK_LIST_ITEM   = "pick_list_item"   # customer picks numbered option
# Social / courtesy / religious signals — thanks ("جزاك الله خير"),
# blessings ("الله يعافيك")، prophet invocations ("صلى الله عليه
# وسلم")، basmala ("بسم الله"), compliments ("كفو", "ما قصرت").
# These messages carry no commercial intent and MUST NOT trigger the
# product / catalog / KB / LLM-expansion paths — they get a short,
# culturally-appropriate canned reply via ACTION_SOCIAL_REPLY.
# Slot: ``social_category`` ∈ {thanks, blessing, prophet_invocation,
# basmala, compliment, general_courtesy}.
INTENT_SOCIAL           = "social"
# Customer is asking about NAHLA (the SaaS platform) itself — not
# the merchant's products. Subscription, API, dashboard, Meta /
# WhatsApp Business linking, campaigns, AI capabilities, packages,
# pricing of Nahla. The merchant brain MUST NOT try to answer these
# with the merchant's product catalogue — the answer comes via
# ACTION_PLATFORM_REPLY with a short scoping line that points the
# customer at Nahla support without inventing platform facts.
# Slot: ``platform_topic`` ∈ {subscription, integration, api,
# ai_capabilities, campaigns, dashboard, meta_connection,
# general_platform}.
INTENT_PLATFORM_INQUIRY = "platform_inquiry"
# Advisory need-based product questions — health / use-case oriented
# ("عسل ما يرفع السكر"، "عطر ثابت"، "جوال بطاريته قوية"). NOT a request
# to name a SKU; route to solution-seeking commerce advisory.
#
# Canonical intent name; legacy string kept for backward compatibility.
INTENT_SOLUTION_SEEKING_COMMERCE = "solution_seeking_commerce"
INTENT_NEED_BASED_PRODUCT_ADVICE = INTENT_SOLUTION_SEEKING_COMMERCE
# Slot: ``need_category`` / ``solution_axis`` ∈ closed axes from
# ``brain.commerce.solution_seeking`` (health_diet, audience_age, …).


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
    # WhatsApp conversation phone — auto-filled; never ask customer unless missing/invalid.
    customer_phone: str = ""
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
    # ── Payment-receipt funnel (bank-transfer flow) ──────────────────────
    # When the bot asks the customer to send a transfer receipt
    # (PDF/image), ``awaiting_payment_receipt`` flips True. The next
    # inbound PDF/image while this flag is set is short-circuited to
    # the "receipt-received" deterministic acknowledgement (no LLM,
    # no product re-discovery). After acknowledgement,
    # ``payment_receipt_received=True`` and ``order_status``
    # transitions to ``"under_review"``.
    #
    # ``payment_receipt_metadata`` keeps a slim trail of which
    # inbound message carried the receipt — useful for the merchant
    # drawer to deep-link back to the source PDF and for audits.
    #
    # ``order_status`` is the high-level funnel marker the dashboard
    # and admin debug endpoints expose. Values follow the natural
    # order: ``""`` (none) → ``"discovery"`` → ``"awaiting_product"``
    # → ``"awaiting_address"`` → ``"awaiting_payment"`` →
    # ``"awaiting_receipt"`` → ``"under_review"`` → ``"complete"`` /
    # ``"cancelled"``. Free-form so we can refine without a
    # migration; the brain only reads, never enforces an enum.
    awaiting_payment_receipt: bool = False
    payment_receipt_received: bool = False
    payment_receipt_at:       str = ""
    payment_receipt_metadata: Dict[str, Any] = field(default_factory=dict)
    order_status: str = ""
    # ── Order creation evidence (P1-C-1) ───────────────────────────────
    # ``creating`` | ``created`` | ``failed`` — stamped by DraftOrderHandler
    # so track-order and outbound guards speak honestly about Salla state.
    order_creation_status: str = ""
    salla_order_id: str = ""
    # ── Text-only payment claim understanding flag (Wave 1, W1.1) ────
    # Stamped by ``core.payment_intent._stamp_text_claim_unverified_state``
    # when the customer says "حولت" / "تم التحويل" without attached
    # media. Wave 1 commits W1.2 / W1.4 will consume these to drive
    # the brain's prompt overlay. They live as first-class fields on
    # ``OrderPreparationState`` so :class:`brain.state.store.DefaultStateStore`
    # cannot silently drop them on its full ``brain_state`` replace
    # in ``save()`` — the diagnostic identified that drop as a
    # structural risk.
    #
    # NEVER imply confirmed payment. They are pure understanding
    # signals. ``payment_receipt_received`` / ``order_status`` are
    # not touched by the stamp helper.
    payment_claim_unverified:    bool = False
    payment_claim_unverified_at: str = ""
    payment_claim_text_preview:  str = ""
    # PR-4 — multi-item WhatsApp cart mirror (consumed by nahla_order_bridge).
    line_items: List[Dict[str, Any]] = field(default_factory=list)
    cart_deltas: List[Dict[str, Any]] = field(default_factory=list)
    # Native catalog checkout total (session-only until DB draft exists).
    catalog_checkout_total: Optional[float] = None
    catalog_checkout_currency: str = ""
    checkout_channel: str = ""
    awaiting_checkout_channel: bool = False
    catalog_line_items_authoritative: bool = False
    product_mentions: List[Dict[str, Any]] = field(default_factory=list)
    # Gift / recipient delivery (P0 gift-order gate)
    recipient_name: str = ""
    fulfillment_kind: str = ""
    pending_cart_confirmation: Dict[str, Any] = field(default_factory=dict)
    pending_delivery_location: Dict[str, Any] = field(default_factory=dict)
    # Turn-local clarification when bare qty/variant needs product mapping (Phase 2).
    active_order_quantity_clarification: str = ""
    # ── Variant choice gate (migration 0064 — Phase 3) ───────────────────
    # When the resolver returns a parent with 2+ in-stock variants, the
    # responder ships ``ask_product_variants`` instead of the product
    # card and flips ``awaiting_variant_choice=True``. The customer's
    # next message (numeric / variant label) is then routed to a
    # variant pick. ``pending_variant_product_id`` stores the parent's
    # ``Product.id`` so the engine knows which variants to consider on
    # the reply turn. The variant-aware sender reads
    # ``selected_variant_id`` / ``selected_variant_retailer_id`` to
    # ship the correct per-SKU Meta card.
    awaiting_variant_choice: bool = False
    pending_variant_product_id: str = ""
    selected_variant_id:          str = ""
    selected_variant_retailer_id: str = ""

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
            "customer_phone": str(self.customer_phone or ""),
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
            "awaiting_payment_receipt": self.awaiting_payment_receipt,
            "payment_receipt_received": self.payment_receipt_received,
            "payment_receipt_at":       self.payment_receipt_at,
            "payment_receipt_metadata": dict(self.payment_receipt_metadata or {}),
            "order_status":             self.order_status,
            "order_creation_status":    self.order_creation_status,
            "salla_order_id":           self.salla_order_id,
            "payment_claim_unverified":    self.payment_claim_unverified,
            "payment_claim_unverified_at": self.payment_claim_unverified_at,
            "payment_claim_text_preview":  self.payment_claim_text_preview,
            "line_items": list(self.line_items or []),
            "cart_deltas": list(self.cart_deltas or []),
            "catalog_checkout_total": self.catalog_checkout_total,
            "catalog_checkout_currency": str(self.catalog_checkout_currency or ""),
            "checkout_channel": str(self.checkout_channel or ""),
            "awaiting_checkout_channel": bool(self.awaiting_checkout_channel),
            "catalog_line_items_authoritative": bool(self.catalog_line_items_authoritative),
            "product_mentions": list(self.product_mentions or []),
            "recipient_name": str(self.recipient_name or ""),
            "fulfillment_kind": str(self.fulfillment_kind or ""),
            "pending_cart_confirmation": dict(self.pending_cart_confirmation or {}),
            "pending_delivery_location": dict(self.pending_delivery_location or {}),
            "active_order_quantity_clarification": str(self.active_order_quantity_clarification or ""),
            "awaiting_variant_choice":  self.awaiting_variant_choice,
            "pending_variant_product_id": self.pending_variant_product_id,
            "selected_variant_id":        self.selected_variant_id,
            "selected_variant_retailer_id": self.selected_variant_retailer_id,
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
            customer_phone=str(raw.get("customer_phone", "") or ""),
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
            awaiting_payment_receipt=bool(raw.get("awaiting_payment_receipt", False)),
            payment_receipt_received=bool(raw.get("payment_receipt_received", False)),
            payment_receipt_at=str(raw.get("payment_receipt_at", "") or ""),
            payment_receipt_metadata=dict(raw.get("payment_receipt_metadata") or {}),
            order_status=str(raw.get("order_status", "") or ""),
            order_creation_status=str(raw.get("order_creation_status", "") or ""),
            salla_order_id=str(raw.get("salla_order_id", "") or ""),
            payment_claim_unverified=bool(raw.get("payment_claim_unverified", False)),
            payment_claim_unverified_at=str(raw.get("payment_claim_unverified_at", "") or ""),
            payment_claim_text_preview=str(raw.get("payment_claim_text_preview", "") or ""),
            line_items=list(raw.get("line_items") or []),
            cart_deltas=list(raw.get("cart_deltas") or []),
            catalog_checkout_total=_as_optional_float(raw.get("catalog_checkout_total")),
            catalog_checkout_currency=str(raw.get("catalog_checkout_currency") or ""),
            checkout_channel=str(raw.get("checkout_channel", "") or ""),
            awaiting_checkout_channel=bool(raw.get("awaiting_checkout_channel", False)),
            catalog_line_items_authoritative=bool(
                raw.get("catalog_line_items_authoritative", False)
            ),
            product_mentions=[
                dict(x) for x in (raw.get("product_mentions") or []) if isinstance(x, dict)
            ],
            recipient_name=str(raw.get("recipient_name", "") or ""),
            fulfillment_kind=str(raw.get("fulfillment_kind", "") or ""),
            pending_cart_confirmation=dict(raw.get("pending_cart_confirmation") or {}),
            pending_delivery_location=dict(raw.get("pending_delivery_location") or {}),
            active_order_quantity_clarification=str(
                raw.get("active_order_quantity_clarification", "") or ""
            ),
            awaiting_variant_choice=bool(raw.get("awaiting_variant_choice", False)),
            pending_variant_product_id=str(raw.get("pending_variant_product_id", "") or ""),
            selected_variant_id=str(raw.get("selected_variant_id", "") or ""),
            selected_variant_retailer_id=str(raw.get("selected_variant_retailer_id", "") or ""),
        )


@dataclass
class MerchantConversationState:
    """
    Persistent state of a merchant-customer conversation.
    Serialised to / from Conversation.extra_metadata['brain_state'].
    """
    stage: str = "discovery"
    greeted: bool = False
    # Set to True the first time the bot introduces itself by name /
    # role / "I am AI" inside this conversation. Once set, the LLM
    # prompt and the deterministic templates BOTH must avoid repeating
    # "أنا نحلة" / "أنا مستشارة" / "أنا ذكاء اصطناعي" — they only
    # surface again if the customer explicitly asks the identity FAQ
    # (which the brain detects via INTENT_WHO_ARE_YOU). This is what
    # closes the "البوت يعرّف نفسه في كل رسالة" production complaint.
    assistant_identity_introduced: bool = False
    last_intent: str = INTENT_GENERAL
    current_product_focus: Optional[Dict[str, Any]] = None   # {id, title, price, external_id}
    # Prior product focus — used for return-to-first / ordinal correction without
    # phrase-owned reply trees. Written only by commerce_focus_owner.
    previous_product_focus: Optional[Dict[str, Any]] = None
    # Snapshot preserved across shipping / tracking digressions.
    suspended_product_focus: Optional[Dict[str, Any]] = None
    # Active conversation focus mode: product | order_tracking | shipping_policy | "".
    conversation_focus: str = ""
    draft_order_id: Optional[str] = None
    checkout_url: Optional[str] = None
    customer_goal: str = ""
    # Multi-turn topic memory for suppression (delivery/payment/support/…).
    recent_topic: str = ""
    recent_topic_turn: int = 0
    last_fallback_fingerprint: str = ""
    last_fallback_turn: int = 0
    # Last turn we returned salam — avoids repetitive salam spam.
    last_salam_return_turn: int = 0
    last_salam_return_level: str = ""
    # Turn when ``current_product_focus`` was last set or reinforced.
    product_focus_turn: int = 0
    # Turn when a product card/image was last sent — decays faster than text focus.
    visual_focus_turn: int = 0
    # Semantically repaired inbound used for intent classification this turn.
    last_inbound_canonical: str = ""
    last_inbound_canonical_turn: int = 0
    last_question_asked: str = ""
    last_question_answered: bool = True
    recommended_next_step: str = ""
    order_prep: OrderPreparationState = field(default_factory=OrderPreparationState)
    turn: int = 0
    updated_at: str = ""
    # Last products shown as numbered list — used to resolve numeric picks
    last_search_candidates: List[Dict[str, Any]] = field(default_factory=list)
    # Full fetched pool + cursor for progressive "باقي الخيارات" browsing.
    catalog_browse_pool: List[Dict[str, Any]] = field(default_factory=list)
    catalog_browse_offset: int = 0
    last_browse_query: str = ""
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
    pending_delivery_location: Dict[str, Any] = field(default_factory=dict)
    # Most recent brain action (`propose_draft_order`, `search_products`,
    # `stash_address_pre_product`, …) — used for the BRAIN_RESULT trace
    # log and the `/debug/recent-whatsapp-turns` endpoint.
    last_action: str = ""
    # Shadow/future enforce: last resolved PresentationMode for this turn
    # (price_only | visual | discovery_list | …). Empty when unclassified.
    last_presentation_mode: str = ""
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
    # ── Conversation-context memory (May 2026) ───────────────────────────
    # Topic of the LAST platform-inquiry turn (subscription / integration /
    # api / meta_connection / …). Persists across turns so a follow-up
    # "نعم" / "كيف؟" is resolved as platform continuation rather than
    # going through commerce intents again. Cleared the moment the brain
    # routes to a non-platform action (product / price / order / etc.).
    last_platform_topic: str = ""
    # Short tag describing what the bot's previous question/offer would
    # be confirmed by a bare "نعم" / "طيب" / "أرسل". Examples:
    #   "send_platform_link"      → resend last platform CTA
    #   "send_payment_link"       → resend the checkout URL
    #   "send_product_card"       → re-send the current product card
    #   "explain_product"         → expand on current_product_focus
    # Cleared automatically the next turn the bot DOESN'T ask a yes/no.
    pending_confirmation: str = ""
    # Multi-turn conversation objective (product-origin verification, …).
    # Short TTL — cleared on explicit purchase intent or topic shift.
    active_conversation_objective: str = ""
    objective_started_turn: int = 0
    objective_last_reinforced_turn: int = 0
    objective_evidence: Dict[str, Any] = field(default_factory=dict)
    # Last outbound CTA URL surfaced to the customer + how many turns ago.
    # Used by the repetition guard to avoid sending the same link twice in
    # a row. Updated by the responder after each successful outbound.
    last_link_sent: str = ""
    last_link_sent_turn: int = 0
    # ── Customer gender (May 2026 — light personalisation layer) ──────────
    # Sticky classification from the gender detector
    # (modules/ai/gender/detector.py). Used ONLY by the
    # ACTION_SOCIAL_REPLY branch of the composer to pick a
    # female-coded conjugation when confidence is high enough. Empty
    # string means "unknown / never classified" — Arabic's masculine
    # default applies. Confidence is decayed mildly each turn until a
    # fresh signal reinforces it; see detector.py for the cascade.
    customer_gender_hint: str = ""
    customer_gender_confidence: float = 0.0
    customer_gender_source: str = ""
    # Staff contact cards already dispatched this conversation.
    # Each entry: {"name", "phone", "turn"}. Generic across merchants;
    # escalation-chain logic (Phase 2) derives ordering from KB/config.
    staff_contacts_sent: List[Dict[str, Any]] = field(default_factory=list)
    # Active commerce funnel lock (category/product/variant/order stage).
    commerce_session: Dict[str, Any] = field(default_factory=dict)
    # Phase 2 — session commerce objective (independent from stage).
    commerce_objective: str = ""
    commerce_objective_turn: int = 0
    commerce_objective_evidence: Dict[str, Any] = field(default_factory=dict)
    # Last resolved discovery strategy mode for progressive browse continuity.
    last_discovery_mode: str = ""
    # Phase 4B — selection context after discovery presentation.
    last_presented_products: List[Dict[str, Any]] = field(default_factory=list)
    last_presented_collections: List[Dict[str, Any]] = field(default_factory=list)
    selected_product_id: str = ""
    selected_variant_id: str = ""
    selected_collection: str = ""
    selection_context_turn: int = 0
    current_catalog_group: Optional[Dict[str, Any]] = None
    last_presented_group_products: List[Dict[str, Any]] = field(default_factory=list)
    collections_pool: List[Dict[str, Any]] = field(default_factory=list)
    collections_offset: int = 0
    collections_page_size: int = 0
    collections_next_available: bool = False
    group_products_pool: List[Dict[str, Any]] = field(default_factory=list)
    group_products_offset: int = 0
    group_products_page_size: int = 0
    next_page_available: bool = False
    catalog_navigation_source: str = ""
    native_catalog_send_failed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "greeted": self.greeted,
            "assistant_identity_introduced": self.assistant_identity_introduced,
            "last_intent": self.last_intent,
            "current_product_focus": self.current_product_focus,
            "previous_product_focus": self.previous_product_focus,
            "suspended_product_focus": self.suspended_product_focus,
            "conversation_focus": self.conversation_focus,
            "draft_order_id": self.draft_order_id,
            "checkout_url": self.checkout_url,
            "customer_goal": self.customer_goal,
            "recent_topic": self.recent_topic,
            "recent_topic_turn": self.recent_topic_turn,
            "last_fallback_fingerprint": self.last_fallback_fingerprint,
            "last_fallback_turn": self.last_fallback_turn,
            "last_salam_return_turn": self.last_salam_return_turn,
            "last_salam_return_level": self.last_salam_return_level,
            "product_focus_turn": self.product_focus_turn,
            "visual_focus_turn": self.visual_focus_turn,
            "last_inbound_canonical": self.last_inbound_canonical,
            "last_inbound_canonical_turn": self.last_inbound_canonical_turn,
            "last_question_asked": self.last_question_asked,
            "last_question_answered": self.last_question_answered,
            "recommended_next_step": self.recommended_next_step,
            "order_prep": self.order_prep.to_dict(),
            "turn": self.turn,
            "updated_at": self.updated_at,
            "last_search_candidates": self.last_search_candidates,
            "catalog_browse_pool": self.catalog_browse_pool,
            "catalog_browse_offset": self.catalog_browse_offset,
            "last_browse_query": self.last_browse_query,
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
            "pending_delivery_location": dict(self.pending_delivery_location or {}),
            "last_action": self.last_action,
            "last_presentation_mode": self.last_presentation_mode,
            "general_streak": self.general_streak,
            "current_selected_options": self.current_selected_options,
            "pending_option_groups": list(self.pending_option_groups or []),
            "awaiting_option_confirmation": self.awaiting_option_confirmation,
            "last_platform_topic": self.last_platform_topic,
            "pending_confirmation": self.pending_confirmation,
            "active_conversation_objective": self.active_conversation_objective,
            "objective_started_turn": self.objective_started_turn,
            "objective_last_reinforced_turn": self.objective_last_reinforced_turn,
            "objective_evidence": dict(self.objective_evidence or {}),
            "last_link_sent": self.last_link_sent,
            "last_link_sent_turn": self.last_link_sent_turn,
            "customer_gender_hint": self.customer_gender_hint,
            "customer_gender_confidence": self.customer_gender_confidence,
            "customer_gender_source": self.customer_gender_source,
            "staff_contacts_sent": list(self.staff_contacts_sent or []),
            "commerce_session": dict(self.commerce_session or {}),
            "commerce_objective": self.commerce_objective,
            "commerce_objective_turn": self.commerce_objective_turn,
            "commerce_objective_evidence": dict(self.commerce_objective_evidence or {}),
            "last_discovery_mode": self.last_discovery_mode,
            "last_presented_products": self.last_presented_products,
            "last_presented_collections": self.last_presented_collections,
            "selected_product_id": self.selected_product_id,
            "selected_variant_id": self.selected_variant_id,
            "selected_collection": self.selected_collection,
            "selection_context_turn": self.selection_context_turn,
            "current_catalog_group": self.current_catalog_group,
            "last_presented_group_products": self.last_presented_group_products,
            "collections_pool": self.collections_pool,
            "collections_offset": self.collections_offset,
            "collections_page_size": self.collections_page_size,
            "collections_next_available": self.collections_next_available,
            "group_products_pool": self.group_products_pool,
            "group_products_offset": self.group_products_offset,
            "group_products_page_size": self.group_products_page_size,
            "next_page_available": self.next_page_available,
            "catalog_navigation_source": self.catalog_navigation_source,
            "native_catalog_send_failed": self.native_catalog_send_failed,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "MerchantConversationState":
        return MerchantConversationState(
            stage=d.get("stage", "discovery"),
            greeted=bool(d.get("greeted", False)),
            assistant_identity_introduced=bool(d.get("assistant_identity_introduced", False)),
            last_intent=d.get("last_intent", INTENT_GENERAL),
            current_product_focus=d.get("current_product_focus"),
            previous_product_focus=d.get("previous_product_focus"),
            suspended_product_focus=d.get("suspended_product_focus"),
            conversation_focus=str(d.get("conversation_focus", "") or ""),
            draft_order_id=d.get("draft_order_id"),
            checkout_url=d.get("checkout_url"),
            customer_goal=d.get("customer_goal", ""),
            recent_topic=str(d.get("recent_topic", "") or ""),
            recent_topic_turn=int(d.get("recent_topic_turn") or 0),
            last_fallback_fingerprint=str(d.get("last_fallback_fingerprint", "") or ""),
            last_fallback_turn=int(d.get("last_fallback_turn") or 0),
            last_salam_return_turn=int(d.get("last_salam_return_turn") or 0),
            last_salam_return_level=str(d.get("last_salam_return_level", "") or ""),
            product_focus_turn=int(d.get("product_focus_turn") or 0),
            visual_focus_turn=int(d.get("visual_focus_turn") or 0),
            last_inbound_canonical=str(d.get("last_inbound_canonical", "") or ""),
            last_inbound_canonical_turn=int(d.get("last_inbound_canonical_turn") or 0),
            last_question_asked=d.get("last_question_asked", ""),
            last_question_answered=bool(d.get("last_question_answered", True)),
            recommended_next_step=d.get("recommended_next_step", ""),
            order_prep=OrderPreparationState.from_dict(d.get("order_prep")),
            turn=int(d.get("turn", 0)),
            updated_at=d.get("updated_at", ""),
            last_search_candidates=list(d.get("last_search_candidates") or []),
            catalog_browse_pool=list(d.get("catalog_browse_pool") or []),
            catalog_browse_offset=int(d.get("catalog_browse_offset", 0) or 0),
            last_browse_query=str(d.get("last_browse_query", "") or ""),
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
            pending_delivery_location=dict(d.get("pending_delivery_location") or {}),
            last_action=str(d.get("last_action", "") or ""),
            last_presentation_mode=str(d.get("last_presentation_mode", "") or ""),
            general_streak=int(d.get("general_streak", 0) or 0),
            current_selected_options=dict(d.get("current_selected_options") or {}),
            pending_option_groups=[
                str(g) for g in (d.get("pending_option_groups") or []) if g
            ],
            awaiting_option_confirmation=bool(d.get("awaiting_option_confirmation", False)),
            last_platform_topic=str(d.get("last_platform_topic", "") or ""),
            pending_confirmation=str(d.get("pending_confirmation", "") or ""),
            active_conversation_objective=str(
                d.get("active_conversation_objective", "") or ""
            ),
            objective_started_turn=int(d.get("objective_started_turn") or 0),
            objective_last_reinforced_turn=int(
                d.get("objective_last_reinforced_turn") or 0
            ),
            objective_evidence=dict(d.get("objective_evidence") or {}),
            last_link_sent=str(d.get("last_link_sent", "") or ""),
            last_link_sent_turn=int(d.get("last_link_sent_turn") or 0),
            customer_gender_hint=str(d.get("customer_gender_hint", "") or ""),
            customer_gender_confidence=float(
                d.get("customer_gender_confidence") or 0.0
            ),
            customer_gender_source=str(d.get("customer_gender_source", "") or ""),
            staff_contacts_sent=list(d.get("staff_contacts_sent") or []),
            commerce_session=dict(d.get("commerce_session") or {}),
            commerce_objective=str(d.get("commerce_objective", "") or ""),
            commerce_objective_turn=int(d.get("commerce_objective_turn") or 0),
            commerce_objective_evidence=dict(d.get("commerce_objective_evidence") or {}),
            last_discovery_mode=str(d.get("last_discovery_mode", "") or ""),
            last_presented_products=list(d.get("last_presented_products") or []),
            last_presented_collections=list(d.get("last_presented_collections") or []),
            selected_product_id=str(d.get("selected_product_id", "") or ""),
            selected_variant_id=str(d.get("selected_variant_id", "") or ""),
            selected_collection=str(d.get("selected_collection", "") or ""),
            selection_context_turn=int(d.get("selection_context_turn") or 0),
            current_catalog_group=d.get("current_catalog_group"),
            last_presented_group_products=list(d.get("last_presented_group_products") or []),
            collections_pool=list(d.get("collections_pool") or []),
            collections_offset=int(d.get("collections_offset") or 0),
            collections_page_size=int(d.get("collections_page_size") or 0),
            collections_next_available=bool(d.get("collections_next_available", False)),
            group_products_pool=list(d.get("group_products_pool") or []),
            group_products_offset=int(d.get("group_products_offset") or 0),
            group_products_page_size=int(d.get("group_products_page_size") or 0),
            next_page_available=bool(d.get("next_page_available", False)),
            catalog_navigation_source=str(d.get("catalog_navigation_source", "") or ""),
            native_catalog_send_failed=bool(d.get("native_catalog_send_failed", False)),
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
    # Resolver audit — same chain as safety-net / CTA (Jun 2026 store inquiry fix).
    store_url_resolved: bool = False
    store_url_source: str = "none"
    store_url_resolve_reason: str = ""
    # Physical-location URL (Google / Apple / Waze maps) — populated
    # from the May 2026 #36 maps resolver chain. Empty string means
    # the merchant has not configured a maps URL anywhere; the FAQ
    # template falls back to an honest clarifying line in that case.
    maps_url: str = ""
    store_description: str = ""
    store_contact_phone: str = ""
    store_contact_email: str = ""
    # Merchant-configured assistant persona (TenantSettings.ai_settings.
    # assistant_name). Surfaced in greeting / identity templates so the
    # bot can introduce itself by the merchant's chosen name (default
    # "نحلة" — see core.tenant.DEFAULT_AI). Empty string means "use
    # the generic 'مساعد {store_name}' phrasing".
    assistant_name: str = ""

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
    # ── Platform-inquiry KB gateway (May 2026 — UX fix) ──────────────────────
    # When ACTION_PLATFORM_REPLY fires we still route away from catalogue
    # search, but ``manual_knowledge_base`` may contain real onboarding /
    # subscription / WhatsApp-docs written by the merchant team. Those
    # paragraphs pass through ``extract_platform_kb_excerpt(...)`` into
    # ``platform_kb_excerpt``. When non-empty the responder delegates to the
    # thin LLM path while ``prompt_builder`` swaps the Facts block for this
    # excerpt-only slice + anti-sales guardrails.
    platform_kb_mode: bool = False
    platform_topic: str = ""
    platform_kb_excerpt: str = ""
    # When True, suppress [PRODUCT:...] vocabulary and catalog escalation
    # for social / religious / greeting media (May 2026 non-commerce gate).
    non_commerce_block_mode: bool = False
    # Advisory need-based product guidance (diabetes-safe, colon, …).
    need_based_advice_mode: bool = False
    need_category: str = ""
    # True after the bot has introduced itself ("أنا نحلة" / "أنا
    # مساعدتك الذكية") once in this conversation. The HIGH PRIORITY
    # block reads this field and FORBIDS the LLM from re-introducing
    # in subsequent turns. Mirrors ``MerchantConversationState.
    # assistant_identity_introduced`` and is the production fix for
    # the "البوت يعرّف نفسه في كل رسالة" complaint.
    identity_already_introduced: bool = False
    # ── Relational frame (May 2026 #7 — semantic stance layer) ───────────────
    # Closed-enum classification of the customer's CURRENT stance for this
    # turn (see modules/ai/brain/intent/stance_detector.py). Empty string
    # means "unknown / no override" and the LLM behaves exactly as before.
    # When non-empty, the directive is prepended to ``response_goal`` so the
    # LLM reads the stance through the right lens (e.g. ``deferred`` ⇒ no
    # sales pitch; ``polite_close`` ⇒ no follow-up question). Surfaced for
    # observability — operators grep for ``relational_frame=`` in logs.
    relational_frame: str = ""
    relational_evidence: str = ""
    # Persona expression profile (Phase 3A) — suppresses commerce prompt
    # layers on persona_identity / persona_social compose turns.
    persona_expression_mode: bool = False
    persona_topic: str = ""
    persona_kind: str = ""
    # Contextual clarify compose (Phase 1 — not persona_expression layer).
    contextual_clarify_mode: bool = False
    ambiguity_class: str = ""
    clarification_evidence: Dict[str, Any] = field(default_factory=dict)
    # Intent priority overlay for compose (primary goal, no-echo rule).
    intent_priority_focus: str = ""
    primary_customer_goal: str = ""
    # Redacted model-input attestation (ids/counts/keys only) for architecture evidence.
    model_payload_attestation: Optional[Dict[str, Any]] = None


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
    # Human-Priority Mode flag (forwarded by the webhook when
    # ``should_skip_ai`` returns ``"human_priority"``). When True the
    # PolicyGate clamps aggressive actions (no payment links, no order
    # creation, no coupon push, no upsell) and the responder appends a
    # short reassurance line so the customer knows the team is on the
    # way without the AI competing with them. Strictly turn-local —
    # NOT persisted on the Conversation row.
    human_priority: bool = False
    # Non-commerce safety layer — blocks catalog / recommendation escalation.
    block_commerce_escalation: bool = False
    non_commerce_category: str = ""
    # Structured post-order commerce context loaded from
    # ``Conversation.extra_metadata`` (Phase A). Empty dict when absent.
    commerce_bundle: Dict[str, Any] = field(default_factory=dict)
    # Relational layer verdict for THIS turn (May 2026 — Tenant 33 #49,
    # Commit 1 of the relational architecture rollout). Computed in
    # ``Pipeline.process`` when ``RELATIONAL_LAYER_ENABLED`` is on.
    # Carries the conversation moment, lifecycle, sentiment, post-
    # purchase window and a non-imperative advisory the brain prompt
    # overlay can use later. STRICT architectural rule (pinned in
    # ``modules.ai.brain.relational.contracts``): this object MUST
    # NEVER fabricate business state; it may only influence tone /
    # framing / suppression / prioritisation downstream.
    # Commit 1 attaches the verdict but no consumer reads it yet —
    # zero behaviour change.
    relational_state: Optional[Any] = None
    # Phase 1 semantic turn interpreter — contextual repair before routing.
    # Populated in ``Pipeline.process`` when a short/ambiguous turn is
    # repaired using conversation anchors. Guards still enforce execution.
    semantic_interpretation: Optional[Any] = None
    raw_message: str = ""
    # State relevance verdict — blocks stale workflow resurrection.
    state_relevance: Optional[Any] = None
    # Structured goal-based regimen when KB hit exists (P0 goal commerce).
    goal_regimen_bundle: Optional[Any] = None
    # Customer Intent Priority verdict (AI-ARCH-007). Computed after
    # intent classification; consumed by clarification, discovery, compose.
    intent_priority: Optional[Any] = None
    # Social & Human Context Layer (P0) — computed after intent priority.
    social_human_context: Optional[Any] = None
    # Phase 1.5 — drop stale summary/history on emoji/status after long gap.
    fresh_social_context: bool = False
    fresh_social_context_reason: str = ""
    # Turn Understanding + Turn Arbiter — Phase 1 shadow (read-only).
    # Populated before decide(); telemetry completed after decide().
    # Never mutates routing or reply in Phase 1.
    turn_understanding_shadow: Optional[Any] = None
    turn_arbitration_shadow: Optional[Any] = None
    turn_shadow_telemetry: Optional[Any] = None
    # Phase 1 commerce turn contract — built pre-decide; shadow/log-only until Phase 2.
    commerce_turn_contract: Optional[Any] = None
    # PR-B1 — merchant operational policy hint from KB (shadow-only; no enforce).
    merchant_operational_policy_hint: Optional[Any] = None
    # Scoped trusted-context projection for Brain/Compose (structured facts only).
    trusted_context_projection: Optional[Dict[str, Any]] = None
    # Redacted model-input attestation (ids/counts/keys only) for architecture evidence.
    model_payload_attestation: Optional[Dict[str, Any]] = None
    # Tenant commerce permissions — loaded once per turn from commerce_permissions.
    commerce_permissions: Optional["CommercePermissionSet"] = None
    permission_source: str = ""


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
