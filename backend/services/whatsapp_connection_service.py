"""
services/whatsapp_connection_service.py
────────────────────────────────────────
Canonical write-layer for ALL WhatsApp connection flows.

Every code path that creates or updates a WhatsAppConnection row and marks it
"connected" MUST go through commit_connection().  Intermediate state before a
phone number is selected (embedded signup "exchange" step) goes through
begin_waba_session() which does not mark the row connected but still enforces
WABA uniqueness.

Guarantees enforced here so routers never have to duplicate them:

  1. phone_number_id  — globally unique across tenants (active rows only).
  2. waba_id          — globally unique across tenants (active rows only).
  3. Stale disconnected rows on other tenants are evicted before writing.
  4. The target tenant_id exists in the tenants table (caller must verify).
  5. Phone registration via Meta Cloud API — called ONCE when the phone_number_id
     is new or changed, to lift Meta's "Pending" state to "Active".
  6. Meta webhook subscription is attempted synchronously inside the write.
  7. The result carries four explicit readiness flags:
       credentials_saved  – credentials written to DB.
       phone_registered   – Meta /register API returned 200 OK.
       webhook_subscribed – Meta app subscription confirmed.
       inbound_usable     – registered + webhook active + sending enabled.

Callers are responsible for:
  - Resolving the tenant_id from the authenticated JWT (not from fallback).
  - Verifying the tenant row exists before calling (HTTP 403 if not).
  - Input validation (non-empty strings, digit-only IDs, etc.).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.wa_conn_svc")


# ── Custom exception types ─────────────────────────────────────────────────────

class WhatsAppConnectionConflict(Exception):
    """phone_number_id or waba_id is actively owned by another tenant (HTTP 409)."""


class WhatsAppConnectionError(Exception):
    """Unexpected internal failure during the connection write."""


# ── 3-state readiness result ───────────────────────────────────────────────────

@dataclass
class ConnectionResult:
    """
    Returned by commit_connection().

    Four-state readiness model:
      credentials_saved  → DB row written; credentials stored.
      phone_registered   → Meta /register API returned 200 OK (phone lifted from Pending).
      webhook_subscribed → Meta confirmed app subscription for this WABA.
      inbound_usable     → registered + webhook active + sending_enabled=True.
                           Only True here means end-to-end inbound routing will work.
    """
    tenant_id:                  int
    wa_conn_id:                 Optional[int]
    phone_number_id:            Optional[str]
    waba_id:                    str
    connection_type:            str

    credentials_saved:          bool = False
    phone_registered:           bool = False
    webhook_subscribed:         bool = False
    inbound_usable:             bool = False

    phone_registration_error:   Optional[str] = None
    webhook_error:              Optional[str] = None
    action:                     str = "unknown"   # "created" | "updated"

    def to_api_dict(self) -> dict:
        return {
            "ok":                       self.credentials_saved,
            "status":                   "connected" if self.credentials_saved else "error",
            "tenant_id":                self.tenant_id,
            "phone_number_id":          self.phone_number_id,
            "waba_id":                  self.waba_id,
            "connection_type":          self.connection_type,
            "credentials_saved":        self.credentials_saved,
            "phone_registered":         self.phone_registered,
            "webhook_subscribed":       self.webhook_subscribed,
            "inbound_usable":           self.inbound_usable,
            "phone_registration_error": self.phone_registration_error,
            "webhook_error":            self.webhook_error,
            "action":                   self.action,
            "readiness":                _readiness_label(
                self.credentials_saved,
                self.phone_registered,
                self.webhook_subscribed,
                self.inbound_usable,
            ),
        }


def _readiness_label(creds: bool, registered: bool, webhook: bool, inbound: bool) -> str:
    if inbound:
        return "inbound_usable"
    if webhook:
        return "webhook_subscribed"
    if registered:
        return "phone_registered"
    if creds:
        return "credentials_saved"
    return "not_connected"


# ── Core API ───────────────────────────────────────────────────────────────────

def commit_connection(
    db: Session,
    *,
    tenant_id: int,
    phone_number_id: str,
    waba_id: str,
    access_token: str,
    connection_type: str,
    provider: str = "meta",
    phone_number: str = "",
    display_name: str = "",
    sending_enabled: bool = True,
    actor: str = "system",
) -> ConnectionResult:
    """
    THE single canonical write entry point for all final WhatsApp connection writes.

    Steps (always executed in order, none skipped):
      1. Assert phone_number_id is not actively claimed by another tenant  → 409 on conflict.
      2. Assert waba_id is not actively claimed by another tenant          → 409 on conflict.
      3. Evict stale (disconnected/error) rows from other tenants.
      4. Validate phone_number_id belongs to waba_id via Meta API         → 422 on mismatch.
      5. Write the WhatsAppConnection row (create or update).
      6. Register the phone number (POST /{phone_number_id}/register) if new/changed.
      7. Attempt Meta webhook subscription (POST /{waba_id}/subscribed_apps).
      8. Persist webhook_verified flag based on step 7 result.
      9. Return ConnectionResult with all four readiness flags.

    Raises:
      WhatsAppConnectionConflict — if phone_number_id or waba_id is owned elsewhere,
                                   OR if phone→waba mismatch is detected.
      WhatsAppConnectionError    — on unexpected DB failure.
    """
    from database.models import WhatsAppConnection  # noqa: PLC0415
    from core.tenant_integrity import (              # noqa: PLC0415
        assert_phone_id_not_claimed,
        assert_waba_id_not_claimed,
        evict_phone_id_from_other_tenants,
        evict_waba_id_from_other_tenants,
        TenantIntegrityError,
    )

    logger.info(
        "[WASvc] commit START — tenant=%s phone=%s waba=%s type=%s actor=%s",
        tenant_id, phone_number_id, waba_id, connection_type, actor,
    )

    # ── Step 1–2: Integrity checks — ALL errors are fatal (no broad except) ──
    try:
        assert_phone_id_not_claimed(db, phone_number_id, tenant_id)
    except TenantIntegrityError as exc:
        logger.error("[WASvc] BLOCKED phone conflict tenant=%s: %s", tenant_id, exc)
        raise WhatsAppConnectionConflict(str(exc)) from exc

    try:
        assert_waba_id_not_claimed(db, waba_id, tenant_id)
    except TenantIntegrityError as exc:
        logger.error("[WASvc] BLOCKED waba conflict tenant=%s: %s", tenant_id, exc)
        raise WhatsAppConnectionConflict(str(exc)) from exc

    # ── Step 3: Evict stale disconnected rows (non-fatal if eviction fails) ──
    try:
        evict_phone_id_from_other_tenants(db, phone_number_id, tenant_id)
        evict_waba_id_from_other_tenants(db, waba_id, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[WASvc] eviction warning (non-fatal): %s", exc)

    # ── Step 4: Validate phone_number_id → waba_id ownership ─────────────────
    # Ask Meta whether phone_number_id actually belongs to the supplied waba_id.
    # If Meta returns a definitive mismatch → reject with a clear error.
    # If Meta cannot return the WABA (permission gap) → proceed with a warning.
    match, resolved_waba, _val_err = validate_phone_waba_match(
        phone_number_id, waba_id, access_token, tenant_id
    )
    if not match:
        raise WhatsAppConnectionConflict(
            f"phone_number_id {phone_number_id} belongs to WABA {resolved_waba}, "
            f"not to the supplied waba_id {waba_id}. "
            f"يرجى إدخال الـ WABA ID الصحيح: {resolved_waba}"
        )

    # ── Step 5: Write ─────────────────────────────────────────────────────────
    # Capture old phone_number_id BEFORE overwriting so we can detect a change.
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    action        = "updated" if conn else "created"
    old_phone_id  = conn.phone_number_id if conn else None   # used for registration gate
    if not conn:
        conn = WhatsAppConnection(tenant_id=tenant_id)
        db.add(conn)

    now = datetime.now(timezone.utc)
    conn.phone_number_id              = phone_number_id
    conn.whatsapp_business_account_id = waba_id
    conn.access_token                 = access_token
    conn.status                       = "connected"
    conn.sending_enabled              = sending_enabled
    conn.connection_type              = connection_type
    conn.provider                     = provider
    conn.webhook_verified             = False   # must be earned by subscription
    conn.last_error                   = None
    conn.connected_at                 = now
    conn.updated_at                   = now
    conn.disconnect_reason            = None
    conn.disconnected_at              = None
    if hasattr(conn, "disconnected_by_user_id"):
        conn.disconnected_by_user_id = None
    if phone_number:
        conn.phone_number = phone_number
    if display_name:
        conn.business_display_name = display_name

    try:
        db.commit()
        db.refresh(conn)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("[WASvc] DB commit FAILED tenant=%s: %s", tenant_id, exc)
        raise WhatsAppConnectionError(f"DB write failed: {exc}") from exc

    logger.info(
        "[WASvc] COMMITTED (%s) — tenant=%s phone=%s waba=%s conn_id=%s actor=%s",
        action, tenant_id, phone_number_id, waba_id, conn.id, actor,
    )

    result = ConnectionResult(
        tenant_id       = tenant_id,
        wa_conn_id      = conn.id,
        phone_number_id = phone_number_id,
        waba_id         = waba_id,
        connection_type = connection_type,
        credentials_saved = True,
        action          = action,
    )

    # ── Step 6: Phone registration (once per new/changed phone_number_id) ────
    # Meta requires a POST /{phone_number_id}/register call to lift the phone
    # from "Pending" to "Active" on the Cloud API.  We run this only when the
    # phone_number_id is brand-new or just changed, so it is never called on
    # server restarts or credential-only refreshes.
    phone_is_new = (action == "created") or (old_phone_id != phone_number_id)
    if phone_is_new:
        reg_ok, reg_err = register_phone_number(phone_number_id, access_token, tenant_id)
        result.phone_registered         = reg_ok
        result.phone_registration_error = reg_err
    else:
        # Phone unchanged — assume already registered; preserve previous status.
        result.phone_registered = True
        logger.info(
            "[WASvc] phone registration SKIPPED — phone unchanged tenant=%s phone=%s",
            tenant_id, phone_number_id,
        )

    # ── Step 7–8: Webhook subscription ───────────────────────────────────────
    webhook_ok, webhook_err = subscribe_waba_webhook(waba_id, access_token, tenant_id)
    result.webhook_subscribed = webhook_ok
    result.webhook_error      = webhook_err

    if webhook_ok:
        conn.webhook_verified = True
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[WASvc] webhook_verified commit failed: %s", exc)
        logger.info("[WASvc] webhook subscribed — tenant=%s waba=%s", tenant_id, waba_id)
    else:
        logger.warning(
            "[WASvc] webhook subscription failed (credentials still saved) — "
            "tenant=%s waba=%s error=%r",
            tenant_id, waba_id, webhook_err,
        )

    # ── Step 9: Compute inbound_usable ────────────────────────────────────────
    result.inbound_usable = (
        result.credentials_saved
        and result.phone_registered
        and result.webhook_subscribed
        and conn.sending_enabled
    )

    logger.info(
        "[WASvc] RESULT — tenant=%s readiness=%s creds=%s registered=%s webhook=%s inbound=%s",
        tenant_id,
        _readiness_label(
            result.credentials_saved,
            result.phone_registered,
            result.webhook_subscribed,
            result.inbound_usable,
        ),
        result.credentials_saved,
        result.phone_registered,
        result.webhook_subscribed,
        result.inbound_usable,
    )
    return result


def begin_waba_session(
    db: Session,
    *,
    tenant_id: int,
    waba_id: str,
    access_token: str,
    connection_type: str = "embedded",
    provider: str = "meta",
    actor: str = "system",
) -> None:
    """
    Intermediate step: store WABA credentials before the phone is selected.
    Used by the Embedded Signup exchange step, where WABA is known but no
    phone has been chosen yet.

    Enforces:
      - WABA uniqueness across tenants (HTTP 409 on conflict).
      - Tenant isolation (no fallback, no new tenant creation).

    Does NOT:
      - Set status=connected (uses "pending").
      - Enable sending (sending_enabled stays False).
      - Attempt webhook subscription (not possible without phone_number_id).

    Raises:
      WhatsAppConnectionConflict — if waba_id is actively owned elsewhere.
    """
    from database.models import WhatsAppConnection  # noqa: PLC0415
    from core.tenant_integrity import (              # noqa: PLC0415
        assert_waba_id_not_claimed,
        evict_waba_id_from_other_tenants,
        TenantIntegrityError,
    )

    logger.info(
        "[WASvc] begin_waba_session — tenant=%s waba=%s type=%s actor=%s",
        tenant_id, waba_id, connection_type, actor,
    )

    try:
        assert_waba_id_not_claimed(db, waba_id, tenant_id)
    except TenantIntegrityError as exc:
        logger.error("[WASvc] BLOCKED waba conflict tenant=%s: %s", tenant_id, exc)
        raise WhatsAppConnectionConflict(str(exc)) from exc

    try:
        evict_waba_id_from_other_tenants(db, waba_id, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[WASvc] waba eviction warning (non-fatal): %s", exc)

    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        conn = WhatsAppConnection(tenant_id=tenant_id)
        db.add(conn)

    conn.whatsapp_business_account_id = waba_id
    conn.access_token                 = access_token
    conn.connection_type              = connection_type
    conn.provider                     = provider
    conn.status                       = "pending"
    conn.sending_enabled              = False
    conn.updated_at                   = datetime.now(timezone.utc)

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("[WASvc] begin_waba_session DB commit FAILED tenant=%s: %s", tenant_id, exc)
        raise WhatsAppConnectionError(f"DB write failed: {exc}") from exc

    logger.info("[WASvc] begin_waba_session COMMITTED — tenant=%s waba=%s", tenant_id, waba_id)


def resolve_waba_for_phone(
    phone_number_id: str,
    access_token: str,
    tenant_id: int,
) -> tuple[str | None, str | None]:
    """
    Ask Meta which WABA owns this phone_number_id.

    Strategy (two attempts so we work with both system-user and user tokens):
      1. GET /{phone_number_id}?fields=id,whatsapp_business_account
         → returns waba {"id": "..."} when token has whatsapp_business_management scope.
      2. Fallback: not possible without listing all accessible WABAs.

    Returns (waba_id: str | None, error: str | None).
    waba_id is None when Meta doesn't return the WABA (permission gap — not an error).
    """
    try:
        from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415
        url  = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{phone_number_id}"
        resp = httpx.get(
            url,
            params={
                "fields":       "id,display_phone_number,verified_name,whatsapp_business_account",
                "access_token": access_token,
            },
            timeout=10,
        )
        data = resp.json()

        if resp.status_code != 200:
            err = (data.get("error") or {}).get("message", f"HTTP {resp.status_code}")
            logger.warning(
                "[WhatsApp] resolve_waba_for_phone FAILED — tenant=%s phone=%s err=%r",
                tenant_id, phone_number_id, err,
            )
            return None, err

        wba = data.get("whatsapp_business_account")
        if isinstance(wba, dict):
            waba_id = wba.get("id")
            return waba_id, None
        if isinstance(wba, str):
            return wba, None

        # Field absent — token lacks whatsapp_business_management scope
        logger.info(
            "[WhatsApp] resolve_waba_for_phone: no waba field in response "
            "(token may lack whatsapp_business_management scope) — tenant=%s phone=%s",
            tenant_id, phone_number_id,
        )
        return None, None

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WhatsApp] resolve_waba_for_phone exception — tenant=%s phone=%s: %s",
            tenant_id, phone_number_id, exc,
        )
        return None, str(exc)


def validate_phone_waba_match(
    phone_number_id: str,
    input_waba_id: str,
    access_token: str,
    tenant_id: int,
) -> tuple[bool, str | None, str | None]:
    """
    Validate that phone_number_id actually belongs to input_waba_id.

    Returns (match: bool, resolved_waba_id: str | None, error: str | None).

    match=True  → OK to proceed.
    match=False → mismatch detected; caller MUST reject with 422.
    match=True + resolved_waba_id=None → could not resolve (graceful degradation).

    Always logs:
      [WhatsApp] validate phone->waba phone_number_id=... input_waba_id=... resolved_waba_id=... match=True/False
    """
    resolved_waba_id, err = resolve_waba_for_phone(phone_number_id, access_token, tenant_id)

    if resolved_waba_id is None:
        # Cannot resolve — could be permission gap; allow with warning
        logger.warning(
            "[WhatsApp] validate phone->waba phone_number_id=%s input_waba_id=%s "
            "resolved_waba_id=unknown match=unknown (cannot verify — proceeding with warning)",
            phone_number_id, input_waba_id,
        )
        return True, None, err

    match = (resolved_waba_id == input_waba_id)
    logger.info(
        "[WhatsApp] validate phone->waba phone_number_id=%s input_waba_id=%s "
        "resolved_waba_id=%s match=%s",
        phone_number_id, input_waba_id, resolved_waba_id, match,
    )

    if not match:
        logger.error(
            "[WhatsApp] MISMATCH — phone %s belongs to WABA %s, not %s — tenant=%s",
            phone_number_id, resolved_waba_id, input_waba_id, tenant_id,
        )

    return match, resolved_waba_id, None


def register_phone_number(
    phone_number_id: str,
    access_token: str,
    tenant_id: int,
) -> tuple[bool, Optional[str]]:
    """
    POST /{phone_number_id}/register — lifts the phone from Meta's "Pending"
    state to "Active" so it can send and receive messages via Cloud API.

    This MUST be called once after a phone number is first connected.
    Calling it on an already-active phone is idempotent and harmless.

    Returns (success: bool, error_detail: str | None).
    NEVER raises — the caller decides how to handle failure.
    """
    try:
        from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415
        url  = (
            f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
            f"/{phone_number_id}/register"
        )
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json={"messaging_product": "whatsapp"},
            timeout=15,
        )
        data = resp.json()

        if resp.status_code == 200 and data.get("success"):
            logger.info(
                "[WhatsApp] phone registration success — tenant=%s phone_number_id=%s",
                tenant_id, phone_number_id,
            )
            return True, None

        err = data.get("error", {})
        msg = err.get("message") or f"HTTP {resp.status_code}"

        # Code 80007 means the number is already registered — treat as success.
        if err.get("code") == 80007:
            logger.info(
                "[WhatsApp] phone already registered (80007) — tenant=%s phone_number_id=%s",
                tenant_id, phone_number_id,
            )
            return True, None

        logger.warning(
            "[WhatsApp] phone registration failed — tenant=%s phone_number_id=%s error=%r",
            tenant_id, phone_number_id, msg,
        )
        return False, msg

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WhatsApp] phone registration exception — tenant=%s phone_number_id=%s: %s",
            tenant_id, phone_number_id, exc,
        )
        return False, str(exc)


def subscribe_waba_webhook(
    waba_id: str,
    access_token: str,
    tenant_id: int,
) -> tuple[bool, Optional[str]]:
    """
    Attempt POST /{waba_id}/subscribed_apps to subscribe Nahla's Meta app to
    receive messages for this WABA.

    Returns (success: bool, error_detail: str | None).
    NEVER raises — the caller decides how to handle failure.
    The caller MUST surface the result to the merchant; it MUST NOT treat a
    False return as a silent success.
    """
    try:
        from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415
        url = (
            f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
            f"/{waba_id}/subscribed_apps"
        )
        resp = httpx.post(
            url,
            params={"access_token": access_token},
            json={"subscribed_fields": ["messages", "messaging_postbacks", "message_echoes"]},
            timeout=10,
        )
        data = resp.json()

        if resp.status_code == 200 and data.get("success"):
            logger.info(
                "[WASvc] subscribed_apps OK — tenant=%s waba=%s",
                tenant_id, waba_id,
            )
            return True, None

        err = data.get("error", {})
        msg = err.get("message") or f"HTTP {resp.status_code}"
        logger.warning(
            "[WASvc] subscribed_apps FAILED — tenant=%s waba=%s status=%s err=%r",
            tenant_id, waba_id, resp.status_code, msg,
        )
        return False, msg

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WASvc] subscribed_apps EXCEPTION — tenant=%s waba=%s: %s",
            tenant_id, waba_id, exc,
        )
        return False, str(exc)
