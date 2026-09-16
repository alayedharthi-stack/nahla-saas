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
        # ── Authority layer (core.customer_name_authority) ───────────
        # Mirror of customer_name_provenance. Must survive CIS merges
        # for the same reason the keys above must.
        "customer_name_authority",
        "proposed_name_classification",
        "customer_name_evidence_kind",
        "customer_name_evidence_ref",
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
    "order_webhook": 95,
    "order_sync": 95,
    "order_incremental": 95,
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
    "order_webhook": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "order_sync": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
    "order_incremental": (SOURCE_SALLA_ORDER, STATUS_VERIFIED, 1.0),
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
        if src in {"order_webhook", "order_sync", "order_incremental"} and platform:
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


def _resolve_display_name(
    *,
    name: str,
    proposed: str,
    status: str,
    manual_cleared: bool,
    proposed_classification: str = "",
) -> str:
    from core.customer_display import is_valid_customer_display_name  # noqa: PLC0415

    if manual_cleared and not name:
        return ""
    if name and is_valid_customer_display_name(name):
        return name
    # A WhatsApp profile hint may only surface as a display name when
    # the classifier called it a person name. NOT_PERSON_NAME and
    # AMBIGUOUS hints are retained for merchant review but never shown
    # — this is what keeps "الحمد لله" off invoices and shipping labels.
    if proposed and _proposed_is_displayable(proposed, proposed_classification):
        return proposed
    return name or ""


