"""
backend/tests/test_media_url_canonicalisation.py
────────────────────────────────────────────────
Regression suite for the platform-wide media URL hygiene applied at
send-time inside ``backend/core/ai_libraries.py``.

Two failure modes were observed in production traffic:

  1. **Plain HTTP**: ``request.base_url`` returned an ``http://`` URL
     because Railway terminates TLS upstream and the inner app sees
     plain HTTP. ``AIMediaItem.file_url`` was persisted with that
     scheme and Meta's WhatsApp Cloud API silently rejects non-HTTPS
     media fetches. ``_maybe_force_https`` already addresses this for
     a known set of managed platforms (railway/heroku/vercel/fly/
     render).

  2. **Raw managed-platform host**: even after the HTTPS upgrade, the
     URL still pointed at the platform's preview hostname (e.g.
     ``nahla-saas-production.up.railway.app``). That hostname:
       - leaks infra to merchants/customers,
       - changes when the service is renamed,
       - is not the canonical public-facing endpoint
         (``api.nahlah.ai``).

This is fixed by ``_canonicalise_managed_host`` (May 2026 #35), which
swaps the scheme + host[:port] portion onto the value declared in
``NAHLA_PUBLIC_BASE_URL`` while preserving the path/query exactly.
The behaviour is platform-wide (every tenant's old media row gets
auto-corrected at send time) and gated by the env var so local dev
is untouched.

Tests below pin:

  * the helper is a no-op when the env var is missing,
  * the helper is a no-op for already-canonical URLs,
  * the helper rewrites Railway / Heroku / Vercel / Fly / Render
    preview domains onto the configured public base,
  * the helper preserves the URL path, query string, and fragment,
  * the helper survives malformed input without raising,
  * ``validate_media_for_send`` chains scheme-upgrade + canonicalise
    in the right order so a plain-HTTP Railway URL ends up on the
    canonical host with HTTPS in a single pass.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ── _canonicalise_managed_host — the pure helper ───────────────────────────


def test_canonicalise_noop_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``NAHLA_PUBLIC_BASE_URL`` the helper is a strict
    pass-through. Local dev stays on whatever URL was uploaded."""
    monkeypatch.delenv("NAHLA_PUBLIC_BASE_URL", raising=False)
    from core.ai_libraries import _canonicalise_managed_host
    url = "http://nahla-saas-production.up.railway.app/intelligence/ai-media/file/1"
    assert _canonicalise_managed_host(url) == url


def test_canonicalise_noop_for_canonical_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the URL is already on the canonical public base, no
    rewrite happens. Idempotent under repeated calls."""
    monkeypatch.setenv("NAHLA_PUBLIC_BASE_URL", "https://api.nahlah.ai")
    from core.ai_libraries import _canonicalise_managed_host
    url = "https://api.nahlah.ai/intelligence/ai-media/file/42"
    assert _canonicalise_managed_host(url) == url
    # Idempotent.
    assert _canonicalise_managed_host(_canonicalise_managed_host(url)) == url


def test_canonicalise_rewrites_railway_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Tenant 33 production case generalised: any Railway preview
    host gets swapped onto the canonical base while the path is
    preserved exactly so the file id survives."""
    monkeypatch.setenv("NAHLA_PUBLIC_BASE_URL", "https://api.nahlah.ai")
    from core.ai_libraries import _canonicalise_managed_host
    out = _canonicalise_managed_host(
        "https://nahla-saas-production.up.railway.app/intelligence/ai-media/file/1"
    )
    assert out == "https://api.nahlah.ai/intelligence/ai-media/file/1"


