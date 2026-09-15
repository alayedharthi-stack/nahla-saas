"""Canonical, transport-neutral conversation presentation contract.

The WhatsApp wire payload is the authoritative description of what the
customer was sent.  This module projects that payload into a compact JSON
``ResponseBundle`` which can be persisted on ``MessageEvent.metadata`` and
rendered by the merchant dashboard (or a future internal test channel).

The projection is deliberately presentation-only: it never decides what to
send, changes a payload, or invents commerce facts.  Product/order enrichment
is performed only from tenant-scoped persisted rows.  Legacy messages degrade
to their stored body when richer metadata is unavailable.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional


RESPONSE_BUNDLE_VERSION = "response_bundle_v1"
PRESENTATION_VERSION = "message_presentation_v1"
_BUTTON_SEPARATOR = "━━━━━"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
_VALID_KINDS = {
    "text",
    "media",
    "product",
    "template",
    "order_lifecycle",
    "interactive",
}
_VALID_ACTION_KINDS = {
    "quick_reply",
    "url",
    "open_product",
    "open_catalog",
    "tracking",
    "copy_code",
    "phone",
}
_VALID_DELIVERY_STATES = {"queued", "sent", "delivered", "read", "failed", "suppressed", "unknown"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _http_url(value: Any) -> Optional[str]:
    raw = _text(value)
    if raw.lower().startswith(("https://", "http://")) and "{" not in raw and "}" not in raw:
        return raw
    return None


def _media_url(value: Any) -> Optional[str]:
    raw = _text(value)
    if raw.startswith("/") or raw.lower().startswith(("https://", "http://")):
        return raw
    return None


def _replace_numeric_placeholders(source: Any, values: Iterable[Any]) -> str:
    text = str(source or "")
    resolved = [str(value or "") for value in values]

    def repl(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return resolved[index] if 0 <= index < len(resolved) else match.group(0)

    return _PLACEHOLDER_RE.sub(repl, text)


def _replace_known_placeholders(source: Any, values: Dict[str, Any]) -> str:
    text = str(source or "")
    for placeholder, value in values.items():
        text = text.replace(str(placeholder), str(value or ""))
    return text


def _delivery_from_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    provider = meta.get("provider_send") if isinstance(meta.get("provider_send"), dict) else {}
    state = _text(meta.get("delivery_status")).lower()
    if not state:
        if meta.get("_status_read"):
            state = "read"
        elif meta.get("_status_delivered"):
            state = "delivered"
        elif meta.get("_status_failed"):
            state = "failed"
        else:
            state = _text(provider.get("status")).lower() or "unknown"
    if state not in _VALID_DELIVERY_STATES:
        state = "unknown"
    error = provider.get("error") if isinstance(provider.get("error"), dict) else None
    if error is None and isinstance(meta.get("delivery_error"), list):
        error = {"provider_errors": deepcopy(meta.get("delivery_error"))}
    return {
        "state": state,
        "wamid": _text(provider.get("wamid") or meta.get("wa_message_id")) or None,
        "error": deepcopy(error) if error else None,
    }


def _normalise_action(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    kind = _text(raw.get("kind")).lower()
    label = _text(raw.get("label"))
    if kind not in _VALID_ACTION_KINDS or not label:
        return None
    out: Dict[str, Any] = {"kind": kind, "label": label}
    url = _http_url(raw.get("url"))
    if url:
        out["url"] = url
    payload = _text(raw.get("payload"))
    if payload:
        out["payload"] = payload[:256]
    return out


def _normalise_media(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    kind = _text(raw.get("kind")).lower()
    if kind not in {"image", "video", "audio", "document"}:
        return None
    return {
        "kind": kind,
        "url": _media_url(raw.get("url") or raw.get("storage_url")),
        "mime_type": _text(raw.get("mime_type")) or None,
        "caption": _text(raw.get("caption")) or None,
        "filename": _text(raw.get("filename")) or None,
        "load_state": _text(raw.get("load_state") or raw.get("download_status")) or None,
        "error": _text(raw.get("error")) or None,
    }


def normalise_response_bundle(raw: Any, *, delivery: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Validate untrusted/legacy JSON into the closed presentation contract."""
    if not isinstance(raw, dict):
        return None
    items = raw.get("presentations")
    if not isinstance(items, list):
        return None
    presentations: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("kind")).lower()
        if kind not in _VALID_KINDS:
            continue
        body = str(item.get("body") or "")
        normalised: Dict[str, Any] = {
            "version": PRESENTATION_VERSION,
            "kind": kind,
            "body": body,
        }
        direction = _text(item.get("text_direction")).lower()
        if direction in {"auto", "rtl", "ltr"}:
            normalised["text_direction"] = direction
        media = _normalise_media(item.get("media"))
        if media:
            normalised["media"] = media
        product = item.get("product")
        if isinstance(product, dict):
            normalised["product"] = {
                "id": _text(product.get("id")) or None,
                "retailer_id": _text(product.get("retailer_id")) or None,
                "name": _text(product.get("name")) or None,
                "image_url": _http_url(product.get("image_url")),
                "price": _text(product.get("price")) or None,
                "currency": _text(product.get("currency")) or None,
                "availability": (
                    bool(product.get("availability"))
                    if isinstance(product.get("availability"), bool)
                    else None
                ),
                "url": _http_url(product.get("url")),
            }
        template = item.get("template")
        if isinstance(template, dict):
            normalised["template"] = {
                "name": _text(template.get("name")) or None,
                "category": _text(template.get("category")) or None,
                "language": _text(template.get("language")) or None,
                "service_key": _text(template.get("service_key")) or None,
                "footer": _text(template.get("footer")) or None,
            }
        order = item.get("order")
        if isinstance(order, dict):
            normalised["order"] = {
                "id": _text(order.get("id")) or None,
                "reference": _text(order.get("reference")) or None,
                "status": _text(order.get("status")) or None,
                "lifecycle": _text(order.get("lifecycle")) or None,
            }
        actions = [_normalise_action(action) for action in (item.get("actions") or [])]
        normalised["actions"] = [action for action in actions if action]
        presentations.append(normalised)
    if not presentations:
        return None
    return {
        "version": RESPONSE_BUNDLE_VERSION,
        "presentations": presentations,
        "delivery": delivery or raw.get("delivery") or {"state": "unknown", "wamid": None, "error": None},
    }


