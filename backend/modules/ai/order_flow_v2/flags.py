"""OrderFlowV2 feature flags — central gate for legacy checkout isolation."""
from __future__ import annotations


def _truthy(raw: str) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def is_order_flow_v2_enabled() -> bool:
    try:
        from core.config import ORDER_FLOW_V2_ENABLED  # noqa: PLC0415

        return bool(ORDER_FLOW_V2_ENABLED)
    except Exception:  # noqa: BLE001
        return False


def is_legacy_order_flow_disabled() -> bool:
    try:
        from core.config import LEGACY_ORDER_FLOW_DISABLED  # noqa: PLC0415

        return bool(LEGACY_ORDER_FLOW_DISABLED)
    except Exception:  # noqa: BLE001
        return False


def is_order_flow_v2_shadow_enabled() -> bool:
    try:
        from core.config import ORDER_FLOW_V2_SHADOW_ENABLED  # noqa: PLC0415

        return bool(ORDER_FLOW_V2_SHADOW_ENABLED)
    except Exception:  # noqa: BLE001
        return True


def should_skip_legacy_order_flow_reply() -> bool:
    """True when legacy checkout reply producers must not own the turn."""
    return is_order_flow_v2_enabled() or is_legacy_order_flow_disabled()


def is_v2_checkout_scope_active(order_prep: dict | None) -> bool:
    """True when this conversation turn is inside an active V2 checkout session."""
    prep = dict(order_prep or {})
    if prep.get("order_flow_v2_active"):
        return True
    return is_order_flow_v2_enabled() and bool(prep.get("order_flow_v2_pending"))
