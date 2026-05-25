"""
core/receipt_extraction.py
──────────────────────────
Wave 1 W1.3 — Receipt field extraction & structured visibility.
**Telemetry only**.

What this module owns
─────────────────────
A typed, frozen ``ReceiptFields`` snapshot of structured fields the
pipeline can extract from a receipt's text (OCR'd PDF text, image
vision text, caption, filename), plus a pluggable
``ReceiptFieldsExtractor`` abstraction so future waves can swap in:

  * additional OCR engines (Tesseract, Azure Document Intelligence,
    Saudi-bank-specific templates),
  * layout-aware parsers,
  * multimodal extraction (Vision + LLM hybrids),
  * structured-PDF parsers.

The current default implementation is a regex + heuristic
extractor. The abstraction is what matters — when we replace it
with a vision-template extractor in a future wave, the consumers
(decision layer, telemetry, future verification) keep working
unchanged because the contract is the dataclass shape, not the
engine.

W1.3 invariants (locked by tests)
─────────────────────────────────
1. **Telemetry only.** ``compute_receipt_fields`` MUST NOT
   touch state, MUST NOT mutate inputs, MUST NOT raise. It
   produces a snapshot the orchestrator can log; it does NOT
   gate any decision in W1.3. Verification consumption is
   reserved for W1.4.
2. **Per-field confidence.** Every extracted field has its own
   ``FieldConfidence`` (``HIGH`` / ``MEDIUM`` / ``LOW`` /
   ``ABSENT``). W1.4 needs finer-grained decisions than a single
   overall score. Architecturally pinned by tests.
3. **Full-text aware.** ``compose_full_evidence_text`` prefers
   the full PDF text (``pdf_text_full``) over the 280-char preview
   (``pdf_text_preview``). When only the preview is available,
   the snapshot reports ``source_text_was_truncated=True`` so
   on-call can quantify how often truncation is the bottleneck
   for tenant-aware verification.
4. **Closed confidence vocabulary.** ``FieldConfidence`` is a
   four-state enum, pinned. Drift fails the build.
5. **Engine extensibility.** ``ReceiptFieldsExtractor`` is an
   abstract protocol. Future engines register via
   ``register_extractor`` without touching the orchestrator or
   the dataclass shape.
6. **Kill switch.** ``RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED``
   (default OFF) gates ONLY logging. The pure functions are
   always safe to call.

Telemetry log shape
───────────────────
Canonical grep target::

    [PAYMENT_RECEIPT_EXTRACTED]
    tenant_id=<int> conversation_id=<int|None> message_id=<str|None>
    phone=*<last4> source=<call_site>
    extractor=regex_heuristic source_text_field=pdf_text_full
    source_text_length=<int> source_text_was_truncated=<bool>
    iban_count=<int> iban_confidence=<HIGH|MEDIUM|LOW|ABSENT>
    beneficiary_count=<int> beneficiary_confidence=<...>
    bank_brand_count=<int> bank_brand_confidence=<...>
    amount_count=<int> amount_confidence=<...>
    reference_count=<int> reference_confidence=<...>
    date_count=<int> date_confidence=<...>
    overall_confidence=<...>

Operators answer "why did the receipt fail to verify?" by
correlating ``[PAYMENT_RECEIPT_EXTRACTED]`` (what we saw) with
``[PAYMENT_VERIFICATION_DECISION]`` (what we decided).
"""
from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any, Dict, FrozenSet, List, Mapping, Optional, Tuple,
)

logger = logging.getLogger("nahla.receipt_extraction")


# ── 1. Closed confidence vocabulary ─────────────────────────────────


class FieldConfidence(str, Enum):
    """Per-field confidence level. Closed enum — drift fails the
    build. ``ABSENT`` is distinct from ``LOW``: ``ABSENT`` means
    "the extractor saw nothing"; ``LOW`` means "the extractor saw
    something but it didn't match the expected shape with enough
    structure to trust"."""

    HIGH    = "high"
    MEDIUM  = "medium"
    LOW     = "low"
    ABSENT  = "absent"


