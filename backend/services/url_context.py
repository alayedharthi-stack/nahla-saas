"""Current-turn URL enrichment for MerchantBrain.

Produces provenance-labelled URL_CONTEXT facts. Does not infer purchase
or checkout intent from URL presence. Enrichment uses the universal
``url_enrichment`` pipeline (HTML metadata, JSON-LD, oEmbed, readable text).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from urllib.parse import unquote, urlparse

from core.inbound_url_spans import (
    extract_inbound_url_spans,
)
from observability.rate_limiter import check_rate_limit
from services.safe_http_fetch import (
    SafeHttpResult,
    TOTAL_TIMEOUT_S,
    fetch_url_async,
    redact_url_for_log,
)
from services.url_enrichment import run_enrichment_pipeline
from services.url_enrichment.types import EnrichmentDraft, PipelineTraceState
from services.url_enrichment.url_safety import sanitize_oembed_target_url

logger = logging.getLogger("nahla.url_context")

MAX_URLS_PER_TURN = 1
TITLE_MAX = 180
DESCRIPTION_MAX = 400
AUTHOR_MAX = 80
IMAGE_URL_MAX = 500
CACHE_TTL_S = 600.0
CACHE_MAX_ENTRIES = 256
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW_S = 60

FetchFn = Callable[[str], Union[SafeHttpResult, Awaitable[SafeHttpResult]]]
CatalogLookup = Callable[[int, str], Optional[Dict[str, Any]]]


@dataclass
class UrlContext:
    original_url: str = ""
    canonical_url: str = ""
    provider_domain: str = ""
    content_type: str = ""
    page_title: str = ""
    safe_description: str = ""
    author_or_channel: str = ""
    preview_image_metadata: Dict[str, Any] = field(default_factory=dict)
    extraction_status: str = "unavailable"
    confidence: float = 0.0
    source: str = "none"
    error_class: str = ""
    content_trust: str = "untrusted_web_metadata"
    watched_or_transcribed: bool = False
    catalog_product: Dict[str, Any] = field(default_factory=dict)
    fetch_body_truncated: bool = False
    content_kind: str = ""
    published_at: str = ""
    readable_excerpt: str = ""
    metadata_quality: str = "empty"
    useful_metadata_present: bool = False

    def to_public_dict(self) -> Dict[str, Any]:
        safe_original = sanitize_oembed_target_url(
            final_url=self.original_url,
            canonical_url=self.canonical_url or self.original_url,
        ) or self.original_url
        safe_canonical = sanitize_oembed_target_url(
            final_url=self.canonical_url or self.original_url,
            canonical_url=self.canonical_url or self.original_url,
        ) or safe_original
        payload = {
            "original_url": safe_original,
            "canonical_url": safe_canonical,
            "provider_domain": self.provider_domain,
            "content_type": self.content_type,
            "page_title": self.page_title,
            "safe_description": self.safe_description,
            "author_or_channel": self.author_or_channel,
            "preview_image_metadata": dict(self.preview_image_metadata or {}),
            "extraction_status": self.extraction_status,
            "confidence": self.confidence,
            "source": self.source,
            "error_class": self.error_class,
            "content_trust": "untrusted_web_metadata",
            "content_channel": "untrusted_web_metadata",
            "watched_or_transcribed": False,
            "not_instructions": True,
            "fetch_body_truncated": bool(self.fetch_body_truncated),
            "content_kind": self.content_kind,
            "published_at": self.published_at,
            "readable_excerpt": self.readable_excerpt,
            "metadata_quality": self.metadata_quality,
            "useful_metadata_present": bool(self.useful_metadata_present),
        }
        if self.catalog_product:
            payload["catalog_product"] = dict(self.catalog_product)
        return payload


_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[float, UrlContext]] = {}
_turn_fetches: threading.local = threading.local()


def _cache_key(tenant_id: int, url: str) -> str:
    needle = _normalize_url_for_compare(url)
    digest = hashlib.sha256(f"{int(tenant_id or 0)}|{needle}".encode("utf-8")).hexdigest()
    return digest


def _cache_get(tenant_id: int, url: str) -> Optional[UrlContext]:
    key = _cache_key(tenant_id, url)
    now = time.monotonic()
    with _cache_lock:
        row = _cache.get(key)
        if not row:
            return None
        expires, value = row
        if expires < now:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(tenant_id: int, url: str, value: UrlContext) -> None:
    key = _cache_key(tenant_id, url)
    now = time.monotonic()
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            stale = [k for k, (exp, _) in _cache.items() if exp < now]
            for item in stale:
                _cache.pop(item, None)
            if len(_cache) >= CACHE_MAX_ENTRIES:
                oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[: max(1, len(_cache) // 8)]
                for item, _val in oldest:
                    _cache.pop(item, None)
        _cache[key] = (now + CACHE_TTL_S, value)


def begin_url_context_turn() -> None:
    _turn_fetches.urls = set()


def reset_url_context_cache() -> None:
    with _cache_lock:
        _cache.clear()
    begin_url_context_turn()
    try:
        from observability import rate_limiter as rl  # noqa: PLC0415

        with rl._store_lock:
            stale = [
                key
                for key in list(rl._store)
                if str(key).startswith("url_context:")
            ]
            for key in stale:
                rl._store.pop(key, None)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — test reset must not fail enrichment
        pass


def _normalize_url_for_compare(url: str) -> str:
    """Cache/catalog compare key only.

    Inbound detection stays in ``core.inbound_url_spans``. This helper
    must not grow into a second parser or fetch path.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    candidate = raw
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
    except Exception:
        return raw.lower().rstrip("/")
    host = (parsed.hostname or "").lower()
    path = unquote(parsed.path or "")
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    query = parsed.query or ""
    return f"{host}{path}?{query}"


