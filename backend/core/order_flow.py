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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.order_flow")


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


def _load_brain_state(
    db: Any, *, tenant_id: int, phone: str,
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """Return ``(conversation_row, brain_state_dict)``. Both may be
    empty when the conversation hasn't been touched yet. Never
    raises."""
    if db is None or not tenant_id or not phone:
        return None, {}
    try:
        from models import Conversation  # noqa: PLC0415
        from core.phone import normalize_phone_e164  # noqa: PLC0415
        e164 = normalize_phone_e164(phone) or phone
        conv = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == int(tenant_id),
                Conversation.customer_phone == e164,
            )
            .order_by(Conversation.id.desc())
            .first()
        )
        if conv is None:
            # Some installs key conversations by raw (non-normalised)
            # phone too. Try the raw value as a last resort.
            conv = (
                db.query(Conversation)
                .filter(
                    Conversation.tenant_id == int(tenant_id),
                    Conversation.customer_phone == phone,
                )
                .order_by(Conversation.id.desc())
                .first()
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


def _focus_summary(brain_state: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a flat summary of the order focus from the persisted
    brain state. Returns empty fields when nothing is known. Used by
    both ``build_order_context`` and the deterministic receipt
    acknowledgement so they speak the same shape."""
    focus = brain_state.get("current_product_focus")
    if not isinstance(focus, dict):
        focus = {}
    op = brain_state.get("order_prep") or brain_state.get("order_preparation") or {}
    if not isinstance(op, dict):
        op = {}
    return {
        "selected_product":      str(focus.get("title") or focus.get("name") or ""),
        "selected_product_id":   str(focus.get("id") or op.get("product_id") or ""),
        "price":                 focus.get("price"),
        "currency":              str(focus.get("currency") or "SAR"),
        "city":                  str(op.get("city") or ""),
        "short_address_code":    str(op.get("short_address_code") or ""),
        "google_maps_url":       str(op.get("google_maps_url") or ""),
        "customer_first_name":   str(op.get("customer_first_name") or ""),
        "customer_last_name":    str(op.get("customer_last_name") or ""),
        "missing_fields":        list(op.get("missing_fields") or []),
        "awaiting_payment_receipt": bool(op.get("awaiting_payment_receipt")),
        "payment_receipt_received": bool(op.get("payment_receipt_received")),
        "order_status":          str(op.get("order_status") or ""),
        "payment_method":        str(
            brain_state.get("payment_method") or op.get("payment_method") or ""
        ),
    }


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


def _compose_receipt_ack(summary: Dict[str, Any]) -> str:
    """Build the deterministic "we received your transfer receipt"
    Arabic reply. The reply surfaces the product + price + national
    address so the customer can immediately confirm what's being
    processed — closes the "did the bot lose my context?" anxiety
    that motivated this whole change."""
    lines: List[str] = [
        "وصلنا إيصال التحويل، شكراً لك 🌷",
        "تم استلام الطلب وسيتم مراجعته وتجهيزه بإذن الله.",
    ]
    detail_bits: List[str] = []
    if summary.get("selected_product"):
        prod = summary["selected_product"]
        price_str = _format_price(summary.get("price"), summary.get("currency") or "SAR")
        if price_str:
            detail_bits.append(f"الطلب: {prod} ({price_str})")
        else:
            detail_bits.append(f"الطلب: {prod}")
    if summary.get("short_address_code"):
        detail_bits.append(f"العنوان الوطني: {summary['short_address_code']}")
    elif summary.get("city"):
        detail_bits.append(f"المدينة: {summary['city']}")
    if detail_bits:
        lines.append("")
        lines.extend(detail_bits)
    return "\n".join(lines)


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
    if inbound_normalized_type not in ("document", "image"):
        return None

    # The PDF classifier or the image vision check sets these slots.
    kind = (inbound_metadata or {}).get("pdf_kind") \
        or (inbound_metadata or {}).get("image_kind")
    if kind != "payment_receipt":
        return None

    # Universal payment-evidence gate.
    pe_status = (inbound_metadata or {}).get("payment_evidence_status")
    if pe_status and pe_status != "confirmed":
        logger.info(
            "[PAYMENT_EVIDENCE] receipt short-circuit blocked tenant=%s "
            "phone=*%s pdf_kind=%s payment_evidence_status=%s "
            "payment_evidence_reason=%s",
            tenant_id, (phone[-4:] if phone else ""), kind,
            pe_status,
            (inbound_metadata or {}).get("payment_evidence_reason"),
        )
        return None

    _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
    summary = _focus_summary(bs)

    # Guard: we only short-circuit when the conversation has SOME
    # evidence of an active order. Otherwise a random PDF that
    # happens to be classified as a receipt could trigger an
    # acknowledgement for an order that doesn't exist.
    has_active = bool(summary.get("selected_product"))
    awaiting   = bool(summary.get("awaiting_payment_receipt"))
    if not (has_active or awaiting):
        logger.info(
            "[ORDER_FLOW_STATE] receipt arrived but no active order — "
            "letting brain handle | tenant=%s phone=*%s",
            tenant_id, phone[-4:] if phone else "",
        )
        return None

    state_patch: Dict[str, Any] = {
        "awaiting_payment_receipt": False,
        "payment_receipt_received": True,
        "payment_receipt_at":       datetime.now(timezone.utc).isoformat(),
        "order_status":             "under_review",
        "payment_receipt_metadata": {
            "kind":             kind,
            "confidence":       (inbound_metadata or {}).get("pdf_kind_confidence")
                                 or (inbound_metadata or {}).get("image_kind_confidence"),
            "wa_message_id":    (inbound_metadata or {}).get("wa_message_id"),
            "filename":         (inbound_metadata or {}).get("filename"),
            "mime_type":        (inbound_metadata or {}).get("mime_type"),
            "storage_url":      (inbound_metadata or {}).get("storage_url"),
            "storage_sha256":   (inbound_metadata or {}).get("storage_sha256"),
            "received_at":      datetime.now(timezone.utc).isoformat(),
        },
    }

    reply_text = _compose_receipt_ack(summary)

    logger.info(
        "[ORDER_FLOW_STATE] receipt acknowledged tenant=%s phone=*%s "
        "kind=%s product=%r price=%s address_code=%s awaiting_was=%s",
        tenant_id, phone[-4:] if phone else "", kind,
        summary.get("selected_product"),
        summary.get("price"),
        summary.get("short_address_code"),
        awaiting,
    )
    return {
        "reply_text":  reply_text,
        "summary":     summary,
        "state_patch": state_patch,
    }


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
    ):
        return None

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

    _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
    summary = _focus_summary(bs)
    awaiting = bool(summary.get("awaiting_payment_receipt"))

    reply_text = compose_payment_evidence_reply(
        pe_status,
        awaiting_receipt=awaiting,
    )
    if not reply_text:
        return None

    logger.info(
        "[PAYMENT_EVIDENCE] short_circuit=payment_evidence_soft "
        "tenant=%s phone=*%s payment_evidence_status=%s "
        "payment_evidence_reason=%s kind=%s awaiting=%s product=%r",
        tenant_id, (phone[-4:] if phone else ""), pe_status,
        md.get("payment_evidence_reason"), kind, awaiting,
        summary.get("selected_product"),
    )
    return {
        "reply_text":  reply_text,
        "summary":     summary,
        # Empty state_patch — no order-status mutation, no
        # awaiting_payment_receipt flip. This branch is informational
        # only; the customer hasn't completed anything yet.
        "state_patch": {},
    }


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
        from models import Conversation  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        from core.phone import normalize_phone_e164  # noqa: PLC0415
        e164 = normalize_phone_e164(phone) or phone
        conv = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == int(tenant_id),
                Conversation.customer_phone == e164,
            )
            .order_by(Conversation.id.desc())
            .first()
        )
        if conv is None:
            conv = (
                db.query(Conversation)
                .filter(
                    Conversation.tenant_id == int(tenant_id),
                    Conversation.customer_phone == phone,
                )
                .order_by(Conversation.id.desc())
                .first()
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
        before = {k: op.get(k) for k in state_patch.keys()}
        op.update(state_patch)
        bs["order_prep"] = op
        meta["brain_state"] = bs
        conv.extra_metadata = meta
        try:
            flag_modified(conv, "extra_metadata")
        except Exception:
            pass
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
        except Exception:
            pass
        return False


def mark_awaiting_receipt(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
) -> bool:
    """Convenience wrapper for setting
    ``awaiting_payment_receipt=True`` after the bot's outbound
    asked for a receipt. The webhook calls this from inside
    ``_handle_merchant_message`` right after a successful send."""
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
) -> str:
    """Return a context-relevant fallback when the brain's reply
    tripped the near-duplicate guard. Without this, the merchant
    would have seen the generic "أنا هنا — قول وش تحتاج وأكمل معك"
    line MID-FUNNEL — which is exactly the bug we are fixing.

    Priority:
        1. Receipt already received → "طلبك تحت المراجعة الآن".
        2. Active order with product + price → contextual nudge.
        3. Awaiting receipt → re-prompt for the receipt nicely.
        4. Empty / discovery state → the original ``default_fallback``.

    Never raises. Always returns a non-empty string.
    """
    try:
        _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        s = _focus_summary(bs)

        if s.get("payment_receipt_received"):
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
            return (
                "أنا بانتظار إيصال التحويل بإذنك — أرسله هنا "
                "(صورة أو PDF) وأكمل لك الطلب فوراً. 🌷"
            )

        if s.get("selected_product") and s.get("price") is not None:
            price_str = _format_price(s["price"], s.get("currency") or "SAR")
            base = f"طلبك الحالي: {s['selected_product']}"
            if price_str:
                base += f" بسعر {price_str}"
            base += ". تأمر بشيء أكمّل لك فيه؟"
            return base

        if s.get("selected_product"):
            return (
                f"طلبك الحالي: {s['selected_product']}. "
                "تأمر بشيء أكمّل لك فيه؟"
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ORDER_FLOW_STATE] context_aware_dedup_fallback failed: %s",
            exc,
        )

    return default_fallback or "تأمر بشيء أكمّل لك فيه؟"
