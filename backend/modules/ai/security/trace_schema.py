"""
modules/ai/security/trace_schema.py
───────────────────────────────────
Anonymized turn-outcome event schema.

Every signal that may leave the per-tenant boundary (and end up in the
``cross_merchant_signals`` table consumed by global / vertical learning)
must conform to ``TraceEvent`` and pass ``validate_anonymized``.

Design rules
────────────
* Only categorical, statistical or bucketed fields are allowed.
* No raw text (messages, product titles, customer names, addresses).
* No identifiers (raw tenant_id, customer_id, phone, order id, product id).
* Tenant identity is always represented as ``tenant_hash`` produced by
  ``anonymize_tenant`` — a salted SHA-256 truncation that cannot be
  reversed without the salt.
* Money amounts are stored as **buckets** never as raw values.

The validator is intentionally strict; if a future field is needed it
must be added to ``ALLOWED_EXTRA_KEYS`` explicitly.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger("nahla.ai.security.trace")

# Bounded identifier contract shared with ``cross_merchant_signals.model_path``.
MODEL_PATH_MAX_LENGTH = 128


class ModelPathTooLongError(ValueError):
    """Raised when a structured telemetry path exceeds its storage contract."""

    def __init__(self, *, actual_length: int, max_length: int) -> None:
        self.actual_length = actual_length
        self.max_length = max_length
        super().__init__(
            f"model_path exceeds maximum length ({max_length})"
        )


# ── Categorical enums (string constants kept simple to JSON-serialise) ──

class OutcomeKind:
    """Coarse outcome of a turn from the merchant brain perspective."""
    GREET            = "greet"
    BROWSE           = "browse"
    PRODUCT_PRESENTED = "product_presented"
    OBJECTION        = "objection"
    ADDED_TO_CART    = "added_to_cart"
    CHECKOUT_STARTED = "checkout_started"
    PAYMENT_SENT     = "payment_sent"
    CONVERSION       = "conversion"
    ABANDONED        = "abandoned"
    HANDOFF          = "handoff"
    SUPPORT          = "support"
    ERROR            = "error"
    UNKNOWN          = "unknown"


class UIMode:
    """The UI surface used to render the assistant reply."""
    TEXT     = "text"
    BUTTONS  = "buttons"
    LIST     = "list"
    PRODUCT_CARDS = "product_cards"
    VOICE    = "voice"
    IMAGE    = "image"
    UNKNOWN  = "unknown"


class LearningTier:
    """Three-tier learning hierarchy.

    * ``GLOBAL``   — fully anonymized, useful for any merchant.
    * ``VERTICAL`` — anonymized but partitioned by industry.
    * ``MERCHANT`` — kept inside per-tenant tables only.  This tier MUST
                     never be written to ``CrossMerchantLearningStore``.
    """
    GLOBAL    = "global"
    VERTICAL  = "vertical"
    MERCHANT  = "merchant"


# ── Forbidden + allowed schema keys ─────────────────────────────────────

# These keys must NEVER appear on a TraceEvent or inside its ``extra`` dict.
# The list intentionally over-reports — false positives are cheap, but a
# missed leak is permanent.
FORBIDDEN_TRACE_KEYS: FrozenSet[str] = frozenset({
    # Identity
    "tenant_id", "customer_id", "user_id", "agent_id",
    # Contact info
    "phone", "phone_number", "customer_phone", "normalized_phone",
    "email", "customer_email",
    # PII
    "name", "customer_name", "first_name", "last_name", "full_name",
    "address", "street", "city", "district", "postal_code",
    "national_id", "iqama", "passport",
    # Conversation content
    "message", "message_text", "reply", "reply_text", "raw_message",
    "transcript", "summary_text",
    # Store-private
    "product_id", "external_id", "sku", "title", "product_title",
    "description", "store_url", "store_name",
    # Money
    "price", "amount", "total", "subtotal", "currency_amount",
    # Conversation refs
    "conversation_id", "session_id", "draft_order_id", "order_id",
    "checkout_url", "payment_link",
})

# Whitelisted keys allowed inside ``TraceEvent.extra`` — anything else is
# stripped at validation time so accidental additions cannot leak.
#
# Phase 1.8 additions (``hint_*``) describe the *presence* and *adoption*
# of a learned ``policy_hint`` on this turn.  All values are categorical
# (already validated upstream by the learner) or simple booleans, so they
# satisfy the same anti-leak guarantees as the rest of the schema:
#   * hint_present / hint_aligned / hint_used → bool
#   * hint_action / hint_ui / hint_scope     → categorical labels (same
#       shape as the existing ``action`` / ``ui_mode`` columns)
#   * hint_confidence_bucket / hint_sample_bucket → bucketed string
ALLOWED_EXTRA_KEYS: FrozenSet[str] = frozenset({
    "fact_guard_modified",
    "policy_modified",
    "had_recommendations",
    "had_repeat_purchase",
    "used_web_search",
    "had_voice_input",
    "had_buttons",
    "history_length_bucket",
    "intent_confidence_bucket",
    "tool_count",
    "stage_before",
    "stage_after",
    "decision_path",
    "model_family",
    "language",
    "rule_version",
    # ── Phase 1.8 adoption-measurement keys ──
    "hint_present",
    "hint_aligned",
    "hint_used",
    "hint_action",
    "hint_ui",
    "hint_scope",
    "hint_confidence_bucket",
    "hint_sample_bucket",
    # ── Phase 1.9 soft-bias keys ──
    # All values here are categorical / boolean labels produced by the
    # bias layer.  ``bias_type`` and ``bias_reason`` are bounded short
    # strings (e.g. "ui+choice_count" or "ready:uplift_0.12:n=500_2k");
    # ``final_ui_mode`` matches the ``UIMode`` ontology and
    # ``final_recommendation_shape`` is one of the constants returned by
    # ``_classify_recommendation_shape`` in the memory updater.
    "bias_applied",
    "bias_type",
    "bias_reason",
    "bias_intent",
    "bias_industry",
    "final_ui_mode",
    "final_recommendation_shape",
    "final_choice_count_bucket",
})


# ── Anonymization helpers ───────────────────────────────────────────────

def anonymize_tenant(tenant_id: int, *, salt: Optional[str] = None) -> str:
    """Return a salted SHA-256 hex digest (16 chars) for ``tenant_id``.

    The truncation is large enough to avoid practical collisions across
    realistic merchant counts but small enough to keep storage and logs
    compact.
    """
    if tenant_id is None:
        raise ValueError("tenant_id is required")
    if salt is None:
        try:
            from core.config import CROSS_MERCHANT_ANON_SALT
            salt = CROSS_MERCHANT_ANON_SALT
        except Exception:
            salt = "nahla-local-dev-salt-do-not-use-in-prod"
    payload = f"{salt}|{int(tenant_id)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def value_bucket(amount: Any, currency: str = "SAR") -> str:
    """Map a monetary amount to a coarse, currency-aware bucket label.

    Returns ``"unknown"`` for missing / non-numeric inputs.  Buckets are
    intentionally coarse so per-merchant pricing cannot be reconstructed
    from a sequence of events.
    """
    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        return "unknown"
    if value <= 0:
        return "zero"
    if value < 50:
        return "0_50"
    if value < 100:
        return "50_100"
    if value < 250:
        return "100_250"
    if value < 500:
        return "250_500"
    if value < 1000:
        return "500_1000"
    if value < 2500:
        return "1000_2500"
    if value < 5000:
        return "2500_5000"
    return "5000_plus"


def industry_of(tenant_settings: Any) -> str:
    """Best-effort industry tag derived from ``TenantSettings`` JSONB.

    The function is defensive — any access error returns ``"unknown"``.
    """
    if tenant_settings is None:
        return "unknown"
    try:
        store = getattr(tenant_settings, "store_settings", None) or {}
        if isinstance(store, dict):
            value = (
                store.get("industry")
                or store.get("vertical")
                or store.get("category")
            )
            if value:
                return str(value).strip().lower()
    except Exception:
        return "unknown"
    return "unknown"


def sanitize_extra(extra: Any) -> Dict[str, Any]:
    """Return a copy of ``extra`` with only whitelisted keys retained.

    Any forbidden / unknown key is dropped.  Values that are not JSON-safe
    primitives are coerced to string for safe storage.
    """
    if not isinstance(extra, dict):
        return {}
    clean: Dict[str, Any] = {}
    for k, v in extra.items():
        if k not in ALLOWED_EXTRA_KEYS:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            clean[k] = v
        else:
            clean[k] = str(v)
    return clean


def normalize_model_path(model_path: Any) -> str:
    """Normalize a structured routing path for cross-merchant telemetry."""
    normalized = (str(model_path or "rule")).strip().lower() or "rule"
    if len(normalized) > MODEL_PATH_MAX_LENGTH:
        raise ModelPathTooLongError(
            actual_length=len(normalized),
            max_length=MODEL_PATH_MAX_LENGTH,
        )
    return normalized


# ── Event dataclass ─────────────────────────────────────────────────────

@dataclass
class TraceEvent:
    """The only shape allowed to be persisted in the cross-merchant store."""
    tenant_hash: str
    industry: str
    intent: str
    action: str
    ui_mode: str
    outcome: str
    value_bucket: str = "unknown"
    turn_index: int = 0
    model_path: str = "rule"
    latency_ms: int = 0
    tier: str = LearningTier.GLOBAL
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_hash":  self.tenant_hash,
            "industry":     self.industry,
            "intent":       self.intent,
            "action":       self.action,
            "ui_mode":      self.ui_mode,
            "outcome":      self.outcome,
            "value_bucket": self.value_bucket,
            "turn_index":   self.turn_index,
            "model_path":   self.model_path,
            "latency_ms":   self.latency_ms,
            "tier":         self.tier,
            "extra":        dict(self.extra or {}),
        }


def validate_anonymized(event: TraceEvent) -> TraceEvent:
    """Validate and sanitise a ``TraceEvent`` before it is persisted.

    Raises ``ValueError`` when the event still contains forbidden raw
    fields.  Returns a fresh ``TraceEvent`` whose ``extra`` dict has been
    filtered through ``sanitize_extra``.
    """
    if not isinstance(event, TraceEvent):
        raise ValueError(f"expected TraceEvent, got {type(event).__name__}")

    if not event.tenant_hash or not isinstance(event.tenant_hash, str):
        raise ValueError("tenant_hash is required and must be str")
    if any(ch.isdigit() and len(event.tenant_hash) <= 4 for ch in event.tenant_hash):
        # Defensive: a too-short or pure-digit hash would basically be a
        # raw tenant id.  Reject it early so callers cannot bypass
        # ``anonymize_tenant``.
        if event.tenant_hash.isdigit():
            raise ValueError("tenant_hash looks like a raw tenant id")

    for forbidden in FORBIDDEN_TRACE_KEYS:
        if forbidden in (event.extra or {}):
            raise ValueError(
                f"TraceEvent.extra contains forbidden key '{forbidden}'"
            )

    # Tier guard — merchant-specific signals must not leave per-tenant tables.
    if event.tier == LearningTier.MERCHANT:
        raise ValueError(
            "merchant-tier events must not be written to the cross-merchant "
            "store; keep them in tenant-scoped tables"
        )
    if event.tier not in (LearningTier.GLOBAL, LearningTier.VERTICAL):
        raise ValueError(f"unknown learning tier: {event.tier!r}")

    # Hard normalise simple categorical fields
    cleaned = TraceEvent(
        tenant_hash  = event.tenant_hash,
        industry     = (event.industry or "unknown").strip().lower() or "unknown",
        intent       = (event.intent or "unknown").strip().lower() or "unknown",
        action       = (event.action or "unknown").strip().lower() or "unknown",
        ui_mode      = (event.ui_mode or UIMode.UNKNOWN).strip().lower() or UIMode.UNKNOWN,
        outcome      = (event.outcome or OutcomeKind.UNKNOWN).strip().lower() or OutcomeKind.UNKNOWN,
        value_bucket = (event.value_bucket or "unknown").strip().lower() or "unknown",
        turn_index   = max(int(event.turn_index or 0), 0),
        model_path   = normalize_model_path(event.model_path),
        latency_ms   = max(int(event.latency_ms or 0), 0),
        tier         = event.tier,
        extra        = sanitize_extra(event.extra),
    )
    return cleaned
