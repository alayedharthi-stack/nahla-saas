"""
modules/ai/tools/web_search.py
──────────────────────────────
Minimal web search helper used by the unified CommerceToolRuntime.

The policy here is intentionally conservative:
  - only fetch top public web snippets
  - always return source URLs
  - keep the output summarised and short
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any, Dict, List
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger("nahla.ai.web_search")

_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
    r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


async def search_web(query: str, *, tenant_id: int | None = None, max_results: int = 5) -> Dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        return {"query": "", "summary": "", "results": [], "citations": []}

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(_SEARCH_URL.format(query=quote_plus(query)))
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.warning("[WebSearch] tenant=%s query=%r failed: %s", tenant_id, query, exc)
        return {"query": query, "summary": "", "results": [], "citations": []}

    parsed: List[Dict[str, str]] = []
    for match in _RESULT_RE.finditer(html):
        title = _clean_html(match.group("title"))
        snippet = _clean_html(match.group("snippet"))
        url = unescape(match.group("url")).strip()
        if not title or not url:
            continue
        parsed.append({"title": title, "snippet": snippet, "url": url})
        if len(parsed) >= max_results:
            break

    summary = " ".join(item["snippet"] for item in parsed[:3] if item.get("snippet")).strip()
    return {
        "query": query,
        "summary": summary,
        "results": parsed,
        "citations": [item["url"] for item in parsed],
    }


def _clean_html(value: str) -> str:
    text = unescape(_TAG_RE.sub(" ", value or ""))
    return " ".join(text.split())
