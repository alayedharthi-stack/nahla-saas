"""
core/customer_name_authority.py
───────────────────────────────
THE canonical customer-name authority.

Single source of truth for the question "what is this customer's name,
and who says so?". Every writer of ``Customer.name`` must route through
``resolve_canonical_customer_name`` — directly, or via
``core.customer_identity_resolver.apply_customer_name`` which delegates
here.

Authority ladder (strictly ordered, highest first)
==================================================

    VERIFIED_ECOMMERCE        Salla / Zid / Shopify customer or order
                              payloads. The merchant's store is the
                              system of record for who the buyer is.
    CUSTOMER_SELF_REPORTED    The customer stated their own name with
                              explicit evidence ("اسمي محمد", or a
                              direct answer to Nahlah's own "ما اسمك؟").
    WHATSAPP_PROFILE          The WhatsApp profile display string. A
                              hint, never an assertion — anyone can set
                              it to "الحمد لله".
    UNKNOWN                   Nothing trustworthy is known.

Rules the ladder enforces (AGENTS.md: operational facts are deterministic):

  * A lower authority NEVER overwrites a higher one.
  * An equal authority may refresh (stores correct their own data).
  * A BLANK value never erases a stored name, at any authority. Absence
    of evidence is not evidence of absence — a Salla resync with an
    empty ``first_name`` must not wipe a good name.
  * Merchant / manual override is NOT a rung on this ladder. It is an
    orthogonal LOCK that sits above the whole ladder, preserved verbatim
    from the pre-existing ``manual_name_override`` semantics.

WhatsApp profile classification
===============================

WhatsApp profile strings are user-controlled free text. They are
classified conservatively into three buckets before they are allowed
anywhere near a canonical name:

    PERSON_NAME       Confidently a human name → may become canonical
                      (but still only at WHATSAPP_PROFILE authority).
    NOT_PERSON_NAME   Confidently not a name (status/religious phrases,
                      commerce text, role labels, cities) → dropped.
                      Never stored as a hint, never displayed.
    AMBIGUOUS         Could be a name, could be a word. Retained as a
                      non-canonical hint only. Never displayed as the
                      customer's name, never used operationally.

The classifier is deliberately asymmetric. Rejecting a real name costs
a merchant one inline edit. Accepting "الحمد لله" puts a religious
phrase on an invoice and a shipping label.

Crucially it must NOT blanket-reject single Arabic words: دعاء، نور،
جود are ordinary given names that also exist as common nouns. Religious
and status detection is therefore PHRASE-structural (الحمد + لله), never
"contains a religious-looking token" — which is also what keeps
عبدالله / عبد الله / أبو عبدالله classified as person names.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, FrozenSet, Mapping, Optional

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


# Canonical string form persisted in provenance + JSONB metadata.
AUTHORITY_UNKNOWN = "UNKNOWN"
AUTHORITY_WHATSAPP_PROFILE = "WHATSAPP_PROFILE"
AUTHORITY_CUSTOMER_SELF_REPORTED = "CUSTOMER_SELF_REPORTED"
AUTHORITY_VERIFIED_ECOMMERCE = "VERIFIED_ECOMMERCE"

_AUTHORITY_BY_NAME: Dict[str, NameAuthority] = {
    a.name: a for a in NameAuthority
}

# ── Source string → authority ────────────────────────────────────────
#
# Mirrors the source vocabulary already in customer_identity_resolver /
# customer_name_adoption_guard so no caller has to learn a new one.

_VERIFIED_ECOMMERCE_SOURCES: FrozenSet[str] = frozenset({
    "salla", "salla_sync", "salla_order", "customer_webhook",
    "zid", "zid_sync", "zid_order",
    "shopify", "shopify_sync", "shopify_order",
    "commerce_platform", "sales_channel", "platform_verified",
    "order", "order_sync", "order_webhook", "order_incremental",
})

_SELF_REPORTED_SOURCES: FrozenSet[str] = frozenset({
    "customer_message", "ai_detected_name",
})

_WHATSAPP_PROFILE_SOURCES: FrozenSet[str] = frozenset({
    "whatsapp_profile", "whatsapp_inbound", "whatsapp_lead", "widget",
})

# Merchant/manual sources are handled by the override LOCK, not the
# ladder. Listed here so ``authority_for_source`` never silently
# downgrades them to UNKNOWN.
_MERCHANT_SOURCES: FrozenSet[str] = frozenset({
    "merchant_correction", "merchant_manual", "manual", "manual_admin",
})


def authority_for_source(source: Optional[str]) -> NameAuthority:
    """Map a caller source string onto the authority ladder."""
    src = (source or "").strip().lower()
    if src in _VERIFIED_ECOMMERCE_SOURCES:
        return NameAuthority.VERIFIED_ECOMMERCE
    if src in _SELF_REPORTED_SOURCES:
        return NameAuthority.CUSTOMER_SELF_REPORTED
    if src in _WHATSAPP_PROFILE_SOURCES:
        return NameAuthority.WHATSAPP_PROFILE
    if src in _MERCHANT_SOURCES:
        # Merchant writes always arrive with force_merchant=True, which
        # short-circuits the ladder. If one ever arrives without it we
        # treat it as self-reported rather than as a store fact.
        return NameAuthority.CUSTOMER_SELF_REPORTED
    return NameAuthority.UNKNOWN


def authority_from_label(label: Optional[str]) -> NameAuthority:
    """Parse a persisted authority label back into the enum."""
    return _AUTHORITY_BY_NAME.get(
        str(label or "").strip().upper(), NameAuthority.UNKNOWN,
    )


# ══════════════════════════════════════════════════════════════════════
# WhatsApp profile classification
# ══════════════════════════════════════════════════════════════════════

class ProfileNameClass(str):
    """Marker type kept as ``str`` so it serialises into JSONB cleanly."""


PERSON_NAME = "PERSON_NAME"
NOT_PERSON_NAME = "NOT_PERSON_NAME"
AMBIGUOUS = "AMBIGUOUS"


_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF\U0001F600-\U0001F64F\U00002600-\U000027BF]+",
    flags=re.UNICODE,
)
_DIGIT_RE = re.compile(r"\d")


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


# ── Religious / devotional phrase structure ──────────────────────────
#
# These are detected STRUCTURALLY, not by "contains الله". A phrase is
# devotional when it opens with a devotional verb/noun head, or when it
# closes on الله/لله without a personal-name prefix opening it.

_DEVOTIONAL_HEADS: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    "الحمد", "الحمدلله", "استغفر", "استغفرالله", "سبحان", "سبحانه",
    "تبارك", "بارك", "جزاك", "جزاكم", "حسبي", "حسبنا", "اللهم",
    "توكلت", "توكلنا", "بسم", "ماشاء", "انشاء", "شكرا",
    "اشهد", "لاحول", "استغفري", "الشكر", "بحمد", "والحمد",
    "يارب", "ياالله", "اللهي", "ربي", "ربنا",
})

# Second token of a two-word devotional head, e.g. "ما شاء الله",
# "لا حول", "إن شاء الله".
_DEVOTIONAL_BIGRAM_HEADS: FrozenSet[str] = frozenset(
    _normalize_arabic(t) for t in {"ما شاء", "ان شاء", "لا حول", "لا اله", "يا رب", "يا الله"}
)

# Tokens that make a trailing الله/لله devotional rather than onomastic.
# ``عبد الله`` is a name; ``الحمد لله`` is not. The difference is the head.
_NAME_PREFIX_HEADS: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    "عبد", "عبيد", "ابو", "ابا", "ابي", "ام", "اما", "امي",
    "بن", "ابن", "بنت", "ابنه", "ال", "الشيخ",
})

_ALLAH_TAILS: FrozenSet[str] = frozenset(
    _normalize_arabic(t) for t in {"الله", "لله", "اللة", "اللهم"}
)

# ── Non-religious status / presence phrases ──────────────────────────
_STATUS_PHRASE_TOKENS: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    "متوفر", "مشغول", "مسافر", "نايم", "متاح", "مشغوله", "متوفره",
    "مغلق", "مفتوح", "اونلاين", "اوفلاين", "available", "busy", "online",
    "offline", "away", "working", "afk",
    # Commercial / storefront profile strings
    "للبيع", "مبيعات", "عروض", "تخفيضات", "توصيل", "خدمة", "متجر",
    "محل", "مؤسسة", "مؤسسه", "شركة", "شركه", "مطعم", "كافيه",
})

# ── Curated common Arabic given names ────────────────────────────────
#
# Purpose: rescue single-token names that are ALSO ordinary nouns, which
# a purely structural classifier would have to call AMBIGUOUS. This list
# is additive-only — never used to reject anything.
_COMMON_GIVEN_NAMES: FrozenSet[str] = frozenset(_normalize_arabic(t) for t in {
    # Explicitly required by the identity spec — common nouns AND names.
    "دعاء", "نور", "جود",
    # Feminine
    "سارة", "ساره", "نورة", "نوره", "مريم", "فاطمة", "فاطمه", "عائشة",
    "عائشه", "هند", "لمى", "لمي", "ريم", "رزان", "شهد", "جواهر",
    "العنود", "منال", "أمل", "امل", "هيا", "دانة", "دانه", "غادة",
    "غاده", "أروى", "اروى", "بشاير", "لطيفة", "لطيفه", "منيرة",
    "منيره", "أسماء", "اسماء", "رهف", "وعد", "ندى", "ولاء", "صفاء",
    "رغد", "تالا", "ليان", "جنى", "جني", "رتاج", "أمجاد", "امجاد",
    "حنين", "بيان", "روان", "لين", "سلمى", "سلمي", "عبير", "إيمان",
    "ايمان", "رنا", "دلال", "نوف", "جمانة", "جمانه", "أفنان", "افنان",
    # Masculine
    "محمد", "أحمد", "احمد", "عبدالله", "عبدالرحمن", "عبدالعزيز",
    "خالد", "فهد", "سعود", "سلطان", "بندر", "تركي", "ناصر", "سعد",
    "ماجد", "مشعل", "نواف", "فيصل", "عمر", "علي", "حسن", "حسين",
    "يوسف", "إبراهيم", "ابراهيم", "عثمان", "طلال", "زياد", "رياض",
    "وليد", "سامي", "رامي", "أنس", "انس", "معاذ", "مازن", "ريان",
    "راكان", "فارس", "صالح", "طارق", "هشام", "أيمن", "ايمن", "بدر",
    "راشد", "مهند", "يزيد", "عبدالملك", "عبدالإله", "عبداله",
    "حارث", "غانم", "مروان", "زيد", "أسامة", "اسامه", "جابر",
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


def _is_devotional_phrase(tokens: list[str], norm_tokens: list[str]) -> bool:
    """Structural devotional-phrase test. See module docstring."""
    if not norm_tokens:
        return False

    head = norm_tokens[0]
    if head in _DEVOTIONAL_HEADS:
        return True

    if len(norm_tokens) >= 2:
        bigram = f"{norm_tokens[0]} {norm_tokens[1]}"
        if bigram in _DEVOTIONAL_BIGRAM_HEADS:
            return True

    # Trailing الله / لله with a non-onomastic head:
    #   "الحمد لله" → devotional      "عبد الله" → name
    if len(norm_tokens) >= 2 and norm_tokens[-1] in _ALLAH_TAILS:
        if head not in _NAME_PREFIX_HEADS:
            return True

    return False


def classify_whatsapp_profile_name(raw: Optional[str]) -> ProfileClassification:
    """
    Classify a WhatsApp profile string as PERSON_NAME / NOT_PERSON_NAME /
    AMBIGUOUS.

    Conservative by construction: anything not confidently a person name
    and not confidently junk lands in AMBIGUOUS, which is retained as a
    hint but never becomes canonical and is never displayed.
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
    if _is_devotional_phrase(tokens, norm_tokens):
        return ProfileClassification(
            NOT_PERSON_NAME, reason="devotional_phrase",
        )

    if any(t in _STATUS_PHRASE_TOKENS for t in norm_tokens):
        return ProfileClassification(NOT_PERSON_NAME, reason="status_phrase")

    # Reuse the platform validator for fillers, commerce text, cities,
    # role labels, deictic phrases, repeated tokens, bad characters.
    try:
        from core.customer_name_validator import (  # noqa: PLC0415
            validate_customer_name,
        )

        validation = validate_customer_name(text)
    except Exception:  # noqa: BLE001 — validator must never break ingestion
        logger.exception("[NAME_AUTHORITY] validator unavailable")
        return ProfileClassification(AMBIGUOUS, cleaned=text, reason="validator_error")

    if not validation.valid:
        return ProfileClassification(
            NOT_PERSON_NAME, reason=validation.reason or "validator_reject",
        )

    cleaned = validation.cleaned or text

    # ── Positive person-name signals ─────────────────────────────────
    if any(t in _COMMON_GIVEN_NAMES for t in norm_tokens):
        return ProfileClassification(
            PERSON_NAME, cleaned=cleaned, reason="known_given_name",
        )

    if norm_tokens[0] in _NAME_PREFIX_HEADS and len(norm_tokens) >= 2:
        # أبو خالد / عبد الرحمن / بنت فهد
        return ProfileClassification(
            PERSON_NAME, cleaned=cleaned, reason="name_prefix",
        )

    if len(norm_tokens) >= 2:
        # Multi-token, survived every reject rule, no devotional or
        # status structure — this is name-shaped.
        return ProfileClassification(
            PERSON_NAME, cleaned=cleaned, reason="multi_token_name_shape",
        )

    # Single unrecognised token: could be a name, could be a noun.
    return ProfileClassification(
        AMBIGUOUS, cleaned=cleaned, reason="single_unknown_token",
    )


