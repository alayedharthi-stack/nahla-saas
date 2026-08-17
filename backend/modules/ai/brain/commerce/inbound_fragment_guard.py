"""
inbound_fragment_guard.py
─────────────────────────
Platform guards for repeated short inbound fragments and catalog fallback
containment on non-catalog turns (coupon/discount, social fragments, media).
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.brain.commerce.inbound_fragment_guard")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_FRAGMENT_MAX_LEN = 12
_PRAYER_FRAGMENT_MAX_LEN = 16
_FRAGMENT_TTL_SECONDS = 90.0
_FRAGMENT_CLARIFY_ONCE = (
    "وصلت رسالتك، تقدر توضّح طلبك أكثر؟"
)

_CATALOG_FALLBACK_MARKER = "الخيارات المؤكدة من الكتالوج"

_DISCOUNT_COUPON_RE = re.compile(
    r"(?:"
    r"كود\s+خصم|كود\s+تخفيض|"
    r"عندكم\s+كود|"
    r"الكود\s+ما|كود\s+ما|"
    r"كوبون|كوبونات|"
    r"خصم|تخفيض|"
    r"promo\s*code|discount\s*code|coupon"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Narrow: only very short blessing fragments (e.g. «بشرك»). Longer social
# replies such as «ابشرك والله بخير» defer to Brain persona — not catalog block.
_PRAYER_SOCIAL_FRAGMENT_RE = re.compile(
    r"^(?:"
    r"ب?شرك|"
    r"ب?شرك\s+الله|"
    r"ب?شرك\s+الله\s+بالخير"
    r")\s*[،,.!؟?🤍🌷]*$",
    re.UNICODE | re.IGNORECASE,
)

_UNSUPPORTED_MEDIA_TYPES = frozenset({
    "unsupported",
    "reaction",
    "revoke",
    "ephemeral",
    "system",
    "unknown",
})

_CATALOG_BROWSE_TOPICS = frozenset({
    "commerce_entry_catalog",
    "commerce_entry_catalog_delivery",
    "catalog_browse",
    "catalog_navigation",
    "product_search",
    "product_discovery",
})

_lock = threading.Lock()
_fragment_windows: Dict[Tuple[int, str, str], Tuple[float, int, bool]] = {}


def _normalize(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
    )
    return _WS_RE.sub(" ", t).strip()


def is_catalog_fallback_reply(text: str) -> bool:
    return _CATALOG_FALLBACK_MARKER in (text or "")


def is_discount_coupon_inquiry(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_DISCOUNT_COUPON_RE.search(_normalize(raw)))


def is_prayer_social_fragment(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _normalize(raw)
    if len(norm) > _PRAYER_FRAGMENT_MAX_LEN:
        return False
    return bool(_PRAYER_SOCIAL_FRAGMENT_RE.search(norm))


def is_unsupported_media_turn(
    inbound_metadata: Optional[Dict[str, Any]] = None,
    *,
    inbound_text: str = "",
) -> bool:
    meta = dict(inbound_metadata or {})
    media_type = str(
        meta.get("normalized_type")
        or meta.get("source_type")
        or meta.get("msg_type")
        or "",
    ).strip().lower()
    if media_type in _UNSUPPORTED_MEDIA_TYPES:
        return True
    body = (inbound_text or "").strip()
    if body.startswith("[") and (
        "unsupported" in body.lower()
        or "رسالة وسائط" in body
        or "merchant_" in body.lower()
    ):
        return True
    return False


def build_discount_coupon_support_reply(*, customer_has_code: bool = True) -> str:
    """Cautious coupon support — never catalog. TODO: policy-aware ask vs verify."""
    if customer_has_code:
        return "أرسل لي كود الخصم اللي عندك وأتحقق منه لك."
    return "أتحقق من أكواد الخصم المتاحة وأرجع لك."


def _has_explicit_catalog_browse_intent(
    inbound_text: str,
    *,
    intent: Any = None,
    decision_topic: str = "",
) -> bool:
    topic = str(decision_topic or "").strip().lower()
    if topic in _CATALOG_BROWSE_TOPICS:
        return True
    try:
        from modules.ai.brain.catalog.catalog_browse_turn_policy import (  # noqa: PLC0415
            is_catalog_browse_message,
        )

        intent_name = str(getattr(intent, "name", "") or "")
        if is_catalog_browse_message(inbound_text or "", intent_name=intent_name):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[INBOUND_FRAGMENT_GUARD] catalog_browse_probe_failed")
    return False


def should_block_catalog_grounding_fallback(
    *,
    inbound_text: str = "",
    inbound_metadata: Optional[Dict[str, Any]] = None,
    intent: Any = None,
    decision_topic: str = "",
    protected_final_reply: bool = False,
    facts: Any = None,
) -> Tuple[bool, str]:
    """Return (blocked, reason) when catalog fallback must not replace the reply."""
    if protected_final_reply:
        return True, "protected_final_reply"

    intent_name = str(getattr(intent, "name", "") or "").strip()
    if intent_name in {
        "track_order",
        "latest_order_summary",
        "order_history_count",
        "order_reference_list",
    }:
        return True, "order_evidence_owner"

    if _has_explicit_catalog_browse_intent(
        inbound_text,
        intent=intent,
        decision_topic=decision_topic,
    ):
        return False, ""

    if getattr(facts, "has_coupons", False) or getattr(facts, "shareable_promotions", None):
        return True, "promotion_facts_present"

    if is_discount_coupon_inquiry(inbound_text):
        return True, "discount_coupon_inquiry"

    if is_prayer_social_fragment(inbound_text):
        return True, "prayer_social_fragment"

    if is_unsupported_media_turn(inbound_metadata, inbound_text=inbound_text):
        return True, "unsupported_media"

    norm = _normalize(inbound_text)
    if norm and len(norm) <= 12:
        if not _has_explicit_catalog_browse_intent(
            inbound_text,
            intent=intent,
            decision_topic=decision_topic,
        ):
            try:
                from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
                    has_explicit_product_commerce_intent,
                )

                if not has_explicit_product_commerce_intent(inbound_text or ""):
                    return True, "short_non_catalog_fragment"
            except Exception:  # noqa: BLE001
                return True, "short_non_catalog_fragment"

    meta = dict(inbound_metadata or {})
    if meta.get("inbound_fragment_duplicate"):
        return True, "duplicate_fragment"

    return False, ""


@dataclass(frozen=True)
class DuplicateFragmentDecision:
    process_turn: bool
    send_clarification_once: bool = False
    reason: str = ""


def evaluate_duplicate_fragment_turn(
    *,
    tenant_id: int,
    customer_phone: str,
    text: str,
    max_len: int = _FRAGMENT_MAX_LEN,
    ttl_seconds: float = _FRAGMENT_TTL_SECONDS,
) -> DuplicateFragmentDecision:
    """
    Short repeated inbound fragments from the same customer should not each
    spawn a full brain turn with catalog fallback.
    """
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            extract_bare_order_reference,
        )

        if extract_bare_order_reference(text):
            return DuplicateFragmentDecision(process_turn=True)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — bare order ref probe is best-effort
        pass

    norm = _normalize(text)
    if not norm or len(norm) > max_len:
        return DuplicateFragmentDecision(process_turn=True)

    phone = (customer_phone or "").strip()
    if not phone:
        return DuplicateFragmentDecision(process_turn=True)

    key = (int(tenant_id), phone, norm)
    now = time.monotonic()

    with _lock:
        existing = _fragment_windows.get(key)
        if existing is not None:
            first_seen, count, clarification_sent = existing
            if now - first_seen > ttl_seconds:
                existing = None
            else:
                count += 1
                if count == 2 and not clarification_sent:
                    _fragment_windows[key] = (first_seen, count, True)
                    return DuplicateFragmentDecision(
                        process_turn=False,
                        send_clarification_once=True,
                        reason="duplicate_fragment_clarify_once",
                    )
                _fragment_windows[key] = (first_seen, count, clarification_sent)
                return DuplicateFragmentDecision(
                    process_turn=False,
                    reason="duplicate_fragment_silent",
                )

        _fragment_windows[key] = (now, 1, False)
        if len(_fragment_windows) > 2048:
            stale = [
                k for k, (started, _, _) in _fragment_windows.items()
                if now - started > ttl_seconds
            ]
            for k in stale[:128]:
                _fragment_windows.pop(k, None)

    return DuplicateFragmentDecision(process_turn=True)


def duplicate_fragment_clarification_reply(
    *,
    inbound_text: str = "",
) -> str:
    del inbound_text
    return _FRAGMENT_CLARIFY_ONCE


def reset_fragment_cache_for_tests() -> None:
    with _lock:
        _fragment_windows.clear()


__all__ = [
    "DuplicateFragmentDecision",
    "build_discount_coupon_support_reply",
    "duplicate_fragment_clarification_reply",
    "evaluate_duplicate_fragment_turn",
    "is_catalog_fallback_reply",
    "is_discount_coupon_inquiry",
    "is_prayer_social_fragment",
    "is_unsupported_media_turn",
    "reset_fragment_cache_for_tests",
    "should_block_catalog_grounding_fallback",
]
