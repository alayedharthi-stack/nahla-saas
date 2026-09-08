"""Universal URL enrichment orchestration."""
from __future__ import annotations

from typing import Awaitable, Callable, Optional, Tuple

from backend.services.safe_http_fetch import SafeHttpResult

from .oembed import fetch_oembed
from .quality import assess_draft_quality, merge_preferring_richer
from .providers.base import ProviderAdapterRequest
from .providers.registry import resolve_provider_adapter
from .readable_text import extract_readable_excerpt
from .standard_metadata import has_useful_standard_metadata, parse_html_metadata
from .structured_data import extract_json_ld_metadata
from .text import provider_domain
from .types import EnrichmentDraft, PipelineTraceState
from .url_safety import same_origin, sanitize_oembed_target_url

FetchFn = Callable[..., Awaitable[SafeHttpResult]]
MAX_EXTERNAL_FETCHES = 2


def _adapter_request(
    *,
    final_url: str,
    draft: EnrichmentDraft,
) -> ProviderAdapterRequest:
    return ProviderAdapterRequest(
        final_url=final_url,
        canonical_url=draft.canonical_url or final_url,
        canonical_domain=draft.canonical_domain,
        page_title=draft.page_title,
        safe_description=draft.safe_description,
        author_or_channel=draft.author_or_channel,
        metadata_quality=draft.metadata_quality,
    )


async def run_enrichment_pipeline(
    original_url: str,
    fetched: SafeHttpResult,
    *,
    fetch: FetchFn,
    deadline: Optional[float] = None,
) -> Tuple[EnrichmentDraft, PipelineTraceState]:
    trace = PipelineTraceState(external_fetch_count=1)
    draft = EnrichmentDraft()
    final_url = fetched.final_url or original_url
    html = fetched.body.decode("utf-8", errors="ignore") if fetched.body else ""

    if not fetched.ok:
        trace.metadata_quality = "empty"
        trace.useful_metadata_present = False
        return draft, trace

    # 1) Standard HTML metadata
    trace.record_attempt("html_metadata")
    standard, declared_oembed = parse_html_metadata(html, final_url)
    merge_preferring_richer(
        draft,
        page_title=standard.page_title,
        safe_description=standard.safe_description,
        author_or_channel=standard.author_or_channel,
        content_kind=standard.content_kind,
        published_at=standard.published_at,
        canonical_url=standard.canonical_url or final_url,
        preview_image=standard.preview_image,
        source="html_metadata",
    )
    if not draft.canonical_domain:
        draft.canonical_domain = provider_domain(final_url)
    trace.standard_metadata_status = "ok" if has_useful_standard_metadata(draft) else "thin"

    # 2) Structured data
    trace.record_attempt("json_ld")
    structured = extract_json_ld_metadata(html, final_url)
    if structured.sources_used:
        merge_preferring_richer(
            draft,
            page_title=structured.page_title,
            safe_description=structured.safe_description,
            author_or_channel=structured.author_or_channel,
            content_kind=structured.content_kind,
            published_at=structured.published_at,
            preview_image=structured.preview_image,
            source="json_ld",
        )
        trace.structured_data_status = "ok"
    else:
        trace.structured_data_status = "empty"

    quality, useful = assess_draft_quality(draft)
    draft.metadata_quality = quality
    draft.useful_metadata_present = useful

    # 3) oEmbed discovery or provider adapter (single extra fetch max)
    oembed_endpoint: Optional[str] = None
    trace.oembed_discovery_status = "none"
    if declared_oembed:
        if same_origin(declared_oembed, final_url) or same_origin(declared_oembed, draft.canonical_url):
            oembed_endpoint = declared_oembed
            trace.oembed_discovery_status = "declared"
        else:
            trace.oembed_discovery_status = "cross_origin_blocked"

    if not useful and trace.external_fetch_count < MAX_EXTERNAL_FETCHES:
        adapter = None
        if not oembed_endpoint:
            adapter = resolve_provider_adapter(_adapter_request(final_url=final_url, draft=draft))
            if adapter:
                trace.provider_adapter = adapter.name
                oembed_endpoint = adapter.oembed_endpoint(_adapter_request(final_url=final_url, draft=draft))
                trace.oembed_discovery_status = "adapter"

        if oembed_endpoint:
            trace.record_attempt("oembed")
            trace.external_fetch_count += 1
            safe_target = sanitize_oembed_target_url(
                final_url=final_url,
                canonical_url=draft.canonical_url or final_url,
            )
            oembed_draft, oembed_status = await fetch_oembed(
                page_url=safe_target or final_url,
                endpoint=oembed_endpoint,
                fetch=fetch,
                deadline=deadline,
            )
            trace.provider_enrichment_status = oembed_status
            if oembed_status == "ok":
                merge_preferring_richer(
                    draft,
                    page_title=oembed_draft.page_title,
                    safe_description=oembed_draft.safe_description,
                    author_or_channel=oembed_draft.author_or_channel,
                    preview_image=oembed_draft.preview_image,
                    source="oembed",
                )
        elif adapter:
            trace.provider_enrichment_status = "skipped"
        else:
            trace.provider_enrichment_status = "not_needed"

    quality, useful = assess_draft_quality(draft)
    draft.metadata_quality = quality
    draft.useful_metadata_present = useful

    # 4) Readable text fallback (no extra fetch)
    if not useful:
        trace.record_attempt("readable_text")
        excerpt = extract_readable_excerpt(html)
        if excerpt:
            merge_preferring_richer(
                draft,
                readable_excerpt=excerpt,
                source="readable_text",
            )
            trace.readable_text_status = "ok"
        else:
            trace.readable_text_status = "empty"
        quality, useful = assess_draft_quality(draft)
        draft.metadata_quality = quality
        draft.useful_metadata_present = useful

    if draft.sources_used:
        trace.metadata_source_selected = draft.sources_used[-1]
    trace.metadata_quality = draft.metadata_quality
    trace.useful_metadata_present = draft.useful_metadata_present
    return draft, trace
