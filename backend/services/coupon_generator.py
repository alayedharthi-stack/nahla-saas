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
POOL_SIZE_PER_SEGMENT = 3
DEFAULT_POOL_REFILL_THRESHOLD = 1
MAX_POOL_TARGET_PER_SEGMENT = 15

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


# Per-customer coupon defaults keyed by CRM customer_status atom. This
# is intentionally atom-level — one customer's status drives one coupon
# decision. Cohort-level decisions (audience targeting) live in
# `services.nahla_segments` instead.
#
# String values are pulled from `services.crm_atoms.CrmStatus` so a typo
# here would be a NameError at import time, not a silent missing key
# at run time.
from services.crm_atoms import (  # noqa: E402 — kept after Tenant imports above
    STATUS_ALIASES as SEGMENT_ALIASES,
    CrmStatus,
    canonical_status as _canonical_segment,
)

SEGMENT_DEFAULTS: Dict[str, Dict[str, Any]] = {
    CrmStatus.NEW:      {"discount_pct": 15, "expiry_days": 1, "label": "عميل جديد"},
    CrmStatus.ACTIVE:   {"discount_pct": 5,  "expiry_days": 3, "label": "عميل نشط"},
    CrmStatus.VIP:      {"discount_pct": 20, "expiry_days": 3, "label": "عميل مميز"},
    CrmStatus.AT_RISK:  {"discount_pct": 25, "expiry_days": 1, "label": "في خطر المغادرة"},
    CrmStatus.INACTIVE: {"discount_pct": 30, "expiry_days": 1, "label": "عميل غير نشط"},
}

# Segments for which we auto-generate a coupon on a customer status change.
EVENT_DRIVEN_SEGMENTS = frozenset({
    CrmStatus.NEW,
    CrmStatus.ACTIVE,
    CrmStatus.VIP,
    CrmStatus.AT_RISK,
})


# CRM segment → coupon_level mapping. Used by the pool generator and the
# on-demand path to write the taxonomy column on every new coupon so the
# dashboard can group / filter without re-deriving from extra_metadata.
SEGMENT_TO_LEVEL: Dict[str, str] = {
    CrmStatus.NEW:      "bronze",
    CrmStatus.ACTIVE:   "silver",
    CrmStatus.VIP:      "gold",
    CrmStatus.AT_RISK:  "vip",
    CrmStatus.INACTIVE: "vip",
}


def _segment_to_level(segment: str) -> str:
    """Return the coupon_level (bronze/silver/gold/vip) for a CRM segment.
    Unknown segments fall back to ``silver`` so we never write NULL."""
    return SEGMENT_TO_LEVEL.get(_canonical_segment(segment), "silver")

# `_canonical_segment` is re-exported above (back-compat shim used by
# offer_decision_service.py and several tests). It now delegates to
# `services.crm_atoms.canonical_status`, which knows about the same
# aliases (`churned` → `inactive`) plus full validation. The old
# behavior of returning `"active"` for an empty/None input is preserved
# because `canonical_status` uses `default="active"` by default.


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


