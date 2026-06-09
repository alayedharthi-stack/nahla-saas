"""
service_availability_gate.py
────────────────────────────
ARCH-HANDOFF-001 — block false ``talk_to_human`` / pre-brain handoff on
service-availability phrasing (``فيه أحد يقدر يلسعني``) while preserving
genuine staff-wait requests (``فيه أحد يرد؟``, ``كلموني``).

Platform-wide; no tenant-specific logic.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Customer waits for the *store team on WhatsApp* to respond.
_RESPONSE_WAIT_AFTER_AHAD_RE = re.compile(
    r"(?:"
    r"(?:في|فيه|هل\s*في|هل\s*يوجد|يوجد)\s+"
    r"(?:أحد|احد|واحد|حد)\s+"
    r"(?:يرد|يردّ|يرد\s*علي|يكلمني|يتواصل|يحكي|يجاوبني|يجاوب\s*علي)"
    r"|"
    r"(?:محد|ماحد|ما\s*أحد|ما\s*احد)\s*(?:رد|يرد|يجاوب|يكلمني)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Bare "anyone there?" — short message ending right after أحد/حد.
_SHORT_AHAD_WAIT_RE = re.compile(
    r"^\s*(?:في|فيه|هل\s*في|هل\s*يوجد|يوجد)\s+"
    r"(?:أحد|احد|واحد|حد)\s*[\?؟]?\s*$",
    re.IGNORECASE | re.UNICODE,
)

# ``فيه أحد هنا`` — waiting for presence, not a service capability ask.
_AHAD_HERE_WAIT_RE = re.compile(
    r"(?:في|فيه|هل\s*في|هل\s*يوجد|يوجد)\s+"
    r"(?:أحد|احد|واحد|حد)\s+هنا",
    re.IGNORECASE | re.UNICODE,
)

# ``فيه موظف يرد`` / ``هل يوجد موظف يتواصل`` — staff-noun + response verb.
_STAFF_NOUN_RESPONSE_WAIT_RE = re.compile(
    r"(?:في|فيه|هل\s*في|هل\s*يوجد|يوجد)\s+"
    r"(?:موظف|مختص|مسؤول|مشرف|شخص|بشري|إنسان|انسان)\s+"
    r"(?:يرد|يردّ|يتواصل|يكلمني|يحكي|يجاوب)",
    re.IGNORECASE | re.UNICODE,
)

# Service / capability verb after أحد — "is there someone who can X?"
_SERVICE_VERB_RE = (
    r"يقدر|يسوي|يساعد|يلسع|يبيع|يوصل|يصلح|يشرح|ينصح|يفيد|يفهم|"
    r"يعمل|يجهز|يختار|يوصي|يركب|يصل|يقدم|ينفذ|يضبط|يخدم|"
    r"يأخذ|ياخذ|يستقبل|يعرف"
)

_SERVICE_AFTER_AHAD_RE = re.compile(
    rf"(?:في|فيه|هل\s*في|هل\s*يوجد|يوجد)\s+"
    rf"(?:أحد|احد|واحد|حد)\s+"
    rf"(?:{_SERVICE_VERB_RE})",
    re.IGNORECASE | re.UNICODE,
)

# Staff-noun prefix + service verb (P04 false positives).
_STAFF_NOUN_SERVICE_RE = re.compile(
    rf"(?:في|فيه|هل\s*في|هل\s*يوجد|يوجد)\s+"
    rf"(?:موظف|مختص|مسؤول|مشرف|شخص|بشري|إنسان|انسان)\s+"
    rf"(?!يرد|يتواصل|يكلمني|يحكي|يجاوب)"
    rf"(?:{_SERVICE_VERB_RE}|يشرح|ينصح|يفيد)",
    re.IGNORECASE | re.UNICODE,
)

# Profession / expert descriptor after أحد (consultation, not handoff).
_PROFESSION_AFTER_AHAD_RE = re.compile(
    r"(?:في|فيه|هل\s*في|هل\s*يوجد|يوجد)\s+"
    r"(?:أحد|احد|واحد|حد)\s+"
    r"(?:مختص|خبير|دكتور|فني|استشاري|طبيب|ممرض|ممرضه|ممرضة)",
    re.IGNORECASE | re.UNICODE,
)

# Location tail after أحد without a response-wait verb.
_LOCATION_AFTER_AHAD_RE = re.compile(
    r"(?:في|فيه|هل\s*في|هل\s*يوجد|يوجد)\s+"
    r"(?:أحد|احد|واحد|حد)\s+"
    r"(?:"
    r"(?:في|ب(?:ال)?|عند)\s*"
    r"(?:ال)?(?:رياض|جده|جدة|الدمام|مكه|مكة|المدينه|المدينة|"
    r"الخبر|الطائف|تبوك|أبها|ابها|القصيم|الاحساء|الأحساء|فرع|محل)"
    r"|قريب"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# ``فيه احد من عندكم اسمه خالد`` — person lookup, not handoff.
_TEAM_PERSON_LOOKUP_RE = re.compile(
    r"(?:أحد|احد)\s+من\s+عندكم",
    re.IGNORECASE | re.UNICODE,
)

# ``هل يوجد مختص بالعسل`` — staff-noun + profession/location tail.
_STAFF_NOUN_ADVISORY_RE = re.compile(
    r"(?:في|فيه|هل\s*في|هل\s*يوجد|يوجد)\s+"
    r"(?:موظف|مختص|مسؤول|شخص)\s+"
    r"(?:بال|في|عن|ل)\s*\S+",
    re.IGNORECASE | re.UNICODE,
)

# ``فيه أحد يجاوب على استفساري`` — answers a question, not inbox wait.
_SERVICE_ANSWER_RE = re.compile(
    r"(?:أحد|احد)\s+"
    r"(?:يقدر\s+)?(?:يجاوب|يفيد|يشرح|ينصح)\s+"
    r"(?:على|في|بخصوص|عن)",
    re.IGNORECASE | re.UNICODE,
)


def _norm_ar(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = unicodedata.normalize("NFKC", text.strip())
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    )
    t = re.sub(r"[؟?!.,؛:]", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def is_genuine_staff_wait_inquiry(message: str) -> bool:
    """True when the customer is waiting for the store team to respond."""
    norm = _norm_ar(message)
    if not norm:
        return False
    if _SHORT_AHAD_WAIT_RE.search(norm):
        return True
    if _AHAD_HERE_WAIT_RE.search(norm):
        return True
    if _RESPONSE_WAIT_AFTER_AHAD_RE.search(norm):
        return True
    if _STAFF_NOUN_RESPONSE_WAIT_RE.search(norm):
        return True
    return False


def is_service_availability_inquiry(message: str) -> bool:
    """
    True when the message asks whether someone can perform a service /
    is available at a location — NOT a WhatsApp handoff request.
    """
    norm = _norm_ar(message)
    if not norm:
        return False

    if is_genuine_staff_wait_inquiry(message):
        return False

    if _SERVICE_AFTER_AHAD_RE.search(norm):
        return True
    if _STAFF_NOUN_SERVICE_RE.search(norm):
        return True
    if _PROFESSION_AFTER_AHAD_RE.search(norm):
        return True
    if _LOCATION_AFTER_AHAD_RE.search(norm):
        return True
    if _STAFF_NOUN_ADVISORY_RE.search(norm):
        return True
    if _SERVICE_ANSWER_RE.search(norm):
        return True
    if _TEAM_PERSON_LOOKUP_RE.search(norm):
        return True

    return False


__all__ = [
    "is_genuine_staff_wait_inquiry",
    "is_service_availability_inquiry",
]
