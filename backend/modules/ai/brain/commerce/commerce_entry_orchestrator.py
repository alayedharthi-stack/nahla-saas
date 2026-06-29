"""
commerce_entry_orchestrator.py
──────────────────────────────
Commerce entry routing — CE1 status branch + CE2 catalog delivery hook.

CE1: status/story reply price/qty/buy ownership.
CE2: see commerce_entry_catalog_delivery.py for catalog send/block ownership.
CE4: see product_knowledge_or_comparison.py for knowledge/comparison ownership.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.brain.commerce_entry_orchestrator")

TOPIC_COMMERCE_ENTRY_STATUS = "commerce_entry_status"


class CustomerAction(str, Enum):
    PRICE = "price"
    BUY = "buy"
    QUANTITY = "quantity"
    ASK_VARIANT_OR_SIZE = "ask_variant_or_size"
    KNOWLEDGE = "knowledge"
    DELEGATE = "delegate"


def classify_customer_action(
    message: str,
    *,
    quantity_hint: Optional[Dict[str, Any]] = None,
    has_product_focus: bool = False,
) -> CustomerAction:
    """Classify a status-reply follow-up into a deterministic commerce action."""
    from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: PLC0415
        _ORDER_VERB_RE,
        _PRICE_FOLLOWUP_RE,
        _WANT_RE,
        extract_status_reply_quantity,
    )

    raw = (message or "").strip()
    if not raw:
        return CustomerAction.DELEGATE

    try:
        from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: PLC0415
            classify_product_knowledge_kind,
        )

        if classify_product_knowledge_kind(raw) is not None:
            return CustomerAction.KNOWLEDGE
    except Exception:  # noqa: BLE001  # noqa: silent-ok — knowledge probe is best-effort
        pass

    if _PRICE_FOLLOWUP_RE.search(raw):
        return CustomerAction.PRICE

    qty = dict(quantity_hint or {})
    if not qty.get("quantity") and not qty.get("variant"):
        extracted = extract_status_reply_quantity(raw)
        if extracted:
            qty = extracted

    if qty.get("quantity") or qty.get("variant"):
        return CustomerAction.QUANTITY

    if _ORDER_VERB_RE.search(raw):
        return CustomerAction.BUY

    if _WANT_RE.search(raw) and has_product_focus:
        return CustomerAction.QUANTITY

    return CustomerAction.DELEGATE


def enrich_product_focus_from_catalog(
    db: Any,
    tenant_id: int,
    focus: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Enrich pinned focus from catalog evidence when match is trusted."""
    out = dict(focus or {})
    if not out:
        return out

    pid = out.get("id") or out.get("product_id")
    retailer_id = str(
        out.get("product_retailer_id")
        or out.get("catalog_retailer_id")
        or out.get("meta_retailer_id")
        or ""
    ).strip()

    row = None
    match_strategy = str(out.get("catalog_match_confidence") or "")

    if db and tenant_id:
        if pid:
            row = _fetch_product_row(db, tenant_id, product_id=pid)
            if row is not None:
                match_strategy = match_strategy or "direct_id"
        if row is None and retailer_id:
            try:
                from modules.ai.brain.pipeline import _resolve_catalog_product  # noqa: PLC0415

                row, strategy = _resolve_catalog_product(
                    db=db,
                    tenant_id=int(tenant_id),
                    sku=retailer_id,
                    unit_price=None,
                    allow_price_fallback=False,
                )
                if row is not None:
                    match_strategy = strategy or "referred_product"
            except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — catalog enrich is best-effort
                logger.debug(
                    "[COMMERCE_ENTRY] retailer lookup failed tenant=%s err=%s",
                    tenant_id,
                    exc,
                )

    if row is None:
        return out

    title = str(getattr(row, "title", "") or "").strip()
    if title:
        out["title"] = title
    out["id"] = getattr(row, "id", out.get("id"))
    price = getattr(row, "price", None)
    if price is not None:
        try:
            out["price"] = float(price)
        except (TypeError, ValueError):
            pass
    meta_rid = getattr(row, "meta_retailer_id", None)
    if meta_rid:
        out["meta_retailer_id"] = str(meta_rid)
    if match_strategy:
        out["catalog_match_confidence"] = match_strategy

    try:
        from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

        formatted = CatalogContextBuilder(db, int(tenant_id))._format(row)
        if isinstance(formatted, dict):
            if formatted.get("variants"):
                out["variants"] = list(formatted.get("variants") or [])
            if formatted.get("price") is not None and out.get("price") is None:
                out["price"] = formatted.get("price")
            if formatted.get("sale_price") is not None:
                out["sale_price"] = formatted.get("sale_price")
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — variant enrich is best-effort
        logger.debug("[COMMERCE_ENTRY] catalog format failed tenant=%s err=%s", tenant_id, exc)

    return out


