"""
brain/intent/slot_extractor.py
───────────────────────────────
Uses a tiny Claude Haiku call (< 200 tokens) to extract semantic slots
from a message:

    product_query  — the search term to look up in the catalog
    price_range    — {"min": float, "max": float}  (either key may be absent)
    quantity       — int  (how many items the customer wants)
    order_id       — string  (if the customer references an existing order)
    intent_hint    — the extractor's own best-guess at intent name

When the Anthropic key is missing or the call fails the extractor returns
an empty dict (graceful degradation — the rules layer result is used as-is).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List

from services.address_resolution import extract_address_signals

logger = logging.getLogger("nahla.brain.slot_extractor")

_SYSTEM = """أنت مُستخرِج معلومات دقيق. أجِب دائمًا بـ JSON فقط، بدون أي نص إضافي.

المهمة: استخرج الحقول التالية من رسالة المستخدم:
- product_query: string — ماذا يبحث المستخدم عن منتج؟ (فارغ إن لم يوجد)
- price_range: {min: number, max: number} — النطاق السعري إن ذُكر (فارغ إن لم يوجد)
- quantity: number — الكمية المطلوبة (افتراضي 1 إن لم يُذكر)
- order_id: string — رقم الطلب إن ذُكر (فارغ إن لم يوجد)
- customer_name: string — اسم العميل الكامل إن ذُكر
- customer_first_name: string — الاسم الأول إن أمكن
- customer_last_name: string — اسم العائلة إن أمكن
- customer_email: string — البريد الإلكتروني إن ذُكر
- city: string — المدينة إن ذُكرت
- short_address_code: string — الرمز الوطني المختصر إن وُجد مثل ABCD1234
- google_maps_url: string — رابط خرائط Google إن وُجد
- address_line: string — أي وصف عنوان حر إن وُجد
- street: string — الشارع إن ذُكر
- district: string — الحي إن ذُكر
- postal_code: string — الرمز البريدي إن ذُكر
- building_number: string — رقم المبنى إن ذُكر
- additional_number: string — الرقم الإضافي إن ذُكر
- latitude: number — خط العرض إن وُجد
- longitude: number — خط الطول إن وُجد
- intent_hint: string — أفضل تخمين للنية: greeting|who_are_you|ask_product|ask_price|start_order|pay_now|ask_shipping|ask_store_info|ask_owner_contact|hesitation|talk_to_human|track_order|general

أجِب بـ JSON صالح فقط."""

_EXTRACT_SCHEMA = {
    "product_query": "",
    "price_range": {},
    "quantity": 1,
    "order_id": "",
    "customer_name": "",
    "customer_first_name": "",
    "customer_last_name": "",
    "customer_email": "",
    "city": "",
    "short_address_code": "",
    "google_maps_url": "",
    "address_line": "",
    "street": "",
    "district": "",
    "postal_code": "",
    "building_number": "",
    "additional_number": "",
    "latitude": None,
    "longitude": None,
    "intent_hint": "general",
}


async def extract_slots(
    message: str,
    history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Call Claude Haiku to extract slots.
    Returns a dict matching _EXTRACT_SCHEMA keys (may be partial).
    Falls back to empty dict on any error.
    """
    deterministic = _extract_deterministic_slots(message)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return deterministic

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)

        # Summarise last 2 turns for context (no full history to keep tokens low)
        context_turns = history[-4:] if history else []
        history_lines = []
        for turn in context_turns:
            direction = turn.get("direction", "in")
            body = turn.get("body", "")
            prefix = "عميل" if direction == "in" else "ذكاء"
            history_lines.append(f"{prefix}: {body}")
        history_text = "\n".join(history_lines)

        user_content = f"السياق السابق:\n{history_text}\n\nرسالة المستخدم الحالية:\n{message}"

        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )

        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        slots = json.loads(raw)

        merged = dict(slots or {})
        for key, value in deterministic.items():
            if merged.get(key) in ("", {}, None):
                merged[key] = value

        logger.debug("[SlotExtractor] extracted=%s", merged)
        return merged

    except json.JSONDecodeError as exc:
        logger.warning("[SlotExtractor] JSON parse error: %s", exc)
        return deterministic
    except Exception as exc:
        logger.warning("[SlotExtractor] extraction failed: %s", exc)
        return deterministic


def _extract_deterministic_slots(message: str) -> Dict[str, Any]:
    text = message or ""
    signals = extract_address_signals(text)
    slots: Dict[str, Any] = {}

    if signals.get("short_address_code"):
        slots["short_address_code"] = signals["short_address_code"]
    if signals.get("google_maps_url"):
        slots["google_maps_url"] = signals["google_maps_url"]
    if signals.get("latitude") is not None:
        slots["latitude"] = signals["latitude"]
    if signals.get("longitude") is not None:
        slots["longitude"] = signals["longitude"]

    email_match = re.search(r"\b[\w.\-+]+@[\w.\-]+\.\w+\b", text)
    if email_match:
        slots["customer_email"] = email_match.group(0)

    return slots
