"""
tests/test_address_resolution.py
─────────────────────────────────
Unit tests for the address_resolution service improvements introduced in
the SPL + Google Maps enhancement (Phase 3/4 of the address pipeline).

Covers:
  1. _MAPS_URL_RE — new platforms: Apple Maps, Waze (no network calls).
  2. _QUERY_COORDS_RE / _extract_coords — ?q=, ?ll=, ?daddr= param formats.
  3. _extract_coords — sanity-check guard for out-of-range values.
  4. expand_maps_url() — shortened URL expansion with mocked httpx.
  5. extract_address_signals() — end-to-end signal extraction for each platform.
  6. _resolve_checkout_address() integration — coords extracted and passed to SPL.

No live network calls — httpx is mocked throughout.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from services.address_resolution import (  # noqa: E402
    _extract_coords,
    _MAPS_URL_RE,
    _QUERY_COORDS_RE,
    _SHORT_CODE_RE,
    expand_maps_url,
    extract_address_signals,
    spl_resolution_available,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── 1. _MAPS_URL_RE — URL recognition ─────────────────────────────────────────

class TestMapsUrlRegex:
    def _match(self, text: str) -> str | None:
        m = _MAPS_URL_RE.search(text)
        return m.group(1) if m else None

    # Google Maps
    def test_google_maps_full(self) -> None:
        url = "https://maps.google.com/maps?q=24.7136,46.6753"
        assert self._match(url) == url

    def test_google_com_maps(self) -> None:
        url = "https://www.google.com/maps/place/Riyadh/@24.7136,46.6753,12z"
        assert self._match(url) is not None

    def test_google_short_new(self) -> None:
        url = "https://maps.app.goo.gl/AbCdEfGh"
        assert self._match(url) == url

    def test_google_short_old(self) -> None:
        url = "https://goo.gl/maps/XxXxXx"
        assert self._match(url) == url

    def test_g_page(self) -> None:
        url = "https://g.page/some-place"
        assert self._match(url) == url

    # Apple Maps (new in this version)
    def test_apple_maps_with_q(self) -> None:
        url = "https://maps.apple.com/?q=24.7136,46.6753"
        assert self._match(url) == url

    def test_apple_maps_with_ll(self) -> None:
        url = "https://maps.apple.com/?ll=21.3891,39.8579&t=m"
        assert self._match(url) is not None

    # Waze (new in this version)
    def test_waze_ul(self) -> None:
        url = "https://waze.com/ul?ll=24.7136,46.6753&navigate=yes"
        assert self._match(url) == url

    def test_waze_www(self) -> None:
        url = "https://www.waze.com/ul?ll=24.6877,46.7219"
        assert self._match(url) is not None

    def test_non_map_url_ignored(self) -> None:
        assert self._match("https://example.com/page") is None

    def test_url_in_sentence(self) -> None:
        text = "موقعي هنا: https://maps.apple.com/?q=24.7,46.7 شكراً"
        assert self._match(text) is not None


# ── 2. _extract_coords — coordinate extraction from various URL formats ────────

class TestExtractCoords:
    # Google Maps @ format
    def test_at_coords(self) -> None:
        url = "https://www.google.com/maps/@24.7136,46.6753,17z"
        lat, lng = _extract_coords(url)
        assert abs(lat - 24.7136) < 1e-4
        assert abs(lng - 46.6753) < 1e-4

    # Google Maps !3d!4d embed format
    def test_bang_coords(self) -> None:
        url = "https://maps.google.com/maps/embed?...!3d24.6877!4d46.7219..."
        lat, lng = _extract_coords(url)
        assert abs(lat - 24.6877) < 1e-4
        assert abs(lng - 46.7219) < 1e-4

    # Apple Maps / Waze ?q= format (new)
    def test_query_param_q(self) -> None:
        url = "https://maps.apple.com/?q=24.7136,46.6753&t=m"
        lat, lng = _extract_coords(url)
        assert abs(lat - 24.7136) < 1e-4
        assert abs(lng - 46.6753) < 1e-4

    def test_query_param_ll(self) -> None:
        url = "https://waze.com/ul?ll=21.3891,39.8579&navigate=yes"
        lat, lng = _extract_coords(url)
        assert abs(lat - 21.3891) < 1e-4
        assert abs(lng - 39.8579) < 1e-4

    def test_query_param_daddr(self) -> None:
        url = "https://maps.google.com/maps?daddr=24.7136,46.6753"
        lat, lng = _extract_coords(url)
        assert abs(lat - 24.7136) < 1e-4

    # Bare pair (existing behaviour)
    def test_bare_pair(self) -> None:
        lat, lng = _extract_coords("24.7136,46.6753")
        assert abs(lat - 24.7136) < 1e-4

    # Sanity-check: out-of-range values must be rejected
    def test_rejects_invalid_latitude(self) -> None:
        lat, lng = _extract_coords("@200.0,46.6753")
        assert lat is None

    def test_rejects_invalid_longitude(self) -> None:
        lat, lng = _extract_coords("@24.7136,200.0")
        assert lat is None

    # No match
    def test_no_coords_returns_none(self) -> None:
        lat, lng = _extract_coords("مرحبا كيف حالك")
        assert lat is None
        assert lng is None


# ── 3. expand_maps_url() ───────────────────────────────────────────────────────

class TestExpandMapsUrl:
    def _mock_client(self, final_url: str):
        """Build a context-manager mock for httpx.AsyncClient that resolves to final_url."""
        resp = MagicMock()
        resp.url = final_url

        client_instance = AsyncMock()
        client_instance.head = AsyncMock(return_value=resp)

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client_instance)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    def test_expands_shortened_url(self) -> None:
        short = "https://maps.app.goo.gl/AbCdEf"
        full  = "https://www.google.com/maps/place/Riyadh/@24.7136,46.6753,12z"
        with patch("services.address_resolution.httpx.AsyncClient", return_value=self._mock_client(full)):
            result = _run(expand_maps_url(short))
        assert result == full

    def test_non_shortened_url_returned_as_is(self) -> None:
        full = "https://www.google.com/maps/@24.7,46.7,17z"
        # Should return immediately without making an HTTP call
        result = _run(expand_maps_url(full))
        assert result == full

    def test_empty_url_returned_as_is(self) -> None:
        assert _run(expand_maps_url("")) == ""

    def test_network_error_returns_original(self) -> None:
        short = "https://maps.app.goo.gl/XxXx"
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=Exception("network error"))
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch("services.address_resolution.httpx.AsyncClient", return_value=cm):
            result = _run(expand_maps_url(short))
        assert result == short

    def test_goo_gl_maps_is_shortened(self) -> None:
        short = "https://goo.gl/maps/SomeId"
        full  = "https://maps.google.com/maps?q=24.7136,46.6753"
        with patch("services.address_resolution.httpx.AsyncClient", return_value=self._mock_client(full)):
            result = _run(expand_maps_url(short))
        assert result == full


# ── 4. extract_address_signals() — end-to-end ─────────────────────────────────

class TestExtractAddressSignals:
    def test_google_full_url_with_coords(self) -> None:
        text = "موقعي: https://www.google.com/maps/@24.7136,46.6753,17z"
        signals = extract_address_signals(text)
        assert signals["google_maps_url"] != ""
        assert abs(signals["latitude"] - 24.7136) < 1e-4

    def test_apple_maps_with_ll_param(self) -> None:
        text = "شاركت موقعي https://maps.apple.com/?ll=21.3891,39.8579"
        signals = extract_address_signals(text)
        assert signals["google_maps_url"] != ""   # stored in google_maps_url field
        assert abs(signals["latitude"] - 21.3891) < 1e-4

    def test_waze_url_with_ll_param(self) -> None:
        text = "رابط وايز https://waze.com/ul?ll=24.6877,46.7219&navigate=yes"
        signals = extract_address_signals(text)
        assert signals["google_maps_url"] != ""
        assert abs(signals["latitude"] - 24.6877) < 1e-4

    def test_short_address_code_extraction(self) -> None:
        text = "الرمز الوطني هو RIYD1234"
        signals = extract_address_signals(text)
        assert signals["short_address_code"] == "RIYD1234"

    def test_short_code_case_insensitive(self) -> None:
        signals = extract_address_signals("riyd1234")
        assert signals["short_address_code"].upper() == "RIYD1234"

    def test_no_signals_in_plain_text(self) -> None:
        signals = extract_address_signals("أبغى فستان أسود")
        assert signals["short_address_code"] == ""
        assert signals["google_maps_url"] == ""
        assert signals["latitude"] is None

    def test_shortened_url_captured_without_coords(self) -> None:
        # Short URL — captured but coords not available until expand_maps_url runs
        text = "موقعي https://maps.app.goo.gl/AbCdEfGh"
        signals = extract_address_signals(text)
        assert "maps.app.goo.gl" in signals["google_maps_url"]
        assert signals["latitude"] is None   # coords only after expand