FIELD_CONFIDENCE_ALL: FrozenSet[FieldConfidence] = frozenset(FieldConfidence)
FIELD_CONFIDENCE_VALUES: FrozenSet[str] = frozenset({c.value for c in FieldConfidence})

# Order from strongest to weakest — used by ``_max_confidence``.
_CONFIDENCE_RANK: Dict[FieldConfidence, int] = {
    FieldConfidence.HIGH:   3,
    FieldConfidence.MEDIUM: 2,
    FieldConfidence.LOW:    1,
    FieldConfidence.ABSENT: 0,
}


def _max_confidence(*confidences: FieldConfidence) -> FieldConfidence:
    """Aggregate per-field confidences into an overall confidence
    using the strongest signal. Pure."""
    best = FieldConfidence.ABSENT
    best_rank = -1
    for c in confidences:
        rank = _CONFIDENCE_RANK.get(c, 0)
        if rank > best_rank:
            best_rank = rank
            best = c
    return best


# ── 2. Extracted-amount value object ────────────────────────────────


@dataclass(frozen=True)
class ExtractedAmount:
    """A single extracted monetary value. ``raw`` keeps the source
    span for debugging; ``value`` is best-effort numeric (string to
    avoid float drift on arbitrary precision); ``currency`` is a
    short token (e.g. ``SAR``, ``USD``, or empty when the source
    didn't label one)."""

    raw: str = ""
    value: str = ""
    currency: str = ""


# ── 3. Receipt fields snapshot ──────────────────────────────────────


