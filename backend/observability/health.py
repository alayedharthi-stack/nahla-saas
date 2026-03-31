"""
HealthChecker
─────────────
Probes each subsystem and returns a structured status dict.
Used by GET /system/health.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict

import httpx

logger = logging.getLogger("nahla.health")

PROBE_TIMEOUT = 5.0


async def check_database(db) -> Dict[str, Any]:
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:120]}


async def check_orchestrator(orchestrator_url: str) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            resp = await client.get(f"{orchestrator_url}/health")
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "status": "ok",
                    "model": data.get("model"),
                    "pipeline_stages": len(data.get("pipeline_stages", [])),
                }
            return {"status": "degraded", "http_status": resp.status_code}
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)[:120]}


def check_moyasar(moyasar_cfg: dict) -> Dict[str, Any]:
    if moyasar_cfg.get("enabled") and moyasar_cfg.get("secret_key"):
        return {"status": "configured", "enabled": True}
    return {"status": "not_configured", "enabled": False}


def check_salla(tenant_id: int) -> Dict[str, Any]:
    try:
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
        from store_integration.registry import get_adapter
        adapter = get_adapter(tenant_id)
        if adapter:
            return {"status": "configured", "platform": adapter.platform}
        return {"status": "not_configured"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:80]}


def overall_status(components: Dict[str, Dict]) -> str:
    """Return 'ok', 'degraded', or 'error' based on component statuses."""
    statuses = [v.get("status", "unknown") for v in components.values()]
    if "error" in statuses:
        return "error"
    if any(s in ("unreachable", "degraded") for s in statuses):
        return "degraded"
    return "ok"
