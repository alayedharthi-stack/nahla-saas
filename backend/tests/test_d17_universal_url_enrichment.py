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
import time
from typing import Any, Optional
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.facts.url_context_facts import (  # noqa: E402
    URL_CONTEXT_USER_TURN_BEGIN,
    project_url_context_facts,
)
from modules.ai.brain.observability.url_context_trace import (  # noqa: E402
    UrlContextTraceRecorder,
    sanitize_url_context_trace,
)
from services.safe_http_fetch import SafeHttpResult, TOTAL_TIMEOUT_S  # noqa: E402
from services.url_context import enrich_current_turn_urls, reset_url_context_cache  # noqa: E402
from services.url_enrichment.oembed import parse_oembed_payload  # noqa: E402
from services.url_enrichment.pipeline import MAX_EXTERNAL_FETCHES, run_enrichment_pipeline  # noqa: E402
from services.url_enrichment.url_safety import resolve_http_url, sanitize_oembed_target_url  # noqa: E402

from test_d17_url_context_enrichment import (  # noqa: E402
    HTML_FIXTURE,
    PUBLIC_PAGE_URL,
    _run_path,
)

ARTICLE_URL = "https://news.example.test/article/cotton-shirt"
JSONLD_URL = "https://blog.example.test/post/jsonld-only"
OEMBED_PAGE_URL = "https://video.example.test/watch/clip"
TIKTOK_URL = "https://www.tiktok.com/@creator/video/9001"
TIKTOK_SENSITIVE_URL = (
    "https://www.tiktok.com/@creator/video/9001?token=secret&utm_source=x#fragment"
)
PRODUCT_URL = "https://shop.example.test/p/sneaker"
PLAIN_HTML_URL = "https://plain.example.test/about"
EMPTY_JS_URL = "https://spa.example.test/empty"
INTERNAL_OEMBED_URL = "https://media.example.test/embed-me"
BLOCKED_REDIRECT_URL = "https://blocked.example.test/redirect"
TIMEOUT_URL = "https://slow.example.test/page"
RELATIVE_BASE_URL = "https://cdn.example.test/content/page/"

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
 href="/oembed.json" />
</head><body><p>ignored</p></body></html>"""
TIKTOK_THIN_HTML = """<!doctype html><html><head>
<title>TikTok - Make Your Day</title>
</head><body><div id="app"></div></body></html>"""
TIKTOK_PLATFORM_ONLY_HTML = """<!doctype html><html><head>
<title>TikTok - Make Your Day</title>
<meta property="og:site_name" content="TikTok">
</head><body></body></html>"""
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
RELATIVE_METADATA_HTML = """<!doctype html><html><head>
<link rel="canonical" href="../article/view">
<link rel="alternate" type="application/json+oembed" href="oembed.json">
<meta property="og:image" content="../images/thumb.jpg">
</head><body></body></html>"""


class RoutingFetch:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.deadlines: list[Optional[float]] = []

    def __call__(self, url: str, deadline: Optional[float] = None) -> SafeHttpResult:
        self.calls.append(url)
        self.deadlines.append(deadline)
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


def _ok_page(url: str, html: str) -> SafeHttpResult:
    return SafeHttpResult(
        ok=True,
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        body=html.encode("utf-8"),
    )


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


def test_03_declared_same_origin_oembed_endpoint() -> None:
    fetch = RoutingFetch(
        {
            OEMBED_PAGE_URL: OEMBED_DECLARE_HTML,
            "https://video.example.test/oembed.json*": lambda _url: SafeHttpResult(
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


def test_09_same_origin_oembed_blocked_by_safe_fetch() -> None:
    html = """<html><head>
