"""
services/whatsapp_connection_service.py
────────────────────────────────────────
Canonical write-layer for ALL WhatsApp connection flows.

Credential writes go through commit_connection(). That write is not
canonical successful-connected truth. Ordinary Meta Cloud API readiness
(register + webhook + inbound_usable) must be proven first, then
finalize_successful_whatsapp_connection() owns status=connected.

Intermediate state before a phone number is selected (embedded signup
"exchange" step) goes through begin_waba_session() which does not mark the
row connected but still enforces WABA uniqueness.

Guarantees enforced here so routers never have to duplicate them:

  1. phone_number_id  — globally unique across tenants (active rows only).
  2. waba_id          — globally unique across tenants (active rows only).
  3. Stale disconnected rows on other tenants are evicted before writing.
  4. The target tenant_id exists in the tenants table (caller must verify).
  5. Phone registration via Meta Cloud API — /register is the ordinary-path
     proof that the current phone identity is active. Unchanged phone id is
     not proof. The call is idempotent (including Meta 80007 already-registered).
  6. Meta webhook subscription is attempted synchronously inside the write.
  7. The result carries four explicit readiness flags:
       credentials_saved  – credentials written to DB.
       phone_registered   – this attempt proved Cloud /register, or Cloud
                            /register is not required for the provider mode.
       webhook_subscribed – Meta app subscription confirmed.
       inbound_usable     – ordinary Meta Cloud path is fully ready.

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
            "status":                   "connected" if self.inbound_usable else (
                "pending" if self.credentials_saved else "error"
            ),
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
    skip_phone_register: bool = False,
    subscribed_fields: Optional[list] = None,
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
      9. If inbound_usable, call the canonical successful-connection finalizer.
     10. Return ConnectionResult with all four readiness flags.

    Raises:
      WhatsAppConnectionConflict — if phone_number_id or waba_id is owned elsewhere,
                                   OR if phone→waba mismatch is detected.
      WhatsAppConnectionError    — on unexpected DB failure or canonical finalization failure.
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
    # Capture previous identity BEFORE overwriting so a replacement cannot
    # inherit the old row's connected truth.
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    action        = "updated" if conn else "created"
    old_phone_id  = conn.phone_number_id if conn else None
    old_waba_id   = getattr(conn, "whatsapp_business_account_id", None) if conn else None
    old_provider  = getattr(conn, "provider", None) if conn else None
    old_conn_type = getattr(conn, "connection_type", None) if conn else None
    if not conn:
        conn = WhatsAppConnection(tenant_id=tenant_id)
        db.add(conn)

    now = datetime.now(timezone.utc)
    same_identity = (
        action == "updated"
        and (old_phone_id or "") == (phone_number_id or "")
        and (old_waba_id or "") == (waba_id or "")
        and (old_provider or "meta") == (provider or "meta")
        and (old_conn_type or "") == (connection_type or "")
    )
    already_successfully_connected = (
        same_identity
        and str(getattr(conn, "status", "") or "") == "connected"
        and getattr(conn, "connected_at", None) is not None
    )
    conn.phone_number_id              = phone_number_id
    conn.whatsapp_business_account_id = waba_id
    conn.connection_type              = connection_type
    conn.provider                     = provider
    if not already_successfully_connected:
        conn.status                   = "pending"
    conn.webhook_verified             = False   # must be earned by subscription
    conn.last_error                   = None
    conn.updated_at                   = now
    conn.disconnect_reason            = None
    conn.disconnected_at              = None

    # ── Token validation + encrypted persistence (Meta only) ─────────────────
    from services.whatsapp_platform.wa_connection_secrets import store_access_token  # noqa: PLC0415
    effective_sending = sending_enabled
    if provider == "meta":
        from services.whatsapp_platform.wa_token_validation import (  # noqa: PLC0415
            apply_validation_to_connection,
            production_sending_allowed,
            validate_meta_access_token_sync,
        )
        validation = validate_meta_access_token_sync(access_token)
        apply_validation_to_connection(conn, validation)
        if not validation.is_valid:
            raise WhatsAppConnectionError(
                validation.error_message or "Meta access token is invalid."
            )
        if not production_sending_allowed(validation):
            effective_sending = False
            conn.last_error = (
                "Token saved but not production-ready: "
                + "; ".join(validation.warnings or ["Use a permanent System User token."])
            )[:500]
            logger.warning(
                "[WASvc] non-production token tenant=%s status=%s warnings=%s",
                tenant_id, validation.token_status, validation.warnings,
            )
        if validation.token_source_label == "system_user" and validation.token_status == "valid":
            conn.token_type = "permanent_system_user"
        elif validation.expires_at:
            conn.token_type = "long_lived"
    store_access_token(conn, access_token)
    conn.sending_enabled = effective_sending
    if hasattr(conn, "disconnected_by_user_id"):
        conn.disconnected_by_user_id = None
    if phone_number:
        conn.phone_number = phone_number
    if display_name:
        conn.business_display_name = display_name

    # ── Auto-fill display fields from Meta if the caller did not supply them ──
    # Why this lives BEFORE the credential commit:
    #   We want the persisted row to never reach `status="connected"` while
    #   `phone_number` / `business_display_name` are NULL — that combination
    #   is the "half-bootstrapped" bug observed on tenant=1 (see
    #   docs/runbooks/whatsapp-half-bootstrap-rca.md). Canonical connected
    #   is minted later by finalize_successful_whatsapp_connection().
    # Why it is best-effort:
    #   Meta is allowed to be slow or rate-limited, and we never want a
    #   transient Graph hiccup to block a successful credential write.
    #   `fetch_phone_metadata` swallows exceptions and returns all-None on
    #   failure; the row will then persist with whatever the caller passed
    #   (possibly NULL) and the backfill script can repair it later.
    if not conn.phone_number or not conn.business_display_name:
        meta_lookup = fetch_phone_metadata(phone_number_id, access_token, tenant_id)
        if not conn.phone_number and meta_lookup.get("display_phone_number"):
            conn.phone_number = meta_lookup["display_phone_number"]
        if not conn.business_display_name and meta_lookup.get("verified_name"):
            conn.business_display_name = meta_lookup["verified_name"]
        if not conn.phone_number or not conn.business_display_name:
            logger.warning(
                "[WASvc] half-bootstrapped row — tenant=%s phone_id=%s "
                "phone_number=%r display_name=%r (Meta lookup did not return them) — "
                "row will be written but display fields stay NULL until backfilled",
                tenant_id, phone_number_id,
                conn.phone_number, conn.business_display_name,
            )

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

    # ── Step 6: Phone registration ───────────────────────────────────────────
    # Ordinary Meta Cloud API: /register is idempotent and is the authoritative
    # proof for THIS attempt. Unchanged phone_number_id is not proof.
    # Coexistence / skip_phone_register: Cloud /register is not required and
    # does not make commit_connection the successful-readiness owner.
    from services.meta_coexistence import coexistence_webhook_fields, is_coexistence_mode  # noqa: PLC0415

    cloud_register_not_required = skip_phone_register or is_coexistence_mode(conn)
    if cloud_register_not_required:
        result.phone_registered = True
        logger.info(
            "[WASvc] Cloud /register not required — coexistence/skip tenant=%s phone=%s",
            tenant_id, phone_number_id,
        )
    else:
        reg_ok, reg_err = register_phone_number(phone_number_id, access_token, tenant_id)
        result.phone_registered         = reg_ok
        result.phone_registration_error = reg_err
        if not reg_ok:
            conn.last_error = (reg_err or "phone register failed")[:500]

    # ── Step 7–8: Webhook subscription ───────────────────────────────────────
    # Per Meta Cloud API docs: subscription happens on the PHONE_NUMBER_ID,
    # not the WABA_ID. The WABA endpoint returns "Unsupported post request"
    # for many tokens. We pass waba_id only as a defensive fallback.
    if subscribed_fields is None and is_coexistence_mode(conn):
        subscribed_fields = coexistence_webhook_fields()
    webhook_ok, webhook_err = subscribe_phone_webhook(
        phone_number_id,
        access_token,
        tenant_id,
        waba_id=waba_id,
        subscribed_fields=subscribed_fields,
    )
    result.webhook_subscribed = webhook_ok
    result.webhook_error      = webhook_err

    if webhook_ok:
        conn.webhook_verified = True
        logger.info(
            "[WASvc] webhook subscribed — tenant=%s phone=%s waba=%s",
            tenant_id, phone_number_id, waba_id,
        )
    else:
        logger.warning(
            "[WASvc] webhook subscription failed (credentials still saved) — "
            "tenant=%s phone=%s waba=%s error=%r",
            tenant_id, phone_number_id, waba_id, webhook_err,
        )
        if not conn.last_error:
            conn.last_error = (webhook_err or "webhook subscription failed")[:500]

    # ── Step 9: Compute inbound_usable then canonical successful finalization ─
    # Coexistence credential persistence may skip Cloud /register, but that is
    # not ordinary Meta readiness. Successful Coexistence finalization stays
    # with the Coexistence provider path (eligibility, webhook, SMB).
    result.inbound_usable = (
        (not cloud_register_not_required)
        and result.credentials_saved
        and result.phone_registered
        and result.webhook_subscribed
        and bool(conn.sending_enabled)
    )

    if result.inbound_usable:
        from core.whatsapp_connection_finalization import (  # noqa: PLC0415
            WhatsAppConnectionFinalizationError,
            finalize_successful_whatsapp_connection,
        )
        try:
            finalize_successful_whatsapp_connection(db, conn)
        except WhatsAppConnectionFinalizationError as exc:
            logger.error(
                "[WASvc] canonical finalization failed tenant=%s: %s", tenant_id, exc,
            )
            raise WhatsAppConnectionError(
                f"WhatsApp connection finalization failed: {exc}"
            ) from exc
    else:
        if not already_successfully_connected:
            conn.status = "pending"
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[WASvc] pending-readiness commit failed: %s", exc)

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

    prior_waba = str(conn.whatsapp_business_account_id or "")
    conn.whatsapp_business_account_id = waba_id
    from services.whatsapp_platform.wa_connection_secrets import store_access_token  # noqa: PLC0415
    store_access_token(conn, access_token)
    conn.connection_type              = connection_type
    conn.provider                     = provider
    conn.status                       = "pending"
    conn.sending_enabled              = False
    conn.webhook_verified             = False
    if prior_waba and prior_waba != str(waba_id or ""):
        conn.phone_number_id = None
        conn.phone_number = None
        from services.meta_coexistence import invalidate_identity_scoped_proof  # noqa: PLC0415
        invalidate_identity_scoped_proof(conn)
    conn.updated_at                   = datetime.now(timezone.utc)

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("[WASvc] begin_waba_session DB commit FAILED tenant=%s: %s", tenant_id, exc)
        raise WhatsAppConnectionError(f"DB write failed: {exc}") from exc

    logger.info("[WASvc] begin_waba_session COMMITTED — tenant=%s waba=%s", tenant_id, waba_id)


def fetch_phone_metadata(
    phone_number_id: str,
    access_token: str,
    tenant_id: int | None = None,
) -> dict[str, str | None]:
    """
    Look up the public display fields for a Cloud API phone number.

    Returns a dict with `display_phone_number`, `verified_name`, and
    `whatsapp_business_account_id`. Any field that Meta does not return
    (or that is missing because the token lacks scope) becomes `None`.
    Network / Graph errors are logged and the call returns an all-None
    dict — callers MUST treat the result as best-effort, never as a
    blocking dependency for marking the connection `connected`.

    This helper is intentionally side-effect free so it can be used by:
      • commit_connection() to populate the row right after the write.
      • scripts/backfill_whatsapp_phone_metadata.py to repair existing rows.
    """
    out: dict[str, str | None] = {
        "display_phone_number":         None,
        "verified_name":                None,
        "whatsapp_business_account_id": None,
    }
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
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            err = (data.get("error") or {}).get("message", f"HTTP {resp.status_code}")
            logger.warning(
                "[WhatsApp] fetch_phone_metadata FAILED — tenant=%s phone=%s err=%r",
                tenant_id, phone_number_id, err,
            )
            return out

        out["display_phone_number"] = (data.get("display_phone_number") or None)
        out["verified_name"]        = (data.get("verified_name") or None)
        wba = data.get("whatsapp_business_account")
        if isinstance(wba, dict):
            out["whatsapp_business_account_id"] = wba.get("id") or None
        elif isinstance(wba, str):
            out["whatsapp_business_account_id"] = wba
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WhatsApp] fetch_phone_metadata exception — tenant=%s phone=%s: %s",
            tenant_id, phone_number_id, exc,
        )
        return out


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


def subscribe_phone_webhook(
    phone_number_id: str,
    access_token: str,
    tenant_id: int,
    *,
    waba_id: Optional[str] = None,
    subscribed_fields: Optional[list] = None,
    prefer_waba: bool = False,
) -> tuple[bool, Optional[str]]:
    """
    Subscribe Nahla's Meta app to receive webhooks for a WhatsApp asset.

    Standard Embedded / Cloud API completion prefers
    ``POST /{WABA_ID}/subscribed_apps`` (same order as Guardian).
    Direct / legacy callers keep phone-first with a narrow WABA fallback.
    """
    if not phone_number_id and not waba_id:
        return False, "phone_number_id (or waba_id) is required for subscription"

    try:
        from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415

        fields = list(subscribed_fields) if subscribed_fields else [
            "messages", "messaging_postbacks", "message_echoes",
        ]
        attempts: list[tuple[str, str]] = []
        if prefer_waba and waba_id:
            attempts.append(("waba", waba_id))
            if phone_number_id:
                attempts.append(("phone", phone_number_id))
        else:
            if phone_number_id:
                attempts.append(("phone", phone_number_id))
            elif waba_id:
                attempts.append(("waba", waba_id))

        last_msg: Optional[str] = None
        for target_kind, target_id in attempts:
            url = (
                f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
                f"/{target_id}/subscribed_apps"
            )
            resp = httpx.post(
                url,
                params={"access_token": access_token},
                json={"subscribed_fields": fields},
                timeout=10,
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("success"):
                logger.info(
                    "[WASvc] subscribed_apps OK — tenant=%s %s_id=%s",
                    tenant_id, target_kind, target_id,
                )
                return True, None

            err = data.get("error", {})
            last_msg = err.get("message") or f"HTTP {resp.status_code}"
            logger.warning(
                "[WASvc] subscribed_apps FAILED — tenant=%s %s_id=%s status=%s err=%r",
                tenant_id, target_kind, target_id, resp.status_code, last_msg,
            )
            if (
                not prefer_waba
                and target_kind == "phone"
                and waba_id
                and resp.status_code == 400
                and "unsupported" in str(last_msg).lower()
            ):
                logger.info(
                    "[WASvc] phone-level subscribe rejected — retrying WABA-level "
                    "tenant=%s waba=%s",
                    tenant_id, waba_id,
                )
                fallback_url = (
                    f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
                    f"/{waba_id}/subscribed_apps"
                )
                fb_resp = httpx.post(
                    fallback_url,
                    params={"access_token": access_token},
                    json={"subscribed_fields": fields},
                    timeout=10,
                )
                fb_data = fb_resp.json()
                if fb_resp.status_code == 200 and fb_data.get("success"):
                    logger.info(
                        "[WASvc] subscribed_apps OK via WABA fallback — tenant=%s waba=%s",
                        tenant_id, waba_id,
                    )
                    return True, None

        return False, last_msg

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WASvc] subscribed_apps EXCEPTION — tenant=%s phone=%s waba=%s: %s",
            tenant_id, phone_number_id, waba_id, exc,
        )
        return False, str(exc)


# Backwards-compat alias so older imports keep working until callers migrate.
# New code should call subscribe_phone_webhook(...) and pass phone_number_id.
def subscribe_waba_webhook(
    waba_id: str,
    access_token: str,
    tenant_id: int,
    *,
    phone_number_id: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    return subscribe_phone_webhook(
        phone_number_id or "",
        access_token,
        tenant_id,
        waba_id=waba_id,
    )