def _product_for_retailer(db: Any, tenant_id: Optional[int], retailer_id: str) -> Optional[Dict[str, Any]]:
    if db is None or not tenant_id or not retailer_id:
        return None
    try:
        from models import Product, ProductVariant  # noqa: PLC0415
        from sqlalchemy import or_  # noqa: PLC0415

        variant = (
            db.query(ProductVariant)
            .filter(
                ProductVariant.tenant_id == int(tenant_id),
                ProductVariant.retailer_id == retailer_id,
            )
            .first()
        )
        product = None
        if variant is not None:
            product = (
                db.query(Product)
                .filter(Product.tenant_id == int(tenant_id), Product.id == variant.product_id)
                .first()
            )
        if product is None:
            product = (
                db.query(Product)
                .filter(
                    Product.tenant_id == int(tenant_id),
                    or_(
                        Product.meta_retailer_id == retailer_id,
                        Product.canonical_retailer_id == retailer_id,
                        Product.external_id == retailer_id,
                        Product.sku == retailer_id,
                    ),
                )
                .first()
            )
        if product is None:
            return None
        meta = dict(getattr(product, "extra_metadata", None) or {})
        image_url = (
            getattr(variant, "image_url", None)
            if variant is not None else None
        ) or meta.get("image_url")
        price = (
            getattr(variant, "price", None)
            if variant is not None else None
        ) or getattr(product, "price", None)
        currency = (
            getattr(variant, "currency", None)
            if variant is not None else None
        ) or meta.get("currency") or "SAR"
        availability = (
            bool(getattr(variant, "in_stock", True))
            if variant is not None
            else bool(getattr(product, "in_stock", True))
        )
        return {
            "id": str(product.id),
            "retailer_id": retailer_id,
            "name": _text(product.title) or None,
            "image_url": _http_url(image_url),
            "price": _text(price) or None,
            "currency": _text(currency) or None,
            "availability": availability,
            "url": _http_url(meta.get("product_url") or meta.get("url")),
        }
    except Exception:  # noqa: silent-ok — optional product enrichment falls back to payload facts
        return None


def _template_definition(db: Any, tenant_id: Optional[int], name: str) -> Any:
    if db is None or not tenant_id or not name:
        return None
    try:
        from models import WhatsAppTemplate  # noqa: PLC0415

        return (
            db.query(WhatsAppTemplate)
            .filter(WhatsAppTemplate.tenant_id == int(tenant_id), WhatsAppTemplate.name == name)
            .order_by(WhatsAppTemplate.updated_at.desc(), WhatsAppTemplate.id.desc())
            .first()
        )
    except Exception:  # noqa: silent-ok — missing legacy template falls back to stored body
        return None


