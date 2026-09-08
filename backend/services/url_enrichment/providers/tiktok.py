"""TikTok provider adapter (first registry entry)."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urlparse

from .base import ProviderAdapterRequest

_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
_VIDEO_PATH = re.compile(r"/@[^/]+/video/\d+", re.I)
_SHORT_PATH = re.compile(r"/t/[A-Za-z0-9]+", re.I)


class TikTokProviderAdapter:
    name = "tiktok"

    def matches(self, request: ProviderAdapterRequest) -> bool:
        host = (request.canonical_domain or "").lower()
        if host not in _TIKTOK_HOSTS and not host.endswith(".tiktok.com"):
            return False
        if request.metadata_quality != "thin":
            return False
        return self._is_video_url(request.final_url)

    def oembed_endpoint(self, request: ProviderAdapterRequest) -> Optional[str]:
        if not self.matches(request):
            return None
        return f"https://www.tiktok.com/oembed?url={quote(request.final_url, safe='')}"

    @staticmethod
    def _is_video_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        path = parsed.path or ""
        return bool(_VIDEO_PATH.search(path) or _SHORT_PATH.search(path))
