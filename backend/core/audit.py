"""
core/audit.py
─────────────
Structured audit logging helper shared across all modules.
"""
import logging

_audit_logger = logging.getLogger("nahla.audit")


def audit(event: str, **ctx) -> None:
    """Emit a structured audit log line to the nahla.audit logger."""
    parts = " ".join(f"{k}={v}" for k, v in ctx.items())
    _audit_logger.info("AUDIT event=%s %s", event, parts)
