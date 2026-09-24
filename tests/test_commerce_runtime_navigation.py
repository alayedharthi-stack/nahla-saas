"""The parts of paging that need no database, and the line it must not cross.

The durable half — single use, expiry, scope, races, cleanup — is proven against
real PostgreSQL in ``tests/commerce_reliability/test_commerce_runtime_navigation_pg.py``,
because it is the database that makes those guarantees. What is here is
everything a store cannot help with: the two row namespaces staying disjoint,
and a paged list refusing to exist unless every part of it is real.

Merchant-agnostic: products are opaque integers, and rotating generic categories
where a title is needed at all.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.commerce_runtime import agent_contracts as ac  # noqa: E402
from core.commerce_runtime import navigation as nav  # noqa: E402
from core.commerce_runtime import navigation_models as nm  # noqa: E402
from core.commerce_runtime import reply_choices as rc  # noqa: E402

TITLES = ("قميص قطني أزرق", "حذاء رياضي أبيض", "عطر ورد 100ml", "حزام جلد بني",
          "ساعة يد كلاسيكية", "نظارة شمسية", "حقيبة كتف", "وشاح صوف", "قبعة صيفية",
          "معطف خفيف", "سترة رياضية", "جوارب قطنية")


def product(product_id: int) -> Dict[str, Any]:
    return {"product_id": product_id, "title": TITLES[product_id % len(TITLES)],
            "price": "100.00", "currency": "SAR",
            "evidence_ref": rc.product_ref(product_id)}


def observed(product_ids: Sequence[int]) -> List[ac.ToolObservation]:
    rows = [product(i) for i in product_ids]
    return [ac.ToolObservation(call_id="m1", tool_name="search_products", ok=True,
                               result={"status": "ok", "found": True, "products": rows},
                               error_code=None, error=None,
                               evidence_refs=tuple(rc.product_ref(i) for i in product_ids))]


def draft(product_ids: Sequence[int], *, more_label: str = "المزيد",
          button: str = "اختر") -> ac.ReplyDraft:
    request: Dict[str, Any] = {"product_ids": list(product_ids), "button": button}
    if more_label:
        request["more_label"] = more_label
    return ac.ReplyDraft(text="عندنا أكثر من خيار:",
                         evidence_refs=tuple(rc.product_ref(i) for i in product_ids),
                         claims_commerce_facts=True, payload={rc.REQUESTED_KEY: request})


class FakePage:
    """What ``navigation.open_browse`` hands back, without a database."""

    def __init__(self, product_ids: Sequence[int], next_token: str) -> None:
        self.product_ids = tuple(product_ids)
        self.next_token = next_token

    @property
    def has_next(self) -> bool:
        return bool(self.next_token)


# ── The two namespaces are disjoint ──────────────────────────────────────────

def test_a_page_token_is_never_read_as_a_product() -> None:
    token = nav.new_token()
    assert rc.product_id_from_row_id(nav.row_id(token)) is None


def test_a_product_row_is_never_read_as_a_page_token() -> None:
    assert nav.token_from_row_id(rc.row_id(7)) is None


def test_neither_resolver_is_fooled_by_a_bare_prefix_or_junk() -> None:
    for junk in ("", "   ", nm.NAVIGATION_ROW_PREFIX, "nahla:more", "nahla:choice:", "42"):
        assert nav.token_from_row_id(junk) is None, junk


def test_a_token_carries_no_catalogue_identity() -> None:
    """Nothing about a token says which products it will show, so nothing can
    be inferred from it and nothing forged by editing it."""
    token = nav.new_token()
    assert len(token) >= 24 and nav.new_token() != token


# ── A paged list is all-or-nothing ───────────────────────────────────────────

def test_nine_products_and_one_affordance_when_a_page_follows() -> None:
    ids = list(range(1, 21))
    page = FakePage(ids[:9], "tok-2")
    chosen, reason = rc.selection(draft(ids), observed(ids), page=page)
    assert reason == rc.PAGED and chosen is not None
    rows = list(chosen.rows)
    assert len(rows) == nm.MAX_ROWS
    assert [r["id"] for r in rows[:9]] == [rc.row_id(i) for i in ids[:9]]
    assert rows[-1] == {"id": nav.row_id("tok-2"), "title": "المزيد"}
    # The affordance is not a product: it is not among the selection's ids, and
    # it carries none of the merchant's values.
    assert chosen.product_ids == tuple(ids[:9])
    assert set(rows[-1]) == {"id", "title"}


def test_without_a_word_for_the_affordance_there_is_no_paging() -> None:
    """The same rule the card's button follows: customer-facing wording is the
    model's, and the platform invents none. Without it the whole selector is
    withheld and every option is carried as a line — today's behaviour."""
    ids = list(range(1, 21))
    _chosen, reason = rc.selection(draft(ids, more_label=""), observed(ids),
                                   page=FakePage(ids[:9], "tok-2"))
    assert reason == rc.TOO_MANY


def test_without_a_stored_continuation_there_is_no_paging() -> None:
    ids = list(range(1, 21))
    for page in (None, FakePage(ids[:9], "")):
        _chosen, reason = rc.selection(draft(ids), observed(ids), page=page)
        assert reason == rc.TOO_MANY


def test_a_page_naming_a_product_this_turn_did_not_read_is_refused() -> None:
    """Nothing partial is shown and nothing is guessed."""
    ids = list(range(1, 21))
    stale = FakePage([*ids[:8], 999], "tok-2")
    _chosen, reason = rc.selection(draft(ids), observed(ids), page=stale)
    assert reason == rc.TOO_MANY


def test_a_list_that_fits_is_never_paged() -> None:
    ids = list(range(1, 6))
    chosen, reason = rc.selection(draft(ids), observed(ids), page=FakePage(ids, "tok-2"))
    assert reason == rc.OFFERED and chosen is not None
    assert [r["id"] for r in chosen.rows] == [rc.row_id(i) for i in ids]
    assert not any(str(r["id"]).startswith(nm.NAVIGATION_ROW_PREFIX) for r in chosen.rows)


def test_the_withheld_options_still_reach_the_customer_as_lines() -> None:
    """Whatever stops paging, no option the model meant to offer is lost."""
    ids = list(range(1, 21))
    final, reason = rc.finalize(draft(ids, more_label=""), observed(ids))
    assert reason == rc.TOO_MANY and rc.payload_rows(final.payload)[0] == []
    for i in ids:
        assert product(i)["title"] in final.text


# ── The ordering the store is handed ─────────────────────────────────────────

def test_the_browse_order_is_the_callers_and_is_never_re_sorted() -> None:
    assert nav._clean_ids([9, 3, 7, 3, 1]) == (9, 3, 7, 1)


def test_unusable_identities_are_dropped_rather_than_guessed() -> None:
    assert nav._clean_ids([1, 0, -4, None, "x", True, 2.0, 2]) == (1, 2)


def test_a_more_label_longer_than_the_row_renders_is_bounded() -> None:
    long_word = "م" * 80
    bounded = rc.requested_more_label(draft([1, 2], more_label=long_word))
    assert len(bounded) == rc.MAX_ROW_TITLE