def _get_coupon_dashboard_block(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Return the merchant's ``coupons_dashboard`` settings block (raw,
    unnormalised). Empty dict when nothing has been saved yet."""
    ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    if not ts:
        return {}
    meta = ts.extra_metadata or {}
    return dict(meta.get("coupons_dashboard") or {})


def _get_levels_config(db: Session, tenant_id: int) -> Dict[str, Dict[str, Any]]:
    """Return ``{level_id: level_dict}`` from the merchant's saved levels
    so the pool generator can apply per-tier overrides without re-importing
    the router module."""
    block = _get_coupon_dashboard_block(db, tenant_id)
    raw = block.get("levels") or []
    out: Dict[str, Dict[str, Any]] = {}
    for entry in raw:
        if isinstance(entry, dict):
            lid = str(entry.get("id") or "").lower()
            if lid in {"bronze", "silver", "gold", "vip"}:
                out[lid] = entry
    return out



def _get_warm_pool_config(db: Session, tenant_id: int) -> Dict[str, int]:
    block = _get_coupon_dashboard_block(db, tenant_id)
    warm = dict(block.get("warm_pool") or {})
    target = int(warm.get("target_per_segment") or POOL_SIZE_PER_SEGMENT)
    target = max(0, min(target, MAX_POOL_TARGET_PER_SEGMENT))
    refill = int(warm.get("refill_threshold") or DEFAULT_POOL_REFILL_THRESHOLD)
    refill = max(0, min(refill, target))
    return {"target_per_segment": target, "refill_threshold": refill}

def _get_ai_policy(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Return the merchant's AI coupon policy with safe defaults."""
    block = _get_coupon_dashboard_block(db, tenant_id)
    raw = block.get("ai_policy") or {}
    return {
        "enabled":             bool(raw.get("enabled", True)),
        "allowed_levels":      [str(x).lower() for x in (raw.get("allowed_levels") or ["bronze", "silver"])],
        "min_remaining_hours": int(raw.get("min_remaining_hours") or 3),
        "pool_mode":           str(raw.get("pool_mode") or "pool_first").lower(),
    }


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
        coupon_level: Optional[str] = None,
        allocation_channel: Optional[str] = None,
        source_type: str = "system",
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
            resolved_level = (coupon_level or _segment_to_level(canonical_segment)).lower()
            resolved_channel = (allocation_channel or "shared").lower()
            metadata: Dict[str, Any] = {
                "source": "auto",
                "target_segment": canonical_segment,
                "discount_pct": discount,
                "used": "false",
                "salla_synced": True,
                "category": "auto",
                "active": True,
                "coupon_level": resolved_level,
                "allocation_channel": resolved_channel,
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
                source_type=source_type,
                coupon_level=resolved_level,
                allocation_channel=resolved_channel,
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
        """Top up the coupon pool for all segments. Returns counts created per segment.

        Per-level overrides (discount_default, validity_hours) come from
        the merchant's coupon dashboard settings (``coupons_dashboard.levels``).
        Falls back to ``SEGMENT_DEFAULTS`` when no level config exists.
        """
        ai_policy = _get_ai_policy(self.db, self.tenant_id)
        if ai_policy.get("pool_mode") == "on_demand_only":
            return {segment: 0 for segment in SEGMENT_DEFAULTS}

        limits = _get_merchant_limits(self.db, self.tenant_id)
        adapter = self._get_adapter()
        levels_cfg = _get_levels_config(self.db, self.tenant_id)
        warm_cfg = _get_warm_pool_config(self.db, self.tenant_id)
        target_per_segment = warm_cfg["target_per_segment"]
        refill_threshold = warm_cfg["refill_threshold"]
        created: Dict[str, int] = {}
        reserved_codes = self._reserved_codes()

        for segment, defaults in SEGMENT_DEFAULTS.items():
            current = self._count_pool(segment)
            if current > refill_threshold:
                created[segment] = 0
                continue
            needed = target_per_segment - current
            if needed <= 0:
                created[segment] = 0
                continue

            level_id = _segment_to_level(segment)
            level_cfg = levels_cfg.get(level_id) or {}
            if level_cfg and level_cfg.get("enabled") is False:
                created[segment] = 0
                continue

            base_discount = int(level_cfg.get("discount_default") or defaults["discount_pct"])
            discount = _clamp(
                base_discount,
                int(level_cfg.get("discount_min") or limits["min_discount"]),
                int(level_cfg.get("discount_max") or limits["max_discount"]),
            )
            validity_hours = int(level_cfg.get("validity_hours") or (defaults["expiry_days"] * 24))
            expiry_days = max(1, (validity_hours + 23) // 24)

            channel = "shared"
            allowed = level_cfg.get("allowed_channels") or []
            if allowed:
                # Pool coupons are pre-generated for any allowed channel; if
                # the merchant restricted a level to AI-only, tag the pool
                # entries accordingly so the campaign dispatcher won't grab
                # them.
                channel = allowed[0] if len(allowed) == 1 else "shared"

            count = 0
            for _ in range(needed):
                coupon = await self._create_one_coupon(
                    segment=segment,
                    discount=discount,
                    expiry_days=expiry_days,
                    reserved_codes=reserved_codes,
                    adapter=adapter,
                    coupon_level=level_id,
                    allocation_channel=channel,
                    source_type="system",
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

    def pick_coupon_for_segment(
        self,
        segment: str,
        *,
        for_channel: str = "ai",
    ) -> Optional[Coupon]:
        """Pick an available auto-coupon for the given segment.

        When ``for_channel == "ai"`` we honour the merchant's AI policy:
          • Refuse if the AI is disabled.
          • Skip levels the merchant excluded from AI use.
          • Skip coupons whose remaining validity is below the merchant's
            minimum (default 3h).
        """
        now = datetime.now(timezone.utc)
        canonical_segment = _canonical_segment(segment)

        if for_channel == "ai":
            policy = _get_ai_policy(self.db, self.tenant_id)
            if not policy.get("enabled", True):
                return None
            level_id = _segment_to_level(canonical_segment)
            allowed_levels = policy.get("allowed_levels") or []
            if allowed_levels and level_id not in allowed_levels:
                return None
            if policy.get("pool_mode") == "on_demand_only":
                return None
            min_remaining_hours = int(policy.get("min_remaining_hours") or 0)
        else:
            min_remaining_hours = 0

        candidates = (
            self.db.query(Coupon)
            .filter(*self._pool_filter(canonical_segment))
            .order_by(Coupon.id.asc())
            .limit(50)
            .all()
        )

        coupon: Optional[Coupon] = None
        for cand in candidates:
            exp = cand.expires_at
            if exp is not None:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if min_remaining_hours and (exp - now) < timedelta(hours=min_remaining_hours):
                    continue
            coupon = cand
            break

        if coupon:
            self._mark_coupon_sent(coupon, sent_at=now, commit=True)
        return coupon

    async def create_on_demand(
        self,
        segment: str,
        requested_discount_pct: Optional[int] = None,
        *,
        validity_days_override: Optional[int] = None,
        allocation_channel: str = "ai",
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

        # AI policy gate — when the AI is disabled, or this segment maps
        # to a level the merchant blocked from AI use, refuse silently so
        # the conversation engine can fall back to a static incentive.
        if allocation_channel == "ai":
            policy = _get_ai_policy(self.db, self.tenant_id)
            if not policy.get("enabled", True):
                return None
            level_id = _segment_to_level(canonical_segment)
            allowed_levels = policy.get("allowed_levels") or []
            if allowed_levels and level_id not in allowed_levels:
                return None

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
            coupon_level=_segment_to_level(canonical_segment),
            allocation_channel=allocation_channel,
            source_type="system",
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

        # Event-driven path: triggered by CRM status changes from the
        # autopilot, not from a live AI conversation. Bypass the AI policy
        # gate so a customer crossing into VIP still gets their coupon
        # even if the merchant blocked the AI from issuing VIP codes
        # mid-chat.
        coupon = self.pick_coupon_for_segment(canonical_segment, for_channel="autopilot")
        if coupon is not None:
            return coupon

        return await self.create_on_demand(canonical_segment, allocation_channel="autopilot")

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