def _proposed_is_displayable(proposed: str, classification: str) -> bool:
    """True only for profile hints classified PERSON_NAME."""
    from core.customer_name_authority import (  # noqa: PLC0415
        PERSON_NAME,
        classify_whatsapp_profile_name,
    )

    stored = str(classification or "").strip().upper()
    if stored:
        return stored == PERSON_NAME
    # Legacy hint written before the classifier existed — classify now
    # instead of trusting it.
    return classify_whatsapp_profile_name(proposed).is_person_name


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
    display = _resolve_display_name(
        name=name,
        proposed=proposed,
        status=status,
        manual_cleared=manual_cleared,
        proposed_classification=str(
            meta.get("proposed_name_classification") or ""
        ),
    )
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

    Automatic sources (store sync, WhatsApp profile, customer message)
    are gated by ONE authority engine:

        normalize source → validate → classify / evidence
        → current canonical authority → resolve_canonical_customer_name
        → apply ONLY that decision → persist provenance

    ``force_merchant`` is the orthogonal merchant lock path and bypasses
    the ladder by design (dashboard inline edit, order correction).

    Returns True when ``Customer.name`` or identity metadata changed.
    """
    if customer is None:
        return False

    meta = _meta(customer)
    canon_source, canon_status, base_conf = normalize_identity_source(
        source,
        platform=platform,
        explicit_customer_entry=explicit_customer_entry,
    )
    current_name = str(getattr(customer, "name", None) or "").strip()

    # ── Merchant lock path (unchanged semantics) ──────────────────────
    if force_merchant:
        src_norm = (source or "").strip().lower()
        if src_norm == SOURCE_MANUAL_ADMIN:
            canon_source, base_conf = SOURCE_MANUAL_ADMIN, 0.98
        else:
            canon_source, base_conf = SOURCE_MERCHANT, 0.95
        canon_status = STATUS_CUSTOMER_ENTERED
        if not (raw_name and str(raw_name).strip()):
            return False
        from core.customer_name_validator import normalize_merchant_manual_name  # noqa: PLC0415

        mval = normalize_merchant_manual_name(raw_name)
        if not mval.valid:
            logger.info(
                "[CUSTOMER_IDENTITY] rejected merchant name=%r source=%s reason=%s",
                str(raw_name)[:60], source, mval.reason,
            )
            return False
        cleaned = mval.cleaned
        customer.name = cleaned
        meta["customer_name_source"] = canon_source
        meta["customer_name_status"] = canon_status
        meta["customer_name_confidence"] = base_conf
        meta["customer_name_updated_at"] = _utcnow_iso()
        meta["name_source"] = source or canon_source
        meta.pop("customer_name_rejected_reason", None)
        meta["manual_name_override"] = True
        meta["manual_name_cleared"] = False
        meta["manual_name_source"] = source or canon_source
        from core.customer_name_authority import MERCHANT_OVERRIDE_LABEL  # noqa: PLC0415

        meta["customer_name_authority"] = MERCHANT_OVERRIDE_LABEL
        meta.pop("customer_name_evidence_kind", None)
        meta.pop("customer_name_evidence_ref", None)
        customer.extra_metadata = meta
        logger.info(
            "[CUSTOMER_IDENTITY] applied merchant name=%r status=%s source=%s",
            cleaned, canon_status, canon_source,
        )
        try:
            from core.customer_name_provenance import record_merchant_name_write  # noqa: PLC0415

            record_merchant_name_write(
                customer, cleaned, source=source or canon_source, previous_name=current_name,
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — audit mirror must never break the merchant write
            logger.debug("[CUSTOMER_IDENTITY] provenance mirror failed", exc_info=True)
        return True

    # ── Automatic sources: the authority engine ───────────────────────
    from core.customer_name_authority import (  # noqa: PLC0415
        DECISION_APPLIED,
        DECISION_BLOCKED_CLASS,
        DECISION_BLOCKED_EVIDENCE,
        DECISION_BLOCKED_VALIDATION,
        DECISION_HINT_ONLY,
        NOT_PERSON_NAME,
        PERSON_NAME,
        NameAuthority,
        NameAuthorityDecision,
        authority_for_canonical_source,
        classify_whatsapp_profile_name,
        evaluate_self_report_evidence,
        resolve_canonical_customer_name,
    )
    from core.customer_name_provenance import read_name_authority  # noqa: PLC0415

    incoming_authority = authority_for_canonical_source(canon_source)
    current_authority = read_name_authority(customer)
    override_flag = bool(meta.get("manual_name_override"))
    cleared_flag = bool(meta.get("manual_name_cleared"))
    merchant_locked = is_manual_name_locked(customer)
    merchant_cleared = override_flag and cleared_flag and not current_name

    def _attempt(code: str, reason: str, *, classification: str = "") -> NameAuthorityDecision:
        return NameAuthorityDecision(
            code,
            canonical_name=current_name,
            authority=current_authority,
            previous_name=current_name,
            previous_authority=current_authority,
            classification=classification,
            reason=reason,
            incoming_name=str(raw_name or "").strip()[:80],
            incoming_authority=incoming_authority,
        )

    validation = validate_customer_name(raw_name)
    if not validation.valid:
        if raw_name and str(raw_name).strip():
            logger.info(
                "[CUSTOMER_IDENTITY] rejected name=%r source=%s reason=%s",
                str(raw_name)[:60], source, validation.reason,
            )
            _record_authority_decision(
                customer, _attempt(DECISION_BLOCKED_VALIDATION, validation.reason), source=source,
            )
            # A stored name is never demoted by an invalid hint; a
            # nameless row records the rejection for the dashboard.
            if not current_name and incoming_authority != NameAuthority.CUSTOMER_SELF_REPORTED:
                meta["customer_name_rejected_reason"] = validation.reason
                meta["customer_name_status"] = STATUS_REJECTED
                meta["customer_name_updated_at"] = _utcnow_iso()
                customer.extra_metadata = meta
                return True
        return False

    cleaned = validation.cleaned
    confidence = max(base_conf, validation.confidence)

    # ── Classify a WhatsApp profile string ────────────────────────────
    classification = ""
    if incoming_authority == NameAuthority.WHATSAPP_PROFILE:
        verdict = classify_whatsapp_profile_name(raw_name)
        classification = verdict.classification
        cleaned = verdict.cleaned or cleaned
        if classification == NOT_PERSON_NAME:
            logger.info(
                "[CUSTOMER_IDENTITY] profile hint rejected name=%r reason=%s",
                str(raw_name or "")[:60], verdict.reason,
            )
            _record_authority_decision(
                customer,
                _attempt(DECISION_BLOCKED_CLASS, verdict.reason, classification=classification),
                source=source,
            )
            return False

    # ── Self-reported names need explicit evidence ────────────────────
    evidence_kind = ""
    evidence_ref: Dict[str, Any] = {}
    if incoming_authority == NameAuthority.CUSTOMER_SELF_REPORTED:
        evidence = evaluate_self_report_evidence(cleaned, message_context)
        if not evidence.accepted:
            logger.info(
                "[CUSTOMER_IDENTITY] self-report rejected name=%r reason=%s",
                str(raw_name or "")[:60], evidence.reason,
            )
            _record_authority_decision(
                customer, _attempt(DECISION_BLOCKED_EVIDENCE, evidence.reason), source=source,
            )
            return False
        evidence_kind = evidence.kind
        evidence_ref = dict(evidence.detail or {})

    # ── THE decision ──────────────────────────────────────────────────
    decision = resolve_canonical_customer_name(
        incoming_name=cleaned,
        incoming_authority=incoming_authority,
        current_name=current_name,
        current_authority=current_authority,
        merchant_locked=merchant_locked,
        merchant_cleared=merchant_cleared,
        classification=classification,
        evidence_kind=evidence_kind,
    )

    if decision.decision == DECISION_APPLIED:
        customer.name = decision.canonical_name
        # A WhatsApp-authority name is canonical/display identity but
        # stays STATUS_PROPOSED: never official for shipping/invoices.
        status = STATUS_PROPOSED if decision.authority == NameAuthority.WHATSAPP_PROFILE else canon_status
        meta["customer_name_source"] = canon_source
        meta["customer_name_status"] = status
        meta["customer_name_confidence"] = confidence
        meta["customer_name_updated_at"] = _utcnow_iso()
        meta["name_source"] = source or canon_source
        meta["customer_name_authority"] = decision.authority.label
        meta.pop("customer_name_rejected_reason", None)
        if decision.authority == NameAuthority.WHATSAPP_PROFILE:
            meta["proposed_name"] = decision.canonical_name
            meta["proposed_name_classification"] = PERSON_NAME
            meta.pop("customer_name_evidence_kind", None)
            meta.pop("customer_name_evidence_ref", None)
        else:
            meta.pop("proposed_name", None)
            meta.pop("proposed_name_classification", None)
            if evidence_kind:
                meta["customer_name_evidence_kind"] = evidence_kind
                meta["customer_name_evidence_ref"] = evidence_ref or None
            else:
                meta.pop("customer_name_evidence_kind", None)
                meta.pop("customer_name_evidence_ref", None)
        if cleared_flag and status == STATUS_CUSTOMER_ENTERED:
            meta["manual_name_cleared"] = False
        customer.extra_metadata = meta
        logger.info(
            "[CUSTOMER_IDENTITY] applied name=%r status=%s source=%s authority=%s conf=%.2f",
            decision.canonical_name, status, canon_source, decision.authority.label, confidence,
        )
        _record_authority_decision(
            customer, decision, source=source, evidence_ref=evidence_ref,
            merchant_locked=merchant_locked,
        )
        return True

    if decision.decision == DECISION_HINT_ONLY:
        # AMBIGUOUS profile hint: retained for merchant review only.
        # Canonical name / status / source / authority are untouched.
        meta["proposed_name"] = cleaned
        meta["proposed_name_classification"] = classification or ""
        meta["customer_name_updated_at"] = _utcnow_iso()
        customer.extra_metadata = meta
        logger.info(
            "[CUSTOMER_IDENTITY] profile hint retained name=%r class=%s",
            cleaned, classification or "unclassified",
        )
        _record_authority_decision(customer, decision, source=source, profile_hint=cleaned)
        return True

    logger.info(
        "[CUSTOMER_IDENTITY] blocked decision=%s reason=%s existing=%s/%s incoming=%s",
        decision.decision, decision.reason, current_authority.label,
        meta.get("customer_name_status"), incoming_authority.label,
    )
    _record_authority_decision(customer, decision, source=source)
    return False


def _record_authority_decision(
    customer: Any,
    decision: Any,
    *,
    source: Optional[str],
    evidence_ref: Optional[Dict[str, Any]] = None,
    profile_hint: str = "",
    merchant_locked: bool = False,
) -> None:
    """Mirror a resolver decision into durable provenance. Never raises."""
    try:
        from core.customer_name_provenance import record_name_decision  # noqa: PLC0415

        record_name_decision(
            customer, decision, source=source, evidence_ref=evidence_ref,
            profile_hint=profile_hint, merchant_locked=merchant_locked,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — audit mirror must never break ingestion or store sync
        logger.debug("[CUSTOMER_IDENTITY] provenance mirror failed", exc_info=True)


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