# ══════════════════════════════════════════════════════════════════════
# Self-reported name evidence
# ══════════════════════════════════════════════════════════════════════

# Evidence kinds accepted as "the customer told us their own name".
EVIDENCE_EXPLICIT_STATEMENT = "explicit_self_statement"   # "اسمي محمد"
EVIDENCE_DIRECT_ANSWER = "direct_answer_to_name_question"  # after "ما اسمك؟"
EVIDENCE_EXPLICIT_CORRECTION = "explicit_name_correction"  # "صحح اسمي"

_SELF_REPORT_EVIDENCE_KINDS: FrozenSet[str] = frozenset({
    EVIDENCE_EXPLICIT_STATEMENT,
    EVIDENCE_DIRECT_ANSWER,
    EVIDENCE_EXPLICIT_CORRECTION,
})


@dataclass(frozen=True)
class SelfReportEvidence:
    """Why we believe the customer stated their OWN name."""

    accepted: bool
    kind: str = ""
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


def evaluate_self_report_evidence(
    candidate: Optional[str],
    message_context: Optional[Mapping[str, Any]] = None,
) -> SelfReportEvidence:
    """
    Accept a self-reported name ONLY with explicit evidence.

    Accepted evidence:
      1. An explicit self-identification pattern in the inbound text,
         per ``core.customer_name_extractor`` ("اسمي محمد", "معك فهد").
      2. A direct answer to a name question Nahlah itself just asked
         (``awaiting_name_answer`` set by the asking turn).
      3. An explicit correction of a stored name ("صحح اسمي").

    Everything else — including a bare name-shaped message, and any
    third-party name mentioned in passing ("أرسلها لأخوي سعد") — is
    rejected. We never infer the customer's own name from an arbitrary
    mention.
    """
    name = str(candidate or "").strip()
    if not name:
        return SelfReportEvidence(False, reason="empty_candidate")

    ctx = dict(message_context or {})
    inbound_text = str(
        ctx.get("message")
        or ctx.get("inbound_text")
        or ctx.get("raw_message")
        or "",
    ).strip()

    # (2) Direct answer to Nahlah's own question. The asking turn must
    # have flagged it — we never retro-infer that a question was asked.
    if bool(ctx.get("awaiting_name_answer")):
        return SelfReportEvidence(
            True,
            kind=EVIDENCE_DIRECT_ANSWER,
            reason="answered_name_question",
            detail={"asked_by": str(ctx.get("name_question_asked_by") or "nahla")},
        )

    # (3) Explicit correction of a stored name.
    try:
        from core.customer_name_adoption_guard import (  # noqa: PLC0415
            is_explicit_name_correction_message,
        )

        if bool(ctx.get("explicit_name_correction")) or (
            inbound_text and is_explicit_name_correction_message(inbound_text)
        ):
            return SelfReportEvidence(
                True,
                kind=EVIDENCE_EXPLICIT_CORRECTION,
                reason="explicit_correction_phrase",
            )
    except Exception:  # noqa: BLE001
        logger.exception("[NAME_AUTHORITY] correction detector unavailable")

    # (1) Explicit self-identification pattern in the message itself.
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
            if hit_value and _normalize_arabic(hit_value) == _normalize_arabic(name):
                return SelfReportEvidence(
                    True,
                    kind=EVIDENCE_EXPLICIT_STATEMENT,
                    reason="self_introduction_pattern",
                    detail={"pattern": str(getattr(hit, "pattern", "") or "")},
                )
            return SelfReportEvidence(
                False,
                reason="candidate_not_the_self_reported_span",
                detail={"extracted": hit_value[:60]},
            )

    # A pre-verified evidence kind may be passed explicitly by a caller
    # that already did the work (e.g. the order funnel's name slot).
    declared = str(ctx.get("self_report_evidence") or "").strip()
    if declared in _SELF_REPORT_EVIDENCE_KINDS:
        return SelfReportEvidence(True, kind=declared, reason="declared_by_caller")

    return SelfReportEvidence(False, reason="no_explicit_evidence")


