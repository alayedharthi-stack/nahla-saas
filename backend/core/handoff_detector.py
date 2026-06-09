"""
core/handoff_detector.py
─────────────────────────
Deterministic Arabic-normalised matcher for explicit "transfer me to a
human" requests. Independent from the AI brain — used by the WhatsApp
webhook BEFORE the brain runs so a customer asking for a human agent
ALWAYS lands in the merchant's "طلب موظف" inbox filter, even when:

  * the brain raises an exception mid-turn,
  * the LLM fallback misclassifies the phrase,
  * the rule-based intent classifier misses the specific dialect
    variation,
  * the conversation is in any state (mid-order, awaiting receipt,
    paused, …).

Design constraints
──────────────────
* **Zero dependencies**: pure-Python, no DB, no I/O. Safe to import
  on the synchronous critical path.
* **Conservative**: must not fire on unrelated commerce phrases. We
  pair every handoff verb with a target token so "موظف" doesn't
  escalate on "أنا موظف لدى …".
* **Dialect tolerant**: Saudi / Gulf / MSA wordings all map to the
  same normalised form; alef/ya/ta-marbuta/hamza variants are
  collapsed before matching.
* **Diacritic blind**: Arabic tashkīl is stripped before comparison
  so customers typing with full vocalisation still match.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Optional


# ── Arabic normalisation ────────────────────────────────────────────
#
# Collapses every common typographic variation merchants see on
# WhatsApp into a single canonical form:
#
#   ا / أ / إ / آ / ٱ      →   ا
#   ى / ي                 →   ي
#   ة                     →   ه
#   tashkīl (fatha, kasra, damma, sukūn, shadda, tanwīn, tatwīl)  → removed
#   non-Arabic letters    →   lowercased Latin (so "Maps" matches "maps")

_ARABIC_ALEF_VARIANTS = ("\u0623", "\u0625", "\u0622", "\u0671")   # أ إ آ ٱ
_ARABIC_YA_VARIANTS   = ("\u0649",)                                 # ى
_ARABIC_TA_MARBUTA    = "\u0629"                                    # ة
_ARABIC_TATWIL        = "\u0640"                                    # ـ
# Strip only the combining-mark / harakāt range. ``\u0671`` (alef
# wasla) is a full letter and is handled below in
# ``_ARABIC_ALEF_VARIANTS`` — listing it here would erase the alef
# entirely instead of normalising it.
_ARABIC_DIACRITICS_RE = re.compile(
    "[\u064b-\u0652\u0670]"  # tanwin / fatha / kasra / damma / sukūn / shadda / superscript-alef
)


def normalize_arabic_text(text: Optional[str]) -> str:
    """Return ``text`` with the Arabic typographic variations above
    collapsed to a canonical lower-case form. ``None`` / non-string
    input becomes ``""``.

    Never raises.
    """
    if not text or not isinstance(text, str):
        return ""
    t = text.strip()
    if not t:
        return ""
    # Remove tatweel and diacritics.
    t = t.replace(_ARABIC_TATWIL, "")
    t = _ARABIC_DIACRITICS_RE.sub("", t)
    # Collapse alef variants.
    for ch in _ARABIC_ALEF_VARIANTS:
        t = t.replace(ch, "\u0627")
    # Collapse alef-maksura to ya.
    for ch in _ARABIC_YA_VARIANTS:
        t = t.replace(ch, "\u064a")
    # Ta-marbuta → ha (covers "مدفوعة" → "مدفوعه").
    t = t.replace(_ARABIC_TA_MARBUTA, "\u0647")
    # Lowercase Latin segments so English mixed-in tokens match.
    t = t.lower()
    # Collapse runs of whitespace.
    t = re.sub(r"\s+", " ", t)
    return t


# ── Handoff phrase library ─────────────────────────────────────────
#
# Every entry is stored in the normalised form produced by
# ``normalize_arabic_text`` so the matcher can use cheap substring /
# regex checks without re-normalising at runtime.
#
# Two layers of matching:
#
#   1. ``_EXACT_HANDOFF_PHRASES`` — full message after normalisation
#      equals one of these. Used for short Saudi acknowledgements
#      that would over-match inside longer commerce sentences
#      ("كلموني" inside "كلموني لما توصل الشحنة" is NOT a handoff
#      request — but a bare "كلموني" is).
#
#   2. ``_SUBSTRING_HANDOFF_PHRASES`` — any of these appearing
#      anywhere in the normalised message triggers a handoff. Curated
#      so substring matching does not produce false positives on
#      unrelated commerce text.

_EXACT_HANDOFF_PHRASES = frozenset(normalize_arabic_text(p) for p in (
    "كلموني",
    "كلميني",
    "كلمني",
    "حولني",
    "حوّلني",
    "حولوني",
    "حوّلوني",
    "ودي اكلم احد",
    "ودي اتكلم مع احد",
    "ابي موظف",
    "ابغى موظف",
    "ابي مختص",
    "ابغى مختص",
    "ابي مسؤول",
    "ابغى مسؤول",
    "احد يرد",
    "احد يرد علي",
    "في احد يرد",
    "فيه احد يرد",
    "في احد",
    "ما احد رد",
    "ماحد رد",
    "محد رد",
    "محد رد علي",
    "ما احد يرد",
    "محد يرد",
    "ماحد يرد",
    "ما حد رد",
    "ما حد يرد",
    "ما حد رد علي",
    "ما حد يرد علي",
    "خدمه العملاء",
    "خدمة العملاء",
    "الدعم الفني",
    "دعم العملاء",
    "human",
    "agent",
    "support",
    "talk to human",
    "real person",
    "real human",
))


_SUBSTRING_HANDOFF_PHRASES = tuple(normalize_arabic_text(p) for p in (
    # "transfer me to a staff / owner / supervisor / specialist"
    "حولني لموظف",
    "حولني للموظف",
    "حولني الى موظف",
    "حولني على موظف",
    "حولني لمشرف",
    "حولني للمشرف",
    "حولني لمختص",
    "حولني للمختص",
    "حولني لمسؤول",
    "حولني للمسؤول",
    "حولني لشخص",
    "حولني لانسان",
    "حولني للدعم",
    "حولني لخدمه العملاء",
    "حولني لخدمة العملاء",
    "حولوني لموظف",
    "حولوني للموظف",
    "حولوني لمسؤول",
    # "I want to talk / speak / chat with someone / a staff member"
    "ابي اكلم احد",
    "ابغى اكلم احد",
    "ابي اتكلم مع احد",
    "ابغى اتكلم مع احد",
    "ابي اتحدث مع احد",
    "ابغى اتحدث مع احد",
    "ابي احكي مع احد",
    "ابغى احكي مع احد",
    "ابي اكلم موظف",
    "ابغى اكلم موظف",
    "ابي اتكلم مع موظف",
    "ابغى اتكلم مع موظف",
    "ابي اتحدث مع موظف",
    "ابي اتكلم مع مختص",
    "ابغى اتكلم مع مختص",
    "ابي اتكلم مع مسؤول",
    "ابغى اتكلم مع مسؤول",
    "ابي اتكلم مع شخص",
    "ابي اتكلم مع انسان",
    "ابي اتكلم مع بشر",
    "ابي اكلم بشر",
    "ابغى اكلم بشر",
    # "I need / want a human"
    "احتاج موظف",
    "احتاج مختص",
    "احتاج مسؤول",
    "احتاج انسان",
    "احتاج بشر",
    "محتاج موظف",
    "محتاج مختص",
    "محتاج بشري",
    "اريد موظف",
    "اريد مختص",
    "اريد التحدث مع موظف",
    "اريد التحدث مع شخص",
    "اريد التحدث مع انسان",
    # Polite "call me / get back to me"
    "كلموني لو سمحتم",
    "كلموني الحين",
    "ابي تكلموني",
    "ابغى تكلموني",
    "اتصلوا فيني",
    "اتصلوا بي",
    "اتصلوا علي",
    "ردوا علي",
    "ردوا عليه",
    "ردوا عليّ",
    "ما حد رد علي",
    "ماحد رد علي",
    "محد رد علي",
    # "Is anybody there / answering?" — require response verb; bare
    # «هل في احد» substring removed (ARCH-HANDOFF-001) — it matched
    # service questions like «هل يوجد أحد يقدر يشرح».
    "في احد يرد علي",
    "فيه احد يرد علي",
    "هل في احد يرد",
    "هل يوجد احد يرد",
    # "I want / need customer service / support"
    "ابي خدمه العملاء",
    "ابغى خدمه العملاء",
    "ابي الدعم",
    "ابغى الدعم",
    "اريد خدمه العملاء",
    # ── Owner / management / shop-owner / supervisor contact (May 2026 #42)
    # Production regression on Tenant 33: a customer typed
    #   "أبي أتواصل مع المالك"
    # and Nahla replied with a generic store-intro
    #   "أنا نحلة... متخصصين في العسل..."
    # because the message:
    #   1. Did NOT match _SUBSTRING_HANDOFF_PHRASES (no "ابي اتكلم
    #      مع احد" / "حولني لموظف" wording),
    #   2. Did NOT match the rule classifier's INTENT_TALK_HUMAN
    #      target-noun set (المالك / صاحب المحل / الإدارة / المسؤول
    #      were missing from the noun list — only "موظف / مسؤول /
    #      شخص" were anchored),
    #   3. Did NOT match the INTENT_ASK_OWNER_CONTACT noun-form
    #      patterns ("التواصل مع المالك" requires the noun form
    #      التواصل, not the verb form أتواصل),
    # so the message fell through to the default LLM compose path
    # which hallucinated a store intro.
    #
    # Fix: every "verb + المالك / صاحب المحل / الادارة / المسؤول"
    # phrasing the production transcripts surfaced is added to the
    # substring scan so the PRE-BRAIN handoff guard catches it BEFORE
    # any LLM call. The webhook then uses the new
    # ``HANDOFF_OWNER_ACK_TEXT_AR`` (a clarifier-style ack) instead
    # of the generic team copy — see ``is_owner_contact_request``.
    "ابي اتواصل مع المالك",
    "ابغى اتواصل مع المالك",
    "اتواصل مع المالك",
    "ابي اكلم المالك",
    "ابغى اكلم المالك",
    "اكلم المالك",
    "ابي تواصل مع المالك",
    "ابغى تواصل مع المالك",
    "ودي اكلم المالك",
    "ودي اتواصل مع المالك",
    "ابي احكي مع المالك",
    "ابغى احكي مع المالك",
    "اكلم صاحب المحل",
    "اتواصل مع صاحب المحل",
    "ابي اكلم صاحب المحل",
    "ابغى اكلم صاحب المحل",
    "ابي اتواصل مع صاحب المحل",
    "ابغى اتواصل مع صاحب المحل",
    "ابي صاحب المحل",
    "ابي صاحب المتجر",
    "ابي اكلم صاحب المتجر",
    "اتواصل مع صاحب المتجر",
    "ابي اتواصل مع الادارة",
    "ابغى اتواصل مع الادارة",
    "اتواصل مع الادارة",
    "ابي اكلم الادارة",
    "ابغى اكلم الادارة",
    "اكلم الادارة",
    "ابي الادارة",
    "ابغى الادارة",
    "ابي اتواصل مع المسؤول",
    "ابغى اتواصل مع المسؤول",
    "اتواصل مع المسؤول",
    "ابي اكلم المسؤول",
    "ابغى اكلم المسؤول",
    "اكلم المسؤول",
    # English fallbacks
    "talk to a human",
    "talk to an agent",
    "speak to someone",
    "speak to an agent",
    "real human please",
    "customer service",
    "i need support",
    "i need a human",
    "transfer me to",
    "connect me to",
    # Owner / management — English
    "talk to the owner",
    "speak to the owner",
    "talk to management",
    "speak to management",
    "shop owner",
    "store owner",
))


# ── Owner-contact specific detector (May 2026 #42) ──────────────────
#
# Returns True for the SUBSET of handoff requests that are explicitly
# about contacting the OWNER / MANAGEMENT / SHOP-OWNER / SUPERVISOR.
# These messages still get the full handoff plumbing (needs_human,
# handoff_active, paused AI, merchant inbox alert), but the customer-
# facing acknowledgement uses the clarifier-style copy below so we:
#
#   1. Honour the customer's specific framing ("المالك", not "موظف"),
#   2. Ask one focused clarifier question — "ممكن توضح سبب التواصل؟"
#   3. Promise to forward the request to the right person, without
#      pretending we already know who that is.
#
# The detector is intentionally narrow — it requires BOTH:
#   * an escalation/contact verb (أتواصل / أكلم / احكي / contact / talk),
#   * AND an owner-noun token (المالك / صاحب المحل / الادارة /
#     المسؤول / owner / management / shop owner / store owner).
# This guards against false positives on messages that happen to
# mention "المسؤول" inside a non-handoff context (e.g. a product
# review).
#
# Pure-string check; never raises.

_OWNER_NOUN_TOKENS = (
    "المالك",
    "مالك المحل",
    "مالك المتجر",
    "صاحب المحل",
    "صاحب المتجر",
    "صاحب الموقع",
    "الادارة",
    "ادارة المحل",
    "ادارة المتجر",
    "المسؤول",
    "المسوول",
    "المشرف العام",
)

_OWNER_VERB_TOKENS = (
    "اتواصل",
    "تواصل",
    "اكلم",
    "كلم",
    "احكي",
    "اتكلم",
    "اتحدث",
    "تحدث",
    "ودي اكلم",
    "ودي اتواصل",
    "ابي اكلم",
    "ابي اتواصل",
    "ابغى اكلم",
    "ابغى اتواصل",
    "اريد التواصل",
    "ارفع طلب",
    "ابي ارفع طلب",
    "اشتكي",
    "اقدم شكوى",
)

_OWNER_NOUN_TOKENS_NORM = tuple(
    normalize_arabic_text(t) for t in _OWNER_NOUN_TOKENS if t
)
_OWNER_VERB_TOKENS_NORM = tuple(
    normalize_arabic_text(t) for t in _OWNER_VERB_TOKENS if t
)

_OWNER_ENGLISH_PHRASES = (
    "talk to the owner",
    "speak to the owner",
    "talk to management",
    "speak to management",
    "contact the owner",
    "contact management",
    "shop owner",
    "store owner",
    "the owner",
    "the management",
)


def is_owner_contact_request(text: Optional[str]) -> bool:
    """Return True iff the message is an explicit ask to contact the
    OWNER / MANAGEMENT / SHOP-OWNER / SUPERVISOR.

    Pure-string check, intentionally narrow:
      * BOTH a contact/escalation verb AND an owner-noun token must
        appear in the normalised message (Arabic),
      * OR one of the high-precision English phrases below appears.

    Caller pattern (webhook): treat the message as a handoff (uses the
    ``is_handoff_request`` plumbing — needs_human, handoff_active,
    paused AI, merchant inbox alert) AND override the acknowledgement
    text to one of the tier-specific copies. See
    ``classify_owner_escalation_tier`` for the routing.

    Never raises.
    """
    norm = normalize_arabic_text(text)
    if not norm:
        return False

    # English short-circuit — phrase library is high-precision.
    for phrase in _OWNER_ENGLISH_PHRASES:
        if phrase and phrase in norm:
            return True

    # Arabic: require BOTH a contact/escalation verb AND an owner
    # noun-token in the same normalised message. We deliberately do
    # NOT enforce strict ordering or proximity — Saudi/Gulf phrasings
    # interleave verbs and nouns liberally ("أبي أتواصل مع المالك",
    # "المالك أبي أكلمه", "ودي مع المالك أحكي").
    has_owner_noun = any(t and t in norm for t in _OWNER_NOUN_TOKENS_NORM)
    if not has_owner_noun:
        return False
    has_contact_verb = any(v and v in norm for v in _OWNER_VERB_TOKENS_NORM)
    return has_contact_verb


# ── Owner-contact escalation TIERS (May 2026 #44) ──────────────────
#
# Merchant feedback after the May 2026 #43 polish was that pausing
# the AI on EVERY owner-contact request was over-aggressive: a vague
# "أبي أكلم المالك" with no reason should not freeze the conversation
# while the merchant tracks down the customer's intent. The merchant
# specified four behavioural tiers:
#
#   1. **VAGUE owner request** ("أبي أكلم المالك" with no reason) →
#      send the clarifier ack, set ``needs_human=True`` so the
#      merchant inbox sees the entry, but DO NOT flip
#      ``handoff_active`` / ``status="human"`` and DO NOT pause the
#      AI. The next customer message can keep flowing through the
#      Brain (e.g. they answer the clarifier and the AI helps with
#      whatever they actually wanted).
#
#   2. **CLEAR owner / management request** ("أبي أكلم المالك بخصوص
#      الدفع" / a follow-up that has substance beyond the bare verb-
#      noun pair) → flip the FULL handoff plumbing, alert the
#      merchant, BUT keep AI alive so the customer can ask shipping/
#      product questions while waiting for the human. The ack
#      acknowledges the forwarding without pretending the AI is
#      stepping aside completely.
#
#   3. **COMPLAINT / sensitive case** ("احتيال", "غش", "ابي
#      استرجاع", "اشتكي عليكم" …) → full handoff + PAUSE AI. The
#      AI must NOT keep "selling" while a customer is escalating a
#      grievance. The ack is apologetic and pinned to a "نراجع
#      الموضوع فورًا" promise.
#
#   4. **Owner phone share** stays an explicit, manual decision in
#      the merchant inbox — never auto-shared by the AI on the first
#      contact request. (No code change needed; reasserted here as a
#      design principle so a future commit doesn't accidentally
#      add an "ASSET_OWNER_PHONE" auto-attach for this path.)

OWNER_TIER_VAGUE     = "owner_vague"
OWNER_TIER_CLEAR     = "owner_clear"
OWNER_TIER_COMPLAINT = "owner_complaint"


# Complaint / refund / sensitive-case phrase library. Curated from
# Tenant 33 production transcripts + a small set of common Saudi
# escalation idioms. Substring-matched against the normalised text
# so dialect variants ("ارجاع" / "استرجاع" / "استرداد") all hit.
_COMPLAINT_PHRASES = (
    # Fraud / dishonesty accusations
    "غش",
    "احتيال",
    "نصب",
    "نصابين",
    "خدعتوني",
    "خدعتموني",
    "اخدعتوني",
    "كذبتوا",
    "كذبتم",
    "ضحكتوا علي",
    "ضحكتم علي",
    "سرقه",
    "سرقتوني",
    "حرامي",
    "حرامية",
    # Religious / moral framing of grievance
    "حرام عليكم",
    "حرام عليكوا",
    "ما تستحون",
    "ما تخافون الله",
    "والله ظلم",
    "ظلم",
    "مظلوم",
    # Refund / return / cancel-paid
    "ابي ارد",
    "ابي ارجع",
    "ابي استرد",
    "ابي استرداد",
    "ابي استرجاع",
    "ابي استرجع",
    "ارجاع المنتج",
    "ارجاع الطلب",
    "استرجاع المنتج",
    "استرجاع الطلب",
    "استرداد المبلغ",
    "استرداد الفلوس",
    "ابي فلوسي",
    "ابي رد فلوسي",
    "ابي ارجع فلوسي",
    "ابغى استرد",
    "ابغى ارجاع",
    # Formal complaint / threat to escalate externally
    "اشتكي",
    "بشتكي",
    "بشكي",
    "اشكي",
    "مشتكي",
    "مشكتي",
    "شكوى",
    "شكوي",
    "بقدم شكوى",
    "ارفع شكوى",
    "ابي اشتكي",
    "ابغى اشتكي",
    "حقوق المستهلك",
    "حقوقي",
    "بشكيكم",
    "نشتكي عليكم",
    "نشكيكم",
    "هيئة المستهلك",
    "وزارة التجارة",
    "بلغ عنكم",
    "ابلغ عنكم",
    # English fallbacks
    "scam",
    "fraud",
    "refund",
    "complaint",
    "report you",
    "i want my money back",
)

_COMPLAINT_PHRASES_NORM = tuple(
    normalize_arabic_text(p) for p in _COMPLAINT_PHRASES if p
)


def is_complaint_signal(text: Optional[str]) -> bool:
    """Return True when the message reads as a COMPLAINT / refund
    request / sensitive grievance — not just a polite handoff.

    Pure-string check; never raises. Caller pattern: when this fires
    INSIDE an owner-contact context the webhook routes the turn to
    the COMPLAINT tier (apologetic ack + pause AI). When it fires
    OUTSIDE owner-contact context the brain still gets the turn and
    can decide on the right response — we deliberately do NOT
    auto-escalate every "ابي ارجاع" customer because some of those
    are genuine post-purchase logistics questions the brain can
    answer without humans.
    """
    norm = normalize_arabic_text(text)
    if not norm:
        return False
    for phrase in _COMPLAINT_PHRASES_NORM:
        if phrase and phrase in norm:
            return True
    return False


# MULTI-WORD pleasantries / fillers — stripped via substring scan
# BEFORE tokenisation. We keep these intentionally short and high-
# precision so substring strip can't fragment unrelated text.
_OWNER_RESIDUE_MULTI_FILLERS = tuple(normalize_arabic_text(t) for t in (
    "السلام عليكم", "وعليكم السلام", "السلام عليكم ورحمه الله",
    "لو سمحت", "لو سمحتي", "لو سمحتم",
    "من فضلك", "من فضلكم", "من فضلكي",
))

# SINGLE-WORD fillers — matched on whole-word boundaries during the
# tokenised pass (NOT via raw substring replace). Keeping single
# Arabic prepositions ("ل", "ب", "من") in this list was the bug
# that chopped "السلام" into "س‍ا م" — so they live here, not in a
# substring-replace list.
_OWNER_RESIDUE_FILLER_WORDS = frozenset(normalize_arabic_text(t) for t in (
    "ابي", "ابغى", "ابغا", "اريد", "احتاج", "محتاج",
    "ودي", "بدي", "ممكن", "رجاء", "رجاءا",
    "مع", "ل", "لي", "للـ", "بـ", "ب", "من", "الى", "إلى",
    "حاب", "حابب", "حابه", "اللي", "لو", "كذا",
    "وش", "شو", "ايش", "اش",
    "اهلا", "مرحبا", "شكرا", "شكرا",
    "السلام", "عليكم",      # individual halves of greetings
    "ورحمه", "الله", "وبركاته",
))


# Pre-computed splits of the verb / noun token sets into single-word
# vs multi-word forms. Multi-word entries ("صاحب المحل" / "ودي اكلم")
# must be substring-stripped FIRST because per-token filtering can't
# match a phrase that spans two whitespace-separated words.
_OWNER_TOKENS_MULTI_WORD = tuple(
    t for t in (_OWNER_VERB_TOKENS_NORM + _OWNER_NOUN_TOKENS_NORM)
    if t and " " in t
)
_OWNER_TOKENS_SINGLE_WORD = tuple(
    t for t in (_OWNER_VERB_TOKENS_NORM + _OWNER_NOUN_TOKENS_NORM)
    if t and " " not in t
)


def _owner_request_residue(text: str) -> str:
    """Return the customer's substantive residue AFTER stripping the
    owner-verb / owner-noun pair plus generic boilerplate fillers.

    Algorithm (revised May 2026 #44):

      1. Strip MULTI-WORD owner verbs / nouns / pleasantries via
         substring scan. These are high-precision phrases
         ("صاحب المحل" / "السلام عليكم" / "ودي اكلم") that don't
         fragment unrelated words because they all carry whitespace
         themselves.
      2. Tokenise on whitespace.
      3. Per token, drop when:
           * The token CONTAINS a single-word owner-verb / owner-
             noun substring (handles preposition prefixes like
             "للمالك" / "للمسؤول" / "بالمالك" — they all match
             "المالك" / "المسؤول" by ``tok in word``), OR
           * The token equals a single-word filler ("ابي" / "مع" /
             "ل" / …) — single Arabic letters like "ل" only get
             stripped here (where they're whole-word), never via
             substring replace (which would chop "السلام" into
             "اسام").
      4. Join the survivors. Empty residue → caller infers VAGUE.

    Why the previous version was wrong: substring-replacing single-
    letter Arabic prepositions ("ل", "ب", "من") chopped "السلام
    عليكم" into "ا سام عيكم" — those fragments survived as 4-char
    "alpha residue" and pushed the bare salam-prefixed turn into
    CLEAR tier instead of VAGUE.
    """
    norm = normalize_arabic_text(text)
    if not norm:
        return ""

    # 1. Strip multi-word phrases (owner phrases + pleasantries).
    residue = norm
    for phrase in _OWNER_TOKENS_MULTI_WORD + _OWNER_RESIDUE_MULTI_FILLERS:
        if phrase:
            residue = residue.replace(phrase, " ")

    # 2 + 3. Tokenise + per-token strip.
    kept = []
    for word in residue.split():
        if not word:
            continue
        # Drop if the token CONTAINS a single-word owner-verb /
        # owner-noun substring. This catches:
        #   * the bare token itself ("المالك"),
        #   * Arabic preposition prefixes ("للمالك" / "بالمالك"),
        #   * pronoun suffixes ("اكلمه" — though uncommon).
        if any(tok in word for tok in _OWNER_TOKENS_SINGLE_WORD):
            continue
        # Drop if the token equals a single-word filler.
        if word in _OWNER_RESIDUE_FILLER_WORDS:
            continue
        kept.append(word)

    return " ".join(kept)


# Minimum word-character count in the residue for the message to
# count as "carrying a reason". Empirically calibrated on Tenant 33:
#   * "أبي أكلم المالك"                  → residue "" / 0 chars  → VAGUE
#   * "أبي أكلم المالك بخصوص الدفع"      → residue "بخصوص الدفع" / 9 chars → CLEAR
#   * "أبي أكلم المالك السلام عليكم"     → residue ""             → VAGUE
#   * "أبي اتواصل مع المالك مشكلة طلبي"  → residue "مشكله طلبي"   → CLEAR
_OWNER_REASON_MIN_WORD_CHARS = 5


def classify_owner_escalation_tier(text: Optional[str]) -> str:
    """Tier the owner-contact request by severity / substance.

    Returns one of:
      * ``OWNER_TIER_COMPLAINT`` — message carries a complaint /
        refund / sensitive-case signal. Highest priority — overrides
        substance check because "احتيال" alone is enough to demand
        an apologetic ack + AI pause even without a long explanation.
      * ``OWNER_TIER_CLEAR``     — owner-contact + a stated reason
        (substantive residue ≥ 5 word chars after stripping the
        verb/noun pair and boilerplate). Webhook flips full handoff
        flags but keeps AI alive.
      * ``OWNER_TIER_VAGUE``     — bare owner-contact phrasing with
        no stated reason. Webhook sends the clarifier ack and a
        soft ``needs_human=True`` flag, AI stays alive.

    Caller is responsible for ensuring ``is_owner_contact_request``
    fired first — passing a non-owner-contact message returns
    ``OWNER_TIER_VAGUE`` by default.

    Never raises.
    """
    if not text:
        return OWNER_TIER_VAGUE

    # Complaint signal trumps substance — even bare "احتيال" goes
    # straight to the highest tier.
    if is_complaint_signal(text):
        return OWNER_TIER_COMPLAINT

    residue = _owner_request_residue(text)
    word_chars = sum(1 for c in residue if c.isalpha())
    if word_chars >= _OWNER_REASON_MIN_WORD_CHARS:
        return OWNER_TIER_CLEAR
    return OWNER_TIER_VAGUE


def is_handoff_request(text: Optional[str]) -> bool:
    """Return True when the inbound text is an unambiguous request
    to be transferred to a human agent.

    The check is intentionally conservative — we'd rather miss a
    rare phrasing (the brain still classifies normally) than escalate
    a customer who said "أنا موظف لدى…" or "حول لي الفاتورة".

    Never raises.
    """
    norm = normalize_arabic_text(text)
    if not norm:
        return False

    # Exact whole-message handoff (e.g. bare "كلموني").
    if norm in _EXACT_HANDOFF_PHRASES:
        return True

    # Substring scan against the high-precision phrase library.
    matched = False
    for phrase in _SUBSTRING_HANDOFF_PHRASES:
        if phrase and phrase in norm:
            matched = True
            break

    if not matched:
        return False

    # ARCH-HANDOFF-001 — align with rules gate: «هل يوجد أحد يقدر…» is
    # service availability, not a handoff request.
    try:
        from modules.ai.brain.intent.service_availability_gate import (  # noqa: PLC0415
            is_service_availability_inquiry,
        )
        if is_service_availability_inquiry(text or ""):
            return False
    except Exception:  # noqa: BLE001
        pass

    return True


# Single-line handoff acknowledgement. Kept short and tone-safe; no
# promises about timing other than "soon". The same wording is used
# by the pre-brain guard and the outer-exception guard so the
# customer experience is identical across the two paths.
HANDOFF_ACK_TEXT_AR = "تمام، راح يتواصل معك أحد فريقنا في أقرب وقت 🌷"


# Owner-contact acknowledgement (May 2026 #42 + #43 polish).
#
# Used by the pre-brain handoff guard ONLY when the inbound also
# matches ``is_owner_contact_request`` — i.e. the customer specifically
# asked to talk to the OWNER / MANAGEMENT / SHOP-OWNER, not just any
# staff member.
#
# May 2026 #43 polish — merchant feedback on Tenant 33 was that the
# initial wording ("ممكن توضح لي سبب التواصل مع المالك؟ وراح أرفع
# طلبك للإدارة/المسؤول المناسب") was technically correct but felt
# "support-gateway" formal — closer to a corporate ticketing form
# than a Saudi WhatsApp store conversation. The new copy:
#
#   "أكيد 🌷
#    وش الطلب أو المشكلة اللي حاب توصله للمالك؟ وبرفعه للمسؤول
#    المناسب مباشرة."
#
# Why this wording works better:
#   * "وش الطلب أو المشكلة" reads as Saudi spoken Arabic, not MSA.
#     The customer is more likely to volunteer the actual reason
#     instead of typing "أبي أتواصل" again.
#   * Two concrete buckets ("الطلب أو المشكلة") gently nudge the
#     customer to commit to one shape — easier for the merchant to
#     triage than an open "ما هو سبب التواصل؟".
#   * Newline after "أكيد 🌷" gives the eye a beat — the
#     acknowledgement reads like a warm answer, not a paragraph.
#   * "مباشرة" ends with action: we are not punting the customer
#     through layers of approval, we are forwarding immediately.
#
# Functional contract is unchanged: the customer-facing line ships
# alongside the SAME plumbing as before — needs_human + handoff_active
# flipped, AI paused for the conversation, merchant inbox sees the
# "طلب موظف" entry. Only the wording moved.
HANDOFF_OWNER_ACK_TEXT_AR = (
    "أكيد 🌷\n"
    "وش الطلب أو المشكلة اللي حاب توصله للمالك؟ "
    "وبرفعه للمسؤول المناسب مباشرة."
)


# CLEAR-tier ack (May 2026 #44).
#
# Used when the customer EXPLICITLY asked to talk to the owner AND
# already stated a reason (substantive residue ≥ 5 word chars). The
# webhook flips the full handoff plumbing in this tier — the merchant
# inbox sees a "طلب موظف" entry — but the AI is intentionally NOT
# paused. The customer can keep asking parallel questions
# (shipping / product / payment) and the brain will respond, while
# the merchant prepares to address the owner-level request offline.
#
# Wording:
#   * Acknowledges the forwarding without lying about timing.
#   * Echoes the customer's framing ("طلبك" — keeps the customer
#     feeling heard, doesn't recast their issue as something else).
#   * Leaves the door open ("لو حابة تسألين عن شي ثاني، أنا هنا")
#     so the customer doesn't feel they have to wait silently —
#     critical because the AI is still active.
HANDOFF_OWNER_HANDOFF_TEXT_AR = (
    "تمام 🌷 وصلني طلبك ورفعته للمسؤول المناسب.\n"
    "لو حابة تسألين عن شي ثاني — توصيل، منتج، دفع — أنا هنا."
)


# COMPLAINT-tier ack (May 2026 #44).
#
# Fires only when the customer's message carries a complaint /
# refund / sensitive-case signal AND is also an owner-contact
# request. The AI is paused in this tier so we don't keep "selling"
# while a grievance is open.
#
# Wording:
#   * Apologetic, no defensiveness, no "هذا مو من اختصاصي".
#   * Honest about the next step ("نراجع الموضوع فورًا") without
#     promising an outcome the merchant hasn't approved (refund /
#     compensation).
#   * No CTA — the conversation belongs to the human now.
HANDOFF_OWNER_COMPLAINT_TEXT_AR = (
    "نعتذر منك 🌷\n"
    "وصلني الموضوع ورفعته للمسؤول المباشر، وراح يتواصل معك "
    "في أقرب وقت لمراجعة الموضوع وإيجاد الحل المناسب."
)


# ── Post-payment modification detector ──────────────────────────────
#
# When a customer has ALREADY paid (``payment_receipt_received=True``
# or ``order_status in {under_review, processing}``) and then asks to
# add a product, remove an item, change quantity, swap a variant, or
# cancel — the bot used to either ignore the request entirely or push
# the customer back through the product-search flow as if no order
# existed. Both behaviours erode trust.
#
# Our policy is conservative: any post-payment modification request
# triggers a human handoff so the merchant can manually:
#   * recompute the total and any delta payment,
#   * re-confirm the shipping window,
#   * decide whether to refund / partially refund,
#   * decide if the modification is even feasible at the prep stage.
#
# The detector below is INTENT only — it does not look at state. The
# webhook caller is responsible for combining this signal with the
# post-payment context check.

_SUBSTRING_POST_PAYMENT_MODIFICATION_PHRASES = tuple(
    normalize_arabic_text(p) for p in (
        # Add another product to the order
        "ابي اضيف",
        "ابغى اضيف",
        "اريد اضيف",
        "ابي اضيف منتج",
        "ابغى اضيف منتج",
        "ضيف لي",
        "ضيفي لي",
        "ضيفوا لي",
        "اضيف شي",
        "اضافه منتج",
        "اضافة منتج",
        "اضافه طلب",
        "اضافة طلب",
        "اضف عليه",
        "اضف للطلب",
        "اضفه للطلب",
        "ابي اطلب معه",
        "ابغى اطلب معه",
        # Modify quantity / variant / size
        "ابي اعدل",
        "ابغى اعدل",
        "اريد اعدل",
        "اعدل الطلب",
        "اعدل طلب",
        "اعدل طلبي",
        "تعديل الطلب",
        "تعديل طلب",
        "تعديل طلبي",
        "ابي اغير",
        "ابغى اغير",
        "اغير الطلب",
        "اغير طلبي",
        "اغير المنتج",
        "غيروا لي",
        "غير لي الطلب",
        "بدل المنتج",
        "بدلوا لي",
        "ابي ابدل",
        "ابغى ابدل",
        "زيد الكميه",
        "زيد الكمية",
        "زود الكميه",
        "زود الكمية",
        "زيدي الكميه",
        "نقص الكميه",
        "نقص الكمية",
        "قلل الكميه",
        "قلل الكمية",
        # Remove / cancel parts of the order
        "ابي احذف",
        "ابغى احذف",
        "احذف منتج",
        "احذفوا",
        "الغي المنتج",
        "الغي طلب",
        "الغي الطلب",
        "إلغاء الطلب",
        "الغاء الطلب",
        "ابي الغي",
        "ابغى الغي",
        "تراجع عن الطلب",
        "ما ابي الطلب",
        "ما ابغى الطلب",
        # English fallbacks
        "add another",
        "add one more",
        "modify my order",
        "change my order",
        "cancel my order",
        "remove item",
        "increase quantity",
        "decrease quantity",
    )
)


def is_post_payment_modification_request(text: Optional[str]) -> bool:
    """Return True when the inbound text reads as an explicit request
    to add / modify / cancel a product line on an order that has
    already been paid for.

    Pure-string check. Caller is responsible for confirming the
    conversation is in a post-payment state before acting on the
    signal — see ``HANDOFF_POST_PAYMENT_ACK_TEXT_AR``.

    Never raises.
    """
    norm = normalize_arabic_text(text)
    if not norm:
        return False
    for phrase in _SUBSTRING_POST_PAYMENT_MODIFICATION_PHRASES:
        if phrase and phrase in norm:
            return True
    return False


# Acknowledgement for the post-payment modification handoff branch.
# Slightly different from the plain handoff wording so the customer
# knows their amendment will be reviewed by a human — not just
# "someone will reach out" generically.
HANDOFF_POST_PAYMENT_ACK_TEXT_AR = (
    "وصلنا طلبك بالتعديل على الطلب المدفوع 🌷 "
    "راح يتواصل معك أحد فريقنا قريباً لمراجعة التعديل وإكماله."
)


# ─────────────────────────────────────────────────────────────────────
# Handoff pause policy (May 2026 #46 — Tenant 33)
# ─────────────────────────────────────────────────────────────────────
#
# Background: in May-2026 production we observed that customers who
# typed "أبي أتواصل مع المالك" — even with the gentle clarifier ack —
# stopped getting AI replies for the rest of the session. The cause
# was the pre-brain handoff guard calling ``pause_ai`` on every tier
# except VAGUE, plus several other handoff branches (loop pause,
# brain-side handoff, support-escalation, outer-exception handoff)
# also flipping ``ai_paused = True``. The customer would then ask
# perfectly normal questions ("ايش طرق التوصيل؟"، "كم سعر الكيلو؟")
# and the conversation would be silent until staff stepped in.
#
# Merchant policy (Tenant 33, May 2026 #46): the AI must NOT pause
# itself based on customer-side handoff/escalation signals. Only
# manual pause from the staff dashboard should silence replies.
# Tags (``needs_human`` / ``handoff_active`` / owner tier / staff
# notification) remain useful so the dashboard surfaces the request,
# but they are advisory — not a kill-switch.
#
# This helper is the SINGLE place that maps an escalation tier to
# the pause/flip plumbing the webhook should perform. Inlining the
# decision here keeps the webhook short and makes the policy
# trivially unit-testable.
#
# IMPORTANT — this policy ONLY governs AI pause/handoff plumbing for
# customer-side escalation requests. It does NOT change:
#   * manual pause from the dashboard (``pause_ai`` is still callable)
#   * loop-guard pause when the customer side itself looks automated
#   * internal-number / blocklist pause
#   * rate-limit pause
# Those are self-protective mechanisms unrelated to handoff intent.

# Tier label used when the customer asked for a generic human ("ابي
# اتكلم مع موظف") without owner-specific framing. The pre-brain
# handoff guard hands this to the resolver as the default.
GENERIC_HANDOFF_TIER = "generic_handoff"


def resolve_handoff_pause_policy(
    tier: Optional[str],
) -> Dict[str, bool]:
    """Map an escalation tier to the (do_full_flip, do_create_session,
    do_pause_ai) tuple the webhook applies after sending the ack.

    Returns a plain dict with three boolean keys so the call site
    keeps its current shape:

        policy = resolve_handoff_pause_policy(tier)
        _do_full_handoff_flip = policy["do_full_handoff_flip"]
        _do_create_session    = policy["do_create_session"]
        _do_pause_ai          = policy["do_pause_ai"]

    Tier semantics (May 2026 #46 policy):

      * VAGUE              — clarifier ack only. Soft ``needs_human``
                             flag, no full flip, no session, AI alive.
      * CLEAR              — full flip + session so the dashboard
                             shows a real "طلب موظف" entry. AI alive.
      * COMPLAINT          — full flip + session so staff sees a
                             priority entry. AI alive (the customer
                             may keep asking unrelated questions
                             while staff prepares to follow up).
      * GENERIC            — full flip + session. AI alive.
      * unknown / None     — falls back to GENERIC behaviour.

    Per Tenant 33 #46 — every tier returns ``do_pause_ai=False``.
    Manual pause from the dashboard is the ONLY path that flips
    ``Conversation.ai_paused``.
    """
    tier_norm = (tier or "").strip() or GENERIC_HANDOFF_TIER

    if tier_norm == OWNER_TIER_VAGUE:
        return {
            "do_full_handoff_flip": False,
            "do_create_session":    False,
            "do_pause_ai":          False,
        }

    # CLEAR / COMPLAINT / GENERIC / unknown — all share the same
    # plumbing now: surface to staff but never silence the AI.
    return {
        "do_full_handoff_flip": True,
        "do_create_session":    True,
        "do_pause_ai":          False,
    }


__all__ = [
    "GENERIC_HANDOFF_TIER",
    "HANDOFF_ACK_TEXT_AR",
    "HANDOFF_OWNER_ACK_TEXT_AR",
    "HANDOFF_OWNER_COMPLAINT_TEXT_AR",
    "HANDOFF_OWNER_HANDOFF_TEXT_AR",
    "HANDOFF_POST_PAYMENT_ACK_TEXT_AR",
    "OWNER_TIER_CLEAR",
    "OWNER_TIER_COMPLAINT",
    "OWNER_TIER_VAGUE",
    "classify_owner_escalation_tier",
    "is_complaint_signal",
    "is_handoff_request",
    "is_owner_contact_request",
    "is_post_payment_modification_request",
    "normalize_arabic_text",
    "resolve_handoff_pause_policy",
]
