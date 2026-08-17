"""
core/order_flow.py
──────────────────
Order-flow context + deterministic receipt handler.

This module sits between the WhatsApp webhook and the AI brain. It
implements two responsibilities the brain cannot do well on its own:

1. ``build_order_context``
   ─────────────────────────
   Pull the bot's idea of the current order from
   ``Conversation.extra_metadata['brain_state']`` and project it as a
   small dict for the media normalizer. The PDF/image classifier uses
   this dict to boost ambiguous documents (e.g. a Saudi-bank receipt
   with the filename ``document_1778767962508.pdf``) to
   ``pdf_kind=payment_receipt`` when the bot was demonstrably waiting
   for a transfer receipt.

2. ``maybe_handle_receipt_inbound``
   ─────────────────────────────────
   Short-circuit decision: when a PDF / image arrives, classified as a
   payment receipt, AND the conversation has an active product focus
   (or ``awaiting_payment_receipt=True``), the webhook should NOT pass
   the inbound to the brain. The brain has been observed to lose the
   product context on follow-up text turns and re-ask product
   discovery. We bypass the brain entirely:

       - Mutate state: ``payment_receipt_received=True``,
         ``order_status="under_review"``,
         ``awaiting_payment_receipt=False``.
       - Send a deterministic Arabic acknowledgement that surfaces the
         product + price + national address so the customer knows
         their order is in good hands.
       - Emit ``[ORDER_FLOW_STATE]`` log lines at the transition.

The brain still runs on every OTHER inbound (text follow-ups, product
questions, address corrections). This module is intentionally narrow:
it owns ONE corner of the funnel — the receipt arrival — and resists
the temptation to grow into a general state machine.

The third helper, ``context_aware_dedup_fallback``, replaces the
canned "أنا هنا — قول وش تحتاج وأكمل معك" fallback the webhook used
to send whenever the brain's draft tripped the near-duplicate guard.
When order state is non-empty, we substitute a context-relevant line
("طلبك قيد التجهيز ...") so the merchant never sees a stale generic
prompt mid-funnel.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.order_flow")


# ── Payment contradiction guard (Wave 1, W1.1 — May 2026) ───────────
#
# Production complaint: within the same beat the bot would say
#   "وصلني الإيصال"      (got the receipt)
# and then ask for it again
#   "أرسل لي الإيصال"    (send the receipt).
#
# Root cause located in the diagnostic: the post-send
# ``mark_awaiting_receipt`` hook scans the OUTBOUND text for the bare
# keyword "إيصال" (see ``_RECEIPT_ASK_KEYWORDS`` below). The bot's own
# receipt acknowledgement contains "إيصال", so the keyword scan
# false-matches and flips ``awaiting_payment_receipt=True`` IMMEDIATELY
# AFTER ``payment_receipt_received=True`` was just set on the same
# turn. Next turn the bot then asks for the receipt as if nothing
# arrived.
#
# This guard refuses the awaiting flip when the receipt was confirmed
# inside the recency window (default 30 minutes). It does NOT change
# any other behaviour — the keyword scan still works, the legacy
# code path is preserved, the flip simply gets vetoed when the state
# would contradict itself.
#
# Strict invariants:
#   * Pure read of brain state. Never mutates anything.
#   * Never raises. Any DB / shape / parse failure falls through to
#     the legacy ``apply_state_patch`` call — defensive, because the
#     post-send hook must not break the outbound response path.
#   * Independent kill switch ``PAYMENT_CONTRADICTION_GUARD_ENABLED``
#     (default OFF). Wave 1 will flip ON after telemetry confirms
#     the guard is silent on legitimate flips.
#   * Conservative window: a missing / unparseable
#     ``payment_receipt_at`` is TREATED AS RECENT, because we'd
#     rather skip a legitimate awaiting-flip than overwrite a
#     genuine receipt-confirmed state.
_PAYMENT_CONTRADICTION_GUARD_RECENT_RECEIPT_WINDOW_SECS = 30 * 60


def _payment_contradiction_guard_enabled() -> bool:
    """Return ``True`` when ``PAYMENT_CONTRADICTION_GUARD_ENABLED`` is
    set to a truthy value. Wave 1 default = OFF (staged rollout)."""
    raw = (
        os.environ.get("PAYMENT_CONTRADICTION_GUARD_ENABLED") or ""
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _w13_emit_receipt_extraction(
    *,
    tenant_id: Any,
    phone: Optional[str],
    conversation_id: Any = None,
    message_id: Any = None,
    source: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Wave 1 W1.3 — observation-only emission of the
    ``[PAYMENT_RECEIPT_EXTRACTED]`` log line.

    Runs the receipt-extraction layer on the inbound metadata to
    surface what fields the regex-heuristic extractor sees in the
    full-text body (vs the legacy 280-char preview), with
    per-field confidence. Behaviour is byte-identical with the
    ``RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED`` flag off — this
    helper neither computes nor logs anything in that case. NEVER
    raises. Independent from the W1.2 verdict-telemetry flag.
    """
    try:
        from core.receipt_extraction import (  # noqa: PLC0415
            compute_receipt_fields,
            is_receipt_extraction_telemetry_enabled,
            log_receipt_fields,
        )
        if not is_receipt_extraction_telemetry_enabled():
            return
        fields = compute_receipt_fields(metadata=metadata or {})
        log_receipt_fields(
            tenant_id=tenant_id, phone=phone,
            conversation_id=conversation_id,
            message_id=message_id,
            source=source,
            fields=fields,
        )
    except Exception:
        # Telemetry must never break the pipeline.
        return


def _w12_emit_receipt_verdict(
    *,
    tenant_id: Any,
    phone: Optional[str],
    conversation_id: Any = None,
    message_id: Any = None,
    source: str,
    payment_understanding: Any = None,
    payment_evidence_status: Optional[str] = None,
    image_kind: Optional[str] = None,
    pdf_kind: Optional[str] = None,
    has_attached_media: bool = False,
    has_text_only_claim: bool = False,
) -> None:
    """Wave 1 W1.2 — observation-only emission of the unified
    ``[PAYMENT_VERIFICATION_DECISION]`` log line.

    Folds the existing ``PaymentUnderstanding`` (W1.0) and
    ``payment_evidence_status`` (legacy classifier) signals into
    one closed verdict and emits a structured log line. Behaviour
    is byte-identical with the ``RECEIPT_VERDICT_TELEMETRY_ENABLED``
    flag off — this helper neither computes nor logs anything in
    that case. Even with the flag on, callers MUST NOT consume the
    verdict for state decisions in W1.2; the consumption surface
    arrives in W1.4. NEVER raises.
    """
    try:
        from core.receipt_verdict import (  # noqa: PLC0415
            compute_receipt_verdict,
            is_receipt_verdict_telemetry_enabled,
            log_receipt_verdict,
        )
        if not is_receipt_verdict_telemetry_enabled():
            return
        rv = compute_receipt_verdict(
            payment_understanding=payment_understanding,
            payment_evidence_status=payment_evidence_status,
            image_kind=image_kind,
            pdf_kind=pdf_kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
        )
        log_receipt_verdict(
            tenant_id=tenant_id, phone=phone,
            conversation_id=conversation_id,
            message_id=message_id,
            source=source,
            verdict=rv,
        )
    except Exception:
        # Telemetry must never break the pipeline.
        return


def _receipt_received_recently(received_at_iso: str) -> bool:
    """Return ``True`` when ``received_at_iso`` is within the
    contradiction-guard recency window.

    Behaviour:
      * Empty / missing string  -> ``True``  (defensive: never
        downgrade a confirmed receipt because the ISO field is
        missing).
      * Unparseable string      -> ``True``  (same reason).
      * Naive timestamp         -> assumed UTC.
      * Inside the window       -> ``True``.
      * Outside the window      -> ``False``.

    Pure function. Never raises.
    """
    if not received_at_iso:
        return True
    try:
        from datetime import timedelta  # noqa: PLC0415,F401
        ts = datetime.fromisoformat(
            str(received_at_iso).replace("Z", "+00:00")
        )
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return delta.total_seconds() <= _PAYMENT_CONTRADICTION_GUARD_RECENT_RECEIPT_WINDOW_SECS
    except Exception:
        return True


# ── Receipt-request keywords (Arabic + English) ─────────────────────
#
# Used by ``detect_awaiting_receipt_in_reply`` to scan the bot's
# outbound draft right before it ships. If the reply asks the
# customer for a transfer receipt, we flip
# ``awaiting_payment_receipt=True`` so the NEXT inbound document
# is classified with high confidence.
_RECEIPT_ASK_KEYWORDS = (
    "إيصال",
    "ايصال",
    "إذا حولت",
    "اذا حولت",
    "أرسل لي الإيصال",
    "ارسل لي الايصال",
    "أرسل الإيصال",
    "ارسل الايصال",
    "ابعث الإيصال",
    "ابعث الايصال",
    "إيصال التحويل",
    "ايصال التحويل",
    "screenshot of the transfer",
    "transfer receipt",
    "send the receipt",
    "send receipt",
)


def detect_awaiting_receipt_in_reply(reply_text: str) -> bool:
    """Return True when the bot's outbound text asks the customer for
    a payment receipt. Pure heuristic; no LLM call.

    The check is intentionally permissive: a small false-positive rate
    is fine because the only consequence is that we boost the next
    inbound PDF/image to ``payment_receipt`` with higher confidence.
    A false-negative (failing to detect a receipt request) is worse —
    the customer then sends a generic-looking PDF and the brain loses
    context.
    """
    if not reply_text or not isinstance(reply_text, str):
        return False
    blob = reply_text.lower()
    return any(k in blob or k in reply_text for k in _RECEIPT_ASK_KEYWORDS)


def _normalize_e164(raw: Optional[str]) -> Optional[str]:
    """Best-effort wrapper around ``utils.phone_utils.normalize_to_e164``
    that never raises (lazy-imported so the unit-tested module stays
    cheap when libphonenumber isn't installed)."""
    if not raw:
        return None
    try:
        from utils.phone_utils import normalize_to_e164  # noqa: PLC0415
        return normalize_to_e164(raw) or None
    except Exception:
        return None


