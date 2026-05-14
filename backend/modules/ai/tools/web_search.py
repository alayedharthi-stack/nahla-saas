"""
modules/ai/tools/web_search.py
──────────────────────────────
Web-search tool (DISABLED BY DEFAULT — May 2026 hard gate).

Why this is gated
─────────────────
Nahla is a merchant-scoped sales assistant, not a general-purpose
search engine. A May 2026 incident report showed AI replies leaking
raw DuckDuckGo search dumps (encoded URLs, ``uddg=…&rut=…``
fragments, Wikipedia citations) into customer WhatsApp threads after
an out-of-scope question ("ايهما حساب كهرباء الشقة"). That broke
merchant trust instantly.

We now treat external retrieval as an **explicitly opt-in** feature:

  * ``MERCHANT_EXTERNAL_RESEARCH_ENABLED=true`` in the host env → the
    tool actually performs a fetch. Default: OFF.
  * If the env is not on, the tool returns an empty result immediately
    AND logs ``[EXTERNAL_RESEARCH_BLOCKED]`` so ops can see how often
    the AI is reaching for it.

The decision engine has a parallel kill switch — when external
research is disabled it never proposes the ``web_search`` action in
the first place. This module is the second line of defence: even if
something somewhere does call ``search_web()`` directly, we don't go
to the network and we don't return any citations that could leak.

A separate outbound sanitiser (``core/outbound_sanitizer.py``) is the
final line of defence at the WhatsApp send path — it scrubs any
reply that contains DuckDuckGo / encoded-URL markers regardless of
which subsystem produced them.
"""
from __future__ import annotations

import logging
import os
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

# Empty-result skeleton — returned when the kill switch is off. Keep
# the shape identical to a real response so downstream callers don't
# have to special-case "disabled" vs "no hits".
_EMPTY_RESULT: Dict[str, Any] = {
    "query": "",
    "summary": "",
    "results": [],
    "citations": [],
    "disabled": True,
}


def external_research_enabled() -> bool:
    """Return True iff the host explicitly opted into external web
    retrieval via the env. Defaults to ``False``.

    Accepts the usual truthy variants (``true``/``1``/``yes``/``on``);
    everything else (including unset) is treated as off. The env is
    re-read on every call so ops can flip the switch without a
    restart — important during incident response.
    """
    raw = os.environ.get("MERCHANT_EXTERNAL_RESEARCH_ENABLED", "")
    return raw.strip().lower() in {"true", "1", "yes", "on", "enabled"}


async def search_web(
    query: str,
    *,
    tenant_id: int | None = None,
    max_results: int = 5,
) -> Dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        return {**_EMPTY_RESULT}

    # ── HARD KILL SWITCH (May 2026) ───────────────────────────────
    # Default-off. The decision engine also short-circuits, but we
    # gate here too so a stray direct call from any subsystem can't
    # exfiltrate a search dump into a customer thread.
    if not external_research_enabled():
        logger.info(
            "[EXTERNAL_RESEARCH_BLOCKED] tenant=%s reason=disabled_by_env "
            "query=%r",
            tenant_id, query[:80],
        )
        return {**_EMPTY_RESULT, "query": query}

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(_SEARCH_URL.format(query=quote_plus(query)))
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.warning("[WebSearch] tenant=%s query=%r failed: %s", tenant_id, query, exc)
        return {**_EMPTY_RESULT, "query": query}

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
        "disabled": False,
    }


def _clean_html(value: str) -> str:
    text = unescape(_TAG_RE.sub(" ", value or ""))
    return " ".join(text.split())
