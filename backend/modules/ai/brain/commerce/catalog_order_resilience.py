"""
Phase 2.7 — resilient native catalog checkout when V2 / brain paths fail.

Operational only: deterministic checkout continuity for WhatsApp ``type=order``
events. Never re-opens product / quantity / variant discovery once catalog
line items are known.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.commerce.catalog_order_resilience")

_FORBIDDEN_CATALOG_PRODUCT_PROMPT_RES = (
    re.compile(r"وش\s*(?:الم(?:نتج|نتجات)|عدد|العدد|الكم(?:ية)?|الوزن)", re.I | re.UNICODE),
    re.compile(r"باقي\s+تحدد\s+(?:ال)?(?:منتج|الكمية)", re.I | re.UNICODE),
    re.compile(r"وش\s+خيار\s+يناسبك", re.I | re.UNICODE),
    re.compile(r"(?:أ?ي|اي)\s+(?:حجم|وزن|خيار)\s+ت(?:فضل|بي)", re.I | re.UNICODE),
)

_PRODUCT_MISSING_SLOTS = frozenset({
    "product",
    "products",
    "product_id",
    "variant",
    "quantity",
    "qty",
    "weight",
})


def safe_line_item_quantity(raw: Any, *, default: float = 1.0) -> float:
    """Parse catalog line-item quantity without crashing; preserves decimals (e.g. 2.5)."""
    try:
        if raw is None or raw == "":
            return default
        val = float(raw)
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default


def format_line_item_quantity(qty: float) -> str:
    """Display quantity without truncating fractional catalog amounts."""
    if abs(qty - round(qty)) < 1e-9:
        return str(int(round(qty)))
    return f"{qty:.4f}".rstrip("0").rstrip(".")


def reply_contains_forbidden_catalog_product_question(text: str) -> bool:
    blob = str(text or "").strip()
    if not blob:
        return False
    return any(p.search(blob) for p in _FORBIDDEN_CATALOG_PRODUCT_PROMPT_RES)


def _line_items_from_meta(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = meta.get("product_items")
    if isinstance(raw, list) and raw:
        return [dict(x) for x in raw if isinstance(x, dict)]
    order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
    nested = order.get("product_items")
    if isinstance(nested, list) and nested:
        return [dict(x) for x in nested if isinstance(x, dict)]
    return []


def catalog_resilience_known_facts(
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
    known_facts: Optional[Dict[str, Any]] = None,
    state: Any = None,
) -> Dict[str, Any]:
    facts = dict(known_facts or {})
    meta = dict(inbound_metadata or {})
    try:
        from modules.ai.order_flow_v2.triggers import is_catalog_order_inbound  # noqa: PLC0415

        if is_catalog_order_inbound(meta, message):
            facts.setdefault("catalog_order_current_turn", True)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — trigger import is best-effort
        pass
    items = _line_items_from_meta(meta)
    if items:
        facts.setdefault("line_items_known", True)
    if state is not None:
        prep = getattr(state, "order_prep", None)
        prep_d: Dict[str, Any] = {}
        if prep is not None:
            if hasattr(prep, "to_dict"):
                try:
                    prep_d = dict(prep.to_dict())
                except Exception:  # noqa: BLE001
                    prep_d = {}
            elif isinstance(prep, dict):
                prep_d = dict(prep)
        li = list(prep_d.get("line_items") or [])
        if li:
            facts.setdefault("line_items_known", True)
        try:
            from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
                is_catalog_line_items_authoritative_from_prep,
            )

            if is_catalog_line_items_authoritative_from_prep(prep):
                facts.setdefault("active_catalog_checkout", True)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — active checkout probe is best-effort
            pass
    return facts


def is_catalog_checkout_product_question_forbidden(
    *,
    ctx: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
    known_facts: Optional[Dict[str, Any]] = None,
    state: Any = None,
) -> bool:
    """True when customer-facing copy must not ask product / quantity / weight."""
    meta = dict(inbound_metadata or {})
    msg = str(message or "").strip()
    if ctx is not None:
        meta = meta or dict((getattr(ctx, "profile", None) or {}).get("inbound_metadata") or {})
        if not msg:
            msg = str(getattr(ctx, "message", "") or "")
        if state is None:
            state = getattr(ctx, "state", None)
        contract = getattr(ctx, "commerce_turn_contract", None)
        if contract is not None:
            known_facts = dict(getattr(contract, "known_facts", None) or known_facts or {})
    facts = catalog_resilience_known_facts(
        inbound_metadata=meta,
        message=msg,
        known_facts=known_facts,
        state=state,
    )
    if facts.get("catalog_order_current_turn") and facts.get("line_items_known"):
        return True
    if facts.get("active_catalog_checkout") and facts.get("line_items_known"):
        return True
    return False


def resolve_store_external_id(
    db: Any,
    tenant_id: int,
    retailer_id: str,
    *,
    line_item: Optional[Dict[str, Any]] = None,
) -> str:
    """Map WhatsApp ``product_retailer_id`` to the store platform product id."""
    item = dict(line_item or {})
    for key in ("salla_product_id", "store_external_id", "store_product_id", "external_id"):
        val = str(item.get(key) or "").strip()
        if val and val != str(item.get("product_retailer_id") or "").strip():
            return val
    rid = str(retailer_id or item.get("product_retailer_id") or "").strip()
    if not rid or db is None or not tenant_id:
        return ""
    try:
        from core.wa_native_catalog_order import match_retailer_id  # noqa: PLC0415
        from models import Product  # noqa: PLC0415

        match = match_retailer_id(db, int(tenant_id), rid)
        if not match.matched or not match.product_id:
            return ""
        product = (
            db.query(Product)
            .filter(Product.id == int(match.product_id), Product.tenant_id == int(tenant_id))
            .first()
        )
        if product is None:
            return ""
        return str(getattr(product, "external_id", "") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CATALOG_ORDER_RESILIENCE] external_id resolve failed tenant=%s retailer_id=%r err=%s",
            tenant_id,
            rid,
            exc,
        )
        return ""


def enrich_catalog_product_with_store_ids(
    db: Any,
    tenant_id: int,
    product: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach store ``external_id`` when resolvable — never invent ids."""
    out = dict(product or {})
    retailer_id = str(
        out.get("product_retailer_id")
        or out.get("retailer_id")
        or out.get("id")
        or "",
    ).strip()
    if str(out.get("external_id") or "").strip():
        return out
    line_items = list(out.get("line_items") or [])
    first = next((li for li in line_items if isinstance(li, dict)), {}) or {}
    ext = resolve_store_external_id(
        db,
        int(tenant_id),
        retailer_id,
        line_item=first if isinstance(first, dict) else None,
    )
    if ext:
        out["external_id"] = ext
        out["salla_product_id"] = ext
    return out


