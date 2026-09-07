"""Current-turn URL enrichment for MerchantBrain.

Produces provenance-labelled URL_CONTEXT facts. Does not infer purchase
or checkout intent from URL presence. No domain special-case in routing.
TikTok/oEmbed HTML is parsed by the same generic metadata extractor.
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
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import unquote, urlparse

from core.inbound_url_spans import (
    extract_inbound_url_spans,
)
from observability.rate_limiter import check_rate_limit
from services.safe_http_fetch import (
    SafeHttpResult,
    fetch_url_async,
    redact_url_for_log,
)

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

    def to_public_dict(self) -> Dict[str, Any]:
        payload = {
            "original_url": self.original_url,
            "canonical_url": self.canonical_url or self.original_url,
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
            "fetch_body_truncated": bool(self.fetch_body_truncated),
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


class _MetaHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self.title_parts: List[str] = []
        self.metas: Dict[str, str] = {}
        self.canonical = ""

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        ad = {str(k or "").lower(): str(v or "") for k, v in attrs}
        low = tag.lower()
        if low == "title":
            self._in_title = True
            return
        if low == "meta":
            key = (ad.get("property") or ad.get("name") or ad.get("itemprop") or "").lower()
            content = ad.get("content") or ""
            if key and content and key not in self.metas:
                self.metas[key] = content
            return
        if low == "link" and "canonical" in (ad.get("rel") or "").lower():
            href = ad.get("href") or ""
            if href and not self.canonical:
                self.canonical = href

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def parse_html_metadata(html: str, *, base_url: str) -> Dict[str, str]:
    parser = _MetaHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.exception("[url_context] html metadata parse failed")
    title = "".join(parser.title_parts)
    og_title = parser.metas.get("og:title") or parser.metas.get("twitter:title") or ""
    og_desc = (
        parser.metas.get("og:description")
        or parser.metas.get("twitter:description")
        or parser.metas.get("description")
        or ""
    )
    og_image = parser.metas.get("og:image") or parser.metas.get("twitter:image") or ""
    og_url = parser.metas.get("og:url") or parser.canonical or ""
    author = (
        parser.metas.get("og:site_name")
        or parser.metas.get("author")
        or parser.metas.get("article:author")
        or ""
    )
    return {
        "title": og_title or title,
        "description": og_desc,
        "image": _absolute_url(base_url, og_image)[:IMAGE_URL_MAX],
        "canonical": _absolute_url(base_url, og_url) or og_url,
        "author": author,
        "html_title": title,
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
        "description": str(data.get("html") or data.get("description") or "")[:DESCRIPTION_MAX],
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


def _from_fetch(original: str, fetched: SafeHttpResult) -> UrlContext:
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
    title = _sanitize_text(meta.get("title") or meta.get("html_title"), TITLE_MAX)
    description = _sanitize_text(meta.get("description"), DESCRIPTION_MAX)
    author = _sanitize_text(meta.get("author"), AUTHOR_MAX)
    image = _sanitize_text(meta.get("image"), IMAGE_URL_MAX)
    canonical = _sanitize_text(meta.get("canonical") or fetched.final_url or original, 500)
    if not title and not description and not author:
        if truncated:
            return _unavailable(original, "oversized", source=source)
        return _unavailable(original, "metadata_missing", source=source)
    confidence = 0.8 if title else 0.55
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
        fetch_body_truncated=truncated,
    )


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


async def enrich_current_turn_urls(
    *,
    message: str,
    tenant_id: int = 0,
    db: Any = None,
    fetch: Optional[FetchFn] = None,
    catalog_lookup: Optional[CatalogLookup] = None,
) -> List[UrlContext]:
    """Enrich at most one distinct current-turn URL. Never raises."""
    urls = select_current_turn_urls(message)
    if not urls:
        return []
    original = urls[0]
    try:
        cached = _cache_get(int(tenant_id or 0), original)
        if cached is not None:
            return [cached]
        lookup = catalog_lookup
        if lookup is None and db is not None:
            lookup = lambda tid, url: lookup_catalog_product_by_url(db, tid, url)  # noqa: E731
        if lookup is not None:
            product = lookup(int(tenant_id or 0), original)
            if product:
                result = _from_catalog(original, product)
                _cache_put(int(tenant_id or 0), original, result)
                return [result]
        rate_key = f"url_context:{int(tenant_id or 0)}"
        if not check_rate_limit(rate_key, max_count=RATE_LIMIT_MAX, window_seconds=RATE_LIMIT_WINDOW_S):
            result = _unavailable(original, "rate_limited", source="rate_limit")
            return [result]
        fetches = getattr(_turn_fetches, "urls", None)
        if fetches is None:
            fetches = set()
            _turn_fetches.urls = fetches
        fetch_marker = f"{int(tenant_id or 0)}|{_normalize_url_for_compare(original)}"
        if fetch_marker in fetches:
            return [_unavailable(original, "duplicate_turn_fetch", source="cache")]
        fetches.add(fetch_marker)
        if fetch is None:
            fetched = await fetch_url_async(original)
        else:
            got = fetch(original)
            fetched = await got if asyncio.iscoroutine(got) else got
        result = _from_fetch(original, fetched)
        if result.extraction_status == "ok":
            _cache_put(int(tenant_id or 0), original, result)
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
        logger.warning(
            "[url_context] enrich failed tenant=%s url=%s err=%s",
            tenant_id,
            redact_url_for_log(original),
            type(exc).__name__,
        )
        return [_unavailable(original, "enrich_exception")]


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
