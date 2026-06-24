"""OrderFlowV2 ingest — apply customer slots from inbound text."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from core.wa_address_ingestion import resolve_address_state_patch
from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots

_NAME_SPLIT_RE = re.compile(r"\s+")
_ARABIC_NAME_RE = re.compile(r"^[\u0600-\u06FF\s]+$")


def _split_full_name(text: str) -> Dict[str, str]:
    parts = [p for p in _NAME_SPLIT_RE.split(str(text or "").strip()) if p]
    if len(parts) < 2:
        return {}
    if not _ARABIC_NAME_RE.match(text.strip()):
        return {}
    return {
        "customer_first_name": parts[0],
        "customer_last_name": " ".join(parts[1:]),
    }


def apply_inbound_slots(
    *,
    message: str,
    inbound_normalized_type: str = "text",
    inbound_metadata: Optional[Dict[str, Any]] = None,
    order_prep: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a state patch from deterministic slot extraction."""
    patch: Dict[str, Any] = {}
    prep = dict(order_prep or {})
    text = str(message or "").strip()
    if not text:
        return patch

    addr_patch = resolve_address_state_patch(
        inbound_normalized_type=inbound_normalized_type,
        inbound_metadata=inbound_metadata,
        inbound_text=text,
    )
    if addr_patch:
        patch.update(addr_patch)

    slots = extract_ordering_slots(text)
    for key in (
        "customer_first_name",
        "customer_last_name",
        "city",
        "short_address_code",
        "google_maps_url",
        "latitude",
        "longitude",
        "address_line",
    ):
        val = slots.get(key)
        if val not in (None, "") and not prep.get(key):
            patch[key] = val

    if not prep.get("customer_first_name") and not patch.get("customer_first_name"):
        name_patch = _split_full_name(text)
        patch.update({k: v for k, v in name_patch.items() if v and not prep.get(k)})

    return patch