def _sanitize_text(value: Any, limit: int) -> str:
    """Length-limit and strip markup/control chars. Not a jailbreak detector.

    Natural-language page text remains quoted untrusted data. Do not add
    customer-language phrase lists here.
    """
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _absolute_url(base: str, maybe_relative: str) -> str:
    raw = str(maybe_relative or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    try:
        parsed = urlparse(base)
        if raw.startswith("//"):
            return f"{parsed.scheme}:{raw}"
        if raw.startswith("/"):
            return f"{parsed.scheme}://{parsed.netloc}{raw}"
    except Exception:
        return ""
    return ""


def parse_html_metadata(html: str, *, base_url: str) -> Dict[str, str]:
    from services.url_enrichment.standard_metadata import parse_html_metadata as _parse  # noqa: PLC0415

    draft, _ = _parse(html, base_url)
    return {
        "title": draft.page_title,
        "description": draft.safe_description,
        "image": draft.preview_image,
        "canonical": draft.canonical_url,
        "author": draft.author_or_channel,
        "html_title": _sanitize_text(
            re.search(r"<title[^>]*>(.*?)</title>", html or "", flags=re.I | re.S).group(1)
            if re.search(r"<title[^>]*>(.*?)</title>", html or "", flags=re.I | re.S)
            else "",
            TITLE_MAX,
        ),
    }


def parse_json_metadata(body: bytes) -> Dict[str, str]:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "title": str(data.get("title") or ""),
        "description": str(data.get("description") or "")[:DESCRIPTION_MAX],
        "image": str(data.get("thumbnail_url") or data.get("url") or "")[:IMAGE_URL_MAX],
        "canonical": str(data.get("url") or ""),
        "author": str(data.get("author_name") or data.get("provider_name") or ""),
        "html_title": "",
    }


def _provider_domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _unavailable(original: str, error_class: str, *, source: str = "none") -> UrlContext:
    return UrlContext(
        original_url=original,
        canonical_url=original,
        provider_domain=_provider_domain(original),
        extraction_status="unavailable",
        confidence=0.0,
        source=source,
        error_class=error_class,
    )


def _blocked(original: str, error_class: str) -> UrlContext:
    ctx = _unavailable(original, error_class, source="safe_http")
    ctx.extraction_status = "blocked"
    return ctx


