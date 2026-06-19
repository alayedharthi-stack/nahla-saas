from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Customer, CustomerProfile, Order
from observability.event_logger import log_event
from services.offer_decision_flags import DecisionMode, tenant_decision_mode
from utils.phone_utils import normalize_to_e164, normalize_phone_compat

logger = logging.getLogger("nahla.customer_intelligence")

# ── Name-source trust hierarchy ───────────────────────────────────────────────
# A name from a higher-trust source should never be overwritten by a lower-trust
# one.  For example, a Salla-synced full Arabic name like "محمد العمري" must not
# be replaced by the WhatsApp contact alias "Mohammed" that arrives with every
# inbound message.
#
# Scale: 10 = most authoritative, 0 = least.
_NAME_SOURCE_TRUST: Dict[str, int] = {
    "salla_sync":          10,  # Official store registry — highest authority
    "customer_webhook":    10,  # Salla customer.created/updated webhook
    "zid_sync":            10,  # Zid store sync (when integrated)
    "order_webhook":        8,  # Name extracted from a real placed order
    "order_sync":           8,
    "order":                8,
    # ``ai_detected_name`` — the customer explicitly said their name in
    # chat ("اسمي محمد"). Ranked between merchant typing (5) and order-
    # derived names (8): higher than a casual import row, lower than a
    # confirmed Salla order — because the AI extractor is conservative
    # but not infallible.
    "ai_detected_name":     6,
    "merchant_correction":  6,  # Merchant fixed it via the inline pencil
    "manual":               5,  # Merchant typed the name themselves
    "manual_import":        4,  # Uploaded CSV / XLSX
    "bulk_retry":           2,
    "whatsapp_inbound":     1,  # WhatsApp contact name — unreliable alias
    "whatsapp_lead":        1,
    "widget":               1,
}


def _name_trust_level(source: Optional[str]) -> int:
    """Return the trust level for a given customer-data source string."""
    return _NAME_SOURCE_TRUST.get(source or "", 0)


def _should_update_name(
    *,
    existing_name: Optional[str],
    existing_source: Optional[str],
    new_name: str,
    new_source: Optional[str],
) -> bool:
    """Decide whether *new_name* (from *new_source*) should replace
    *existing_name* (whose data came from *existing_source*).

    Rules:
    • If there is no existing name → always fill it.
    • If the new source has strictly lower trust → never overwrite.
    • Equal or higher trust → overwrite (e.g. Salla re-sync can correct a name).
    """
    if not new_name:
        return False
    if not (existing_name or "").strip():
        return True  # fill empty names unconditionally
    new_trust      = _name_trust_level(new_source)
    existing_trust = _name_trust_level(existing_source)
    return new_trust >= existing_trust


COUNTABLE_ORDER_STATUSES = frozenset(
    {
        "paid",
        "confirmed",
        "processing",
        "shipped",
        "out_for_delivery",
        "delivered",
        "completed",
    }
)
EXCLUDED_ORDER_STATUSES = frozenset(
    {
        "cancelled",
        "canceled",
        "failed",
        "payment_failed",
        "refunded",
        "voided",
        "abandoned",
        "draft",
    }
)

CUSTOMER_STATUS_ORDER = (
    "lead",
    "new",
    "active",
    "vip",
    "at_risk",
    "inactive",
)
CUSTOMER_STATUS_LABELS: Dict[str, str] = {
    "lead": "عميل محتمل",
    "new": "عميل جديد",
    "active": "عميل نشط",
    "vip": "عميل VIP",
    "at_risk": "في خطر المغادرة",
    "inactive": "غير نشط",
}

# Recent WhatsApp / profile contact within this window prevents a stale
# purchase-only verdict of ``inactive``.
RECENT_CONTACT_DAYS = 30

RFM_SEGMENT_ORDER = (
    "lead",
    "champions",
    "loyal_customers",
    "potential_loyalists",
    "new_customers",
    "promising",
    "needs_attention",
    "about_to_sleep",
    "at_risk",
    "cant_lose_them",
    "hibernating",
    "lost_customers",
    "regulars",
)
RFM_SEGMENT_LABELS: Dict[str, str] = {
    "lead": "عميل محتمل",
    "champions": "الأبطال",
    "loyal_customers": "عملاء أوفياء",
    "potential_loyalists": "مرشحون للولاء",
    "new_customers": "عملاء جدد",
    "promising": "واعدون",
    "needs_attention": "يحتاجون اهتمامًا",
    "about_to_sleep": "على وشك الخمول",
    "at_risk": "في خطر",
    "cant_lose_them": "لا يجب خسارتهم",
    "hibernating": "شبه خاملين",
    "lost_customers": "عملاء مفقودون",
    "regulars": "منتظمون",
}

_PHONE_DIGITS_RE = re.compile(r"[^\d]")


@dataclass(frozen=True)
class CustomerMetrics:
    total_orders: int
    total_spend_sar: float
    average_order_value_sar: float
    max_single_order_sar: float
    first_seen_at: Optional[datetime]
    last_seen_at: Optional[datetime]
    first_order_at: Optional[datetime]
    last_order_at: Optional[datetime]
    days_since_first_order: Optional[int]
    days_since_last_order: Optional[int]
    days_since_last_seen: Optional[int]


@dataclass(frozen=True)
class RFMScores:
    recency: int
    frequency: int
    monetary: int
    total: int
    code: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_name(raw: Any) -> str:
    return " ".join(str(raw or "").strip().lower().split())


def normalize_phone(raw: Any) -> str:
    """
    Normalize a phone number to E.164 format.
    Returns E.164 string on success, '' (falsy) on failure.

    Delegates to utils.phone_utils.normalize_to_e164 which uses
    Google's libphonenumber for international support.
    Backward-compatible: callers using `if not normalize_phone(x):`
    continue to work unchanged.
    """
    return normalize_phone_compat(raw)


