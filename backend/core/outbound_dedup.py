"""
core/outbound_dedup.py
──────────────────────
In-process idempotency guard for **outbound** WhatsApp sends.

Why
===
Several recovery / retry paths can cause the SAME logical AI reply to
reach ``_post_wa`` more than once:

1. Webhook redelivery (Meta / 360dialog retries an inbound POST that
   times out at our edge). The inbound dedup cache catches most of
   these within its 10-minute TTL — but workers restart, the cache
   evicts, and a redelivery 11 minutes later regenerates the same
   AI response from scratch.
2. ``auto-register → retry`` path inside ``_post_wa``. Already
   protected per-process via ``_AUTO_REREGISTERED_PHONE_IDS``, but
   that set is in-memory and resets on reload.
3. Worker restart / blue-green deploy: the AI pipeline persists a
   MessageEvent with ``provider_send.status='queued'`` and then
   crashes before the POST returns. A future heuristic recovery
   task (or a merchant manually pressing "resend") could re-issue.
4. Manual ``/conversations/reply`` collision: the merchant double-
   clicks the send button or the dashboard retries on a 502.

The classification helper in ``core/outbound_send_status.py`` already
refuses to overwrite a row that's already ``sent``/``failed``, so the
STATUS column is idempotent. But that doesn't stop a second physical
HTTP POST from reaching Meta — the merchant's customer would then
receive the same Arabic paragraph twice.

This module is the upstream guard: it short-circuits ``_post_wa``
when an identical outbound (same tenant, same recipient, same body)
was already attempted within the dedup window.

Design notes
============
* **Deterministic key.** Hash of ``(tenant_id, normalized_recipient,
  message_signature)``. ``message_signature`` is a SHA-256 of the
  payload's user-visible content (text body, template name +
  parameters, media URL). Headers, request IDs, transient ``ctx``
  fields are excluded so retries match the original.
* **Cache value carries the wamid.** When the first POST succeeded
  with a ``wamid`` we keep it. Subsequent calls within the TTL get a
  ``DedupResult(skip=True, wamid=<prior>)`` so the caller can re-
  stamp the new MessageEvent with the existing wamid AND still
  return ``True`` from the bool-returning ``_post_wa`` (the customer
  IS receiving the reply — the first POST handled it).
* **In-flight marker.** While the first POST is awaiting the wire,
  the cache holds ``wamid=None, in_flight=True``. Concurrent calls
  see ``in_flight`` and SKIP without sending. They get a
  ``DedupResult(skip=True, wamid=None, reason="in_flight")`` which
  callers interpret as "another worker has this — don't send,
  don't claim failure".
* **TTL.** 5 minutes by default. Longer than any sane retry window,
  much shorter than the 24-hour service window so the cache can't
  drift across distinct merchant-intent sends.
* **Pure-Python.** Same shape as ``inbound_dedup``. No infra cost.
  Per-process; across multiple uvicorn workers a duplicate can
  theoretically slip through (worst case: 2 workers race the same
  retry), but the wire-layer ``stamp_outbound_send_status`` filter
  on ``status_text == QUEUED`` keeps the STATUS column consistent.

Public API
==========

    from core.outbound_dedup import (
        check_outbound_send,     # called BEFORE provider_send_message
        record_outbound_result,  # called AFTER provider_send_message
    )

    res = check_outbound_send(tenant_id=t, recipient=r, payload=p)
    if res.skip:
        # short-circuit: do not POST, stamp the row, return ok
        return True

    resp_data, ctx = await provider_send_message(...)
    record_outbound_result(
        tenant_id=t, recipient=r, payload=p,
        wamid=resp_data.get("_nahla_wamid"),
        succeeded="error" not in (resp_data or {}),
    )

Both helpers are crash-safe: if anything goes wrong inside the
dedup logic they return "no skip / no record" and log a warning.
The send path MUST NEVER be blocked by a dedup bug.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.outbound_dedup")

# (tenant_id, recipient_suffix, message_signature) → _CacheEntry
_CACHE: Dict[Tuple[str, str, str], "_CacheEntry"] = {}
_LOCK = threading.Lock()

_DEFAULT_TTL_SECONDS = 300.0
_SWEEP_BUDGET = 64


@dataclass
class _CacheEntry:
    expires_at: float
    in_flight:  bool
    wamid:      Optional[str]
    succeeded:  Optional[bool]


@dataclass
class DedupResult:
    """Returned by :func:`check_outbound_send`.

    Fields
    ------
    skip
        ``True`` when the caller should NOT POST to the provider.
        The caller should either return ``True`` (if ``wamid`` is
        set — the message reached Meta on a prior attempt) or
        return ``False`` while logging the reason (``in_flight``
        means another concurrent caller is already POSTing it).
    wamid
        wamid from the prior successful POST when available, else
        ``None``.
    reason
        One of ``"in_flight"`` / ``"already_sent"`` /
        ``"already_failed"`` / ``""``. Empty string when
        ``skip=False``.
    """
    skip:   bool
    wamid:  Optional[str]
    reason: str


def _phone_suffix(phone: str) -> str:
    """Last 9 digits — same shape as ``stamp_outbound_send_status``
    uses for its JSONB suffix match. Cheap normalisation that
    survives ``+966``/``966``/``00966``/``05`` variations."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if digits else ""


