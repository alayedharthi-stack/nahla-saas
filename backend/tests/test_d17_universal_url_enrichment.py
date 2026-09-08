"""D17 universal URL enrichment pipeline regression tests.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Optional
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.facts.url_context_facts import (  # noqa: E402
    URL_CONTEXT_USER_TURN_BEGIN,
    bind_url_context_to_user_turn,
    project_url_context_facts,
)
from modules.ai.brain.observability.url_context_trace import (  # noqa: E402
    UrlContextTraceRecorder,
    sanitize_url_context_trace,
)
from modules.ai.brain.types import BrainContext, CommerceFacts, MerchantConversationState  # noqa: E402
from services.safe_http_fetch import SafeHttpResult  # noqa: E402
from services.url_context import enrich_current_turn_urls, reset_url_context_cache  # noqa: E402
from services.url_enrichment.oembed import parse_oembed_payload  # noqa: E402
from services.url_enrichment.pipeline import MAX_EXTERNAL_FETCHES  # noqa: E402

from test_d17_url_context_enrichment import (  # noqa: E402
    HTML_FIXTURE,
    PUBLIC_PAGE_URL,
    _provider_payloads,
    _run_path,
    _user_turn_json,
)

ARTICLE_URL = "https://news.example.test/article/cotton-shirt"
JSONLD_URL = "https://blog.example.test/post/jsonld-only"
OEMBED_PAGE_URL = "https://video.example.test/watch/clip"
TIKTOK_URL = "https://www.tiktok.com/@creator/video/9001"
PRODUCT_URL = "https://shop.example.test/p/sneaker"
PLAIN_HTML_URL = "https://plain.example.test/about"
EMPTY_JS_URL = "https://spa.example.test/empty"
INTERNAL_OEMBED_URL = "https://media.example.test/embed-me"
BLOCKED_REDIRECT_URL = "https://blocked.example.test/redirect"
TIMEOUT_URL = "https://slow.example.test/page"

ARTICLE_HTML = HTML_FIXTURE
JSONLD_ONLY_HTML = """<!doctype html><html><head>
<script type="application/ld+json">{
  "@context": "https://schema.org",
  "@type": "NewsArticle",
  "headline": "عطر ورد 100ml — مراجعة عامة",
  "description": "مراجعة منتج عطرية لمتجر تجريبي عام",
  "author": {"@type": "Person", "name": "نورة عبدالله"},
  "datePublished": "2026-01-02",
  "image": "https://cdn.example.test/perfume.jpg"
}</script>
</head><body></body></html>"""
OEMBED_DECLARE_HTML = """<!doctype html><html><head>
<link rel="alternate" type="application/json+oembed"
 href="https://oembed.example.test/embed?format=json" />