def extract_order_datetime(raw: Any) -> Optional[datetime]:
    candidates: list[Any] = []

    if isinstance(raw, Order):
        meta = getattr(raw, "extra_metadata", None) or {}
        candidates.extend(
            [
                meta.get("created_at"),
                meta.get("updated_at"),
                getattr(raw, "created_at", None),
                getattr(raw, "updated_at", None),
            ]
        )
        raw = getattr(raw, "customer_info", None)

    if isinstance(raw, dict):
        candidates.extend(
            [
                raw.get("created_at"),
                raw.get("updated_at"),
                raw.get("date"),
                (raw.get("date") or {}).get("date")
                if isinstance(raw.get("date"), dict)
                else None,
            ]
        )

    for value in candidates:
        if isinstance(value, datetime):
            return ensure_utc(value)
        if not value:
            continue
        text = str(value).strip()
        if not text:
            continue
        for candidate in (
            text.replace("Z", "+00:00"),
            text.replace(" ", "T", 1),
            text.split(".", 1)[0].replace(" ", "T", 1),
        ):
            try:
                return ensure_utc(datetime.fromisoformat(candidate))
            except Exception:
                continue
    return None


def order_status_key(order_or_status: Any) -> str:
    """
    Return a normalized lowercase status string suitable for comparison
    against COUNTABLE_ORDER_STATUSES / EXCLUDED_ORDER_STATUSES.

    Heals legacy rows that stored the Salla status as a Python repr of the
    full status dict (e.g. "{'id': 566146469, 'name': '...',
    'slug': 'under_review'}") — extracts the slug at READ time so customer
    classification never depends on a backfill having run.
    """
    if isinstance(order_or_status, Order):
        text = str(getattr(order_or_status, "status", "") or "").strip()
    else:
        text = str(order_or_status or "").strip()

    if text.startswith("{"):
        try:
            import ast as _ast  # noqa: PLC0415
            parsed = _ast.literal_eval(text)
            if isinstance(parsed, dict):
                text = str(
                    parsed.get("slug")
                    or parsed.get("name")
                    or parsed.get("code")
                    or text
                )
        except (ValueError, SyntaxError):
            pass
    return text.strip().lower()


def parse_order_total(raw: Any) -> float:
    if isinstance(raw, Order):
        raw = getattr(raw, "total", None)
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        return 0.0
    try:
        return round(float(text), 2)
    except (TypeError, ValueError):
        return 0.0


def extract_order_customer_phone(order_or_payload: Any) -> str:
    info = {}
    if isinstance(order_or_payload, Order):
        info = getattr(order_or_payload, "customer_info", None) or {}
    elif isinstance(order_or_payload, dict):
        info = dict(order_or_payload.get("customer_info") or order_or_payload.get("customer") or {})
    return normalize_phone(info.get("mobile") or info.get("phone") or "")


def extract_order_customer_name(order_or_payload: Any) -> str:
    info = {}
    if isinstance(order_or_payload, Order):
        info = getattr(order_or_payload, "customer_info", None) or {}
    elif isinstance(order_or_payload, dict):
        info = dict(order_or_payload.get("customer_info") or order_or_payload.get("customer") or {})
    return str(info.get("name") or order_or_payload.get("customer_name") or "").strip() if isinstance(order_or_payload, dict) else str(info.get("name") or "").strip()


def is_countable_order(order_or_status: Any) -> bool:
    status = order_status_key(order_or_status)
    if status in EXCLUDED_ORDER_STATUSES:
        return False
    if isinstance(order_or_status, Order) and bool(getattr(order_or_status, "is_abandoned", False)):
        return False
    if status in COUNTABLE_ORDER_STATUSES:
        return True
    return status not in EXCLUDED_ORDER_STATUSES and bool(status)


def _days_since_last_seen(metrics: CustomerMetrics, now: datetime) -> Optional[int]:
    if metrics.days_since_last_seen is not None:
        return metrics.days_since_last_seen
    if metrics.last_seen_at is not None:
        return max(0, (now - ensure_utc(metrics.last_seen_at)).days)
    return None


def compute_customer_status(metrics: CustomerMetrics, now: Optional[datetime] = None) -> str:
    current_time = ensure_utc(now) or utcnow()
    if metrics.total_orders <= 0:
        return "lead"

    vip_candidate = (
        (metrics.total_spend_sar >= 2000 and metrics.total_orders >= 5)
        or metrics.total_spend_sar >= 3000
    )
    if vip_candidate:
        return "vip"

    is_new_window = (
        metrics.first_order_at is not None
        and metrics.days_since_first_order is not None
        and metrics.days_since_first_order <= 30
    )
    days_since_contact = _days_since_last_seen(metrics, current_time)
    has_recent_contact = (
        days_since_contact is not None
        and days_since_contact <= RECENT_CONTACT_DAYS
    )

    if has_recent_contact:
        if is_new_window:
            return "new"
        return "active"

    is_recent = (
        metrics.last_order_at is not None
        and metrics.days_since_last_order is not None
        and metrics.days_since_last_order <= 60
    )

    if is_recent and not is_new_window:
        return "active"

    if is_new_window:
        return "new"

    if (
        metrics.last_order_at is not None
        and metrics.days_since_last_order is not None
        and metrics.days_since_last_order <= 90
    ):
        return "at_risk"

    return "inactive"


def compute_churn_risk_score(metrics: CustomerMetrics) -> float:
    if metrics.total_orders <= 0 or metrics.days_since_last_order is None:
        return 0.0

    days_inactive = metrics.days_since_last_order
    if days_inactive <= 14:
        churn_risk = max(0.02, days_inactive * 0.005)
    elif days_inactive <= 30:
        churn_risk = 0.10 + (days_inactive - 14) * 0.008
    elif days_inactive <= 60:
        churn_risk = 0.23 + (days_inactive - 30) * 0.01
    elif days_inactive <= 90:
        churn_risk = 0.53 + (days_inactive - 60) * 0.008
    else:
        churn_risk = 0.77 + min((days_inactive - 90) * 0.002, 0.23)
    return round(min(churn_risk, 1.0), 3)


def compute_lifetime_value_score(metrics: CustomerMetrics) -> float:
    if metrics.total_orders <= 0:
        return 0.0
    base = metrics.total_spend_sar / 3000.0
    return round(min(max(base, 0.0), 1.0), 3)


