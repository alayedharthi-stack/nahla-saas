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
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Optional

import httpx

from core.config import (
    INBOUND_MEDIA_MAX_BYTES,
    META_GRAPH_API_VERSION,
    NAHLA_STT_LANGUAGE,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    OPENAI_AUDIO_MODEL,
    OPENAI_VISION_MODEL,
)
from services.inbound_media_storage import save_inbound_media
from services.whatsapp_platform.token_manager import get_token_for_operation

logger = logging.getLogger("nahla.ai.media")

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

    if not OPENAI_API_KEY:
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

    if not OPENAI_API_KEY:
        base_meta["vision_status"] = "skipped"
        base_meta["vision_error"]  = "vision_not_configured"
        return _image_with_fallback(base_meta, caption)

    try:
        vision_text = await _describe_image_with_openai(
            file_bytes=file_bytes,
            mime_type=actual_mime,
            caption_hint=caption,
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
        base_meta["vision_status"] = "empty"
        base_meta["vision_error"]  = "empty_description"
        return _image_with_fallback(base_meta, caption)

    base_meta["vision_status"] = "ok"
    base_meta["vision_text"]   = vision_text
    base_meta["ai_used_image"] = True

    # ── Combine caption + vision description ────────────────────
    if caption:
        combined = f"{caption}\n\n[وصف الصورة] {vision_text}"
    else:
        combined = f"[وصف الصورة المرسلة] {vision_text}"
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
    """
    try:
        token_ctx = await get_token_for_operation(
            db, wa_conn, tenant_id=tenant_id, operation="media_download",
        )
        graph = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
        headers = {"Authorization": f"Bearer {token_ctx.token}"}

        async with httpx.AsyncClient(timeout=25) as client:
            meta_resp = await client.get(
                f"{graph}/{media_id}", headers=headers,
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
            if not media_url:
                logger.warning(
                    "[MediaNormalizer] Meta returned no url tenant=%s "
                    "media_id=%s",
                    tenant_id, media_id,
                )
                return None

            media_resp = await client.get(media_url, headers=headers)
            media_resp.raise_for_status()
            file_bytes = media_resp.content

        if not file_bytes:
            logger.warning(
                "[MediaNormalizer] Meta returned empty body tenant=%s "
                "media_id=%s",
                tenant_id, media_id,
            )
            return None
        if len(file_bytes) > INBOUND_MEDIA_MAX_BYTES:
            logger.warning(
                "[MediaNormalizer] media too large tenant=%s media_id=%s "
                "bytes=%d cap=%d",
                tenant_id, media_id, len(file_bytes),
                INBOUND_MEDIA_MAX_BYTES,
            )
            return None
        return {"bytes": file_bytes, "mime_type": resolved_mime}
    except httpx.HTTPError as exc:
        logger.warning(
            "[MediaNormalizer] Meta media download failed tenant=%s "
            "media_id=%s err=%s",
            tenant_id, media_id, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[MediaNormalizer] unexpected download error tenant=%s "
            "media_id=%s err=%s",
            tenant_id, media_id, exc,
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
) -> str:
    """Transcribe an in-memory audio blob via the OpenAI Whisper /
    ``audio/transcriptions`` endpoint with an Arabic language hint
    so Saudi-dialect voice notes are handled gracefully.

    We use a NamedTemporaryFile because httpx multipart wants a file
    handle. The temp file is cleaned up after the call regardless of
    success/failure.
    """
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    suffix = _guess_suffix(mime_type)
    tmp_path: Optional[Path] = None
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
        return str(data.get("text") or "").strip()
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
        "Authorization": f"Bearer {OPENAI_API_KEY}",
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

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(
            f"{OPENAI_API_BASE.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices") or []
    if not choices:
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
        return "".join(parts).strip()
    return str(content or "").strip()


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
