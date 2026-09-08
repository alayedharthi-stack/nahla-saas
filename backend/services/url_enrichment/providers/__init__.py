"""Provider adapter registry for URL enrichment."""
from __future__ import annotations

from typing import List, Optional

from .base import ProviderAdapter, ProviderAdapterRequest
from .tiktok import TikTokProviderAdapter

_ADAPTERS: List[ProviderAdapter] = [
    TikTokProviderAdapter(),
]


def resolve_provider_adapter(request: ProviderAdapterRequest) -> Optional[ProviderAdapter]:
    for adapter in _ADAPTERS:
        if adapter.matches(request):
            return adapter
    return None