def _load_brain_state(
    db: Any, *, tenant_id: int, phone: str,
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """Return ``(conversation_row, brain_state_dict)``. Both may be
    empty when the conversation hasn't been touched yet. Never
    raises."""
    if db is None or not tenant_id or not phone:
        return None, {}
    try:
        from models import Conversation, Customer  # noqa: PLC0415
        e164 = _normalize_e164(phone) or phone
        conv = _find_conversation_by_phone(
            db, tenant_id=int(tenant_id), phones=(e164, phone),
            Conversation=Conversation, Customer=Customer,
        )
        if conv is None:
            return None, {}
        meta = dict(conv.extra_metadata or {})
        bs = meta.get("brain_state") or {}
        if not isinstance(bs, dict):
            bs = {}
        return conv, bs
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ORDER_FLOW_STATE] load brain_state failed: %s", exc)
        return None, {}


def _find_conversation_by_phone(
    db: Any,
    *,
    tenant_id: int,
    phones: Tuple[str, ...],
    Conversation: Any,
    Customer: Any,
) -> Optional[Any]:
    """Locate the latest ``Conversation`` row for the given customer
    phone. Conversation rows are linked to customers via
    ``customer_id`` (Customer.normalized_phone / Customer.phone holds
    the actual number) — there's no ``customer_phone`` column on
    Conversation itself. We try the JOIN-based lookup first, then
    fall back to the legacy ``extra_metadata['customer_phone']``
    payload for rows created before the customer link existed."""
    phone_candidates = tuple({p for p in phones if p})
    if not phone_candidates:
        return None
    try:
        conv = (
            db.query(Conversation)
            .join(Customer, Conversation.customer_id == Customer.id)
            .filter(
                Conversation.tenant_id == int(tenant_id),
                (
                    Customer.normalized_phone.in_(phone_candidates)
                    | Customer.phone.in_(phone_candidates)
                ),
            )
            .order_by(Conversation.id.desc())
            .first()
        )
        if conv is not None:
            return conv
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ORDER_FLOW_STATE] conversation lookup via customer join "
            "failed: %s", exc,
        )

    try:
        from sqlalchemy import or_  # noqa: PLC0415
        meta_phone_clauses = []
        for p in phone_candidates:
            meta_phone_clauses.append(
                Conversation.extra_metadata.op("->>")("customer_phone") == p,
            )
            meta_phone_clauses.append(
                Conversation.extra_metadata.op("->>")("phone") == p,
            )
        if meta_phone_clauses:
            return (
                db.query(Conversation)
                .filter(
                    Conversation.tenant_id == int(tenant_id),
                    or_(*meta_phone_clauses),
                )
                .order_by(Conversation.id.desc())
                .first()
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ORDER_FLOW_STATE] conversation lookup via metadata "
            "fallback failed: %s", exc,
        )
    return None


def _format_line_items_summary(line_items: List[Dict[str, Any]]) -> str:
    names: List[str] = []
    for item in line_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or item.get("title") or item.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return " + ".join(names[:5])


def _resolve_order_totals(op: Dict[str, Any], line_items: List[Dict[str, Any]]) -> tuple[Any, str]:
    total = op.get("catalog_checkout_total") or op.get("order_total") or op.get("order_flow_v2_catalog_total")
    currency = str(op.get("catalog_checkout_currency") or op.get("order_flow_v2_currency") or "SAR")
    if total is None and line_items:
        try:
            from core.wa_cart_line_items import cart_total_amount  # noqa: PLC0415

            total = cart_total_amount(line_items)
        except Exception:  # noqa: BLE001
            total = None
    return total, currency