def _filter_catalog_missing(missing: List[str]) -> List[str]:
    return [m for m in list(missing or []) if str(m).strip().lower() not in _PRODUCT_MISSING_SLOTS]


def build_catalog_checkout_safe_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    missing_fields: Optional[List[str]] = None,
    extraction_incomplete: bool = False,
) -> str:
    """Deterministic checkout reply — name / city / address only."""
    if extraction_incomplete or order_prep.get("catalog_order_extraction_incomplete"):
        from modules.ai.order_flow_v2.replies import (  # noqa: PLC0415
            build_catalog_order_extraction_fallback_reply,
        )

        return build_catalog_order_extraction_fallback_reply(order_prep=order_prep)
    from modules.ai.order_flow_v2.replies import build_catalog_order_start_reply  # noqa: PLC0415

    filtered = _filter_catalog_missing(list(missing_fields or []))
    return build_catalog_order_start_reply(
        order_prep=order_prep,
        brain_state=dict(brain_state or {}),
        missing_fields=filtered,
    )


def build_catalog_checkout_safe_reply_for_ctx(
    ctx: Any,
    *,
    order_prep: Optional[Dict[str, Any]] = None,
    extraction_incomplete: bool = False,
) -> str:
    prep = dict(order_prep or {})
    state = getattr(ctx, "state", None)
    if not prep and state is not None:
        op = getattr(state, "order_prep", None)
        if op is not None and hasattr(op, "to_dict"):
            try:
                prep = dict(op.to_dict())
            except Exception:  # noqa: BLE001
                prep = {}
        elif isinstance(op, dict):
            prep = dict(op)
    bs: Dict[str, Any] = {}
    if state is not None and hasattr(state, "to_dict"):
        try:
            bs = dict(state.to_dict())
        except Exception:  # noqa: BLE001
            bs = {}
    missing: List[str] = []
    try:
        from modules.ai.order_flow_v2.missing_fields import compute_v2_missing_fields  # noqa: PLC0415
        from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
            merge_prep_with_customer_identity,
            resolve_catalog_checkout_customer_identity,
        )

        meta = dict((getattr(ctx, "profile", None) or {}).get("inbound_metadata") or {})
        identity = resolve_catalog_checkout_customer_identity(
            db=getattr(ctx, "_db", None),
            tenant_id=int(getattr(ctx, "tenant_id", 0) or 0) or None,
            phone=str(getattr(ctx, "customer_phone", "") or ""),
            order_prep=prep,
            profile=dict(getattr(ctx, "profile", None) or {}),
        )
        prep = merge_prep_with_customer_identity(prep, identity)
        missing = _filter_catalog_missing(
            compute_v2_missing_fields(
                prep,
                brain_state=bs,
                whatsapp_phone=str(getattr(ctx, "customer_phone", "") or ""),
                db=getattr(ctx, "_db", None),
                tenant_id=int(getattr(ctx, "tenant_id", 0) or 0) or None,
                inbound_metadata=meta,
            )
        )
    except Exception:  # noqa: BLE001
        missing = []
    return build_catalog_checkout_safe_reply(
        order_prep=prep,
        brain_state=bs,
        missing_fields=missing,
        extraction_incomplete=extraction_incomplete,
    )


