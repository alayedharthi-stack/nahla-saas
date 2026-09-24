from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.orm import Session

from core.config import META_GRAPH_API_VERSION
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
    UnsupportedWhatsAppProvider,
    provider_is_supported,
    raw_provider,
    require_supported_provider,
    wa_provider,
)
from .token_manager import (
    WhatsAppTokenContext,
    get_token_for_operation,
    unsupported_provider_context,
)

logger = logging.getLogger("nahla.whatsapp.service")


# ── F18: classification helpers ────────────────────────────────────
# A "send" operation is one whose response is expected to carry a
# ``messages[0].id`` (wamid). For sends, a 2xx response WITHOUT a
# wamid is a provider failure — not a success — and we must surface
# it as such so the caller doesn't persist a misleading "delivered"
# state. Non-send POSTs (template submit, webhook configure, etc.)
# legitimately have no wamid and must NOT be misclassified.

# Path → "is this a send call?". We match by the trailing segment so
# Meta (``{phone_id}/messages``) sends
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
    ``None`` on any structural mismatch. Meta uses
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


UNSUPPORTED_PROVIDER_CODE = "unsupported_provider"


def _unsupported_provider_envelope(conn: Any) -> Dict[str, Any]:
    """The answer for a connection this platform cannot speak for.

    A definite failure, not an ambiguous one: nothing was sent, nothing can
    have been accepted, and the caller must not resend or fall back to Meta.
    Shaped like a provider error envelope so every existing classifier,
    audit row and UI already handles it.
    """
    return {
        "error": {
            "message": (
                "whatsapp connection provider "
                f"{raw_provider(conn)!r} is not supported; Meta WhatsApp Cloud API "
                "is the only supported provider"
            ),
            "type": UNSUPPORTED_PROVIDER_CODE,
            "code": 0,
            "_nahla_unsupported_provider": True,
        }
    }


async def _resolve_token(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    operation: str,
    prefer_platform: bool = False,
) -> WhatsAppTokenContext:
    """The token for this operation — or, for a row naming a retired provider,
    a context that carries no token and reads no credential.

    Every request wrapper below refuses such a context before a request exists,
    so the refusal is definite and recorded; and because the token is resolved
    only for a supported row, no refresh is attempted at Meta's OAuth endpoint
    with another provider's key and no token state is written onto the row.
    """
    if conn is not None and not provider_is_supported(conn):
        logger.error("[WA token] op=%s tenant=%s provider=%r — refused before token "
                     "resolution; Meta WhatsApp Cloud API is the only supported provider",
                     operation, tenant_id, raw_provider(conn))
        return unsupported_provider_context(conn)
    return await get_token_for_operation(
        db, conn, tenant_id=tenant_id, operation=operation, prefer_platform=prefer_platform,
    )


def _provider_base_url(conn: Any) -> str:
    """Meta Graph, or a refusal. A connection this platform cannot speak for is
    never given Meta's base URL: it would send one provider's traffic, with
    another provider's credentials, to a phone number Meta may not hold."""
    require_supported_provider(conn)
    return GRAPH


