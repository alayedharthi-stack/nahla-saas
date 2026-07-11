"""
Arrival soft delivery — pre-brain welcome without vCard or escalation (PR-C).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from modules.operations.branch_arrival_keyword_evidence import (
    BranchActionConfig,
)

logger = logging.getLogger("nahla.brain.arrival_soft_delivery")

MSG_ARRIVAL_SOFT_WELCOME = "أهلاً بك، في انتظارك 🌷"


@dataclass(frozen=True)
class ArrivalSoftDeliveryDecision:
    reply_text: str
    maps_url: str = ""
    cta_button_label: str = ""
    resend_maps: bool = False
    reason: str = "arrival_soft"
    skip_brain: bool = True


def _location_reminder_text(config: BranchActionConfig) -> str:
    maps_url = (config.maps_url or "").strip()
    if not maps_url:
        return ""
    try:
        from modules.ai.postprocess.safety_nets import _build_location_reply  # noqa: PLC0415

        return _build_location_reply(
            maps_url,
            branch_name=(config.name or "").strip(),
            has_branch_details=bool((config.name or "").strip()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ARRIVAL_SOFT] location_reply_build_failed err=%s", exc)
        return maps_url


def evaluate_arrival_soft_delivery(
    config: BranchActionConfig,
) -> ArrivalSoftDeliveryDecision:
    maps_url = (config.maps_url or "").strip()
    resend = bool(maps_url)
    cta_label = "موقع المتجر"
    if resend:
        try:
            from core.wa_link_buttons import classify_url  # noqa: PLC0415

            cls = classify_url(maps_url)
            if cls.button_title:
                cta_label = cls.button_title
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ARRIVAL_SOFT] cta_classify_failed err=%s", exc)

    reply_parts = [MSG_ARRIVAL_SOFT_WELCOME]
    location_text = _location_reminder_text(config)
    if location_text:
        reply_parts.append(location_text)

    return ArrivalSoftDeliveryDecision(
        reply_text="\n\n".join(reply_parts),
        maps_url=maps_url if resend else "",
        cta_button_label=cta_label,
        resend_maps=resend,
        reason="arrival_soft_welcome",
    )


__all__ = [
    "ArrivalSoftDeliveryDecision",
    "MSG_ARRIVAL_SOFT_WELCOME",
    "evaluate_arrival_soft_delivery",
]
