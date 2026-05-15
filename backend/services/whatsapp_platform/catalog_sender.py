"""
services/whatsapp_platform/catalog_sender.py
─────────────────────────────────────────────
Provider-agnostic Meta WhatsApp Catalog sender.

Builds and dispatches ``interactive.type = "product"`` (single product
card) and ``interactive.type = "product_list"`` (multi-product, up to
30 across 10 sections) payloads. Both 360dialog and Meta Cloud API
accept the same JSON body — the dispatching layer
(``provider_send_message``) already abstracts the URL/headers, so this
module only owns:

1. Payload construction (the schema bits unique to catalog messages).
2. Eligibility check (delegated to :mod:`core.catalog`).
3. Structured outcome objects so callers can log
   ``[CATALOG_SEND_SUCCESS]`` / ``[CATALOG_SEND_FAILED]`` /
   ``[CATALOG_FALLBACK_TEXT]`` consistently.

What this module deliberately does NOT do
─────────────────────────────────────────
* Decide which products to send — that's the caller's job (the
  ``[PRODUCT:...]`` resolver in ``whatsapp_webhook.py``).
* Persist anything — no DB writes here. The webhook owns the outbound
  message row, the delivery-events row, and the dedup cache.
* Fall back to the legacy image+CTA path on its own. The caller is
  responsible for the fallback because the legacy path needs the
  conversation context (convo id, tenant id, the `_send_media_message`
  / `_send_cta_url` helpers). We return a ``CatalogSendResult`` with
  ``fallback_recommended=True`` and the caller invokes the legacy
  senders. This keeps the wire-layer Single-Responsibility intact.

Logging convention
──────────────────
Every send attempt emits exactly two log lines:

* ``[CATALOG_SEND_ATTEMPT]`` — before the HTTP POST. Includes
  tenant_id, the truncated recipient, the retailer_id(s), section
  count for multi-product.
* ``[CATALOG_SEND_SUCCESS]`` or ``[CATALOG_SEND_FAILED]`` — after.
  Success includes the message id from the provider; failure includes
  the error string and the provider's HTTP status when available.

Eligibility short-circuits emit ``[CATALOG_NOT_ELIGIBLE]`` instead so
ops can grep the reason without parsing exception traces.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from core.catalog import (
    CatalogEligibility,
    effective_retailer_id,
    is_catalog_eligible,
)

from .service import provider_send_message

logger = logging.getLogger("nahla.catalog_sender")

# Meta hard limits for ``interactive.type = "product_list"``:
#   * up to 10 sections,
#   * up to 30 products total across all sections.
# Validated client-side so we never trip a 400 from Meta and waste a
# round-trip.
MAX_SECTIONS = 10
MAX_PRODUCTS_TOTAL = 30
MAX_BODY_LEN = 1024
MAX_FOOTER_LEN = 60
MAX_HEADER_LEN = 60


# ─────────────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CatalogSection:
    """One section inside a ``product_list`` message.

    *title* is the section header shown to the customer (e.g.
    "عسل السدر", "أكثر مبيعاً"). *retailer_ids* lists the Meta
    retailer ids in display order. Empty sections are silently
    dropped before the payload is built.
    """
    title: str
    retailer_ids: Sequence[str]


@dataclass
class CatalogSendResult:
    """Outcome returned by every catalog send call.

    Callers read three fields:

    * ``success`` — True iff the provider returned 2xx AND a message id.
    * ``fallback_recommended`` — True when we did NOT send a catalog
      message (either eligibility failed or the provider rejected the
      payload). The caller MUST invoke the legacy image+CTA path so
      the customer still gets a reply.
    * ``message_id`` — the provider's wamid on success. None
      otherwise.

    ``reason`` mirrors the
    :class:`core.catalog.CatalogEligibility.reason` vocabulary, plus
    additional send-time values: ``"provider_error"``,
    ``"transport_error"``, ``"sent"``.
    """
    success: bool
    fallback_recommended: bool
    reason: str
    message_id: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Payload builders (pure functions — easy to snapshot test)
# ─────────────────────────────────────────────────────────────────────────────

def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    # Soft truncation — keep the head, leave room for the ellipsis.
    return text[: max(limit - 1, 1)] + "…"


def build_single_product_payload(
    *,
    to: str,
    catalog_id: str,
    retailer_id: str,
    body_text: str,
    footer_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct the JSON body for a single-product catalog message.

    Schema reference (Meta Cloud API + 360dialog parity):
    https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages

    ``body`` is required by Meta; we always set a sensible default
    when the caller passes an empty string so we don't 400 on edge
    cases. ``footer`` is optional and capped at 60 chars by Meta.
    """
    body = _truncate(body_text, MAX_BODY_LEN) or "تفضّل المنتج 👇"
    interactive: Dict[str, Any] = {
        "type": "product",
        "body": {"text": body},
        "action": {
            "catalog_id": str(catalog_id),
            "product_retailer_id": str(retailer_id),
        },
    }
    footer = _truncate(footer_text, MAX_FOOTER_LEN)
    if footer:
        interactive["footer"] = {"text": footer}
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }


