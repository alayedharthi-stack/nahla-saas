"""
tests/test_customer_display.py
──────────────────────────────
Contract tests for ``core/customer_display.py`` after the **May 2026
policy change** that retired runtime name sanitisation.

Why the contract changed
────────────────────────
Previously this module's ``display_customer_name_or_fallback`` was
called at every greeting site (campaigns, automation templates, the
AI prompt builder) to strip commercial filler ("عميل", "customer",
"زبون" …) from ``Customer.name`` before it hit a ``{{1}}`` slot.

That created a two-layer cleanup conflict with the new bulk admin
tool ("تنظيف أسماء العملاء" on the customers page). Decision:

  * Layer 1 (runtime sanitiser) is **disabled**.
  * Layer 2 (bulk admin tool) is the **sole** source of truth.

What we lock down now
─────────────────────
1. **Passthrough helper** — ``display_name_passthrough_or_fallback``
   never mutates a non-empty input. It only swaps the static
   fallback in when the value is ``None`` / empty / whitespace.
2. **Back-compat aliases** — ``display_customer_name_or_fallback`` and
   ``display_customer_name`` both resolve to the passthrough helper
   so older imports keep working with the new semantics.
3. **Bulk-tool sanitiser preserved** — ``sanitize_display_customer_name``
   is still exported (the admin cleanup tool uses an equivalent copy
   in ``services/customer_name_cleanup.py``); these tests document
   what the legacy helper still returns for callers that depend on it.
4. **Defensive contract** — neither helper raises on ``None`` /
   non-string / weird payloads.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.customer_display import (  # noqa: E402
    DEFAULT_FALLBACK_NAME,
    display_customer_name,
    display_customer_name_or_fallback,
    display_name_passthrough_or_fallback,
    sanitize_display_customer_name,
)


# ── 1) Passthrough helper — the new runtime contract ──────────────────


class TestPassthroughHelper:
    """``display_name_passthrough_or_fallback`` is the runtime helper
    used by campaigns / automations / AI prompts after May 2026.

    Its contract is intentionally tiny: return the stored value
    verbatim if usable, otherwise the static fallback. Anything
    fancier (stopword stripping, phone-only detection) belongs to
    the bulk admin tool, which mutates the DB row once."""

    @pytest.mark.parametrize("raw, expected", [
        # Names already clean — passthrough.
        ("أبو خالد",       "أبو خالد"),
        ("Ahmed",          "Ahmed"),
        ("عبد الرحمن",     "عبد الرحمن"),
        # Whitespace stripped, but content preserved.
        ("  محمد  ",       "محمد"),
        # ⚠ Names that look "dirty" (still contain "عميل" / "customer")
        # are NOT cleaned at runtime — the merchant is expected to
        # have run the bulk cleanup tool on the customers page. Until
        # then we pass the raw value through unchanged.
        ("أنهار زبون",     "أنهار زبون"),
        ("محمد عميل",      "محمد عميل"),
        ("customer Ahmed", "customer Ahmed"),
    ])
    def test_passthrough_keeps_value(self, raw, expected):
        assert display_name_passthrough_or_fallback(raw) == expected

    @pytest.mark.parametrize("raw", [
        None,
        "",
        "   ",
        "\t\n",
    ])
    def test_empty_inputs_use_fallback(self, raw):
        out = display_name_passthrough_or_fallback(raw)
        assert out == DEFAULT_FALLBACK_NAME
        # The May 2026 warmer wording, locked-in.
        assert "الغالي" in out

    @pytest.mark.parametrize("raw", [42, 3.14, [], {}, object()])
    def test_non_string_uses_fallback(self, raw):
        assert display_name_passthrough_or_fallback(raw) == DEFAULT_FALLBACK_NAME

    def test_custom_fallback_honoured(self):
        out = display_name_passthrough_or_fallback("", fallback="صديقي")
        assert out == "صديقي"


# ── 2) Back-compat aliases ────────────────────────────────────────────


class TestBackCompatAliases:
    """Older code paths import ``display_customer_name_or_fallback``
    or ``display_customer_name``. They must keep working but with
    the new passthrough behaviour — NOT the old sanitiser."""

    def test_or_fallback_alias_is_passthrough(self):
        # Still resolves; same semantics as the new helper.
        assert display_customer_name_or_fallback("أنهار زبون") == "أنهار زبون"
        assert display_customer_name_or_fallback("") == DEFAULT_FALLBACK_NAME

    def test_short_alias_is_passthrough(self):
        assert display_customer_name("Ahmed") == "Ahmed"
        assert display_customer_name(None) == DEFAULT_FALLBACK_NAME

    def test_aliases_share_identity(self):
        # They point at the same callable so behaviour can never drift.
        assert display_customer_name_or_fallback is display_name_passthrough_or_fallback
        assert display_customer_name is display_name_passthrough_or_fallback


# ── 3) Legacy sanitiser — preserved for the admin tool ────────────────


class TestLegacySanitiser:
    """``sanitize_display_customer_name`` is NO LONGER wired into the
    runtime path. It's kept on the module for callers that still
    want token-aware cleaning (the bulk admin tool re-implements its
    own copy with extra heuristics — phone-only detection, confidence
    scoring — in ``services/customer_name_cleanup.py``)."""

    @pytest.mark.parametrize("raw, expected", [
        ("أنهار زبون",          "أنهار"),
        ("محمد عميل",           "محمد"),
        ("customer Ahmed",      "Ahmed"),
        ("Customer Ahmed",      "Ahmed"),
        ("العميل سامي",         "سامي"),
        ("أبو عميل خالد",       "أبو خالد"),
    ])
    def test_still_strips_commercial_tokens(self, raw, expected):
        assert sanitize_display_customer_name(raw) == expected

    @pytest.mark.parametrize("raw, expected", [
        ("العميل أبو خالد",     "أبو خالد"),
        ("أم محمد",             "أم محمد"),
        ("عبد الرحمن",          "عبد الرحمن"),
        ("آل عايد",             "آل عايد"),
        ("محمد بن سلمان",       "محمد بن سلمان"),
        ("customer أبو فيصل",   "أبو فيصل"),
    ])
    def test_compound_names_intact(self, raw, expected):
        assert sanitize_display_customer_name(raw) == expected

    @pytest.mark.parametrize("raw", [
        None, "", "   ", "عميل", "Customer", "العميل",
        "عميل جديد", "ضيف المتجر", "test", "n/a", "???", "🎉🎁", "A",
    ])
    def test_returns_none_for_unsalvageable(self, raw):
        assert sanitize_display_customer_name(raw) is None

    @pytest.mark.parametrize("raw", [None, 42, 3.14, [], {}, object()])
    def test_non_string_returns_none(self, raw):
        assert sanitize_display_customer_name(raw) is None

    def test_apostrophes_and_hyphens_kept(self):
        assert sanitize_display_customer_name("Al-Sayed") == "Al-Sayed"
        assert sanitize_display_customer_name("D'Angelo") == "D'Angelo"


# ── 4) Default fallback constant ──────────────────────────────────────


class TestFallbackConstant:
    """The static fallback wording is part of the customer-facing copy
    contract — flipping it is a campaign-wide change. Lock it down."""

    def test_constant_value(self):
        assert DEFAULT_FALLBACK_NAME == "عميلنا الغالي"

    def test_warmer_than_old_phrasing(self):
        # May 2026 merchant feedback: "الغالي" > "العزيز".
        assert "الغالي" in DEFAULT_FALLBACK_NAME
        assert "العزيز" not in DEFAULT_FALLBACK_NAME
