"""
template_ai/generator.py — backward-compatible shim
─────────────────────────────────────────────────────
Canonical source has moved to:
  backend/modules/ai/templates/generator.py

This file re-exports everything from the new location so existing
import paths (from template_ai.generator import ...) keep working
without modification during the migration period.
"""
from modules.ai.templates.generator import (  # noqa: F401
    SUPPORTED_OBJECTIVES,
    generate_template_draft,
)
