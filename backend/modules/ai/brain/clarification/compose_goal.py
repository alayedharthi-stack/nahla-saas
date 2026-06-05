"""
clarification/compose_goal.py
─────────────────────────────
Behavioral compose goals for contextual clarify (generative path).

Lives outside persona_expression.py — pipeline wires goals; LLM owns wording.
"""
from __future__ import annotations


def compose_contextual_clarify_goal(*, ambiguity_class: str) -> str:
    """
    Response goal for generative contextual clarification.

    Behavioral guidance only — no canned Arabic customer text.
    """
    _cls = str(ambiguity_class or "unknown").strip() or "unknown"
    return (
        f"contextual_clarify — ambiguity_class={_cls}. "
        "The customer message lacks information required to proceed. "
        "Compose ONE short natural Saudi Arabic WhatsApp clarification question "
        "using conversation context and the clarification_evidence block in "
        "this prompt — ask about the specific missing datum for this class, "
        "not a generic attribute/spec checklist. "
        "Preserve Nahla's warm conversational persona; do not switch to "
        "system, workflow, support-desk, or template-engine voice. "
        "Do NOT enumerate store capabilities or pitch unrelated products. "
        "Do NOT use [PRODUCT:…] or [MEDIA_KEY:…]. "
        "Maximum 1–3 short lines."
    )


__all__ = ["compose_contextual_clarify_goal"]
