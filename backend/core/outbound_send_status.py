"""
backend/core/outbound_send_status.py
─────────────────────────────────────
Bridge between the WhatsApp wire layer (``services.whatsapp_platform.service``)
and the persisted ``MessageEvent`` rows that drive the conversation
inbox.

Why this module exists
──────────────────────
Every AI / brain / loop-guard / identity / support-escalation / manual
reply path in ``backend/routers/whatsapp_webhook.py`` AND
``backend/routers/conversations.py::reply_to_conversation`` writes an
outbound ``MessageEvent`` row to the database **before** the provider
POST happens. The dashboard reads those rows verbatim and renders
every outbound bubble with a green double-check icon, no matter what
Meta / 360dialog actually did with the bytes.

When the provider POST fails (HTTP non-2xx, ``error`` envelope, missing
``messages[0].id`` — the F18 "silent" failure mode, or a transport
exception), the saved row is left in its pre-send "looks delivered"
state. The merchant sees the AI reply in the UI; the customer's
WhatsApp has nothing. This is the production bug we are closing.

What this module does
─────────────────────
``stamp_outbound_send_status`` finds the most recent outbound
``MessageEvent`` row for ``(tenant_id, recipient)`` that is still in
the ``queued`` state and writes the wire-layer result into
``extra_metadata.provider_send``:

  {
    "status":         "sent" | "failed",
    "classification": "ok" | "non_2xx" | "provider_error_field"
                      | "missing_wamid" | "exception",
    "wamid":          "wamid.HBgL..." | null,
    "operation":      "send_message" | "send_message_retry" | ...,
    "completed_at":   "2026-05-14T13:41:02.123456+00:00",
    "error": {
        "code":         100,
        "subcode":      33,
        "message":      "Invalid parameter",
        "type":         "GraphMethodException",
        "fbtrace_id":   "AaBbCc...",
        "key":          "out_of_24h_window",   # from meta_errors classifier
        "label_ar":     "خارج نافذة 24 ساعة",
        "is_recoverable": true,
        "advice_ar":   "أرسل قالب Meta معتمد ..."
    } | null
  }

The dashboard ``/conversations/messages/{phone}`` endpoint surfaces
these fields as ``sendStatus`` / ``sendError`` / ``wamid`` so the UI
can render a clock (queued) / double-check (sent) / red X with an
Arabic explanation (failed) instead of the misleading unconditional
double-check.

Why "most recent within a window" instead of an explicit row id
───────────────────────────────────────────────────────────────
``StateManager.save_message`` (called from dozens of locations in the
webhook) does not return the row id, and refactoring every caller
would be a high-risk diff in a 270 KB file. Instead the wire layer —
which always knows ``tenant_id`` and ``payload["to"]`` — walks back
through the latest outbound rows for this recipient and stamps the
one that is still in ``queued`` state. The lookup window is bounded
to 5 minutes so a stale, never-stamped row from a previous turn
cannot be accidentally overwritten by a later send.

Operational guarantees
──────────────────────
* The function NEVER raises — observability is best-effort; an
  unexpected DB failure here MUST NOT break a real send. We
  ``rollback()`` and return ``None``.
* The function uses a SAVEPOINT (``db.begin_nested``) so the caller's
  outer transaction is never corrupted by our writes.
* If no matching row is found (e.g. the send came from a path that
  doesn't persist a MessageEvent — a notification template, a
  campaign send, etc.) the function quietly returns ``None``.
* Idempotent: a second call for the same (tenant, recipient) with a
  different outcome overwrites the previous stamp. Callers that emit
  a retry-after-register sequence (``send_message`` →
  ``send_message_retry``) get the final outcome.

Cross-references
────────────────
* ``services.whatsapp_platform.service.provider_post_with_context``
  produces the classification we consume.
* ``services.meta_errors.classify_meta_error`` produces the Arabic
  label + advice we attach to ``error``.
* ``routers.admin_debug`` ``GET /admin/debug/last-provider-send``
  reads the F18 ring buffer; the field names here mirror that
  endpoint so the support engineer can cross-reference one row to
  one wire attempt.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.outbound_send_status")


# How far back we look for an outbound MessageEvent to stamp. A merchant
# reply is normally persisted in the same request that triggers the
# provider POST, so 5 minutes is comfortably above the worst-case
# latency between persist and send (marker resolution + media fetch +
# product card rendering can add up to ~30s on a slow tenant).
_STAMP_LOOKUP_WINDOW = timedelta(minutes=5)

# Status constants — mirror the strings the wire layer + UI use.
STATUS_QUEUED = "queued"
STATUS_SENT   = "sent"
STATUS_FAILED = "failed"

# Classification constants from `core.wa_provider_observability`. We
# re-export them here so callers don't have to pull both modules.
CLASSIFICATION_OK              = "ok"
CLASSIFICATION_NON_2XX         = "non_2xx"
CLASSIFICATION_PROVIDER_ERROR  = "provider_error_field"
CLASSIFICATION_MISSING_WAMID   = "missing_wamid"
CLASSIFICATION_EXCEPTION       = "exception"


def _digits_only(value: Any) -> str:
    """Strip every non-digit so we can match phones that vary by
    formatting (``+966...`` vs ``966...`` vs ``00966...``). Returns
    an empty string for ``None`` / non-stringable inputs.
    """
    if value is None:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())


def _phone_suffix(value: Any) -> str:
    """Last 9 digits of a phone. WhatsApp message ids never need more
    than 9-digit suffix matching to disambiguate within a tenant —
    even the largest country codes share <=3 digits so the unique
    portion of any phone fits in the suffix. Empty string when the
    input is empty.
    """
    d = _digits_only(value)
    return d[-9:] if len(d) >= 9 else d


def _extract_meta_error(
    response_body: Any,
) -> Dict[str, Optional[str]]:
    """Pull ``error.code / .error_subcode / .message / .type /
    .fbtrace_id`` out of a Meta / 360dialog error envelope. Tolerates
    any shape: missing keys, non-dict bodies, lists, ``None``. Always
    returns the same key set so callers don't need ``.get`` chains.
    """
    out: Dict[str, Optional[str]] = {
        "code":       None,
        "subcode":    None,
        "message":    None,
        "type":       None,
        "fbtrace_id": None,
    }
    if not isinstance(response_body, dict):
        return out
    err = response_body.get("error")
    if not isinstance(err, dict):
        return out
    # Meta uses both ``error_subcode`` AND ``error_data.details``;
    # we capture the first one. 360dialog occasionally uses ``subcode``
    # directly — handle both.
    out["code"]       = err.get("code")
    out["subcode"]    = err.get("error_subcode") or err.get("subcode")
    out["message"]    = err.get("message") or err.get("details") or err.get("type")
    out["type"]       = err.get("type")
    out["fbtrace_id"] = err.get("fbtrace_id")
    return out


def _classify_with_meta_errors(
    *,
    classification: str,
    response_body: Any,
    error_text: Optional[str],
) -> Dict[str, Any]:
    """Run the wire-layer error through ``services.meta_errors`` so
    the UI receives an Arabic, actionable label without having to
    look up codes itself. Returns ``{}`` for ``ok`` so we don't
    pollute successful rows with an ``error`` block.
    """
    if classification == CLASSIFICATION_OK:
        return {}
    meta_err = _extract_meta_error(response_body)
    out: Dict[str, Any] = {
        "code":       meta_err.get("code"),
        "subcode":    meta_err.get("subcode"),
        "message":    meta_err.get("message"),
        "type":       meta_err.get("type"),
        "fbtrace_id": meta_err.get("fbtrace_id"),
    }
    # When the classification is `exception` we won't have a Meta
    # error body — surface the transport error text so the UI still
    # has something concrete to render.
    if classification == CLASSIFICATION_EXCEPTION and error_text and not out["message"]:
        out["message"] = error_text
    # ── missing_wamid: synthesize a friendly merchant label ──────────
    # The provider returned 2xx but no message id. ``services.meta_errors``
    # doesn't have a numeric code to match on, so without this short-
    # circuit we'd fall through to "unknown". The label below mirrors
    # the rest of the registry's tone.
    if classification == CLASSIFICATION_MISSING_WAMID:
        out["key"]              = "missing_wamid"
        out["label_ar"]         = "لم يصدر معرّف الرسالة من المزود"
        out["severity"]         = "warning"
        out["is_recoverable"]   = True
        out["advice_ar"]        = (
            "حاول إرسال الرسالة مجدداً. إذا تكرر الأمر فقد يكون لدى "
            "مزود واتساب (Cloud API/360dialog) خلل مؤقت."
        )
        out["message"] = out.get("message") or error_text or (
            "provider returned 2xx but no message id"
        )
        return out
    try:
        from services.meta_errors import classify_meta_error  # noqa: PLC0415
        classified = classify_meta_error(
            code=out.get("code"),
            subcode=out.get("subcode"),
            error_type=out.get("type"),
            message=out.get("message"),
            raw_response=response_body,
        )
        out["key"]              = classified.key
        out["label_ar"]         = classified.label_ar
        out["severity"]         = classified.severity
        out["is_recoverable"]   = classified.is_recoverable
        out["advice_ar"]        = classified.advice_ar
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[outbound_send_status] meta_errors classification failed: %s",
            exc,
        )
        out["key"]      = "unknown"
        out["label_ar"] = "تعذّر تسليم الرسالة"
    return out


def build_provider_send_block(
    *,
    classification: str,
    response_body: Any,
    wamid: Optional[str],
    operation: str,
    error_text: Optional[str] = None,
    duration_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """Compose the ``provider_send`` extra_metadata block. Pure — no
    DB access — so it can be reused by:

      * ``stamp_outbound_send_status``         (this module)
      * ``record_outbound_message`` pre-stamp  (``conversations.py``)
      * tests
    """
    # ── ``ok`` without a wamid is NOT considered sent ─────────────────
    # Some providers (and a few 360dialog edge cases) return a 2xx
    # envelope without ``messages[0].id``. We refuse to claim
    # "delivered" without a wamid — the dashboard would render a
    # ✔✔ that the customer never saw. Downgrade to a structured
    # failure with key=missing_wamid so the merchant gets a clear
    # banner instead of a misleading green check.
    wamid_str = (wamid or "").strip() if isinstance(wamid, str) else (
        str(wamid).strip() if wamid is not None else ""
    )
    if classification == CLASSIFICATION_OK and not wamid_str:
        classification = CLASSIFICATION_MISSING_WAMID
        error_text = error_text or "provider returned 2xx but no message id"

    is_ok = (classification == CLASSIFICATION_OK)
    block: Dict[str, Any] = {
        "status":         STATUS_SENT if is_ok else STATUS_FAILED,
        "classification": classification,
        "operation":      operation,
        "wamid":          (wamid_str or None) if is_ok else None,
        "completed_at":   datetime.now(timezone.utc).isoformat(),
    }
    if duration_ms is not None:
        try:
            block["duration_ms"] = round(float(duration_ms), 1)
        except Exception:
            pass
    err = _classify_with_meta_errors(
        classification=classification,
        response_body=response_body,
        error_text=error_text,
    )
    if err:
        block["error"] = err
    return block


def build_queued_block(*, operation: str) -> Dict[str, Any]:
    """Initial ``provider_send`` block written when we persist the
    outbound row but haven't seen the wire-layer result yet. The
    dashboard renders this as a clock icon.
    """
    return {
        "status":      STATUS_QUEUED,
        "operation":   operation,
        "queued_at":   datetime.now(timezone.utc).isoformat(),
    }


def stamp_outbound_send_status(
    db: Any,
    *,
    tenant_id: Optional[int],
    recipient: str,
    classification: str,
    response_body: Any,
    wamid: Optional[str],
    operation: str,
    error_text: Optional[str] = None,
    duration_ms: Optional[float] = None,
) -> Optional[int]:
    """Find the most recent outbound MessageEvent for
    ``(tenant_id, recipient)`` within the last ``_STAMP_LOOKUP_WINDOW``
    and stamp ``extra_metadata.provider_send`` with the wire-layer
    result. Returns the stamped row id on success, ``None`` when no
    matching row was found (or on any DB error — never raises).

    Phone matching strategy
    ───────────────────────
    ``MessageEvent.extra_metadata["phone"]`` is the canonical key
    that ``StateManager.save_message`` writes. ``recipient`` may be
    a slightly different shape (``+966...`` vs ``966...``) depending
    on which caller assembled the payload. We use a JSONB suffix
    match in PostgreSQL so both shapes line up.
    """
    if db is None or not recipient:
        return None
    if tenant_id is None:
        return None
    suffix = _phone_suffix(recipient)
    if not suffix:
        return None

    cutoff = datetime.utcnow() - _STAMP_LOOKUP_WINDOW
    try:
        from models import MessageEvent  # noqa: PLC0415
        from sqlalchemy import or_, func  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        # We compare the suffix of either ``phone`` or ``customer_phone``
        # in metadata. JSONB ``->>`` returns text, so ``func.right``
        # gives us the last N characters cheaply.
        phone_text   = MessageEvent.extra_metadata["phone"].astext
        customer_txt = MessageEvent.extra_metadata["customer_phone"].astext
        # Filter to rows already in ``queued`` state. We deliberately
        # don't re-stamp rows already marked ``sent`` so a webhook
        # status callback (delivered / read) from Meta can't overwrite
        # the wire-layer outcome.
        status_text  = MessageEvent.extra_metadata["provider_send"]["status"].astext

        # NB: db.begin_nested() may not be supported when the outer
        # transaction is in a bad state. We try, and on failure we
        # fall back to a plain query without the SAVEPOINT — the
        # later db.flush() will still surface any real corruption.
        nested_ok = False
        try:
            db.begin_nested()
            nested_ok = True
        except Exception:
            nested_ok = False

        try:
            row = (
                db.query(MessageEvent)
                .filter(
                    MessageEvent.tenant_id == tenant_id,
                    func.lower(MessageEvent.direction) == "outbound",
                    MessageEvent.created_at >= cutoff,
                    or_(
                        func.right(phone_text, len(suffix)) == suffix,
                        func.right(customer_txt, len(suffix)) == suffix,
                    ),
                    or_(
                        status_text.is_(None),
                        status_text == STATUS_QUEUED,
                    ),
                )
                .order_by(MessageEvent.id.desc())
                .first()
            )
            if row is None:
                if nested_ok:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                logger.debug(
                    "[outbound_send_status] no candidate row to stamp "
                    "tenant=%s to=%s classification=%s",
                    tenant_id, recipient, classification,
                )
                return None

            block = build_provider_send_block(
                classification=classification,
                response_body=response_body,
                wamid=wamid,
                operation=operation,
                error_text=error_text,
                duration_ms=duration_ms,
            )

            meta = dict(row.extra_metadata or {})
            # Preserve any earlier queued_at so the UI can compute
            # latency = completed_at - queued_at.
            prev = meta.get("provider_send") or {}
            if isinstance(prev, dict) and prev.get("queued_at"):
                block["queued_at"] = prev["queued_at"]
            meta["provider_send"] = block
            row.extra_metadata = meta
            flag_modified(row, "extra_metadata")
            db.add(row)
            db.flush()
            db.commit()

            logger.info(
                "[outbound_send_status] stamped message_event=%s tenant=%s to=%s "
                "status=%s classification=%s wamid=%s error_key=%s",
                row.id, tenant_id, recipient, block["status"], classification,
                wamid[-8:] if wamid else None,
                (block.get("error") or {}).get("key"),
            )
            return int(row.id)
        except Exception as inner_exc:  # noqa: BLE001
            logger.warning(
                "[outbound_send_status] stamp failed tenant=%s to=%s err=%s",
                tenant_id, recipient, inner_exc,
            )
            try:
                db.rollback()
            except Exception:
                pass
            return None
    except Exception as outer_exc:  # noqa: BLE001
        # Import or query construction failed — never break the send
        # path over this.
        logger.warning(
            "[outbound_send_status] setup failed tenant=%s to=%s err=%s",
            tenant_id, recipient, outer_exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _find_queued_outbound_row(
    db: Any,
    *,
    tenant_id: int,
    recipient: str,
) -> Optional[Any]:
    """Lookup helper extracted so unit tests can stub it without
    having to fake the full SQLAlchemy column chain. Returns the
    most-recent ``queued`` outbound MessageEvent for
    ``(tenant_id, recipient)`` within ``_STAMP_LOOKUP_WINDOW``, or
    ``None`` on miss / any DB error.

    Never raises.
    """
    suffix = _phone_suffix(recipient)
    if not suffix:
        return None
    cutoff = datetime.utcnow() - _STAMP_LOOKUP_WINDOW
    try:
        from models import MessageEvent  # noqa: PLC0415
        from sqlalchemy import or_, func  # noqa: PLC0415

        phone_text   = MessageEvent.extra_metadata["phone"].astext
        customer_txt = MessageEvent.extra_metadata["customer_phone"].astext
        status_text  = MessageEvent.extra_metadata["provider_send"]["status"].astext
        return (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == tenant_id,
                func.lower(MessageEvent.direction) == "outbound",
                MessageEvent.created_at >= cutoff,
                or_(
                    func.right(phone_text, len(suffix)) == suffix,
                    func.right(customer_txt, len(suffix)) == suffix,
                ),
                or_(
                    status_text.is_(None),
                    status_text == STATUS_QUEUED,
                ),
            )
            .order_by(MessageEvent.id.desc())
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[outbound_send_status] _find_queued_outbound_row failed "
            "tenant=%s to=%s err=%s",
            tenant_id, recipient, exc,
        )
        return None


def sync_outbound_body_to_final(
    db: Any,
    *,
    tenant_id: Optional[int],
    recipient: str,
    final_body: str,
    reason: str = "post_safety_nets",
) -> Optional[int]:
    """Update the body of the most recent queued outbound MessageEvent
    for ``(tenant_id, recipient)`` so the dashboard sees what the
    customer actually receives — not the brain's raw pre-safety-net
    text.

    Why this exists (May 2026 #32 — Tenant 33 production case)
    ─────────────────────────────────────────────────────────
    ``StateManager.save_message(direction="outbound")`` is called from
    ``whatsapp_webhook.py:5883`` IMMEDIATELY after the brain produces
    a reply, and BEFORE the post-LLM safety nets run. The safety nets
    routinely modify the reply text:

      * ``apply_store_link_safety_net`` injects ``store_url`` when
        the customer asked for the store link but the LLM forgot it.
      * ``apply_clear_intent_fallback_net`` rewrites generic apology
        replies into intent-aware nudges.
      * ``apply_delivery_info_context_net`` rewrites dismissive
        replies when the customer was responding to a delivery prompt.
      * ``reasoning_scrub`` drops leaked-thought lines.
      * ``maybe_scrub_unkept_asset_promise`` (May 2026 #31) rewrites
        false-promise spans when the corresponding asset is missing.
      * The CTA-button extractor strips inline URLs and replaces the
        body with a short ``"تفضل المتجر 🌷"``-style label.

    Without this sync, the dashboard renders the OLD body and the
    customer's WhatsApp shows the NEW body — exactly the divergence
    the merchant flagged: "نحلة تعرض رسالة لم تصل واتساب".

    Contract
    ────────
    * Pure observability glue — must NEVER raise. Any DB hiccup is
      logged and swallowed; the send path is unaffected.
    * Uses the SAME lookup as ``stamp_outbound_send_status`` so we
      always update the row the wire layer will stamp next. If no
      candidate row exists (rare; only when the persist failed too)
      we return ``None`` and the caller silently moves on.
    * Idempotent: callers can invoke this multiple times during a
      single turn; the last call wins.

    Returns the row id we touched, or ``None`` on miss / error.
    """
    if db is None or not recipient:
        return None
    if tenant_id is None:
        return None

    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        nested_ok = False
        try:
            db.begin_nested()
            nested_ok = True
        except Exception:
            nested_ok = False

        try:
            row = _find_queued_outbound_row(
                db, tenant_id=int(tenant_id), recipient=recipient,
            )
            if row is None:
                if nested_ok:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                logger.debug(
                    "[OUTBOUND_BODY_SYNC] no candidate row tenant=%s to=%s "
                    "reason=%s",
                    tenant_id, recipient, reason,
                )
                return None

            previous_body = row.body or ""
            new_body = final_body or ""
            if previous_body == new_body:
                if nested_ok:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                logger.debug(
                    "[OUTBOUND_BODY_SYNC] no-op (identical body) tenant=%s "
                    "to=%s row=%s reason=%s",
                    tenant_id, recipient, row.id, reason,
                )
                return int(row.id)

            row.body = new_body
            # Stamp a small audit trail in extra_metadata so we can
            # always reconstruct what the brain originally produced.
            meta = dict(row.extra_metadata or {})
            history = list(meta.get("body_sync_history") or [])
            # Cap history at the last 3 sync events — enough to debug
            # without bloating the JSONB column.
            history.append({
                "reason":       reason,
                "at":           datetime.now(timezone.utc).isoformat(),
                "len_before":   len(previous_body),
                "len_after":    len(new_body),
                # Short preview so a grep is enough to verify a fix
                # without joining tables. Truncated aggressively so
                # PII / long URLs don't blow up the JSONB.
                "preview_from": previous_body[:80],
                "preview_to":   new_body[:80],
            })
            meta["body_sync_history"] = history[-3:]
            row.extra_metadata = meta
            flag_modified(row, "extra_metadata")
            db.add(row)
            db.flush()
            db.commit()

            logger.info(
                "[OUTBOUND_BODY_SYNC] tenant=%s to=%s row=%s reason=%s "
                "len_before=%d len_after=%d delta=%+d",
                tenant_id, recipient, row.id, reason,
                len(previous_body), len(new_body),
                len(new_body) - len(previous_body),
            )
            return int(row.id)
        except Exception as inner_exc:  # noqa: BLE001
            logger.warning(
                "[OUTBOUND_BODY_SYNC] sync failed tenant=%s to=%s err=%s",
                tenant_id, recipient, inner_exc,
            )
            try:
                db.rollback()
            except Exception:
                pass
            return None
    except Exception as outer_exc:  # noqa: BLE001
        logger.warning(
            "[OUTBOUND_BODY_SYNC] setup failed tenant=%s to=%s err=%s",
            tenant_id, recipient, outer_exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def stamp_message_event_id(
    db: Any,
    *,
    message_event_id: int,
    classification: str,
    response_body: Any,
    wamid: Optional[str],
    operation: str,
    error_text: Optional[str] = None,
    duration_ms: Optional[float] = None,
) -> bool:
    """Direct stamp by row id. Used by callers that already hold the
    persisted MessageEvent.id (e.g. ``/conversations/reply`` which
    creates the row inline). Never raises.
    """
    if db is None or not message_event_id:
        return False
    try:
        from models import MessageEvent  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        row = db.query(MessageEvent).filter(MessageEvent.id == message_event_id).first()
        if row is None:
            return False
        block = build_provider_send_block(
            classification=classification,
            response_body=response_body,
            wamid=wamid,
            operation=operation,
            error_text=error_text,
            duration_ms=duration_ms,
        )
        meta = dict(row.extra_metadata or {})
        prev = meta.get("provider_send") or {}
        if isinstance(prev, dict) and prev.get("queued_at"):
            block["queued_at"] = prev["queued_at"]
        meta["provider_send"] = block
        row.extra_metadata = meta
        flag_modified(row, "extra_metadata")
        db.add(row)
        db.flush()
        db.commit()
        logger.info(
            "[outbound_send_status] stamped (by id) message_event=%s status=%s "
            "classification=%s wamid=%s error_key=%s",
            message_event_id, block["status"], classification,
            wamid[-8:] if wamid else None,
            (block.get("error") or {}).get("key"),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[outbound_send_status] stamp_by_id failed message_event=%s err=%s",
            message_event_id, exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return False
