"""
core/customer_identity_resolver.py
──────────────────────────────────
Evidence-based customer identity: source, status, confidence, and strict
usage gates for operational documents (orders, invoices, shipping).

Canonical metadata keys on ``Customer.extra_metadata``:

  customer_name_source
  customer_name_status
  customer_name_confidence
  customer_name_updated_at
  proposed_name            — WhatsApp profile hint when not official

Legacy ``name_source`` is kept in sync for existing callers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from core.customer_name_validator import validate_customer_name

logger = logging.getLogger("nahla.customer_identity_resolver")

# ── Status values ─────────────────────────────────────────────────────────────
STATUS_VERIFIED = "verified"
STATUS_CUSTOMER_ENTERED = "customer_entered_validated"
STATUS_PROPOSED = "proposed"
STATUS_MISSING = "missing"
STATUS_REJECTED = "rejected"

OFFICIAL_STATUSES = frozenset({STATUS_VERIFIED, STATUS_CUSTOMER_ENTERED})

# ── Source values ─────────────────────────────────────────────────────────────
SOURCE_SALLA_ORDER = "salla_order"
SOURCE_ZID_ORDER = "zid_order"
SOURCE_SHOPIFY_ORDER = "shopify_order"
SOURCE_WHATSAPP_PROFILE = "whatsapp_profile"
SOURCE_CUSTOMER_MESSAGE = "customer_message"
SOURCE_MERCHANT = "merchant_correction"
SOURCE_MANUAL_ADMIN = "manual_admin"

# Keys written by apply_customer_name / manual PATCH — must survive CIS metadata merges.
IDENTITY_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "customer_name_source",
        "customer_name_status",
        "customer_name_confidence",
        "customer_name_updated_at",
        "proposed_name",
        "name_source",
        "customer_name_rejected_reason",
        "manual_name_override",
        "manual_name_cleared",
        "manual_name_edited_at",
        "manual_name_previous",
        "manual_name_source",
    }
)

_SOURCE_TRUST: Dict[str, int] = {
    SOURCE_SALLA_ORDER: 100,
    SOURCE_ZID_ORDER: 100,
    SOURCE_SHOPIFY_ORDER: 100,
    SOURCE_MERCHANT: 90,
    SOURCE_MANUAL_ADMIN: 95,
    SOURCE_CUSTOMER_MESSAGE: 80,
    SOURCE_WHATSAPP_PROFILE: 10,
    # Legacy aliases mapped at runtime
    "salla": 100,
    "salla_sync": 100,
    "customer_webhook": 100,
    "zid": 100,
    "zid_sync": 100,
    "shopify": 100,
    "shopify_sync": 100,
    "commerce_platform": 100,
    "sales_channel": 100,
    "platform_verified": 100,
    "order_webhook": 95,
    "order_sync": 95,
    "order_incremental": 95,
    "order": 95,
    "ai_detected_name": 80,
    "merchant_correction": 90,
    "merchant_manual": 95,
    "manual": 85,
    "manual_import": 40,
    "whatsapp_inbound": 10,
    "whatsapp_lead": 10,
    "widget": 10,
}

_LEGACY_SOURCE_MAP: Dict[str, Tuple[str, str, float]] = {
    "salla": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "salla_sync": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "customer_webhook": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "zid": (SOURCE_ZID_ORDER, STATUS_VERIFIED, 1.0),
    "zid_sync": (SOURCE_ZID_ORDER, STATUS_VERIFIED, 1.0),
    "shopify": (SOURCE_SHOPIFY_ORDER, STATUS_VERIFIED, 1.0),
    "shopify_sync": (SOURCE_SHOPIFY_ORDER, STATUS_VERIFIED, 1.0),
    "commerce_platform": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "sales_channel": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "platform_verified": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "order_webhook": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "order_sync": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "order_incremental": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "order": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "whatsapp_inbound": (SOURCE_WHATSAPP_PROFILE, STATUS_PROPOSED, 0.4),
    "whatsapp_lead": (SOURCE_WHATSAPP_PROFILE, STATUS_PROPOSED, 0.4),
    "ai_detected_name": (SOURCE_CUSTOMER_MESSAGE, STATUS_CUSTOMER_ENTERED, 0.85),
    "merchant_correction": (SOURCE_MERCHANT, STATUS_CUSTOMER_ENTERED, 0.95),
    "merchant_manual": (SOURCE_MERCHANT, STATUS_CUSTOMER_ENTERED, 0.95),
    "manual": (SOURCE_MERCHANT, STATUS_CUSTOMER_ENTERED, 0.9),
    "manual_admin": (SOURCE_MANUAL_ADMIN, STATUS_CUSTOMER_ENTERED, 0.98),
}


@dataclass(frozen=True)
class CustomerIdentitySnapshot:
    customer_name: str
    customer_name_source: str
    customer_name_status: str
    customer_name_confidence: float
    customer_name_updated_at: Optional[str]
    proposed_name: str = ""
    display_name: str = ""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta(customer: Any) -> Dict[str, Any]:
    return dict(getattr(customer, "extra_metadata", None) or {})


def _trust(source: Optional[str]) -> int:
    return _SOURCE_TRUST.get(source or "", 0)


def normalize_identity_source(
    source: Optional[str],
    *,
    platform: Optional[str] = None,
    explicit_customer_entry: bool = False,
) -> Tuple[str, str, float]:
    """Map caller source (+ optional store platform) → (source, status, confidence)."""
    src = (source or "").strip().lower()
    if explicit_customer_entry:
        return SOURCE_CUSTOMER_MESSAGE, STATUS_CUSTOMER_ENTERED, 0.85

    if src in _LEGACY_SOURCE_MAP:
        canon, status, conf = _LEGACY_SOURCE_MAP[src]
        if src in {"order_webhook", "order_sync", "order"} and platform:
            plat = platform.strip().lower()
            if plat == "zid":
                return SOURCE_ZID_ORDER, STATUS_VERIFIED, 1.0
            if plat == "shopify":
                return SOURCE_SHOPIFY_ORDER, STATUS_VERIFIED, 1.0
        return canon, status, conf

    if src == SOURCE_WHATSAPP_PROFILE:
        return SOURCE_WHATSAPP_PROFILE, STATUS_PROPOSED, 0.4
    if src in {SOURCE_SALLA_ORDER, SOURCE_ZID_ORDER, SOURCE_SHOPIFY_ORDER}:
        return src, STATUS_VERIFIED, 1.0
    if src == SOURCE_CUSTOMER_MESSAGE:
        return SOURCE_CUSTOMER_MESSAGE, STATUS_CUSTOMER_ENTERED, 0.85
    if src == SOURCE_MERCHANT:
        return SOURCE_MERCHANT, STATUS_CUSTOMER_ENTERED, 0.95
    if src == SOURCE_MANUAL_ADMIN:
        return SOURCE_MANUAL_ADMIN, STATUS_CUSTOMER_ENTERED, 0.98

    return src or SOURCE_WHATSAPP_PROFILE, STATUS_PROPOSED, 0.3


def is_official_name_status(status: Optional[str]) -> bool:
    return (status or "").strip().lower() in OFFICIAL_STATUSES


def read_customer_identity(customer: Any) -> CustomerIdentitySnapshot:
    """Read identity fields from a Customer row."""
    meta = _meta(customer)
    name = str(getattr(customer, "name", None) or "").strip()
    source = str(
        meta.get("customer_name_source")
        or meta.get("name_source")
        or getattr(customer, "acquisition_channel", "")
        or ""
    ).strip()
    status = str(meta.get("customer_name_status") or "").strip()
    proposed = str(meta.get("proposed_name") or "").strip()
    updated_at = meta.get("customer_name_updated_at")
    try:
        confidence = float(meta.get("customer_name_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if not status:
        if name:
            _, status, confidence = normalize_identity_source(source)
        elif proposed:
            status = STATUS_PROPOSED
        else:
            status = STATUS_MISSING

    manual_cleared = bool(meta.get("manual_name_cleared"))
    if manual_cleared and not name:
        # Merchant intentionally wiped the name — never resurrect a
        # WhatsApp-profile ``proposed_name`` ghost in the table.
        display = ""
    elif is_official_name_status(status) and name:
        display = name
    else:
        display = proposed or name
    return CustomerIdentitySnapshot(
        customer_name=name,
        customer_name_source=source,
        customer_name_status=status,
        customer_name_confidence=confidence,
        customer_name_updated_at=str(updated_at) if updated_at else None,
        proposed_name=proposed,
        display_name=display,
    )


def display_name_for_customer(customer: Any, *, phone_fallback: str = "") -> str:
    from core.customer_display import is_valid_customer_display_name  # noqa: PLC0415

    snap = read_customer_identity(customer)
    if snap.display_name and is_valid_customer_display_name(snap.display_name):
        return snap.display_name
    return phone_fallback


def merge_identity_metadata(
    target: Dict[str, Any],
    customer: Any,
) -> Dict[str, Any]:
    """Preserve resolver + manual-override keys when CIS merges inbound metadata."""
    src = _meta(customer)
    for key in IDENTITY_METADATA_KEYS:
        if key in src:
            target[key] = src[key]
    return target


def is_manual_name_locked(customer: Any) -> bool:
    meta = _meta(customer)
    if not bool(meta.get("manual_name_override")):
        return False
    if bool(meta.get("manual_name_cleared")):
        return False
    return bool(str(getattr(customer, "name", None) or "").strip())


def can_use_name_for_operations(customer: Any) -> bool:
    snap = read_customer_identity(customer)
    return is_official_name_status(snap.customer_name_status) and bool(snap.customer_name)


def _should_overwrite(
    *,
    existing_status: str,
    existing_source: str,
    new_status: str,
    new_source: str,
) -> bool:
    if not is_official_name_status(existing_status):
        return True
    if is_official_name_status(new_status) and _trust(new_source) >= _trust(existing_source):
        return True
    return False


def apply_customer_name(
    customer: Any,
    raw_name: Optional[str],
    *,
    source: Optional[str],
    platform: Optional[str] = None,
    explicit_customer_entry: bool = False,
    force_merchant: bool = False,
    message_context: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Validate and persist a customer name with provenance.

    Returns True when ``Customer.name`` or identity metadata changed.
    """
    if customer is None:
        return False

    meta = _meta(customer)
    validation = validate_customer_name(raw_name)
    canon_source, canon_status, base_conf = normalize_identity_source(
        source,
        platform=platform,
        explicit_customer_entry=explicit_customer_entry,
    )

    if force_merchant:
        src_norm = (source or "").strip().lower()
        if src_norm == SOURCE_MANUAL_ADMIN:
            canon_source = SOURCE_MANUAL_ADMIN
            base_conf = 0.98
        else:
            canon_source = SOURCE_MERCHANT
            base_conf = 0.95
        canon_status = STATUS_CUSTOMER_ENTERED

    existing_snap = read_customer_identity(customer)
    existing_status = existing_snap.customer_name_status
    existing_source = existing_snap.customer_name_source or str(
        meta.get("name_source") or getattr(customer, "acquisition_channel", "") or ""
    )

    override_flag = bool(meta.get("manual_name_override"))
    cleared_flag = bool(meta.get("manual_name_cleared"))
    current_name = str(getattr(customer, "name", None) or "").strip()

    if not validation.valid:
        if canon_source == SOURCE_CUSTOMER_MESSAGE and not force_merchant:
            logger.info(
                "[CUSTOMER_IDENTITY] blocked ai candidate name=%r reason=%s",
                str(raw_name or "")[:60],
                validation.reason,
            )
            return False
        if raw_name and str(raw_name).strip():
            logger.info(
                "[CUSTOMER_IDENTITY] rejected name=%r source=%s reason=%s",
                str(raw_name)[:60],
                source,
                validation.reason,
            )
            meta["customer_name_rejected_reason"] = validation.reason
            meta["customer_name_status"] = STATUS_REJECTED
            meta["customer_name_updated_at"] = _utcnow_iso()
            customer.extra_metadata = meta
            return True
        return False

    cleaned = validation.cleaned
    confidence = max(base_conf, validation.confidence)
    if canon_status == STATUS_PROPOSED:
        if override_flag and not force_merchant:
            logger.debug(
                "[CUSTOMER_IDENTITY] blocked proposed by manual_name_override id=%s",
                getattr(customer, "id", None),
            )
            return False
        meta["proposed_name"] = cleaned
        meta["customer_name_source"] = canon_source
        meta["customer_name_status"] = STATUS_PROPOSED
        meta["customer_name_confidence"] = confidence
        meta["customer_name_updated_at"] = _utcnow_iso()
        meta["name_source"] = source or canon_source
        customer.extra_metadata = meta
        logger.info(
            "[CUSTOMER_IDENTITY] proposed only name=%r source=%s",
            cleaned,
            canon_source,
        )
        return True

    # Official path
    if override_flag and current_name and not force_merchant:
        logger.debug(
            "[CUSTOMER_IDENTITY] blocked by manual_name_override id=%s",
            getattr(customer, "id", None),
        )
        return False

    if (
        override_flag
        and cleared_flag
        and not current_name
        and canon_status != STATUS_CUSTOMER_ENTERED
        and _trust(canon_source) < _trust(SOURCE_CUSTOMER_MESSAGE)
    ):
        return False

    if canon_source == SOURCE_CUSTOMER_MESSAGE and not force_merchant:
        from core.customer_name_adoption_guard import can_ai_update_customer_name  # noqa: PLC0415

        policy_context = {
            **dict(message_context or {}),
            "source": source or canon_source,
            "explicit_customer_entry": explicit_customer_entry,
        }
        if not can_ai_update_customer_name(customer, cleaned, policy_context):
            logger.info(
                "[CUSTOMER_IDENTITY] blocked by ai name policy id=%s source=%s",
                getattr(customer, "id", None),
                source or canon_source,
            )
            return False

    if current_name and not _should_overwrite(
        existing_status=existing_status,
        existing_source=existing_source,
        new_status=canon_status,
        new_source=canon_source,
    ):
        logger.debug(
            "[CUSTOMER_IDENTITY] blocked overwrite existing=%s/%s new=%s/%s",
            existing_status,
            existing_source,
            canon_status,
            canon_source,
        )
        return False

    customer.name = cleaned
    meta["customer_name_source"] = canon_source
    meta["customer_name_status"] = canon_status
    meta["customer_name_confidence"] = confidence
    meta["customer_name_updated_at"] = _utcnow_iso()
    meta["name_source"] = source or canon_source
    meta.pop("customer_name_rejected_reason", None)
    if force_merchant:
        meta["manual_name_override"] = True
        meta["manual_name_cleared"] = False
        meta["manual_name_source"] = source or canon_source
    if cleared_flag and canon_status == STATUS_CUSTOMER_ENTERED:
        meta["manual_name_cleared"] = False
    customer.extra_metadata = meta
    logger.info(
        "[CUSTOMER_IDENTITY] applied name=%r status=%s source=%s conf=%.2f",
        cleaned,
        canon_status,
        canon_source,
        confidence,
    )
    return True


def official_name_from_prep_and_customer(
    prep: Any,
    customer: Any,
    *,
    fallback: str = "",
) -> str:
    """
    Name safe for order / invoice / shipping payloads.

    Uses order-prep explicit customer statement first, then verified
    customer row — never proposed WhatsApp profile aliases.
    """
    first = str(getattr(prep, "customer_first_name", "") or "").strip()
    last = str(getattr(prep, "customer_last_name", "") or "").strip()
    prep_name = " ".join(p for p in (first, last) if p).strip()
    prov = dict(getattr(prep, "identity_provenance", None) or {})
    prep_provenance = prov.get("customer_name") or prov.get("recipient_name")

    if prep_name:
        v = validate_customer_name(prep_name)
        if v.valid and prep_provenance in {
            "explicit_customer_statement",
            "confirmation_yes",
        }:
            return v.cleaned

    if customer is not None and can_use_name_for_operations(customer):
        snap = read_customer_identity(customer)
        if snap.customer_name:
            return snap.customer_name

    fb = str(fallback or "").strip()
    if fb and validate_customer_name(fb).valid and can_use_name_for_operations(customer):
        return fb
    return ""