def lookup_catalog_product_by_url(db: Any, tenant_id: int, url: str) -> Optional[Dict[str, Any]]:
    if db is None or not tenant_id or not url:
        return None
    needle = _normalize_url_for_compare(url)
    if not needle:
        return None
    try:
        from models import Product  # noqa: PLC0415
    except Exception:
        logger.exception("[url_context] Product model unavailable for catalog URL match")
        return None
    try:
        rows = (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id))
            .limit(400)
            .all()
        )
    except Exception:
        logger.exception("[url_context] catalog lookup failed tenant=%s", tenant_id)
        return None
    for row in rows or []:
        meta = getattr(row, "extra_metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        candidates = [
            meta.get("product_url"),
            meta.get("url"),
            getattr(row, "product_url", None),
            getattr(row, "url", None),
        ]
        for candidate in candidates:
            if _normalize_url_for_compare(str(candidate or "")) == needle:
                return {
                    "product_id": str(getattr(row, "id", "") or ""),
                    "external_id": str(getattr(row, "external_id", "") or ""),
                    "title": _sanitize_text(getattr(row, "title", "") or "", TITLE_MAX),
                    "price": getattr(row, "price", None),
                    "purchase_intent": False,
                }
    return None


def _from_catalog(original: str, product: Dict[str, Any]) -> UrlContext:
    title = _sanitize_text(product.get("title"), TITLE_MAX)
    return UrlContext(
        original_url=original,
        canonical_url=original,
        provider_domain=_provider_domain(original),
        content_type="catalog_product",
        page_title=title,
        safe_description=_sanitize_text(title, DESCRIPTION_MAX),
        extraction_status="ok",
        confidence=0.95,
        source="tenant_catalog",
        catalog_product={
            "product_id": str(product.get("product_id") or ""),
            "external_id": str(product.get("external_id") or ""),
            "title": title,
            "price": product.get("price"),
            "purchase_intent": False,
        },
    )


def _primary_source_label(draft: EnrichmentDraft) -> str:
    sources = list(draft.sources_used or [])
    if "oembed" in sources:
        return "oembed"
    if "json_ld" in sources:
        return "json_ld"
    if "readable_text" in sources and not draft.safe_description and not draft.author_or_channel:
        return "readable_text"
    if "html_metadata" in sources:
        return "html_metadata"
    return draft.enrichment_source or "html_metadata"


def _context_from_metadata_dict(
    original: str,
    fetched: SafeHttpResult,
    meta: Dict[str, str],
    *,
    source: str,
    draft: Optional[EnrichmentDraft] = None,
) -> UrlContext:
    title = _sanitize_text(meta.get("title") or meta.get("html_title"), TITLE_MAX)
    description = _sanitize_text(meta.get("description"), DESCRIPTION_MAX)
    author = _sanitize_text(meta.get("author"), AUTHOR_MAX)
    image = _sanitize_text(meta.get("image"), IMAGE_URL_MAX)
    canonical = _sanitize_text(meta.get("canonical") or fetched.final_url or original, 500)
    excerpt = _sanitize_text(getattr(draft, "readable_excerpt", "") if draft else "", DESCRIPTION_MAX)
    metadata_quality = str(getattr(draft, "metadata_quality", "") or "empty")
    useful = bool(getattr(draft, "useful_metadata_present", False))
    if not title and not description and not author and not excerpt:
        truncated = bool(getattr(fetched, "body_truncated", False))
        if truncated:
            return _unavailable(original, "oversized", source=source)
        return _unavailable(original, "metadata_missing", source=source)
    confidence = 0.85 if useful else (0.8 if title else 0.55)
    image_meta: Dict[str, Any] = {}
    if image.startswith("http"):
        image_meta = {"url_present": True, "url_host": _provider_domain(image)}
    return UrlContext(
        original_url=original,
        canonical_url=canonical or fetched.final_url or original,
        provider_domain=_provider_domain(canonical or fetched.final_url or original),
        content_type=fetched.content_type or "text/html",
        page_title=title,
        safe_description=description,
        author_or_channel=author,
        preview_image_metadata=image_meta,
        extraction_status="ok",
        confidence=confidence,
        source=source,
        fetch_body_truncated=bool(getattr(fetched, "body_truncated", False)),
        content_kind=_sanitize_text(getattr(draft, "content_kind", "") if draft else "", 80),
        published_at=_sanitize_text(getattr(draft, "published_at", "") if draft else "", 80),
        readable_excerpt=excerpt,
        metadata_quality=metadata_quality,
        useful_metadata_present=useful,
    )