def try_catalog_order_pre_brain_safe_reply(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build a safe deterministic reply for native catalog orders when OrderFlowV2
    cannot own the turn (shadow off-path or internal error).
    """
    meta = dict(inbound_metadata or {})
    text = str(message or "").strip()
    try:
        from modules.ai.order_flow_v2.triggers import is_catalog_order_inbound  # noqa: PLC0415

        if not is_catalog_order_inbound(meta, text):
            return ""
    except Exception:  # noqa: BLE001
        return ""

    facts = catalog_resilience_known_facts(
        inbound_metadata=meta,
        message=text,
    )
    if not facts.get("line_items_known") and not _line_items_from_meta(meta):
        if "[طلب كتالوج من العميل]" not in text:
            return ""

    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415
        from core.wa_native_catalog_order import (  # noqa: PLC0415
            build_line_items_from_payload,
            parse_native_catalog_order,
        )
        from modules.ai.order_flow_v2.missing_fields import compute_v2_missing_fields  # noqa: PLC0415
        from modules.ai.order_flow_v2.state import prep_dict  # noqa: PLC0415

        if text:
            meta.setdefault("_catalog_order_message", text)
        payload = parse_native_catalog_order(dict(meta.get("order") or {}), metadata=meta)
        resolution = build_line_items_from_payload(db, int(tenant_id), payload)
        conversation, brain_state = _load_brain_state(
            db, tenant_id=int(tenant_id), phone=customer_phone,
        )
        order_prep = prep_dict((brain_state or {}).get("order_prep") or {})
        patch: Dict[str, Any] = {
            "line_items": list(resolution.line_items),
            "order_flow_v2_trusted_price": True,
            "catalog_line_items_authoritative": bool(resolution.line_items),
        }
        if resolution.line_items:
            facts["line_items_known"] = True
        skus = [
            item.product_retailer_id
            for item in payload.items
            if str(item.product_retailer_id or "").strip()
        ]
        if skus:
            patch["catalog_skus"] = skus
        if payload.total_price is not None:
            patch["order_flow_v2_catalog_total"] = float(payload.total_price)
            patch["order_total"] = float(payload.total_price)
        expected_lines = int(payload.text_line_count or 0)
        actual_lines = len(payload.items or [])
        extraction_incomplete = bool(
            not resolution.line_items
            or int(getattr(resolution, "unmatched_count", 0) or 0) > 0
            or (expected_lines > 0 and actual_lines < expected_lines)
        )
        if extraction_incomplete:
            patch["catalog_order_extraction_incomplete"] = True
        merged = {**order_prep, **patch}
        from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
            filter_missing_for_known_catalog_customer,
            merge_prep_with_customer_identity,
            resolve_catalog_checkout_customer_identity,
        )

        identity = resolve_catalog_checkout_customer_identity(
            db=db,
            tenant_id=int(tenant_id),
            phone=customer_phone,
            order_prep=merged,
        )
        merged = merge_prep_with_customer_identity(merged, identity)
        missing = _filter_catalog_missing(
            compute_v2_missing_fields(
                merged,
                brain_state=dict(brain_state or {}),
                whatsapp_phone=customer_phone,
                db=db,
                tenant_id=int(tenant_id),
                conversation=conversation,
                inbound_metadata=meta,
            )
        )
        missing = filter_missing_for_known_catalog_customer(
            missing,
            known_facts=identity.known_facts,
            phone=customer_phone,
        )
        identity_facts = dict(identity.known_facts or {})
        facts.update(identity_facts)
        reply = build_catalog_checkout_safe_reply(
            order_prep=merged,
            brain_state=dict(brain_state or {}),
            missing_fields=missing,
            extraction_incomplete=extraction_incomplete,
        )
        from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
            reply_contains_forbidden_catalog_name_question,
            sanitize_forbidden_catalog_name_question,
        )

        if identity_facts.get("customer_name_known") and reply_contains_forbidden_catalog_name_question(reply):
            reply = sanitize_forbidden_catalog_name_question(
                reply,
                known_facts=identity_facts,
                missing_fields=missing,
            )
        if reply_contains_forbidden_catalog_product_question(reply):
            logger.error(
                "[CATALOG_ORDER_RESILIENCE] blocked unsafe pre-brain reply tenant=%s phone=%s",
                tenant_id,
                customer_phone,
            )
            from modules.ai.order_flow_v2.replies import (  # noqa: PLC0415
                build_catalog_order_extraction_fallback_reply,
            )

            return build_catalog_order_extraction_fallback_reply(order_prep=merged)
        return reply
    except Exception:
        logger.exception(
            "[CATALOG_ORDER_RESILIENCE] pre-brain safe reply failed tenant=%s phone=%s",
            tenant_id,
            customer_phone,
        )
        from modules.ai.order_flow_v2.replies import (  # noqa: PLC0415
            build_catalog_order_extraction_fallback_reply,
        )

        return build_catalog_order_extraction_fallback_reply(
            order_prep={"catalog_skus": _line_items_from_meta(meta)},
        )


def sanitize_forbidden_catalog_product_question(
    reply: str,
    *,
    ctx: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
    known_facts: Optional[Dict[str, Any]] = None,
    state: Any = None,
) -> str:
    """Replace forbidden catalog product prompts with safe checkout copy."""
    if not is_catalog_checkout_product_question_forbidden(
        ctx=ctx,
        inbound_metadata=inbound_metadata,
        message=message,
        known_facts=known_facts,
        state=state,
    ):
        return str(reply or "")
    if not reply_contains_forbidden_catalog_product_question(reply):
        return str(reply or "")
    safe = ""
    if ctx is not None:
        safe = build_catalog_checkout_safe_reply_for_ctx(ctx)
    if not safe:
        from modules.ai.order_flow_v2.replies import (  # noqa: PLC0415
            build_catalog_order_extraction_fallback_reply,
        )

        safe = build_catalog_order_extraction_fallback_reply(order_prep={})
    logger.warning(
        "[CATALOG_ORDER_RESILIENCE] sanitized forbidden product prompt | preview=%r safe_preview=%r",
        str(reply or "")[:80],
        safe[:80],
    )
    return safe


__all__ = [
    "build_catalog_checkout_safe_reply",
    "build_catalog_checkout_safe_reply_for_ctx",
    "catalog_resilience_known_facts",
    "enrich_catalog_product_with_store_ids",
    "is_catalog_checkout_product_question_forbidden",
    "reply_contains_forbidden_catalog_product_question",
    "resolve_store_external_id",
    "safe_line_item_quantity",
    "format_line_item_quantity",
    "sanitize_forbidden_catalog_product_question",
    "try_catalog_order_pre_brain_safe_reply",
]