def compute_rfm_scores(metrics: CustomerMetrics, now: Optional[datetime] = None) -> RFMScores:
    _ = now
    if metrics.total_orders <= 0 or metrics.last_order_at is None:
        return RFMScores(recency=0, frequency=0, monetary=0, total=0, code="000")

    days = metrics.days_since_last_order if metrics.days_since_last_order is not None else 999
    if days <= 7:
        recency = 5
    elif days <= 30:
        recency = 4
    elif days <= 60:
        recency = 3
    elif days <= 90:
        recency = 2
    else:
        recency = 1

    orders = metrics.total_orders
    if orders >= 10:
        frequency = 5
    elif orders >= 6:
        frequency = 4
    elif orders >= 3:
        frequency = 3
    elif orders >= 2:
        frequency = 2
    else:
        frequency = 1

    spend = metrics.total_spend_sar
    if spend >= 5000:
        monetary = 5
    elif spend >= 2500:
        monetary = 4
    elif spend >= 1000:
        monetary = 3
    elif spend >= 300:
        monetary = 2
    else:
        monetary = 1

    total = recency + frequency + monetary
    return RFMScores(
        recency=recency,
        frequency=frequency,
        monetary=monetary,
        total=total,
        code=f"{recency}{frequency}{monetary}",
    )


def compute_rfm_segment(scores: RFMScores, status: str) -> str:
    if status == "lead" or scores.total == 0:
        return "lead"

    r = scores.recency
    f = scores.frequency
    m = scores.monetary

    if r >= 5 and f >= 4 and m >= 4:
        return "champions"
    if r >= 3 and f >= 4 and m >= 3:
        return "loyal_customers"
    if r >= 4 and f >= 2 and m >= 2:
        return "potential_loyalists"
    if r == 5 and f == 1:
        return "new_customers"
    if r == 4 and f == 1:
        return "promising"
    if r in {2, 3} and f in {2, 3}:
        return "needs_attention"
    if r == 2 and f <= 2:
        return "about_to_sleep"
    if r == 1 and f >= 4:
        return "cant_lose_them"
    if r <= 2 and f >= 3:
        return "at_risk"
    if r == 1 and f <= 2:
        return "lost_customers"
    if r == 2:
        return "hibernating"
    return "regulars"