</head><body><p>ignored</p></body></html>"""
TIKTOK_THIN_HTML = """<!doctype html><html><head>
<title>TikTok - Make Your Day</title>
</head><body><div id="app"></div></body></html>"""
PRODUCT_HTML = """<!doctype html><html><head>
<meta property="og:type" content="product">
<meta property="og:title" content="حذاء رياضي أبيض">
<meta property="og:description" content="حذاء رياضي أبيض من متجر تجريبي عام">
<meta property="og:site_name" content="متجر تجريبي عام">
</head><body></body></html>"""
PLAIN_HTML = """<!doctype html><html><head><title>Home</title></head><body>
<nav>Home About Contact</nav>
<p>متجر تجريبي عام يقدم منتجات متنوعة للعملاء في مختلف المدن.</p>
<script>window.__STATE__={}</script>
</body></html>"""
EMPTY_JS_HTML = """<!doctype html><html><head></head>
<body><div id="root"></div><script>window.boot()</script></body></html>"""


@pytest.fixture(autouse=True)
def _stub_safe_oembed_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.parse import urlparse

    from backend.services.safe_http_fetch import SafeFetchRequest, validate_destination as real_validate

    def _validate(url: str, **kwargs: Any) -> tuple[Any, str]:
        if "127.0.0.1" in url or "localhost" in url:
            return real_validate(url, **kwargs)
        parsed = urlparse(url)
        host = parsed.hostname or "example.test"
        port = parsed.port or (443 if (parsed.scheme or "https") == "https" else 80)
        req = SafeFetchRequest(
            url=url,
            hostname=host,
            ip="93.184.216.34",
            port=port,
            scheme=parsed.scheme or "https",
            path=parsed.path or "/",
            timeout_connect=1.0,
            timeout_read=1.0,
        )
        return req, ""

    monkeypatch.setattr("services.url_enrichment.oembed.validate_destination", _validate)


class RoutingFetch:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str) -> SafeHttpResult:
        self.calls.append(url)
        if url in self.routes:
            handler = self.routes[url]
        else:
            handler = None
            for key, candidate in self.routes.items():
                if key.endswith("*") and url.startswith(key[:-1]):
                    handler = candidate
                    break
        if handler is None:
            return SafeHttpResult(ok=False, url=url, final_url=url, error_class="unavailable")
        if callable(handler):
            return handler(url)
        if isinstance(handler, SafeHttpResult):
            return handler
        if isinstance(handler, str):
            return SafeHttpResult(
                ok=True,
                url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                body=handler.encode("utf-8"),
            )
        return SafeHttpResult(ok=False, url=url, final_url=url, error_class="unavailable")


def _oembed_json(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def _enrich(message: str, fetch: Any, *, trace: Optional[UrlContextTraceRecorder] = None) -> tuple[Any, UrlContextTraceRecorder]:
    reset_url_context_cache()
    trace = trace or UrlContextTraceRecorder()
    rows = asyncio.run(
        enrich_current_turn_urls(
            message=message,
            tenant_id=9001,
            fetch=fetch,
            trace=trace,
        )
    )
    return rows[0], trace


def test_01_article_open_graph_metadata() -> None:
    fetch = RoutingFetch({ARTICLE_URL: ARTICLE_HTML})
    result, trace = _enrich(ARTICLE_URL, fetch)
    assert result.extraction_status == "ok"
    assert result.metadata_quality == "useful"
    assert result.useful_metadata_present is True
    assert "قميص قطني" in result.page_title
    assert result.safe_description
    assert result.author_or_channel
    assert trace._fields.get("standard_metadata_status") == "ok"


def test_02_json_ld_only_page() -> None:
    fetch = RoutingFetch({JSONLD_URL: JSONLD_ONLY_HTML})
    result, trace = _enrich(JSONLD_URL, fetch)
    assert result.extraction_status == "ok"
    assert result.metadata_quality == "useful"
    assert "عطر ورد" in result.page_title
    assert "نورة عبدالله" in result.author_or_channel
    assert trace._fields.get("structured_data_status") == "ok"
    assert trace._fields.get("metadata_source_selected") == "json_ld"


def test_03_declared_oembed_endpoint() -> None:
    oembed_url = "https://oembed.example.test/embed?format=json"
    fetch = RoutingFetch(
        {
            OEMBED_PAGE_URL: OEMBED_DECLARE_HTML,
            "https://oembed.example.test/embed*": lambda _url: SafeHttpResult(
                ok=True,
                url=_url,
                final_url=_url,
                status=200,
                content_type="application/json",
                body=_oembed_json(
                    title="مقطع فيديو تجريبي",
                    author_name="قناة عامة",
                    thumbnail_url="https://cdn.example.test/thumb.jpg",
                ).encode("utf-8"),
            ),
        }
    )
    result, trace = _enrich(OEMBED_PAGE_URL, fetch)
    assert result.extraction_status == "ok"
    assert result.metadata_quality == "useful"
    assert result.page_title == "مقطع فيديو تجريبي"
    assert result.author_or_channel == "قناة عامة"
    assert trace._fields.get("oembed_discovery_status") == "declared"
    assert trace._fields.get("provider_enrichment_status") == "ok"
    assert len(fetch.calls) <= MAX_EXTERNAL_FETCHES


def test_04_tiktok_thin_html_then_oembed() -> None:
    oembed_endpoint = f"https://www.tiktok.com/oembed?url={TIKTOK_URL}"
    fetch = RoutingFetch(
        {
            TIKTOK_URL: TIKTOK_THIN_HTML,
            "https://www.tiktok.com/oembed*": lambda _url: SafeHttpResult(
                ok=True,
                url=_url,
                final_url=_url,
                status=200,
                content_type="application/json",
                body=_oembed_json(
                    title="فيديو تجريبي عن حذاء رياضي",
                    author_name="creator",
                    thumbnail_url="https://cdn.example.test/tiktok.jpg",
                    html="<iframe src='evil'></iframe>",
                ).encode("utf-8"),
            ),
        }
    )
    result, trace = _enrich(TIKTOK_URL, fetch)
    assert result.extraction_status == "ok"
    assert result.metadata_quality == "useful"
    assert result.useful_metadata_present is True
    assert "حذاء" in result.page_title
    assert result.author_or_channel == "creator"
    assert trace._fields.get("provider_adapter") == "tiktok"
    assert trace._fields.get("metadata_source_selected") == "oembed"
    assert len(fetch.calls) == 2


def test_05_product_page_useful_metadata() -> None:
    fetch = RoutingFetch({PRODUCT_URL: PRODUCT_HTML})
    result, _trace = _enrich(PRODUCT_URL, fetch)
    assert result.extraction_status == "ok"
    assert result.metadata_quality == "useful"
    assert result.content_kind == "product"
    assert "حذاء رياضي" in result.page_title


def test_06_readable_text_fallback() -> None:
    fetch = RoutingFetch({PLAIN_HTML_URL: PLAIN_HTML})
    result, trace = _enrich(PLAIN_HTML_URL, fetch)
    assert result.extraction_status == "ok"
    assert result.metadata_quality == "useful"
    assert "متجر تجريبي عام" in (result.readable_excerpt or result.safe_description or "")
    assert trace._fields.get("readable_text_status") == "ok"


def test_07_empty_js_shell_no_useful_metadata() -> None:
    fetch = RoutingFetch({EMPTY_JS_URL: EMPTY_JS_HTML})
    result, trace = _enrich(EMPTY_JS_URL, fetch)
    assert result.extraction_status == "unavailable"
    assert result.metadata_quality == "empty"
    assert result.useful_metadata_present is False
    assert trace._fields.get("metadata_quality") == "empty"


def test_08_oembed_html_field_ignored() -> None:
    parsed = parse_oembed_payload(
        {
            "title": "safe title",
            "html": "<script>alert('xss')</script><iframe></iframe>",
            "description": "safe description",
        }
    )
    assert "<script" not in json.dumps(parsed).lower()
    assert "<iframe" not in json.dumps(parsed).lower()


def test_09_internal_oembed_endpoint_blocked() -> None:
    internal = "http://127.0.0.1/oembed"
    html = f"""<html><head>
