"""Isolated coupon capability probe (Phase 2B shadow + Phase 2C canary consume).

The probe itself never mutates intent, Brain state, coupon rows, or WhatsApp
output. It never issues coupons. Canary routing may read the classification
after a separate ownership gate. Fail-closed to capability=none.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional

from services.customer_request_coupon_canary import is_customer_coupon_canary_tenant
from services.customer_request_coupon_service import (
    CUSTOMER_COUPON_LIVE_ISSUANCE,
    CUSTOMER_COUPON_LIVE_ROUTING,
)

logger = logging.getLogger("nahla.brain.coupon_capability_probe")

SHADOW_PROBE_ENV = "NAHLA_CUSTOMER_COUPON_CAPABILITY_PROBE_SHADOW"
PROBE_TIMEOUT_SECONDS = 4.0
CAPABILITY_CUSTOMER_COUPON_REQUEST = "customer_coupon_request"
CAPABILITY_NONE = "none"
ALLOWED_CAPABILITIES = frozenset({CAPABILITY_CUSTOMER_COUPON_REQUEST, CAPABILITY_NONE})

# Semantic purpose only. No customer utterance examples, keyword lists,
# regex, or Arabic phrase tables. Do not modify slot_extractor._SYSTEM.
COUPON_CAPABILITY_PROBE_SYSTEM = """You classify the semantic purpose of one customer message for a store assistant.

Decide whether the customer is asking the store to provide, reveal, or grant a redeemable personal coupon, code, or benefit for that customer.

Return JSON only with this exact schema:
{"capability":"customer_coupon_request"}
or
{"capability":"none"}

Use customer_coupon_request only when the customer's purpose is to obtain a redeemable personal coupon benefit from the store for themselves.

It does not mean the customer is merely describing, asking about, or referring to a discount or price reduction already associated with a product, quantity, bundle, campaign, promotion, or listed price, unless the semantic purpose is actually to obtain a redeemable coupon benefit for themselves.

Use none for every other purpose, including product discovery, price questions, catalog browsing, starting an order, choosing a variant, shipping, payment, tracking, order history, store information, human handoff, greetings, complaints, price dissatisfaction without requesting a benefit, and purchase hesitation.

