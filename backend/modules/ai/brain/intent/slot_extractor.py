"""
brain/intent/slot_extractor.py
───────────────────────────────
Uses a tiny Claude Haiku call to extract semantic slots from a message.

Slots extracted
───────────────
  product_query        — the search term to look up in the catalog
  price_range          — {"min": float, "max": float}
  quantity             — int (how many items the customer wants)
  order_id             — string (if the customer references an existing order)
  customer_name        — full name
  customer_first_name  — first name
  customer_last_name   — family name
  customer_email       — email address
  city                 — city name
  short_address_code   — Saudi national address code (e.g. RIYD2342)
  google_maps_url      — Google Maps share/short link
  address_line         — free-text address
  street / district / postal_code / building_number / additional_number
  latitude / longitude
  intent_hint          — LLM best-guess intent label

Design goals
────────────
  1. Compact output — LLM only returns fields with actual values so the
     JSON stays small (≈ 60–200 tokens even for complex multi-field
     messages). This is the main fix for the P0 truncation bug.
  2. max_tokens=350 — enough for the densest possible compact response,
     up from the former 200 which cut off mid-JSON on complex messages.
  3. Truncation repair — if the LLM is still cut short, _repair_json()
     attempts to close the broken JSON before giving up.
  4. Minimal history — last 2 turns, capped at 80 chars each, so history
     never cannibalises the output budget.
  5. Graceful degradation — any failure returns deterministic slots only
     (address code, email, coordinates from regex). No exception bubbles.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List

from services.address_resolution import extract_address_signals

logger = logging.getLogger("nahla.brain.slot_extractor")

# ── System prompt ─────────────────────────────────────────────────────────────
# KEY CHANGE: instruct the model to return ONLY non-empty fields (compact mode).
# This keeps the JSON small even when the customer writes all their info in one
# message ("اسمي علي، من الرياض، الحي XYZW1234") — previously that would
# overflow the 200-token limit and produce a broken JSON.
_SYSTEM = """أنت مُستخرِج معلومات دقيق. أجِب دائمًا بـ JSON صالح فقط، بدون أي نص إضافي.

المهمة: استخرج من رسالة المستخدم الحقول التالية — رسالة واحدة قد تحتوي حقولاً متعددة في آنٍ واحد، استخرجها جميعًا:

- product_query: string — المنتج المطلوب (فارغ إن لم يُذكر)
- price_range: {min: number, max: number} — النطاق السعري (فارغ إن لم يُذكر)
- quantity: number — الكمية (افتراضي 1)
- order_id: string — رقم الطلب إن ذُكر
- customer_name: string — الاسم الكامل
- customer_first_name: string — الاسم الأول فقط
- customer_last_name: string — اسم العائلة فقط
- customer_email: string — البريد الإلكتروني
- city: string — المدينة
- short_address_code: string — الرمز الوطني المختصر مثل ABCD1234
- google_maps_url: string — رابط خرائط Google
- address_line: string — وصف عنوان حر
- street: string — الشارع
- district: string — الحي
- postal_code: string — الرمز البريدي
- building_number: string — رقم المبنى
- additional_number: string — الرقم الإضافي
- latitude: number — خط العرض
- longitude: number — خط الطول
- intent_hint: string — أفضل تخمين للنية: greeting|who_are_you|ask_product|ask_price|start_order|pay_now|ask_shipping|ask_store_info|ask_owner_contact|ask_payment_info|hesitation|talk_to_human|track_order|general

⚠️ قاعدة مهمة: أرجع فقط الحقول التي لها قيمة فعلية. لا تُضمِّن الحقول الفارغة أو الافتراضية (مثل "" أو {} أو null أو 1 للكمية إن لم تُذكر).
استثناء: أرجع دائمًا intent_hint حتى لو كان "general".

