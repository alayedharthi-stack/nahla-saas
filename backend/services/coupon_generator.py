"""
services/coupon_generator.py
─────────────────────────────
Automatic coupon pool management.

Maintains a pool of pre-generated coupons per customer segment so the AI
agent can immediately hand out a real coupon during a conversation.

Pool size: 15 coupons per segment (5 segments = 75 coupons max per tenant).

Code format (source of truth)
─────────────────────────────
    prefix   : "NH"
    body     : 3 characters drawn uniformly from A-Z 0-9
    length   : 5
    regex    : ^NH[A-Z0-9]{3}$
    examples : NH4K7, NH3A9, NH7K2

This gives 36^3 = 46,656 codes per tenant — enough headroom that collision
retries are effectively free.

Legacy `NHL\\d{3}` codes from before this fix are grandfathered:
  • They are recognised by ``_is_short_coupon_code`` so existing reporting
    and pool counts keep working.
  • They are loaded into ``_reserved_codes`` so the new generator never
    reuses an old number and produces duplicates.
  • New issuance always uses the new 5-char format.

Coupons are created FIRST in Salla, THEN stored locally. If the local DB
insert fails after Salla succeeded, the Salla coupon is deleted as
compensation (``delete_coupon_by_code``) so the two sides stay in sync.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

_THIS = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.abspath(os.path.join(_THIS, "../../database"))
for _p in (_THIS, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Coupon, Tenant, TenantSettings  # noqa: E402
from core.obs import EVENTS, log_event  # noqa: E402

logger = logging.getLogger("nahla-backend")

# ── Code format ───────────────────────────────────────────────────────────────
POOL_SIZE_PER_SEGMENT = 15

SHORT_CODE_PREFIX = "NH"
SHORT_CODE_BODY_LEN = 3
SHORT_CODE_LENGTH = len(SHORT_CODE_PREFIX) + SHORT_CODE_BODY_LEN  # 5
SHORT_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
SHORT_CODE_PATTERN = re.compile(rf"^{SHORT_CODE_PREFIX}[A-Z0-9]{{{SHORT_CODE_BODY_LEN}}}$")

# Grandfathered legacy format: NHL + 3 digits (e.g. NHL042).
LEGACY_PREFIX = "NHL"
LEGACY_LENGTH = 6
LEGACY_PATTERN = re.compile(rf"^{LEGACY_PREFIX}\d{{3}}$")

TOTAL_CODE_SPACE = len(SHORT_CODE_ALPHABET) ** SHORT_CODE_BODY_LEN  # 46,656

KSA_TZ = timezone(timedelta(hours=3))


class CouponPoolExhausted(RuntimeError):
    """Raised when we cannot find an unused code after many attempts."""


SEGMENT_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "new":     {"discount_pct": 15, "expiry_days": 1, "label": "عميل جديد"},
    "active":  {"discount_pct": 5,  "expiry_days": 3, "label": "عميل نشط"},
    "vip":     {"discount_pct": 20, "expiry_days": 3, "label": "عميل مميز"},
    "at_risk": {"discount_pct": 25, "expiry_days": 1, "label": "في خطر المغادرة"},
    "inactive": {"discount_pct": 30, "expiry_days": 1, "label": "عميل غير نشط"},
}

# Segments for which we auto-generate a coupon on a customer status change.
EVENT_DRIVEN_SEGMENTS = frozenset({"new", "active", "vip", "at_risk"})

SEGMENT_ALIASES: Dict[str, str] = {
    "churned": "inactive",
}


def _canonical_segment(segment: str) -> str:
    raw = str(segment or "").strip().lower()
    return SEGMENT_ALIASES.get(raw, raw or "active")


def _is_short_coupon_code(code: Optional[str]) -> bool:
    """Accept both the new (NH***) and legacy (NHL###) short-code formats."""
    value = str(code or "").strip().upper()
    if not value:
        return False
    return bool(SHORT_CODE_PATTERN.match(value) or LEGACY_PATTERN.match(value))


def _random_short_code() -> str:
    body = "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(SHORT_CODE_BODY_LEN))
    return SHORT_CODE_PREFIX + body


def _next_short_code(reserved_codes: set[str], *, max_attempts: int = 200) -> str:
    """
    Return a fresh NH*** code not present in ``reserved_codes``.

    ``reserved_codes`` is mutated in-place — the caller's set stays up-to-date
    across multiple calls in the same batch.

    Raises :class:`CouponPoolExhausted` after ``max_attempts`` failures. With
    46,656 total codes and typical pool sizes << 1000 this should never fire
    in practice — it's a guard against runaway loops in pathological cases.
    """
    for _ in range(max_attempts):
        code = _random_short_code()
        if code not in reserved_codes:
            reserved_codes.add(code)
            return code
    raise CouponPoolExhausted(
        f"No free NH*** code found after {max_attempts} attempts "
        f"(reserved={len(reserved_codes)} of {TOTAL_CODE_SPACE})"
    )


def _parse_provider_expiry(raw_value: Any) -> Optional[datetime]:
    if raw_value in (None, ""):
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_coupon_expiry(provider_result: Optional[Dict[str, Any]], fallback_days: int) -> datetime:
    raw_expiry = None
    if isinstance(provider_result, dict):
        raw_expiry = (
            provider_result.get("expire_date")
            or provider_result.get("expiry_date")
            or provider_result.get("expires_at")
        )
    parsed = _parse_provider_expiry(raw_expiry)
    if parsed:
        return parsed
    return datetime.now(timezone.utc) + timedelta(days=fallback_days)


def build_coupon_send_payload(coupon: Coupon) -> Dict[str, Optional[str]]:
    expires_at = getattr(coupon, "expires_at", None)
    if expires_at and getattr(expires_at, "tzinfo", None) is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expires_iso = expires_at.astimezone(timezone.utc).isoformat() if expires_at else None
    expires_text = None
    if expires_at:
        local_expiry = expires_at.astimezone(KSA_TZ)
        expires_text = f"{local_expiry:%Y-%m-%d} الساعة {local_expiry:%H:%M} بتوقيت السعودية"
    return {
        "code": str(getattr(coupon, "code", "") or ""),
        "expires_at": expires_iso,
        "expires_text": expires_text,
    }


_DEFAULT_MAX_DISCOUNT = 10


def _get_merchant_limits(db: Session, tenant_id: int) -> Dict[str, int]:
    """Read max discount from TenantSettings.ai_settings (dashboard source of truth),
    falling back to Tenant.coupon_policy, then to _DEFAULT_MAX_DISCOUNT."""
    ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    if ts:
        ai = ts.ai_settings or {}
        try:
            max_disc = int(ai.get("allowed_discount_levels", 0))
            if max_disc > 0:
                return {"min_discount": 0, "max_discount": max_disc}
        except (ValueError, TypeError):
            pass

    tenant = db.query(Tenant).filter_by(id=tenant_id).first()
    if tenant:
        policy = tenant.coupon_policy or {}
        max_val = policy.get("max_discount")
        if max_val is not None:
            return {
                "min_discount": int(policy.get("min_discount", 0)),
                "max_discount": int(max_val),
            }

    return {"min_discount": 0, "max_discount": _DEFAULT_MAX_DISCOUNT}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


class CouponGeneratorService:
    def __init__(self, db: Session, tenant_id: int):
        self.db = db
        self.tenant_id = tenant_id

    def _reserved_codes(self) -> set[str]:
        """All existing coupon codes for this tenant (both new and legacy)."""
        rows = (
            self.db.query(Coupon.code)
            .filter(Coupon.tenant_id == self.tenant_id)
            .all()
        )
        return {str(code or "").strip().upper() for (code,) in rows if code}

    def _mark_coupon_sent(
        self,
        coupon: Coupon,
        *,
        sent_at: Optional[datetime] = None,
        commit: bool = True,
    ) -> None:
        sent_at = sent_at or datetime.now(timezone.utc)
        meta = dict(coupon.extra_metadata or {})
        meta["used"] = "true"
        meta["used_at"] = sent_at.isoformat()
        meta["sent_at"] = sent_at.isoformat()
        send_payload = build_coupon_send_payload(coupon)
        if send_payload.get("expires_at"):
            meta["sent_expiry_at"] = send_payload["expires_at"]
        if send_payload.get("expires_text"):
            meta["sent_expiry_text"] = send_payload["expires_text"]
        coupon.extra_metadata = meta
        flag_modified(coupon, "extra_metadata")
        if commit:
            self.db.commit()

    def _pool_filter(self, segment: str):
        """SQLAlchemy filter matching both NH*** and NHL### codes for a segment."""
        segment = _canonical_segment(segment)
        now = datetime.now(timezone.utc)
        return (
            Coupon.tenant_id == self.tenant_id,
            or_(
                (Coupon.code.like(f"{SHORT_CODE_PREFIX}___"))
                & (func.length(Coupon.code) == SHORT_CODE_LENGTH),
                (Coupon.code.like(f"{LEGACY_PREFIX}___"))
                & (func.length(Coupon.code) == LEGACY_LENGTH),
            ),
            Coupon.extra_metadata["source"].astext == "auto",
            Coupon.extra_metadata["target_segment"].astext == segment,
            Coupon.extra_metadata["used"].astext != "true",
            Coupon.extra_metadata["salla_synced"].astext == "true",
            (Coupon.expires_at == None) | (Coupon.expires_at > now),  # noqa: E711
        )

    def _count_pool(self, segment: str) -> int:
        """Count unused auto-coupons for a segment that haven't expired."""
        return self.db.query(Coupon).filter(*self._pool_filter(segment)).count()

    async def _create_one_coupon(
        self,
        *,
        segment: str,
        discount: int,
        expiry_days: int,
        reserved_codes: set[str],
        adapter,
        extra_flags: Optional[Dict[str, Any]] = None,
        label: Optional[str] = None,
    ) -> Optional[Coupon]:
        """
        Single-coupon creation with:
          1. Generate a unique NH*** code (retry on collision).
          2. Create in Salla (via adapter). Fail → return None.
          3. Insert locally. On IntegrityError → regenerate code and retry
             the whole Salla+DB transaction. On other DB error → compensate
             by deleting the Salla coupon we just created.
        """
        canonical_segment = _canonical_segment(segment)
        label = label or SEGMENT_DEFAULTS.get(canonical_segment, {}).get("label", canonical_segment)

        # The outer retry loop handles local-DB collisions (extremely rare but
        # possible under concurrent pool top-ups for the same tenant).
        MAX_OUTER_ATTEMPTS = 5
        for outer_attempt in range(MAX_OUTER_ATTEMPTS):
            try:
                code = _next_short_code(reserved_codes)
            except CouponPoolExhausted as exc:
                log_event(
                    EVENTS.COUPON_POOL_EXHAUSTED,
                    tenant_id=self.tenant_id,
                    segment=canonical_segment,
                    err=exc,
                )
                return None

            log_event(
                EVENTS.COUPON_AUTOGEN_TRIGGERED,
                tenant_id=self.tenant_id,
                segment=canonical_segment,
                code=code,
                discount=discount,
                attempt=outer_attempt + 1,
            )

            if adapter is None:
                # No adapter → we cannot create in Salla → do not insert locally.
                log_event(
                    EVENTS.COUPON_AUTOGEN_FAILED,
                    tenant_id=self.tenant_id,
                    segment=canonical_segment,
                    code=code,
                    err="no_salla_adapter",
                )
                return None

            try:
                salla_result = await adapter.create_coupon(
                    code=code,
                    discount_type="percentage",
                    discount_value=discount,
                    expiry_days=expiry_days,
                )
            except Exception as exc:
                log_event(
                    EVENTS.COUPON_AUTOGEN_FAILED,
                    tenant_id=self.tenant_id,
                    segment=canonical_segment,
                    code=code,
                    stage="salla_create",
                    err=exc,
                )
                return None

            if salla_result is None:
                log_event(
                    EVENTS.COUPON_AUTOGEN_FAILED,
                    tenant_id=self.tenant_id,
                    segment=canonical_segment,
                    code=code,
                    stage="salla_create",
                    err="salla_returned_none",
                )
                return None

            # Salla has the coupon now — insert into our DB.
            expires_at = _resolve_coupon_expiry(
                salla_result if isinstance(salla_result, dict) else None,
                expiry_days,
            )
            metadata: Dict[str, Any] = {
                "source": "auto",
                "target_segment": canonical_segment,
                "discount_pct": discount,
                "used": "false",
                "salla_synced": True,
                "category": "auto",
                "active": True,
            }
            if extra_flags:
                metadata.update(extra_flags)

            coupon = Coupon(
                tenant_id=self.tenant_id,
                code=code,
                description=f"كوبون تلقائي - {label}",
                discount_type="percentage",
                discount_value=str(discount),
                expires_at=expires_at,
                extra_metadata=metadata,
            )
            self.db.add(coupon)
            try:
                self.db.commit()
            except IntegrityError as exc:
                # Collision at the DB layer. This can happen if another worker
                # reserved the same code first. Roll back, remove the Salla
                # coupon we orphaned, and try a new code.
                self.db.rollback()
                log_event(
                    EVENTS.COUPON_AUTOGEN_COLLISION,
                    tenant_id=self.tenant_id,
                    segment=canonical_segment,
                    code=code,
                    attempt=outer_attempt + 1,
                    err=exc,
                )
                try:
                    await adapter.delete_coupon_by_code(code)
                    log_event(
                        EVENTS.COUPON_AUTOGEN_ROLLED_BACK,
                        tenant_id=self.tenant_id,
                        segment=canonical_segment,
                        code=code,
                        reason="db_integrity_error",
                    )
                except Exception as comp_exc:
                    logger.exception(
                        "[CouponGenerator] Salla compensation delete failed tenant=%s code=%s: %s",
                        self.tenant_id, code, comp_exc,
                    )
                # Refresh reserved_codes from DB so we don't hand out the
                # colliding code again in subsequent attempts.
                reserved_codes.update(self._reserved_codes())
                continue
            except Exception as exc:
                # Non-integrity DB failure — same compensation, but do NOT
                # retry, since the error may be systemic (e.g. DB down).
                self.db.rollback()
                log_event(
                    EVENTS.COUPON_AUTOGEN_FAILED,
                    tenant_id=self.tenant_id,
                    segment=canonical_segment,
                    code=code,
                    stage="db_insert",
                    err=exc,
                )
                try:
                    await adapter.delete_coupon_by_code(code)
                    log_event(
                        EVENTS.COUPON_AUTOGEN_ROLLED_BACK,
                        tenant_id=self.tenant_id,
                        segment=canonical_segment,
                        code=code,
                        reason="db_error",
                    )
                except Exception as comp_exc:
                    logger.exception(
                        "[CouponGenerator] Salla compensation delete failed tenant=%s code=%s: %s",
                        self.tenant_id, code, comp_exc,
                    )
                return None

            log_event(
                EVENTS.COUPON_AUTOGEN_CREATED,
                tenant_id=self.tenant_id,
                segment=canonical_segment,
                code=code,
                coupon_id=coupon.id,
                discount=discount,
            )
            return coupon

        # Exhausted outer retries — persistent collisions.
        log_event(
            EVENTS.COUPON_AUTOGEN_FAILED,
            tenant_id=self.tenant_id,
            segment=canonical_segment,
            err="persistent_collision",
        )
        return None

    async def ensure_coupon_pool(self) -> Dict[str, int]:
        """Top up the coupon pool for all segments. Returns counts created per segment."""
        limits = _get_merchant_limits(self.db, self.tenant_id)
        adapter = self._get_adapter()
        created: Dict[str, int] = {}
        reserved_codes = self._reserved_codes()

        for segment, defaults in SEGMENT_DEFAULTS.items():
            current = self._count_pool(segment)
            needed = POOL_SIZE_PER_SEGMENT - current
            if needed <= 0:
                created[segment] = 0
                continue

            discount = _clamp(
                defaults["discount_pct"],
                limits["min_discount"],
                limits["max_discount"],
            )
            expiry_days = defaults["expiry_days"]
            count = 0
            for _ in range(needed):
                coupon = await self._create_one_coupon(
                    segment=segment,
                    discount=discount,
                    expiry_days=expiry_days,
                    reserved_codes=reserved_codes,
                    adapter=adapter,
                )
                if coupon is not None:
                    count += 1
            created[segment] = count

        total = sum(created.values())
        if total:
            logger.info(
                "tenant=%s coupon pool topped up: %s",
                self.tenant_id, created,
            )
        return created

    def pick_coupon_for_segment(self, segment: str) -> Optional[Coupon]:
        """Pick an available auto-coupon for the given segment."""
        now = datetime.now(timezone.utc)
        coupon = (
            self.db.query(Coupon)
            .filter(*self._pool_filter(segment))
            .first()
        )
        if coupon:
            self._mark_coupon_sent(coupon, sent_at=now, commit=True)
        return coupon

    async def create_on_demand(
        self,
        segment: str,
        requested_discount_pct: Optional[int] = None,
        *,
        validity_days_override: Optional[int] = None,
    ) -> Optional[Coupon]:
        """Create a single coupon on-demand when the pool is empty.

        ``validity_days_override`` lets callers (typically the automation
        engine forwarding a merchant-edited rule from /coupons) override the
        default expiry without changing the segment's catalogue defaults.
        """
        canonical_segment = _canonical_segment(segment)
        limits = _get_merchant_limits(self.db, self.tenant_id)
        defaults = SEGMENT_DEFAULTS.get(canonical_segment, SEGMENT_DEFAULTS["active"])
        base_discount = defaults["discount_pct"]
        if isinstance(requested_discount_pct, int):
            base_discount = requested_discount_pct
        discount = _clamp(base_discount, limits["min_discount"], limits["max_discount"])
        expiry_days = defaults["expiry_days"]
        if isinstance(validity_days_override, int) and validity_days_override > 0:
            expiry_days = validity_days_override

        reserved_codes = self._reserved_codes()
        adapter = self._get_adapter()

        coupon = await self._create_one_coupon(
            segment=canonical_segment,
            discount=discount,
            expiry_days=expiry_days,
            reserved_codes=reserved_codes,
            adapter=adapter,
            extra_flags={
                "on_demand": True,
                "used": "true",
                "used_at": datetime.now(timezone.utc).isoformat(),
            },
            label=defaults.get("label"),
        )
        if coupon is None:
            return None

        # Mark as sent so `pick_coupon_for_segment` won't pick it again.
        self._mark_coupon_sent(coupon, sent_at=datetime.now(timezone.utc), commit=True)
        return coupon

    async def generate_for_customer(
        self,
        customer_id: int,
        segment: str,
        *,
        reason: str = "segment_change",
    ) -> Optional[Coupon]:
        """
        Event-driven coupon generation.

        Called from CustomerIntelligenceService.recompute_profile_for_customer
        whenever a customer's status transitions into a segment that warrants
        an automatic coupon (see ``EVENT_DRIVEN_SEGMENTS``).

        Behaviour:
          • First try ``pick_coupon_for_segment`` (cheap — reads from pool).
          • If the pool is empty, call ``create_on_demand`` to synthesize one.
          • Never raises; logs structured events on success/failure.
        """
        canonical_segment = _canonical_segment(segment)
        if canonical_segment not in EVENT_DRIVEN_SEGMENTS:
            return None

        log_event(
            EVENTS.COUPON_AUTOGEN_TRIGGERED,
            tenant_id=self.tenant_id,
            customer_id=customer_id,
            segment=canonical_segment,
            reason=reason,
            mode="event_driven",
        )

        coupon = self.pick_coupon_for_segment(canonical_segment)
        if coupon is not None:
            return coupon

        return await self.create_on_demand(canonical_segment)

    def _get_adapter(self):
        try:
            sys.path.insert(0, os.path.abspath(os.path.join(_THIS, "..")))
            from store_integration.registry import get_adapter
            return get_adapter(self.tenant_id)
        except Exception as exc:
            logger.warning(
                "[CouponGenerator] could not build store adapter tenant=%s: %s",
                self.tenant_id, exc,
            )
            return None
