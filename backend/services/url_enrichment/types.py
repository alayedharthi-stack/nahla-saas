"""Shared types for the universal URL enrichment pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EnrichmentDraft:
    page_title: str = ""
    safe_description: str = ""
    author_or_channel: str = ""
    content_kind: str = ""
    published_at: str = ""
    canonical_url: str = ""
    canonical_domain: str = ""
    preview_image: str = ""
    readable_excerpt: str = ""
    enrichment_source: str = ""
    metadata_quality: str = "empty"
    useful_metadata_present: bool = False
    sources_used: List[str] = field(default_factory=list)


@dataclass
class PipelineTraceState:
    metadata_sources_attempted: List[str] = field(default_factory=list)
    metadata_source_selected: str = ""
    standard_metadata_status: str = "not_run"
    structured_data_status: str = "not_run"
    oembed_discovery_status: str = "not_run"
    provider_adapter: str = ""
    provider_enrichment_status: str = "not_run"
    readable_text_status: str = "not_run"
    metadata_quality: str = "empty"
    useful_metadata_present: bool = False
    external_fetch_count: int = 1

    def record_attempt(self, source: str) -> None:
        if source and source not in self.metadata_sources_attempted:
            self.metadata_sources_attempted.append(source)

    def to_trace_fields(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "metadata_quality": self.metadata_quality,
            "useful_metadata_present": bool(self.useful_metadata_present),
            "external_fetch_count": max(1, int(self.external_fetch_count or 1)),
        }
        if self.metadata_sources_attempted:
            out["metadata_sources_attempted"] = ",".join(self.metadata_sources_attempted)
        if self.metadata_source_selected:
            out["metadata_source_selected"] = self.metadata_source_selected
        if self.standard_metadata_status != "not_run":
            out["standard_metadata_status"] = self.standard_metadata_status
        if self.structured_data_status != "not_run":
            out["structured_data_status"] = self.structured_data_status
        if self.oembed_discovery_status != "not_run":
            out["oembed_discovery_status"] = self.oembed_discovery_status
        if self.provider_adapter:
            out["provider_adapter"] = self.provider_adapter
        if self.provider_enrichment_status != "not_run":
            out["provider_enrichment_status"] = self.provider_enrichment_status
        if self.readable_text_status != "not_run":
            out["readable_text_status"] = self.readable_text_status
        return out
