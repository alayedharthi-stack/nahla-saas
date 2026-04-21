"""
modules/ai/routing
──────────────────
Top-level conversation routing primitives that sit ABOVE the Merchant
Brain / legacy-AI split. This package owns the question "which mode owns
the conversation right now?" and never mutates the engine, provider
selection, or prompt builders downstream.
"""