أجِب بـ JSON مضغوط فقط."""

# Default schema — used to merge deterministic + LLM slots, and as fallback
_EXTRACT_SCHEMA: Dict[str, Any] = {
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
    """Call Claude Haiku to extract slots.

    Returns a dict with non-empty values from _EXTRACT_SCHEMA.
    Falls back to deterministic-only on any error.
    """
    deterministic = _extract_deterministic_slots(message)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return deterministic

    try:
        import asyncio
        import anthropic  # noqa: PLC0415

        # 12-second hard timeout (SDK + asyncio guard)
        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=12.0)

        # Last 2 turns only, each capped at 80 chars to save output budget
        context_turns = history[-4:] if history else []
        history_lines = []
        for turn in context_turns[-2:]:
            direction = turn.get("direction", "in")
            body = str(turn.get("body", ""))[:80]
            prefix = "عميل" if direction == "in" else "ذكاء"
            history_lines.append(f"{prefix}: {body}")
        history_text = "\n".join(history_lines)

        if history_text:
            user_content = f"السياق السابق:\n{history_text}\n\nرسالة المستخدم:\n{message}"
        else:
            user_content = f"رسالة المستخدم:\n{message}"

        _slot_model = os.environ.get("ANTHROPIC_SLOT_MODEL") or "claude-3-5-haiku-20241022"

        from modules.ai.orchestrator.llm_cost_audit import emit_llm_cost_audit  # noqa: PLC0415

        emit_llm_cost_audit(
            model=_slot_model,
            provider="anthropic",
            messages_count=1,
            system_chars=len(_SYSTEM),
            messages_chars=len(user_content),
            total_prompt_chars=len(_SYSTEM) + len(user_content),
            estimated_input_tokens=(len(_SYSTEM) + len(user_content)) // 4,
            reason="brain.intent.slot_extractor",
        )

        # max_tokens raised from 200 → 350.
        # With compact output, 350 is ample for even the densest response
        # (≈ 15 filled fields × ~20 tokens each ≈ 300 tokens).
        response = await asyncio.wait_for(
            client.messages.create(
                model=_slot_model,
                max_tokens=350,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user_content}],
            ),
            timeout=12.0,
        )

        raw = response.content[0].text.strip()

        # Strip markdown code fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        # Attempt normal parse; fall back to repair on failure
        try:
            slots = json.loads(raw)
        except json.JSONDecodeError:
            repaired = _repair_json(raw)
            if repaired is not None:
                logger.info(
                    "[SlotExtractor] repaired truncated JSON (original_len=%d repaired_len=%d)",
                    len(raw), len(str(repaired)),
                )
                slots = repaired
            else:
                logger.warning("[SlotExtractor] JSON parse failed, raw=%r", raw[:120])
                return deterministic

        # Merge strategy:
        #   - Address-signal keys (SPL code, Maps URL, coords, email) are found
        #     by deterministic regex which is more reliable than LLM pattern
        #     matching — these ALWAYS override whatever the LLM returned.
        #   - All other keys: deterministic fills in only if the LLM left them empty.
        _REGEX_PRIORITY = {"short_address_code", "google_maps_url", "latitude", "longitude", "customer_email"}

        merged: Dict[str, Any] = dict(slots or {})
        for key, value in deterministic.items():
            if key in _REGEX_PRIORITY:
                merged[key] = value          # unconditional override
            elif merged.get(key) in ("", {}, None):
                merged[key] = value          # fill empty only

        # Strip residual empty / default values before returning
        merged = {k: v for k, v in merged.items() if v not in ("", {}, None)}

        logger.debug("[SlotExtractor] extracted=%s", merged)
        return merged

    except TimeoutError as exc:
        logger.warning("[SlotExtractor] timeout (%s) — falling back to deterministic", exc)
        return deterministic
    except Exception as exc:
        logger.warning(
            "[SlotExtractor] extraction failed (type=%s): %s",
            type(exc).__name__, exc,
        )
        return deterministic


# ── Truncation repair ─────────────────────────────────────────────────────────

def _repair_json(raw: str) -> Dict[str, Any] | None:
    """Try to salvage a partially-truncated JSON object.

    Strategy:
      1. Find the last fully-closed key:value pair.
      2. Close the object with `}` and attempt to parse.
      3. Return None if it still fails.

    This handles the common case where max_tokens cut the JSON mid-value
    (e.g. `{"customer_name": "محمد", "city": "الري`).
    """
    if not raw.startswith("{"):
        return None

    # Try progressively shorter substrings, stopping at the last comma or quote
    # before the cut. We scan backwards for a safe truncation point.
    attempt = raw.rstrip()

    # Common patterns that indicate a clean end of a value
    for pattern in (
        r',\s*"[^"]+"\s*:\s*"[^"]*"',   # last complete string value
        r',\s*"[^"]+"\s*:\s*\d+(?:\.\d+)?',  # last complete number value
        r',\s*"[^"]+"\s*:\s*(?:true|false|null)',  # last complete bool/null
    ):
        match = None
        for m in re.finditer(pattern, attempt):
            match = m
        if match:
            truncated = attempt[: match.end()] + "}"
            try:
                return json.loads(truncated)
            except json.JSONDecodeError:
                pass

    # Last resort: try closing after the last complete key-value pair
    # by finding the last quote-colon-value sequence
    last_comma = attempt.rfind(",")
    if last_comma > 0:
        candidate = attempt[:last_comma] + "}"
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Try just closing the object as-is
    for suffix in ("}", '"}', '"}}'):
        try:
            return json.loads(attempt + suffix)
        except json.JSONDecodeError:
            pass

    return None


# ── Deterministic extraction ──────────────────────────────────────────────────

def _extract_deterministic_slots(message: str) -> Dict[str, Any]:
    """Extract slots that can be identified reliably without an LLM call.

    Includes:
    - Saudi national address code (regex via address_resolution)
    - Google Maps URLs (regex)
    - GPS coordinates (regex)
    - Email addresses (regex)
    """
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
