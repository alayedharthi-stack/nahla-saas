"""
modules/ai/media/normalizer.py
──────────────────────────────
Normalize WhatsApp inbound payloads into one internal shape so the
rest of the pipeline (webhook router → brain → reply) never needs to
think about ``message.type``.

Phase 1 scope (this file):
  * ``text``                — pass-through.
  * ``interactive``         — button/list replies.
  * ``audio`` / ``voice``   — download → persist → Whisper transcribe.
  * ``image``               — download → persist → OpenAI Vision describe.

Hardening over the original Phase-0 implementation
──────────────────────────────────────────────────
* Bytes are persisted to ``services.inbound_media_storage`` BEFORE we
  call any model. Meta's media URL expires in ~5 minutes; relying on
  it for re-tries / dashboard playback is a guaranteed dead-link bug.
* Status fields are granular and structured (``audio_download_status``,
  ``transcript_status``, ``transcript_error``) so the merchant can see
  exactly which stage failed — instead of one opaque ``reason: failed``.
* The result carries an ``ai_used_*`` flag so the conversation drawer
  can show "🎙️ نحلة استمعت إلى الرسالة" badges with confidence.
* On transcription / vision failure we DO NOT lose the message: the
  webhook gets ``fallback_reply_ar`` and persists the inbound row
  with a permanent storage URL so the merchant can replay it.

Storage:
  ``uploads/inbound-media/<tenant_id>/<YYYYMM>/<sha256>.<ext>``
  Served via ``GET /media/inbound/<tenant_id>/<slug>``.

Tests live in ``tests/test_inbound_media.py``.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Optional

import httpx

from core.config import (
    D360_API_BASE_URL,
    INBOUND_MEDIA_MAX_BYTES,
    META_GRAPH_API_VERSION,
    NAHLA_STT_LANGUAGE,
    OPENAI_API_BASE,
    OPENAI_AUDIO_MODEL,
    OPENAI_VISION_MODEL,
)
from services.inbound_media_storage import save_inbound_media
from services.whatsapp_platform.provider_utils import (
    WHATSAPP_PROVIDER_360DIALOG,
    wa_provider,
)
from services.whatsapp_platform.token_manager import get_token_for_operation

logger = logging.getLogger("nahla.ai.media")


# ── Runtime env getters + process diagnostics ───────────────────────
#
# Why we re-read OPENAI_API_KEY from os.environ on every call instead
# of importing the constant from core.config:
#
#   `core.config.OPENAI_API_KEY` is evaluated ONCE at module load.
#   On Railway, if the env var is set AFTER a service's process has
#   already started (or a service is deployed before the var is
#   provisioned), that process captures the empty string forever —
#   even though a SIBLING service (e.g. `web` vs `worker`) booted
#   with the var present and works correctly.
#
#   This produced a confusing symptom: GET /admin/debug/media-env
#   (served by `web`) showed `openai_key_present: true`, while
#   inbound media messages handled by `worker` showed
#   "OPENAI_API_KEY مفقود". Same repo, same code — different env
#   timing at process start.
#
# Re-reading from os.environ lets a process pick up a newly-set var
# on the NEXT inbound media event without needing a full redeploy.
# A simple `kill -SIGTERM` (or Railway "Restart") is enough. A fresh
# deploy is no longer the only path to recovery.

def _runtime_openai_key() -> str:
    """Fresh re-read of OPENAI_API_KEY on every call. Returns empty
    string when unset. Never raises."""
    return os.environ.get("OPENAI_API_KEY") or ""


def _service_role() -> str:
    """Best-effort identification of which Railway service this
    process belongs to. Used in diagnostic log lines so support can
    answer "which process is missing the env var?" by grep."""
    # Railway sets this for every service at deploy time.
    rail = os.environ.get("RAILWAY_SERVICE_NAME")
    if rail:
        return rail
    # Manual override (useful for local docker-compose where each
    # container can set its own NAHLA_SERVICE_ROLE).
    manual = os.environ.get("NAHLA_SERVICE_ROLE")
    if manual:
        return manual
    # Fall back to inferring from argv. We can't tell web from worker
    # reliably without the env var, so this is informational only.
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    if "uvicorn" in argv0 or "uvicorn" in " ".join(sys.argv):
        return "web?"
    if any(s in argv0.lower() for s in ("worker", "celery", "rq", "arq")):
        return "worker?"
    if "scheduler" in argv0.lower() or "cron" in argv0.lower():
        return "scheduler?"
    return "unknown"


# Boot-time snapshot of the env. Captured ONCE at module import so we
# can compare against the runtime value on every skip — if they
# differ, the operator knows the env was changed mid-process and the
# fix is just to restart this service.
_BOOT_PID = os.getpid()
_BOOT_SERVICE = _service_role()
_BOOT_OPENAI_KEY_PRESENT = bool(_runtime_openai_key())
_BOOT_FFMPEG_FOUND = shutil.which("ffmpeg") is not None

# Boot diagnostic — one line per process. Search Railway logs for
# `[MEDIA_NORMALIZER_BOOT]` to enumerate every process that has
# loaded this module and the env state it saw at boot. If `web`
# logs `openai_key_present_at_boot=True` but `worker` logs
# `openai_key_present_at_boot=False`, the worker just needs a
# restart (env is now present in Railway, the process just hasn't
# picked it up).
logger.info(
    "[MEDIA_NORMALIZER_BOOT] pid=%d service=%s "
    "openai_key_present_at_boot=%s vision_model=%s audio_model=%s "
    "stt_language=%s ffmpeg_found=%s",
    _BOOT_PID, _BOOT_SERVICE,
    _BOOT_OPENAI_KEY_PRESENT, OPENAI_VISION_MODEL, OPENAI_AUDIO_MODEL,
    NAHLA_STT_LANGUAGE, _BOOT_FFMPEG_FOUND,
)


def _log_skip(reason: str, *, tenant_id: Any, media_id: Any, kind: str) -> None:
    """Emit a structured WARN line every time the normalizer skips
    audio/image processing due to a missing OpenAI key. Carries both
    the boot-time and current env state so an operator can tell
    instantly whether a process restart will fix the issue."""
    current_present = bool(_runtime_openai_key())
    logger.warning(
        "[MEDIA_NORMALIZER_SKIP] reason=%s kind=%s pid=%d service=%s "
        "openai_key_present_now=%s openai_key_present_at_boot=%s "
        "tenant=%s media_id=%s%s",
        reason, kind, _BOOT_PID, _BOOT_SERVICE,
        current_present, _BOOT_OPENAI_KEY_PRESENT,
        tenant_id, media_id,
        (" (restart this service to pick up the env var)"
         if (current_present and not _BOOT_OPENAI_KEY_PRESENT) else ""),
    )

# ── Canonical Arabic fallbacks ──────────────────────────────────────
# Surfaced to the customer when we can't extract any usable text from
# a media message. Kept here so every code path that gives up on media
# uses the exact same wording — operators have a single grep target
# when they want to tweak the copy.
AUDIO_FALLBACK_REPLY_AR = (
    "وصلني التسجيل، لكن لم أتمكن من سماعه بوضوح. "
    "ممكن تكتب طلبك؟"
)
IMAGE_FALLBACK_REPLY_AR = (
    "وصلتني الصورة، لكن لم أتمكن من قراءة محتواها بوضوح. "
    "ممكن توضح طلبك بنص؟"
)
DOCUMENT_FALLBACK_REPLY_AR = (
    "وصلني الملف، لكن لم أتمكن من قراءة محتواه. "
    "ممكن توضح غرض الملف بنص؟"
)


# ── PDF / document heuristic classifier ─────────────────────────────
#
# Lightweight, dependency-free classifier for inbound WhatsApp
# documents. We do NOT extract PDF text — that requires shipping a
# new dependency (``pypdf`` / ``pdfplumber``) and the parsing of
# Saudi bank receipts is unreliable enough that we'd still need
# heuristics on top. Instead we lean on:
#
#   * The document's ``filename`` (e.g. "Transfer-Receipt.pdf",
#     "إيصال_التحويل.pdf").
#   * The document's ``caption`` (text the customer typed alongside).
#   * The merchant's recent conversation context (passed in by the
#     webhook): if the bot just asked for an ``إيصال`` or there's an
#     active product focus with a confirmed price + address, a PDF
#     in that moment is overwhelmingly a payment receipt.
#
# Categories produced:
#   * ``payment_receipt``  → bank-transfer receipt, deposit slip
#   * ``invoice``          → tax/sales invoice the customer forwarded
#   * ``identity``         → ID / passport scan
#   * ``shipping_label``   → courier waybill
#   * ``catalog``          → product catalog the customer shared
#   * ``unknown``          → couldn't decide; treat as generic doc
#
# The classifier is deliberately fuzzy-conservative: we accept some
# false positives on ``payment_receipt`` because the downstream
# response just says "we received the receipt, the order is under
# review" — even if the document was actually an invoice, that's a
# reasonable thing to say after an order confirmation flow.

_PDF_RECEIPT_FILENAME_KEYWORDS = (
    "receipt", "transfer", "rajhi", "stcpay", "alinma", "alahli",
    "snb", "ncb", "sabb", "barwa", "albilad", "anb",
    "إيصال", "ايصال", "تحويل", "حواله", "حوالة", "تحوي",
    "transferreceipt", "remittance", "payment",
)
_PDF_INVOICE_KEYWORDS = (
    "invoice", "فاتورة", "فاتوره", "tax-invoice", "vat",
)
_PDF_IDENTITY_KEYWORDS = (
    "id-card", "id_", "passport", "هوية", "جواز",
)
_PDF_SHIPPING_KEYWORDS = (
    "waybill", "label", "smsa", "aramex", "dhl", "redbox",
    "بوليصة", "بوليصه", "شحنة", "شحنه",
)
_PDF_CATALOG_KEYWORDS = (
    "catalog", "catalogue", "كتالوج", "كاتالوج", "كتالوغ",
)


def classify_inbound_document(
    *,
    filename: Optional[str],
    caption: Optional[str],
    mime_type: Optional[str],
    order_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Heuristic-classify an inbound PDF/document into one of the
    enum slots above. ``order_context`` is an optional hint passed
    in by the webhook so the classifier can boost the
    ``payment_receipt`` verdict when the conversation is already
    inside a transfer-payment flow:

        order_context = {
            "awaiting_payment_receipt": bool,
            "has_active_order":         bool,  # product + price known
            "has_address":              bool,
            "selected_product":         str|None,
            "price":                    float|None,
        }

    Return shape::

        {
          "category":   "payment_receipt" | ... | "unknown",
          "confidence": "high" | "medium" | "low",
          "reasons":    [<arabic-or-en label, …>],
          "signals": {
              "filename_matched": bool,
              "caption_matched":  bool,
              "context_boosted":  bool,
          },
        }

    Never raises. Pure-Python — safe to call from any path.
    """
    fn = (filename or "").lower()
    cap = (caption or "").lower()
    blob = f"{fn}  {cap}"

    reasons: list = []
    fn_match = False
    cap_match = False

    # Pass 1 — keyword scan on filename + caption.
    if any(k in blob for k in _PDF_RECEIPT_FILENAME_KEYWORDS):
        category = "payment_receipt"
        # Decide whether the hit was in filename or caption for tracing.
        fn_match = any(k in fn for k in _PDF_RECEIPT_FILENAME_KEYWORDS)
        cap_match = any(k in cap for k in _PDF_RECEIPT_FILENAME_KEYWORDS)
        reasons.append("filename/caption matches receipt keyword")
        confidence = "high" if (fn_match or cap_match) else "medium"
    elif any(k in blob for k in _PDF_INVOICE_KEYWORDS):
        category = "invoice"
        reasons.append("filename/caption matches invoice keyword")
        confidence = "medium"
    elif any(k in blob for k in _PDF_IDENTITY_KEYWORDS):
        category = "identity"
        reasons.append("filename/caption matches identity keyword")
        confidence = "medium"
    elif any(k in blob for k in _PDF_SHIPPING_KEYWORDS):
        category = "shipping_label"
        reasons.append("filename/caption matches shipping keyword")
        confidence = "medium"
    elif any(k in blob for k in _PDF_CATALOG_KEYWORDS):
        category = "catalog"
        reasons.append("filename/caption matches catalog keyword")
        confidence = "low"
    else:
        category = "unknown"
        confidence = "low"

    # Pass 2 — context boost. A "no-keyword" PDF arriving during an
    # active payment-receipt waiting state gets promoted to a
    # receipt. This is the single most impactful heuristic: most
    # Saudi banks generate PDF receipts with timestamp-only filenames
    # like ``document_1778767962508.pdf`` that match none of the
    # keyword lists.
    context_boosted = False
    if order_context:
        awaiting = bool(order_context.get("awaiting_payment_receipt"))
        active   = bool(order_context.get("has_active_order"))
        has_addr = bool(order_context.get("has_address"))
        if awaiting and category in ("unknown", "payment_receipt"):
            category = "payment_receipt"
            confidence = "high"
            reasons.append("awaiting_payment_receipt context")
            context_boosted = True
        elif active and has_addr and category == "unknown":
            # Product + address are locked in — almost certainly a
            # payment receipt the customer is sending unprompted.
            category = "payment_receipt"
            confidence = "medium"
            reasons.append("active_order_with_address context")
            context_boosted = True

    return {
        "category":   category,
        "confidence": confidence,
        "reasons":    reasons,
        "signals": {
            "filename_matched": fn_match,
            "caption_matched":  cap_match,
            "context_boosted":  context_boosted,
        },
    }