<link rel="alternate" type="application/json+oembed" href="https://media.example.test/oembed.json" />
<title>Home</title>
</head></html>"""
    fetch = RoutingFetch(
        {
            INTERNAL_OEMBED_URL: html,
            "https://media.example.test/oembed.json*": SafeHttpResult(
                ok=False,
                url="https://media.example.test/oembed.json",
                final_url="https://media.example.test/oembed.json",
                error_class="ip_blocked",
            ),
        }
    )
    result, trace = _enrich(INTERNAL_OEMBED_URL, fetch)
    assert len(fetch.calls) == 2
    assert trace._fields.get("provider_enrichment_status") == "blocked"
    assert result.useful_metadata_present is False


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


def test_11_invalid_json_same_origin_best_effort() -> None:
    html = """<html><head>
<link rel="alternate" type="application/json+oembed" href="/bad.json" />
<title>Home</title>
</head></html>"""
    fetch = RoutingFetch(
        {
            TIMEOUT_URL: html,
            "https://slow.example.test/bad.json*": "not-json",
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
    html = """<html><head>
<link rel="alternate" type="application/json+oembed" href="/embed" />
<title>TikTok - Make Your Day</title>
</head></html>"""
    fetch = RoutingFetch(
        {
            TIKTOK_URL: html,
            "https://www.tiktok.com/oembed*": _oembed_json(
                title="فيديو TikTok",
                author_name="creator",
            ),
        }
    )
    _result, trace = _enrich(TIKTOK_URL, fetch)
    assert len(fetch.calls) <= MAX_EXTERNAL_FETCHES
    assert int(trace._fields.get("external_fetch_count") or 0) <= MAX_EXTERNAL_FETCHES


def test_platform_name_only_stays_thin_until_adapter() -> None:
    fetch = RoutingFetch(
        {
            TIKTOK_URL: TIKTOK_PLATFORM_ONLY_HTML,
            "https://www.tiktok.com/oembed*": _oembed_json(
                title="فيديو عن عطر ورد",
                author_name="creator",
            ),
        }
    )
    result, trace = _enrich(TIKTOK_URL, fetch)
    assert trace._fields.get("standard_metadata_status") == "thin"
    assert result.metadata_quality == "useful"
    assert result.useful_metadata_present is True
    assert trace._fields.get("provider_adapter") == "tiktok"


def test_sensitive_query_and_fragment_not_forwarded_to_oembed() -> None:
    fetch = RoutingFetch(
        {
            TIKTOK_SENSITIVE_URL: TIKTOK_THIN_HTML,
            "https://www.tiktok.com/oembed*": lambda url: SafeHttpResult(
                ok=True,
                url=url,
                final_url=url,
                status=200,
                content_type="application/json",
                body=_oembed_json(title="فيديو منقح", author_name="creator").encode("utf-8"),
            ),
        }
    )
    result, trace = _enrich(TIKTOK_SENSITIVE_URL, fetch)
    assert len(fetch.calls) == 2
    oembed_call = fetch.calls[1]
    assert "token=secret" not in oembed_call
    assert "utm_source" not in oembed_call
    assert "#fragment" not in oembed_call
    blob = json.dumps(
        {
            "trace": trace.to_sparse_public_dict(),
            "facts": project_url_context_facts([result]),
        },
        ensure_ascii=False,
    )
    assert "token=secret" not in blob
    assert "utm_source=x" not in blob
    assert result.metadata_quality == "useful"


def test_cross_origin_declared_oembed_is_not_fetched() -> None:
    html = """<html><head>
