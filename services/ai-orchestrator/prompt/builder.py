"""
services/ai-orchestrator/prompt/builder.py
──────────────────────────────────────────
Backward-compatible shim for the legacy orchestrator service.

Canonical source of truth:
  backend/modules/ai/prompts/builder.py

Routes inside services/ai-orchestrator still import this module, but the
implementation now delegates entirely to the canonical AI module so the legacy
service behaves as a compatibility shell.
"""

from modules.ai.prompts.builder import build_system_prompt  # noqa: F401