def _payload_signature(payload: Dict[str, Any]) -> str:
    """SHA-256 over a canonicalized view of the payload. We
    intentionally hash a **subset** so transport-level fields
    (``messaging_product``, ``to``, ``recipient_type``) don't
    create artificial diversity for identical merchant intent.

    Hashed components:
        * ``type``
        * ``text.body`` (or ``image.link`` / ``audio.link`` etc.)
        * ``template.name`` + ``template.language.code`` +
          flattened parameter values
        * ``interactive.body.text`` + button labels
    """
    try:
        canon: Dict[str, Any] = {"type": payload.get("type")}

        t = payload.get("type")
        if t == "text":
            body = ((payload.get("text") or {}).get("body") or "")
            canon["body"] = body.strip()
        elif t == "template":
            tpl = payload.get("template") or {}
            canon["name"] = tpl.get("name") or ""
            canon["lang"] = (tpl.get("language") or {}).get("code") or ""
            # Flatten component params so reordering keys doesn't
            # spuriously change the signature.
            params: list = []
            for comp in (tpl.get("components") or []):
                ctype = comp.get("type")
                for p in (comp.get("parameters") or []):
                    val = (
                        p.get("text")
                        or (p.get("currency") or {}).get("fallback_value")
                        or (p.get("date_time") or {}).get("fallback_value")
                        or (p.get("image") or {}).get("link")
                        or (p.get("document") or {}).get("link")
                        or ""
                    )
                    params.append(f"{ctype}:{val}")
            canon["params"] = params
        elif t in ("image", "video", "audio", "document"):
            media = payload.get(t) or {}
            canon["link"]    = media.get("link") or ""
            canon["caption"] = (media.get("caption") or "").strip()
        elif t == "interactive":
            inter = payload.get("interactive") or {}
            canon["body"] = ((inter.get("body") or {}).get("text") or "").strip()
            btns = []
            action = inter.get("action") or {}
            for b in (action.get("buttons") or []):
                rb = b.get("reply") or {}
                btns.append(f"{rb.get('id')}:{rb.get('title')}")
            canon["btns"] = btns

        raw = json.dumps(canon, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except Exception:
        # If anything weird is in the payload, fall back to a
        # whole-payload hash. Better to over-isolate (a few rows
        # not deduped) than to under-isolate (different intents
        # collapsing into one).
        try:
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
        except Exception:
            return hashlib.sha256(str(payload).encode("utf-8", "replace")).hexdigest()


def _sweep_locked(now: float) -> None:
    """Drop up to ``_SWEEP_BUDGET`` expired entries. Must be called
    under ``_LOCK``."""
    swept = 0
    for k, entry in list(_CACHE.items()):
        if entry.expires_at <= now:
            _CACHE.pop(k, None)
            swept += 1
            if swept >= _SWEEP_BUDGET:
                break


def check_outbound_send(
    *,
    tenant_id: Optional[int],
    recipient: str,
    payload: Dict[str, Any],
    ttl_seconds: float = _DEFAULT_TTL_SECONDS,
) -> DedupResult:
    """Decide whether to POST to the provider.

    Returns a :class:`DedupResult`. The caller MUST treat
    ``skip=True`` as authoritative: do not POST, do not retry on
    transport error (another worker / earlier attempt owns this).
    """
    try:
        if tenant_id is None:
            return DedupResult(skip=False, wamid=None, reason="")
        suffix = _phone_suffix(recipient)
        if not suffix:
            return DedupResult(skip=False, wamid=None, reason="")
        sig = _payload_signature(payload or {})
        key: Tuple[str, str, str] = (str(tenant_id), suffix, sig)
        now = time.monotonic()
        with _LOCK:
            _sweep_locked(now)
            entry = _CACHE.get(key)
            if entry is not None and entry.expires_at > now:
                if entry.in_flight:
                    logger.warning(
                        "[outbound_dedup] suppressed concurrent send "
                        "tenant=%s to=*%s sig=%s reason=in_flight",
                        tenant_id, suffix[-4:], sig[:10],
                    )
                    return DedupResult(
                        skip=True, wamid=None, reason="in_flight",
                    )
                if entry.succeeded is True:
                    logger.warning(
                        "[outbound_dedup] suppressed duplicate send "
                        "tenant=%s to=*%s sig=%s reason=already_sent "
                        "wamid=%s",
                        tenant_id, suffix[-4:], sig[:10],
                        (entry.wamid or "")[-8:] or None,
                    )
                    return DedupResult(
                        skip=True, wamid=entry.wamid,
                        reason="already_sent",
                    )
                # entry.succeeded is False or None → previous attempt
                # failed. We deliberately ALLOW the retry by falling
                # through; failed sends SHOULD be retryable. We still
                # reset the in-flight marker.
            # Mark this attempt as in-flight so concurrent calls
            # within ``ttl_seconds`` skip.
            _CACHE[key] = _CacheEntry(
                expires_at=now + ttl_seconds,
                in_flight=True,
                wamid=None,
                succeeded=None,
            )
        return DedupResult(skip=False, wamid=None, reason="")
    except Exception as exc:  # noqa: BLE001
        # Never block a send because of a dedup bug.
        logger.warning("[outbound_dedup] check failed: %s", exc)
        return DedupResult(skip=False, wamid=None, reason="")


def record_outbound_result(
    *,
    tenant_id: Optional[int],
    recipient: str,
    payload: Dict[str, Any],
    wamid: Optional[str],
    succeeded: bool,
    ttl_seconds: float = _DEFAULT_TTL_SECONDS,
) -> None:
    """Record the outcome of a real POST so future duplicate calls
    within ``ttl_seconds`` short-circuit. Crash-safe (logs and
    swallows any exception)."""
    try:
        if tenant_id is None:
            return
        suffix = _phone_suffix(recipient)
        if not suffix:
            return
        sig = _payload_signature(payload or {})
        key: Tuple[str, str, str] = (str(tenant_id), suffix, sig)
        now = time.monotonic()
        with _LOCK:
            _sweep_locked(now)
            _CACHE[key] = _CacheEntry(
                expires_at=now + ttl_seconds,
                in_flight=False,
                wamid=wamid or None,
                succeeded=bool(succeeded),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[outbound_dedup] record failed: %s", exc)


def clear_outbound_dedup() -> None:
    """Test helper. Drops the entire cache."""
    with _LOCK:
        _CACHE.clear()
