"""
availability_guard_policy.py
────────────────────────────
Pure inbound/evidence policy for product availability truth guard.

Keeps operational rewrite decisions tenant-agnostic — no canned reply text.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, List, Optional

logger = logging.getLogger("nahla.brain.availability_guard_policy")

_BARE_ORDER_REF_RE = re.compile(r"^\d{6,12}$")

_BROWSE_ALTERNATIVES_PHRASES = (
    "وش غيرها",
    "ايش غيرها",
    "ايه غيرها",
    "وش ثاني",
    "ايش ثاني",
    "غيرها",
    "ثاني",
    "خيارات ثانيه",
    "خيارات ثانية",
    "what else",
    "anything else",
    "other options",
)

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return s.lower().strip()


def browse_alternatives_requested(message: str) -> bool:
    """Customer asks for other options after a prior browse turn."""
    norm = _norm_ar(message or "")
    if not norm:
        return False
    if norm in {"غيرها", "ثاني"}:
        return True
    return any(phrase in norm for phrase in _BROWSE_ALTERNATIVES_PHRASES)


def _recent_customer_order_reference(history: Optional[List[Any]]) -> str:
    if not history:
        return ""
    try:
        for turn in reversed(history):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("in", "inbound", ""):
                continue
            body = str((turn or {}).get("body") or "").strip()
            if not body:
                continue
            compact = re.sub(r"\s+", "", body)
            if _BARE_ORDER_REF_RE.match(compact):
                return compact
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _existing_order_support_thread(
    message: str,
    *,
    availability_context: Optional[Any] = None,
) -> bool:
    ctx = availability_context if isinstance(availability_context, dict) else {}
    history = ctx.get("history")
    try:
        from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: PLC0415
            is_placed_order_statement,
        )

        if is_placed_order_statement(message):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — placed-order probe is best-effort
        pass
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            has_existing_order_evidence,
        )

        if has_existing_order_evidence(
            state=ctx.get("state"),
            history=history,
            commerce_bundle=ctx.get("commerce_bundle"),
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — order evidence probe is best-effort
        pass
    if _recent_customer_order_reference(history):
        return True
    return False


def inbound_exempt_from_availability_rewrite(
    message: str,
    *,
    availability_context: Optional[Any] = None,
) -> bool:
    """
    Turns where availability claim rewrite must not fire — broad browse,
    browse continuation, delivery/location policy questions, staff/contact.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if _existing_order_support_thread(raw, availability_context=availability_context):
        return True
    try:
        from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
            should_block_product_availability_rewrite,
        )

        if should_block_product_availability_rewrite(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional staff guard import
        pass
    try:
        from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )

        if global_availability_browse_requested(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import at guard boundary
        pass
    if browse_alternatives_requested(raw):
        return True
    try:
        from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
            extract_inquiry_product_query,
            is_open_category_inquiry_turn,
        )

        inquiry_q = extract_inquiry_product_query(raw)
        if is_open_category_inquiry_turn(raw, inquiry_q):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional open-inquiry gate import
        pass
    try:
        from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: PLC0415
            is_conversational_non_product_inbound,
            is_negative_logistics_or_contact_context,
        )

        if is_negative_logistics_or_contact_context(raw):
            return True
        if is_conversational_non_product_inbound(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import at guard boundary
        pass
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            should_exempt_from_availability_rewrite,
        )

        if should_exempt_from_availability_rewrite(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import at guard boundary
        pass
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_arrival_or_visit_signal,
            is_location_query,
        )

        if is_location_query(raw) or is_arrival_or_visit_signal(raw):
            return True
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[AVAILABILITY_GUARD] contact_route_policy_check_failed err=%s",
            exc,
        )

        topic = detect_solution_seeking_suppression(raw, skip_recent_topic=True)
        if topic in {"delivery_intent", "location_intent"}:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import at guard boundary
        pass
    return False


def evidence_allows_positive_claim(evidence_state: str) -> bool:
    """True when a positive availability wording in the reply may pass through."""
    return evidence_state in {
        "resolved_available",
        "variant_options",
    }


def should_block_availability_rewrite(
    *,
    inbound_text: str,
    evidence_state: str,
    guard_action: str,
    availability_context: Optional[Any] = None,
) -> bool:
    """
    Block canned rewrites — pass the brain/compose reply through unchanged.

    Used for variant families, browse/delivery turns, staff/contact/showroom
    turns, and allowed evidence.
    """
    if inbound_exempt_from_availability_rewrite(inbound_text, availability_context=availability_context):
        return True
    try:
        from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
            should_block_product_availability_rewrite,
        )

        if should_block_product_availability_rewrite(
            inbound_text,
            guard_action=guard_action,
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional staff guard import
        pass
    if guard_action == "allowed":
        return True
    if guard_action in ("rewrite_conflict", "rewrite_unknown"):
        if evidence_state == "variant_options":
            return True
        if evidence_allows_positive_claim(evidence_state) and guard_action == "rewrite_conflict":
            return True
    return False
