"""
brain/intent/classifier.py
──────────────────────────
IntentClassifier — the single entry point consumed by MerchantBrain.

Phase 1 hybrid strategy:
  1. Run rules.match() synchronously (0 latency).
  2. If confidence >= RULES_ONLY_THRESHOLD: return immediately.
  3. Otherwise: run slot_extractor.extract_slots() (fast Haiku call).
     Merge the LLM's intent_hint into the result if the LLM's hint is
     more specific than the rules result.

This keeps the "happy path" (clear Arabic greeting / product ask / buy)
at zero extra latency while falling through to LLM only for ambiguous input.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..types import (
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
)
from . import rules
from . import slot_extractor as _slot_mod

logger = logging.getLogger("nahla.brain.classifier")

# Rules with confidence >= this bypass LLM slot extraction
RULES_ONLY_THRESHOLD = 0.85


class DefaultIntentClassifier:
    """
    Implements the IntentClassifier protocol.
    """

    async def classify(
        self,
        message: str,
        history: List[Dict[str, Any]],
        state: MerchantConversationState,
    ) -> Intent:
        # ── Layer 1: rules ─────────────────────────────────────────────────
        rule_intent = rules.match(message)

        if rule_intent and rule_intent.confidence >= RULES_ONLY_THRESHOLD:
            logger.debug(
                "[Classifier] rules-only | intent=%s conf=%.2f",
                rule_intent.name, rule_intent.confidence,
            )
            return rule_intent

        # ── Layer 2: LLM slot extraction ───────────────────────────────────
        slots = await _slot_mod.extract_slots(message, history)

        if not slots:
            # LLM unavailable or empty — fall back to rules or general
            if rule_intent:
                return rule_intent
            return Intent(
                name=INTENT_GENERAL,
                confidence=0.50,
                raw_message=message,
                extraction_method="rules",
            )

        # Merge: start from the rules intent (or general), then enrich slots
        base_intent = rule_intent.name if rule_intent else INTENT_GENERAL
        base_conf   = rule_intent.confidence if rule_intent else 0.50

        llm_hint = slots.pop("intent_hint", None) or INTENT_GENERAL

        # If the LLM disagrees with rules and it's a high-confidence rules
        # signal we keep the rules result; otherwise trust LLM
        if rule_intent and base_conf >= 0.75:
            resolved_name = base_intent
            resolved_conf = base_conf
            method        = "hybrid"
        else:
            resolved_name = llm_hint
            resolved_conf = 0.72   # moderate confidence for pure-LLM result
            method        = "llm"

        # Remove empty string values from slots
        clean_slots = {k: v for k, v in slots.items() if v not in ("", {}, None)}

        intent = Intent(
            name=resolved_name,
            confidence=resolved_conf,
            slots=clean_slots,
            raw_message=message,
            extraction_method=method,
        )
        logger.debug(
            "[Classifier] %s | intent=%s conf=%.2f slots=%s",
            method, resolved_name, resolved_conf, clean_slots,
        )
        return intent
