"""
template_ai/policy_validator.py — backward-compatible shim
───────────────────────────────────────────────────────────
Canonical source has moved to:
  backend/modules/ai/templates/policy_validator.py

This file re-exports everything from the new location so existing
import paths (from template_ai.policy_validator import ...) keep working
without modification during the migration period.
"""
from modules.ai.templates.policy_validator import (  # noqa: F401
    ValidationResult,
    validate_draft,
)
