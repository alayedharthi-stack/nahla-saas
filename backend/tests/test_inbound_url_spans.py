"""Structural inbound URL-span projection (D3)."""
from __future__ import annotations

from core.inbound_url_spans import (
    extract_inbound_url_spans,
    is_url_only_inbound,
    semantic_text_excluding_url_spans,
    url_matches_inbound_span,
)


class TestInboundUrlSpans:
    def test_https_and_http(self) -> None:
        https = extract_inbound_url_spans("see https://example.net/share/abc now")
        http = extract_inbound_url_spans("see http://example.net/share/abc now")
        assert https == ["https://example.net/share/abc"]
        assert http == ["http://example.net/share/abc"]

    def test_www(self) -> None:
        spans = extract_inbound_url_spans("www.example.net/x وش رأيك؟")
        assert spans == ["www.example.net/x"]
        assert semantic_text_excluding_url_spans("www.example.net/x وش رأيك؟") == "وش رأيك؟"

    def test_schemeless_host_path(self) -> None:
        text = "vt.tiktok.com/test"
        assert extract_inbound_url_spans(text) == ["vt.tiktok.com/test"]
        assert is_url_only_inbound(text) is True

    def test_query_and_fragment(self) -> None:
        url = "https://example.net/p?q=1&b=2#frag"
        assert extract_inbound_url_spans(url) == [url]

    def test_multiple_urls(self) -> None:
        text = "https://a.example/x ثم https://b.example/y"
        spans = extract_inbound_url_spans(text)
        assert spans == ["https://a.example/x", "https://b.example/y"]
        assert semantic_text_excluding_url_spans(text) == "ثم"

    def test_arabic_punctuation_after_url(self) -> None:
        text = "https://example.net/x؟"
        assert extract_inbound_url_spans(text) == ["https://example.net/x"]
        assert semantic_text_excluding_url_spans(text) == ""

    def test_url_inside_parentheses(self) -> None:
        text = "(https://example.net/x)"
        assert extract_inbound_url_spans(text) == ["https://example.net/x"]

    def test_arabic_question_before_and_after(self) -> None:
        text = "وش رأيك؟ https://example.net/x وبعدين"
        assert semantic_text_excluding_url_spans(text) == "وش رأيك؟ وبعدين"

    def test_does_not_eat_adjacent_question(self) -> None:
        text = "https://vt.tiktok.com/test/ وش رأيك؟"
        assert semantic_text_excluding_url_spans(text) == "وش رأيك؟"

    def test_ordinary_words_not_removed(self) -> None:
        text = "عندكم حساب tiktok ولا website؟"
        assert extract_inbound_url_spans(text) == []
        assert semantic_text_excluding_url_spans(text) == text

    def test_url_only(self) -> None:
        assert is_url_only_inbound("https://example.net/share/abc") is True
        assert is_url_only_inbound("وش رابط المتجر؟") is False

    def test_inbound_match_ignores_scheme(self) -> None:
        assert url_matches_inbound_span(
            "https://vt.tiktok.com/test",
            ["vt.tiktok.com/test"],
        )
