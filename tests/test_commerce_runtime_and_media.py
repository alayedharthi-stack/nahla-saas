from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _run(coro):
    return asyncio.run(coro)


def test_web_search_tool_returns_summary():
    from modules.ai.commerce.runtime import CommerceToolRuntime

    runtime = CommerceToolRuntime(MagicMock(), tenant_id=1)
    with patch(
        "modules.ai.tools.web_search.search_web",
        new=AsyncMock(return_value={
            "query": "فوائد العسل",
            "summary": "العسل قد يستخدم غذائياً وله استخدامات عامة.",
            "results": [{"title": "Result", "url": "https://example.com", "snippet": "Snippet"}],
            "citations": ["https://example.com"],
        }),
    ):
        result = _run(runtime.execute("web_search", {"query": "فوائد العسل"}))

    assert result.ok is True
    assert "العسل" in result.payload["summary"]
    assert result.payload["citations"] == ["https://example.com"]


def test_audio_normalizer_returns_transcribed_text():
    from modules.ai.media.normalizer import normalize_whatsapp_inbound

    with patch(
        "modules.ai.media.normalizer._transcribe_audio",
        new=AsyncMock(return_value={"text": "أبغى فستان بسعر 150", "reason": "ok", "mime_type": "audio/ogg"}),
    ):
        result = _run(
            normalize_whatsapp_inbound(
                db=MagicMock(),
                wa_conn=MagicMock(),
                tenant_id=1,
                message={"type": "audio", "audio": {"id": "123", "mime_type": "audio/ogg"}},
            )
        )

    assert result.should_process is True
    assert result.normalized_type == "audio"
    assert "فستان" in result.text
