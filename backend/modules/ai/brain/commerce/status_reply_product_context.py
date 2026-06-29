"""
status_reply_product_context.py
───────────────────────────────
Ownership for WhatsApp status/story reply product continuity.

When a customer replies to a merchant status that shows or names a product,
follow-up turns (quantity, price pronoun, order verbs) must inherit that
product — not fall through to generic availability fallbacks.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.brain.status_reply_product_context")

TOPIC_STATUS_REPLY_PRODUCT_CONTEXT = "status_reply_product_context"

_SESSION_KEY = "status_reply_product_context"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_QUANTITY_ORDER_RE = re.compile(
    r"(?:"
    r"(?:نبغ[ىي]|نبي|ابغ[ىي]|ابي|أب[ىي]|أريد|اريد|ودي|حاب)\s+"
    r"(?:كilo|كيلo|كيلو|كيلوين|كيلogram|kg|واحد|واحدة|حبة|حبتين|\d+)"
    r"|(?:كilo|كيلo|كيلو|كيلوين)\s*(?:واحد|واحدة|ثنتين|2)?"
    r"|(?:حبتين|اثنين|2)\s*(?:كilo|كيلo|كيلo|kg)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_FOLLOWUP_RE = re.compile(
    r"(?:"
    r"كم\s*سعر(?:ه|ها)?|بكم|ثمن(?:ه|ها)?|كم\s*ثمن(?:ه|ها)?|"
    r"how\s*much|price\s*(?:it|this)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ORDER_VERB_RE = re.compile(
    r"(?:"
    r"(?:أبغ[اه]|ابغ[اه]|أب[يه]|اب[يه]|ارسل(?:ه|ها)?|أرسل(?:ه|ها)?|"
    r"خذه|خذها|اطلب(?:ه|ها)?|أطلب(?:ه|ها)?|جهز(?:ه|ها)?)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_KILO_DUAL_RE = re.compile(r"كيلوين|2\s*(?:كilo|كيلo|كيلo|kg)", re.I)
_KILO_ONE_RE = re.compile(r"(?:كilo|كيلo|كيلo|1\s*kg)\b", re.I)
_WANT_RE = re.compile(
    r"(?:نبغ[ىي]|نبي|ابغ[ىي]|ابي|أب[ىي]|أريد|اريد|ودي|حاب)\b",
    re.I,
)


@dataclass(frozen=True)
class StatusReplyProductContext:
    source: str = ""
    product_title: str = ""
    product_id: Any = None
    catalog_retailer_id: str = ""
    status_wa_message_id: str = ""
    status_body_preview: str = ""
    has_trusted_title: bool = False
    has_image_only: bool = False
    referred_product: Dict[str, Any] = field(default_factory=dict)
    quantity_hint: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "product_title": self.product_title,
            "product_id": self.product_id,
            "catalog_retailer_id": self.catalog_retailer_id,
            "status_wa_message_id": self.status_wa_message_id,
            "status_body_preview": self.status_body_preview,
            "has_trusted_title": self.has_trusted_title,
            "has_image_only": self.has_image_only,
            "referred_product": dict(self.referred_product or {}),
            "quantity_hint": dict(self.quantity_hint or {}),
            "active": True,
        }


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def is_status_reply_inbound(inbound_metadata: Optional[Dict[str, Any]]) -> bool:
    meta = dict(inbound_metadata or {})
    if meta.get("is_status_or_reply_context"):
        return True
    if str(meta.get("referred_wa_message_id") or "").strip():
        return True
    if isinstance(meta.get("referred_product"), dict) and meta.get("referred_product"):
        return True
    if isinstance(meta.get("whatsapp_context"), dict) and meta.get("whatsapp_context"):
        return True
    return False


def is_status_reply_follow_up_message(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    if _QUANTITY_ORDER_RE.search(raw):
        return True
    if _PRICE_FOLLOWUP_RE.search(raw):
        return True
    if _ORDER_VERB_RE.search(raw):
        return True
    return False


def extract_status_reply_quantity(message: str) -> Dict[str, Any]:
    raw = (message or "").strip()
    norm = _norm(raw)
    if not raw:
        return {}
    if _KILO_DUAL_RE.search(norm):
        return {"quantity": 2, "unit": "kg", "variant": "2kg", "raw": raw}
    if _KILO_ONE_RE.search(norm) and _WANT_RE.search(norm):
        return {"quantity": 1, "unit": "kg", "variant": "1kg", "raw": raw}
    if re.search(r"حبتين|اثنين|^2\b", norm):
        return {"quantity": 2, "unit": "piece", "raw": raw}
    return {"raw": raw} if _WANT_RE.search(norm) else {}


def _lookup_outbound_by_wa_message_id(
    db: Any,
    tenant_id: int,
    wa_message_id: str,
) -> Optional[Any]:
    if not db or not tenant_id or not wa_message_id:
        return None
    try:
        from models import MessageEvent  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional models import in tests/stubs
        return None
    try:
        return (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == int(tenant_id),
                MessageEvent.extra_metadata["wa_message_id"].astext == wa_message_id,
            )
            .order_by(MessageEvent.id.desc())
            .first()
        )
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — outbound lookup is best-effort
        logger.debug(
            "[STATUS_REPLY_CTX] outbound lookup failed tenant=%s wa_id=%s err=%s",
            tenant_id,
            wa_message_id,
            exc,
        )
        return None


def _status_text_from_outbound(row: Any) -> str:
    if row is None:
        return ""
    body = str(getattr(row, "body", "") or "").strip()
    meta = dict(getattr(row, "extra_metadata", None) or {})
    ni = dict(meta.get("normalized_inbound") or {})
    for candidate in (
        body,
        str(ni.get("caption") or ""),
        str(meta.get("caption") or ""),
        str(meta.get("status_text") or ""),
        str(meta.get("body") or ""),
    ):
        c = (candidate or "").strip()
        if c and c != body:
            return c
    return body


def _match_catalog_product_in_text(
    db: Any,
    tenant_id: int,
    text: str,
    *,
    catalog_retailer_id: str = "",
) -> tuple[Any, str]:
    if catalog_retailer_id:
        try:
            from modules.ai.brain.pipeline import _resolve_catalog_product  # noqa: PLC0415

            row, strategy = _resolve_catalog_product(
                db=db,
                tenant_id=int(tenant_id),
                sku=str(catalog_retailer_id),
                unit_price=None,
                allow_price_fallback=False,
            )
            if row is not None:
                return row, f"referred_product_{strategy}"
        except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — referred_product lookup is best-effort
            logger.debug(
                "[STATUS_REPLY_CTX] referred_product lookup failed tenant=%s err=%s",
                tenant_id,
                exc,
            )

    norm_text = _norm(text)
    if len(norm_text) < 3:
        return None, "miss"
    try:
        from database.models import Product  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional models import in tests/stubs
        return None, "miss"
    try:
        rows = (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id))
            .limit(500)
            .all()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog fetch is best-effort
        return None, "miss"
    best = None
    best_len = 0
    for row in rows:
        title = str(getattr(row, "title", "") or "").strip()
        nt = _norm(title)
        if len(nt) < 3:
            continue
        if nt in norm_text and len(nt) > best_len:
            best = row
            best_len = len(nt)
    if best is not None:
        return best, "title_substring"
    return None, "miss"


def resolve_status_reply_product_context(
    db: Any,
    tenant_id: int,
    inbound_metadata: Optional[Dict[str, Any]],
) -> Optional[StatusReplyProductContext]:
    meta = dict(inbound_metadata or {})
    if not is_status_reply_inbound(meta):
        return None

    wa_id = str(meta.get("referred_wa_message_id") or "").strip()
    referred = dict(meta.get("referred_product") or {})
    catalog_id = str(referred.get("catalog_id") or "").strip()
    retailer_id = str(referred.get("product_retailer_id") or "").strip()

    status_text = ""
    source = "whatsapp_context"
    has_image_only = False

    if retailer_id:
        source = "referred_product"
        row, match_strategy = _match_catalog_product_in_text(
            db,
            tenant_id,
            "",
            catalog_retailer_id=retailer_id,
        )
        if row is not None:
            title = str(getattr(row, "title", "") or "").strip()
            return StatusReplyProductContext(
                source=f"{source}:{match_strategy}",
                product_title=title,
                product_id=getattr(row, "id", None),
                catalog_retailer_id=retailer_id,
                status_wa_message_id=wa_id,
                status_body_preview=status_text[:120],
                has_trusted_title=bool(title),
                referred_product=referred,
            )

    if wa_id:
        outbound = _lookup_outbound_by_wa_message_id(db, tenant_id, wa_id)
        status_text = _status_text_from_outbound(outbound)
        if status_text:
            source = "outbound_lookup"
        else:
            meta_out = dict(getattr(outbound, "extra_metadata", None) or {}) if outbound else {}
            ni = dict(meta_out.get("normalized_inbound") or {})
            mime = str(ni.get("mime_type") or meta_out.get("mime_type") or "")
            if mime.startswith("image/") or ni.get("source_type") == "image":
                has_image_only = True
                source = "outbound_image_only"

    if status_text:
        row, match_strategy = _match_catalog_product_in_text(db, tenant_id, status_text)
        if row is not None:
            title = str(getattr(row, "title", "") or "").strip()
            return StatusReplyProductContext(
                source=f"{source}:{match_strategy}",
                product_title=title,
                product_id=getattr(row, "id", None),
                status_wa_message_id=wa_id,
                status_body_preview=status_text[:120],
                has_trusted_title=bool(title),
                has_image_only=False,
                referred_product=referred,
            )
        return StatusReplyProductContext(
            source=f"{source}:unresolved_text",
            product_title="",
            status_wa_message_id=wa_id,
            status_body_preview=status_text[:120],
            has_trusted_title=False,
            has_image_only=False,
            referred_product=referred,
        )

    if has_image_only:
        return StatusReplyProductContext(
            source="outbound_image_only",
            status_wa_message_id=wa_id,
            has_trusted_title=False,
            has_image_only=True,
            referred_product=referred,
        )

    if wa_id or referred:
        return StatusReplyProductContext(
            source=source,
            status_wa_message_id=wa_id,
            has_trusted_title=False,
            has_image_only=not bool(catalog_id or retailer_id),
            referred_product=referred,
        )
    return None


def get_persisted_status_reply_context(state: Any) -> Dict[str, Any]:
    session = dict(getattr(state, "commerce_session", None) or {})
    raw = session.get(_SESSION_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _persist_status_reply_context(state: Any, ctx: StatusReplyProductContext) -> None:
    session = dict(getattr(state, "commerce_session", None) or {})
    session[_SESSION_KEY] = ctx.to_dict()
    state.commerce_session = session


def apply_status_reply_product_context_to_state(
    *,
    db: Any,
    tenant_id: int,
    message: str,
    state: Any,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[StatusReplyProductContext]:
    meta = dict(inbound_metadata or {})
    persisted = get_persisted_status_reply_context(state)
    resolved = resolve_status_reply_product_context(db, tenant_id, meta)

    if resolved is None and persisted.get("active"):
        if is_status_reply_follow_up_message(message):
            resolved = StatusReplyProductContext(
                source="persisted_session",
                product_title=str(persisted.get("product_title") or ""),
                product_id=persisted.get("product_id"),
                catalog_retailer_id=str(persisted.get("catalog_retailer_id") or ""),
                status_wa_message_id=str(persisted.get("status_wa_message_id") or ""),
                status_body_preview=str(persisted.get("status_body_preview") or ""),
                has_trusted_title=bool(persisted.get("has_trusted_title")),
                has_image_only=bool(persisted.get("has_image_only")),
                referred_product=dict(persisted.get("referred_product") or {}),
                quantity_hint=dict(persisted.get("quantity_hint") or {}),
            )
        else:
            return None

    if resolved is None:
        return None

    qty = extract_status_reply_quantity(message)
    if qty:
        resolved = StatusReplyProductContext(
            source=resolved.source,
            product_title=resolved.product_title,
            product_id=resolved.product_id,
            catalog_retailer_id=resolved.catalog_retailer_id,
            status_wa_message_id=resolved.status_wa_message_id,
            status_body_preview=resolved.status_body_preview,
            has_trusted_title=resolved.has_trusted_title,
            has_image_only=resolved.has_image_only,
            referred_product=dict(resolved.referred_product or {}),
            quantity_hint=qty,
        )

    _persist_status_reply_context(state, resolved)
    meta["status_reply_product_context"] = resolved.to_dict()

    if resolved.has_trusted_title and not getattr(state, "current_product_focus", None):
        focus = {
            "id": resolved.product_id,
            "title": resolved.product_title,
            "from_status_reply": True,
            "status_wa_message_id": resolved.status_wa_message_id,
        }
        if resolved.catalog_retailer_id:
            focus["product_retailer_id"] = resolved.catalog_retailer_id
        if qty:
            focus["requested_quantity"] = qty
        state.current_product_focus = focus
        try:
            state.product_focus_turn = int(getattr(state, "turn", 0) or 0)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — focus turn stamp is optional
            pass
        logger.info(
            "[STATUS_REPLY_CTX] pinned product focus tenant=%s title=%r source=%s qty=%s",
            tenant_id,
            resolved.product_title,
            resolved.source,
            qty or "-",
        )
    elif resolved.has_trusted_title and getattr(state, "current_product_focus", None):
        focus = dict(state.current_product_focus or {})
        if not focus.get("title"):
            focus["title"] = resolved.product_title
        focus.setdefault("from_status_reply", True)
        if qty:
            focus["requested_quantity"] = qty
        state.current_product_focus = focus

    return resolved


def compose_status_reply_product_goal(ctx: StatusReplyProductContext, message: str) -> str:
    if ctx.has_image_only and not ctx.has_trusted_title:
        return (
            "STATUS REPLY compose principles: customer replied to a status with a "
            "product image but no trusted product name in evidence; ask ONE clear "
            "clarifying question — which product in the status they mean and what "
            "size/quantity; do not claim availability or price; do not say "
            "التوفر قيد التحقق or generic catalog browse."
        )
    if ctx.has_trusted_title and ctx.quantity_hint:
        return (
            "STATUS REPLY compose principles: customer replied to a merchant status "
            f"about «{ctx.product_title}» and requested quantity/size; acknowledge "
            "naturally and continue toward price or order using catalog evidence only "
            "for that product; do not ask which type they want when the status product "
            "is already known; do not say التوفر قيد التحقق."
        )
    if ctx.has_trusted_title and _PRICE_FOLLOWUP_RE.search(message or ""):
        return (
            "STATUS REPLY compose principles: customer asks price for the product "
            f"from the status («{ctx.product_title}»); give price from catalog "
            "evidence or ask which size/variant if price depends on size; never "
            "reply with availability uncertainty or التوفر قيد التحقق."
        )
    if ctx.has_trusted_title:
        return (
            "STATUS REPLY compose principles: continue the status product thread "
            f"for «{ctx.product_title}»; use catalog evidence only; no generic browse."
        )
    return (
        "STATUS REPLY compose principles: customer replied to a status but product "
        "is not confirmed in evidence; ask ONE clarifying question for product name "
        "or size; no availability verification wording."
    )


def try_status_reply_product_decision(ctx: Any) -> Optional[Any]:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    state = getattr(ctx, "state", None)
    profile = getattr(ctx, "profile", None) or {}
    inbound_meta = dict(profile.get("inbound_metadata") or {})
    session_ctx = get_persisted_status_reply_context(state)
    if not session_ctx.get("active") and not is_status_reply_inbound(inbound_meta):
        return None

    message = str(getattr(ctx, "message", "") or "").strip()
    if not is_status_reply_follow_up_message(message) and not is_status_reply_inbound(inbound_meta):
        return None

    db = getattr(ctx, "_db", None)
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    sr = apply_status_reply_product_context_to_state(
        db=db,
        tenant_id=tenant_id,
        message=message,
        state=state,
        inbound_metadata=inbound_meta,
    )
    if sr is None:
        sr_data = session_ctx
        if not sr_data.get("active"):
            return None
        sr = StatusReplyProductContext(
            source=str(sr_data.get("source") or "persisted"),
            product_title=str(sr_data.get("product_title") or ""),
            product_id=sr_data.get("product_id"),
            has_trusted_title=bool(sr_data.get("has_trusted_title")),
            has_image_only=bool(sr_data.get("has_image_only")),
            quantity_hint=dict(sr_data.get("quantity_hint") or {}),
        )

    if sr.has_trusted_title and _PRICE_FOLLOWUP_RE.search(message):
        focus = dict(getattr(state, "current_product_focus", None) or {})
        if focus.get("title") or sr.product_title:
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": TOPIC_STATUS_REPLY_PRODUCT_CONTEXT,
                    "response_goal": compose_status_reply_product_goal(sr, message),
                    "product": {
                        "id": focus.get("id") or sr.product_id,
                        "title": focus.get("title") or sr.product_title,
                    },
                },
                reason="status_reply_product_context — price on status product",
                confidence=0.92,
            )
        from modules.ai.brain.product_discovery_gate import try_price_query_decision  # noqa: PLC0415

        price_dec = try_price_query_decision(ctx)
        if price_dec is not None:
            return price_dec

    needs_clarify = (not sr.has_trusted_title) or (
        sr.has_image_only and not sr.product_title
    )
    if needs_clarify and is_status_reply_follow_up_message(message):
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": TOPIC_STATUS_REPLY_PRODUCT_CONTEXT,
                "response_goal": compose_status_reply_product_goal(sr, message),
                "block_commerce_escalation": False,
            },
            reason="status_reply_product_context — clarify product from status",
            confidence=0.91,
        )

    if sr.has_trusted_title and (
        sr.quantity_hint or _ORDER_VERB_RE.search(message)
    ):
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": TOPIC_STATUS_REPLY_PRODUCT_CONTEXT,
                "response_goal": compose_status_reply_product_goal(sr, message),
                "product": {
                    "id": sr.product_id,
                    "title": sr.product_title,
                },
                "block_commerce_escalation": False,
            },
            reason="status_reply_product_context — quantity/order on status product",
            confidence=0.90,
        )

    if sr.has_trusted_title:
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": TOPIC_STATUS_REPLY_PRODUCT_CONTEXT,
                "response_goal": compose_status_reply_product_goal(sr, message),
                "product": {
                    "id": sr.product_id,
                    "title": sr.product_title,
                },
            },
            reason="status_reply_product_context — continue status thread",
            confidence=0.88,
        )
    return None


def status_reply_context_blocks_availability_fallback(
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    state: Any = None,
) -> bool:
    meta = dict(inbound_metadata or {})
    if isinstance(meta.get("status_reply_product_context"), dict):
        return True
    if get_persisted_status_reply_context(state).get("active"):
        return True
    focus = dict(getattr(state, "current_product_focus", None) or {})
    return bool(focus.get("from_status_reply"))


__all__ = [
    "TOPIC_STATUS_REPLY_PRODUCT_CONTEXT",
    "StatusReplyProductContext",
    "apply_status_reply_product_context_to_state",
    "compose_status_reply_product_goal",
    "extract_status_reply_quantity",
    "get_persisted_status_reply_context",
    "is_status_reply_follow_up_message",
    "is_status_reply_inbound",
    "resolve_status_reply_product_context",
    "status_reply_context_blocks_availability_fallback",
    "try_status_reply_product_decision",
]
