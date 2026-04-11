"""
template_ai/health_evaluator.py — backward-compatible shim
────────────────────────────────────────────────────────────
Canonical source has moved to:
  backend/modules/ai/templates/health_evaluator.py

This file re-exports everything from the new location so existing
import paths (from template_ai.health_evaluator import ...) keep working
without modification during the migration period.
"""
from modules.ai.templates.health_evaluator import (  # noqa: F401
    UNUSED_WARNING_DAYS,
    UNUSED_ARCHIVE_DAYS,
    OLD_TEMPLATE_DAYS,
    evaluate_templates,
    health_summary,
)