def _provider_headers(conn: Any, ctx: WhatsAppTokenContext) -> Dict[str, str]:
    require_supported_provider(conn)
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
# slot before the payload hits Meta.
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
    if not provider_is_supported(conn):
        logger.error("[WA provider_get] refused op=%s tenant=%s provider=%s — unsupported",
                     operation, tenant_id, raw_provider(conn))
        return _unsupported_provider_envelope(conn)
    headers = _provider_headers(conn, ctx)
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
    if not provider_is_supported(conn):
        # Refused before the request exists. Recorded like any other definite
        # provider failure so the outcome is "did not send", never "may have
        # been accepted".
        logger.error("[WA provider_post] refused op=%s tenant=%s provider=%s — unsupported",
                     operation, tenant_id, raw_provider(conn))
        refusal = _unsupported_provider_envelope(conn)
        try:
            _record_provider_attempt(
                tenant_id=tenant_id, operation=operation, provider=provider, method="POST",
                full_url="", path=path, request_payload=json,
                headers_summary={"token_source": getattr(ctx, "source", None)},
                response_status=None, response_body=refusal, parsed_wamid=None,
                classification=CLASSIFICATION_PROVIDER_ERROR, duration_ms=None,
                error_text=f"unsupported provider {raw_provider(conn)!r}",
                connection_phone_number_id=(
                    getattr(conn, "phone_number_id", None) if conn is not None else None),
                connection_id=getattr(conn, "id", None) if conn is not None else None,
                connection_type=(
                    getattr(conn, "connection_type", None) if conn is not None else None),
            )
        except Exception:  # noqa: BLE001 - observability is best-effort
            logger.warning("[WA provider_post] could not record the refusal")
        return refusal
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
        if is_send:
            from core.outbound_wire_audit import record_wire_attempt  # noqa: PLC0415

            record_wire_attempt(tenant_id=tenant_id, payload=json or {},
                                operation=operation, classification=CLASSIFICATION_EXCEPTION)
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

    if is_send:
        from core.outbound_wire_audit import record_wire_attempt  # noqa: PLC0415

        record_wire_attempt(tenant_id=tenant_id, payload=json or {}, operation=operation,
                            classification=classification, wamid=wamid)
    # ── Outbound MessageEvent send-status bridge ──────────────────────
    # Attach the F18 classification + parsed wamid + timing to the
    # returned dict so the upstream caller (``_post_wa`` in
    # ``routers.whatsapp_webhook``) can stamp the persisted outbound
    # ``MessageEvent`` row with the wire-layer outcome without
    # re-deriving the classification. We use leading-underscore
    # keys so this metadata cannot collide with any provider field
    # name (Meta responses never carry ``_nahla_*``).
    # Caller is free to ignore these fields — non-send paths
    # (template submit, webhook config) just don't read them.
    if isinstance(data, dict):
        try:
            data["_nahla_classification"] = classification
            data["_nahla_wamid"]          = wamid
            data["_nahla_is_send"]        = is_send
            data["_nahla_duration_ms"]    = duration_ms
            # HTTP status lets the caller tell a DEFINITIVE 4xx rejection
            # (safe to recover with a corrected payload) from an ambiguous
            # 5xx / transport outcome (never resend automatically).
            data["_nahla_http_status"]    = status_code
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
    ctx = await _resolve_token(
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
    ctx = await _resolve_token(
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
    if conn is not None and not provider_is_supported(conn):
        # Refused before anything else happens to this send: no credential is
        # read, no refresh is attempted, no token state is written, and the
        # attempt is recorded as a definite provider failure rather than an
        # ambiguous transport exception. ``provider_post_with_context`` is the
        # one place that refusal is shaped and recorded.
        refused = unsupported_provider_context(conn)
        data = await provider_post_with_context(
            conn,
            refused,
            tenant_id=tenant_id,
            operation=operation,
            path=f"{phone_id}/messages",
            json=dict(payload or {}),
            timeout=timeout,
        )
        return data, refused
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
                    evaluate_campaign_send,
                )

                _msg_type = str(send_payload.get("type") or "text").strip().lower()
                if operation == "campaign_send" and _msg_type == "template" and not allow_manual:
                    _block = evaluate_campaign_send(
                        db, tenant_id=int(tenant_id), customer_phone=formatted_to,
                        blocked_path=blocked_path or operation,
                    )
                else:
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
                                    "Outbound send blocked by Nahla safety policy: "
                                    f"{_block.reason}"
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

    ctx = await _resolve_token(
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
    from core.outbound_wire_audit import observe_wire_payload  # noqa: PLC0415

    observe_wire_payload(tenant_id, send_payload, "provider_payload_assembly")
    send_payload = _scrub_outbound_payload(send_payload)
    observe_wire_payload(tenant_id, send_payload, "provider_marker_scrub")
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
    ctx = await _resolve_token(
        db,
        conn,
        tenant_id=tenant_id,
        operation="template_submit",
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)
    path = f"{waba_id}/message_templates"
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
    """
    ctx = await _resolve_token(
        db, conn,
        tenant_id=tenant_id,
        operation="template_delete",
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)
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
    ctx = await _resolve_token(
        db,
        conn,
        tenant_id=tenant_id,
        operation="template_sync",
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)
    path = f"{waba_id}/message_templates"
    # Explicitly request fields including `status` — without this
    # parameter Meta Graph API v20+ may omit the status field entirely,
    # causing every template to default to PENDING in the sync loop
    # (`item.get("status") or "PENDING"`).
    # `limit=250` avoids missing templates behind pagination.
    params: Optional[Dict[str, Any]] = {
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


# Since October 2025 Meta sets the business-initiated messaging limit per
# business portfolio and deprecated the phone-number field
# ``messaging_limit_tier``; the current value is
# ``whatsapp_business_manager_messaging_limit``. The two are never requested
# together: once Meta rejects the deprecated field it could fail the whole
# request and hide the valid current value. The current field is read first;
# the deprecated one only in a separate fallback request, logged as such.
PORTFOLIO_LIMIT_FIELD = "whatsapp_business_manager_messaging_limit"
LEGACY_LIMIT_FIELD = "messaging_limit_tier"
CURRENT_TIER_FIELDS = f"{PORTFOLIO_LIMIT_FIELD},quality_rating"
LEGACY_TIER_FIELDS = f"{LEGACY_LIMIT_FIELD},quality_rating"


def extract_messaging_limit(data: Any) -> tuple:
    """``(tier, field)`` from a phone-number read; the portfolio field wins.
    Accepts the value as a plain tier string or as an object carrying it."""
    if not isinstance(data, dict) or data.get("error"):
        return None, None
    for key in (PORTFOLIO_LIMIT_FIELD, LEGACY_LIMIT_FIELD):
        raw = data.get(key)
        if isinstance(raw, dict):
            raw = next((raw.get(k) for k in ("current_limit", "tier", "messaging_limit",
                                              "value", "limit") if raw.get(k)), None)
        if isinstance(raw, (str, int)) and str(raw).strip():
            return str(raw).strip(), key
    return None, None


async def fetch_meta_portfolio_id(
    conn: Any, ctx: "WhatsAppTokenContext", *, tenant_id: Optional[int] = None,
) -> Optional[str]:
    """The business portfolio that owns the connection's WABA
    (``GET /{waba_id}?fields=owner_business_info``) — the scope Meta's
    messaging limit applies to. None when Meta does not say."""
    waba_id = getattr(conn, "whatsapp_business_account_id", None)
    if not waba_id or not getattr(ctx, "token", None):
        return None
    try:
        data = await provider_get_with_context(
            conn, ctx, tenant_id=tenant_id, operation="fetch_waba_owner",
            path=f"{waba_id}", params={"fields": "owner_business_info"}, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[WA] fetch_meta_portfolio_id tenant=%s failed: %s", tenant_id, exc)
        return None
    info = data.get("owner_business_info") if isinstance(data, dict) else None
    pid = info.get("id") if isinstance(info, dict) else None
    return str(pid).strip() if pid else None


async def fetch_meta_phone_tier(
    conn: Any,
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fetch messaging_limit and quality_rating for this connection's phone
    number from Meta Graph: ``GET /{phone_id}?fields=...``.

    Return shape ALWAYS includes a ``_diagnostics`` block listing the path
    tried, the HTTP status (best-effort), and a redacted snippet of the
    response. The UI surfaces this so the merchant can see WHY we still show
    e.g. ``TIER_250`` after Meta granted them a higher tier.

    On any failure we leave the cached row untouched (return empty
    ``messaging_limit``); the UI flags it as stale.
    """
    phone_id = getattr(conn, "phone_number_id", None)
    provider = wa_provider(conn)

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

    async def _read(fields: str) -> Any:
        try:
            data = await provider_get_with_context(
                conn, ctx,
                tenant_id=tenant_id,
                operation="fetch_phone_tier",
                path=f"{phone_id}",
                params={"fields": fields},
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            _record(f"GET /{phone_id}?fields={fields}", None, None,
                    error=f"{type(exc).__name__}: {exc}"[:200])
            logger.warning(
                "[WA] fetch_meta_phone_tier %s failed tenant=%s provider=%s: %s",
                fields, tenant_id, provider, exc,
            )
            return None
        _record(f"GET /{phone_id}?fields={fields}",
                "error" if isinstance(data, dict) and data.get("error") else "2xx?", data)
        return data

    # ── 1. The current, portfolio-level field ────────────────────────────────
    data = await _read(CURRENT_TIER_FIELDS)
    tier, tier_field = extract_messaging_limit(data)
    quality = data.get("quality_rating") if isinstance(data, dict) and not data.get("error") else None
    if tier:
        return {
            "messaging_limit": tier,
            "messaging_limit_field": tier_field,
            "messaging_limit_source": "current_field",
            "quality_rating":  quality,
            "_diagnostics":    diagnostics,
        }

    # ── 2. Fallback: the deprecated per-number field, in its own request ─────
    legacy = await _read(LEGACY_TIER_FIELDS)
    tier, tier_field = extract_messaging_limit(legacy)
    if tier:
        logger.warning(
            "[WA] fetch_meta_phone_tier tenant=%s: %s unavailable, using deprecated %s=%s",
            tenant_id, PORTFOLIO_LIMIT_FIELD, tier_field, tier,
        )
        if quality is None and isinstance(legacy, dict):
            quality = legacy.get("quality_rating")
        return {
            "messaging_limit": tier,
            "messaging_limit_field": tier_field,
            "messaging_limit_source": "legacy_fallback",
            "quality_rating":  quality,
            "_diagnostics":    diagnostics,
        }

    # Nothing worked. Return the diagnostics so the UI can render them
    # and the merchant can see WHY we don't have a fresh tier value.
    return {
        "messaging_limit": None,
        "messaging_limit_source": "failed",
        "quality_rating":  quality,
        "_diagnostics":    diagnostics,
    }