def apply_quantity_to_order_prep(state: Any, quantity_hint: Optional[Dict[str, Any]]) -> None:
    """Wire extracted quantity into order_prep and product focus."""
    if not quantity_hint or state is None:
        return

    qty_val = quantity_hint.get("quantity")
    if isinstance(qty_val, (int, float)) and qty_val > 0:
        try:
            state.order_prep.quantity = max(1, int(round(float(qty_val))))
        except (TypeError, ValueError):
            pass

    focus = dict(getattr(state, "current_product_focus", None) or {})
    pid = focus.get("id") or focus.get("product_id")
    if pid:
        state.order_prep.product_id = str(pid)

    focus["requested_quantity"] = dict(quantity_hint)
    if quantity_hint.get("variant"):
        focus["requested_variant"] = str(quantity_hint.get("variant"))
    state.current_product_focus = focus


def resolve_status_entry(ctx: Any, sr: Any) -> Optional[Any]:
    """
    Route status-reply commerce actions to deterministic handlers.

    Supports price, buy, quantity, variant/size clarify, and delegate-to-engine.
    """
    from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: PLC0415
        compose_status_reply_product_goal,
        is_status_reply_follow_up_message,
    )
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    message = str(getattr(ctx, "message", "") or "").strip()
    state = getattr(ctx, "state", None)
    db = getattr(ctx, "_db", None)
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)

    needs_clarify = (not sr.has_trusted_title) or (
        sr.has_image_only and not sr.product_title
    )
    if needs_clarify and is_status_reply_follow_up_message(message):
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": TOPIC_COMMERCE_ENTRY_STATUS,
                "response_goal": compose_status_reply_product_goal(sr, message),
                "block_commerce_escalation": False,
            },
            reason="commerce_entry_status — clarify product from status",
            confidence=0.91,
        )

    if not sr.has_trusted_title:
        return None

    focus = dict(getattr(state, "current_product_focus", None) or {})
    if not focus.get("title") and sr.product_title:
        focus["title"] = sr.product_title
        focus.setdefault("from_status_reply", True)
        state.current_product_focus = focus

    enriched = enrich_product_focus_from_catalog(db, tenant_id, focus)
    if enriched:
        state.current_product_focus = enriched
        focus = enriched

    qty_hint = dict(sr.quantity_hint or {})
    if qty_hint:
        apply_quantity_to_order_prep(state, qty_hint)

    action = classify_customer_action(
        message,
        quantity_hint=qty_hint,
        has_product_focus=bool(focus.get("title") or focus.get("id")),
    )

    if action == CustomerAction.PRICE:
        price_dec = _resolve_status_price(ctx, sr)
        if price_dec is not None:
            return price_dec

    if action in (CustomerAction.BUY, CustomerAction.QUANTITY):
        buy_dec = _resolve_status_buy_or_quantity(ctx, sr)
        if buy_dec is not None:
            return buy_dec

    if action == CustomerAction.KNOWLEDGE:
        return None

    if action == CustomerAction.DELEGATE:
        return None

    return None


def _resolve_status_price(ctx: Any, sr: Any) -> Optional[Any]:
    from modules.ai.brain.commerce.variant_pricing import (  # noqa: PLC0415
        try_variant_pricing_decision,
    )
    from modules.ai.brain.decision.actions import (  # noqa: PLC0415
        ACTION_CLARIFY,
        ACTION_LLM_REPLY,
        ACTION_VARIANT_PRICING,
    )
    from modules.ai.brain.product_discovery_gate import try_price_query_decision  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    variant_dec = try_variant_pricing_decision(ctx)
    if variant_dec is not None:
        return variant_dec

    price_ctx = _ctx_with_price_intent(ctx)
    price_dec = try_price_query_decision(price_ctx)
    if price_dec is not None and price_dec.action not in (ACTION_LLM_REPLY,):
        return price_dec

    clarify = _status_variant_size_clarify(ctx)
    if clarify is not None:
        return clarify

    focus = dict(getattr(getattr(ctx, "state", None), "current_product_focus", None) or {})
    if focus.get("title") or sr.product_title:
        return Decision(
            action=ACTION_CLARIFY,
            args={
                "question": "حدّد المقاس أو الحجم المطلوب.",
            },
            reason="commerce_entry_status — price needs size/variant",
            confidence=0.90,
        )
    return None


