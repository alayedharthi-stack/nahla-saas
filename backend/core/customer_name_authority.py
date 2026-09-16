"""
core/customer_name_authority.py
───────────────────────────────
THE canonical customer-name authority.

Single source of truth for the question "what is this customer's name,
and who says so?". ``core.customer_identity_resolver.apply_customer_name``
is the only writer of ``Customer.name`` for automatic sources, and it
applies **only** the decision returned by ``resolve_canonical_customer_name``
below. Nothing else decides precedence.

Authority ladder (strictly ordered, highest first)
==================================================

    VERIFIED_ECOMMERCE        Salla / Zid / Shopify customer or order
                              payloads. The merchant's store is the
                              system of record for who the buyer is.
    CUSTOMER_SELF_REPORTED    The customer stated their own name with
                              explicit evidence ("اسمي محمد", a direct
                              answer to Nahlah's own "ما اسمك؟", or an
                              explicit correction).
    WHATSAPP_PROFILE          The WhatsApp profile display string,
                              classified PERSON_NAME. Becomes the
                              customer's canonical/display name when
                              nothing stronger exists — but stays
                              operationally low-trust (never used for
                              shipping/invoices; see STATUS_PROPOSED).
    UNKNOWN                   Nothing trustworthy is known.

Rules the ladder enforces (AGENTS.md: operational facts are deterministic):

  * A lower authority NEVER overwrites a higher one.
  * An equal authority may refresh (stores correct their own data), with
    one guard: a validated self-report is replaced only by an explicit
    correction or a direct answer to Nahlah's question, never by a bare
    restatement in passing.
  * A BLANK value never erases a stored name, at any authority.
  * A stored name of UNKNOWN provenance (legacy rows, CSV imports) is
    protected from WHATSAPP_PROFILE but yields to self-report / store.
  * Merchant / manual override is NOT a rung on this ladder. It is an
    orthogonal LOCK above the whole ladder, preserved verbatim from the
    pre-existing ``manual_name_override`` / ``manual_name_cleared``
    semantics: a locked name is untouchable; a merchant-cleared name is
    refilled only by self-report or store, never by a profile hint.

Authority is derived from the resolver's already-normalised canonical
source (``normalize_identity_source``), so there is exactly one
source→trust map in the platform, not two that drift.

WhatsApp profile classification
===============================

    PERSON_NAME       Confidently a human name → may become canonical
                      at WHATSAPP_PROFILE authority.
    NOT_PERSON_NAME   Confidently not a name (devotional/status phrases,
                      commerce text, role labels, cities) → dropped.
    AMBIGUOUS         Could be a name, could be a word. Retained as a
                      non-canonical hint only. Never displayed as the
                      customer's name, never used operationally.

Deliberately asymmetric and deliberately conservative for single
tokens: Arabic given names are very often ordinary words (دعاء =
supplication, نور = light, جود = generosity, أمل = hope, وعد = promise).
From a profile string alone those are AMBIGUOUS — the customer saying
"اسمي دعاء" or the store saying "دعاء محمد العتيبي" resolves it. A
single token is PERSON_NAME only under a deterministic rule: it is in
the name-only lexicon (no common-noun/adjective reading), or it is a
theophoric compound (عبد + name of God). No LLM is involved anywhere.

Devotional detection is phrase-STRUCTURAL (a devotional head token, or a
trailing الله/لله without an onomastic head), never "contains a
religious-looking token" — which is what keeps عبد الله / أبو عبدالله
classified as person names while الحمد لله is not.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

logger = logging.getLogger("nahla.customer_name_authority")


# ══════════════════════════════════════════════════════════════════════
# Authority ladder
# ══════════════════════════════════════════════════════════════════════

class NameAuthority(IntEnum):
    """Ordered authority levels. Compare with ``>`` / ``>=``."""

    UNKNOWN = 0
    WHATSAPP_PROFILE = 10
    CUSTOMER_SELF_REPORTED = 50
    VERIFIED_ECOMMERCE = 100

    @property
    def label(self) -> str:
        return self.name


AUTHORITY_UNKNOWN = "UNKNOWN"
AUTHORITY_WHATSAPP_PROFILE = "WHATSAPP_PROFILE"
AUTHORITY_CUSTOMER_SELF_REPORTED = "CUSTOMER_SELF_REPORTED"
AUTHORITY_VERIFIED_ECOMMERCE = "VERIFIED_ECOMMERCE"

# Provenance label for names written by the merchant lock path. It is
# NOT a ladder rung — ``authority_from_label`` maps it to UNKNOWN and
# the ``merchant_locked`` flag carries the precedence.
MERCHANT_OVERRIDE_LABEL = "MERCHANT_OVERRIDE"

_AUTHORITY_BY_NAME: Dict[str, NameAuthority] = {a.name: a for a in NameAuthority}


def authority_for_canonical_source(canonical_source: Optional[str]) -> NameAuthority:
    """
    Map a *canonical* resolver source onto the ladder.

    Canonical sources are the outputs of
    ``customer_identity_resolver.normalize_identity_source``; this is the
    only source→authority map in the platform.
    """
    src = (canonical_source or "").strip().lower()
    if src in {"salla_order", "zid_order", "shopify_order"}:
        return NameAuthority.VERIFIED_ECOMMERCE
    if src == "customer_message":
        return NameAuthority.CUSTOMER_SELF_REPORTED
    if src == "whatsapp_profile":
        return NameAuthority.WHATSAPP_PROFILE
    # merchant_correction / manual_admin arrive through the lock path and
    # are represented by ``merchant_locked``, not by a rung. Anything
    # else (manual_import, widget, tracking_lead, unknown) is UNKNOWN.
    return NameAuthority.UNKNOWN


def authority_for_source(
    source: Optional[str],
    *,
    platform: Optional[str] = None,
    explicit_customer_entry: bool = False,
) -> NameAuthority:
    """Map a raw caller source string onto the ladder via normalisation."""
    src = (source or "").strip().lower()
    if not src and not explicit_customer_entry:
        return NameAuthority.UNKNOWN
    from core.customer_identity_resolver import (  # noqa: PLC0415
        normalize_identity_source,
    )

    canonical, _status, _conf = normalize_identity_source(
        src,
        platform=platform,
        explicit_customer_entry=explicit_customer_entry,
    )
    # normalize_identity_source() falls back to whatsapp_profile for
    # unknown non-empty strings; only a genuine WhatsApp source may carry
    # WHATSAPP_PROFILE authority.
    if canonical == "whatsapp_profile" and src not in _WHATSAPP_PROFILE_SOURCES:
        return NameAuthority.UNKNOWN
    return authority_for_canonical_source(canonical)


_WHATSAPP_PROFILE_SOURCES: FrozenSet[str] = frozenset({
    "whatsapp_profile", "whatsapp_inbound", "whatsapp_lead",
})


def authority_from_label(label: Optional[str]) -> NameAuthority:
    """Parse a persisted authority label back into the enum."""
    return _AUTHORITY_BY_NAME.get(
        str(label or "").strip().upper(), NameAuthority.UNKNOWN,
    )


# ══════════════════════════════════════════════════════════════════════
# WhatsApp profile classification
# ══════════════════════════════════════════════════════════════════════

PERSON_NAME = "PERSON_NAME"
NOT_PERSON_NAME = "NOT_PERSON_NAME"
AMBIGUOUS = "AMBIGUOUS"


_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF\U0001F600-\U0001F64F\U00002600-\U000027BF]+",
    flags=re.UNICODE,
)
_DIGIT_RE = re.compile(r"\d")
_NON_LETTER_RE = re.compile(r"[^\w؀-ۿݐ-ݿ]+", flags=re.UNICODE)


def _normalize_arabic(token: str) -> str:
    """Strip diacritics/tatweel and fold orthographic variants."""
    t = (token or "").strip()
    t = re.sub(r"[ً-ٰٟـ]", "", t)
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )
    return t.lower()


def _normalize_full(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _letter_tokens(text: str) -> List[str]:
    """Normalised letter-only tokens; punctuation and emoji removed."""
    cleaned = _NON_LETTER_RE.sub(" ", _EMOJI_RE.sub(" ", str(text or "")))
    return [
        _normalize_arabic(t)
        for t in cleaned.split()
        if t and not _DIGIT_RE.search(t) and _normalize_arabic(t)
    ]


# ── Religious / devotional phrase structure ──────────────────────────

_DEVOTIONAL_HEADS: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    "الحمد", "الحمدلله", "استغفر", "استغفرالله", "سبحان", "سبحانه",
    "تبارك", "بارك", "جزاك", "جزاكم", "حسبي", "حسبنا", "اللهم",
    "توكلت", "توكلنا", "بسم", "ماشاء", "انشاء", "شكرا",
    "اشهد", "لاحول", "استغفري", "الشكر", "بحمد", "والحمد",
    "يارب", "ياالله", "اللهي", "ربي", "ربنا",
})

_DEVOTIONAL_BIGRAM_HEADS: FrozenSet[str] = frozenset(
    _normalize_arabic(t) for t in {"ما شاء", "ان شاء", "لا حول", "لا اله", "يا رب", "يا الله"}
)

# Heads that make a trailing الله onomastic rather than devotional.
_NAME_PREFIX_HEADS: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    "عبد", "عبيد", "ابو", "ابا", "ابي", "ام", "اما", "امي",
    "بن", "ابن", "بنت", "ابنه", "ال", "الشيخ",
})

_ALLAH_TAILS: FrozenSet[str] = frozenset(
    _normalize_arabic(t) for t in {"الله", "لله", "اللة", "اللهم"}
)

# ── Non-religious status / storefront phrases ────────────────────────
_STATUS_PHRASE_TOKENS: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    "متوفر", "مشغول", "مسافر", "نايم", "متاح", "مشغوله", "متوفره",
    "مغلق", "مفتوح", "اونلاين", "اوفلاين", "available", "busy", "online",
    "offline", "away", "working", "afk",
    "للبيع", "مبيعات", "عروض", "تخفيضات", "توصيل", "خدمة", "متجر",
    "محل", "مؤسسة", "مؤسسه", "شركة", "شركه", "مطعم", "كافيه",
})

# ── Single-token lexicons ─────────────────────────────────────────────
#
# NAME-ONLY: tokens with no ordinary common-noun / adjective reading in
# contemporary Arabic. A bare profile string equal to one of these is a
# person name under a deterministic rule. Additive-only; never used to
# reject anything.
_NAME_ONLY_SINGLE_TOKENS: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    # Masculine
    "محمد", "أحمد", "احمد", "خالد", "فهد", "إبراهيم", "ابراهيم", "يوسف",
    "عثمان", "فيصل", "نواف", "سعود", "طلال", "زياد", "حسين", "هشام",
    "مازن", "معاذ", "أنس", "انس", "راكان", "مشعل", "مروان", "أسامة",
    "اسامه", "اسامة", "متعب", "مساعد", "منصور", "عبدالله", "عبدالرحمن",
    "عبدالعزيز", "عبدالملك", "عبدالإله", "عبدالاله", "عبداله",
    # Feminine
    "فاطمة", "فاطمه", "عائشة", "عائشه", "مريم", "سارة", "ساره", "نورة",
    "نوره", "خديجة", "خديجه", "لمى", "لمي", "العنود", "تالا", "رتاج",
    "روان", "سلمى", "سلمي", "نوف", "ريما", "موضي", "مضاوي", "وضحى",
    "وضحي", "شيماء", "زينب", "رقية", "رقيه", "سمية", "سميه",
})

# POLYSEMOUS: real, common given names that are ALSO ordinary words.
# From a profile string alone these are AMBIGUOUS — never rejected,
# never canonicalised without stronger evidence. Explicitly enumerated
# so the classifier's reason is legible in logs and provenance.
_POLYSEMOUS_GIVEN_NAMES: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    "دعاء", "نور", "جود", "أمل", "امل", "وعد", "شهد", "ريم", "ندى",
    "ولاء", "صفاء", "رغد", "بيان", "لين", "حنين", "جنى", "جني",
    "إيمان", "ايمان", "عبير", "منال", "دلال", "هيا", "هند", "مها",
    "جوري", "حصة", "حصه", "لولوة", "لولوه", "رنا", "رزان", "جواهر",
    "دانة", "دانه", "غادة", "غاده", "أروى", "اروى", "بشاير", "لطيفة",
    "لطيفه", "منيرة", "منيره", "أسماء", "اسماء", "رهف", "ليان",
    "جمانة", "جمانه", "أفنان", "افنان", "سعد", "ماجد", "ناصر", "صالح",
    "سلطان", "بدر", "زيد", "عمر", "علي", "فارس", "ريان", "راشد",
    "وليد", "سامي", "رامي", "حسن", "يزيد", "غانم", "حارث", "مهند",
    "طارق", "أيمن", "ايمن", "بندر", "تركي", "جابر", "سالم", "سعيد",
    "كريم", "عادل", "جميل", "نبيل", "أمين", "امين", "شريف", "منير",
})


@dataclass(frozen=True)
class ProfileClassification:
    """Result of classifying a WhatsApp profile display string."""

    classification: str
    cleaned: str = ""
    reason: str = ""

    @property
    def is_person_name(self) -> bool:
        return self.classification == PERSON_NAME

    @property
    def is_rejected(self) -> bool:
        return self.classification == NOT_PERSON_NAME


def _is_devotional_phrase(norm_tokens: List[str]) -> bool:
    """Structural devotional-phrase test. See module docstring."""
    if not norm_tokens:
        return False
    head = norm_tokens[0]
    if head in _DEVOTIONAL_HEADS:
        return True
    if len(norm_tokens) >= 2 and f"{norm_tokens[0]} {norm_tokens[1]}" in _DEVOTIONAL_BIGRAM_HEADS:
        return True
    # "الحمد لله" → devotional; "عبد الله" → name. The head decides.
    if len(norm_tokens) >= 2 and norm_tokens[-1] in _ALLAH_TAILS and head not in _NAME_PREFIX_HEADS:
        return True
    return False


def _is_theophoric_compound(norm_token: str) -> bool:
    """عبدالله / عبدالرحمن written as one token."""
    return norm_token.startswith("عبد") and len(norm_token) >= 6


def classify_whatsapp_profile_name(raw: Optional[str]) -> ProfileClassification:
    """
    Classify a WhatsApp profile string as PERSON_NAME / NOT_PERSON_NAME /
    AMBIGUOUS. Deterministic; see module docstring for the rules.
    """
    if raw is None or not isinstance(raw, str):
        return ProfileClassification(NOT_PERSON_NAME, reason="empty")

    text = _normalize_full(_EMOJI_RE.sub(" ", raw))
    if not text:
        return ProfileClassification(NOT_PERSON_NAME, reason="empty")
    if _DIGIT_RE.search(text):
        return ProfileClassification(NOT_PERSON_NAME, reason="contains_digits")
    if len(text) > 60:
        return ProfileClassification(NOT_PERSON_NAME, reason="too_long")

    tokens = [t for t in text.split(" ") if t]
    if not tokens:
        return ProfileClassification(NOT_PERSON_NAME, reason="empty")
    if len(tokens) > 4:
        return ProfileClassification(NOT_PERSON_NAME, reason="token_count")

    norm_tokens = [_normalize_arabic(t) for t in tokens]

    # ── Hard rejects ─────────────────────────────────────────────────
    if _is_devotional_phrase(norm_tokens):
        return ProfileClassification(NOT_PERSON_NAME, reason="devotional_phrase")
    if any(t in _STATUS_PHRASE_TOKENS for t in norm_tokens):
        return ProfileClassification(NOT_PERSON_NAME, reason="status_phrase")

    try:
        from core.customer_name_validator import validate_customer_name  # noqa: PLC0415

        validation = validate_customer_name(text)
    except Exception:  # noqa: BLE001 — validator must never break ingestion
        logger.exception("[NAME_AUTHORITY] validator unavailable")
        return ProfileClassification(AMBIGUOUS, cleaned=text, reason="validator_error")

    if not validation.valid:
        return ProfileClassification(
            NOT_PERSON_NAME, reason=validation.reason or "validator_reject",
        )

    cleaned = validation.cleaned or text

    # ── Multi-token: name-shaped and survived every reject rule ──────
    if len(norm_tokens) >= 2:
        if norm_tokens[0] in _NAME_PREFIX_HEADS:
            return ProfileClassification(PERSON_NAME, cleaned=cleaned, reason="name_prefix")
        return ProfileClassification(
            PERSON_NAME, cleaned=cleaned, reason="multi_token_name_shape",
        )

    # ── Single token: deterministic positive rules only ──────────────
    token = norm_tokens[0]
    if _is_theophoric_compound(token):
        return ProfileClassification(PERSON_NAME, cleaned=cleaned, reason="theophoric_compound")
    if token in _NAME_ONLY_SINGLE_TOKENS:
        return ProfileClassification(PERSON_NAME, cleaned=cleaned, reason="name_only_lexicon")
    if token in _POLYSEMOUS_GIVEN_NAMES:
        return ProfileClassification(AMBIGUOUS, cleaned=cleaned, reason="polysemous_given_name")
    return ProfileClassification(AMBIGUOUS, cleaned=cleaned, reason="single_unknown_token")


# ══════════════════════════════════════════════════════════════════════
# Self-reported name evidence
# ══════════════════════════════════════════════════════════════════════

EVIDENCE_EXPLICIT_STATEMENT = "explicit_self_statement"    # "اسمي محمد"
EVIDENCE_DIRECT_ANSWER = "direct_answer_to_name_question"  # after "ما اسمك؟"
EVIDENCE_EXPLICIT_CORRECTION = "explicit_name_correction"  # "صحح اسمي" / completion

# Leading particles a customer may put before a bare name answer.
_ANSWER_LEAD_PARTICLES: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    "انا", "أنا", "اسمي", "إسمي", "الاسم", "اسم", "هو", "هي", "طيب",
    "اوك", "اوكي", "تمام", "حسنا", "حسناً", "نعم", "ايوه", "ايوة", "أيوه",
    "اهلا", "أهلا", "هلا", "مرحبا", "ابشر", "ابشري", "اكيد", "yes", "ok",
    "okay", "name", "my", "is", "i", "am", "im",
})
_ANSWER_TAIL_PARTICLES: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    "شكرا", "شكراً", "thanks", "thank", "you", "طيب", "تمام",
})


def is_bare_name_answer(text: Optional[str], candidate: Optional[str]) -> bool:
    """
    True when ``text`` is nothing but the candidate name, optionally
    wrapped in answer particles ("أنا محمد أحمد", "اسمي: محمد", "محمد 🙏").

    This is the deterministic guard behind direct-answer capture: even
    when Nahlah just asked for the name, "كلمت خالد" / "الهدية لسارة" /
    "أرسلها لمحمد" are sentences about other people, not answers.
    """
    cand = _letter_tokens(candidate or "")
    got = _letter_tokens(text or "")
    if not cand or not got:
        return False
    while got and got[0] in _ANSWER_LEAD_PARTICLES and len(got) > len(cand):
        got = got[1:]
    while got and got[-1] in _ANSWER_TAIL_PARTICLES and len(got) > len(cand):
        got = got[:-1]
    return got == cand


@dataclass(frozen=True)
class SelfReportEvidence:
    """Why we believe the customer stated their OWN name."""

    accepted: bool
    kind: str = ""
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


def _evidence_ref(ctx: Mapping[str, Any], inbound_text: str, **extra: Any) -> Dict[str, Any]:
    """Durable reference to the message that carried the evidence."""
    ref: Dict[str, Any] = {}
    for key in ("conversation_id", "message_id", "wa_message_id", "asked_slot", "name_capture_pattern"):
        val = ctx.get(key)
        if val not in (None, ""):
            ref[key] = val
    if inbound_text:
        ref["message_excerpt"] = inbound_text[:80]
    ref.update({k: v for k, v in extra.items() if v not in (None, "")})
    return ref


def evaluate_self_report_evidence(
    candidate: Optional[str],
    message_context: Optional[Mapping[str, Any]] = None,
) -> SelfReportEvidence:
    """
    Accept a self-reported name ONLY with explicit evidence.

      1. An explicit self-identification pattern in the inbound text
         ("اسمي محمد", "معك فهد") — ``core.customer_name_extractor``.
      2. A direct, bare answer to a name question Nahlah itself asked
         (``awaiting_name_answer`` set by the asking turn; never
         retro-inferred) — see ``is_bare_name_answer``.
      3. An explicit correction/completion of a stored name.

    Role/logistics context ("مندوب SMSA") and any third-party mention
    ("أرسلها لأخوي سعد") are rejected. No free-text inference.
    """
    name = str(candidate or "").strip()
    if not name:
        return SelfReportEvidence(False, reason="empty_candidate")

    ctx = dict(message_context or {})
    inbound_text = str(
        ctx.get("message") or ctx.get("inbound_text") or ctx.get("raw_message") or "",
    ).strip()

    try:
        from core.customer_name_adoption_guard import (  # noqa: PLC0415
            contains_customer_name_role_context,
            is_explicit_name_correction_message,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[NAME_AUTHORITY] adoption guard unavailable")
        return SelfReportEvidence(False, reason="guard_unavailable")

    if contains_customer_name_role_context(name) or contains_customer_name_role_context(inbound_text):
        return SelfReportEvidence(False, reason="role_context")

    # (3) Explicit correction / completion of a stored name — evaluated
    # first because it is the most specific evidence: "اسمي الصحيح هشام"
    # also matches the self-introduction pattern below, and a correction
    # must outrank a bare restatement at equal authority.
    if bool(ctx.get("explicit_name_correction")) or (
        inbound_text and is_explicit_name_correction_message(inbound_text)
    ):
        return SelfReportEvidence(
            True,
            kind=EVIDENCE_EXPLICIT_CORRECTION,
            reason="explicit_correction",
            detail=_evidence_ref(ctx, inbound_text),
        )

    # (1) Explicit self-identification pattern.
    hit = None
    if inbound_text:
        try:
            from core.customer_name_extractor import (  # noqa: PLC0415
                extract_high_confidence_name,
            )

            hit = extract_high_confidence_name(inbound_text)
        except Exception:  # noqa: BLE001
            logger.exception("[NAME_AUTHORITY] extractor unavailable")
            hit = None
        if hit is not None:
            hit_value = str(getattr(hit, "value", "") or "").strip()
            if hit_value and _letter_tokens(hit_value) == _letter_tokens(name):
                pattern = str(getattr(hit, "pattern", "") or "")
                return SelfReportEvidence(
                    True,
                    kind=EVIDENCE_EXPLICIT_STATEMENT,
                    reason="self_introduction_pattern",
                    detail=_evidence_ref(ctx, inbound_text, pattern=pattern),
                )

    # (2) Direct answer to Nahlah's own question — must be a bare name.
    if bool(ctx.get("awaiting_name_answer")):
        if is_bare_name_answer(inbound_text, name):
            return SelfReportEvidence(
                True,
                kind=EVIDENCE_DIRECT_ANSWER,
                reason="bare_answer_to_name_question",
                detail=_evidence_ref(
                    ctx, inbound_text,
                    asked_by=str(ctx.get("name_question_asked_by") or "nahla"),
                ),
            )
        return SelfReportEvidence(False, reason="not_a_bare_name_answer")

    if hit is not None:
        return SelfReportEvidence(
            False,
            reason="candidate_not_the_self_reported_span",
            detail={"extracted": str(getattr(hit, "value", "") or "")[:60]},
        )
    return SelfReportEvidence(False, reason="no_explicit_evidence")


# ══════════════════════════════════════════════════════════════════════
# The resolver
# ══════════════════════════════════════════════════════════════════════

DECISION_APPLIED = "applied"
DECISION_HINT_ONLY = "hint_only"
DECISION_BLOCKED_LOWER = "blocked_lower_authority"
DECISION_BLOCKED_LOCK = "blocked_manual_lock"
DECISION_BLOCKED_BLANK = "blocked_blank_value"
DECISION_BLOCKED_CLASS = "blocked_classification"
DECISION_BLOCKED_EVIDENCE = "blocked_no_self_report_evidence"
DECISION_BLOCKED_VALIDATION = "blocked_validation"
DECISION_NOOP = "noop"

CANONICAL_CHANGING_DECISIONS: FrozenSet[str] = frozenset({DECISION_APPLIED})

# Evidence strong enough to replace an already-validated self-report.
_SELF_REPORT_REFRESH_EVIDENCE: FrozenSet[str] = frozenset({
    EVIDENCE_EXPLICIT_CORRECTION, EVIDENCE_DIRECT_ANSWER,
})


@dataclass(frozen=True)
class NameAuthorityDecision:
    """What the authority decided, and why. Always safe to log."""

    decision: str
    canonical_name: str = ""
    authority: NameAuthority = NameAuthority.UNKNOWN
    previous_name: str = ""
    previous_authority: NameAuthority = NameAuthority.UNKNOWN
    classification: str = ""
    evidence_kind: str = ""
    reason: str = ""
    incoming_name: str = ""
    incoming_authority: NameAuthority = NameAuthority.UNKNOWN
    # Provenance label override for the merchant lock path.
    authority_label: str = ""

    @property
    def changed_canonical(self) -> bool:
        return self.decision in CANONICAL_CHANGING_DECISIONS

    @property
    def effective_authority_label(self) -> str:
        return self.authority_label or self.authority.label

    def as_provenance(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "canonical_name": self.canonical_name,
            "authority": self.effective_authority_label,
            "previous_name": self.previous_name,
            "previous_authority": self.previous_authority.label,
            "classification": self.classification,
            "evidence_kind": self.evidence_kind,
            "reason": self.reason,
            "incoming_name": self.incoming_name,
            "incoming_authority": self.incoming_authority.label,
        }


def resolve_canonical_customer_name(
    *,
    incoming_name: Optional[str],
    incoming_authority: NameAuthority,
    current_name: Optional[str],
    current_authority: NameAuthority,
    merchant_locked: bool = False,
    merchant_cleared: bool = False,
    classification: Optional[str] = None,
    evidence_kind: str = "",
) -> NameAuthorityDecision:
    """
    THE precedence decision. Pure function — no I/O, no ORM, no clock.

    ``merchant_locked``  — ``manual_name_override`` with a stored name.
    ``merchant_cleared`` — merchant intentionally emptied the name; only
                           self-report or store may refill it.
    """
    incoming = _normalize_full(str(incoming_name or ""))
    current = _normalize_full(str(current_name or ""))
    base: Dict[str, Any] = {
        "previous_name": current,
        "previous_authority": current_authority,
        "classification": classification or "",
        "evidence_kind": evidence_kind,
        "incoming_name": incoming,
        "incoming_authority": incoming_authority,
    }

    def _blocked(code: str, reason: str) -> NameAuthorityDecision:
        return NameAuthorityDecision(
            code, canonical_name=current, authority=current_authority, reason=reason, **base,
        )

    # ── Blank never erases, at any authority ─────────────────────────
    if not incoming:
        return _blocked(DECISION_BLOCKED_BLANK, "blank_incoming_never_erases")

    # ── Merchant lock outranks the whole ladder ──────────────────────
    if merchant_locked and current:
        return _blocked(DECISION_BLOCKED_LOCK, "manual_name_override_active")
    if merchant_cleared and not current and incoming_authority < NameAuthority.CUSTOMER_SELF_REPORTED:
        return _blocked(DECISION_BLOCKED_LOCK, "manual_name_cleared_blocks_low_authority")

    # ── WhatsApp profile must be classified before it can be canonical
    if incoming_authority == NameAuthority.WHATSAPP_PROFILE:
        if classification == NOT_PERSON_NAME:
            return _blocked(DECISION_BLOCKED_CLASS, "profile_not_person_name")
        if classification != PERSON_NAME:
            return _blocked(DECISION_HINT_ONLY, "profile_ambiguous_hint_only")

    # ── Self-reported requires explicit evidence ─────────────────────
    if incoming_authority == NameAuthority.CUSTOMER_SELF_REPORTED and not evidence_kind:
        return _blocked(DECISION_BLOCKED_EVIDENCE, "self_report_without_evidence")

    # ── A name of unknown provenance is protected from profile hints ─
    if (
        current
        and current_authority == NameAuthority.UNKNOWN
        and incoming_authority <= NameAuthority.WHATSAPP_PROFILE
    ):
        return _blocked(DECISION_BLOCKED_LOWER, "unknown_provenance_name_protected_from_profile")

    # ── The ladder ───────────────────────────────────────────────────
    if current and incoming_authority < current_authority:
        return _blocked(
            DECISION_BLOCKED_LOWER,
            f"{incoming_authority.label}_below_{current_authority.label}",
        )
    if current and incoming == current and incoming_authority == current_authority:
        return _blocked(DECISION_NOOP, "unchanged")
    # A validated self-reported name is not flipped by a bare restatement
    # ("معك هشام"): only an explicit correction, or a direct answer to
    # Nahlah's own question, may replace it at equal authority.
    if (
        current
        and incoming_authority == current_authority == NameAuthority.CUSTOMER_SELF_REPORTED
        and evidence_kind not in _SELF_REPORT_REFRESH_EVIDENCE
    ):
        return _blocked(DECISION_BLOCKED_EVIDENCE, "self_report_restatement_needs_correction")

    return NameAuthorityDecision(
        DECISION_APPLIED,
        canonical_name=incoming,
        authority=incoming_authority,
        reason="authority_satisfied",
        **base,
    )
