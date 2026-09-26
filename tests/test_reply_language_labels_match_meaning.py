"""The dashboard's language labels say what the assistant is actually told.

A merchant picks a reply language from three labels; the assistant receives the
platform meaning of the saved value (``tenant_overlay.LANGUAGE_MAP``), on the
legacy path and in the commerce runtime alike. Before this contract the option
that carries Saudi dialect — and also answers an English-speaking customer in
English — was labelled «عربي فقط», so a merchant who wanted Saudi Arabic *and*
English picked «ثنائي اللغة», whose meaning names no dialect at all.

Nothing here chooses for the merchant: which value a store saves stays the
merchant's decision. What is pinned is only that the label they read and the
meaning the model reads agree on the two facts a merchant chooses by — which
option fixes a dialect, and that each option still answers a customer in the
other language.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.prompts.tenant_overlay import LANGUAGE_MAP  # noqa: E402

PAGE = REPO_ROOT / "dashboard" / "src" / "pages" / "Intelligence.tsx"
SAUDI = "السعودية"


def _language_options() -> Dict[str, str]:
    """``value -> label`` for the reply-language select, read from the page."""
    source = PAGE.read_text(encoding="utf-8")
    select = re.search(r"value=\{ai\.default_language\}.*?</select>", source, re.S)
    assert select, "the reply-language select was not found on the page"
    options = dict(re.findall(r'<option value="([^"]+)">([^<]+)</option>', select.group(0)))
    assert set(options) == {"arabic", "english", "bilingual"}, options
    return options


def test_every_label_names_a_value_the_platform_gives_a_meaning() -> None:
    for value in _language_options():
        assert LANGUAGE_MAP.get(value), value


def test_only_the_option_that_fixes_a_dialect_says_it_does() -> None:
    options = _language_options()
    for value, label in options.items():
        assert (SAUDI in label) == (SAUDI in LANGUAGE_MAP[value]), (value, label)
    assert SAUDI in options["arabic"]


def test_no_label_calls_an_option_one_language_only_when_it_answers_in_both() -> None:
    """Both single-language meanings switch for a customer who writes in the
    other language, so neither may be labelled as that language "only"."""
    for value in ("arabic", "english"):
        assert "فقط" not in _language_options()[value]
    assert "الإنجليزية" in LANGUAGE_MAP["arabic"] or "للإنجليزية" in LANGUAGE_MAP["arabic"]
    assert "Arabic" in LANGUAGE_MAP["english"]
