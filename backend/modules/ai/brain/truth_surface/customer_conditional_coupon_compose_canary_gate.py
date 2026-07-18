"""
customer_conditional_coupon_compose_canary_gate.py
──────────────────────────────────────────────────
Early, tenant-scoped, test-mode compose canary gate for conditional-coupon
Layer 0 reads and compose routing.

Shadow observation remains independent of this gate. Compose master flag alone
does not authorize I/O — all canary conditions must pass first.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional, Set, Tuple

from core.tenant import STORE_AI_MODE_TEST, merge_ai_defaults, resolve_store_ai_mode

from .flags import is_customer_conditional_coupon_compose_enabled

AI_SETTINGS_ALLOWLIST_KEY = "customer_conditional_coupon_compose_allowlist_tenants"
ENV_ALLOWLIST_KEY = "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ALLOWLIST_TENANTS"
MAX_ALLOWLIST_TENANTS = 64

REASON_COMPOSE_MASTER_DISABLED = "compose_master_disabled"
REASON_TENANT_MISSING = "tenant_missing"
REASON_TENANT_NOT_ALLOWLISTED = "tenant_not_allowlisted"
REASON_ALLOWLIST_CONFIG_MALFORMED = "allowlist_config_malformed"
REASON_NOT_TEST_MODE = "not_test_mode"
REASON_STORE_MODE_UNKNOWN = "store_mode_unknown"
REASON_NOT_RELEVANT = "not_relevant"
REASON_PHONE_MISSING = "phone_missing"
REASON_PHONE_NOT_ALLOWLISTED = "phone_not_allowlisted"
REASON_POLICY_EVALUATION_ERROR = "policy_evaluation_error"
REASON_ALLOWED = "allowed"

_ALLOWLIST_PARSE_CACHE: Optional[Tuple[str, Optional[FrozenSet[int]]]] = None


@dataclass(frozen=True)
class CustomerConditionalCouponComposeCanaryDecision:
    """Single source of truth for compose-canary eligibility on one turn."""

    allowed: bool
    reason: str
    compose_master_enabled: bool = False
    relevance_required: bool = False
    relevance_satisfied: bool = False


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def _parse_allowlist_tokens(raw: str) -> Tuple[Optional[FrozenSet[int]], Optional[str]]:
    tokens = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    if not tokens:
        return frozenset(), None
    if len(tokens) > MAX_ALLOWLIST_TENANTS:
        return None, REASON_ALLOWLIST_CONFIG_MALFORMED
    out: set[int] = set()
    for token in tokens:
        try:
            value = int(token)
        except (TypeError, ValueError):
            return None, REASON_ALLOWLIST_CONFIG_MALFORMED
        if value <= 0:
            return None, REASON_ALLOWLIST_CONFIG_MALFORMED
        out.add(value)
    return frozenset(out), None


def _parse_allowlist_from_ai_settings(
    ai_settings: Optional[Dict[str, Any]],
) -> Tuple[Optional[FrozenSet[int]], Optional[str], bool]:
    """Return (tenants, error_reason, configured_in_ai_settings)."""
    if not isinstance(ai_settings, dict):
        return None, None, False
    if AI_SETTINGS_ALLOWLIST_KEY not in ai_settings:
        return None, None, False
    raw = ai_settings.get(AI_SETTINGS_ALLOWLIST_KEY)
    if raw is None:
        return frozenset(), None, True
    if not isinstance(raw, list):
        return None, REASON_ALLOWLIST_CONFIG_MALFORMED, True
    if len(raw) > MAX_ALLOWLIST_TENANTS:
        return None, REASON_ALLOWLIST_CONFIG_MALFORMED, True
    out: set[int] = set()
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            return None, REASON_ALLOWLIST_CONFIG_MALFORMED, True
        if value <= 0:
            return None, REASON_ALLOWLIST_CONFIG_MALFORMED, True
        out.add(value)
    return frozenset(out), None, True


def _resolve_allowlist_tenants(
    ai_settings: Optional[Dict[str, Any]],
) -> Tuple[Optional[FrozenSet[int]], Optional[str]]:
    global _ALLOWLIST_PARSE_CACHE

    ai_tenants, ai_error, ai_configured = _parse_allowlist_from_ai_settings(ai_settings)
    if ai_error:
        return None, ai_error

    env_raw = os.getenv(ENV_ALLOWLIST_KEY, "").strip()
    env_tenants: Optional[FrozenSet[int]] = None
    if env_raw:
        if _ALLOWLIST_PARSE_CACHE is not None:
            status, cached = _ALLOWLIST_PARSE_CACHE
            if status == "malformed":
                return None, REASON_ALLOWLIST_CONFIG_MALFORMED
            env_tenants = cached
        else:
            parsed, env_error = _parse_allowlist_tokens(env_raw)
            if env_error:
                _ALLOWLIST_PARSE_CACHE = ("malformed", None)
                return None, env_error
            _ALLOWLIST_PARSE_CACHE = ("ok", parsed)
            env_tenants = parsed
    elif _ALLOWLIST_PARSE_CACHE is not None and _ALLOWLIST_PARSE_CACHE[0] == "malformed":
        return None, REASON_ALLOWLIST_CONFIG_MALFORMED

    if ai_configured:
        return ai_tenants, None
    if env_tenants is not None:
        return env_tenants, None
    return frozenset(), None


def clear_customer_conditional_coupon_compose_canary_allowlist_cache() -> None:
    """Test helper — reset env allowlist parse cache."""
    global _ALLOWLIST_PARSE_CACHE
    _ALLOWLIST_PARSE_CACHE = None


def evaluate_customer_conditional_coupon_compose_canary(
    *,
    tenant_id: Optional[int],
    customer_phone: str = "",
    message: str = "",
    inbound_metadata: Optional[Dict[str, Any]] = None,
    ai_settings: Optional[Dict[str, Any]] = None,
    require_relevance: bool = True,
) -> CustomerConditionalCouponComposeCanaryDecision:
    """
    Evaluate compose canary eligibility for one turn.

  Fail-closed on missing tenant, malformed allowlist, unknown store mode,
  absent phone when phone gating applies, relevance miss, or policy errors.
    """
    compose_master_enabled = is_customer_conditional_coupon_compose_enabled()
    if not compose_master_enabled:
        return CustomerConditionalCouponComposeCanaryDecision(
            allowed=False,
            reason=REASON_COMPOSE_MASTER_DISABLED,
            compose_master_enabled=False,
            relevance_required=require_relevance,
            relevance_satisfied=False,
        )

    try:
        if tenant_id is None or int(tenant_id) <= 0:
            return CustomerConditionalCouponComposeCanaryDecision(
                allowed=False,
                reason=REASON_TENANT_MISSING,
                compose_master_enabled=True,
                relevance_required=require_relevance,
                relevance_satisfied=False,
            )

        allowlist, allowlist_error = _resolve_allowlist_tenants(ai_settings)
        if allowlist_error:
            return CustomerConditionalCouponComposeCanaryDecision(
                allowed=False,
                reason=allowlist_error,
                compose_master_enabled=True,
                relevance_required=require_relevance,
                relevance_satisfied=False,
            )
        if int(tenant_id) not in (allowlist or frozenset()):
            return CustomerConditionalCouponComposeCanaryDecision(
                allowed=False,
                reason=REASON_TENANT_NOT_ALLOWLISTED,
                compose_master_enabled=True,
                relevance_required=require_relevance,
                relevance_satisfied=False,
            )

        ai = merge_ai_defaults(dict(ai_settings or {}))
        store_mode = resolve_store_ai_mode(ai)
        if store_mode not in {STORE_AI_MODE_TEST}:
            if not ai_settings:
                reason = REASON_STORE_MODE_UNKNOWN
            else:
                reason = REASON_NOT_TEST_MODE
            return CustomerConditionalCouponComposeCanaryDecision(
                allowed=False,
                reason=reason,
                compose_master_enabled=True,
                relevance_required=require_relevance,
                relevance_satisfied=False,
            )

        from .customer_conditional_coupon_loader import (  # noqa: PLC0415
            should_load_customer_conditional_coupon_facts,
        )

        relevance_satisfied = should_load_customer_conditional_coupon_facts(
            message=message,
            inbound_metadata=inbound_metadata,
        )
        if require_relevance and not relevance_satisfied:
            return CustomerConditionalCouponComposeCanaryDecision(
                allowed=False,
                reason=REASON_NOT_RELEVANT,
                compose_master_enabled=True,
                relevance_required=True,
                relevance_satisfied=False,
            )

        phone = _normalize_phone(customer_phone)
        allow_phones = {
            _normalize_phone(p)
            for p in (ai.get("ai_test_allowed_numbers") or [])
            if str(p).strip()
        }
        if not phone:
            return CustomerConditionalCouponComposeCanaryDecision(
                allowed=False,
                reason=REASON_PHONE_MISSING,
                compose_master_enabled=True,
                relevance_required=require_relevance,
                relevance_satisfied=relevance_satisfied,
            )
        if phone not in allow_phones:
            return CustomerConditionalCouponComposeCanaryDecision(
                allowed=False,
                reason=REASON_PHONE_NOT_ALLOWLISTED,
                compose_master_enabled=True,
                relevance_required=require_relevance,
                relevance_satisfied=relevance_satisfied,
            )

        return CustomerConditionalCouponComposeCanaryDecision(
            allowed=True,
            reason=REASON_ALLOWED,
            compose_master_enabled=True,
            relevance_required=require_relevance,
            relevance_satisfied=relevance_satisfied,
        )
    except Exception:  # noqa: BLE001
        return CustomerConditionalCouponComposeCanaryDecision(
            allowed=False,
            reason=REASON_POLICY_EVALUATION_ERROR,
            compose_master_enabled=compose_master_enabled,
            relevance_required=require_relevance,
            relevance_satisfied=False,
        )


def is_customer_conditional_coupon_compose_canary_allowed(
    *,
    tenant_id: Optional[int],
    customer_phone: str = "",
    message: str = "",
    inbound_metadata: Optional[Dict[str, Any]] = None,
    ai_settings: Optional[Dict[str, Any]] = None,
    require_relevance: bool = True,
) -> bool:
    return evaluate_customer_conditional_coupon_compose_canary(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        message=message,
        inbound_metadata=inbound_metadata,
        ai_settings=ai_settings,
        require_relevance=require_relevance,
    ).allowed


def should_load_customer_conditional_coupon_layer0_for_turn(
    *,
    tenant_id: Optional[int],
    customer_phone: str = "",
    message: str = "",
    inbound_metadata: Optional[Dict[str, Any]] = None,
    ai_settings: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Layer 0 load gate for one turn.

    Shadow flag bypasses compose canary. Compose path requires full canary pass.
    """
    from .flags import (  # noqa: PLC0415
        is_customer_conditional_coupon_compose_enabled,
        is_customer_conditional_coupon_shadow_enabled,
    )

    if (
        not is_customer_conditional_coupon_shadow_enabled()
        and not is_customer_conditional_coupon_compose_enabled()
    ):
        return False, "layer0_flags_disabled"

    if is_customer_conditional_coupon_shadow_enabled():
        from .customer_conditional_coupon_loader import (  # noqa: PLC0415
            should_load_customer_conditional_coupon_facts,
        )

        if should_load_customer_conditional_coupon_facts(
            message=message,
            inbound_metadata=inbound_metadata,
        ):
            return True, REASON_ALLOWED
        return False, REASON_NOT_RELEVANT

    decision = evaluate_customer_conditional_coupon_compose_canary(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        message=message,
        inbound_metadata=inbound_metadata,
        ai_settings=ai_settings,
        require_relevance=True,
    )
    return decision.allowed, decision.reason


