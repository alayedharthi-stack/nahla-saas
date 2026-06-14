"""
Location link policy — pre-brain deterministic maps URL delivery.

Runs before staff contact policy so «موقعكم وين» never enters escalation.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("nahla.brain.location_link_policy")

_FLAG_FALSY = frozenset({"0", "false", "no", "off"})


def location_link_policy_enabled() -> bool:
    raw = os.getenv("LOCATION_LINK_POLICY_ENABLED", "1").strip().lower()
    return raw not in _FLAG_FALSY


@dataclass(frozen=True)
class LocationLinkPolicyDecision:
    reply_text: str
    maps_url: str = ""
    source: str = ""
    reason: str = ""
    cta_button_label: str = ""
    use_cta: bool = False


def evaluate_location_link_policy(
    db: object,
    *,
    tenant_id: int,
    message: str,
) -> Optional[LocationLinkPolicyDecision]:
    """Return a short-circuit decision for physical location asks."""
    if not location_link_policy_enabled():
        return None

    from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
        MSG_LOCATION_NOT_CONFIGURED,
        is_location_query,
    )

    if not is_location_query(message or ""):
        return None

    try:
        from modules.ai.postprocess.safety_nets import (  # noqa: PLC0415
            _build_location_reply,
            _lookup_tenant_maps_url,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[LOCATION_LINK_POLICY] import_failed tenant=%s err=%s",
            tenant_id, exc,
        )
        return None

    maps_url, source = _lookup_tenant_maps_url(db, int(tenant_id or 0))
    if maps_url:
        cta_label = "موقع المتجر"
        use_cta = True
        try:
            from core.wa_link_buttons import classify_url  # noqa: PLC0415

            cls = classify_url(maps_url)
            if cls.button_title:
                cta_label = cls.button_title
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "[LOCATION_LINK_POLICY] tenant=%s deliver=true source=%s use_cta=true",
            tenant_id, source or "-",
        )
        return LocationLinkPolicyDecision(
            reply_text="موقعنا 📍",
            maps_url=maps_url,
            source=source or "",
            reason="maps_url_configured",
            cta_button_label=cta_label,
            use_cta=use_cta,
        )

    logger.info(
        "[LOCATION_LINK_POLICY] tenant=%s deliver=false reason=no_maps_url",
        tenant_id,
    )
    return LocationLinkPolicyDecision(
        reply_text=MSG_LOCATION_NOT_CONFIGURED,
        reason="no_maps_url_configured",
    )


__all__ = [
    "LocationLinkPolicyDecision",
    "evaluate_location_link_policy",
    "location_link_policy_enabled",
]
