from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.orm import Session

from core.config import D360_API_BASE_URL, D360_PARTNER_API_KEY, D360_PARTNER_HUB_BASE, META_GRAPH_API_VERSION
from core.wa_provider_observability import (
    CLASSIFICATION_EXCEPTION,
    CLASSIFICATION_MISSING_WAMID,
    CLASSIFICATION_NON_2XX,
    CLASSIFICATION_OK,
    CLASSIFICATION_PROVIDER_ERROR,
    record_attempt as _record_provider_attempt,
    summarize_headers as _summarize_provider_headers,
)
from .provider_utils import (
    WHATSAPP_CONNECTION_TYPE_COEXISTENCE,
    WHATSAPP_PROVIDER_360DIALOG,
    wa_provider,
)
from services.d360_logging import (
    d360_extract_remote_url,
    d360_url_flags,
    d360_live_verify_step_record,
    d360_response_summary,
    d360_safe_webhook_result,
    d360_safe_error_payload,
    d360_sanitize_live_verify_probe,
    log_d360_verify,
)
from core.log_redaction import redact_graph_id
from .token_manager import WhatsAppTokenContext, get_token_for_operation

logger = logging.getLogger("nahla.whatsapp.service")


# ── F18: classification helpers ────────────────────────────────────
# A "send" operation is one whose response is expected to carry a
# ``messages[0].id`` (wamid). For sends, a 2xx response WITHOUT a
# wamid is a provider failure — not a success — and we must surface
# it as such so the caller doesn't persist a misleading "delivered"
# state. Non-send POSTs (template submit, webhook configure, etc.)
# legitimately have no wamid and must NOT be misclassified.

# Path → "is this a send call?". We match by the trailing segment so
# Meta (``{phone_id}/messages``) and 360dialog (``messages``) both
# resolve correctly.
_SEND_PATH_SUFFIXES = ("/messages", "messages")


def _is_send_path(path: str) -> bool:
    """True when ``path`` is the conversational-message send endpoint
    for either provider."""
    if not path:
        return False
    p = path.strip().lstrip("/")
    if p == "messages":
        return True
    return p.endswith("/messages")


def _extract_wamid(body: Any) -> Optional[str]:
    """Pull ``messages[0].id`` out of a provider response, or
    ``None`` on any structural mismatch. Both Meta and 360dialog use
    the same success shape.
    """
    if not isinstance(body, dict):
        return None
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return None
    first = msgs[0]
    if not isinstance(first, dict):
        return None
    mid = first.get("id")
    return str(mid).strip() if mid else None


def _classify_response(
    *,
    is_send: bool,
    status_code: Optional[int],
    body: Any,
    wamid: Optional[str],
) -> str:
    """Decide which ``CLASSIFICATION_*`` bucket the response falls
    into. Order of checks matters — we walk from "definitely broken"
    to "looks fine"."""
    if status_code is None:
        return CLASSIFICATION_EXCEPTION
    if status_code < 200 or status_code >= 300:
        return CLASSIFICATION_NON_2XX
    if isinstance(body, dict) and "error" in body and body.get("error"):
        return CLASSIFICATION_PROVIDER_ERROR
    if is_send and not wamid:
        # 2xx response on a send op WITHOUT a wamid is a provider
        # failure even though no exception was raised. Pre-F18 we
        # would have called this a success and persisted a fake
        # "delivered" state.
        return CLASSIFICATION_MISSING_WAMID
    return CLASSIFICATION_OK

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
D360_BASE = D360_API_BASE_URL.rstrip("/")
_D360_PARTNER_HUB = D360_PARTNER_HUB_BASE.rstrip("/")


def _provider_base_url(conn: Any) -> str:
    provider = wa_provider(conn)
    if provider == WHATSAPP_PROVIDER_360DIALOG:
        return D360_BASE
    return GRAPH


def _provider_headers(conn: Any, ctx: WhatsAppTokenContext) -> Dict[str, str]:
    provider = wa_provider(conn)
    if provider == WHATSAPP_PROVIDER_360DIALOG:
        return {
            "D360-API-KEY": ctx.token,
            "Content-Type": "application/json",
        }
    return {
        "Authorization": f"Bearer {ctx.token}",
        "Content-Type": "application/json",
    }


def _provider_url(conn: Any, path: str) -> str:
    base = _provider_base_url(conn)
    clean = path.lstrip("/")
    return f"{base}/{clean}" if clean else base


# ── Wire-layer marker scrub ─────────────────────────────────────────────
#
# Every outbound WhatsApp message goes through ``provider_send_message``
# (templates use ``provider_submit_template`` instead — see below for
# why those are deliberately skipped). This helper strips any internal
# ``[FOO]`` / ``[FOO:bar]`` token the AI may have leaked into a text
# slot before the payload hits Meta / 360dialog.
#
# Background: merchants reported customers receiving ``[TRANSFER]`` and
# similar markers literally in WhatsApp. The root cause is GPT
# hallucinating placeholders it saw in earlier turns / system prompts.
# A scrub was already present in ``whatsapp_webhook._handle_ai_reply``,
# but it only protected the AI-merchant-brain reply path. Every other
# outbound caller (manual `/conversations/reply`, automation engine,
# order notifications, cart recovery, admin direct-send, fallback /
# loop-guard replies in the webhook itself) bypassed it.
#
# By installing the scrub at the wire layer instead of at each caller,
# we guarantee defense-in-depth: a future caller that forgets to
# sanitize cannot leak markers, because the bytes literally cannot
# leave this process without passing through here.
#
# Slots we sanitize (Meta Graph "messages" payload shape):
#   text:        text.body
#   interactive: interactive.header.text (if header.type=="text")
#                interactive.body.text
#                interactive.footer.text
#                interactive.action.buttons[i].reply.title
#                interactive.action.parameters.display_text  (cta_url)
#                interactive.action.sections[j].title
#                interactive.action.sections[j].rows[k].title
#                interactive.action.sections[j].rows[k].description
#   image:       image.caption
#   video:       video.caption
#   document:    document.caption
#
# Slots we deliberately DON'T touch:
#   template.*  — pre-approved by Meta. Parameter values flow from DB
#                 (customer_name, coupon_code, store_name) — never from
#                 GPT output — so internal markers cannot reach there.
#   *.link / *.id / *.media_id — non-text identifiers.
#   to / phone_number_id — non-text identifiers.

