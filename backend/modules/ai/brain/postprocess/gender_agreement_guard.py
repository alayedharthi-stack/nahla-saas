"""
postprocess/gender_agreement_guard.py
─────────────────────────────────────
Final outbound grammar guard for Arabic gender agreement.

Post-compose only — swaps individual address words, never canned replies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from modules.ai.gender.address_guard import apply_address_gender_guard
from modules.ai.gender.context import (
    REPLY_STYLE_NEUTRAL,
    CustomerGenderContext,
    resolve_customer_gender_context,
)

logger = logging.getLogger("nahla.brain.postprocess.gender_agreement_guard")


@dataclass(frozen=True)
class GenderAgreementGuardResult:
    reply: str
    replaced: bool
    reason: str = ""
    reply_style: str = REPLY_STYLE_NEUTRAL
    gender: str = "unknown"
    gender_source: str = "unknown"
    swap_count: int = 0


def validate_gender_agreement(
    reply: str,
    gender_context: CustomerGenderContext,
) -> GenderAgreementGuardResult:
    """Fix only gender-mismatched address words; leave correct replies intact."""
    text = reply or ""
    if not text.strip():
        return GenderAgreementGuardResult(
            reply=text,
            replaced=False,
            reply_style=gender_context.reply_style,
            gender=gender_context.gender,
            gender_source=gender_context.source,
        )

    fixed = apply_address_gender_guard(text, gender_context.reply_style)
    reason = ""
    if fixed.changed:
        reason = f"gender_address_guard:{fixed.mode}:swaps={fixed.swaps}"

    return GenderAgreementGuardResult(
        reply=fixed.text,
        replaced=fixed.changed,
        reason=reason,
        reply_style=fixed.mode,
        gender=gender_context.gender,
        gender_source=gender_context.source,
        swap_count=fixed.swaps,
    )


def apply_gender_agreement_guard(
    reply: str,
    *,
    gender_context: Optional[CustomerGenderContext] = None,
    message: str = "",
    customer_name: str = "",
    state: Any = None,
    profile: Optional[dict] = None,
    tenant_id: Any = None,
) -> GenderAgreementGuardResult:
    """Resolve gender context (when omitted) and enforce address agreement."""
    ctx = gender_context or resolve_customer_gender_context(
        message=message,
        customer_name=customer_name,
        state=state,
        profile=profile,
    )
    result = validate_gender_agreement(reply or "", ctx)
    if result.replaced:
        try:
            logger.info(
                "[GENDER_AGREEMENT_GUARD] tenant=%s gender=%s source=%s "
                "style=%s swaps=%d preview=%r",
                tenant_id,
                result.gender,
                result.gender_source,
                result.reply_style,
                result.swap_count,
                (result.reply or "")[:80],
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not block send
            pass
    return result


__all__ = [
    "GenderAgreementGuardResult",
    "apply_gender_agreement_guard",
    "validate_gender_agreement",
]
