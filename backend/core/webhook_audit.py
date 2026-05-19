"""
core/webhook_audit.py
─────────────────────
Lightweight aggregation of webhook signature verification outcomes.

Why a dedicated module
──────────────────────
Phase 1B ships every provider in "audit-mode" first: we run the signature
verifier, record the result, but do NOT change accept/reject behaviour.
That gives us 7+ days of telemetry to confirm signatures match before we
flip enforcement flags. To get useful telemetry we need:

* Daily counts per (provider, tenant, status) — so dashboards can show
  "% valid this week" per merchant.
* A small ring of recent invalid / missing samples — so an operator
  investigating a mismatch can see the actual header value, IP, and
  user-agent without grep-ing logs.

We intentionally do NOT persist every audited event as a ``WebhookEvent``
row: Meta inbound volume on a busy merchant can be thousands per day, and
Salla / Moyasar already persist their own events. Redis is the right
substrate — bounded memory, TTL eviction, multi-worker shared state.

When Redis is absent we degrade to per-process counters that survive only
until the worker restarts. That is fine for local dev and acceptable for
the audit window: production has Redis configured (verified by
``preflight_check.py``).

Public API
──────────
* ``record_result(result, *, tenant_id=None, request_meta=None)`` — record one
  verification outcome. Always non-blocking, never raises.
* ``get_summary(provider=None, *, days=7)``                       — return
  aggregate counts per (provider, status, day) for dashboards.
* ``get_recent_failures(provider=None, *, limit=50)``             — return
  the most recent invalid / missing samples for human inspection.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.audit import audit
from core.redis_client import get_redis
from core.webhook_security import SignatureStatus, VerificationResult

logger = logging.getLogger("nahla.webhook_audit")


_COUNTERS_TTL_SECONDS = 30 * 24 * 60 * 60   # 30 days of daily counters
_FAILURES_TTL_SECONDS = 7 * 24 * 60 * 60    # 7 days of recent samples
_FAILURES_RING_SIZE   = 200                 # bounded sample ring per provider


# ── In-process fallback (used only when REDIS_URL is empty) ────────────────────
# defaultdict[(provider, tenant, status, day_iso)] -> int
_local_counters: Dict[tuple, int] = defaultdict(int)
_local_failures: Dict[str, List[dict]] = defaultdict(list)


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _scrub_header_sample(value: Optional[str]) -> Optional[str]:
    """Return at most 16 chars of a signature header — enough to debug a
    typo / rotation mismatch, not enough to forge a signature."""
    if not value:
        return None
    s = str(value).strip()
    if len(s) <= 16:
        return s + "…"  # keep the trailing marker so logs are unambiguous
    return s[:16] + "…"


def record_result(
    result: VerificationResult,
    *,
    tenant_id: Optional[int] = None,
    request_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one verification outcome. Best-effort; never raises.

    ``request_meta`` may carry ``ip``, ``user_agent``, ``store_id``,
    ``signature_header_sample``. We scrub the signature header sample to
    16 chars so the audit log never preserves a forgeable secret.
    """
    provider = result.provider
    status_value = result.status.value
    day = _today_iso()
    tenant_label = str(tenant_id) if tenant_id is not None else "-"

    # Always emit a structured audit log line — that is our durable trail
    # in Railway / SIEM regardless of Redis availability.
    audit_payload: Dict[str, Any] = {
        "provider": provider,
        "tenant_id": tenant_label,
        "signature_status": status_value,
        "header_present": result.header_present,
        "detail": result.detail,
    }
    if request_meta:
        for k in ("ip", "user_agent", "store_id"):
            v = request_meta.get(k)
            if v:
                audit_payload[k] = str(v)[:120]
        sample = request_meta.get("signature_header_sample")
        if sample:
            audit_payload["sig_sample"] = _scrub_header_sample(sample)

    try:
        audit("webhook_signature_audit", **audit_payload)
    except Exception:  # noqa: silent-ok — audit is best-effort
        pass

    r = get_redis()
    if r is None:
        # Local fallback — useful for dev only.
        _local_counters[(provider, tenant_label, status_value, day)] += 1
        if result.status in (SignatureStatus.INVALID, SignatureStatus.MISSING):
            ring = _local_failures[provider]
            ring.append(_failure_sample(result, tenant_label, request_meta))
            if len(ring) > _FAILURES_RING_SIZE:
                del ring[: len(ring) - _FAILURES_RING_SIZE]
        return

    try:
        # Daily counters: hash keyed by day, fields by (tenant, status).
        counter_key = f"webhook:audit:counters:{provider}:{day}"
        field = f"{tenant_label}:{status_value}"
        pipe = r.pipeline()
        pipe.hincrby(counter_key, field, 1)
        pipe.expire(counter_key, _COUNTERS_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 — never break webhooks on telemetry failures
        logger.warning("[webhook_audit] counter HINCRBY failed: %s", exc)

    if result.status not in (SignatureStatus.INVALID, SignatureStatus.MISSING):
        return

    # Recent-failure ring (LPUSH + LTRIM) for operator inspection.
    sample_payload = _failure_sample(result, tenant_label, request_meta)
    try:
        ring_key = f"webhook:audit:recent_failures:{provider}"
        pipe = r.pipeline()
        pipe.lpush(ring_key, json.dumps(sample_payload, default=str))
        pipe.ltrim(ring_key, 0, _FAILURES_RING_SIZE - 1)
        pipe.expire(ring_key, _FAILURES_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 — recent-failure ring is best-effort
        logger.warning("[webhook_audit] failure ring LPUSH failed: %s", exc)


def _failure_sample(
    result: VerificationResult,
    tenant_label: str,
    request_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    meta = dict(request_meta or {})
    sample = meta.get("signature_header_sample")
    return {
        "ts": int(time.time()),
        "provider": result.provider,
        "tenant_id": tenant_label,
        "status": result.status.value,
        "detail": result.detail,
        "header_present": result.header_present,
        "ip": meta.get("ip"),
        "user_agent": (meta.get("user_agent") or "")[:120],
        "store_id": meta.get("store_id"),
        "sig_sample": _scrub_header_sample(sample),
    }


# ── Read APIs (for the operator dashboard) ─────────────────────────────────────


def get_summary(
    provider: Optional[str] = None,
    *,
    days: int = 7,
) -> Dict[str, Any]:
    """Return aggregated counts for the last ``days`` days.

    Shape::

        {
          "providers": {
            "salla": {
              "totals": {"valid": 1234, "invalid": 0, "missing": 12, "secret_not_configured": 0},
              "by_day": {
                "2026-05-12": {"valid": 200, ...},
                "2026-05-13": {"valid": 220, ...},
              },
              "tenants": {
                "5":  {"valid": 1000, "invalid": 0, "missing": 12, "secret_not_configured": 0},
                "12": {"valid": 234,  "invalid": 0, "missing": 0,  "secret_not_configured": 0},
              }
            },
            ...
          },
          "redis_available": true,
          "since": "2026-05-12",
          "until": "2026-05-18"
        }
    """
    days = max(1, min(int(days), 30))
    today = datetime.now(timezone.utc).date()
    day_strs = [(today.fromordinal(today.toordinal() - i)).isoformat() for i in range(days)]
    day_strs.reverse()

    providers_filter = [provider] if provider else _ALL_PROVIDERS
    out: Dict[str, Any] = {
        "providers": {},
        "since": day_strs[0],
        "until": day_strs[-1],
        "redis_available": False,
    }

    r = get_redis()
    if r is not None:
        out["redis_available"] = True
        for prov in providers_filter:
            agg = _empty_agg()
            for day in day_strs:
                key = f"webhook:audit:counters:{prov}:{day}"
                try:
                    fields = r.hgetall(key) or {}
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[webhook_audit] HGETALL failed for %s: %s", key, exc)
                    fields = {}
                _merge_fields(agg, day, fields)
            out["providers"][prov] = agg
        return out

    # Local fallback aggregation.
    for prov in providers_filter:
        agg = _empty_agg()
        for day in day_strs:
            for (cprov, tenant, status, cday), count in _local_counters.items():
                if cprov != prov or cday != day:
                    continue
                _bump(agg, day, tenant, status, count)
        out["providers"][prov] = agg
    return out


def record_replay(
    provider: str,
    *,
    tenant_id: Optional[int] = None,
    request_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a replay-detected event in the same audit ring as bad signatures.

    Replay rejections are a legitimate denial path even when the signature
    is valid (an attacker re-using a captured signed body), so they share
    the operator dashboard with INVALID / MISSING failures. The status
    label here is the synthetic ``"replay"`` (NOT a member of
    ``SignatureStatus``) so the dashboard can chart them as a separate
    series without polluting the signature counters.
    """
    tenant_label = str(tenant_id) if tenant_id is not None else "-"
    day = _today_iso()
    audit_payload: Dict[str, Any] = {
        "provider": provider,
        "tenant_id": tenant_label,
        "signature_status": "replay",
    }
    if request_meta:
        for k in ("ip", "user_agent", "store_id"):
            v = request_meta.get(k)
            if v:
                audit_payload[k] = str(v)[:120]
    try:
        audit("webhook_signature_audit", **audit_payload)
    except Exception:  # noqa: silent-ok — audit is best-effort
        pass

    sample: Dict[str, Any] = {
        "ts": int(time.time()),
        "provider": provider,
        "tenant_id": tenant_label,
        "status": "replay",
        "detail": "duplicate body hash within replay TTL",
        "header_present": False,
        "ip": (request_meta or {}).get("ip"),
        "user_agent": ((request_meta or {}).get("user_agent") or "")[:120],
        "store_id": (request_meta or {}).get("store_id"),
        "sig_sample": None,
    }

    r = get_redis()
    if r is None:
        _local_counters[(provider, tenant_label, "replay", day)] += 1
        ring = _local_failures[provider]
        ring.append(sample)
        if len(ring) > _FAILURES_RING_SIZE:
            del ring[: len(ring) - _FAILURES_RING_SIZE]
        return

    try:
        counter_key = f"webhook:audit:counters:{provider}:{day}"
        ring_key = f"webhook:audit:recent_failures:{provider}"
        pipe = r.pipeline()
        pipe.hincrby(counter_key, f"{tenant_label}:replay", 1)
        pipe.expire(counter_key, _COUNTERS_TTL_SECONDS)
        pipe.lpush(ring_key, json.dumps(sample, default=str))
        pipe.ltrim(ring_key, 0, _FAILURES_RING_SIZE - 1)
        pipe.expire(ring_key, _FAILURES_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[webhook_audit] replay record failed: %s", exc)


def get_recent_failures(
    provider: Optional[str] = None,
    *,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return the most recent invalid / missing samples.

    The ring is bounded to ``_FAILURES_RING_SIZE`` per provider; this just
    decodes the head of each ring up to ``limit`` total.
    """
    limit = max(1, min(int(limit), _FAILURES_RING_SIZE))
    providers_filter = [provider] if provider else _ALL_PROVIDERS
    rows: List[Dict[str, Any]] = []

    r = get_redis()
    if r is not None:
        for prov in providers_filter:
            try:
                items = r.lrange(f"webhook:audit:recent_failures:{prov}", 0, limit - 1) or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("[webhook_audit] LRANGE failed for %s: %s", prov, exc)
                continue
            for raw in items:
                try:
                    rows.append(json.loads(raw))
                except Exception:  # noqa: silent-ok — drop malformed ring entries
                    continue
    else:
        for prov in providers_filter:
            rows.extend(_local_failures.get(prov, [])[-limit:])

    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[:limit]


# ── Helpers ────────────────────────────────────────────────────────────────────

_ALL_PROVIDERS: tuple[str, ...] = (
    "meta",
    "salla",
    "salla_oauth",
    "salla_app_settings",
    "zid",
    "moyasar",
    "moyasar_subscription",
    "hyperpay",
    "d360",
)


_STATUS_KEYS: tuple[str, ...] = tuple(s.value for s in SignatureStatus)


def _empty_agg() -> Dict[str, Any]:
    return {
        "totals": {k: 0 for k in _STATUS_KEYS},
        "by_day": {},
        "tenants": {},
    }


def _bump(agg: Dict[str, Any], day: str, tenant: str, status: str, count: int) -> None:
    if status not in _STATUS_KEYS:
        return
    agg["totals"][status] += count
    by_day = agg["by_day"].setdefault(day, {k: 0 for k in _STATUS_KEYS})
    by_day[status] += count
    by_tenant = agg["tenants"].setdefault(tenant, {k: 0 for k in _STATUS_KEYS})
    by_tenant[status] += count


def _merge_fields(agg: Dict[str, Any], day: str, fields: Dict[str, str]) -> None:
    """``fields`` is the result of HGETALL — keys like ``"5:valid"`` → count."""
    for raw_key, raw_val in fields.items():
        try:
            tenant, status = str(raw_key).rsplit(":", 1)
            count = int(raw_val)
        except Exception:  # noqa: silent-ok — drop malformed counter keys
            continue
        _bump(agg, day, tenant, status, count)
