"""Provider adapter base types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class ProviderAdapterRequest:
    final_url: str
    canonical_url: str
    canonical_domain: str
    page_title: str
    safe_description: str
    author_or_channel: str
    metadata_quality: str


class ProviderAdapter(Protocol):
    name: str

    def matches(self, request: ProviderAdapterRequest) -> bool:
        ...

    def oembed_endpoint(self, request: ProviderAdapterRequest) -> Optional[str]:
        ...