async def _bounded_fetch(
    url: str,
    *,
    fetch: FetchFn,
    deadline: float,
) -> SafeHttpResult:
    try:
        got = fetch(url, deadline=deadline)
    except TypeError:
        got = fetch(url)
    return await got if asyncio.iscoroutine(got) else got


async def _from_fetch_async(
    original: str,
    fetched: SafeHttpResult,
    *,
    fetch: FetchFn,
    trace: Any = None,
    deadline: Optional[float] = None,
) -> UrlContext:
    if not fetched.ok:
        err = fetched.error_class or "unavailable"
        if err in {
            "ip_blocked",
            "host_blocked",
            "scheme_blocked",
            "credentials_blocked",
        }:
            return _blocked(original, err)
        return _unavailable(original, err, source="safe_http")

    body = fetched.body or b""
    truncated = bool(getattr(fetched, "body_truncated", False))
    ctype = (fetched.content_type or "").lower()
    if "json" in ctype:
        meta = parse_json_metadata(body)
        return _context_from_metadata_dict(original, fetched, meta, source="json_metadata")

    async def _secondary_fetch(url: str, **kwargs: Any) -> SafeHttpResult:
        dl = float(kwargs.get("deadline") or deadline or 0.0)
        if dl > 0:
            return await _bounded_fetch(url, fetch=fetch, deadline=dl)
        return await _bounded_fetch(url, fetch=fetch, deadline=time.monotonic() + TOTAL_TIMEOUT_S)

    draft, pipeline_trace = await run_enrichment_pipeline(
        original,
        fetched,
        fetch=_secondary_fetch,
        deadline=deadline,
    )
    _trace_call(trace, "mark_pipeline_state", state=pipeline_trace)
    meta = {
        "title": draft.page_title,
        "description": draft.safe_description,
        "image": draft.preview_image,
        "canonical": draft.canonical_url or fetched.final_url or original,
        "author": draft.author_or_channel,
        "html_title": draft.page_title,
    }
    return _context_from_metadata_dict(
        original,
        fetched,
        meta,
        source=_primary_source_label(draft),
        draft=draft,
    )


def _from_fetch(original: str, fetched: SafeHttpResult) -> UrlContext:
    """Sync compatibility shim for callers that do not run the async pipeline."""
    if not fetched.ok:
        err = fetched.error_class or "unavailable"
        if err in {
            "ip_blocked",
            "host_blocked",
            "scheme_blocked",
            "credentials_blocked",
        }:
            return _blocked(original, err)
        return _unavailable(original, err, source="safe_http")
    meta: Dict[str, str] = {}
    source = "html_metadata"
    body = fetched.body or b""
    truncated = bool(getattr(fetched, "body_truncated", False))
    ctype = (fetched.content_type or "").lower()
    if "json" in ctype:
        meta = parse_json_metadata(body)
        source = "json_metadata"
    else:
        try:
            html = body.decode("utf-8", "replace")
        except Exception:
            html = ""
        meta = parse_html_metadata(html, base_url=fetched.final_url or original)
    return _context_from_metadata_dict(original, fetched, meta, source=source)