Do not invent coupon codes, discount amounts, expiry, or eligibility.
Do not classify from isolated surface words. Infer the customer's purpose.
If uncertain, return none.
"""

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def customer_coupon_live_routing_enabled() -> bool:
    return bool(CUSTOMER_COUPON_LIVE_ROUTING)


def customer_coupon_live_issuance_enabled() -> bool:
    return bool(CUSTOMER_COUPON_LIVE_ISSUANCE)


def shadow_coupon_capability_probe_enabled() -> bool:
    raw = str(os.environ.get(SHADOW_PROBE_ENV, "") or "").strip().lower()
    return raw in _TRUTHY


def _shadow_telemetry(
    *,
    capability: str = CAPABILITY_NONE,
    run: bool = False,
    parse_ok: bool = True,
    probe_ms: int = 0,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "coupon_capability_probe_run": bool(run),
        "coupon_capability": capability if capability in ALLOWED_CAPABILITIES else CAPABILITY_NONE,
        "coupon_capability_probe_ms": int(probe_ms),
        "coupon_capability_parse_ok": bool(parse_ok),
        "coupon_capability_shadow_only": True,
        "coupon_capability_live_routing": False,
        "coupon_capability_live_issuance": False,
    }
    if extra:
        payload.update(extra)
    return payload


def parse_coupon_capability_payload(raw: Any) -> tuple[str, bool]:
    """Fail-closed parser. Unknown/invalid → (none, False)."""
    if raw is None:
        return CAPABILITY_NONE, False
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if not text:
        return CAPABILITY_NONE, False
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return CAPABILITY_NONE, False
        try:
            parsed = json.loads(text[start : end + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            return CAPABILITY_NONE, False
    if not isinstance(parsed, dict):
        return CAPABILITY_NONE, False
    value = parsed.get("capability")
    if value not in ALLOWED_CAPABILITIES:
        return CAPABILITY_NONE, False
    if set(parsed.keys()) - {"capability"}:
        # Extra keys are ignored; capability still accepted if valid.
        pass
    return str(value), True


def _resolve_probe_model() -> str:
    tiny = os.environ.get("NAHLA_MODEL_TINY", "").strip()
    if tiny:
        return tiny
    from modules.ai.orchestrator.customer_chat_models import (  # noqa: PLC0415
        resolve_tiny_customer_chat_model,
    )

    return resolve_tiny_customer_chat_model()


async def run_coupon_capability_probe(
    message: str,
    *,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Execute the isolated probe. Never issues coupons. Never mutates Brain."""
    started = time.monotonic()
    user_content = str(message or "").strip()
    if not user_content:
        return _shadow_telemetry(run=True, parse_ok=True, probe_ms=0)

    model = _resolve_probe_model()
    try:
        import asyncio  # noqa: PLC0415

        from modules.ai.orchestrator.providers.openai_compatible_provider import (  # noqa: PLC0415
            OpenAICompatibleProvider,
        )

        provider = OpenAICompatibleProvider()
        audit_context = {
            "model_override": model,
            "reason": "brain.intent.coupon_capability_probe_shadow",
            "estimated_input_tokens": (len(COUPON_CAPABILITY_PROBE_SYSTEM) + len(user_content)) // 4,
        }
        result = await asyncio.wait_for(
            asyncio.to_thread(
                provider.call,
                user_content,
                COUPON_CAPABILITY_PROBE_SYSTEM,
                audit_context=audit_context,
            ),
            timeout=timeout_seconds,
        )
        raw_text = ""
        if isinstance(result, dict):
            raw_text = str(
                result.get("reply_text")
                or result.get("text")
                or result.get("content")
                or ""
            )
        elif result is not None:
            raw_text = str(result)
        capability, parse_ok = parse_coupon_capability_payload(raw_text)
        ms = int((time.monotonic() - started) * 1000)
        return _shadow_telemetry(
            capability=capability,
            run=True,
            parse_ok=parse_ok,
            probe_ms=ms,
        )
    except TimeoutError:
        ms = int((time.monotonic() - started) * 1000)
        logger.info("coupon_capability_probe timeout ms=%s", ms)
        return _shadow_telemetry(run=True, parse_ok=False, probe_ms=ms)
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — shadow probe fail-closed
        ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "coupon_capability_probe provider_error class=%s ms=%s",
            exc.__class__.__name__,
            ms,
        )
        return _shadow_telemetry(run=True, parse_ok=False, probe_ms=ms)


async def maybe_run_shadow_coupon_capability_probe(
    message: str,
    history: Any = None,
) -> Dict[str, Any]:
    """Production entry: no-op unless the shadow env flag is on.

    ``history`` is accepted for call-site compatibility and ignored so the
    probe cannot encode prior-turn phrase tables.
    """
    del history
    if CUSTOMER_COUPON_LIVE_ROUTING or CUSTOMER_COUPON_LIVE_ISSUANCE:
        return _shadow_telemetry(run=False, parse_ok=True)
    if not shadow_coupon_capability_probe_enabled():
        return _shadow_telemetry(run=False, parse_ok=True)
    return await run_coupon_capability_probe(message)


async def maybe_run_coupon_capability_probe_for_turn(
    message: str,
    *,
    tenant_id: Optional[int] = None,
    history: Any = None,
) -> Dict[str, Any]:
    """Run the isolated probe when shadow telemetry is on OR the tenant is canary.

    Non-canary + shadow off → no model call, capability=none (existing path).
    Probe failure/timeout → fail closed. Never allocates coupons.
    ``history`` is accepted for call-site compatibility and ignored.
    """
    del history
    canary = is_customer_coupon_canary_tenant(tenant_id)
    shadow = shadow_coupon_capability_probe_enabled()
    if not canary and not shadow:
        return _shadow_telemetry(
            run=False,
            parse_ok=True,
            extra={
                "coupon_capability_canary_eligible": False,
                "coupon_capability_shadow_only": True,
            },
        )
    result = await run_coupon_capability_probe(message)
    result["coupon_capability_canary_eligible"] = canary
    result["coupon_capability_shadow_only"] = not canary
    return result
