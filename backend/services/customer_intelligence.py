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

logger = logging.getLogger("nahla.customer_intelligence")

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
    text = str(raw or "").strip()
    digits = _PHONE_DIGITS_RE.sub("", text)
    if not digits:
        return ""

    if digits.startswith("00"):
        digits = digits[2:]

    if digits.startswith("966"):
        digits = digits[3:]

    if digits.startswith("0") and len(digits) == 10:
        digits = digits[1:]

    if len(digits) == 9 and digits.startswith("5"):
        return f"+966{digits}"

    if text.startswith("+") and len(digits) >= 8:
        return f"+{digits}"

    if len(digits) >= 8:
        return f"+{digits}"

    return ""


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
    if isinstance(order_or_status, Order):
        return str(getattr(order_or_status, "status", "") or "").strip().lower()
    return str(order_or_status or "").strip().lower()


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


def compute_customer_status(metrics: CustomerMetrics, now: Optional[datetime] = None) -> str:
    _ = now
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
        if not external_id:
            return None
        target = str(external_id).strip()
        if not target:
            return None
        for customer in self._query_customers():
            meta = customer.extra_metadata or {}
            if str(meta.get("salla_id") or meta.get("external_id") or "").strip() == target:
                return customer
        return None

    def find_customer_by_phone(
        self,
        raw_phone: Any,
        *,
        exclude_customer_id: Optional[int] = None,
    ) -> Optional[Customer]:
        normalized = normalize_phone(raw_phone)
        if not normalized:
            return None

        exact = (
            self.db.query(Customer)
            .filter(
                Customer.tenant_id == self.tenant_id,
                Customer.phone == normalized,
            )
            .first()
        )
        if exact and exact.id != exclude_customer_id:
            return exact

        for customer in self._query_customers():
            if exclude_customer_id is not None and customer.id == exclude_customer_id:
                continue
            if normalize_phone(customer.phone) == normalized:
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
            profile = CustomerProfile(
                customer_id=customer.id,
                tenant_id=self.tenant_id,
                segment="lead",
                customer_status="lead",
                rfm_segment="lead",
                first_seen_at=now,
                last_seen_at=now,
                metrics_computed_at=now,
                last_recomputed_reason="profile_initialized",
                updated_at=now,
            )
            self.db.add(profile)
            self.db.flush()

        if seen_at:
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
        normalized_phone = normalize_phone(phone)
        clean_name = str(name or "").strip()
        clean_email = str(email or "").strip() or None

        if not any([normalized_phone, clean_name, clean_email, external_id]):
            return None

        customer = self._find_customer_by_external_id(external_id)
        if customer is None and normalized_phone:
            customer = self.find_customer_by_phone(normalized_phone)

        if customer is None and clean_name:
            normalized_name = normalize_name(clean_name)
            for row in self._query_customers():
                if normalize_name(row.name) == normalized_name:
                    customer = row
                    break

        metadata = dict(customer.extra_metadata or {}) if customer else {}
        metadata.update(extra_metadata or {})
        if external_id:
            metadata["salla_id"] = str(external_id)
        if source:
            metadata["source"] = source

        if customer is None:
            customer = Customer(
                tenant_id=self.tenant_id,
                phone=normalized_phone or None,
                name=clean_name or None,
                email=clean_email,
                extra_metadata=metadata or None,
            )
            self.db.add(customer)
            self.db.flush()
        else:
            if normalized_phone:
                customer.phone = normalized_phone
            if clean_name:
                customer.name = clean_name
            if clean_email:
                customer.email = clean_email
            customer.extra_metadata = metadata or None

        self.ensure_profile(customer, seen_at=seen_at)
        return customer

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
        if normalized_phone and normalized_phone in phone_index:
            return list(phone_index.get(normalized_phone, []))

        normalized_name = normalize_name(customer.name)
        if normalized_name and normalized_name in name_index:
            return list(name_index.get(normalized_name, []))

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
                emit_automation_event(
                    self.db,
                    self.tenant_id,
                    "customer_status_changed",
                    customer_id=customer.id,
                    payload={
                        "from": old_status,
                        "to": status,
                        "rfm_segment": rfm_segment,
                        "reason": reason,
                    },
                )
            except Exception as exc:
                # Errors in automation emission used to be silently debug-logged,
                # hiding real outages. Now surfaced as ERROR with full stack.
                logger.exception(
                    "[Intelligence] emit customer_status_changed failed tenant=%s customer=%s: %s",
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
                    svc = CouponGeneratorService(self.db, self.tenant_id)

                    async def _run():
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