def _resolve_status_buy_or_quantity(ctx: Any, sr: Any) -> Optional[Any]:
    from modules.ai.brain.commerce.variant_pricing import (  # noqa: PLC0415
        _clarify_variant_question,
        bindings_from_catalog_product,
        parse_unit_from_text,
        resolve_variant,
        try_variant_pricing_decision,
    )
    from modules.ai.brain.decision.actions import (  # noqa: PLC0415
        ACTION_CLARIFY,
        ACTION_PROPOSE_DRAFT_ORDER,
        ACTION_VARIANT_PRICING,
    )
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    state = getattr(ctx, "state", None)
    facts = getattr(ctx, "facts", None)
    focus = dict(getattr(state, "current_product_focus", None) or {})
    product = dict(focus)

    qty_hint = dict(sr.quantity_hint or {})
    apply_quantity_to_order_prep(state, qty_hint)

    variants = bindings_from_catalog_product(product)
    if len(variants) > 1:
        unit_probe = str(qty_hint.get("variant") or getattr(ctx, "message", "") or "")
        requested_unit = parse_unit_from_text(unit_probe)
        var_outcome = resolve_variant(
            getattr(ctx, "message", "") or "",
            variants=variants,
            requested_unit=requested_unit,
            tenant_id=getattr(ctx, "tenant_id", None),
        )
        if var_outcome.status == "ambiguous":
            return Decision(
                action=ACTION_CLARIFY,
                args={
                    "question": _clarify_variant_question(
                        var_outcome.candidates or list(variants),
                    ),
                },
                reason="commerce_entry_status — ambiguous variant for buy/qty",
                confidence=0.93,
            )

    price_dec = try_variant_pricing_decision(ctx)
    if price_dec is not None and price_dec.action == ACTION_CLARIFY:
        return price_dec

    orderable = bool(getattr(facts, "orderable", False))
    has_products = bool(getattr(facts, "has_products", False))
    if orderable and has_products and focus:
        product_arg = dict(focus)
        if qty_hint:
            product_arg["requested_quantity"] = qty_hint
        args: Dict[str, Any] = {"product": product_arg}
        prep_qty = getattr(getattr(state, "order_prep", None), "quantity", None)
        if prep_qty:
            args["quantity"] = prep_qty
        return Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args=args,
            reason="commerce_entry_status — buy/qty on status product focus",
            confidence=0.92,
        )

    clarify = _status_variant_size_clarify(ctx)
    if clarify is not None:
        return clarify

    return None


def _status_variant_size_clarify(ctx: Any) -> Optional[Any]:
    from modules.ai.brain.commerce.variant_pricing import (  # noqa: PLC0415
        _clarify_variant_question,
        bindings_from_catalog_product,
    )
    from modules.ai.brain.decision.actions import ACTION_CLARIFY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    focus = dict(getattr(getattr(ctx, "state", None), "current_product_focus", None) or {})
    variants = bindings_from_catalog_product(focus)
    if len(variants) <= 1:
        return None
    return Decision(
        action=ACTION_CLARIFY,
        args={"question": _clarify_variant_question(variants)},
        reason="commerce_entry_status — ask variant/size only",
        confidence=0.91,
    )


def _ctx_with_price_intent(ctx: Any) -> Any:
    from modules.ai.brain.types import INTENT_ASK_PRICE, Intent  # noqa: PLC0415

    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    if intent_name in ("ask_price", "ask_product"):
        return ctx

    patched = Intent(
        name=INTENT_ASK_PRICE,
        confidence=0.92,
        raw_message=str(getattr(ctx, "message", "") or ""),
        slots=dict(getattr(getattr(ctx, "intent", None), "slots", None) or {}),
    )
    try:
        object.__setattr__(ctx, "intent", patched)
    except Exception:  # noqa: BLE001
        ctx.intent = patched  # type: ignore[attr-defined]
    return ctx


def _fetch_product_row(db: Any, tenant_id: int, *, product_id: Any) -> Any:
    try:
        from database.models import Product  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional models import in tests/stubs
        try:
            from models import Product  # noqa: PLC0415
        except Exception:  # noqa: BLE001  # noqa: silent-ok
            return None
    try:
        return (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id), Product.id == int(product_id))
            .first()
        )
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — product fetch is best-effort
        logger.debug(
            "[COMMERCE_ENTRY] product fetch failed tenant=%s id=%s err=%s",
            tenant_id,
            product_id,
            exc,
        )
        return None


__all__ = [
    "TOPIC_COMMERCE_ENTRY_STATUS",
    "CustomerAction",
    "apply_quantity_to_order_prep",
    "classify_customer_action",
    "enrich_product_focus_from_catalog",
    "resolve_status_entry",
]