# ══════════════════════════════════════════════════════════════════════
# The resolver
# ══════════════════════════════════════════════════════════════════════

# Decision outcomes.
DECISION_APPLIED = "applied"            # canonical name changed
DECISION_HINT_ONLY = "hint_only"        # stored as non-canonical hint
DECISION_BLOCKED_LOWER = "blocked_lower_authority"
DECISION_BLOCKED_LOCK = "blocked_manual_lock"
DECISION_BLOCKED_BLANK = "blocked_blank_value"
DECISION_BLOCKED_CLASS = "blocked_classification"
DECISION_BLOCKED_EVIDENCE = "blocked_no_self_report_evidence"
DECISION_NOOP = "noop"


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

    @property
    def changed_canonical(self) -> bool:
        return self.decision == DECISION_APPLIED

    def as_provenance(self) -> Dict[str, Any]:
        """Serialisable provenance payload."""
        return {
            "decision": self.decision,
            "canonical_name": self.canonical_name,
            "authority": self.authority.label,
            "previous_name": self.previous_name,
            "previous_authority": self.previous_authority.label,
            "classification": self.classification,
            "evidence_kind": self.evidence_kind,
            "reason": self.reason,
        }


def resolve_canonical_customer_name(
    *,
    incoming_name: Optional[str],
    incoming_authority: NameAuthority,
    current_name: Optional[str],
    current_authority: NameAuthority,
    merchant_locked: bool = False,
    classification: Optional[str] = None,
    evidence_kind: str = "",
) -> NameAuthorityDecision:
    """
    THE precedence decision. Pure function — no I/O, no ORM, no clock.

    Everything the platform believes about customer-name precedence is
    encoded here and nowhere else.
    """
    incoming = _normalize_full(str(incoming_name or ""))
    current = _normalize_full(str(current_name or ""))

    base = {
        "previous_name": current,
        "previous_authority": current_authority,
        "classification": classification or "",
        "evidence_kind": evidence_kind,
    }

    # ── Blank never erases, at any authority ─────────────────────────
    if not incoming:
        return NameAuthorityDecision(
            DECISION_BLOCKED_BLANK,
            canonical_name=current,
            authority=current_authority,
            reason="blank_incoming_never_erases",
            **base,
        )

    # ── Merchant/manual lock outranks the whole ladder ───────────────
    # Preserved verbatim from pre-existing manual_name_override
    # semantics: a merchant who typed a name owns it until they change
    # or clear it themselves.
    if merchant_locked and current:
        return NameAuthorityDecision(
            DECISION_BLOCKED_LOCK,
            canonical_name=current,
            authority=current_authority,
            reason="manual_name_override_active",
            **base,
        )

    # ── WhatsApp profile must be classified before it can be canonical
    if incoming_authority == NameAuthority.WHATSAPP_PROFILE:
        if classification == NOT_PERSON_NAME:
            return NameAuthorityDecision(
                DECISION_BLOCKED_CLASS,
                canonical_name=current,
                authority=current_authority,
                reason="profile_not_person_name",
                **base,
            )
        if classification != PERSON_NAME:
            # AMBIGUOUS (or unclassified): hint only, never canonical.
            return NameAuthorityDecision(
                DECISION_HINT_ONLY,
                canonical_name=current,
                authority=current_authority,
                reason="profile_ambiguous_hint_only",
                **base,
            )

    # ── Self-reported requires explicit evidence ─────────────────────
    if incoming_authority == NameAuthority.CUSTOMER_SELF_REPORTED and not evidence_kind:
        return NameAuthorityDecision(
            DECISION_BLOCKED_EVIDENCE,
            canonical_name=current,
            authority=current_authority,
            reason="self_report_without_evidence",
            **base,
        )

    # ── The ladder ───────────────────────────────────────────────────
    if current and incoming_authority < current_authority:
        return NameAuthorityDecision(
            DECISION_BLOCKED_LOWER,
            canonical_name=current,
            authority=current_authority,
            reason=(
                f"{incoming_authority.label}_below_{current_authority.label}"
            ),
            **base,
        )

    if current and incoming == current and incoming_authority == current_authority:
        return NameAuthorityDecision(
            DECISION_NOOP,
            canonical_name=current,
            authority=current_authority,
            reason="unchanged",
            **base,
        )

    return NameAuthorityDecision(
        DECISION_APPLIED,
        canonical_name=incoming,
        authority=incoming_authority,
        reason="authority_satisfied",
        **base,
    )