<link rel="alternate" type="application/json+oembed" href="{internal}" />
</head></html>"""
    fetch = RoutingFetch({INTERNAL_OEMBED_URL: html})
    result, trace = _enrich(INTERNAL_OEMBED_URL, fetch)
    assert len(fetch.calls) == 1
    assert trace._fields.get("provider_enrichment_status") == "blocked"


def test_10_redirect_to_blocked_domain() -> None:
    fetch = RoutingFetch(
        {
            BLOCKED_REDIRECT_URL: SafeHttpResult(
                ok=False,
                url=BLOCKED_REDIRECT_URL,
                final_url="http://127.0.0.1/private",
                error_class="ip_blocked",
            )
        }
    )
    result, _trace = _enrich(BLOCKED_REDIRECT_URL, fetch)
    assert result.extraction_status == "blocked"
    assert result.error_class == "ip_blocked"


def test_11_timeout_or_invalid_json_best_effort() -> None:
    oembed_url = "https://oembed.example.test/bad"
    html = f"""<html><head>
<link rel="alternate" type="application/json+oembed" href="{oembed_url}" />
<title>Home</title>
</head></html>"""
    fetch = RoutingFetch(
        {
            TIMEOUT_URL: html,
            "https://oembed.example.test/bad*": "not-json",
        }
    )
    result, trace = _enrich(TIMEOUT_URL, fetch)
    assert trace._fields.get("provider_enrichment_status") == "invalid_json"
    assert result.useful_metadata_present is False
    assert result.metadata_quality in {"thin", "empty"}


def test_12_useful_metadata_reaches_user_role_provider_payload() -> None:
    out = _run_path(ARTICLE_URL, fetch=RoutingFetch({ARTICLE_URL: ARTICLE_HTML}))
    parsed = dict(out["user_turn_facts"] or {})
    assert parsed.get("useful_metadata_present") is True
    assert parsed.get("page_title")
    assert parsed["page_title"] in json.dumps(out["providers"], ensure_ascii=False)
    assert parsed["page_title"] not in (out["prompt"] or "")


def test_13_no_raw_html_or_external_instructions_in_system_prompt() -> None:
    out = _run_path(ARTICLE_URL, fetch=RoutingFetch({ARTICLE_URL: ARTICLE_HTML}))
    system = str(out["prompt"] or "")
    assert "<html" not in system.lower()
    assert URL_CONTEXT_USER_TURN_BEGIN not in system
    assert "ignore previous instructions" not in system.lower()


def test_14_existing_good_html_path_unchanged() -> None:
    out = _run_path(PUBLIC_PAGE_URL, fetch=RoutingFetch({PUBLIC_PAGE_URL: HTML_FIXTURE}))
    parsed = dict(out["user_turn_facts"] or {})
    assert parsed.get("extraction_status") == "ok"
    assert parsed.get("metadata_quality") == "useful"
    assert "قميص قطني" in str(parsed.get("page_title") or "")


def test_15_max_two_external_fetches_per_url() -> None:
    oembed_url = "https://oembed.example.test/embed"
    html = f"""<html><head>