def select_current_turn_urls(message: str) -> List[str]:
    spans = extract_inbound_url_spans(message or "")
    out: List[str] = []
    seen = set()
    for span in spans:
        raw = str(span or "").strip()
        if raw.lower().startswith("www."):
            raw = "https://" + raw
        elif not re.match(r"^https?://", raw, re.IGNORECASE):
            raw = "https://" + raw
        key = _normalize_url_for_compare(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(raw)
        if len(out) >= MAX_URLS_PER_TURN:
            break
    return out


def _trace_call(trace: Any, method: str, **kwargs: Any) -> None:
    if trace is None:
        return
    try:
        fn = getattr(trace, method, None)
        if callable(fn):
            fn(**kwargs)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — trace must not affect enrichment
        pass


async def enrich_current_turn_urls(
    *,
    message: str,
    tenant_id: int = 0,
    db: Any = None,
    fetch: Optional[FetchFn] = None,
    catalog_lookup: Optional[CatalogLookup] = None,
    trace: Any = None,
) -> List[UrlContext]:
    """Enrich at most one distinct current-turn URL. Never raises."""
    spans = extract_inbound_url_spans(message or "")
    urls = select_current_turn_urls(message)
    _trace_call(
        trace,
        "mark_detector",
        url_count=len(spans),
        candidate_count=len(urls),
    )
    if not urls:
        _trace_call(trace, "mark_no_url")
        return []
    original = urls[0]
    try:
        cached = _cache_get(int(tenant_id or 0), original)
        if cached is not None:
            _trace_call(trace, "mark_cache_status", status="hit")
            _trace_call(trace, "mark_enrichment_result", result=cached)
            return [cached]
        _trace_call(trace, "mark_cache_status", status="miss")
        lookup = catalog_lookup
        if lookup is None and db is not None:
            lookup = lambda tid, url: lookup_catalog_product_by_url(db, tid, url)  # noqa: E731
        if lookup is not None:
            product = lookup(int(tenant_id or 0), original)
            _trace_call(
                trace,
                "mark_catalog_lookup",
                ran=True,
                matched=bool(product),
            )
            if product:
                result = _from_catalog(original, product)
                _cache_put(int(tenant_id or 0), original, result)
                _trace_call(trace, "mark_enrichment_result", result=result)
                return [result]
        rate_key = f"url_context:{int(tenant_id or 0)}"
        allowed = check_rate_limit(
            rate_key,
            max_count=RATE_LIMIT_MAX,
            window_seconds=RATE_LIMIT_WINDOW_S,
        )
        _trace_call(trace, "mark_rate_limit", checked=True, allowed=allowed)
        if not allowed:
            result = _unavailable(original, "rate_limited", source="rate_limit")
            _trace_call(trace, "mark_enrichment_result", result=result)
            return [result]
        fetches = getattr(_turn_fetches, "urls", None)
        if fetches is None:
            fetches = set()
            _turn_fetches.urls = fetches
        fetch_marker = f"{int(tenant_id or 0)}|{_normalize_url_for_compare(original)}"
        if fetch_marker in fetches:
            _trace_call(trace, "mark_cache_status", status="duplicate_turn")
            result = _unavailable(original, "duplicate_turn_fetch", source="cache")
            _trace_call(trace, "mark_enrichment_result", result=result)
            return [result]
        fetches.add(fetch_marker)
        _trace_call(trace, "mark_fetch_attempted")
        enrichment_deadline = time.monotonic() + TOTAL_TIMEOUT_S
        if fetch is None:
            fetched = await fetch_url_async(original, deadline=enrichment_deadline)
            fetch_fn: FetchFn = fetch_url_async
        else:
            fetched = await _bounded_fetch(original, fetch=fetch, deadline=enrichment_deadline)
            fetch_fn = fetch
        _trace_call(trace, "mark_fetch_transport", fetched=fetched)
        result = await _from_fetch_async(
            original,
            fetched,
            fetch=fetch_fn,
            trace=trace,
            deadline=enrichment_deadline,
        )
        if result.extraction_status == "ok":
            _cache_put(int(tenant_id or 0), original, result)
        _trace_call(trace, "mark_enrichment_result", result=result)
        logger.info(
            "[url_context] tenant=%s status=%s source=%s err=%s url=%s",
            tenant_id,
            result.extraction_status,
            result.source,
            result.error_class or "-",
            redact_url_for_log(original),
        )
        return [result]
    except Exception as exc:  # noqa: BLE001
        _trace_call(trace, "record_failure", stage="enrichment", exception=exc)
        logger.warning(
            "[url_context] enrich failed tenant=%s url=%s err=%s",
            tenant_id,
            redact_url_for_log(original),
            type(exc).__name__,
        )
        result = _unavailable(original, "enrich_exception")
        _trace_call(trace, "mark_enrichment_result", result=result)
        return [result]


def current_turn_has_url(message: str) -> bool:
    return bool(extract_inbound_url_spans(message or ""))


__all__ = [
    "UrlContext",
    "current_turn_has_url",
    "enrich_current_turn_urls",
    "lookup_catalog_product_by_url",
    "parse_html_metadata",
    "reset_url_context_cache",
    "begin_url_context_turn",
    "select_current_turn_urls",
]