@dataclass(frozen=True)
class ReceiptFields:
    """Frozen snapshot of every field a receipt extractor produced
    for a single inbound. Stable contract — adding fields is
    additive, removing or renaming requires a deliberate test
    update.

    The snapshot is for **observation only** in W1.3. Future waves
    (W1.4+) may consume specific fields under stricter rules.
    """

    # ── Extracted values ──────────────────────────────────────────
    ibans:          Tuple[str, ...] = ()
    beneficiaries:  Tuple[str, ...] = ()
    bank_brands:    Tuple[str, ...] = ()
    amounts:        Tuple[ExtractedAmount, ...] = ()
    references:     Tuple[str, ...] = ()
    dates:          Tuple[str, ...] = ()

    # ── Per-field confidence (closed enum) ────────────────────────
    iban_confidence:        FieldConfidence = FieldConfidence.ABSENT
    beneficiary_confidence: FieldConfidence = FieldConfidence.ABSENT
    bank_brand_confidence:  FieldConfidence = FieldConfidence.ABSENT
    amount_confidence:      FieldConfidence = FieldConfidence.ABSENT
    reference_confidence:   FieldConfidence = FieldConfidence.ABSENT
    date_confidence:        FieldConfidence = FieldConfidence.ABSENT

    # ── Aggregate confidence (derived from the per-field set) ─────
    overall_confidence:     FieldConfidence = FieldConfidence.ABSENT

    # ── Provenance ────────────────────────────────────────────────
    source_engine:               str  = ""
    source_text_field:           str  = ""
    source_text_length:          int  = 0
    source_text_was_truncated:   bool = False
    source_text_preview_len:     int  = 0   # length of what would have been visible via the legacy preview
    source_text_full_len:        int  = 0   # length of the full text the extractor actually saw

    # Optional extension surface. Engines MAY attach engine-specific
    # diagnostics here without forcing every consumer to update.
    # NEVER consumed by decision logic. Telemetry-only.
    engine_diagnostics: Tuple[Tuple[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        """``True`` when no field has any extracted value. Useful
        for the ``not_payment`` fallback in the verdict layer
        without coupling the layers."""
        return (
            not self.ibans
            and not self.beneficiaries
            and not self.bank_brands
            and not self.amounts
            and not self.references
            and not self.dates
        )

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "extractor":                  self.source_engine,
            "source_text_field":          self.source_text_field,
            "source_text_length":         self.source_text_length,
            "source_text_was_truncated":  self.source_text_was_truncated,
            "source_text_preview_len":    self.source_text_preview_len,
            "source_text_full_len":       self.source_text_full_len,
            "iban_count":                 len(self.ibans),
            "iban_confidence":            self.iban_confidence.value,
            "beneficiary_count":          len(self.beneficiaries),
            "beneficiary_confidence":     self.beneficiary_confidence.value,
            "bank_brand_count":           len(self.bank_brands),
            "bank_brand_confidence":      self.bank_brand_confidence.value,
            "amount_count":               len(self.amounts),
            "amount_confidence":          self.amount_confidence.value,
            "reference_count":            len(self.references),
            "reference_confidence":       self.reference_confidence.value,
            "date_count":                 len(self.dates),
            "date_confidence":            self.date_confidence.value,
            "overall_confidence":         self.overall_confidence.value,
        }


# ── 4. Extractor abstraction ────────────────────────────────────────


class ReceiptFieldsExtractor(ABC):
    """Abstract receipt-field extractor. The contract is:

      * ``name`` — short, stable engine token used for provenance
        in logs (e.g. ``"regex_heuristic"``, ``"vision_layout"``,
        ``"saudi_bank_template_v1"``).
      * ``extract(text, metadata) -> ReceiptFields`` — pure: must
        not mutate inputs, must not raise (catch internally and
        return an empty / low-confidence snapshot instead).

    Future engines register via :func:`register_extractor` and the
    orchestrator iterates through registered extractors in priority
    order. The default ``RegexHeuristicExtractor`` is always
    registered.
    """

    name: str = "abstract"

    @abstractmethod
    def extract(
        self, *,
        text: str,
        metadata: Mapping[str, Any],
    ) -> ReceiptFields:
        ...


_EXTRACTOR_REGISTRY: List[ReceiptFieldsExtractor] = []


def register_extractor(extractor: ReceiptFieldsExtractor) -> None:
    """Register an additional extractor. Future waves can register
    a ``VisionLayoutExtractor`` or a bank-template extractor here.
    Idempotent — re-registering the same name replaces the
    existing entry."""
    if not isinstance(extractor, ReceiptFieldsExtractor):
        raise TypeError(
            "register_extractor() requires a ReceiptFieldsExtractor"
        )
    name = (extractor.name or "").strip()
    if not name or name == "abstract":
        raise ValueError(
            "ReceiptFieldsExtractor.name must be a non-empty stable token"
        )
    for i, existing in enumerate(_EXTRACTOR_REGISTRY):
        if existing.name == name:
            _EXTRACTOR_REGISTRY[i] = extractor
            return
    _EXTRACTOR_REGISTRY.append(extractor)


def list_registered_extractors() -> Tuple[str, ...]:
    """Return the registered extractor names in priority order."""
    return tuple(e.name for e in _EXTRACTOR_REGISTRY)


# ── 5. Default regex + heuristic extractor ──────────────────────────


# Reference / transaction-id label patterns. Bounded length to
# avoid swallowing entire paragraphs after a colon.
_REFERENCE_LABEL_RE = re.compile(
    r"(?:رقم\s*العملية|رقم\s*المرجع|رقم\s*التحويل|رقم\s*العمليه|"
    r"المرجع|reference(?:\s*(?:no|number|id))?|ref(?:\s*no)?|"
    r"transaction\s*(?:id|no|number)|operation\s*(?:id|no|number))"
    r"\s*[:#\-–—]?\s*(?P<ref>[A-Za-z0-9][A-Za-z0-9\-]{4,38}[A-Za-z0-9])",
    re.IGNORECASE,
)

# Standalone "looks like a transaction id" — long digit run when no
# label is around. Bounded to avoid swallowing IBANs (24+) or
# phone numbers (9–10).
_REFERENCE_BARE_RE = re.compile(
    r"(?<![\d])(?P<ref>\d{8,16})(?!\d)",
)

# Saudi-style currency-labelled amount: ``250.00 ر.س`` /
# ``250 ريال`` / ``SAR 250.00`` / ``SR 250``. Captured in three
# variants so we can attach an explicit currency token.
_AMOUNT_LABELED_RE = re.compile(
    r"(?:(?P<currency_pre>SAR|SR)\s*)?"
    r"(?P<value>\d{1,9}(?:[.,]\d{1,2})?)\s*"
    r"(?P<currency_post>ر\s*\.?\s*س|ريال|SAR|SR)?",
    re.IGNORECASE,
)

# Date patterns we recognise — bounded coverage; we don't try to
# parse Hijri vs Gregorian, just record what we saw.
_DATE_RES: Tuple[re.Pattern, ...] = (
    # ISO-like 2026-05-25 / 2026/05/25
    re.compile(r"\b(?P<d>\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})\b"),
    # 25-05-2026 / 25/5/26
    re.compile(r"\b(?P<d>\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})\b"),
    # ISO with time
    re.compile(
        r"\b(?P<d>\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\b"
    ),
)


def _heuristic_amount_confidence(amounts: Tuple[ExtractedAmount, ...]) -> FieldConfidence:
    if not amounts:
        return FieldConfidence.ABSENT
    has_currency = any(a.currency for a in amounts)
    if has_currency and len(amounts) >= 1:
        return FieldConfidence.HIGH
    if len(amounts) >= 1:
        return FieldConfidence.MEDIUM
    return FieldConfidence.ABSENT


def _normalize_currency(token: str) -> str:
    """Return a stable short token for the captured currency or
    empty when nothing useful was seen."""
    if not token:
        return ""
    raw = token.strip().upper()
    raw_norm = raw.replace(".", "").replace(" ", "")
    if raw_norm in ("SAR", "SR", "رس", "رياﻝ", "ريال"):
        return "SAR"
    if "ريال" in token or "ر.س" in token or "ر س" in token or token.strip().endswith("رس"):
        return "SAR"
    return raw


class RegexHeuristicExtractor(ReceiptFieldsExtractor):
    """Default extractor. Reuses the validated regex helpers in
    :mod:`core.tenant_payment_accounts` for IBANs, beneficiaries,
    and bank brands so extraction normalisation matches what the
    verification layer expects. Adds local heuristics for amounts,
    references, and dates.

    Pure. Never raises into the caller.
    """

    name = "regex_heuristic"

    def extract(
        self, *,
        text: str,
        metadata: Mapping[str, Any],
    ) -> ReceiptFields:
        try:
            return self._extract_unsafe(
                text=text or "", metadata=metadata or {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[PAYMENT_RECEIPT_EXTRACTED] regex extractor failed "
                "(returning empty snapshot): %s", exc,
            )
            return ReceiptFields(source_engine=self.name)

    def _extract_unsafe(
        self, *,
        text: str,
        metadata: Mapping[str, Any],
    ) -> ReceiptFields:
        from core.tenant_payment_accounts import (  # noqa: PLC0415
            _scan_bank_brands,
            extract_beneficiaries,
            extract_ibans,
        )

        ibans          = tuple(extract_ibans(text))
        beneficiaries  = tuple(extract_beneficiaries(text))
        bank_brands    = tuple(_scan_bank_brands(text))
        amounts        = self._extract_amounts(text)
        references     = self._extract_references(text, ibans=ibans)
        dates          = self._extract_dates(text)

        # Per-field confidence rules. Conservative — W1.3 is
        # observation only, so we lean toward MEDIUM rather than
        # HIGH unless the signal is unambiguously strong.
        iban_conf = (
            FieldConfidence.HIGH if len(ibans) >= 1 else FieldConfidence.ABSENT
        )

        if beneficiaries:
            beneficiary_conf = FieldConfidence.HIGH
        elif (metadata.get("filename") or metadata.get("caption")) and bank_brands:
            beneficiary_conf = FieldConfidence.LOW
        else:
            beneficiary_conf = FieldConfidence.ABSENT

        bank_brand_conf = (
            FieldConfidence.HIGH if bank_brands else FieldConfidence.ABSENT
        )
        amount_conf     = _heuristic_amount_confidence(amounts)
        reference_conf  = self._reference_confidence(text, references)
        date_conf       = (
            FieldConfidence.HIGH if dates else FieldConfidence.ABSENT
        )

        overall = _max_confidence(
            iban_conf, beneficiary_conf, bank_brand_conf,
            amount_conf, reference_conf, date_conf,
        )

        return ReceiptFields(
            ibans=ibans,
            beneficiaries=beneficiaries,
            bank_brands=bank_brands,
            amounts=amounts,
            references=references,
            dates=dates,
            iban_confidence=iban_conf,
            beneficiary_confidence=beneficiary_conf,
            bank_brand_confidence=bank_brand_conf,
            amount_confidence=amount_conf,
            reference_confidence=reference_conf,
            date_confidence=date_conf,
            overall_confidence=overall,
            source_engine=self.name,
        )

    @staticmethod
    def _extract_amounts(text: str) -> Tuple[ExtractedAmount, ...]:
        if not text:
            return ()
        out: List[ExtractedAmount] = []
        seen: set = set()
        for m in _AMOUNT_LABELED_RE.finditer(text):
            value = (m.group("value") or "").strip()
            if not value:
                continue
            currency_token = (
                m.group("currency_pre") or m.group("currency_post") or ""
            )
            currency = _normalize_currency(currency_token)
            # Skip plain numbers without any currency context AND
            # without obvious labelling — those are picked up via
            # the standalone reference regex if relevant.
            if not currency:
                # Allow a single 'amount-shaped' value through as
                # MEDIUM evidence only when the token is short
                # enough to plausibly be money (avoid IBAN runs).
                if len(value.replace(",", "").replace(".", "")) > 9:
                    continue
                # Require some explicit money-context marker.
                window_start = max(0, m.start() - 20)
                window_end = m.end() + 20
                window = text[window_start:window_end]
                if not re.search(
                    r"المبلغ|amount|قيمة|paid|payment|حول|حولت|"
                    r"transferred|total",
                    window,
                    flags=re.IGNORECASE,
                ):
                    continue
            key = (value, currency)
            if key in seen:
                continue
            seen.add(key)
            out.append(ExtractedAmount(
                raw=m.group(0).strip(),
                value=value,
                currency=currency,
            ))
            if len(out) >= 20:
                break
        return tuple(out)

    @staticmethod
    def _extract_references(
        text: str, *, ibans: Tuple[str, ...],
    ) -> Tuple[str, ...]:
        if not text:
            return ()
        out: List[str] = []
        seen: set = set()
        # Labelled references are highest signal.
        for m in _REFERENCE_LABEL_RE.finditer(text):
            ref = (m.group("ref") or "").strip()
            if ref and ref not in seen:
                seen.add(ref)
                out.append(ref)
        # Bare references — only consider when no labelled ones
        # were captured, to avoid false positives that look like
        # IBANs or phone numbers stripped of context.
        if not out:
            iban_substrings = {
                # Strip the SA prefix and split into 4-digit chunks
                # so a plain digit run that's part of an IBAN
                # doesn't get mis-promoted to a reference.
                re.sub(r"\D", "", iban) for iban in ibans
            }
            for m in _REFERENCE_BARE_RE.finditer(text):
                ref = (m.group("ref") or "").strip()
                if not ref or ref in seen:
                    continue
                # If this digit run is a substring of an IBAN we
                # already extracted, skip it.
                if any(ref in s for s in iban_substrings if s):
                    continue
                seen.add(ref)
                out.append(ref)
                if len(out) >= 20:
                    break
        return tuple(out)

    @staticmethod
    def _reference_confidence(
        text: str, references: Tuple[str, ...],
    ) -> FieldConfidence:
        if not references:
            return FieldConfidence.ABSENT
        # If any was captured via the labelled regex, treat it as HIGH;
        # bare digit runs are MEDIUM at best.
        if _REFERENCE_LABEL_RE.search(text or ""):
            return FieldConfidence.HIGH
        return FieldConfidence.MEDIUM

    @staticmethod
    def _extract_dates(text: str) -> Tuple[str, ...]:
        if not text:
            return ()
        out: List[str] = []
        seen: set = set()
        for pattern in _DATE_RES:
            for m in pattern.finditer(text):
                d = (m.group("d") or "").strip()
                if d and d not in seen:
                    seen.add(d)
                    out.append(d)
        return tuple(out)


# Always register the default extractor.
register_extractor(RegexHeuristicExtractor())


# ── 6. Full evidence text composer ──────────────────────────────────


# Cap the persisted full text at a generous bound so a misbehaving
# OCR pipeline can't blow up structured logs / DB rows. Receipts in
# practice are well under this limit.
_FULL_TEXT_PERSIST_CAP = 8000


def compose_full_evidence_text(
    metadata: Mapping[str, Any],
) -> Tuple[str, str, bool, int, int]:
    """Assemble the most complete payment-evidence text available
    on this inbound. Returns
    ``(text, source_field, was_truncated, preview_len, full_len)``:

      * ``text`` — the highest-fidelity text we can hand to an
        extractor. Preference order: ``pdf_text_full`` >
        ``vision_text`` > ``ocr_text`` > ``pdf_text_preview`` >
        ``caption`` > ``filename``. We concatenate all available
        non-empty sources but tag the *primary* source field for
        provenance.
      * ``source_field`` — which metadata key supplied the primary
        text content. Used in the ``[PAYMENT_RECEIPT_EXTRACTED]``
        log so on-call can see whether we were operating on a
        full PDF body or only the legacy 280-char preview.
      * ``was_truncated`` — ``True`` when only the legacy preview
        was available (no full-text field). Quantifies how often
        the 280-char window is the bottleneck.
      * ``preview_len`` / ``full_len`` — character counts of the
        legacy preview and the full text we actually used.

    Pure. Never raises.
    """
    md = metadata or {}

    def _get(key: str) -> str:
        v = md.get(key)
        if v is None:
            return ""
        try:
            return str(v)
        except Exception:
            return ""

    pdf_full       = _get("pdf_text_full")
    vision_text    = _get("vision_text")
    ocr_text       = _get("ocr_text")
    pdf_preview    = _get("pdf_text_preview")
    caption        = _get("caption")
    filename       = _get("filename")

    primary = ""
    primary_field = ""
    if pdf_full.strip():
        primary, primary_field = pdf_full, "pdf_text_full"
    elif vision_text.strip():
        primary, primary_field = vision_text, "vision_text"
    elif ocr_text.strip():
        primary, primary_field = ocr_text, "ocr_text"
    elif pdf_preview.strip():
        primary, primary_field = pdf_preview, "pdf_text_preview"
    elif caption.strip():
        primary, primary_field = caption, "caption"
    elif filename.strip():
        primary, primary_field = filename, "filename"

    # Compose the union of all available text so the extractor can
    # see auxiliary signals (caption + filename + body), without
    # losing the primary field tag for provenance.
    parts = [s for s in (
        primary, caption, filename,
        # Include the others when they aren't already the primary.
        pdf_preview if primary_field != "pdf_text_preview" else "",
    ) if s and s.strip()]
    text = "\n".join(parts).strip()

    # Truncation telemetry: ``True`` whenever we did NOT have a
    # full-text field available and had to fall back to the
    # 280-char preview as the only body source.
    has_full_text_field = bool(pdf_full.strip() or vision_text.strip() or ocr_text.strip())
    was_truncated = (not has_full_text_field) and bool(pdf_preview.strip())

    preview_len = len(pdf_preview)
    full_len    = len(primary)

    return text, primary_field, was_truncated, preview_len, full_len


# ── 7. Kill switch ──────────────────────────────────────────────────


def is_receipt_extraction_telemetry_enabled() -> bool:
    """Return ``True`` when
    ``RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED`` is set to a
    truthy value. Default OFF — staged rollout per merchant
    directive. Independent from the W1.1 contradiction-guard flag
    and the W1.2 verdict-telemetry flag."""
    raw = (
        os.environ.get("RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED") or ""
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


# ── 8. Pure orchestrator ────────────────────────────────────────────


def compute_receipt_fields(
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    extractor: Optional[ReceiptFieldsExtractor] = None,
) -> ReceiptFields:
    """Run the configured extractor over the full evidence text
    composed from ``metadata``. Returns a populated
    :class:`ReceiptFields` snapshot. Pure; never raises; never
    mutates ``metadata``.

    Parameters
    ----------
    metadata:
        Inbound metadata dict (caption, filename, pdf_text_full,
        pdf_text_preview, vision_text, ocr_text, …). ``None`` is
        treated as an empty mapping.
    extractor:
        Optional extractor override. When ``None``, the first
        registered extractor is used (currently
        :class:`RegexHeuristicExtractor`). Future waves may pass
        a different engine for A/B observation.
    """
    md = metadata or {}
    text, source_field, was_truncated, preview_len, full_len = (
        compose_full_evidence_text(md)
    )

    eng: Optional[ReceiptFieldsExtractor] = extractor
    if eng is None:
        if not _EXTRACTOR_REGISTRY:
            return ReceiptFields(
                source_engine="",
                source_text_field=source_field,
                source_text_length=len(text),
                source_text_was_truncated=was_truncated,
                source_text_preview_len=preview_len,
                source_text_full_len=full_len,
            )
        eng = _EXTRACTOR_REGISTRY[0]

    try:
        snapshot = eng.extract(text=text, metadata=md)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[PAYMENT_RECEIPT_EXTRACTED] extractor=%s raised; returning "
            "empty snapshot: %s", getattr(eng, "name", "<unknown>"), exc,
        )
        snapshot = ReceiptFields(source_engine=getattr(eng, "name", "") or "")

    # Re-stamp provenance fields the extractor itself may have
    # left blank. The extractor owns the field values and
    # confidences; the orchestrator owns the provenance.
    return ReceiptFields(
        ibans=snapshot.ibans,
        beneficiaries=snapshot.beneficiaries,
        bank_brands=snapshot.bank_brands,
        amounts=snapshot.amounts,
        references=snapshot.references,
        dates=snapshot.dates,
        iban_confidence=snapshot.iban_confidence,
        beneficiary_confidence=snapshot.beneficiary_confidence,
        bank_brand_confidence=snapshot.bank_brand_confidence,
        amount_confidence=snapshot.amount_confidence,
        reference_confidence=snapshot.reference_confidence,
        date_confidence=snapshot.date_confidence,
        overall_confidence=snapshot.overall_confidence,
        source_engine=snapshot.source_engine or getattr(eng, "name", ""),
        source_text_field=source_field,
        source_text_length=len(text),
        source_text_was_truncated=was_truncated,
        source_text_preview_len=preview_len,
        source_text_full_len=full_len,
        engine_diagnostics=snapshot.engine_diagnostics,
    )


# ── 9. Log emission ─────────────────────────────────────────────────


def log_receipt_fields(
    *,
    tenant_id: Any,
    phone: Optional[str] = None,
    conversation_id: Any = None,
    message_id: Any = None,
    source: str,
    fields: ReceiptFields,
) -> None:
    """Emit the canonical ``[PAYMENT_RECEIPT_EXTRACTED]`` log line.
    Gated by ``RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED``. Never
    raises.

    Field shape locked by
    ``test_log_line_carries_all_canonical_fields`` — drift fails
    the build.
    """
    if not is_receipt_extraction_telemetry_enabled():
        return
    try:
        masked_phone = ""
        if phone:
            try:
                masked_phone = "*" + str(phone)[-4:]
            except Exception:
                masked_phone = ""
        body = " ".join(f"{k}={v}" for k, v in fields.to_log_dict().items())
        logger.info(
            "[PAYMENT_RECEIPT_EXTRACTED] "
            "tenant_id=%s conversation_id=%s message_id=%s "
            "phone=%s source=%s %s",
            tenant_id, conversation_id, message_id,
            masked_phone, source, body,
        )
    except Exception:
        return


__all__ = [
    "FieldConfidence",
    "FIELD_CONFIDENCE_ALL",
    "FIELD_CONFIDENCE_VALUES",
    "ExtractedAmount",
    "ReceiptFields",
    "ReceiptFieldsExtractor",
    "RegexHeuristicExtractor",
    "register_extractor",
    "list_registered_extractors",
    "compose_full_evidence_text",
    "compute_receipt_fields",
    "log_receipt_fields",
    "is_receipt_extraction_telemetry_enabled",
]
