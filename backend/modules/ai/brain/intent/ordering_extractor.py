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
from typing import Any, Dict

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
    "موقعي", "موقع", "هنا",
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


def extract_ordering_slots(message: str) -> Dict[str, Any]:
    """
    Best-effort slot extractor for messages received during the order flow.

    Returns a (possibly empty) dict with any of:
      customer_name, customer_first_name, customer_last_name,
      city, short_address_code, google_maps_url, latitude, longitude.

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

    # ── Layer 1: Labeled field parsing ────────────────────────────────
    # Handles: "الاسم تركي الحارثي - المدينة الذهب - العنوان الوطني TAPA7401"
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

    # ── Layer 3: Lexicon-based city detection (if not already found) ──
    if not slots.get("city"):
        city = _detect_city(text)
        if city:
            slots["city"] = city

    # ── Layer 4: Heuristic name detection (if not already found) ─────
    if not slots.get("customer_first_name"):
        name_first, name_last = _detect_name(text, slots)
        if name_first or name_last:
            if name_first:
                slots["customer_first_name"] = name_first
            if name_last:
                slots["customer_last_name"] = name_last
            slots["customer_name"] = " ".join(p for p in (name_first, name_last) if p).strip()

    if slots:
        logger.debug("[OrderingExtractor] slots=%s", slots)
    return slots


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
    for line in lines:
        if "http" in line.lower() or "://" in line:
            continue
        if already_extracted.get("short_address_code") and \
                already_extracted["short_address_code"] in line.upper():
            continue
        normalized = _normalize_arabic(line)
        if normalized in _SAUDI_CITIES:
            continue  # this line is a city, not a name
        if _DIGIT_RE.search(line):
            continue
        candidate = _clean_name_candidate(line)
        if not candidate:
            continue
        first, last = _split_name(candidate)
        return first, last

    return "", ""


def _clean_name_candidate(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""

    # Reject if any non-name token appears prominently
    tokens = [t for t in re.split(r"\s+", text) if t]
    if not tokens or len(tokens) > 4:
        return ""

    # Build the rejection set with normalised forms so "ابغى" (with alef) and
    # "ابغي" (after alef-maksura → yaa) both match the same blocklist entry.
    blocklist = {_normalize_arabic(t) for t in _ARABIC_NON_NAME_TOKENS}

    cleaned = []
    for tok in tokens:
        normalized = _normalize_arabic(tok)
        if normalized in blocklist:
            return ""
        cleaned.append(tok)

    candidate = " ".join(cleaned).strip()

    # Must look like Arabic letters or simple Latin name — anything else
    # (mixed digits, punctuation-heavy, emoji) is rejected.
    if _ARABIC_LETTERS_RE.match(candidate):
        return candidate
    if _LATIN_NAME_RE.match(candidate):
        return candidate
    return ""


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
