"""
modules/ai/brain/postprocess/shipment_truth_guard.py
────────────────────────────────────────────────────
Block false shipment-completed wording when structured shipment
evidence is missing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from modules.ai.brain.postprocess.shipment_evidence import (
    ShipmentEvidenceResult,
    evaluate_shipment_evidence,
)

logger = logging.getLogger("nahla.brain.postprocess.shipment_truth_guard")

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")

# Legacy export — kept for callers/tests; guard no longer injects this template.
SAFE_PRE_SHIPMENT_REPLY_AR = (
    "طلبك تحت المراجعة/التجهيز، وبنبلغك برابط التتبع أول ما يصدر 🚚"
)

_SHIPMENT_COMPLETED_MARKERS = (
    "تم الشحن",
    "تم شحن طلبك",
    "تم شحن الطلب",
    "شحناه",
    "شحنت لك",
    "شحنت لكم",
    "شحنت طلبك",
    "تم تسليمها للناقل",
    "في الطريق لشركة الشحن",
    "خرجت مع شركة الشحن",
    "طلبك بالطريق",
    "طلبك في الطريق",
    "تم انشاء الشحنه",
    "تم إنشاء الشحنة",
)

_SHIPMENT_COMPLETED_RES = (
    re.compile(
        r"شحنت(?:\s*لك|\s*لكم|\s+ال)?(?:طلب|الطلب)?",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"تم\s*شحن(?:ه|ها|(?:\s+)?(?:طلب(?:ك|كم)|الطلب))?",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:طلب(?:ك|كم)|الطلب)\s*(?:في|بال)\s*الط(?:ر|)يق",
        re.UNICODE | re.IGNORECASE,
    ),
)

_DELIVERY_ETA_RES = (
    re.compile(
        r"يوصل(?:ك|كم| طلب(?:ك|كم))?\s*خلال",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"يوصل(?:ك|كم| طلب(?:ك|كم))?\s*(?:قريب|ب(?:عد|سرعة))",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"يستغرق\s*(?:من\s*)?\d+\s*[-–]\s*\d+\s*ايام",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"خلال\s*\d+\s*[-–]\s*\d+\s*ايام\s*(?:عمل)?",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:ال)?(?:شحن|توصيل).*\d+\s*[-–]\s*\d+\s*ايام",
        re.UNICODE | re.IGNORECASE,
    ),
)

_TRACKING_PROMISE_RES = (
    re.compile(
        r"(?:نرسل|بنرسل|راح\s*نرسل)(?:\s*لك|\s*لكم|\s+لك)?.*(?:تتبع|tracking)",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:رابط|رقم)\s*(?:ال)?تتبع.*(?:نرسل|ارسل|أرسل|بنرسل)",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:تحديثات|رابط\s*(?:ال)?تتبع).*(?:نرسل|بنرسل)",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"رابط\s*(?:ال)?تتبع",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"نرسل.{0,25}مباشر[هة]",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:تحديثات|متابعة).*(?:تتبع|شحن)",
        re.UNICODE | re.IGNORECASE,
    ),
)

CLAIM_KIND_SHIPMENT = "ungrounded_shipment_claim"
CLAIM_KIND_DELIVERY_ETA = "ungrounded_delivery_eta"
CLAIM_KIND_TRACKING_PROMISE = "ungrounded_tracking_promise"
CLAIM_KIND_CARRIER_CHANGE = "ungrounded_carrier_change"

_CARRIER_CHANGE_RES = (
    re.compile(
        r"(?:غير(?:ت|نا)|تم\s*تغيير|س(?:غ|ـ)?ير|راح\s*أغير|ب(?:غ|ـ)?ير).{0,40}(?:شحن|شركة\s*الشحن|carrier)",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:changed|updated|switched)\s+(?:the\s+)?(?:shipping|carrier|courier)",
        re.UNICODE | re.IGNORECASE,
    ),
)


def _norm(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = _NORMALISE_AR_RE.sub("", text)
    t = t.replace("ـ", "")
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ى", "ي").replace("ة", "ه")
    )
    return t.lower().strip()


def _matches_any_pattern(norm: str, patterns: tuple) -> bool:
    return any(p.search(norm) for p in patterns)


def reply_contains_shipment_completed_wording(reply: Optional[str]) -> bool:
    norm = _norm(reply)
    if not norm:
        return False
    markers = (_norm(marker) for marker in _SHIPMENT_COMPLETED_MARKERS)
    if any(marker in norm for marker in markers):
        return True
    return _matches_any_pattern(norm, _SHIPMENT_COMPLETED_RES)


def reply_contains_delivery_eta_claim(reply: Optional[str]) -> bool:
    norm = _norm(reply)
    if not norm:
        return False
    return _matches_any_pattern(norm, _DELIVERY_ETA_RES)


def reply_contains_tracking_promise_claim(reply: Optional[str]) -> bool:
    norm = _norm(reply)
    if not norm:
        return False
    return _matches_any_pattern(norm, _TRACKING_PROMISE_RES)


def reply_contains_carrier_change_claim(reply: Optional[str]) -> bool:
    norm = _norm(reply)
    if not norm:
        return False
    return _matches_any_pattern(norm, _CARRIER_CHANGE_RES)


def _carrier_change_execution_succeeded(
    extra_metadata: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    for md in (extra_metadata, inbound_metadata):
        if not isinstance(md, dict):
            continue
        if md.get("shipping_carrier_change_succeeded") is True:
            return True
        exec_log = md.get("last_execution") or md.get("action_execution")
        if isinstance(exec_log, dict):
            action = str(exec_log.get("action") or "").strip().lower()
            if action in {"change_shipping_carrier", "update_shipping_carrier"}:
                return bool(exec_log.get("success") is True)
    return False


def detect_ungrounded_shipment_claim_kinds(reply: Optional[str]) -> Tuple[str, ...]:
    """Return blocked claim kinds present in *reply* (pre-evidence check)."""
    kinds: list[str] = []
    if reply_contains_shipment_completed_wording(reply):
        kinds.append(CLAIM_KIND_SHIPMENT)
    if reply_contains_delivery_eta_claim(reply):
        kinds.append(CLAIM_KIND_DELIVERY_ETA)
    if reply_contains_tracking_promise_claim(reply):
        kinds.append(CLAIM_KIND_TRACKING_PROMISE)
    if reply_contains_carrier_change_claim(reply):
        kinds.append(CLAIM_KIND_CARRIER_CHANGE)
    return tuple(kinds)


def chunk_contains_blocked_shipment_claim(
    chunk: str,
    kinds: Optional[Tuple[str, ...]] = None,
) -> bool:
    active = kinds or detect_ungrounded_shipment_claim_kinds(chunk)
    if not active:
        return False
    if CLAIM_KIND_SHIPMENT in active and reply_contains_shipment_completed_wording(chunk):
        return True
    if CLAIM_KIND_DELIVERY_ETA in active and reply_contains_delivery_eta_claim(chunk):
        return True
    if CLAIM_KIND_TRACKING_PROMISE in active and reply_contains_tracking_promise_claim(chunk):
        return True
    if CLAIM_KIND_CARRIER_CHANGE in active and reply_contains_carrier_change_claim(chunk):
        return True
    return False


def strip_ungrounded_shipment_claim_sentences(
    reply: str,
    kinds: Optional[Tuple[str, ...]] = None,
) -> str:
    """Remove sentences/chunks containing ungrounded shipment operational claims."""
    raw = (reply or "").strip()
    active = kinds or detect_ungrounded_shipment_claim_kinds(raw)
    if not raw or not active:
        return raw

    kept: list[str] = []
    for chunk in re.split(r"(?<=[.!?؟،])\s+|\n+", raw):
        part = chunk.strip().rstrip("،,.")
        if part and not chunk_contains_blocked_shipment_claim(part, active):
            kept.append(part)
    return " ".join(kept).strip()


@dataclass(frozen=True)
class ShipmentTruthGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""
    evidence: Optional[ShipmentEvidenceResult] = None
    blocked_claims: Tuple[str, ...] = ()
    scrubbed_empty: bool = False


def log_shipment_truth_guard(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    action: str,
    reason: str,
    evidence_source: str,
    order_status: str,
    tracking_present: bool,
    blocked_claims: Tuple[str, ...] = (),
) -> None:
    try:
        logger.info(
            "[SHIPMENT_TRUTH_GUARD] tenant_id=%s conversation_id=%s "
            "action=%s reason=%s evidence_source=%s order_status=%s "
            "tracking_present=%s blocked_claims=%s",
            tenant_id,
            conversation_id,
            action,
            reason or "-",
            evidence_source or "-",
            order_status or "-",
            bool(tracking_present),
            ",".join(blocked_claims) if blocked_claims else "-",
        )
    except Exception:  # noqa: BLE001
        pass


def should_skip_brain_silent_ack_after_shipment_scrub(
    *,
    reply: str,
    shipment_claim_scrubbed_empty: bool,
) -> bool:
    """True when shipment guard scrubbed all claims — do not inject silent ACK."""
    return bool(shipment_claim_scrubbed_empty and not (reply or "").strip())


def resolve_outbound_after_shipment_scrub(
    *,
    guard_result: ShipmentTruthGuardResult,
    empty_reply_fallback_text: str,
    skip_brain_silent_ack: bool = False,
) -> tuple[str, bool, bool]:
    """
    Resolve webhook/pipeline outbound after shipment guard scrub.

    Returns ``(final_reply, suppress_send, skip_silent_ack)``.
    """
    reply = str(guard_result.reply or "")
    if not guard_result.scrubbed_empty:
        return reply, False, False

    if skip_brain_silent_ack or should_skip_brain_silent_ack_after_shipment_scrub(
        reply=reply,
        shipment_claim_scrubbed_empty=True,
    ):
        return "", True, True

    fallback = (empty_reply_fallback_text or "").strip()
    if fallback:
        return fallback, False, False
    return "", True, True


def apply_shipment_truth_guard(
    *,
    reply: str,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    payment_receipt_received: bool = False,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> ShipmentTruthGuardResult:
    try:
        original = str(reply or "")
        if not original.strip():
            return ShipmentTruthGuardResult(reply=original, action="allowed")

        blocked_kinds = detect_ungrounded_shipment_claim_kinds(original)
        evidence = evaluate_shipment_evidence(
            commerce_bundle=commerce_bundle,
            extra_metadata=extra_metadata,
            inbound_metadata=inbound_metadata,
            payment_receipt_received=payment_receipt_received,
        )

        if not blocked_kinds:
            log_shipment_truth_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="allowed",
                reason="no_operational_shipment_claims",
                evidence_source=evidence.evidence_source,
                order_status=evidence.order_status,
                tracking_present=evidence.tracking_present,
            )
            return ShipmentTruthGuardResult(
                reply=original,
                action="allowed",
                evidence=evidence,
            )

        needs_shipment = any(
            kind in blocked_kinds
            for kind in (
                CLAIM_KIND_SHIPMENT,
                CLAIM_KIND_DELIVERY_ETA,
                CLAIM_KIND_TRACKING_PROMISE,
            )
        )
        needs_carrier = CLAIM_KIND_CARRIER_CHANGE in blocked_kinds
        shipment_ok = (not needs_shipment) or evidence.evidence_ok
        carrier_change_ok = (not needs_carrier) or _carrier_change_execution_succeeded(
            extra_metadata,
            inbound_metadata,
        )

        if shipment_ok and carrier_change_ok:
            log_shipment_truth_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="allowed",
                reason=evidence.reason,
                evidence_source=evidence.evidence_source,
                order_status=evidence.order_status,
                tracking_present=evidence.tracking_present,
            )
            return ShipmentTruthGuardResult(
                reply=original,
                action="allowed",
                evidence=evidence,
            )

        scrubbed = strip_ungrounded_shipment_claim_sentences(original, blocked_kinds)
        scrubbed_empty = not scrubbed.strip()
        log_shipment_truth_guard(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action="blocked_ungrounded_shipment_claim",
            reason=evidence.reason,
            evidence_source=evidence.evidence_source,
            order_status=evidence.order_status,
            tracking_present=evidence.tracking_present,
            blocked_claims=blocked_kinds,
        )
        return ShipmentTruthGuardResult(
            reply=scrubbed,
            action="blocked_ungrounded_shipment_claim",
            replaced=(scrubbed != original),
            reason=evidence.reason,
            evidence=evidence,
            blocked_claims=blocked_kinds,
            scrubbed_empty=scrubbed_empty,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[SHIPMENT_TRUTH_GUARD] guard failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return ShipmentTruthGuardResult(reply=str(reply or ""), action="allowed")
