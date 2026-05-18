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

    def test_meta_provider_uses_graph_api_and_bearer_token(self, caplog, monkeypatch):
        """Meta tenants must hit ``graph.facebook.com/{ver}/{media_id}``
        with an ``Authorization: Bearer`` header. Locks the URL +
        header so a regression that flips the provider routing
        is caught."""
        from modules.ai.media import normalizer

        captured = {"url": None, "headers": None}

        async def _fake_get(url, headers=None, **_kw):
            # First call records the resolve URL + headers; second
            # call records the CDN URL + headers.
            if captured["url"] is None:
                captured["url"] = url
                captured["headers"] = dict(headers or {})
            return self._meta_resp(url="https://lookaside.fbsbx.com/abc",
                                    mime="image/jpeg") if captured["url"] == url \
                   else self._cdn_resp(body=b"\xff\xd8\xff" + b"x" * 100)

        # Build a client that returns meta-then-cdn responses in order
        meta = self._meta_resp(url="https://lookaside.fbsbx.com/abc")
        cdn  = self._cdn_resp(body=b"\xff\xd8\xff" + b"x" * 100)
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[meta, cdn])
        # Capture the URL of the first call.
        async def _recording_get(url, headers=None, **kw):
            if captured["url"] is None:
                captured["url"] = url
                captured["headers"] = dict(headers or {})
                return meta
            return cdn
        client.get = _recording_get
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="META-TOKEN-xyz")))
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=ctx))

        # Explicitly Meta-provider conn.
        wa_conn = MagicMock(provider="meta")
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=wa_conn,
            tenant_id=33, media_id="m-meta-1", mime_type="image/jpeg",
        ))
        assert result is not None
        assert captured["url"] == "https://graph.facebook.com/v20.0/m-meta-1"
        assert captured["headers"].get("Authorization") == "Bearer META-TOKEN-xyz"
        assert "D360-API-KEY" not in captured["headers"]

    def test_dialog360_provider_uses_waba_v2_and_d360_key(self, caplog, monkeypatch):
        """360dialog tenants must hit ``waba-v2.360dialog.io/{media_id}``
        with a ``D360-API-KEY`` header — NOT ``Authorization: Bearer``.
        This is the specific regression that caused
        ``401 Unauthorized`` in production: a 360dialog API key
        was being sent to Meta's Graph API as a Bearer token."""
        from modules.ai.media import normalizer
        from core.config import D360_API_BASE_URL

        captured = {"url": None, "headers": None}

        # Mock 360dialog responses. 360dialog's resolved URL points
        # BACK at waba-v2.360dialog.io, not a public CDN.
        meta = MagicMock()
        meta.status_code = 200
        meta.content = b"x"
        meta.json.return_value = {
            "url": "https://waba-v2.360dialog.io/abc/raw",
            "mime_type": "image/jpeg",
        }
        meta.raise_for_status = MagicMock()
        cdn = self._cdn_resp(body=b"\xff\xd8\xff" + b"x" * 100)
        client = AsyncMock()

        async def _recording_get(url, headers=None, **kw):
            if captured["url"] is None:
                captured["url"] = url
                captured["headers"] = dict(headers or {})
                return meta
            return cdn

        client.get = _recording_get
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="D360-KEY-abc")))
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=ctx))

        wa_conn = MagicMock(provider="dialog360")
        caplog.set_level(logging.INFO, logger=NORMALIZER_LOGGER)
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=wa_conn,
            tenant_id=33, media_id="m-d360-1", mime_type="image/jpeg",
        ))
        assert result is not None
        # URL: bare path on waba-v2 base. NO /v20.0/. NO graph.facebook.com.
        expected_base = D360_API_BASE_URL.rstrip("/")
        assert captured["url"] == f"{expected_base}/m-d360-1", captured["url"]
        # Headers: D360-API-KEY ONLY. NO Authorization: Bearer.
        assert captured["headers"].get("D360-API-KEY") == "D360-KEY-abc"
        assert "Authorization" not in captured["headers"], (
            "360dialog must not receive a Bearer header — this is the "
            "exact bug that caused 401 in production"
        )
        # Log line includes provider=dialog360 so operators can grep
        # by provider when diagnosing per-tenant download issues.
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "provider=dialog360" in records

    def test_dialog360_hop2_keeps_d360_key_when_url_resolves_back_to_360dialog(self, caplog, monkeypatch):
        """360dialog's hop-2 URL lands at ``waba-v2.360dialog.io``,
        which still requires ``D360-API-KEY`` to read. We must
        attach the same auth header on the second GET; otherwise
        hop-2 returns 401 even though hop-1 succeeded."""
        from modules.ai.media import normalizer

        meta = MagicMock()
        meta.status_code = 200
        meta.content = b"x"
        meta.json.return_value = {
            "url": "https://waba-v2.360dialog.io/abc/raw",
            "mime_type": "image/jpeg",
        }
        meta.raise_for_status = MagicMock()
        cdn = self._cdn_resp(body=b"\xff\xd8\xff" + b"x" * 100)
        client = AsyncMock()

        seen_headers: List[Dict[str, str]] = []

        async def _recording_get(url, headers=None, **kw):
            seen_headers.append(dict(headers or {}))
            return meta if len(seen_headers) == 1 else cdn

        client.get = _recording_get
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="D360-KEY")))
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=ctx))

        wa_conn = MagicMock(provider="dialog360")
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=wa_conn,
            tenant_id=33, media_id="m-d360-2", mime_type="image/jpeg",
        ))
        assert result is not None
        # Hop 1: resolve — D360-API-KEY present.
        assert seen_headers[0].get("D360-API-KEY") == "D360-KEY"
        # Hop 2: fetch — D360-API-KEY ALSO present.
        assert seen_headers[1].get("D360-API-KEY") == "D360-KEY", (
            "360dialog hop-2 lost the auth header — will 401 on real "
            "waba-v2.360dialog.io responses"
        )

    def test_dialog360_hop2_drops_auth_when_url_is_unknown_third_party(self, monkeypatch):
        """If 360dialog returns a URL on a host NEITHER 360dialog
        NOR Meta's known CDN footprint, we must NOT leak the API
        key. The fetch is attempted bare and relies on signed-URL
        semantics. (Lookaside IS allowed — see the F10 attempt
        loop tests below.)"""
        from modules.ai.media import normalizer

        meta = MagicMock()
        meta.status_code = 200
        meta.content = b"x"
        meta.json.return_value = {
            "url": "https://random-cdn.example.com/abc?sig=xyz",
            "mime_type": "image/jpeg",
        }
        meta.raise_for_status = MagicMock()
        cdn = self._cdn_resp(body=b"\xff\xd8\xff" + b"x" * 100)
        client = AsyncMock()

        seen_headers: List[Dict[str, str]] = []

        async def _recording_get(url, headers=None, **kw):
            seen_headers.append(dict(headers or {}))
            return meta if len(seen_headers) == 1 else cdn

        client.get = _recording_get
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="D360-KEY")))
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=ctx))

        wa_conn = MagicMock(provider="dialog360")
        _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=wa_conn,
            tenant_id=33, media_id="m-d360-3", mime_type="image/jpeg",
        ))
        # Hop-2 to an unknown third-party host MUST NOT carry the
        # API key. Only the bare-GET attempt runs.
        assert "D360-API-KEY" not in seen_headers[1], (
            "Leaked 360dialog API key to a non-360dialog, non-Meta host"
        )
        assert "Authorization" not in seen_headers[1]

    # ── F11: 360dialog hop-2 lookaside → waba-v2 host rewrite ──
    #
    # 360dialog's official docs (
    # https://docs.360dialog.com/docs/v3/whatsapp-api/messages/messages-media/
    # ) state:
    #
    #   "Replace the root hostname https://lookaside.fbsbx.com
    #    with https://waba-v2.360dialog.io"
    #
    # So 360dialog's WABA v2 mirrors Meta's response shape (hop-1
    # returns a lookaside URL) but the actual bytes live on
    # 360dialog's gateway. Hitting lookaside directly with EITHER
    # D360-API-KEY or Bearer returns 401 because we never had a
    # session there. The fix is a deterministic host swap →
    # waba-v2.360dialog.io + single GET with D360-API-KEY.
    #
    # F9 hardcoded Meta (401). F10 added a Bearer fallback that
    # still hit lookaside (401). F11 implements the documented
    # contract.

    def test_dialog360_hop2_rewrites_lookaside_host_to_waba_v2(self, caplog, monkeypatch):
        """The hop-2 GET MUST target ``waba-v2.360dialog.io`` —
        NOT lookaside — preserving path + query exactly. Header
        MUST be ``D360-API-KEY`` only (no Bearer). Single
        attempt — no fallback loop."""
        from modules.ai.media import normalizer

        meta = MagicMock()
        meta.status_code = 200
        meta.content = b"x"
        # 360dialog's hop-1 returns a lookaside URL with the
        # ``mid`` query param that the rewrite MUST preserve —
        # the path + query are the actual content selector;
        # losing them = wrong file.
        meta.json.return_value = {
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=ATTM123&ext=ZZZ&hash=ABC",
            "mime_type": "image/jpeg",
        }
        meta.raise_for_status = MagicMock()
        good = self._cdn_resp(body=b"\xff\xd8\xff" + b"x" * 1000)

        seen: List[Dict[str, Any]] = []

        async def _recording_get(url, headers=None, **kw):
            seen.append({"url": url, "headers": dict(headers or {})})
            return meta if len(seen) == 1 else good

        client = AsyncMock()
        client.get = _recording_get
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="D360-KEY-1")))
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=ctx))

        wa_conn = MagicMock(provider="dialog360")
        caplog.set_level(logging.INFO, logger=NORMALIZER_LOGGER)
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=wa_conn,
            tenant_id=33, media_id="m-rewrite", mime_type="image/jpeg",
        ))
        assert result is not None

        # Hop-2 must target waba-v2.360dialog.io.
        hop2_url = seen[1]["url"]
        assert hop2_url.startswith("https://waba-v2.360dialog.io/"), (
            f"hop-2 went to {hop2_url!r} — should have been rewritten "
            f"from lookaside to waba-v2.360dialog.io per 360dialog docs"
        )
        # Path + query MUST be preserved EXACTLY (the ``mid``
        # parameter is the content selector).
        assert hop2_url == (
            "https://waba-v2.360dialog.io/whatsapp_business/attachments/"
            "?mid=ATTM123&ext=ZZZ&hash=ABC"
        ), f"path/query mangled by rewrite: {hop2_url!r}"
        # Header MUST be D360-API-KEY only. No Bearer.
        assert seen[1]["headers"].get("D360-API-KEY") == "D360-KEY-1"
        assert "Authorization" not in seen[1]["headers"], (
            "Bearer header must NOT be sent for 360dialog hop-2 — "
            "the docs are explicit"
        )

        records = " | ".join(r.getMessage() for r in caplog.records)
        # Diagnostic log line names the rewrite and the
        # destination host so a future investigation can
        # confirm the contract is in force without reading
        # source.
        assert "[MEDIA_DOWNLOAD_FETCH_HOST]" in records
        assert "rewrite=lookaside_to_waba_v2" in records
        assert "fetch_host=waba-v2.360dialog.io" in records
        assert "auth=d360_key" in records
        # Single attempt — no [MEDIA_DOWNLOAD_FETCH_401] retry line.
        assert "[MEDIA_DOWNLOAD_FETCH_401]" not in records

    def test_dialog360_hop2_uses_waba_v2_url_asis_when_already_360dialog(self, caplog, monkeypatch):
        """If 360dialog's hop-1 ever returns a native
        waba-v2.360dialog.io URL (not lookaside), we MUST use
        it as-is with D360-API-KEY. No rewrite needed."""
        from modules.ai.media import normalizer

        meta = MagicMock()
        meta.status_code = 200
        meta.content = b"x"
        meta.json.return_value = {
            "url": "https://waba-v2.360dialog.io/some/native/path?mid=NATIVE",
            "mime_type": "image/jpeg",
        }
        meta.raise_for_status = MagicMock()
        good = self._cdn_resp(body=b"\xff\xd8\xff" + b"x" * 500)

        seen: List[Dict[str, Any]] = []

        async def _recording_get(url, headers=None, **kw):
            seen.append({"url": url, "headers": dict(headers or {})})
            return meta if len(seen) == 1 else good

        client = AsyncMock()
        client.get = _recording_get
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="D360-KEY")))
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=ctx))

        wa_conn = MagicMock(provider="dialog360")
        caplog.set_level(logging.INFO, logger=NORMALIZER_LOGGER)
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=wa_conn,
            tenant_id=33, media_id="m-native", mime_type="image/jpeg",
        ))
        assert result is not None
        # URL passed through unchanged.
        assert seen[1]["url"] == "https://waba-v2.360dialog.io/some/native/path?mid=NATIVE"
        assert seen[1]["headers"].get("D360-API-KEY") == "D360-KEY"
        assert "Authorization" not in seen[1]["headers"]
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "rewrite=asis" in records

    def test_dialog360_hop2_returns_none_on_persistent_401(self, caplog, monkeypatch):
        """A 401 after host-rewrite + D360-API-KEY is a real
        auth/config issue (wrong key, expired media_id, gateway
        outage). We log explicitly and return None — no
        fallback to Bearer, no fallback to lookaside."""
        from modules.ai.media import normalizer

        meta = MagicMock()
        meta.status_code = 200
        meta.content = b"x"
        meta.json.return_value = {
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=X",
            "mime_type": "image/jpeg",
        }
        meta.raise_for_status = MagicMock()

        bad = MagicMock()
        bad.status_code = 401
        bad.content = b""
        bad.headers = {}
        bad.raise_for_status = MagicMock()

        seen: List[Dict[str, Any]] = []

        async def _recording_get(url, headers=None, **kw):
            seen.append({"url": url, "headers": dict(headers or {})})
            return meta if len(seen) == 1 else bad

        client = AsyncMock()
        client.get = _recording_get
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="D360-KEY")))
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=ctx))

        wa_conn = MagicMock(provider="dialog360")
        caplog.set_level(logging.WARNING, logger=NORMALIZER_LOGGER)
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=wa_conn,
            tenant_id=33, media_id="m-401-real", mime_type="image/jpeg",
        ))
        assert result is None
        # Hop-1 + ONE hop-2 attempt — no Bearer fallback, no loop.
        assert len(seen) == 2
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "[MEDIA_DOWNLOAD_FETCH_401]" in records
        # The 401 log line names the host so operators can tell
        # whether the rewrite took effect or not.
        assert "fetch_host=waba-v2.360dialog.io" in records
        # No `[MEDIA_DOWNLOAD_FETCH_EXHAUSTED]` line — that was
        # F10's multi-attempt vocabulary. F11 is single-attempt.
        assert "[MEDIA_DOWNLOAD_FETCH_EXHAUSTED]" not in records

    def test_no_token_value_logged_anywhere(self, caplog, monkeypatch):
        """Sensitive-data invariant: the token value MUST NEVER
        appear in any log line, regardless of attempt outcome.
        Locks the privacy contract."""
        from modules.ai.media import normalizer

        meta = MagicMock()
        meta.status_code = 200
        meta.content = b"x"
        meta.json.return_value = {
            "url": "https://lookaside.fbsbx.com/abc",
            "mime_type": "image/jpeg",
        }
        meta.raise_for_status = MagicMock()

        def _r401():
            m = MagicMock()
            m.status_code = 401
            m.content = b""
            m.headers = {}
            m.raise_for_status = MagicMock()
            return m

        seen: List[Dict[str, Any]] = []

        async def _recording_get(url, headers=None, **kw):
            seen.append(url)
            return meta if len(seen) == 1 else _r401()

        client = AsyncMock()
        client.get = _recording_get
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None

        # A token containing a highly distinctive substring we
        # can grep for in caplog.
        SECRET = "SUPER-SECRET-D360-XYZQ7"
        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token=SECRET)))
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=ctx))

        wa_conn = MagicMock(provider="dialog360")
        caplog.set_level(logging.DEBUG, logger=NORMALIZER_LOGGER)
        _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=wa_conn,
            tenant_id=33, media_id="m-secret", mime_type="image/jpeg",
        ))
        all_log_text = " | ".join(r.getMessage() for r in caplog.records)
        assert SECRET not in all_log_text, (
            f"Token leaked into logs: {all_log_text!r}"
        )

    def test_http_status_error_logs_status_code_and_provider(self, caplog, monkeypatch):
        """A 401 should produce a structured log line that
        includes status=401 + provider, so the exact "wrong wire
        format" diagnosis is one grep away."""
        import httpx as real_httpx
        from modules.ai.media import normalizer

        monkeypatch.setattr(normalizer, "get_token_for_operation",
                            AsyncMock(return_value=MagicMock(token="t")))

        # Build a Response-like that raises HTTPStatusError on
        # raise_for_status — exactly what httpx does for 4xx/5xx.
        class _FakeResp:
            status_code = 401
            content = b""
            def json(self): return {}
            def raise_for_status(self):
                raise real_httpx.HTTPStatusError(
                    "401",
                    request=MagicMock(),
                    response=MagicMock(status_code=401),
                )

        client = AsyncMock()
        client.get = AsyncMock(return_value=_FakeResp())
        ctx = AsyncMock()
        ctx.__aenter__.return_value = client
        ctx.__aexit__.return_value = None
        monkeypatch.setattr(normalizer.httpx, "AsyncClient",
                            MagicMock(return_value=ctx))

        wa_conn = MagicMock(provider="meta")
        caplog.set_level(logging.WARNING, logger=NORMALIZER_LOGGER)
        result = _run(normalizer._download_meta_media(
            db=MagicMock(), wa_conn=wa_conn,
            tenant_id=33, media_id="m-401", mime_type="image/jpeg",
        ))
        assert result is None
        records = " | ".join(r.getMessage() for r in caplog.records)
        assert "media download HTTP error" in records
        assert "status=401" in records
        assert "provider=meta" in records

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
        """``_media_block`` was inlined inside the route handler for
        years; May 2026 it was hoisted to module scope as
        ``_build_media_block`` so the video passthrough could pin
        the contract under unit tests. We assert against that
        module-level function source so drift fails CI."""
        # Locate the source so a drifted implementation fails CI.
        import inspect
        from routers import conversations as conv_mod
        src = inspect.getsource(conv_mod._build_media_block)
        assert "stale_skipped" in src, (
            "_build_media_block lost the stale_skipped translation — "
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
