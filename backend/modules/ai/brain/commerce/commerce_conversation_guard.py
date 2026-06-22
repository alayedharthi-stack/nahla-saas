"""
Commerce conversation guard — P0 drift prevention (platform-wide).

Deterministic inbound/outbound guards:
  • Quoted bot echo strip
  • Social ack / dua (no product extraction)
  • Category lock persistence
  • Variant + order intent capture
  • Catalog-grounded availability hints

Personality stays in compose; guards own operational truth.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from modules.ai.brain.types import INTENT_ASK_COD, INTENT_SOCIAL

from .commerce_browse_category_guard import (
    extract_browse_category_scope,
    filter_products_to_browse_category,
    should_exclude_cross_category_product,
)
from .commerce_inquiry_boundary import (
    has_explicit_order_select_signal,
    is_commerce_inquiry_turn,
)

logger = logging.getLogger("nahla.brain.commerce.conversation_guard")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_SOCIAL_ACK_RE = re.compile(
    r"(?:"
    r"الله\s+يسعدك|الله\s+يحفظك|جزاك\s+الله|جزاكم\s+الله|"
    r"مشكور|مشكورة|تشرفت|تشرفنا|"
    r"مبشر(?:ة)?\s+بالخير|بالخير\s+والجنه|بالجنه|"
    r"وياك\s+يارب|وإياك\s+يارب|آ?مين|amen|"
    r"الله\s+يعطيك\s+العافيه|الله\s+يعطيكم\s+العافيه|"
    r"يعطيك\s+العافيه|يعطيكم\s+العافيه|"
    r"الله\s+يعافيك|الله\s+يعافيكم|"
    r"بيض\s+الله\s+وجه|بارك\s+الله\s+فيك|حفظك\s+الله|"
    r"^\s*تسلم(?:ي|وا|ون)?(?:\s|$|[.!،])"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Colloquial delivery-received + blessing (e.g. «وصل والله يبيض وجهك»).
_DELIVERY_SOCIAL_THANKS_RE = re.compile(
    r"(?:"
    r"^وصل(?:ت|نا|ني)?\s+.*?(?:بيض|يبيض|بارك|الله|وجه|فيك|لك|شكر|حلال|مال)"
    r"|(?:^|\s)وصل(?:ت|نا|ني)?(?:\s|$).*(?:الله|بيض|يبيض|بارك)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_COD_RE = re.compile(
    r"(?:"
    r"الدفع\s+عند\s+الاستلام|دفع\s+عند\s+الاستلام|"
    r"كاش\s+عند\s+التسليم|كاش\s+عند\s+الاستلام|"
    r"أ?حاسب\s+عند\s+وصول|أ?سلم\s+المبلغ\s+وقت\s+الاستلام|"
    r"تسليم\s+المبلغ\s+وقت\s+استلام|"
    r"هل\s+أ?دفع\s+عند\s+الاستلام|"
    r"مافي\s+إ?مكان(?:ية)?\s+تسليم\s+المبلغ\s+وقت\s+استلام|"
    r"\bcod\b"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_VARIANT_WEIGHT_RE = re.compile(
    r"(?:"
    r"ربع\s*كيل?و|نصف\s*كيل?و|"
    r"كيل?و\s*واحد|1\s*كيل?و|٢\s*كيل?و|2\s*كيل?و|"
    r"٣\s*كيل?و|3\s*كيل?و|"
    r"quarter\s*kg|half\s*kg|1\s*kg"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_IN_TEXT_RE = re.compile(
    r"(\d{2,4})\s*(?:ريال|r(?:iyal)?|sar)?",
    re.UNICODE | re.IGNORECASE,
)

# Platform category families — not merchant SKUs.
_HONEY_TOKENS = frozenset({"عسل", "اعسال", "honey"})
_HONEY_SUBTYPE_HINTS = frozenset({"سدر", "طلح", "سمر", "برسيم", "sider", "talh"})
_ORDER_VERB_TOKENS = frozenset({
    "احتاج", "ابي", "ابغ", "اريد", "want", "need",
})
_PURE_NON_COMMERCE_RE = re.compile(
    r"(?:"
    r"^(?:مرحبا|مرحب(?:ة|اً)?|هلا|اهلا|أ?هلا|"
    r"السلام\s+عليكم|سلام\s+عليكم|"
    r"صباح\s+ال(?:خير|نور)|مساء\s+ال(?:خير|نور)|"
    r"hello|hi|hey|good\s+(?:morning|evening)|"
    r"كيف\s+حالك|شلونك|وش\s+اخبارك)\s*[.!?؟]*$"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_VENOM_DRIFT_MARKERS = frozenset({
    "سم", "كريم", "زيت", "venom", "cream", "oil", "خليه", "خلية", "طلع", "نخيل",
})


@dataclass
class CommerceSession:
    active_category: str = ""
    active_catalog_group_slug: str = ""
    active_product: str = ""
    active_variant: str = ""
    active_price: Optional[float] = None
    availability_status: str = ""
    order_intent: bool = False
    stage: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_category": self.active_category,
            "active_catalog_group_slug": self.active_catalog_group_slug,
            "active_product": self.active_product,
            "active_variant": self.active_variant,
            "active_price": self.active_price,
            "availability_status": self.availability_status,
            "order_intent": bool(self.order_intent),
            "stage": self.stage,
        }

    @staticmethod
    def from_dict(raw: Optional[Dict[str, Any]]) -> "CommerceSession":
        d = dict(raw or {})
        price = d.get("active_price")
        try:
            price_val = float(price) if price is not None and str(price).strip() else None
        except (TypeError, ValueError):
            price_val = None
        return CommerceSession(
            active_category=str(d.get("active_category") or ""),
            active_catalog_group_slug=str(d.get("active_catalog_group_slug") or ""),
            active_product=str(d.get("active_product") or ""),
            active_variant=str(d.get("active_variant") or ""),
            active_price=price_val,
            availability_status=str(d.get("availability_status") or ""),
            order_intent=bool(d.get("order_intent")),
            stage=str(d.get("stage") or ""),
        )


@dataclass
class CommerceInboundPrep:
    original_message: str
    customer_addition: str
    message_for_classification: str
    quoted_bot_stripped: bool = False
    is_social_ack_only: bool = False
    is_ask_cod: bool = False
    intent_override: Optional[str] = None
    variant_selection: Dict[str, Any] = field(default_factory=dict)
    category_scope: Optional[str] = None
    session: CommerceSession = field(default_factory=CommerceSession)
    availability_note: str = ""
    order_summary_hint: str = ""
    is_browse_inquiry: bool = False


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text).strip().lower())
    s = _NORM_RE.sub("", s)
    s = (
        s.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", s).strip()


def _tokens(text: str) -> List[str]:
    n = _norm(text)
    return [t for t in re.split(r"[\s,،.!؟?\n]+", n) if t]


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _recent_bot_bodies(history: Sequence[Mapping[str, Any]], limit: int = 4) -> List[str]:
    bodies: List[str] = []
    for turn in reversed(list(history or [])):
        if not isinstance(turn, dict):
            continue
        direction = str(turn.get("direction") or turn.get("role") or "").lower()
        if direction in ("out", "outbound", "assistant"):
            body = str(turn.get("body") or turn.get("content") or "").strip()
            if body:
                bodies.append(body)
        if len(bodies) >= limit:
            break
    return bodies


def strip_quoted_bot_echo(
    message: str,
    history: Sequence[Mapping[str, Any]],
    *,
    similarity_threshold: float = 0.72,
) -> Tuple[str, bool]:
    """Remove lines/paragraphs that echo recent bot replies."""
    raw = (message or "").strip()
    if not raw:
        return raw, False

    bot_bodies = _recent_bot_bodies(history)
    if not bot_bodies:
        return raw, False

    chunks = re.split(r"\n\s*\n|\n(?=\S)", raw)
    kept: List[str] = []
    stripped_any = False

    for chunk in chunks:
        piece = chunk.strip()
        if not piece:
            continue
        is_echo = False
        for bot in bot_bodies:
            if _similarity(piece, bot) >= similarity_threshold:
                is_echo = True
                break
            for bot_line in bot.splitlines():
                bot_line = bot_line.strip()
                if len(bot_line) < 12:
                    continue
                if _similarity(piece, bot_line) >= similarity_threshold:
                    is_echo = True
                    break
            if is_echo:
                break
        if is_echo:
            stripped_any = True
        else:
            kept.append(piece)

    customer_addition = "\n".join(kept).strip()
    if not customer_addition and stripped_any:
        customer_addition = raw
    return customer_addition, stripped_any


def is_delivery_social_thanks(text: str) -> bool:
    """True for post-delivery social thanks — not store arrival."""
    raw = (text or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if not _DELIVERY_SOCIAL_THANKS_RE.search(norm):
        return False
    productish = any(
        tok in norm
        for tok in ("عسل", "كilo", "كيلو", "سدر", "طلح", "ريال", "سعر", "متوفر")
    )
    return not productish


def is_social_ack_message(text: str) -> bool:
    norm = _norm(text)
    if not norm:
        return False
    if is_delivery_social_thanks(text):
        return True
    if _SOCIAL_ACK_RE.search(norm):
        productish = any(
            tok in norm
            for tok in ("عسل", "كilo", "كيلو", "سدر", "طلح", "ريال", "سعر", "متوفر")
        )
        return not productish
    tokens = set(_tokens(norm))
    if tokens <= {"آمين", "امين", "amen", "مشكور", "مشكورة"}:
        return True
    return False


def detect_ask_cod(text: str) -> bool:
    return bool(_COD_RE.search(_norm(text)))


def _detect_honey_scope(text: str) -> Optional[str]:
    norm = _norm(text)
    if not norm:
        return None
    tokens = set(_tokens(norm))
    if tokens & _HONEY_TOKENS or "عسل" in norm:
        return "عسل"
    if tokens & _HONEY_SUBTYPE_HINTS or any(h in norm for h in _HONEY_SUBTYPE_HINTS):
        return "عسل"
    return None


def _catalog_row_is_honey_sku(row: Mapping[str, Any]) -> bool:
    """True when a catalog row is honey-family, not cream/oil derivative."""
    title = str(row.get("title") or row.get("name") or "")
    category = str(row.get("category") or "")
    blob = _norm(f"{category} {title}")
    if not blob:
        return False
    if any(marker in blob.split() or marker in blob for marker in _VENOM_DRIFT_MARKERS):
        if "عسل" not in blob and not (set(_tokens(blob)) & _HONEY_SUBTYPE_HINTS):
            return False
    if "عسل" in blob or (set(_tokens(blob)) & _HONEY_TOKENS):
        return True
    if set(_tokens(blob)) & _HONEY_SUBTYPE_HINTS:
        return True
    try:
        from .honey_browse_strategy import classify_honey_type  # noqa: PLC0415

        if classify_honey_type(title):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — honey type classify optional
        pass
    return False


def catalog_has_honey_skus(catalog: Sequence[Mapping[str, Any]]) -> bool:
    """Platform-wide: any synced honey SKU — used to lock browse after order intent."""
    return any(
        isinstance(row, Mapping) and _catalog_row_is_honey_sku(row)
        for row in (catalog or [])
    )


def maybe_lock_honey_order_context(
    state: Any,
    message: str,
    *,
    catalog: Sequence[Mapping[str, Any]] = (),
) -> bool:
    """
    Persist honey category lock after bare order-start in honey-capable catalogs.

    Returns True when ``commerce_session.active_category`` was set to ``عسل``.
    """
    try:
        from .start_order_verb_guard import is_bare_start_order_phrase  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional guard import must not block session lock
        return False

    if not is_bare_start_order_phrase(message or ""):
        return False
    if not catalog_has_honey_skus(catalog):
        return False

    session = load_commerce_session(state)
    session.order_intent = True
    session.stage = session.stage or "order_intent"
    if not str(session.active_category or "").strip():
        session.active_category = "عسل"
        apply_commerce_session(state, session)
        logger.info(
            "[COMMERCE_SESSION] locked active_category=عسل reason=bare_start_order",
        )
        return True
    apply_commerce_session(state, session)
    return str(session.active_category or "").strip() == "عسل"


def _product_matches_name(catalog_row: Mapping[str, Any], name_hint: str) -> bool:
    hint = _norm(name_hint)
    if not hint:
        return False
    blob = _norm(
        " ".join(
            str(catalog_row.get(k) or "")
            for k in ("title", "name", "category")
        )
    )
    return hint in blob or all(part in blob for part in hint.split() if len(part) >= 3)


def catalog_availability_for_name(
    product_name: str,
    catalog: Sequence[Mapping[str, Any]],
) -> str:
    """Return ``available`` | ``unavailable`` | ``unknown`` from catalog rows."""
    matches = [p for p in catalog if _product_matches_name(p, product_name)]
    if not matches:
        return "unknown"
    for row in matches:
        qty = row.get("quantity")
        if qty is None:
            qty = row.get("stock")
        if qty is None:
            avail = row.get("available")
            if avail is False:
                continue
            if avail is True:
                return "available"
            continue
        try:
            if int(qty) > 0:
                return "available"
        except (TypeError, ValueError):
            return "available"
    return "unavailable"


def filter_catalog_for_active_category(
    catalog: Sequence[Mapping[str, Any]],
    *,
    category_scope: str,
    message: str = "",
) -> List[Dict[str, Any]]:
    rows = [dict(p) for p in catalog]
    if not category_scope:
        return rows
    scoped = filter_products_to_browse_category(
        rows,
        message=message or category_scope,
        query=category_scope,
    )
    return scoped or rows


def product_title_drifts_from_honey(title: str) -> bool:
    norm = _norm(title)
    if not norm:
        return False
    if any(m in norm for m in _VENOM_DRIFT_MARKERS):
        if "عسل" not in norm:
            return True
    return should_exclude_cross_category_product(
        {"title": title, "category": "عسل"},
        scope="عسل",
        message="",
    )


def detect_variant_order_selection(
    text: str,
    *,
    session: CommerceSession,
    catalog: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    norm = _norm(text)
    if not norm:
        return {}

    if is_commerce_inquiry_turn(text) and not has_explicit_order_select_signal(text):
        return {}

    variant = ""
    if _VARIANT_WEIGHT_RE.search(norm):
        variant = _VARIANT_WEIGHT_RE.search(norm).group(0).strip()

    product = session.active_product or ""
    for row in catalog:
        title = str(row.get("title") or row.get("name") or "")
        if title and _norm(title) in norm:
            product = title
            break
    if not product:
        for hint in ("طلح", "سدر", "سمر", "برسيم"):
            if hint in norm:
                product = f"عسل {hint}"
                break

    price = session.active_price
    price_match = _PRICE_IN_TEXT_RE.search(text or "")
    if price_match:
        try:
            price = float(price_match.group(1))
        except (TypeError, ValueError):
            pass

    has_order_signal = bool(variant) or bool(set(_tokens(norm)) & _ORDER_VERB_TOKENS)
    if not has_order_signal:
        return {}

    return {
        "active_product": product,
        "active_variant": variant,
        "active_price": price,
        "order_intent": True,
        "stage": "variant_selected",
    }


def build_order_summary_hint(session: CommerceSession) -> str:
    if not session.order_intent or not session.active_product:
        return ""
    parts = [f"{session.active_product}"]
    if session.active_variant:
        parts.append(f"— {session.active_variant}")
    line = " ".join(parts)
    if session.active_price is not None:
        line += f"\nالسعر: {int(session.active_price)} ريال"
    line += "\nنكمل الطلب: أرسل الاسم، المدينة، الحي، ورقم الجوال."
    return line.strip()


def load_commerce_session(state: Any) -> CommerceSession:
    raw = getattr(state, "commerce_session", None)
    if isinstance(raw, CommerceSession):
        return raw
    if isinstance(raw, dict):
        return CommerceSession.from_dict(raw)
    return CommerceSession()


def apply_commerce_session(state: Any, session: CommerceSession) -> None:
    try:
        state.commerce_session = session.to_dict()
    except Exception:  # noqa: silent-ok - state patch is best-effort on duck-typed state
        pass


def prepare_commerce_inbound(
    message: str,
    *,
    state: Any = None,
    history: Sequence[Mapping[str, Any]] = (),
    catalog: Sequence[Mapping[str, Any]] = (),
) -> CommerceInboundPrep:
    """Pre-classify commerce guard — mutates session on ``state`` when provided."""
    session = load_commerce_session(state)
    original = (message or "").strip()

    customer_addition, stripped = strip_quoted_bot_echo(original, history)
    classify_text = customer_addition or original

    if is_social_ack_message(classify_text) and not detect_ask_cod(classify_text):
        return CommerceInboundPrep(
            original_message=original,
            customer_addition=classify_text,
            message_for_classification=classify_text,
            quoted_bot_stripped=stripped,
            is_social_ack_only=True,
            intent_override=INTENT_SOCIAL,
            session=session,
        )

    if detect_ask_cod(classify_text):
        return CommerceInboundPrep(
            original_message=original,
            customer_addition=classify_text,
            message_for_classification=classify_text,
            quoted_bot_stripped=stripped,
            is_ask_cod=True,
            intent_override=INTENT_ASK_COD,
            session=session,
        )

    if _PURE_NON_COMMERCE_RE.search(_norm(classify_text)):
        return CommerceInboundPrep(
            original_message=original,
            customer_addition=classify_text,
            message_for_classification=classify_text,
            quoted_bot_stripped=stripped,
            session=session,
        )

    scope = _detect_honey_scope(classify_text)
    browse_inquiry = is_commerce_inquiry_turn(classify_text) and not has_explicit_order_select_signal(
        classify_text,
    )
    if scope:
        session.active_category = scope
        session.stage = session.stage or "category_browse"

    if scope and "سدر" in _norm(classify_text):
        avail = catalog_availability_for_name("سدر", catalog)
        session.availability_status = avail
        if avail == "unavailable" and has_explicit_order_select_signal(classify_text):
            session.active_product = session.active_product or "عسل سدر"

    if browse_inquiry:
        session.order_intent = False
        if state is not None:
            try:
                state.last_browse_query = classify_text
                if getattr(state, "stage", "") == "ordering":
                    state.stage = "exploring"
                if getattr(state, "pending_action", "") == "collect_delivery_info":
                    state.pending_action = ""
            except Exception:  # noqa: silent-ok - state patch is best-effort on duck-typed state
                pass

    variant_patch = detect_variant_order_selection(
        classify_text, session=session, catalog=catalog,
    )
    if variant_patch and not browse_inquiry:
        session.active_product = str(variant_patch.get("active_product") or session.active_product)
        session.active_variant = str(variant_patch.get("active_variant") or session.active_variant)
        price = variant_patch.get("active_price")
        if price is not None:
            session.active_price = float(price)
        session.order_intent = True
        session.stage = "variant_selected"
        if state is not None:
            apply_commerce_session(state, session)
            try:
                state.stage = "ordering"
                state.pending_action = "collect_delivery_info"
            except Exception:  # noqa: silent-ok - state patch is best-effort on duck-typed state
                pass
        apply_commerce_session(state, session)

    availability_note = ""
    if session.availability_status == "unavailable" and "سدر" in _norm(classify_text):
        availability_note = "sider_unavailable"

    return CommerceInboundPrep(
        original_message=original,
        customer_addition=classify_text,
        message_for_classification=classify_text,
        quoted_bot_stripped=stripped,
        category_scope=scope or session.active_category or None,
        session=session,
        availability_note=availability_note,
        order_summary_hint=build_order_summary_hint(session),
        is_browse_inquiry=browse_inquiry,
    )


__all__ = [
    "CommerceInboundPrep",
    "CommerceSession",
    "apply_commerce_session",
    "build_order_summary_hint",
    "catalog_availability_for_name",
    "catalog_has_honey_skus",
    "detect_ask_cod",
    "filter_catalog_for_active_category",
    "is_delivery_social_thanks",
    "is_social_ack_message",
    "load_commerce_session",
    "maybe_lock_honey_order_context",
    "prepare_commerce_inbound",
    "product_title_drifts_from_honey",
    "strip_quoted_bot_echo",
]
