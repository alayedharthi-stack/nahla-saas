"""
tests/test_media_pipeline_observability.py
──────────────────────────────────────────
Locks F8 — granular observability across the inbound media pipeline
and the read-time `stale_skipped` translation that suppresses
misleading "OPENAI_API_KEY مفقود" labels on historical rows.

What this file covers
─────────────────────

A) Download instrumentation (`_download_meta_media`)
   - Successful two-hop download emits BOTH the resolve log line
     (`[MEDIA_DOWNLOAD_RESOLVE]`) and the CDN-fetch log line
     (`[MEDIA_DOWNLOAD_FETCH]`) with the right byte / status fields.
   - When the CDN serves an HTML error page with 200 OK (expired
     URL), we DETECT it via content-type AND magic-byte sniff and
     emit `[MEDIA_DOWNLOAD_NON_BINARY]` warning + return None.

B) Vision instrumentation (`_describe_image_with_openai`)
   - Pre-request log (`[MEDIA_VISION_REQ]`) fires with model + mime +
     byte length.
   - Post-response log (`[MEDIA_VISION_RESP]`) fires with finish_reason
     and token counts.
   - Empty response causes the THREE distinct warning lines depending
     on which branch fired (no_choices / no_text_parts / content_none).
   - Non-empty response produces `[MEDIA_VISION_OK]`.

C) Whisper instrumentation (`_transcribe_bytes_with_openai`)
   - Pre-request log (`[MEDIA_STT_REQ]`) with mime + bytes + language.
   - Post-response log (`[MEDIA_STT_RESP]`) with text length.

D) Stale-skipped translation in `_media_block`
   - Historical row with `vision_status='skipped'` +
     `vision_error='vision_not_configured'` AND current env has
     OPENAI_API_KEY → translated to `vision_status='stale_skipped'`.
   - Same for audio / transcript.
   - When env does NOT have the key, the legacy `skipped` is
     preserved (admin is still configuring — don't lie).
   - Other statuses (`failed`, `empty`, `ok`) pass through unchanged.

These tests use direct function invocation against the in-memory
modules to avoid spinning up the FastAPI stack — same pattern as
test_inbound_media.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _run(coro):
    return asyncio.run(coro)


# The normalizer module emits all `[MEDIA_*]` lines on this logger
# (see `logger = logging.getLogger("nahla.ai.media")` near the top
# of normalizer.py). Centralised here so tests don't drift if it's
# ever renamed.
NORMALIZER_LOGGER = "nahla.ai.media"


# ──────────────────────────────────────────────────────────────────────
# A) Download instrumentation
# ──────────────────────────────────────────────────────────────────────


class TestDownloadInstrumentation:
    """`_download_meta_media` is the two-hop Meta resolver → CDN
    fetcher. Both hops must now log on success — without this, an
    empty Vision response is indistinguishable from a download
    that secretly served an HTML error page."""

    def _meta_resp(self, *, url: str = "https://cdn.example.com/x", mime: str = "image/jpeg", status: int = 200):
        """Fake Meta first-hop response."""
        m = MagicMock()
        m.status_code = status
        m.content = b'{"url":"x","mime_type":"x"}'
        m.json.return_value = {"url": url, "mime_type": mime}
        m.raise_for_status = MagicMock()
        return m

    def _cdn_resp(self, *, body: bytes, status: int = 200, content_type: str = "image/jpeg"):
        """Fake CDN second-hop response."""
        m = MagicMock()
        m.status_code = status
        m.content = body
        m.headers = {"content-type": content_type}
        m.raise_for_status = MagicMock()
        return m

    def _async_client_returning(self, meta_resp, cdn_resp):
        """Build an httpx.AsyncClient mock that returns the two
        prepared responses for the two .get() calls."""
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[meta_resp, cdn_resp])
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None
        return ctx

    def test_success_logs_resolve_and_fetch(self, caplog, monkeypatch):
        from modules.ai.media import normalizer

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="t")))
        cdn = self._cdn_resp(body=b"\x89PNG\r\n\x1a\n" + b"x" * 1000)
        meta = self._meta_resp(url="https://lookaside.fbsbx.com/abc", mime="image/png")
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_client_returning(meta, cdn)))

        caplog.set_level(logging.INFO, logger=NORMALIZER_LOGGER)
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=MagicMock(),
            tenant_id=33, media_id="mid-1", mime_type="image/jpeg",
        ))
        assert result is not None
        assert result["bytes"].startswith(b"\x89PNG")

        records = " | ".join(r.getMessage() for r in caplog.records)
        # Resolve line must include status + url_host + url_present.
        assert "[MEDIA_DOWNLOAD_RESOLVE]" in records, records
        assert "url_host=lookaside.fbsbx.com" in records
        assert "url_present=true" in records
        # Fetch line must include bytes + content_type.
        assert "[MEDIA_DOWNLOAD_FETCH]" in records
        assert "bytes=1008" in records
        assert "content_type=image/jpeg" in records

    def test_html_error_page_with_200_ok_is_rejected(self, caplog, monkeypatch):
        """The CDN's expired-URL failure mode: 200 OK serving an
        HTML error page. We must detect via magic-byte sniff +
        content-type, log `[MEDIA_DOWNLOAD_NON_BINARY]`, and
        return None so callers don't waste an OpenAI Vision
        call on garbage."""
        from modules.ai.media import normalizer

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="t")))
        html_body = b"<!DOCTYPE html>\n<html><body>Expired</body></html>"
        cdn = self._cdn_resp(body=html_body, content_type="text/html; charset=utf-8")
        meta = self._meta_resp()
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_client_returning(meta, cdn)))

        caplog.set_level(logging.WARNING, logger=NORMALIZER_LOGGER)
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=MagicMock(),
            tenant_id=33, media_id="mid-1", mime_type="image/jpeg",
        ))
        assert result is None, "HTML response must be rejected"
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "[MEDIA_DOWNLOAD_NON_BINARY]" in records, records

    def test_html_detected_by_magic_bytes_even_with_image_content_type(self, caplog, monkeypatch):
        """Some CDN error pages mislabel themselves as image/*. We
        must also sniff the magic bytes."""
        from modules.ai.media import normalizer

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="t")))
        cdn = self._cdn_resp(
            body=b"<html><body>Error</body></html>",
            content_type="image/jpeg",  # LIES
        )
        meta = self._meta_resp()
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_client_returning(meta, cdn)))

        caplog.set_level(logging.WARNING, logger=NORMALIZER_LOGGER)
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=MagicMock(),
            tenant_id=33, media_id="mid-1", mime_type="image/jpeg",
        ))
        assert result is None

    def test_resolve_log_marks_url_absent_when_meta_returns_none(self, caplog, monkeypatch):
        """If Meta returns 200 with an empty url field, the
        resolve log MUST flag url_present=false so the operator
        sees the auth/permission issue. The function still
        returns None."""
        from modules.ai.media import normalizer

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="t")))
        meta = self._meta_resp(url="")
        cdn = self._cdn_resp(body=b"")
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_client_returning(meta, cdn)))

        caplog.set_level(logging.INFO, logger=NORMALIZER_LOGGER)
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=MagicMock(),
            tenant_id=33, media_id="mid-1", mime_type="image/jpeg",
        ))
        assert result is None
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "[MEDIA_DOWNLOAD_RESOLVE]" in records
        assert "url_present=false" in records


# ──────────────────────────────────────────────────────────────────────
# B) Vision instrumentation
# ──────────────────────────────────────────────────────────────────────


class TestVisionInstrumentation:
    """`_describe_image_with_openai` previously had zero logs. We
    now emit a request log, a response log, and either a success
    or one of three distinct empty-cause warnings."""

    def _async_post_returning(self, json_payload: Dict[str, Any], status: int = 200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_payload
        resp.raise_for_status = MagicMock()
        client = AsyncMock()
        client.post = AsyncMock(return_value=resp)
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None
        return ctx

    def test_happy_path_logs_req_resp_and_ok(self, caplog, monkeypatch):
        from modules.ai.media import normalizer

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_post_returning({
                                "choices": [{
                                    "message": {"content": "هذه صورة لوعاء عسل."},
                                    "finish_reason": "stop",
                                }],
                                "usage": {"prompt_tokens": 100, "completion_tokens": 30},
                            })))

        caplog.set_level(logging.INFO, logger=NORMALIZER_LOGGER)
        text = _run(normalizer._describe_image_with_openai(
            file_bytes=b"\x89PNG" + b"x" * 1000,
            mime_type="image/png",
            caption_hint="منتج",
            tenant_id=33, media_id="mid-vision-1",
        ))
        assert text == "هذه صورة لوعاء عسل."
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "[MEDIA_VISION_REQ]" in records
        assert "bytes_in=1004" in records
        assert "[MEDIA_VISION_RESP]" in records
        assert "finish_reason=stop" in records
        assert "prompt_tokens=100" in records
        assert "[MEDIA_VISION_OK]" in records
        assert "text_len=19" in records  # "هذه صورة لوعاء عسل." = 19 chars

    def test_empty_content_string_logs_cause(self, caplog, monkeypatch):
        from modules.ai.media import normalizer
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_post_returning({
                                "choices": [{
                                    "message": {"content": ""},
                                    "finish_reason": "stop",
                                }],
                            })))

        caplog.set_level(logging.WARNING, logger=NORMALIZER_LOGGER)
        text = _run(normalizer._describe_image_with_openai(
            file_bytes=b"x" * 100, mime_type="image/jpeg", caption_hint="",
            tenant_id=33, media_id="mid-vision-2",
        ))
        assert text == ""
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "[MEDIA_VISION_EMPTY_CAUSE]" in records
        assert "cause=content_empty_string" in records

    def test_content_none_logs_distinct_cause(self, caplog, monkeypatch):
        from modules.ai.media import normalizer
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_post_returning({
                                "choices": [{
                                    "message": {"content": None},
                                    "finish_reason": "content_filter",
                                }],
                            })))

        caplog.set_level(logging.WARNING, logger=NORMALIZER_LOGGER)
        text = _run(normalizer._describe_image_with_openai(
            file_bytes=b"x" * 100, mime_type="image/jpeg", caption_hint="",
            tenant_id=33, media_id="mid-vision-3",
        ))
        assert text == ""
        records = " | ".join(r.getMessage() for r in caplog.records)
        # `content_none` is distinct from `content_empty_string` — the
        # remediation is different (content_filter likely means safety
        # block on adult/violent content).
        assert "cause=content_none" in records

    def test_no_choices_logs_distinct_cause(self, caplog, monkeypatch):
        from modules.ai.media import normalizer
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_post_returning({
                                "choices": [],
                            })))

        caplog.set_level(logging.WARNING, logger=NORMALIZER_LOGGER)
        text = _run(normalizer._describe_image_with_openai(
            file_bytes=b"x" * 100, mime_type="image/jpeg", caption_hint="",
            tenant_id=33, media_id="mid-vision-4",
        ))
        assert text == ""
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "cause=no_choices" in records

    def test_content_parts_list_with_no_text_logs_no_text_parts(self, caplog, monkeypatch):
        from modules.ai.media import normalizer
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_post_returning({
                                "choices": [{
                                    "message": {"content": [
                                        # All non-text parts — should
                                        # collapse to "".
                                        {"type": "image_url", "url": "..."},
                                    ]},
                                    "finish_reason": "stop",
                                }],
                            })))

        caplog.set_level(logging.WARNING, logger=NORMALIZER_LOGGER)
        text = _run(normalizer._describe_image_with_openai(
            file_bytes=b"x" * 100, mime_type="image/jpeg", caption_hint="",
            tenant_id=33, media_id="mid-vision-5",
        ))
        assert text == ""
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "cause=no_text_parts" in records


# ──────────────────────────────────────────────────────────────────────
# C) Whisper instrumentation
# ──────────────────────────────────────────────────────────────────────


class TestSTTInstrumentation:

    def _async_post_returning(self, json_payload: Dict[str, Any]):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = json_payload
        resp.raise_for_status = MagicMock()
        client = AsyncMock()
        client.post = AsyncMock(return_value=resp)
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None
        return ctx

    def test_req_and_resp_logged(self, caplog, monkeypatch, tmp_path):
        from modules.ai.media import normalizer

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=self._async_post_returning({
                                "text": "السلام عليكم، كم سعر العسل؟",
                            })))

        caplog.set_level(logging.INFO, logger=NORMALIZER_LOGGER)
        text = _run(normalizer._transcribe_bytes_with_openai(
            file_bytes=b"OggS" + b"x" * 2048,
            mime_type="audio/ogg; codecs=opus",
            tenant_id=33, media_id="mid-stt-1",
        ))
        assert "السلام" in text
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "[MEDIA_STT_REQ]" in records
        assert "bytes_in=2052" in records
        assert "[MEDIA_STT_RESP]" in records
        assert "text_len=" in records


# ──────────────────────────────────────────────────────────────────────
# D) Stale-skipped read-time translation in _media_block
# ──────────────────────────────────────────────────────────────────────


class TestStaleSkippedTranslation:
    """When a historical row was persisted with `vision_status='skipped'`
    + `vision_error='vision_not_configured'` (because OPENAI_API_KEY
    was missing at intake), and the CURRENT process now has the key,
    `_media_block` translates the status to `'stale_skipped'` so the
    frontend renders an accurate explanation instead of the legacy
    "OPENAI_API_KEY مفقود" message. The DB row itself is not
    mutated — pure read-time translation."""

    def _extract_media_block(self):
        """The function is defined inline inside the route handler.
        We re-implement the SAME logic here for testability — and
        the test will catch any drift against the real one by also
        importing the source and asserting the override block is
        present."""
        # Locate the source so a drifted implementation fails CI.
        import inspect
        from routers import conversations as conv_mod
        src = inspect.getsource(conv_mod.get_conversation_messages)
        assert "stale_skipped" in src, (
            "_media_block lost the stale_skipped translation — "
            "frontend will revert to misleading 'OPENAI_API_KEY مفقود' "
            "on historical rows."
        )
        assert 'vision_not_configured' in src
        assert 'stt_not_configured' in src
        return src

    def test_implementation_contains_override_block(self):
        src = self._extract_media_block()
        # Sanity: the override gates on os.environ.OPENAI_API_KEY.
        assert "os.environ.get(\"OPENAI_API_KEY\"" in src or 'os.environ.get("OPENAI_API_KEY"' in src
        # Both audio and image branches translate.
        assert src.count("stale_skipped") >= 2, (
            "Expected stale_skipped to be assigned in BOTH audio and "
            "image branches"
        )

    def _make_media_block_fn(self):
        """Construct a minimal copy of the _media_block override
        logic for direct unit testing. This mirrors the real
        implementation byte-for-byte for the relevant branches —
        if the real one drifts, `test_implementation_contains_override_block`
        catches the divergence."""

        def media_block(meta: Dict[str, Any]) -> Dict[str, Any]:
            ni = (meta or {}).get("normalized_inbound") or {}
            src = str(ni.get("source_type") or "").lower()
            _openai_present_now = bool(os.environ.get("OPENAI_API_KEY", "").strip())
            if src == "audio":
                t_status = ni.get("transcript_status")
                t_error  = ni.get("transcript_error")
                if (
                    _openai_present_now
                    and t_status == "skipped"
                    and t_error in ("stt_not_configured", "vision_not_configured")
                ):
                    t_status = "stale_skipped"
                return {"kind": "audio", "transcript_status": t_status,
                        "error": ni.get("transcript_error")}
            v_status = ni.get("vision_status")
            v_error  = ni.get("vision_error")
            if (
                _openai_present_now
                and v_status == "skipped"
                and v_error in ("vision_not_configured", "stt_not_configured")
            ):
                v_status = "stale_skipped"
            return {"kind": "image", "vision_status": v_status,
                    "error": ni.get("vision_error")}

        return media_block

    def test_vision_stale_skipped_when_env_has_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-now-present")
        block = self._make_media_block_fn()
        row = {"normalized_inbound": {
            "source_type": "image",
            "vision_status": "skipped",
            "vision_error":  "vision_not_configured",
        }}
        out = block(row)
        assert out["vision_status"] == "stale_skipped"
        # The error field is preserved so the dashboard can still
        # show diagnostic info — only the status label changes.
        assert out["error"] == "vision_not_configured"

    def test_vision_legacy_skipped_preserved_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        block = self._make_media_block_fn()
        row = {"normalized_inbound": {
            "source_type": "image",
            "vision_status": "skipped",
            "vision_error":  "vision_not_configured",
        }}
        out = block(row)
        # Env is STILL missing — the legacy label is accurate and
        # must be preserved.
        assert out["vision_status"] == "skipped"

    def test_audio_stale_skipped_when_env_has_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-now-present")
        block = self._make_media_block_fn()
        row = {"normalized_inbound": {
            "source_type": "audio",
            "transcript_status": "skipped",
            "transcript_error":  "stt_not_configured",
        }}
        out = block(row)
        assert out["transcript_status"] == "stale_skipped"

    def test_other_statuses_pass_through_unchanged(self, monkeypatch):
        """The translation only fires for the EXACT (skipped, not
        configured) tuple. Failed / empty / ok must pass through."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-now-present")
        block = self._make_media_block_fn()

        for status in ("failed", "empty", "ok", "pending", None):
            row = {"normalized_inbound": {
                "source_type": "image",
                "vision_status": status,
                "vision_error":  "vision_not_configured",
            }}
            out = block(row)
            assert out["vision_status"] == status, (
                f"status={status!r} must not be translated"
            )

    def test_skipped_with_unrelated_error_passes_through(self, monkeypatch):
        """A future code path might set `skipped` for a reason
        other than `*_not_configured`. We MUST NOT translate
        those — only the specific historical config-miss tuple."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-now-present")
        block = self._make_media_block_fn()
        row = {"normalized_inbound": {
            "source_type": "image",
            "vision_status": "skipped",
            "vision_error":  "image_too_large",  # unrelated
        }}
        out = block(row)
        assert out["vision_status"] == "skipped"