@dataclass
class MediaNormalizationResult:
    """Single contract returned by ``normalize_whatsapp_inbound``.

    Most fields are optional — text / interactive turns just leave
    media-only data empty. The webhook keys off:

      * ``should_process`` — whether we have enough text to call the
        brain. False for unsupported types AND for media that failed
        to transcribe / describe but DIDN'T leave us a caption.
      * ``fallback_reply_ar`` — when set, the webhook sends THIS
        instead of running the brain. Used for media we received
        successfully but couldn't extract text from.
      * ``metadata`` — full structured payload that gets stamped onto
        ``MessageEvent.extra_metadata.normalized_inbound`` for the
        media-debug endpoint and the conversation drawer player.
    """
    normalized_type: str = "unsupported"
    text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    should_process: bool = False
    # NEW: explicit fallback reply for the "we got the media but
    # couldn't extract usable text" branch. None means "no special
    # handling needed — the dispatcher behaves as before".
    fallback_reply_ar: Optional[str] = None


# ── Public entry point ──────────────────────────────────────────────


async def normalize_whatsapp_inbound(
    *,
    db: Any,
    wa_conn: Any,
    tenant_id: int,
    message: Dict[str, Any],
    order_context: Optional[Dict[str, Any]] = None,
) -> MediaNormalizationResult:
    msg_type = str(message.get("type") or "").strip()
    ts_raw = message.get("timestamp")
    wa_msg_id = str(message.get("id") or "").strip()

    if msg_type == "text":
        text = str((message.get("text") or {}).get("body") or "").strip()
        return MediaNormalizationResult(
            normalized_type="text",
            text=text,
            metadata={
                "source_type":  "text",
                "wa_timestamp": ts_raw,
                "wa_message_id": wa_msg_id or None,
            },
            should_process=bool(text),
        )

    if msg_type == "interactive":
        return MediaNormalizationResult(
            normalized_type="interactive",
            metadata={
                "source_type":  "interactive",
                "interactive":  message.get("interactive", {}),
                "wa_timestamp": ts_raw,
                "wa_message_id": wa_msg_id or None,
            },
            should_process=True,
        )

    if msg_type in {"audio", "voice"}:
        return await _process_audio(
            db=db,
            wa_conn=wa_conn,
            tenant_id=tenant_id,
            audio_payload=message.get("audio") or {},
            ts_raw=ts_raw,
            wa_msg_id=wa_msg_id,
            is_voice_note=(msg_type == "voice"),
        )

    if msg_type == "image":
        return await _process_image(
            db=db,
            wa_conn=wa_conn,
            tenant_id=tenant_id,
            image_payload=message.get("image") or {},
            ts_raw=ts_raw,
            wa_msg_id=wa_msg_id,
            order_context=order_context,
        )

    if msg_type == "document":
        # PDFs and other document attachments. Before this branch
        # existed PDFs were silently dropped at the webhook
        # (``INBOUND_IGNORED_UNSUPPORTED``) — a customer who sent a
        # bank-transfer receipt got NO acknowledgement and the AI
        # re-asked product discovery on the next text turn. We now
        # download the document, persist it for the merchant
        # drawer, heuristically classify it (payment_receipt /
        # invoice / identity / shipping / catalog / unknown) and
        # push a structured "[وثيقة PDF — تصنيف: X]" text into the
        # brain so downstream rules can react.
        return await _process_document(
            db=db,
            wa_conn=wa_conn,
            tenant_id=tenant_id,
            document_payload=message.get("document") or {},
            ts_raw=ts_raw,
            wa_msg_id=wa_msg_id,
            order_context=order_context,
        )

    return MediaNormalizationResult(
        normalized_type=msg_type or "unsupported",
        metadata={
            "source_type":  msg_type or "unsupported",
            "wa_timestamp": ts_raw,
            "wa_message_id": wa_msg_id or None,
        },
        should_process=False,
    )