def _focus_summary(brain_state: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a flat summary of the order focus from the persisted
    brain state. Returns empty fields when nothing is known. Used by
    both ``build_order_context`` and the deterministic receipt
    acknowledgement so they speak the same shape.

    For receipt replies, use ``_receipt_turn_summary`` which applies
    ``receipt_order_grounding`` — this helper may still surface browse
    focus for non-receipt consumers."""
    focus = brain_state.get("current_product_focus")
    if not isinstance(focus, dict):
        focus = {}
    op = brain_state.get("order_prep") or brain_state.get("order_preparation") or {}
    if not isinstance(op, dict):
        op = {}
    line_items = list(op.get("line_items") or brain_state.get("cart_items") or [])
    selected = str(focus.get("title") or focus.get("name") or "")
    price = focus.get("price")
    currency = str(focus.get("currency") or "SAR")
    if line_items:
        selected = _format_line_items_summary(line_items) or selected
        total, cur = _resolve_order_totals(op, line_items)
        if total is not None:
            price = total
        if cur:
            currency = cur
    return {
        "selected_product":      selected,
        "selected_product_id":   str(focus.get("id") or op.get("product_id") or ""),
        "price":                 price,
        "currency":              currency,
        "line_items_count":      len(line_items),
        "is_multi_item":         len(line_items) > 1,
        "city":                  str(op.get("city") or ""),
        "short_address_code":    str(op.get("short_address_code") or ""),
        "google_maps_url":       str(op.get("google_maps_url") or ""),
        "customer_first_name":   str(op.get("customer_first_name") or ""),
        "customer_last_name":    str(op.get("customer_last_name") or ""),
        "missing_fields":        list(op.get("missing_fields") or []),
        "awaiting_payment_receipt": bool(op.get("awaiting_payment_receipt")),
        "payment_receipt_received": bool(op.get("payment_receipt_received")),
        "order_status":          str(op.get("order_status") or ""),
        "order_creation_status": str(op.get("order_creation_status") or ""),
        "salla_order_id":        str(op.get("salla_order_id") or ""),
        "payment_method":        str(
            brain_state.get("payment_method") or op.get("payment_method") or ""
        ),
    }


def _receipt_turn_summary(
    brain_state: Dict[str, Any],
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Receipt-path summary — product/address only when order evidence exists."""
    from core.receipt_order_grounding import (  # noqa: PLC0415
        apply_receipt_grounding_to_summary,
        evaluate_receipt_order_grounding,
    )

    raw = _focus_summary(brain_state)
    evidence = evaluate_receipt_order_grounding(
        brain_state,
        inbound_metadata=inbound_metadata,
    )
    return apply_receipt_grounding_to_summary(raw, evidence)


def build_order_context(
    *, db: Any, tenant_id: int, phone: str,
) -> Dict[str, Any]:
    """Project the current order focus as a context dict for the
    media normalizer. Keys consumed by
    ``modules.ai.media.normalizer.classify_inbound_document``:

      * ``awaiting_payment_receipt``
      * ``has_active_order``     (product + price known)
      * ``has_address``          (national address code present)
      * ``selected_product``
      * ``price``

    Empty / partial when the conversation has no active focus.
    """
    _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
    s = _focus_summary(bs)
    return {
        "awaiting_payment_receipt": s["awaiting_payment_receipt"],
        "has_active_order": bool(
            s["selected_product"] and s["price"] is not None
        ),
        "has_address": bool(s["short_address_code"] or s["google_maps_url"]),
        "selected_product": s["selected_product"],
        "price":            s["price"],
        "currency":         s["currency"],
        "order_status":     s["order_status"],
        "payment_method":   s["payment_method"],
    }


def _format_price(value: Any, currency: str = "SAR") -> str:
    """Render a price like ``"358 ر.س"``. Falls back to a bare
    number when the currency is unknown."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    if amount.is_integer():
        amount_str = str(int(amount))
    else:
        amount_str = f"{amount:.2f}".rstrip("0").rstrip(".")
    cur = (currency or "").upper()
    symbol = {"SAR": "ر.س", "USD": "$", "AED": "د.إ"}.get(cur, cur)
    return f"{amount_str} {symbol}".strip()


def _missing_shipping_fields(summary: Dict[str, Any]) -> List[str]:
    """Return the list of customer-side address fields still missing
    after a payment receipt has been confirmed. Kept tight: we ask
    only for what we cannot derive from anything already in state.

    The order matches the order in which we want the customer to
    answer (first name → last name → city → location proof).
    """
    missing: List[str] = []
    if not (summary.get("customer_first_name") or "").strip():
        missing.append("first_name")
    if not (summary.get("customer_last_name") or "").strip():
        missing.append("last_name")
    if not (summary.get("city") or "").strip():
        missing.append("city")
    has_location_proof = bool(
        (summary.get("short_address_code") or "").strip()
        or (summary.get("google_maps_url") or "").strip()
    )
    if not has_location_proof:
        missing.append("location")
    return missing


def _compose_address_interview(missing: List[str]) -> str:
    """Render the post-receipt address interview as a single short
    Arabic message. Keeps the wording minimal so the customer can
    paste everything in one reply.

    All fields are asked together (not one-by-one) so the order
    funnel doesn't feel bureaucratic — production tests showed
    customers abandon at any prompt that asks for "one thing at a
    time" after they already paid.
    """
    if not missing:
        return ""
    lines: List[str] = ["عشان نوصّل طلبك بأسرع وقت، أرسل لنا:"]
    if "first_name" in missing or "last_name" in missing:
        lines.append("• الاسم الأول والأخير")
    if "city" in missing:
        lines.append("• مدينة التوصيل")
    if "location" in missing:
        lines.append(
            "• الموقع: رابط قوقل ماب أو العنوان الوطني (٤ أحرف + ٤ أرقام)"
        )
    return "\n".join(lines)


def _compose_receipt_ack(
    summary: Dict[str, Any],
    *,
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the deterministic payment-receipt Arabic reply.

    Product, quantity, and address asks require confirmed order evidence
    (see ``core.receipt_order_grounding``). Receipt media alone grounds
    only transfer received + optional amount review."""
    from core.receipt_order_grounding import (  # noqa: PLC0415
        apply_receipt_grounding_to_summary,
        compose_grounded_receipt_ack,
        evaluate_receipt_order_grounding,
    )

    grounded = dict(summary or {})
    if "can_mention_receipt_product" not in grounded:
        evidence = evaluate_receipt_order_grounding(
            brain_state or {},
            inbound_metadata=inbound_metadata,
        )
        grounded = apply_receipt_grounding_to_summary(grounded, evidence)
    return compose_grounded_receipt_ack(grounded)


def _receipt_text_fields(inbound_metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Persist OCR/vision text alongside receipt pointers for revenue bridge."""
    md = inbound_metadata or {}
    fields = {
        "vision_text":              md.get("vision_text"),
        "frame_vision_text":        md.get("frame_vision_text"),
        "ocr_text":                 md.get("ocr_text"),
        "pdf_text_preview":         md.get("pdf_text_preview"),
        "pdf_text_full":            md.get("pdf_text_full"),
        "caption":                  md.get("caption"),
        "confirmed_payment_amount": md.get("confirmed_payment_amount") or md.get("amount"),
        "receipt_data":             md.get("receipt_data"),
    }
    return {k: v for k, v in fields.items() if v not in (None, "")}


def _compose_payment_state_patch(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    conversation: Any,
    summary: Dict[str, Any],
    payment_state: str,
    receipt_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Build order_prep patch for payment evidence / receipt confirmation."""
    from core.bank_transfer_receipt_resolver import (  # noqa: PLC0415
        PAYMENT_EVIDENCE_RECEIVED,
        PAYMENT_RECEIVED,
    )
    from core.payment_media_metadata import enrich_payment_receipt_metadata  # noqa: PLC0415

    enriched = enrich_payment_receipt_metadata(
        receipt_metadata,
        tenant_id=tenant_id,
        phone=phone,
        conversation=conversation,
    )
    patch: Dict[str, Any] = {"payment_receipt_metadata": enriched}
    has_active = bool(summary.get("can_mention_receipt_product")) or bool(
        summary.get("receipt_order_evidence", {}).get("has_confirmed_order")
    )
    awaiting = bool(summary.get("awaiting_payment_receipt"))
    now_iso = datetime.now(timezone.utc).isoformat()

    if payment_state == PAYMENT_RECEIVED and (has_active or awaiting):
        patch.update({
            "payment_method":              "bank_transfer",
            "payment_status":              "pending_verification",
            "awaiting_payment_receipt":    False,
            "payment_receipt_received":    True,
            "payment_receipt_at":          now_iso,
            "payment_confirmed":           False,
            "payment_verification_status": "pending",
            "payment_submission_received": True,
            "payment_submission_at":       now_iso,
            "payment_submission_type":     "receipt",
            "payment_submission_source":   "whatsapp",
            "order_status":                "payment_submitted",
        })
    elif payment_state == PAYMENT_EVIDENCE_RECEIVED and (has_active or awaiting):
        patch.update({
            "payment_evidence_received": True,
            "payment_evidence_at":       now_iso,
        })
    return patch


def maybe_handle_receipt_inbound(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    inbound_normalized_type: str,
    inbound_metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Decide whether to short-circuit the brain because the inbound
    is a payment receipt arriving during an active order.

    Returns:
        ``None`` — the brain should run as normal.
        ``{"reply_text": str, "summary": dict, "state_patch": dict}``
        — caller MUST send ``reply_text`` to the customer instead of
        invoking the brain, and apply ``state_patch`` to
        ``Conversation.extra_metadata['brain_state']['order_prep']``.

    The function NEVER mutates the DB itself — that's the caller's
    job. We separate the decision from the side-effect so this helper
    can be unit-tested with a fake ``db`` that returns canned state.

    Universal payment-evidence gate (May 2026)
    ──────────────────────────────────────────
    Even when the legacy classifier set ``pdf_kind=payment_receipt``,
    we now refuse to short-circuit unless the dedicated payment-
    evidence classifier *also* returned ``status="confirmed"``. The
    normalizer already demotes pre-transfer-review screens to
    ``payment_pre_review`` and ambiguous data-entry screens to
    ``payment_pending_evidence``, but we add a defence-in-depth
    check here so any older normalizer output (rollback, downgrade,
    cached message replay) still cannot leak through to the "thanks,
    order under review" ACK without explicit completion proof.
    """
    from core.payment_media_metadata import flatten_inbound_payment_metadata  # noqa: PLC0415
    from core.payment_receipt_attachment_gate import (  # noqa: PLC0415
        has_inbound_attachment,
        payment_context_active,
        try_metadata_receipt_short_circuit,
    )
    from core.receipt_order_grounding import (  # noqa: PLC0415
        compose_grounded_receipt_ack,
        evaluate_receipt_order_grounding,
        receipt_payment_context_active,
    )

    md = flatten_inbound_payment_metadata(inbound_metadata or {})
    if not has_inbound_attachment(inbound_normalized_type, md):
        return None

    _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
    evidence = evaluate_receipt_order_grounding(bs, inbound_metadata=md)
    summary = _receipt_turn_summary(bs, inbound_metadata=md)

    has_confirmed_order = evidence.has_confirmed_order
    awaiting = bool(summary.get("awaiting_payment_receipt"))
    kind_early = md.get("pdf_kind") or md.get("image_kind")
    pe_early = md.get("payment_evidence_status")
    receipt_media_confirmed = kind_early == "payment_receipt" and (
        not pe_early or pe_early == "confirmed"
    )
    if not (
        has_confirmed_order
        or awaiting
        or summary.get("payment_receipt_received")
        or receipt_media_confirmed
    ):
        if not receipt_payment_context_active(summary, brain_state=bs):
            logger.info(
                "[ORDER_FLOW_STATE] receipt arrived but no payment context — "
                "letting brain handle | tenant=%s phone=*%s",
                tenant_id, phone[-4:] if phone else "",
            )
            return None

    # The PDF classifier or the image vision check sets these slots.
    kind = md.get("pdf_kind") or md.get("image_kind")
    pe_status = md.get("payment_evidence_status")
    strict_ok = kind == "payment_receipt" and (
        not pe_status or pe_status == "confirmed"
    )
    if not strict_ok:
        _metadata_decision = try_metadata_receipt_short_circuit(
            inbound_normalized_type=inbound_normalized_type,
            inbound_metadata=md,
            summary=summary,
        )
        if _metadata_decision is not None:
            logger.info(
                "[PAYMENT_RECEIPT_ATTACHMENT] short_circuit tenant=%s phone=*%s "
                "route=%s duplicate=%s",
                tenant_id,
                phone[-4:] if phone else "",
                _metadata_decision.get("route"),
                _metadata_decision.get("duplicate"),
            )
            _metadata_decision["summary"] = summary
            if not _metadata_decision.get("duplicate"):
                _metadata_decision["reply_text"] = compose_grounded_receipt_ack(summary)
                from core.reply_instruction import (  # noqa: PLC0415
                    attach_instruction_to_decision,
                    build_payment_receipt_instruction,
                )

                _metadata_decision = attach_instruction_to_decision(
                    _metadata_decision,
                    build_payment_receipt_instruction(
                        legacy_copy=_metadata_decision["reply_text"],
                        summary=summary,
                    ),
                )
        return _metadata_decision

    if pe_status and pe_status != "confirmed":
        logger.info(
            "[PAYMENT_EVIDENCE] receipt short-circuit blocked tenant=%s "
            "phone=*%s pdf_kind=%s payment_evidence_status=%s "
            "payment_evidence_reason=%s",
            tenant_id, (phone[-4:] if phone else ""), kind,
            pe_status,
            md.get("payment_evidence_reason"),
        )
        return try_metadata_receipt_short_circuit(
            inbound_normalized_type=inbound_normalized_type,
            inbound_metadata=md,
            summary=summary,
        )

    inbound_metadata = md

    # Confirmed receipt media always short-circuits; reply content is
    # grounded by ``receipt_order_grounding`` (stale focus is not enough).
    # ── Tenant-account verification gate (May 2026 #48) ───────────
    # Even when the deterministic ``payment_evidence`` classifier
    # said ``confirmed`` (a real receipt with completion markers),
    # we must NOT treat the order as paid until the receipt's
    # IBAN / beneficiary matches one of the merchant's registered
    # official accounts. Otherwise a customer's screenshot of an
    # unrelated transfer (to a personal account, a previous
    # merchant, a friend, …) would silently flip the order into
    # ``under_review`` + ``payment_receipt_received=True``.
    #
    # Policy:
    #   * Tenant has NO registered ``bank_transfer`` /
    #     ``payment_method`` KB sections → legacy behaviour
    #     (we have nothing to compare against, never block).
    #   * Tenant HAS accounts AND we have some evidence text from
    #     the inbound (caption / filename / pdf_text_preview /
    #     vision_text / ocr_text) → enforce strict match.
    #   * Tenant has accounts BUT the inbound carries no text we
    #     can scan → fall back to legacy. The deterministic
    #     ``classify_payment_evidence`` already gated this turn on
    #     completion markers; we only block when we have a
    #     concrete IBAN / beneficiary to compare.
    _understanding_block: Optional[Dict[str, Any]] = None
    # Wave 1 W1.2 — hoisted out of the inner try block so the
    # receipt-verdict telemetry below can read it regardless of
    # which branch of the verification gate ran. The verdict
    # telemetry NEVER changes behaviour; it only emits a structured
    # log line for vocabulary unification.
    _understanding_pu: Optional[Any] = None
    try:
        from core.tenant_payment_accounts import (  # noqa: PLC0415
            load_tenant_payment_accounts,
        )
        from core.payment_understanding import (  # noqa: PLC0415
            compute_payment_understanding,
            log_payment_understanding,
        )
        _accounts = load_tenant_payment_accounts(db, tenant_id=tenant_id)
        _ev_text_blob = "\n".join(filter(None, [
            (inbound_metadata or {}).get("caption") or "",
            (inbound_metadata or {}).get("filename") or "",
            (inbound_metadata or {}).get("pdf_text_preview") or "",
            (inbound_metadata or {}).get("vision_text") or "",
            (inbound_metadata or {}).get("ocr_text") or "",
        ])).strip()
        if _accounts.has_accounts and _ev_text_blob:
            _verdict = compute_payment_understanding(
                tenant_accounts=_accounts,
                evidence_text=_ev_text_blob,
                has_text_only_claim=False,
            )
            _understanding_pu = _verdict
            log_payment_understanding(
                tenant_id=tenant_id, phone=phone,
                source="receipt_inbound",
                verdict=_verdict,
                extra={"pdf_kind": kind},
            )
            _understanding_block = {
                "status":              _verdict.status,
                "reason":              _verdict.reason,
                "matched_iban":        _verdict.matched_iban,
                "matched_beneficiary": _verdict.matched_beneficiary,
            }
            if not _verdict.can_flip_receipt_received:
                # Wave 1 W1.2 — emit the unified verdict telemetry
                # BEFORE returning, so an "expected paid → blocked"
                # turn still produces one structured log line.
                _w12_emit_receipt_verdict(
                    tenant_id=tenant_id, phone=phone,
                    conversation_id=getattr(_conv, "id", None),
                    message_id=(inbound_metadata or {}).get("message_id"),
                    source="receipt_inbound_blocked",
                    payment_understanding=_understanding_pu,
                    payment_evidence_status=pe_status,
                    image_kind=(inbound_metadata or {}).get("image_kind"),
                    pdf_kind=(inbound_metadata or {}).get("pdf_kind"),
                    has_attached_media=True,
                    has_text_only_claim=False,
                )
                # Wave 1 W1.3 — emit extraction telemetry for the
                # blocked path so we can correlate "what did the
                # extractor see?" with "why did the verifier block?".
                _w13_emit_receipt_extraction(
                    tenant_id=tenant_id, phone=phone,
                    conversation_id=getattr(_conv, "id", None),
                    message_id=(inbound_metadata or {}).get("message_id"),
                    source="receipt_inbound_blocked",
                    metadata=inbound_metadata,
                )
                logger.info(
                    "[ORDER_FLOW_STATE] receipt short-circuit blocked by "
                    "tenant-account verification tenant=%s phone=*%s "
                    "payment_understanding_status=%s reason=%s",
                    tenant_id, (phone[-4:] if phone else ""),
                    _verdict.status, _verdict.reason,
                )
                return None
    except Exception as _u_exc:  # noqa: BLE001
        logger.debug(
            "[ORDER_FLOW_STATE] payment-understanding probe failed "
            "(non-fatal) tenant=%s err=%s",
            tenant_id, _u_exc,
        )

    # Wave 1 W1.2 — receipt-verdict telemetry for the receipt-confirmed
    # path. Observation only: behaviour is identical with the flag
    # off, and even with the flag on the verdict is NOT consumed by
    # any state-flip decision (that arrives in W1.4).
    _w12_emit_receipt_verdict(
        tenant_id=tenant_id, phone=phone,
        conversation_id=getattr(_conv, "id", None),
        message_id=(inbound_metadata or {}).get("message_id"),
        source="receipt_inbound",
        payment_understanding=_understanding_pu,
        payment_evidence_status=pe_status,
        image_kind=(inbound_metadata or {}).get("image_kind"),
        pdf_kind=(inbound_metadata or {}).get("pdf_kind"),
        has_attached_media=True,
        has_text_only_claim=False,
    )
    # Wave 1 W1.3 — extraction telemetry for the same call site.
    _w13_emit_receipt_extraction(
        tenant_id=tenant_id, phone=phone,
        conversation_id=getattr(_conv, "id", None),
        message_id=(inbound_metadata or {}).get("message_id"),
        source="receipt_inbound",
        metadata=inbound_metadata,
    )

    state_patch: Dict[str, Any] = {
        "payment_receipt_metadata": {
            "kind":             kind,
            "confidence":       (inbound_metadata or {}).get("pdf_kind_confidence")
                                 or (inbound_metadata or {}).get("image_kind_confidence"),
            "tenant_account_match": _understanding_block,
            "wa_message_id":    (inbound_metadata or {}).get("wa_message_id"),
            "filename":         (inbound_metadata or {}).get("filename"),
            "mime_type":        (inbound_metadata or {}).get("mime_type"),
            "storage_url":      (inbound_metadata or {}).get("storage_url"),
            "storage_sha256":   (inbound_metadata or {}).get("storage_sha256"),
            "received_at":      datetime.now(timezone.utc).isoformat(),
            **_receipt_text_fields(inbound_metadata or {}),
        },
    }
    try:
        from core.payment_media_metadata import enrich_payment_receipt_metadata  # noqa: PLC0415

        state_patch["payment_receipt_metadata"] = enrich_payment_receipt_metadata(
            state_patch["payment_receipt_metadata"],
            tenant_id=tenant_id,
            phone=phone,
            conversation=_conv,
        )
    except Exception:
        pass

    state_patch.update({
        "awaiting_payment_receipt":    False,
        "payment_receipt_received":    True,
        "payment_receipt_at":          datetime.now(timezone.utc).isoformat(),
        "payment_confirmed":           False,
        "payment_verification_status": "pending",
        "payment_submission_received": True,
        "payment_submission_at":       datetime.now(timezone.utc).isoformat(),
        "payment_submission_type":     "receipt",
        "payment_submission_source":   "whatsapp",
    })
    if has_confirmed_order or awaiting:
        state_patch["order_status"] = "payment_submitted"

    reply_text = _compose_receipt_ack(summary)

    logger.info(
        "[ORDER_FLOW_STATE] receipt acknowledged tenant=%s phone=*%s "
        "kind=%s product=%r price=%s address_code=%s awaiting_was=%s "
        "confirmed_order=%s reason=%s",
        tenant_id, phone[-4:] if phone else "", kind,
        summary.get("selected_product"),
        summary.get("price"),
        summary.get("short_address_code"),
        awaiting,
        has_confirmed_order,
        evidence.reason,
    )
    from core.reply_instruction import (  # noqa: PLC0415
        attach_instruction_to_decision,
        build_payment_receipt_instruction,
    )

    return attach_instruction_to_decision(
        {
            "reply_text":  reply_text,
            "summary":     summary,
            "state_patch": state_patch,
        },
        build_payment_receipt_instruction(
            legacy_copy=reply_text,
            summary=summary,
        ),
    )


def maybe_handle_payment_evidence_inbound(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    inbound_normalized_type: str,
    inbound_metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Sibling of ``maybe_handle_receipt_inbound`` that fires when
    the inbound is payment-related but NOT yet confirmed —
    i.e. the customer sent a pre-transfer review screen, or a
    screenshot of bank/IBAN data, or a PDF with payment context but
    no completion marker.

    For these cases we:
      * Reply with a short, polite, tone-safe sentence that asks
        for the final receipt after the transfer is executed.
      * Do NOT mutate ``order_status`` / ``payment_receipt_received``.
      * Do NOT leak any internal phone/agent contact.
      * Still let the customer's funnel continue (the brain stays
        on standby for the next inbound, which is hopefully the
        real receipt).

    Returns the same shape as ``maybe_handle_receipt_inbound`` so
    the webhook caller can use the identical send / persist path,
    except ``state_patch`` is intentionally empty — we don't want
    to record anything that downstream code might interpret as
    progress.

    Returns ``None`` when the inbound is not payment-evidence at
    all (then the regular brain pipeline runs).
    """
    if inbound_normalized_type not in ("document", "image"):
        return None
    md = inbound_metadata or {}
    pe_status = md.get("payment_evidence_status")
    if pe_status not in (
        "pre_transfer_review",
        "needs_confirmation",
        "bill_payment_unrelated",
        "amount_only_insufficient",
        "invalid_receipt",
    ):
        return None

    _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
    summary = _focus_summary(bs)

    # ── Bank transfer receipt resolver (P0) ─────────────────────────
    # Do NOT treat a completed Rajhi receipt (amount + beneficiary +
    # merchant account match) as a pre-transfer review screen just
    # because OCR also captured ``تأكيد التحويل``.
    try:
        from core.bank_transfer_receipt_resolver import (  # noqa: PLC0415
            PAYMENT_EVIDENCE_RECEIVED,
            PAYMENT_PENDING_CONFIRMATION,
            PAYMENT_RECEIVED,
            PAYMENT_REVIEW_REQUIRED,
            resolve_bank_transfer_receipt,
        )
        from core.payment_media_metadata import payment_text_blob  # noqa: PLC0415
        from core.tenant_payment_accounts import load_tenant_payment_accounts  # noqa: PLC0415

        _resolver_accounts = load_tenant_payment_accounts(db, tenant_id=tenant_id)
        _resolver_blob = payment_text_blob(md)
        _resolver = resolve_bank_transfer_receipt(
            _resolver_blob,
            tenant_accounts=_resolver_accounts,
            filename=str(md.get("filename") or ""),
            legacy_pe_status=str(pe_status or ""),
        )
        if _resolver.payment_state == PAYMENT_REVIEW_REQUIRED:
            # Resolution metadata only — never treat mismatch as an
            # active-order promotion or payment-state flip.
            logger.info(
                "[PAYMENT_EVIDENCE] resolver=review_required tenant=%s "
                "phone=*%s reason=%s (no state flip)",
                tenant_id, (phone[-4:] if phone else ""),
                _resolver.reason,
            )
            try:
                from core.bank_transfer_receipt_resolver import (  # noqa: PLC0415
                    apply_resolution_to_metadata,
                )
                apply_resolution_to_metadata(md, _resolver)
            except Exception:  # noqa: silent-ok — optional payment receipt metadata; keep order flow running
                pass
            # Fall through to tenant-account verification / active-order
            # promotion gates — mismatch must return None there.
        elif _resolver.payment_state in (
            PAYMENT_RECEIVED,
            PAYMENT_EVIDENCE_RECEIVED,
        ):
            kind = md.get("pdf_kind") or md.get("image_kind")
            reply_text = _resolver.reply_ar or _compose_receipt_ack(summary)
            logger.info(
                "[PAYMENT_EVIDENCE] resolver=promoted tenant=%s phone=*%s "
                "state=%s reason=%s product=%r",
                tenant_id, (phone[-4:] if phone else ""),
                _resolver.payment_state, _resolver.reason,
                summary.get("selected_product"),
            )
            receipt_meta = {
                "kind":            kind or "payment_receipt",
                "promoted_from":   pe_status or "resolver",
                "promoted_at":     datetime.now(timezone.utc).isoformat(),
                "wa_message_id":   md.get("wa_message_id"),
                "filename":        md.get("filename"),
                "mime_type":       md.get("mime_type"),
                "storage_url":     md.get("storage_url"),
                "storage_sha256":  md.get("storage_sha256"),
                "received_at":     datetime.now(timezone.utc).isoformat(),
                **_receipt_text_fields(md),
                **_resolver.to_metadata_patch(),
            }
            from core.reply_instruction import (  # noqa: PLC0415
                attach_instruction_to_decision,
                build_payment_receipt_instruction,
            )

            return attach_instruction_to_decision(
                {
                    "reply_text":  reply_text,
                    "summary":     summary,
                    "state_patch": _compose_payment_state_patch(
                        db=db,
                        tenant_id=tenant_id,
                        phone=phone,
                        conversation=_conv,
                        summary=summary,
                        payment_state=_resolver.payment_state,
                        receipt_metadata=receipt_meta,
                    ),
                },
                build_payment_receipt_instruction(
                    legacy_copy=reply_text,
                    summary=summary,
                ),
            )
        if _resolver.payment_state != PAYMENT_PENDING_CONFIRMATION:
            logger.debug(
                "[PAYMENT_EVIDENCE] resolver=no_short_circuit tenant=%s "
                "state=%s reason=%s",
                tenant_id, _resolver.payment_state, _resolver.reason,
            )
    except Exception as _res_exc:  # noqa: BLE001  # noqa: silent-ok — fall through to legacy promotion
        logger.debug(
            "[PAYMENT_EVIDENCE] bank receipt resolver skipped tenant=%s err=%s",
            tenant_id, _res_exc,
        )
    awaiting = bool(summary.get("awaiting_payment_receipt"))
    has_active_order = bool(summary.get("selected_product"))

    try:
        from modules.ai.media.semantic_classifier import (  # noqa: PLC0415
            allows_payment_media_ack,
            log_payment_media_rejected,
            metadata_qualifies_for_payment_evidence_soft_reply,
        )

        # Payment-ACK semantic gate applies to attachment ack copy only.
        # Deterministic ``pre_transfer_review`` / ``needs_confirmation``
        # pairs from ``core.payment_evidence`` use the operational soft
        # reply path — they do not claim completed payment.
        if not metadata_qualifies_for_payment_evidence_soft_reply(md):
            if not allows_payment_media_ack(
                semantic_category=str(md.get("media_semantic_category") or ""),
                payment_evidence_status=pe_status,
                awaiting_payment_receipt=awaiting,
                has_active_order=has_active_order,
            ):
                log_payment_media_rejected(
                    tenant_id=tenant_id,
                    reason="semantic_not_payment",
                    category=str(md.get("media_semantic_category") or ""),
                )
                return None
    except Exception as _sem_exc:  # noqa: BLE001
        logger.debug(
            "[PAYMENT_MEDIA_REJECTED] semantic gate skipped tenant=%s err=%s",
            tenant_id, _sem_exc,
        )

    # Belt-and-braces: also look at the kind slot in case a future
    # caller forgets to thread the payment-evidence status through.
    kind = md.get("pdf_kind") or md.get("image_kind")
    if pe_status is None and kind not in (
        "payment_pre_review", "payment_pending_evidence",
    ):
        return None

    try:
        from core.payment_evidence import (  # noqa: PLC0415
            compose_payment_evidence_reply,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[PAYMENT_EVIDENCE] compose import failed "
            "tenant=%s err=%s", tenant_id, exc,
        )
        return None

    # ── Active-order promotion (May 2026 hotfix) ───────────────────
    # When the customer already has an active order AND we were
    # awaiting their payment receipt (i.e. we explicitly asked them
    # to send proof of transfer), any bank-related document or
    # screenshot is the answer. Real bank PDFs occasionally print
    # "تأكيد التحويل" / "تأكيد العملية" headers that match our
    # pre-transfer phrase library — even though the body shows the
    # transfer is complete. Refusing to accept those forces the
    # customer to re-send the same file, eroding trust.
    #
    # Policy: if the conversation has a selected_product AND we were
    # awaiting their receipt, promote ANY payment-context evidence
    # to confirmed instead of asking them to retake it. The customer
    # is then routed to the receipt ACK which collects shipping
    # details (name, city, location proof).
    #
    # This is intentionally GATED on ``awaiting`` so a random bank
    # screenshot dropped into an unrelated conversation does NOT
    # mark a non-existent order as paid.
    if has_active_order and awaiting:
        # ── Tenant-account verification gate (May 2026 #48) ──────
        # The active-order promotion branch is the riskiest auto-flip:
        # a customer who explicitly asked for a receipt screenshot
        # gets the receipt-confirmed state mutation as soon as ANY
        # bank-related document arrives. When the merchant has
        # registered accounts AND we have any text to scan, we
        # verify the IBAN / beneficiary against those accounts
        # before promoting. Tenants without registered accounts —
        # or inbounds without extractable text — keep legacy
        # behaviour to avoid regressing existing flows.
        _understanding_block: Optional[Dict[str, Any]] = None
        _block_promotion = False
        # Wave 1 W1.2 — hoisted so the verdict telemetry below has
        # access to the underlying ``PaymentUnderstanding`` object
        # regardless of which branch of the verification gate ran.
        _understanding_pu: Optional[Any] = None
        try:
            from core.tenant_payment_accounts import (  # noqa: PLC0415
                load_tenant_payment_accounts,
            )
            from core.payment_understanding import (  # noqa: PLC0415
                compute_payment_understanding,
                log_payment_understanding,
            )
            _accounts = load_tenant_payment_accounts(db, tenant_id=tenant_id)
            _ev_text_blob = "\n".join(filter(None, [
                md.get("caption") or "",
                md.get("filename") or "",
                md.get("pdf_text_preview") or "",
                md.get("vision_text") or "",
                md.get("ocr_text") or "",
            ])).strip()
            if _accounts.has_accounts and _ev_text_blob:
                _verdict = compute_payment_understanding(
                    tenant_accounts=_accounts,
                    evidence_text=_ev_text_blob,
                    has_text_only_claim=False,
                )
                _understanding_pu = _verdict
                log_payment_understanding(
                    tenant_id=tenant_id, phone=phone,
                    source="active_order_promotion",
                    verdict=_verdict,
                    extra={"pe_status": pe_status, "kind": kind},
                )
                _understanding_block = {
                    "status":              _verdict.status,
                    "reason":              _verdict.reason,
                    "matched_iban":        _verdict.matched_iban,
                    "matched_beneficiary": _verdict.matched_beneficiary,
                }
                if not _verdict.can_flip_receipt_received:
                    logger.info(
                        "[PAYMENT_EVIDENCE] active-order promotion blocked by "
                        "tenant-account verification tenant=%s phone=*%s "
                        "payment_understanding_status=%s reason=%s",
                        tenant_id, (phone[-4:] if phone else ""),
                        _verdict.status, _verdict.reason,
                    )
                    # Fall through: skip auto-promotion entirely.
                    _block_promotion = True
        except Exception as _u_exc:  # noqa: BLE001
            logger.debug(
                "[PAYMENT_EVIDENCE] payment-understanding probe failed "
                "(non-fatal) tenant=%s err=%s",
                tenant_id, _u_exc,
            )

        # Wave 1 W1.2 — receipt-verdict telemetry for the
        # active-order promotion path. Observation only.
        _w12_emit_receipt_verdict(
            tenant_id=tenant_id, phone=phone,
            conversation_id=getattr(_conv, "id", None),
            message_id=md.get("message_id"),
            source=(
                "active_order_promotion_blocked"
                if _block_promotion else "active_order_promotion"
            ),
            payment_understanding=_understanding_pu,
            payment_evidence_status=pe_status,
            image_kind=md.get("image_kind"),
            pdf_kind=md.get("pdf_kind"),
            has_attached_media=True,
            has_text_only_claim=False,
        )
        # Wave 1 W1.3 — extraction telemetry for the same call site.
        _w13_emit_receipt_extraction(
            tenant_id=tenant_id, phone=phone,
            conversation_id=getattr(_conv, "id", None),
            message_id=md.get("message_id"),
            source=(
                "active_order_promotion_blocked"
                if _block_promotion else "active_order_promotion"
            ),
            metadata=md,
        )

        if _block_promotion:
            # Skip the auto-promotion entirely. The legacy soft
            # reply is intentionally suppressed too — we want the
            # brain to handle this turn naturally instead of
            # shipping a hardcoded "send the final receipt" line
            # that would confuse a customer whose receipt simply
            # went to the wrong account.
            return None

        _pe_norm = str(pe_status or "").strip().lower()
        if _pe_norm in {
            "bill_payment_unrelated",
            "amount_only_insufficient",
            "invalid_receipt",
            "not_payment",
        }:
            reply_text = compose_payment_evidence_reply(
                _pe_norm,
                awaiting_receipt=awaiting,
            )
            if reply_text:
                logger.info(
                    "[PAYMENT_EVIDENCE] active-order promotion skipped "
                    "tenant=%s phone=*%s pe_status=%s reason=%s",
                    tenant_id, (phone[-4:] if phone else ""),
                    pe_status, md.get("payment_evidence_reason"),
                )
                return {
                    "reply_text": reply_text,
                    "summary": summary,
                    "state_patch": {},
                }
            return None

        logger.info(
            "[PAYMENT_EVIDENCE] active-order promotion → confirmed "
            "tenant=%s phone=*%s pe_status=%s reason=%s product=%r",
            tenant_id, (phone[-4:] if phone else ""),
            pe_status, md.get("payment_evidence_reason"),
            summary.get("selected_product"),
        )
        confirmed_patch: Dict[str, Any] = {
            "awaiting_payment_receipt":    False,
            "payment_receipt_received":    True,
            "payment_receipt_at":          datetime.now(timezone.utc).isoformat(),
            "payment_confirmed":           False,
            "payment_verification_status": "pending",
            "payment_submission_received": True,
            "payment_submission_at":       datetime.now(timezone.utc).isoformat(),
            "payment_submission_type":     "receipt",
            "payment_submission_source":   "whatsapp",
            "order_status":                "payment_submitted",
            "payment_receipt_metadata": {
                "kind":            kind or "payment_receipt",
                "promoted_from":   pe_status or "evidence_active_order",
                "promoted_at":     datetime.now(timezone.utc).isoformat(),
                "wa_message_id":   md.get("wa_message_id"),
                "filename":        md.get("filename"),
                "mime_type":       md.get("mime_type"),
                "storage_url":     md.get("storage_url"),
                "storage_sha256":  md.get("storage_sha256"),
                "tenant_account_match": _understanding_block,
                **_receipt_text_fields(md),
            },
        }
        # ``_compose_receipt_ack`` includes the structured address
        # interview when shipping fields are still missing — that's
        # the explicit user requirement: never re-ask for product
        # selection; ask only for the missing shipping data.
        return {
            "reply_text":  _compose_receipt_ack(summary),
            "summary":     summary,
            "state_patch": confirmed_patch,
        }

    reply_text = compose_payment_evidence_reply(
        pe_status,
        awaiting_receipt=awaiting,
    )
    if not reply_text:
        return None

    try:
        from core.payment_relevance_gate import (  # noqa: PLC0415
            PaymentRelevanceLogContext,
            validate_payment_evidence_prompt,
        )
        _prv = validate_payment_evidence_prompt(
            message=str(md.get("caption") or md.get("vision_text") or ""),
            inbound_metadata=md,
            normalized_type=inbound_normalized_type,
            state_summary=summary,
            history=None,
            tenant_id=tenant_id,
            route="payment_evidence_inbound",
            log_context=PaymentRelevanceLogContext(
                tenant_id=tenant_id,
                phone_tail=(phone[-4:] if phone else ""),
                message=str(md.get("caption") or md.get("vision_text") or ""),
                inbound_metadata=md,
                normalized_type=inbound_normalized_type,
                fallback_source="payment_evidence_inbound",
                artifact=False,
                final_action="evidence_soft_prompt",
            ),
        )
        if not _prv.allowed:
            logger.info(
                "[PAYMENT_EVIDENCE] evidence prompt blocked by relevance gate "
                "tenant=%s phone=*%s reason=%s",
                tenant_id, (phone[-4:] if phone else ""), _prv.reason,
            )
            return None
    except Exception as _prg_exc:  # noqa: BLE001
        logger.debug(
            "[PAYMENT_EVIDENCE] relevance gate skipped tenant=%s err=%s",
            tenant_id, _prg_exc,
        )

    logger.info(
        "[PAYMENT_EVIDENCE] short_circuit=payment_evidence_soft "
        "tenant=%s phone=*%s payment_evidence_status=%s "
        "payment_evidence_reason=%s kind=%s awaiting=%s product=%r",
        tenant_id, (phone[-4:] if phone else ""), pe_status,
        md.get("payment_evidence_reason"), kind, awaiting,
        summary.get("selected_product"),
    )
    from core.reply_instruction import (  # noqa: PLC0415
        attach_instruction_to_decision,
        build_payment_evidence_instruction,
    )

    caption_or_text = str(
        md.get("caption") or md.get("vision_text") or md.get("ocr_text") or ""
    ).strip()
    return attach_instruction_to_decision(
        {
            "reply_text":  reply_text,
            "summary":     summary,
            # Empty state_patch — no order-status mutation, no
            # awaiting_payment_receipt flip. This branch is informational
            # only; the customer hasn't completed anything yet.
            "state_patch": {},
        },
        build_payment_evidence_instruction(
            pe_status=str(pe_status or ""),
            pe_reason=str(md.get("payment_evidence_reason") or ""),
            legacy_copy=reply_text,
            summary=summary,
            inbound_text=caption_or_text,
        ),
    )


def maybe_handle_map_image_inbound(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    inbound_normalized_type: str,
    inbound_metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Short-circuit for map screenshots (Apple/Google Maps).

    Customers frequently send a map screenshot when asked for their
    location. The image isn't a real WhatsApp ``location`` message,
    so we cannot extract coordinates — but we DO know it's a
    location signal and we can ask for a parseable form (a Google
    Maps share link OR the 4-letter+4-digit national short code)
    without falling through to the brain's generic "أبشر، أنا هنا"
    fallback.

    Returns the standard short-circuit shape
    (``{"reply_text", "summary", "state_patch"}``) or ``None`` if
    the inbound isn't a map screenshot / the conversation has no
    active order.
    """
    if inbound_normalized_type != "image":
        return None
    md = inbound_metadata or {}
    if md.get("image_kind") != "map_screenshot":
        return None

    recent_inbound: list = []
    try:
        from core.conversation_engine import StateManager  # noqa: PLC0415

        _hist = StateManager.load_history(
            db, phone=phone, limit=10, tenant_id=tenant_id,
        )
        recent_inbound = [
            str(h.get("body") or "").strip()
            for h in (_hist or [])
            if str(h.get("direction") or "") == "inbound"
            and str(h.get("body") or "").strip()
        ][-5:]
    except Exception:  # noqa: BLE001
        recent_inbound = []

    try:
        from modules.ai.brain.state.support_listing_topic import (  # noqa: PLC0415
            detect_support_listing_from_image_metadata,
        )

        if detect_support_listing_from_image_metadata(md, recent_inbound):
            logger.info(
                "[ORDER_FLOW_STATE] map_screenshot skipped — support/listing "
                "context tenant=%s phone=*%s recent_preview=%r",
                tenant_id,
                (phone or "")[-4:],
                (recent_inbound[-1] if recent_inbound else "")[:80],
            )
            return None
    except Exception as _sl_exc:  # noqa: BLE001
        logger.debug(
            "[ORDER_FLOW_STATE] support/listing map guard skipped tenant=%s: %s",
            tenant_id, _sl_exc,
        )

    _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
    summary = _focus_summary(bs)

    has_active = bool(summary.get("selected_product"))
    receipt_received = bool(summary.get("payment_receipt_received"))
    awaiting_receipt = bool(summary.get("awaiting_payment_receipt"))

    # Only short-circuit when there's *something* the bot is doing
    # — otherwise a random map screenshot on a fresh conversation
    # would get an awkward "send me the link" reply with no context.
    if not (has_active or receipt_received or awaiting_receipt):
        return None

    # If the customer already provided a short address code OR a
    # Google Maps URL earlier, we don't need to nag again.
    has_location_proof = bool(
        (summary.get("short_address_code") or "").strip()
        or (summary.get("google_maps_url") or "").strip()
    )
    if has_location_proof:
        # Acknowledge softly so the customer doesn't think the bot
        # ignored their image, but don't overwrite stored location.
        reply_text = (
            "وصلتنا لقطة الخريطة، الموقع المسجَّل عندنا كافٍ للتوصيل بإذن الله. "
            "إذا تغيّر الموقع، أرسل لنا رابط قوقل ماب أو "
            "العنوان الوطني نصياً."
        )
        from core.reply_instruction import (  # noqa: PLC0415
            attach_instruction_to_decision,
            build_map_image_instruction,
        )

        return attach_instruction_to_decision(
            {
                "reply_text":  reply_text,
                "summary":     summary,
                "state_patch": {},
            },
            build_map_image_instruction(
                legacy_copy=reply_text,
                summary=summary,
            ),
        )

    reply_lines = [
        "وصلتنا لقطة الخريطة، شكراً 🌷",
        "بس عشان نضمن دقّة التوصيل نحتاج الموقع بصيغة قابلة للقراءة:",
        "• رابط قوقل ماب (Google Maps) — share location → copy link",
        "• أو العنوان الوطني (٤ أحرف + ٤ أرقام، مثل: RHRH1234)",
    ]
    state_patch: Dict[str, Any] = {
        "awaiting_location_text": True,
    }
    logger.info(
        "[ORDER_FLOW_STATE] map_screenshot short-circuit fired "
        "tenant=%s phone=*%s product=%r receipt_received=%s",
        tenant_id, (phone or "")[-4:],
        summary.get("selected_product"), receipt_received,
    )
    _map_reply = "\n".join(reply_lines)
    from core.reply_instruction import (  # noqa: PLC0415
        attach_instruction_to_decision,
        build_map_image_instruction,
    )

    return attach_instruction_to_decision(
        {
            "reply_text":  _map_reply,
            "summary":     summary,
            "state_patch": state_patch,
        },
        build_map_image_instruction(
            legacy_copy=_map_reply,
            summary=summary,
        ),
    )


def maybe_handle_wa_address_inbound(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    inbound_normalized_type: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> Optional[Dict[str, Any]]:
    """Ingest WhatsApp location pins or accepted map URLs into order_prep.

    Never handles payment receipts or payment claims. Returns the standard
    short-circuit dict or ``None`` when the inbound is not address data.
    """
    from core.wa_address_ingestion import (  # noqa: PLC0415
        compose_address_reply,
        is_accepted_maps_url,
        is_bare_short_address_code,
        resolve_address_state_patch,
    )

    if inbound_normalized_type == "location":
        pass
    elif inbound_normalized_type == "text" and (
        is_accepted_maps_url(inbound_text)
        or is_bare_short_address_code(inbound_text)
    ):
        pass
    else:
        return None

    address_patch = resolve_address_state_patch(
        inbound_normalized_type=inbound_normalized_type,
        inbound_metadata=inbound_metadata,
        inbound_text=inbound_text,
    )
    if not address_patch:
        return None

    _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
    if _conv is None:
        return None

    op = dict(bs.get("order_prep") or bs.get("order_preparation") or {})
    summary = _focus_summary(bs)
    has_active = bool(
        summary.get("selected_product")
        or op.get("line_items")
        or bs.get("cart_items")
    )
    if not has_active:
        try:
            from modules.ai.brain.commerce.gift_order_gate import (  # noqa: PLC0415
                build_pending_delivery_location_patch,
            )

            stash = build_pending_delivery_location_patch(address_patch)
            bs["pending_delivery_location"] = stash.get("pending_delivery_location") or {}
            op.update(stash)
            meta = dict(getattr(_conv, "extra_metadata", None) or {})
            meta["brain_state"] = {**bs, "order_prep": op}
            _conv.extra_metadata = meta
            try:
                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

                flag_modified(_conv, "extra_metadata")
            except Exception:  # noqa: BLE001
                pass
            db.flush()
            logger.info(
                "[ORDER_FLOW_STATE] stashed pending_delivery_location (pre-order) "
                "tenant=%s phone=*%s type=%s",
                tenant_id,
                (phone or "")[-4:],
                inbound_normalized_type,
            )
        except Exception as _stash_exc:  # noqa: BLE001
            logger.warning(
                "[ORDER_FLOW_STATE] pending_delivery_location stash failed: %s",
                _stash_exc,
            )
        logger.info(
            "[ORDER_FLOW_STATE] address inbound ignored — no active order "
            "tenant=%s phone=*%s type=%s",
            tenant_id, (phone or "")[-4:], inbound_normalized_type,
        )
        return None

    line_items = list(op.get("line_items") or bs.get("cart_items") or [])
    from core.merchant_payment_methods import load_merchant_payment_methods  # noqa: PLC0415

    payment_methods = load_merchant_payment_methods(db, tenant_id)
    reply_text = compose_address_reply(
        order_prep=op,
        brain_state=bs,
        line_items=line_items,
        payment_methods=payment_methods,
    )
    state_patch = {
        **address_patch,
        "awaiting_location_text": False,
    }
    logger.info(
        "[ORDER_FLOW_STATE] address short-circuit fired tenant=%s phone=*%s "
        "type=%s address_type=%s",
        tenant_id,
        (phone or "")[-4:],
        inbound_normalized_type,
        address_patch.get("delivery_address_type"),
    )
    from core.reply_instruction import (  # noqa: PLC0415
        attach_instruction_to_decision,
        build_address_instruction,
    )

    return attach_instruction_to_decision(
        {
            "reply_text":  reply_text,
            "summary":     summary,
            "state_patch": state_patch,
            "deterministic_path": "address_ingest_ack",
        },
        build_address_instruction(
            legacy_copy=reply_text,
            summary=summary,
            address_type=str(address_patch.get("delivery_address_type") or ""),
            inbound_text=str(inbound_text or ""),
        ),
    )


def maybe_handle_payment_method_selection_inbound(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    inbound_text: str,
) -> Optional[Dict[str, Any]]:
    """Validate and persist customer payment method choice for WA checkout."""
    from core.merchant_payment_methods import (  # noqa: PLC0415
        build_payment_method_state_patch,
        load_merchant_payment_methods,
        resolve_indexed_choice,
        validate_payment_method_choice,
    )
    from core.order_payment_policy import (  # noqa: PLC0415
        PAYMENT_METHOD_BANK_TRANSFER,
        PAYMENT_METHOD_CASH_ON_DELIVERY,
        PAYMENT_METHOD_MOYASAR,
    )
    from core.wa_order_lifecycle import compute_wa_missing_fields  # noqa: PLC0415

    text = str(inbound_text or "").strip()
    if not text or len(text) > 120:
        return None

    methods = load_merchant_payment_methods(db, tenant_id)
    chosen = resolve_indexed_choice(text, methods)
    if not chosen:
        return None

    _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
    if _conv is None:
        return None

    op = dict(bs.get("order_prep") or bs.get("order_preparation") or {})
    summary = _focus_summary(bs)
    line_items = list(op.get("line_items") or bs.get("cart_items") or [])
    missing = compute_wa_missing_fields(op, brain_state=bs, line_items=line_items)
    if missing:
        return None

    rejection = validate_payment_method_choice(chosen, methods)
    if rejection:
        return {
            "reply_text":  rejection,
            "summary":     summary,
            "state_patch": {},
        }

    state_patch = build_payment_method_state_patch(chosen)
    if chosen == PAYMENT_METHOD_BANK_TRANSFER:
        reply_text = (
            "تم اختيار التحويل البنكي ✅\n"
            "بعد التحويل، أرسلي صورة الإيصال أو إثبات الدفع."
        )
    elif chosen == PAYMENT_METHOD_CASH_ON_DELIVERY:
        reply_text = "تم اختيار الدفع عند الاستلام ✅"
    elif chosen == PAYMENT_METHOD_MOYASAR:
        reply_text = (
            "تم اختيار الدفع الإلكتروني ✅\n"
            "سنرسل لك رابط الدفع قريباً."
        )
    else:
        reply_text = "تم تسجيل طريقة الدفع ✅"

    logger.info(
        "[ORDER_FLOW_STATE] payment_method short-circuit tenant=%s phone=*%s method=%s",
        tenant_id,
        (phone or "")[-4:],
        chosen,
    )
    from core.reply_instruction import (  # noqa: PLC0415
        attach_instruction_to_decision,
        build_payment_method_instruction,
    )

    return attach_instruction_to_decision(
        {
            "reply_text":  reply_text,
            "summary":     summary,
            "state_patch": state_patch,
            "deterministic_path": "payment_method_ack",
        },
        build_payment_method_instruction(
            legacy_copy=reply_text,
            payment_method=str(chosen or ""),
            summary=summary,
            inbound_text=text,
        ),
    )


def _name_field_looks_like_phone(text: str) -> bool:
    digits = str(text or "").lstrip("+").replace(" ", "").replace("-", "")
    return bool(digits) and digits.isdigit() and len(digits) >= 7


def _protect_customer_name_patch(
    existing_op: Dict[str, Any],
    state_patch: Dict[str, Any],
) -> Dict[str, Any]:
    """Drop empty/phone-like name patches that would erase a captured name."""
    if not state_patch:
        return state_patch
    filtered = dict(state_patch)
    for field in ("customer_first_name", "customer_last_name"):
        if field not in filtered:
            continue
        incoming = str(filtered.get(field) or "").strip()
        existing = str(existing_op.get(field) or "").strip()
        if existing and not _name_field_looks_like_phone(existing):
            if not incoming or _name_field_looks_like_phone(incoming):
                logger.info(
                    "[ORDER_NAME_PATCH] blocked overwrite field=%s existing=%r "
                    "incoming=%r source=apply_state_patch",
                    field,
                    existing,
                    incoming,
                )
                del filtered[field]
    return filtered


def apply_state_patch(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    state_patch: Dict[str, Any],
) -> bool:
    """Persist a state mutation onto
    ``Conversation.extra_metadata['brain_state']['order_prep']``.
    The shape mirrors :class:`OrderPreparationState.to_dict`. Returns
    True on success, False on any failure (never raises)."""
    if not state_patch:
        return False
    try:
        from models import Conversation, Customer  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        e164 = _normalize_e164(phone) or phone
        conv = _find_conversation_by_phone(
            db, tenant_id=int(tenant_id), phones=(e164, phone),
            Conversation=Conversation, Customer=Customer,
        )
        if conv is None:
            logger.info(
                "[ORDER_FLOW_STATE] apply_state_patch: no conversation "
                "row tenant=%s phone=*%s",
                tenant_id, phone[-4:] if phone else "",
            )
            return False
        meta = dict(conv.extra_metadata or {})
        bs = dict(meta.get("brain_state") or {})
        op = dict(bs.get("order_prep") or bs.get("order_preparation") or {})
        state_patch = _protect_customer_name_patch(op, state_patch)
        if not state_patch:
            return False
        before = {k: op.get(k) for k in state_patch.keys()}
        op.update(state_patch)
        bs["order_prep"] = op
        meta["brain_state"] = bs
        try:
            from core.active_order_context import maybe_persist_from_patch  # noqa: PLC0415

            maybe_persist_from_patch(
                meta,
                brain_state=bs,
                order_prep=op,
                state_patch=state_patch,
            )
        except Exception as _aoc_exc:  # noqa: BLE001
            logger.warning(
                "[ACTIVE_ORDER_CONTEXT] persist hook failed tenant=%s: %s",
                tenant_id, _aoc_exc,
            )
        conv.extra_metadata = meta
        try:
            flag_modified(conv, "extra_metadata")
        except Exception:
            pass
        # ── Paid-order signal ────────────────────────────────────────
        # Stamp the dedicated column only when payment is explicitly
        # confirmed — not when the customer merely submitted a receipt
        # (``payment_receipt_received`` → payment_submitted path).
        _payment_confirmed = (
            state_patch.get("payment_confirmed") is True
            or state_patch.get("verified_by_staff") is True
            or state_patch.get("payment_verified") is True
        )
        if _payment_confirmed:
            try:
                if getattr(conv, "last_payment_confirmed_at", None) is None:
                    conv.last_payment_confirmed_at = datetime.now(timezone.utc)
            except Exception:
                pass
        # Phase 1+2 — sync draft/paid Nahla order (guarded; never blocks ACK).
        try:
            from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: PLC0415
            sync_nahla_wa_order(
                db,
                tenant_id=int(tenant_id),
                conversation=conv,
                brain_state=bs,
                order_prep=op,
                trigger="state_patch",
            )
        except Exception as _bridge_exc:  # noqa: BLE001
            logger.warning(
                "[NAHLA_ORDER_BRIDGE] hook failed tenant=%s conv=%s: %s",
                tenant_id, getattr(conv, "id", None), _bridge_exc,
            )
        db.add(conv)
        db.commit()
        logger.info(
            "[ORDER_FLOW_STATE] state_patch_applied tenant=%s phone=*%s "
            "before=%s after=%s",
            tenant_id, phone[-4:] if phone else "",
            before, {k: op.get(k) for k in state_patch.keys()},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ORDER_FLOW_STATE] apply_state_patch failed: %s", exc,
        )
        try:
            db.rollback()
        except Exception:  # noqa: silent-ok — rollback best-effort after apply_state_patch failure
            pass
        return False


def persist_checkout_location_outcome(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    state_patch: Optional[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Persist a location/address checkout patch and return ``(ok, reason)``.

    Callers must not claim the location was saved unless ``ok`` is True.
    """
    patch = dict(state_patch or {})
    if not patch:
        logger.info(
            "[ORDER_FLOW_STATE] location persist skipped empty_patch tenant=%s",
            tenant_id,
        )
        return False, "empty_patch"
    try:
        ok = bool(
            apply_state_patch(
                db,
                tenant_id=int(tenant_id),
                phone=phone or "",
                state_patch=patch,
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[ORDER_FLOW_STATE] location persist exception tenant=%s phone=*%s",
            tenant_id,
            (phone or "")[-4:],
        )
        return False, "apply_state_patch_exception"
    if ok:
        return True, "persisted"
    logger.warning(
        "[ORDER_FLOW_STATE] location persist failed tenant=%s phone=*%s "
        "— not claiming saved",
        tenant_id,
        (phone or "")[-4:],
    )
    return False, "apply_state_patch_false"


def persist_checkout_location_patch(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    state_patch: Optional[Dict[str, Any]],
) -> bool:
    """Persist a location/address checkout patch.

    Callers must not claim the location was saved unless this returns True.
    """
    ok, _reason = persist_checkout_location_outcome(
        db,
        tenant_id=tenant_id,
        phone=phone,
        state_patch=state_patch,
    )
    return ok


def mark_awaiting_receipt(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    conversation_id: Optional[int] = None,
) -> bool:
    """Convenience wrapper for setting
    ``awaiting_payment_receipt=True`` after the bot's outbound
    asked for a receipt. The webhook calls this from inside
    ``_handle_merchant_message`` right after a successful send.

    Tenant 33 #50 (Wave 1, W1.1 — contradiction guard):
    when ``PAYMENT_CONTRADICTION_GUARD_ENABLED`` is on AND the
    persisted ``order_prep`` already records a recent
    ``payment_receipt_received=True``, refuse the flip. This closes
    the false-match where the bot's own ACK ("وصلنا إيصال التحويل
    ...") triggers ``detect_awaiting_receipt_in_reply`` against its
    own keyword list — which would otherwise produce the production
    complaint of "got the receipt" + "send the receipt" inside the
    same beat. The guard never raises and never blocks an
    apply_state_patch failure cascade; with the flag off, behaviour
    is byte-identical to the pre-guard implementation.

    Returns ``True`` when ``apply_state_patch`` actually ran and
    succeeded; ``False`` either because the patch failed OR because
    the guard refused the flip (the caller's structured log already
    distinguishes via the ``[PAYMENT_CONTRADICTION_GUARD]`` line).
    """
    if _payment_contradiction_guard_enabled():
        try:
            _conv, bs = _load_brain_state(
                db, tenant_id=tenant_id, phone=phone,
            )
            op = (
                bs.get("order_prep") or bs.get("order_preparation") or {}
            ) if isinstance(bs, dict) else {}
            received = bool(op.get("payment_receipt_received"))
            received_at = str(op.get("payment_receipt_at") or "").strip()
            if received and _receipt_received_recently(received_at):
                _conv_id = (
                    conversation_id
                    if conversation_id is not None
                    else getattr(_conv, "id", None)
                )
                logger.info(
                    "[PAYMENT_CONTRADICTION_GUARD] "
                    "decision=block_awaiting_flip "
                    "reason=recent_receipt_received_blocks_awaiting_flip "
                    "tenant_id=%s conversation_id=%s phone=*%s "
                    "payment_receipt_at=%s window_secs=%s",
                    tenant_id, _conv_id,
                    phone[-4:] if phone else "",
                    received_at or "<missing>",
                    _PAYMENT_CONTRADICTION_GUARD_RECENT_RECEIPT_WINDOW_SECS,
                )
                return False
        except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — guard failure must not block flip
            # Defensive: never let the guard's own failure mode
            # block the legitimate flip. The legacy code path runs
            # below.
            logger.debug(
                "[PAYMENT_CONTRADICTION_GUARD] inspection failed "
                "(falling through to legacy flip): %s", exc,
            )
    return apply_state_patch(
        db,
        tenant_id=tenant_id,
        phone=phone,
        state_patch={
            "awaiting_payment_receipt": True,
            "order_status":             "awaiting_receipt",
        },
    )


def context_aware_dedup_fallback(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    history: List[Any],
    default_fallback: str,
    inbound_text: Optional[str] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    normalized_type: Optional[str] = None,
) -> str:
    """Return a context-relevant fallback when the brain's reply
    tripped the near-duplicate guard. Without this, the merchant
    would have seen the generic "أنا هنا — قول وش تحتاج وأكمل معك"
    line MID-FUNNEL — which is exactly the bug we are fixing.

    Priority:
        1. Receipt already received → "طلبك تحت المراجعة الآن".
        2. Active order with product + price → contextual nudge.
        3. Awaiting receipt → re-prompt only when payment workflow
           resume gate allows the payment flow to continue.
        4. Empty / discovery state → the original ``default_fallback``.

    Never raises. Returns empty string when no operational substitute exists
    (P1-D-1: no personality / CS canned ``default_fallback``).
    """
    try:
        _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        s = _focus_summary(bs)

        from core.dedup_order_state_gate import (  # noqa: PLC0415
            log_dedup_state_mismatch,
            should_suppress_dedup_order_templates,
        )

        _suppress, _suppress_reason = should_suppress_dedup_order_templates(
            message=inbound_text or "",
            summary=s,
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
        )

        if s.get("payment_receipt_received"):
            if _suppress:
                log_dedup_state_mismatch(
                    tenant_id=tenant_id,
                    phone_tail=(phone[-4:] if phone else ""),
                    reason=_suppress_reason,
                    inbound_preview=inbound_text or "",
                    blocked_template="payment_receipt_under_review",
                    payment_receipt_received=True,
                )
            else:
                prod = s.get("selected_product")
                if prod:
                    return (
                        f"طلبك ({prod}) تحت المراجعة الآن — "
                        "بنتواصل معك أول ما يتجهز للشحن بإذن الله. 🌷"
                    )
                return (
                    "طلبك تحت المراجعة الآن — بنتواصل معك أول ما يتجهز "
                    "للشحن بإذن الله. 🌷"
                )

        if s.get("awaiting_payment_receipt"):
            _payment_resume = True
            try:
                from core.payment_relevance_gate import (  # noqa: PLC0415
                    PaymentRelevanceLogContext,
                    validate_payment_workflow_resume,
                )
                _prv = validate_payment_workflow_resume(
                    message=inbound_text or "",
                    inbound_metadata=inbound_metadata,
                    normalized_type=normalized_type,
                    state_summary=s,
                    history=history,
                    tenant_id=tenant_id,
                    route="dedup_fallback",
                    log_context=PaymentRelevanceLogContext(
                        tenant_id=tenant_id,
                        phone_tail=(phone[-4:] if phone else ""),
                        message=inbound_text or "",
                        inbound_metadata=inbound_metadata,
                        normalized_type=normalized_type,
                        dedup=True,
                        fallback_source="dedup_fallback",
                        artifact=False,
                        final_action="dedup_payment_resume_check",
                    ),
                )
                if not _prv.allowed:
                    _payment_resume = False
            except Exception:  # noqa: BLE001
                if inbound_text:
                    try:
                        from modules.ai.brain.state.state_relevance import (  # noqa: PLC0415
                            log_state_resurrection_blocked,
                            should_block_workflow_resume,
                            validate_state_relevance_from_summary,
                        )
                        _verdict = validate_state_relevance_from_summary(
                            message=inbound_text,
                            summary=s,
                        )
                        if should_block_workflow_resume("payment_flow", _verdict):
                            log_state_resurrection_blocked(
                                tenant_id=tenant_id,
                                blocked_state="payment_flow",
                                reason="no_payment_semantics",
                                preview=inbound_text[:80],
                                intent_hint=_verdict.current_intent_hint,
                            )
                            _payment_resume = False
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — payment resume probe best-effort
                        pass

            if _payment_resume:
                if inbound_text:
                    try:
                        from core.payment_intent import (  # noqa: PLC0415
                            is_post_shipment_delivery_confirmation,
                        )
                        if is_post_shipment_delivery_confirmation(
                            db,
                            tenant_id=tenant_id,
                            phone=phone,
                            inbound_text=inbound_text,
                        ):
                            logger.info(
                                "[ORDER_FLOW_STATE] suppressing receipt re-prompt "
                                "— post-shipment delivery confirmation detected "
                                "tenant=%s phone=*%s",
                                tenant_id, (phone or "")[-4:],
                            )
                        else:
                            return (
                                "أنا بانتظار إيصال التحويل بإذنك — أرسله هنا "
                                "(صورة أو PDF) وأكمل لك الطلب فوراً. 🌷"
                            )
                    except Exception:
                        return (
                            "أنا بانتظار إيصال التحويل بإذنك — أرسله هنا "
                            "(صورة أو PDF) وأكمل لك الطلب فوراً. 🌷"
                        )
                else:
                    return (
                        "أنا بانتظار إيصال التحويل بإذنك — أرسله هنا "
                        "(صورة أو PDF) وأكمل لك الطلب فوراً. 🌷"
                    )

        if s.get("selected_product") and s.get("price") is not None:
            if _suppress:
                log_dedup_state_mismatch(
                    tenant_id=tenant_id,
                    phone_tail=(phone[-4:] if phone else ""),
                    reason=_suppress_reason,
                    inbound_preview=inbound_text or "",
                    blocked_template="active_order_nudge_with_price",
                    payment_receipt_received=bool(s.get("payment_receipt_received")),
                )
            else:
                price_str = _format_price(s["price"], s.get("currency") or "SAR")
                base = f"طلبك الحالي: {s['selected_product']}"
                if price_str:
                    base += f" بسعر {price_str}"
                base += ". تأمر بشيء أكمّل لك فيه؟"
                return base

        if s.get("selected_product"):
            if _suppress:
                log_dedup_state_mismatch(
                    tenant_id=tenant_id,
                    phone_tail=(phone[-4:] if phone else ""),
                    reason=_suppress_reason,
                    inbound_preview=inbound_text or "",
                    blocked_template="active_order_nudge",
                    payment_receipt_received=bool(s.get("payment_receipt_received")),
                )
            else:
                return (
                    f"طلبك الحالي: {s['selected_product']}. "
                    "تأمر بشيء أكمّل لك فيه؟"
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ORDER_FLOW_STATE] context_aware_dedup_fallback failed: %s",
            exc,
        )

    return default_fallback or ""
