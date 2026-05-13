"""Post-processing layer for the AI reply pipeline.

Modules here run *after* the LLM produced its text and *before* the
text reaches the WhatsApp wire. They are intentionally rule-based,
deterministic, and conservative — no LLM rewriting, no paraphrasing,
no semantic modification.
"""
