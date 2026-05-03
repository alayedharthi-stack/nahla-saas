"""
SallaAdapter
────────────
Implements BaseStoreAdapter for the Salla e-commerce platform.
API base: https://api.salla.dev/admin/v2
Auth: Bearer token (OAuth2 access token from Salla App)
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from store_integration.models import (
    NormalizedOffer,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedVariant,
    OrderInput,
    OrderItem,
    ShippingOption,
)
from store_integration.registry import register_adapter
from store_adapters.base_adapter import BaseStoreAdapter

logger = logging.getLogger("nahla.adapter.salla")

SALLA_API_BASE = "https://api.salla.dev/admin/v2"
REQUEST_TIMEOUT = 20.0


class SallaTokenRevokedException(Exception):
    """Raised when Salla returns invalid_grant — token permanently revoked."""


class SallaOrderValidationError(ValueError):
    """Raised when an order payload is missing fields Salla requires.

    The decision/composer layer catches this and re-asks the customer for
    the named ``missing`` fields instead of letting the request blow up
    inside Salla with a 422.

    Attributes:
        missing: list of canonical field names (e.g. ``["customer_first_name",
            "city", "payment_method"]``) the order flow must collect before
            retrying ``create_order``.
        payload_keys: top-level keys present in the rejected payload — useful
            in logs to confirm the payload shape we built.
    """

    def __init__(self, missing: List[str], payload_keys: Optional[List[str]] = None):
        self.missing = list(missing or [])
        self.payload_keys = list(payload_keys or [])
        super().__init__(
            "salla_order_payload_invalid: missing=" + ",".join(self.missing)
        )


# ── Required fields by section in the Salla create-order payload ──────────────
# Sourced from real-world Salla 422 responses + the Admin API v2 docs.
# Keep this list short — it represents what Salla *truly* refuses to accept,
# not every nice-to-have field. Anything optional is enforced upstream by
# the order-flow conversation, not here.
_SALLA_REQUIRED_PAYLOAD_RULES: Dict[str, List[str]] = {
    "products":          [],            # at least one item with identifier
    "customer.first_name": [],
    "customer.mobile":     [],
    "payment.accepted_methods": [],     # at least one slug
    "address.city":        [],          # required when shipping is needed
    # `address.street` is required by Salla. We accept either an explicit
    # street, the synthesised "city - الرمز الوطني XXXX" string, or a
    # Maps URL fallback — the validator only ensures street is non-empty
    # in the final payload.
    "address.street":      [],
}


# ── Saudi city → region mapping (best-effort) ────────────────────────────────
# Used for the optional `address.region` field. Salla also accepts the city
# value here, so any miss falls back to the input city verbatim. The list
# only covers the most common shipping destinations — we prefer "good
# enough for the top 95%" over a full ISO 3166-2 mapping.
_SAUDI_REGION_BY_CITY: Dict[str, str] = {
    # Riyadh region
    "الرياض": "منطقة الرياض",  "الخرج": "منطقة الرياض", "الدرعية": "منطقة الرياض",
    "riyadh": "Riyadh Region", "ar-riyadh": "Riyadh Region",
    # Makkah region
    "مكة": "منطقة مكة المكرمة", "مكة المكرمة": "منطقة مكة المكرمة",
    "جدة": "منطقة مكة المكرمة", "الطائف": "منطقة مكة المكرمة",
    "makkah": "Makkah Region",  "jeddah": "Makkah Region", "taif": "Makkah Region",
    # Madinah region
    "المدينة": "منطقة المدينة المنورة", "المدينة المنورة": "منطقة المدينة المنورة",
    "ينبع": "منطقة المدينة المنورة",
    "madinah": "Madinah Region", "yanbu": "Madinah Region",
    # Eastern Province
    "الدمام": "المنطقة الشرقية", "الخبر": "المنطقة الشرقية",
    "الظهران": "المنطقة الشرقية", "الأحساء": "المنطقة الشرقية", "الجبيل": "المنطقة الشرقية",
    "dammam": "Eastern Province", "khobar": "Eastern Province",
    # Asir
    "أبها": "منطقة عسير", "خميس مشيط": "منطقة عسير", "abha": "Aseer Region",
    # Qassim
    "بريدة": "منطقة القصيم", "عنيزة": "منطقة القصيم",
    "buraidah": "Qassim Region",
    # Tabuk / Hail / Najran / Jazan / Northern / Bahah
    "تبوك": "منطقة تبوك", "حائل": "منطقة حائل", "نجران": "منطقة نجران",
    "جازان": "منطقة جازان", "الباحة": "منطقة الباحة", "عرعر": "الحدود الشمالية",
}


def _resolve_saudi_region(city: str) -> str:
    """Return the Saudi region name for a given city, or the city itself
    when no mapping exists. Case-insensitive on Latin characters."""
    raw = (city or "").strip()
    if not raw:
        return ""
    key = raw.lower() if raw.isascii() else raw
    return _SAUDI_REGION_BY_CITY.get(key, raw)


def validate_salla_order_payload(body: Dict[str, Any]) -> List[str]:
    """Return the list of canonical field names missing from a Salla order
    payload. Empty list means the payload satisfies Salla's hard requirements.

    Canonical names map back to ``OrderInput`` slots so the conversation
    layer can ask for them in Arabic without translating Salla-speak.
    """
    missing: List[str] = []

    # ── Products ─────────────────────────────────────────────────────────────
    products = body.get("products") or []
    if not products or not isinstance(products, list):
        missing.append("product")
    else:
        first = products[0] or {}
        if not first.get("identifier"):
            missing.append("product_id")

    # ── Customer ─────────────────────────────────────────────────────────────
    customer = body.get("customer") or {}
    if not (customer.get("first_name") or "").strip():
        missing.append("customer_first_name")
    if not (customer.get("mobile") or "").strip():
        missing.append("customer_phone")

    # ── Payment ──────────────────────────────────────────────────────────────
    pay = body.get("payment") or {}
    accepted = pay.get("accepted_methods") or []
    if not accepted:
        missing.append("payment_method")

    # ── Address (only when shipping is actually being requested) ──────────────
    # If the body has no `address` block AND no shipping section, we treat
    # this as a digital / pickup order and don't enforce address fields.
    has_shipping = bool(body.get("shipping")) or bool(body.get("address"))
    if has_shipping:
        addr = body.get("address") or {}
        if not (addr.get("city") or "").strip():
            missing.append("city")
        if not (addr.get("street") or "").strip():
            missing.append("address")
        # Country defaults to Saudi Arabia in _build_order_body — we don't
        # add it to `missing` here because we control the default.

    return missing


@register_adapter("salla")
class SallaAdapter(BaseStoreAdapter):
    platform = "salla"

    def __init__(
        self,
        api_key: str,
        store_id: str = "",
        refresh_token: str = "",
        tenant_id: int = 0,
        integration_id: Optional[int] = None,
        expires_at: Optional[str] = None,
    ):
        self.api_key = api_key
        self.store_id = store_id
        self._refresh_token = refresh_token
        self._tenant_id = tenant_id
        # The exact DB row this adapter was built from.  MUST be used when
        # persisting refreshed tokens or marking needs_reauth so we always
        # update the correct row — not whichever `.first()` returns (which
        # may be a different, older integration).
        self._integration_id: Optional[int] = integration_id
        # ISO timestamp when the current access_token expires (populated from
        # config.expires_at / config.token_expires_at at construction time).
        # Updated in-memory after each successful refresh so the guard stays
        # accurate without re-reading the DB on every call.
        self._expires_at: Optional[str] = expires_at

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _refresh_access_token(self) -> bool:
        """Use refresh_token to get a new access_token from Salla.

        Returns True on success, False on any failure.

        Refresh-token null-safety: if Salla's response does not include a new
        refresh_token, the existing one is kept as-is — it is NEVER replaced
        with null/empty.

        Race-condition safety: acquires the in-process asyncio lock for this
        integration_id before calling Salla's OAuth endpoint.  If another
        coroutine is already refreshing the same integration, this call
        returns True optimistically (the other coroutine will update the DB).

        Failure escalation: after 3 consecutive failures, sets needs_reauth=True
        and logs [SALLA TOKEN] refresh failed 3 times; needs reauth.
        """
        # Pre-condition: refresh_token must be present
        if not self._refresh_token:
            logger.error(
                "[Salla Token] refresh failed tenant=%s reason=no_refresh_token "
                "(merchant must re-authorise Salla integration)",
                self._tenant_id,
            )
            self._mark_needs_reauth("no_refresh_token")
            return False
        if not self._tenant_id:
            logger.error(
                "[Salla Token] refresh failed tenant=0 reason=no_tenant_id "
                "(adapter constructed without tenant_id)",
            )
            return False

        client_id     = os.environ.get("SALLA_CLIENT_ID", "")
        client_secret = os.environ.get("SALLA_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            logger.error(
                "[Salla Token] refresh failed tenant=%s reason=missing_oauth_env "
                "(SALLA_CLIENT_ID/SALLA_CLIENT_SECRET not configured)",
                self._tenant_id,
            )
            # Config issue, not a token problem — don't set needs_reauth
            return False

        # ── Acquire in-process lock — skip if another coroutine is already refreshing ──
        from core.salla_token_lock import salla_asyncio_lock  # noqa: PLC0415
        async with salla_asyncio_lock(self._integration_id, caller="adapter") as acquired:
            if not acquired:
                # Another coroutine in this process is refreshing the same
                # integration.  Return True optimistically — by the time the
                # caller uses self.api_key, the other coroutine will have
                # updated it in the DB and in-memory.
                logger.info(
                    "[Salla Token] refresh deferred to concurrent task | tenant=%s",
                    self._tenant_id,
                )
                return True

            logger.info(
                "[Salla Token] refresh started tenant=%s integration_id=%s",
                self._tenant_id, self._integration_id,
            )
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        "https://accounts.salla.sa/oauth2/token",
                        data={
                            "grant_type":    "refresh_token",
                            "client_id":     client_id,
                            "client_secret": client_secret,
                            "refresh_token": self._refresh_token,
                        },
                        headers={
                            "Accept":       "application/json",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )
                    if resp.status_code != 200:
                        resp_text = resp.text[:300]
                        # invalid_grant = token permanently revoked by Salla
                        if resp.status_code == 400 and "invalid_grant" in resp_text:
                            logger.error(
                                "[Salla Token] refresh failed tenant=%s reason=invalid_grant "
                                "(refresh_token revoked by Salla — merchant must re-authorise)",
                                self._tenant_id,
                            )
                            self._mark_needs_reauth("invalid_grant")
                            raise SallaTokenRevokedException(
                                f"Salla refresh_token revoked for tenant={self._tenant_id} (invalid_grant)"
                            )
                        # For all other failures: record + maybe escalate
                        err_msg = f"HTTP {resp.status_code}: {resp_text}"
                        if 400 <= resp.status_code < 500:
                            logger.error(
                                "[Salla Token] refresh failed tenant=%s reason=oauth_%d response=%s",
                                self._tenant_id, resp.status_code, resp_text,
                            )
                        else:
                            logger.error(
                                "[Salla Token] refresh failed tenant=%s reason=oauth_%d (transient) response=%s",
                                self._tenant_id, resp.status_code, resp_text,
                            )
                        self._record_refresh_failure(err_msg)
                        return False

                    data        = resp.json()
                    new_access  = data.get("access_token", "")
                    # Guard: never overwrite refresh_token with null/empty
                    _raw_rt     = data.get("refresh_token")
                    new_refresh = _raw_rt if _raw_rt else self._refresh_token
                    expires_in  = data.get("expires_in")
                    if not new_access:
                        logger.error(
                            "[Salla Token] refresh failed tenant=%s reason=empty_access_token",
                            self._tenant_id,
                        )
                        self._record_refresh_failure("empty_access_token")
                        return False

                    # Update in-memory + persist
                    self.api_key        = new_access
                    self._refresh_token = new_refresh
                    self._persist_refreshed_tokens(new_access, new_refresh, expires_in)
                    logger.info(
                        "[Salla Token] refresh success tenant=%s expires_in=%s",
                        self._tenant_id, expires_in,
                    )
                    return True

            except SallaTokenRevokedException:
                raise
            except Exception as exc:
                logger.exception(
                    "[Salla Token] refresh failed tenant=%s reason=exception err=%s",
                    self._tenant_id, exc,
                )
                self._record_refresh_failure(str(exc)[:400])
                return False

    def _require_auth(self, operation: str = "API call") -> None:
        """Preflight check before write operations.

        Policy:
          • NO access_token  → hard block (nothing we can do)
          • access_token only (no refresh_token) → WARNING + proceed.
            The token may still be valid. If Salla returns 401, the
            caller's retry logic will attempt a refresh (which will fail
            and then mark needs_reauth, prompting the merchant to reconnect).
          • Both tokens present → normal operation; refresh is available.
        """
        if not self.api_key:
            logger.error(
                "[Salla] blocked API call — no access_token | "
                "tenant=%s integration_id=%s operation=%s",
                self._tenant_id, self._integration_id, operation,
            )
            raise SallaTokenRevokedException(
                f"Integration for tenant={self._tenant_id} has no access_token "
                f"(operation={operation}). Merchant must reconnect Salla."
            )

        if not self._refresh_token:
            # access_token exists but cannot be auto-refreshed.
            # Proceed optimistically — Salla access_tokens can last up to
            # 14 days. If this one is expired, the 401 handler will fire
            # and mark needs_reauth.
            logger.warning(
                "[Salla Integration] operating without refresh_token | "
                "tenant=%s integration_id=%s operation=%s "
                "has_access_token=True has_refresh_token=False "
                "— will proceed; 401 will mark needs_reauth",
                self._tenant_id, self._integration_id, operation,
            )
        else:
            logger.info(
                "[Salla Integration] selected for order creation | "
                "tenant=%s integration_id=%s operation=%s "
                "has_access_token=True has_refresh_token=True",
                self._tenant_id, self._integration_id, operation,
            )

    async def _ensure_token_fresh(self) -> None:
        """Proactively refresh the access_token before an API call if it is
        expired or expiring within 24 hours.

        Uses self._expires_at (set at construction or updated after each
        successful refresh) so no extra DB query is needed on the hot path.
        Only runs when both refresh_token and expires_at are known.

        Logs [SALLA TOKEN] access token refreshed before API call on success.
        Silent on error — reactive 401 handling in _get/_post acts as fallback.
        """
        if not self._refresh_token or not self._expires_at:
            return
        try:
            _exp_dt = datetime.fromisoformat(self._expires_at.replace("Z", "+00:00"))
            if _exp_dt.tzinfo is None:
                _exp_dt = _exp_dt.replace(tzinfo=timezone.utc)
            _days_until = (_exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400
            if _days_until < 1:
                logger.info(
                    "[SALLA TOKEN] access token refreshed before API call | "
                    "tenant=%s days_until_expiry=%.2f",
                    self._tenant_id, _days_until,
                )
                await self._refresh_access_token()
        except Exception as exc:
            logger.debug("[Salla Token] _ensure_token_fresh error (non-fatal): %s", exc)

    def _mark_needs_reauth(self, reason: str = "unknown") -> None:
        """Mark integration as `needs_reauth=True` and stop syncing.

        Sets:
          • config.needs_reauth         = True
          • config.needs_reauth_reason  = reason
          • config.needs_reauth_at      = ISO timestamp
          • config.refresh_token        = removed (revoked / unusable)

        Always targets self._integration_id when available, so we update
        the CORRECT row — not whichever `.first()` returns.
        """
        try:
            from database.session import SessionLocal  # noqa: PLC0415
            from database.models import Integration as _Integration  # noqa: PLC0415
            _db = SessionLocal()
            try:
                q = _db.query(_Integration)
                if self._integration_id:
                    q = q.filter(_Integration.id == self._integration_id)
                else:
                    # Fallback: no id stored — update the canonical row only.
                    # ORDER BY id DESC so newest (canonical) row is updated first.
                    q = q.filter(
                        _Integration.tenant_id == self._tenant_id,
                        _Integration.provider == "salla",
                    ).order_by(_Integration.id.desc())
                intg = q.first()
                if intg:
                    cfg = dict(intg.config or {})
                    cfg.pop("refresh_token", None)
                    cfg["needs_reauth"]        = True
                    cfg["needs_reauth_reason"] = reason
                    cfg["needs_reauth_at"]     = datetime.now(timezone.utc).isoformat()
                    intg.config = cfg
                    _db.commit()
                    logger.warning(
                        "[Salla Token] needs_reauth=true tenant=%s integration_id=%s "
                        "reason=%s — sync paused until merchant re-authorises",
                        self._tenant_id, intg.id, reason,
                    )
            finally:
                _db.close()
        except Exception as exc:
            logger.warning(
                "[Salla Token] failed to persist needs_reauth tenant=%s: %s",
                self._tenant_id, exc,
            )

    def _record_refresh_failure(self, error_msg: str) -> None:
        """Persist a refresh failure and apply grace-window escalation.

        Increments ``token_refresh_attempts`` and sets failure metadata.
        Applies the 24-hour grace window via ``should_escalate_to_needs_reauth``:
        only sets ``needs_reauth=True`` after 3 clustered failures AND the token
        is actually expiring soon — preventing false alarms from transient outages.

        Never disables the integration.
        """
        try:
            from database.session import SessionLocal  # noqa: PLC0415
            from database.models import Integration as _Integration  # noqa: PLC0415
            from core.salla_token_alerts import (  # noqa: PLC0415
                should_escalate_to_needs_reauth,
                log_metric_failed,
                log_metric_needs_reauth,
            )
            _db = SessionLocal()
            try:
                q = _db.query(_Integration)
                if self._integration_id:
                    q = q.filter(_Integration.id == self._integration_id)
                else:
                    q = q.filter(
                        _Integration.tenant_id == self._tenant_id,
                        _Integration.provider  == "salla",
                    ).order_by(_Integration.id.desc())
                intg = q.first()
                if intg:
                    cfg          = dict(intg.config or {})
                    prev_attempts = cfg.get("token_refresh_attempts", 0)
                    new_attempts  = prev_attempts + 1
                    _now          = datetime.now(timezone.utc)
                    _now_iso      = _now.isoformat()

                    # Track start of failure streak for grace window
                    if prev_attempts == 0:
                        cfg["token_refresh_first_failed_at"] = _now_iso

                    cfg["token_refresh_status"]    = "failed"
                    cfg["token_refresh_error"]     = error_msg
                    cfg["token_refresh_failed_at"] = _now_iso
                    cfg["token_refresh_attempts"]  = new_attempts

                    logger.warning(
                        "[SALLA TOKEN] refresh failed | tenant=%s integration_id=%s "
                        "attempts=%s error=%s",
                        self._tenant_id, self._integration_id, new_attempts, error_msg,
                    )
                    log_metric_failed(self._tenant_id or 0, cfg.get("store_id", "?"), new_attempts)

                    # ── Grace-window escalation ──────────────────────────────
                    escalate, reason = should_escalate_to_needs_reauth(cfg, _now)
                    if escalate and not cfg.get("needs_reauth"):
                        cfg["needs_reauth"]        = True
                        cfg["needs_reauth_reason"] = reason
                        cfg["needs_reauth_at"]     = _now_iso
                        logger.critical(
                            "[SALLA TOKEN] refresh failed 3 times; needs reauth | "
                            "tenant=%s integration_id=%s attempts=%s reason=%s",
                            self._tenant_id, self._integration_id, new_attempts, reason,
                        )
                        log_metric_needs_reauth(
                            self._tenant_id or 0, cfg.get("store_id", "?"), reason or "unknown"
                        )
                        # Alert sending is fire-and-forget inside a sync context;
                        # the scheduler sends the email for scheduler-triggered failures.
                        # For on-demand (401) failures, we log critical and rely on
                        # the next scheduler cycle to send the email (which has the
                        # async context needed for send_email).

                    intg.config = cfg
                    _db.commit()
            finally:
                _db.close()
        except Exception as exc:
            logger.warning(
                "[Salla Token] failed to record refresh failure tenant=%s: %s",
                self._tenant_id, exc,
            )

    def _persist_refreshed_tokens(
        self,
        access_token: str,
        refresh_token: str,
        expires_in: Optional[int] = None,
    ) -> None:
        """Save refreshed tokens back to the CORRECT Integration row.

        Always uses self._integration_id (the exact row this adapter was
        built from) so tokens are never accidentally written to a different
        row that happened to be first in DB ordering.
        """
        try:
            from database.session import SessionLocal
            from database.models import Integration
            db = SessionLocal()
            try:
                q = db.query(Integration)
                if self._integration_id:
                    q = q.filter(Integration.id == self._integration_id)
                else:
                    # No id — fall back to newest row so we update the
                    # canonical integration, not the oldest stale one.
                    q = q.filter(
                        Integration.tenant_id == self._tenant_id,
                        Integration.provider == "salla",
                    ).order_by(Integration.id.desc())
                intg = q.first()
                if intg:
                    cfg = dict(intg.config or {})
                    _now_persist = datetime.now(timezone.utc)
                    cfg["api_key"]               = access_token
                    # Guard: never overwrite existing refresh_token with null
                    cfg["refresh_token"]         = refresh_token or cfg.get("refresh_token", "")
                    cfg["last_token_refresh"]    = _now_persist.isoformat()  # backward compat
                    cfg["last_token_refresh_at"] = _now_persist.isoformat()  # new field
                    cfg["token_refresh_status"]  = "success"
                    cfg["token_refresh_attempts"] = 0  # reset streak on success
                    cfg.pop("token_refresh_error",            None)
                    cfg.pop("token_refresh_failed_at",        None)
                    cfg.pop("token_refresh_first_failed_at",  None)  # reset grace window
                    cfg.pop("needs_reauth",                   None)
                    cfg.pop("needs_reauth_reason",            None)
                    cfg.pop("needs_reauth_at",                None)
                    cfg.pop("token_reauth_alert_sent_at",     None)  # reset alert cooldown
                    from core.salla_token_alerts import log_metric_success  # noqa: PLC0415
                    log_metric_success(self._tenant_id or 0, cfg.get("store_id", "?"))
                    if expires_in:
                        try:
                            _exp_at = _now_persist + timedelta(seconds=int(expires_in))
                            _exp_iso = _exp_at.isoformat()
                            cfg["token_expires_at"] = _exp_iso   # backward compat
                            cfg["expires_at"]       = _exp_iso   # new canonical field
                            self._expires_at        = _exp_iso   # update in-memory
                        except Exception:
                            pass
                    # Successful refresh clears any stale reauth/no_auto_refresh flags.
                    cfg.pop("needs_reauth", None)
                    cfg.pop("needs_reauth_reason", None)
                    cfg.pop("needs_reauth_at", None)
                    cfg.pop("no_auto_refresh", None)
                    cfg.pop("no_auto_refresh_reason", None)
                    cfg.pop("no_auto_refresh_at", None)
                    intg.config = cfg
                    db.commit()
                    logger.info(
                        "[Salla Token] tokens persisted → integration_id=%s tenant=%s",
                        intg.id, self._tenant_id,
                    )
            finally:
                db.close()
        except Exception as exc:
            logger.warning("[Salla Token] failed to persist refreshed tokens: %s", exc)

    async def _get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        await self._ensure_token_fresh()
        url = f"{SALLA_API_BASE}{path}"
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, headers=self._headers(), params=params or {})
            logger.info(
                "[Salla API] GET %s → %d | tenant=%s store=%s",
                path, resp.status_code, self._tenant_id, self.store_id,
            )
            if resp.status_code == 401:
                logger.warning(
                    "[Salla Token] 401 detected tenant=%s path=%s response=%s",
                    self._tenant_id, path, resp.text[:200],
                )
                # _refresh_access_token logs `refresh started/success/failed`
                # itself and raises SallaTokenRevokedException on invalid_grant.
                refreshed = await self._refresh_access_token()
                if refreshed:
                    logger.info(
                        "[Salla Token] retry original request tenant=%s path=%s method=GET",
                        self._tenant_id, path,
                    )
                    resp = await client.get(url, headers=self._headers(), params=params or {})
                    logger.info(
                        "[Salla API] RETRY GET %s → %d | tenant=%s",
                        path, resp.status_code, self._tenant_id,
                    )
                    if resp.status_code == 401:
                        # Still 401 after refresh → token is somehow invalid → halt.
                        logger.error(
                            "[Salla Token] retry still returned 401 tenant=%s path=%s — "
                            "marking needs_reauth",
                            self._tenant_id, path,
                        )
                        self._mark_needs_reauth("retry_still_401")
                        raise SallaTokenRevokedException(
                            f"Salla still 401 after refresh for tenant={self._tenant_id}"
                        )
                else:
                    # Refresh failed; _refresh_access_token already marked
                    # needs_reauth (when appropriate). Stop this sync.
                    raise SallaTokenRevokedException(
                        f"Salla token refresh failed for tenant={self._tenant_id} on {path}"
                    )
            if resp.status_code >= 400:
                logger.error(
                    "[Salla API] ERROR GET %s → %d | tenant=%s body=%s",
                    path, resp.status_code, self._tenant_id, resp.text[:300],
                )
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_token_fresh()
        import json as _json
        url = f"{SALLA_API_BASE}{path}"
        # Always emit the request payload for /orders at ERROR level so it
        # appears alongside any failure in Railway log filters that hide INFO.
        _payload_str = _json.dumps(body, ensure_ascii=False)
        if path == "/orders":
            # Unmissable pre-flight log — this MUST appear on every order
            # creation attempt regardless of which path triggered it. If
            # we ever see Salla return 422 without this line, it means a
            # different process is posting orders (impossible via this
            # adapter) or the deployment is stale.
            try:
                _items_brief = [
                    {
                        "identifier": p.get("identifier"),
                        "quantity": p.get("quantity"),
                        "options": p.get("options"),
                    }
                    for p in (body.get("products") or [])
                ]
            except Exception:
                _items_brief = []
            logger.error(
                "[SallaAdapter] ABOUT_TO_POST_ORDER | tenant=%s products=%s",
                self._tenant_id,
                _items_brief,
            )
            logger.error(
                "[SallaAdapter] POST /orders REQUEST | tenant=%s payload=%s",
                self._tenant_id,
                _payload_str,
            )
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
            logger.info("[Salla API] POST %s → %d | tenant=%s", path, resp.status_code, self._tenant_id)
            if resp.status_code == 401:
                logger.warning(
                    "[Salla Token] 401 detected tenant=%s path=%s method=POST response=%s",
                    self._tenant_id, path, resp.text[:200],
                )
                refreshed = await self._refresh_access_token()
                if refreshed:
                    logger.info(
                        "[Salla Token] retry original request tenant=%s path=%s method=POST",
                        self._tenant_id, path,
                    )
                    resp = await client.post(url, headers=self._headers(), json=body)
                    logger.info(
                        "[Salla API] RETRY POST %s → %d | tenant=%s",
                        path, resp.status_code, self._tenant_id,
                    )
                    if resp.status_code == 401:
                        logger.error(
                            "[Salla Token] retry still returned 401 tenant=%s path=%s — "
                            "marking needs_reauth",
                            self._tenant_id, path,
                        )
                        self._mark_needs_reauth("retry_still_401")
                        raise SallaTokenRevokedException(
                            f"Salla still 401 after refresh for tenant={self._tenant_id}"
                        )
                else:
                    raise SallaTokenRevokedException(
                        f"Salla token refresh failed for tenant={self._tenant_id} on POST {path}"
                    )
            if resp.status_code >= 400:
                # Emit the FULL response body — DO NOT truncate. Salla's 422
                # validation messages are nested under `error.fields` and we
                # need the entire structure to know which field was rejected.
                _raw_text = resp.text or ""
                _parsed: Optional[Dict[str, Any]] = None
                try:
                    _parsed_obj = resp.json()
                    if isinstance(_parsed_obj, dict):
                        _parsed = _parsed_obj
                except Exception:
                    _parsed = None

                logger.error(
                    "[SallaAdapter] POST %s FAILED | tenant=%s status=%d response=%s",
                    path, self._tenant_id, resp.status_code, _raw_text,
                )
                logger.error(
                    "[SallaAdapter] POST %s FAILED | tenant=%s request_payload=%s",
                    path, self._tenant_id, _payload_str,
                )
                # Best-effort field-level breakdown to make root cause obvious.
                if _parsed is not None:
                    _err = _parsed.get("error") if isinstance(_parsed.get("error"), dict) else None
                    _msg = (_parsed.get("message")
                            or (_err.get("message") if _err else "")
                            or "")
                    _fields = (_err or {}).get("fields") if _err else None
                    if _fields is None:
                        _fields = _parsed.get("errors") or _parsed.get("fields")
                    logger.error(
                        "[SallaAdapter] POST %s FAILED | tenant=%s status=%d "
                        "salla_message=%r salla_fields=%s",
                        path, self._tenant_id, resp.status_code,
                        _msg, _json.dumps(_fields, ensure_ascii=False) if _fields is not None else "<none>",
                    )
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str) -> bool:
        """DELETE helper. Returns True on 2xx, False otherwise (never raises)."""
        await self._ensure_token_fresh()
        url = f"{SALLA_API_BASE}{path}"
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.delete(url, headers=self._headers())
                if resp.status_code == 401:
                    logger.warning(
                        "[Salla Token] 401 detected tenant=%s path=%s method=DELETE",
                        self._tenant_id, path,
                    )
                    if await self._refresh_access_token():
                        logger.info(
                            "[Salla Token] retry original request tenant=%s path=%s method=DELETE",
                            self._tenant_id, path,
                        )
                        resp = await client.delete(url, headers=self._headers())
                logger.info(
                    "[Salla API] DELETE %s → %d | tenant=%s", path, resp.status_code, self._tenant_id,
                )
                return 200 <= resp.status_code < 300 or resp.status_code == 404
        except Exception as exc:
            self._log_error("_delete", exc)
            return False

    def _log_error(self, method: str, exc: Exception) -> None:
        logger.error(f"SallaAdapter.{method} failed: {exc}", exc_info=True)

    # ── Pagination helper ────────────────────────────────────────────────────

    async def _get_all_pages(
        self,
        path: str,
        per_page: int = 50,
        extra_params: Optional[Dict[str, Any]] = None,
        label: str = "",
    ) -> List[Dict[str, Any]]:
        """Fetch ALL pages from a paginated Salla endpoint until data is exhausted.

        No hard page limit — continues until:
          1. API returns an empty page, OR
          2. Current page >= total pages reported by API, OR
          3. A single page returns fewer items than per_page (last page).
        """
        tag = label or path.strip("/")
        all_items: List[Dict[str, Any]] = []
        page = 1
        total_pages_hint = None

        while True:
            params: Dict[str, Any] = {"per_page": per_page, "page": page}
            if extra_params:
                params.update(extra_params)

            try:
                data = await self._get(path, params)
            except SallaTokenRevokedException:
                raise  # propagate — callers must handle this as a hard stop
            except Exception as exc:
                logger.error(
                    "[Salla:%s] tenant=%s page %d FAILED — stopping pagination: %s",
                    tag, self._tenant_id, page, exc,
                )
                break

            items = data.get("data") or []
            all_items.extend(items)

            pagination = data.get("pagination") or data.get("meta") or {}
            total_pages_hint = pagination.get(
                "totalPages",
                pagination.get("last_page", pagination.get("total_pages", None)),
            )
            total_items_hint = pagination.get(
                "total", pagination.get("count", None),
            )

            logger.info(
                "[Salla:%s] tenant=%s page %d → %d items (cumulative=%d%s)",
                tag, self._tenant_id, page, len(items), len(all_items),
                f", total_pages={total_pages_hint}" if total_pages_hint else "",
            )

            if not items:
                break
            if total_pages_hint and page >= total_pages_hint:
                break
            if len(items) < per_page:
                break

            page += 1

        logger.info(
            "[Salla:%s] tenant=%s pagination complete — %d total items across %d pages",
            tag, self._tenant_id, len(all_items), page,
        )
        return all_items

    # ── Products ───────────────────────────────────────────────────────────────

    async def get_products(self, updated_since: Optional[str] = None) -> List[NormalizedProduct]:
        try:
            extra: Optional[Dict[str, Any]] = None
            if updated_since:
                extra = {"updated_at_min": updated_since}
            raw_list = await self._get_all_pages("/products", label="products", extra_params=extra)
            return [self._normalize_product(p) for p in raw_list]
        except httpx.HTTPStatusError as exc:
            self._log_error("get_products", exc)
            logger.error(f"Salla get_products HTTP error {exc.response.status_code}: {exc.response.text[:200]}")
            raise
        except Exception as exc:
            self._log_error("get_products", exc)
            raise

    async def get_pages(self) -> List[Dict[str, Any]]:
        """Fetch all store pages from Salla CMS (GET /pages).

        Returns raw page dicts. Failures are logged and an empty list is
        returned so callers can treat this as a non-fatal, best-effort fetch.
        Each page dict contains at minimum: id, title, slug, status, content (HTML).
        """
        try:
            raw_list = await self._get_all_pages("/pages", label="pages", per_page=50)
            logger.info(
                "[Salla] get_pages: fetched %d pages | tenant=%s",
                len(raw_list), self._tenant_id,
            )
            return raw_list
        except Exception as exc:
            self._log_error("get_pages", exc)
            logger.warning(
                "[Salla] get_pages failed (non-fatal) | tenant=%s error=%s",
                self._tenant_id, exc,
            )
            return []

    async def get_product(self, product_id: str) -> Optional[NormalizedProduct]:
        try:
            data = await self._get(f"/products/{product_id}")
            raw = data.get("data") or {}
            if not raw:
                # Log what Salla actually returned — helps diagnose why the
                # product is missing: stale id, wrong scope, archived item, etc.
                logger.warning(
                    "[SallaAdapter] get_product(%s) returned empty data | "
                    "salla_success=%s salla_status=%s — "
                    "product may be deleted, archived, or out of sync | tenant=%s",
                    product_id,
                    data.get("success"),
                    data.get("status"),
                    self._tenant_id,
                )
                return None

            # ── Always reconcile against /products/{id}/options ───────────
            # Salla's product detail endpoint frequently returns the
            # product without its `options` array (we have observed
            # this on real merchant stores even after passing
            # `?include=options`). Hitting the dedicated options
            # endpoint UNCONDITIONALLY is the only reliable way to
            # know whether the product has variant option groups —
            # otherwise we end up posting an order with no options
            # and Salla rejects it with 422
            # ("خيارات المنتج مطلوبة"). The endpoint is cheap and
            # the response is small, so we always reconcile.
            try:
                opt_data = await self._get(f"/products/{product_id}/options")
                fallback_opts = opt_data.get("data") or []
                detail_opts = raw.get("options") or []
                if isinstance(fallback_opts, list) and len(fallback_opts) > len(detail_opts or []):
                    raw["options"] = fallback_opts
                    logger.info(
                        "[SallaAdapter] product options reconciled via /products/%s/options | "
                        "detail=%d dedicated=%d",
                        product_id,
                        len(detail_opts or []),
                        len(fallback_opts),
                    )
            except httpx.HTTPStatusError as opt_exc:
                # Endpoint not available on every Salla plan/scope.
                logger.info(
                    "[SallaAdapter] /products/%s/options unavailable (%s) — "
                    "using detail-endpoint options only",
                    product_id, opt_exc.response.status_code,
                )
            except Exception as opt_exc:
                logger.info(
                    "[SallaAdapter] product options fallback fetch failed | "
                    "product=%s err=%s",
                    product_id, opt_exc,
                )

            normalized = self._normalize_product(raw)
            logger.info(
                "[SallaAdapter] get_product | id=%s title=%r option_groups=%d "
                "has_required_options=%s",
                product_id,
                normalized.title,
                len(normalized.options or []),
                normalized.has_required_options,
            )
            return normalized
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            self._log_error("get_product", exc)
            raise
        except Exception as exc:
            self._log_error("get_product", exc)
            raise

    async def get_product_variants(self, product_id: str) -> List[NormalizedVariant]:
        product = await self.get_product(product_id)
        return product.variants if product else []

    def _normalize_product(self, raw: Dict[str, Any]) -> NormalizedProduct:
        price_block = raw.get("price") or {}
        price_amount = price_block.get("amount") if isinstance(price_block, dict) else raw.get("price")
        try:
            price_f = float(price_amount) if price_amount is not None else None
        except (TypeError, ValueError):
            price_f = None

        variants = [
            self._normalize_variant(v)
            for v in (raw.get("variants") or [])
        ]

        options, has_required = self._normalize_options(raw.get("options") or [])

        return NormalizedProduct(
            id=str(raw.get("id", "")),
            title=raw.get("name") or raw.get("title") or "",
            price=price_f,
            currency=(price_block.get("currency") if isinstance(price_block, dict) else "SAR") or "SAR",
            sku=raw.get("sku") or "",
            in_stock=(raw.get("quantity", 1) or 0) > 0,
            stock_quantity=raw.get("quantity"),
            description=(raw.get("description") or "")[:300],
            image_url=raw.get("main_image") or raw.get("thumbnail"),
            product_url=raw.get("url"),
            tags=raw.get("tags") or [],
            variants=variants,
            options=options,
            has_required_options=has_required,
        )

    def _normalize_options(
        self, raw_options: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Convert a Salla `options` array into a stable, JSON-friendly shape.

        Salla returns each option group as
            {id, name, type, required, values: [{id, name, ...}, ...]}.
        We keep the same structure (so the adapter can refer back to ids
        when posting an order) and surface `has_required_options` so the
        Brain knows whether to ask the customer before creating the order.
        """
        out: List[Dict[str, Any]] = []
        has_required = False
        for opt in raw_options or []:
            if not isinstance(opt, dict):
                continue
            opt_id = opt.get("id")
            opt_name = (opt.get("name") or "").strip()
            opt_type = (opt.get("type") or "select")
            # Salla's product API is unreliable about the `required`
            # flag — some payloads omit it entirely, others use
            # `is_required`, and we have observed Salla return 422
            # ("خيارات المنتج مطلوبة") for products whose option
            # objects had `required: false`. Treat EVERY option group
            # with values as required from the conversation flow's
            # perspective: sending an option Salla deems optional is
            # harmless, while skipping one Salla deems required
            # blocks order creation. The explicit `is_required: true`
            # / `required: true` paths are kept for completeness but
            # do not change the default.
            opt_required = True  # safe default — see comment above
            if "required" in opt:
                opt_required = bool(opt.get("required")) or opt_required
            if "is_required" in opt:
                opt_required = bool(opt.get("is_required")) or opt_required
            values_raw = opt.get("values") or []
            values_out: List[Dict[str, Any]] = []
            for val in values_raw:
                if not isinstance(val, dict):
                    continue
                values_out.append({
                    "id": val.get("id"),
                    "name": (val.get("name") or "").strip(),
                    "price": val.get("price"),
                    "image_url": val.get("image_url") or val.get("image"),
                })
            if not opt_name:
                continue
            out.append({
                "id": opt_id,
                "name": opt_name,
                "type": opt_type,
                "required": opt_required,
                "values": values_out,
            })
            if opt_required and values_out:
                has_required = True
        return out, has_required

    def _normalize_variant(self, raw: Dict[str, Any]) -> NormalizedVariant:
        price_block = raw.get("price") or {}
        price_amount = price_block.get("amount") if isinstance(price_block, dict) else raw.get("price")
        try:
            price_f = float(price_amount) if price_amount is not None else None
        except (TypeError, ValueError):
            price_f = None
        return NormalizedVariant(
            id=str(raw.get("id", "")),
            title=raw.get("name") or str(raw.get("id", "")),
            price=price_f,
            sku=raw.get("sku"),
            in_stock=raw.get("available", True),
            stock_quantity=raw.get("quantity"),
        )

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def _assert_required_options_present(self, order_input: OrderInput) -> None:
        """Last-line defence against Salla 422 (`خيارات المنتج مطلوبة`).

        For every order item that arrives WITHOUT any options selected,
        fetch the product from Salla and check whether it has any option
        groups (treating ALL groups as potentially required because Salla's
        `required` flag is unreliable). If yes, abort the order — do NOT
        call POST /orders. Raises
        ``ValueError("required_product_options_missing")``.
        """
        for item in order_input.items or []:
            if item.options:
                continue
            pid = str(item.product_id or "").strip()
            if not pid:
                continue
            try:
                product = await self.get_product(pid)
            except Exception as exc:
                logger.warning(
                    "[SallaAdapter] options pre-flight: get_product failed | "
                    "product=%s err=%s — proceeding anyway",
                    pid, exc,
                )
                continue
            if not product:
                continue
            # Treat any group-with-values as required: Salla's per-group
            # `required` flag has been observed to lie.
            groups_with_values = [
                g for g in (product.options or []) if g.get("values")
            ]
            if groups_with_values:
                logger.error(
                    "[SallaAdapter] BLOCKING create_order: product has options but none supplied | "
                    "tenant=%s product=%s groups=%s",
                    self._tenant_id, pid,
                    [g.get("name") for g in groups_with_values],
                )
                raise ValueError("required_product_options_missing")

    async def _resolve_variant_id(
        self,
        product_id: str,
        selected_options: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Find the Salla variant whose option_values match the selection.

        Salla orders for products with size/colour variants accept either
        an ``options`` array or an explicit ``variant_id``. Sending both
        is the safest combination — ``variant_id`` is unambiguous and
        bypasses any Salla-side options-resolution edge cases. This
        helper queries ``/products/{id}/variants`` and looks for the
        variant whose ``related_options`` (or equivalent) align with the
        selected option_id+value_id pairs.
        """
        if not (product_id and selected_options):
            return None
        # Build a lookup of {option_id: value_id} from the picked options.
        wanted: Dict[str, str] = {}
        for sel in selected_options:
            if not isinstance(sel, dict):
                continue
            oid = sel.get("option_id") if "option_id" in sel else sel.get("id")
            vid = sel.get("value_id") if "value_id" in sel else sel.get("value")
            if oid is None or vid is None:
                continue
            wanted[str(oid)] = str(vid)
        if not wanted:
            return None
        try:
            data = await self._get(f"/products/{product_id}/variants")
        except httpx.HTTPStatusError as exc:
            logger.info(
                "[SallaAdapter] /products/%s/variants unavailable (%s) — skipping variant resolution",
                product_id, exc.response.status_code,
            )
            return None
        except Exception as exc:
            logger.info(
                "[SallaAdapter] variant resolution failed | product=%s err=%s",
                product_id, exc,
            )
            return None
        variants = data.get("data") or []
        if not isinstance(variants, list):
            return None
        for v in variants:
            if not isinstance(v, dict):
                continue
            # Salla returns option_values under a few different keys
            # depending on the API version: `related_options`,
            # `options`, or `option_values`. Accept any of them.
            v_opts = (
                v.get("related_options")
                or v.get("option_values")
                or v.get("options")
                or []
            )
            if not isinstance(v_opts, list):
                continue
            v_map: Dict[str, str] = {}
            for vo in v_opts:
                if not isinstance(vo, dict):
                    continue
                vo_oid = vo.get("option_id") or vo.get("id")
                vo_vid = (
                    vo.get("value_id")
                    or vo.get("option_value_id")
                    or vo.get("value")
                )
                if vo_oid is not None and vo_vid is not None:
                    v_map[str(vo_oid)] = str(vo_vid)
            if v_map and all(v_map.get(k) == val for k, val in wanted.items()):
                vid = str(v.get("id") or "")
                if vid:
                    logger.info(
                        "[SallaAdapter] variant resolved | product=%s variant_id=%s selection=%s",
                        product_id, vid, wanted,
                    )
                    return vid
        logger.info(
            "[SallaAdapter] no matching variant found | product=%s wanted=%s variants=%d",
            product_id, wanted, len(variants),
        )
        return None

    async def _enrich_items_with_variant_id(self, order_input: OrderInput) -> None:
        """Best-effort: attach variant_id to each item with selected options.

        Sending Salla both ``options`` and ``variant_id`` is more reliable
        than options alone — variant_id is unambiguous and removes any
        Salla-side options-resolution edge cases that have been observed
        to return 422 ("خيارات المنتج مطلوبة") even with options present.
        """
        for item in order_input.items or []:
            if item.variant_id or not item.options:
                continue
            try:
                vid = await self._resolve_variant_id(str(item.product_id), item.options)
            except Exception as exc:
                logger.info(
                    "[SallaAdapter] variant enrichment failed (non-blocking) | "
                    "product=%s err=%s",
                    item.product_id, exc,
                )
                continue
            if vid:
                item.variant_id = vid

    async def create_order(self, order_input: OrderInput) -> NormalizedOrder:
        self._require_auth("create_order")
        logger.error(
            "[ORDER FLOW] preparing payload for Salla | tenant=%s product=%s "
            "first_name=%r last_name=%r phone=%s city=%r short_code=%r",
            self._tenant_id,
            (order_input.items[0].product_id if order_input.items else "?"),
            bool(order_input.customer_first_name),
            bool(order_input.customer_last_name),
            bool(order_input.customer_phone),
            order_input.city,
            order_input.short_address_code,
        )
        await self._assert_required_options_present(order_input)
        await self._enrich_items_with_variant_id(order_input)
        shipping_company_id = order_input.shipping_company_id
        if not shipping_company_id:
            shipping_company_id = await self._get_default_shipping_company_id(order_input.city)
        body = self._build_order_body(order_input, draft=False, shipping_company_id=shipping_company_id)
        # Hard validation BEFORE POST — never let Salla 422 us when we
        # could have asked the customer for the missing field instead.
        missing = validate_salla_order_payload(body)
        if missing:
            logger.error(
                "[ORDER FLOW] BLOCKED create_order — payload missing required fields | "
                "tenant=%s missing=%s payload_keys=%s",
                self._tenant_id, missing, sorted(list(body.keys())),
            )
            raise SallaOrderValidationError(
                missing=missing,
                payload_keys=list(body.keys()),
            )
        # ── Hard guard: delivery_method MUST be present before POST ─────────
        body["delivery_method"] = body.get("delivery_method") or "shipping"
        logger.error(
            "[SallaAdapter] DELIVERY METHOD GUARDED BEFORE POST | action=create_order "
            "method=%s tenant=%s top_level_keys=%s",
            body["delivery_method"], self._tenant_id, sorted(list(body.keys())),
        )

        self._log_outgoing_payload("create_order", body, shipping_company_id)
        try:
            data = await self._post("/orders", body)
            order = self._normalize_order(data.get("data", data), order_input)
            self._log_salla_response("create_order", 201, data, order)
            return order
        except httpx.HTTPStatusError as exc:
            self._log_salla_failure("create_order", exc, body)
            raise
        except Exception as exc:
            self._log_error("create_order", exc)
            raise

    async def create_draft_order(self, order_input: OrderInput) -> NormalizedOrder:
        self._require_auth("create_draft_order")
        logger.error(
            "[ORDER FLOW] preparing payload for Salla (draft) | tenant=%s product=%s "
            "first_name=%r last_name=%r phone=%s city=%r short_code=%r",
            self._tenant_id,
            (order_input.items[0].product_id if order_input.items else "?"),
            bool(order_input.customer_first_name),
            bool(order_input.customer_last_name),
            bool(order_input.customer_phone),
            order_input.city,
            order_input.short_address_code,
        )
        await self._assert_required_options_present(order_input)
        await self._enrich_items_with_variant_id(order_input)
        # ── Shipping resolution ───────────────────────────────────────────────────
        # Auto-resolve the default shipping company if not already cached.
        # We never ask the customer for shipping; we just pick Salla's first zone.
        shipping_company_id = order_input.shipping_company_id
        if not shipping_company_id:
            logger.info(
                "[ORDER FLOW] resolving shipping method | tenant=%s city=%r",
                self._tenant_id, order_input.city,
            )
            shipping_company_id = await self._get_default_shipping_company_id(order_input.city)
            if shipping_company_id:
                logger.info(
                    "[ORDER FLOW] selected default shipping method | company_id=%s tenant=%s",
                    shipping_company_id, self._tenant_id,
                )
            else:
                logger.info(
                    "[ORDER FLOW] shipping method unavailable, proceeding without | tenant=%s city=%r",
                    self._tenant_id, order_input.city,
                )
        else:
            logger.info(
                "[ORDER FLOW] using cached shipping method | company_id=%s tenant=%s",
                shipping_company_id, self._tenant_id,
            )

        body = self._build_order_body(order_input, draft=True, shipping_company_id=shipping_company_id)

        # ── HARD VALIDATION before POST ───────────────────────────────────────
        # If anything Salla truly requires is missing, refuse to POST and let
        # the conversation layer ask the customer for the missing slot. This
        # turns silent 422s into actionable "ask for X" turns.
        missing = validate_salla_order_payload(body)
        if missing:
            logger.error(
                "[ORDER FLOW] BLOCKED create_draft_order — payload missing required fields | "
                "tenant=%s missing=%s payload_keys=%s product=%s "
                "city=%r short_code=%r",
                self._tenant_id, missing, sorted(list(body.keys())),
                (order_input.items[0].product_id if order_input.items else "?"),
                order_input.city, order_input.short_address_code,
            )
            raise SallaOrderValidationError(
                missing=missing,
                payload_keys=list(body.keys()),
            )

        # ── Verbose pre-POST log: every field a merchant would want to see ───
        self._log_outgoing_payload("create_draft_order", body, shipping_company_id)

        # ── FULL PAYLOAD LOG (Railway-visible before every POST /orders) ──────
        # This is the canonical diagnostic: copy the exact JSON and paste it
        # into Salla's API explorer to reproduce the rejection locally.
        try:
            import json as _json
            _full_payload_str = _json.dumps(body, ensure_ascii=False)
            logger.error(
                "[SallaAdapter] FINAL OUTGOING PAYLOAD FULL | action=create_draft_order "
                "tenant=%s payload=%s",
                self._tenant_id, _full_payload_str,
            )
        except Exception as _fp_exc:
            logger.warning("[SallaAdapter] FINAL OUTGOING PAYLOAD log failed: %s", _fp_exc)

        # ── Hard guard: delivery_method MUST be present before POST ─────────
        body["delivery_method"] = body.get("delivery_method") or "shipping"
        logger.error(
            "[SallaAdapter] DELIVERY METHOD GUARDED BEFORE POST | action=create_draft_order "
            "method=%s tenant=%s top_level_keys=%s",
            body["delivery_method"], self._tenant_id, sorted(list(body.keys())),
        )

        # ── Assertion: options must be in products[0] when required ──────────
        _first_prod = ((body.get("products") or [{}])[0]) or {}
        _payload_opts = _first_prod.get("options") or []
        if order_input.items and order_input.items[0].options and not _payload_opts:
            logger.error(
                "[SallaAdapter] ASSERTION FAILED: options were in OrderInput but "
                "NOT in final payload products[0] | tenant=%s product=%s "
                "input_options=%s payload_product=%s",
                self._tenant_id,
                order_input.items[0].product_id,
                order_input.items[0].options,
                _first_prod,
            )

        try:
            data = await self._post("/orders", body)
            order = self._normalize_order(data.get("data", data), order_input)

            # ── Structured response log ─────────────────────────────────────
            self._log_salla_response("create_draft_order", 201, data, order)

            # ── Payment URL fallback ──────────────────────────────────────────────
            # Salla does not always embed the payment URL in the create response.
            # If it is missing, make one extra GET /orders/{id} call to fetch it.
            if not order.payment_link and order.id:
                logger.info(
                    "[ORDER FLOW] payment url absent in create response, fetching separately "
                    "| order_id=%s tenant=%s",
                    order.id, self._tenant_id,
                )
                try:
                    fetched_url = await self.generate_payment_link(order.id, order.total)
                    if fetched_url:
                        order.payment_link = fetched_url
                        logger.info(
                            "[ORDER FLOW] payment url fetched via GET /orders | "
                            "order_id=%s url=%s tenant=%s",
                            order.id, fetched_url, self._tenant_id,
                        )
                except Exception as _fetch_exc:
                    logger.warning(
                        "[ORDER FLOW] payment url fetch failed (non-blocking) | "
                        "order_id=%s err=%s tenant=%s",
                        order.id, _fetch_exc, self._tenant_id,
                    )

            return order
        except httpx.HTTPStatusError as exc:
            self._log_salla_failure("create_draft_order", exc, body)
            raise
        except Exception as exc:
            self._log_error("create_draft_order", exc)
            raise

    # ──────────────────────────────────────────────────────────────────────────
    # Structured request / response logging for the create-order pipeline.
    # Centralised here so both create_order() and create_draft_order() emit
    # the EXACT same lines — operators only need to grep for one tag.
    # ──────────────────────────────────────────────────────────────────────────
    def _log_outgoing_payload(
        self,
        action: str,
        body: Dict[str, Any],
        shipping_company_id: Optional[int],
    ) -> None:
        """Emit a single structured log line summarising what we're about to
        POST to Salla. The full body is logged separately at INFO so 12-factor
        log aggregators can index it without polluting the hot path.

        Secrets are not present in order payloads (no tokens, no card data),
        so we log the body verbatim. Customer phone is only the last 4 digits.
        """
        try:
            customer = body.get("customer") or {}
            payment  = body.get("payment") or {}
            shipping = body.get("shipping") or {}
            address  = body.get("address") or {}
            products = body.get("products") or []
            first_p  = (products[0] if products else {}) or {}

            phone_raw = customer.get("mobile") or ""
            phone_masked = (
                f"***{phone_raw[-4:]}" if isinstance(phone_raw, str) and len(phone_raw) >= 4
                else phone_raw
            )

            logger.info(
                "[SallaAdapter] ABOUT_TO_POST_ORDER | action=%s tenant=%s "
                "product_id=%s variant_id=%s qty=%s "
                "options_count=%d payment_methods=%s payment_status=%s "
                "shipping_company_id=%s shipping_keys=%s "
                "city=%r street_set=%s country=%r "
                "customer_first=%r customer_last=%r email_set=%s mobile=%s "
                "draft=%s top_level_keys=%s",
                action, self._tenant_id,
                first_p.get("identifier"), first_p.get("variant_id"),
                first_p.get("quantity"),
                len(first_p.get("options") or []),
                payment.get("accepted_methods"), payment.get("status"),
                shipping_company_id or shipping.get("company_id"),
                sorted(list(shipping.keys())),
                address.get("city"), bool(address.get("street")),
                address.get("country"),
                customer.get("first_name"), customer.get("last_name"),
                bool(customer.get("email")), phone_masked,
                body.get("status") == "under_review",
                sorted(list(body.keys())),
            )
        except Exception as _exc:
            logger.warning("[SallaAdapter] _log_outgoing_payload swallowed: %s", _exc)

    def _log_salla_response(
        self,
        action: str,
        status_code: int,
        raw: Dict[str, Any],
        order: NormalizedOrder,
    ) -> None:
        """Single structured line per successful Salla response."""
        try:
            data = raw.get("data") if isinstance(raw, dict) else {}
            data = data if isinstance(data, dict) else {}
            logger.info(
                "[SallaAdapter] SALLA_RESPONSE_OK | action=%s tenant=%s "
                "status_code=%s salla_order_id=%s reference_id=%s "
                "order_number=%s status=%s total=%s currency=%s "
                "has_payment_link=%s",
                action, self._tenant_id, status_code,
                order.id, order.reference_id,
                data.get("reference_id") or data.get("order_number") or order.reference_id,
                order.status, order.total, order.currency,
                bool(order.payment_link),
            )
        except Exception as _exc:
            logger.warning("[SallaAdapter] _log_salla_response swallowed: %s", _exc)

    def _log_salla_failure(
        self,
        action: str,
        exc: "httpx.HTTPStatusError",
        body: Dict[str, Any],
    ) -> None:
        """Structured failure line — always paired with `[SallaAdapter] _log_error`
        emitted by the existing handler. Designed so a single grep reveals the
        full failure context (status, response, the payload we sent).
        """
        try:
            import json as _json
            status = exc.response.status_code
            text   = (exc.response.text or "")
            try:
                json_body = exc.response.json()
            except Exception:
                json_body = None
            customer = body.get("customer") or {}
            phone = customer.get("mobile") or ""
            phone_masked = f"***{phone[-4:]}" if len(phone) >= 4 else phone
            _first_prod = ((body.get("products") or [{}])[0]) or {}
            logger.error(
                "[SallaAdapter] SALLA_RESPONSE_FAIL | action=%s tenant=%s "
                "status_code=%s "
                "sent_product_id=%s sent_options=%s "
                "sent_payment=%s sent_shipping_company_id=%s "
                "sent_city=%r sent_mobile=%s",
                action, self._tenant_id, status,
                _first_prod.get("identifier") or _first_prod.get("id"),
                _first_prod.get("options"),
                (body.get("payment") or {}).get("accepted_methods"),
                (body.get("shipping") or {}).get("company_id"),
                (body.get("address") or {}).get("city"),
                phone_masked,
            )
            # Log full response body as a separate line for easy copy-paste
            logger.error(
                "[SallaAdapter] SALLA_RESPONSE_BODY | action=%s tenant=%s "
                "status_code=%s body=%s",
                action, self._tenant_id, status, text,
            )
            # Surface the structured 422 body if Salla returned one.
            if isinstance(json_body, dict):
                err = json_body.get("error") or {}
                if isinstance(err, dict):
                    if err.get("fields"):
                        logger.error(
                            "[SallaAdapter] SALLA_422_FIELDS | action=%s tenant=%s "
                            "rejected_fields=%s message=%s",
                            action, self._tenant_id,
                            err.get("fields"), err.get("message"),
                        )
                    elif err.get("message"):
                        logger.error(
                            "[SallaAdapter] SALLA_422_MESSAGE | action=%s tenant=%s "
                            "message=%s",
                            action, self._tenant_id, err.get("message"),
                        )
        except Exception as _exc:
            logger.warning("[SallaAdapter] _log_salla_failure swallowed: %s", _exc)

    async def _get_default_shipping_company_id(self, city: str = "") -> Optional[int]:
        """Return the Salla zone/company ID of the first available shipping option.

        Tries with the customer's city first, falls back to no filter.
        Returns None if no zones are configured or the API call fails.
        """
        for attempt_city in ([city, ""] if city else [""]):
            try:
                params: Dict[str, str] = {}
                if attempt_city:
                    params["city"] = attempt_city
                data = await self._get("/shipping/zones", params)
                zones = data.get("data") or []
                if zones:
                    zone_id = zones[0].get("id")
                    if zone_id is not None:
                        return int(zone_id)
            except Exception as exc:
                logger.warning(
                    "[SallaAdapter] _get_default_shipping_company_id failed | "
                    "city=%r attempt=%r err=%s",
                    city, attempt_city, exc,
                )
        return None

    @staticmethod
    def _normalize_mobile(phone: str) -> str:
        """Normalise to E.164 (+966XXXXXXXXX) — Salla Admin API v2 requires this format.

        Salla's 422 response confirms:
          "رقم الهاتف يجب ان يبدأ بـ + متبوعا برقم الدولة"
          (Phone number must start with + followed by country code)

        WhatsApp gives us either +966XXXXXXXXX, 966XXXXXXXXX, or 0XXXXXXXXX.
        All three must be normalised to +966XXXXXXXXX.

        Examples:
          +966555906901  →  +966555906901  (already correct)
           966555906901  →  +966555906901
           0555906901    →  +966555906901  (Saudi local → E.164)
        """
        raw = (phone or "").strip().replace(" ", "").replace("-", "")
        if raw.startswith("+"):
            return raw                          # already E.164
        if raw.startswith("966") and len(raw) >= 12:
            return f"+{raw}"                   # 966XXXXXXXXX → +966XXXXXXXXX
        if raw.startswith("0") and len(raw) == 10:
            return f"+966{raw[1:]}"            # 0XXXXXXXXX  → +966XXXXXXXXX
        # Fallback: prepend + if it looks like digits
        if raw.isdigit():
            return f"+{raw}"
        return raw

    def _build_order_body(
        self,
        order_input: OrderInput,
        draft: bool,
        shipping_company_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        # ── Products ─────────────────────────────────────────────────────────────
        # Send both `id` (what Salla /orders docs show in examples) AND the
        # `identifier`/`identifier_type` pair (v2 Admin API).  Salla uses
        # whichever it recognises — sending both is safe and avoids 422s when
        # the merchant's plan/version only supports one form.
        products = []
        for item in order_input.items:
            _pid_int: Any
            try:
                _pid_int = int(item.product_id)
            except (TypeError, ValueError):
                _pid_int = item.product_id
            entry: Dict[str, Any] = {
                "id":              _pid_int,          # simple form (newer API)
                "identifier_type": "id",              # v2 Admin API form
                "identifier":      str(_pid_int),
                "quantity":        item.quantity,
            }
            if item.variant_id:
                try:
                    entry["variant_id"] = int(item.variant_id)
                except (TypeError, ValueError):
                    entry["variant_id"] = item.variant_id
            # Salla expects {"id": option_id, "value": value_id} per option
            # group. We accept either explicit value_id (preferred) or fall
            # back to value_name when the merchant uses free-text options.
            if item.options:
                # Build both the array format (`[{id, value}]`) and the
                # dict format (`{option_id: value_id}`). Salla accepts
                # either depending on the API/store config — sending
                # both shapes is safe (Salla ignores the unused one).
                opts_payload: List[Dict[str, Any]] = []
                opts_dict: Dict[str, Any] = {}
                for sel in item.options:
                    if not isinstance(sel, dict):
                        continue
                    _oid = sel.get("option_id") if "option_id" in sel else sel.get("id")
                    _vid = sel.get("value_id") if "value_id" in sel else sel.get("value")
                    if _oid is None:
                        continue
                    _val = _vid if _vid is not None else sel.get("value_name")
                    if _val is None:
                        continue
                    # Salla schema requires value to be an array: {"id": X, "value": [Y]}
                    opts_payload.append({"id": _oid, "value": [_val]})
                    opts_dict[str(_oid)] = _val
                if opts_payload:
                    entry["options"] = opts_payload
                logger.info(
                    "[SallaAdapter] FINAL OPTIONS PAYLOAD | product=%s options=%s",
                    item.product_id, opts_payload,
                )
                logger.info(
                    "[SallaAdapter] item options built | product=%s raw=%s "
                    "array_payload=%s dict_payload=%s variant_id=%s",
                    item.product_id, item.options, opts_payload, opts_dict, item.variant_id,
                )
            else:
                logger.info(
                    "[SallaAdapter] item options EMPTY | product=%s variant_id=%s",
                    item.product_id, item.variant_id,
                )
            products.append(entry)

        # ── Phone — Salla requires E.164 (+966XXXXXXXXX) ────────────────────────
        mobile = self._normalize_mobile(order_input.customer_phone)
        logger.info(
            "[SallaAdapter] phone normalization | raw=%r normalized=%r tenant=%s",
            order_input.customer_phone, mobile, self._tenant_id,
        )

        # ── Customer name ────────────────────────────────────────────────────────
        _first = (order_input.customer_first_name or "").strip()
        _last  = (order_input.customer_last_name  or "").strip()
        if not _first:
            _parts = (order_input.customer_name or "").strip().split()
            _first = _parts[0] if _parts else ""
            if not _last:
                _last = " ".join(_parts[1:]) if len(_parts) > 1 else ""

        # ── Payment — Salla Admin API v2:
        #   `payment.accepted_methods` is REQUIRED by Salla (422 otherwise:
        #   "حقل وسائل الدفع المتاحة مطلوب"). The slugs must be a subset of
        #   the methods the merchant has enabled in Salla. The only slug
        #   that is guaranteed to be enabled on every store is `cod` (cash
        #   on delivery), so that is the safe default. Operators who want
        #   online payment can override via env (comma-separated):
        #     SALLA_DEFAULT_PAYMENT_METHODS=mada,cod,credit_card
        import os as _os
        _methods_env = (_os.environ.get("SALLA_DEFAULT_PAYMENT_METHODS") or "").strip()
        if _methods_env:
            _accepted_methods = [m.strip() for m in _methods_env.split(",") if m.strip()]
        else:
            _accepted_methods = ["cod"]
        payment_block: Dict[str, Any] = {
            "status": "pending_payment",
            "accepted_methods": _accepted_methods,
        }

        body: Dict[str, Any] = {
            "products": products,
            "customer": {
                "first_name": _first or (order_input.customer_name or "عميل"),
                "last_name":  _last,
                "mobile":     mobile,
            },
            "payment": payment_block,
        }
        # ── Customer email ───────────────────────────────────────────────────────
        # Salla does not REQUIRE email for COD orders, but some workflows (auto
        # invoice, abandoned-cart recovery, account creation) silently fail
        # without one. We pass through the merchant-supplied email when
        # available, otherwise generate a stable namespaced placeholder
        # derived from the customer's WhatsApp number so the same person
        # always maps to the same Salla customer record.
        _email = (order_input.customer_email or "").strip()
        if not _email:
            _digits = "".join(ch for ch in (order_input.customer_phone or "") if ch.isdigit())
            if _digits:
                _email = f"wa{_digits[-9:]}@nahlah.local"
        if _email:
            body["customer"]["email"] = _email

        # ── Shipping ─────────────────────────────────────────────────────────────
        # Pass the resolved shipping company/zone ID to Salla.
        # If no ID was resolved, omit the block and let Salla use store defaults.
        _sid = shipping_company_id or order_input.shipping_company_id
        if _sid:
            body["shipping"] = {"company_id": _sid}

        # ── Delivery method — Salla REQUIRES this field ───────────────────────
        # "shipping" for normal delivery, "pickup" for in-store collection.
        # We default to "shipping"; if the merchant wants pickup they must
        # pass delivery_method="pickup" through the OrderInput in the future.
        _delivery_method = getattr(order_input, "delivery_method", None) or "shipping"
        body["delivery_method"] = _delivery_method
        logger.info(
            "[SallaAdapter] DELIVERY METHOD SET | method=%s tenant=%s",
            _delivery_method, self._tenant_id,
        )

        # Build address block — include city and short address code whenever available.
        # ── Address ──────────────────────────────────────────────────────────────
        # Saudi customers typically supply a national short address code (TAPA7401)
        # with a city. Salla rejects the bare alphanumeric code as a street value
        # ("street must be a readable address"), so when no real street is
        # available we synthesise a human-readable fallback such as
        # "الطائف - الرمز الوطني TAPA7401" or "العنوان عبر الرمز الوطني: TAPA7401".
        # If a Google Maps URL was provided, it gets a sensible textual fallback
        # too. The raw code itself is still preserved in the order note for the
        # merchant to see.
        street_val = (order_input.street or order_input.address or "").strip()
        _short_code_clean = (order_input.short_address_code or "").strip().upper()
        _maps_url_clean = (order_input.google_maps_url or "").strip()

        if not street_val and _short_code_clean:
            if order_input.city:
                street_val = f"{order_input.city.strip()} - الرمز الوطني {_short_code_clean}"
            else:
                street_val = f"العنوان عبر الرمز الوطني {_short_code_clean}"
        elif not street_val and _maps_url_clean:
            if order_input.city:
                street_val = f"{order_input.city.strip()} - الموقع عبر خرائط Google"
            else:
                street_val = "الموقع عبر خرائط Google"

        if order_input.city or street_val:
            addr: Dict[str, Any] = {}
            if order_input.city:
                addr["city"] = order_input.city
            if street_val:
                addr["street"] = street_val
            if order_input.building_number:
                addr["building_number"] = order_input.building_number
            if order_input.district:
                addr["district"] = order_input.district
            if order_input.postal_code:
                addr["zip_code"] = order_input.postal_code
            if order_input.additional_number:
                addr["additional_number"] = order_input.additional_number
            # Salla expects a country on shipping address; default to Saudi Arabia.
            addr.setdefault("country", "Saudi Arabia")
            # Region helps Salla resolve a shipping zone when the merchant
            # configured zones per region.  Best-effort mapping from the
            # most common Saudi cities; falls back to the city itself,
            # which is acceptable for Salla.
            addr.setdefault("region", _resolve_saudi_region(order_input.city))
            body["address"] = addr

        # ── Notes (human-readable) ───────────────────────────────────────────────
        notes_parts = []
        if order_input.notes:
            notes_parts.append(order_input.notes)
        if order_input.short_address_code and order_input.short_address_code not in (order_input.notes or ""):
            notes_parts.append(f"العنوان الوطني: {order_input.short_address_code}")
        if order_input.google_maps_url and order_input.google_maps_url not in (order_input.notes or ""):
            notes_parts.append(f"خريطة: {order_input.google_maps_url}")
        if notes_parts:
            body["note"] = " | ".join(notes_parts)   # Salla uses "note" (singular)

        if draft:
            body["status"] = "under_review"   # Salla draft-equivalent status

        logger.info(
            "[SallaAdapter] _build_order_body | product_id=%s city=%s short_code=%s "
            "mobile=%s has_street=%s",
            products[0]["identifier"] if products else "?",
            order_input.city or "",
            order_input.short_address_code or "",
            mobile,
            bool(street_val),
        )

        # ── Final-body diagnostic — fires on every call so we catch any gap ──
        logger.error(
            "[SallaAdapter] FINAL BODY KEYS BEFORE RETURN | keys=%s tenant=%s "
            "delivery_method=%s",
            sorted(list(body.keys())), self._tenant_id,
            body.get("delivery_method"),
        )
        assert "delivery_method" in body, (
            f"[SallaAdapter] delivery_method MISSING before return — tenant={self._tenant_id}"
        )

        return body

    async def get_order(self, order_id: str) -> Optional[NormalizedOrder]:
        try:
            data = await self._get(f"/orders/{order_id}")
            raw = data.get("data")
            return self._normalize_order(raw, None) if raw else None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            self._log_error("get_order", exc)
            raise
        except Exception as exc:
            self._log_error("get_order", exc)
            raise

    async def get_orders(self, updated_since: Optional[str] = None) -> List[NormalizedOrder]:
        extra: Optional[Dict[str, Any]] = None
        if updated_since:
            date_only = str(updated_since).split("T", 1)[0]
            extra = {"from_date": date_only}
        try:
            raw_list = await self._get_all_pages("/orders", label="orders", extra_params=extra)
            return [self._normalize_order(o, None) for o in raw_list]
        except httpx.HTTPStatusError as exc:
            self._log_error("get_orders", exc)
            logger.error(
                "Salla get_orders HTTP error %s: %s",
                exc.response.status_code,
                exc.response.text[:300],
            )
            raise
        except Exception as exc:
            self._log_error("get_orders", exc)
            raise

    async def get_customer_orders(self, customer_phone: str) -> List[NormalizedOrder]:
        try:
            data = await self._get("/orders", {"mobile": customer_phone, "per_page": 10})
            return [self._normalize_order(o, None) for o in data.get("data", [])]
        except Exception as exc:
            self._log_error("get_customer_orders", exc)
            return []

    # ── Abandoned carts ────────────────────────────────────────────────────────
    #
    # Salla's `/orders` endpoint NEVER returns abandoned carts — those live
    # behind the dedicated Merchant API endpoint:
    #
    #     GET https://api.salla.dev/admin/v2/carts/abandoned
    #
    # docs:  https://docs.salla.dev/api-5394138 (List Abandoned Carts)
    # scope: ``carts.read``
    #
    # CRITICAL: an earlier version of this adapter called ``/carts``
    # (without the ``/abandoned`` suffix). That path silently returns a
    # 404 / empty body on Salla, which is then swallowed by the
    # ``except`` blocks below — the symptom on the merchant's screen is
    # that "Salla shows N abandoned carts" while Nahla's dashboard sits
    # on zero forever. The fix below restores the documented path.
    #
    # The Salla response shape is:
    #   { "status": 200, "success": true,
    #     "data": [ { "id": ..., "total": {amount,currency},
    #                 "checkout_url": "...", "customer": {...},
    #                 "items": [...], "created_at": {date,timezone,...} } ],
    #     "pagination": {count,total,perPage,currentPage,totalPages,links} }
    #
    # ``_get_all_pages`` already extracts the ``data`` array and walks
    # pagination — we just need the right URL.
    async def get_abandoned_carts(self) -> List[Dict[str, Any]]:
        """Fetch all abandoned carts from Salla.

        Returns the raw cart dicts (not normalized into NormalizedOrder) so
        the sync layer can preserve cart-specific fields like ``checkout_url``
        and ``items`` exactly as Salla returns them. Never raises — returns
        an empty list on any error so the orders sync pipeline keeps moving.
        """
        try:
            return await self._get_all_pages(
                "/carts/abandoned", label="abandoned_carts",
            )
        except SallaTokenRevokedException:
            raise
        except httpx.HTTPStatusError as exc:
            self._log_error("get_abandoned_carts", exc)
            logger.error(
                "Salla get_abandoned_carts HTTP %s: %s",
                exc.response.status_code, exc.response.text[:300],
            )
            return []
        except Exception as exc:
            self._log_error("get_abandoned_carts", exc)
            return []

    def _normalize_order(self, raw: Dict[str, Any], order_input: Optional[OrderInput]) -> NormalizedOrder:
        amounts = raw.get("amounts") or {}

        # Salla returns `amounts.total` either as `{"amount": 100, "currency": "SAR"}`
        # or as a flat number depending on endpoint. Some endpoints (notably the
        # listing endpoint) put the grand total at `raw["total"]` directly. Fall
        # through to every plausible shape so we never silently store 0.0 for a
        # real order.
        total = 0.0
        currency = "SAR"
        for candidate in (
            amounts.get("total"),
            amounts.get("sub_total"),
            raw.get("total"),
            raw.get("amount"),
            raw.get("price"),
        ):
            if candidate is None:
                continue
            if isinstance(candidate, dict):
                amt = candidate.get("amount") or candidate.get("value") or 0
                cur = candidate.get("currency")
                try:
                    parsed = float(amt or 0)
                except (TypeError, ValueError):
                    parsed = 0.0
                if parsed > 0:
                    total = parsed
                    if cur:
                        currency = str(cur)
                    break
            else:
                try:
                    parsed = float(candidate or 0)
                except (TypeError, ValueError):
                    parsed = 0.0
                if parsed > 0:
                    total = parsed
                    break

        # Salla returns the payment URL under several possible keys depending on
        # endpoint version. Check all known shapes before falling back to None.
        _urls = raw.get("urls") or {}
        payment_link = (
            raw.get("payment_url")
            or raw.get("checkout_url")
            or _urls.get("payment")
            or _urls.get("checkout")
            or _urls.get("pay")
        )

        items = []
        for li in (raw.get("items") or raw.get("line_items") or []):
            price_val = li.get("price")
            unit_price = None
            if isinstance(price_val, dict):
                unit_price = float(price_val.get("amount", 0) or 0)
            items.append(OrderItem(
                product_id=str(li.get("product_id") or li.get("id", "")),
                product_title=li.get("name") or li.get("product_name") or "",
                variant_id=str(li.get("variant_id")) if li.get("variant_id") else None,
                quantity=li.get("quantity", 1),
                unit_price=unit_price,
            ))

        customer = raw.get("customer") or {}
        cname = str(customer.get("name") or (order_input.customer_name if order_input else "") or "")
        cphone = str(customer.get("mobile") or (order_input.customer_phone if order_input else "") or "")

        # CRITICAL: Salla returns `status` as a dict like
        #   {"id": 566146469, "name": "بإنتظار المراجعة", "slug": "under_review",
        #    "customized": {...}}
        # `str(dict)` produced a Python repr (e.g. "{'id': 566146469, ...}") which
        # poisoned every downstream consumer (dashboard, customer classifier,
        # automations). Always extract the canonical slug; fall back to name then
        # to the literal string so unrecognized shapes are still searchable.
        status_raw = raw.get("status")
        if isinstance(status_raw, dict):
            status_str = str(
                status_raw.get("slug")
                or status_raw.get("name")
                or status_raw.get("code")
                or "pending"
            ).strip()
        elif status_raw is None:
            status_str = "pending"
        else:
            status_str = str(status_raw).strip() or "pending"

        # Salla sends `created_at` as either a plain ISO string or
        # `{"date": "2026-04-15 12:00:00.000000", "timezone_type": 3,
        #   "timezone": "Asia/Riyadh"}`. Preserve the inner date string when nested.
        created_raw = raw.get("created_at") or raw.get("date") or ""
        if isinstance(created_raw, dict):
            created_str = str(created_raw.get("date") or "")
        else:
            created_str = str(created_raw or "")

        # Salla returns BOTH `id` (internal numeric primary key) and
        # `reference_id` (the human-visible order number the merchant sees
        # in their Salla dashboard, e.g. 1585297702). We want to keep
        # using `id` for stable upserts but also expose `reference_id`
        # to the dashboard so merchants see the same number Salla shows.
        internal_id = str(raw.get("id") or raw.get("reference_id", "")).strip()
        reference   = str(raw.get("reference_id") or raw.get("id", "")).strip()

        return NormalizedOrder(
            id=internal_id,
            reference_id=reference or internal_id,
            status=status_str,
            total=total,
            currency=currency,
            payment_link=payment_link,
            customer_name=cname,
            customer_phone=cphone,
            items=items,
            created_at=created_str,
            source="salla",
        )

    # ── Payment ────────────────────────────────────────────────────────────────

    async def generate_payment_link(self, order_id: str, amount: float) -> Optional[str]:
        try:
            data = await self._get(f"/orders/{order_id}")
            raw = data.get("data", {})
            return raw.get("payment_url") or raw.get("checkout_url")
        except Exception as exc:
            self._log_error("generate_payment_link", exc)
            return None

    # ── Shipping ───────────────────────────────────────────────────────────────

    async def get_shipping_options(self, city: str = "") -> List[ShippingOption]:
        try:
            params = {"city": city} if city else {}
            data = await self._get("/shipping/zones", params)
            options = []
            for zone in (data.get("data") or []):
                costs = zone.get("costs") or zone.get("prices") or [{}]
                cost_entry = costs[0] if costs else {}
                zone_id = zone.get("id")
                options.append(ShippingOption(
                    name=zone.get("name") or zone.get("courier_name") or "شحن",
                    cost=float(cost_entry.get("amount", 0) or 0),
                    currency=cost_entry.get("currency", "SAR"),
                    estimated_days=str(zone.get("min_days", "")) or None,
                    zone=zone.get("name"),
                    courier=zone.get("courier_name"),
                    company_id=int(zone_id) if zone_id is not None else None,
                ))
            return options
        except Exception as exc:
            self._log_error("get_shipping_options", exc)
            return []

    # ── Customers ──────────────────────────────────────────────────────────────

    async def get_customers(self, updated_since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch all customers from Salla across all pages until exhaustion."""
        try:
            extra: Optional[Dict[str, Any]] = None
            if updated_since:
                extra = {"updated_at_min": updated_since}
            return await self._get_all_pages("/customers", label="customers", extra_params=extra)
        except Exception as exc:
            self._log_error("get_customers", exc)
            return []

    # ── Offers / Coupons ──────────────────────────────────────────────────────

    async def get_coupons(self) -> List[Dict[str, Any]]:
        """Return raw coupon dicts from Salla across all pages until exhaustion."""
        try:
            return await self._get_all_pages("/coupons", label="coupons")
        except Exception as exc:
            self._log_error("get_coupons", exc)
            return []

    async def create_coupon(
        self,
        code: str,
        discount_type: str = "percentage",
        discount_value: int = 10,
        expiry_days: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """Create a coupon in Salla. Returns the created coupon data or None.

        Requires a valid access_token + refresh_token.

        Salla Admin API v2 (verified live, April 2026) expects:
          type   = "percentage" | "fixed"   (lowercase)
          amount = numeric discount value   (single field; no percent_off/amount_off)
        The previous uppercase/split-field shape is rejected with
        422 alert.invalid_fields{type, amount}.
        """
        self._require_auth("create_coupon")
        start_dt = datetime.now(timezone.utc)
        expiry_dt = start_dt + timedelta(days=expiry_days)
        start  = start_dt.strftime("%Y-%m-%d")
        expiry = expiry_dt.strftime("%Y-%m-%d")

        salla_type = "percentage" if discount_type in ("percentage", "PERCENT", "percent") else "fixed"

        payload = {
            "code":                   code,
            "type":                   salla_type,
            "amount":                 int(discount_value),
            "start_date":             start,
            "expiry_date":            expiry,
            "free_shipping":          False,
            "exclude_sale_products":  False,
        }
        try:
            data = await self._post("/coupons", payload)
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data["data"].setdefault("expires_at", expiry_dt.isoformat())
                data["data"].setdefault("expiry_date", expiry)
            elif isinstance(data, dict):
                data.setdefault("expires_at", expiry_dt.isoformat())
            logger.info("Salla coupon created: %s | tenant=%s", code, self._tenant_id)
            return data.get("data", data)
        except httpx.HTTPStatusError as exc:
            self._log_error("create_coupon", exc)
            logger.error(
                "Salla create_coupon HTTP %s: %s",
                exc.response.status_code, exc.response.text[:500],
            )
            return None
        except Exception as exc:
            self._log_error("create_coupon", exc)
            return None

    async def delete_coupon_by_code(self, code: str) -> bool:
        """
        Delete a Salla coupon by its code. Used for compensation when we
        created a coupon in Salla but the local DB insert then failed — we
        must remove the orphan to keep the two sides in sync.

        Returns True if Salla confirms deletion (or the coupon is already
        gone), False on any other failure. Never raises.
        """
        if not code:
            return False
        try:
            data = await self._get("/coupons", {"code": code, "per_page": 1})
            rows = data.get("data") or [] if isinstance(data, dict) else []
            if not rows:
                return True
            target = rows[0]
            coupon_id = target.get("id") if isinstance(target, dict) else None
            if not coupon_id:
                return False
            return await self._delete(f"/coupons/{coupon_id}")
        except Exception as exc:
            self._log_error("delete_coupon_by_code", exc)
            return False

    async def get_active_offers(self) -> List[NormalizedOffer]:
        try:
            data = await self._get("/coupons", {"status": "active", "per_page": 20})
            return [self._normalize_coupon(c) for c in (data.get("data") or [])]
        except Exception as exc:
            self._log_error("get_active_offers", exc)
            return []

    async def validate_coupon(self, code: str) -> Optional[NormalizedOffer]:
        try:
            data = await self._get("/coupons", {"code": code})
            results = data.get("data") or []
            for c in results:
                if c.get("code") == code:
                    offer = self._normalize_coupon(c)
                    return offer if offer.valid else None
            return None
        except Exception as exc:
            self._log_error("validate_coupon", exc)
            return None

    def _normalize_coupon(self, raw: Dict[str, Any]) -> NormalizedOffer:
        coupon_type = "percentage" if raw.get("percent") else "fixed"
        value = float(raw.get("percent") or raw.get("amount") or 0)
        expires_raw = raw.get("expire_date")
        valid = raw.get("status", "active") == "active"
        if expires_raw:
            try:
                exp = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
                if exp < datetime.now(timezone.utc).replace(tzinfo=exp.tzinfo):
                    valid = False
            except Exception:
                pass
        return NormalizedOffer(
            code=raw.get("code"),
            type=coupon_type,
            value=value,
            min_order=float(raw.get("minimum_order_amount") or 0) or None,
            expires_at=str(expires_raw) if expires_raw else None,
            description=raw.get("description"),
            valid=valid,
        )