def _scrub_outbound_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of ``payload`` with every known text-bearing
    field passed through :func:`scrub_internal_markers`. Idempotent —
    if no markers are present the values are returned unchanged.

    Errors here MUST NOT block the send. The scrub is defense-in-depth
    for hallucinated markers; a bug in the regex shouldn't prevent a
    legitimate reply from reaching the customer. On any exception we
    log and pass the original payload through.
    """
    if not isinstance(payload, dict):
        return payload
    try:
        from core.ai_libraries import scrub_internal_markers  # noqa: PLC0415
    except Exception as exc:
        logger.warning("[WA_WIRE_SCRUB] import failed err=%s", exc)
        return payload

    out = dict(payload)
    mtype = out.get("type")
    scrubbed_any = False

    def _clean(v: Any) -> Any:
        nonlocal scrubbed_any
        if not isinstance(v, str) or not v:
            return v
        new = scrub_internal_markers(v)
        if new != v:
            scrubbed_any = True
        return new

    try:
        if mtype == "text" and isinstance(out.get("text"), dict):
            t = dict(out["text"])
            t["body"] = _clean(t.get("body"))
            out["text"] = t

        elif mtype == "interactive" and isinstance(out.get("interactive"), dict):
            inter = dict(out["interactive"])
            # Header (only when header.type == "text")
            hdr = inter.get("header")
            if isinstance(hdr, dict) and hdr.get("type") == "text":
                hdr = dict(hdr)
                hdr["text"] = _clean(hdr.get("text"))
                inter["header"] = hdr
            # Body
            body = inter.get("body")
            if isinstance(body, dict):
                body = dict(body)
                body["text"] = _clean(body.get("text"))
                inter["body"] = body
            # Footer
            ftr = inter.get("footer")
            if isinstance(ftr, dict):
                ftr = dict(ftr)
                ftr["text"] = _clean(ftr.get("text"))
                inter["footer"] = ftr
            # Action — button labels + list section/row titles
            action = inter.get("action")
            if isinstance(action, dict):
                action = dict(action)
                # CTA-URL display label
                params = action.get("parameters")
                if isinstance(params, dict):
                    params = dict(params)
                    params["display_text"] = _clean(params.get("display_text"))
                    action["parameters"] = params
                btns = action.get("buttons")
                if isinstance(btns, list):
                    new_btns = []
                    for b in btns:
                        if isinstance(b, dict):
                            b = dict(b)
                            reply = b.get("reply")
                            if isinstance(reply, dict):
                                reply = dict(reply)
                                reply["title"] = _clean(reply.get("title"))
                                b["reply"] = reply
                        new_btns.append(b)
                    action["buttons"] = new_btns
                secs = action.get("sections")
                if isinstance(secs, list):
                    new_secs = []
                    for s in secs:
                        if isinstance(s, dict):
                            s = dict(s)
                            s["title"] = _clean(s.get("title"))
                            rows = s.get("rows")
                            if isinstance(rows, list):
                                new_rows = []
                                for r in rows:
                                    if isinstance(r, dict):
                                        r = dict(r)
                                        r["title"] = _clean(r.get("title"))
                                        r["description"] = _clean(r.get("description"))
                                    new_rows.append(r)
                                s["rows"] = new_rows
                        new_secs.append(s)
                    action["sections"] = new_secs
                inter["action"] = action
            out["interactive"] = inter

        elif mtype in ("image", "video", "document") and isinstance(out.get(mtype), dict):
            media = dict(out[mtype])
            media["caption"] = _clean(media.get("caption"))
            out[mtype] = media

        # Untyped / template / sticker / reaction etc. → no text slots
        # to scrub. Pass through unchanged.
    except Exception as exc:
        logger.warning(
            "[WA_WIRE_SCRUB] failed type=%s err=%s — sending original payload",
            mtype, exc,
        )
        return payload

    if scrubbed_any:
        logger.info("[WA_WIRE_SCRUB] cleaned type=%s", mtype)
    return out


async def provider_get_with_context(
    conn: Any,
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    headers = _provider_headers(conn, ctx)
    if wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        headers.pop("Content-Type", None)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(_provider_url(conn, path), headers=headers, params=params or {})
        data = resp.json()
    logger.info(
        "[WA provider_get] op=%s tenant=%s provider=%s path=%s status=%s source=%s",
        operation, tenant_id, wa_provider(conn), path, resp.status_code, ctx.source,
    )
    return data


async def provider_post_with_context(
    conn: Any,
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    """POST to the provider with full observability.

    Records every attempt into the in-memory ring buffer consumed by
    ``GET /admin/debug/last-provider-send`` (see
    ``core/wa_provider_observability.py``). For *send* operations
    (path ends in ``/messages``) a 2xx response WITHOUT a wamid is
    classified as a provider failure and an ``error`` envelope is
    INJECTED into the returned dict so downstream callers
    (``whatsapp_webhook._post_wa``, campaign dispatcher,
    ``record_outbound_message``) treat it as failed rather than
    persisting a fake success state.

    Logging keys:
      ``[WA provider_post]``         — one line per request, always
      ``[WA_SEND_FAIL_NON_2XX]``     — non-2xx status on a send
      ``[WA_SEND_FAIL_PROVIDER_ERR]``— 2xx but the body carries
                                       ``error`` envelope
      ``[WA_INVALID_PROVIDER_RESPONSE]`` — 2xx, no error, but
                                           missing wamid on a send
      ``[WA_SEND_OK]``               — wamid present
      ``[WA_SEND_EXCEPTION]``        — transport-level failure
    """
    provider = wa_provider(conn)
    full_url = _provider_url(conn, path)
    headers  = _provider_headers(conn, ctx)
    headers_summary = _summarize_provider_headers(headers, token_source=ctx.source)
    is_send  = _is_send_path(path)
    conn_phone_id  = getattr(conn, "phone_number_id", None) if conn is not None else None
    conn_id        = getattr(conn, "id", None) if conn is not None else None
    conn_type      = getattr(conn, "connection_type", None) if conn is not None else None

    started_at  = time.monotonic()
    status_code: Optional[int] = None
    data: Dict[str, Any] = {}
    response_text: Optional[str] = None
    error_text:    Optional[str] = None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                full_url,
                headers=headers,
                json=json or {},
                params=params or {},
            )
            status_code  = resp.status_code
            response_text = resp.text  # captured for the ring buffer
            try:
                data = resp.json()
            except Exception:
                # Provider returned non-JSON (HTML error page, plain
                # text). Surface as a synthetic error envelope so the
                # downstream classifier reports provider_error.
                data = {
                    "error": {
                        "message": (
                            "provider returned non-JSON body. "
                            f"status={status_code} body_preview="
                            f"{(response_text or '')[:200]!r}"
                        ),
                        "type": "non_json_response",
                    },
                }
    except Exception as exc:  # noqa: BLE001
        error_text = f"{type(exc).__name__}: {exc}"
        logger.error(
            "[WA_SEND_EXCEPTION] op=%s tenant=%s provider=%s path=%s err=%s",
            operation, tenant_id, provider, path, error_text,
        )
        _record_provider_attempt(
            tenant_id=tenant_id,
            operation=operation,
            provider=provider,
            method="POST",
            full_url=full_url,
            path=path,
            request_payload=json,
            headers_summary=headers_summary,
            response_status=None,
            response_body=None,
            parsed_wamid=None,
            classification=CLASSIFICATION_EXCEPTION,
            duration_ms=(time.monotonic() - started_at) * 1000.0,
            error_text=error_text,
            connection_phone_number_id=conn_phone_id,
            connection_id=conn_id,
            connection_type=conn_type,
        )
        # Preserve the historical contract: re-raise on transport
        # failure so existing exception handlers in the webhook /
        # campaign dispatcher keep working.
        raise

    wamid = _extract_wamid(data)
    classification = _classify_response(
        is_send=is_send,
        status_code=status_code,
        body=data,
        wamid=wamid,
    )
    duration_ms = (time.monotonic() - started_at) * 1000.0

    # Top-of-funnel always-on line (preserves old grep keys).
    logger.info(
        "[WA provider_post] op=%s tenant=%s provider=%s path=%s status=%s "
        "source=%s is_send=%s wamid_present=%s classification=%s duration_ms=%.1f "
        "conn_phone_id=%s",
        operation, tenant_id, provider, path, status_code,
        ctx.source, is_send, bool(wamid), classification, duration_ms,
        conn_phone_id,
    )

    if classification == CLASSIFICATION_NON_2XX:
        logger.warning(
            "[WA_SEND_FAIL_NON_2XX] op=%s tenant=%s provider=%s status=%s "
            "url=%s body_preview=%.500s",
            operation, tenant_id, provider, status_code, full_url, response_text or "",
        )
    elif classification == CLASSIFICATION_PROVIDER_ERROR:
        logger.warning(
            "[WA_SEND_FAIL_PROVIDER_ERR] op=%s tenant=%s provider=%s status=%s "
            "error=%.500s",
            operation, tenant_id, provider, status_code,
            (data.get("error") if isinstance(data, dict) else None) or "",
        )
    elif classification == CLASSIFICATION_MISSING_WAMID:
        # The exact failure mode F18 was created to catch: provider
        # accepted (2xx, no error envelope) but never returned a
        # ``messages[0].id``. Without the explicit guard this would
        # have been silently classified as success.
        logger.warning(
            "[WA_INVALID_PROVIDER_RESPONSE] op=%s tenant=%s provider=%s "
            "status=%s url=%s conn_phone_id=%s body=%.500s",
            operation, tenant_id, provider, status_code, full_url,
            conn_phone_id, str(data)[:500],
        )
        # Inject a synthetic error envelope so the downstream success
        # detector (``"error" in resp_data``) treats this as a failed
        # send. Without this, ``_post_wa`` would have returned True
        # for a message that never reached the customer.
        if isinstance(data, dict) and "error" not in data:
            data = dict(data)
            data["error"] = {
                "message": (
                    "provider returned 2xx but no messages[0].id "
                    "(no wamid). treated as send failure by nahla "
                    "wire layer."
                ),
                "type":    "missing_wamid",
                "code":    "WA_INVALID_PROVIDER_RESPONSE",
                "nahla_injected": True,
            }
    elif classification == CLASSIFICATION_OK and is_send:
        logger.info(
            "[WA_SEND_OK] op=%s tenant=%s provider=%s wamid_tail=%s duration_ms=%.1f",
            operation, tenant_id, provider,
            wamid[-8:] if wamid else None, duration_ms,
        )

    _record_provider_attempt(
        tenant_id=tenant_id,
        operation=operation,
        provider=provider,
        method="POST",
        full_url=full_url,
        path=path,
        request_payload=json,
        headers_summary=headers_summary,
        response_status=status_code,
        response_body=data,
        parsed_wamid=wamid,
        classification=classification,
        duration_ms=duration_ms,
        error_text=None,
        connection_phone_number_id=conn_phone_id,
        connection_id=conn_id,
        connection_type=conn_type,
    )

    # ── Outbound MessageEvent send-status bridge ──────────────────────
    # Attach the F18 classification + parsed wamid + timing to the
    # returned dict so the upstream caller (``_post_wa`` in
    # ``routers.whatsapp_webhook``) can stamp the persisted outbound
    # ``MessageEvent`` row with the wire-layer outcome without
    # re-deriving the classification. We use leading-underscore
    # keys so this metadata cannot collide with any provider field
    # name (Meta / 360dialog responses never carry ``_nahla_*``).
    # Caller is free to ignore these fields — non-send paths
    # (template submit, webhook config) just don't read them.
    if isinstance(data, dict):
        try:
            data["_nahla_classification"] = classification
            data["_nahla_wamid"]          = wamid
            data["_nahla_is_send"]        = is_send
            data["_nahla_duration_ms"]    = duration_ms
        except Exception:
            # Some providers occasionally hand back a dict subclass
            # that rejects new keys; never let bookkeeping break the
            # actual send.
            pass

    return data


async def graph_get_with_context(
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    return await provider_get_with_context(
        None,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=path,
        params=params,
        timeout=timeout,
    )


async def graph_post_with_context(
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    return await provider_post_with_context(
        None,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=path,
        json=json,
        params=params,
        timeout=timeout,
    )


async def graph_get(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation=operation,
    )
    data = await provider_get_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=path,
        params=params,
        timeout=timeout,
    )
    return data, ctx


async def graph_post(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation=operation,
    )
    data = await provider_post_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=path,
        json=json,
        params=params,
        timeout=timeout,
    )
    return data, ctx


async def provider_send_message(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    operation: str,
    phone_id: str,
    payload: Dict[str, Any],
    prefer_platform: bool = False,
    timeout: float = 20,
    allow_manual: bool = False,
    blocked_path: str = "provider_send_message",
    automation_guard: bool = True,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    from core.acceptance_execution_context import deny_external_egress  # noqa: PLC0415

    deny_external_egress(
        egress_kind="whatsapp_provider",
        operation=operation or "provider_send_message",
        tenant_id=tenant_id,
    )
    send_payload = dict(payload or {})
    send_payload.pop("_nahla_inbound_id", None)
    raw_to = str(send_payload.get("to") or "").strip()
    if raw_to:
        from utils.phone_utils import (  # noqa: PLC0415
            format_wa_send_recipient,
            redact_phone_for_log,
        )
        formatted_to = format_wa_send_recipient(raw_to)
        if not formatted_to:
            logger.warning(
                "[WA_RECIPIENT_INVALID] tenant_id=%s operation=%s phone_number_id=%s "
                "raw_recipient=%s normalized_absent=true — skipping provider send",
                tenant_id,
                operation,
                phone_id,
                redact_phone_for_log(raw_to),
            )
            return (
                {
                    "error": {
                        "code": "invalid_recipient",
                        "type": "ValidationError",
                        "message": (
                            "Recipient phone could not be normalized for WhatsApp send"
                        ),
                    },
                    "_nahla_classification": "recipient_invalid",
                },
                WhatsAppTokenContext(
                    token="",
                    source="validation_skip",
                    token_status="skipped",
                    expires_at=None,
                    oauth_session_status="",
                    oauth_session_message=None,
                ),
            )
        send_payload["to"] = formatted_to

        if automation_guard and db is not None and tenant_id:
            try:
                from core.automation_send_guard import (  # noqa: PLC0415
                    evaluate_automation_send,
                )

                _msg_type = str(send_payload.get("type") or "text").strip().lower()
                _block = evaluate_automation_send(
                    db,
                    tenant_id=tenant_id,
                    customer_phone=formatted_to,
                    message_type=_msg_type,
                    blocked_path=blocked_path or operation,
                    allow_manual=allow_manual,
                )
                if _block.block:
                    return (
                        {
                            "error": {
                                "code": "automation_blocked",
                                "type": "AutomationBlocked",
                                "message": (
                                    "Outbound send blocked: conversation under "
                                    "human supervision or AI disabled"
                                ),
                            },
                            "_nahla_classification": "automation_blocked",
                            "_nahla_block_reason": _block.reason,
                        },
                        WhatsAppTokenContext(
                            token="",
                            source="automation_guard",
                            token_status="skipped",
                            expires_at=None,
                            oauth_session_status="",
                            oauth_session_message=None,
                        ),
                    )
            except Exception as _guard_exc:  # noqa: BLE001
                logger.warning(
                    "[AUTOMATION_BLOCKED] guard check failed (non-fatal) "
                    "tenant_id=%s err=%s",
                    tenant_id,
                    _guard_exc,
                )

        if not allow_manual and db is not None and tenant_id:
            try:
                from core.wa_usage import (  # noqa: PLC0415
                    check_limit,
                    conversation_quota_category_for_operation,
                )

                _category = conversation_quota_category_for_operation(operation)
                _quota = check_limit(db, int(tenant_id), category=_category)
                if not _quota.allowed:
                    logger.info(
                        "[CONVERSATION_LIMIT] provider_send blocked tenant=%s op=%s "
                        "used=%s limit=%s reason=%s",
                        tenant_id,
                        operation,
                        _quota.used_total,
                        _quota.limit,
                        _quota.reason,
                    )
                    return (
                        {
                            "error": {
                                "code": _quota.reason,
                                "type": "ConversationQuotaExceeded",
                                "message": (
                                    "Outbound send blocked: monthly conversation "
                                    "plan limit reached"
                                ),
                            },
                            "_nahla_classification": "conversation_quota_blocked",
                            "_nahla_block_reason": _quota.reason,
                            "_nahla_quota_used": _quota.used_total,
                            "_nahla_quota_limit": _quota.limit,
                        },
                        WhatsAppTokenContext(
                            token="",
                            source="conversation_quota_guard",
                            token_status="skipped",
                            expires_at=None,
                            oauth_session_status="",
                            oauth_session_message=None,
                        ),
                    )
            except Exception as _quota_exc:  # noqa: BLE001
                logger.warning(
                    "[CONVERSATION_LIMIT] provider pre_send check failed tenant=%s err=%s",
                    tenant_id,
                    _quota_exc,
                )

    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation=operation,
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)
    # Wire-layer scrub: strip any [TRANSFER] / [DEBUG] / [ACTION] /
    # [INTERNAL] / [MEDIA:N] / etc. tokens the AI may have leaked into
    # a text-bearing slot. Runs on EVERY caller (webhook reply,
    # manual /conversations/reply, automation engine, orders,
    # cart recovery, admin direct-send) before any byte leaves
    # this process. See _scrub_outbound_payload docstring.
    send_payload = _scrub_outbound_payload(send_payload)
    if provider == WHATSAPP_PROVIDER_360DIALOG:
        send_payload.setdefault("recipient_type", "individual")
        data = await provider_post_with_context(
            conn,
            ctx,
            tenant_id=tenant_id,
            operation=operation,
            path="messages",
            json=send_payload,
            timeout=timeout,
        )
        return data, ctx
    data = await provider_post_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=f"{phone_id}/messages",
        json=send_payload,
        timeout=timeout,
    )
    return data, ctx


async def provider_submit_template(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    waba_id: str,
    payload: Dict[str, Any],
    prefer_platform: bool = False,
    timeout: float = 20,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation="template_submit",
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)
    path = "v1/configs/templates" if provider == WHATSAPP_PROVIDER_360DIALOG else f"{waba_id}/message_templates"
    data = await provider_post_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation="template_submit",
        path=path,
        json=payload,
        timeout=timeout,
    )
    return data, ctx


async def provider_delete_template(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    waba_id: str,
    template_name: str,
    prefer_platform: bool = False,
    timeout: float = 20,
) -> Dict[str, Any]:
    """
    Delete a template from Meta by name.

    Meta API: DELETE /{waba_id}/message_templates?name={template_name}
    360dialog: DELETE v1/configs/templates?name={template_name}
    """
    ctx = await get_token_for_operation(
        db, conn,
        tenant_id=tenant_id,
        operation="template_delete",
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)

    if provider == WHATSAPP_PROVIDER_360DIALOG:
        path = f"v1/configs/templates"
    else:
        path = f"{waba_id}/message_templates"

    headers = _provider_headers(conn, ctx)
    url = _provider_url(conn, path)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.delete(url, headers=headers, params={"name": template_name})
        data = resp.json()

    logger.info(
        "[WA template_delete] tenant=%s provider=%s name=%s status=%s",
        tenant_id, provider, template_name, resp.status_code,
    )
    return data


async def provider_list_templates(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    waba_id: str,
    prefer_platform: bool = False,
    timeout: float = 20,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation="template_sync",
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)

    if provider == WHATSAPP_PROVIDER_360DIALOG:
        path = "v1/configs/templates"
        params: Optional[Dict[str, Any]] = None
    else:
        path = f"{waba_id}/message_templates"
        # Explicitly request fields including `status` — without this
        # parameter Meta Graph API v20+ may omit the status field entirely,
        # causing every template to default to PENDING in the sync loop
        # (`item.get("status") or "PENDING"`).
        # `limit=250` avoids missing templates behind pagination.
        params = {
            "fields": "name,status,category,language,components,rejected_reason,quality_score,id",
            "limit": "250",
        }

    data = await provider_get_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation="template_sync",
        path=path,
        params=params,
        timeout=timeout,
    )

    # ── Pagination: follow `paging.next` to collect ALL templates ─────────
    # Meta returns at most `limit` items per page. For accounts with
    # hundreds of templates we must follow the cursor chain.
    if provider != WHATSAPP_PROVIDER_360DIALOG:
        all_items = list(data.get("data") or [])
        next_url = (data.get("paging") or {}).get("next")
        pages = 0
        while next_url and pages < 20:  # safety cap
            pages += 1
            try:
                headers = _provider_headers(conn, ctx)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(next_url, headers=headers)
                    page = resp.json()
                all_items.extend(page.get("data") or [])
                next_url = (page.get("paging") or {}).get("next")
            except Exception as exc:
                logger.warning(
                    "[WA template_sync] pagination failed tenant=%s page=%d: %s",
                    tenant_id, pages, exc,
                )
                break
        if pages:
            logger.info(
                "[WA template_sync] tenant=%s fetched %d extra page(s), total=%d templates",
                tenant_id, pages, len(all_items),
            )
        data = {**data, "data": all_items}

    return data, ctx


async def dialog360_configure_webhook(
    *,
    api_key: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 5,
) -> Dict[str, Any]:
    """Register (POST) the channel webhook URL with 360dialog.

    The endpoint accepts a single URL plus optional custom headers that
    360dialog will replay on every webhook delivery. Nahla uses this to
    inject the per-tenant `X-Nahla-Coexistence-Secret` header.
    """
    req_headers = {
        "D360-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {"url": url}
    if headers:
        payload["headers"] = headers
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{D360_BASE}/v1/configs/webhook", headers=req_headers, json=payload)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
    summary = d360_response_summary({"status_code": resp.status_code, **(data if isinstance(data, dict) else {})})
    logger.info("[WA dialog360 webhook] configure status=%s summary=%s", resp.status_code, summary)
    if resp.status_code >= 400 and "error" not in data:
        data = {"error": data, "status_code": resp.status_code}
    return d360_safe_webhook_result(resp.status_code, data)





def _enrich_safe_d360_webhook_read(
    data: object,
    http_status: int,
    *,
    expected_url: Optional[str] = None,
    local_phone_number_id: Optional[str] = None,
) -> Dict[str, Any]:
    safe = d360_safe_webhook_result(http_status, data if isinstance(data, dict) else {})
    if isinstance(data, dict):
        remote = d360_extract_remote_url(data)
        flags = d360_url_flags(remote, expected_url)
        safe.update(
            {
                "remote_url_present": flags["remote_url_present"],
                "expected_url_present": flags["expected_url_present"],
                "url_matches_expected": flags["url_matches_expected"],
            }
        )
        if data.get("waba_id"):
            safe["has_waba_id"] = True
        nums = data.get("numbers_on_this_waba")
        if isinstance(nums, list):
            safe["numbers_count"] = len(nums)
            if local_phone_number_id:
                safe["local_phone_listed"] = str(local_phone_number_id) in [str(n) for n in nums]
    return safe

async def dialog360_get_webhook_config(
    *,
    api_key: str,
    timeout: float = 5,
    expected_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Read back the currently configured channel webhook from 360dialog.

    Used by the owner-panel "Verify" action: we compare the URL 360dialog has
    on file against the URL Nahla expects and surface a mismatch instead of
    silently trusting the local cache.
    """
    req_headers = {"D360-API-KEY": api_key}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{D360_BASE}/v1/configs/webhook", headers=req_headers)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
    summary = d360_response_summary({"status_code": resp.status_code, **(data if isinstance(data, dict) else {})})
    logger.info("[WA dialog360 webhook] read status=%s summary=%s", resp.status_code, summary)
    if resp.status_code >= 400:
        return {"error": data, "status_code": resp.status_code}
    return _enrich_safe_d360_webhook_read(data, resp.status_code, expected_url=expected_url)