<link rel="alternate" type="application/json+oembed" href="{oembed_url}" />
<title>TikTok - Make Your Day</title>
</head></html>"""
    fetch = RoutingFetch(
        {
            TIKTOK_URL: html,
            "https://oembed.example.test/embed*": _oembed_json(title="ignored thin", author_name=""),
            f"https://www.tiktok.com/oembed?url={TIKTOK_URL}": _oembed_json(
                title="فيديو TikTok",
                author_name="creator",
            ),
        }
    )
    _result, trace = _enrich(TIKTOK_URL, fetch)
    assert len(fetch.calls) <= MAX_EXTERNAL_FETCHES
    assert int(trace._fields.get("external_fetch_count") or 0) <= MAX_EXTERNAL_FETCHES


def test_trace_fields_sanitized_without_raw_content() -> None:
    trace = UrlContextTraceRecorder()
    trace.mark_detector(url_count=1, candidate_count=1)
    trace.mark_fetch_attempted()
    trace.mark_pipeline_state(
        state=type(
            "S",
            (),
            {
                "to_trace_fields": lambda self: {
                    "metadata_sources_attempted": "html_metadata,oembed",
                    "metadata_source_selected": "oembed",
                    "standard_metadata_status": "thin",
                    "structured_data_status": "empty",
                    "oembed_discovery_status": "adapter",
                    "provider_adapter": "tiktok",
                    "provider_enrichment_status": "ok",
                    "readable_text_status": "not_run",
                    "metadata_quality": "useful",
                    "useful_metadata_present": True,
                    "external_fetch_count": 2,
                    "raw_html": "<script>",
                    "page_title": "secret",
                }
            },
        )()
    )
    trace.mark_enrichment_result(
        type(
            "R",
            (),
            {
                "extraction_status": "ok",
                "page_title": "secret",
                "safe_description": "",
                "author_or_channel": "creator",
                "preview_image_metadata": {"url_present": True},
                "source": "oembed",
                "metadata_quality": "useful",
                "useful_metadata_present": True,
                "error_class": "",
            },
        )()
    )
    trace.finalize(completed=True)
    sanitized = sanitize_url_context_trace(trace.to_sparse_public_dict())
    assert sanitized is not None
    assert sanitized.get("metadata_quality") == "useful"
    assert sanitized.get("external_fetch_count") == 2
    assert "raw_html" not in sanitized
    assert "secret" not in json.dumps(sanitized)