class CustomerIntelligenceService:
    def __init__(self, db: Session, tenant_id: int):
        self.db = db
        self.tenant_id = tenant_id

    def _query_customers(self) -> list[Customer]:
        return (
            self.db.query(Customer)
            .filter(Customer.tenant_id == self.tenant_id)
            .all()
        )

    def _find_customer_by_external_id(self, external_id: Optional[str]) -> Optional[Customer]:
        """
        Find a tenant-scoped customer by their Salla external ID.
        Priority 1: check the first-class salla_customer_id column (indexed, fast).
        Priority 2: JSONB metadata fallback for rows pre-dating migration 0031.
        Always scoped by tenant_id — never cross-tenant.
        """
        if not external_id:
            return None
        target = str(external_id).strip()
        if not target:
            return None

        # Fast path: first-class column (migration 0031+)
        by_column = (
            self.db.query(Customer)
            .filter(
                Customer.tenant_id == self.tenant_id,
                Customer.salla_customer_id == target,
            )
            .first()
        )
        if by_column:
            return by_column

        # Legacy path: JSONB metadata (pre-0031 rows)
        for customer in self._query_customers():
            meta = customer.extra_metadata or {}
            if str(meta.get("salla_id") or meta.get("external_id") or "").strip() == target:
                # Repair: promote to first-class column so next lookup is fast
                customer.salla_customer_id = target
                return customer
        return None

    def find_customer_by_phone(
        self,
        raw_phone: Any,
        *,
        exclude_customer_id: Optional[int] = None,
    ) -> Optional[Customer]:
        """
        Find a tenant-scoped customer by phone number.

        Lookup order:
        1. normalized_phone column (indexed, E.164) — fast and reliable.
        2. normalized phone compared against Customer.phone (legacy rows
           pre-dating migration 0032 that don't have normalized_phone set).
        Never crosses tenant boundaries.
        """
        e164 = normalize_to_e164(str(raw_phone or "").strip())
        if not e164:
            return None

        # Priority 1: normalized_phone column (migration 0032+)
        by_norm = (
            self.db.query(Customer)
            .filter(
                Customer.tenant_id == self.tenant_id,
                Customer.normalized_phone == e164,
            )
            .first()
        )
        if by_norm and by_norm.id != exclude_customer_id:
            return by_norm

        # Priority 2: legacy rows — compare normalized forms of Customer.phone
        for customer in self._query_customers():
            if exclude_customer_id is not None and customer.id == exclude_customer_id:
                continue
            if customer.id == (by_norm.id if by_norm else None):
                continue
            cust_e164 = normalize_to_e164(customer.phone)
            if cust_e164 and cust_e164 == e164:
                # Repair: populate normalized_phone for next time
                customer.normalized_phone = e164
                return customer
        return None

    def ensure_profile(
        self,
        customer: Customer,
        *,
        seen_at: Optional[datetime] = None,
    ) -> CustomerProfile:
        now = ensure_utc(seen_at) or utcnow()
        profile = (
            self.db.query(CustomerProfile)
            .filter(
                CustomerProfile.customer_id == customer.id,
                CustomerProfile.tenant_id == self.tenant_id,
            )
            .first()
        )
        if not profile:
            init_contact = ensure_utc(seen_at) if seen_at else None
            profile = CustomerProfile(
                customer_id=customer.id,
                tenant_id=self.tenant_id,
                segment="lead",
                customer_status="lead",
                rfm_segment="lead",
                first_seen_at=init_contact or utcnow(),
                last_seen_at=init_contact,
                metrics_computed_at=utcnow(),
                last_recomputed_reason="profile_initialized",
                updated_at=utcnow(),
            )
            self.db.add(profile)
            self.db.flush()

        if seen_at:
            now = ensure_utc(seen_at) or utcnow()
            if not profile.first_seen_at or ensure_utc(profile.first_seen_at) > now:
                profile.first_seen_at = now
            if not profile.last_seen_at or ensure_utc(profile.last_seen_at) < now:
                profile.last_seen_at = now
            profile.updated_at = now
        return profile

    def upsert_customer_identity(
        self,
        *,
        phone: Any = None,
        name: Optional[str] = None,
        email: Optional[str] = None,
        external_id: Optional[str] = None,
        source: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        seen_at: Optional[datetime] = None,
    ) -> Optional[Customer]:
        """
        Find-or-create a customer scoped to self.tenant_id.

        Identity resolution order (all tenant-scoped):
          1. salla_customer_id / external_id column (indexed, fast)
          2. normalized phone number
          3. normalized name (fuzzy fallback only)

        On creation: sets acquisition_channel, salla_customer_id, first_seen_at.
        On update: syncs salla_customer_id, updates last_interaction_at for WA.
        Never creates a customer across tenants.
        """
        normalized_phone = normalize_phone(phone)
        clean_name       = str(name or "").strip()
        clean_email      = str(email or "").strip() or None

        # ── Central name-adoption guard (Jun 2026 P0) ─────────────────────
        # Block outbound echoes, campaigns, automations, and any other
        # non-inbound channel from adopting message text as identity.
        # Trusted admin/import/order paths pass through; WhatsApp profile
        # hints remain proposed-only via the resolver.
        from core.customer_name_adoption_guard import (  # noqa: PLC0415
            filter_name_for_identity_upsert,
        )

        _explicit_entry = (source or "") == "ai_detected_name"
        clean_name, _name_mode = filter_name_for_identity_upsert(
            clean_name,
            source,
            explicit_customer_entry=_explicit_entry,
        )
        if _name_mode == "blocked":
            clean_name = ""

        if not any([normalized_phone, clean_name, clean_email, external_id]):
            return None

        now     = ensure_utc(seen_at) or utcnow()
        e164    = normalize_to_e164(str(phone or "").strip()) if phone else None
        # normalized_phone supersedes the old normalize_phone() result
        if e164:
            normalized_phone = e164

        # ── Identity resolution ───────────────────────────────────────────────
        customer = self._find_customer_by_external_id(external_id)
        if customer is None and e164:
            customer = self.find_customer_by_phone(e164)
        # WhatsApp channel identity is the sender phone — never merge two
        # different numbers onto one Customer row via profile-name equality.
        _wa_phone_authoritative_sources = frozenset({
            "whatsapp_inbound",
            "whatsapp_lead",
            "whatsapp_outbound_echo",
        })
        if (
            customer is None
            and clean_name
            and (source or "") not in _wa_phone_authoritative_sources
        ):
            normalized_name = normalize_name(clean_name)
            for row in self._query_customers():
                if normalize_name(row.name) == normalized_name:
                    customer = row
                    break

        # ── JSONB metadata (legacy compatibility) ─────────────────────────────
        metadata = dict(customer.extra_metadata or {}) if customer else {}
        metadata.update(extra_metadata or {})
        if external_id:
            metadata["salla_id"] = str(external_id)
        if source:
            metadata["source"] = source

        # ── Determine acquisition_channel from source string ──────────────────
        _channel_map = {
            "salla_sync":       "salla_sync",
            "customer_webhook": "salla_sync",
            "order_sync":       "order",
            "order_webhook":    "order",
            "whatsapp_inbound": "whatsapp_inbound",
            "whatsapp_lead":    "whatsapp_inbound",
            # May 2026 #37 — outbound echoes from the merchant's
            # mobile WA app (Coexistence ``smb_message_echoes``).
            # They MUST NOT bump ``last_interaction_at`` because
            # that field tracks customer-side activity for the
            # silence-detector / re-engagement window. Treating
            # echoes as inbound also forces a UPDATE customers
            # statement per merchant message — under contention
            # this hits the 5s statement_timeout and crashes the
            # echo branch. The echo channel is a distinct identity
            # source that finds-or-creates the customer row but
            # leaves the timestamp alone.
            "whatsapp_outbound_echo": "whatsapp_outbound_echo",
        }
        channel = _channel_map.get(source or "", source or None)

        if customer is None:
            # ── CREATE ────────────────────────────────────────────────────────
            # Store raw phone for display; normalized_phone (E.164) for identity.
            raw_phone_display = str(phone or "").strip() or None
            customer = Customer(
                tenant_id           = self.tenant_id,
                phone               = raw_phone_display,
                normalized_phone    = e164,
                name                = None,
                email               = clean_email,
                extra_metadata      = metadata or None,
                salla_customer_id   = str(external_id).strip() if external_id else None,
                acquisition_channel = channel,
                first_seen_at       = now,
                last_interaction_at = now if channel == "whatsapp_inbound" else None,
            )
            self.db.add(customer)
            self.db.flush()
            if clean_name:
                from core.customer_identity_resolver import apply_customer_name  # noqa: PLC0415

                platform = str((metadata or {}).get("store_platform") or "").strip() or None
                apply_customer_name(
                    customer,
                    clean_name,
                    source=source,
                    platform=platform,
                    explicit_customer_entry=(source or "") == "ai_detected_name",
                )
            logger.debug(
                "[CIS] customer CREATED | tenant=%s e164=%s salla_id=%s channel=%s",
                self.tenant_id, e164, external_id, channel,
            )
        else:
            # ── UPDATE ────────────────────────────────────────────────────────
            if phone:
                # Always overwrite phone with raw display value
                customer.phone = str(phone).strip() or customer.phone
            if e164:
                # Always keep normalized_phone up-to-date (E.164)
                customer.normalized_phone = e164

            # Name update: evidence-based resolver — trust hierarchy +
            # validator gate. Low-trust WhatsApp profile hints stay
            # proposed; verified store names never lose to chat filler.
            if clean_name:
                from core.customer_identity_resolver import apply_customer_name  # noqa: PLC0415

                platform = str((metadata or {}).get("store_platform") or "").strip() or None
                apply_customer_name(
                    customer,
                    clean_name,
                    source=source,
                    platform=platform,
                    explicit_customer_entry=(source or "") == "ai_detected_name",
                    force_merchant=(source or "") in {
                        "merchant_correction",
                        "manual",
                        "manual_admin",
                    },
                )

            if clean_email:
                customer.email = clean_email
            from core.customer_identity_resolver import merge_identity_metadata  # noqa: PLC0415

            metadata = merge_identity_metadata(metadata, customer)
            customer.extra_metadata = metadata or None
            if external_id and not customer.salla_customer_id:
                customer.salla_customer_id = str(external_id).strip()
            if channel == "whatsapp_inbound":
                customer.last_interaction_at = now
            if not customer.first_seen_at:
                customer.first_seen_at = now
            logger.debug(
                "[CIS] customer UPDATED | tenant=%s id=%s e164=%s channel=%s",
                self.tenant_id, customer.id, e164, channel,
            )

        self.ensure_profile(customer, seen_at=seen_at)
        return customer

    def persist_order_flow_contact_phone(
        self,
        *,
        channel_phone: str,
        contact_phone_raw: str,
    ) -> bool:
        """
        Store an alternate contact/shipping phone on the Customer row without
        rewriting WhatsApp channel identity (``normalized_phone``).

        ``channel_phone`` is the WA sender id used for lookup; ``contact_phone_raw``
        is the number the customer volunteered during checkout (may differ).
        """
        from utils.phone_utils import normalize_to_e164  # noqa: PLC0415

        channel_raw = str(channel_phone or "").strip()
        contact_raw = str(contact_phone_raw or "").strip()
        if not channel_raw or not contact_raw:
            return False

        channel_e164 = normalize_to_e164(channel_raw) or channel_raw
        contact_e164 = normalize_to_e164(contact_raw)
        if not contact_e164:
            return False

        customer = self.find_customer_by_phone(channel_e164)
        if customer is None and channel_raw != channel_e164:
            customer = self.find_customer_by_phone(channel_raw)
        if customer is None:
            logger.debug(
                "[CIS] contact_phone skip (no customer) tenant=%s channel=%s",
                self.tenant_id,
                channel_raw[:20],
            )
            return False

        existing_norm = (customer.normalized_phone or "").strip()
        if contact_e164 == existing_norm or contact_e164 == channel_e164:
            return False

        metadata = dict(customer.extra_metadata or {})
        if (
            metadata.get("contact_phone") == contact_e164
            and metadata.get("shipping_phone") == contact_e164
        ):
            return False

        metadata["contact_phone"] = contact_e164
        metadata["shipping_phone"] = contact_e164
        metadata["contact_phone_source"] = "ai_order_flow"
        metadata["contact_phone_updated_at"] = utcnow().isoformat()
        customer.extra_metadata = metadata
        logger.info(
            "[CUSTOMER_PROFILE_UPDATED] field=contact_phone tenant=%s "
            "customer_id=%s channel=%s contact=%s",
            self.tenant_id,
            customer.id,
            channel_e164,
            contact_e164,
        )
        return True

    def upsert_lead_customer(
        self,
        *,
        phone: Any,
        name: Optional[str] = None,
        email: Optional[str] = None,
        source: str = "whatsapp_lead",
        extra_metadata: Optional[Dict[str, Any]] = None,
        commit: bool = True,
    ) -> Optional[Customer]:
        now = utcnow()
        metadata = dict(extra_metadata or {})
        metadata.setdefault("lead_source", source)
        customer = self.upsert_customer_identity(
            phone=phone,
            name=name,
            email=email,
            source=source,
            extra_metadata=metadata,
            seen_at=now,
        )
        if customer is None:
            return None

        self.recompute_profile_for_customer(
            customer.id,
            reason=source,
            commit=commit,
            emit_event=True,
        )
        return customer

    def upsert_customer_from_order(
        self,
        order_payload: Dict[str, Any],
        *,
        source: str = "order_sync",
        commit: bool = False,
    ) -> Optional[Customer]:
        customer_info = dict(order_payload.get("customer_info") or {})
        extra_metadata = {
            "source": source,
            "last_order_external_id": str(order_payload.get("external_id") or ""),
        }
        customer = self.upsert_customer_identity(
            phone=customer_info.get("mobile") or customer_info.get("phone"),
            name=customer_info.get("name"),
            email=customer_info.get("email"),
            source=source,
            extra_metadata=extra_metadata,
            seen_at=extract_order_datetime(order_payload) or utcnow(),
        )
        if commit:
            self.db.commit()
        return customer

    def _index_orders(self, orders: Iterable[Order]) -> tuple[Dict[str, list[Order]], Dict[str, list[Order]]]:
        phone_index: Dict[str, list[Order]] = {}
        name_index: Dict[str, list[Order]] = {}
        for order in orders:
            phone = extract_order_customer_phone(order)
            if phone:
                phone_index.setdefault(phone, []).append(order)
            name = normalize_name(extract_order_customer_name(order))
            if name:
                name_index.setdefault(name, []).append(order)
        return phone_index, name_index

    def _orders_for_customer(
        self,
        customer: Customer,
        *,
        phone_index: Optional[Dict[str, list[Order]]] = None,
        name_index: Optional[Dict[str, list[Order]]] = None,
        orders: Optional[list[Order]] = None,
    ) -> list[Order]:
        if phone_index is None or name_index is None:
            base_orders = orders
            if base_orders is None:
                base_orders = (
                    self.db.query(Order)
                    .filter(Order.tenant_id == self.tenant_id)
                    .all()
                )
            phone_index, name_index = self._index_orders(base_orders)

        normalized_phone = normalize_phone(customer.phone)
        if not normalized_phone:
            normalized_phone = normalize_phone(
                getattr(customer, "normalized_phone", None) or ""
            )
        if normalized_phone and normalized_phone in phone_index:
            return list(phone_index.get(normalized_phone, []))

        return []

    def _build_metrics(
        self,
        customer: Customer,
        *,
        profile: Optional[CustomerProfile],
        orders: list[Order],
        now: Optional[datetime] = None,
    ) -> CustomerMetrics:
        current_time = ensure_utc(now) or utcnow()
        all_order_dates = [extract_order_datetime(order) for order in orders]
        all_order_dates = [dt for dt in all_order_dates if dt]

        countable_orders = [order for order in orders if is_countable_order(order)]
        countable_dates = [extract_order_datetime(order) for order in countable_orders]
        countable_dates = [dt for dt in countable_dates if dt]
        totals = [parse_order_total(order) for order in countable_orders]

        first_seen_candidates = [
            ensure_utc(profile.first_seen_at) if profile else None,
            min(all_order_dates) if all_order_dates else None,
        ]
        first_seen_at = min((dt for dt in first_seen_candidates if dt), default=None)

        last_seen_candidates = [
            ensure_utc(profile.last_seen_at) if profile else None,
            max(all_order_dates) if all_order_dates else None,
        ]
        last_seen_at = max((dt for dt in last_seen_candidates if dt), default=None)

        first_order_at = min(countable_dates) if countable_dates else None
        last_order_at = max(countable_dates) if countable_dates else None

        if first_seen_at is None:
            first_seen_at = ensure_utc(profile.first_seen_at) if profile else None
        if last_seen_at is None:
            last_seen_at = ensure_utc(profile.last_seen_at) if profile else None

        profile_contact_at = ensure_utc(profile.last_seen_at) if profile else None

        if first_seen_at is None and profile is not None:
            first_seen_at = ensure_utc(profile.metrics_computed_at) or current_time
        if last_seen_at is None and first_seen_at is not None:
            last_seen_at = first_seen_at

        days_since_first_order = (
            max(0, (current_time - first_order_at).days) if first_order_at else None
        )
        days_since_last_order = (
            max(0, (current_time - last_order_at).days) if last_order_at else None
        )
        # Engagement gating uses explicit profile contact (WhatsApp / ensure_profile),
        # not synthetic last_seen fallbacks from metrics_computed_at.
        days_since_last_seen = (
            max(0, (current_time - profile_contact_at).days)
            if profile_contact_at
            else None
        )

        total_spend_sar = round(sum(totals), 2)
        total_orders = len(countable_orders)

        return CustomerMetrics(
            total_orders=total_orders,
            total_spend_sar=total_spend_sar,
            average_order_value_sar=round(total_spend_sar / total_orders, 2) if total_orders else 0.0,
            max_single_order_sar=round(max(totals), 2) if totals else 0.0,
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            first_order_at=first_order_at,
            last_order_at=last_order_at,
            days_since_first_order=days_since_first_order,
            days_since_last_order=days_since_last_order,
            days_since_last_seen=days_since_last_seen,
        )

    def _apply_profile_metrics(
        self,
        profile: CustomerProfile,
        *,
        metrics: CustomerMetrics,
        status: str,
        rfm_scores: RFMScores,
        rfm_segment: str,
        reason: str,
        computed_at: datetime,
    ) -> None:
        profile.total_orders = metrics.total_orders
        profile.total_spend_sar = metrics.total_spend_sar
        profile.average_order_value_sar = metrics.average_order_value_sar
        profile.max_single_order_sar = metrics.max_single_order_sar
        profile.first_seen_at = metrics.first_seen_at
        profile.last_seen_at = metrics.last_seen_at
        profile.first_order_at = metrics.first_order_at
        profile.last_order_at = metrics.last_order_at
        profile.segment = status
        profile.customer_status = status
        profile.churn_risk_score = compute_churn_risk_score(metrics)
        profile.lifetime_value_score = compute_lifetime_value_score(metrics)
        profile.is_returning = metrics.total_orders > 1
        profile.rfm_recency_score = rfm_scores.recency
        profile.rfm_frequency_score = rfm_scores.frequency
        profile.rfm_monetary_score = rfm_scores.monetary
        profile.rfm_total_score = rfm_scores.total
        profile.rfm_code = rfm_scores.code
        profile.rfm_segment = rfm_segment
        profile.metrics_computed_at = computed_at
        profile.last_recomputed_reason = reason
        profile.updated_at = computed_at

    def recompute_profile_for_customer(
        self,
        customer_id: int,
        reason: str,
        *,
        commit: bool = True,
        emit_event: bool = True,
    ) -> Optional[CustomerProfile]:
        customer = (
            self.db.query(Customer)
            .filter(
                Customer.id == customer_id,
                Customer.tenant_id == self.tenant_id,
            )
            .first()
        )
        if customer is None:
            return None

        profile = self.ensure_profile(customer)
        old_status = profile.customer_status  # capture before recompute
        orders = self._orders_for_customer(customer)
        now = utcnow()
        metrics = self._build_metrics(customer, profile=profile, orders=orders, now=now)
        status = compute_customer_status(metrics, now)
        rfm_scores = compute_rfm_scores(metrics, now)
        rfm_segment = compute_rfm_segment(rfm_scores, status)
        self._apply_profile_metrics(
            profile,
            metrics=metrics,
            status=status,
            rfm_scores=rfm_scores,
            rfm_segment=rfm_segment,
            reason=reason,
            computed_at=now,
        )
        self.db.add(profile)

        # Emit customer_status_changed when the status actually transitions.
        # Also fire the event-driven coupon autogen for the new segment so the
        # merchant's coupon pool reflects the classification change in real
        # time (previously this only happened via the 6-hour scheduler).
        status_changed = bool(old_status and old_status != status)
        if status_changed:
            from core.obs import EVENTS as _EVENTS, log_event as _log_event  # noqa: PLC0415
            _log_event(
                _EVENTS.CUSTOMER_CLASSIFICATION_CHANGED,
                tenant_id=self.tenant_id,
                customer_id=customer.id,
                old_status=old_status,
                new_status=status,
                rfm_segment=rfm_segment,
                reason=reason,
            )
            try:
                from core.automation_engine import emit_automation_event  # noqa: PLC0415
                from core.automation_triggers import AutomationTrigger  # noqa: PLC0415

                payload = {
                    "from":        old_status,
                    "to":          status,
                    "rfm_segment": rfm_segment,
                    "reason":      reason,
                }

                # Always emit the generic status-change event — kept for
                # backward compat with any custom tenant automations.
                emit_automation_event(
                    self.db,
                    self.tenant_id,
                    "customer_status_changed",
                    customer_id=customer.id,
                    payload=payload,
                )

                # Emit specific triggers so the seeded automations
                # (customer_winback / vip_upgrade) — whose trigger_event was
                # normalised in migration 0024 to the canonical names below —
                # can match without relying on payload conditions alone.
                if status in ("inactive", "at_risk"):
                    emit_automation_event(
                        self.db,
                        self.tenant_id,
                        AutomationTrigger.CUSTOMER_INACTIVE.value,
                        customer_id=customer.id,
                        payload=payload,
                    )
                elif status == "vip":
                    emit_automation_event(
                        self.db,
                        self.tenant_id,
                        AutomationTrigger.VIP_CUSTOMER_UPGRADE.value,
                        customer_id=customer.id,
                        payload=payload,
                    )
            except Exception as exc:
                # Errors in automation emission used to be silently debug-logged,
                # hiding real outages. Now surfaced as ERROR with full stack.
                logger.exception(
                    "[Intelligence] emit automation events failed tenant=%s customer=%s: %s",
                    self.tenant_id, customer.id, exc,
                )

        if emit_event:
            log_event(
                self.db,
                self.tenant_id,
                "ai_sales",
                "customer_profile.recomputed",
                f"تمت إعادة حساب ملف العميل {customer.id}",
                payload={
                    "customer_id": customer.id,
                    "reason": reason,
                    "customer_status": status,
                    "rfm_segment": rfm_segment,
                    "total_orders": metrics.total_orders,
                    "total_spend_sar": metrics.total_spend_sar,
                },
                reference_id=str(customer.id),
            )

        if commit:
            self.db.commit()
        else:
            self.db.flush()

        # Event-driven coupon autogen: if the classification just changed
        # into a segment we auto-reward (new/active/vip/at_risk), schedule a
        # coupon for this customer. Done AFTER commit so a coupon failure
        # never rolls back the profile update. Wrapped in its own try/except
        # so the coupon pipeline never takes down the classification pipeline.
        if status_changed and commit:
            try:
                import asyncio as _asyncio  # noqa: PLC0415
                from services.coupon_generator import (  # noqa: PLC0415
                    CouponGeneratorService,
                    EVENT_DRIVEN_SEGMENTS,
                )
                if status in EVENT_DRIVEN_SEGMENTS:
                    decision_mode = self._tenant_decision_mode()

                    async def _run():
                        if decision_mode is DecisionMode.ENFORCE:
                            await self._segment_change_via_decision_service(
                                customer_id=customer.id, segment=status, reason=reason,
                            )
                            return
                        if decision_mode is DecisionMode.ADVISORY:
                            await self._segment_change_advisory(
                                customer_id=customer.id, segment=status, reason=reason,
                            )
                            return
                        svc = CouponGeneratorService(self.db, self.tenant_id)
                        await svc.generate_for_customer(
                            customer.id, status, reason=f"status_change:{reason}"
                        )

                    try:
                        loop = _asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop is not None:
                        # Fire-and-forget inside an async context (the common
                        # case — we are called from the dispatcher / FastAPI
                        # handler). The coupon generator does its own error
                        # logging; we do not await so the caller is not blocked.
                        loop.create_task(_run())
                    else:
                        # Synchronous call site (scripts/tests). Run to completion.
                        _asyncio.run(_run())
            except Exception as exc:
                logger.exception(
                    "[Intelligence] event-driven coupon trigger failed tenant=%s customer=%s: %s",
                    self.tenant_id, customer.id, exc,
                )

        return profile

    # ── Decision-service routing (Phase 4) ──────────────────────────────
    #
    # The per-tenant rollout mode for OfferDecisionService is owned by
    # `services.offer_decision_flags`. On a segment status change we
    # dispatch to one of three branches:
    #
    #   ENFORCE  → service issues the coupon (`_segment_change_via_decision_service`).
    #   ADVISORY → service writes a ledger row only; legacy
    #              `CouponGeneratorService.generate_for_customer` still
    #              issues the coupon, which we back-stamp with
    #              `decision_id` (`_segment_change_advisory`).
    #   OFF      → legacy path runs unchanged, no ledger writes.
    #
    # All three branches absorb their own failures so the classification
    # path is never affected by a coupon-side issue.

    def _tenant_decision_mode(self) -> DecisionMode:
        return tenant_decision_mode(self.db, self.tenant_id)

    async def _segment_change_via_decision_service(
        self,
        *,
        customer_id: int,
        segment: str,
        reason: str,
    ) -> None:
        """ENFORCE — decide → apply through the shared service. Failures
        are absorbed (logged) — never escalates to the classification
        path."""
        ctx = self._build_segment_change_context(customer_id=customer_id, segment=segment)
        if ctx is None:
            return
        try:
            from services.offer_decision_service import (  # noqa: PLC0415
                SOURCE_NONE,
                apply_decision,
                decide,
            )

            decision = decide(self.db, ctx)
            self.db.commit()
            if decision.source == SOURCE_NONE:
                logger.info(
                    "[Intelligence] segment-change decision skipped tenant=%s customer=%s "
                    "segment=%s reasons=%s",
                    self.tenant_id, customer_id, segment, decision.reason_codes,
                )
                return
            extras = await apply_decision(self.db, ctx=ctx, decision=decision, customer=None)
            if extras.get("coupon_code"):
                logger.info(
                    "[Intelligence] segment-change coupon issued tenant=%s customer=%s "
                    "segment=%s code=%s reasons=%s reason=%s",
                    self.tenant_id, customer_id, segment, extras["coupon_code"],
                    decision.reason_codes, reason,
                )
        except Exception as exc:
            logger.exception(
                "[Intelligence] decision-service segment_change failed tenant=%s customer=%s: %s",
                self.tenant_id, customer_id, exc,
            )

    async def _segment_change_advisory(
        self,
        *,
        customer_id: int,
        segment: str,
        reason: str,
    ) -> None:
        """ADVISORY — write a ledger row capturing what the policy would
        have done, then fall through to the legacy
        `CouponGeneratorService` and stamp the resulting coupon with
        `decision_id`."""
        from services.coupon_generator import CouponGeneratorService  # noqa: PLC0415

        decision = None
        try:
            from services.offer_decision_service import decide  # noqa: PLC0415

            ctx = self._build_segment_change_context(customer_id=customer_id, segment=segment)
            if ctx is not None:
                decision = decide(self.db, ctx)
                self.db.commit()
                logger.info(
                    "[Intelligence] advisory segment-change tenant=%s customer=%s "
                    "segment=%s would-have %s/%s reasons=%s",
                    self.tenant_id, customer_id, segment,
                    decision.source, decision.discount_value, decision.reason_codes,
                )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "[Intelligence] advisory segment-change decide failed tenant=%s customer=%s: %s",
                self.tenant_id, customer_id, exc,
            )

        try:
            svc = CouponGeneratorService(self.db, self.tenant_id)
            coupon = await svc.generate_for_customer(
                customer_id, segment, reason=f"status_change:{reason}"
            )
        except Exception as exc:
            logger.exception(
                "[Intelligence] legacy generate_for_customer failed tenant=%s customer=%s: %s",
                self.tenant_id, customer_id, exc,
            )
            return

        if decision is not None and coupon is not None:
            try:
                from services.offer_decision_flags import (  # noqa: PLC0415
                    stamp_decision_id_on_coupon,
                )
                stamp_decision_id_on_coupon(
                    self.db, coupon, decision, mode_label="advisory_segment_change",
                )
                self.db.commit()
            except Exception as exc:  # pragma: no cover
                logger.debug(
                    "[Intelligence] advisory coupon stamp failed tenant=%s: %s",
                    self.tenant_id, exc,
                )

    def _build_segment_change_context(
        self,
        *,
        customer_id: int,
        segment: str,
    ) -> Optional["Any"]:
        """Compose the OfferDecisionContext for a segment-change event.
        Shared between the ENFORCE and ADVISORY branches so the ledger
        row is byte-identical (modulo decision_id) across modes."""
        try:
            from services.offer_decision_service import (  # noqa: PLC0415
                OfferDecisionContext,
                SURFACE_SEGMENT_CHANGE,
                collect_signals,
            )
            signals = collect_signals(self.db, tenant_id=self.tenant_id, customer_id=customer_id)
            return OfferDecisionContext(
                tenant_id         = self.tenant_id,
                surface           = SURFACE_SEGMENT_CHANGE,
                customer_id       = customer_id,
                automation_type   = f"segment_change:{segment}",
                suggested_source  = "coupon",
                suggested_segment = segment,
                signals           = signals,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("[Intelligence] segment-change ctx build failed: %s", exc)
            return None

    def rebuild_profiles_for_tenant(
        self,
        reason: str,
        *,
        commit: bool = True,
        emit_event: bool = True,
    ) -> int:
        customers = self._query_customers()
        orders = (
            self.db.query(Order)
            .filter(Order.tenant_id == self.tenant_id)
            .all()
        )
        phone_index, name_index = self._index_orders(orders)
        profiles = (
            self.db.query(CustomerProfile)
            .filter(CustomerProfile.tenant_id == self.tenant_id)
            .all()
        )
        profile_map = {profile.customer_id: profile for profile in profiles}

        now = utcnow()
        updated = 0
        for customer in customers:
            profile = profile_map.get(customer.id) or self.ensure_profile(customer)
            matched_orders = self._orders_for_customer(
                customer,
                phone_index=phone_index,
                name_index=name_index,
            )
            metrics = self._build_metrics(customer, profile=profile, orders=matched_orders, now=now)
            status = compute_customer_status(metrics, now)
            rfm_scores = compute_rfm_scores(metrics, now)
            rfm_segment = compute_rfm_segment(rfm_scores, status)
            self._apply_profile_metrics(
                profile,
                metrics=metrics,
                status=status,
                rfm_scores=rfm_scores,
                rfm_segment=rfm_segment,
                reason=reason,
                computed_at=now,
            )
            self.db.add(profile)
            updated += 1

        if emit_event:
            log_event(
                self.db,
                self.tenant_id,
                "ai_sales",
                "customer_profile.rebuilt",
                f"تمت إعادة بناء ملفات العملاء للمتجر ({updated})",
                payload={"reason": reason, "profiles_updated": updated},
                reference_id=str(self.tenant_id),
            )

        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return updated

    def status_counts(self) -> Dict[str, int]:
        counts = {key: 0 for key in CUSTOMER_STATUS_ORDER}
        rows = (
            self.db.query(
                CustomerProfile.customer_status,
                func.count(CustomerProfile.id),
            )
            .filter(CustomerProfile.tenant_id == self.tenant_id)
            .group_by(CustomerProfile.customer_status)
            .all()
        )
        for status, count in rows:
            key = str(status or "lead")
            counts[key] = int(count)

        total_customers = (
            self.db.query(func.count(Customer.id))
            .filter(Customer.tenant_id == self.tenant_id)
            .scalar()
            or 0
        )
        counted = sum(counts.values())
        if total_customers > counted:
            counts["lead"] += total_customers - counted
        return counts

    def rfm_segment_counts(self) -> Dict[str, int]:
        counts = {key: 0 for key in RFM_SEGMENT_ORDER}
        rows = (
            self.db.query(
                CustomerProfile.rfm_segment,
                func.count(CustomerProfile.id),
            )
            .filter(CustomerProfile.tenant_id == self.tenant_id)
            .group_by(CustomerProfile.rfm_segment)
            .all()
        )
        for segment, count in rows:
            key = str(segment or "lead")
            counts[key] = int(count)

        total_customers = (
            self.db.query(func.count(Customer.id))
            .filter(Customer.tenant_id == self.tenant_id)
            .scalar()
            or 0
        )
        counted = sum(counts.values())
        if total_customers > counted:
            counts["lead"] += total_customers - counted
        return counts

    def customers_metrics_summary(self) -> Dict[str, Any]:
        status_counts = self.status_counts()
        rfm_counts = self.rfm_segment_counts()
        total_customers = (
            self.db.query(func.count(Customer.id))
            .filter(Customer.tenant_id == self.tenant_id)
            .scalar()
            or 0
        )
        return {
            "total_customers": int(total_customers),
            "status_counts": status_counts,
            "rfm_segment_counts": rfm_counts,
            "active_customers": status_counts["active"],
            "vip_customers": status_counts["vip"],
            "new_customers": status_counts["new"],
            "at_risk_customers": status_counts["at_risk"],
            "inactive_customers": status_counts["inactive"],
            "leads": status_counts["lead"],
        }
