"""
catalog/catalog_intelligence_telemetry.py
─────────────────────────────────────────
Catalog Intelligence Phase 6 — grep-friendly runtime telemetry.

Filter example::

    railway logs | rg '\\[CATALOG_INTELLIGENCE\\]'
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("nahla.catalog_intelligence.telemetry")


def _fmt_value(val: Any) -> str:
    if val is None:
        return "-"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    text = str(val).replace("\n", " ").replace("=", "≡")
    if len(text) > 120:
        return text[:117] + "..."
    return text


def emit_catalog_intelligence_event(
    event: str,
    *,
    tenant_id: Optional[int] = None,
    **fields: Any,
) -> None:
    """Emit a single structured log line. Never raises."""
    try:
        parts = [f"event={event}"]
        if tenant_id is not None:
            parts.append(f"tenant={tenant_id}")
        for key, val in fields.items():
            if val is None or val == "":
                continue
            parts.append(f"{key}={_fmt_value(val)}")
        logger.info("[CATALOG_INTELLIGENCE] %s", " ".join(parts))
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_INTELLIGENCE] emit_failed event=%s", event)


__all__ = ["emit_catalog_intelligence_event"]
