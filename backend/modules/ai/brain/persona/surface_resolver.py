"""Map Brain compose paths to FactBoundPersonaComposer surfaces."""
from __future__ import annotations

from typing import Optional

from ..types import BrainContext
from .facts_bundle import PHASE2_SOCIAL_SURFACES


def _active_commerce_greeting_stage(ctx: BrainContext) -> Optional[str]:
    from ..state.stages import STAGE_CHECKOUT, STAGE_DECIDING, STAGE_ORDERING  # noqa: PLC0415

    stage = str(getattr(getattr(ctx, "state", None), "stage", "") or "")
    if stage == STAGE_CHECKOUT:
        return "checkout"
    if stage in {STAGE_ORDERING, STAGE_DECIDING}:
        return "ordering"
    return None


def _is_checkin_inbound(inbound_text: str) -> bool:
    from ..postprocess.social_single_reply_guard import (  # noqa: PLC0415
        _WELLBEING_PHRASES_RE,
    )

    return bool(_WELLBEING_PHRASES_RE.search(str(inbound_text or "").strip()))


def _is_dua_inbound(inbound_text: str) -> bool:
    from ..compose.persona_template_engine import (  # noqa: PLC0415
        inbound_is_religious_dua_exchange,
    )

    return inbound_is_religious_dua_exchange(inbound_text)


def _is_thanks_inbound(inbound_text: str) -> bool:
    from ..compose.persona_template_engine import (  # noqa: PLC0415
        _inbound_is_religious_thanks,
    )

    norm = str(inbound_text or "").strip().lower()
    if _inbound_is_religious_thanks(inbound_text):
        return True
    thanks_markers = ("شكر", "مشكور", "ما قصرت", "تسلم", "يعطيك العافية")
    return any(m in norm for m in thanks_markers)


def resolve_greet_surface(ctx: BrainContext, *, re_greet: bool = False) -> Optional[str]:
    """Return a Phase-2 social surface for ACTION_GREET, or None to keep legacy path."""
    inbound = str(getattr(ctx, "message", "") or "").strip()
    if not inbound:
        return "social_greeting"

    commerce_ctx = _active_commerce_greeting_stage(ctx)
    if commerce_ctx in {"checkout", "ordering"}:
        return None

    try:
        from ..postprocess.social_checkout_pressure_guard import (  # noqa: PLC0415
            is_pure_phatic_bypass_turn,
        )

        if not is_pure_phatic_bypass_turn(inbound) and not re_greet:
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — resolver must not break greet
        if not re_greet:
            return None

    if _is_checkin_inbound(inbound):
        return "social_checkin"
    if _is_thanks_inbound(inbound):
        return "thanks"
    if _is_dua_inbound(inbound):
        return "dua"
    return "social_greeting"


def resolve_social_surface(category: str, *, inbound_text: str = "") -> Optional[str]:
    """Return a Phase-2 social surface for ACTION_SOCIAL_REPLY, or None."""
    from ..postprocess.social_single_reply_guard import (  # noqa: PLC0415
        resolve_time_aware_social_category,
    )
    from ..persona_expression import is_template_only_social_category  # noqa: PLC0415

    cat = resolve_time_aware_social_category(
        (category or "general_courtesy").strip().lower() or "general_courtesy",
        inbound_text=inbound_text,
    )
    if is_template_only_social_category(cat):
        return None

    inbound = str(inbound_text or "").strip()
    if cat in {"thanks", "gratitude"} or _is_thanks_inbound(inbound):
        return "thanks"
    if cat in {"blessing", "dua", "religious_thanks"} or _is_dua_inbound(inbound):
        return "dua"
    if cat in {"wellbeing_check", "general_courtesy"} or _is_checkin_inbound(inbound):
        return "social_checkin"
    if cat in {"greeting", "salutation"}:
        return "social_greeting"

    try:
        from ..postprocess.social_checkout_pressure_guard import (  # noqa: PLC0415
            is_pure_phatic_bypass_turn,
        )

        if is_pure_phatic_bypass_turn(inbound):
            return "social_greeting"
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    return None


def is_allowed_phase2_surface(surface: str) -> bool:
    return str(surface or "").strip() in PHASE2_SOCIAL_SURFACES
