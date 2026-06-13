"""
ai_usage_ledger.py
──────────────────
Fail-safe persistence for per-LLM-call usage and cost rows.

Never stores message or prompt content. Ledger write failures must not
break customer replies.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from modules.ai.orchestrator.ai_usage_pricing import compute_usage_cost_usd
from modules.ai.orchestrator.llm_cost_audit import approx_tokens_from_chars

_log = logging.getLogger("nahla.ai.usage_ledger")

TOKEN_SOURCE_ACTUAL = "actual"
TOKEN_SOURCE_ESTIMATED = "estimated"


def extract_anthropic_usage(
    *,
    response: Any = None,
    httpx_data: Optional[Mapping[str, Any]] = None,
) -> Tuple[Optional[Dict[str, int]], bool]:
    """
    Parse Anthropic usage from SDK response or httpx JSON.

    Returns (usage_dict, has_actual_usage). When usage object exists,
    token_source must be ``actual`` even if counts are zero.
    """
    if response is not None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None, False
        return {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            "cache_write_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        }, True

    if httpx_data is not None:
        usage = httpx_data.get("usage")
        if not isinstance(usage, dict):
            return None, False
        return {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        }, True

    return None, False


def _resolve_store_id(tenant_id: Optional[int], store_id: Optional[int]) -> Optional[int]:
    if store_id is not None:
        return store_id
    return tenant_id


def record_ai_usage_event(
    *,
    tenant_id: Optional[int] = None,
    store_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    turn_id: Optional[int] = None,
    provider: str,
    model: str,
    reason: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    estimated_input_tokens: Optional[int] = None,
    estimated_output_tokens: Optional[int] = None,
    token_source: str,
    request_id: Optional[str] = None,
    db: Any = None,
) -> None:
    """Persist one ledger row; never raises."""
    try:
        if token_source == TOKEN_SOURCE_ACTUAL:
            bill_input = int(input_tokens or 0)
            bill_output = int(output_tokens or 0)
            bill_cache_read = int(cache_read_tokens or 0)
            bill_cache_write = int(cache_write_tokens or 0)
        else:
            bill_input = int(estimated_input_tokens or 0)
            bill_output = int(estimated_output_tokens or 0)
            bill_cache_read = 0
            bill_cache_write = 0

        costs = compute_usage_cost_usd(
            provider=provider,
            model=model,
            input_tokens=bill_input,
            output_tokens=bill_output,
            cache_read_tokens=bill_cache_read,
            cache_write_tokens=bill_cache_write,
        )

        from database.models import AIUsageEvent  # noqa: PLC0415

        row = AIUsageEvent(
            tenant_id=tenant_id,
            store_id=_resolve_store_id(tenant_id, store_id),
            conversation_id=conversation_id,
            turn_id=turn_id,
            provider=(provider or "unknown")[:64],
            model=(model or "unknown")[:128],
            reason=(reason or "unknown")[:128],
            input_tokens=input_tokens if token_source == TOKEN_SOURCE_ACTUAL else None,
            output_tokens=output_tokens if token_source == TOKEN_SOURCE_ACTUAL else None,
            cache_read_tokens=cache_read_tokens if token_source == TOKEN_SOURCE_ACTUAL else None,
            cache_write_tokens=cache_write_tokens if token_source == TOKEN_SOURCE_ACTUAL else None,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            token_source=token_source,
            input_cost_usd=costs["input_cost_usd"],
            output_cost_usd=costs["output_cost_usd"],
            cache_cost_usd=costs["cache_cost_usd"],
            total_cost_usd=costs["total_cost_usd"],
            pricing_version=costs["pricing_version"],
            request_id=request_id,
        )

        if db is not None:
            db.add(row)
            db.flush()
            return

        from database.session import SessionLocal  # noqa: PLC0415

        session = SessionLocal()
        try:
            session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 — ledger must never break replies
        _log.warning(
            "[AI_USAGE_LEDGER_WRITE_ERROR] provider=%s model=%s reason=%s err=%s",
            provider,
            model,
            reason,
            type(exc).__name__,
        )


def record_ai_usage_from_anthropic(
    *,
    audit_extra: Optional[Mapping[str, Any]] = None,
    model: str,
    provider: str = "anthropic",
    response: Any = None,
    httpx_data: Optional[Mapping[str, Any]] = None,
    reply_text: str = "",
    total_prompt_chars: int = 0,
    db: Any = None,
) -> None:
    """Convenience wrapper for Anthropic call sites; never raises."""
    try:
        extra = dict(audit_extra or {})
        usage, has_actual = extract_anthropic_usage(response=response, httpx_data=httpx_data)
        est_input = int(
            extra.get("estimated_input_tokens")
            or approx_tokens_from_chars(total_prompt_chars)
        )
        est_output = approx_tokens_from_chars(len(reply_text or ""))

        request_id = None
        if response is not None:
            request_id = getattr(response, "id", None)
        elif httpx_data is not None:
            request_id = httpx_data.get("id")

        if has_actual and usage is not None:
            record_ai_usage_event(
                tenant_id=extra.get("tenant_id"),
                store_id=extra.get("store_id"),
                conversation_id=extra.get("conversation_id"),
                turn_id=extra.get("turn_id"),
                provider=provider,
                model=model,
                reason=str(extra.get("reason") or "anthropic_provider._call_internal"),
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_read_tokens=usage["cache_read_tokens"],
                cache_write_tokens=usage["cache_write_tokens"],
                estimated_input_tokens=est_input,
                estimated_output_tokens=est_output,
                token_source=TOKEN_SOURCE_ACTUAL,
                request_id=request_id,
                db=db,
            )
            return

        record_ai_usage_event(
            tenant_id=extra.get("tenant_id"),
            store_id=extra.get("store_id"),
            conversation_id=extra.get("conversation_id"),
            turn_id=extra.get("turn_id"),
            provider=provider,
            model=model,
            reason=str(extra.get("reason") or "anthropic_provider._call_internal"),
            estimated_input_tokens=est_input,
            estimated_output_tokens=est_output,
            token_source=TOKEN_SOURCE_ESTIMATED,
            request_id=request_id,
            db=db,
        )
    except Exception as exc:  # noqa: BLE001 — ledger must never break replies
        _log.warning(
            "[AI_USAGE_LEDGER_WRITE_ERROR] provider=%s model=%s err=%s",
            provider,
            model,
            type(exc).__name__,
        )


def ledger_period_start(period: str) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    key = (period or "7d").strip().lower()
    if key in {"24h", "1d"}:
        return now - timedelta(hours=24)
    if key in {"7d", "week"}:
        return now - timedelta(days=7)
    if key in {"mtd", "month"}:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if key in {"all", "lifetime", ""}:
        return None
    return now - timedelta(days=7)


def _decimal_sum(values: List[Any]) -> Decimal:
    total = Decimal("0")
    for value in values:
        if value is None:
            continue
        total += Decimal(str(value))
    return total


def _round_display_usd(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001")))


def _aggregate_events(events: List[Any]) -> Dict[str, Any]:
    actual_cost = Decimal("0")
    estimated_cost = Decimal("0")
    unattributed_cost = Decimal("0")
    actual_tokens = 0
    estimated_tokens = 0
    models: Dict[str, int] = {}
    providers: Dict[str, int] = {}
    reasons: Dict[str, int] = {}
    calls_total = 0

    for event in events:
        calls_total += 1
        cost = Decimal(str(event.total_cost_usd or 0))
        models[event.model] = models.get(event.model, 0) + 1
        providers[event.provider] = providers.get(event.provider, 0) + 1
        reasons[event.reason] = reasons.get(event.reason, 0) + 1

        if event.tenant_id is None:
            unattributed_cost += cost
            continue

        if event.token_source == TOKEN_SOURCE_ACTUAL:
            actual_cost += cost
            actual_tokens += int(event.input_tokens or 0) + int(event.output_tokens or 0)
        else:
            estimated_cost += cost
            estimated_tokens += int(event.estimated_input_tokens or 0) + int(
                event.estimated_output_tokens or 0
            )

    return {
        "calls_total": calls_total,
        "actual_total_cost_usd": _round_display_usd(actual_cost),
        "estimated_total_cost_usd": _round_display_usd(estimated_cost),
        "unattributed_total_cost_usd": _round_display_usd(unattributed_cost),
        "actual_total_tokens": actual_tokens,
        "estimated_total_tokens": estimated_tokens,
        "models": [
            {"model": model, "count": count}
            for model, count in sorted(models.items(), key=lambda item: item[1], reverse=True)
        ],
        "providers": [
            {"provider": provider, "count": count}
            for provider, count in sorted(providers.items(), key=lambda item: item[1], reverse=True)
        ],
        "reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reasons.items(), key=lambda item: item[1], reverse=True)
        ],
    }


def aggregate_tenant_ledger(
    db: Any,
    tenant_id: int,
    *,
    period: str = "7d",
) -> Dict[str, Any]:
    from database.models import AIUsageEvent  # noqa: PLC0415

    since = ledger_period_start(period)
    query = db.query(AIUsageEvent).filter(AIUsageEvent.tenant_id == tenant_id)
    if since is not None:
        query = query.filter(AIUsageEvent.created_at >= since)
    events = query.all()
    payload = _aggregate_events(events)
    payload["tenant_id"] = tenant_id
    payload["period"] = period
    return payload


def aggregate_platform_ledger(
    db: Any,
    *,
    period: str = "7d",
) -> Dict[str, Any]:
    from database.models import AIUsageEvent  # noqa: PLC0415

    since = ledger_period_start(period)
    query = db.query(AIUsageEvent)
    if since is not None:
        query = query.filter(AIUsageEvent.created_at >= since)
    events = query.all()
    payload = _aggregate_events(events)
    payload["period"] = period
    return payload
