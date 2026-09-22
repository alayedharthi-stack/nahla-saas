"""Regression contracts for notification links into the conversations inbox."""
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONVERSATIONS_PAGE = REPO_ROOT / "dashboard" / "src" / "pages" / "Conversations.tsx"


def _deep_link_search_source() -> str:
    source = CONVERSATIONS_PAGE.read_text(encoding="utf-8")
    start = source.index("async function fetchOutlineForPhoneMaybe(")
    end = source.index("const appendNextPage = async", start)
    return source[start:end]


def test_notification_deep_link_searches_all_unfiltered_pages():
    source = _deep_link_search_source()

    assert "while (!signal.aborted)" in source
    assert "featureRealityApi.conversations({ signal, limit: 200, offset })" in source
    assert "phonesMatch(c.phone, phoneGuess)" in source
    assert "if (!res.has_more || res.conversations.length === 0) return null" in source
    assert "offset += res.conversations.length" in source


def test_superseded_deep_link_cannot_write_stale_inbox_state():
    source = CONVERSATIONS_PAGE.read_text(encoding="utf-8")
    supplemental = source.index("const supplemental = await fetchOutlineForPhoneMaybe(")
    stale_guard = source.index(
        "if (gen !== listReqGen.current || signal.aborted) return",
        supplemental,
    )
    state_write = source.index("const rowsSnap = rows", supplemental)

    assert supplemental < stale_guard < state_write
