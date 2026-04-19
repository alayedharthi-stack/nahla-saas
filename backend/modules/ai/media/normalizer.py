"""
modules/ai/media/normalizer.py
──────────────────────────────
Normalize WhatsApp inbound payloads into one internal shape.

Phase 1 scope:
  - text
  - interactive button/list replies
  - audio/voice notes → speech-to-text when credentials are available
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Optional

import httpx

from core.config import META_GRAPH_API_VERSION, OPENAI_API_BASE, OPENAI_API_KEY, OPENAI_AUDIO_MODEL
from services.whatsapp_platform.token_manager import get_token_for_operation

logger = logging.getLogger("nahla.ai.media")


@dataclass
class MediaNormalizationResult:
    normalized_type: str = "unsupported"
    text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    should_process: bool = False


async def normalize_whatsapp_inbound(
    *,
    db: Any,
    wa_conn: Any,
    tenant_id: int,
    message: Dict[str, Any],
) -> MediaNormalizationResult:
    msg_type = str(message.get("type") or "").strip()
    if msg_type == "text":
        text = str((message.get("text") or {}).get("body") or "").strip()
        return MediaNormalizationResult(
            normalized_type="text",
            text=text,
            metadata={"source_type": "text"},
            should_process=bool(text),
        )

    if msg_type == "interactive":
        return MediaNormalizationResult(
            normalized_type="interactive",
            metadata={"source_type": "interactive", "interactive": message.get("interactive", {})},
            should_process=True,
        )

    if msg_type in {"audio", "voice"}:
        transcription = await _transcribe_audio(
            db=db,
            wa_conn=wa_conn,
            tenant_id=tenant_id,
            audio_payload=message.get("audio") or {},
        )
        return MediaNormalizationResult(
            normalized_type="audio",
            text=transcription.get("text", ""),
            metadata=transcription,
            should_process=bool(transcription.get("text")),
        )

    return MediaNormalizationResult(
        normalized_type=msg_type or "unsupported",
        metadata={"source_type": msg_type or "unsupported"},
        should_process=False,
    )


async def _transcribe_audio(
    *,
    db: Any,
    wa_conn: Any,
    tenant_id: int,
    audio_payload: Dict[str, Any],
) -> Dict[str, Any]:
    media_id = str(audio_payload.get("id") or "").strip()
    mime_type = str(audio_payload.get("mime_type") or "").strip()
    if not media_id:
        return {"text": "", "reason": "missing_media_id", "mime_type": mime_type}

    if not OPENAI_API_KEY:
        return {"text": "", "reason": "stt_not_configured", "mime_type": mime_type}

    try:
        token_ctx = await get_token_for_operation(
            db,
            wa_conn,
            tenant_id=tenant_id,
            operation="media_download",
        )
        graph = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
        headers = {"Authorization": f"Bearer {token_ctx.token}"}

        async with httpx.AsyncClient(timeout=25) as client:
            meta_resp = await client.get(f"{graph}/{media_id}", headers=headers)
            meta_resp.raise_for_status()
            meta = meta_resp.json()
            media_url = str(meta.get("url") or "").strip()
            if not media_url:
                return {"text": "", "reason": "missing_media_url", "mime_type": mime_type}

            media_resp = await client.get(media_url, headers=headers)
            media_resp.raise_for_status()
            file_bytes = media_resp.content

        suffix = _guess_suffix(mime_type)
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file_bytes)
            temp_path = Path(tmp.name)

        try:
            text = await _transcribe_with_openai(temp_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

        return {
            "text": text.strip(),
            "reason": "ok" if text.strip() else "empty_transcript",
            "mime_type": mime_type,
            "media_id": media_id,
        }
    except Exception as exc:
        logger.warning("[MediaNormalizer] audio transcription failed tenant=%s: %s", tenant_id, exc)
        return {"text": "", "reason": "transcription_failed", "mime_type": mime_type, "media_id": media_id}


async def _transcribe_with_openai(path: Path) -> str:
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    async with httpx.AsyncClient(timeout=45) as client:
        with path.open("rb") as f:
            files = {
                "file": (path.name, f, "application/octet-stream"),
                "model": (None, OPENAI_AUDIO_MODEL),
                "response_format": (None, "json"),
            }
            resp = await client.post(
                f"{OPENAI_API_BASE.rstrip('/')}/audio/transcriptions",
                headers=headers,
                files=files,
            )
            resp.raise_for_status()
            data = resp.json()
    return str(data.get("text") or "").strip()


def _guess_suffix(mime_type: str) -> str:
    mime = mime_type.lower()
    if "ogg" in mime:
        return ".ogg"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    if "wav" in mime:
        return ".wav"
    if "mp4" in mime:
        return ".mp4"
    return ".bin"
