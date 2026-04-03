"""
ClaudeEngine
────────────
Calls the Anthropic Claude API with the full customer-aware system prompt.
Uses Claude's native tool use so that product/coupon suggestions are
returned as structured data (not parsed from freetext).

Uses the official `anthropic` Python SDK when available.
Falls back to raw httpx if the SDK is not installed.
Falls back to rule-based responses if no API key is configured.

Environment variables:
  ANTHROPIC_API_KEY   — primary (standard Anthropic env var)
  CLAUDE_API_KEY      — legacy alias (checked if ANTHROPIC_API_KEY is not set)
  CLAUDE_MODEL        — model ID (default: claude-opus-4-6)

Tools available to Claude:
  suggest_product   — recommend a specific product by ID
  suggest_coupon    — propose a discount percentage
  suggest_bundle    — recommend a product bundle
  propose_order     — initiate a draft order
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger("ai-orchestrator.engine")

# Support ANTHROPIC_API_KEY (standard) with CLAUDE_API_KEY as legacy fallback
CLAUDE_API_KEY  = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY", "")
CLAUDE_MODEL    = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")
CLAUDE_API_BASE = "https://api.anthropic.com/v1"

# Try to import the official Anthropic SDK
try:
    import anthropic as _anthropic
    _SDK_AVAILABLE = True
    logger.info("Anthropic SDK loaded — using SDK client")
except ImportError:
    _SDK_AVAILABLE = False
    import httpx
    logger.warning("anthropic SDK not installed — falling back to raw httpx")


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
            "Only suggest a coupon if the customer shows price sensitivity or hesitation."
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
    if not CLAUDE_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY configured — using rule-based fallback")
        return {
            "reply":   _rule_based_fallback(user_message),
            "actions": [],
            "model":   "rule-based",
        }

    if _SDK_AVAILABLE:
        return await _call_via_sdk(system_prompt, user_message)
    else:
        return await _call_via_httpx(system_prompt, user_message)


# ── SDK implementation ────────────────────────────────────────────────────────

async def _call_via_sdk(system_prompt: str, user_message: str) -> Dict[str, Any]:
    """Use the official anthropic SDK (preferred)."""
    client = _anthropic.AsyncAnthropic(api_key=CLAUDE_API_KEY)
    try:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system_prompt,
            tools=_TOOLS,
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": user_message}],
        )
    except _anthropic.AuthenticationError:
        logger.error("Claude SDK: authentication failed — check ANTHROPIC_API_KEY")
        return {
            "reply":   _rule_based_fallback(user_message),
            "actions": [],
            "model":   "rule-based-fallback",
        }
    except _anthropic.APIConnectionError as exc:
        logger.error(f"Claude SDK: connection error — {exc}")
        return {
            "reply":   _rule_based_fallback(user_message),
            "actions": [],
            "model":   "rule-based-fallback",
        }
    except _anthropic.APIError as exc:
        logger.error(f"Claude SDK: API error — {exc}")
        return {
            "reply":   _rule_based_fallback(user_message),
            "actions": [],
            "model":   "rule-based-fallback",
        }

    reply   = ""
    actions = []
    for block in response.content:
        if block.type == "text":
            reply = block.text
        elif block.type == "tool_use":
            actions.append({
                "type":    block.name,
                "payload": block.input,
            })

    return {"reply": reply, "actions": actions, "model": CLAUDE_MODEL}


# ── httpx fallback implementation ─────────────────────────────────────────────

async def _call_via_httpx(system_prompt: str, user_message: str) -> Dict[str, Any]:
    """Raw httpx fallback when the anthropic SDK is not installed."""
    headers = {
        "x-api-key":         CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 1024,
        "system":     system_prompt,
        "tools":      _TOOLS,
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": user_message}],
    }
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{CLAUDE_API_BASE}/messages",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.error(f"Claude httpx call failed: {exc}")
        return {
            "reply":   _rule_based_fallback(user_message),
            "actions": [],
            "model":   "rule-based-fallback",
        }

    reply   = ""
    actions = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            reply = block.get("text", "")
        elif block.get("type") == "tool_use":
            actions.append({
                "type":    block.get("name", ""),
                "payload": block.get("input", {}),
            })

    return {"reply": reply, "actions": actions, "model": CLAUDE_MODEL}


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
