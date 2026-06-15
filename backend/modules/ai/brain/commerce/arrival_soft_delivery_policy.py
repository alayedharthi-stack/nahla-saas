"""
Arrival soft delivery — pre-brain welcome without vCard or escalation (PR-C).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from modules.operations.branch_arrival_keyword_evidence import (
    ARRIVAL_MODE_LOCATION_AND_RECEPTION,
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


def evaluate_arrival_soft_delivery(
    config: BranchActionConfig,
) -> ArrivalSoftDeliveryDecision:
    resend = (
        config.arrival_response_mode == ARRIVAL_MODE_LOCATION_AND_RECEPTION
        and bool(config.maps_url)
    )
    cta_label = "موقع المتجر"
    if resend:
        try:
            from core.wa_link_buttons import classify_url  # noqa: PLC0415

            cls = classify_url(config.maps_url)
            if cls.button_title:
                cta_label = cls.button_title
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ARRIVAL_SOFT] cta_classify_failed err=%s", exc)

    return ArrivalSoftDeliveryDecision(
        reply_text=MSG_ARRIVAL_SOFT_WELCOME,
        maps_url=config.maps_url if resend else "",
        cta_button_label=cta_label,
        resend_maps=resend,
        reason="arrival_soft_welcome",
    )


__all__ = [
    "ArrivalSoftDeliveryDecision",
    "MSG_ARRIVAL_SOFT_WELCOME",
    "evaluate_arrival_soft_delivery",
]
