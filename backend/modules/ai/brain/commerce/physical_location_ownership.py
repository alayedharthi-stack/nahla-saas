"""
Physical location ownership — intent/action routing before catalog/storefront.

The system decides *what* to send (maps CTA vs missing-config brief).
Composer / LLM phrasing stays non-deterministic except for pre-approved
operational fallbacks (``MSG_LOCATION_NOT_CONFIGURED``).
"""
from __future__ import annotations

from typing import FrozenSet, Optional

from .link_intent import (
    LinkIntentType,
    is_explicit_direct_location_request,
    resolve_link_intent,
)

ACTION_SEND_STORE_LOCATION = "send_store_location"
ACTION_PHYSICAL_LOCATION_MISSING_CONFIG = "physical_location_missing_config"

FORBIDDEN_LOCATION_SUBSTITUTIONS: FrozenSet[str] = frozenset({
    "storefront_link",
    "catalog_send",
    "native_catalog_fallback",
    "product_browse",
})


def is_physical_location_request(message: str) -> bool:
    """True when the customer asks for a physical branch/showroom location."""
    return resolve_link_intent(message or "") == LinkIntentType.PHYSICAL_LOCATION


def is_website_storefront_request(message: str) -> bool:
    """True when the customer asks for the online store / website URL."""
    return resolve_link_intent(message or "") == LinkIntentType.WEBSITE_URL


def should_block_catalog_substitution_for_location(message: str) -> bool:
    """Block catalog / storefront fallback when intent is physical location."""
    return is_physical_location_request(message)


def build_physical_location_decision_args(
    *,
    maps_url: str = "",
) -> dict:
    """Decision args for brain path — action ownership without new templates."""
    url = str(maps_url or "").strip()
    if url:
        return {
            "response_purpose": ACTION_SEND_STORE_LOCATION,
            "required_action": ACTION_SEND_STORE_LOCATION,
            "forbidden_substitutions": sorted(FORBIDDEN_LOCATION_SUBSTITUTIONS),
            "maps_url": url,
        }
    return {
        "response_purpose": ACTION_PHYSICAL_LOCATION_MISSING_CONFIG,
        "required_action": ACTION_PHYSICAL_LOCATION_MISSING_CONFIG,
        "forbidden_substitutions": sorted(FORBIDDEN_LOCATION_SUBSTITUTIONS),
    }


__all__ = [
    "ACTION_PHYSICAL_LOCATION_MISSING_CONFIG",
    "ACTION_SEND_STORE_LOCATION",
    "FORBIDDEN_LOCATION_SUBSTITUTIONS",
    "build_physical_location_decision_args",
    "is_explicit_direct_location_request",
    "is_physical_location_request",
    "is_website_storefront_request",
    "resolve_link_intent",
    "should_block_catalog_substitution_for_location",
]
