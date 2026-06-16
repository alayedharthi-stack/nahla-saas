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
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.availability_guard_policy")

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


def inbound_exempt_from_availability_rewrite(message: str) -> bool:
    """
    Turns where availability claim rewrite must not fire — broad browse,
    browse continuation, delivery/location policy questions.
    """
    raw = (message or "").strip()
    if not raw:
        return False
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
        from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: PLC0415
            is_conversational_non_product_inbound,
        )

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
) -> bool:
    """
    Block canned rewrites — pass the brain/compose reply through unchanged.

    Used for variant families, browse/delivery turns, and allowed evidence.
    """
    if inbound_exempt_from_availability_rewrite(inbound_text):
        return True
    if guard_action == "allowed":
        return True
    if guard_action in ("rewrite_conflict", "rewrite_unknown"):
        if evidence_state == "variant_options":
            return True
        if evidence_allows_positive_claim(evidence_state) and guard_action == "rewrite_conflict":
            return True
    return False