def build_product_list_payload(
    *,
    to: str,
    catalog_id: str,
    sections: Sequence[CatalogSection],
    body_text: str,
    header_text: Optional[str] = None,
    footer_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct the JSON body for a multi-product catalog message.

    Drops empty sections (no retailer_ids) before counting. Truncates
    the section list to ``MAX_SECTIONS`` and the global product count
    to ``MAX_PRODUCTS_TOTAL`` so we never trip a Meta 400. Section
    titles are truncated to 24 chars (Meta's documented cap) inside
    the payload builder, not by callers.
    """
    cleaned_sections: List[Dict[str, Any]] = []
    products_seen = 0
    for sec in sections:
        ids = [str(rid).strip() for rid in (sec.retailer_ids or []) if rid]
        if not ids:
            continue
        if products_seen >= MAX_PRODUCTS_TOTAL:
            break
        remaining = MAX_PRODUCTS_TOTAL - products_seen
        if len(ids) > remaining:
            ids = ids[:remaining]
        products_seen += len(ids)
        cleaned_sections.append({
            "title": (_truncate(sec.title, 24) or "المنتجات"),
            "product_items": [
                {"product_retailer_id": rid} for rid in ids
            ],
        })
        if len(cleaned_sections) >= MAX_SECTIONS:
            break

    if not cleaned_sections:
        raise ValueError(
            "build_product_list_payload: no non-empty sections — "
            "caller must pre-filter or route to single-product send."
        )

    body = _truncate(body_text, MAX_BODY_LEN) or "اخترلك أنسب الخيارات 👇"
    interactive: Dict[str, Any] = {
        "type": "product_list",
        "body": {"text": body},
        "action": {
            "catalog_id": str(catalog_id),
            "sections": cleaned_sections,
        },
    }
    header = _truncate(header_text, MAX_HEADER_LEN)
    if header:
        interactive["header"] = {"type": "text", "text": header}
    footer = _truncate(footer_text, MAX_FOOTER_LEN)
    if footer:
        interactive["footer"] = {"text": footer}
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Send wrappers (eligibility + dispatch + structured result)
# ─────────────────────────────────────────────────────────────────────────────

def _phone_suffix(to: str) -> str:
    s = str(to or "")
    return s[-4:] if len(s) >= 4 else "****"


def _extract_message_id(resp: Dict[str, Any]) -> Optional[str]:
    """Pull the wamid out of the provider response.

    Both 360dialog and Meta Cloud return ``{"messages": [{"id": "..."}]}``.
    Defensive — log and return None on shape mismatch so the caller can
    flag a failure without crashing the conversation.
    """
    try:
        msgs = (resp or {}).get("messages") or []
        if msgs and isinstance(msgs, list):
            mid = msgs[0].get("id")
            if mid:
                return str(mid)
    except Exception:  # noqa: BLE001
        pass
    return None


def _eligibility_to_result(
    elig: CatalogEligibility,
    *,
    tenant_id: Optional[int],
    to: str,
) -> CatalogSendResult:
    logger.info(
        "[CATALOG_NOT_ELIGIBLE] tenant=%s to=*%s reason=%s",
        tenant_id, _phone_suffix(to), elig.reason,
    )
    return CatalogSendResult(
        success=False,
        fallback_recommended=True,
        reason=elig.reason,
    )


async def send_single_product_message(
    db: Session,
    connection: Any,
    *,
    tenant_id: Optional[int],
    to: str,
    phone_id: str,
    retailer_id: str,
    body_text: str,
    footer_text: Optional[str] = None,
    timeout: float = 20.0,
) -> CatalogSendResult:
    """Send a single-product card via Meta WhatsApp Catalog.

    Returns a :class:`CatalogSendResult` — never raises for routine
    failures (eligibility, provider 4xx/5xx, transport hiccups). Only
    a programmer error (missing arguments) raises ``TypeError`` /
    ``ValueError`` so the caller's typing is enforced.

    On any non-success result the caller MUST honour
    ``fallback_recommended`` and invoke the legacy image+CTA path.
    Silence is never acceptable.
    """
    retailer_id = (retailer_id or "").strip()
    if not retailer_id:
        return _eligibility_to_result(
            CatalogEligibility(ok=False, reason="no_retailer_id"),
            tenant_id=tenant_id, to=to,
        )
    elig = is_catalog_eligible(connection, products=None)
    if not elig.ok:
        return _eligibility_to_result(elig, tenant_id=tenant_id, to=to)
    catalog_id = str(getattr(connection, "meta_catalog_id", "") or "").strip()

    payload = build_single_product_payload(
        to=to,
        catalog_id=catalog_id,
        retailer_id=retailer_id,
        body_text=body_text,
        footer_text=footer_text,
    )

    logger.info(
        "[CATALOG_SEND_ATTEMPT] tenant=%s to=*%s kind=single "
        "catalog_id=%s retailer_id=%s body_len=%d",
        tenant_id, _phone_suffix(to), catalog_id, retailer_id,
        len(payload["interactive"]["body"]["text"]),
    )

    try:
        resp, _ctx = await provider_send_message(
            db, connection,
            tenant_id=tenant_id,
            operation="send_catalog_product",
            phone_id=phone_id,
            payload=payload,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[CATALOG_SEND_FAILED] tenant=%s to=*%s kind=single "
            "reason=transport_error err=%s",
            tenant_id, _phone_suffix(to), exc,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="transport_error",
            error=str(exc),
        )

    message_id = _extract_message_id(resp)
    if not message_id:
        logger.error(
            "[CATALOG_SEND_FAILED] tenant=%s to=*%s kind=single "
            "reason=provider_error response=%r",
            tenant_id, _phone_suffix(to), (resp or {}).get("error") or resp,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="provider_error",
            raw_response=resp or {},
            error=str((resp or {}).get("error") or ""),
        )

    logger.info(
        "[CATALOG_SEND_SUCCESS] tenant=%s to=*%s kind=single "
        "wamid=...%s retailer_id=%s",
        tenant_id, _phone_suffix(to), message_id[-8:], retailer_id,
    )
    return CatalogSendResult(
        success=True,
        fallback_recommended=False,
        reason="sent",
        message_id=message_id,
        raw_response=resp or {},
    )


async def send_multi_product_message(
    db: Session,
    connection: Any,
    *,
    tenant_id: Optional[int],
    to: str,
    phone_id: str,
    sections: Sequence[CatalogSection],
    body_text: str,
    header_text: Optional[str] = None,
    footer_text: Optional[str] = None,
    timeout: float = 20.0,
) -> CatalogSendResult:
    """Send a multi-product card via Meta WhatsApp Catalog.

    Same contract as :func:`send_single_product_message`: never raises
    for routine failures, always returns ``CatalogSendResult`` with
    ``fallback_recommended`` set when the caller should switch to the
    legacy image+CTA path.

    Empty sections are dropped and product counts are capped to the
    Meta-documented limits before the payload is built — callers can
    pass large iterables without pre-trimming.
    """
    # Quick eligibility against the connection itself; per-product
    # retailer_id presence is checked by the section sanitiser below
    # (we need to know the catalog_id BEFORE building the payload).
    elig = is_catalog_eligible(connection, products=None)
    if not elig.ok:
        return _eligibility_to_result(elig, tenant_id=tenant_id, to=to)
    catalog_id = str(getattr(connection, "meta_catalog_id", "") or "").strip()

    try:
        payload = build_product_list_payload(
            to=to,
            catalog_id=catalog_id,
            sections=sections,
            body_text=body_text,
            header_text=header_text,
            footer_text=footer_text,
        )
    except ValueError as ve:
        logger.info(
            "[CATALOG_NOT_ELIGIBLE] tenant=%s to=*%s reason=no_retailer_id "
            "(no non-empty sections after sanitise) err=%s",
            tenant_id, _phone_suffix(to), ve,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="no_retailer_id",
            error=str(ve),
        )

    section_count = len(payload["interactive"]["action"]["sections"])
    product_count = sum(
        len(s.get("product_items") or [])
        for s in payload["interactive"]["action"]["sections"]
    )
    logger.info(
        "[CATALOG_SEND_ATTEMPT] tenant=%s to=*%s kind=multi "
        "catalog_id=%s sections=%d products=%d body_len=%d",
        tenant_id, _phone_suffix(to), catalog_id,
        section_count, product_count,
        len(payload["interactive"]["body"]["text"]),
    )

    try:
        resp, _ctx = await provider_send_message(
            db, connection,
            tenant_id=tenant_id,
            operation="send_catalog_product_list",
            phone_id=phone_id,
            payload=payload,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[CATALOG_SEND_FAILED] tenant=%s to=*%s kind=multi "
            "reason=transport_error err=%s",
            tenant_id, _phone_suffix(to), exc,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="transport_error",
            error=str(exc),
        )

    message_id = _extract_message_id(resp)
    if not message_id:
        logger.error(
            "[CATALOG_SEND_FAILED] tenant=%s to=*%s kind=multi "
            "reason=provider_error response=%r",
            tenant_id, _phone_suffix(to), (resp or {}).get("error") or resp,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="provider_error",
            raw_response=resp or {},
            error=str((resp or {}).get("error") or ""),
        )

    logger.info(
        "[CATALOG_SEND_SUCCESS] tenant=%s to=*%s kind=multi "
        "wamid=...%s sections=%d products=%d",
        tenant_id, _phone_suffix(to), message_id[-8:],
        section_count, product_count,
    )
    return CatalogSendResult(
        success=True,
        fallback_recommended=False,
        reason="sent",
        message_id=message_id,
        raw_response=resp or {},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: turn a list of products into a CatalogSection
# ─────────────────────────────────────────────────────────────────────────────

def products_to_section(
    title: str,
    products: Sequence[Any],
) -> CatalogSection:
    """Build a :class:`CatalogSection` from ORM products / dicts.

    Skips products with no resolvable retailer id. Used by the webhook
    wiring (phase 4) so the call site stays declarative:

    .. code-block:: python

        section = products_to_section("الأكثر مبيعاً", top_products)
        await send_multi_product_message(... sections=[section] ...)
    """
    ids: List[str] = []
    for p in products or []:
        rid = effective_retailer_id(p)
        if rid:
            ids.append(rid)
    return CatalogSection(title=title or "المنتجات", retailer_ids=tuple(ids))


__all__: List[str] = [
    "CatalogSection",
    "CatalogSendResult",
    "MAX_PRODUCTS_TOTAL",
    "MAX_SECTIONS",
    "build_product_list_payload",
    "build_single_product_payload",
    "products_to_section",
    "send_multi_product_message",
    "send_single_product_message",
]