# ── 360dialog WABA-level webhook (Coexistence) ────────────────────────────────
#
# 360dialog supports two webhook scopes for inbound traffic:
#
#   1. **Phone-number / Channel** — `POST /v1/configs/webhook`  (per channel)
#   2. **WABA-level**             — `POST /waba_webhook`        (whole WABA)
#
# Delivery priority (per 360dialog docs):
#   Phone-Number webhook > WABA webhook > nothing (callbacks drop).
#
# In Coexistence (WhatsApp Business App + Cloud API sharing the same number)
# the WABA-level webhook is what actually drives `messages` callbacks for
# every phone hanging off that WABA. If a re-link rotates the channel's
# `phone_number_id` and the WABA-level webhook was never set (or was wiped
# by a prior re-onboarding), inbound delivery silently stops — the
# 360dialog UI keeps showing the Channel Webhook ✓ while WABA Webhook = N/A,
# and `recent-webhook-events` reads `events_returned=0` even after fresh
# customer messages. The Set/Get helpers below let us drive that scope from
# code so we never depend on a manual hub-UI step again.

async def dialog360_get_waba_webhook(
    *,
    api_key: str,
    timeout: float = 5,
    expected_url: Optional[str] = None,
    local_phone_number_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Read the current WABA-level webhook config from 360dialog.

    Response includes ``url``, ``headers``, ``waba_id``, ``numbers_on_this_waba``.
    """
    req_headers = {"D360-API-KEY": api_key}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{D360_BASE}/waba_webhook", headers=req_headers)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
    summary = d360_response_summary({"status_code": resp.status_code, **(data if isinstance(data, dict) else {})})
    logger.info("[WA dialog360 waba_webhook] read status=%s summary=%s", resp.status_code, summary)
    if resp.status_code >= 400:
        return _enrich_safe_d360_webhook_read(
            {"error": data, "status_code": resp.status_code},
            resp.status_code,
            expected_url=expected_url,
        )
    return _enrich_safe_d360_webhook_read(
            data,
            resp.status_code,
            expected_url=expected_url,
            local_phone_number_id=local_phone_number_id,
        )


async def dialog360_set_waba_webhook(
    *,
    api_key: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    override_all: bool = True,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """Configure (POST) the WABA-level webhook URL with 360dialog.

    Parameters
    ----------
    api_key
        Channel D360-API-KEY for any channel on the target WABA.
    url
        Full HTTPS callback URL (no underscores in domain, no explicit port).
    headers
        Optional custom headers 360dialog will replay on every delivery.
        Nahla uses this to inject ``X-Nahla-Coexistence-Secret`` so the
        receiving router can drop forged events.
    override_all
        When ``True``, the WABA webhook is applied to **every** Cloud API
        number under this WABA, regardless of any pre-existing
        phone-number-level webhook. When ``False``, only numbers that
        currently lack a phone-number-level webhook are touched. For
        Nahla we default to ``True`` so a stale per-channel webhook on
        an old ``phone_number_id`` can never silently swallow inbound
        traffic for the new one.

    Notes
    -----
    360dialog applies the change asynchronously (15–20 s); call
    ``dialog360_get_waba_webhook`` after a short delay to confirm.
    """
    req_headers = {
        "D360-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {"url": url, "override_all": bool(override_all)}
    if headers:
        payload["headers"] = headers
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{D360_BASE}/waba_webhook",
            headers=req_headers,
            json=payload,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
    summary = d360_response_summary({"status_code": resp.status_code, **(data if isinstance(data, dict) else {})})
    logger.info(
        "[WA dialog360 waba_webhook] set status=%s override_all=%s summary=%s",
        resp.status_code, override_all, summary,
    )
    if resp.status_code >= 400 and "error" not in data:
        data = {"error": data, "status_code": resp.status_code}
    return d360_safe_webhook_result(resp.status_code, data)


def _clip_body(body: Any, limit: int = 240) -> str:
    try:
        import json as _json  # noqa: PLC0415
        txt = _json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else str(body)
    except Exception:
        txt = str(body)
    txt = txt.replace("\n", " ").strip()
    return txt[:limit] + ("…" if len(txt) > limit else "")


async def dialog360_live_verify_probes(
    *,
    tenant_id: int,
    api_key: str,
    phone_number_id: str,
    waba_id: str,
    channel_id: Optional[str],
    connection_type: str,
    partner_id: Optional[str],
    timeout: float = 10.0,
) -> Dict[str, Any]:
    coexistence = str(connection_type or "").strip().lower() == WHATSAPP_CONNECTION_TYPE_COEXISTENCE
    steps: list[Dict[str, Any]] = []

    logger.info(
        "[D360 live-verify] tenant=%s coexistence=%s channel_present=%s phone_present=%s waba_present=%s partner_cfg=%s",
        tenant_id,
        coexistence,
        bool(channel_id),
        bool(phone_number_id),
        bool(waba_id),
        bool(partner_id),
    )

    auth_revoked = False

    def _record_step(
        *,
        name: str,
        method: str,
        uses_channel_key: bool,
        status_code: Optional[int],
        ok_http: bool,
        error_type: Optional[str] = None,
    ) -> None:
        nonlocal auth_revoked
        if uses_channel_key and status_code in (401, 403):
            auth_revoked = True
        step = d360_live_verify_step_record(
            name,
            method,
            status_code=status_code,
            ok_http=ok_http,
            uses_channel_key=uses_channel_key,
            error_type=error_type,
        )
        steps.append(step)
        logger.info(
            "[D360 live-verify] tenant=%s step=%s method=%s status=%s ok=%s error_type=%s",
            tenant_id,
            name,
            step["method"],
            status_code,
            ok_http,
            error_type,
        )

    hdr_chan = {"D360-API-KEY": api_key}

    async def _step_http(
        *,
        name: str,
        method: str,
        url: str,
        headers: Dict[str, str],
        params: Optional[Dict[str, str]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        uses_channel_key: bool = False,
    ) -> None:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method.upper() == "GET":
                    resp = await client.get(url, headers=headers, params=params or {})
                else:
                    resp = await client.request(method.upper(), url, headers=headers, json=json_body)
            ok_http = 200 <= resp.status_code < 300
            err_type = None if ok_http else "remote_error"
            _record_step(
                name=name,
                method=method,
                uses_channel_key=uses_channel_key,
                status_code=resp.status_code,
                ok_http=ok_http,
                error_type=err_type,
            )
        except Exception as exc:
            logger.warning(
                "[D360 live-verify] tenant=%s step=%s FAILED error_type=%s",
                tenant_id,
                name,
                type(exc).__name__,
            )
            _record_step(
                name=name,
                method=method,
                uses_channel_key=uses_channel_key,
                status_code=None,
                ok_http=False,
                error_type=type(exc).__name__,
            )

    await _step_http(
        name="v1_configs",
        method="GET",
        url=f"{D360_BASE}/v1/configs",
        headers=dict(hdr_chan),
        uses_channel_key=True,
    )

    await _step_http(
        name="webhook_read",
        method="GET",
        url=f"{D360_BASE}/v1/configs/webhook",
        headers={**hdr_chan, "Content-Type": "application/json"},
        uses_channel_key=True,
    )

    if phone_number_id:
        await _step_http(
            name="phone_object",
            method="GET",
            url=f"{D360_BASE}/{phone_number_id}",
            headers=dict(hdr_chan),
            params={"fields": "id,display_phone_number,verified_name,quality_rating,whatsapp_business_account"},
            uses_channel_key=True,
        )

    if partner_id and channel_id and D360_PARTNER_API_KEY:
        p_url = f"{_D360_PARTNER_HUB}/api/v2/partners/{partner_id}/channels/{channel_id}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    p_url,
                    headers={"Authorization": f"Bearer {D360_PARTNER_API_KEY}"},
                )
            ok_http = 200 <= resp.status_code < 300
            try:
                pdata = resp.json()
            except Exception:
                pdata = {}
            if isinstance(pdata, dict) and pdata.get("error"):
                ok_http = False
            _record_step(
                name="partner_channel",
                method="GET",
                uses_channel_key=False,
                status_code=resp.status_code,
                ok_http=ok_http,
                error_type=None if ok_http else "remote_error",
            )
        except Exception as exc:
            logger.warning(
                "[D360 live-verify] tenant=%s step=partner_channel FAILED error_type=%s",
                tenant_id,
                type(exc).__name__,
            )
            _record_step(
                name="partner_channel",
                method="GET",
                uses_channel_key=False,
                status_code=None,
                ok_http=False,
                error_type=type(exc).__name__,
            )

    composite_alive = any(
        s.get("ok")
        for s in steps
        if s["step"] in {"v1_configs", "webhook_read", "phone_object", "partner_channel"}
    )

    summary = " | ".join(
        f"{s['step']}={s.get('status_code')}{'ok' if s.get('ok') else 'fail'}"
        for s in steps
    )

    raw = {
        "coexistence_mode": coexistence,
        "composite_alive": composite_alive,
        "channel_auth_revoked": auth_revoked,
        "steps": steps,
        "summary": summary,
    }
    return d360_sanitize_live_verify_probe(raw) | {
        "composite_alive": composite_alive,
        "channel_auth_revoked": auth_revoked,
        "summary": summary,
    }

# ── 360dialog Partner API helpers ─────────────────────────────────────────────


async def dialog360_generate_api_key(
    *,
    partner_id: str,
    channel_id: str,
    timeout: float = 5,
) -> Dict[str, Any]:
    """
    Generate (or retrieve) the D360-API-KEY for a channel the merchant connected
    during Integrated Onboarding.

    POST https://hub.360dialog.com/api/v2/partners/{partner_id}/channels/{channel_id}/api-keys
    Authorization: Bearer {D360_PARTNER_API_KEY}
    """
    if not D360_PARTNER_API_KEY:
        return {"error": "D360_PARTNER_API_KEY not configured"}
    url = f"{_D360_PARTNER_HUB}/api/v2/partners/{partner_id}/channels/{channel_id}/api-keys"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {D360_PARTNER_API_KEY}",
                "Content-Type": "application/json",
            },
        )
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    logger.info(
        "[D360 partner] generate_api_key partner=%s channel=%s status=%s",
        partner_id, channel_id, resp.status_code,
    )
    return data


async def dialog360_get_channel_info(
    *,
    partner_id: str,
    channel_id: str,
    timeout: float = 5,
) -> Dict[str, Any]:
    """
    Retrieve channel details (status, phone_number, waba_id, etc.) from Partner API.

    GET https://hub.360dialog.com/api/v2/partners/{partner_id}/channels/{channel_id}
    """
    if not D360_PARTNER_API_KEY:
        return {"error": "D360_PARTNER_API_KEY not configured"}
    url = f"{_D360_PARTNER_HUB}/api/v2/partners/{partner_id}/channels/{channel_id}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {D360_PARTNER_API_KEY}"},
        )
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


async def dialog360_resolve_channel_metadata(
    *,
    api_key: str,
    phone_number_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    partner_id: Optional[str] = None,
    timeout: float = 5,
) -> Dict[str, Any]:
    """Best-effort resolver for missing 360dialog channel metadata.

    Calls every reasonable 360dialog endpoint we have credentials for and
    merges the results into a single normalised payload:

        {
          "waba_id":         str | None,
          "phone_number_id": str | None,
          "phone_number":    str | None,
          "display_name":    str | None,
          "channel_status":  str | None,
          "sources":         [str, ...],   # which endpoints contributed
          "errors":          {endpoint: error_msg},
          "raw":             {endpoint: raw response},
        }

    Resolution sources, in priority order:

      1. **Partner API** (`hub.360dialog.com/api/v2/partners/.../channels/...`)
         — Most authoritative when we have D360_PARTNER_API_KEY + channel_id.
         Returns waba_id, phone_number, status, etc.
      2. **Channel API: GET /v1/configs** with the per-tenant `D360-API-KEY`
         — Returns webhook config + sometimes ``on_behalf_of_business_info``
         and the channel's own phone metadata.
      3. **Phone object endpoint**: ``GET /<phone_number_id>`` against the
         WABA-V2 host using the api_key as a Meta-style bearer. 360dialog's
         WABA-V2 cluster mirrors Meta Cloud API for this path and returns
         ``display_phone_number`` + ``verified_name`` when the channel is
         active.

    The caller decides what to persist; the resolver itself is read-only."""
    out: Dict[str, Any] = {
        "waba_id":         None,
        "phone_number_id": phone_number_id,
        "phone_number":    None,
        "display_name":    None,
        "channel_status":  None,
        "sources":         [],
        "errors":          {},
        "raw":             {},
    }

    if not api_key and not (partner_id and channel_id):
        out["errors"]["resolver"] = "no credentials available"
        return out

    # ── 1. Partner API ─────────────────────────────────────────────────
    if partner_id and channel_id and D360_PARTNER_API_KEY:
        try:
            info = await dialog360_get_channel_info(partner_id=partner_id, channel_id=channel_id)
            out["raw"]["partner"] = info
            if isinstance(info, dict) and "error" not in info:
                out["waba_id"]        = out["waba_id"] or info.get("waba_id") or info.get("waba_account_id")
                out["phone_number"]   = out["phone_number"] or info.get("phone_number") or info.get("phone")
                out["display_name"]   = out["display_name"] or info.get("name") or info.get("verified_name")
                out["channel_status"] = out["channel_status"] or info.get("status")
                out["sources"].append("partner")
            elif isinstance(info, dict) and "error" in info:
                out["errors"]["partner"] = str(info.get("error"))[:200]
        except Exception as exc:
            out["errors"]["partner"] = f"{type(exc).__name__}: {exc}"[:200]

    # ── 2. Channel-level GET /v1/configs ───────────────────────────────
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{D360_BASE}/v1/configs",
                    headers={"D360-API-KEY": api_key},
                )
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text}
            out["raw"]["v1_configs"] = {"status_code": resp.status_code, "body": data}
            if 200 <= resp.status_code < 300 and isinstance(data, dict):
                # 360dialog mixes flat + nested shapes across product
                # versions. Probe both.
                obo = data.get("on_behalf_of_business_info") or {}
                phone = data.get("phone") or data.get("phone_number") or {}
                out["waba_id"] = (
                    out["waba_id"]
                    or data.get("waba_id")
                    or data.get("waba_account_id")
                    or obo.get("waba_id")
                    or obo.get("id")
                )
                out["phone_number_id"] = (
                    out["phone_number_id"]
                    or data.get("phone_number_id")
                    or (phone.get("id") if isinstance(phone, dict) else None)
                )
                out["phone_number"] = (
                    out["phone_number"]
                    or data.get("display_phone_number")
                    or (phone.get("display_phone_number") if isinstance(phone, dict) else None)
                )
                out["display_name"] = (
                    out["display_name"]
                    or data.get("verified_name")
                    or (phone.get("verified_name") if isinstance(phone, dict) else None)
                )
                out["sources"].append("v1_configs")
            elif resp.status_code >= 400:
                out["errors"]["v1_configs"] = f"http_{resp.status_code}: {str(data)[:200]}"
        except Exception as exc:
            out["errors"]["v1_configs"] = f"{type(exc).__name__}: {exc}"[:200]

    # ── 3. Phone object endpoint (WABA-V2 / Cloud API parity) ──────────
    pnid = out["phone_number_id"]
    if api_key and pnid:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{D360_BASE}/{pnid}",
                    headers={"D360-API-KEY": api_key},
                    params={"fields": "id,display_phone_number,verified_name,quality_rating,whatsapp_business_account"},
                )
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text}
            out["raw"]["phone_object"] = {"status_code": resp.status_code, "body": data}
            if 200 <= resp.status_code < 300 and isinstance(data, dict):
                wba = data.get("whatsapp_business_account") or {}
                out["waba_id"]      = out["waba_id"] or (wba.get("id") if isinstance(wba, dict) else None)
                out["phone_number"] = out["phone_number"] or data.get("display_phone_number")
                out["display_name"] = out["display_name"] or data.get("verified_name")
                out["sources"].append("phone_object")
            elif resp.status_code >= 400:
                out["errors"]["phone_object"] = f"http_{resp.status_code}: {str(data)[:200]}"
        except Exception as exc:
            out["errors"]["phone_object"] = f"{type(exc).__name__}: {exc}"[:200]

    logger.info(
        "[D360 resolver] phone_number_id=%s channel_id=%s sources=%s errors=%s "
        "→ waba=%s phone=%s name=%s",
        phone_number_id, channel_id, out["sources"], list(out["errors"].keys()),
        out["waba_id"], out["phone_number"], out["display_name"],
    )
    return out


async def fetch_meta_phone_tier(
    conn: Any,
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fetch messaging_limit and quality_rating for the phone from whatever
    provider Meta lives behind for THIS connection.

    Routing rules (set on ``WhatsAppConnection.provider``):
      * ``meta``       → Graph API direct: ``GET /{phone_id}?fields=...``
      * ``dialog360``  → 360dialog Cloud API. The Coexistence relay only
                         proxies a *subset* of Graph fields; in particular
                         ``messaging_limit_tier`` is NOT consistently
                         exposed. We probe three paths in order and
                         return the first one that yields a non-empty
                         tier:
                           1. ``GET /{phone_id}?fields=messaging_limit_tier,quality_rating``
                              — works for direct Meta, sometimes for d360.
                           2. ``GET /v1/configs`` — d360 channel config
                              endpoint; some accounts surface the tier
                              under ``messaging_limit`` here.
                           3. ``GET /v1/health/messaging-tier`` —
                              d360 health proxy where it exists.

    Return shape now ALWAYS includes a ``_diagnostics`` block listing
    each path tried, the HTTP status (best-effort), and a redacted
    snippet of the response. The UI surfaces this so the merchant can
    see WHY we still show e.g. ``TIER_250`` after Meta granted them a
    higher tier — usually because the provider's read path doesn't
    expose the field and we're rendering a stale cached value.

    On any failure we leave the cached row untouched (return empty
    ``messaging_limit``); the UI flags it as stale.
    """
    phone_id = getattr(conn, "phone_number_id", None)
    provider = (getattr(conn, "provider", None) or "meta").strip().lower()
    is_d360 = provider in ("dialog360", "360dialog", "d360")

    diagnostics: list = []

    def _record(path: str, status: Any, body: Any, error: Optional[str] = None) -> None:
        # Truncate huge bodies — we just need the shape, not megabytes
        # of HTML. Strings beyond 600 chars rarely add diagnostic value
        # and would bloat the API response.
        snippet: Any
        if isinstance(body, (dict, list)):
            try:
                import json as _json  # noqa: PLC0415
                snippet = _json.loads(_json.dumps(body, default=str))
            except Exception:
                snippet = str(body)[:600]
        else:
            snippet = (str(body)[:600]) if body is not None else None
        diagnostics.append({
            "path":   path,
            "status": status,
            "error":  error,
            "body":   snippet,
        })

    if not phone_id or not ctx.token:
        return {
            "messaging_limit": None,
            "quality_rating":  None,
            "_diagnostics":    [{"path": "(skipped)", "error": "no phone_id or token"}],
        }

    # ── 1) Graph-style ``GET /{phone_id}`` — works for direct Meta, sometimes d360
    try:
        data = await provider_get_with_context(
            conn, ctx,
            tenant_id=tenant_id,
            operation="fetch_phone_tier",
            path=f"{phone_id}",
            params={"fields": "messaging_limit_tier,quality_rating"},
            timeout=15,
        )
        _record(f"GET /{phone_id}?fields=messaging_limit_tier,quality_rating", "2xx?", data)
        tier = data.get("messaging_limit_tier") if isinstance(data, dict) else None
        quality = data.get("quality_rating") if isinstance(data, dict) else None
        if not tier and is_d360 and isinstance(data, dict):
            tier = data.get("messaging_limit") or data.get("tier")
        if tier:
            return {
                "messaging_limit": tier,
                "quality_rating":  quality,
                "_diagnostics":    diagnostics,
            }
    except Exception as exc:
        _record(f"GET /{phone_id}", None, None, error=f"{type(exc).__name__}: {exc}"[:200])
        logger.warning(
            "[WA] fetch_meta_phone_tier phone_id path failed tenant=%s provider=%s: %s",
            tenant_id, provider, exc,
        )

    # ── 2 & 3) 360dialog-specific fallbacks ───────────────────────────────────
    if is_d360:
        # ``GET /v1/configs`` — channel-level metadata. Some d360 tenants
        # see ``messaging_limit`` on this object (legacy product).
        try:
            data = await provider_get_with_context(
                conn, ctx,
                tenant_id=tenant_id,
                operation="fetch_phone_tier_v1_configs",
                path="v1/configs",
                params=None,
                timeout=15,
            )
            _record("GET /v1/configs", "2xx?", data)
            if isinstance(data, dict):
                tier = (
                    data.get("messaging_limit_tier")
                    or data.get("messaging_limit")
                    or (data.get("phone") or {}).get("messaging_limit_tier") if isinstance(data.get("phone"), dict) else None
                )
                quality = (
                    data.get("quality_rating")
                    or ((data.get("phone") or {}).get("quality_rating") if isinstance(data.get("phone"), dict) else None)
                )
                if tier:
                    return {
                        "messaging_limit": tier,
                        "quality_rating":  quality,
                        "_diagnostics":    diagnostics,
                    }
        except Exception as exc:
            _record("GET /v1/configs", None, None, error=f"{type(exc).__name__}: {exc}"[:200])
            logger.warning(
                "[WA] fetch_meta_phone_tier v1/configs failed tenant=%s: %s",
                tenant_id, exc,
            )

        # ``GET /v1/health/messaging-tier`` — d360 health proxy. Returns
        # 404 for tenants without the feature; that's fine, we record it
        # in diagnostics and return empty.
        try:
            data = await provider_get_with_context(
                conn, ctx,
                tenant_id=tenant_id,
                operation="fetch_phone_tier_health",
                path="v1/health/messaging-tier",
                params=None,
                timeout=15,
            )
            _record("GET /v1/health/messaging-tier", "2xx?", data)
            if isinstance(data, dict):
                tier = data.get("messaging_limit_tier") or data.get("tier") or data.get("messaging_limit")
                if tier:
                    return {
                        "messaging_limit": tier,
                        "quality_rating":  data.get("quality_rating"),
                        "_diagnostics":    diagnostics,
                    }
        except Exception as exc:
            _record("GET /v1/health/messaging-tier", None, None, error=f"{type(exc).__name__}: {exc}"[:200])

    # Nothing worked. Return the diagnostics so the UI can render them
    # and the merchant can see WHY we don't have a fresh tier value.
    return {
        "messaging_limit": None,
        "quality_rating":  None,
        "_diagnostics":    diagnostics,
    }
