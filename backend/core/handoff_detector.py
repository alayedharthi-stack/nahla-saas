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
from typing import Iterable, Optional


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
    # "Is anybody there / answering?"
    "في احد يرد علي",
    "فيه احد يرد علي",
    "هل في احد",
    "هل يوجد احد",
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
    text to ``HANDOFF_OWNER_ACK_TEXT_AR``.

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
    for phrase in _SUBSTRING_HANDOFF_PHRASES:
        if phrase and phrase in norm:
            return True

    return False


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


__all__ = [
    "HANDOFF_ACK_TEXT_AR",
    "HANDOFF_OWNER_ACK_TEXT_AR",
    "HANDOFF_POST_PAYMENT_ACK_TEXT_AR",
    "is_handoff_request",
    "is_owner_contact_request",
    "is_post_payment_modification_request",
    "normalize_arabic_text",
]
