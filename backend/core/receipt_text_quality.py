"""
core/receipt_text_quality.py
────────────────────────────
P0 Bank Receipt Extraction Reliability — measurement layer.

Pure functions that score extracted receipt text quality and emit
**shadow** OCR-escalation signals for telemetry. This module MUST
NOT change payment decisions, order state, brain wording, or OCR
behaviour in the measurement phase.

What this module owns
─────────────────────
1. ``compute_text_quality`` — platform-wide text quality scoring for
   any OCR / PDF-extracted blob (not bank-specific).
2. ``is_garbled_text`` — derived garbled flag from the snapshot.
3. ``compute_ocr_escalation_shadow`` — observation-only decision:
   "would we escalate to Vision OCR?" without invoking Vision.

Kill switch
───────────
``RECEIPT_TEXT_QUALITY_TELEMETRY_ENABLED`` (default OFF) gates log
emission and optional metadata stamping at the normalizer call site.
The pure functions are always safe to call from tests and audit
scripts regardless of the flag.

Canonical log targets
─────────────────────
::

    [RECEIPT_TEXT_QUALITY]
    tenant_id=<int> media_id=<str|None> source=<call_site>
    text_length=<int> quality_score=<float> is_garbled=<bool>
    symbol_ratio=<float> glued_token_count=<int>
    arabic_garble_ratio=<float> readable_letter_ratio=<float>
    garble_reasons=<comma-separated>

    [RECEIPT_OCR_ESCALATION_SHADOW]
    tenant_id=<int> media_id=<str|None> source=<call_site>
    would_escalate=<bool> shadow_reason=<label>
    pdf_kind=<str> pdf_text_status=<str> quality_score=<float>
    is_garbled=<bool> amount_confidence=<str> iban_confidence=<str>
    beneficiary_confidence=<str> ocr_not_invoked=true
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping, Optional, Tuple

logger = logging.getLogger("nahla.receipt_text_quality")


# ── Platform-wide thresholds (not bank / tenant specific) ───────────

QUALITY_SCORE_LOW_THRESHOLD: float = 0.65
SYMBOL_RATIO_GARBLED_THRESHOLD: float = 0.06
ARABIC_GARBLE_RATIO_THRESHOLD: float = 0.35
READABLE_LETTER_RATIO_LOW: float = 0.25
GLUED_TOKEN_MIN_LEN: int = 10

# Payment-shaped document kinds — used only for shadow escalation
# telemetry, never for payment decisions in this module.
_PAYMENT_PDF_KINDS: FrozenSet[str] = frozenset({
    "payment_receipt",
    "payment_pre_review",
    "payment_pending_evidence",
})

# Common Arabic letters (platform-wide alphabet check, not a lexicon).
_ARABIC_LETTERS: FrozenSet[str] = frozenset(
    "ابتثجحخدذرزسشصضطظعغفقكلمنهويىءآأإةؤئ "
)

# Control chars + noisy symbols often seen in corrupted PDF streams.
_NOISY_SYMBOLS: FrozenSet[str] = frozenset("%#@^&*~|\\<>{}[]")

# Saudi IBAN token shape — exclude from glued-token heuristics.
_IBAN_TOKEN_RE = re.compile(r"^SA[\d\s\-]{20,}$", re.IGNORECASE)


@dataclass(frozen=True)
class TextQualitySnapshot:
    """Frozen quality observation for a single text blob."""

    text_length: int = 0
    quality_score: float = 0.0
    is_garbled: bool = False
    symbol_ratio: float = 0.0
    glued_token_count: int = 0
    arabic_garble_ratio: float = 0.0
    readable_letter_ratio: float = 0.0
    garble_reasons: Tuple[str, ...] = ()

    def to_log_dict(self) -> dict:
        return {
            "text_length":            self.text_length,
            "quality_score":          round(self.quality_score, 4),
            "is_garbled":             self.is_garbled,
            "symbol_ratio":           round(self.symbol_ratio, 4),
            "glued_token_count":      self.glued_token_count,
            "arabic_garble_ratio":    round(self.arabic_garble_ratio, 4),
            "readable_letter_ratio":  round(self.readable_letter_ratio, 4),
            "garble_reasons":         ",".join(self.garble_reasons) or "-",
        }


@dataclass(frozen=True)
class OcrEscalationShadow:
    """Shadow-only OCR escalation observation. Never invokes Vision."""

    would_escalate: bool = False
    shadow_reason: str = "not_applicable"
    is_payment_candidate: bool = False
    pypdf_succeeded: bool = False
    quality: Optional[TextQualitySnapshot] = None
    amount_confidence: str = "absent"
    iban_confidence: str = "absent"
    beneficiary_confidence: str = "absent"

    def to_log_dict(self) -> dict:
        q = self.quality or TextQualitySnapshot()
        return {
            "would_escalate":         self.would_escalate,
            "shadow_reason":          self.shadow_reason,
            "is_payment_candidate":   self.is_payment_candidate,
            "pypdf_succeeded":        self.pypdf_succeeded,
            "quality_score":          round(q.quality_score, 4),
            "is_garbled":             q.is_garbled,
            "amount_confidence":      self.amount_confidence,
            "iban_confidence":        self.iban_confidence,
            "beneficiary_confidence": self.beneficiary_confidence,
            "ocr_not_invoked":        True,
        }


def is_receipt_text_quality_telemetry_enabled() -> bool:
    """Return ``True`` when ``RECEIPT_TEXT_QUALITY_TELEMETRY_ENABLED``
    is truthy. Default OFF — measurement phase only."""
    raw = (
        os.environ.get("RECEIPT_TEXT_QUALITY_TELEMETRY_ENABLED") or ""
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _count_glued_tokens(text: str) -> int:
    """Count single tokens with case-transition gluing (e.g.
    ``SARAmount``, ``TransferReceipt``). Platform-wide; excludes
    Saudi IBAN tokens and bare digit runs."""
    count = 0
    for word in re.findall(r"\b[^\s]{4,}\b", text):
        if _IBAN_TOKEN_RE.match(word.replace(" ", "").replace("-", "")):
            continue
        if word.replace(".", "").replace(",", "").isdigit():
            continue
        if re.search(r"[a-z][A-Z]", word):
            count += 1
            continue
        if re.search(r"[A-Z]{2,}[a-z]", word):
            count += 1
    return count


def is_receipt_measurement_telemetry_enabled() -> bool:
    """Return ``True`` when any receipt measurement flag is on.
    Convenience helper for operators enabling the 7–14 day window."""
    if is_receipt_text_quality_telemetry_enabled():
        return True
    try:
        from core.receipt_extraction import (  # noqa: PLC0415
            is_receipt_extraction_telemetry_enabled,
        )
        if is_receipt_extraction_telemetry_enabled():
            return True
    except Exception:
        pass
    try:
        from core.receipt_verdict import (  # noqa: PLC0415
            is_receipt_verdict_telemetry_enabled,
        )
        if is_receipt_verdict_telemetry_enabled():
            return True
    except Exception:
        pass
    return False


def compute_text_quality(text: Optional[str]) -> TextQualitySnapshot:
    """Score extracted text quality. Pure; never raises.

    Higher ``quality_score`` means more readable / trustworthy text.
    ``is_garbled`` is ``True`` when one or more garble heuristics
    fire — platform-wide, not bank-specific.
    """
    try:
        return _compute_text_quality_unsafe(text)
    except Exception:
        return TextQualitySnapshot()


def is_garbled_text(text: Optional[str]) -> bool:
    """Return ``True`` when :func:`compute_text_quality` marks the
    blob as garbled. Convenience wrapper for callers and tests."""
    return compute_text_quality(text).is_garbled


def _compute_text_quality_unsafe(text: Optional[str]) -> TextQualitySnapshot:
    raw = (text or "").strip()
    if not raw:
        return TextQualitySnapshot(
            text_length=0,
            quality_score=0.0,
            is_garbled=False,
            garble_reasons=(),
        )

    n = len(raw)
    reasons: list = []

    symbol_count = sum(
        1 for c in raw
        if unicodedata.category(c).startswith("C") or c in _NOISY_SYMBOLS
    )
    symbol_ratio = symbol_count / max(n, 1)
    if symbol_ratio >= SYMBOL_RATIO_GARBLED_THRESHOLD:
        reasons.append("high_symbol_ratio")

    glued_count = _count_glued_tokens(raw)
    if glued_count >= 1:
        reasons.append("glued_tokens")

    arabic_chars = [c for c in raw if "\u0600" <= c <= "\u06FF"]
    arabic_count = len(arabic_chars)
    arabic_garble_count = sum(
        1 for c in arabic_chars if c not in _ARABIC_LETTERS
    )
    arabic_garble_ratio = (
        arabic_garble_count / max(arabic_count, 1) if arabic_count > 0
        else 0.0
    )
    if arabic_count >= 4 and arabic_garble_ratio >= ARABIC_GARBLE_RATIO_THRESHOLD:
        reasons.append("arabic_garble")

    latin_count = sum(1 for c in raw if ("A" <= c <= "Z") or ("a" <= c <= "z"))
    letter_count = latin_count + arabic_count
    readable_letter_ratio = letter_count / max(n, 1)
    if readable_letter_ratio < READABLE_LETTER_RATIO_LOW:
        reasons.append("low_letter_ratio")

    score = 1.0
    score -= min(0.45, symbol_ratio * 4.0)
    score -= min(0.25, glued_count * 0.12)
    if arabic_count > 3:
        score -= min(0.35, arabic_garble_ratio * 1.5)
    if readable_letter_ratio < READABLE_LETTER_RATIO_LOW:
        score -= 0.15
    score = max(0.0, min(1.0, score))

    is_garbled = score < QUALITY_SCORE_LOW_THRESHOLD

    return TextQualitySnapshot(
        text_length=n,
        quality_score=round(score, 4),
        is_garbled=is_garbled,
        symbol_ratio=round(symbol_ratio, 4),
        glued_token_count=glued_count,
        arabic_garble_ratio=round(arabic_garble_ratio, 4),
        readable_letter_ratio=round(readable_letter_ratio, 4),
        garble_reasons=tuple(reasons),
    )


def _core_fields_unreliable(
    *,
    amount_confidence: str,
    iban_confidence: str,
    beneficiary_confidence: str,
) -> bool:
    """``True`` when two or more core bank fields are absent/low."""
    weak = {"absent", "low"}
    weak_count = sum(
        1 for c in (
            amount_confidence,
            iban_confidence,
            beneficiary_confidence,
        )
        if (c or "").lower() in weak
    )
    return weak_count >= 2


def compute_ocr_escalation_shadow(
    *,
    text: Optional[str],
    pdf_kind: Optional[str] = None,
    pdf_text_status: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> OcrEscalationShadow:
    """Shadow-only: would Vision OCR be warranted? Never invokes OCR.

    Parameters
    ----------
    text:
        The pypdf-extracted body (before any live OCR escalation).
    pdf_kind:
        Document classifier output from the normalizer.
    pdf_text_status:
        ``ok`` | ``empty`` | ``ocr`` | … from pypdf / OCR path.
    metadata:
        Optional inbound metadata for field-confidence observation
        via :func:`core.receipt_extraction.compute_receipt_fields`.
    """
    try:
        return _compute_ocr_escalation_shadow_unsafe(
            text=text,
            pdf_kind=pdf_kind,
            pdf_text_status=pdf_text_status,
            metadata=metadata,
        )
    except Exception:
        return OcrEscalationShadow(shadow_reason="shadow_compute_error")


def _compute_ocr_escalation_shadow_unsafe(
    *,
    text: Optional[str],
    pdf_kind: Optional[str],
    pdf_text_status: Optional[str],
    metadata: Optional[Mapping[str, Any]],
) -> OcrEscalationShadow:
    kind = (pdf_kind or "").strip().lower()
    status = (pdf_text_status or "").strip().lower()
    is_payment_candidate = kind in _PAYMENT_PDF_KINDS
    pypdf_succeeded = status == "ok" and bool((text or "").strip())

    quality = compute_text_quality(text)

    amount_conf = iban_conf = beneficiary_conf = "absent"
    if metadata:
        try:
            from core.receipt_extraction import compute_receipt_fields  # noqa: PLC0415
            fields = compute_receipt_fields(metadata={
                **dict(metadata),
                "pdf_text_full": text or metadata.get("pdf_text_full") or "",
            })
            amount_conf = fields.amount_confidence.value
            iban_conf = fields.iban_confidence.value
            beneficiary_conf = fields.beneficiary_confidence.value
        except Exception:
            pass

    if not is_payment_candidate:
        return OcrEscalationShadow(
            would_escalate=False,
            shadow_reason="not_payment_candidate",
            is_payment_candidate=False,
            pypdf_succeeded=pypdf_succeeded,
            quality=quality,
            amount_confidence=amount_conf,
            iban_confidence=iban_conf,
            beneficiary_confidence=beneficiary_conf,
        )

    if status == "ocr":
        return OcrEscalationShadow(
            would_escalate=False,
            shadow_reason="already_ocr_path",
            is_payment_candidate=True,
            pypdf_succeeded=False,
            quality=quality,
            amount_confidence=amount_conf,
            iban_confidence=iban_conf,
            beneficiary_confidence=beneficiary_conf,
        )

    if not (text or "").strip():
        return OcrEscalationShadow(
            would_escalate=True,
            shadow_reason="empty_pypdf_text",
            is_payment_candidate=True,
            pypdf_succeeded=False,
            quality=quality,
            amount_confidence=amount_conf,
            iban_confidence=iban_conf,
            beneficiary_confidence=beneficiary_conf,
        )

    core_weak = _core_fields_unreliable(
        amount_confidence=amount_conf,
        iban_confidence=iban_conf,
        beneficiary_confidence=beneficiary_conf,
    )

    if quality.is_garbled and core_weak:
        return OcrEscalationShadow(
            would_escalate=True,
            shadow_reason="garbled_and_core_fields_unreliable",
            is_payment_candidate=True,
            pypdf_succeeded=pypdf_succeeded,
            quality=quality,
            amount_confidence=amount_conf,
            iban_confidence=iban_conf,
            beneficiary_confidence=beneficiary_conf,
        )

    if quality.is_garbled:
        return OcrEscalationShadow(
            would_escalate=True,
            shadow_reason="garbled_text",
            is_payment_candidate=True,
            pypdf_succeeded=pypdf_succeeded,
            quality=quality,
            amount_confidence=amount_conf,
            iban_confidence=iban_conf,
            beneficiary_confidence=beneficiary_conf,
        )

    if (
        quality.quality_score < QUALITY_SCORE_LOW_THRESHOLD
        and core_weak
    ):
        return OcrEscalationShadow(
            would_escalate=True,
            shadow_reason="low_quality_and_core_fields_unreliable",
            is_payment_candidate=True,
            pypdf_succeeded=pypdf_succeeded,
            quality=quality,
            amount_confidence=amount_conf,
            iban_confidence=iban_conf,
            beneficiary_confidence=beneficiary_conf,
        )

    return OcrEscalationShadow(
        would_escalate=False,
        shadow_reason="quality_acceptable",
        is_payment_candidate=True,
        pypdf_succeeded=pypdf_succeeded,
        quality=quality,
        amount_confidence=amount_conf,
        iban_confidence=iban_conf,
        beneficiary_confidence=beneficiary_conf,
    )


def log_text_quality(
    *,
    tenant_id: Any,
    media_id: Optional[str] = None,
    source: str,
    snapshot: TextQualitySnapshot,
    pdf_text_status: Optional[str] = None,
) -> None:
    """Emit ``[RECEIPT_TEXT_QUALITY]``. Gated; never raises."""
    if not is_receipt_text_quality_telemetry_enabled():
        return
    try:
        body = " ".join(
            f"{k}={v}" for k, v in snapshot.to_log_dict().items()
        )
        logger.info(
            "[RECEIPT_TEXT_QUALITY] tenant_id=%s media_id=%s "
            "source=%s pdf_text_status=%s %s",
            tenant_id, media_id, source, pdf_text_status or "-", body,
        )
    except Exception:
        return


def log_ocr_escalation_shadow(
    *,
    tenant_id: Any,
    media_id: Optional[str] = None,
    source: str,
    pdf_kind: Optional[str] = None,
    pdf_text_status: Optional[str] = None,
    shadow: OcrEscalationShadow,
) -> None:
    """Emit ``[RECEIPT_OCR_ESCALATION_SHADOW]``. Gated; never raises."""
    if not is_receipt_text_quality_telemetry_enabled():
        return
    try:
        body = " ".join(
            f"{k}={v}" for k, v in shadow.to_log_dict().items()
        )
        logger.info(
            "[RECEIPT_OCR_ESCALATION_SHADOW] tenant_id=%s media_id=%s "
            "source=%s pdf_kind=%s pdf_text_status=%s %s",
            tenant_id, media_id, source,
            pdf_kind or "-", pdf_text_status or "-", body,
        )
    except Exception:
        return


def stamp_measurement_metadata(
    base_meta: dict,
    *,
    pypdf_text: str,
    pypdf_status: str,
) -> None:
    """Add observation-only fields to inbound metadata when the
    measurement flag is on. Mutates ``base_meta`` in place; never
    raises. Does NOT affect payment or brain behaviour."""
    if not is_receipt_text_quality_telemetry_enabled():
        return
    try:
        quality = compute_text_quality(pypdf_text)
        base_meta["receipt_text_quality_score"] = quality.quality_score
        base_meta["receipt_text_is_garbled"] = quality.is_garbled
        base_meta["receipt_text_garble_reasons"] = list(quality.garble_reasons)
        base_meta["pdf_pypdf_text_status"] = pypdf_status or None
    except Exception:
        return


def emit_document_receipt_measurement(
    *,
    tenant_id: Any,
    media_id: Optional[str],
    base_meta: dict,
    pypdf_text: str,
) -> None:
    """Normaliser call site: quality log + shadow escalation log +
    optional field-extraction telemetry. Observation only."""
    if not is_receipt_measurement_telemetry_enabled():
        return
    try:
        pdf_kind = str(base_meta.get("pdf_kind") or "")
        pdf_status = str(base_meta.get("pdf_text_status") or "")
        pypdf_status = str(
            base_meta.get("pdf_pypdf_text_status") or pdf_status
        )

        quality = compute_text_quality(pypdf_text)
        log_text_quality(
            tenant_id=tenant_id,
            media_id=media_id,
            source="document_pdf",
            snapshot=quality,
            pdf_text_status=pypdf_status,
        )

        shadow = compute_ocr_escalation_shadow(
            text=pypdf_text,
            pdf_kind=pdf_kind,
            pdf_text_status=pypdf_status,
            metadata=base_meta,
        )
        base_meta["receipt_ocr_escalation_shadow"] = shadow.would_escalate
        base_meta["receipt_ocr_escalation_reason"] = shadow.shadow_reason

        log_ocr_escalation_shadow(
            tenant_id=tenant_id,
            media_id=media_id,
            source="document_pdf",
            pdf_kind=pdf_kind,
            pdf_text_status=pypdf_status,
            shadow=shadow,
        )

        from core.receipt_extraction import (  # noqa: PLC0415
            compute_receipt_fields,
            is_receipt_extraction_telemetry_enabled,
            log_receipt_fields,
        )
        if is_receipt_extraction_telemetry_enabled():
            fields = compute_receipt_fields(metadata=base_meta)
            log_receipt_fields(
                tenant_id=tenant_id,
                message_id=media_id,
                source="document_pdf",
                fields=fields,
            )
    except Exception:
        return


__all__ = [
    "QUALITY_SCORE_LOW_THRESHOLD",
    "TextQualitySnapshot",
    "OcrEscalationShadow",
    "compute_text_quality",
    "is_garbled_text",
    "compute_ocr_escalation_shadow",
    "is_receipt_text_quality_telemetry_enabled",
    "is_receipt_measurement_telemetry_enabled",
    "log_text_quality",
    "log_ocr_escalation_shadow",
    "stamp_measurement_metadata",
    "emit_document_receipt_measurement",
]
