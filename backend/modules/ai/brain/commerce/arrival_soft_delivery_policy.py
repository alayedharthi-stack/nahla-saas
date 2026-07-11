"""
Arrival soft delivery — structured maps resend without deterministic prose (PR-C).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from modules.operations.branch_arrival_keyword_evidence import (
    BranchActionConfig,
)

logger = logging.getLogger("nahla.brain.arrival_soft_delivery")


@dataclass(frozen=True)
class ArrivalSoftDeliveryDecision:
    maps_url: str = ""
    cta_button_label: str = ""
    resend_maps: bool = False
    location_already_sent: bool = True
    branch_name: str = ""
    reason: str = "arrival_soft"
    skip_brain: bool = True


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

    return ArrivalSoftDeliveryDecision(
        maps_url=maps_url if resend else "",
        cta_button_label=cta_label,
        resend_maps=resend,
        location_already_sent=True,
        branch_name=(config.name or "").strip(),
        reason="arrival_soft_welcome",
    )


__all__ = [
    "ArrivalSoftDeliveryDecision",
    "evaluate_arrival_soft_delivery",
]
