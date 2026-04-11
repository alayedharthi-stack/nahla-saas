"""
backend/modules/ai/orchestrator/prompt_versioning.py
──────────────────────────────────────────────────────
Lightweight prompt metadata for AI reply traceability.

Public surface:
  fingerprint_prompt(prompt_text)  → PromptMeta dataclass

PromptMeta fields:
  prompt_name    : str   — logical name of the prompt builder in use
  prompt_version : str   — semver-style version, bumped when prompt logic changes
  prompt_hash    : str   — deterministic 8-char hex fingerprint of the exact text sent
  builder_source : str   — dotted module path of the prompt builder function

Design:
  - Pure function — no side effects, no I/O, no network.
  - Hash is SHA-256 of the UTF-8 encoded prompt text, truncated to 8 hex chars.
    It is deterministic: same prompt text always → same hash.
  - prompt_version is a manual constant tied to the prompt builder implementation.
    It must be incremented (or bumped in CI) whenever prompts.builder logic changes.
  - No DB writes. No external calls. Log-based and metadata-only.

When to bump PROMPT_VERSION:
  Increment the minor version when prompt section order, wording, or behaviour
  changes significantly.  Increment patch for cosmetic/whitespace fixes.
  This is the single source of truth for the active prompt version.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# ── Prompt version declaration ─────────────────────────────────────────────────
# Update this whenever the prompt builder logic changes materially.
# Format: MAJOR.MINOR.PATCH
PROMPT_VERSION: str = "1.0.0"

# Canonical name and source of the currently active prompt builder
PROMPT_NAME: str = "nahla-whatsapp-sales"
PROMPT_BUILDER_SOURCE: str = "modules.ai.prompts.builder.build_system_prompt"

_HASH_LENGTH: int = 8   # hex chars → 4 bytes → 32 bits; sufficient for traceability


@dataclass(frozen=True)
class PromptMeta:
    """
    Immutable metadata record for one prompt that was sent to an AI provider.

    Attached to payload.metadata["prompt"] on the success path.
    Emitted as a [prompt-meta] log entry alongside [cost-est] and [chain-obs].
    """
    prompt_name:    str
    prompt_version: str
    prompt_hash:    str   # 8-char hex fingerprint of the exact prompt text
    builder_source: str


def fingerprint_prompt(prompt_text: str) -> PromptMeta:
    """
    Compute a PromptMeta record for the given prompt text.

    Parameters
    ----------
    prompt_text : the exact system prompt string that was / will be sent to
                  the LLM provider.

    Returns
    -------
    PromptMeta with a deterministic 8-char hex hash of the prompt content.

    Never raises.
    """
    digest = hashlib.sha256(prompt_text.encode("utf-8", errors="replace")).hexdigest()
    short_hash = digest[:_HASH_LENGTH]

    return PromptMeta(
        prompt_name=PROMPT_NAME,
        prompt_version=PROMPT_VERSION,
        prompt_hash=short_hash,
        builder_source=PROMPT_BUILDER_SOURCE,
    )