<link rel="alternate" type="application/json+oembed" href="https://evil.example.test/oembed" />
<title>Home</title>
</head></html>"""
    fetch = RoutingFetch(
        {
            OEMBED_PAGE_URL: html,
            "https://evil.example.test/oembed*": _oembed_json(title="should-not-run", author_name="x"),
        }
    )
    _result, trace = _enrich(OEMBED_PAGE_URL, fetch)
    assert len(fetch.calls) == 1
    assert trace._fields.get("oembed_discovery_status") == "cross_origin_blocked"


def test_relative_url_resolution_for_metadata_and_oembed() -> None:
    fetch = RoutingFetch(
        {
            RELATIVE_BASE_URL: RELATIVE_METADATA_HTML,
            "https://cdn.example.test/content/page/oembed.json*": _oembed_json(
                title="مقال نسبي",
                author_name="كاتب",
            ),
        }
    )
    result, trace = _enrich(RELATIVE_BASE_URL, fetch)
    assert result.canonical_url.endswith("/content/article/view")
    assert resolve_http_url(RELATIVE_BASE_URL, "../images/thumb.jpg") == (
        "https://cdn.example.test/content/images/thumb.jpg"
    )
    assert trace._fields.get("provider_enrichment_status") == "ok"
    assert result.page_title == "مقال نسبي"


def test_relative_url_resolution_rejects_credentials_and_non_http() -> None:
    assert resolve_http_url("https://cdn.example.test/a/", "javascript:alert(1)") == ""
    assert resolve_http_url("https://cdn.example.test/a/", "data:text/plain,hi") == ""
    assert resolve_http_url("https://cdn.example.test/a/", "//evil.test/x") == "https://evil.test/x"
    assert resolve_http_url("https://user:pass@cdn.example.test/a/", "../x") == ""


def test_shared_enrichment_deadline_not_doubled() -> None:
    shared_deadline = 12345.678

    async def _fetch(url: str, deadline: Optional[float] = None) -> SafeHttpResult:
        recorded.append((url, deadline))
        return SafeHttpResult(
            ok=True,
            url=url,
            final_url=url,
            status=200,
            content_type="application/json",
            body=_oembed_json(title="فيديو TikTok", author_name="creator").encode("utf-8"),
        )

    recorded: list[tuple[str, Optional[float]]] = []

    async def _go() -> None:
        await run_enrichment_pipeline(
            TIKTOK_URL,
            _ok_page(TIKTOK_URL, TIKTOK_THIN_HTML),
            fetch=_fetch,
            deadline=shared_deadline,
        )

    asyncio.run(_go())
    assert len(recorded) == 1
    assert recorded[0][1] == shared_deadline
    assert shared_deadline < time.monotonic() + (TOTAL_TIMEOUT_S * 2)


def test_enrich_turn_passes_single_deadline_to_both_fetches() -> None:
    fetch = RoutingFetch(
        {
            TIKTOK_URL: TIKTOK_THIN_HTML,
            "https://www.tiktok.com/oembed*": _oembed_json(title="فيديو", author_name="creator"),
        }
    )
    _result, _trace = _enrich(TIKTOK_URL, fetch)
    assert len(fetch.deadlines) == 2
    assert fetch.deadlines[0] is not None
    assert fetch.deadlines[1] == fetch.deadlines[0]


def test_tiktok_oembed_reaches_user_role_provider_payload() -> None:
    fetch = RoutingFetch(
        {
            TIKTOK_URL: TIKTOK_THIN_HTML,
            "https://www.tiktok.com/oembed*": _oembed_json(
                title="فيديو TikTok عن حذاء رياضي",
                author_name="creator",
                html="<iframe></iframe>",
            ),
        }
    )
    out = _run_path(TIKTOK_URL, fetch=fetch)
    parsed = dict(out["user_turn_facts"] or {})
    providers_blob = json.dumps(out["providers"], ensure_ascii=False)
    system_blob = str(out["prompt"] or "")

    assert parsed.get("metadata_quality") == "useful"
    assert parsed.get("watched_or_transcribed") is False
    assert "فيديو TikTok" in providers_blob
    assert "creator" in providers_blob
    assert "فيديو TikTok" not in system_blob
    assert "<iframe" not in providers_blob
    assert "<html" not in providers_blob.lower()
    assert URL_CONTEXT_USER_TURN_BEGIN in str(out["provider_message"] or "")
    assert len(fetch.calls) == 2
    assert getattr(out["ctx"], "url_context_fetch_count", 0) <= MAX_EXTERNAL_FETCHES


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