def compose_canary_gate_telemetry_metadata(
    decision: CustomerConditionalCouponComposeCanaryDecision,
) -> Dict[str, Any]:
    """Auditable gate telemetry — no allowlist contents or PII."""
    return {
        "conditional_coupon_compose_canary_allowed": bool(decision.allowed),
        "conditional_coupon_compose_canary_reason": str(decision.reason or ""),
        "conditional_coupon_compose_master_enabled": bool(decision.compose_master_enabled),
        "conditional_coupon_compose_relevance_required": bool(decision.relevance_required),
        "conditional_coupon_compose_relevance_satisfied": bool(decision.relevance_satisfied),
    }


__all__ = [
    "AI_SETTINGS_ALLOWLIST_KEY",
    "CustomerConditionalCouponComposeCanaryDecision",
    "ENV_ALLOWLIST_KEY",
    "REASON_ALLOWLIST_CONFIG_MALFORMED",
    "REASON_ALLOWED",
    "REASON_COMPOSE_MASTER_DISABLED",
    "REASON_NOT_RELEVANT",
    "REASON_NOT_TEST_MODE",
    "REASON_PHONE_MISSING",
    "REASON_PHONE_NOT_ALLOWLISTED",
    "REASON_POLICY_EVALUATION_ERROR",
    "REASON_STORE_MODE_UNKNOWN",
    "REASON_TENANT_MISSING",
    "REASON_TENANT_NOT_ALLOWLISTED",
    "clear_customer_conditional_coupon_compose_canary_allowlist_cache",
    "compose_canary_gate_telemetry_metadata",
    "evaluate_customer_conditional_coupon_compose_canary",
    "is_customer_conditional_coupon_compose_canary_allowed",
    "should_load_customer_conditional_coupon_layer0_for_turn",
]
