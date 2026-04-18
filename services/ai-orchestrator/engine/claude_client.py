"""
ClaudeEngine
────────────
Compatibility wrapper for the legacy orchestrator service.

This file no longer owns Anthropic execution logic directly.
Instead it delegates to the canonical Anthropic provider implementation in:
  backend/modules/ai/orchestrator/providers/anthropic_provider.py

The HTTP contract of services/ai-orchestrator remains unchanged:
- returns reply text
- returns structured action proposals from Claude tool use
- falls back to the same bilingual rule-based replies when unavailable
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List

logger = logging.getLogger("ai-orchestrator.engine")

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
for _p in (_REPO_ROOT, _BACKEND_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.orchestrator.providers.registry import get_provider

CLAUDE_API_KEY  = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY", "")
CLAUDE_MODEL    = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")


# ── Tool definitions ──────────────────────────────────────────────────────────
_TOOLS: List[Dict] = [
    {
        "name": "suggest_product",
        "description": (
            "Recommend a specific product to the customer. "
            "Use the product's numeric database ID. "
            "Only call this tool for products listed in the system prompt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "integer",
                    "description": "The numeric product ID from the AVAILABLE PRODUCTS list",
                },
                "product_title": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "Why this product suits this specific customer",
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Product categories (used by policy guard)",
                },
            },
            "required": ["product_id", "product_title", "reason"],
        },
    },
    {
        "name": "suggest_coupon",
        "description": (
            "Propose a discount for the customer. "
            "The discount percentage must fit within the store's coupon policy. "
            "Only suggest a coupon if ALL of the following conditions are met: "
            "(1) actual products are listed in the AVAILABLE PRODUCTS section of your context, "
            "(2) the customer shows clear price sensitivity or hesitation about a specific product. "
            "NEVER suggest a coupon when the product catalogue is empty or unavailable — "
            "there is nothing to apply a discount to."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "discount_pct": {
                    "type": "integer",
                    "description": "Discount percentage (e.g. 10 for 10% off)",
                },
                "coupon_type": {
                    "type": "string",
                    "enum": ["percentage", "fixed"],
                    "description": "Type of discount",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this discount is appropriate for this customer",
                },
            },
            "required": ["discount_pct", "reason"],
        },
    },
    {
        "name": "suggest_bundle",
        "description": (
            "Recommend a bundle of complementary products. "
            "Use only products listed in the system prompt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of product IDs that form the bundle",
                },
                "bundle_name": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["product_ids", "reason"],
        },
    },
    {
        "name": "propose_order",
        "description": (
            "Propose creating a draft order for the customer when they are ready to buy. "
            "Only call this when the customer has clearly expressed intent to purchase."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "reason": {"type": "string"},
            },
            "required": ["product_ids", "reason"],
        },
    },
]


# ── Main entry point ──────────────────────────────────────────────────────────

async def call_claude(
    system_prompt: str,
    user_message: str,
) -> Dict[str, Any]:
    """
    Call Claude and return:
    {
        "reply":   str,            # conversational WhatsApp reply
        "actions": [               # tool-use blocks — validated by PolicyGuard
            {"type": str, "payload": dict}
        ],
        "model":   str,
    }
    """
    provider = get_provider("anthropic")
    if provider is None or not hasattr(provider, "call_with_tools"):
        logger.warning("Anthropic provider unavailable in registry — using rule-based fallback")
        return {
            "reply":   _rule_based_fallback(user_message),
            "actions": [],
            "model":   "rule-based",
        }

    if not CLAUDE_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY configured — using rule-based fallback")
        return {
            "reply":   _rule_based_fallback(user_message),
            "actions": [],
            "model":   "rule-based",
        }
    try:
        raw = provider.call_with_tools(
            message=user_message,
            prompt=system_prompt,
            tools=_TOOLS,
            tool_choice="auto",
        )
        reply = str(raw.get("reply_text", ""))
        actions = raw.get("actions", []) or []
        model = str(raw.get("model", CLAUDE_MODEL))
        if reply or actions:
            return {"reply": reply, "actions": actions, "model": model}
        logger.warning("Canonical Anthropic provider returned empty reply/actions — using rule-based fallback")
    except Exception as exc:
        logger.error("Canonical Anthropic provider call failed: %s", exc)

    return {
        "reply":   _rule_based_fallback(user_message),
        "actions": [],
        "model":   "rule-based-fallback",
    }


# ── Bilingual rule-based fallback ─────────────────────────────────────────────

def _rule_based_fallback(message: str) -> str:
    text = message.strip().lower()

    greetings = {"hi", "hello", "hey", "مرحبا", "أهلا", "اهلا", "سلام", "هلا"}
    if any(g in text for g in greetings):
        return "مرحباً! 👋 كيف أقدر أساعدك اليوم؟\n\nHello! 👋 How can I help you today?"

    if any(w in text for w in ("منتج", "product", "price", "سعر", "catalog")):
        return "يسعدني مساعدتك! 🛍️ أخبرني بما تبحث عنه.\n\nHappy to help! 🛍️ Tell me what you're looking for."

    if any(w in text for w in ("order", "طلب", "اطلب", "buy", "purchase")):
        return (
            "ممتاز! 🎉 لإتمام الطلب أحتاج: اسمك، عنوان التوصيل، طريقة الدفع.\n\n"
            "Great! 🎉 To complete your order I need: your name, delivery address, payment method."
        )

    if any(w in text for w in ("coupon", "discount", "كوبون", "خصم", "offer")):
        return "لدينا عروض حصرية! 🏷️ تواصل معنا لمعرفة الكوبونات المتاحة.\n\nWe have exclusive offers! 🏷️ Ask us about available coupon codes."

    if any(w in text for w in ("delivery", "توصيل", "شحن", "shipping")):
        return "نوصل لجميع مناطق المملكة 🚚 خلال 1-3 أيام عمل.\n\nWe deliver across Saudi Arabia 🚚 in 1-3 business days."

    if any(w in text for w in ("human", "agent", "موظف", "تكلم", "speak")):
        return "بالطبع! سأحولك لأحد موظفينا. ⏳\n\nOf course! Connecting you with a team member now. ⏳"

    return "شكراً لتواصلك معنا! 😊 كيف أقدر أساعدك؟\n\nThanks for reaching out! 😊 How can I assist you?"