def _template_from_wire(payload: Dict[str, Any], *, db: Any, tenant_id: Optional[int]) -> Dict[str, Any]:
    wire = payload.get("template") if isinstance(payload.get("template"), dict) else {}
    name = _text(wire.get("name"))
    definition = _template_definition(db, tenant_id, name)
    components = list(getattr(definition, "components", None) or [])
    sent_components = wire.get("components") if isinstance(wire.get("components"), list) else []
    body_values: List[str] = []
    header_values: List[str] = []
    sent_buttons: Dict[tuple[str, int], Dict[str, Any]] = {}
    header_media: Optional[Dict[str, Any]] = None
    for component in sent_components:
        if not isinstance(component, dict):
            continue
        ctype = _text(component.get("type")).lower()
        params = component.get("parameters") if isinstance(component.get("parameters"), list) else []
        if ctype == "body":
            body_values = [_text(p.get("text")) for p in params if isinstance(p, dict)]
        elif ctype == "header":
            header_values = [_text(p.get("text")) for p in params if isinstance(p, dict) and p.get("text") is not None]
            for param in params:
                if not isinstance(param, dict):
                    continue
                media_kind = _text(param.get("type")).lower()
                if media_kind not in {"image", "video", "document"}:
                    continue
                media_block = param.get(media_kind) if isinstance(param.get(media_kind), dict) else {}
                header_media = {
                    "kind": media_kind,
                    "url": _media_url(media_block.get("link")),
                    "caption": None,
                    "filename": _text(media_block.get("filename")) or None,
                }
        elif ctype == "button":
            subtype = _text(component.get("sub_type")).lower()
            try:
                index = int(component.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            sent_buttons[(subtype, index)] = component

    body = ""
    footer = ""
    actions: List[Dict[str, Any]] = []
    header_text = ""
    for component in components:
        if not isinstance(component, dict):
            continue
        ctype = _text(component.get("type")).upper()
        if ctype == "BODY":
            body = _replace_numeric_placeholders(component.get("text"), body_values)
        elif ctype == "FOOTER":
            footer = _text(component.get("text"))
        elif ctype == "HEADER":
            fmt = _text(component.get("format")).upper()
            if fmt == "TEXT":
                header_text = _replace_numeric_placeholders(component.get("text"), header_values)
            elif fmt == "IMAGE" and header_media is None:
                example = component.get("example") if isinstance(component.get("example"), dict) else {}
                header_media = {"kind": "image", "url": _http_url(example.get("header_url")), "caption": None}
        elif ctype == "BUTTONS":
            for index, button in enumerate(component.get("buttons") or []):
                if not isinstance(button, dict):
                    continue
                btype = _text(button.get("type")).upper()
                label = _text(button.get("text"))
                sent = sent_buttons.get((btype.lower(), index), {})
                params = sent.get("parameters") if isinstance(sent.get("parameters"), list) else []
                first = params[0] if params and isinstance(params[0], dict) else {}
                if btype == "URL":
                    raw_url = _text(button.get("url"))
                    suffix = _text(first.get("text"))
                    url = raw_url.replace("{{1}}", suffix) if suffix else raw_url
                    actions.append({"kind": "url", "label": label or "Open", "url": _http_url(url)})
                elif btype == "QUICK_REPLY":
                    actions.append({"kind": "quick_reply", "label": label, "payload": _text(first.get("payload"))})
                elif btype == "COPY_CODE":
                    actions.append({"kind": "copy_code", "label": label or "Copy", "payload": _text(first.get("coupon_code"))})
                elif btype == "PHONE_NUMBER":
                    actions.append({
                        "kind": "phone",
                        "label": label,
                        "payload": _text(button.get("phone_number")),
                    })

    visible_body = "\n\n".join(part for part in (header_text, body) if part)
    service_key = _text(getattr(definition, "service_key", None))
    kind = "order_lifecycle" if service_key else "template"
    return {
        "version": PRESENTATION_VERSION,
        "kind": kind,
        "body": visible_body,
        "text_direction": "auto",
        "media": _normalise_media(header_media),
        "template": {
            "name": name or None,
            "category": _text(getattr(definition, "category", None)) or None,
            "language": _text((wire.get("language") or {}).get("code") if isinstance(wire.get("language"), dict) else None) or _text(getattr(definition, "language", None)) or None,
            "service_key": service_key or None,
            "footer": footer or None,
        },
        "actions": [action for action in (_normalise_action(a) for a in actions) if action],
    }


def presentation_from_provider_payload(
    payload: Any,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Project one exact WhatsApp provider payload into MessagePresentation."""
    if not isinstance(payload, dict):
        return None
    payload_type = _text(payload.get("type")).lower()
    if payload_type == "template":
        return _template_from_wire(payload, db=db, tenant_id=tenant_id)
    if payload_type == "text":
        block = payload.get("text") if isinstance(payload.get("text"), dict) else {}
        return {
            "version": PRESENTATION_VERSION,
            "kind": "text",
            "body": str(block.get("body") or ""),
            "text_direction": "auto",
            "actions": [],
        }
    if payload_type in {"image", "video", "audio", "document"}:
        block = payload.get(payload_type) if isinstance(payload.get(payload_type), dict) else {}
        media = {
            "kind": payload_type,
            "url": _media_url(block.get("link")),
            "caption": _text(block.get("caption")) or None,
            "filename": _text(block.get("filename")) or None,
        }
        return {
            "version": PRESENTATION_VERSION,
            "kind": "media",
            "body": _text(block.get("caption")),
            "text_direction": "auto",
            "media": _normalise_media(media),
            "actions": [],
        }
    if payload_type != "interactive":
        return None

    interactive = payload.get("interactive") if isinstance(payload.get("interactive"), dict) else {}
    interactive_type = _text(interactive.get("type")).lower()
    body_block = interactive.get("body") if isinstance(interactive.get("body"), dict) else {}
    header = interactive.get("header") if isinstance(interactive.get("header"), dict) else {}
    footer = interactive.get("footer") if isinstance(interactive.get("footer"), dict) else {}
    action = interactive.get("action") if isinstance(interactive.get("action"), dict) else {}
    body = str(body_block.get("text") or "")
    media = None
    if _text(header.get("type")).lower() in {"image", "video", "document"}:
        mkind = _text(header.get("type")).lower()
        media_block = header.get(mkind) if isinstance(header.get(mkind), dict) else {}
        media = _normalise_media({"kind": mkind, "url": media_block.get("link")})
    actions: List[Dict[str, Any]] = []
    product = None
    kind = "interactive"
    if interactive_type == "button":
        for button in action.get("buttons") or []:
            reply = button.get("reply") if isinstance(button, dict) and isinstance(button.get("reply"), dict) else {}
            actions.append({"kind": "quick_reply", "label": _text(reply.get("title")), "payload": _text(reply.get("id"))})
    elif interactive_type == "cta_url":
        params = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
        actions.append({"kind": "url", "label": _text(params.get("display_text")) or "Open", "url": _http_url(params.get("url"))})
        kind = "product" if media else "interactive"
    elif interactive_type == "product":
        retailer_id = _text(action.get("product_retailer_id"))
        product = _product_for_retailer(db, tenant_id, retailer_id) or {
            "id": None,
            "retailer_id": retailer_id or None,
            "name": None,
            "image_url": None,
            "price": None,
            "currency": None,
            "availability": None,
            "url": None,
        }
        actions.append({"kind": "open_product", "label": "Open product"})
        kind = "product"
    elif interactive_type == "catalog_message":
        params = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
        retailer_id = _text(params.get("thumbnail_product_retailer_id"))
        product = _product_for_retailer(db, tenant_id, retailer_id) if retailer_id else None
        actions.append({"kind": "open_catalog", "label": "Open catalog"})
        kind = "product"
    elif interactive_type == "product_list":
        retailer_id = ""
        for section in action.get("sections") or []:
            items = section.get("product_items") if isinstance(section, dict) else []
            if items and isinstance(items[0], dict):
                retailer_id = _text(items[0].get("product_retailer_id"))
                if retailer_id:
                    break
        product = _product_for_retailer(db, tenant_id, retailer_id) if retailer_id else None
        actions.append({"kind": "open_catalog", "label": "Open catalog"})
        kind = "product"

    return {
        "version": PRESENTATION_VERSION,
        "kind": kind,
        "body": body,
        "text_direction": "auto",
        "media": media,
        "product": product,
        "template": {"footer": _text(footer.get("text")) or None} if footer else None,
        "actions": [action for action in (_normalise_action(a) for a in actions) if action],
    }


def _legacy_actions_from_body(body: str) -> tuple[str, List[Dict[str, Any]]]:
    if _BUTTON_SEPARATOR not in body:
        return body, []
    text_part, raw_buttons = body.split(_BUTTON_SEPARATOR, 1)
    actions: List[Dict[str, Any]] = []
    for raw in raw_buttons.splitlines():
        label = raw.strip()
        if not label:
            continue
        if label.startswith("📋"):
            actions.append({"kind": "copy_code", "label": label.removeprefix("📋").strip()})
        elif label.startswith("🔗"):
            actions.append({"kind": "url", "label": label.removeprefix("🔗").strip()})
        elif label.startswith("📞"):
            actions.append({"kind": "phone", "label": label.removeprefix("📞").strip()})
        else:
            actions.append({"kind": "quick_reply", "label": label.removeprefix("↩️").strip()})
    return text_part.rstrip(), [a for a in (_normalise_action(item) for item in actions) if a]


def _legacy_template_presentation(
    db: Any,
    row: Any,
    body: str,
    meta: Dict[str, Any],
    *,
    template_lookup: Optional[Dict[str, Any]] = None,
    order_lookup: Optional[Dict[int, Any]] = None,
) -> Optional[Dict[str, Any]]:
    name = _text(meta.get("template_name"))
    definition = (
        template_lookup.get(name)
        if template_lookup is not None
        else _template_definition(db, getattr(row, "tenant_id", None), name)
    )
    if definition is None:
        return None
    visible_body, parsed_actions = _legacy_actions_from_body(body)
    components = list(getattr(definition, "components", None) or [])
    footer = ""
    actions: List[Dict[str, Any]] = parsed_actions
    media = None
    order_id = meta.get("order_id")
    order = None
    if order_id:
        try:
            order = order_lookup.get(int(order_id)) if order_lookup is not None else None
            if order is None and order_lookup is None:
                from models import Order  # noqa: PLC0415

                order = db.query(Order).filter(
                    Order.tenant_id == int(row.tenant_id), Order.id == int(order_id),
                ).first()
        except Exception:
            order = None
    if not visible_body or visible_body == f"[{name}]":
        # New lifecycle records can carry only a template marker.  Use the
        # tenant-scoped order/template rows to materialise its persisted facts.
        payload: Dict[str, Any] = {}
        if order is not None:
            try:
                order_meta = dict(getattr(order, "extra_metadata", None) or {})
                line_items = list(getattr(order, "line_items", None) or [])
                first_item = line_items[0] if line_items and isinstance(line_items[0], dict) else {}
                payload = {
                    "order_id": order.id,
                    "order_number": getattr(order, "external_order_number", None) or getattr(order, "external_id", None) or order.id,
                    "external_order_number": getattr(order, "external_order_number", None),
                    "total": getattr(order, "total", None),
                    "order_total": getattr(order, "total", None),
                    "checkout_url": getattr(order, "checkout_url", None),
                    "payment_url": getattr(order, "checkout_url", None),
                    "product_name": first_item.get("name") or first_item.get("title") or first_item.get("product_name") or order_meta.get("product_title"),
                    "status": getattr(order, "status", None),
                }
                from core.automation_engine import _build_template_vars, _resolve_store_name  # noqa: PLC0415

                customer_stub = type("PresentationCustomer", (), {"name": getattr(order, "customer_name", None) or ""})()
                event_stub = type("PresentationEvent", (), {"payload": payload})()
                values = _build_template_vars(
                    event_stub,
                    customer_stub,
                    {},
                    template_name=name,
                    template_source_key=getattr(definition, "nahla_source_key", None),
                    store_name=_resolve_store_name(db, int(row.tenant_id)),
                )
                body_component = next((c for c in components if _text(c.get("type")).upper() == "BODY"), {})
                visible_body = _replace_known_placeholders(body_component.get("text"), values)
            except Exception:
                visible_body = body
    for component in components:
        if not isinstance(component, dict):
            continue
        ctype = _text(component.get("type")).upper()
        if ctype == "FOOTER":
            footer = _text(component.get("text"))
        elif ctype == "HEADER" and _text(component.get("format")).upper() == "IMAGE":
            # Legacy rows did not persist the exact outbound media payload.
            # Show only a URL that was itself persisted on the template
            # definition; never substitute today's tenant/runtime image.
            example = component.get("example") if isinstance(component.get("example"), dict) else {}
            handles = example.get("header_handle") if isinstance(example.get("header_handle"), list) else []
            url = example.get("header_url") or (handles[0] if handles else None)
            media = _normalise_media({"kind": "image", "url": url})
        elif ctype == "BUTTONS" and not actions:
            for button in component.get("buttons") or []:
                if not isinstance(button, dict):
                    continue
                btype = _text(button.get("type")).upper()
                label = _text(button.get("text"))
                if btype == "URL":
                    actions.append({"kind": "url", "label": label or "Open", "url": _http_url(button.get("url"))})
                elif btype == "QUICK_REPLY":
                    actions.append({"kind": "quick_reply", "label": label})
                elif btype == "COPY_CODE":
                    actions.append({"kind": "copy_code", "label": label or "Copy"})
                elif btype == "PHONE_NUMBER":
                    actions.append({
                        "kind": "phone",
                        "label": label,
                        "payload": _text(button.get("phone_number")),
                    })
    service_key = _text(getattr(definition, "service_key", None))
    presentation: Dict[str, Any] = {
        "version": PRESENTATION_VERSION,
        "kind": "order_lifecycle" if service_key else "template",
        "body": visible_body,
        "text_direction": "auto",
        "media": media,
        "template": {
            "name": name,
            "category": _text(getattr(definition, "category", None)) or None,
            "language": _text(getattr(definition, "language", None)) or None,
            "service_key": service_key or None,
            "footer": footer or None,
        },
        "actions": actions,
    }
    if order_id:
        presentation["order"] = {
            "id": str(order_id),
            "reference": _text(
                getattr(order, "external_order_number", None)
                or getattr(order, "external_id", None)
                or order_id
            ) or None,
            "status": _text(getattr(order, "status", None)) or None,
            "lifecycle": service_key or _text(getattr(row, "event_type", None)) or None,
        }
    return presentation


def response_bundle_for_message_event(
    row: Any,
    *,
    db: Any = None,
    media_block: Optional[Dict[str, Any]] = None,
    template_lookup: Optional[Dict[str, Any]] = None,
    order_lookup: Optional[Dict[int, Any]] = None,
) -> Dict[str, Any]:
    """Return a safe ResponseBundle for both canonical and legacy rows."""
    meta = dict(getattr(row, "extra_metadata", None) or {})
    delivery = _delivery_from_metadata(meta)
    stored = normalise_response_bundle(meta.get("response_bundle"), delivery=delivery)
    if stored:
        return stored

    body = str(getattr(row, "body", None) or "")
    presentations: List[Dict[str, Any]] = []
    if media_block:
        media = _normalise_media(media_block)
        presentations.append({
            "version": PRESENTATION_VERSION,
            "kind": "media",
            "body": _text(media_block.get("caption")) or body,
            "text_direction": "auto",
            "media": media,
            "actions": [],
        })
    elif meta.get("template_name"):
        template = _legacy_template_presentation(
            db,
            row,
            body,
            meta,
            template_lookup=template_lookup,
            order_lookup=order_lookup,
        )
        if template:
            presentations.append(template)
    if not presentations:
        visible_body, actions = _legacy_actions_from_body(body)
        presentations.append({
            "version": PRESENTATION_VERSION,
            "kind": "interactive" if actions else "text",
            "body": visible_body,
            "text_direction": "auto",
            "actions": actions,
        })
    return {
        "version": RESPONSE_BUNDLE_VERSION,
        "presentations": presentations,
        "delivery": delivery,
    }


def append_wire_presentation(
    existing: Any,
    presentation: Optional[Dict[str, Any]],
    *,
    delivery: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Append one accepted wire presentation, deduping exact retries."""
    if presentation is None:
        return normalise_response_bundle(existing, delivery=delivery)
    current = normalise_response_bundle(existing, delivery=delivery) or {
        "version": RESPONSE_BUNDLE_VERSION,
        "presentations": [],
        "delivery": delivery or {"state": "unknown", "wamid": None, "error": None},
    }
    candidate = normalise_response_bundle(
        {"version": RESPONSE_BUNDLE_VERSION, "presentations": [presentation]},
        delivery=current.get("delivery"),
    )
    if not candidate:
        return current
    item = candidate["presentations"][0]
    signature = repr(item)
    if signature not in {repr(existing_item) for existing_item in current["presentations"]}:
        current["presentations"].append(item)
    return current


__all__ = [
    "PRESENTATION_VERSION",
    "RESPONSE_BUNDLE_VERSION",
    "append_wire_presentation",
    "normalise_response_bundle",
    "presentation_from_provider_payload",
    "response_bundle_for_message_event",
]
