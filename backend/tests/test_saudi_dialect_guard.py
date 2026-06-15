"""P0-A — Saudi dialect guard replaces non-Saudi outbound phrasing."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.postprocess.saudi_dialect_guard import (  # noqa: E402
    apply_saudi_dialect_guard,
)


class TestSaudiDialectGuard:
    @pytest.mark.parametrize(
        "raw,expected_fragment",
        [
            ("شنو المنتج؟", "وش المنتج"),
            ("شنو بالذات تبغى؟", "وش اللي تبحث عنه"),
            ("أرسل عنوانك بتاعك", "عنوانك أو موقعك"),
            ("شلون أطلب؟", "كيف"),
            ("هسة متوفر", "الحين"),
            ("عندنا هواية خيارات", "كثير"),
        ],
    )
    def test_replaces_non_saudi_tokens(self, raw: str, expected_fragment: str) -> None:
        result = apply_saudi_dialect_guard(raw, locale="ar", tenant_id=1)
        assert result.replaced is True
        assert expected_fragment in result.reply

    def test_skips_non_arabic_locale(self) -> None:
        result = apply_saudi_dialect_guard("شنو المنتج؟", locale="en")
        assert result.replaced is False
        assert result.reply == "شنو المنتج؟"

    def test_preserves_clean_saudi_wording(self) -> None:
        clean = "وش المنتج اللي تبغاه؟ أرسل عنوانك أو موقعك"
        result = apply_saudi_dialect_guard(clean, locale="ar")
        assert result.replaced is False
        assert result.reply == clean

    def test_does_not_rewrite_unrelated_reply_body(self) -> None:
        raw = (
            "أبشر، عسل الطلح متوفر بعدة أحجام: ربع كيلo 126 ريال، "
            "ونص كيلo 240 ريال. أي حجم يناسبك؟"
        )
        result = apply_saudi_dialect_guard(raw, locale="ar")
        assert result.replaced is False
        assert result.reply == raw