@pytest.mark.parametrize("host", [
    "myapp.herokuapp.com",
    "myapp.vercel.app",
    "myapp.fly.dev",
    "myapp.onrender.com",  # render's preview subdomain
])
def test_canonicalise_rewrites_all_managed_hosts(
    host: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All five managed-host families recognised by ``_PROD_HTTPS_HOSTS``
    must be rewritten — not just Railway. This makes the fix
    platform-wide regardless of where the merchant's nahla deployment
    is hosted."""
    monkeypatch.setenv("NAHLA_PUBLIC_BASE_URL", "https://api.nahlah.ai")
    from core.ai_libraries import _canonicalise_managed_host
    out = _canonicalise_managed_host(f"https://{host}/intelligence/ai-media/file/7")
    assert out == "https://api.nahlah.ai/intelligence/ai-media/file/7"


def test_canonicalise_preserves_query_and_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query strings (``?token=…``) and fragments (``#frag``) often
    carry signed-URL params or scroll anchors. The rewrite must leave
    them untouched."""
    monkeypatch.setenv("NAHLA_PUBLIC_BASE_URL", "https://api.nahlah.ai")
    from core.ai_libraries import _canonicalise_managed_host
    out = _canonicalise_managed_host(
        "https://x.up.railway.app/path/to/file?token=abc&v=2#section",
    )
    assert out == "https://api.nahlah.ai/path/to/file?token=abc&v=2#section"


def test_canonicalise_promotes_bare_base_to_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators sometimes set ``NAHLA_PUBLIC_BASE_URL=api.nahlah.ai``
    without a scheme. The helper must default to https:// so we never
    emit a downgraded URL."""
    monkeypatch.setenv("NAHLA_PUBLIC_BASE_URL", "api.nahlah.ai")
    from core.ai_libraries import _canonicalise_managed_host
    out = _canonicalise_managed_host(
        "https://x.up.railway.app/intelligence/ai-media/file/9",
    )
    assert out.startswith("https://api.nahlah.ai/")


def test_canonicalise_strips_trailing_slash_on_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NAHLA_PUBLIC_BASE_URL`` declared with a trailing slash must
    not produce a doubled slash in the rewritten URL."""
    monkeypatch.setenv("NAHLA_PUBLIC_BASE_URL", "https://api.nahlah.ai/")
    from core.ai_libraries import _canonicalise_managed_host
    out = _canonicalise_managed_host(
        "https://x.up.railway.app/intelligence/ai-media/file/9",
    )
    assert "//intelligence" not in out  # would mean double slash
    assert out == "https://api.nahlah.ai/intelligence/ai-media/file/9"


def test_canonicalise_noop_for_unrelated_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merchant-supplied URL that points at their OWN domain (e.g.
    a custom-domain product image hosted on ``ayedhoney.com``) must
    NOT be rewritten — only managed-platform hosts are in scope.
    Otherwise we'd break legitimate external-asset references."""
    monkeypatch.setenv("NAHLA_PUBLIC_BASE_URL", "https://api.nahlah.ai")
    from core.ai_libraries import _canonicalise_managed_host
    url = "https://ayedhoney.com/wp-content/uploads/2025/honey.jpg"
    assert _canonicalise_managed_host(url) == url


def test_canonicalise_safe_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty / None / malformed input must NEVER raise — a URL
    parsing hiccup can never crash a customer reply."""
    monkeypatch.setenv("NAHLA_PUBLIC_BASE_URL", "https://api.nahlah.ai")
    from core.ai_libraries import _canonicalise_managed_host
    assert _canonicalise_managed_host("") == ""
    assert _canonicalise_managed_host(None) is None  # type: ignore[arg-type]
    # Not a URL — no host at all. Helper must just return it.
    assert _canonicalise_managed_host("not a url") == "not a url"


# ── validate_media_for_send chains scheme-upgrade + canonicalise ───────────


class _FakeAIMediaRow:
    """Minimal stand-in for the SQLAlchemy ``AIMediaItem`` row that
    ``validate_media_for_send`` re-fetches when ``db`` is provided."""
    def __init__(
        self, *, id: int, tenant_id: int, file_url: str, is_active: bool = True,
    ) -> None:
        self.id = id
        self.tenant_id = tenant_id
        self.file_url = file_url
        self.is_active = is_active
        self.storage_kind = "external"
        self.storage_path = None
        self.mime_type = "image/jpeg"
        self.file_size_bytes = None


class _FakeQuery:
    def __init__(self, row: _FakeAIMediaRow) -> None:
        self._row = row

    def filter(self, *args: Any, **kwargs: Any) -> "_FakeQuery":
        return self

    def first(self) -> _FakeAIMediaRow:
        return self._row


class _FakeDB:
    def __init__(self, row: _FakeAIMediaRow) -> None:
        self._row = row

    def query(self, _model: Any) -> _FakeQuery:
        return _FakeQuery(self._row)


def test_validate_chain_upgrades_then_canonicalises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The most important platform-wide test: a row persisted with
    ``http://nahla-saas-production.up.railway.app/intelligence/ai-media/file/1``
    must come out the validator as
    ``https://api.nahlah.ai/intelligence/ai-media/file/1`` in a SINGLE
    validate call — both fixes applied together. This is the exact
    Tenant 33 Rajhi-barcode shape but assertions only check the URL
    transformation, no tenant-specific data.
    """
    monkeypatch.setenv("NAHLA_PUBLIC_BASE_URL", "https://api.nahlah.ai")
    from core.ai_libraries import validate_media_for_send

    row = _FakeAIMediaRow(
        id=1,
        tenant_id=33,
        file_url="http://nahla-saas-production.up.railway.app/intelligence/ai-media/file/1",
    )
    db = _FakeDB(row)

    ok, err, item = validate_media_for_send(
        {"id": 1, "media_type": "image", "tenant_id": 33},
        expected_tenant_id=33,
        db=db,
    )
    assert ok, f"validate returned err={err}"
    assert item is not None
    assert item["file_url"] == (
        "https://api.nahlah.ai/intelligence/ai-media/file/1"
    ), f"got {item['file_url']!r}"


def test_validate_canonicalise_skipped_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``NAHLA_PUBLIC_BASE_URL`` is unset (local dev / staging
    without the var), the validator must still run the HTTPS upgrade
    but leave the host alone. No surprise rewriting based on a missing
    env var."""
    monkeypatch.delenv("NAHLA_PUBLIC_BASE_URL", raising=False)
    from core.ai_libraries import validate_media_for_send

    row = _FakeAIMediaRow(
        id=2,
        tenant_id=33,
        file_url="http://nahla-saas-production.up.railway.app/intelligence/ai-media/file/2",
    )
    db = _FakeDB(row)

    ok, err, item = validate_media_for_send(
        {"id": 2, "media_type": "image", "tenant_id": 33},
        expected_tenant_id=33,
        db=db,
    )
    assert ok, f"validate returned err={err}"
    assert item is not None
    # HTTPS upgrade applied (Railway is in _PROD_HTTPS_HOSTS).
    assert item["file_url"].startswith("https://")
    # But host is untouched because the canonicaliser is opt-in.
    assert "railway.app" in item["file_url"]
