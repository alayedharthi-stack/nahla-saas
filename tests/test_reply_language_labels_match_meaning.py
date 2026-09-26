"""The dashboard's language and dialect choices say what the assistant is told.

A merchant picks a reply language and, independently, an Arabic dialect. The
language labels state which language the assistant answers in; the model reads
the platform meaning of the saved language (``tenant_overlay.LANGUAGE_MAP``).
The dialect is its own field, whose saved values are the platform's
(``core.reply_dialect.ARABIC_DIALECTS``), each with a meaning the commerce
runtime hands the model (``ARABIC_DIALECT_MEANING``).

Nothing here chooses for the merchant: which values a store saves stays the
merchant's decision. What is pinned is that the labels they read and the
meanings the model reads agree — no language label claims a dialect (the
dialect field does that), no label calls an option one language "only" when it
also answers the other, and every dialect the page offers is one the platform
defines.
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

from core.reply_dialect import ARABIC_DIALECT_MEANING, ARABIC_DIALECTS  # noqa: E402
from modules.ai.prompts.tenant_overlay import LANGUAGE_MAP  # noqa: E402

PAGE = REPO_ROOT / "dashboard" / "src" / "pages" / "Intelligence.tsx"
SAUDI = "السعودية"


def _select(bound_to: str) -> str:
    source = PAGE.read_text(encoding="utf-8")
    select = re.search(r"value=\{" + re.escape(bound_to) + r"\}.*?</select>", source, re.S)
    assert select, f"the select bound to {bound_to} was not found on the page"
    return select.group(0)


def _language_options() -> Dict[str, str]:
    """``value -> label`` for the reply-language select, read from the page."""
    options = dict(re.findall(r'<option value="([^"]+)">([^<]+)</option>',
                              _select("ai.default_language")))
    assert set(options) == {"arabic", "english", "bilingual"}, options
    return options


def _dialect_option_values() -> tuple:
    """The option values of the Arabic-dialect select, in page order."""
    return tuple(re.findall(r'<option value="([^"]*)">', _select("ai.arabic_dialect ?? ''")))


def test_every_label_names_a_value_the_platform_gives_a_meaning() -> None:
    for value in _language_options():
        assert LANGUAGE_MAP.get(value), value


def test_no_language_label_claims_a_dialect() -> None:
    """The dialect is chosen in its own field; a language label naming one
    would contradict whatever that field says."""
    for value, label in _language_options().items():
        assert SAUDI not in label, (value, label)


def test_no_label_calls_an_option_one_language_only_when_it_answers_in_both() -> None:
    """Both single-language meanings switch for a customer who writes in the
    other language, so neither may be labelled as that language "only"."""
    for value in ("arabic", "english"):
        assert "فقط" not in _language_options()[value]
    assert "الإنجليزية" in LANGUAGE_MAP["arabic"] or "للإنجليزية" in LANGUAGE_MAP["arabic"]
    assert "Arabic" in LANGUAGE_MAP["english"]


def test_the_dialect_select_offers_exactly_the_platforms_dialects_and_not_chosen() -> None:
    assert _dialect_option_values() == ("",) + ARABIC_DIALECTS


def test_every_offered_dialect_has_a_meaning_the_model_is_given() -> None:
    for value in _dialect_option_values():
        if value:
            assert ARABIC_DIALECT_MEANING.get(value), value


def test_the_not_chosen_option_shows_the_effective_value_for_each_language() -> None:
    """Nothing chosen: "arabic" still means Saudi colloquial (its platform
    meaning names it), the other two name no dialect. The empty option's label
    is computed from the language on the page, so the page must branch on it."""
    empty_option = re.search(r'<option value="">(.*?)</option>', _select("ai.arabic_dialect ?? ''"),
                             re.S)
    assert empty_option, "the not-chosen dialect option was not found"
    label = empty_option.group(1)
    assert "ai.default_language === 'arabic'" in label
    assert SAUDI in label
    assert SAUDI in LANGUAGE_MAP["arabic"]
    for value in ("english", "bilingual"):
        assert SAUDI not in LANGUAGE_MAP[value]