# ── Audio (voice note + audio file) ─────────────────────────────────


async def _process_audio(
    *,
    db: Any,
    wa_conn: Any,
    tenant_id: int,
    audio_payload: Dict[str, Any],
    ts_raw: Any,
    wa_msg_id: str,
    is_voice_note: bool,
) -> MediaNormalizationResult:
    """Download → persist → transcribe an inbound audio payload.

    Returns a MediaNormalizationResult whose ``metadata`` always
    includes the per-stage status fields the merchant brain and the
    media-debug endpoint key off. Every short-circuit (missing
    credentials, download failure, transcription failure) STILL goes
    through the storage stage when possible, so the conversation
    drawer can play the recording even when STT bailed.
    """
    media_id = str(audio_payload.get("id") or "").strip()
    mime_type = str(audio_payload.get("mime_type") or "").strip()
    # Meta sometimes ships ``audio.voice`` as a boolean; default to
    # the message-type-based detection when absent.
    voice_flag = bool(audio_payload.get("voice", is_voice_note))
    # WhatsApp does not currently expose ``duration`` on inbound
    # audio payloads, but 360dialog occasionally relays it. Read
    # defensively so it ends up in the debug payload when present.
    duration_seconds = audio_payload.get("duration")
    caption = str(audio_payload.get("caption") or "").strip()

    base_meta: Dict[str, Any] = {
        "source_type":            "audio",
        "media_id":               media_id or None,
        "mime_type":              mime_type or None,
        "voice":                  voice_flag,
        "caption":                caption or None,
        "duration_seconds":       duration_seconds,
        "wa_timestamp":           ts_raw,
        "wa_message_id":          wa_msg_id or None,
        "audio_download_status":  "pending",
        "transcript_status":      "pending",
        "transcript_text":        None,
        "transcript_error":       None,
        "ai_used_audio":          False,
        "storage_url":            None,
        "storage_sha256":         None,
        "byte_size":              None,
    }

    if not media_id:
        return _audio_failure(
            base_meta,
            download_status="failed",
            transcript_status="skipped",
            transcript_error="missing_media_id",
            caption=caption,
        )

    if not _runtime_openai_key():
        _log_skip(
            "stt_not_configured",
            tenant_id=tenant_id, media_id=media_id, kind="audio",
        )
        # We still TRY to download + persist so the dashboard renders
        # the recording. Transcript stays empty + the merchant sees
        # "STT not configured" in the media-debug panel.
        downloaded = await _download_meta_media(
            db=db, wa_conn=wa_conn, tenant_id=tenant_id,
            media_id=media_id, mime_type=mime_type,
        )
        if downloaded is None:
            return _audio_failure(
                base_meta,
                download_status="failed",
                transcript_status="skipped",
                transcript_error="stt_not_configured",
                caption=caption,
            )
        stored = _try_persist(
            tenant_id=tenant_id, file_bytes=downloaded["bytes"],
            mime_type=downloaded["mime_type"] or mime_type,
            kind="audio", media_id=media_id,
        )
        if stored is not None:
            base_meta["storage_url"]    = stored.storage_url
            base_meta["storage_sha256"] = stored.sha256
            base_meta["byte_size"]      = stored.byte_size
            if not base_meta.get("mime_type"):
                base_meta["mime_type"] = stored.mime_type
        base_meta["audio_download_status"] = "ok"
        base_meta["transcript_status"]     = "skipped"
        base_meta["transcript_error"]      = "stt_not_configured"
        return _audio_with_fallback(base_meta, caption)

    # ── Happy-ish path: download, persist, transcribe ────────────
    downloaded = await _download_meta_media(
        db=db, wa_conn=wa_conn, tenant_id=tenant_id,
        media_id=media_id, mime_type=mime_type,
    )
    if downloaded is None:
        return _audio_failure(
            base_meta,
            download_status="failed",
            transcript_status="skipped",
            transcript_error="download_failed",
            caption=caption,
        )

    actual_mime = downloaded["mime_type"] or mime_type
    file_bytes = downloaded["bytes"]

    stored = _try_persist(
        tenant_id=tenant_id, file_bytes=file_bytes,
        mime_type=actual_mime, kind="audio", media_id=media_id,
    )
    if stored is not None:
        base_meta["storage_url"]    = stored.storage_url
        base_meta["storage_sha256"] = stored.sha256
        base_meta["byte_size"]      = stored.byte_size
        base_meta["mime_type"]      = stored.mime_type
    base_meta["audio_download_status"] = "ok"

    # Transcription. We run it AFTER storage so even a Whisper crash
    # leaves the recording playable in the drawer.
    try:
        transcript = await _transcribe_bytes_with_openai(
            file_bytes=file_bytes,
            mime_type=actual_mime,
            tenant_id=tenant_id,
            media_id=media_id,
        )
    except Exception as exc:
        logger.warning(
            "[MediaNormalizer] audio transcription failed tenant=%s "
            "media_id=%s err=%s",
            tenant_id, media_id, exc,
        )
        base_meta["transcript_status"] = "failed"
        base_meta["transcript_error"]  = (
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
        return _audio_with_fallback(base_meta, caption)

    if not transcript:
        logger.warning(
            "[MEDIA_STT_EMPTY] tenant=%s media_id=%s mime=%s bytes=%d "
            "— whisper returned no usable text",
            tenant_id, media_id, actual_mime or "—", len(file_bytes),
        )
        base_meta["transcript_status"] = "empty"
        base_meta["transcript_error"]  = "empty_transcript"
        return _audio_with_fallback(base_meta, caption)

    base_meta["transcript_status"] = "ok"
    base_meta["transcript_text"]   = transcript
    base_meta["ai_used_audio"]     = True

    # ── Combine caption + transcript ────────────────────────────
    # Meta rarely ships a caption with audio, but 360dialog and the
    # web client occasionally do. Concatenate so the brain sees both.
    combined = transcript
    if caption:
        combined = f"{caption}\n\n[تفريغ التسجيل] {transcript}"

    return MediaNormalizationResult(
        normalized_type="audio",
        text=combined,
        metadata=base_meta,
        should_process=True,
    )


def _audio_failure(
    base_meta: Dict[str, Any],
    *,
    download_status: str,
    transcript_status: str,
    transcript_error: str,
    caption: str,
) -> MediaNormalizationResult:
    base_meta["audio_download_status"] = download_status
    base_meta["transcript_status"]     = transcript_status
    base_meta["transcript_error"]      = transcript_error
    return _audio_with_fallback(base_meta, caption)


def _audio_with_fallback(
    base_meta: Dict[str, Any],
    caption: str,
) -> MediaNormalizationResult:
    """Build the result for "we have audio but no transcript".

    If the customer sent a caption alongside the voice note we
    forward the caption to the brain and skip the fallback reply —
    they DID provide text, just not via the recording. Otherwise we
    surface the Arabic fallback so the conversation doesn't die.
    """
    if caption:
        return MediaNormalizationResult(
            normalized_type="audio",
            text=caption,
            metadata=base_meta,
            should_process=True,
        )
    return MediaNormalizationResult(
        normalized_type="audio",
        text="",
        metadata=base_meta,
        should_process=False,
        fallback_reply_ar=AUDIO_FALLBACK_REPLY_AR,
    )


# ── Image ───────────────────────────────────────────────────────────


async def _process_image(
    *,
    db: Any,
    wa_conn: Any,
    tenant_id: int,
    image_payload: Dict[str, Any],
    ts_raw: Any,
    wa_msg_id: str,
    order_context: Optional[Dict[str, Any]] = None,
) -> MediaNormalizationResult:
    """Download → persist → describe an inbound image payload."""
    media_id = str(image_payload.get("id") or "").strip()
    mime_type = str(image_payload.get("mime_type") or "").strip()
    caption = str(image_payload.get("caption") or "").strip()

    base_meta: Dict[str, Any] = {
        "source_type":          "image",
        "media_id":             media_id or None,
        "mime_type":            mime_type or None,
        "caption":              caption or None,
        "wa_timestamp":         ts_raw,
        "wa_message_id":        wa_msg_id or None,
        "image_download_status": "pending",
        "vision_status":         "pending",
        "vision_text":           None,
        "vision_error":          None,
        "ai_used_image":         False,
        "storage_url":           None,
        "storage_sha256":        None,
        "byte_size":             None,
    }

    if not media_id:
        return _image_failure(
            base_meta,
            download_status="failed",
            vision_status="skipped",
            vision_error="missing_media_id",
            caption=caption,
        )

    downloaded = await _download_meta_media(
        db=db, wa_conn=wa_conn, tenant_id=tenant_id,
        media_id=media_id, mime_type=mime_type,
    )
    if downloaded is None:
        return _image_failure(
            base_meta,
            download_status="failed",
            vision_status="skipped",
            vision_error="download_failed",
            caption=caption,
        )

    actual_mime = downloaded["mime_type"] or mime_type
    file_bytes  = downloaded["bytes"]

    stored = _try_persist(
        tenant_id=tenant_id, file_bytes=file_bytes,
        mime_type=actual_mime, kind="image", media_id=media_id,
    )
    if stored is not None:
        base_meta["storage_url"]    = stored.storage_url
        base_meta["storage_sha256"] = stored.sha256
        base_meta["byte_size"]      = stored.byte_size
        base_meta["mime_type"]      = stored.mime_type
    base_meta["image_download_status"] = "ok"

    if not _runtime_openai_key():
        _log_skip(
            "vision_not_configured",
            tenant_id=tenant_id, media_id=media_id, kind="image",
        )
        base_meta["vision_status"] = "skipped"
        base_meta["vision_error"]  = "vision_not_configured"
        return _image_with_fallback(base_meta, caption)

    try:
        vision_text = await _describe_image_with_openai(
            file_bytes=file_bytes,
            mime_type=actual_mime,
            caption_hint=caption,
            tenant_id=tenant_id,
            media_id=media_id,
        )
    except Exception as exc:
        logger.warning(
            "[MediaNormalizer] image vision failed tenant=%s "
            "media_id=%s err=%s",
            tenant_id, media_id, exc,
        )
        base_meta["vision_status"] = "failed"
        base_meta["vision_error"]  = (
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
        return _image_with_fallback(base_meta, caption)

    if not vision_text:
        logger.warning(
            "[MEDIA_VISION_EMPTY] tenant=%s media_id=%s mime=%s bytes=%d "
            "caption_present=%s — openai returned no usable text",
            tenant_id, media_id, actual_mime or "—",
            len(file_bytes), "true" if caption else "false",
        )
        base_meta["vision_status"] = "empty"
        base_meta["vision_error"]  = "empty_description"
        return _image_with_fallback(base_meta, caption)

    base_meta["vision_status"] = "ok"
    base_meta["vision_text"]   = vision_text
    base_meta["ai_used_image"] = True

    # ── Receipt detection from vision text ────────────────────────
    # The vision system prompt instructs the model to label
    # "إثبات دفع / إيصال". When that phrase appears in the vision
    # text AND the conversation is in an active payment-receipt
    # waiting state, surface a structured ``image_kind`` slot so
    # the webhook can short-circuit into the deterministic
    # "receipt-received" acknowledgement instead of running the
    # generic LLM reply path (which used to lose product context).
    try:
        _vision_lc = (vision_text or "").lower()
        if any(k in _vision_lc for k in (
            "إيصال", "ايصال", "تحويل", "حواله", "حوالة",
            "إثبات دفع", "اثبات دفع", "receipt", "transfer",
            "rajhi", "stcpay", "alinma", "snb", "alahli",
        )):
            base_meta["image_kind"] = "payment_receipt"
            base_meta["image_kind_confidence"] = "high"
            base_meta["image_kind_reasons"] = ["vision_text_keyword"]
        elif order_context and bool(
            order_context.get("awaiting_payment_receipt")
        ):
            # No keywords in the vision text but the bot just asked
            # for a receipt — high-confidence boost based on flow
            # state. Same logic as the PDF classifier's context
            # boost.
            base_meta["image_kind"] = "payment_receipt"
            base_meta["image_kind_confidence"] = "high"
            base_meta["image_kind_reasons"] = ["awaiting_payment_receipt"]
    except Exception:  # noqa: BLE001
        pass

    # ── Combine caption + vision description ────────────────────
    if caption:
        combined = f"{caption}\n\n[وصف الصورة] {vision_text}"
    else:
        combined = f"[وصف الصورة المرسلة] {vision_text}"
    if base_meta.get("image_kind") == "payment_receipt":
        # Tag the brain-facing text so downstream rules can detect
        # without re-parsing the Arabic description.
        combined = "[تصنيف الصورة: إيصال تحويل بنكي]\n" + combined
    return MediaNormalizationResult(
        normalized_type="image",
        text=combined,
        metadata=base_meta,
        should_process=True,
    )


def _image_failure(
    base_meta: Dict[str, Any],
    *,
    download_status: str,
    vision_status: str,
    vision_error: str,
    caption: str,
) -> MediaNormalizationResult:
    base_meta["image_download_status"] = download_status
    base_meta["vision_status"]         = vision_status
    base_meta["vision_error"]          = vision_error
    return _image_with_fallback(base_meta, caption)


def _image_with_fallback(
    base_meta: Dict[str, Any],
    caption: str,
) -> MediaNormalizationResult:
    if caption:
        return MediaNormalizationResult(
            normalized_type="image",
            text=caption,
            metadata=base_meta,
            should_process=True,
        )
    return MediaNormalizationResult(
        normalized_type="image",
        text="",
        metadata=base_meta,
        should_process=False,
        fallback_reply_ar=IMAGE_FALLBACK_REPLY_AR,
    )


# ── Document (PDF, etc.) ────────────────────────────────────────────


async def _process_document(
    *,
    db: Any,
    wa_conn: Any,
    tenant_id: int,
    document_payload: Dict[str, Any],
    ts_raw: Any,
    wa_msg_id: str,
    order_context: Optional[Dict[str, Any]] = None,
) -> MediaNormalizationResult:
    """Handle inbound WhatsApp documents (PDFs, primarily).

    We do not OCR or text-extract the PDF — that would add a new
    dependency and Saudi bank receipts vary too much for reliable
    parsing. Instead we:

      1. Download the bytes so the merchant can re-open the file
         from the conversation drawer even after Meta's 5-minute
         CDN URL expires.
      2. Persist via :func:`save_inbound_media` for permanent
         storage.
      3. Heuristically classify via :func:`classify_inbound_document`
         using filename + caption + order context.
      4. Compose a brain-facing text marker like
         ``"[وثيقة PDF — تصنيف: إيصال تحويل بنكي] {filename}"``
         so the decision engine can route on it without needing
         to read the actual PDF.

    The result is ``normalized_type="document"``; the webhook now
    treats this as a kept type (alongside ``text``/``audio``/``image``).
    """
    media_id  = str(document_payload.get("id") or "").strip()
    mime_type = str(document_payload.get("mime_type") or "").strip()
    caption   = str(document_payload.get("caption") or "").strip()
    filename  = str(document_payload.get("filename") or "").strip()

    base_meta: Dict[str, Any] = {
        "source_type":             "document",
        "media_id":                media_id or None,
        "mime_type":               mime_type or None,
        "caption":                 caption or None,
        "filename":                filename or None,
        "wa_timestamp":            ts_raw,
        "wa_message_id":           wa_msg_id or None,
        "document_download_status": "pending",
        "storage_url":             None,
        "storage_sha256":          None,
        "byte_size":               None,
        # Filled in below by the heuristic classifier.
        "pdf_kind":                "unknown",
        "pdf_kind_confidence":     "low",
        "pdf_kind_reasons":        [],
    }

    if not media_id:
        base_meta["document_download_status"] = "failed"
        base_meta["document_error"] = "missing_media_id"
        return _document_with_fallback(base_meta, caption, filename)

    downloaded = await _download_meta_media(
        db=db, wa_conn=wa_conn, tenant_id=tenant_id,
        media_id=media_id, mime_type=mime_type,
    )
    if downloaded is None:
        base_meta["document_download_status"] = "failed"
        base_meta["document_error"] = "download_failed"
        return _document_with_fallback(base_meta, caption, filename)

    actual_mime = downloaded["mime_type"] or mime_type or "application/pdf"
    file_bytes  = downloaded["bytes"]

    stored = _try_persist(
        tenant_id=tenant_id, file_bytes=file_bytes,
        mime_type=actual_mime, kind="document", media_id=media_id,
    )
    if stored is not None:
        base_meta["storage_url"]    = stored.storage_url
        base_meta["storage_sha256"] = stored.sha256
        base_meta["byte_size"]      = stored.byte_size
        base_meta["mime_type"]      = stored.mime_type
    base_meta["document_download_status"] = "ok"

    # ── Heuristic classification ─────────────────────────────────
    verdict = classify_inbound_document(
        filename=filename,
        caption=caption,
        mime_type=actual_mime,
        order_context=order_context,
    )
    base_meta["pdf_kind"]            = verdict.get("category") or "unknown"
    base_meta["pdf_kind_confidence"] = verdict.get("confidence") or "low"
    base_meta["pdf_kind_reasons"]    = verdict.get("reasons") or []
    base_meta["pdf_kind_signals"]    = verdict.get("signals") or {}

    # ── Compose brain-facing text ────────────────────────────────
    label_ar = {
        "payment_receipt": "إيصال تحويل بنكي",
        "invoice":         "فاتورة",
        "identity":        "وثيقة هوية",
        "shipping_label":  "بوليصة شحن",
        "catalog":         "كتالوج منتجات",
        "unknown":         "مستند",
    }.get(base_meta["pdf_kind"], "مستند")

    pieces: list = []
    pieces.append(f"[وثيقة PDF — تصنيف: {label_ar}]")
    if filename:
        pieces.append(f"اسم الملف: {filename}")
    if caption:
        pieces.append(f"تعليق العميل: {caption}")
    if base_meta["pdf_kind"] == "payment_receipt":
        # Make the receipt arrival impossible to miss in the prompt
        # so the LLM (when not short-circuited deterministically)
        # cannot accidentally re-ask product discovery.
        pieces.append(
            "ملاحظة للنظام: العميل أرسل إيصال تحويل بنكي. "
            "لا تعد سؤال العميل عن المنتج، واعتمد على state الطلب الحالي."
        )
    combined = "\n".join(pieces)

    logger.info(
        "[ORDER_FLOW_STATE] inbound_document tenant=%s media_id=%s "
        "filename=%r pdf_kind=%s confidence=%s reasons=%s "
        "context_awaiting=%s context_active=%s",
        tenant_id, media_id, filename,
        base_meta["pdf_kind"], base_meta["pdf_kind_confidence"],
        base_meta["pdf_kind_reasons"],
        bool((order_context or {}).get("awaiting_payment_receipt")),
        bool((order_context or {}).get("has_active_order")),
    )

    return MediaNormalizationResult(
        normalized_type="document",
        text=combined,
        metadata=base_meta,
        should_process=True,
    )


def _document_with_fallback(
    base_meta: Dict[str, Any],
    caption: str,
    filename: str,
) -> MediaNormalizationResult:
    """Return a structured fallback when we couldn't download the
    PDF. We still try to flow through to the brain — the merchant
    needs to know the customer attempted to send a document even
    if we couldn't read it."""
    text_bits: list = ["[وثيقة PDF — لم نستطع تحميل الملف]"]
    if filename:
        text_bits.append(f"اسم الملف: {filename}")
    if caption:
        text_bits.append(f"تعليق العميل: {caption}")
    combined = "\n".join(text_bits)
    if combined and (filename or caption):
        return MediaNormalizationResult(
            normalized_type="document",
            text=combined,
            metadata=base_meta,
            should_process=True,
        )
    return MediaNormalizationResult(
        normalized_type="document",
        text="",
        metadata=base_meta,
        should_process=False,
        fallback_reply_ar=DOCUMENT_FALLBACK_REPLY_AR,
    )


# ── Download helper ─────────────────────────────────────────────────


async def _download_meta_media(
    *,
    db: Any,
    wa_conn: Any,
    tenant_id: int,
    media_id: str,
    mime_type: str,
) -> Optional[Dict[str, Any]]:
    """Resolve a Meta/360dialog ``media_id`` → temporary CDN URL →
    binary bytes. Returns ``None`` on any failure so callers can
    decide whether to fall back to the storage / caption path.
    Never raises (logs + returns None) — the whole pipeline is
    designed to keep the conversation alive even when Meta hiccups.

    Provider routing
    ────────────────
    The repo supports both Meta Cloud and 360dialog (BSP). Each
    speaks a different media-download wire format:

      * Meta Cloud:
          GET https://graph.facebook.com/{ver}/{media_id}
          Authorization: Bearer <Meta WABA access token>
        → returns ``{"url": "<https://lookaside.fbsbx.com/…>",
                    "mime_type": "<mime>"}``
        → second hop fetches the bytes from the lookaside CDN with
          the SAME Authorization header.

      * 360dialog:
          GET https://waba-v2.360dialog.io/{media_id}
          D360-API-KEY: <360dialog API key>
        → returns the same shape but the ``url`` points BACK at
          ``waba-v2.360dialog.io`` (no public CDN), so the second
          hop ALSO uses the D360-API-KEY header.

    Routing on ``wa_provider(wa_conn)`` mirrors what
    ``services.whatsapp_platform.service._provider_base_url`` and
    ``_provider_headers`` already do for the outbound send path.
    The previous implementation hard-coded the Meta path which
    caused a 401 Unauthorized on every 360dialog inbound media
    event — a 360dialog API key was being sent to Meta's Graph
    API as a Bearer token.
    """
    provider = "meta"
    try:
        try:
            provider = wa_provider(wa_conn) or "meta"
        except Exception:
            provider = "meta"

        token_ctx = await get_token_for_operation(
            db, wa_conn, tenant_id=tenant_id, operation="media_download",
        )

        # ── Branch on provider for base URL + auth header ────────
        # We deliberately compute both the resolve URL (hop 1) and
        # the auth header here, BEFORE the httpx client opens, so a
        # provider misconfiguration produces a single clean log
        # line instead of an opaque network error.
        if provider == WHATSAPP_PROVIDER_360DIALOG:
            base = D360_API_BASE_URL.rstrip("/")
            resolve_url = f"{base}/{media_id}"
            headers = {"D360-API-KEY": token_ctx.token}
        else:
            base = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
            resolve_url = f"{base}/{media_id}"
            headers = {"Authorization": f"Bearer {token_ctx.token}"}

        async with httpx.AsyncClient(timeout=25) as client:
            meta_resp = await client.get(
                resolve_url, headers=headers,
            )
            meta_resp.raise_for_status()
            meta_json = meta_resp.json() if meta_resp.content else {}
            media_url = str(meta_json.get("url") or "").strip()
            # Meta's response sometimes includes a more authoritative
            # mime_type than the inbound payload (e.g. for re-encoded
            # voice notes). Prefer it when present.
            resolved_mime = str(
                meta_json.get("mime_type") or mime_type or ""
            ).strip()
            # ── Success log for the URL-RESOLVE step ──────────────
            # Without this, when the customer's WhatsApp message
            # produces a vision-empty fallback the operator can't
            # tell whether Meta returned a 200 with a usable URL
            # or whether the second hop failed. We log the resolve
            # outcome (with URL host + tail only — never the full
            # token-carrying URL) and the resolved mime so the two
            # hops can be traced independently in Railway logs.
            try:
                _media_host = ""
                if media_url:
                    from urllib.parse import urlparse  # noqa: PLC0415
                    _media_host = urlparse(media_url).netloc or ""
            except Exception:
                _media_host = ""
            logger.info(
                "[MEDIA_DOWNLOAD_RESOLVE] tenant=%s media_id=%s "
                "provider=%s status=%d mime=%s url_host=%s "
                "url_present=%s",
                tenant_id, media_id, provider,
                int(meta_resp.status_code),
                resolved_mime or "—", _media_host or "—",
                "true" if media_url else "false",
            )
            if not media_url:
                logger.warning(
                    "[MediaNormalizer] %s returned no url tenant=%s "
                    "media_id=%s",
                    provider, tenant_id, media_id,
                )
                return None

            # ── Hop-2 auth strategy ──────────────────────────────
            #
            # Provider-by-provider behaviour, per official docs:
            #
            #   * Meta Cloud:
            #     Resolved URL → ``lookaside.fbsbx.com/whatsapp_business``
            #     Auth        → ``Authorization: Bearer <Meta token>``
            #     Fetch the resolved URL as-is.
            #
            #   * 360dialog (per
            #     https://docs.360dialog.com/docs/v3/whatsapp-api/messages/messages-media/):
            #
            #       "Replace the root hostname
            #        https://lookaside.fbsbx.com with
            #        https://waba-v2.360dialog.io"
            #
            #     360dialog mirrors Meta's response shape (so
            #     hop-1 returns a lookaside URL), but the actual
            #     bytes live on 360dialog's gateway, NOT on
            #     Meta's CDN. Hitting lookaside directly with
            #     either D360-API-KEY or Bearer returns 401 —
            #     we never had a session there in the first
            #     place. The fix is a deterministic host swap
            #     to ``waba-v2.360dialog.io``, preserving path +
            #     query, then a single GET with
            #     ``D360-API-KEY: <D360 key>``.
            #
            # F9 hardcoded Meta. F10 added a Bearer fallback for
            # 360dialog that ALSO went to lookaside — which 360dialog
            # explicitly documents will not work. F11 implements the
            # actual documented contract.
            try:
                _media_host_for_hop2 = (_media_host or "").lower()
            except Exception:
                _media_host_for_hop2 = ""

            _is_meta_host = (
                _media_host_for_hop2.endswith("facebook.com")
                or _media_host_for_hop2.endswith("fbcdn.net")
                or _media_host_for_hop2.endswith("fbsbx.com")
            )
            _is_d360_host = _media_host_for_hop2.endswith("360dialog.io")

            # Decide the EXACT hop-2 URL + headers based on the
            # provider × host matrix. Single attempt — no
            # fallback loop. If this fails, the failure is real
            # (wrong key, expired media_id, gateway outage) and
            # the right action is to log + return None.
            fetch_url = media_url
            fetch_headers: Dict[str, str] = {}
            host_rewrite_label = "asis"

            if provider == WHATSAPP_PROVIDER_360DIALOG:
                if _is_d360_host:
                    # 360dialog returned a native waba-v2 URL
                    # (some accounts / endpoints do). Use as-is.
                    fetch_headers = {"D360-API-KEY": token_ctx.token}
                    host_rewrite_label = "asis"
                elif _is_meta_host:
                    # Documented path: swap lookaside → waba-v2.
                    # Preserve path + query EXACTLY (the ``mid``
                    # query param is the actual content
                    # selector — losing it = wrong file).
                    try:
                        from urllib.parse import urlparse, urlunparse  # noqa: PLC0415
                        _parsed = urlparse(media_url)
                        # waba-v2.360dialog.io is the documented
                        # canonical host; use it regardless of
                        # the configured D360_API_BASE_URL value
                        # so we never accidentally point at the
                        # partner-hub host (which lacks the
                        # media-attachment route).
                        fetch_url = urlunparse(_parsed._replace(
                            scheme="https",
                            netloc="waba-v2.360dialog.io",
                        ))
                    except Exception as rewrite_exc:  # noqa: BLE001
                        logger.warning(
                            "[MEDIA_DOWNLOAD_HOST_REWRITE_FAILED] "
                            "tenant=%s media_id=%s err=%s — falling "
                            "back to original URL",
                            tenant_id, media_id, rewrite_exc,
                        )
                        fetch_url = media_url
                    fetch_headers = {"D360-API-KEY": token_ctx.token}
                    host_rewrite_label = "lookaside_to_waba_v2"
                else:
                    # Unknown third-party host. Don't leak the
                    # API key — bare GET, signed-URL semantics.
                    fetch_headers = {}
                    host_rewrite_label = "bare_unknown_host"
            else:
                # Meta: standard contract — fetch lookaside with
                # Bearer.
                if _is_meta_host:
                    fetch_headers = {"Authorization": f"Bearer {token_ctx.token}"}
                else:
                    fetch_headers = {}
                host_rewrite_label = "asis"

            # We log the rewrite outcome BEFORE the network call
            # so a failure mode that crashes the request is
            # still attributable. The token value is never
            # logged; only the symbolic ``host_rewrite_label``
            # and the destination host (NOT the full URL — the
            # path includes a session-binding ``mid`` parameter
            # that we treat as sensitive).
            try:
                from urllib.parse import urlparse as _urlparse  # noqa: PLC0415
                _fetch_host = _urlparse(fetch_url).netloc or "—"
            except Exception:
                _fetch_host = "—"
            logger.info(
                "[MEDIA_DOWNLOAD_FETCH_HOST] tenant=%s media_id=%s "
                "provider=%s rewrite=%s fetch_host=%s auth=%s",
                tenant_id, media_id, provider, host_rewrite_label,
                _fetch_host,
                "d360_key" if "D360-API-KEY" in fetch_headers
                else "bearer" if "Authorization" in fetch_headers
                else "bare",
            )

            media_resp = None
            file_bytes = b""
            try:
                media_resp = await client.get(fetch_url, headers=fetch_headers)
            except Exception as fetch_exc:  # noqa: BLE001
                logger.warning(
                    "[MEDIA_DOWNLOAD_FETCH_EXC] tenant=%s media_id=%s "
                    "provider=%s rewrite=%s exc=%s",
                    tenant_id, media_id, provider, host_rewrite_label,
                    type(fetch_exc).__name__,
                )
                raise

            if media_resp.status_code == 401:
                # Single-attempt design: a 401 here is a real
                # auth/config issue, not a wire-format guess.
                # Log explicitly so the support team can see
                # whether the failing host is lookaside (host
                # rewrite didn't take effect for some reason) or
                # waba-v2 (genuine API-key problem).
                logger.warning(
                    "[MEDIA_DOWNLOAD_FETCH_401] tenant=%s media_id=%s "
                    "provider=%s rewrite=%s fetch_host=%s — verify "
                    "tenant API key and provider routing",
                    tenant_id, media_id, provider, host_rewrite_label,
                    _fetch_host,
                )
                return None
            media_resp.raise_for_status()
            file_bytes = media_resp.content
            # ── Success log for the CDN-FETCH step ────────────────
            # This is THE crucial line that proves bytes arrived
            # (and how many). When vision later returns empty
            # text, we can grep the same media_id and see whether
            # we actually downloaded a real image or a 200-OK
            # error page. Content-Type from the CDN is also key —
            # WhatsApp sometimes returns text/html if the URL
            # expired between resolve and fetch.
            _cdn_ct = media_resp.headers.get("content-type") or ""
            logger.info(
                "[MEDIA_DOWNLOAD_FETCH] tenant=%s media_id=%s "
                "provider=%s status=%d bytes=%d content_type=%s",
                tenant_id, media_id, provider,
                int(media_resp.status_code),
                len(file_bytes), _cdn_ct or "—",
            )

        if not file_bytes:
            logger.warning(
                "[MediaNormalizer] %s returned empty body tenant=%s "
                "media_id=%s",
                provider, tenant_id, media_id,
            )
            return None
        if len(file_bytes) > INBOUND_MEDIA_MAX_BYTES:
            logger.warning(
                "[MediaNormalizer] media too large tenant=%s media_id=%s "
                "provider=%s bytes=%d cap=%d",
                tenant_id, media_id, provider, len(file_bytes),
                INBOUND_MEDIA_MAX_BYTES,
            )
            return None
        # ── Sanity check: the CDN sometimes serves an HTML error
        # page with 200 OK when the temporary URL expired between
        # the resolve and fetch hops. Detect by content-type +
        # magic-byte sniff so the caller can short-circuit before
        # spending an OpenAI Vision call on garbage.
        looks_like_html = False
        try:
            head = bytes(file_bytes[:8]).lstrip().lower()
            if head.startswith(b"<!doctype") or head.startswith(b"<html") or head.startswith(b"<?xml"):
                looks_like_html = True
        except Exception:
            looks_like_html = False
        if looks_like_html or (_cdn_ct.lower().startswith("text/html") if _cdn_ct else False):
            logger.warning(
                "[MEDIA_DOWNLOAD_NON_BINARY] tenant=%s media_id=%s "
                "provider=%s content_type=%s bytes=%d head=%s — "
                "likely expired CDN URL serving HTML error page",
                tenant_id, media_id, provider, _cdn_ct or "—",
                len(file_bytes), bytes(file_bytes[:24]),
            )
            return None
        return {"bytes": file_bytes, "mime_type": resolved_mime}
    except httpx.HTTPStatusError as exc:
        # Distinguish HTTP errors (with status codes) from connection
        # errors. A 401 with provider=meta + 360dialog token in
        # ctx.source is a strong "wrong wire format" signal — the
        # exact diagnostic we wanted to make trivial.
        _status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning(
            "[MediaNormalizer] media download HTTP error tenant=%s "
            "provider=%s media_id=%s status=%s err=%s",
            tenant_id, provider, media_id, _status, exc,
        )
        return None
    except httpx.HTTPError as exc:
        logger.warning(
            "[MediaNormalizer] media download failed tenant=%s "
            "provider=%s media_id=%s err=%s",
            tenant_id, provider, media_id, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[MediaNormalizer] unexpected download error tenant=%s "
            "provider=%s media_id=%s err=%s",
            tenant_id, provider, media_id, exc,
        )
        return None


def _try_persist(
    *,
    tenant_id: int,
    file_bytes: bytes,
    mime_type: str,
    kind: str,
    media_id: str,
):
    """Best-effort wrapper around ``save_inbound_media`` so a disk
    failure in production never breaks the conversation. Returns the
    storage handle on success, ``None`` on failure (caller still gets
    a usable transcript / vision result, just without playback)."""
    try:
        return save_inbound_media(
            tenant_id=tenant_id,
            file_bytes=file_bytes,
            mime_type=mime_type,
            kind=kind,
            media_id=media_id or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[MediaNormalizer] persistent storage failed tenant=%s "
            "kind=%s media_id=%s err=%s",
            tenant_id, kind, media_id, exc,
        )
        return None


# ── OpenAI wrappers ─────────────────────────────────────────────────


async def _transcribe_bytes_with_openai(
    *, file_bytes: bytes, mime_type: str,
    tenant_id: Optional[int] = None, media_id: Optional[str] = None,
) -> str:
    """Transcribe an in-memory audio blob via the OpenAI Whisper /
    ``audio/transcriptions`` endpoint with an Arabic language hint
    so Saudi-dialect voice notes are handled gracefully.

    We use a NamedTemporaryFile because httpx multipart wants a file
    handle. The temp file is cleaned up after the call regardless of
    success/failure.
    """
    headers = {"Authorization": f"Bearer {_runtime_openai_key()}"}
    suffix = _guess_suffix(mime_type)
    tmp_path: Optional[Path] = None
    # ── Pre-request log ───────────────────────────────────────
    # Same rationale as the vision pre-request log: when whisper
    # returns empty, we need to know whether we sent it a real
    # audio file or 0 bytes / a 200-OK HTML page that slipped
    # past the download non-binary check.
    logger.info(
        "[MEDIA_STT_REQ] tenant=%s media_id=%s model=%s mime=%s "
        "ext_guess=%s bytes_in=%d language=%s",
        tenant_id, media_id, OPENAI_AUDIO_MODEL, mime_type or "—",
        suffix, len(file_bytes), NAHLA_STT_LANGUAGE,
    )
    try:
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file_bytes)
            tmp_path = Path(tmp.name)

        async with httpx.AsyncClient(timeout=45) as client:
            with tmp_path.open("rb") as f:
                files = {
                    "file": (tmp_path.name, f, "application/octet-stream"),
                    "model": (None, OPENAI_AUDIO_MODEL),
                    "response_format": (None, "json"),
                    # Whisper supports a per-request language hint.
                    # For Arabic / Saudi-dialect voice notes this
                    # measurably reduces hallucinations into
                    # phonetically-similar Persian/Urdu tokens.
                    "language": (None, NAHLA_STT_LANGUAGE),
                }
                resp = await client.post(
                    f"{OPENAI_API_BASE.rstrip('/')}/audio/transcriptions",
                    headers=headers,
                    files=files,
                )
                resp.raise_for_status()
                data = resp.json()
        text = str(data.get("text") or "").strip()
        # ── Post-response log ─────────────────────────────────
        # Whisper sometimes returns valid JSON with text="" for
        # very short / silent / encrypted-codec recordings. We
        # log preview (max 120 chars) so audit can see what the
        # customer was trying to say, but never the full
        # transcription (PII).
        logger.info(
            "[MEDIA_STT_RESP] tenant=%s media_id=%s status=%d "
            "text_len=%d preview=%r",
            tenant_id, media_id, int(resp.status_code),
            len(text), text[:120],
        )
        return text
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


async def _describe_image_with_openai(
    *,
    file_bytes: bytes,
    mime_type: str,
    caption_hint: str,
    tenant_id: Optional[int] = None,
    media_id: Optional[str] = None,
) -> str:
    """Run an OpenAI Vision describe over the inbound image.

    We send the image inline as a data URL so we don't need to expose
    our own storage URL externally (Meta's URL is gone the moment we
    download). The system prompt asks for a *concise Arabic*
    description suitable for being concatenated into the brain's
    conversation context — no flowery English captions.
    """
    import base64

    headers = {
        "Authorization": f"Bearer {_runtime_openai_key()}",
        "Content-Type":  "application/json",
    }
    b64 = base64.b64encode(file_bytes).decode("ascii")
    safe_mime = (mime_type or "image/jpeg").split(";", 1)[0].strip() or "image/jpeg"
    data_url = f"data:{safe_mime};base64,{b64}"

    system_prompt = (
        "أنت مساعد بصري في متجر إلكتروني عربي. مهمتك وصف الصورة "
        "المرسلة من العميل بشكل موجز وعملي، بحيث يستفيد منها مساعد "
        "خدمة العملاء لاحقاً. اذكر:\n"
        "1) نوع المحتوى (منتج / فاتورة / لقطة شاشة / إيصال / إثبات دفع "
        "/ شخصية / مستند / صورة عامة).\n"
        "2) النص المرئي إن وُجد (اقرأه كما هو دون ترجمة).\n"
        "3) أي تفاصيل مهمة قد يحتاجها فريق الدعم (رقم طلب، اسم منتج، "
        "مبلغ، تاريخ، علامة تجارية).\n"
        "اكتب الوصف بالعربية الفصحى، في حدود 3–5 أسطر، دون مقدمات."
    )
    user_text = (
        f"وصف الصورة المرفقة. تعليق العميل المرفق (قد يكون فارغاً): "
        f"{caption_hint or '—'}"
    )

    body = {
        "model": OPENAI_VISION_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            },
        ],
        "max_tokens": 400,
        "temperature": 0.2,
    }

    # ── Pre-request log ────────────────────────────────────────
    # Without this, when vision returns empty text we cannot
    # tell whether the payload was reasonable or malformed.
    # We log byte counts, not bytes themselves, to keep
    # customer images out of log storage.
    logger.info(
        "[MEDIA_VISION_REQ] tenant=%s media_id=%s model=%s "
        "mime=%s bytes_in=%d b64_len=%d caption_present=%s",
        tenant_id, media_id, OPENAI_VISION_MODEL,
        safe_mime, len(file_bytes), len(b64),
        "true" if caption_hint else "false",
    )

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(
            f"{OPENAI_API_BASE.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices") or []
    # ── Post-response shape log ────────────────────────────────
    # Disambiguates the THREE empty paths below: no choices,
    # empty content list, content-as-None. Each has a distinct
    # remediation (model misconfig vs. safety filter vs. plain
    # API error).
    _finish_reason = ""
    try:
        if choices:
            _finish_reason = str(choices[0].get("finish_reason") or "")
    except Exception:
        _finish_reason = ""
    _usage = data.get("usage") or {}
    logger.info(
        "[MEDIA_VISION_RESP] tenant=%s media_id=%s status=%d "
        "choices=%d finish_reason=%s prompt_tokens=%s "
        "completion_tokens=%s",
        tenant_id, media_id, int(resp.status_code),
        len(choices), _finish_reason or "—",
        _usage.get("prompt_tokens"), _usage.get("completion_tokens"),
    )
    if not choices:
        logger.warning(
            "[MEDIA_VISION_EMPTY_CAUSE] tenant=%s media_id=%s "
            "cause=no_choices",
            tenant_id, media_id,
        )
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        # The new content-parts shape — concatenate text parts.
        parts = [
            str(p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        result = "".join(parts).strip()
        if not result:
            logger.warning(
                "[MEDIA_VISION_EMPTY_CAUSE] tenant=%s media_id=%s "
                "cause=no_text_parts content_parts=%d",
                tenant_id, media_id, len(content),
            )
        else:
            logger.info(
                "[MEDIA_VISION_OK] tenant=%s media_id=%s "
                "text_len=%d preview=%r",
                tenant_id, media_id, len(result), result[:120],
            )
        return result
    result = str(content or "").strip()
    if not result:
        logger.warning(
            "[MEDIA_VISION_EMPTY_CAUSE] tenant=%s media_id=%s "
            "cause=%s",
            tenant_id, media_id,
            "content_none" if content is None else "content_empty_string",
        )
    else:
        logger.info(
            "[MEDIA_VISION_OK] tenant=%s media_id=%s "
            "text_len=%d preview=%r",
            tenant_id, media_id, len(result), result[:120],
        )
    return result


def _guess_suffix(mime_type: str) -> str:
    mime = (mime_type or "").lower()
    if "ogg" in mime or "opus" in mime:
        return ".ogg"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    if "wav" in mime:
        return ".wav"
    if "m4a" in mime or "mp4a" in mime:
        return ".m4a"
    if "mp4" in mime:
        return ".mp4"
    if "webm" in mime:
        return ".webm"
    return ".bin"
