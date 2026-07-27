"""
brain/intent/ordering_extractor.py
──────────────────────────────────
Deterministic, regex/lexicon-based ordering-slot extractor.

This is intentionally NOT an LLM. Its job is to guarantee that during the
order flow we never lose a free-text answer like "تركي الحارثي / الطائف /
<google maps url>" just because Haiku is unavailable or because the
classifier already returned a high-confidence rules intent.

Scope (kept narrow on purpose):

  * Saudi short national address code (e.g. "RIYD2342")
  * Google Maps short / share URLs
  * Bare GPS coordinates ("24.7136,46.6753")
  * A best-effort Arabic name guess (1-4 Arabic tokens, no digits/symbols)
  * A best-effort Saudi-city guess (against a small canonical lexicon)

The extractor is conservative — it returns a slot ONLY when reasonably
sure, leaving the confirmation step to the bot's order summary. False
positives are worse than false negatives here because a misread name
becomes the customer's real name in the draft order.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, Optional

from services.address_resolution import extract_address_signals

logger = logging.getLogger("nahla.brain.ordering_extractor")


# ── Saudi cities (canonical Arabic spellings + common variants) ────────
# Keep this list tight: it's used to *recognise* a city in free text, not
# to validate input. Variants are normalised below before matching.
_SAUDI_CITIES = {
    "الرياض": "الرياض",
    "رياض": "الرياض",
    "جدة": "جدة",
    "جده": "جدة",
    "مكة": "مكة المكرمة",
    "مكه": "مكة المكرمة",
    "مكة المكرمة": "مكة المكرمة",
    "المدينة": "المدينة المنورة",
    "المدينه": "المدينة المنورة",
    "المدينة المنورة": "المدينة المنورة",
    "الدمام": "الدمام",
    "الخبر": "الخبر",
    "الظهران": "الظهران",
    "الطائف": "الطائف",
    "الطايف": "الطائف",
    "تبوك": "تبوك",
    "بريدة": "بريدة",
    "بريده": "بريدة",
    "حائل": "حائل",
    "حايل": "حائل",
    "أبها": "أبها",
    "ابها": "أبها",
    "خميس مشيط": "خميس مشيط",
    "نجران": "نجران",
    "جازان": "جازان",
    "جيزان": "جازان",
    "الجبيل": "الجبيل",
    "ينبع": "ينبع",
    "عرعر": "عرعر",
    "سكاكا": "سكاكا",
    "الباحة": "الباحة",
    "القطيف": "القطيف",
    "الأحساء": "الأحساء",
    "الاحساء": "الأحساء",
    "الهفوف": "الأحساء",
    "حفر الباطن": "حفر الباطن",
    "القنفذة": "القنفذة",
    "بيشة": "بيشة",
    "محايل": "محايل عسير",
    "الخرج": "الخرج",
    "المجمعة": "المجمعة",
    "الدوادمي": "الدوادمي",
    "رابغ": "رابغ",
    "الليث": "الليث",
    "الذهب": "الذهب",
    "ذهبان": "ذهبان",
    "بحرة": "بحرة",
    "الزلفي": "الزلفي",
    "شقراء": "شقراء",
    "عفيف": "عفيف",
    "الرس": "الرس",
    "القويعية": "القويعية",
    "وادي الدواسر": "وادي الدواسر",
    "الخفجي": "الخفجي",
    "بقيق": "بقيق",
    "الجوف": "الجوف",
    "طريف": "طريف",
    "الوجه": "الوجه",
    "ضباء": "ضباء",
    "أملج": "أملج",
    "صبيا": "صبيا",
    "أبو عريش": "أبو عريش",
    "الحريق": "الحريق",
    "المندق": "المندق",
    "بلجرشي": "بلجرشي",
    "العقيق": "العقيق",
    "المخواة": "المخواة",
    "القنفذه": "القنفذة",
    "الخرمة": "الخرمة",
    "رنية": "رنية",
    "الكامل": "الكامل",
    "تثليث": "تثليث",
}

# Tokens that are clearly not a name even when written in Arabic. Used to
# reject false-positive name guesses ("نعم"/"شكراً"/etc.).
_ARABIC_NON_NAME_TOKENS = {
    "نعم", "لا", "اوكي", "اوك", "تمام", "موافق", "ابد", "ابدا", "ابداً",
    "اكيد", "اكيد طبعا", "طبعا", "ايوه", "ايوة", "اه", "آه",
    "شكرا", "شكراً", "مرحبا", "اهلا", "أهلاً", "السلام",
    "ابغى", "ابي", "ابى", "اريد", "أرغب", "ممكن", "لو سمحت", "وش",
    "وين", "كيف", "متى", "كم", "ليش", "ليه", "حقي", "ذا", "هذا",
    "هذه", "هذي", "ذي", "هاد", "بدي", "اشتري", "أرسل", "ارسل",
    "موقعي", "موقع", "هنا", "العنوان", "عنوان", "العنوان الوطني",
    "العنوان المختصر", "رقم المبنى", "الشارع", "الحي", "الرمز البريدي",
    "المدينة", "رقم الجوال", "طلح", "صفي", "سمر", "سدر", "شوك",
    # ── Arrival / presence verbs (May 2026 hotfix) ───────────────────
    # Production bug: "وصلت" / "أنا وصلت" / "جايه الحين" were being
    # captured as the customer's name in the order funnel, producing
    # "أبوي وصلت" greetings on subsequent turns. These tokens are
    # status statements, NEVER personal names. Normalised forms only
    # — see _normalize_arabic for the canonical mapping.
    "وصلت", "وصل", "وصلنا", "وصلتي", "وصلتو",
    "جاي", "جايه", "جاية", "جايين",
    "راجع", "راجعه", "راجعة", "رايح", "رايحه", "رايحة",
    "طالع", "طالعه", "طالعة", "نازل", "نازله", "نازلة",
    "موجود", "موجوده", "متوفر", "متوفره",
    "جاهز", "جاهزه", "حاضر", "حاضره",
    "بانتظار", "بانتظارك", "منتظر", "منتظره", "منتظرك",
    "اقرب", "قريب", "بعيد",
    # Temporal / payment-promise tokens — never personal names.
    "بعد", "شوي", "قليل", "احول", "بحول", "حول", "ارسل", "ارسله",
    "بدفع", "دفع", "تحويل", "حواله", "حوالة",
}

_ARABIC_LETTERS_RE = re.compile(r"^[\u0621-\u064A][\u0621-\u064A\s]*$")
_LATIN_NAME_RE     = re.compile(r"^[A-Za-z][A-Za-z\.\-\' ]{1,40}$")
_DIGIT_RE          = re.compile(r"\d")

# Labeled field patterns: customer sends structured data like
# "الاسم: تركي الحارثي / المدينة: الذهب / العنوان الوطني: TAPA7401"
# These capture the value after the Arabic label keyword.
_LABEL_NAME_RE = re.compile(
    r"(?:^|[\n/،,\-|])\s*(?:الاسم|اسمي|اسمك)\s*[:/\-]?\s*([^\n/،,\-|]{2,40})",
    re.IGNORECASE | re.UNICODE | re.MULTILINE,
)
_LABEL_CITY_RE = re.compile(
    r"(?:^|[\n/،,\-|])\s*(?:المدينة|المدينه|مدينة التوصيل|مدينة الشحن|مدينة|المنطقة)\s*[:/\-]?\s*([^\n/،,\-|]{2,30})",
    re.IGNORECASE | re.UNICODE | re.MULTILINE,
)
_LABEL_PHONE_RE = re.compile(
    r"(?:^|[\n/،,\-|])\s*(?:رقم\s*الجوال|الجوال|رقم\s*التواصل|الهاتف|phone|mobile)"
    r"\s*[:/\-]?\s*([+\d٠-٩\s().-]{7,24})",
    re.IGNORECASE | re.UNICODE | re.MULTILINE,
)
_ADDRESS_FIELD_RES = {
    "building_number": re.compile(
        r"(?:^|[\n/،,\-|])\s*(?:رقم\s*المبنى|المبنى|building\s*number)"
        r"\s*[:/\-]?\s*([0-9٠-٩]{3,8})",
        re.IGNORECASE | re.UNICODE | re.MULTILINE,
    ),
    "additional_number": re.compile(
        r"(?:^|[\n/،,\-|])\s*(?:الرقم\s*الفرعي|الرقم\s*الإضافي|additional\s*number)"
        r"\s*[:/\-]?\s*([0-9٠-٩]{3,8})",
        re.IGNORECASE | re.UNICODE | re.MULTILINE,
    ),
    "street": re.compile(
        r"(?:^|[\n/،,\-|])\s*(?:الشارع|شارع|street)\s*[:/\-]?\s*([^\n/،,\-|]{2,60})",
        re.IGNORECASE | re.UNICODE | re.MULTILINE,
    ),
    "district": re.compile(
        r"(?:^|[\n/،,\-|])\s*(?:الحي|حي|district|neighborhood)\s*[:/\-]?\s*([^\n/،,\-|]{2,60})",
        re.IGNORECASE | re.UNICODE | re.MULTILINE,
    ),
    "postal_code": re.compile(
        r"(?:^|[\n/،,\-|])\s*(?:الرمز\s*البريدي|postal\s*code|zip)"
        r"\s*[:/\-]?\s*([0-9٠-٩]{4,8})",
        re.IGNORECASE | re.UNICODE | re.MULTILINE,
    ),
}

# Strip list prefixes like "1- ", "2.", "١- " before name heuristics.
_NUMBERED_LINE_PREFIX_RE = re.compile(
    r"^\s*(?:[1-9][0-9]?|[١٢٣٤٥٦٧٨٩][٠-٩]{0,2})\s*[-–—.)]\s*",
    re.UNICODE,
)
_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_QTY_NORM_RE = re.compile(f"[{_DIA}]+")
_QTY_WS_RE = re.compile(r"\s+")

# Dual / counted-noun quantity forms — structural, not phrase-list driven.
_AR_DUAL_QTY_RE = re.compile(
    r"^(?:كميتين|حبتين|قطعتين|اثنتين|اثنين|إثنين|ثنتين)\s*$",
    re.I | re.UNICODE,
)
_AR_COUNTED_NOUN_RE = re.compile(
    r"(?:حبة|حبات|حبه|حبتين|قطعة|قطع|قطعتين|كمية|كميات|كميتين|عدد|وحدة|وحدات)",
    re.I | re.UNICODE,
)
_AR_SPELLED_QTY_WITH_NOUN_RE = re.compile(
    r"^(?P<num>"
    r"واحد|واحدة|حبة|"
    r"اثنين|إثنين|اثنتين|ثنتين|"
    r"ثلاث|ثلاثة|تلات|تلاته|"
    r"اربع|أربع|اربعة|أربعة|"
    r"خمس|خمسة|"
    r"ست|ستة|"
    r"سبع|سبعة|"
    r"ثمان|ثمانية|"
    r"تسع|تسعة|"
    r"عشر|عشرة"
    r")\s+"
    r"(?:حبة|حبات|حبه|قطعة|قطع|كمية|كميات|عدد|وحدة|وحدات)\s*$",
    re.I | re.UNICODE,
)
_BARE_DIGIT_QTY_RE = re.compile(r"^[0-9٠-٩]{1,3}$", re.UNICODE)

# Reused from cart_intent_extractor._AR_NUM_WORDS (extended for counted-noun parser).
_AR_COUNT_WORDS = {
    "واحد": 1, "واحدة": 1, "حبة": 1,
    "اثنين": 2, "إثنين": 2, "اثنتين": 2, "ثنتين": 2,
    "حبتين": 2, "كميتين": 2, "قطعتين": 2,
    "ثلاث": 3, "ثلاثة": 3, "تلات": 3, "تلاته": 3,
    "اربع": 4, "أربع": 4, "اربعة": 4, "أربعة": 4,
    "خمس": 5, "خمسة": 5,
    "ست": 6, "ستة": 6,
    "سبع": 7, "سبعة": 7,
    "ثمان": 8, "ثمانية": 8,
    "تسع": 9, "تسعة": 9,
    "عشر": 10, "عشرة": 10,
}

_NON_NAME_LINE_PREFIXES = (
    "العنوان",
    "العنوان الوطني",
    "العنوان المختصر",
    "رقم المبنى",
    "الرقم الفرعي",
    "الرقم الإضافي",
    "الشارع",
    "شارع",
    "الحي",
    "حي",
    "الرمز البريدي",
    "المدينة",
    "رقم الجوال",
)


def _norm_qty_text(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _QTY_NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _QTY_WS_RE.sub(" ", t).strip()


def extract_ordering_quantity(message: str) -> Optional[int]:
    """
    Structural Arabic quantity parser for the single-product order funnel.

    Reuses ``active_order_quantity_extract._parse_count_quantity`` for the
    حبتين / اثنين / 4-forms it already covers, then extends with dual forms
    (كميتين, قطعتين), spelled-number + counted-noun (ثلاث حبات), and bare
    ASCII / Eastern-Arabic digits.
    """
    text = (message or "").strip()
    if not text:
        return None

    from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: PLC0415
        _parse_count_quantity,
    )

    reused = _parse_count_quantity(text)
    if reused:
        return reused

    norm = _norm_qty_text(text)
    if not norm:
        return None

    if _AR_DUAL_QTY_RE.match(norm):
        return 2

    spelled = _AR_SPELLED_QTY_WITH_NOUN_RE.match(norm)
    if spelled:
        num_word = _norm_qty_text(spelled.group("num") or "")
        if num_word in _AR_COUNT_WORDS:
            return _AR_COUNT_WORDS[num_word]

    if _BARE_DIGIT_QTY_RE.match(text.strip()):
        digits = _digits_to_western(text.strip())
        try:
            qty = int(digits)
        except (TypeError, ValueError):
            return None
        return qty if qty >= 1 else None

    # Leading spelled number + optional counted noun anywhere in a short message.
    tokens = norm.split()
    if tokens and tokens[0] in _AR_COUNT_WORDS and len(tokens) <= 3:
        if len(tokens) == 1 or _AR_COUNTED_NOUN_RE.search(norm):
            return _AR_COUNT_WORDS[tokens[0]]

    return None


def message_is_quantity_only(message: str) -> bool:
    """True when the inbound text is purely a quantity expression."""
    return extract_ordering_quantity(message) is not None


def extract_ordering_slots(message: str) -> Dict[str, Any]:
    """
    Best-effort slot extractor for messages received during the order flow.

    Returns a (possibly empty) dict with any of:
      customer_name, customer_first_name, customer_last_name,
      city, customer_phone, short_address_code, google_maps_url,
      latitude, longitude, and structured national-address fields.

    Priority order:
      1. Labeled fields ("الاسم X", "المدينة X") — highest precision
      2. Address signals (short code, maps URL, GPS)
      3. Lexicon-based city detection
      4. Heuristic name detection
    """
    text = (message or "").strip()
    if not text:
        return {}

    slots: Dict[str, Any] = {}

    quantity = extract_ordering_quantity(text)
    if quantity is not None:
        slots["quantity"] = quantity

    _skip_name = message_is_quantity_only(text)
    if not _skip_name:
        try:
            from modules.ai.brain.postprocess.payment_reply_guard import (  # noqa: PLC0415
                detect_future_transfer_intent,
            )
            _skip_name = detect_future_transfer_intent(text)
        except Exception:  # noqa: BLE001
            _skip_name = False

    # ── Layer 1: Labeled field parsing ────────────────────────────────
    if not _skip_name:
        labeled_name = _extract_labeled_name(text)
        if labeled_name:
            first, last = _split_name(labeled_name)
            if first:
                slots["customer_first_name"] = first
            if last:
                slots["customer_last_name"] = last
            slots["customer_name"] = labeled_name

    labeled_city = _extract_labeled_city(text)
    if labeled_city:
        slots["city"] = labeled_city

    labeled_phone = _extract_labeled_phone(text)
    if labeled_phone:
        slots["customer_phone"] = labeled_phone

    # ── Layer 2: Address: short code + maps URL + GPS coords ──────────
    address_signals = extract_address_signals(text)
    if address_signals.get("short_address_code"):
        slots["short_address_code"] = address_signals["short_address_code"]
    if address_signals.get("google_maps_url"):
        slots["google_maps_url"] = address_signals["google_maps_url"]
    if address_signals.get("latitude") is not None:
        slots["latitude"] = address_signals["latitude"]
    if address_signals.get("longitude") is not None:
        slots["longitude"] = address_signals["longitude"]
    slots.update(_extract_structured_address_fields(text))

    # ── Layer 3: Lexicon-based city detection (if not already found) ──
    if not slots.get("city"):
        city = _detect_city(text)
        if city:
            slots["city"] = city

    # ── Layer 4: Heuristic name detection (if not already found) ─────
    if not _skip_name and not slots.get("quantity") and not slots.get("customer_first_name"):
        name_first, name_last = _detect_name(text, slots)
        if name_first or name_last:
            if name_first:
                slots["customer_first_name"] = name_first
            if name_last:
                slots["customer_last_name"] = name_last
            slots["customer_name"] = " ".join(
                p for p in (name_first, name_last) if p
            ).strip()

    if slots:
        if slots.get("customer_first_name") or slots.get("customer_name"):
            logger.info(
                "[ORDER_NAME_CAPTURE] source=ordering_extractor "
                "first=%r last=%r full=%r short_code=%r",
                slots.get("customer_first_name"),
                slots.get("customer_last_name"),
                slots.get("customer_name"),
                slots.get("short_address_code"),
            )
        logger.debug("[OrderingExtractor] slots=%s", slots)
    return slots


def _strip_numbered_list_prefix(line: str) -> str:
    """Remove leading ``1-`` / ``2.`` / ``١-`` list markers from a line."""
    text = (line or "").strip()
    for _ in range(3):
        m = _NUMBERED_LINE_PREFIX_RE.match(text)
        if not m:
            break
        text = text[m.end():].strip()
    return text


def _extract_labeled_name(text: str) -> str:
    """Extract name from 'الاسم X' pattern. Returns cleaned full name or ''."""
    m = _LABEL_NAME_RE.search(text)
    if not m:
        return ""
    raw = m.group(1).strip()
    # Remove trailing label noise (digits, special chars)
    raw = re.sub(r"[/،,\-|:]+$", "", raw).strip()
    # Must be 2+ Arabic tokens without digits
    if _DIGIT_RE.search(raw):
        return ""
    tokens = [t for t in raw.split() if len(t) >= 2]
    if not tokens:
        return ""
    return " ".join(tokens)


def _extract_labeled_city(text: str) -> str:
    """Extract city from 'المدينة X' pattern. Returns cleaned city or ''."""
    m = _LABEL_CITY_RE.search(text)
    if not m:
        return ""
    raw = m.group(1).strip()
    raw = re.sub(r"[/،,\-|:]+$", "", raw).strip()
    if not raw or _DIGIT_RE.search(raw):
        return ""
    # Normalize and check lexicon first for canonical spelling
    normalized = _normalize_arabic(raw)
    return _SAUDI_CITIES.get(normalized, raw)


def _digits_to_western(text: str) -> str:
    return (text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))


def _extract_labeled_phone(text: str) -> str:
    m = _LABEL_PHONE_RE.search(text)
    if not m:
        return ""
    raw = _digits_to_western(m.group(1))
    cleaned = re.sub(r"[^\d+]", "", raw)
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 9:
        return ""
    return cleaned


def _extract_structured_address_fields(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for key, pattern in _ADDRESS_FIELD_RES.items():
        m = pattern.search(text or "")
        if not m:
            continue
        value = _digits_to_western((m.group(1) or "").strip())
        value = re.sub(r"[/،,\-|:]+$", "", value).strip()
        if value:
            fields[key] = value
    if any(fields.get(k) for k in ("street", "district", "postal_code", "building_number")):
        fields["address_line"] = "، ".join(
            v for v in (
                fields.get("street"),
                fields.get("district"),
                fields.get("building_number"),
                fields.get("additional_number"),
                fields.get("postal_code"),
            )
            if v
        )
    return fields


# ── Internals ───────────────────────────────────────────────────────────

def _detect_city(text: str) -> str:
    # Try line-by-line first so "الطائف" alone on its own line wins fast.
    candidates = [text] + [ln.strip() for ln in text.splitlines() if ln.strip()]
    for chunk in candidates:
        normalized = _normalize_arabic(chunk)
        if normalized in _SAUDI_CITIES:
            return _SAUDI_CITIES[normalized]
    # Then a token-window scan for inline mentions ("أنا من الرياض").
    tokens = [_normalize_arabic(t) for t in re.split(r"\s+", text) if t.strip()]
    for size in (3, 2, 1):
        for i in range(len(tokens) - size + 1):
            window = " ".join(tokens[i:i + size]).strip()
            if window in _SAUDI_CITIES:
                return _SAUDI_CITIES[window]
    return ""


def _detect_name(text: str, already_extracted: Dict[str, Any]) -> tuple[str, str]:
    # Skip lines that are clearly a URL or a structured signal.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "", ""

    # Prefer the first line that doesn't look like an address/URL/code.
    for raw_line in lines:
        line = _strip_numbered_list_prefix(raw_line)
        if not line:
            continue
        if "http" in line.lower() or "://" in line:
            continue
        if already_extracted.get("short_address_code") and \
                already_extracted["short_address_code"] in line.upper():
            continue
        normalized = _normalize_arabic(line)
        if normalized in _SAUDI_CITIES:
            continue  # this line is a city, not a name
        if normalized in {_normalize_arabic(t) for t in _ARABIC_NON_NAME_TOKENS}:
            continue
        if any(
            normalized.startswith(_normalize_arabic(prefix))
            for prefix in _NON_NAME_LINE_PREFIXES
        ):
            continue
        if "كيلو" in normalized or "كيلo" in normalized or "عسل" in normalized:
            continue
        # Reject lines that still contain address codes (4 letters + 4 digits).
        if re.search(r"[A-Za-z]{4}\d{4}", line.upper()):
            continue
        if _DIGIT_RE.search(line):
            continue
        if extract_ordering_quantity(line):
            continue
        candidate = _clean_name_candidate(line)
        if not candidate:
            continue
        first, last = _split_name(candidate)
        return first, last

    return "", ""


def _clean_name_candidate(raw: str) -> str:
    from core.customer_name_validator import validate_customer_name  # noqa: PLC0415

    hit = validate_customer_name(raw)
    return hit.cleaned if hit.valid else ""


def _split_name(full_name: str) -> tuple[str, str]:
    parts = [p.strip() for p in full_name.split() if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _normalize_arabic(text: str) -> str:
    """
    Normalize Arabic text for lexicon matching:
    * remove tatweel / diacritics
    * unify alef / yaa / taa-marbuta variants
    * strip punctuation and collapse whitespace
    * lowercase ASCII fallback
    """
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)            # diacritics + tatweel
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ئ", "ي").replace("ة", "ه")
    t = re.sub(r"[^\u0621-\u064AA-Za-z\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t
